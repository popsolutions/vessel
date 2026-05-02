"""Tests for `hmm_client.logging_config` — structlog + stdlib bridge."""

from __future__ import annotations

import io
import json
import logging

import pytest
import structlog

from hmm_client import logging_config


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Each test starts with a fresh _CONFIGURED flag and clean env."""
    monkeypatch.setattr(logging_config, "_CONFIGURED", False)
    monkeypatch.delenv("VESSEL_LOG_JSON", raising=False)
    monkeypatch.delenv("VESSEL_LOG_LEVEL", raising=False)
    yield
    # Restore root logger to a clean state so other test modules don't
    # see our handler.
    root = logging.getLogger()
    root.handlers = []
    root.setLevel(logging.WARNING)


@pytest.mark.unit
class TestSetup:
    def test_idempotent(self):
        logging_config.setup()
        assert logging_config.is_configured() is True
        # Calling again should not re-configure (no-op)
        logging_config.setup()
        assert logging_config.is_configured() is True

    def test_force_reconfigures(self, monkeypatch):
        logging_config.setup()
        # Switch mode and force re-setup
        monkeypatch.setenv("VESSEL_LOG_JSON", "1")
        logging_config.setup(force=True)
        assert logging_config.is_configured() is True

    def _renderer(self):
        # The renderer lives on the stdlib handler's ProcessorFormatter
        # (last processor) — that's the single rendering point now,
        # which lets structlog and stdlib loggers share one output format.
        handler = logging.getLogger().handlers[0]
        return handler.formatter.processors[-1]

    def test_pretty_mode_when_env_unset(self):
        logging_config.setup()
        renderer = self._renderer()
        assert not isinstance(renderer, structlog.processors.JSONRenderer)

    def test_json_mode_when_env_set(self, monkeypatch):
        monkeypatch.setenv("VESSEL_LOG_JSON", "1")
        logging_config.setup(force=True)
        renderer = self._renderer()
        assert isinstance(renderer, structlog.processors.JSONRenderer)


@pytest.mark.unit
class TestJsonOutput:
    def test_stdlib_log_emits_json(self, monkeypatch):
        monkeypatch.setenv("VESSEL_LOG_JSON", "1")
        logging_config.setup(force=True)

        # Redirect the configured handler's stream so we can inspect output
        buf = io.StringIO()
        for h in logging.getLogger().handlers:
            h.stream = buf

        log = logging.getLogger("test.module")
        log.warning("blade powered off")

        line = buf.getvalue().strip()
        assert line, "expected at least one log line"
        rec = json.loads(line)
        assert rec["event"] == "blade powered off"
        assert rec["level"] == "warning"
        assert rec["logger"] == "test.module"
        assert "timestamp" in rec

    def test_structlog_log_emits_json(self, monkeypatch):
        monkeypatch.setenv("VESSEL_LOG_JSON", "1")
        logging_config.setup(force=True)

        buf = io.StringIO()
        for h in logging.getLogger().handlers:
            h.stream = buf

        log = structlog.get_logger("blade.ops")
        log.info("powered off", slot=3, action="off")

        line = buf.getvalue().strip()
        rec = json.loads(line)
        assert rec["event"] == "powered off"
        assert rec["slot"] == 3
        assert rec["action"] == "off"
        assert rec["level"] == "info"


@pytest.mark.unit
class TestLevel:
    def test_default_level_is_info(self):
        logging_config.setup()
        assert logging.getLogger().level == logging.INFO

    def test_env_overrides_level(self, monkeypatch):
        monkeypatch.setenv("VESSEL_LOG_LEVEL", "DEBUG")
        logging_config.setup(force=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_invalid_level_falls_back_to_info(self, monkeypatch):
        monkeypatch.setenv("VESSEL_LOG_LEVEL", "BOGUS")
        logging_config.setup(force=True)
        assert logging.getLogger().level == logging.INFO
