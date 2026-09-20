"""Join `.claude/skills/verify-change/SKILL.md` to the gate it tells you to run.

Script: tests/test_verify_change_skill.py
What: Reads the one shipped skill and checks every pointer it hand-copies --
      its front matter, its catalog row, the command list it says
      `.github/copilot-instructions.md` shares, each path those commands
      name, the `jq` key the gate reads, the `fail_under` it says `.coveragerc`
      does not set, the four documents it says the threshold is checked
      against, the two tools it says skip, and the sections and build commands
      it sends the reader to -- against the file that actually decides each.
Doing: Parses the SKILL.md front matter and its `## Run` fence into an ordered
       command list; compares that list with the identical fence in
       `.github/copilot-instructions.md` and with what `ci.yml` runs;
       glob-expands the `shellcheck` and `hadolint` arguments and joins them
       both ways against the tracked tree and the CI steps; reads
       `.coverage-thresholds.json` through the skill's own `jq` filter; parses
       `.coveragerc` as the ini file coverage reads; AST-walks
       `tests/test_atomic_image_builder.py` for the document list its
       threshold test checks and for every `skipUnless(shutil.which(...))` in
       the suite; splits `CONTRIBUTING.md` and `maintainer_docs/MAINTAINER.md`
       on their headings and asserts the named section still holds the thing
       the skill says is in it; runs `maintenance_audit.py --help` for the
       flag; and checks `git check-ignore` still ships the directory.
Why: `.claude/skills/` was opened by no test at any tier. `.coveragerc`
     measures Python modules only, ruff does not read Markdown, and the
     Containerfile copies `atomic_image_builder.py`, `template_snapshots/`
     and `container/entrypoint.sh` into the image and nothing else -- so the
     end-to-end tier cannot reach this directory either. No percentage moves
     when a command in here stops being the command CI runs. This file is the
     procedure an agent follows to decide a change is ready to push, and a
     stale step does not error: it reports a gate that was never run, or
     quietly drops the check that would have caught the defect.
Goal: Make a renamed CI step, a moved shell harness, a new Containerfile, a
      renamed threshold key, a third tool that skips on a missing binary, or a
      renamed MAINTAINER.md section fail here, instead of quietly turning the
      one shipped skill into a procedure that verifies less than it claims.

The joins are two-way wherever a one-way assertion would leave one side free
to drift: asserting only that `shellcheck` in the skill names files that exist
leaves CI free to check a file the skill never mentions, and asserting only
that CI's list is covered leaves the skill free to name a file CI skips. Both
directions hold or the instruction is wrong for somebody.

The command list is asserted equal to `.github/copilot-instructions.md`'s,
ordering included, because that is what the skill claims: "has the same list;
this adds the ordering and what to do with each result". The prose around the
fence is what this file adds, not the fence.
"""

from __future__ import annotations

import ast
import configparser
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from _workflow_steps import step_command

ROOT = Path(__file__).resolve().parent.parent

SKILL_DIR = ROOT / ".claude/skills/verify-change"
SKILL_PATH = SKILL_DIR / "SKILL.md"
SKILLS_README = ROOT / ".claude/skills/README.md"
COPILOT_PATH = ROOT / ".github/copilot-instructions.md"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
THRESHOLDS_PATH = ROOT / ".coverage-thresholds.json"
COVERAGERC = ROOT / ".coveragerc"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
MAINTAINER = ROOT / "maintainer_docs/MAINTAINER.md"
E2E_README = ROOT / "tests/e2e/README.md"
AUDIT_SCRIPT = ROOT / "maintenance_audit.py"
BUILDER_TESTS = ROOT / "tests/test_atomic_image_builder.py"

# The skill, named rather than discovered. Discovering it would make every
# assertion below vacuous the moment the file stopped being shipped, which is
# one of the things this module exists to catch; the two-way catalog join in
# `test_every_shipped_skill_has_a_catalog_row` is what stops a second skill
# being added without joining it to the README.
SKILL_NAME = "verify-change"


def _front_matter(text: str) -> dict[str, str]:
    """The `key: value` lines of a leading `---` fenced block."""
    if not text.startswith("---\n"):
        raise AssertionError(f"{SKILL_PATH} does not open with YAML front matter")
    end = text.index("\n---\n", 3)
    fields = {}
    for line in text[4:end].splitlines():
        key, _, value = line.partition(": ")
        fields[key.strip()] = value.strip()
    return fields


def _fenced_bash(text: str, *, after: str) -> list[str]:
    """The lines of the first ```bash fence following a heading.

    Comments and blank lines are dropped: they are the prose inside the fence,
    not commands. Nothing here writes a continuation, so a trailing backslash
    is a drift this refuses to normalise away.
    """
    start = text.index(after)
    fence = text.index("```bash\n", start) + len("```bash\n")
    body = text[fence : text.index("```", fence)]
    commands = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            raise AssertionError(f"line continuation in the fence after {after!r}: {stripped}")
        commands.append(stripped)
    return commands


def _sections(text: str, level: int) -> dict[str, str]:
    """Heading title -> body, split on headings of exactly one level."""
    marker = "#" * level + " "
    sections = {}
    title = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith(marker) and not line.startswith(marker + "#"):
            if title is not None:
                sections[title] = "\n".join(body)
            title = line[len(marker) :].strip()
            body = []
        elif title is not None:
            body.append(line)
    if title is not None:
        sections[title] = "\n".join(body)
    return sections


def _shell_words(command: str) -> list[str]:
    """A command split into words, with the quoting these fences use removed."""
    return [word.strip("\"'") for word in command.split()]


def _expand(argument: str) -> list[str]:
    """Paths a shell argument names, glob-expanded against the checkout."""
    if any(char in argument for char in "*?["):
        return sorted(str(path.relative_to(ROOT)) for path in ROOT.glob(argument))
    return [argument]


SKILL_TEXT = SKILL_PATH.read_text()
SKILL_COMMANDS = _fenced_bash(SKILL_TEXT, after="## Run")


def _command_starting(prefix: str) -> str:
    """The one command in the skill's fence that starts with `prefix`."""
    matches = [command for command in SKILL_COMMANDS if command.startswith(prefix)]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {prefix!r} command in {SKILL_PATH}, found {len(matches)}")
    return matches[0]


class VerifyChangeSkillStructureTests(unittest.TestCase):
    def test_front_matter_names_the_directory_that_holds_it(self) -> None:
        # Claude Code resolves a skill by its front-matter name, not by the
        # directory it sits in, so the two disagreeing is invisible until the
        # skill is invoked and the wrong one -- or none -- answers.
        fields = _front_matter(SKILL_TEXT)
        self.assertEqual(
            fields.get("name"),
            SKILL_DIR.name,
            f"{SKILL_PATH} declares a name that is not its directory",
        )
        self.assertEqual(fields.get("name"), SKILL_NAME)
        self.assertTrue(
            fields.get("description"),
            "a skill with no description is one the agent never selects",
        )

    def test_every_shipped_skill_has_a_catalog_row_and_every_row_a_skill(self) -> None:
        # Two-way: a row pointing at a deleted skill sends the reader nowhere,
        # and a skill with no row is one nobody finds from the index.
        shipped = sorted(
            path.parent.name for path in (ROOT / ".claude/skills").glob("*/SKILL.md")
        )
        linked = sorted(re.findall(r"\[`([^`]+)`\]\(([^)]+)/SKILL\.md\)", SKILLS_README.read_text()))
        self.assertEqual([name for name, _ in linked], [target for _, target in linked],
                         "a catalog row labels one skill and links another")
        self.assertEqual(shipped, [name for name, _ in linked])
        self.assertIn(SKILL_NAME, shipped)
        for name in shipped:
            self.assertTrue((ROOT / ".claude/skills" / name / "SKILL.md").is_file())

    def test_git_still_ships_the_skill_the_gitignore_re_includes(self) -> None:
        # `.gitignore` excludes `.claude/*` and re-includes this directory. A
        # re-include lost in an edit does not error: the file simply stops
        # being tracked, and the next clone has no skill at all.
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(SKILL_PATH.relative_to(ROOT))],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(ignored.returncode, 1, f"{SKILL_PATH} is ignored by .gitignore")
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(SKILL_PATH.relative_to(ROOT))],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        self.assertEqual(tracked.returncode, 0, f"{SKILL_PATH} is not tracked")


class VerifyChangeRunBlockTests(unittest.TestCase):
    def test_the_command_list_is_the_one_copilot_instructions_ships(self) -> None:
        # The skill says the canonical brief "has the same list; this adds the
        # ordering and what to do with each result". Same list means same
        # commands in the same order: a check added to one file and not the
        # other leaves one of the two reporting a gate it never ran.
        self.assertEqual(
            SKILL_COMMANDS,
            _fenced_bash(COPILOT_PATH.read_text(), after="## Before you push"),
            "the skill's Run block and .github/copilot-instructions.md's have drifted apart",
        )

    def test_every_path_the_commands_name_exists(self) -> None:
        # A command naming a moved file fails in the reader's terminal with a
        # shell error they then have to diagnose, mid-procedure.
        named = []
        for command in SKILL_COMMANDS:
            for word in _shell_words(command):
                if word.startswith("-") or "/" not in word and not word.endswith((".py", ".json", ".sh")):
                    continue
                if word.startswith("$") or "(" in word:
                    continue
                named.append(word)
        self.assertTrue(named, "no paths parsed out of the Run block")
        for argument in named:
            with self.subTest(argument=argument):
                expanded = _expand(argument)
                self.assertTrue(expanded, f"{argument} matches nothing in the checkout")
                for path in expanded:
                    self.assertTrue((ROOT / path).exists(), f"{SKILL_PATH} runs a missing {path}")

    def test_the_test_command_is_the_one_ci_runs(self) -> None:
        self.assertIn(
            _command_starting("python3 -m coverage run"),
            step_command(CI_WORKFLOW, "Run tests"),
            "the skill's coverage run is not what ci.yml's Run tests step runs",
        )
        self.assertIn(
            "python3 -m unittest discover -s tests",
            SKILL_COMMANDS,
            "the skill no longer runs the bare suite first",
        )
        self.assertTrue(
            (ROOT / "tests").is_dir(),
            "`discover -s tests` names a directory that does not exist",
        )

    def test_the_gate_reads_the_key_the_threshold_file_actually_holds(self) -> None:
        # The gate's whole point is that the number is not written down here.
        # A renamed key turns `jq -er` into an empty `--fail-under=`, which
        # coverage rejects -- but only for whoever runs it, and only after the
        # suite has finished.
        gate = _command_starting("python3 -m coverage report")
        filters = re.findall(r"jq -er '([^']+)' ([^)\"]+)", gate)
        self.assertEqual(len(filters), 1, f"expected one jq filter in {gate!r}")
        expression, threshold_file = filters[0]
        self.assertEqual(threshold_file.strip(), str(THRESHOLDS_PATH.relative_to(ROOT)))
        value = json.loads(THRESHOLDS_PATH.read_text())
        for key in expression.strip().lstrip(".").split("."):
            self.assertIn(key, value, f"{expression} does not resolve in {threshold_file}")
            value = value[key]
        self.assertIsInstance(value, int, f"{expression} is not a number coverage can gate on")
        # And it is the same filter ci.yml reads, so the local gate and the
        # pushed gate cannot enforce two different numbers.
        self.assertIn(
            f"jq -er '{expression}' {threshold_file.strip()}",
            step_command(CI_WORKFLOW, "Report coverage"),
            "ci.yml reads a different threshold than the skill tells you to",
        )

    def test_coveragerc_still_sets_no_fail_under(self) -> None:
        # The comment above the gate command explains why the bare `coverage
        # report` is not enough. If `.coveragerc` ever grew a `fail_under`,
        # that explanation would be wrong in the direction that matters: a
        # reader who believed it would keep passing a threshold that the
        # config had already made redundant, or conflict with.
        parser = configparser.ConfigParser()
        parser.read(COVERAGERC)
        for section in parser.sections():
            self.assertNotIn(
                "fail_under",
                parser[section],
                f".coveragerc's [{section}] sets fail_under; the skill says it does not",
            )

    def test_shellcheck_covers_exactly_the_files_ci_shellchecks(self) -> None:
        # Two-way. The skill writes `tests/e2e/*.sh` and ci.yml writes the
        # three files out, so a new end-to-end script joins the local run by
        # the glob and the CI run only when somebody remembers -- which is the
        # drift this pins.
        skill_files = set()
        for argument in _shell_words(_command_starting("shellcheck"))[1:]:
            if argument.startswith("-"):
                continue
            skill_files.update(_expand(argument))
        ci_files = {
            word
            for word in _shell_words(step_command(CI_WORKFLOW, "Run shellcheck"))[1:]
            if not word.startswith("-") and word != "\\"
        }
        self.assertEqual(skill_files, ci_files, "the skill and ci.yml shellcheck different files")
        self.assertTrue(skill_files)
        for path in skill_files:
            self.assertTrue((ROOT / path).is_file(), f"shellcheck is pointed at a missing {path}")
        # Every end-to-end script is reached by the glob, so none is linted
        # only by whoever thought to name it.
        self.assertLessEqual(
            {str(path.relative_to(ROOT)) for path in (ROOT / "tests/e2e").glob("*.sh")},
            skill_files,
        )

    def test_the_shell_harnesses_run_after_the_static_check_of_them(self) -> None:
        # The comment says shellcheck is static analysis only and the
        # behavioral assertions are in these harnesses. An order that ran the
        # harnesses first would contradict the ordering the skill exists to
        # add, and a harness listed but not shellchecked is one half of the
        # pair missing.
        harnesses = [command for command in SKILL_COMMANDS if command.startswith("tests/")]
        self.assertEqual(harnesses, ["tests/test_contrib_aib.sh", "tests/test_entrypoint.sh"])
        shellcheck_at = SKILL_COMMANDS.index(_command_starting("shellcheck"))
        for harness in harnesses:
            self.assertTrue((ROOT / harness).is_file())
            self.assertGreater(SKILL_COMMANDS.index(harness), shellcheck_at)
            self.assertIn(harness, _command_starting("shellcheck"))

    def test_actionlint_is_invoked_with_no_file_arguments(self) -> None:
        # The comment's reason -- that a bare invocation lints every workflow
        # and so cannot fall out of step with the directory -- stops being
        # true the moment a file argument appears.
        self.assertEqual(_command_starting("actionlint"), "actionlint")
        self.assertEqual(step_command(CI_WORKFLOW, "Run actionlint").strip(), "actionlint")

    def test_hadolint_lints_every_containerfile_in_the_tree(self) -> None:
        # hadolint takes a file list, so a third Containerfile is linted by
        # nobody until it is named here and in ci.yml. `template_snapshots/`
        # is excluded: those files are vendored copies of somebody else's
        # tree, kept byte-identical to upstream and governed by
        # maintenance_audit.py's drift check, so linting them would report
        # findings nobody here may fix.
        named = _shell_words(_command_starting("hadolint"))[1:]
        tracked = [
            path
            for path in subprocess.run(
                ["git", "ls-files", "Containerfile*", "*/Containerfile*"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()
            if not path.startswith("template_snapshots/")
        ]
        self.assertEqual(sorted(named), sorted(tracked), "a Containerfile the skill does not lint")
        self.assertEqual(
            sorted(named),
            sorted(_shell_words(step_command(CI_WORKFLOW, "Run hadolint"))[1:]),
            "ci.yml hadolints a different set than the skill does",
        )

    def test_the_audit_still_accepts_the_flag_the_skill_runs(self) -> None:
        audit = _command_starting("python3 maintenance_audit.py")
        flags = [word for word in _shell_words(audit) if word.startswith("--")]
        self.assertTrue(flags, f"{audit!r} passes no flag")
        help_text = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT), "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for flag in flags:
            self.assertIn(flag, help_text, f"maintenance_audit.py no longer accepts {flag}")


def _skipped_tools() -> set[str]:
    """Binaries the suite skips on, read from every `skipUnless` decorator."""
    tools = set()
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
            if name not in {"skipUnless", "skipIf"}:
                continue
            for argument in ast.walk(node.args[0] if node.args else ast.Constant(None)):
                if (
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Attribute)
                    and argument.func.attr == "which"
                    and argument.args
                    and isinstance(argument.args[0], ast.Constant)
                ):
                    tools.add(argument.args[0].value)
    return tools


class VerifyChangeProseTests(unittest.TestCase):
    def test_the_tools_it_says_skip_are_the_tools_the_suite_skips_on(self) -> None:
        # "Two tools do that here" is a count as much as a list. A third tool
        # gaining a `skipUnless` would make a green local run hide one more
        # thing than the skill warns about, and nothing else would say so.
        #
        # The suite also skips on `bash`, which is deliberately not listed:
        # the skill's own reason for naming a tool is "CI installs both at
        # pinned versions, so a skip here becomes a real run there", and that
        # is the set joined against -- the binaries ci.yml fetches from a
        # pinned release. A missing `bash` is not that case; the Run block's
        # two shell harnesses cannot run at all without it, so it can never
        # be the silent skip this section warns about.
        section = _sections(SKILL_TEXT, 2)["What a missing tool hides"]
        listed = set(re.findall(r"^- \*\*`([^`]+)`\*\*", section, flags=re.MULTILINE))
        ci_text = CI_WORKFLOW.read_text()
        pinned = {
            tool
            for tool in _skipped_tools()
            if re.search(rf"/{re.escape(tool)}/releases/download/", ci_text)
        }
        self.assertEqual(listed, pinned, "the skill's skip list is not the suite's")
        self.assertIn("Two tools", section, "the skill's count is no longer written as two")
        self.assertEqual(len(listed), 2)
        for tool in _skipped_tools() - listed:
            with self.subTest(tool=tool):
                self.assertTrue(
                    any(
                        (ROOT / harness).read_text().splitlines()[0].endswith(tool)
                        for harness in ("tests/test_contrib_aib.sh", "tests/test_entrypoint.sh")
                    ),
                    f"{tool} skips tests, is not pinned by ci.yml, and is not what the "
                    f"Run block's harnesses already require -- the skill should name it",
                )

    def test_the_skip_report_command_uses_the_verbose_flag_that_prints_skips(self) -> None:
        # `grep -i skipped` finds nothing without `-v`: unittest prints the
        # reasons only in verbose mode, so a dropped flag turns the command
        # into one that always reports no skips at all.
        section = _sections(SKILL_TEXT, 2)["What a missing tool hides"]
        command = re.search(r"`(python3 -m unittest[^`]+)`", section)
        self.assertIsNotNone(command, "the skill no longer gives a command for listing skips")
        self.assertIn(" -v", command.group(1))
        self.assertIn("skipped", command.group(1))

    def test_the_documents_it_says_the_gate_is_checked_against_are_the_checked_ones(self) -> None:
        # The claim is what makes a docs-only change run the suite. Reading
        # the list out of the test that enforces it, rather than restating it,
        # is what keeps the two in step: a document added to the enforcing
        # list and not to the skill leaves a contributor believing they can
        # skip the run.
        module = ast.parse(BUILDER_TESTS.read_text())
        documented: list[str] | None = None
        for node in ast.walk(module):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if getattr(target, "id", None) != "documented" or not isinstance(node.value, ast.List):
                continue
            values = [element.value for element in node.value.elts if isinstance(element, ast.Constant)]
            if all(value.endswith(".md") for value in values):
                self.assertIsNone(documented, "more than one `documented` list to join against")
                documented = values
        self.assertIsNotNone(documented, "tests/test_atomic_image_builder.py no longer builds a `documented` list")
        flat = " ".join(_sections(SKILL_TEXT, 2)["Reading the results"].split())
        marker = "threshold is checked against"
        self.assertIn(marker, flat, "the skill no longer says which documents the threshold is checked against")
        rest = flat[flat.index(marker) + len(marker) :]
        # The sentence ends at the first period that is not part of a file
        # extension: `CONTRIBUTING.md,` has one too, and cutting there would
        # read the list as a single entry.
        stop = re.search(r"\.(?=\s|$)", rest)
        sentence = rest[: stop.start()] if stop else rest
        named = set(re.findall(r"[\w./-]+\.md|PR template", sentence))
        # The skill names each document the way a reader would say it --
        # `MAINTAINER.md` for `maintainer_docs/MAINTAINER.md`, "the PR
        # template" for the file -- so the join is by what each name can only
        # mean, not by string equality with the path.
        def _matches(name: str, relative_path: str) -> bool:
            if name == "PR template":
                return "pull_request_template" in relative_path
            return relative_path == name or relative_path.endswith("/" + name)

        for relative_path in documented:
            with self.subTest(document=relative_path):
                self.assertTrue((ROOT / relative_path).is_file(), f"{relative_path} is checked but does not exist")
                self.assertTrue(
                    any(_matches(name, relative_path) for name in named),
                    f"the threshold test checks {relative_path}, which the skill does not list",
                )
        for name in named:
            with self.subTest(name=name):
                self.assertTrue(
                    any(_matches(name, relative_path) for relative_path in documented),
                    f"the skill says the threshold is checked against {name}, which the test does not check",
                )

    def test_the_sections_it_sends_the_reader_to_exist_and_still_hold_the_answer(self) -> None:
        # Two-way, and the body is read as well as the title: a heading that
        # kept its name after its contents moved elsewhere is the same broken
        # pointer with a passing test.
        self.assertIn("CONTRIBUTING.md`'s *Tests* section", SKILL_TEXT)
        contributing = _sections(CONTRIBUTING.read_text(), 2)
        self.assertIn("Tests", contributing)
        self.assertIn(
            "pip install",
            contributing["Tests"],
            "CONTRIBUTING.md's Tests section no longer holds the install command the skill sends readers to",
        )
        self.assertRegex(
            contributing["Tests"],
            r"ruff==\d",
            "the skill promises pinned versions; CONTRIBUTING.md's Tests section pins none",
        )

        self.assertIn("MAINTAINER.md`'s *Reading the weekly audit*", SKILL_TEXT)
        maintainer = _sections(MAINTAINER.read_text(), 2)
        self.assertIn("Reading the weekly audit", maintainer)
        body = maintainer["Reading the weekly audit"]
        for word in ("failure", "advisor"):
            self.assertIn(
                word,
                body.lower(),
                "the section the skill cites for the failure/advisory line no longer draws it",
            )

    def test_the_end_to_end_pointer_names_a_readme_with_two_build_commands(self) -> None:
        # "tests/e2e/README.md has the two build commands" is a count, and the
        # count is the reader's cue that one image is built from the other.
        uncovered = _sections(SKILL_TEXT, 2)["What this does not cover"]
        self.assertIn("tests/e2e/README.md", uncovered)
        self.assertIn("tests/e2e/smoke.sh", uncovered)
        self.assertTrue((ROOT / "tests/e2e/smoke.sh").is_file())
        builds = [
            line.strip()
            for line in E2E_README.read_text().splitlines()
            if re.match(r"(podman|docker|buildah) build", line.strip())
        ]
        self.assertEqual(len(builds), 2, f"tests/e2e/README.md has {len(builds)} build commands, not two")
        self.assertTrue(any("-f Containerfile " in line for line in builds))
        self.assertTrue(any("container/Containerfile.coverage" in line for line in builds))

    def test_what_it_calls_unverified_locally_is_what_the_image_ships(self) -> None:
        # The list of paths the local gate cannot verify is only correct while
        # it covers everything the Containerfile copies in. A new COPY source
        # outside it is a change the skill would tell you is already verified.
        uncovered = _sections(SKILL_TEXT, 2)["What this does not cover"]
        copied = [
            line.split()[1]
            for line in (ROOT / "Containerfile").read_text().splitlines()
            if line.startswith("COPY ")
        ]
        self.assertTrue(copied, "Containerfile copies nothing; the claim has no subject left")
        for source in copied:
            with self.subTest(source=source):
                self.assertTrue(
                    source in uncovered or source.split("/")[0] + "/" in uncovered,
                    f"the Containerfile copies in {source}, which the skill does not list as unverified",
                )


if __name__ == "__main__":
    unittest.main()
