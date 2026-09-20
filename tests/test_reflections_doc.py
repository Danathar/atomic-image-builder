"""Join docs/reflections/ to the tree its one entry is a retrospective on.

Script: tests/test_reflections_doc.py
What: Reads docs/reflections/README.md and every entry beside it, then
      resolves the claims each still makes about live machinery -- the ruff
      config filename, the single source of the coverage threshold, the
      end-to-end suites' mounts and lint, the three guards the sweep left
      behind, the local-gate skill, the `ai-fix-requested` intake comment and
      the gated/advisory split -- against the workflow, script or config file
      that decides each one.
Doing: Discovers the entries rather than naming them, parses the index and the
       learning-artifact table out of README.md, resolves every Markdown link
       and in-document anchor in the directory, and AST-reads the two guards
       in tests/test_atomic_image_builder.py that the entry describes instead
       of restating what they contain.
Why: Nothing under docs/reflections/ was opened by any test at any tier.
     `.coveragerc` measures six Python modules and `.coveragerc.e2e` measures
     `atomic_image_builder` inside the image -- and the Containerfile COPYs
     only that module, template_snapshots/ and the entrypoint, so no Markdown
     file can move a percentage in either tier. The only test that touched
     this directory before this one is the repo-wide table aligner in
     tests/test_format_markdown_tables.py, which checks column padding, and a
     `docs/reflections/a.md` literal in tests/test_review_rubric_doc.py used
     as a path-shaped string without reading anything.
Goal: Make a renamed ruff config, a threshold spelled back into a workflow, a
      bare `:ro` mount, an e2e script dropped from the lint step or the path
      filter, a local-gate skill that stops running the shell harnesses, or an
      intake comment reordered so the check-first instruction moves fail here,
      instead of leaving a retrospective describing a repository that no
      longer exists.

The entry's dated figures are deliberately *not* asserted: it says they are
"as they were on 2026-09-03, not as they are", so a test that compared them
against today's coverage would be asserting the opposite of what the document
promises. What is checked there is that the disclaimer is still present, names
the entry's own date, and points at the document that has the current numbers.

Literals are parsed out of the documents rather than restated, with one
exception: REQUIRED_MENTIONS, the small set of names the entry must still
carry. An assertion computed only over what a document happens to say passes
once the document says nothing.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _workflow_steps import step_command, step_run_body  # noqa: E402

REFLECTIONS = ROOT / "docs/reflections"
INDEX_PATH = REFLECTIONS / "README.md"
INDEX = INDEX_PATH.read_text()

SWEEP_PATH = REFLECTIONS / "2026-09-03-acmm-sweep.md"
# Read on demand, not at import: a renamed entry must fail these tests with a
# sentence saying so, not crash the module before the directory tests -- the
# ones that would have caught the rename -- get to run.
SWEEP = SWEEP_PATH.read_text() if SWEEP_PATH.is_file() else ""

CI = ROOT / ".github/workflows/ci.yml"
AI_FIX = ROOT / ".github/workflows/ai-fix.yml"
THRESHOLDS_PATH = ROOT / ".coverage-thresholds.json"
SKILL_PATH = ROOT / ".claude/skills/verify-change/SKILL.md"
SUITE_PATH = ROOT / "tests/test_atomic_image_builder.py"
CLAUDE_MD_PATH = ROOT / "CLAUDE.md"
E2E = ROOT / "tests/e2e"

MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
TABLE_ROW = re.compile(r"^\|(.+)\|$", re.MULTILINE)
# The filename form README.md states: an ISO date, then a slug.
ENTRY_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
HEADING = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)
COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}

# Names the sweep entry has to keep. Each anchors an assertion below; without
# this set, deleting the sentence that carries one turns its assertion into a
# no-op rather than a failure.
REQUIRED_MENTIONS = (
    ".ruff.toml",
    "ruff.toml",
    "ACTION_PINS",
    "`:ro`",
    "ai-fix-requested",
    "2026-09-03",
)

INDEX_REQUIRED_MENTIONS = (
    ".claude/memory/corrections.md",
    ".claude/checkpoint.md",
    "docs/reflections/",
    "copilot-instructions.md",
)


def entry_paths() -> list[Path]:
    """Every entry in the directory, discovered rather than listed.

    Discovery is the point: an entry added without an index line, or an index
    line for a file nobody wrote, is exactly what this module has to catch.
    """
    return sorted(p for p in REFLECTIONS.glob("*.md") if p.name != "README.md")


def tracked(relative_path: str) -> bool:
    """Whether git tracks a path.

    The working tree is the wrong thing to ask about: a maintainer's scratch
    `.ruff.toml` would fail a check the repository does not ship.
    """
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--error-unmatch", relative_path],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def anchor_slug(heading: str) -> str:
    """GitHub's anchor for a heading, near enough for the headings here."""
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"[\s]+", "-", slug)


def suite_function(name: str) -> ast.FunctionDef:
    """One test function of tests/test_atomic_image_builder.py, as AST.

    The entry describes three guards by what they do. Reading them is how
    those descriptions get checked; restating their contents here would make
    this module the fourth copy of a rule the entry is already about.
    """
    tree = ast.parse(SUITE_PATH.read_text())
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(found) != 1:
        raise AssertionError(
            f"expected exactly one {name!r} in tests/test_atomic_image_builder.py, "
            f"found {len(found)}"
        )
    return found[0]


def string_constants(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


class ReflectionsDirectoryTests(unittest.TestCase):
    """The format contract README.md states about its own directory."""

    def test_directory_holds_only_the_index_and_dated_entries(self) -> None:
        present = sorted(p.name for p in REFLECTIONS.iterdir() if p.is_file())
        unexpected = [
            name
            for name in present
            if name != "README.md" and not ENTRY_NAME.match(name)
        ]
        self.assertEqual(
            unexpected,
            [],
            "a file under docs/reflections/ is neither the index nor a dated "
            "entry; README.md says each file is dated in its name",
        )
        self.assertIn("README.md", present, "docs/reflections/ has no index")

    def test_every_entry_is_dated_in_its_name(self) -> None:
        entries = entry_paths()
        self.assertGreater(
            len(entries),
            0,
            "docs/reflections/ holds no entry; an index of nothing states a "
            "contract no file is held to",
        )
        for path in entries:
            self.assertRegex(
                path.name,
                ENTRY_NAME,
                f"{path.name} is not <ISO date>-<slug>.md, the form "
                f"docs/reflections/README.md states",
            )

    def test_the_index_lists_every_entry_and_nothing_else(self) -> None:
        # The index is the only thing pointing at an entry -- no workflow or
        # script reads this directory -- so an entry missing from it is an
        # entry nobody finds.
        body = INDEX.split("## Index", 1)
        self.assertEqual(len(body), 2, "docs/reflections/README.md has no Index section")
        listed = [
            target
            for text, target in MARKDOWN_LINK.findall(body[1])
            if not target.startswith("http")
        ]
        self.assertEqual(
            sorted(listed),
            sorted(p.name for p in entry_paths()),
            "the Index and the files in docs/reflections/ disagree",
        )

    def test_each_index_line_carries_the_entry_date_and_its_title(self) -> None:
        body = INDEX.split("## Index", 1)[1]
        for text, target in MARKDOWN_LINK.findall(body):
            if target.startswith("http"):
                continue
            path = REFLECTIONS / target
            date = ENTRY_NAME.match(path.name).group(1)
            self.assertTrue(
                text.startswith(date),
                f"the index calls {target} {text!r}, which does not open with "
                f"the {date} in its filename",
            )
            title = HEADING.search(path.read_text())
            self.assertIsNotNone(title, f"{target} has no heading to title it")
            self.assertEqual(
                text,
                title.group(1),
                f"the index text for {target} is not its own title",
            )

    def test_every_link_in_the_directory_resolves(self) -> None:
        # These documents are almost entirely links into the rest of the repo,
        # and ../ hops from docs/reflections/ are two levels deep. A test that
        # read the prose and not the targets would pass over a renamed file.
        for doc in [INDEX_PATH, *entry_paths()]:
            for text, target in MARKDOWN_LINK.findall(doc.read_text()):
                if target.startswith("http"):
                    continue
                path_part, _, fragment = target.partition("#")
                if path_part:
                    resolved = (doc.parent / path_part).resolve()
                    self.assertTrue(
                        resolved.exists(),
                        f"{doc.name} links to {target} ({text!r}), which does not exist",
                    )
                else:
                    resolved = doc
                if not fragment:
                    continue
                if resolved.is_dir() or resolved.suffix != ".md":
                    continue
                slugs = {anchor_slug(h) for h in HEADING.findall(resolved.read_text())}
                self.assertIn(
                    fragment,
                    slugs,
                    f"{doc.name} links to #{fragment} in {path_part or doc.name}, "
                    f"which has no such heading",
                )

    def test_the_index_still_names_what_it_is_asserted_on(self) -> None:
        for mention in INDEX_REQUIRED_MENTIONS:
            self.assertIn(
                mention,
                INDEX,
                f"docs/reflections/README.md no longer names {mention}, which "
                f"this module asserts against",
            )


class LearningArtifactBoundaryTests(unittest.TestCase):
    """README.md's table says there are three artifacts and where each lives."""

    def artifact_rows(self) -> list[list[str]]:
        rows = [
            [cell.strip() for cell in row.split("|")]
            for row in TABLE_ROW.findall(INDEX.split("## Index", 1)[0])
        ]
        return [row for row in rows if row and not set(row[0]) <= {"-", ":", ""}][1:]

    def test_the_table_names_three_artifacts_and_each_exists(self) -> None:
        rows = self.artifact_rows()
        self.assertEqual(
            len(rows),
            3,
            "the learning-artifact table no longer has three rows, and the "
            "prose beside it calls this directory the third artifact",
        )
        targets = []
        for row in rows:
            link = MARKDOWN_LINK.search(row[0])
            # The row for this directory names itself and has nowhere to link
            # to, so it is written as a repo-relative path in a code span
            # while the other two are ../ links out of here.
            if link:
                target, resolved = link.group(2), INDEX_PATH.parent / link.group(2)
            else:
                target, resolved = row[0].strip("`"), ROOT / row[0].strip("`")
            targets.append(target)
            self.assertTrue(
                resolved.resolve().exists(),
                f"the table names {target}, which does not exist",
            )
        self.assertEqual(
            len(set(targets)),
            3,
            "the table names the same artifact twice",
        )

    def test_each_row_states_a_distinct_grain_and_trigger(self) -> None:
        rows = self.artifact_rows()
        grains = [row[1] for row in rows]
        triggers = [row[2] for row in rows]
        self.assertEqual(
            len(set(grains)),
            len(grains),
            "two learning artifacts claim the same grain, which is the overlap "
            "the table exists to prevent",
        )
        self.assertEqual(len(set(triggers)), len(triggers))

    def test_claude_md_agrees_this_is_the_third_artifact(self) -> None:
        # CLAUDE.md is auto-loaded and the README is not, so the boundary is
        # stated in both. Two statements of one rule is a drift pair.
        claude_md = CLAUDE_MD_PATH.read_text()
        self.assertIn(
            "docs/reflections/",
            claude_md,
            "CLAUDE.md no longer names the directory whose boundary it states",
        )
        self.assertRegex(
            claude_md,
            r"third and longest-grained",
            "CLAUDE.md no longer calls docs/reflections/ the third artifact, "
            "which docs/reflections/README.md says it does",
        )

    def test_claude_md_counts_the_artifacts_it_then_lists(self) -> None:
        # The same defect one level down: a sentence that counts the bullets
        # under it, and bullets that are added without touching the sentence.
        claude_md = CLAUDE_MD_PATH.read_text().splitlines()
        counted = [
            (i, line)
            for i, line in enumerate(claude_md)
            if re.search(r"\b(\w+) things in this directory\b", line)
        ]
        self.assertEqual(
            len(counted),
            1,
            "CLAUDE.md no longer counts the things in .claude/ exactly once",
        )
        index, line = counted[0]
        word = re.search(r"\b(\w+) things in this directory\b", line).group(1).lower()
        self.assertIn(word, COUNT_WORDS, f"CLAUDE.md counts them {word!r}")
        bullets = 0
        for following in claude_md[index + 1 :]:
            if following.startswith("- "):
                bullets += 1
            elif bullets and not following.startswith((" ", "\t")) and following.strip():
                break
        self.assertEqual(
            COUNT_WORDS[word],
            bullets,
            f"CLAUDE.md says {word} things and then lists {bullets}",
        )


class SweepEntryClaimTests(unittest.TestCase):
    """The 2026-09-03 entry's claims about machinery that is still here."""

    def test_the_entry_still_names_what_it_is_asserted_on(self) -> None:
        self.assertTrue(
            SWEEP_PATH.is_file(),
            f"{SWEEP_PATH.name} is gone; the claims below are asserted against "
            f"it by name, so a renamed entry has to be renamed here too",
        )
        for mention in REQUIRED_MENTIONS:
            self.assertIn(
                mention,
                SWEEP,
                f"the sweep entry no longer names {mention}, which this module "
                f"asserts against",
            )

    def test_the_ruff_config_uses_the_plain_filename(self) -> None:
        # "`.ruff.toml` silently wins over `ruff.toml`, so the dotted name is a
        # trap for whoever adds the plain one later."
        self.assertTrue(tracked("ruff.toml"), "ruff.toml is not tracked")
        self.assertFalse(
            tracked(".ruff.toml"),
            ".ruff.toml is tracked again; it silently wins over ruff.toml, "
            "which is the trap the sweep entry describes removing",
        )

    def test_the_gate_and_the_scenarios_are_files_of_their_own(self) -> None:
        # "the gate and the tests were written inline in `ci.yml` rather than
        # in files of their own."
        self.assertTrue(THRESHOLDS_PATH.is_file(), ".coverage-thresholds.json is missing")
        ci = CI.read_text()
        for script in ("tests/e2e/smoke.sh", "tests/e2e/coverage_scenarios.sh"):
            self.assertTrue(
                (ROOT / script).is_file(), f"{script} is missing"
            )
            # Named in a `run:`, not merely mentioned. ci.yml discusses these
            # scripts in comments, so a substring search passes a workflow
            # that only talks about running them.
            self.assertRegex(
                ci,
                rf"(?m)^\s*run: {re.escape(script)}\b",
                f"ci.yml no longer runs {script}; the scenarios are back inline",
            )

    def test_the_threshold_has_one_source_the_workflow_reads(self) -> None:
        # "The gate's threshold was written out in five places with nothing
        # tying them together."
        thresholds = json.loads(THRESHOLDS_PATH.read_text())
        ci = CI.read_text()
        self.assertIn(".coverage-thresholds.json", ci)
        self.assertNotRegex(
            ci,
            r"--fail-under=\d",
            "ci.yml spells a threshold literal, which silently wins over the "
            "file it reads",
        )
        self.assertNotIn(
            str(thresholds["gated"]["unit"]),
            step_run_body(CI, "Report coverage"),
            "the gating step writes the threshold value instead of reading it",
        )

    def test_only_the_unit_tier_is_gated(self) -> None:
        # "unit coverage stood at 100% and was the *weakest* of the coverage
        # signals ... The end-to-end tier ... stood near 10%." The figures are
        # dated; which tier carries the gate is not.
        thresholds = json.loads(THRESHOLDS_PATH.read_text())
        self.assertEqual(
            sorted(thresholds["gated"]),
            ["unit"],
            "a second tier is gated, or the unit gate is gone",
        )
        self.assertIn(
            "e2e",
            thresholds["advisory"],
            "the end-to-end tier is no longer advisory, and the entry explains "
            "why its number reads low",
        )

    def test_the_scenarios_can_be_run_locally(self) -> None:
        # "The end-to-end scenarios could not be run locally, were not linted,
        # and used a bare `:ro` mount." Each half is checked; this is the first.
        readme = (E2E / "README.md").read_text()
        for command in ("podman build", "tests/e2e/smoke.sh", "tests/e2e/coverage_scenarios.sh"):
            self.assertIn(
                command,
                readme,
                f"tests/e2e/README.md no longer shows how to run {command} "
                f"locally",
            )
        modes = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-s", "tests/e2e"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        executable = {
            line.split("\t", 1)[1]
            for line in modes
            if line.startswith("100755")
        }
        self.assertEqual(
            executable,
            {"tests/e2e/smoke.sh", "tests/e2e/coverage_scenarios.sh"},
            "the suites README.md tells a reader to execute are not the ones "
            "committed with the executable bit",
        )

    def test_the_scenarios_are_linted_by_a_job_that_always_runs(self) -> None:
        # The second half. The lint step sits in the unconditional `test` job
        # rather than the path-scoped container job, so it runs on every push.
        shellcheck = step_command(CI, "Run shellcheck")
        for script in sorted(p.name for p in E2E.glob("*.sh")):
            self.assertIn(
                f"tests/e2e/{script}",
                shellcheck,
                f"shellcheck no longer covers tests/e2e/{script}",
            )

    def test_no_mount_in_the_suites_is_bare_ro(self) -> None:
        # The third half, and the one that failed on every machine this tool
        # targets: `:ro` without a relabel is unreadable to the container on an
        # SELinux host.
        suffix = re.search(
            r'^E2E_MOUNT_SUFFIX="([^"]+)"', (E2E / "lib.sh").read_text(), re.MULTILINE
        )
        self.assertIsNotNone(
            suffix, "tests/e2e/lib.sh no longer defines E2E_MOUNT_SUFFIX"
        )
        self.assertRegex(
            suffix.group(1),
            r"(^|,)[zZ]($|,)",
            f"E2E_MOUNT_SUFFIX is {suffix.group(1)!r}, which does not relabel "
            f"the source for container access",
        )
        for script in sorted(E2E.glob("*.sh")):
            for mount in re.findall(r'-v "([^"]+)"', script.read_text()):
                _, _, options = mount.rpartition(":")
                self.assertTrue(
                    options == "$E2E_MOUNT_SUFFIX" or re.search(r"[zZ]", options),
                    f"{script.name} mounts {mount} without a relabel",
                )

    def test_the_pointer_guard_compares_prose_not_command_blocks(self) -> None:
        # Guard 2: "comparing sentences, which then flagged a *command block*
        # that a 'run these commands' skill has to contain."
        import test_atomic_image_builder

        sample = (
            "A sentence long enough to count as prose rather than a heading here.\n"
            "```bash\n"
            "python3 -m coverage report --fail-under=$(jq -er '.gated.unit' file)\n"
            "```\n"
        )
        prose = test_atomic_image_builder._markdown_prose(sample)
        self.assertIn(
            "A sentence long enough to count as prose rather than a heading here.",
            prose,
        )
        self.assertEqual(
            [line for line in prose if "coverage report" in line],
            [],
            "the pointer guard reads fenced command blocks as prose again, "
            "which forces a skill that lists commands into evasion",
        )
        # And the skill it flagged is still a skill that lists commands.
        self.assertIn("```", SKILL_PATH.read_text())

    def test_the_pointer_guard_reads_tracked_files(self) -> None:
        # Guard 3: "A scan of the working tree rather than tracked files, which
        # failed on a maintainer's personal untracked file -- green in CI, red
        # on their machine."
        guard = suite_function("test_agent_guidance_has_one_canonical_file")
        self.assertIn(
            "ls-files",
            string_constants(guard),
            "the pointer guard no longer asks git which files are tracked",
        )

    def test_the_threshold_check_strips_clock_times(self) -> None:
        # Guard 4: "A threshold check that read `04` out of the clock time
        # `04:00` because the same line mentioned the gate."
        check = suite_function("test_coverage_gate_threshold_has_one_source_of_truth")
        strippers = []
        for constant in string_constants(check):
            try:
                pattern = re.compile(constant)
            except re.error:
                continue
            # fullmatch, not search: the check's own `\b\d+\b` finds the `04`
            # inside `04:00`, which is the bug rather than the fix. What has
            # to exist is a pattern that claims the whole clock time, so the
            # substitution removes it before any number is read.
            if pattern.fullmatch("04:00"):
                strippers.append(constant)
        self.assertTrue(
            strippers,
            "the threshold check has no pattern that claims a whole clock "
            "time, so `04:00` on a line about the gate reads as a stale "
            "threshold again",
        )
        self.assertTrue(
            any(
                isinstance(node.func, ast.Attribute) and node.func.attr == "sub"
                for node in ast.walk(check)
                if isinstance(node, ast.Call)
            ),
            "the threshold check no longer substitutes anything out of a gate "
            "line before reading its numbers",
        )

    def test_the_container_job_fires_on_changes_to_the_suites_it_runs(self) -> None:
        # "A path-scoped CI job did not fire on changes to the very end-to-end
        # suites it runs, while lint still passed -- so a change to a test read
        # as covered."
        ci = CI.read_text()
        path_filter = re.search(r"git diff --quiet .*?;", ci)
        self.assertIsNotNone(
            path_filter, "the container job's path filter has changed shape"
        )
        self.assertIn(
            "tests/e2e/",
            path_filter.group(0),
            "the container job skips changes to the suites it runs",
        )

    def test_the_local_gate_skill_runs_the_shell_harnesses_with_a_threshold(self) -> None:
        # "A 'complete local gate' skill omitted the two behavioural shell
        # harnesses and ran coverage without a threshold, so it could have
        # declared a change ready that CI rejects."
        skill = SKILL_PATH.read_text()
        fences = re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        self.assertTrue(fences, "the verify-change skill has no command fence")
        # The first token of each command line. shellcheck's argument list
        # names both harnesses too, so a membership test on the text passes a
        # skill that only lints them -- which is the omission the entry is
        # about: static analysis standing in for running them.
        commands = {
            line.split()[0]
            for fence in fences
            for line in fence.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for harness in ("tests/test_contrib_aib.sh", "tests/test_entrypoint.sh"):
            self.assertIn(
                harness,
                commands,
                f"the verify-change skill no longer runs {harness}; "
                f"shellcheck naming it is not running it",
            )
        self.assertRegex(
            skill,
            r"--fail-under=\"\$\(jq [^\"]*\.gated\.unit[^\"]*"
            r"\.coverage-thresholds\.json[^\"]*\)\"",
            "the verify-change skill no longer reads the gate from "
            ".coverage-thresholds.json; a bare report exits 0 however far "
            "coverage has fallen, and a literal here drifts from ci.yml",
        )

    def test_the_intake_comment_closes_by_telling_the_reader_to_check_first(self) -> None:
        # "carried into the `ai-fix-requested` intake comment added at the end
        # of the sweep, which closes by telling its reader to do exactly that."
        body = step_run_body(AI_FIX, "Post the intake comment")
        echoed = [
            line.strip()[len("echo ") :].strip().strip('"')
            for line in body.splitlines()
            if line.strip().startswith("echo ")
        ]
        echoed = [line for line in echoed if line]
        self.assertTrue(echoed, "the intake comment echoes nothing")
        self.assertIn(
            "Before treating this as new work",
            "\n".join(echoed),
            "the intake comment no longer tells its reader to check whether "
            "the capability already exists",
        )
        opening = echoed[0]
        self.assertNotIn(
            "Before treating this as new work",
            opening,
            "the entry says the comment closes with the check-first "
            "instruction, and it now opens with it",
        )
        closing = "\n".join(echoed[-5:])
        self.assertIn(
            "Before treating this as new work",
            closing,
            "the check-first instruction is no longer in the comment's closing "
            "lines, which is what the entry claims about it",
        )

    def test_no_dashboard_was_added(self) -> None:
        # "A dashboard would have placed all of those side by side as though
        # comparable. That is why there is not one."
        tracked_paths = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        dashboards = [
            path
            for path in tracked_paths
            if "dashboard" in path.lower() and not path.startswith("tests/")
        ]
        self.assertEqual(
            dashboards,
            [],
            "a dashboard was added; the entry says why there is not one",
        )

    def test_the_dated_figures_are_marked_as_of_the_entry_date(self) -> None:
        date = ENTRY_NAME.match(SWEEP_PATH.name).group(1)
        self.assertIn(
            f"as they were on {date}, not as they are",
            SWEEP,
            "the sweep entry's figures lost the sentence that dates them, and "
            "nothing else keeps them from being read as current",
        )
        metrics = MARKDOWN_LINK.findall(SWEEP)
        self.assertIn(
            "../metrics.md",
            [target for _, target in metrics],
            "the entry no longer points at the document with the current "
            "numbers",
        )


if __name__ == "__main__":
    unittest.main()
