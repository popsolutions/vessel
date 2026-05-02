"""SSH dispatcher wrapper for CX310 VRP CLI access.

The chassis switches sit on the internal mgmt fabric (172.31.1.x) and
are typically not directly routable from the operator's workstation.
Two access paths:

  1. **Direct**: operator's host has a route into 172.31.1.0/24
     (e.g. via the chassis-host VPN). Plain `Dispatcher.connect()`.
  2. **Jump via HMM**: SSH to the HMM (192.168.1.30) first, then
     `ssh swi<N>` from inside the HMM dispatcher shell. Future
     `connect_via_hmm()` will wrap this once the live CX310 access
     path is exercised.

This module is a SCAFFOLD — the public API is stable but the actual
VRP `screen-length 0 temporary` setup, paging-prompt handling, and
`return ; save` write-confirm flow will be filled in once we have a
live CX310 to test against.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import paramiko

log = logging.getLogger(__name__)


class DispatcherError(RuntimeError):
    """Raised when the dispatcher cannot reach or authenticate to the switch."""


@dataclass
class SshTarget:
    host: str
    username: str
    password: str | None = None
    key_path: str | None = None
    port: int = 22


class Dispatcher:
    """Thin paramiko wrapper around a single VRP CLI session.

    Currently exposes one operation: `run(command) -> str`. Higher-
    level wrappers (`enter_system_view`, `commit`, `save`) will land
    once the live CX310 reveals which prompt/paging quirks need
    handling — this is intentionally minimal until then.
    """

    def __init__(self, target: SshTarget) -> None:
        self.target = target
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        client = paramiko.SSHClient()
        # Operators control the chassis-internal host inventory, so
        # AutoAddPolicy is acceptable for fabric IPs. For the public
        # surface (HMM 192.168.1.30) the operator should pin the host
        # key in `~/.ssh/known_hosts` — paramiko picks that up via
        # `load_system_host_keys()` and uses it before falling back.
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, object] = {
            "hostname": self.target.host,
            "username": self.target.username,
            "port": self.target.port,
            "timeout": 15,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if self.target.password:
            kwargs["password"] = self.target.password
        if self.target.key_path:
            kwargs["key_filename"] = self.target.key_path
        try:
            client.connect(**kwargs)
        except (paramiko.SSHException, OSError) as exc:
            raise DispatcherError(f"SSH connect to {self.target.host} failed: {exc}") from exc
        self._client = client
        log.info("dispatcher connected to %s", self.target.host)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def run(self, command: str, *, timeout: float = 30.0) -> str:
        """Execute a single VRP command, return stdout text.

        SCAFFOLD: uses `exec_command` which works for non-interactive
        commands. Interactive flows (paging prompts on `display`
        commands, write confirmations) will need an `invoke_shell`
        channel + `screen-length 0 temporary` setup — TBD with live
        hardware.

        Every command — even read-only `display` — gets one audit
        record. The chassis switch is sensitive enough that "what was
        queried, by whom, when" is itself a security signal.
        """
        from .. import audit

        if self._client is None:
            raise DispatcherError("dispatcher not connected")
        try:
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            stdin.close()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
        except Exception as exc:
            audit.log_op(
                op="switch.cli",
                target_kind="switch",
                target_id=self.target.host,
                result="failed",
                evidence={"command": command, "error": str(exc)},
            )
            raise
        if err.strip():
            log.warning("VRP stderr: %s", err.strip())
        audit.log_op(
            op="switch.cli",
            target_kind="switch",
            target_id=self.target.host,
            result="success",
            evidence={
                "command": command,
                "output_tail": out[-512:],
                "stderr_tail": err[-256:] if err.strip() else None,
            },
        )
        return out


@contextmanager
def open_dispatcher(target: SshTarget) -> Iterator[Dispatcher]:
    d = Dispatcher(target)
    try:
        d.connect()
        yield d
    finally:
        d.close()
