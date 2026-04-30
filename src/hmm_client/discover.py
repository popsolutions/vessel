from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

from .config import Settings
from .redfish import RedfishClient

console = Console()

ROOT_ENTRIES = (
    "Systems",
    "Chassis",
    "Managers",
    "AccountService",
    "UpdateService",
    "EventService",
    "TaskService",
    "SessionService",
    "Registries",
)

SUB_COLLECTIONS = (
    "EthernetInterfaces",
    "VirtualMedia",
    "SerialInterfaces",
    "NetworkProtocol",
    "LogServices",
    "Storage",
    "Bios",
    "Power",
    "Thermal",
    "Processors",
    "Memory",
)


def _path_to_filename(path: str) -> str:
    return path.strip("/").replace("/", "_").replace("$", "_") + ".json"


def _safe_get(rf: RedfishClient, path: str) -> dict[str, Any]:
    try:
        return rf.get(path)
    except httpx.HTTPStatusError as e:
        return {"_error": e.response.status_code, "_path": path}
    except httpx.HTTPError as e:
        return {"_error": str(e), "_path": path}


def main() -> None:
    s = Settings.load()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path("discovery") / stamp
    out.mkdir(parents=True, exist_ok=True)

    console.rule(f"Redfish discovery {s.hmm_host} -> {out}")
    seen: dict[str, dict[str, Any]] = {}

    with RedfishClient(s.hmm_host, s.hmm_user, s.hmm_password, verify=s.verify_tls) as rf:

        def fetch(path: str) -> dict[str, Any]:
            if path in seen:
                return seen[path]
            d = _safe_get(rf, path)
            seen[path] = d
            (out / _path_to_filename(path)).write_text(json.dumps(d, indent=2, sort_keys=True))
            return d

        root = fetch("/redfish/v1/")
        for key in ROOT_ENTRIES:
            entry = root.get(key)
            ref = entry.get("@odata.id") if isinstance(entry, dict) else None
            if not ref:
                continue
            coll = fetch(ref)
            for member in rf.members(coll):
                m = fetch(member)
                for sub in SUB_COLLECTIONS:
                    sub_entry = m.get(sub)
                    sref = sub_entry.get("@odata.id") if isinstance(sub_entry, dict) else None
                    if not sref:
                        continue
                    sc = fetch(sref)
                    for s_m in rf.members(sc):
                        fetch(s_m)

    summary = out / "_summary.txt"
    with summary.open("w") as f:
        f.write(f"Discovered {len(seen)} Redfish resources at {stamp}\n")
        f.write(f"Host: {s.hmm_host}\n\n")
        for path, doc in sorted(seen.items()):
            name = doc.get("Name") or doc.get("Id") or ""
            kind = doc.get("@odata.type", "").split("#")[-1].split(".")[0]
            err = doc.get("_error")
            tag = f"ERR {err}" if err else (kind or "?")
            f.write(f"  [{tag:32s}] {path}  {name}\n")

    console.print(f"[green]Saved {len(seen)} resources ->[/] [bold]{summary}[/]")


if __name__ == "__main__":
    main()
