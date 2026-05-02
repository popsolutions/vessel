"""Comprehensive read-only sweep of every HMM dispatcher we mapped.

Drives the dispatchers that the captured browser sessions revealed,
varying ``bladename``/``fruid``/``ipType`` across every component on
the chassis. Saves one response per call so the upgrade-plan generator
and the future Python SDK have a complete picture without needing more
manual GUI clicking.

Read-only verbs only — no ``update``, ``delete``, ``apply``, ``commit``.

Usage::

    python scripts/dispatch_sweep.py
    # outputs discovery/sweep/<UTC-stamp>/
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("hmm-sweep")

# Hardware layout, per project_hardware.md
BLADE_SLOTS: tuple[int, ...] = (1, 2, 3, 4, 8, 9, 10, 11, 12, 13, 14, 15, 16)
FAN_SLOTS: tuple[int, ...] = (1, 2, 3, 4, 5, 7, 8, 10, 11, 13)
SWITCHES: tuple[str, ...] = ("Swi2", "Swi3")
SMMS: tuple[str, ...] = ("SMM1", "SMM2", "smm", "othersmm")
PEMS: tuple[str, ...] = ("PEM1", "PEM2", "PEM3", "PEM4")
ALL_COMPONENTS: tuple[str, ...] = (
    *SMMS,
    *(f"Slot{n}" for n in BLADE_SLOTS),
    *SWITCHES,
    *(f"Fan{n}" for n in FAN_SLOTS),
    *PEMS,
)

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(s: str) -> str:
    return _SAFE_RE.sub("_", s).strip("_") or "_"


@dataclass
class Call:
    handler: str
    params: dict[str, str]

    def key(self) -> str:
        bits = [self.params.get("actiontype", "")]
        for k in ("bladename", "fruid", "ipType", "smmtype"):
            if k in self.params:
                bits.append(self.params[k])
        return _safe("_".join(b for b in bits if b))


def _build_calls() -> list[Call]:
    calls: list[Call] = []
    for c in ALL_COMPONENTS:
        calls.append(Call("versionhandler.php", {"actiontype": "get", "chassisid": "0", "bladename": c, "fruid": "0"}))
    for n in BLADE_SLOTS:
        calls.append(Call("mezzhandler.php", {"actiontype": "deviceinfo", "chassisid": "0", "bladename": f"Slot{n}"}))
    for at in ("status", "isupdate", "getServerTime"):
        calls.append(Call("queryhandler.php", {"actiontype": at, "chassisid": "0"}))
    calls.append(Call("queryhandler.php", {"actiontype": "isstack", "chassisid": "0", "fruid": "0"}))
    calls.append(Call("queryhandler.php", {"actiontype": "query_policytemplate_list2", "chassisid": "0", "page": "1", "perpage": "100"}))
    calls.append(Call("queryhandler.php", {"actiontype": "query_nodepool_list", "chassisid": "0", "page": "1", "perpage": "100"}))
    for it in ("0", "1", "2"):
        calls.append(Call("networkhandler.php", {"actiontype": "getbmcipdata", "beforeconfig": "0", "chassisid": "0", "ipType": it}))
    for scope in ("eth", "fc", "mgmt"):
        calls.append(Call("networkhandler.php", {"actiontype": "list", "bladename": scope, "chassisid": "0"}))
    for scope in ("swi", "blade", "fan", "pem"):
        calls.append(Call("networkhandler.php", {"actiontype": "listpresentfru", "bladename": scope, "chassisid": "0"}))
    calls.append(Call("smmupgradehandler.php", {"actiontype": "get"}))
    for st in ("1", "2"):
        calls.append(Call("smmupgradehandler.php", {"actiontype": "checkupgrade", "smmtype": st}))
    for at in ("sel", "alarm"):
        calls.append(Call("selhandler.php", {"actiontype": at, "chassisid": "0", "page": "1", "perpage": "100"}))
    calls.append(Call("historyselhandler.php", {"actiontype": "list", "chassisid": "0", "page": "1", "perpage": "100"}))
    calls.append(Call("userhandler.php", {"actiontype": "getassettag", "chassisid": "0"}))
    calls.append(Call("userhandler.php", {"actiontype": "getslotalias", "chassisid": "0"}))
    calls.append(Call("flattenedhandler.php", {"actiontype": "getflattened", "chassisid": "0"}))
    return calls


def _login(client: httpx.Client, base: str, user: str, pwd: str) -> str:
    client.post(
        f"{base}/loginhandler.php",
        data={"actiontype": "queryverify", "chassisid": "0"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    ).raise_for_status()
    r = client.post(
        f"{base}/loginhandler.php",
        data={
            "actiontype": "login",
            "username": user,
            "userpasswd": pwd,
            "usermode": "1",
            "code": "",
            "language": "en",
        },
        headers={"Custom-Token": "0", "X-Requested-With": "XMLHttpRequest"},
    )
    r.raise_for_status()
    m = re.search(r"<csrftoken>([^<]+)</csrftoken>", r.text)
    if not m:
        raise RuntimeError(f"login: no csrftoken in response (first 200 chars): {r.text[:200]}")
    return m.group(1)


def _retcode(text: str) -> int | None:
    m = re.search(r"<retcode>(-?\d+)</retcode>", text)
    if m:
        return int(m.group(1))
    m = re.search(r'"retcode"\s*:\s*(-?\d+)', text)
    return int(m.group(1)) if m else None


def main() -> int:
    host = os.environ.get("HMM_HOST")
    user = os.environ.get("HMM_USER")
    pwd = os.environ.get("HMM_PASSWORD")
    verify = os.environ.get("HMM_VERIFY_TLS", "false").lower() == "true"
    if not (host and user and pwd):
        raise SystemExit("HMM_HOST / HMM_USER / HMM_PASSWORD required")

    base = f"https://{host}"
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = (Path.cwd() / "discovery" / "sweep" / stamp).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("output dir: %s", out_dir)

    calls = _build_calls()
    log.info("planned %d calls", len(calls))

    index: list[dict[str, Any]] = []
    with httpx.Client(verify=verify, timeout=30.0, follow_redirects=False) as client:
        log.info("logging in to %s", base)
        token = _login(client, base, user, pwd)
        log.info("logged in, csrftoken acquired (length=%d)", len(token))

        common_headers = {
            "Custom-Token": token,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": base,
            "Referer": f"{base}/index.html?chassisid=0",
        }

        for i, call in enumerate(calls, start=1):
            try:
                r = client.post(f"{base}/{call.handler}", data=call.params, headers=common_headers)
                status = r.status_code
                body = r.text
                ct = r.headers.get("content-type", "")
            except httpx.HTTPError as exc:
                status = -1
                body = f"<error>{exc}</error>"
                ct = "error/exception"

            ext = ".json" if "json" in ct else ".xml"
            fname = f"{i:04d}_{Path(call.handler).stem}_{call.key()}{ext}"
            (out_dir / fname).write_text(body, encoding="utf-8")
            entry = {
                "seq": i,
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "handler": call.handler,
                "params": call.params,
                "http_status": status,
                "content_type": ct,
                "retcode": _retcode(body),
                "response_file": fname,
            }
            index.append(entry)
            log.info(
                "[%d/%d] %s %s -> http=%s retcode=%s",
                i, len(calls), call.handler, call.params.get("actiontype"),
                status, entry["retcode"],
            )

    (out_dir / "_index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    log.info("wrote %s/_index.json (%d entries)", out_dir, len(index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
