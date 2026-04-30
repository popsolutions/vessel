"""FastAPI app — local web GUI for the hmm CLI.

Mirrors the CLI's surface: list, power, boot, snapshot, vmedia mount.
HTMX-driven server-rendered HTML; Tailwind via CDN; Jinja2 templates.

Run via `hmm gui` (CLI subcommand) or:
    uvicorn hmm_client.gui.app:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
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


def _settings() -> Settings:
    return Settings.load()


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    s = _settings()
    blades, switches = ops.list_inventory(s)
    return templates.TemplateResponse(request, "index.html", {
        "blades": blades,
        "switches": switches,
        "host": s.hmm_host,
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "tasks": list(_tasks.values()),
    })


@app.get("/api/inventory", response_class=HTMLResponse)
def fragment_inventory(request: Request) -> HTMLResponse:
    s = _settings()
    blades, switches = ops.list_inventory(s)
    return templates.TemplateResponse(request, "_grid.html", {
        "blades": blades, "switches": switches,
    })


@app.get("/api/blade/{slot}/bootdev", response_class=HTMLResponse)
def fragment_bootdev(slot: int) -> HTMLResponse:
    out = ops.get_boot_device(_settings(), slot)
    return HTMLResponse(f"<code class='text-xs text-emerald-300'>{out.strip()}</code>")


@app.post("/api/blade/{slot}/power/{action}")
def power(slot: int, action: str) -> JSONResponse:
    if action not in ops.POWER_VALUES and action not in ops.RESET_VALUES:
        raise HTTPException(400, f"unknown action {action!r}")
    out = ops.power(_settings(), slot, action)
    return JSONResponse({"ok": True, "slot": slot, "action": action, "output": out})


@app.post("/api/blade/{slot}/boot/{device}")
def boot(slot: int, device: str, reboot: bool = False) -> JSONResponse:
    if device not in ops.BOOT_DEVICES:
        raise HTTPException(400, f"unknown device {device!r}")
    s = _settings()
    out = ops.set_boot_device(s, slot, device)
    cycled = ""
    if reboot:
        cycled = ops.power(s, slot, "cycle")
    return JSONResponse({
        "ok": True, "slot": slot, "device": device,
        "set_output": out, "cycle_output": cycled,
    })


@app.post("/api/snapshot")
def snapshot_start() -> JSONResponse:
    task_id = f"snap-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    with _tasks_lock:
        _tasks[task_id] = {"id": task_id, "kind": "snapshot", "state": "running"}

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
    slot: int = Form(...),
    iso_path: str = Form(...),
    hard_reset: bool = Form(False),
) -> JSONResponse:
    from ..vmedia.client import mount_iso

    task_id = f"vm-{slot}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    cancel_event = threading.Event()
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id, "kind": "vmedia", "slot": slot, "iso": iso_path,
            "state": "running", "cancel": cancel_event,
        }

    def _run() -> None:
        try:
            mount_iso(_settings(), slot=slot, iso_path=iso_path,
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


@app.post("/api/vmedia/{slot}/release")
def vmedia_release(slot: int, hard: bool = False) -> JSONResponse:
    from ..vmedia.client import force_release_vmedia
    force_release_vmedia(_settings(), slot, hard_reset=hard)
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
