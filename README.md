# Reliability Lab

Reliability Lab is a local Python 3.12 service for practicing service-level
measurement, canary decisions, controlled fault injection, and SQLite recovery.
It stores customer orders in SQLite and reports measurements observed by the
commands in this repository. The example objectives are policy inputs; running
the project does not establish a production SLO or a 99.9% availability claim.

## Start locally

From the repository root with Python 3.12 installed:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
reliability-lab serve --database data\reliability.db
```

The service listens on `127.0.0.1:8000` by default. `RELIABILITY_HOST`,
`RELIABILITY_PORT`, and `RELIABILITY_DB_PATH` configure the listener and default
database path. The server creates the database directory and applies schema
migration version 1 at startup. To set a different port, pass `--port` to
`serve`; `--host` sets the bind address.

This is a reliability lab, not an authenticated order service. Business routes
have no user authentication or TLS. Keep the listener on loopback or behind a
trusted private network; do not expose it to the public internet. See
[security notes](docs/security.md).

Create and read an order from another PowerShell window:

```powershell
$order = @{ customer = "München Bakery"; amount_cents = 4250 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/orders `
  -ContentType application/json -Body $order
Invoke-RestMethod -Method Get -Uri http://127.0.0.1:8000/api/orders
```

`POST /api/orders` accepts exactly `customer` and positive `amount_cents` fields.
Customer names are trimmed and limited to 120 characters; amounts are limited
to 1 through 1,000,000,000 cents. `GET /api/orders` returns the most recent 50
orders. Invalid JSON or fields return HTTP 400; request bodies above 16 KiB
return HTTP 413; SQLite errors return HTTP 503. Health checks are `GET /healthz`
for process liveness and `GET /readyz` for database and migration readiness.

## Observe and compare

Only `/api/orders` contributes to business request metrics. Health, readiness,
metrics, operator, and unknown paths are excluded. The metric labels are bounded
to the fixed route `/api/orders`, method `GET`/`POST`/`OTHER`, and status class
`2xx`/`3xx`/`4xx`/`5xx`/`other`. The Prometheus exposition is at `GET /metrics`.
The configured policy treats 5xx and other statuses as availability errors;
4xx responses count as served requests. Its example objectives are 99%
availability and 250 ms latency.

For example, capture one cohort's observations (network errors are recorded with
status `0`, redirects are recorded as their original 3xx response, and each
output path must be new):

```powershell
reliability-lab load --url http://127.0.0.1:8000/api/orders `
  --cohort stable --requests 100 --concurrency 5 --output evidence\stable.csv
```

Run a second load command with `--cohort candidate` and a different output
file. Point each command at a URL that actually reaches the named cohort; the
cohort is a caller-supplied label and is not inferred from the response. Combine
the resulting CSV observations into one file with this standard library
snippet, then run the offline policy analyzer:

```python
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
```

```powershell
reliability-lab analyze-canary --input evidence\combined.csv --min-samples 20
```

The analyzer rejects missing, malformed, oversized, empty, or one-cohort
evidence. It reports `inconclusive` until both cohorts meet the minimum sample
count, and exits with code 2 for that decision. A failed policy exits 1; a pass
exits 0. Defaults are a candidate error-rate ceiling of 1% and a p95 ceiling of
1.25 times the stable p95. The analyzer only evaluates the supplied rows; it
does not deploy, route, or roll back a release.

`docker compose up --build` starts the app, Prometheus, and Grafana. Set
`GRAFANA_ADMIN_PASSWORD` before starting Compose. The app, Prometheus, and
Grafana ports are bound to loopback. The example images are pinned in
`compose.yaml`. The local Docker, Prometheus, and Grafana services have not been
executed as part of this repository setup when Docker is unavailable.

## Fault drill and recovery

Fault injection is disabled unless `RELIABILITY_OPERATOR_TOKEN` is set to at
least 32 visible ASCII characters. Configure the same secret in the service
and the drill process through a local secret store or environment. The token is
not a CLI argument and is not logged. Authenticated `POST /operator/fault`
accepts only `{"kind":"unavailable","ttl_seconds":N}` for 1–30 seconds;
`POST /operator/clear` removes an active fault. The fault affects only business
order requests. In a multi-pod deployment the fault is process-local; target a
single instance (for example, by port-forwarding its Pod) if the purpose is to
measure that instance without service-level balancing. Run a drill with:

```powershell
reliability-lab fault-drill --url http://127.0.0.1:8000/api/orders `
  --requests 20 --concurrency 2 --ttl-seconds 5 --output evidence\fault.json
```

The command captures status and latency observations, clears the fault in a
`finally` path, then checks that requests succeed. It exits nonzero if injection,
cleanup, injected failures, or recovery verification fail. If cleanup itself
fails, the service-side TTL remains the upper bound on the injected fault.

Create a consistent SQLite online backup and restore it to a new path:

```powershell
reliability-lab backup --database data\reliability.db --output evidence\orders-backup.db
reliability-lab restore --backup evidence\orders-backup.db --output evidence\orders-restored.db
```

Each backup has a separate JSON manifest by default. Backup and restore refuse
to overwrite output files. The restore verifies the manifest's physical file
hash, SQLite integrity, record count, and canonical record checksum before and
after restore. `reliability-lab recovery-drill` creates a fresh sample order,
backs it up, restores to a separate file, and writes evidence under
`evidence/recovery-drill-<id>/`. Its elapsed times are local observations, not
contractual RTO/RPO values; it does not validate cloud storage or failover.

## Deployment and validation

The Argo Rollouts manifests in `kubernetes/reliability-lab.yaml` define stable
and candidate Services plus an `AnalysisTemplate`. Failed candidate sample,
error-rate, or p95 checks fail the rollout analysis, allowing the Argo Rollouts
controller to abort and return traffic to the stable ReplicaSet. Candidate
metrics are separated by the `track` label discovered by
`prometheus/prometheus-kubernetes.yml`. The analysis needs real candidate
traffic; insufficient samples fail closed. See [the deployment runbook](docs/runbook.md)
for setup requirements and recovery steps.

The Kubernetes example uses one shared ReadWriteOnce SQLite volume. It is a
single-node learning setup and is not a highly available multi-node database
deployment. Configure an appropriate local/default StorageClass and build or
load `reliability-lab:local` into the target cluster before applying the
Rollout. Install the Argo Rollouts controller and CRDs separately.

CI runs Ruff, Bandit, pip-audit, Python compilation, and Prometheus configuration
and rule syntax checks. It does not run a Python behavioral test suite. Local
Docker/cluster execution requires those services and controllers to be
available; static checks do not validate runtime networking, scheduling, or
rollout behavior. See [the incident report template](docs/incident-report-template.md)
for a blank record format, not observed incident evidence.

Run the Python checks locally after installing the pinned development tools:

```powershell
python -m pip install -e ".[dev]"
ruff check src
bandit --quiet --recursive src
pip-audit --skip-editable
python -m compileall -q src
```
