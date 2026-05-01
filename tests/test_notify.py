"""Tests for `hmm_client.notify` — Telegram notification sink."""

from __future__ import annotations

import pytest

from hmm_client.notify import is_configured, notify_telegram


@pytest.mark.unit
class TestNotifyConfiguration:
    def test_is_configured_false_when_both_unset(self, monkeypatch):
        monkeypatch.delenv("VESSEL_TG_BOT_TOKEN", raising=False)
        monkeypatch.delenv("VESSEL_TG_CHAT_ID", raising=False)
        assert is_configured() is False

    def test_is_configured_false_when_only_token_set(self, monkeypatch):
        monkeypatch.setenv("VESSEL_TG_BOT_TOKEN", "fake:token")
        monkeypatch.delenv("VESSEL_TG_CHAT_ID", raising=False)
        assert is_configured() is False

    def test_is_configured_false_when_only_chat_id_set(self, monkeypatch):
        monkeypatch.delenv("VESSEL_TG_BOT_TOKEN", raising=False)
        monkeypatch.setenv("VESSEL_TG_CHAT_ID", "123")
        assert is_configured() is False

    def test_is_configured_true_when_both_set(self, monkeypatch):
        monkeypatch.setenv("VESSEL_TG_BOT_TOKEN", "fake:token")
        monkeypatch.setenv("VESSEL_TG_CHAT_ID", "123")
        assert is_configured() is True

    def test_empty_string_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("VESSEL_TG_BOT_TOKEN", "")
        monkeypatch.setenv("VESSEL_TG_CHAT_ID", "")
        assert is_configured() is False


@pytest.mark.unit
class TestNotifyTelegramNoop:
    """Without env config, notify_telegram must silently return False
    so that wiring it into runtime paths is safe by default."""

    def test_returns_false_without_config(self, monkeypatch):
        monkeypatch.delenv("VESSEL_TG_BOT_TOKEN", raising=False)
        monkeypatch.delenv("VESSEL_TG_CHAT_ID", raising=False)
        assert notify_telegram("test") is False

    def test_does_not_raise_on_unknown_level(self, monkeypatch):
        monkeypatch.delenv("VESSEL_TG_BOT_TOKEN", raising=False)
        monkeypatch.delenv("VESSEL_TG_CHAT_ID", raising=False)
        assert notify_telegram("test", level="bogus") is False


@pytest.mark.unit
class TestNotifyTelegramSend:
    """When configured, notify_telegram POSTs to Telegram. We mock httpx
    so no real API call is made."""

    def test_posts_with_correct_payload(self, monkeypatch):
        monkeypatch.setenv("VESSEL_TG_BOT_TOKEN", "12345:ABCDEF")
        monkeypatch.setenv("VESSEL_TG_CHAT_ID", "999")

        captured: dict = {}

        class _FakeResponse:
            status_code = 200
            text = "ok"

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["payload"] = kwargs.get("json")
            captured["timeout"] = kwargs.get("timeout")
            return _FakeResponse()

        import hmm_client.notify as mod

        monkeypatch.setattr(mod.httpx, "post", fake_post)

        ok = notify_telegram("hello", level="ok")
        assert ok is True
        assert captured["url"] == "https://api.telegram.org/bot12345:ABCDEF/sendMessage"
        assert captured["payload"]["chat_id"] == "999"
        assert "✅" in captured["payload"]["text"]
        assert "hello" in captured["payload"]["text"]
        assert captured["payload"]["disable_web_page_preview"] is True

    def test_returns_false_on_non_200(self, monkeypatch):
        monkeypatch.setenv("VESSEL_TG_BOT_TOKEN", "x:y")
        monkeypatch.setenv("VESSEL_TG_CHAT_ID", "1")

        class _FailResp:
            status_code = 401
            text = "unauthorized"

        def fake_post(*a, **kw):
            return _FailResp()

        import hmm_client.notify as mod

        monkeypatch.setattr(mod.httpx, "post", fake_post)
        assert notify_telegram("test") is False

    def test_swallows_httpx_errors(self, monkeypatch):
        monkeypatch.setenv("VESSEL_TG_BOT_TOKEN", "x:y")
        monkeypatch.setenv("VESSEL_TG_CHAT_ID", "1")

        import httpx

        def boom(*a, **kw):
            raise httpx.ConnectError("network down")

        import hmm_client.notify as mod

        monkeypatch.setattr(mod.httpx, "post", boom)
        # Must NOT raise — failure to notify can never break the caller
        assert notify_telegram("test") is False

    def test_long_message_truncated_with_ellipsis(self, monkeypatch):
        monkeypatch.setenv("VESSEL_TG_BOT_TOKEN", "x:y")
        monkeypatch.setenv("VESSEL_TG_CHAT_ID", "1")

        captured: dict = {}

        class _OK:
            status_code = 200
            text = "ok"

        def fake_post(url, **kwargs):
            captured["text"] = kwargs["json"]["text"]
            return _OK()

        import hmm_client.notify as mod

        monkeypatch.setattr(mod.httpx, "post", fake_post)
        notify_telegram("x" * 5000)
        assert len(captured["text"]) <= 4001
        assert captured["text"].endswith("…")
