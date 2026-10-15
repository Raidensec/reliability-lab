# Operations runbook

## Start and check the service

Start with `reliability-lab serve --database data/reliability.db`. The default
bind address is loopback `127.0.0.1:8000`. For a container, set
`RELIABILITY_HOST=0.0.0.0` and `RELIABILITY_DB_PATH=/data/reliability.db`.

- `GET /healthz` reports process liveness; it does not query SQLite.
- `GET /readyz` opens SQLite, verifies migration version 1, and checks the
  orders table. A database error or missing migration returns HTTP 503.
- `GET /metrics` exposes Prometheus text format. It is not included in business
  request metrics.
- `GET /api/orders` lists up to 50 latest orders.
- `POST /api/orders` creates one order; validate input at the API boundary.

For storage alerts, inspect the application logs for the fixed messages
`Order query failed`, `Order persistence failed`, or `Readiness database check
failed`. Logs intentionally omit request bodies and tokens.

## SLI and alert policy

The Prometheus rules in `prometheus/rules.yml` use the following example policy:

- Availability objective: 99% of measured business responses are not 5xx or
  `other`. The denominator includes all `/api/orders` responses, including 4xx.
- Latency objective: no more than 5% of business response observations exceed
  250 ms (p95 target at 250 ms).
- Fast burn alerts require both 5-minute and 1-hour error-budget burn over
  14.4x; slow burn alerts require both 30-minute and 6-hour burn over 6x.
- `ReliabilityLabNoBusinessTraffic` alerts if there are no business observations
  for five minutes. No traffic is unknown, not a healthy SLI.

These ratios have no minimum traffic threshold beyond requiring a positive rate
in each alert window. Treat low-volume signals with that limitation in mind.
Health, readiness, operator, and metrics requests do not enter the SLI.

## Fault injection

Set the same strong `RELIABILITY_OPERATOR_TOKEN` in the service and the client
running `fault-drill`. It must be at least 32 visible ASCII characters. The
operator routes are disabled when the service has no token. Use the CLI drill
to trigger a time-limited 503 fault; its duration is capped at 30 seconds, and
the CLI clears it in a `finally` path. If the client exits or loses access
before clearing, the server-side expiry clears it automatically. A failed clear
is recorded as a failed drill and should be followed by a readiness and order
request check. Fault state is held in one process, so service-level traffic may
reach unaffected replicas. To target one replica in Kubernetes, use a port
forward to that Pod rather than the balancing Service.

The order API has no user authentication or TLS. Keep all endpoints on loopback
or a trusted private network; do not publish the service or operator routes to
the public internet.

## Backup and restore

Before moving a backup, preserve its `.manifest.json` file beside it. `backup`
uses SQLite's online backup API, then records both the database file hash and a
deterministic order-record checksum. `restore` verifies the manifest and backup
integrity before creating a new database, then checks record count and checksum.
It refuses to overwrite an existing destination. Keep the source database
available until the restored record checks are reviewed.

For a disposable end-to-end local recovery exercise, run
`reliability-lab recovery-drill --output-dir evidence`. Inspect the emitted
`recovery-drill.json` and the backup manifest before treating the drill as
complete. The elapsed times describe that local run only; this workflow does
not establish an RTO/RPO commitment or test cloud backups.

## Argo Rollouts canary

Install a compatible Argo Rollouts controller and CRDs, build or load the app
image, deploy the Prometheus configuration, then apply the manifests as shown
below. The `AnalysisTemplate` checks candidate sample count, 5-minute server
error rate, and 5-minute p95 latency. Each measurement runs once per minute for
three measurements: at least 20 candidate responses in five minutes, no more
than 1% 5xx/other responses, and p95 no higher than 250 ms. Any failed
measurement fails analysis. The Rollout waits
five minutes after each weight change before analysis. This gives Prometheus
time to scrape candidate pods, but the rollout still requires actual candidate
traffic. If traffic is absent or the candidate violates the configured policy,
analysis fails and Argo Rollouts aborts the candidate.

The Kubernetes configmap is built from the checked-in configuration so the
static and cluster scrape configs do not drift from their source files:

```powershell
kubectl create namespace reliability-lab --dry-run=client -o yaml |
  kubectl apply -f -
kubectl create configmap reliability-prometheus-config `
  --from-file=prometheus.yml=prometheus\prometheus-kubernetes.yml `
  --from-file=rules.yml=prometheus\rules.yml `
  --namespace reliability-lab --dry-run=client -o yaml |
  kubectl apply -f -
kubectl apply -f kubernetes\prometheus.yaml
kubectl apply -f kubernetes\reliability-lab.yaml
```

This order creates the namespace and Prometheus before the Rollout begins
analysis. For a registry image, replace `reliability-lab:local` with its
immutable image reference and change `imagePullPolicy: Never` to
`IfNotPresent` or `Always` in `kubernetes/reliability-lab.yaml`. Keep `Never`
only when the image has been loaded into every target node. A single-node local
Kubernetes cluster can load a local Docker image; loading commands vary by
cluster provider. Argo Rollouts
installation is provider-specific; follow the [official installation guide](https://argo-rollouts.readthedocs.io/en/stable/installation/).

Watch the deployment with:

```powershell
kubectl argo rollouts get rollout reliability-lab --namespace reliability-lab --watch
```

If the rollout is aborted, inspect `kubectl describe rollout reliability-lab
-n reliability-lab`, the AnalysisRun objects, and Prometheus query results for
`track="candidate"`. Correct the measured failure or traffic issue, then deploy
a new image. Do not manually promote a candidate that has failed analysis.

The example uses a shared `ReadWriteOnce` PVC for every app replica. Use it only
on a single-node lab with a compatible volume provisioner. SQLite on this shared
PVC is not a multi-node HA design. Prometheus uses an ephemeral `emptyDir` TSDB
in the manifest, so its history is lost if that pod is recreated.

## Evidence and incident notes

Load CSVs, recovery reports, and fault-drill reports are created exclusively;
choose a new output path for each run. The `.gitignore` excludes local evidence
and database files. Keep generated artifacts out of Git unless they are
anonymized and intentionally reviewed. The [incident template](incident-report-template.md)
is blank and is not evidence from a real incident.
