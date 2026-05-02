"""mitmproxy addon — record every HMM web-API flow as a JSON file.

Usage::

    # one-off capture into a fresh timestamped folder under discovery/api/
    HMM_HOST=192.168.1.30 \\
    mitmdump -s scripts/capture_hmm.py --listen-port 8080 \\
             --set ssl_insecure=true --set termlog_verbosity=info

The browser (Palemoon for the applet, Chrome for the rest) is configured
to use ``http://127.0.0.1:8080`` as HTTP/HTTPS proxy and to trust the
mitmproxy CA (``~/.mitmproxy/mitmproxy-ca-cert.pem``). Every flow whose
host matches ``$HMM_HOST`` (or any host in ``$HMM_CAPTURE_HOSTS``,
comma-separated) is persisted as one JSON file.

Each file is named ``<seq>_<METHOD>_<safe_path>.json`` so the directory
listing is already a chronological log of operator actions.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mitmproxy import ctx, http

_PATH_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_BODY_BYTES = 1 << 20  # 1 MiB cap per body — protects against large uploads


def _safe_segment(value: str, max_len: int = 80) -> str:
    cleaned = _PATH_SAFE_RE.sub("_", value).strip("_") or "root"
    return cleaned[:max_len]


def _decode_body(raw: bytes | None) -> dict[str, Any]:
    if not raw:
        return {"encoding": "empty", "value": ""}
    truncated = len(raw) > _MAX_BODY_BYTES
    payload = raw[:_MAX_BODY_BYTES]
    try:
        text = payload.decode("utf-8")
        return {"encoding": "utf-8", "value": text, "truncated": truncated}
    except UnicodeDecodeError:
        return {"encoding": "hex", "value": payload.hex(), "truncated": truncated}


class HMMCapture:
    """Persist matching flows as one JSON file each."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._hosts = self._load_hosts()
        self._dir = self._resolve_dir()
        self._dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load_hosts() -> set[str]:
        raw = os.environ.get("HMM_CAPTURE_HOSTS") or os.environ.get("HMM_HOST", "")
        hosts = {h.strip().lower() for h in raw.split(",") if h.strip()}
        if not hosts:
            raise RuntimeError(
                "HMM_HOST or HMM_CAPTURE_HOSTS must be set so we don't record "
                "unrelated traffic."
            )
        return hosts

    @staticmethod
    def _resolve_dir() -> Path:
        override = os.environ.get("HMM_CAPTURE_DIR")
        if override:
            return Path(override).resolve()
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return (Path.cwd() / "discovery" / "api" / stamp).resolve()

    def load(self, loader) -> None:  # noqa: ANN001 — mitmproxy hook signature
        ctx.log.info(f"[hmm-capture] writing flows to {self._dir}")
        ctx.log.info(f"[hmm-capture] filtering for hosts: {sorted(self._hosts)}")

    def response(self, flow: http.HTTPFlow) -> None:
        host = (flow.request.pretty_host or "").lower()
        if host not in self._hosts:
            return
        with self._lock:
            self._seq += 1
            seq = self._seq
        record = self._build_record(flow, seq)
        filename = self._filename(seq, flow.request.method, flow.request.path)
        out = self._dir / filename
        try:
            out.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        except OSError as exc:
            ctx.log.error(f"[hmm-capture] failed to write {out}: {exc}")
            return
        status = flow.response.status_code if flow.response else "no-resp"
        ctx.log.info(
            f"[hmm-capture] {seq:04d} {flow.request.method} "
            f"{flow.request.path} -> {status}"
        )

    @staticmethod
    def _build_record(flow: http.HTTPFlow, seq: int) -> dict[str, Any]:
        req = flow.request
        resp = flow.response
        return {
            "seq": seq,
            "captured_at": datetime.now(tz=timezone.utc).isoformat(),
            "scheme": req.scheme,
            "host": req.pretty_host,
            "port": req.port,
            "method": req.method,
            "path": req.path,
            "url": req.pretty_url,
            "request": {
                "headers": dict(req.headers.items()),
                "body": _decode_body(req.raw_content),
            },
            "response": {
                "status": resp.status_code if resp else None,
                "reason": resp.reason if resp else None,
                "headers": dict(resp.headers.items()) if resp else {},
                "body": _decode_body(resp.raw_content) if resp else None,
            },
        }

    @staticmethod
    def _filename(seq: int, method: str, raw_path: str) -> str:
        path_part = raw_path.split("?", 1)[0]
        segs = [_safe_segment(s) for s in path_part.strip("/").split("/") if s]
        slug = "_".join(segs) or "root"
        return f"{seq:04d}_{method.upper()}_{slug}.json"


addons = [HMMCapture()]
