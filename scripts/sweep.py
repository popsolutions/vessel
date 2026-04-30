"""Ping sweep the chassis-internal fabrics from inside the HMM.

The HMM's restricted SSH dispatcher allows `ping`, so we open one shell per
target in a small thread pool and parse the BusyBox-style ping output.

Usage: python scripts/sweep.py
Writes: discovery/ibmc-sweep-<UTC-stamp>.json
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hmm_client.config import Settings  # noqa: E402

NETS = ["172.31.0", "172.31.1"]
PING_RE = re.compile(r"(\d+) packets received")


def _drain(chan) -> str:
    buf = b""
    while chan.recv_ready():
        buf += chan.recv(8192)
    return buf.decode(errors="replace")


def ping(host: str, user: str, password: str, target: str) -> tuple[str, bool]:
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=user, password=password, timeout=10,
                allow_agent=False, look_for_keys=False)
    try:
        chan = cli.invoke_shell()
        time.sleep(0.6)
        _drain(chan)
        chan.send(f"ping -c 1 -W 1 {target}\r\n")
        time.sleep(2.0)
        out = _drain(chan)
        m = PING_RE.search(out)
        return target, bool(m and int(m.group(1)) > 0)
    finally:
        cli.close()


def main() -> None:
    s = Settings.load()
    targets = [f"{net}.{i}" for net in NETS for i in range(1, 255)]
    results: dict[str, str] = {}
    print(f"Sweeping {len(targets)} addresses (parallel=8) via {s.hmm_host} ...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        for target, up in ex.map(
            lambda t: ping(s.hmm_host, s.hmm_user, s.hmm_password, t),
            targets,
        ):
            results[target] = "up" if up else "down"
            if up:
                print(f"  UP  {target}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path("discovery") / f"ibmc-sweep-{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps({"timestamp": stamp, "host": s.hmm_host, "results": results}, indent=2)
    )
    up_hosts = [ip for ip, st in results.items() if st == "up"]
    print(f"\n{len(up_hosts)} hosts up. Saved -> {out}")


if __name__ == "__main__":
    main()
