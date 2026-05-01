# Audit log

Every chassis-mutating operation appends an NDJSON record to
`$VESSEL_AUDIT_LOG` (default `./audit.log`). This is non-negotiable —
the project rule from `SECURITY.md` is "snapshot first, change second,
record always."

## Format

One JSON object per line. Sample:

```json
{
  "ts": "2026-05-01T20:35:42Z",
  "actor": "marcos@workstation",
  "target": {"kind": "blade", "id": "slot3"},
  "op": "power.cycle",
  "snapshot_id": "snap-2026-05-01T20-35-12Z",
  "result": "success",
  "evidence": {
    "before": {"power_state": "On"},
    "after": {"power_state": "Off"},
    "command": "ipmcset -d powerstate -v 4"
  }
}
```

## Field reference

| Field | Type | Notes |
|---|---|---|
| `ts` | string | ISO-8601 UTC seconds (`YYYY-MM-DDTHH:MM:SSZ`) |
| `actor` | string | `<user>@<host>` by default; override via `VESSEL_AUDIT_ACTOR` |
| `target.kind` | enum | `chassis` / `switch` / `blade` / `hmm` / `fan` / `psu` |
| `target.id` | string | Stable ID (`slot3`, `swi2`, etc.) |
| `op` | string | Dotted verb (`power.cycle`, `vlan.add`, …) |
| `snapshot_id` | string \| null | Pre-change snapshot ID |
| `result` | enum | `success` / `rollback` / `failed` / `dry-run` / `in-progress` |
| `evidence` | object | Free-form context. Credential keys auto-redacted. |

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `VESSEL_AUDIT_LOG` | `./audit.log` | Path. Use append-only / WORM in production. |
| `VESSEL_AUDIT_ACTOR` | `<user>@<host>` | Override actor for CI / ephemeral identity. |

## Redaction

Evidence values are recursively redacted when their key contains
(case-insensitive) any of: `password`, `passwd`, `secret`, `token`,
`apikey`, `api_key`, `private_key`, `auth`, `credential`, `cookie`,
`bearer`. Redacted values become `"***"`.

## Read records

```python
from hmm_client import audit
records = audit.read_records(limit=100)   # tail
```

## Failure mode

If the audit file can't be written (disk full, permission denied),
`log_op()` returns `False` and logs a warning to stderr — but **never
raises**. Aborting the primary operation because audit failed would
leave chassis state half-changed; the right answer is loud monitoring
on the warning logs instead.
