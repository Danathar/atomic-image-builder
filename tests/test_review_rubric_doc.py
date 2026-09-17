"""Join docs/review-rubric.md, and the .editorconfig its item 4 cites, to the tree.

Script: tests/test_review_rubric_doc.py
What: Reads docs/review-rubric.md and resolves every claim each numbered item
      makes about the machinery -- the gate list that lives in CONTRIBUTING.md
      rather than here, the consolidated coverage threshold, the advisory
      tiers, the graduated drift check, the `insert_final_newline` semantics,
      the path-scoped container job, the vendored snapshot and the tool
      permissions -- against the workflow, script or config file that decides
      it. Then it holds `.editorconfig` itself to the claim in its own header:
      that every rule in it was measured off the committed tree, so applying
      it to an existing file is a no-op.
Doing: Splits the rubric by numbered heading, pulls its links and inline code
       spans, and checks each against the committed tree. Parses
       `.editorconfig` into sections, resolves each section's glob against
       `git ls-files` with a matcher that carries its own case table, and
       compares every rule against what the matched files actually contain.
Why: Neither file was opened by any test at any tier. `.coveragerc` measures
     six Python modules and `.coveragerc.e2e` has the same Python-only shape,
     so neither a Markdown claim nor an EditorConfig rule can move a
     percentage -- the only test that touched either before this one is the
     repo-wide table aligner in tests/test_format_markdown_tables.py, which
     checks column padding, and a `classify()` case in
     tests/test_risk_tiers_doc.py that uses the string ".editorconfig" as a
     path-shaped literal without reading the file. An EditorConfig rule that
     stops describing the tree is worse than absent: editors apply it, so it
     rewrites files instead of leaving them alone, and the rubric is the
     document that tells a reviewer that exact failure mode.
Goal: Make a renamed gate, a threshold spelled back into a workflow, a drift
      check re-armed to fail weekly, a path filter that drops tests/e2e/, a
      permission dropped from .claude/settings.json, or an EditorConfig rule
      that no longer matches the files it names fail here, instead of leaving
      the rubric describing a repository that no longer exists.

The rubric's literals are parsed out of the document rather than restated: a
test that repeats them is a second copy to keep in step. What is hard-coded is
the *shape* of each claim, plus REQUIRED_MENTIONS -- the small set of literals
the document must still name, because an assertion computed only over what the
document happens to say passes once the document says nothing.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _workflow_steps import step_command, step_run_body  # noqa: E402

DOC_PATH = ROOT / "docs/review-rubric.md"
DOC = DOC_PATH.read_text()

EDITORCONFIG_PATH = ROOT / ".editorconfig"
EDITORCONFIG = EDITORCONFIG_PATH.read_text()

CI = ROOT / ".github/workflows/ci.yml"
AUDIT_WORKFLOW = ROOT / ".github/workflows/maintenance-audit.yml"
AUDIT_SCRIPT = ROOT / "maintenance_audit.py"
CONTRIBUTING_PATH = ROOT / "CONTRIBUTING.md"
THRESHOLDS_PATH = ROOT / ".coverage-thresholds.json"
SETTINGS_PATH = ROOT / ".claude/settings.json"
SNAPSHOT_DIR = ROOT / "template_snapshots"

REPO_SLUG = "Danathar/atomic-image-builder"

MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
BACKTICKED = re.compile(r"`([^`]+)`")
NUMBERED_HEADING = re.compile(r"^## (\d+)\. (.+)$", re.MULTILINE)

# Literals the rubric has to keep naming. Each anchors an assertion below;
# without this set, deleting the sentence that carries one would turn its
# assertion into a no-op rather than a failure.
REQUIRED_MENTIONS = (
    "insert_final_newline = false",
    "template_snapshots/",
    ".claude/settings.json",
)

# The gate names CONTRIBUTING.md has to list, derived below from what ci.yml's
# `test` job actually runs rather than written out here.
GATE_TOOLS = ("unittest", "coverage", "ruff", "shellcheck", "actionlint", "hadolint")


def tracked_files() -> list[str]:
    """Every path `git ls-files` reports, relative to the checkout root."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [name for name in out.split("\0") if name]


TRACKED = tracked_files()


def section_body(title_number: int) -> str:
    """The body of `## <n>. ...`, up to the next `##` heading."""
    starts = [m for m in NUMBERED_HEADING.finditer(DOC) if int(m.group(1)) == title_number]
    if len(starts) != 1:
        raise AssertionError(f"expected exactly one `## {title_number}.` heading, found {len(starts)}")
    start = starts[0]
    nxt = DOC.find("\n## ", start.end())
    return DOC[start.end() : nxt if nxt != -1 else len(DOC)]


# --------------------------------------------------------------------------
# EditorConfig parsing and glob matching.
#
# Hand-rolled rather than taken from a library: CI installs only `coverage`
# and `ruff` for the unit suite (CONTRIBUTING.md pins both), so a test module
# here may not import a third-party parser. The subset understood is what this
# repo's .editorconfig writes -- `*`, `?`, `**`, `{a,b}` and a literal path --
# and the matcher carries its own case table in GlobMatcherTests, because a
# matcher asserted only through the file it is pointed at can be wrong in the
# same direction as the thing it checks.
# --------------------------------------------------------------------------


def editorconfig_sections() -> list[tuple[str, dict[str, str]]]:
    """`.editorconfig`'s `[glob]` sections, in file order, with their rules."""
    sections: list[tuple[str, dict[str, str]]] = []
    current: dict[str, str] | None = None
    for raw in EDITORCONFIG.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = {}
            sections.append((line[1:-1], current))
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise AssertionError(f".editorconfig line is neither a section nor a rule: {raw!r}")
        if current is None:
            continue  # preamble (`root = true`), handled separately
        current[key.strip()] = value.strip()
    return sections


def editorconfig_preamble() -> dict[str, str]:
    """The rules written above the first `[glob]` section."""
    preamble: dict[str, str] = {}
    for raw in EDITORCONFIG.splitlines():
        line = raw.strip()
        if line.startswith("["):
            break
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        preamble[key.strip()] = value.strip()
    return preamble


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Compile one EditorConfig section glob against checkout-relative paths.

    A glob with no `/` in it matches in any directory, which is why `*.md`
    reaches docs/ and `.simplecov` reaches a copy anywhere. `*` stops at a
    separator and `**` crosses one -- the distinction `template_snapshots/**`
    depends on, and the reason this is not `fnmatch`, whose `*` crosses `/`.
    """
    anchored = "/" in glob
    out = ["^"] if anchored else ["^(?:.*/)?"]
    i = 0
    while i < len(glob):
        char = glob[i]
        if char == "*":
            if glob[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "{":
            close = glob.index("}", i)
            alts = glob[i + 1 : close].split(",")
            out.append("(?:" + "|".join(re.escape(alt) for alt in alts) + ")")
            i = close + 1
            continue
        else:
            out.append(re.escape(char))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def files_matching(glob: str) -> list[str]:
    """Tracked files a section glob applies to."""
    pattern = glob_to_regex(glob)
    return [name for name in TRACKED if pattern.match(name)]


def resolved_rules(path: str) -> dict[str, str]:
    """The rules in force for one path: later sections win, as EditorConfig says."""
    rules: dict[str, str] = {}
    for glob, section in editorconfig_sections():
        if glob_to_regex(glob).match(path):
            rules.update(section)
    return rules


def yaml_key_indents(text: str) -> list[int]:
    """Indent widths of YAML mapping keys, skipping block-scalar contents.

    A `run: |` body is arbitrary shell, indented to whatever the shell wants,
    so measuring every line would say the workflows are 1-space indented. Only
    the structure carries the `indent_size` claim.
    """
    key = re.compile(r"^( *)(- )?[A-Za-z_][A-Za-z0-9_.-]*:( |$)")
    return [len(m.group(1)) for line in text.splitlines() if (m := key.match(line)) and m.group(1)]


class GlobMatcherTests(unittest.TestCase):
    """The matcher above, against cases the repo's own globs do not all cover."""

    def test_a_bare_glob_matches_at_any_depth(self) -> None:
        pattern = glob_to_regex("*.md")
        self.assertTrue(pattern.match("README.md"))
        self.assertTrue(pattern.match("docs/review-rubric.md"))
        self.assertTrue(pattern.match("docs/reflections/a.md"))
        self.assertFalse(pattern.match("README.mdc"))

    def test_a_literal_name_matches_at_any_depth(self) -> None:
        pattern = glob_to_regex(".simplecov")
        self.assertTrue(pattern.match(".simplecov"))
        self.assertTrue(pattern.match("vendor/.simplecov"))
        self.assertFalse(pattern.match(".simplecov.bak"))

    def test_a_single_star_does_not_cross_a_separator(self) -> None:
        pattern = glob_to_regex("template_snapshots/*")
        self.assertTrue(pattern.match("template_snapshots/README.md"))
        self.assertFalse(pattern.match("template_snapshots/containerfile/Justfile"))

    def test_a_double_star_crosses_separators(self) -> None:
        pattern = glob_to_regex("template_snapshots/**")
        self.assertTrue(pattern.match("template_snapshots/containerfile/Justfile"))
        self.assertFalse(pattern.match("templates/containerfile/Justfile"))

    def test_braces_are_alternation(self) -> None:
        pattern = glob_to_regex("*.{yml,yaml}")
        self.assertTrue(pattern.match(".github/workflows/ci.yml"))
        self.assertTrue(pattern.match("a.yaml"))
        self.assertFalse(pattern.match("a.yml.bak"))

    def test_a_path_glob_is_anchored_at_the_root(self) -> None:
        pattern = glob_to_regex("template_snapshots/containerfile/Justfile")
        self.assertTrue(pattern.match("template_snapshots/containerfile/Justfile"))
        self.assertFalse(pattern.match("vendor/template_snapshots/containerfile/Justfile"))

    def test_a_question_mark_is_one_non_separator_character(self) -> None:
        pattern = glob_to_regex("a?.py")
        self.assertTrue(pattern.match("ab.py"))
        self.assertFalse(pattern.match("a/.py"))

    def test_later_sections_win(self) -> None:
        # `[*]` sets trim_trailing_whitespace = true and `[*.md]` turns it off;
        # reading the sections in the other order would silently invert this.
        self.assertEqual(resolved_rules("docs/review-rubric.md")["trim_trailing_whitespace"], "false")
        self.assertEqual(resolved_rules("atomic_image_builder.py")["trim_trailing_whitespace"], "true")


class DocumentShapeTests(unittest.TestCase):
    """The rubric is an ordered list of numbered items; keep it one."""

    def test_the_numbered_items_are_contiguous_and_in_order(self) -> None:
        numbers = [int(m.group(1)) for m in NUMBERED_HEADING.finditer(DOC)]
        self.assertEqual(
            numbers,
            list(range(1, len(numbers) + 1)),
            "the rubric says it lists what review looks for 'and in what order', so a gap or a "
            "repeat in the numbering makes the order it claims unreadable",
        )
        self.assertGreaterEqual(len(numbers), 8, "items were removed without this test being updated")

    def test_every_anchor_literal_is_still_named(self) -> None:
        for mention in REQUIRED_MENTIONS:
            self.assertIn(
                mention,
                DOC,
                f"docs/review-rubric.md no longer names {mention!r}, which leaves the assertion "
                "resolving it against the tree with nothing to check",
            )

    def test_the_closing_section_is_present(self) -> None:
        self.assertIn(
            "## What review does not do here",
            DOC,
            "the section bounding review's scope is what stops item 1 reading as a mandate to "
            "re-run the gate",
        )


class LinkTests(unittest.TestCase):
    """Every link the rubric offers has to lead somewhere."""

    def test_relative_links_resolve_to_tracked_files(self) -> None:
        for text, target in MARKDOWN_LINK.findall(DOC):
            if target.startswith("http"):
                continue
            resolved = (DOC_PATH.parent / target.split("#", 1)[0]).resolve()
            self.assertTrue(
                resolved.is_file(),
                f"docs/review-rubric.md links [{text}]({target}), which resolves to {resolved} -- "
                "not a file in this checkout",
            )
            self.assertIn(
                str(resolved.relative_to(ROOT)),
                TRACKED,
                f"[{text}]({target}) resolves outside the tracked tree",
            )

    def test_absolute_links_stay_in_this_repository(self) -> None:
        externals = [t for _, t in MARKDOWN_LINK.findall(DOC) if t.startswith("http")]
        self.assertTrue(externals, "the rubric cites no issue; its examples are no longer evidenced")
        for target in externals:
            self.assertTrue(
                target.startswith(f"https://github.com/{REPO_SLUG}/"),
                f"{target} points outside {REPO_SLUG}: every example in this document is supposed "
                "to be one this repository actually hit",
            )

    def test_an_issue_link_agrees_with_the_number_it_is_written_as(self) -> None:
        for text, target in MARKDOWN_LINK.findall(DOC):
            if not target.startswith("http"):
                continue
            match = re.search(r"/issues/(\d+)$", target)
            self.assertIsNotNone(match, f"{target} is not an issue link")
            self.assertEqual(
                text,
                f"#{match.group(1)}",
                f"the link text {text!r} does not name the issue it points at ({target}); a reader "
                "checking the example follows the number, not the URL",
            )


class MechanicalChecksLiveInContributingTests(unittest.TestCase):
    """The rubric's opening claim: the gates are CI's and CONTRIBUTING.md's, not its."""

    def test_the_document_points_at_contributing(self) -> None:
        self.assertIn(
            "CONTRIBUTING.md",
            DOC,
            "the rubric hands the mechanical checks to CONTRIBUTING.md by name; dropping the "
            "pointer leaves a reader with a rubric that silently omits them",
        )

    def test_contributing_lists_every_gate_ci_runs(self) -> None:
        # Derived from ci.yml rather than from a list kept here: a gate added
        # to the workflow and not to CONTRIBUTING.md is exactly the drift the
        # rubric's opening paragraph assumes cannot happen.
        ci_text = CI.read_text()
        contributing = CONTRIBUTING_PATH.read_text()
        for tool in GATE_TOOLS:
            self.assertIn(tool, ci_text, f"ci.yml no longer runs {tool}; GATE_TOOLS is stale")
            self.assertIn(
                tool,
                contributing,
                f"ci.yml runs {tool} and CONTRIBUTING.md does not mention it, so the rubric's "
                "'CONTRIBUTING.md lists them' is no longer true",
            )

    def test_the_rubric_does_not_restate_a_gate_command(self) -> None:
        # "A rubric that repeats 'tests pass' adds nothing a gate has not
        # already decided" -- so the document must not grow a command list of
        # its own, which would be a second copy to keep in step.
        self.assertNotIn("```", DOC, "the rubric has grown a code block; the commands belong in CONTRIBUTING.md")
        for command in ("ruff check", "shellcheck -x", "hadolint ", "python3 -m unittest"):
            self.assertNotIn(
                command,
                DOC,
                f"the rubric now spells {command!r}, which CONTRIBUTING.md already owns",
            )


class SingleSourceTests(unittest.TestCase):
    """Item 2: a value in more than one file needs a single source."""

    def test_the_gated_threshold_is_spelled_in_no_workflow(self) -> None:
        threshold = json.loads(THRESHOLDS_PATH.read_text())["gated"]["unit"]
        for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
            text = workflow.read_text()
            if ".coverage-thresholds.json" not in text:
                continue
            self.assertNotRegex(
                text,
                rf"fail-under[= ]{threshold}\b|fail_under[= ]{threshold}\b",
                f"{workflow.name} spells the gate as {threshold} instead of reading "
                ".coverage-thresholds.json -- item 2 of the rubric is about exactly this",
            )
            self.assertIn(
                "jq -er '.gated.unit' .coverage-thresholds.json",
                text,
                f"{workflow.name} names .coverage-thresholds.json but no longer reads the gate "
                "out of it",
            )

    def test_the_consolidation_the_rubric_cites_still_holds(self) -> None:
        body = section_body(2)
        self.assertIn(
            "single source",
            body,
            "item 2 no longer asks for a single source, which is the rule the rest of this class "
            "checks the tree against",
        )
        self.assertTrue(
            THRESHOLDS_PATH.is_file(),
            "the file item 2 points to as the consolidated home of the threshold is gone",
        )


class AdvisoryTierTests(unittest.TestCase):
    """Item 4: a low percentage in an advisory tier is not a gap."""

    def test_exactly_one_tier_is_gated_and_the_rest_are_advisory(self) -> None:
        thresholds = json.loads(THRESHOLDS_PATH.read_text())
        self.assertEqual(
            list(thresholds["gated"]),
            ["unit"],
            "item 4 tells a reviewer that a low percentage can be the tier working as intended; "
            "that reading only holds while unit is the one gated tier",
        )
        self.assertTrue(
            thresholds["advisory"],
            "every tier is gated now, so the rubric's advice to read a low advisory percentage "
            "as intentional is wrong",
        )

    def test_the_item_still_cites_the_issue_that_decided_it(self) -> None:
        body = section_body(4)
        self.assertRegex(
            body,
            r"\[#\d+\]\(https://github\.com/" + re.escape(REPO_SLUG) + r"/issues/\d+\)",
            "item 4's examples are load-bearing precisely because each one happened here; an "
            "uncited example is a generic checklist entry",
        )


class GraduatedDriftTests(unittest.TestCase):
    """Item 4: failing the weekly audit on upstream drift, until it was graduated."""

    def test_snapshot_drift_fails_only_past_a_threshold(self) -> None:
        source = AUDIT_SCRIPT.read_text()
        self.assertIn(
            "SNAPSHOT_DRIFT_FAILURE_COMMITS",
            source,
            "the graduation item 4 describes was undone: drift now has no failure threshold, so "
            "any movement takes the weekly audit red again",
        )
        self.assertRegex(
            source,
            r"if ahead >= SNAPSHOT_DRIFT_FAILURE_COMMITS",
            "drift no longer compares against the failure threshold before failing",
        )

    def test_the_audit_that_went_red_every_monday_still_runs_on_a_monday(self) -> None:
        # The example names the day. A rescheduled audit does not make item 4
        # wrong, but it does make the sentence unverifiable -- so it has to be
        # rewritten with the workflow rather than left behind by it.
        self.assertRegex(
            AUDIT_WORKFLOW.read_text(),
            r"cron: '0 \d+ \* \* 1'",
            "maintenance-audit.yml no longer runs on Mondays, which is the day item 4's example "
            "names",
        )

    def test_the_formula_check_cannot_turn_the_audit_red(self) -> None:
        body = step_command(AUDIT_WORKFLOW, "Check the Homebrew formula pin")
        self.assertIn("homebrew_formula.py --check", body, "the step no longer runs the formula check")
        self.assertTrue(
            body.rstrip().endswith("|| true"),
            "the formula pin check can now fail the weekly audit; item 4's example is about what "
            f"a permanently red weekly job teaches everyone to do. Step: {body.strip()!r}",
        )

    def test_the_drift_issue_step_exits_zero_whatever_github_does(self) -> None:
        body = step_command(AUDIT_WORKFLOW, "Track snapshot drift as an issue")
        self.assertIn("snapshot_drift_issue.py", body, "the drift step no longer runs the script")
        # The step has no `|| true`, so the script itself is what keeps a
        # GitHub hiccup from recreating the red weekly audit of issue #129.
        self.assertNotIn("|| true", body)
        source = (ROOT / "snapshot_drift_issue.py").read_text()
        main_body = source.split("def main(", 1)[1]
        self.assertNotIn(
            "return 1",
            main_body,
            "snapshot_drift_issue.main can now exit non-zero, which takes the weekly audit red "
            "on a GitHub error -- exactly the failure mode issue #129 graduated away from",
        )


class EditorConfigTests(unittest.TestCase):
    """Item 4's fourth example, and .editorconfig's claim about itself.

    The header says every rule "describes what the files already do -- the
    values were measured, not chosen -- so applying it to an existing file is
    a no-op". That is a testable claim about the whole tracked tree, and it is
    the only thing standing between this file and an editor that reformats a
    file on open.
    """

    def test_it_declares_itself_the_root(self) -> None:
        self.assertEqual(
            editorconfig_preamble().get("root"),
            "true",
            "without `root = true` an .editorconfig further up the filesystem is merged in, so "
            "what applies here depends on where the checkout lives",
        )

    def test_every_section_matches_something_tracked(self) -> None:
        for glob, _ in editorconfig_sections():
            self.assertTrue(
                files_matching(glob),
                f".editorconfig's [{glob}] matches no tracked file: it is a dead rule, and a "
                "dead rule is indistinguishable from one that stopped matching after a rename",
            )

    def test_insert_final_newline_is_never_written_as_false(self) -> None:
        # Item 4: "`insert_final_newline = false` to mean 'leave this file
        # alone', when it means the opposite."
        for glob, rules in editorconfig_sections():
            self.assertNotEqual(
                rules.get("insert_final_newline"),
                "false",
                f"[{glob}] sets insert_final_newline = false, which tells an editor to STRIP the "
                "final newline rather than leave the file alone -- the mistake item 4 of "
                "docs/review-rubric.md names",
            )

    def test_the_snapshot_section_unsets_rather_than_negates(self) -> None:
        rules = resolved_rules("template_snapshots/containerfile/Justfile")
        for key in ("insert_final_newline", "trim_trailing_whitespace"):
            self.assertEqual(
                rules.get(key),
                "unset",
                f"{key} under template_snapshots/ is {rules.get(key)!r}; only `unset` disables "
                "the inherited rule without asserting its opposite",
            )

    def test_the_snapshot_newline_count_in_the_comment_is_the_measured_one(self) -> None:
        # The comment justifies `unset` with a count. If the snapshots are
        # refreshed and the count moves, the justification stops being
        # evidence -- and if it ever reaches 0 or the total, `unset` is no
        # longer the only correct value.
        match = re.search(r"strip the trailing newline from the (\d+)\s*\n?#?\s*snapshot files", EDITORCONFIG)
        self.assertIsNotNone(match, ".editorconfig no longer says how many snapshot files end with a newline")
        claimed = int(match.group(1))
        snapshots = [name for name in TRACKED if name.startswith("template_snapshots/")]
        with_newline = [name for name in snapshots if (ROOT / name).read_bytes().endswith(b"\n")]
        self.assertEqual(
            len(with_newline),
            claimed,
            f".editorconfig says {claimed} snapshot files end with a newline; {len(with_newline)} "
            f"of {len(snapshots)} do",
        )
        self.assertLess(
            len(with_newline),
            len(snapshots),
            "every snapshot file ends with a newline now, so the section's reasoning no longer "
            "describes the tree it protects",
        )

    def test_no_tracked_file_is_indented_with_tabs(self) -> None:
        for name in TRACKED:
            if resolved_rules(name).get("indent_style") != "space":
                continue
            text = self._decoded(name)
            if text is None:
                continue
            self.assertFalse(
                any(line.startswith("\t") for line in text.splitlines()),
                f"{name} has tab-indented lines but .editorconfig resolves indent_style = space "
                "for it, so an editor will rewrite it",
            )

    def test_an_indent_style_tab_section_names_a_file_that_is_tab_indented(self) -> None:
        for glob, rules in editorconfig_sections():
            if rules.get("indent_style") != "tab":
                continue
            matched = files_matching(glob)
            tabbed = [
                name
                for name in matched
                if (text := self._decoded(name)) and any(line.startswith("\t") for line in text.splitlines())
            ]
            self.assertTrue(
                tabbed,
                f".editorconfig's [{glob}] declares indent_style = tab, but none of {matched} is "
                "tab-indented. The rule does not describe the file: it converts it, which is the "
                "drift the section exists to prevent",
            )

    def test_every_file_uses_the_line_ending_and_charset_the_config_names(self) -> None:
        for name in TRACKED:
            rules = resolved_rules(name)
            raw = (ROOT / name).read_bytes()
            if not raw:
                continue
            if rules.get("charset") == "utf-8":
                try:
                    raw.decode()
                except UnicodeDecodeError:
                    self.fail(f"{name} is not valid UTF-8, which .editorconfig says every file here is")
            if rules.get("end_of_line") == "lf":
                self.assertNotIn(b"\r\n", raw, f"{name} has CRLF line endings; .editorconfig resolves lf for it")

    def test_every_file_ends_the_way_the_config_says(self) -> None:
        for name in TRACKED:
            if resolved_rules(name).get("insert_final_newline") != "true":
                continue
            raw = (ROOT / name).read_bytes()
            if not raw:
                continue
            self.assertTrue(
                raw.endswith(b"\n"),
                f"{name} has no final newline, so opening it in an editor honouring "
                ".editorconfig produces a diff -- the config stops being a no-op",
            )

    def test_trailing_whitespace_is_absent_wherever_it_would_be_trimmed(self) -> None:
        for name in TRACKED:
            if resolved_rules(name).get("trim_trailing_whitespace") != "true":
                continue
            text = self._decoded(name)
            if text is None:
                continue
            offenders = [n for n, line in enumerate(text.splitlines(), 1) if line != line.rstrip()]
            self.assertFalse(
                offenders,
                f"{name} has trailing whitespace on line(s) {offenders[:5]}, which an editor "
                "honouring .editorconfig would strip on save",
            )

    def test_markdown_keeps_its_hard_line_breaks(self) -> None:
        # Two trailing spaces are a hard line break in Markdown, so trimming
        # rewrites the text. The rule has to stay off for *.md whether or not
        # any file currently uses one.
        self.assertEqual(
            resolved_rules("docs/review-rubric.md").get("trim_trailing_whitespace"),
            "false",
            "Markdown is back under trim_trailing_whitespace = true, so a hard line break "
            "anywhere in the docs would be silently deleted on save",
        )

    def test_yaml_is_indented_the_way_the_config_says(self) -> None:
        for name in TRACKED:
            rules = resolved_rules(name)
            if not name.endswith((".yml", ".yaml")):
                continue
            size = int(rules["indent_size"])
            indents = yaml_key_indents((ROOT / name).read_text())
            if not indents:
                continue
            step = math.gcd(*indents) if len(indents) > 1 else indents[0]
            self.assertEqual(
                step % size,
                0,
                f"{name}'s mapping keys are indented in steps of {step}, not {size}; "
                ".editorconfig's YAML section claims to describe what the files already do",
            )

    def test_the_ruby_config_is_indented_the_way_the_section_says(self) -> None:
        section = dict(editorconfig_sections())[".simplecov"]
        target = ROOT / ".simplecov"
        self.assertTrue(target.is_file(), ".editorconfig carries a [.simplecov] section and the file is gone")
        indents = [
            len(line) - len(line.lstrip(" ")) for line in target.read_text().splitlines() if re.match(r"^ +\S", line)
        ]
        self.assertTrue(indents, ".simplecov has no indented line, so its indent_size rule asserts nothing")
        step = math.gcd(*indents) if len(indents) > 1 else indents[0]
        self.assertEqual(
            step,
            int(section["indent_size"]),
            f".simplecov is indented in steps of {step}, not {section['indent_size']}",
        )

    @staticmethod
    def _decoded(name: str) -> str | None:
        raw = (ROOT / name).read_bytes()
        try:
            return raw.decode()
        except UnicodeDecodeError:
            return None


class PathScopedJobTests(unittest.TestCase):
    """Item 5: after this change, does the job that exercises it still run?"""

    def test_the_container_job_is_triggered_by_the_suite_it_runs(self) -> None:
        body = step_run_body(CI, "Check for container-related changes")
        pathspec = re.search(r"git diff --quiet [^\n]*? -- ([^\n;]+)", body)
        self.assertIsNotNone(pathspec, "ci.yml's container path gate no longer diffs an explicit pathspec")
        paths = pathspec.group(1).split()
        self.assertIn(
            "tests/e2e/",
            paths,
            "the container job is path-scoped and tests/e2e/ is not a trigger, so a PR editing "
            "only the end-to-end suite skips the job that runs it -- item 5's example, exactly",
        )
        self.assertNotIn(
            "contrib/aib",
            paths,
            "contrib/aib is the host-side wrapper and is not in the image; adding it as a trigger "
            "builds the image for a change it cannot affect",
        )

    def test_the_gate_falls_back_to_building(self) -> None:
        body = step_run_body(CI, "Check for container-related changes")
        self.assertIn(
            "any_changed=true",
            body.split("git cat-file -e")[1].split("elif")[0],
            "an unresolvable diff base no longer forces a build, so the job silently skips "
            "instead of erring towards running",
        )

    def test_the_unit_job_is_not_path_scoped(self) -> None:
        # "while lint still passed": the observation only makes sense while
        # the test job runs on every change.
        ci_text = CI.read_text()
        trigger_block = ci_text.split("jobs:", 1)[0]
        self.assertNotIn(
            "paths:",
            trigger_block,
            "CI is now path-scoped at the workflow level, so the unit and lint gates can skip "
            "entirely -- item 5 assumes they always run",
        )


class VendoredSnapshotTests(unittest.TestCase):
    """Item 6: the snapshot is a pinned copy, refreshed as a unit."""

    def test_the_directory_the_item_names_exists_and_is_tracked(self) -> None:
        self.assertTrue(SNAPSHOT_DIR.is_dir(), "item 6 is about a directory that is no longer here")
        self.assertTrue(
            any(name.startswith("template_snapshots/") for name in TRACKED),
            "template_snapshots/ is untracked now, so 'a pinned copy of upstream' describes "
            "nothing a diff can touch",
        )

    def test_the_editor_config_agrees_that_nothing_should_tidy_it(self) -> None:
        # Item 6 and .editorconfig's snapshot section are the same rule stated
        # twice, in prose and in config. Make them fail together.
        self.assertIn(
            "template_snapshots/**",
            [glob for glob, _ in editorconfig_sections()],
            "the rubric says a diff that tidies template_snapshots/ is a defect, and "
            ".editorconfig no longer carries the section that keeps an editor from producing one",
        )

    def test_the_audit_is_what_refreshes_it_as_a_unit(self) -> None:
        self.assertIn(
            ".template-source",
            AUDIT_SCRIPT.read_text(),
            "nothing records where the snapshot came from, so 'refreshed as a unit' has no "
            "upstream to be refreshed from",
        )


class BlastRadiusTests(unittest.TestCase):
    """Item 7: state the blast radius, and the part a tool enforces."""

    def test_the_item_points_at_the_tier_document(self) -> None:
        body = section_body(7)
        targets = [t for _, t in MARKDOWN_LINK.findall(body)]
        self.assertIn(
            "risk-tiers.md",
            targets,
            "item 7 defers the classification to docs/risk-tiers.md by link; without it the tiers "
            "it names are undefined here",
        )

    def test_the_settings_file_the_item_credits_exists(self) -> None:
        self.assertTrue(
            SETTINGS_PATH.is_file(),
            "item 7 says .claude/settings.json holds the enforced part of the blast radius, and "
            "the file is gone",
        )

    def test_each_capability_the_item_names_is_gated_by_a_rule(self) -> None:
        permissions = json.loads(SETTINGS_PATH.read_text())["permissions"]
        gated = permissions.get("ask", []) + permissions.get("deny", [])
        # The three capabilities item 7 names, each against the rule that
        # actually stops or prompts on it.
        for capability, needle in (
            ("pushes to GitHub repositories", "Bash(git push:*)"),
            ("publishes images", "Bash(podman build:*)"),
            ("rotates a signing key", "Read(./cosign.key)"),
        ):
            self.assertIn(
                needle,
                gated,
                f"item 7 names {capability!r} as something this tool does, and no ask/deny rule "
                f"in .claude/settings.json covers it ({needle} is absent)",
            )

    def test_the_destructive_git_rules_are_denied_outright(self) -> None:
        denied = json.loads(SETTINGS_PATH.read_text())["permissions"]["deny"]
        for rule in ("Bash(git push --force:*)", "Bash(gh repo delete:*)"):
            self.assertIn(
                rule,
                denied,
                f"{rule} moved out of deny; item 7's 'the part of that a tool enforces' is only "
                "true while the irreversible ones cannot be prompted through",
            )


if __name__ == "__main__":
    unittest.main()
