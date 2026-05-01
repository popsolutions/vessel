"""Drift detection (phase 1 of restore).

Compares a snapshot's Redfish files against current live state and reports
what changed. **Does NOT PATCH anything** - apply-mode is a deliberate
follow-up once drift output has been validated against a known change.

Switch-config restore (`swi<N>.tar.gz`) is **not implemented**: the SMM
firmware (v7.63) has no `swiconfimport` companion. Tracked as a separate
issue (depends on direct VRP CLI access via Phase 3).

Usage:
    python -m hmm_client.restore snapshots/<UTC-stamp>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .config import Settings
from .redfish import RedfishClient

console = Console()

# Keys whose volatility makes them noise in a drift report.
IGNORED_KEYS = {
    "@odata.context",
    "@odata.etag",
    "Modified",
    "ModifiedDate",
    "Time",
    "DateTime",
    "DateTimeLocalOffset",
    "PowerConsumedWatts",
    "ReadingCelsius",
    "ReadingVolts",
    "ReadingRPM",
    "Reading",
    "MinReadingRange",
    "MaxReadingRange",
    "LowerThresholdNonCritical",
    "UpperThresholdNonCritical",
}


def _strip(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: _strip(v) for k, v in d.items() if k not in IGNORED_KEYS}
    if isinstance(d, list):
        return [_strip(x) for x in d]
    return d


def _recover_path(filename: str) -> str:
    stem = filename
    if stem.endswith(".json"):
        stem = stem[:-5]
    return "/" + stem.replace("_", "/")


def _diff(snap: Any, live: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if type(snap) is not type(live):
        out.append(f"  {path or '/'}: type {type(snap).__name__} -> {type(live).__name__}")
        return out
    if isinstance(snap, dict):
        for k in sorted(set(snap) | set(live)):
            if k in IGNORED_KEYS:
                continue
            sub = f"{path}.{k}" if path else k
            if k not in live:
                out.append(f"  - {sub}: removed (was {snap[k]!r})")
            elif k not in snap:
                out.append(f"  + {sub}: added ({live[k]!r})")
            else:
                out.extend(_diff(snap[k], live[k], sub))
    elif isinstance(snap, list):
        if snap != live:
            out.append(f"  {path or '/'}: list changed ({len(snap)} -> {len(live)} items)")
    elif snap != live:
        out.append(f"  {path or '/'}: {snap!r} -> {live!r}")
    return out


def detect_drift(snapshot_dir: Path, settings: Settings | None = None) -> int:
    s = settings or Settings.load()
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.exists():
        console.print(f"[red]No manifest at {manifest_path}[/]")
        return 2

    manifest = json.loads(manifest_path.read_text())
    console.rule(f"Drift detection vs {manifest['host']} (snapshot {manifest['timestamp']})")

    redfish_files = sorted(p for p in (snapshot_dir / "hmm" / "redfish").glob("*.json"))
    drifted: list[tuple[str, list[str]]] = []
    same = 0
    errors: list[tuple[str, str]] = []

    with RedfishClient(s.hmm_host, s.hmm_user, s.hmm_password, verify=s.verify_tls) as rf:
        for f in redfish_files:
            snap = json.loads(f.read_text())
            # Prefer the resource's own @odata.id (round-trip-safe);
            # fall back to filename heuristic for older snapshots.
            path = snap.get("@odata.id") or _recover_path(f.name)
            try:
                live = rf.get(path)
            except Exception as e:
                errors.append((path, str(e)[:80]))
                continue
            d = _diff(_strip(snap), _strip(live))
            if d:
                drifted.append((path, d))
            else:
                same += 1

    table = Table(title=f"Summary ({len(redfish_files)} resources)")
    table.add_column("kind")
    table.add_column("count", justify="right")
    table.add_row("[green]unchanged[/]", str(same))
    table.add_row("[yellow]drifted[/]", str(len(drifted)))
    table.add_row("[red]errors[/]", str(len(errors)))
    console.print(table)

    for path, lines in drifted[:20]:
        console.print(f"\n[bold]{path}[/]")
        for ln in lines[:25]:
            console.print(ln)
        if len(lines) > 25:
            console.print(f"  ... and {len(lines) - 25} more")
    if len(drifted) > 20:
        console.print(f"\n... and {len(drifted) - 20} more drifted resources")

    if errors:
        console.print("\n[red]Errors:[/]")
        for p, e in errors[:10]:
            console.print(f"  {p}: {e}")

    return 0 if not drifted and not errors else 1


def main() -> int:
    if len(sys.argv) != 2:
        console.print("Usage: python -m hmm_client.restore <snapshot-dir>")
        return 2
    return detect_drift(Path(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
