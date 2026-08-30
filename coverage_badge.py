#!/usr/bin/env python3
"""Turn a unit coverage percentage into durable badge/trend artifacts.

Coverage artifacts uploaded by .github/workflows/ci.yml expire after 30
days (see CONTRIBUTING.md's Coverage section), and the percentage is
otherwise only visible inside a single run's job summary. This script is
the CI-facing half of publishing it somewhere durable: it writes a
shields.io endpoint-badge JSON payload and appends one CSV trend row
(date, SHA, percentage). CI is expected to push both to a dedicated
branch after a run on main; this script only produces the files.
"""

import argparse
import json
import sys

DEFAULT_HIGH = 90
DEFAULT_LOW = 75


def badge_color(percent: int, high: int = DEFAULT_HIGH, low: int = DEFAULT_LOW) -> str:
    if percent >= high:
        return "brightgreen"
    if percent >= low:
        return "yellow"
    return "red"


def badge_payload(
    percent: int,
    label: str = "unit coverage",
    high: int = DEFAULT_HIGH,
    low: int = DEFAULT_LOW,
) -> dict:
    return {
        "schemaVersion": 1,
        "label": label,
        "message": f"{percent}%",
        "color": badge_color(percent, high=high, low=low),
    }


def trend_row(date: str, sha: str, percent: int) -> str:
    return f"{date},{sha},{percent}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("percent", type=int, help="coverage percentage (0-100)")
    parser.add_argument("--label", default="unit coverage")
    parser.add_argument("--date", required=True, help="UTC date, YYYY-MM-DD")
    parser.add_argument("--sha", required=True, help="commit SHA")
    parser.add_argument("--badge-out", required=True, help="path to write the badge JSON")
    parser.add_argument("--trend-out", required=True, help="path to append the trend row to")
    args = parser.parse_args(argv)

    if not 0 <= args.percent <= 100:
        parser.error("percent must be between 0 and 100")

    with open(args.badge_out, "w", encoding="utf-8") as f:
        json.dump(badge_payload(args.percent, label=args.label), f)
        f.write("\n")

    with open(args.trend_out, "a", encoding="utf-8") as f:
        f.write(trend_row(args.date, args.sha, args.percent) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
