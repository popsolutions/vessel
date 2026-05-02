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
