# Reliability Lab

A small Python service for practicing the reliability work around an API: start
it, collect request observations, compare two cohorts, inject a short outage, and
verify a SQLite backup. The service uses the Python standard library and SQLite.

## Start the API

From PowerShell in the repository root, install with Python 3.12 and start the
service:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
reliability-lab serve --database data\reliability.db
```

The service listens on `127.0.0.1:8000`. It creates the database directory and
applies its schema when it starts. In another PowerShell window, create and
read an order:

```powershell
$order = @{ customer = "München Bakery"; amount_cents = 4250 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/orders `
  -ContentType application/json -Body $order
Invoke-RestMethod -Method Get -Uri http://127.0.0.1:8000/api/orders
```

`POST /api/orders` accepts exactly `customer` and positive integer
`amount_cents`. The customer must be nonblank and at most 120 characters; the
stored name is trimmed. Amounts range from 1 to 1,000,000,000 cents. Invalid
requests return HTTP 400, bodies over 16 KiB return 413, and SQLite errors
return 503. `GET /api/orders` returns the latest 50 orders. `GET /healthz` is a
liveness check; `GET /readyz` checks the database and schema. The order API has
no user authentication or TLS, so keep it on loopback or a trusted private
network. See [security notes](docs/security.md).

## Observe requests

`GET /metrics` exposes Prometheus metrics for requests to `/api/orders` only.
Health, readiness, operator, metrics, and unknown paths are not business
observations. The labels are bounded to the fixed route, method group, and
status class.

The Prometheus rules use an example policy of 99% availability and 250 ms p95
latency. For availability, 5xx and other statuses are errors while 4xx responses
remain in the denominator. These are policy values for the lab; they are not
measured SLO results.

The `load` command sends GET requests and writes timestamp, cohort, latency, and
status observations to a new CSV file. A network failure is recorded as status
`0`; redirects remain their original 3xx response. For a local walkthrough,
start a second copy in another PowerShell window on port 8001:

```powershell
reliability-lab serve --database data\candidate.db --port 8001
```

For this local comparison, use the two local endpoints:

```powershell
$stableUrl = "http://127.0.0.1:8000/api/orders"
$candidateUrl = "http://127.0.0.1:8001/api/orders"
reliability-lab load --url $stableUrl --cohort stable `
  --requests 100 --concurrency 5 --output evidence\stable.csv
reliability-lab load --url $candidateUrl --cohort candidate `
  --requests 100 --concurrency 5 --output evidence\candidate.csv
```

The cohort is a label supplied by the caller, not discovered from the URL or
response. Point each command at the corresponding stable or candidate service.
Combine the observations and compare them offline:

```powershell
@'
import csv
from pathlib import Path

fields = ("timestamp", "cohort", "latency_ms", "status")
with (
    Path("evidence/stable.csv").open(encoding="utf-8", newline="") as stable,
    Path("evidence/candidate.csv").open(encoding="utf-8", newline="") as candidate,
    Path("evidence/combined.csv").open("x", encoding="utf-8", newline="") as output,
):
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(csv.DictReader(stable))
    writer.writerows(csv.DictReader(candidate))
'@ | python -
reliability-lab analyze-canary --input evidence\combined.csv --min-samples 20
```

The analyzer needs both cohorts and at least 20 observations in each by default.
It counts network failures, 1xx, and 5xx responses as errors; 4xx responses are
served responses. Its default policy caps candidate errors at 1% and candidate
p95 at 1.25 times stable p95. Exit code `0` means pass, `1` means policy failure
or invalid evidence, and `2` means inconclusive because a cohort has too few
samples. The result only describes the rows and policy you supplied; it does
not route traffic or deploy a release.

## Inject a fault

Fault injection needs `RELIABILITY_OPERATOR_TOKEN` set when the service starts.
Stop the running server with Ctrl+C, set the token in that PowerShell window
from your local secret store, then restart `reliability-lab serve` with the same
database and port. Set the same token separately in the PowerShell window that
runs the drill. The operator routes are disabled if the service starts without
it; the token must contain at least 32 visible ASCII characters. Keep it in a
local secret store rather than a command argument. Then run the drill with a
fresh output path:

```powershell
reliability-lab fault-drill --url http://127.0.0.1:8000/api/orders `
  --requests 20 --concurrency 2 --ttl-seconds 5 --output evidence\fault.json
```

The drill requests a temporary `unavailable` fault, records the resulting
observations, clears it in a `finally` path, and checks that requests recover.
The accepted TTL is 1–30 seconds. If the clear request fails, the server-side
TTL expires the fault. Fault state belongs to one process; in Kubernetes, target
a Pod directly when you want to measure that instance. The [operations runbook](docs/runbook.md)
covers deployment setup and this single-instance procedure.

## Back up and restore

The backup command uses SQLite's online backup API and writes a JSON manifest
beside the database by default. Restore verifies the manifest's database-file
hash, SQLite integrity, record count, and canonical record checksum before and
after restoring. Both commands refuse to overwrite existing output files.

```powershell
reliability-lab backup --database data\reliability.db `
  --output evidence\orders-backup.db
reliability-lab restore --backup evidence\orders-backup.db `
  --output evidence\orders-restored.db
reliability-lab recovery-drill --output-dir evidence
```

Keep the backup manifest with its database. The recovery drill creates a sample
order and saves its backup, restored database, manifest, and report in a new
`recovery-drill-<id>` directory. Its timings describe that local run; they do
not establish an RTO/RPO or validate cloud storage or failover.

## Deployment examples

`docker compose up --build` starts the app, Prometheus, and Grafana; set
`GRAFANA_ADMIN_PASSWORD` first. The Kubernetes example in
`kubernetes/reliability-lab.yaml` uses Argo Rollouts and Prometheus analysis for
stable/candidate traffic. The sample shares a `ReadWriteOnce` SQLite volume and
is intended for a single-node lab, not a multi-node database or high-availability
setup. The deployment steps and prerequisites are in the [operations runbook](docs/runbook.md).

The repository's CI runs Ruff, Bandit, pip-audit, Python compilation, and
Prometheus configuration and rule syntax checks. These static checks do not
exercise the service or establish runtime behavior for Docker, Kubernetes, or
Argo Rollouts. There is no observed cluster rollout for this example.
The [incident report template](docs/incident-report-template.md) is blank, not
an account of a measured incident.
