"""Firmware-target manifest + diff against current inventory.

The manifest is a YAML mapping ``{component: {field: target_value}}``.
A diff returns one ``FieldDiff`` per (component, field) the manifest
mentions, marking each as ``ok`` (current matches target), ``mismatch``
(current differs from target), ``missing`` (component not present /
field not reported), or ``unknown`` (component absent from inventory).

Field names follow ``InventoryModule._decode_version_block`` keys, e.g.
``"BIOS Version"``, ``"Active iMana Version"``, ``"CPLD Version"``,
``"Software Version"``, ``"Uboot Version"``.

A small example manifest::

    smm:
      "Software Version": "(U54)7.63"
      "CPLD Version": "(U1082)108"
    othersmm: smm           # alias — copy from key "smm"
    Slot1:
      "BIOS Version": "(U41)V502"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Literal

import yaml

from .inventory import ComponentVersion

log = logging.getLogger(__name__)

DiffStatus = Literal["ok", "mismatch", "missing", "unknown"]


class ManifestError(ValueError):
    """Raised on schema problems in the YAML manifest."""


@dataclass(frozen=True)
class FieldDiff:
    """One target field's comparison vs the live inventory."""

    component: str
    field: str
    current: str
    target: str
    status: DiffStatus

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    @property
    def needs_action(self) -> bool:
        return self.status in ("mismatch", "missing")


@dataclass(frozen=True)
class ManifestDiff:
    """Aggregate diff for one inventory snapshot vs one manifest."""

    fields: list[FieldDiff]

    @property
    def needs_action(self) -> list[FieldDiff]:
        return [f for f in self.fields if f.needs_action]

    @property
    def all_ok(self) -> bool:
        return bool(self.fields) and all(f.is_ok for f in self.fields)

    def by_component(self) -> dict[str, list[FieldDiff]]:
        out: dict[str, list[FieldDiff]] = {}
        for f in self.fields:
            out.setdefault(f.component, []).append(f)
        return out


def parse_manifest(text: str | bytes) -> dict[str, dict[str, str]]:
    """Parse YAML manifest text. Resolves single-string-value aliases.

    Aliases let multiple components share targets without copying::

        smm:        {Software Version: ...}
        othersmm: smm

    The resolver walks alias chains; cycles raise ``ManifestError``.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"manifest YAML parse error: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest top-level must be a mapping, got {type(raw).__name__}")

    direct: dict[str, dict[str, str]] = {}
    aliases: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k)
        if isinstance(v, str):
            aliases[key] = v
        elif isinstance(v, dict):
            direct[key] = {str(fk): str(fv) for fk, fv in v.items()}
        else:
            raise ManifestError(
                f"manifest entry {key!r} must be a mapping or alias string, got {type(v).__name__}"
            )

    out = dict(direct)
    for k, target in aliases.items():
        if target not in direct and target not in aliases:
            raise ManifestError(f"alias {k!r} -> {target!r}: target not found")
        seen: set[str] = {k}
        cur = target
        while cur in aliases:
            if cur in seen:
                raise ManifestError(f"alias cycle involving {k!r}")
            seen.add(cur)
            cur = aliases[cur]
        out[k] = direct[cur]
    return out


def diff(
    inventory: Iterable[ComponentVersion],
    manifest: dict[str, dict[str, str]],
) -> ManifestDiff:
    """Compare every manifest entry against the live inventory."""
    by_component = {v.component: v for v in inventory}
    diffs: list[FieldDiff] = []
    for comp, targets in manifest.items():
        cv = by_component.get(comp)
        for field, target in targets.items():
            if cv is None or not cv.present:
                diffs.append(
                    FieldDiff(
                        component=comp,
                        field=field,
                        current="",
                        target=target,
                        status="unknown",
                    )
                )
                continue
            current = cv.fields.get(field, "")
            if not current:
                status: DiffStatus = "missing"
            elif current.strip() == target.strip():
                status = "ok"
            else:
                status = "mismatch"
            diffs.append(
                FieldDiff(
                    component=comp,
                    field=field,
                    current=current,
                    target=target,
                    status=status,
                )
            )
    return ManifestDiff(fields=diffs)
