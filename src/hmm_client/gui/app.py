"""FastAPI app — local web GUI for the hmm CLI.

Mirrors the CLI's surface: list, power, boot, snapshot, vmedia mount.
HTMX-driven server-rendered HTML; Tailwind via CDN; Jinja2 templates.

Run via `hmm gui` (CLI subcommand) or:
    uvicorn hmm_client.gui.app:app --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .. import auth as _auth
from .. import ops
from ..config import Settings
from ..snapshot import run_snapshot

_log = logging.getLogger(__name__)


_HEALTH_PATHS = frozenset(("/healthz", "/readyz"))


def _gui_auth(
    request: Request,
    creds: HTTPBasicCredentials | None = Depends(_auth.security),
) -> str:
    """Global FastAPI dependency: HTTP Basic Auth when configured.

    When `VESSEL_GUI_PASSWORD_HASH` is set, every request must carry
    valid Basic credentials. When unset, returns "anonymous" so route
    handlers can still record an actor in the audit log.

    Health-check endpoints (`/healthz`, `/readyz`) bypass auth so
    Kubernetes / load balancers / uptime probes can poll without
    credentials. They reveal nothing actionable beyond reachability.
    """
    if request.url.path in _HEALTH_PATHS:
        return "healthcheck"
    return _auth.require_auth(creds=creds)


if not _auth.is_enabled():
    # Loud startup warning so operators don't accidentally ship a GUI
    # without auth. We don't refuse to start — zero-config dev-mode is
    # important — but this banner needs to be impossible to miss.
    _log.warning(
        "GUI authentication is DISABLED — anyone with network access "
        "to this port can mutate chassis state. Set "
        "VESSEL_GUI_PASSWORD_HASH (use `hmm gui-password-hash`) "
        "before exposing beyond 127.0.0.1."
    )

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

app = FastAPI(
    title="hmm — Huawei E9000 web",
    version="0.0.1",
    # Global Basic Auth dependency — enforced when
    # VESSEL_GUI_PASSWORD_HASH is set; returns "anonymous" otherwise.
    # WebSocket routes accept the dependency too but don't enforce
    # Basic on their handshake (clients should send a token-bearing
    # query parameter instead — TODO follow-up).
    dependencies=[Depends(_gui_auth)],
)

_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()

# Per-(host, slot) power-state cache. SEL probes are slow (~10-15s each)
# so we keep the answer for a short while; the frontend re-asks on its
# own clock.
_powerstate_cache: dict[tuple[str, int], tuple[float, str]] = {}
_powerstate_ttl_seconds = 30.0
_powerstate_lock = threading.Lock()


_HOST_COOKIE = "hmm_host"


def _settings(request: Request | None = None) -> Settings:
    """Build a Settings, honouring a chassis-host cookie when present."""
    host = None
    if request is not None:
        host = request.cookies.get(_HOST_COOKIE)
    return Settings.load(host_override=host)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe — always 200 if the process can serve requests.

    No external checks; Kubernetes restarts the pod when this fails,
    so it must not depend on chassis reachability or any blocking
    network call. Bypasses auth (see `_gui_auth`).
    """
    return JSONResponse({"status": "ok", "service": "vessel"})


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness probe — 200 only when chassis-touching ops would
    plausibly succeed. Specifically:

      - HMM host is reachable on the configured port (TCP connect)
      - Audit log path is writable (test-write a probe record)

    Returns 503 with a `failing` list when any check fails.
    Kubernetes / load balancers should remove failing instances from
    rotation but NOT restart them — `/healthz` is the restart signal.
    Bypasses auth.
    """
    import socket as _socket

    from .. import audit as _audit

    failing: list[str] = []
    s = _settings()

    # 1. HMM TCP reachability — quick connect with short timeout.
    try:
        with _socket.create_connection((s.hmm_host, 443), timeout=2.0):
            pass
    except OSError as exc:
        failing.append(f"hmm-tcp:{exc.__class__.__name__}")

    # 2. Audit log writable — log_op fails-soft and returns False.
    audit_ok = _audit.log_op(
        op="readyz.probe",
        target_kind="hmm",
        target_id=s.hmm_host,
        result="success",
        evidence={"probe": "readyz writability check"},
    )
    if not audit_ok:
        failing.append("audit-log:unwritable")

    if failing:
        return JSONResponse(
            {"status": "not-ready", "failing": failing},
            status_code=503,
        )
    return JSONResponse({"status": "ready", "checks": ["hmm-tcp", "audit-log"]})


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    s = _settings(request)
    blades: list[dict[str, Any]] = []
    switches: list[dict[str, Any]] = []
    err: str | None = None
    try:
        blades, switches = ops.list_inventory(s)
    except Exception as e:  # connection refused, TLS error, wrong host, etc.
        err = f"could not reach chassis at {s.hmm_host}: {e!s}"
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "blades": blades,
            "switches": switches,
            "host": s.hmm_host,
            "default_host": Settings.load().hmm_host,
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "tasks": list(_tasks.values()),
            "inventory_error": err,
        },
    )


@app.get("/api/iso/list")
def iso_list(dir: str = "") -> JSONResponse:
    """List dirs and .iso/.img files under `dir`.

    Used by the /kvm/{slot} ISO picker. Defaults to `$HMM_ISO_DIR` if
    set, else the user's home directory. The GUI binds to 127.0.0.1
    only; same-machine browsing is acceptable here.
    """
    base = dir.strip() or os.environ.get("HMM_ISO_DIR") or str(Path.home())
    p = Path(base).expanduser()
    try:
        p = p.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise HTTPException(400, f"cannot resolve {base!r}: {e!s}")
    if not p.is_dir():
        raise HTTPException(400, f"{p} is not a directory")
    dirs: list[dict[str, str]] = []
    files: list[dict[str, Any]] = []
    try:
        for entry in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif entry.is_file() and entry.suffix.lower() in (".iso", ".img"):
                    files.append(
                        {
                            "name": entry.name,
                            "path": str(entry),
                            "size": entry.stat().st_size,
                        }
                    )
            except OSError:
                continue
    except PermissionError as e:
        raise HTTPException(403, str(e))
    parent = str(p.parent) if p.parent != p else None
    return JSONResponse({"cwd": str(p), "parent": parent, "dirs": dirs, "files": files})


@app.post("/api/host")
def set_host(request: Request, host: str = Form("")) -> JSONResponse:
    """Persist the active chassis host as a cookie. Empty resets to .env."""
    response = JSONResponse({"ok": True, "host": host.strip() or Settings.load().hmm_host})
    if host.strip():
        response.set_cookie(_HOST_COOKIE, host.strip(), max_age=60 * 60 * 24 * 365, samesite="lax")
    else:
        response.delete_cookie(_HOST_COOKIE)
    return response


@app.get("/api/inventory", response_class=HTMLResponse)
def fragment_inventory(request: Request) -> HTMLResponse:
    s = _settings(request)
    blades, switches = ops.list_inventory(s)
    return templates.TemplateResponse(
        request,
        "_grid.html",
        {
            "blades": blades,
            "switches": switches,
        },
    )


@app.get("/api/blade/{slot}/powerstate")
def blade_powerstate(request: Request, slot: int) -> JSONResponse:
    """Return {state: on|off|unknown} for one blade.

    SEL probes are slow (~10-15s); results are cached per (host, slot)
    for `_powerstate_ttl_seconds`. Frontend fans out parallel requests
    after the page renders so colors fill in over 10-30s without
    blocking the initial paint.
    """
    import time as _t

    s = _settings(request)
    key = (s.hmm_host, slot)
    now = _t.monotonic()
    with _powerstate_lock:
        cached = _powerstate_cache.get(key)
    if cached and (now - cached[0]) < _powerstate_ttl_seconds:
        return JSONResponse({"slot": slot, "state": cached[1], "cached": True})
    state = ops.get_power_state(s, slot)
    with _powerstate_lock:
        _powerstate_cache[key] = (now, state)
    return JSONResponse({"slot": slot, "state": state, "cached": False})


@app.get("/api/blade/{slot}/bootdev", response_class=HTMLResponse)
def fragment_bootdev(request: Request, slot: int) -> HTMLResponse:
    out = ops.get_boot_device(_settings(request), slot)
    return HTMLResponse(f"<code class='text-xs text-emerald-300'>{out.strip()}</code>")


@app.post("/api/blade/{slot}/power/{action}")
def power(request: Request, slot: int, action: str) -> JSONResponse:
    if action not in ops.POWER_VALUES and action not in ops.RESET_VALUES:
        raise HTTPException(400, f"unknown action {action!r}")
    out = ops.power(_settings(request), slot, action)
    return JSONResponse({"ok": True, "slot": slot, "action": action, "output": out})


@app.post("/api/blade/{slot}/boot/{device}")
def boot(request: Request, slot: int, device: str, reboot: bool = False) -> JSONResponse:
    if device not in ops.BOOT_DEVICES:
        raise HTTPException(400, f"unknown device {device!r}")
    s = _settings(request)
    out = ops.set_boot_device(s, slot, device)
    cycled = ""
    if reboot:
        cycled = ops.power(s, slot, "cycle")
    return JSONResponse(
        {
            "ok": True,
            "slot": slot,
            "device": device,
            "set_output": out,
            "cycle_output": cycled,
        }
    )


@app.post("/api/snapshot")
def snapshot_start(request: Request) -> JSONResponse:
    task_id = f"snap-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    s = _settings(request)
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id,
            "kind": "snapshot",
            "host": s.hmm_host,
            "state": "running",
        }

    def _run() -> None:
        try:
            out = run_snapshot()
            with _tasks_lock:
                _tasks[task_id]["state"] = "done"
                _tasks[task_id]["result"] = str(out)
        except Exception as e:
            with _tasks_lock:
                _tasks[task_id]["state"] = "error"
                _tasks[task_id]["error"] = str(e)

    threading.Thread(target=_run, name=task_id, daemon=True).start()
    return JSONResponse({"task_id": task_id})


@app.post("/api/vmedia/mount")
def vmedia_mount(
    request: Request,
    slot: int = Form(...),
    iso_path: str = Form(...),
    hard_reset: bool = Form(False),
) -> JSONResponse:
    from ..vmedia.client import mount_iso

    task_id = f"vm-{slot}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    cancel_event = threading.Event()
    s = _settings(request)
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id,
            "kind": "vmedia",
            "slot": slot,
            "iso": iso_path,
            "state": "running",
            "cancel": cancel_event,
        }

    def _run() -> None:
        try:
            mount_iso(
                s,
                slot=slot,
                iso_path=iso_path,
                auto_hard_reset=hard_reset,
                on_idle=cancel_event.is_set,
            )
            with _tasks_lock:
                _tasks[task_id]["state"] = "ended"
        except Exception as e:
            with _tasks_lock:
                _tasks[task_id]["state"] = "error"
                _tasks[task_id]["error"] = str(e)

    threading.Thread(target=_run, name=task_id, daemon=True).start()
    return JSONResponse({"task_id": task_id})


@app.post("/api/blade/{slot}/sol/start")
def sol_start(request: Request, slot: int, refresh: bool = True) -> JSONResponse:
    """Capture the iBMC's SOL buffer in a background task. Slow (~60-180s)."""
    task_id = f"sol-{slot}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    s = _settings(request)
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id,
            "kind": "sol",
            "slot": slot,
            "state": "running",
        }

    def _run() -> None:
        try:
            text = ops.fetch_sol(s, slot, refresh=refresh)
            with _tasks_lock:
                _tasks[task_id]["state"] = "done"
                _tasks[task_id]["sol_text"] = text
                _tasks[task_id]["result"] = f"{len(text)} bytes captured"
        except Exception as e:
            with _tasks_lock:
                _tasks[task_id]["state"] = "error"
                _tasks[task_id]["error"] = str(e)

    threading.Thread(target=_run, name=task_id, daemon=True).start()
    return JSONResponse({"task_id": task_id})


@app.get("/api/blade/{slot}/sol/latest", response_class=HTMLResponse)
def sol_latest(slot: int) -> HTMLResponse:
    """Return the most recent finished SOL capture for a slot, or a status line."""
    with _tasks_lock:
        candidates = [
            t for t in _tasks.values() if t.get("kind") == "sol" and t.get("slot") == slot
        ]
    if not candidates:
        return HTMLResponse(
            "<p class='text-xs text-slate-500'>no SOL capture yet — click <em>Capture SOL</em>.</p>"
        )
    last = candidates[-1]  # tasks dict is insertion-ordered
    state = last.get("state")
    if state == "running":
        return HTMLResponse(
            f"<p class='text-xs text-amber-400'>● capturing... task {last['id']}"
            f" (~60-180s; refreshes automatically)</p>"
        )
    if state == "error":
        return HTMLResponse(f"<p class='text-xs text-rose-400'>✗ {last.get('error', 'error')}</p>")
    text = last.get("sol_text", "(empty)")
    # escape & wrap in <pre>
    import html as _html

    return HTMLResponse(
        f"<div class='text-xs text-slate-400 mb-1'>"
        f"task {last['id']} — {len(text)} bytes</div>"
        f"<pre class='text-[11px] leading-tight bg-black/40 p-2 rounded "
        f"max-h-96 overflow-auto whitespace-pre'>{_html.escape(text)}</pre>"
    )


@app.post("/api/blade/{slot}/kvm/start")
def kvm_start(slot: int) -> JSONResponse:
    """Hand the UI the URL of the live canvas page.

    The real handshake (K3.5) will land at /api/blade/{slot}/kvm/ws.
    Today the WS endpoint streams a captured replay so the canvas
    pipeline can be exercised end-to-end.
    """
    return JSONResponse({"ok": True, "url": f"/kvm/{slot}"})


@app.get("/kvm/{slot}", response_class=HTMLResponse)
def kvm_canvas(request: Request, slot: int) -> HTMLResponse:
    """Full-screen KVM canvas — connects to /api/blade/{slot}/kvm/ws."""
    return templates.TemplateResponse(
        request,
        "kvm.html",
        {
            "slot": slot,
            "host": _settings(request).hmm_host,
        },
    )


@app.websocket("/api/blade/{slot}/kvm/ws")
async def kvm_ws(ws: WebSocket, slot: int) -> None:
    """Stream decoded KVM frames as PNG bytes.

    Two modes, switched via the `live` query parameter:
      * `?live=1` (default when env HMM_KVM_LIVE_DEFAULT=1) — runs the
        full handshake against the chassis and forwards each decoded
        frame as PNG. On handshake failure, falls back to replay mode
        with a banner explaining what broke.
      * `?live=0` — replays a captured `.bin` from $HMM_KVM_REPLAY_CAPTURE
        (or `./captures/stream_4_s2c.bin`); used for offline UI work.
    """
    await ws.accept()
    qs = dict(ws.query_params)
    default_live = os.environ.get("HMM_KVM_LIVE_DEFAULT", "1") == "1"
    want_live = qs.get("live", "1" if default_live else "0") == "1"

    # Codec selector — `?codec=newrle` opts into the experimental
    # NewRLE/JPEG path; `?codec=oldrle` forces the known-good OldRLE.
    # Anything else (or unset) defers to the HMM_KVM_USE_NEWRLE env
    # var (which itself defaults to OldRLE when unset).
    codec_qs = qs.get("codec", "").lower()
    if codec_qs == "newrle":
        use_newrle: bool | None = True
    elif codec_qs == "oldrle":
        use_newrle = False
    else:
        use_newrle = None

    s = _settings_from_ws(ws)

    if want_live:
        try:
            await _stream_live(ws, s, slot, use_newrle=use_newrle)
            return
        except WebSocketDisconnect:
            return
        except Exception as e:
            log_msg = f"live handshake failed for slot {slot}: {e!s}"
            try:
                await ws.send_json(
                    {"type": "info", "message": log_msg + " — falling back to replay"}
                )
            except Exception:
                return

    # replay fallback
    cap_env = os.environ.get("HMM_KVM_REPLAY_CAPTURE", "").strip()
    if not cap_env:
        default = Path("captures") / "stream_4_s2c.bin"
        if default.exists():
            cap_env = str(default)
    if not cap_env or not Path(cap_env).exists():
        try:
            await ws.send_json(
                {
                    "type": "error",
                    "message": (
                        "no replay capture configured and live failed; "
                        "set HMM_KVM_REPLAY_CAPTURE=/path/to/stream.bin "
                        "or drop a .bin into ./captures/."
                    ),
                }
            )
            await ws.close()
        except Exception:
            pass
        return

    from ..kvm.replay import replay_capture_to_pngs

    frame_delay = float(os.environ.get("HMM_KVM_REPLAY_FPS_DELAY", "0.5"))
    try:
        await ws.send_json({"type": "info", "message": f"replay slot={slot} src={cap_env}"})
        while True:
            frames_sent = 0
            for img_id, png in replay_capture_to_pngs(cap_env):
                await ws.send_json(
                    {"type": "frame", "img_id": img_id, "size": len(png), "source": "replay"}
                )
                await ws.send_bytes(png)
                frames_sent += 1
                await asyncio.sleep(frame_delay)
            await ws.send_json(
                {"type": "info", "message": f"replay loop done ({frames_sent} frames); restarting"}
            )
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return


def _settings_from_ws(ws: WebSocket) -> Settings:
    """Pull the chassis-host cookie off the WebSocket scope."""
    cookie_header = ""
    for k, v in ws.scope.get("headers", []):
        if k == b"cookie":
            cookie_header = v.decode(errors="replace")
            break
    host = None
    for piece in cookie_header.split(";"):
        piece = piece.strip()
        if piece.startswith(_HOST_COOKIE + "="):
            host = piece.split("=", 1)[1]
            break
    return Settings.load(host_override=host)


async def _stream_live(
    ws: WebSocket, s: Settings, slot: int, *, use_newrle: bool | None = None
) -> None:
    """Run the live KVM handshake, push decoded PNGs out and accept
    keyboard/mouse input from the browser.

    The KvmClient does blocking I/O on its sockets, so we bridge:
      * frames: pump `iter_pngs(cli)` (a blocking generator) via the
        default executor → WebSocket.send_bytes
      * input: read text frames off the WS, validate, dispatch to
        `cli.send_key_report` / `cli.send_mouse_abs` (also blocking
        TCP send) via the executor.

    Either side completing or the WS disconnecting tears the other
    side down via the cli.close().
    """
    from ..kvm.client import iter_pngs, open_live_session

    loop = asyncio.get_running_loop()
    await ws.send_json(
        {"type": "info", "message": f"live: connecting host={s.hmm_host} slot={slot}"}
    )

    cli = await loop.run_in_executor(
        None,
        lambda: open_live_session(
            s.hmm_host,
            s.hmm_user,
            s.hmm_password,
            slot,
            verify_tls=s.verify_tls,
            use_newrle=use_newrle,
        ),
    )

    # Coalescing slot: the decoder thread pumps frames at chassis speed
    # (~30 fps) and keeps overwriting `latest` with the freshest one.
    # The WS task only sends what's currently in the slot — if the
    # browser is slow, intermediate frames are silently dropped instead
    # of queueing minutes of stale screens behind a slow socket. This
    # is what keeps us in sync with reality the way the Java applet does.
    latest: list[tuple[int, bytes] | None] = [None]
    decoder_done = False
    new_frame = asyncio.Event()

    def _decoder_thread() -> None:
        nonlocal decoder_done
        try:
            for item in iter_pngs(cli):
                latest[0] = item
                loop.call_soon_threadsafe(new_frame.set)
        finally:
            decoder_done = True
            loop.call_soon_threadsafe(new_frame.set)

    async def _frame_pump() -> None:
        loop.run_in_executor(None, _decoder_thread)
        while True:
            await new_frame.wait()
            new_frame.clear()
            item = latest[0]
            latest[0] = None
            if item is None:
                if decoder_done:
                    await ws.send_json({"type": "info", "message": "live stream ended"})
                    return
                continue
            img_id, png = item
            # Pull native dims out of the PNG IHDR for the status bar — no
            # full PIL decode needed, and PNG dims are at fixed offsets.
            w = h = 0
            if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
                w = int.from_bytes(png[16:20], "big")
                h = int.from_bytes(png[20:24], "big")
            await ws.send_json(
                {
                    "type": "frame",
                    "img_id": img_id,
                    "size": len(png),
                    "source": "live",
                    "w": w,
                    "h": h,
                }
            )
            await ws.send_bytes(png)

    async def _input_pump() -> None:
        while True:
            text = await ws.receive_text()
            try:
                msg = _parse_input_msg(text)
            except ValueError as e:
                await ws.send_json({"type": "info", "message": f"bad input msg: {e}"})
                continue
            if msg is None:
                continue
            kind, args = msg
            try:
                if kind == "key":
                    await loop.run_in_executor(
                        None, lambda r=args["report"]: cli.send_key_report(r)
                    )
                elif kind == "mouse":
                    await loop.run_in_executor(
                        None,
                        lambda a=args: cli.send_mouse_abs(
                            a["x"], a["y"], a["buttons"], a.get("wheel", 0)
                        ),
                    )
            except OSError as e:
                await ws.send_json({"type": "info", "message": f"input send failed: {e}"})
                return

    frame_task = asyncio.create_task(_frame_pump())
    input_task = asyncio.create_task(_input_pump())
    try:
        done, pending = await asyncio.wait(
            {frame_task, input_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                _log.warning("KVM WS slot=%s task ended with %r", slot, exc, exc_info=exc)
            else:
                _log.info("KVM WS slot=%s task ended cleanly (decoder_done=%s)", slot, decoder_done)
    finally:
        await loop.run_in_executor(None, cli.close)
        _log.info("KVM WS slot=%s session closed", slot)


def _parse_input_msg(text: str) -> tuple[str, dict[str, Any]] | None:
    """Validate one client→server text message off the KVM WebSocket.

    Two shapes:
      `{type:"key",  report:[m,0,k1,k2,k3,k4,k5,k6]}`  — 8-byte HID report
      `{type:"mouse", x:0..3000, y:0..3000, buttons:0..7, wheel:-128..127}`
    """
    import json

    msg = json.loads(text)
    kind = msg.get("type")
    if kind == "key":
        report = msg.get("report")
        if not isinstance(report, list) or len(report) != 8:
            raise ValueError("key.report must be 8-byte list")
        if not all(isinstance(b, int) and 0 <= b <= 0xFF for b in report):
            raise ValueError("key.report bytes out of range")
        return "key", {"report": bytes(report)}
    if kind == "mouse":
        try:
            x = int(msg["x"])
            y = int(msg["y"])
            buttons = int(msg["buttons"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"mouse missing field: {e}")
        wheel = int(msg.get("wheel", 0))
        x = max(0, min(0xFFFF, x))
        y = max(0, min(0xFFFF, y))
        wheel = max(-128, min(127, wheel)) & 0xFF
        return "mouse", {"x": x, "y": y, "buttons": buttons & 0xFF, "wheel": wheel}
    return None


@app.post("/api/vmedia/{slot}/release")
def vmedia_release(request: Request, slot: int, hard: bool = False) -> JSONResponse:
    from ..vmedia.client import force_release_vmedia

    force_release_vmedia(_settings(request), slot, hard_reset=hard)
    return JSONResponse({"ok": True, "slot": slot})


@app.post("/api/task/{task_id}/cancel")
def task_cancel(task_id: str) -> JSONResponse:
    with _tasks_lock:
        t = _tasks.get(task_id)
        if not t:
            raise HTTPException(404)
        ev = t.get("cancel")
        if ev:
            ev.set()
            t["state"] = "cancelling"
    return JSONResponse({"ok": True})


@app.get("/api/tasks", response_class=HTMLResponse)
def fragment_tasks(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_tasks.html", {"tasks": list(_tasks.values())})


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Boot uvicorn; optionally open the browser."""
    import uvicorn

    if open_browser:
        url = f"http://{host}:{port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
