"""Append-only structured audit log for chassis-mutating operations.

Per `SECURITY.md` and the project's "snapshot first, change second"
principle, every mutating call against the chassis MUST leave an
auditable trace. This module exists so callers can record one record
per operation without thinking about file handling, redaction, or
serialisation.

## Format

NDJSON (one JSON object per line). Each record::

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

Field reference:

- `ts`            ISO-8601 UTC, seconds precision (`YYYY-MM-DDTHH:MM:SSZ`).
- `actor`         Who initiated. Defaults to `"<user>@<host>"`. Override
                  with `actor=` arg or `VESSEL_AUDIT_ACTOR` env.
- `target.kind`   `"chassis"` | `"switch"` | `"blade"` | `"hmm"` |
                  `"fan"` | `"psu"`
- `target.id`     Stable identifier within kind. `"slot3"`, `"swi2"`,
                  `"hmm.dc1.example.com"`.
- `op`            Dotted verb. `"power.cycle"`, `"vlan.add"`,
                  `"firmware.apply"`, `"snapshot.create"`, etc.
- `snapshot_id`   ID of the pre-change snapshot. `null` only for
                  read-only ops or for snapshot.create itself.
- `result`        `"success"` | `"rollback"` | `"failed"` |
                  `"dry-run"` | `"in-progress"`.
- `evidence`      Free-form dict with redaction applied. Common keys:
                  `before`, `after`, `command`, `error`, `bytes`.

## Configuration

    VESSEL_AUDIT_LOG    Path to the audit log file. Default: ./audit.log
                        Recommended: append-only / WORM storage in prod.
    VESSEL_AUDIT_ACTOR  Override the auto-detected `actor` field.

## Failure mode

If the audit file cannot be written (permission denied, disk full,
parent dir missing), the call returns False and logs a warning to
stderr — **but does not raise**. Audit failure is never allowed to
abort the primary operation, because preventing the operation does
nothing to protect chassis state. Operators should monitor the
warning logs separately.
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

# Serialise concurrent writes from multiple threads — uvicorn workers,
# the heartbeat thread, etc. would otherwise interleave bytes.
_write_lock = threading.Lock()

TargetKind = Literal["chassis", "switch", "blade", "hmm", "fan", "psu"]
Result = Literal["success", "rollback", "failed", "dry-run", "in-progress"]

# Substrings in evidence keys that trigger value redaction. Case-insensitive.
_REDACT_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "private_key",
    "auth",
    "credential",
    "cookie",
    "bearer",
)
_REDACTED = "***"


def _audit_log_path() -> Path:
    return Path(os.environ.get("VESSEL_AUDIT_LOG", "audit.log"))


def _default_actor() -> str:
    if env := os.environ.get("VESSEL_AUDIT_ACTOR", "").strip():
        return env
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    return f"{user}@{host}"


def _redact(value: Any) -> Any:
    """Recursively redact credential-shaped values in evidence dicts.

    Detection is conservative: we redact when a key name *contains* any
    of the hints (case-insensitive). False negatives are possible —
    a value held under a creative key name like `seed` won't be
    touched. Callers handling raw chassis credentials should
    pre-redact before passing into evidence.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and any(h in k.lower() for h in _REDACT_KEY_HINTS):
                out[k] = _REDACTED
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def log_op(
    *,
    op: str,
    target_kind: TargetKind,
    target_id: str,
    result: Result,
    snapshot_id: str | None = None,
    evidence: dict[str, Any] | None = None,
    actor: str | None = None,
) -> bool:
    """Append one audit record. Returns True on success, False on
    failure to write (never raises)."""
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": actor or _default_actor(),
        "target": {"kind": target_kind, "id": target_id},
        "op": op,
        "snapshot_id": snapshot_id,
        "result": result,
        "evidence": _redact(evidence or {}),
    }
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    path = _audit_log_path()
    try:
        # Make parent dir on first write so deployments don't have to
        # pre-create it. Fails-loud only if the path itself is bad.
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock, path.open("a", encoding="utf-8") as f:
            f.write(line)
        return True
    except OSError as exc:
        log.warning("audit write failed (%s): %s", path, exc)
        return False


def read_records(
    *,
    path: Path | str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read audit records back. For tooling / inspection; not the
    primary write path. Skips malformed lines silently (operator can
    still grep the raw file)."""
    p = Path(path) if path else _audit_log_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                log.debug("skipping malformed audit line: %s", line[:80])
                continue
    if limit is not None:
        return out[-limit:]
    return out


__all__ = [
    "log_op",
    "read_records",
    "TargetKind",
    "Result",
]
