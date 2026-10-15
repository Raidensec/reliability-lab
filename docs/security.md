# Security notes

Reliability Lab is a local learning application. The order API has no user
authentication, authorization, TLS, or tenant separation. Keep it bound to
loopback or within a trusted private network. Do not expose it directly to the
public internet.

Fault injection is disabled unless `RELIABILITY_OPERATOR_TOKEN` is configured.
The service requires at least 32 visible ASCII characters and compares the
Bearer token using a constant-time comparison. Configure the same secret in the
service and drill client through a secret store; never pass it as a command-line
argument or commit it. The CLI and HTTP server do not log request payloads or
authorization headers.

The Docker image runs as UID 10001. The Compose app container adds a read-only
root filesystem, drops Linux capabilities, and mounts a writable data volume.
The Kubernetes example sets a restricted Pod Security namespace, non-root UID,
read-only root filesystem, seccomp `RuntimeDefault`, and no added capabilities.
Its Prometheus role can only get, list, and watch pods in the `reliability-lab`
namespace.

The Kubernetes sample is not a production security or availability design. It
uses plaintext in-cluster HTTP and shared SQLite on a ReadWriteOnce PVC. Use an
isolated lab cluster and do not put sensitive customer data in the sample.
