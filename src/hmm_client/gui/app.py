"""FastAPI app — local web GUI for the hmm CLI.

Mirrors the CLI's surface: list, power, boot, snapshot, vmedia mount.
HTMX-driven server-rendered HTML; Tailwind via CDN; Jinja2 templates.

Run via `hmm gui` (CLI subcommand) or:
    uvicorn hmm_client.gui.app:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import asyncio
import os
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .. import ops
from ..config import Settings
from ..snapshot import run_snapshot

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

app = FastAPI(title="hmm — Huawei E9000 web", version="0.0.1")

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
    return templates.TemplateResponse(request, "index.html", {
        "blades": blades,
        "switches": switches,
        "host": s.hmm_host,
        "default_host": Settings.load().hmm_host,
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "tasks": list(_tasks.values()),
        "inventory_error": err,
    })


@app.post("/api/host")
def set_host(request: Request, host: str = Form("")) -> JSONResponse:
    """Persist the active chassis host as a cookie. Empty resets to .env."""
    response = JSONResponse({"ok": True,
                             "host": host.strip() or Settings.load().hmm_host})
    if host.strip():
        response.set_cookie(_HOST_COOKIE, host.strip(),
                            max_age=60 * 60 * 24 * 365, samesite="lax")
    else:
        response.delete_cookie(_HOST_COOKIE)
    return response


@app.get("/api/inventory", response_class=HTMLResponse)
def fragment_inventory(request: Request) -> HTMLResponse:
    s = _settings(request)
    blades, switches = ops.list_inventory(s)
    return templates.TemplateResponse(request, "_grid.html", {
        "blades": blades, "switches": switches,
    })


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
    return JSONResponse({
        "ok": True, "slot": slot, "device": device,
        "set_output": out, "cycle_output": cycled,
    })


@app.post("/api/snapshot")
def snapshot_start(request: Request) -> JSONResponse:
    task_id = f"snap-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    s = _settings(request)
    with _tasks_lock:
        _tasks[task_id] = {"id": task_id, "kind": "snapshot",
                           "host": s.hmm_host, "state": "running"}

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
            "id": task_id, "kind": "vmedia", "slot": slot, "iso": iso_path,
            "state": "running", "cancel": cancel_event,
        }

    def _run() -> None:
        try:
            mount_iso(s, slot=slot, iso_path=iso_path,
                      auto_hard_reset=hard_reset,
                      on_idle=cancel_event.is_set)
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
            "id": task_id, "kind": "sol", "slot": slot, "state": "running",
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
            t for t in _tasks.values()
            if t.get("kind") == "sol" and t.get("slot") == slot
        ]
    if not candidates:
        return HTMLResponse(
            "<p class='text-xs text-slate-500'>"
            "no SOL capture yet — click <em>Capture SOL</em>."
            "</p>"
        )
    last = candidates[-1]  # tasks dict is insertion-ordered
    state = last.get("state")
    if state == "running":
        return HTMLResponse(
            f"<p class='text-xs text-amber-400'>● capturing... task {last['id']}"
            f" (~60-180s; refreshes automatically)</p>"
        )
    if state == "error":
        return HTMLResponse(
            f"<p class='text-xs text-rose-400'>✗ {last.get('error', 'error')}</p>"
        )
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
    return templates.TemplateResponse(request, "kvm.html", {
        "slot": slot, "host": _settings(request).hmm_host,
    })


@app.websocket("/api/blade/{slot}/kvm/ws")
async def kvm_ws(ws: WebSocket, slot: int) -> None:
    """Stream decoded KVM frames as PNG bytes.

    K3 mode: replays bytes from `HMM_KVM_REPLAY_CAPTURE` env var (path
    to a `.bin` server→client dump). When unset, falls back to
    `./captures/stream_4_s2c.bin` if present.

    K3.5 will swap the source for a live `KvmClient.frames()` generator
    without changing the wire format on this socket.
    """
    await ws.accept()
    cap_env = os.environ.get("HMM_KVM_REPLAY_CAPTURE", "").strip()
    if not cap_env:
        default = Path("captures") / "stream_4_s2c.bin"
        if default.exists():
            cap_env = str(default)
    if not cap_env or not Path(cap_env).exists():
        await ws.send_json({
            "type": "error",
            "message": ("no replay capture configured; set "
                        "HMM_KVM_REPLAY_CAPTURE=/path/to/stream.bin or "
                        "drop a .bin into ./captures/. Live mode = K3.5."),
        })
        await ws.close()
        return

    from ..kvm.replay import replay_capture_to_pngs

    frame_delay = float(os.environ.get("HMM_KVM_REPLAY_FPS_DELAY", "0.5"))
    try:
        await ws.send_json({"type": "info",
                            "message": f"replay slot={slot} src={cap_env}"})
        while True:  # loop the capture forever so the page stays animated
            frames_sent = 0
            for img_id, png in replay_capture_to_pngs(cap_env):
                await ws.send_json({"type": "frame", "img_id": img_id,
                                    "size": len(png)})
                await ws.send_bytes(png)
                frames_sent += 1
                await asyncio.sleep(frame_delay)
            await ws.send_json({"type": "info",
                                "message": f"replay loop done "
                                           f"({frames_sent} frames); restarting"})
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return


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
    return templates.TemplateResponse(
        request, "_tasks.html", {"tasks": list(_tasks.values())}
    )


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Boot uvicorn; optionally open the browser."""
    import uvicorn
    if open_browser:
        url = f"http://{host}:{port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
