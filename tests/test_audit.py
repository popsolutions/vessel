"""Tests for `hmm_client.audit` — chassis-mutating-op audit log."""

from __future__ import annotations

import json
import re

import pytest

from hmm_client import audit


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    """Point VESSEL_AUDIT_LOG at a fresh temp file for each test."""
    p = tmp_path / "audit.log"
    monkeypatch.setenv("VESSEL_AUDIT_LOG", str(p))
    return p


@pytest.mark.unit
class TestLogOp:
    def test_writes_one_ndjson_line(self, audit_log):
        ok = audit.log_op(
            op="power.cycle",
            target_kind="blade",
            target_id="slot3",
            result="success",
            snapshot_id="snap-2026-05-01T20-35-12Z",
            evidence={"command": "ipmcset -d powerstate -v 4"},
        )
        assert ok is True
        assert audit_log.exists()
        lines = audit_log.read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["op"] == "power.cycle"
        assert rec["target"] == {"kind": "blade", "id": "slot3"}
        assert rec["result"] == "success"
        assert rec["evidence"]["command"] == "ipmcset -d powerstate -v 4"

    def test_appends_not_overwrites(self, audit_log):
        for i in range(3):
            audit.log_op(
                op="snapshot.create",
                target_kind="hmm",
                target_id=f"hmm.dc1.example.com#{i}",
                result="success",
            )
        records = audit_log.read_text().strip().splitlines()
        assert len(records) == 3

    def test_iso8601_utc_timestamp(self, audit_log):
        audit.log_op(op="x", target_kind="blade", target_id="slot1", result="success")
        rec = json.loads(audit_log.read_text().strip())
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", rec["ts"])

    def test_default_actor_is_user_at_host(self, audit_log, monkeypatch):
        monkeypatch.delenv("VESSEL_AUDIT_ACTOR", raising=False)
        audit.log_op(op="x", target_kind="blade", target_id="slot1", result="success")
        rec = json.loads(audit_log.read_text().strip())
        assert "@" in rec["actor"]

    def test_audit_actor_env_override(self, audit_log, monkeypatch):
        monkeypatch.setenv("VESSEL_AUDIT_ACTOR", "ci-pipeline")
        audit.log_op(op="x", target_kind="blade", target_id="slot1", result="success")
        rec = json.loads(audit_log.read_text().strip())
        assert rec["actor"] == "ci-pipeline"

    def test_explicit_actor_arg_wins_over_env(self, audit_log, monkeypatch):
        monkeypatch.setenv("VESSEL_AUDIT_ACTOR", "from-env")
        audit.log_op(
            op="x",
            target_kind="blade",
            target_id="slot1",
            result="success",
            actor="from-arg",
        )
        rec = json.loads(audit_log.read_text().strip())
        assert rec["actor"] == "from-arg"

    def test_creates_parent_directory(self, tmp_path, monkeypatch):
        nested = tmp_path / "logs" / "audit" / "vessel.log"
        monkeypatch.setenv("VESSEL_AUDIT_LOG", str(nested))
        ok = audit.log_op(op="x", target_kind="blade", target_id="slot1", result="success")
        assert ok is True
        assert nested.exists()

    def test_returns_false_on_unwritable_path(self, tmp_path, monkeypatch):
        # Point the audit log at a path that's actually a directory —
        # opening it for append raises OSError, which the module must
        # swallow and return False from. Audit failure must NEVER
        # propagate up and abort the operation being audited.
        is_a_dir = tmp_path / "this-is-a-dir"
        is_a_dir.mkdir()
        monkeypatch.setenv("VESSEL_AUDIT_LOG", str(is_a_dir))
        ok = audit.log_op(op="x", target_kind="blade", target_id="slot1", result="success")
        assert ok is False


@pytest.mark.unit
class TestRedaction:
    def test_top_level_password_key_redacted(self, audit_log):
        audit.log_op(
            op="login",
            target_kind="hmm",
            target_id="hmm.dc1.example.com",
            result="success",
            evidence={"user": "root", "password": "supersecret"},
        )
        rec = json.loads(audit_log.read_text().strip())
        assert rec["evidence"]["password"] == "***"
        assert rec["evidence"]["user"] == "root"

    def test_nested_token_redacted(self, audit_log):
        audit.log_op(
            op="firmware.apply",
            target_kind="blade",
            target_id="slot3",
            result="success",
            evidence={
                "metadata": {
                    "uploader": "ci",
                    "auth_token": "ghp_xxxxxxxxxxxxxxxx",
                },
            },
        )
        rec = json.loads(audit_log.read_text().strip())
        assert rec["evidence"]["metadata"]["auth_token"] == "***"
        assert rec["evidence"]["metadata"]["uploader"] == "ci"

    def test_redaction_case_insensitive(self, audit_log):
        audit.log_op(
            op="x",
            target_kind="blade",
            target_id="slot1",
            result="success",
            evidence={"AuthBearer": "abc", "API_KEY": "def", "ApiKey": "ghi"},
        )
        rec = json.loads(audit_log.read_text().strip())
        assert rec["evidence"]["AuthBearer"] == "***"
        assert rec["evidence"]["API_KEY"] == "***"
        assert rec["evidence"]["ApiKey"] == "***"

    def test_list_of_dicts_redacted(self, audit_log):
        audit.log_op(
            op="firmware.batch",
            target_kind="chassis",
            target_id="hmm.dc1.example.com",
            result="success",
            evidence={
                "targets": [
                    {"slot": 1, "credential": "x"},
                    {"slot": 2, "credential": "y"},
                ],
            },
        )
        rec = json.loads(audit_log.read_text().strip())
        assert rec["evidence"]["targets"][0]["credential"] == "***"
        assert rec["evidence"]["targets"][1]["credential"] == "***"
        assert rec["evidence"]["targets"][0]["slot"] == 1


@pytest.mark.unit
class TestReadRecords:
    def test_returns_empty_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VESSEL_AUDIT_LOG", str(tmp_path / "nonexistent.log"))
        assert audit.read_records() == []

    def test_round_trip(self, audit_log):
        for i in range(3):
            audit.log_op(
                op=f"op.{i}",
                target_kind="blade",
                target_id=f"slot{i}",
                result="success",
            )
        records = audit.read_records()
        assert len(records) == 3
        assert [r["op"] for r in records] == ["op.0", "op.1", "op.2"]

    def test_limit_returns_tail(self, audit_log):
        for i in range(5):
            audit.log_op(
                op=f"op.{i}",
                target_kind="blade",
                target_id="slot1",
                result="success",
            )
        records = audit.read_records(limit=2)
        assert [r["op"] for r in records] == ["op.3", "op.4"]

    def test_skips_malformed_lines(self, audit_log):
        audit.log_op(op="ok1", target_kind="blade", target_id="slot1", result="success")
        with audit_log.open("a") as f:
            f.write("this is not json at all\n")
        audit.log_op(op="ok2", target_kind="blade", target_id="slot1", result="success")
        records = audit.read_records()
        assert [r["op"] for r in records] == ["ok1", "ok2"]
