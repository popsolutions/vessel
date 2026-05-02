"""Read-only inventory queries against the HMM web API.

Wraps ``versionhandler.php`` (firmware versions per component) and
related queries that the GUI uses to render the chassis state.
"""

from __future__ import annotations

import concurrent.futures
import html
import logging
import re
from dataclasses import dataclass, field

from .client import HMMWebClient, HMMWebError

log = logging.getLogger(__name__)

SMM_COMPONENTS: tuple[str, ...] = ("smm", "othersmm")
SWITCH_SLOTS: tuple[str, ...] = ("Swi1", "Swi2", "Swi3", "Swi4")
BLADE_SLOTS: tuple[int, ...] = tuple(range(1, 17))


@dataclass(frozen=True)
class ComponentVersion:
    """Parsed firmware-version block returned by versionhandler.get."""

    component: str
    present: bool
    raw: str = ""
    fields: dict[str, str] = field(default_factory=dict)


_VERSION_RE = re.compile(r"<version>(.*?)</version>", re.DOTALL)
_FIELD_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9 ]*?)\s+(Version|Ver|Software|Built|CPU|BoardID|PCB|Address)\s*:\s*(.+)$"
)


def _decode_version_block(xml_body: str) -> tuple[str, dict[str, str]]:
    """Pull the <version> block out and decode HMM's escaping conventions."""
    m = _VERSION_RE.search(xml_body)
    if not m:
        return "", {}
    inner = m.group(1)
    text = (
        inner.replace("&lt;br&gt;", "\n")
        .replace("&amp;#40;", "(")
        .replace("&amp;#41;", ")")
    )
    text = html.unescape(text)
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m2 = _FIELD_RE.match(line)
        if m2:
            key = f"{m2.group(1).strip()} {m2.group(2)}".strip()
            fields[key] = m2.group(3).strip()
    return text, fields


class InventoryModule:
    """Wrapper around versionhandler/queryhandler reads."""

    def __init__(self, client: HMMWebClient) -> None:
        self._c = client

    def get_version(self, component: str, fruid: str = "0") -> ComponentVersion:
        """Fetch the firmware version block for a single component.

        Returns ``ComponentVersion(present=False)`` for empty/invalid
        FRUs (HTTP 400 or retcode≠0) so callers can render the whole
        chassis without exception handling per slot.
        """
        try:
            r = self._c.post(
                "versionhandler.php",
                actiontype="get",
                chassisid="0",
                bladename=component,
                fruid=str(fruid),
            )
        except Exception as exc:
            log.debug("versionhandler.get %s -> %s", component, exc)
            return ComponentVersion(component=component, present=False)
        if r.retcode != 0:
            return ComponentVersion(component=component, present=False)
        raw, fields = _decode_version_block(r.body)
        return ComponentVersion(component=component, present=True, raw=raw, fields=fields)

    def list_versions(
        self,
        *,
        components: tuple[str, ...] | None = None,
        max_workers: int = 8,
    ) -> list[ComponentVersion]:
        """List version blocks for every queryable component.

        Issues calls in parallel via a thread pool — ``HMMWebClient``'s
        underlying ``httpx.Client`` is thread-safe for concurrent use.
        Returns results in the same order as ``components``.
        """
        if components is None:
            components = (
                *SMM_COMPONENTS,
                *(f"Slot{n}" for n in BLADE_SLOTS),
                *SWITCH_SLOTS,
            )
        if max_workers <= 1 or len(components) <= 1:
            return [self.get_version(c) for c in components]
        results: list[ComponentVersion | None] = [None] * len(components)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self.get_version, c): i for i, c in enumerate(components)}
            for fut in concurrent.futures.as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:
                    log.warning("list_versions: component %s failed: %s", components[i], exc)
                    results[i] = ComponentVersion(component=components[i], present=False)
        # all slots populated by now (failures filled with present=False)
        return [r for r in results if r is not None]

    def is_upgrading(self) -> bool:
        """Return True if any chassis-level upgrade is in progress."""
        r = self._c.post("queryhandler.php", actiontype="isupdate", chassisid="0")
        if r.root is None:
            raise HMMWebError(f"isupdate: unparseable response: {r.body[:120]!r}")
        state_el = r.root.find("state")
        if state_el is None or state_el.text is None:
            return False
        return state_el.text.strip() != "0"
