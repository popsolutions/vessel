"""Centralised logging configuration — structlog + stdlib bridge.

Emits **JSON** in production (one log line = one JSON object, ready for
Loki/Datadog/Splunk ingestion) and **pretty rendered** lines with
colours in development. The choice is driven by the `VESSEL_LOG_JSON`
env var so dev defaults stay readable without explicit setup.

Existing modules continue to use stdlib `logging.getLogger(__name__)` —
the `setup()` call below installs a stdlib handler that routes
through structlog's `ProcessorFormatter`, so legacy callers
participate in the structured pipeline without changing every
log line.

## Usage

    from hmm_client.logging_config import setup
    setup()                           # call once at process start

    # then anywhere:
    import logging
    log = logging.getLogger(__name__)
    log.info("blade powered off", extra={"slot": 3, "action": "off"})

    # structlog-native callers get richer context binding:
    import structlog
    log = structlog.get_logger()
    log.info("blade powered off", slot=3, action="off")

## Configuration

    VESSEL_LOG_JSON   "1" / "true" / "yes"  →  JSON renderer
                      anything else (or unset) → pretty (dev) renderer
    VESSEL_LOG_LEVEL  Standard level name (DEBUG/INFO/WARNING/ERROR).
                      Default: INFO.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

_CONFIGURED = False


def _is_json_mode() -> bool:
    return os.environ.get("VESSEL_LOG_JSON", "").lower() in ("1", "true", "yes", "on")


def _level() -> int:
    name = os.environ.get("VESSEL_LOG_LEVEL", "INFO").upper().strip()
    return getattr(logging, name, logging.INFO)


def setup(*, force: bool = False) -> None:
    """Configure structlog + stdlib logging. Idempotent — safe to call
    from multiple entry points (CLI, uvicorn, tests). `force=True`
    re-configures even if already done (used by tests)."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    json_mode = _is_json_mode()
    level = _level()

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    # Shared processor chain — applied to BOTH structlog calls and
    # stdlib log records that pass through ProcessorFormatter.
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
    ]

    if json_mode:
        # Production: one JSON object per line, ready for log shippers.
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        # Dev: human-readable, colour when stderr is a tty.
        renderer = structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty(),
            exception_formatter=structlog.dev.plain_traceback,
        )

    # structlog itself does NOT call the final renderer — it hands the
    # event off to the stdlib bridge via `wrap_for_formatter`, and the
    # `ProcessorFormatter` below applies the renderer exactly once.
    # Without this split, structlog-native calls get JSON-encoded
    # twice (once by structlog, once by the stdlib formatter).
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Stdlib bridge: legacy `logging.getLogger(__name__)` calls + the
    # structlog-native calls both flow through here. The `foreign_pre_chain`
    # runs the shared processors on stdlib records (which haven't been
    # touched by structlog); the `processors` list is what actually
    # produces the final output.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace any existing handlers so uvicorn's default and ours don't
    # double-print.
    root.handlers = [handler]
    root.setLevel(level)

    # Tame chatty third-party loggers in production.
    if json_mode:
        for noisy in ("httpx", "httpcore", "uvicorn.access"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def is_configured() -> bool:
    return _CONFIGURED


__all__ = ["setup", "is_configured"]
