# Health checks

Two endpoints for orchestrators:

| Endpoint | Status | Purpose |
|---|---|---|
| `GET /healthz` | Always 200 | **Liveness**. Fails only when the process can't serve HTTP. Restart on failure. |
| `GET /readyz` | 200 / 503 | **Readiness**. Chassis-touching ops would plausibly succeed. Remove from rotation on failure (don't restart). |

Both bypass [GUI auth](gui-auth.md) — load balancers and uptime probes
don't carry credentials, and the endpoints reveal nothing more
actionable than reachability.

## /healthz

```json
{"status": "ok", "service": "vessel"}
```

Makes no chassis network calls. Safe to poll from a Kubernetes
liveness probe at any frequency.

## /readyz

Successful response:

```json
{"status": "ready", "checks": ["hmm-tcp", "audit-log"]}
```

Failure response (HTTP 503):

```json
{"status": "not-ready", "failing": ["hmm-tcp:ConnectionRefusedError"]}
```

Checks performed:

1. **`hmm-tcp`** — TCP connect to `HMM_HOST:443` with 2s timeout
2. **`audit-log`** — write a `readyz.probe` record to the audit log

Each `/readyz` hit appends one audit record (the probe). This is
intentional — it doubles as a heartbeat in the audit stream.

## Kubernetes example

```yaml
readinessProbe:
  httpGet:
    path: /readyz
    port: 8765
  periodSeconds: 30
  failureThreshold: 3

livenessProbe:
  httpGet:
    path: /healthz
    port: 8765
  periodSeconds: 10
  failureThreshold: 6
```
