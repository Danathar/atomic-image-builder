"""Execute the `run:` bodies of .github/workflows/ai-fix.yml.

The intake job is the repo's answer to "what state is `main` actually in?" --
it runs every check CI gates on and posts the result on the labeled issue, so
whoever picks the issue up starts from evidence instead of re-deriving it. No
test ran a line of that shell. tests/test_workflow_dependencies.py reads the
file to compare action pins, and tests/test_lint_command_consistency.py reads
the one shellcheck command out of its check table; both are static scans of
text, and neither executes the loop that turns those commands into a report.

What the shell decides, and what fails quietly when it stops deciding it:

* The gate is only worth reading if it is the same gate. The check table is a
  second copy of ci.yml's list of steps, and a check dropped from it makes the
  intake comment report a green `main` while CI is red on exactly the thing
  that was dropped.
* `.coveragerc` sets no `fail_under`, so `coverage report` exits 0 at any
  percentage. The threshold that makes the coverage entry a pass or a FAIL is
  read from `.coverage-thresholds.json` -- the same file ci.yml reads -- and
  passed as `--fail-under`. Lose that argument and a below-gate run is
  reported as passing; read the wrong key and the comment quotes a gate the
  repo does not enforce.
* The step runs under `set -uo pipefail` and deliberately not `-e`, because a
  failing check is the report's content rather than an error. If `-e` ever
  arrives, the first FAIL truncates the report at that check and the comment
  silently stops mentioning the ones after it.

The steps' shell is extracted from the workflow rather than copied here, so
editing ai-fix.yml re-runs these assertions against the edit. Every tool the
gate calls is a recording stub on PATH: nothing here runs the real suite, and
a case asserts on the argv the gate would have sent. `gh` is stubbed the same
way, so the comment step never reaches GitHub.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _workflow_steps import step_command, step_env, step_run_body

ROOT = Path(__file__).resolve().parents[1]
AI_FIX_WORKFLOW = ROOT / ".github/workflows/ai-fix.yml"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
THRESHOLDS = ROOT / ".coverage-thresholds.json"

GATE_STEP = "Run the gate"
COMMENT_STEP = "Post the intake comment"

# The checks the table runs, in the order the report lists them. Spelled out
# rather than parsed back out of the workflow: a table that reordered or
# renamed itself would otherwise still match, and the report's shape is what
# a reader of the comment relies on.
EXPECTED_CHECKS = ["unittest", "coverage", "ruff", "shellcheck", "actionlint", "hadolint", "audit"]

# ci.yml's `Run <tool>` steps name the linter; the intake table names the
# check. Only one of the two differs.
CI_STEP_TO_CHECK = {"tests": "unittest"}

# Records argv (with $0, so one log shows which tool ran) and can be told to
# fail with a given number of diagnostic lines, which is what the report's
# `tail -20` is measured against.
GENERIC_STUB = r"""#!/usr/bin/env bash
name="$(basename "$0")"
{ printf '%s\t' "$name" "$@"; printf '\n'; } >> "$STUB_LOG"
lines_var="STUB_LINES_${name}"
exit_var="STUB_EXIT_${name}"
count="${!lines_var:-0}"
for ((i = 1; i <= count; i++)); do echo "$name diagnostic $i"; done
exit "${!exit_var:-0}"
"""

# python3 carries five different jobs in this table, so the stub dispatches on
# argv rather than exiting a single way. The coverage entries behave like the
# real thing in the one way the report depends on: `report` alone always
# succeeds, and only `--fail-under` can turn a percentage into a failure.
# Anything the gate runs that is not one of these exits 97 with a message, so
# a command that changed shape surfaces as a FAIL naming itself rather than as
# a stub that silently passed it.
PYTHON_STUB = r"""#!/usr/bin/env bash
{ printf '%s\t' "$(basename "$0")" "$@"; printf '\n'; } >> "$STUB_LOG"
percent="${STUB_COVERAGE_PERCENT:-93}"
case "$*" in
  "-m unittest discover -s tests"|"-m coverage run -m unittest discover -s tests")
    count="${STUB_LINES_unittest:-0}"
    for ((i = 1; i <= count; i++)); do echo "unittest diagnostic $i"; done
    exit "${STUB_EXIT_unittest:-0}"
    ;;
  "-m coverage report --format=total")
    echo "$percent"
    ;;
  "-m coverage report --fail-under="*)
    argv="$*"
    want="${argv##*--fail-under=}"
    if [[ ! "$want" =~ ^[0-9]+$ ]]; then
      echo "option --fail-under: invalid value: '$want'" >&2
      exit 2
    fi
    if (( percent < want )); then
      echo "Coverage failure: total of $percent is less than fail-under=$want" >&2
      exit 2
    fi
    ;;
  "maintenance_audit.py --skip-upstream")
    count="${STUB_LINES_audit:-0}"
    for ((i = 1; i <= count; i++)); do echo "audit diagnostic $i"; done
    exit "${STUB_EXIT_audit:-0}"
    ;;
  *)
    echo "the gate ran an unrecognised python3 command: $*" >&2
    exit 97
    ;;
esac
"""

# Reads the comment body off stdin into a file so a case can assert on what
# would have been posted, not merely that a call happened.
GH_STUB = r"""#!/usr/bin/env bash
{ printf '%s\t' "$(basename "$0")" "$@"; printf '\n'; } >> "$STUB_LOG"
cat > "$STUB_GH_BODY"
exit "${STUB_EXIT_gh:-0}"
"""

REPORT_LINE = re.compile(r"^- `(?P<name>[^`]+)` — (?P<status>pass|\*\*FAIL\*\*)$")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _ci_lint_steps() -> dict[str, str]:
    """ci.yml's `Run <tool>` steps in the gating `test` job, name -> shell.

    Read from the workflow rather than listed here: a check added to CI that
    never reaches the intake table is the drift this join exists to catch, and
    a hand-written list here would have to be edited to notice it.
    """
    lines = CI_WORKFLOW.read_text().splitlines()
    # The `test` job only. container-build is path-scoped and skips on runs
    # that touch nothing in the image, so it is not what "the gate" means.
    end = next((i for i, line in enumerate(lines) if line.startswith("  publish-coverage:")), len(lines))
    steps = {}
    for line in lines[:end]:
        match = re.match(r"^\s+- name: Run (?P<tool>.+)$", line)
        if match:
            tool = match.group("tool")
            steps[tool] = step_command(CI_WORKFLOW, f"Run {tool}")
    return steps


def _check_table() -> list[tuple[str, str]]:
    """The `for check in ... do` table as (check name, command) pairs.

    Read out of the step rather than listed here so a check added to the
    table gets a stub -- and a report line -- without this file being edited,
    which is what keeps "every check ran" from meaning "every check this file
    remembers ran".
    """
    body = step_run_body(AI_FIX_WORKFLOW, GATE_STEP)
    match = re.search(r"^\s*for check in\b(?P<table>.*?)^\s*do$", body, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"{GATE_STEP!r} no longer loops over a check table")
    # Backslash-newline inside a double-quoted word is a line continuation,
    # exactly as bash reads it, so a wrapped entry is one string here too.
    table = re.sub(r"\\\n\s*", " ", match.group("table"))
    entries = re.findall(r'"([^"]+)"', table)
    if not entries:
        raise AssertionError(f"{GATE_STEP!r}'s check table parsed to nothing")
    return [(entry.split(":", 1)[0], entry.split(":", 1)[1].strip()) for entry in entries]


def _tools_the_gate_needs() -> list[str]:
    """Every executable the check table calls, derived from the table itself."""
    tools = set()
    for _, command in _check_table():
        for segment in command.split("&&"):
            tools.add(segment.split()[0])
    return sorted(tools)


def _files_the_checks_name() -> list[str]:
    """Paths the gate's own commands pass to a linter, plus ci.yml's.

    The fixture checkout has to contain them, or `tests/e2e/*.sh` expands to
    nothing and the gate lints a literal glob.
    """
    paths = {"maintenance_audit.py"}
    for step in ("Run shellcheck", "Run hadolint"):
        # Everything after the tool name that is not a flag or a line
        # continuation is a path the checkout has to hold.
        for token in step_command(CI_WORKFLOW, step).split()[1:]:
            if not token.startswith("-") and token != "\\":
                paths.add(token)
    paths.update(str(path.relative_to(ROOT)) for path in ROOT.glob("tests/e2e/*.sh"))
    return sorted(paths)


def _make_stub(directory: Path, name: str, script: str) -> None:
    path = directory / name
    path.write_text(script)
    path.chmod(0o755)


class GateHarness:
    """A throwaway checkout with every tool the gate calls stubbed on PATH."""

    def __init__(self, tmp: Path, *, thresholds: str | None = None) -> None:
        self.root = tmp / "checkout"
        self.root.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.log = tmp / "calls.log"
        self.log.touch()

        (self.root / ".coverage-thresholds.json").write_text(
            THRESHOLDS.read_text() if thresholds is None else thresholds
        )
        for rel in _files_the_checks_name():
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()

        # `git rev-parse --short HEAD` names the commit the report describes.
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "initial")
        self.short_sha = _git(self.root, "rev-parse", "--short", "HEAD")

        for tool in _tools_the_gate_needs():
            _make_stub(self.bin, tool, PYTHON_STUB if tool == "python3" else GENERIC_STUB)
        _make_stub(self.bin, "gh", GH_STUB)
        self.gh_body = tmp / "comment-body.txt"

    def env(self, extra: dict[str, str]) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["STUB_LOG"] = str(self.log)
        env["STUB_GH_BODY"] = str(self.gh_body)
        env.update(extra)
        return env

    def run(self, step: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", step_run_body(AI_FIX_WORKFLOW, step)],
            env=self.env(extra),
            cwd=str(self.root),
            capture_output=True,
            text=True,
        )

    @property
    def calls(self) -> list[list[str]]:
        return [line.split("\t")[:-1] for line in self.log.read_text().splitlines() if line]

    @property
    def intake(self) -> str:
        return (self.root / "intake.md").read_text()

    def report(self) -> list[tuple[str, str]]:
        """The report's check lines as (name, pass | **FAIL**) pairs."""
        found = []
        for line in self.intake.splitlines():
            match = REPORT_LINE.match(line)
            if match:
                found.append((match.group("name"), match.group("status")))
        return found


# `git` and `jq` carry no skip gate: the suite already runs `git ls-files` and
# ci.yml's own jq-reading gate step unguarded, and a gate here would also make
# tests/test_nightly_compliance_workflow.py require that job to install them.
@unittest.skipUnless(shutil.which("bash"), "the step's shell is bash")
class GateStepTests(unittest.TestCase):
    """`Run the gate` -- the step that decides what the intake comment says."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def harness(self, **kwargs) -> GateHarness:
        return GateHarness(self.tmp, **kwargs)

    def threshold(self) -> int:
        return json.loads(THRESHOLDS.read_text())["gated"]["unit"]

    def test_the_step_reads_nothing_from_an_env_block(self) -> None:
        # Everything the gate reads comes from the runner and the checkout. If
        # a variable moves into an `env:` block, this harness would still pick
        # it up from its own environment and the case would pass for the wrong
        # reason -- so pin the absence.
        self.assertEqual(step_env(AI_FIX_WORKFLOW, GATE_STEP), {})

    def test_a_clean_run_reports_every_check_as_passing(self) -> None:
        harness = self.harness()
        proc = harness.run(GATE_STEP)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            harness.report(),
            [(name, "pass") for name in EXPECTED_CHECKS],
            harness.intake,
        )

    def test_the_report_names_each_check_once(self) -> None:
        # Two entries under one name make the comment ambiguous about which
        # command failed, and `${check%%:*}` will happily produce duplicates.
        harness = self.harness()
        harness.run(GATE_STEP)
        names = [name for name, _ in harness.report()]
        self.assertEqual(sorted(names), sorted(set(names)), names)

    def test_every_check_runs_the_command_it_is_named_for(self) -> None:
        # A check line reads as evidence that the named check ran. Nothing in
        # the report distinguishes `audit` running maintenance_audit.py from
        # `audit` running anything that exits 0, so bind each name to the
        # process the gate actually started.
        harness = self.harness()
        harness.run(GATE_STEP)
        for name, command in _check_table():
            for segment in command.split("&&"):
                tokens = [token for token in segment.split() if token != "\\"]
                matching = [call for call in harness.calls if call[0] == tokens[0]]
                self.assertTrue(matching, f"check `{name}` never ran `{tokens[0]}`")
                # Globs are compared by their expansion elsewhere; here only
                # the literal arguments have to appear in one call. The one
                # variable the table interpolates is substituted rather than
                # skipped, and any other would fail below rather than pass
                # unchecked.
                literal = [
                    token.replace("$threshold", str(self.threshold()))
                    for token in tokens[1:]
                    if "*" not in token
                ]
                self.assertTrue(
                    any(all(token in call for token in literal) for call in matching),
                    f"check `{name}` ran `{tokens[0]}` without {literal}: {matching}",
                )

    def test_the_report_covers_every_check_ci_gates_on(self) -> None:
        harness = self.harness()
        harness.run(GATE_STEP)
        reported = {name for name, _ in harness.report()}
        for tool in _ci_lint_steps():
            expected = CI_STEP_TO_CHECK.get(tool, tool)
            self.assertIn(
                expected,
                reported,
                f"ci.yml gates on `Run {tool}` and the intake table does not run it -- "
                "the comment would report a green main while CI is red on it",
            )

    def test_the_linters_are_called_the_way_ci_calls_them(self) -> None:
        # The commands themselves, not just the check names: a table entry
        # that lints one Containerfile where CI lints two reports a pass CI
        # will not reproduce.
        harness = self.harness()
        harness.run(GATE_STEP)
        calls = {call[0]: call[1:] for call in harness.calls if call[0] != "python3"}
        for tool, command in _ci_lint_steps().items():
            if tool == "tests":
                continue
            expected = [token for token in command.split() if token != "\\"]
            # Sorted, because the table writes `tests/e2e/*.sh` where ci.yml
            # spells the three suites out and the glob expands in its own
            # order. What has to match is the argument set.
            self.assertEqual(
                sorted([tool] + calls.get(tool, [])),
                sorted(expected),
                f"the gate runs `{tool}` differently than ci.yml's `Run {tool}` step does",
            )

    def test_the_shellcheck_glob_expands_against_the_checkout(self) -> None:
        # The table writes `tests/e2e/*.sh` where ci.yml spells the files out
        # (tests/test_lint_command_consistency.py compares the two lists). An
        # unmatched glob is passed through literally, and shellcheck would
        # then lint one file that does not exist instead of the suites.
        harness = self.harness()
        harness.run(GATE_STEP)
        shellcheck = next(call for call in harness.calls if call[0] == "shellcheck")
        self.assertNotIn("*", " ".join(shellcheck), "the glob reached shellcheck unexpanded")
        for path in sorted(ROOT.glob("tests/e2e/*.sh")):
            self.assertIn(str(path.relative_to(ROOT)), shellcheck)

    def test_the_report_names_the_commit_it_tested(self) -> None:
        harness = self.harness()
        harness.run(GATE_STEP)
        self.assertIn(f"### Gate on `main` at {harness.short_sha}", harness.intake)

    def test_the_coverage_check_enforces_the_repos_own_threshold(self) -> None:
        harness = self.harness()
        harness.run(GATE_STEP, STUB_COVERAGE_PERCENT="93")
        threshold = self.threshold()
        self.assertIn(
            ["python3", "-m", "coverage", "report", f"--fail-under={threshold}"],
            harness.calls,
            "the coverage check no longer passes the gate from .coverage-thresholds.json; "
            ".coveragerc sets no fail_under, so a bare report exits 0 at any percentage",
        )
        self.assertIn(f"Coverage 93% against a {threshold}% gate.", harness.intake)

    def test_coverage_below_the_gate_is_a_fail_not_a_printed_percentage(self) -> None:
        threshold = self.threshold()
        harness = self.harness()
        harness.run(GATE_STEP, STUB_COVERAGE_PERCENT=str(threshold - 1))
        self.assertEqual(dict(harness.report())["coverage"], "**FAIL**", harness.intake)
        self.assertEqual(dict(harness.report())["unittest"], "pass", harness.intake)
        self.assertIn(f"Coverage {threshold - 1}% against a {threshold}% gate.", harness.intake)

    def test_coverage_exactly_at_the_gate_passes(self) -> None:
        threshold = self.threshold()
        harness = self.harness()
        harness.run(GATE_STEP, STUB_COVERAGE_PERCENT=str(threshold))
        self.assertEqual(dict(harness.report())["coverage"], "pass", harness.intake)

    def test_an_unreadable_threshold_never_reports_coverage_as_passing(self) -> None:
        # `set -u` does not fire on a failed command substitution, so a
        # thresholds file the gate cannot read leaves `threshold` empty and
        # the run continues. The coverage entry has to be a FAIL then: a pass
        # would mean the comment reported an ungated coverage run as gated.
        harness = self.harness(thresholds=json.dumps({"gated": {}}))
        proc = harness.run(GATE_STEP)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(dict(harness.report())["coverage"], "**FAIL**", harness.intake)

    def test_a_failing_check_does_not_truncate_the_report(self) -> None:
        # The step is `set -uo pipefail` on purpose: a red main is the
        # comment's content. Under `-e` the report would stop at ruff and the
        # three checks after it would vanish from the comment without a word.
        harness = self.harness()
        proc = harness.run(GATE_STEP, STUB_EXIT_ruff="1", STUB_LINES_ruff="30")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            harness.report(),
            [(name, "**FAIL**" if name == "ruff" else "pass") for name in EXPECTED_CHECKS],
            harness.intake,
        )

    def test_a_failure_quotes_the_last_twenty_lines_of_its_output(self) -> None:
        harness = self.harness()
        harness.run(GATE_STEP, STUB_EXIT_ruff="1", STUB_LINES_ruff="30")
        lines = harness.intake.splitlines()
        opened = lines.index("- `ruff` — **FAIL**")
        fence = lines.index("```", opened)
        close = lines.index("```", fence + 1)
        quoted = lines[fence + 1 : close]
        self.assertEqual(len(quoted), 20, quoted)
        self.assertEqual(quoted[0], "ruff diagnostic 11")
        self.assertEqual(quoted[-1], "ruff diagnostic 30")

    def test_a_check_that_fails_on_stderr_is_still_quoted(self) -> None:
        # The loop captures `2>&1`. Most of these tools say why they failed on
        # stderr, and a FAIL with an empty code block is a comment that tells
        # the reader to go and run it themselves.
        threshold = self.threshold()
        harness = self.harness()
        harness.run(GATE_STEP, STUB_COVERAGE_PERCENT=str(threshold - 1))
        self.assertIn(f"is less than fail-under={threshold}", harness.intake)

    def test_every_check_that_passes_leaves_the_report_quiet(self) -> None:
        # A passing check contributes one line and no code block, so the
        # comment stays readable when main is green.
        harness = self.harness()
        harness.run(GATE_STEP)
        self.assertNotIn("```", harness.intake, harness.intake)


@unittest.skipUnless(shutil.which("bash"), "the step's shell is bash")
class CommentStepTests(unittest.TestCase):
    """`Post the intake comment` -- the job's only write."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = GateHarness(Path(self._tmp.name))

    def test_the_step_reads_the_issue_number_and_token_from_env(self) -> None:
        env = step_env(AI_FIX_WORKFLOW, COMMENT_STEP)
        self.assertEqual(env["GH_TOKEN"], "${{ github.token }}")
        # Two triggers, one variable. `github.event.issue.number` is unset on
        # a dispatch run, so the fallback is what makes `workflow_dispatch`
        # work at all -- and it names an input that has to exist.
        self.assertEqual(env["ISSUE"], "${{ github.event.issue.number || inputs.issue }}")
        text = AI_FIX_WORKFLOW.read_text()
        self.assertRegex(
            text,
            r"workflow_dispatch:\n    inputs:\n      issue:",
            "the fallback reads inputs.issue and the workflow declares no such input",
        )

    def test_the_job_runs_for_the_label_the_docs_advertise(self) -> None:
        text = AI_FIX_WORKFLOW.read_text()
        label = re.search(r"github\.event\.label\.name == '(?P<label>[^']+)'", text)
        self.assertIsNotNone(label, "the job no longer gates on a label name")
        self.assertIn("github.event_name == 'workflow_dispatch'", text)
        # The maintainer handbook documents this trigger by name. A label
        # renamed in one place and not the other leaves a workflow nobody can
        # start from the documented route.
        self.assertIn(
            f"`{label.group('label')}`",
            (ROOT / "maintainer_docs/MAINTAINER.md").read_text(),
            "maintainer_docs/MAINTAINER.md documents a different trigger label",
        )

    def test_the_comment_carries_the_gate_report_and_its_pointers(self) -> None:
        self.harness.run(GATE_STEP)
        report = self.harness.intake
        proc = self.harness.run(COMMENT_STEP, ISSUE="4242", GH_TOKEN="x", GITHUB_REPOSITORY="Danathar/aib")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [call for call in self.harness.calls if call[0] == "gh"],
            [["gh", "issue", "comment", "4242", "--repo", "Danathar/aib", "--body-file", "-"]],
        )
        body = self.harness.gh_body.read_text()
        self.assertTrue(
            body.startswith("**Intake for `ai-fix-requested`.** Automated context, not a fix."),
            body[:200],
        )
        self.assertIn(report.rstrip(), body)

    def test_the_comment_points_at_files_that_exist(self) -> None:
        # The closing paragraph sends the reader to two files by path. They
        # are the only onward instructions the comment gives, and a stale path
        # is invisible from inside the workflow.
        self.harness.run(GATE_STEP)
        self.harness.run(COMMENT_STEP, ISSUE="1", GH_TOKEN="x", GITHUB_REPOSITORY="Danathar/aib")
        body = self.harness.gh_body.read_text()
        pointed_at = re.findall(r"`([\w./-]+\.md)`", body)
        self.assertTrue(pointed_at, body)
        for rel in pointed_at:
            self.assertTrue((ROOT / rel).is_file(), f"the intake comment points at {rel}, which does not exist")

    def test_a_missing_report_turns_the_run_red(self) -> None:
        # `set -euo pipefail` with the report piped into `gh`: the run fails,
        # which is the signal that the comment is not the whole story. It does
        # not prevent the comment -- `gh` has already read the preamble off
        # the pipe by then -- so the red run is the only thing that says so.
        proc = self.harness.run(COMMENT_STEP, ISSUE="7", GH_TOKEN="x", GITHUB_REPOSITORY="Danathar/aib")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("intake.md", proc.stderr)

    def test_the_gate_result_is_quoted_verbatim_including_failures(self) -> None:
        self.harness.run(GATE_STEP, STUB_EXIT_actionlint="1", STUB_LINES_actionlint="3")
        self.harness.run(COMMENT_STEP, ISSUE="9", GH_TOKEN="x", GITHUB_REPOSITORY="Danathar/aib")
        body = self.harness.gh_body.read_text()
        self.assertIn("- `actionlint` — **FAIL**", body)
        self.assertIn("actionlint diagnostic 3", body)


if __name__ == "__main__":
    unittest.main()
