"""hmm - Linux-native CLI for Huawei E9000 chassis ops.

Subcommands:
  hmm list                          inventory of present blades + switches
  hmm power <slot> on|off|reset|cycle    iBMC-driven power action
  hmm boot  <slot> none|pxe|hdd|cd|floppy [--reboot]
  hmm bootdev <slot>                read current boot-device override
  hmm discover                      walk Redfish (read-only) -> discovery/<stamp>/
  hmm snapshot                      backup HMM + switches -> snapshots/<stamp>/
  hmm drift <snapshot-dir>          compare snapshot vs live
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from . import ops
from .config import Settings
from .discover import main as discover_main
from .restore import detect_drift
from .snapshot import run_snapshot

app = typer.Typer(no_args_is_help=True, help="Huawei E9000 chassis client")
console = Console()


@app.command("list")
def list_cmd(
    show_absent: bool = typer.Option(False, "--all", help="include empty slots"),
) -> None:
    """List populated blades and switches."""
    s = Settings.load()
    with console.status("querying chassis Redfish..."):
        blades, sw = ops.list_inventory(s)

    t = Table(title="Blades")
    t.add_column("Slot", justify="right")
    t.add_column("Model")
    t.add_column("State")
    t.add_column("iBMC IP")
    for b in blades:
        if not show_absent and b["state"] != "Enabled":
            continue
        color = "green" if b["state"] == "Enabled" else "dim"
        t.add_row(str(b["slot"]), b["model"], f"[{color}]{b['state']}[/]", b["ibmc_ip"])
    console.print(t)

    t2 = Table(title="Switches")
    t2.add_column("Slot")
    t2.add_column("Model")
    t2.add_column("State")
    t2.add_column("Mgmt IP")
    for x in sw:
        if not show_absent and x["state"] != "Enabled":
            continue
        color = "green" if x["state"] == "Enabled" else "dim"
        t2.add_row(f"swi{x['slot']}", x["model"], f"[{color}]{x['state']}[/]", x["mgmt_ip"])
    console.print(t2)


@app.command()
def power(
    slot: int = typer.Argument(..., help="blade slot 1..32"),
    action: str = typer.Argument(..., help="on|off|reset|cycle|nmi"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip confirm"),
) -> None:
    """Power on/off/reset/cycle a blade via its iBMC."""
    s = Settings.load()
    ip = ops.ibmc_ip_for_slot(slot)
    console.print(f"[yellow]slot=[/] {slot}  [yellow]iBMC=[/] {ip}  [yellow]action=[/] {action}")
    if action in ("off", "cycle", "reset", "nmi") and not yes:
        if not typer.confirm("apply?", default=False):
            raise typer.Exit(1)
    out = ops.power(s, slot, action)
    console.print(out)


@app.command()
def boot(
    slot: int = typer.Argument(..., help="blade slot"),
    device: str = typer.Argument(..., help="none|pxe|hdd|cd|floppy"),
    reboot: bool = typer.Option(False, "--reboot", help="power-cycle after setting"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip confirm"),
) -> None:
    """Set one-shot boot device override for a blade."""
    s = Settings.load()
    ip = ops.ibmc_ip_for_slot(slot)
    console.print(
        f"[yellow]slot=[/] {slot}  [yellow]iBMC=[/] {ip}  [yellow]device=[/] {device}"
        + ("  [yellow]+power-cycle[/]" if reboot else "")
    )
    if not yes and (reboot or device != "none"):
        if not typer.confirm("apply?", default=False):
            raise typer.Exit(1)
    console.print(ops.set_boot_device(s, slot, device))
    if reboot:
        console.print("[bold]power cycle...[/]")
        console.print(ops.power(s, slot, "cycle"))


@app.command()
def bootdev(slot: int = typer.Argument(..., help="blade slot")) -> None:
    """Read the current boot-device override."""
    s = Settings.load()
    console.print(ops.get_boot_device(s, slot))


@app.command()
def discover() -> None:
    """Read-only Redfish enumeration -> discovery/<UTC-stamp>/."""
    discover_main()


@app.command()
def snapshot() -> None:
    """Backup HMM + switches -> snapshots/<UTC-stamp>/."""
    run_snapshot()


@app.command()
def drift(snapshot_dir: str = typer.Argument(..., help="path to a snapshot dir")) -> None:
    """Compare a snapshot to live state (read-only)."""
    from pathlib import Path

    raise typer.Exit(detect_drift(Path(snapshot_dir)))


@app.command()
def sol(
    slot: int = typer.Argument(..., help="blade slot 1..32"),
    no_refresh: bool = typer.Option(
        False, "--no-refresh", help="re-read previous /tmp/sol.dat without re-capturing"
    ),
    save: str = typer.Option("", "--save", help="also write the buffer to this file"),
) -> None:
    """Capture and dump the iBMC SOL buffer for a blade.

    Runs `ipmcset -d download -v 0` then `ipmcget -d serialrecord -v list`
    on the iBMC. The capture step is slow (~60-180s) — be patient.
    """
    s = Settings.load()
    with console.status(f"capturing SOL for slot {slot}... (this can take 1-3 min)"):
        text = ops.fetch_sol(s, slot, refresh=not no_refresh)
    console.print(text, highlight=False)
    if save:
        from pathlib import Path

        Path(save).write_text(text)
        console.print(f"[green]wrote {len(text)} bytes to {save}[/]")


@app.command("kvm-analyze")
def kvm_analyze(
    capture: str = typer.Argument(..., help="path to a server→client .bin TCP dump"),
    show_tiles: int = typer.Option(
        0, "--show-tiles", help="dump the first N tile tokens of the first image"
    ),
) -> None:
    """Offline analyzer for a captured iKVM server→client byte stream.

    Reports per-frame breakdown, reassembled images, and tile-token stats —
    the K1 deliverable on the road to a working KVM viewer.
    """
    from pathlib import Path
    from .kvm.transport import (
        parse_kvm_stream,
        reassemble_images,
        summarise,
        classify_tiles_jpeg_walk,
    )

    data = Path(capture).read_bytes()
    frames = parse_kvm_stream(data)
    by_op: dict[int, int] = {}
    for fr in frames:
        by_op[fr.op] = by_op.get(fr.op, 0) + 1
    console.print(
        f"[bold]parsed {len(frames)} frames[/] from {len(data)} bytes "
        f"({len(data) - sum(4 + f.body_len for f in frames)} trailing)"
    )
    console.print(
        "[yellow]ops:[/] " + ", ".join(f"0x{op:02x}={n}" for op, n in sorted(by_op.items()))
    )
    images = reassemble_images(frames)
    console.print(f"[bold]reassembled {len(images)} image(s)[/]")
    for img in images:
        s = summarise(img)
        console.print(
            f"  img 0x{img.img_id:02x}: {s['wxh']} {s['size']} B  "
            f"tiles_walked={s['walked_tokens']}/{s['expected_tiles']}  "
            f"types={s['tile_types']}  unwalked={s['unwalked_bytes']} B"
        )
    if show_tiles and images:
        console.print(
            f"\n[yellow]first {show_tiles} tile tokens of img 0x{images[0].img_id:02x}:[/]"
        )
        for tok in list(classify_tiles_jpeg_walk(images[0]))[:show_tiles]:
            console.print(
                f"  #{tok.index:3d}  off={tok.offset:6d}  "
                f"zt={tok.zip_type} rt={tok.r_zip_type}  "
                f"len={tok.length}  body[:8]={tok.body[:8].hex()}"
            )


@app.command("kvm-render")
def kvm_render(
    image_bin: str = typer.Argument(
        ..., help="path to a reassembled image .bin (7004 B for 800x600 POST-screen capture)"
    ),
    out_png: str = typer.Argument(..., help="output PNG path"),
    width: int = typer.Option(800, "--width"),
    height: int = typer.Option(600, "--height"),
) -> None:
    """Decode an OldRLE-compressed image to PNG (K2 deliverable).

    Validates the legacy decoder against captured POST-screen frames.
    Output should look like the BIOS POST screen the chassis was showing
    when the pcap was taken.
    """
    from pathlib import Path
    from .kvm.codec_old import decode_old_rle, bgr233_to_rgb888

    data = Path(image_bin).read_bytes()
    fb = decode_old_rle(data, width, height)
    console.print(
        f"[yellow]decoded {len(data)} B input -> {len(fb)} B framebuffer "
        f"({width}x{height}, BGR233 8bpp)[/]"
    )
    if len(fb) != width * height:
        console.print(f"[red]framebuffer size mismatch — expected {width * height}[/]")
    rgb = bgr233_to_rgb888(fb)
    try:
        from PIL import Image

        img = Image.frombytes("RGB", (width, height), rgb)
        img.save(out_png)
        console.print(f"[green]wrote {out_png} ({width}x{height} RGB888)[/]")
    except ImportError:
        console.print("[red]Pillow not installed; saving raw RGB instead[/]")
        Path(out_png + ".rgb").write_bytes(rgb)


@app.command()
def gui(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="don't open the browser automatically"
    ),
) -> None:
    """Run the local web GUI (FastAPI + HTMX) at http://host:port/."""
    from .gui.app import run as run_gui

    run_gui(host=host, port=port, open_browser=not no_browser)


vmedia_app = typer.Typer(help="VirtualMedia (mount ISO on a blade)")
app.add_typer(vmedia_app, name="vmedia")


@vmedia_app.command("mount")
def vmedia_mount(
    slot: int = typer.Option(..., "--slot", help="blade slot 1..32"),
    iso: str = typer.Option(..., "--iso", help="path to .iso file to expose as virtual CDROM"),
    kvm_port: int = typer.Option(
        2200, "--kvm-port", help="per-blade KVM stream port for codekey nego (default 2200, blade1)"
    ),
    hard_reset: bool = typer.Option(
        False,
        "--hard-reset",
        help="if CN_EXIST persists after soft release, reboot the iBMC "
        "(IPMC only — host CPU/disk are NOT touched, ~30s recovery)",
    ),
    no_auto_release: bool = typer.Option(
        False, "--no-auto-release", help="skip the soft release attempt entirely"
    ),
) -> None:
    """Mount an ISO on a blade as a virtual CDROM."""
    from .vmedia.client import mount_iso

    s = Settings.load()
    try:
        mount_iso(
            s,
            slot=slot,
            iso_path=iso,
            kvm_port=kvm_port,
            auto_release=not no_auto_release,
            auto_hard_reset=hard_reset,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Ctrl-C — closing session[/]")


@vmedia_app.command("release")
def vmedia_release(
    slot: int = typer.Option(..., "--slot", help="blade slot 1..32"),
    hard: bool = typer.Option(
        False, "--hard", help="reset the iBMC IPMC (clears all vmedia state, ~30s downtime)"
    ),
) -> None:
    """Force-release a stale vmedia session on a blade (recovery from CN_EXIST)."""
    from .vmedia.client import force_release_vmedia

    s = Settings.load()
    force_release_vmedia(s, slot, hard_reset=hard)


sessions_app = typer.Typer(help="Manage Redfish sessions on the HMM")
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("list")
def sessions_list() -> None:
    """List all active Redfish sessions on the HMM."""
    s = Settings.load()
    with console.status("querying sessions..."):
        items = ops.list_sessions(s)
    t = Table(title=f"Redfish sessions ({len(items)})")
    t.add_column("ID")
    t.add_column("User")
    t.add_column("URL")
    t.add_column("Mine?")
    for it in items:
        t.add_row(str(it["id"]), str(it["user"]), it["url"], "yes" if it["is_mine"] else "")
    console.print(t)


@sessions_app.command("clean")
def sessions_clean(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Delete every active Redfish session on the HMM (orphans cleanup)."""
    s = Settings.load()
    if not yes and not typer.confirm("delete all sessions except a fresh one?", default=True):
        raise typer.Exit(1)
    with console.status("cleaning..."):
        n = ops.cleanup_sessions(s)
    console.print(f"[green]deleted {n} orphan session(s)[/]")


@app.command("gui-password-hash")
def gui_password_hash_cmd(
    password: str = typer.Option(
        "",
        "--password",
        "-p",
        help="Plaintext password (omit to be prompted interactively)",
    ),
) -> None:
    """Generate a bcrypt hash for VESSEL_GUI_PASSWORD_HASH.

    Without --password the prompt hides input. Copy the resulting
    hash into your `.env` (or secret manager) and restart the GUI.
    """
    from .auth import hash_password

    if not password:
        password = typer.prompt("password", hide_input=True, confirmation_prompt=True)
    if not password:
        console.print("[red]password may not be empty[/]")
        raise typer.Exit(1)
    h = hash_password(password)
    console.print("[dim]Add to your .env (and never commit):[/]")
    console.print(f"VESSEL_GUI_PASSWORD_HASH={h}")


# --- switch sub-app: VLAN CRUD on CX310 ---
switch_app = typer.Typer(no_args_is_help=True, help="CX310 switch ops (VLAN, port)")


def _switch_dispatcher(host: str, user: str, password: str | None):
    """Build a connected Dispatcher; caller responsible for .close()."""
    from .switch.dispatcher import Dispatcher, SshTarget

    target = SshTarget(host=host, username=user, password=password or None)
    d = Dispatcher(target)
    d.connect()
    return d


@switch_app.command("list-vlans")
def switch_list_vlans(
    host: str = typer.Argument(..., help="Switch hostname or IP"),
    user: str = typer.Option("admin", "--user", "-u"),
    password: str = typer.Option(
        "", "--password", "-p", help="Empty → use ssh-agent / ~/.ssh keys"
    ),
) -> None:
    """List VLANs on a switch."""
    from .switch import ops as switch_ops

    d = _switch_dispatcher(host, user, password)
    try:
        vlans = switch_ops.list_vlans(d)
    finally:
        d.close()
    t = Table(title=f"VLANs on {host}")
    t.add_column("VID", justify="right")
    t.add_column("Type")
    t.add_column("Untagged ports")
    t.add_column("Tagged ports")
    for vid in sorted(vlans):
        e = vlans[vid]
        t.add_row(str(vid), e.vlan_type, ", ".join(e.untagged_ports), ", ".join(e.tagged_ports))
    console.print(t)


@switch_app.command("add-vlan")
def switch_add_vlan(
    host: str = typer.Argument(...),
    vid: int = typer.Argument(..., help="VLAN ID 1..4094"),
    name: str = typer.Option("", "--name", "-n", help="Optional VLAN description"),
    user: str = typer.Option("admin", "--user", "-u"),
    password: str = typer.Option("", "--password", "-p"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Create a VLAN. Idempotent. Auto-rollback on watchdog failure."""
    from .switch import ops as switch_ops

    if not yes and not typer.confirm(f"add VLAN {vid} on {host}?", default=False):
        raise typer.Exit(1)
    d = _switch_dispatcher(host, user, password)
    try:
        result = switch_ops.add_vlan(d, vid, name=name or None)
    finally:
        d.close()
    if result.success:
        console.print(
            f"[green]✓ VLAN {vid} {'present' if result.evidence.get('already_existed') else 'added'}[/]"
        )
    else:
        console.print(f"[red]✗ rollback: {result.evidence.get('reason')}[/]")
        raise typer.Exit(2)


@switch_app.command("remove-vlan")
def switch_remove_vlan(
    host: str = typer.Argument(...),
    vid: int = typer.Argument(...),
    user: str = typer.Option("admin", "--user", "-u"),
    password: str = typer.Option("", "--password", "-p"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete a VLAN. Idempotent. Auto-rollback on watchdog failure."""
    from .switch import ops as switch_ops

    if not yes and not typer.confirm(f"remove VLAN {vid} on {host}?", default=False):
        raise typer.Exit(1)
    d = _switch_dispatcher(host, user, password)
    try:
        result = switch_ops.remove_vlan(d, vid)
    finally:
        d.close()
    if result.success:
        console.print(
            f"[green]✓ VLAN {vid} {'absent' if result.evidence.get('already_absent') else 'removed'}[/]"
        )
    else:
        console.print(f"[red]✗ rollback: {result.evidence.get('reason')}[/]")
        raise typer.Exit(2)


@switch_app.command("set-access-port")
def switch_set_access_port(
    host: str = typer.Argument(...),
    port: str = typer.Argument(..., help="e.g. GE0/0/5 or XGE0/0/3"),
    vid: int = typer.Argument(..., help="VLAN ID for untagged egress"),
    user: str = typer.Option("admin", "--user", "-u"),
    password: str = typer.Option("", "--password", "-p"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Configure a port as untagged member of one VLAN."""
    from .switch import ops as switch_ops

    if not yes and not typer.confirm(f"set {port} on {host} as access vlan {vid}?", default=False):
        raise typer.Exit(1)
    d = _switch_dispatcher(host, user, password)
    try:
        result = switch_ops.set_access_port(d, port, vid)
    finally:
        d.close()
    console.print(
        f"[{'green' if result.success else 'red'}]"
        + ("✓" if result.success else "✗")
        + f" {port} access vlan {vid}[/]"
    )
    if not result.success:
        raise typer.Exit(2)


@switch_app.command("set-trunk-port")
def switch_set_trunk_port(
    host: str = typer.Argument(...),
    port: str = typer.Argument(...),
    allowed: str = typer.Argument(..., help="Comma-separated VLAN IDs, e.g. 10,20,30"),
    pvid: int = typer.Option(0, "--pvid", help="Native (untagged) VLAN; 0 = none"),
    user: str = typer.Option("admin", "--user", "-u"),
    password: str = typer.Option("", "--password", "-p"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Configure a port as a trunk carrying multiple VLANs (tagged)."""
    from .switch import ops as switch_ops

    try:
        allowed_vids = [int(x.strip()) for x in allowed.split(",") if x.strip()]
    except ValueError as exc:
        console.print(f"[red]invalid --allowed list: {exc}[/]")
        raise typer.Exit(1) from exc
    if not yes and not typer.confirm(
        f"set {port} on {host} as trunk allow={allowed_vids} pvid={pvid or 'none'}?", default=False
    ):
        raise typer.Exit(1)
    d = _switch_dispatcher(host, user, password)
    try:
        result = switch_ops.set_trunk_port(d, port, allowed_vids=allowed_vids, pvid=pvid or None)
    finally:
        d.close()
    console.print(
        f"[{'green' if result.success else 'red'}]"
        + ("✓" if result.success else "✗")
        + f" {port} trunk allow={allowed_vids}[/]"
    )
    if not result.success:
        raise typer.Exit(2)


app.add_typer(switch_app, name="switch")


# --- firmware sub-app: bulk Redfish UpdateService ---
firmware_app = typer.Typer(no_args_is_help=True, help="Bulk firmware via Redfish UpdateService")


@firmware_app.command("list")
def firmware_list_cmd(
    targets: str = typer.Argument(
        ...,
        help='Comma-sep "kind:id:host:user[:pw]" tuples (e.g. "blade:slot1:172.31.1.11:root")',
    ),
) -> None:
    """List Redfish UpdateService inventory for one or more targets."""
    from . import firmware as fw

    parsed: list[tuple[str, str, str, str, str | None]] = []
    for spec in targets.split(","):
        parts = spec.split(":")
        if len(parts) < 4:
            console.print(f"[red]bad target spec: {spec!r}[/]")
            raise typer.Exit(1)
        kind, id_, host, user, *rest = parts
        pw = rest[0] if rest else None
        parsed.append((kind, id_, host, user, pw))  # type: ignore[arg-type]

    found = fw.list_targets(hosts=parsed)
    t = Table(title=f"Firmware inventory ({len(found)} components)")
    t.add_column("Kind")
    t.add_column("ID")
    t.add_column("Host")
    t.add_column("Component")
    t.add_column("Version")
    t.add_column("Ready")
    for ft in found:
        t.add_row(
            ft.kind,
            ft.id,
            ft.redfish_host,
            ft.component,
            ft.current_version,
            "[green]yes[/]" if ft.is_ready else "[red]no[/]",
        )
    console.print(t)


@firmware_app.command("plan")
def firmware_plan_cmd(
    image_uri: str = typer.Argument(..., help="HTTP(S) URL where the image is hosted"),
    targets: str = typer.Argument(
        ...,
        help='Comma-sep "kind:id:host:user[:pw]" tuples (same format as `list`)',
    ),
    strategy: str = typer.Option("rolling", "--strategy", "-s", help="rolling|canary|all-at-once"),
    batch_size: int = typer.Option(4, "--batch-size", "-b"),
    out: str = typer.Option("", "--out", "-o", help="Save plan as JSON to this path"),
) -> None:
    """Build a firmware plan (dry-run) and print/save it."""
    import json as _json
    from pathlib import Path

    from . import firmware as fw

    parsed: list[tuple[str, str, str, str, str | None]] = []
    for spec in targets.split(","):
        parts = spec.split(":")
        kind, id_, host, user, *rest = parts
        pw = rest[0] if rest else None
        parsed.append((kind, id_, host, user, pw))  # type: ignore[arg-type]

    found = fw.list_targets(hosts=parsed)
    if not found:
        console.print("[red]no targets discovered[/]")
        raise typer.Exit(1)

    p = fw.plan(image_uri, found, strategy=strategy, batch_size=batch_size)  # type: ignore[arg-type]

    payload = p.to_dict()
    if out:
        Path(out).write_text(_json.dumps(payload, indent=2))
        console.print(f"[green]plan saved → {out}[/]")
    else:
        console.print_json(data=payload)
    if p.blockers:
        console.print(f"[red]plan has {len(p.blockers)} blocker(s); apply will refuse[/]")
        raise typer.Exit(2)


@firmware_app.command("apply")
def firmware_apply_cmd(
    plan_file: str = typer.Argument(..., help="Path to plan.json from `plan --out`"),
    user: str = typer.Option(..., "--user", "-u"),
    password: str = typer.Option(..., "--password", "-p"),
    abort_on_failure: bool = typer.Option(True, "--abort-on-failure/--keep-going"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Apply a previously-built plan."""
    import json as _json
    from pathlib import Path

    from . import firmware as fw

    raw = _json.loads(Path(plan_file).read_text())
    targets = [
        fw.FirmwareTarget(
            kind=t["kind"],
            id=t["id"],
            redfish_host=t["host"],
            component=t["component"],
            current_version=t["current_version"],
            update_uri="/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
            is_ready=t["ready"],
        )
        for w in raw["waves"]
        for t in w["targets"]
    ]
    p = fw.FirmwarePlan(
        image_uri=raw["image_uri"],
        strategy=raw["strategy"],
        waves=[fw.FirmwareWave(targets=targets)],  # rebuilt below
        blockers=raw.get("blockers", []),
        canary_dwell_s=raw.get("canary_dwell_s", 60),
    )
    # Reconstruct waves preserving original boundaries
    p.waves = [
        fw.FirmwareWave(
            targets=[t2 for t2 in targets if t2.id == t["id"] and t2.component == t["component"]]
        )
        for w in raw["waves"]
        for t in w["targets"]
    ]

    console.print(
        f"[yellow]applying plan: {p.target_count} target(s), "
        f"{len(p.waves)} wave(s), strategy={p.strategy}[/]"
    )
    if not yes and not typer.confirm("apply?", default=False):
        raise typer.Exit(1)

    results = fw.apply(
        p,
        user=user,
        password=password,
        abort_on_first_failure=abort_on_failure,
    )
    ok = sum(1 for r in results if r.success)
    console.print(f"[bold]done[/]  ok={ok}  failed={len(results) - ok}")
    if any(not r.success for r in results):
        raise typer.Exit(2)


app.add_typer(firmware_app, name="firmware")


@app.command("notify")
def notify_cmd(
    message: str = typer.Argument(..., help="Text to send"),
    level: str = typer.Option(
        "info", "--level", "-l", help="info|ok|warn|error (changes emoji prefix)"
    ),
) -> None:
    """Send a Telegram notification (smoke-test the notify wiring).

    Reads VESSEL_TG_BOT_TOKEN + VESSEL_TG_CHAT_ID from env. Exits 2
    if either is unset so scripts can detect the missing config.
    """
    from .notify import is_configured, notify_telegram

    if not is_configured():
        console.print(
            "[yellow]Telegram not configured: set "
            "VESSEL_TG_BOT_TOKEN and VESSEL_TG_CHAT_ID in env.[/]"
        )
        raise typer.Exit(2)
    ok = notify_telegram(message, level=level)
    if ok:
        console.print("[green]sent[/]")
    else:
        console.print("[red]send failed — check logs[/]")
        raise typer.Exit(1)


if __name__ == "__main__":
    from .logging_config import setup as _setup_logging

    _setup_logging()
    app()
