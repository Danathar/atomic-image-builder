"""Execute the `run:` body of .github/workflows/agent-audit.yml.

The audit reads back the record every agent pull request is supposed to leave
-- the `— hive:` signature line naming its backend and model, and a
Signed-off-by trailer on every commit -- and fails the run when one did not
leave it. Nothing else notices either gap after the merge: the DCO check is
not a required status check in the ruleset, and an omp-backed run pushes under
the maintainer's identity, so the author login alone cannot say which merged
pull requests an agent wrote.

What the shell decides, and what fails quietly when it stops deciding it:

* Which merged pull requests count as an agent's. A Hive-app pull request is
  one by its author; a maintainer-identity pull request is one only by its
  signature line. Drop the second clause and every omp-backed pull request
  silently leaves the audit.
* A Hive-app pull request with no signature line is a finding, and an agent
  pull request with an unsigned commit is a finding. Either one has to turn
  the run red, or the audit is a report nobody reads.
* A merge of main into the branch needs no trailer, and only that merge: on a
  pull request into main, it must have two or more parents and git's wording
  for a merge from main, and no parent after the first may be one of the pull
  request's own commits. The commits list is what the branch held and main
  did not when the pull request merged, so a parent missing from it was on
  main then; asking today's main instead would pass every merge, since
  merging the pull request put both sides of each there.
* A Tier 4 path is reported in the row and does not fail the run. The paths
  are the ones docs/risk-tiers.md names, and the join below reads them out of
  the document so a path added there has to reach the workflow too.
* `gh pr list` returns the newest N and nothing about the rest, so a window
  that fills the cap is refused rather than audited in part.

The step's shell is extracted from the workflow rather than copied here, so
editing agent-audit.yml re-runs these assertions against the edit. `gh` is a
stub serving staged JSON and recording its argv; `jq` is the real one, since
the classification is written in it. Bodies run under a plain `bash -c`, so
the `set -euo pipefail` the body writes for itself is the thing under test.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _workflow_steps import step_env, step_run_body

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/agent-audit.yml"
RISK_TIERS = ROOT / "docs/risk-tiers.md"
AUDIT_STEP = "Audit merged agent pull requests"
REPO = "Danathar/atomic-image-builder"

# Serves `pr list` from merged.json, `pr view N` from pr-N.json and
# `api repos/<repo>/pulls/N/commits` from commits-N.json in $STUB_DIR,
# recording every argv so a case can assert on the query sent. Like the real
# gh, it returns only the fields `--json` names, for `pr list` no more than
# `--limit` entries (30 when the flag is absent), and for `api` the REST
# response passed through the step's own `--jq`, so a field, the limit or the
# filter dropped from the step's query changes what it reads. A path with no
# staged file fails the run, the way a 404 does, and any other API path fails
# it too, so a call the step should not make cannot pass unnoticed.
GH_STUB = r"""#!/usr/bin/env bash
{ printf '%s\t' gh "$@"; printf '\n'; } >> "$STUB_LOG"
fields="" limit=30 filter="." path=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --json) fields="${args[i + 1]}" ;;
    --limit) limit="${args[i + 1]}" ;;
    --jq) filter="${args[i + 1]}" ;;
    repos/*) path="${args[i]}" ;;
  esac
done
project='with_entries(select(.key as $k | $f | split(",") | index($k)))'
case "$1 $2" in
  "pr list") jq --arg f "$fields" --argjson n "$limit" ".[:\$n] | map($project)" "$STUB_DIR/merged.json" ;;
  "pr view") jq --arg f "$fields" "$project" "$STUB_DIR/pr-$3.json" ;;
  api\ *)
    if [[ "$path" =~ ^repos/[^/]+/[^/]+/pulls/([0-9]+)/commits$ ]]; then file="commits-${BASH_REMATCH[1]}.json"
    else echo "gh stub: unexpected api path: $path" >&2; exit 97
    fi
    [ -f "$STUB_DIR/$file" ] || { echo "gh stub: HTTP 404 for $path" >&2; exit 1; }
    jq -cr "$filter" "$STUB_DIR/$file" ;;
  *) echo "gh stub: unexpected command: $*" >&2; exit 97 ;;
esac
"""

SIGNATURE = "— hive: backend=claude model=claude-opus-5-5 effort=medium"
SIGNED = "Hive-Run: Danathar/atomic-image-builder#1\n\nSigned-off-by: Dan <dan@example.com>"

# Parents a case can give a commit. BEFORE is main as it stood just before
# the pull request merged and MAIN an older commit on main; neither is one of
# the pull request's commits. An int is the commit at that position in the
# same pull request, and PREV the one just before it.
BEFORE = "e" * 40
MAIN = "d" * 40
PREV = "prev"
ONE_PARENT = [MAIN]
FROM_MAIN = [PREV, MAIN]


def merged(number: int, *, bot: bool, signed_body: bool, title: str = "fix: a thing") -> dict:
    body = "## What changed\n\nThe thing.\n"
    if signed_body:
        body += f"\n{SIGNATURE}\n"
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/{REPO}/pull/{number}",
        "mergedAt": "2026-09-30T12:00:00Z",
        "mergedBy": {"login": "Danathar"},
        "author": {"login": "app/danathar-atomic-hive" if bot else "Danathar", "is_bot": bot},
        "body": body,
    }


def oid(number: int, index: int) -> str:
    return f"{number:03d}{index:04d}" + "a" * 33


def details(
    number: int,
    commits: list[str],
    files: list[str],
    headlines: list[str] | None = None,
    parents: list[list[str | int]] | None = None,
    base: str = "main",
) -> dict:
    """`gh pr view --json number,files,baseRefName` for *number*, plus its REST commits list.

    *commits* are message bodies, *headlines* their first lines when a case
    needs them, and *parents* each commit's parents (ONE_PARENT unless a case
    makes it a merge), an int or PREV naming another commit of the same pull
    request. The sha is derived from the position so a finding can be matched
    against it. The REST commits go under `rest_commits`, which `stage`
    writes to commits-N.json.
    """
    headlines = headlines or ["test: a change"] * len(commits)
    parents = parents or [ONE_PARENT] * len(commits)

    def sha(index: int, parent: str | int) -> str:
        if parent == PREV:
            assert index > 0, "the first commit has no commit before it"
            return oid(number, index - 1)
        return oid(number, parent) if isinstance(parent, int) else parent

    return {
        "number": number,
        "files": [{"path": path} for path in files],
        "baseRefName": base,
        "rest_commits": [
            {
                "sha": oid(number, index),
                "commit": {"message": f"{headline}\n\n{body}" if body else headline},
                "parents": [{"sha": sha(index, parent)} for parent in shas],
            }
            for index, (headline, body, shas) in enumerate(zip(headlines, commits, parents, strict=True))
        ],
    }


def tier_four_literals() -> list[str]:
    """The backticked paths in Tier 4's **Paths:** paragraph of docs/risk-tiers.md."""
    text = RISK_TIERS.read_text()
    section = text.split("## Tier 4")[1].split("\n## ")[0]
    paragraph = section.split("**Paths:**")[1].split("\n\n")[0]
    literals = re.findall(r"`([^`]+)`", paragraph)
    assert literals, "Tier 4's Paths paragraph names no backticked path"
    return literals


def concrete(literal: str) -> str:
    """A file path the Tier 4 literal covers, for a path that names a directory or glob."""
    if literal.endswith("/**"):
        return literal[: -len("**")] + "main.json"
    if literal.endswith("/"):
        return literal + "something"
    return literal


class AuditStepRun:
    """One execution of the audit step over staged pull requests."""

    def __init__(self, tmp: Path, since: str = "2026-09-01") -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.stub_log = tmp / "gh.log"
        self.summary = tmp / "summary.md"
        self.summary.write_text("")
        self.since = since
        self.work = tmp / "work"
        self.work.mkdir()

    def stage(self, prs: list[dict], detail: list[dict]) -> None:
        """Stage the listing and each pull request's view and commits."""
        (self.tmp / "merged.json").write_text(json.dumps(prs))
        for entry in detail:
            view = {key: value for key, value in entry.items() if key != "rest_commits"}
            (self.tmp / f"pr-{entry['number']}.json").write_text(json.dumps(view))
            (self.tmp / f"commits-{entry['number']}.json").write_text(json.dumps(entry["rest_commits"]))

    def run(self) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.tmp),
            "STUB_LOG": str(self.stub_log),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GH_TOKEN": "stub-token",
            "REPO": REPO,
            "SINCE": self.since,
        }
        return subprocess.run(
            ["bash", "-c", step_run_body(WORKFLOW, AUDIT_STEP)],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
        )

    def gh_calls(self) -> list[list[str]]:
        if not self.stub_log.exists():
            return []
        return [line.split("\t")[:-1] for line in self.stub_log.read_text().splitlines()]


class AuditStepTests(unittest.TestCase):
    def setUp(self) -> None:
        # jq is on every GitHub runner, as shellcheck is; ci.yml reads the
        # coverage threshold with it without installing it. Skipped the way
        # tests/test_git_diff_gate.py skips on shellcheck, rather than with a
        # `skipUnless` decorator: those are joined to the tools ci.yml pins
        # from a release, which jq is not.
        if shutil.which("jq") is None:
            self.skipTest("jq is not installed")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.run_ = AuditStepRun(Path(self.tmpdir.name))

    def test_the_step_reads_the_token_and_repository_from_the_workflow_context(self) -> None:
        env = step_env(WORKFLOW, AUDIT_STEP)
        self.assertEqual(env["GH_TOKEN"], "${{ github.token }}")
        self.assertEqual(env["REPO"], "${{ github.repository }}")
        self.assertEqual(env["SINCE"], "${{ inputs.since }}")

    def test_a_window_where_every_record_is_complete_passes_and_lists_only_agent_pull_requests(self) -> None:
        self.run_.stage(
            [
                merged(10, bot=True, signed_body=True, title="fix: a | b"),
                merged(11, bot=False, signed_body=True),
                merged(12, bot=False, signed_body=False, title="docs: by hand"),
            ],
            [
                details(10, [SIGNED], ["docs/using.md"]),
                details(11, [SIGNED, SIGNED], ["atomic_image_builder.py"]),
            ],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = self.run_.summary.read_text()
        self.assertIn("2 of the 3 pull requests merged in the window were written by an agent.", summary)
        self.assertIn("[#10](https://github.com/Danathar/atomic-image-builder/pull/10) fix: a \\| b", summary)
        self.assertIn("| app/danathar-atomic-hive | Danathar | backend=claude model=claude-opus-5-5 effort=medium | 1 | all | none |", summary)
        self.assertIn("[#11](https://github.com/Danathar/atomic-image-builder/pull/11)", summary)
        self.assertIn("| Danathar | Danathar | backend=claude model=claude-opus-5-5 effort=medium | 2 | all | none |", summary)
        self.assertNotIn("#12", summary)
        self.assertNotIn("Findings", summary)
        self.assertIn("Every agent pull request carries its signature line", summary)
        # The maintainer's own pull request is never fetched in detail.
        viewed = [call[3] for call in self.run_.gh_calls() if call[1:3] == ["pr", "view"]]
        self.assertEqual(sorted(viewed), ["10", "11"])
        self.assertEqual(result.stdout.strip(), summary.strip())

    def test_a_hive_app_pull_request_without_a_signature_line_fails_the_run(self) -> None:
        self.run_.stage(
            [merged(20, bot=True, signed_body=False), merged(21, bot=True, signed_body=True)],
            [details(20, [SIGNED], ["README.md"]), details(21, [SIGNED], ["README.md"])],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        summary = self.run_.summary.read_text()
        self.assertIn("| **missing** | 1 | all | none |", summary)
        self.assertIn("#### Findings", summary)
        self.assertIn("- #20: opened by the Hive app with no `— hive:` signature line", summary)
        self.assertNotIn("- #21:", summary)
        self.assertIn("::error::1 agent pull request(s) merged since 2026-09-01 left an incomplete record", result.stdout)

    def test_an_unsigned_commit_on_an_agent_pull_request_fails_the_run_and_names_the_commit(self) -> None:
        unsigned = "Hive-Run: Danathar/atomic-image-builder#1\n"
        self.run_.stage(
            [merged(30, bot=False, signed_body=True)],
            [details(30, [SIGNED, unsigned, "Signed-off-by: Dan <dan@example.com>"], ["README.md"])],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        summary = self.run_.summary.read_text()
        self.assertIn("| 3 | **2 of 3** | none |", summary)
        self.assertIn("- #30: commit 0300001 carries no Signed-off-by trailer", summary)

    def test_two_unsigned_commits_are_named_together(self) -> None:
        self.run_.stage(
            [merged(31, bot=True, signed_body=True)],
            [details(31, ["", "nothing here", SIGNED], ["README.md"])],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("- #31: commits 0310000, 0310001 carry no Signed-off-by trailer", self.run_.summary.read_text())

    def test_every_tier_four_path_the_risk_document_names_is_listed_and_does_not_fail(self) -> None:
        literals = tier_four_literals()
        paths = [concrete(literal) for literal in literals] + ["docs/using.md", "tests/test_x.py"]
        self.run_.stage([merged(40, bot=True, signed_body=True)], [details(40, [SIGNED], paths)])
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = self.run_.summary.read_text()
        row = next(line for line in summary.splitlines() if line.startswith("| [#40]"))
        cell = row.rstrip("|").rsplit("|", 1)[1].strip()
        listed = re.findall(r"`([^`]+)`", cell)
        self.assertEqual(listed, [concrete(literal) for literal in literals])
        self.assertNotIn("Findings", summary)

    def test_a_window_that_fills_the_cap_is_refused(self) -> None:
        prs = [merged(number, bot=False, signed_body=False) for number in range(1, 501)]
        self.run_.stage(prs, [])
        result = self.run_.run()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("::error::500 pull requests merged since 2026-09-01 reached the 500 cap", result.stdout)
        self.assertEqual(self.run_.summary.read_text(), "")

    def test_the_query_asks_for_merged_pull_requests_since_the_date(self) -> None:
        self.run_.stage([], [])
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (listing,) = [call for call in self.run_.gh_calls() if call[1:3] == ["pr", "list"]]
        self.assertIn("--state", listing)
        self.assertEqual(listing[listing.index("--state") + 1], "merged")
        self.assertEqual(listing[listing.index("--search") + 1], "merged:>=2026-09-01")
        self.assertEqual(listing[listing.index("--repo") + 1], REPO)
        self.assertIn("0 of the 0 pull requests merged in the window", self.run_.summary.read_text())

    def test_a_blank_since_means_the_last_thirty_one_days(self) -> None:
        self.run_.since = ""
        self.run_.stage([], [])
        before = datetime.now(timezone.utc).date()
        result = self.run_.run()
        after = datetime.now(timezone.utc).date()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (listing,) = [call for call in self.run_.gh_calls() if call[1:3] == ["pr", "list"]]
        expected = {f"merged:>={(day - timedelta(days=31)).isoformat()}" for day in (before, after)}
        self.assertIn(listing[listing.index("--search") + 1], expected)

    def test_a_malformed_since_is_refused_before_any_query(self) -> None:
        self.run_.since = "last month"
        self.run_.stage([], [])
        result = self.run_.run()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("::error::since must be YYYY-MM-DD, got 'last month'", result.stdout)
        self.assertEqual(self.run_.gh_calls(), [])


class AuditNearMissTests(unittest.TestCase):
    """Inputs one character from a match, which the cases above never send.

    Every pull request above either carries the record whole or lacks it
    whole, so an anchor dropped from one of the step's patterns, or a field
    dropped from its query, still passes them. Each case here sends the input
    that only the anchor or the field tells apart.
    """

    def setUp(self) -> None:
        if shutil.which("jq") is None:
            self.skipTest("jq is not installed")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.run_ = AuditStepRun(Path(self.tmpdir.name))

    def row(self, number: int) -> str:
        summary = self.run_.summary.read_text()
        return next(line for line in summary.splitlines() if line.startswith(f"| [#{number}]"))

    def test_the_row_carries_every_field_the_listing_returns(self) -> None:
        pr = merged(50, bot=True, signed_body=True, title="fix: the row")
        pr["mergedAt"] = "2026-09-28T23:59:59Z"
        pr["mergedBy"] = {"login": "a-reviewer"}
        self.run_.stage([pr], [details(50, [SIGNED, SIGNED], ["docs/using.md"])])
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.row(50),
            f"| [#50](https://github.com/{REPO}/pull/50) fix: the row | 2026-09-28 | app/danathar-atomic-hive"
            " | a-reviewer | backend=claude model=claude-opus-5-5 effort=medium | 2 | all | none |",
        )

    def test_a_pull_request_merged_by_no_one_recorded_says_unknown(self) -> None:
        pr = merged(51, bot=True, signed_body=True)
        pr["mergedBy"] = None
        self.run_.stage([pr], [details(51, [SIGNED], ["README.md"])])
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("| app/danathar-atomic-hive | unknown | backend=", self.row(51))

    def test_the_listing_is_asked_for_the_cap_it_checks_against(self) -> None:
        # gh's own default is 30. Without --limit the listing stops there and
        # the cap check below it never fires, so a busy month is audited in part.
        self.run_.stage([], [])
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (listing,) = [call for call in self.run_.gh_calls() if call[1:3] == ["pr", "list"]]
        self.assertEqual(listing[listing.index("--limit") + 1], "500")

    def test_a_maintainer_pull_request_that_only_quotes_the_signature_is_not_an_agents(self) -> None:
        pr = merged(52, bot=False, signed_body=False, title="docs: describe the signature")
        pr["body"] = "Every agent pull request ends with a `— hive:` line naming its model.\n"
        self.run_.stage([pr], [])
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 of the 1 pull requests merged in the window were written by an agent.", self.run_.summary.read_text())
        self.assertEqual([call for call in self.run_.gh_calls() if call[1:3] == ["pr", "view"]], [])

    def test_a_merge_commit_needs_no_trailer_and_is_left_out_of_the_count(self) -> None:
        # docs/multi-agent.md tells a contributor to update a pull request from
        # main before it merges. "Update branch" and `git merge origin/main`
        # write a two-parent commit with no trailer, and what it brings in was
        # audited when it reached main. The Signed-off cell counts only the
        # commits that need a trailer, so an unsigned commit next to a merge
        # reads "1 of 2", not "2 of 3".
        headlines = ["fix: the change", "Merge branch 'main' into docs/626-strategy", "fix: the other change"]
        self.run_.stage(
            [merged(58, bot=True, signed_body=True)],
            [details(58, [SIGNED, "", ""], ["README.md"], headlines, parents=[ONE_PARENT, FROM_MAIN, ONE_PARENT])],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(self.row(58).endswith("| 3 | **1 of 2** | none |"), self.row(58))
        self.assertIn("- #58: commit 0580002 carries no Signed-off-by trailer", self.run_.summary.read_text())

    def test_a_signed_branch_updated_from_main_passes(self) -> None:
        # The main side may be main's tip at the moment the pull request
        # merged as well as an older commit on it, and one merge may bring in
        # more than one of them.
        headlines = ["fix: the change", "Merge branch 'main' into x", "Merge origin/main into x", "Merge main into x"]
        self.run_.stage(
            [merged(60, bot=True, signed_body=True)],
            [
                details(
                    60,
                    [SIGNED, "", "", ""],
                    ["README.md"],
                    headlines,
                    parents=[ONE_PARENT, FROM_MAIN, [PREV, BEFORE], [PREV, MAIN, BEFORE]],
                )
            ],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.row(60).endswith("| 4 | all | none |"), self.row(60))

    def test_each_wording_the_headline_rule_accepts_for_a_merge_from_main_passes(self) -> None:
        # A local `git merge origin/main` writes "Merge remote-tracking branch
        # 'origin/main' into ...", and the rule also takes a branch spelled
        # 'origin/main'. Neither is sent as a real merge above, so an
        # alternative dropped from the pattern would still pass there while
        # every such update reported an unsigned commit here.
        headlines = [
            "fix: the change",
            "Merge remote-tracking branch 'origin/main' into docs/x",
            "Merge branch 'origin/main' into docs/x",
        ]
        self.run_.stage(
            [merged(69, bot=True, signed_body=True)],
            [details(69, [SIGNED, "", ""], ["README.md"], headlines, parents=[ONE_PARENT, FROM_MAIN, FROM_MAIN])],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.row(69).endswith("| 3 | all | none |"), self.row(69))

    def test_a_merge_of_another_branch_titled_from_main_still_needs_a_trailer(self) -> None:
        # Merging another branch puts its commits on the pull request, so the
        # merged-in parent is one of them: git's own wording and two parents,
        # but nothing came from main.
        headlines = ["test: on the other branch", "fix: the change", "Merge branch 'main' into docs/x"]
        self.run_.stage(
            [merged(62, bot=True, signed_body=True)],
            [details(62, [SIGNED, SIGNED, ""], ["README.md"], headlines, parents=[ONE_PARENT, ONE_PARENT, [1, 0]])],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(self.row(62).endswith("| 3 | **2 of 3** | none |"), self.row(62))
        self.assertIn("- #62: commit 0620002 carries no Signed-off-by trailer", self.run_.summary.read_text())

    def test_a_merge_made_before_the_branch_had_a_commit_of_its_own_still_needs_a_trailer(self) -> None:
        # A branch whose first act is `git merge --no-ff` of another unmerged
        # branch: the first parent is the main commit it started from, so one
        # parent is outside the pull request, but it is the branch line, not
        # what the merge brought in.
        headlines = ["test: on the other branch", "Merge branch 'main' into docs/x"]
        self.run_.stage(
            [merged(63, bot=True, signed_body=True)],
            [details(63, [SIGNED, ""], ["README.md"], headlines, parents=[ONE_PARENT, [MAIN, 0]])],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(self.row(63).endswith("| 2 | **1 of 2** | none |"), self.row(63))
        self.assertIn("- #63: commit 0630001 carries no Signed-off-by trailer", self.run_.summary.read_text())

    def test_a_merge_of_main_and_another_branch_at_once_still_needs_a_trailer(self) -> None:
        # An octopus merge brings in main and an unmerged branch together, so
        # one parent from outside the pull request is not enough.
        headlines = ["test: on the other branch", "fix: the change", "Merge main into docs/x"]
        self.run_.stage(
            [merged(64, bot=True, signed_body=True)],
            [details(64, [SIGNED, SIGNED, ""], ["README.md"], headlines, parents=[ONE_PARENT, ONE_PARENT, [1, MAIN, 0]])],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("- #64: commit 0640002 carries no Signed-off-by trailer", self.run_.summary.read_text())

    def test_a_merge_on_a_pull_request_into_another_branch_still_needs_a_trailer(self) -> None:
        # Outside the pull request is then that branch, which nothing audits,
        # so a parent from there says nothing about main.
        headlines = ["fix: the change", "Merge branch 'main' into docs/x"]
        self.run_.stage(
            [merged(65, bot=True, signed_body=True)],
            [details(65, [SIGNED, ""], ["README.md"], headlines, parents=[ONE_PARENT, FROM_MAIN], base="release")],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("- #65: commit 0650001 carries no Signed-off-by trailer", self.run_.summary.read_text())

    def test_a_merge_costs_no_call_beyond_the_commits_list(self) -> None:
        # The job token has 1,000 REST requests an hour, and each pull request
        # already spends one on its commits; the rule reads that list and the
        # base branch `gh pr view` returns, and asks GitHub for nothing else.
        headlines = ["fix: the change", "Merge main into x", "Merge main into x"]
        self.run_.stage(
            [merged(66, bot=True, signed_body=True), merged(67, bot=True, signed_body=True)],
            [
                details(66, [SIGNED, "", ""], ["README.md"], headlines, parents=[ONE_PARENT, FROM_MAIN, [PREV, BEFORE]]),
                details(67, [SIGNED], ["README.md"]),
            ],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        paths = [next(arg for arg in call if arg.startswith("repos/")) for call in self.run_.gh_calls() if call[1] == "api"]
        self.assertEqual(sorted(paths), [f"repos/{REPO}/pulls/66/commits", f"repos/{REPO}/pulls/67/commits"])

    def test_a_pull_request_at_the_commit_list_cap_fails_the_run(self) -> None:
        # The REST list stops at 250 commits without saying so, and a commit
        # cut off it would read as a parent from outside the pull request.
        entry = details(68, [SIGNED] * 250, ["README.md"])
        self.run_.stage([merged(68, bot=True, signed_body=True)], [entry])
        result = self.run_.run()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::error::#68 reached the REST list's 250-commit cap", result.stderr)
        self.assertEqual(self.run_.summary.read_text(), "")

    def test_a_pull_request_at_the_file_list_cap_fails_the_run(self) -> None:
        # `gh pr view` stops at 100 files without saying so, and a Tier 4 path
        # past it would read as "none".
        files = [f"docs/f{i}.md" for i in range(99)] + [".github/workflows/ci.yml"]
        entry = details(69, [SIGNED], files)
        self.run_.stage([merged(69, bot=True, signed_body=True)], [entry])
        result = self.run_.run()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::error::#69 reached the 100-file cap", result.stderr)
        self.assertEqual(self.run_.summary.read_text(), "")

    def test_a_one_parent_commit_titled_like_a_merge_still_needs_a_trailer(self) -> None:
        # The headline is the author's to write, so it cannot be the test.
        # Each one's only parent is the commit before it, so the parent count
        # is the one thing that tells it from a merge.
        spoofs = [
            "Merge branch 'main' into docs/x",
            "Merge remote-tracking branch 'origin/main' into docs/x",
            "Merge origin/main into docs/x",
        ]
        self.run_.stage(
            [merged(59, bot=True, signed_body=True)],
            [details(59, [SIGNED] + [""] * len(spoofs), ["README.md"], ["fix: the change", *spoofs], [ONE_PARENT] + [[PREV]] * len(spoofs))],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        oids = ", ".join(f"059{index:04d}" for index in range(1, len(spoofs) + 1))
        self.assertIn(f"- #59: commits {oids} carry no Signed-off-by trailer", self.run_.summary.read_text())

    def test_a_merge_that_is_not_main_into_the_branch_still_needs_a_trailer(self) -> None:
        # Real merges from main by their parents, so only the headline rule
        # can catch them.
        near = [
            "Merge branch 'feature' into docs/x",
            "Merge branch 'maintenance' into docs/x",
            "Merge pull request #1 from Danathar/main",
            "fix: Merge branch 'main' into docs/x",
            "Merge branch 'main' of github.com:Danathar/atomic-image-builder",
            "Merge mainline into docs/x",
            'Merge branch "main" into docs/x',
        ]
        self.run_.stage(
            [merged(61, bot=True, signed_body=True)],
            [details(61, [SIGNED] + [""] * len(near), ["README.md"], ["fix: the change", *near], [ONE_PARENT] + [FROM_MAIN] * len(near))],
        )
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        oids = ", ".join(f"061{index:04d}" for index in range(1, len(near) + 1))
        self.assertIn(f"- #61: commits {oids} carry no Signed-off-by trailer", self.run_.summary.read_text())

    def test_a_commit_that_only_mentions_the_trailer_is_unsigned(self) -> None:
        mentions = "Explain why every commit needs a Signed-off-by: trailer\n\nHive-Run: x#1"
        self.run_.stage([merged(53, bot=True, signed_body=True)], [details(53, [SIGNED, mentions], ["README.md"])])
        result = self.run_.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("- #53: commit 0530001 carries no Signed-off-by trailer", self.run_.summary.read_text())

    def test_a_path_that_only_contains_a_tier_four_path_is_not_listed(self) -> None:
        near = [
            "docs/.github/policies/notes.md",
            "tests/homebrew_formula.py",
            "homebrew_formula.pyc",
            "coverage_badge.pyi",
            ".github/workflows/ci.yml.orig",
            ".claude/settings.json.example",
            "docs/Formula/notes.md",
        ]
        self.run_.stage([merged(54, bot=True, signed_body=True)], [details(54, [SIGNED], near)])
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.row(54).endswith("| 1 | all | none |"), self.row(54))

    def test_a_pipe_in_the_signature_does_not_split_the_row(self) -> None:
        pr = merged(55, bot=True, signed_body=False)
        pr["body"] += "\n— hive: backend=a|b model=m\n"
        self.run_.stage([pr], [details(55, [SIGNED], ["README.md"])])
        result = self.run_.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("| backend=a\\|b model=m | 1 | all | none |", self.row(55))

    def test_a_pull_request_whose_details_cannot_be_read_fails_without_a_report(self) -> None:
        # pr-56.json is never staged, so the stub fails on it. Two things stop
        # the run here: errexit inside the loop with pipefail carrying it out,
        # and the jq program's guard for a pull request with no details. Either
        # alone is enough, so this pins the outcome rather than which one fired.
        self.run_.stage(
            [merged(56, bot=True, signed_body=True), merged(57, bot=True, signed_body=True)],
            [details(57, [SIGNED], ["README.md"])],
        )
        result = self.run_.run()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.run_.summary.read_text(), "")


class TierFourJoinTests(unittest.TestCase):
    def test_the_document_names_the_paths_the_join_expects(self) -> None:
        # The executing test above walks these; if the paragraph's shape moves
        # the reader returns nothing and that test would pass on an empty list.
        literals = tier_four_literals()
        for expected in (".github/workflows/publish-image.yml", ".claude/hooks/", "Formula/"):
            self.assertIn(expected, literals)


if __name__ == "__main__":
    unittest.main()
