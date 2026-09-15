#!/usr/bin/env python3
"""Keep one tracking issue in sync with bundled template snapshot drift.

Below SNAPSHOT_DRIFT_FAILURE_COMMITS the audit stays green (see #129), so the
drift advisory reaches only the job summary of a passing run -- which nobody
opens. This puts it in the issue list instead, where a stale snapshot is
visible without anyone having to go looking for it.

Deliberately one issue, reused: it is edited each week rather than refiled, and
closed when the drift clears. A new issue per run would be the same weekly
noise the advisory bucket exists to avoid, just in a different inbox.

CI-only. Not COPY'd into the published image and never run by the tool.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from maintenance_audit import (
    SUBPROCESS_TIMEOUT_SECONDS,
    TEMPLATE_SOURCES,
    audit_upstream_drift,
    load_template_source,
)

# Issues are matched by exact title rather than a body marker or a label.
# GitHub's search index lags by an unbounded amount, and a weekly job that
# misses an existing issue files a duplicate; listing and comparing titles is
# slower but answers from the current state rather than the index.
ISSUE_TITLE = "Bundled template snapshot trails upstream"
# `gh issue list --limit N` is not a page size, it is a cap -- gh pages through
# the API on its own until it collects N items or runs out. 200 was low enough
# that a repo with more open issues than that could push the tracking issue
# off the fetched page and file a duplicate every week from then on. This is
# high enough to mean "every open issue" for any volume this project is
# plausibly going to reach, while still bounding a pathological repo instead
# of paging forever.
ISSUE_LIST_LIMIT = 10000
# A title says which issue; it says nothing about whose. This repository is
# public and takes issues from anyone, so a title alone is squattable: open an
# issue spelled exactly like ISSUE_TITLE and the weekly audit -- which holds
# `issues: write` -- would edit that issue's body every Monday and eventually
# comment on and close it. Worse, it would never file a real one again, because
# `existing is None` in sync() is what opens the tracking issue and a squatted
# title keeps that branch unreachable. The drift advisory is deliberately
# sub-threshold (see #129), so the issue list is the only place it is visible;
# losing it loses the signal that the bundled snapshots -- and the pinned action
# SHAs they ship to every generated repository -- have gone stale.
#
# So match the author too. gh's issue list is GraphQL-backed and spells an app
# actor "app/github-actions"; the REST spelling is accepted as well so an issue
# created through either path is still recognized as this workflow's own.
TRACKING_ISSUE_AUTHORS = frozenset({"app/github-actions", "github-actions[bot]", "github-actions"})


def run_gh(args: list[str]) -> str:
    # Every failure mode below has to arrive as RuntimeError, because that is
    # the only thing main() catches -- and anything it does not catch fails the
    # step, which fails the weekly audit for a reason having nothing to do with
    # the repo. OSError covers gh being absent or not executable.
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh timed out after {SUBPROCESS_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not run gh: {exc}") from exc
    if proc.returncode != 0:
        detail = "\n".join(part for part in [proc.stdout, proc.stderr] if part).strip()
        raise RuntimeError(detail or f"gh {' '.join(args)} failed")
    return proc.stdout


def collect_drift(repo_root: Path) -> list[str]:
    # Both buckets: an issue that only tracked the advisory would close itself
    # at exactly the point the drift became bad enough to fail the audit.
    messages: list[str] = []
    for source_rel, _workflow_rel in TEMPLATE_SOURCES:
        try:
            source = load_template_source(repo_root / source_rel)
        except (OSError, ValueError):
            # Already reported as a failure by the audit's local checks; this
            # script has nothing to add and must not crash the step.
            continue
        findings, advisories = audit_upstream_drift(source)
        messages.extend(findings)
        messages.extend(advisories)
    return messages


def render_body(messages: list[str], *, run_url: str | None = None) -> str:
    lines = [
        "The bundled template snapshots are checked against their upstreams by",
        "the weekly maintenance audit. Current state:",
        "",
    ]
    lines.extend(f"- {message}" for message in messages)
    lines.extend(
        [
            "",
            "Refreshing is a judgement call, not a chore: read the commit count, and",
            "review the pin updates that come with the refresh. See the maintainer",
            "guide's *Reading the weekly audit* section.",
            "",
            "This issue is updated in place by `.github/workflows/maintenance-audit.yml`",
            "and closes itself once the snapshots match upstream. Editing the body by",
            "hand will be overwritten on the next run.",
        ]
    )
    if run_url:
        lines.extend(["", f"Last checked: {run_url}"])
    return "\n".join(lines) + "\n"


def written_by_this_workflow(author: object) -> bool:
    # Anything that is not a recognized actor object is not ours. An absent or
    # unexpected `author` has to read as "somebody else" rather than as "no
    # objection", or a gh whose payload shape changed would quietly restore the
    # title-only match this exists to replace.
    if not isinstance(author, dict):
        return False
    login = author.get("login")
    return isinstance(login, str) and login in TRACKING_ISSUE_AUTHORS


def find_tracking_issue(repo: str | None = None) -> tuple[int, str] | None:
    args = ["issue", "list", "--state", "open", "--limit", str(ISSUE_LIST_LIMIT), "--json", "number,title,body,author"]
    if repo:
        args.extend(["--repo", repo])
    try:
        payload = json.loads(run_gh(args) or "[]")
    except json.JSONDecodeError as exc:
        # gh exited 0 but did not print JSON -- an auth notice, an upgrade
        # warning. Same treatment as maintenance_audit.github_api_json().
        raise RuntimeError(f"Invalid JSON from gh issue list: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected issue list payload")
    for item in payload:
        if not isinstance(item, dict) or item.get("title") != ISSUE_TITLE:
            continue
        # Skipped rather than reported. A squatted title is indistinguishable
        # here from a maintainer having hand-opened one, and this script must
        # not fail the audit over issue bookkeeping either way -- so it passes
        # over the issue and lets sync() file the genuine one alongside it,
        # which every run after this then finds.
        if not written_by_this_workflow(item.get("author")):
            continue
        number = item.get("number")
        body = item.get("body")
        if isinstance(number, int):
            return number, body if isinstance(body, str) else ""
    return None


def sync(repo_root: Path, *, repo: str | None = None, run_url: str | None = None) -> str:
    messages = collect_drift(repo_root)
    existing = find_tracking_issue(repo)
    repo_args = ["--repo", repo] if repo else []

    if not messages:
        if existing is None:
            return "No snapshot drift and no tracking issue open; nothing to do."
        number, _body = existing
        run_gh(
            ["issue", "comment", str(number), *repo_args, "--body",
             "The bundled template snapshots now match their upstreams. Closing automatically."]
        )
        run_gh(["issue", "close", str(number), *repo_args])
        return f"Snapshot drift cleared; closed #{number}."

    body = render_body(messages, run_url=run_url)
    if existing is None:
        url = run_gh(["issue", "create", *repo_args, "--title", ISSUE_TITLE, "--body", body]).strip()
        return f"Opened snapshot drift tracking issue: {url}"

    number, current_body = existing
    # The "Last checked" line changes every run, so compare everything above it.
    if current_body.split("\nLast checked:")[0].strip() == body.split("\nLast checked:")[0].strip():
        return f"Snapshot drift unchanged; left #{number} as it is."
    run_gh(["issue", "edit", str(number), *repo_args, "--body", body])
    return f"Snapshot drift changed; updated #{number}."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync the bundled template snapshot drift tracking issue.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--repo", default=None, help="owner/name, when not running inside the repo's own checkout.")
    parser.add_argument("--run-url", default=None, help="URL of the run doing the check, recorded in the body.")
    args = parser.parse_args(argv)
    try:
        print(sync(args.repo_root.resolve(), repo=args.repo, run_url=args.run_url))
    except RuntimeError as exc:
        # Never fail the audit over issue bookkeeping: the audit's own exit
        # code is the signal, and this is only how the advisory gets seen.
        print(f"Could not sync the snapshot drift issue: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
