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

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import auth as _auth
from .. import ops
from ..config import Settings
from ..hmm_web import (
    FirmwareModule,
    HealthModule,
    HMMWebClient,
    HMMWebError,
    InventoryModule,
    ManifestError,
    UpgradeTarget,
    diff as _manifest_diff,
    parse_manifest,
)
from ..hmm_web.firmware import bladelist as _bladelist_encode
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
STATIC_DIR = Path(__file__).parent / "static"
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

# Static assets — Huawei-style CSS, future sprite images, etc.
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()

# Per-(host, slot) power-state cache. SEL probes are slow (~10-15s each)
# so we keep the answer for a short while; the frontend re-asks on its
# own clock.
_powerstate_cache: dict[tuple[str, int], tuple[float, str]] = {}
_powerstate_ttl_seconds = 30.0
_powerstate_lock = threading.Lock()

# Chassis inventory TTL cache (Redfish round-trip is ~13s per fetch).
# Keyed by (hmm_host); refresh button can be wired later to force-flush.
_inventory_cache: dict[str, tuple[float, list[dict[str, Any]], list[dict[str, Any]]]] = {}
_inventory_ttl_seconds = 60.0
_inventory_lock = threading.Lock()


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
    import time as _t

    s = _settings(request)
    blades: list[dict[str, Any]] = []
    switches: list[dict[str, Any]] = []
    err: str | None = None
    now = _t.monotonic()
    with _inventory_lock:
        cached = _inventory_cache.get(s.hmm_host)
    if cached and (now - cached[0]) < _inventory_ttl_seconds:
        blades, switches = cached[1], cached[2]
    else:
        try:
            blades, switches = ops.list_inventory(s)
            with _inventory_lock:
                _inventory_cache[s.hmm_host] = (now, blades, switches)
        except Exception as e:  # connection refused, TLS error, wrong host, etc.
            err = f"could not reach chassis at {s.hmm_host}: {e!s}"
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "active_nav": "chassis",
            "blades": blades,
            "switches": switches,
            "host": s.hmm_host,
            "default_host": Settings.load().hmm_host,
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "tasks": list(_tasks.values()),
            "inventory_error": err,
        },
    )


def _stub_page(request: Request, *, active_nav: str, title: str, blurb: str) -> HTMLResponse:
    """Render a placeholder page for HMM menu items not yet built out."""
    s = _settings(request)
    return templates.TemplateResponse(
        request,
        "_stub.html",
        {
            "active_nav": active_nav,
            "page_title": title,
            "blurb": blurb,
            "host": s.hmm_host,
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )


def _page(request: Request, *, active_nav: str, template: str) -> HTMLResponse:
    s = _settings(request)
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_nav": active_nav,
            "host": s.hmm_host,
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )


@app.get("/chassis-settings", response_class=HTMLResponse)
def chassis_settings_page(request: Request) -> HTMLResponse:
    return _page(request, active_nav="chassis-settings", template="chassis_settings.html")


@app.get("/stateless-computing", response_class=HTMLResponse)
def stateless_computing_page(request: Request) -> HTMLResponse:
    return _page(request, active_nav="stateless-computing", template="stateless_computing.html")


@app.get("/psus-fans", response_class=HTMLResponse)
def psus_fans_page(request: Request) -> HTMLResponse:
    return _page(request, active_nav="psus-fans", template="psus_fans.html")


@app.get("/alarm-monitoring", response_class=HTMLResponse)
def alarm_monitoring_page(request: Request) -> HTMLResponse:
    return _page(request, active_nav="alarm-monitoring", template="alarm_monitoring.html")


@app.get("/system-management", response_class=HTMLResponse)
def system_management_page(request: Request) -> HTMLResponse:
    return _page(request, active_nav="system-management", template="system_management.html")


# ----- API endpoints feeding the new menu pages -----


@app.get("/api/chassis-settings/info")
def chassis_settings_info(request: Request) -> JSONResponse:
    """Asset tag + slot aliases + chassis ID — small read fan-out."""
    out: dict[str, Any] = {"asset_tag": None, "slot_aliases": [], "server_time": None, "error": None}
    try:
        with _hmm_web(request) as c:
            tag_r = c.post("userhandler.php", actiontype="getassettag", chassisid="0")
            alias_r = c.post("userhandler.php", actiontype="getslotalias", chassisid="0")
            time_r = c.post("queryhandler.php", actiontype="getServerTime", chassisid="0")
            out["asset_tag_xml"] = tag_r.body
            out["slot_aliases_xml"] = alias_r.body
            out["server_time_xml"] = time_r.body
    except HMMWebError as exc:
        out["error"] = str(exc)
    return JSONResponse(out)


@app.get("/api/stateless-computing/templates", response_class=HTMLResponse)
def stateless_computing_templates(request: Request) -> HTMLResponse:
    """HTMX fragment: policy template list (compute profiles)."""
    rows: list[dict[str, Any]] = []
    err: str | None = None
    try:
        with _hmm_web(request) as c:
            r = c.post(
                "queryhandler.php",
                actiontype="query_policytemplate_list2",
                chassisid="0",
                page="1",
                perpage="10",
                referer_path="/computer_manage.html?chassisid=0",
            )
            if r.root is not None:
                # Defensive: every <template>/<row>/<item> child gets a row;
                # the dispatcher wraps differently across firmware versions.
                for tpl in r.root.iter():
                    if tpl.tag in ("template", "row", "item", "policytemplate"):
                        rows.append(
                            {tag.tag: (tag.text or "") for tag in tpl}
                        )
            if not rows:
                err = "no compute templates defined yet"
    except Exception as exc:
        err = str(exc)
    return templates.TemplateResponse(
        request,
        "_compute_templates.html",
        {"rows": rows, "error": err},
    )


@app.get("/api/psus-fans/state", response_class=HTMLResponse)
def psus_fans_state(request: Request) -> HTMLResponse:
    """HTMX fragment: PSU + fan health derived from selhandler.alarm."""
    err: str | None = None
    psus: list[dict[str, Any]] = []
    fans: list[dict[str, Any]] = []
    try:
        with _hmm_web(request) as c:
            summary = HealthModule(c).list_alarms()
        # Bucket alarms by source so we can pin them to PSU/Fan tiles.
        by_source: dict[str, list[Any]] = {}
        for a in summary.alarms:
            by_source.setdefault(a.source, []).append(a)
        for n in (1, 2, 3, 4, 5):
            key = f"PS{n}"
            matched = [a for a in summary.alarms if key in a.sensorname]
            psus.append({"name": f"PSU{n}", "alarms": matched})
        for n in (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 13):
            matched = [a for a in summary.alarms if f"Fan{n}" in a.sensorname]
            fans.append({"name": f"Fan{n}", "alarms": matched})
    except HMMWebError as exc:
        err = str(exc)
    return templates.TemplateResponse(
        request,
        "_psus_fans_state.html",
        {"psus": psus, "fans": fans, "error": err},
    )


@app.get("/api/system-management/audit", response_class=HTMLResponse)
def system_management_audit(request: Request, n: int = 50) -> HTMLResponse:
    """HTMX fragment: full audit tail (not filtered)."""
    from .. import audit as _audit

    try:
        records = _audit.read_records()
    except Exception as exc:
        return HTMLResponse(
            f'<div class="hmm-alert hmm-alert-error">audit error: {exc}</div>'
        )
    records = records[-max(n, 1):][::-1]
    return templates.TemplateResponse(
        request,
        "_firmware_audit.html",
        {"records": records},
    )


@app.get("/chassis-management", response_class=HTMLResponse)
def chassis_management_page(request: Request) -> HTMLResponse:
    s = _settings(request)
    return templates.TemplateResponse(
        request,
        "chassis_management.html",
        {
            "active_nav": "chassis-management",
            "host": s.hmm_host,
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
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


# ===== HMM proprietary web-API: firmware upgrade panel =====


def _hmm_web(request: Request) -> HMMWebClient:
    """Open and login an HMMWebClient for the active chassis."""
    s = _settings(request)
    c = HMMWebClient.from_settings(s)
    c.login()
    return c


@app.get("/firmware-web", response_class=HTMLResponse)
def firmware_web_page(request: Request) -> HTMLResponse:
    s = _settings(request)
    return templates.TemplateResponse(
        request,
        "firmware_web.html",
        {
            "active_nav": "chassis-management",
            "host": s.hmm_host,
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )


@app.get("/api/firmware-web/inventory", response_class=HTMLResponse)
def firmware_web_inventory(request: Request) -> HTMLResponse:
    """HTMX fragment: table of every component's firmware versions."""
    err: str | None = None
    versions: list[Any] = []
    try:
        with _hmm_web(request) as c:
            versions = InventoryModule(c).list_versions()
    except (HMMWebError, Exception) as exc:
        err = str(exc)
    return templates.TemplateResponse(
        request,
        "_firmware_inventory.html",
        {"versions": versions, "error": err},
    )


@app.post("/api/firmware-web/upload")
async def firmware_web_upload(
    request: Request,
    actor: str = Depends(_gui_auth),
    file: UploadFile = None,  # type: ignore[assignment]
) -> JSONResponse:
    """Upload a firmware image (.hpm) into the HMM upload buffer."""
    if file is None or not file.filename:
        raise HTTPException(400, "no file provided")
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(400, "empty file")
    s = _settings(request)
    from .. import audit as _audit

    try:
        with _hmm_web(request) as c:
            FirmwareModule(c).upload(file.filename, image_bytes)
    except HMMWebError as exc:
        _audit.log_op(
            op="firmware-web.upload",
            target_kind="hmm",
            target_id=s.hmm_host,
            result="failed",
            actor=actor,
            evidence={"filename": file.filename, "bytes": len(image_bytes), "error": str(exc)},
        )
        raise HTTPException(502, f"hmm upload failed: {exc}")
    _audit.log_op(
        op="firmware-web.upload",
        target_kind="hmm",
        target_id=s.hmm_host,
        result="success",
        actor=actor,
        evidence={"filename": file.filename, "bytes": len(image_bytes)},
    )
    return JSONResponse({"ok": True, "filename": file.filename, "bytes": len(image_bytes)})


@app.post("/api/firmware-web/apply")
def firmware_web_apply(
    request: Request,
    actor: str = Depends(_gui_auth),
    bladelist: str = Form("", description="Pre-encoded HMM bladelist token"),
    snapshot_first: bool = Form(True),
) -> JSONResponse:
    """Trigger the upgrade against the previously-uploaded image.

    When ``snapshot_first=True`` (default), runs ``run_snapshot()``
    before invoking the flash so we have a recorded chassis state to
    diff against if the upgrade goes sideways. The snapshot id is
    threaded into the audit record so ``snapshot_id`` correlates with
    later ``firmware-web.apply`` events.
    """
    s = _settings(request)
    from .. import audit as _audit

    targets = _parse_bladelist_form(bladelist)
    if not targets:
        raise HTTPException(400, "no valid targets in bladelist")

    snapshot_id: str | None = None
    if snapshot_first:
        try:
            snap_path = run_snapshot()
            snapshot_id = snap_path.name
        except Exception as exc:
            _audit.log_op(
                op="firmware-web.apply",
                target_kind="hmm",
                target_id=s.hmm_host,
                result="failed",
                actor=actor,
                evidence={"bladelist": bladelist, "phase": "snapshot", "error": str(exc)},
            )
            raise HTTPException(500, f"pre-flash snapshot failed: {exc}")

    try:
        with _hmm_web(request) as c:
            FirmwareModule(c).apply(targets)
    except HMMWebError as exc:
        _audit.log_op(
            op="firmware-web.apply",
            target_kind="hmm",
            target_id=s.hmm_host,
            result="failed",
            actor=actor,
            snapshot_id=snapshot_id,
            evidence={"bladelist": bladelist, "phase": "apply", "error": str(exc)},
        )
        raise HTTPException(502, f"hmm apply failed: {exc}")

    _audit.log_op(
        op="firmware-web.apply",
        target_kind="hmm",
        target_id=s.hmm_host,
        result="in-progress",
        actor=actor,
        snapshot_id=snapshot_id,
        evidence={"bladelist": _bladelist_encode(targets)},
    )
    return JSONResponse(
        {
            "ok": True,
            "bladelist": _bladelist_encode(targets),
            "snapshot_id": snapshot_id,
        }
    )


@app.get("/api/firmware-web/status")
def firmware_web_status(request: Request) -> JSONResponse:
    """Poll progress of the running upgrade. Read-only — no audit entry."""
    try:
        with _hmm_web(request) as c:
            st = FirmwareModule(c).status()
    except HMMWebError as exc:
        raise HTTPException(502, f"hmm status failed: {exc}")
    return JSONResponse(
        {
            "all_done": st.all_done,
            "any_failed": st.any_failed,
            "targets": [
                {
                    "name": t.name,
                    "retcode": t.retcode,
                    "desp": t.desp,
                    "progress": t.progress,
                    "progress_desp": t.progress_desp,
                    "is_terminal": t.is_terminal,
                }
                for t in st.targets
            ],
        }
    )


@app.post("/api/firmware-web/manifest-diff", response_class=HTMLResponse)
async def firmware_web_manifest_diff(
    request: Request,
    file: UploadFile = None,  # type: ignore[assignment]
) -> HTMLResponse:
    """Compare an uploaded manifest YAML against the live inventory.

    Returns an HTMX fragment with one row per (component, field) the
    manifest declares, marked ok/mismatch/missing/unknown.
    """
    if file is None or not file.filename:
        raise HTTPException(400, "no manifest file provided")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty manifest")
    try:
        manifest = parse_manifest(raw)
    except ManifestError as exc:
        return templates.TemplateResponse(
            request,
            "_firmware_manifest_diff.html",
            {"error": str(exc), "diff": None},
        )
    try:
        with _hmm_web(request) as c:
            versions = InventoryModule(c).list_versions()
        d = _manifest_diff(versions, manifest)
    except (HMMWebError, Exception) as exc:
        return templates.TemplateResponse(
            request,
            "_firmware_manifest_diff.html",
            {"error": str(exc), "diff": None},
        )
    return templates.TemplateResponse(
        request,
        "_firmware_manifest_diff.html",
        {"error": None, "diff": d, "filename": file.filename},
    )


@app.get("/api/firmware-web/preflight")
def firmware_web_preflight(request: Request) -> JSONResponse:
    """Pre-flight check before apply: chassis-wide upgrade state + SMM checkupgrade.

    Returns:
        ``is_upgrading``: True if any flash is mid-flight (queryhandler.isupdate).
        ``smm_checks``: ``{"1": ok_bool, "2": ok_bool}`` from ``checkupgrade``.

    Useful as a "should I click apply?" guard — surface in the UI to
    block the operator from kicking off a new flash while one is in
    flight.
    """
    try:
        with _hmm_web(request) as c:
            inv = InventoryModule(c)
            is_up = inv.is_upgrading()
            checks: dict[str, bool] = {}
            for smmtype in ("1", "2"):
                r = c.post(
                    "smmupgradehandler.php",
                    actiontype="checkupgrade",
                    smmtype=smmtype,
                    referer_path="/system_manage_smm.html?chassisid=0",
                )
                checks[smmtype] = (r.retcode == 0)
            try:
                alarms = HealthModule(c).list_alarms()
                alarms_summary = {
                    "total": alarms.total,
                    "critical": alarms.critical,
                    "major": alarms.major,
                    "minor": alarms.minor,
                }
            except HMMWebError:
                alarms_summary = None
    except HMMWebError as exc:
        raise HTTPException(502, f"hmm preflight failed: {exc}")
    return JSONResponse(
        {
            "is_upgrading": is_up,
            "smm_checks": checks,
            "alarms": alarms_summary,
        }
    )


@app.get("/api/firmware-web/audit-tail", response_class=HTMLResponse)
def firmware_web_audit_tail(request: Request, n: int = 30) -> HTMLResponse:
    """HTMX fragment: tail of recent firmware-web audit records."""
    from .. import audit as _audit

    try:
        records = _audit.read_records()
    except Exception as exc:
        return HTMLResponse(
            f'<div class="text-xs text-rose-400">audit tail error: {exc}</div>'
        )
    fw_records = [
        r for r in records if isinstance(r.get("op"), str) and r["op"].startswith("firmware-web.")
    ][-max(n, 1):][::-1]  # newest first
    return templates.TemplateResponse(
        request,
        "_firmware_audit.html",
        {"records": fw_records},
    )


@app.post("/api/firmware-web/cancel")
def firmware_web_cancel(
    request: Request,
    actor: str = Depends(_gui_auth),
) -> JSONResponse:
    """Cancel/cleanup the upload buffer or in-flight upgrade."""
    s = _settings(request)
    from .. import audit as _audit

    try:
        with _hmm_web(request) as c:
            FirmwareModule(c).cancel()
    except HMMWebError as exc:
        _audit.log_op(
            op="firmware-web.cancel",
            target_kind="hmm",
            target_id=s.hmm_host,
            result="failed",
            actor=actor,
            evidence={"error": str(exc)},
        )
        raise HTTPException(502, f"hmm cancel failed: {exc}")
    _audit.log_op(
        op="firmware-web.cancel",
        target_kind="hmm",
        target_id=s.hmm_host,
        result="success",
        actor=actor,
    )
    return JSONResponse({"ok": True})


@app.get("/health", response_class=HTMLResponse)
def health_page(request: Request) -> HTMLResponse:
    s = _settings(request)
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "active_nav": "health",
            "host": s.hmm_host,
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )


@app.get("/api/health/alarms", response_class=HTMLResponse)
def health_alarms(request: Request) -> HTMLResponse:
    err: str | None = None
    summary: Any = None
    try:
        with _hmm_web(request) as c:
            summary = HealthModule(c).list_alarms()
    except (HMMWebError, Exception) as exc:
        err = str(exc)
    return templates.TemplateResponse(
        request,
        "_health_alarms.html",
        {"summary": summary, "error": err},
    )


@app.get("/api/health/sel", response_class=HTMLResponse)
def health_sel(request: Request, bladename: str = "smm", perpage: int = 50) -> HTMLResponse:
    err: str | None = None
    page: Any = None
    # narrow the bladename to known shapes so we don't proxy arbitrary input
    safe = (
        bladename.strip()
        if bladename.strip() in ("smm", "othersmm")
        or bladename.strip().startswith(("Slot", "Swi"))
        else "smm"
    )
    try:
        with _hmm_web(request) as c:
            page = HealthModule(c).list_sel(bladename=safe, perpage=perpage)
    except (HMMWebError, Exception) as exc:
        err = str(exc)
    return templates.TemplateResponse(
        request,
        "_health_sel.html",
        {"page": page, "error": err, "bladename": safe},
    )


@app.get("/snapshots", response_class=HTMLResponse)
def snapshots_index(request: Request) -> HTMLResponse:
    """List snapshot directories under SNAPSHOTS_DIR."""
    s = _settings(request)
    items: list[dict[str, Any]] = []
    if s.snapshots_dir.is_dir():
        for entry in sorted(
            (p for p in s.snapshots_dir.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        ):
            try:
                size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            except OSError:
                size = 0
            items.append({"id": entry.name, "size": size})
    return templates.TemplateResponse(
        request,
        "snapshots.html",
        {
            "active_nav": "snapshots",
            "host": s.hmm_host,
            "snapshots": items,
            "snapshots_dir": str(s.snapshots_dir),
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )


@app.get("/snapshots/{snapshot_id}", response_class=HTMLResponse)
def snapshot_detail(request: Request, snapshot_id: str) -> HTMLResponse:
    """Show the contents of one snapshot: manifest + file tree."""
    s = _settings(request)
    # Reject any path traversal attempts.
    if "/" in snapshot_id or ".." in snapshot_id:
        raise HTTPException(400, "invalid snapshot id")
    snap_dir = (s.snapshots_dir / snapshot_id).resolve()
    if not str(snap_dir).startswith(str(s.snapshots_dir.resolve())) or not snap_dir.is_dir():
        raise HTTPException(404, f"snapshot not found: {snapshot_id}")
    manifest_text = ""
    manifest_path = snap_dir / "manifest.yaml"
    if manifest_path.is_file():
        try:
            manifest_text = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            manifest_text = f"# read error: {exc}"
    files: list[dict[str, Any]] = []
    for f in sorted(snap_dir.rglob("*")):
        if not f.is_file():
            continue
        try:
            files.append(
                {
                    "rel": str(f.relative_to(snap_dir)),
                    "size": f.stat().st_size,
                }
            )
        except OSError:
            continue
    return templates.TemplateResponse(
        request,
        "snapshot.html",
        {
            "active_nav": "snapshots",
            "host": s.hmm_host,
            "snapshot_id": snapshot_id,
            "manifest": manifest_text,
            "files": files,
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )


def _parse_bladelist_form(token: str) -> list[UpgradeTarget]:
    """Decode a form-submitted bladelist back into typed UpgradeTargets.

    Accepts the same shapes the HMM expects:
        ``bothsmm``                  -> [smm_pair]
        ``Swi2:fru0;Swi3:fru0;``     -> [switch Swi2, switch Swi3]
        ``Slot1:fru0;``              -> [blade Slot1]
    """
    token = token.strip()
    if not token:
        return []
    if token == "bothsmm":
        return [UpgradeTarget.smm_pair()]
    out: list[UpgradeTarget] = []
    for piece in token.split(";"):
        piece = piece.strip()
        if not piece:
            continue
        if ":" in piece:
            name, fru = piece.split(":", 1)
            fru = fru.removeprefix("fru").strip() or "0"
            out.append(UpgradeTarget(bladename=name.strip(), fruid=fru))
        else:
            out.append(UpgradeTarget(bladename=piece))
    return out


# ===== end firmware-web =====


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Boot uvicorn; optionally open the browser."""
    import uvicorn

    if open_browser:
        url = f"http://{host}:{port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
