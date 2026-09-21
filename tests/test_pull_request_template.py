"""Join `.github/pull_request_template.md` to the checks it tells a contributor to run.

Script: tests/test_pull_request_template.py
What: Reads the pull request template and checks the things it hand-copies --
      the checklist of local checks, the order it lists them in, the paths
      those commands name, the two `CONTRIBUTING.md` sections it sends the
      reader to, the claim `CONTRIBUTING.md` makes about what it prefills, and
      the location GitHub has to find it at -- against the files that actually
      decide each.
Doing: Splits the template into `##` sections and parses its `- [ ]` lines into
       commands; parses the same command list out of
       `.github/copilot-instructions.md`'s *Before you push* fence; reads every
       command line of `ci.yml`'s `test` job; compares the three both ways on a
       token identity that ignores flags and shell substitutions, so
       `--fail-under=90` and `--fail-under="$(jq ...)"` are the same command and
       a `bashcov` wrapper around a harness still counts as running it;
       glob-expands the paths the commands name and joins them against the
       tracked tree; splits `CONTRIBUTING.md` on its headings; and AST-walks
       `tests/test_atomic_image_builder.py` for the document list its coverage
       threshold test reads.
Why: `.github/pull_request_template.md` was reached by two assertions, neither
     of which opens the checklist as a whole: the threshold test in
     tests/test_atomic_image_builder.py reads any line that states the coverage
     gate, and tests/test_lint_command_consistency.py reads the one
     `shellcheck` invocation. Everything else in the file -- which checks are
     listed at all, what the other five commands are, the section titles it
     points at -- was joined to nothing. No coverage percentage moves when it
     goes stale either: `.coveragerc` measures `atomic_image_builder.py` and
     the other Python modules, ruff does not read Markdown, and the
     Containerfile copies only the script, `template_snapshots/` and
     `container/entrypoint.sh` into the image, so the end-to-end tier cannot
     reach `.github/` at all.
Goal: Make a renamed CI step, a new gate, a moved shell harness, a renamed
      `CONTRIBUTING.md` section or a template moved out of the path GitHub
      reads fail here, instead of leaving a contributor with a checklist they
      can tick in full and still push a change CI rejects.

The checklist join is two-way, and both directions have already been wrong in
this repo. A box that names a command CI does not run sends a contributor to
run something that gates nothing; a command CI runs with no box is the drift
tests/test_lint_command_consistency.py was written for, after this template and
CONTRIBUTING.md came to name four of the seven `shellcheck` files. The
direction that catches the second case is restricted to the commands CI's
`test` job actually runs, because that is what the template itself claims of
the list: "CI runs the automated ones again on this PR". The canonical brief
also lists `python3 maintenance_audit.py --skip-upstream`, which no CI check on
a pull request runs -- it belongs to the scheduled maintenance-audit workflow
-- so requiring a box for it would be requiring the template to say something
untrue.
"""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TEMPLATE_RELATIVE = ".github/pull_request_template.md"
TEMPLATE = ROOT / TEMPLATE_RELATIVE
COPILOT = ROOT / ".github/copilot-instructions.md"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
BUILDER_TESTS = ROOT / "tests/test_atomic_image_builder.py"

CHECKLIST_SECTION = "How it was checked"

# The step list of ci.yml's `test` job. The container-build job is path-scoped
# and skips on a run that touches nothing in the image, so a check that only
# lives there is not one a contributor can be told they must have run.
CI_JOB = "test"

_BOX = re.compile(r"^- \[(?P<mark>.)\] (?P<rest>.*)$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _tracked() -> frozenset[str]:
    """Every path `git ls-files` reports, so an untracked scratch file is not
    mistaken for something the template may name."""
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return frozenset(path for path in listed if path)


def _sections(text: str) -> dict[str, list[str]]:
    """`## ` heading -> the lines under it, in file order."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = []
            sections[line[3:].strip()] = current
        elif current is not None:
            current.append(line)
    return sections


def _boxes(lines: list[str]) -> list[tuple[str, str]]:
    """The `- [ ]` lines of a section, as (mark, text) pairs."""
    found = []
    for line in lines:
        match = _BOX.match(line.strip())
        if match:
            found.append((match.group("mark"), match.group("rest").strip()))
    return found


def _tokens(command: str) -> list[str]:
    """A command's words, falling back to whitespace when quoting is uneven.

    ci.yml embeds a multi-line `jq` program whose single quotes span lines, so
    a line of it read on its own is not lexable. Splitting that line on
    whitespace instead keeps it in the comparison rather than dropping it,
    which matters for the direction that asserts a command is *not* run.
    """
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _identity(command: str) -> tuple[str, ...]:
    """The words of a command that name what it runs.

    Flags and anything holding a shell substitution are dropped, so the
    template's `--fail-under=90` and ci.yml's `--fail-under="$threshold"` are
    one command rather than two. What is left -- the binary or script, the
    subcommand, and the paths -- is what a reader is being told to run.
    """
    return tuple(
        token
        for token in _tokens(command)
        if token and not token.startswith("-") and "$" not in token
    )


def _commands(text: str) -> list[str]:
    """One command per `&&`-joined clause, with surrounding backticks removed."""
    stripped = text.strip()
    if stripped.startswith("`") and stripped.endswith("`"):
        stripped = stripped[1:-1]
    return [part.strip() for part in stripped.split("&&") if part.strip()]


def _fenced_bash(text: str, *, after: str) -> list[str]:
    """The command lines of the first ```bash fence following a heading."""
    start = text.index(after)
    fence = text.index("```bash\n", start) + len("```bash\n")
    body = text[fence : text.index("```", fence)]
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _ci_job_lines() -> list[str]:
    """Every non-comment line of ci.yml's `test` job.

    Parsed by indentation rather than with PyYAML, for the reason
    tests/_workflow_steps.py gives: CI installs only `coverage` and `ruff` for
    the unit suite, so a test module may not import a third-party parser.
    """
    # Continuations are joined first: ci.yml wraps its longest command over
    # three lines, and a line read on its own ends mid-argument-list.
    lines = re.sub(r"\\\n\s*", " ", CI_WORKFLOW.read_text()).splitlines()
    starts = [i for i, line in enumerate(lines) if line == f"  {CI_JOB}:"]
    if len(starts) != 1:
        raise AssertionError(f"expected exactly one {CI_JOB!r} job in {CI_WORKFLOW}")
    start = starts[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith("    "):
            end = i
            break
    return [
        line.strip()
        for line in lines[start:end]
        if line.strip() and not line.strip().startswith("#")
    ]


def _expanded(identity: tuple[str, ...]) -> set[str]:
    """A command's words with any glob replaced by the paths it matches.

    The docs write `tests/e2e/*.sh` where ci.yml names the three files, which
    is the same command said two ways. Expanding here rather than rewriting
    either side keeps the comparison honest in both directions: a fourth
    end-to-end suite that CI forgets to lint still fails, because the glob
    grows and CI's explicit list does not.
    """
    words: set[str] = set()
    for token in identity:
        if "*" in token:
            words.update(str(path.relative_to(ROOT)) for path in ROOT.glob(token))
        else:
            words.add(token)
    return words


def _ci_runs(command: str) -> bool:
    """Whether ci.yml's `test` job runs a command.

    Containment rather than equality: CI runs the two shell harnesses through
    `bashcov`, which adds words to the line without changing which harness is
    executed. Requiring every word of the command to appear in one line is what
    keeps that from matching a different command that merely shares a word --
    `python3` alone appears on half the lines of the job.
    """
    wanted = _expanded(_identity(command))
    if not wanted:
        return False
    return any(wanted <= _expanded(_identity(line)) for line in _ci_job_lines())


class PullRequestTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = TEMPLATE.read_text()
        self.sections = _sections(self.text)
        self.checklist = _boxes(self.sections[CHECKLIST_SECTION])
        self.template_commands = [
            command for _, text in self.checklist for command in _commands(text)
        ]
        self.canonical = _fenced_bash(COPILOT.read_text(), after="## Before you push")

    # -- where GitHub reads it from ------------------------------------------

    def test_template_is_tracked_at_the_path_github_reads(self) -> None:
        # GitHub prefills a pull request body from a handful of accepted
        # spellings and ignores everything else. A rename to one it does not
        # read leaves a file that still looks maintained and prefills nothing,
        # and no other test in the suite opens this path by name.
        self.assertIn(TEMPLATE_RELATIVE, _tracked())

    def test_only_one_pull_request_template_is_shipped(self) -> None:
        # Two accepted spellings in the tree is one template being rendered and
        # one being edited, with nothing saying which is which.
        accepted = re.compile(
            r"^(\.github/|docs/)?pull_request_template\.md$", re.IGNORECASE
        )
        shipped = sorted(path for path in _tracked() if accepted.match(path))
        self.assertEqual(shipped, [TEMPLATE_RELATIVE])

    # -- the checklist -------------------------------------------------------

    def test_checklist_lives_under_the_section_that_describes_it(self) -> None:
        # A box under "What changed" is a box a contributor ticks without
        # having been told it is a command to run.
        for heading, lines in self.sections.items():
            if heading == CHECKLIST_SECTION:
                self.assertTrue(_boxes(lines), f"{CHECKLIST_SECTION} lists no checks")
            else:
                self.assertEqual(
                    _boxes(lines),
                    [],
                    f"{heading!r} carries a checkbox, which belongs under "
                    f"{CHECKLIST_SECTION!r}",
                )

    def test_every_box_ships_unticked(self) -> None:
        # A template that ships with a box already ticked prefills every future
        # pull request with a claim its author never made.
        for mark, text in self.checklist:
            self.assertEqual(mark, " ", f"the box for {text!r} ships ticked")

    def test_every_box_is_a_command_in_backticks(self) -> None:
        # The checklist is copied out of the rendered body and pasted into a
        # shell. Prose in a box is prose someone runs.
        for _, text in self.checklist:
            self.assertTrue(
                text.startswith("`") and text.endswith("`"),
                f"checklist entry {text!r} is not a backticked command",
            )
            self.assertTrue(_identity(text), f"checklist entry {text!r} runs nothing")

    def test_every_box_is_a_command_the_canonical_brief_lists(self) -> None:
        # `.github/copilot-instructions.md` is the canonical brief every other
        # agent-facing file in the repo points at, and its *Before you push*
        # fence is the list. A box outside it is a check that exists for
        # contributors and for nobody else -- which is how the two lists drift.
        canonical = {_identity(command) for command in self.canonical}
        for command in self.template_commands:
            self.assertIn(
                _identity(command),
                canonical,
                f"{TEMPLATE_RELATIVE} asks for {command!r}, which is not in "
                f".github/copilot-instructions.md's *Before you push* list",
            )

    def test_every_canonical_check_ci_runs_has_a_box(self) -> None:
        # The direction that catches a missing check. Restricted to what the
        # `test` job runs, because that is the template's own claim about the
        # list: "CI runs the automated ones again on this PR".
        boxed = {_identity(command) for command in self.template_commands}
        for command in self.canonical:
            if not _ci_runs(command):
                continue
            self.assertIn(
                _identity(command),
                boxed,
                f"ci.yml's {CI_JOB} job runs {command!r} and "
                f"{TEMPLATE_RELATIVE} has no box for it, so a contributor can "
                f"tick every box and still push a change this check rejects",
            )

    def test_the_audit_command_is_the_one_canonical_check_ci_skips(self) -> None:
        # The exemption above, asserted rather than assumed. If a pull request
        # check ever does run the audit, this fails and the box becomes
        # required; if the audit is renamed, this fails rather than silently
        # widening the exemption to a command that does not exist.
        skipped = [command for command in self.canonical if not _ci_runs(command)]
        self.assertEqual(skipped, ["python3 maintenance_audit.py --skip-upstream"])
        self.assertNotIn("maintenance_audit", "\n".join(_ci_job_lines()))

    def test_the_checklist_keeps_the_canonical_order(self) -> None:
        # Order is what makes a list usable top to bottom: the canonical brief
        # runs the tests before the linters and the harnesses immediately after
        # the `shellcheck` that does not exercise them. A box list in another
        # order is a second opinion about what to run first.
        canonical_order = [_identity(command) for command in self.canonical]
        positions = [
            canonical_order.index(_identity(command))
            for command in self.template_commands
        ]
        self.assertEqual(
            positions,
            sorted(positions),
            f"{TEMPLATE_RELATIVE} lists its checks in an order "
            f".github/copilot-instructions.md does not",
        )

    def test_shell_harnesses_are_asked_for_and_not_only_linted(self) -> None:
        # The specific trap the canonical brief spells out: "`shellcheck` is
        # static analysis only; the behavioral assertions for the two shell
        # entrypoints are in the harnesses listed after it". Both harnesses
        # appear in the template as arguments to `shellcheck`, so a checklist
        # that names them only there reads as covering them while running
        # neither.
        for harness in ("tests/test_contrib_aib.sh", "tests/test_entrypoint.sh"):
            self.assertTrue(
                any(
                    _identity(command) == (harness,)
                    for command in self.template_commands
                ),
                f"{harness} is named in {TEMPLATE_RELATIVE} only as a "
                f"shellcheck argument, which lints it without running it",
            )

    def test_every_path_a_box_names_is_in_the_tree(self) -> None:
        # A command naming a file that has moved fails when a contributor runs
        # it, which is the one moment they have no way to tell a stale
        # checklist from a broken change of their own.
        tracked = _tracked()
        for command in self.template_commands:
            for token in _identity(command):
                if "/" not in token and not token.endswith(".sh"):
                    continue
                if "*" in token:
                    matches = sorted(path.relative_to(ROOT) for path in ROOT.glob(token))
                    self.assertTrue(matches, f"{command!r} names {token}, which matches nothing")
                    for match in matches:
                        self.assertIn(str(match), tracked)
                    continue
                self.assertTrue(
                    token in tracked or (ROOT / token).is_dir(),
                    f"{command!r} names {token}, which is not in the tree",
                )

    # -- the pointers --------------------------------------------------------

    def test_every_contributing_section_it_names_exists(self) -> None:
        # The template sends the reader to two sections of CONTRIBUTING.md by
        # title, which is the kind of reference that survives a rename looking
        # perfectly correct.
        headings = {
            line[3:].strip()
            for line in CONTRIBUTING.read_text().splitlines()
            if line.startswith("## ")
        }
        named = set(re.findall(r"CONTRIBUTING\.md's \"?([A-Z][A-Za-z ]+?)\"?(?: section)?[.,]", self.text))
        self.assertTrue(named, f"{TEMPLATE_RELATIVE} names no CONTRIBUTING.md section")
        for title in named:
            self.assertIn(title, headings)

    def test_the_tests_section_still_holds_the_pinned_install(self) -> None:
        # Naming the right section is half of it. The template says the pinned
        # install for the Python tools is in that section, and an unpinned
        # local `ruff` disagrees with CI in both directions.
        self.assertIn("CONTRIBUTING.md's Tests section", self.text)
        sections = _sections(CONTRIBUTING.read_text())
        body = "\n".join(sections["Tests"])
        self.assertRegex(body, r"pip install [^\n]*==")

    def test_contributing_describes_what_this_template_prefills(self) -> None:
        # CONTRIBUTING.md tells a contributor the template prefills "the
        # description with those questions and a checklist of the local
        # checks". Both halves are asserted here so deleting either from the
        # template fails rather than leaving CONTRIBUTING.md describing a file
        # that no longer does it.
        contributing = CONTRIBUTING.read_text()
        self.assertIn(TEMPLATE_RELATIVE, contributing)
        self.assertIn("checklist of the local checks", contributing)
        self.assertTrue(self.checklist)
        headings = [line[3:].strip().lower() for line in self.text.splitlines() if line.startswith("## ")]
        self.assertIn("what changed", headings)
        self.assertIn("why", headings)

    def test_the_threshold_test_still_reads_this_file(self) -> None:
        # The coverage gate number in the checklist is checked by
        # test_coverage_gate_threshold_has_one_source_of_truth, from a list of
        # documents written out there. Dropping this path from that list would
        # silently stop checking the one number in this file that a
        # contributor copies into a shell, and nothing here would notice.
        tree = ast.parse(BUILDER_TESTS.read_text())
        wanted = "test_coverage_gate_threshold_has_one_source_of_truth"
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == wanted
        ]
        self.assertEqual(len(functions), 1, f"{wanted} is gone from {BUILDER_TESTS.name}")
        documented = [
            [element.value for element in node.value.elts]
            for node in ast.walk(functions[0])
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.List)
            and any(
                isinstance(target, ast.Name) and target.id == "documented"
                for target in node.targets
            )
        ]
        self.assertEqual(len(documented), 1, f"{wanted} no longer assigns `documented`")
        self.assertIn(TEMPLATE_RELATIVE, documented[0])

    # -- what the rendered body looks like -----------------------------------

    def test_every_comment_is_closed(self) -> None:
        # An unclosed `<!--` swallows the rest of the template: the body still
        # renders, with the sections after it missing and nothing erroring.
        self.assertEqual(
            self.text.count("<!--"),
            self.text.count("-->"),
            f"{TEMPLATE_RELATIVE} opens and closes an unequal number of comments",
        )
        self.assertEqual(self.text.count("<!--"), len(_COMMENT.findall(self.text)))

    def test_guidance_renders_invisibly(self) -> None:
        # Every instruction in this file is written as an HTML comment so it
        # disappears from the rendered pull request body. Prose outside a
        # comment is prose pasted into every pull request opened from here,
        # where it reads as something the author wrote.
        # A comment is replaced by a blank line per line it spanned, so what
        # survives keeps the line numbers of the file it came from.
        visible = _COMMENT.sub(lambda match: "\n" * match.group().count("\n"), self.text)
        for lineno, line in enumerate(visible.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("## ") or _BOX.match(stripped):
                continue
            self.fail(
                f"{TEMPLATE_RELATIVE}:{lineno} is visible in every pull request "
                f"body opened from this template: {stripped[:60]!r}"
            )


if __name__ == "__main__":
    unittest.main()
