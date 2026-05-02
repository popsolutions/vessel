"""Redact secrets from captured HMM flow files in place.

The mitm captures everything verbatim — including the password the user
types into the HMM login form and any session cookies. This script
walks a capture run-dir and replaces those values with ``<REDACTED>``.

Usage::

    python scripts/redact_captures.py discovery/api/20260502T193022Z

Idempotent — safe to re-run.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("form-field", re.compile(r"(userpasswd|userpwd|password)=([^&\"\\\s<;]+)")),
    ("cookie",     re.compile(r"(SESSID)=([^;\"\\\s<]+)")),
    ("xml-token",  re.compile(r"(<csrftoken>)([^<]+)(</csrftoken>)")),
    ("json-token", re.compile(r'("csrftoken"\s*:\s*")([^"]+)(")')),
)

_REDACTED = "<REDACTED>"


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for label, pat in _PATTERNS:
        if pat.groups == 2:
            new, n = pat.subn(rf"\g<1>={_REDACTED}", text)
        else:
            new, n = pat.subn(rf"\g<1>{_REDACTED}\g<3>", text)
        if n:
            counts[label] = n
            text = new
    return text, counts


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("capture_dir", type=Path)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if not args.capture_dir.is_dir():
        print(f"not a directory: {args.capture_dir}", file=sys.stderr)
        return 1

    total: dict[str, int] = {}
    files_changed = 0
    for flow in sorted(args.capture_dir.glob("*.json")):
        if flow.name.startswith(("inventory", "_")):
            continue
        try:
            raw = flow.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"skip {flow.name}: {exc}", file=sys.stderr)
            continue
        new, counts = redact_text(raw)
        if not counts:
            continue
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
        if not args.dry_run:
            flow.write_text(new, encoding="utf-8")
        files_changed += 1
        print(f"{'would redact' if args.dry_run else 'redacted'} {flow.name}: {counts}")

    print(f"\n{files_changed} file(s) {'would be ' if args.dry_run else ''}modified")
    if total:
        print("totals:", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
