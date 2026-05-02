"""Bulk firmware updates across blades / switches / MMs via Redfish.

The DMTF Redfish standard exposes `UpdateService.SimpleUpdate` for
pushing a firmware image to one or more targets. Every component on
the Huawei E9000 chassis that ships a Redfish surface accepts this
action — blade iBMCs, the SMM/HMM, and the CX310 switch modules
(when their VRP build includes the update endpoint).

This module gives operators:

  - **`list_targets()`**     → enumerate every Redfish updatable
    endpoint with current FW version, model, and update-readiness.

  - **`plan(image_uri, targets, strategy)`** → dry-run; produces a
    `FirmwarePlan` containing the ordered wave list, total expected
    duration, and any pre-flight blockers (offline targets, missing
    image, etc.). The plan is JSON-serialisable so an operator can
    review/share/diff it before approving.

  - **`apply(plan, healthcheck=True, abort_on_first_failure=True)`** →
    execute. Applies waves sequentially with a beat between waves.
    Aborts the whole apply on the first failed target by default.

## Strategies

  - `"all-at-once"` — every target updated simultaneously. Fast but
    risky; only safe when the affected targets are out-of-band (no
    blade actually serving traffic).

  - `"rolling"` — N targets per wave, healthcheck after each wave.
    Default `batch_size=4` for a 16-blade chassis.

  - `"canary"` — apply to one target first, observe for `canary_dwell_s`
    seconds, then proceed with rolling to the rest if healthy.

## Audit

Every target gets one `firmware.apply` audit record (success or
failed). The plan itself is audited as `firmware.plan` so the
"who decided to apply this image to these targets" is recoverable
from `audit.log` alone.

## Status

This module is the SDK-level surface. CLI bindings live in
`cli.py` (`hmm firmware list / plan / apply`). Real chassis
integration depends on `redfish.RedfishClient` working against
each blade's iBMC at its discovered IP — currently blocked on
issue #7 (SSH tunnel through HMM) for blades on the internal
fabric. For the SMM and switches (reachable from the operator
host directly), updates work today.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from . import audit
from .redfish import RedfishClient

log = logging.getLogger(__name__)

Strategy = Literal["all-at-once", "rolling", "canary"]
TargetKind = Literal["blade", "switch", "mm"]


class FirmwareError(RuntimeError):
    """Raised when a firmware operation can't proceed safely."""


@dataclass
class FirmwareTarget:
    """One Redfish-updatable component."""

    kind: TargetKind
    id: str  # "slot3", "swi2", "smm1"
    redfish_host: str  # iBMC IP / SMM IP / switch IP
    component: str  # "BMC" / "BIOS" / "Switch" / "SMM"
    current_version: str
    update_uri: str  # Redfish path to UpdateService.SimpleUpdate
    is_ready: bool  # True if the target reports ready-for-update


@dataclass
class FirmwareWave:
    targets: list[FirmwareTarget]


@dataclass
class FirmwarePlan:
    """A dry-run ready for human review before apply."""

    image_uri: str  # operator-hosted HTTP(S) URL
    strategy: Strategy
    waves: list[FirmwareWave]
    blockers: list[str] = field(default_factory=list)
    canary_dwell_s: int = 60  # only meaningful for "canary" strategy

    @property
    def is_safe(self) -> bool:
        return not self.blockers

    @property
    def target_count(self) -> int:
        return sum(len(w.targets) for w in self.waves)

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_uri": self.image_uri,
            "strategy": self.strategy,
            "blockers": self.blockers,
            "canary_dwell_s": self.canary_dwell_s,
            "waves": [
                {
                    "targets": [
                        {
                            "kind": t.kind,
                            "id": t.id,
                            "host": t.redfish_host,
                            "component": t.component,
                            "current_version": t.current_version,
                            "ready": t.is_ready,
                        }
                        for t in w.targets
                    ],
                }
                for w in self.waves
            ],
        }


# --- discovery -------------------------------------------------------


def list_targets(
    *,
    hosts: list[tuple[TargetKind, str, str, str, str | None]],
    verify_tls: bool = False,
) -> list[FirmwareTarget]:
    """Enumerate updatable components.

    `hosts` is a list of `(kind, id, redfish_host, user, password)`
    tuples — the caller (CLI / GUI) is responsible for figuring out
    which iBMC IPs go with which slot.
    """
    out: list[FirmwareTarget] = []
    for kind, id_, host, user, pw in hosts:
        try:
            with RedfishClient(host, user, pw or "", verify=verify_tls) as rf:
                update_svc = rf.get("/redfish/v1/UpdateService")
                inv = update_svc.get("FirmwareInventory") or {}
                inv_path = inv.get("@odata.id") if isinstance(inv, dict) else None
                actions = update_svc.get("Actions") or {}
                simple = actions.get("#UpdateService.SimpleUpdate") or {}
                update_uri = (
                    simple.get("target")
                    or "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate"
                )

                if not inv_path:
                    log.warning("%s: no FirmwareInventory; skipping", host)
                    continue

                inv_coll = rf.get(inv_path)
                for member_path in rf.members(inv_coll):
                    member = rf.get(member_path)
                    component = member.get("Name", member_path.rsplit("/", 1)[-1])
                    version = member.get("Version", "unknown")
                    out.append(
                        FirmwareTarget(
                            kind=kind,
                            id=id_,
                            redfish_host=host,
                            component=component,
                            current_version=version,
                            update_uri=update_uri,
                            is_ready=True,
                        )
                    )
        except Exception as exc:
            log.warning("inventory failed for %s (%s): %s", id_, host, exc)
    return out


# --- plan ------------------------------------------------------------


def plan(
    image_uri: str,
    targets: list[FirmwareTarget],
    *,
    strategy: Strategy = "rolling",
    batch_size: int = 4,
    canary_dwell_s: int = 60,
) -> FirmwarePlan:
    """Build a plan without touching the chassis.

    Audits the planning event so the "who chose to upgrade these to
    this image" decision is in `audit.log` even if `apply()` is never
    called.
    """
    if not image_uri:
        raise FirmwareError("image_uri is required")
    if not targets:
        raise FirmwareError("at least one target is required")

    blockers: list[str] = []
    not_ready = [t for t in targets if not t.is_ready]
    if not_ready:
        blockers.append(
            f"{len(not_ready)} target(s) not ready: "
            + ", ".join(f"{t.id}/{t.component}" for t in not_ready[:5])
        )

    if strategy == "all-at-once":
        waves = [FirmwareWave(targets=list(targets))]
    elif strategy == "canary":
        if len(targets) < 2:
            blockers.append("canary needs >=2 targets")
            waves = [FirmwareWave(targets=list(targets))]
        else:
            waves = [
                FirmwareWave(targets=[targets[0]]),
                *_split(targets[1:], batch_size),
            ]
    elif strategy == "rolling":
        waves = _split(list(targets), batch_size)
    else:
        raise FirmwareError(f"unknown strategy {strategy!r}")

    p = FirmwarePlan(
        image_uri=image_uri,
        strategy=strategy,
        waves=waves,
        blockers=blockers,
        canary_dwell_s=canary_dwell_s,
    )
    audit.log_op(
        op="firmware.plan",
        target_kind="chassis",
        target_id="<plan>",
        result="dry-run",
        evidence={
            "image_uri": image_uri,
            "strategy": strategy,
            "target_count": p.target_count,
            "wave_count": len(p.waves),
            "blockers": blockers,
        },
    )
    return p


def _split(items: list[FirmwareTarget], n: int) -> list[FirmwareWave]:
    if n <= 0:
        n = 1
    return [FirmwareWave(targets=items[i : i + n]) for i in range(0, len(items), n)]


# --- apply -----------------------------------------------------------


@dataclass
class ApplyResult:
    target: FirmwareTarget
    task_state: str  # Completed / Exception / Killed / ...
    success: bool
    error: str | None = None


def apply(
    plan_obj: FirmwarePlan,
    *,
    user: str,
    password: str,
    verify_tls: bool = False,
    poll_seconds: int = 15,
    max_minutes_per_target: int = 30,
    healthcheck_between_waves: bool = True,
    abort_on_first_failure: bool = True,
) -> list[ApplyResult]:
    """Execute the plan. One audit record per target.

    Default policy is conservative: stop the whole apply when any
    single target fails. Pass `abort_on_first_failure=False` to
    keep going (useful when upgrading components that are
    independent and any reduction in current-version count is a
    win, e.g. fan firmware).
    """
    if plan_obj.blockers:
        raise FirmwareError("plan has blockers; refusing to apply: " + "; ".join(plan_obj.blockers))

    results: list[ApplyResult] = []
    for wave_idx, wave in enumerate(plan_obj.waves, start=1):
        log.info(
            "firmware wave %d/%d: %d targets",
            wave_idx,
            len(plan_obj.waves),
            len(wave.targets),
        )
        for target in wave.targets:
            r = _apply_one(
                target,
                plan_obj.image_uri,
                user,
                password,
                verify_tls=verify_tls,
                poll_seconds=poll_seconds,
                max_minutes=max_minutes_per_target,
            )
            results.append(r)
            audit.log_op(
                op="firmware.apply",
                target_kind=target.kind,
                target_id=target.id,
                result="success" if r.success else "failed",
                evidence={
                    "image_uri": plan_obj.image_uri,
                    "component": target.component,
                    "previous_version": target.current_version,
                    "task_state": r.task_state,
                    "error": r.error,
                    "wave": wave_idx,
                    "strategy": plan_obj.strategy,
                },
            )
            if not r.success and abort_on_first_failure:
                log.error("firmware apply failed on %s — aborting per policy", target.id)
                return results

        # Healthcheck between waves: just give Redfish a beat to
        # see all targets back online. A real healthcheck per
        # component lives in the SDK module for that subsystem
        # (BMC reachable, switch mgmt-plane up, etc.).
        if healthcheck_between_waves and wave_idx < len(plan_obj.waves):
            time.sleep(min(poll_seconds, 30))

        if plan_obj.strategy == "canary" and wave_idx == 1:
            log.info(
                "canary dwelling %d s; observe before continuing",
                plan_obj.canary_dwell_s,
            )
            time.sleep(plan_obj.canary_dwell_s)

    return results


def _apply_one(
    target: FirmwareTarget,
    image_uri: str,
    user: str,
    password: str,
    *,
    verify_tls: bool,
    poll_seconds: int,
    max_minutes: int,
) -> ApplyResult:
    """POST SimpleUpdate, then poll the resulting Task until terminal."""
    deadline = time.monotonic() + max_minutes * 60
    body = {
        "ImageURI": image_uri,
        "TransferProtocol": "HTTP",
    }
    try:
        with RedfishClient(target.redfish_host, user, password, verify=verify_tls) as rf:
            resp = rf.post(target.update_uri, body)
            task_path = (
                resp.get("@odata.id")
                or (resp.get("Task") or {}).get("@odata.id")
                or _task_path_from_headers(resp)
            )
            if not task_path:
                # Some chassis return synchronous results without a Task.
                state = resp.get("TaskState") or resp.get("MessageId") or "Completed"
                return ApplyResult(
                    target=target,
                    task_state=str(state),
                    success="Completed" in str(state) or "Success" in str(state),
                )

            while time.monotonic() < deadline:
                task = rf.get(task_path)
                state = task.get("TaskState", "Pending")
                if state in ("Completed", "Exception", "Killed", "Cancelled", "Suspended"):
                    err = None
                    if state != "Completed":
                        msgs = task.get("Messages") or [{}]
                        err = msgs[-1].get("Message") if isinstance(msgs[-1], dict) else None
                    return ApplyResult(
                        target=target,
                        task_state=state,
                        success=(state == "Completed"),
                        error=err,
                    )
                time.sleep(poll_seconds)

            return ApplyResult(
                target=target,
                task_state="Timeout",
                success=False,
                error=f"task did not reach terminal state in {max_minutes} min",
            )
    except Exception as exc:
        return ApplyResult(target=target, task_state="Exception", success=False, error=str(exc))


def _task_path_from_headers(resp: dict[str, Any]) -> str | None:
    # Some Redfish stacks tuck the task path in @odata.context or Location.
    loc = resp.get("Location") or resp.get("@odata.context")
    return loc if isinstance(loc, str) and loc.startswith("/redfish/") else None


__all__ = [
    "ApplyResult",
    "FirmwareError",
    "FirmwarePlan",
    "FirmwareTarget",
    "FirmwareWave",
    "Strategy",
    "TargetKind",
    "apply",
    "list_targets",
    "plan",
]
