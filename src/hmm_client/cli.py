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
    console.print(f"[yellow]slot=[/] {slot}  [yellow]iBMC=[/] {ip}  [yellow]device=[/] {device}"
                  + ("  [yellow]+power-cycle[/]" if reboot else ""))
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


vmedia_app = typer.Typer(help="VirtualMedia (mount ISO on a blade)")
app.add_typer(vmedia_app, name="vmedia")


@vmedia_app.command("mount")
def vmedia_mount(
    slot: int = typer.Option(..., "--slot", help="blade slot 1..32"),
    iso: str = typer.Option(..., "--iso", help="path to .iso file to expose as virtual CDROM"),
    kvm_port: int = typer.Option(2200, "--kvm-port",
                                 help="per-blade KVM stream port for codekey nego (default 2200, blade1)"),
    no_auto_release: bool = typer.Option(False, "--no-auto-release",
                                         help="don't try to force-release a stuck CN_EXIST session"),
) -> None:
    """Mount an ISO on a blade as a virtual CDROM."""
    from .vmedia.client import mount_iso
    s = Settings.load()
    try:
        mount_iso(s, slot=slot, iso_path=iso, kvm_port=kvm_port,
                  auto_release=not no_auto_release)
    except KeyboardInterrupt:
        console.print("\n[yellow]Ctrl-C — closing session[/]")


@vmedia_app.command("release")
def vmedia_release(
    slot: int = typer.Option(..., "--slot", help="blade slot 1..32"),
) -> None:
    """Force-release a stale vmedia session on a blade (recovery from CN_EXIST)."""
    from .vmedia.client import force_release_vmedia
    s = Settings.load()
    force_release_vmedia(s, slot)


sessions_app = typer.Typer(help="Manage Redfish sessions on the HMM")
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("list")
def sessions_list() -> None:
    """List all active Redfish sessions on the HMM."""
    s = Settings.load()
    with console.status("querying sessions..."):
        items = ops.list_sessions(s)
    t = Table(title=f"Redfish sessions ({len(items)})")
    t.add_column("ID"); t.add_column("User"); t.add_column("URL"); t.add_column("Mine?")
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


if __name__ == "__main__":
    app()
