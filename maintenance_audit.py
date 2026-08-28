#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from atomic_image_builder import ACTION_PINS, ACTION_REF_PINS

TEMPLATE_SOURCES: list[tuple[str, str]] = [
    ("template_snapshots/containerfile/.template-source", "template_snapshots/containerfile/.github/workflows/build.yml"),
    ("template_snapshots/bluebuild/.template-source", "template_snapshots/bluebuild/.github/workflows/build.yml"),
]
WORKFLOW_DIRS: tuple[Path, ...] = (
    Path(".github/workflows"),
    Path("template_snapshots/containerfile/.github/workflows"),
    Path("template_snapshots/bluebuild/.github/workflows"),
)
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s+([^@\s]+)@([^\s#]+)")
VERSION_TAG_RE = re.compile(r"^v(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


@dataclass(frozen=True)
class TemplateSource:
    repo: str
    revision: str


def parse_version_tag(tag: str) -> tuple[int, int, int] | None:
    match = VERSION_TAG_RE.fullmatch(tag)
    if not match:
        return None
    major, minor, patch = (int(part) if part is not None else 0 for part in match.groups())
    return (major, minor, patch)


def version_tag_precision(tag: str) -> int | None:
    match = VERSION_TAG_RE.fullmatch(tag)
    if not match:
        return None
    groups = match.groups()
    if groups[2] is not None:
        return 3
    if groups[1] is not None:
        return 2
    return 1


def is_newer_version_available(current_tag: str, latest_tag: str) -> bool:
    current_version = parse_version_tag(current_tag)
    latest_version = parse_version_tag(latest_tag)
    precision = version_tag_precision(current_tag)
    if current_version is None or latest_version is None or precision is None:
        return False
    if precision == 1:
        return latest_version[0] > current_version[0]
    if precision == 2:
        return latest_version[:2] > current_version[:2]
    return latest_version > current_version


def load_template_source(path: Path) -> TemplateSource:
    data: dict[str, str] = {}
    text = path.read_text()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    repo = data.get("repo", "")
    revision = data.get("revision", "")
    if not repo:
        raise ValueError(f"{path} is missing repo=")
    if not REVISION_RE.fullmatch(revision):
        raise ValueError(f"{path} has an invalid revision=")
    return TemplateSource(repo=repo, revision=revision)


def iter_workflow_action_refs(text: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        match = USES_RE.match(raw_line)
        if match:
            refs.append(match.groups())
    return refs


def iter_local_workflow_paths(repo_root: Path) -> list[Path]:
    paths: set[Path] = set()
    for workflow_dir in WORKFLOW_DIRS:
        directory = repo_root / workflow_dir
        if not directory.is_dir():
            continue
        paths.update(directory.rglob("*.yml"))
        paths.update(directory.rglob("*.yaml"))
    return sorted(paths)


def audit_local_snapshot(repo_root: Path) -> list[str]:
    findings: list[str] = []
    for source_rel, workflow_rel in TEMPLATE_SOURCES:
        source_path = repo_root / source_rel
        workflow_path = repo_root / workflow_rel
        if not source_path.is_file():
            findings.append(f"Missing template metadata file: {source_path}")
        else:
            try:
                load_template_source(source_path)
            except (OSError, ValueError) as exc:
                findings.append(str(exc))
        if not workflow_path.is_file():
            findings.append(f"Missing template workflow file: {workflow_path}")
    for workflow_path in iter_local_workflow_paths(repo_root):
        try:
            workflow_text = workflow_path.read_text()
        except OSError as exc:
            findings.append(f"Unable to read {workflow_path}: {exc}")
            continue

        for action, ref in iter_workflow_action_refs(workflow_text):
            pin = ACTION_REF_PINS.get(f"{action}@{ref}") or ACTION_PINS.get(action)
            if pin is None:
                findings.append(f"Workflow action {action}@{ref} in {workflow_path} is not covered by ACTION_PINS or ACTION_REF_PINS.")
                continue
            sha, _label = pin
            if ref != sha:
                findings.append(
                    f"Workflow action {action}@{ref} in {workflow_path} does not match the pin table SHA {sha}."
                )
    return findings


def query_remote_head(repo: str) -> str:
    proc = subprocess.run(["git", "ls-remote", repo, "HEAD"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = "\n".join(part for part in [proc.stdout, proc.stderr] if part).strip()
        raise RuntimeError(detail or "git ls-remote failed")
    line = next((raw.strip() for raw in proc.stdout.splitlines() if raw.strip()), "")
    sha = line.split()[0] if line else ""
    if not REVISION_RE.fullmatch(sha):
        raise RuntimeError(f"Unexpected ls-remote output: {line or 'empty output'}")
    return sha


def audit_upstream_drift(source: TemplateSource) -> list[str]:
    try:
        head = query_remote_head(source.repo)
    except RuntimeError as exc:
        return [f"Unable to query upstream template HEAD for {source.repo}: {exc}"]
    if head == source.revision:
        return []
    return [
        (
            "Bundled template snapshot differs from upstream HEAD: "
            f"pinned {source.revision}, upstream {head}. Refresh the snapshot and review pin updates."
        )
    ]


def github_api_json(url: str) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "atomic-image-builder-maintenance-audit",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def query_latest_github_semver_tag(action: str) -> str | None:
    payload = github_api_json(f"https://api.github.com/repos/{action}/tags?per_page=100")
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected tag payload for {action}")
    latest_tag: str | None = None
    latest_version: tuple[int, int, int] | None = None
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        version = parse_version_tag(name)
        if version is None:
            continue
        if latest_version is None or version > latest_version:
            latest_tag = name
            latest_version = version
    return latest_tag


def query_github_ref_sha(action: str, ref: str) -> str | None:
    payload = github_api_json(f"https://api.github.com/repos/{action}/commits/{ref}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected commit payload for {action}@{ref}")
    sha = payload.get("sha")
    return sha if isinstance(sha, str) else None


def iter_pinned_refs(
    actions: Mapping[str, tuple[str, str]] = ACTION_PINS,
    ref_pins: Mapping[str, tuple[str, str]] = ACTION_REF_PINS,
) -> list[tuple[str, str, str]]:
    # Both pin tables describe the same thing -- "this action, at this label,
    # is this SHA" -- so freshness can be checked over them together. The
    # ref-pin table is keyed by "action@ref" and holds several keys per pin
    # (the tag and the SHA spellings), so dedupe on (action, label).
    seen: set[tuple[str, str]] = set()
    pinned: list[tuple[str, str, str]] = []
    # actions is keyed by action name, so its entries are already distinct;
    # only the ref-pin table below can repeat a pair. The set is built here
    # for that loop to check against.
    for action, (sha, label) in actions.items():
        seen.add((action, label))
        pinned.append((action, label, sha))
    for key, (sha, label) in ref_pins.items():
        action = key.split("@", 1)[0]
        if (action, label) not in seen:
            seen.add((action, label))
            pinned.append((action, label, sha))
    return pinned


def query_github_comparison(action: str, base: str, head: str) -> tuple[str, int, int] | None:
    payload = github_api_json(f"https://api.github.com/repos/{action}/compare/{base}...{head}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected compare payload for {action}")
    status = payload.get("status")
    ahead = payload.get("ahead_by")
    behind = payload.get("behind_by")
    if not isinstance(status, str) or not isinstance(ahead, int) or not isinstance(behind, int):
        return None
    return status, ahead, behind


def describe_pin_drift(action: str, label: str, sha: str, head: str) -> str:
    # Direction matters more than the mismatch. ublue-os/remove-unwanted-software
    # is pinned 26 commits AHEAD of what its v8 tag now points at -- upstream
    # moved the tag backwards onto an older commit -- so "refresh this pin"
    # would have been a downgrade. Saying only "these differ" invites exactly
    # that mistake, so ask which way before wording the advisory.
    kind = "tag" if parse_version_tag(label) is not None else "branch"
    prefix = f"Action pin {action} no longer matches the {kind} it names: pinned {sha[:12]}, {label} is now {head[:12]}"
    try:
        comparison = query_github_comparison(action, sha, head)
    except RuntimeError:
        comparison = None
    if comparison is None:
        return f"{prefix}. Compare them before refreshing; the direction of the difference is unknown."
    status, ahead, behind = comparison
    if status == "ahead":
        return f"{prefix}, {ahead} commit(s) newer. Repos generated from this pin ship the older action; refresh it."
    if status == "behind":
        return (
            f"{prefix}, which is {behind} commit(s) OLDER than the pin. Upstream moved the {kind} backwards -- "
            "refreshing to it would downgrade the action. Leave the pin alone unless that is what you want."
        )
    return f"{prefix}, on a diverged history. Review both before refreshing."


def audit_action_pin_freshness(
    pinned: Sequence[tuple[str, str, str]] | None = None,
) -> list[str]:
    # A SHA pin carries a human label -- "v7", "main" -- and that label is a
    # claim about what the SHA is. This checks the claim: resolve the label
    # upstream and see whether it still points at the pinned commit.
    #
    # This catches what audit_action_update_availability() cannot. A pin
    # labelled v7 stays "current" against a v7.0.1 release by that function's
    # rules, because the label's precision says only a major bump counts --
    # but the pinned SHA is frozen at v7.0.0, so every repo generated from it
    # ships the older action and gets a Dependabot PR on day one. Same for a
    # branch label like "main", which that function skips entirely.
    #
    # Immutable exact tags (v4.6.0) resolve to themselves forever, so they
    # stay quiet here. Moving tags and branches are the ones that drift -- in
    # either direction, which is why describe_pin_drift() asks which way before
    # telling anyone to refresh.
    findings: list[str] = []
    for action, label, sha in iter_pinned_refs() if pinned is None else pinned:
        try:
            head = query_github_ref_sha(action, label)
        except RuntimeError as exc:
            findings.append(f"Unable to resolve {action}@{label} upstream: {exc}")
            continue
        if head is None or head == sha:
            continue
        findings.append(describe_pin_drift(action, label, sha, head))
    return findings


def audit_action_update_availability(
    actions: Mapping[str, tuple[str, str]] = ACTION_PINS,
) -> list[str]:
    findings: list[str] = []
    for action, (_sha, label) in actions.items():
        if parse_version_tag(label) is None:
            continue
        try:
            latest_tag = query_latest_github_semver_tag(action)
        except RuntimeError as exc:
            findings.append(f"Unable to query upstream tags for {action}: {exc}")
            continue
        if latest_tag is None:
            continue
        if not is_newer_version_available(label, latest_tag):
            continue
        findings.append(
            f"Action pin {action} is behind the latest upstream tag: current {label}, latest {latest_tag}. Review and refresh ACTION_PINS if appropriate."
        )
    return findings


def run_audit(
    repo_root: Path, *, skip_upstream: bool, check_action_updates: bool = False
) -> tuple[list[str], list[str]]:
    """Return (failures, advisories).

    Failures mean the repository is internally inconsistent or has drifted
    from its recorded upstream, and the audit exits non-zero for them.

    Advisories mean an upstream action has moved on. They are reported but do
    not fail the run: a pin that names a branch drifts every time upstream
    commits, so failing on it would leave the weekly audit permanently red
    and train everyone to ignore it.
    """
    findings = audit_local_snapshot(repo_root)
    if not skip_upstream:
        for source_rel, _workflow_rel in TEMPLATE_SOURCES:
            source_path = repo_root / source_rel
            try:
                source = load_template_source(source_path)
            except (OSError, ValueError):
                source = None
            if source is not None:
                findings.extend(audit_upstream_drift(source))
    advisories: list[str] = []
    if check_action_updates:
        advisories.extend(audit_action_update_availability())
        advisories.extend(audit_action_pin_freshness())
    return findings, advisories


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit bundled template snapshot drift and workflow action pin coverage.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Path to the repository root.",
    )
    parser.add_argument(
        "--skip-upstream",
        action="store_true",
        help="Run only local consistency checks without querying the upstream template HEAD.",
    )
    parser.add_argument(
        "--check-action-updates",
        action="store_true",
        help=(
            "Also query upstream and report action pins that trail newer semver tags or no "
            "longer match the tag or branch they name. Reported as advisories, not failures."
        ),
    )
    args = parser.parse_args(argv)

    findings, advisories = run_audit(
        args.repo_root.resolve(),
        skip_upstream=args.skip_upstream,
        check_action_updates=args.check_action_updates,
    )
    if advisories:
        print("Action pin advisories (not failures):")
        for advisory in advisories:
            print(f"- {advisory}")
        print()

    if not findings:
        print("Maintenance audit passed.")
        return 0

    print("Maintenance audit failed:")
    for finding in findings:
        print(f"- {finding}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
