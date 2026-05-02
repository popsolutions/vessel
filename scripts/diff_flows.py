"""Aggregate captured HMM flows into an endpoint inventory.

Reads every ``<seq>_<METHOD>_<path>.json`` file produced by
``scripts/capture_hmm.py`` in a capture directory, groups them into
endpoint templates (numeric IDs and UUIDs replaced with placeholders),
and writes:

- ``<capture_dir>/inventory.md`` — human-friendly summary, one section
  per endpoint with method/status/content-type counts and one sample
  request/response per method.
- ``<capture_dir>/inventory.json`` — same data, machine-readable.

Usage::

    python scripts/diff_flows.py discovery/api/20260502T143015Z
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("hmm-diff")

_NUMERIC_SEG = re.compile(r"^\d+$")
_UUID_SEG = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_HEX_SEG = re.compile(r"^[0-9a-f]{24,}$", re.IGNORECASE)
_BODY_PREVIEW_CHARS = 400


def _templatise(path: str) -> str:
    """Reduce a concrete path to a template."""
    base = path.split("?", 1)[0]
    parts = base.strip("/").split("/")
    out: list[str] = []
    for seg in parts:
        if not seg:
            continue
        if _NUMERIC_SEG.match(seg):
            out.append("{id}")
        elif _UUID_SEG.match(seg):
            out.append("{uuid}")
        elif _HEX_SEG.match(seg):
            out.append("{hex}")
        else:
            out.append(seg)
    return "/" + "/".join(out) if out else "/"


@dataclass
class EndpointAgg:
    template: str
    raw_paths: set[str]
    method_counts: Counter
    status_counts: Counter
    samples: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "raw_paths": sorted(self.raw_paths),
            "methods": dict(self.method_counts),
            "statuses": {str(k): v for k, v in self.status_counts.items()},
            "samples": self.samples,
        }


def _load_flow(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("skipping %s: %s", path.name, exc)
        return None


def _content_type(headers: dict) -> str:
    for k, v in headers.items():
        if k.lower() == "content-type":
            return v.split(";", 1)[0].strip()
    return ""


def _body_preview(body: dict | None) -> str:
    if not body:
        return ""
    if body.get("encoding") == "utf-8":
        return (body.get("value") or "")[:_BODY_PREVIEW_CHARS]
    if body.get("encoding") == "hex":
        return f"<binary, {len(body.get('value', '')) // 2} bytes>"
    return ""


def aggregate(capture_dir: Path) -> dict[str, EndpointAgg]:
    endpoints: dict[str, EndpointAgg] = {}
    flows = sorted(p for p in capture_dir.iterdir() if p.is_file() and p.suffix == ".json")
    for flow_path in flows:
        if flow_path.name.startswith(("inventory", "_")):
            continue
        record = _load_flow(flow_path)
        if not record or "method" not in record or "path" not in record:
            continue
        method = record["method"].upper()
        raw_path = record["path"]
        template = _templatise(raw_path)
        agg = endpoints.get(template)
        if agg is None:
            agg = EndpointAgg(
                template=template,
                raw_paths=set(),
                method_counts=Counter(),
                status_counts=Counter(),
                samples={},
            )
            endpoints[template] = agg
        agg.raw_paths.add(raw_path.split("?", 1)[0])
        agg.method_counts[method] += 1
        resp = record.get("response") or {}
        status = resp.get("status")
        if status is not None:
            agg.status_counts[status] += 1
        if method not in agg.samples:
            req = record.get("request") or {}
            agg.samples[method] = {
                "flow_file": flow_path.name,
                "request_content_type": _content_type(req.get("headers") or {}),
                "request_body_preview": _body_preview(req.get("body")),
                "response_status": status,
                "response_content_type": _content_type(resp.get("headers") or {}),
                "response_body_preview": _body_preview(resp.get("body")),
            }
    return endpoints


def write_json(endpoints: dict[str, EndpointAgg], src_dir: Path, out: Path) -> None:
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_dir": str(src_dir),
        "endpoints": [endpoints[k].to_dict() for k in sorted(endpoints)],
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def write_markdown(endpoints: dict[str, EndpointAgg], src_dir: Path, out: Path) -> None:
    lines: list[str] = [
        "# HMM API endpoint inventory",
        "",
        f"- Source: `{src_dir}`",
        f"- Generated: {datetime.now(tz=timezone.utc).isoformat()}",
        f"- Endpoints discovered: **{len(endpoints)}**",
        "",
        "## Endpoint summary",
        "",
        "| Methods | Endpoint | Calls | Statuses |",
        "| --- | --- | --- | --- |",
    ]
    for tpl in sorted(endpoints):
        agg = endpoints[tpl]
        methods = ",".join(sorted(agg.method_counts))
        calls = sum(agg.method_counts.values())
        statuses = ",".join(f"{s}x{c}" for s, c in sorted(agg.status_counts.items()))
        lines.append(f"| `{methods}` | `{tpl}` | {calls} | {statuses} |")
    lines.append("")
    lines.append("## Per-endpoint detail")
    lines.append("")
    for tpl in sorted(endpoints):
        agg = endpoints[tpl]
        lines.append(f"### `{tpl}`")
        lines.append("")
        if len(agg.raw_paths) > 1:
            lines.append(f"**Concrete paths seen ({len(agg.raw_paths)}):**")
            for rp in sorted(agg.raw_paths)[:20]:
                lines.append(f"- `{rp}`")
            if len(agg.raw_paths) > 20:
                lines.append(f"- _(+{len(agg.raw_paths) - 20} more)_")
            lines.append("")
        for method, sample in sorted(agg.samples.items()):
            lines.append(f"#### `{method}`")
            lines.append("")
            lines.append(f"- Sample flow: `{sample['flow_file']}`")
            lines.append(f"- Response status: `{sample['response_status']}`")
            if sample["request_content_type"]:
                lines.append(f"- Request `Content-Type`: `{sample['request_content_type']}`")
            if sample["response_content_type"]:
                lines.append(f"- Response `Content-Type`: `{sample['response_content_type']}`")
            if sample["request_body_preview"]:
                lines.append("- Request body preview:")
                lines.append("")
                lines.append("```")
                lines.append(sample["request_body_preview"])
                lines.append("```")
            if sample["response_body_preview"]:
                lines.append("- Response body preview:")
                lines.append("")
                lines.append("```")
                lines.append(sample["response_body_preview"])
                lines.append("```")
            lines.append("")
    out.write_text("\n".join(lines))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("capture_dir", type=Path, help="discovery/api/<stamp> directory")
    args = p.parse_args()
    capture_dir = args.capture_dir.resolve()
    if not capture_dir.is_dir():
        raise SystemExit(f"not a directory: {capture_dir}")
    endpoints = aggregate(capture_dir)
    log.info("aggregated %d endpoints from %s", len(endpoints), capture_dir)
    write_json(endpoints, capture_dir, capture_dir / "inventory.json")
    write_markdown(endpoints, capture_dir, capture_dir / "inventory.md")
    log.info("wrote %s and %s", capture_dir / "inventory.json", capture_dir / "inventory.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
