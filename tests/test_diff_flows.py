"""Tests for `scripts/diff_flows.py` — flow aggregation."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "diff_flows.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("diff_flows", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["diff_flows"] = module
    spec.loader.exec_module(module)
    return module


diff_flows = _load_module()


def _write_flow(
    out_dir: Path,
    seq: int,
    method: str,
    path: str,
    *,
    status: int = 200,
    response_body: str = "",
    request_body: str = "",
    content_type: str = "application/json",
) -> Path:
    record = {
        "seq": seq,
        "captured_at": "2026-05-02T14:30:15+00:00",
        "scheme": "https",
        "host": "192.168.1.30",
        "port": 443,
        "method": method,
        "path": path,
        "url": f"https://192.168.1.30{path}",
        "request": {
            "headers": {"Accept": "application/json"},
            "body": (
                {"encoding": "utf-8", "value": request_body, "truncated": False}
                if request_body
                else {"encoding": "empty", "value": ""}
            ),
        },
        "response": {
            "status": status,
            "reason": "OK",
            "headers": {"Content-Type": content_type},
            "body": (
                {"encoding": "utf-8", "value": response_body, "truncated": False}
                if response_body
                else {"encoding": "empty", "value": ""}
            ),
        },
    }
    safe = path.strip("/").replace("/", "_") or "root"
    f = out_dir / f"{seq:04d}_{method}_{safe}.json"
    f.write_text(json.dumps(record))
    return f


@pytest.mark.unit
class TestTemplatise:
    def test_numeric_segment_replaced_with_id(self):
        assert diff_flows._templatise("/redfish/v1/Chassis/1") == "/redfish/v1/Chassis/{id}"

    def test_uuid_segment_replaced_with_uuid(self):
        u = "550e8400-e29b-41d4-a716-446655440000"
        assert diff_flows._templatise(f"/api/sessions/{u}") == "/api/sessions/{uuid}"

    def test_long_hex_segment_replaced_with_hex(self):
        h = "a" * 32
        assert diff_flows._templatise(f"/api/tokens/{h}") == "/api/tokens/{hex}"

    def test_query_string_stripped(self):
        assert diff_flows._templatise("/api/list?page=2&size=10") == "/api/list"

    def test_root_path(self):
        assert diff_flows._templatise("/") == "/"

    def test_literal_segments_preserved(self):
        assert diff_flows._templatise("/api/v1/users") == "/api/v1/users"


@pytest.mark.unit
class TestAggregate:
    def test_groups_concrete_paths_under_template(self, tmp_path):
        _write_flow(tmp_path, 1, "GET", "/redfish/v1/Chassis/1")
        _write_flow(tmp_path, 2, "GET", "/redfish/v1/Chassis/2")
        _write_flow(tmp_path, 3, "GET", "/redfish/v1/Chassis/3")
        endpoints = diff_flows.aggregate(tmp_path)
        assert "/redfish/v1/Chassis/{id}" in endpoints
        agg = endpoints["/redfish/v1/Chassis/{id}"]
        assert agg.method_counts == {"GET": 3}
        assert len(agg.raw_paths) == 3

    def test_counts_status_codes(self, tmp_path):
        _write_flow(tmp_path, 1, "GET", "/api/x", status=200)
        _write_flow(tmp_path, 2, "GET", "/api/x", status=200)
        _write_flow(tmp_path, 3, "GET", "/api/x", status=401)
        agg = diff_flows.aggregate(tmp_path)["/api/x"]
        assert agg.status_counts == {200: 2, 401: 1}

    def test_one_sample_per_method(self, tmp_path):
        _write_flow(tmp_path, 1, "GET", "/api/y", response_body="first")
        _write_flow(tmp_path, 2, "GET", "/api/y", response_body="second")
        _write_flow(tmp_path, 3, "POST", "/api/y", request_body="payload")
        agg = diff_flows.aggregate(tmp_path)["/api/y"]
        assert set(agg.samples) == {"GET", "POST"}
        assert agg.samples["GET"]["response_body_preview"] == "first"
        assert agg.samples["POST"]["request_body_preview"] == "payload"

    def test_skips_inventory_and_underscore_files(self, tmp_path):
        _write_flow(tmp_path, 1, "GET", "/api/z")
        (tmp_path / "inventory.json").write_text("{}")
        (tmp_path / "_crawl_log.jsonl").write_text('{"action":"navigate"}\n')
        endpoints = diff_flows.aggregate(tmp_path)
        assert set(endpoints) == {"/api/z"}

    def test_skips_malformed_json(self, tmp_path):
        _write_flow(tmp_path, 1, "GET", "/api/ok")
        (tmp_path / "0099_GET_broken.json").write_text("{not valid json")
        endpoints = diff_flows.aggregate(tmp_path)
        assert set(endpoints) == {"/api/ok"}


@pytest.mark.unit
class TestWriteOutputs:
    def test_writes_inventory_json_and_md(self, tmp_path):
        _write_flow(tmp_path, 1, "GET", "/redfish/v1/Chassis/1", response_body='{"id":1}')
        _write_flow(tmp_path, 2, "PATCH", "/redfish/v1/Chassis/1", request_body='{"x":1}')
        endpoints = diff_flows.aggregate(tmp_path)
        diff_flows.write_json(endpoints, tmp_path, tmp_path / "inventory.json")
        diff_flows.write_markdown(endpoints, tmp_path, tmp_path / "inventory.md")

        payload = json.loads((tmp_path / "inventory.json").read_text())
        assert payload["source_dir"] == str(tmp_path)
        templates = [e["template"] for e in payload["endpoints"]]
        assert templates == ["/redfish/v1/Chassis/{id}"]
        ep = payload["endpoints"][0]
        assert set(ep["methods"]) == {"GET", "PATCH"}

        md = (tmp_path / "inventory.md").read_text()
        assert "Endpoints discovered: **1**" in md
        assert "/redfish/v1/Chassis/{id}" in md
        assert "GET" in md and "PATCH" in md
