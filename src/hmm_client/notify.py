"""Telegram (and pluggable) notification sink.

Used by long-running operations to ping the operator on their phone:
    - rolling firmware upgrade waves crossing milestones
    - VLAN ops triggering auto-rollback
    - KVM session blocked on chassis-side
    - any background CLI step that finishes / fails while the operator
      is away from the terminal

## Configuration

All credentials are read from environment variables — never hardcoded
and never persisted anywhere. The module is a silent no-op when the
required vars are missing, so wiring it into runtime paths is safe by
default (callers don't have to check whether notifications are
configured).

    VESSEL_TG_BOT_TOKEN   Bot token from @BotFather. Without this, all
                          calls return False and skip sending.
    VESSEL_TG_CHAT_ID     Chat id (integer). Get yours by sending /start
                          to your bot then GET
                          https://api.telegram.org/bot<TOKEN>/getUpdates
                          and reading `result[].message.chat.id`.

## Usage

    from hmm_client.notify import notify_telegram
    notify_telegram("rolling upgrade wave 3/4 complete")
    notify_telegram("auto-rollback fired on swi2", level="error")

`level` only affects the emoji prefix; it doesn't change the destination
or the priority. Levels: `info` (default), `ok`, `warn`, `error`.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

_LEVEL_PREFIX = {
    "info": "ℹ️",
    "ok": "✅",
    "warn": "⚠️",
    "error": "🚨",
}


def _read_config() -> tuple[str, str] | None:
    token = os.environ.get("VESSEL_TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("VESSEL_TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return token, chat_id


def is_configured() -> bool:
    """True if both VESSEL_TG_BOT_TOKEN and VESSEL_TG_CHAT_ID are set."""
    return _read_config() is not None


def notify_telegram(
    message: str,
    *,
    level: str = "info",
    timeout_seconds: float = 5.0,
) -> bool:
    """Send a Telegram message. Silent no-op if not configured.

    Returns True on successful HTTP 200 from Telegram, False otherwise.
    Never raises — notification failure must not break the caller's
    primary operation.
    """
    cfg = _read_config()
    if cfg is None:
        log.debug("Telegram notification skipped (env not configured)")
        return False
    token, chat_id = cfg

    prefix = _LEVEL_PREFIX.get(level.lower(), _LEVEL_PREFIX["info"])
    text = f"{prefix} {message}"
    if len(text) > 4000:
        # Telegram's hard limit is 4096; leave room for the prefix and
        # ellipsis so we don't silently truncate.
        text = text[:3990] + "…"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=timeout_seconds,
        )
        if resp.status_code == 200:
            return True
        log.warning(
            "Telegram send failed: HTTP %d — %s",
            resp.status_code,
            resp.text[:200],
        )
        return False
    except (httpx.HTTPError, OSError) as exc:
        log.warning("Telegram send raised: %s", exc)
        return False


__all__ = ["is_configured", "notify_telegram"]
