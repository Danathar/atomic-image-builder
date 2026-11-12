#!/usr/bin/env python3
"""Fail when a measured module has more missed statements than its ceiling.

The unit gate in .github/workflows/ci.yml is one total percentage. A change
that adds a new guard -- a `raise CommandError(...)` on bad input -- with no
test reaching it barely moves that percentage, so the gate passes and the
guard ships untested. That class kept being found and fixed one guard at a
time, so the per-module ceiling below was adopted as the gate for it.

`ceilings` in .coverage-thresholds.json records, per module, how many
statements the unit run is allowed to leave unreached. A module measured by
the run but absent from `ceilings` has a ceiling of 0. A ceiling is a count of
statements, never a percentage. Lowering one is routine: a test now reaches
a line that nothing reached before. Raising one is a decision a person makes
in the diff, with the reason in the commit.

A ceiling naming a module the run did not measure also fails, so a rename
cannot leave a stale entry quietly allowing nothing.

Reads the JSON report written by `python3 -m coverage json`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

THRESHOLDS = Path(__file__).resolve().parent.parent / ".coverage-thresholds.json"


class CeilingError(Exception):
    """The thresholds file or the coverage report cannot be read."""


def load_ceilings(path: Path) -> dict[str, int]:
    """Return `{module path: most statements allowed missing}` from ``path``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CeilingError(f"{path}: cannot read thresholds: {exc}") from exc
    ceilings = raw.get("ceilings", {}) if isinstance(raw, dict) else None
    if not isinstance(ceilings, dict):
        raise CeilingError(f"{path}: 'ceilings' must be an object of module path -> maximum")
    for module, ceiling in ceilings.items():
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 0:
            raise CeilingError(f"{path}: ceiling for {module} must be a non-negative integer, got {ceiling!r}")
    return dict(ceilings)


def load_missing_lines(path: Path) -> dict[str, list[int]]:
    """Return `{module path: missed statement line numbers}` from a coverage JSON report."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        files = raw["files"]
        return {module: sorted(data["missing_lines"]) for module, data in files.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CeilingError(f"{path}: not a coverage JSON report: {exc!r}") from exc


def evaluate(missing: dict[str, list[int]], ceilings: dict[str, int]) -> list[str]:
    """Return one failure line per module over its ceiling or per stale ceiling."""
    failures = []
    for module, lines in sorted(missing.items()):
        ceiling = ceilings.get(module, 0)
        if len(lines) > ceiling:
            failures.append(
                f"{module}: {len(lines)} statements no test reaches, ceiling {ceiling}; "
                f"missing lines: {', '.join(map(str, lines))}"
            )
    for module in sorted(set(ceilings) - set(missing)):
        failures.append(f"{module}: has a ceiling of {ceilings[module]} but the coverage run did not measure it")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", type=Path, help="coverage JSON report (python3 -m coverage json)")
    parser.add_argument("--thresholds", type=Path, default=THRESHOLDS, help="thresholds file (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        failures = evaluate(load_missing_lines(args.report), load_ceilings(args.thresholds))
    except CeilingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        print(
            "Add a test that reaches the new lines, or raise the module's ceiling in "
            ".coverage-thresholds.json and give the reason in the commit.",
            file=sys.stderr,
        )
        return 1
    print("Every measured module is at or under its missed-statement ceiling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
