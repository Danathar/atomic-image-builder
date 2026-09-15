"""Join docs/risk-tiers.md's claims to the tree it classifies.

The document is the repository's map of blast radius: it decides how much
evidence a change needs and what a reviewer looks at first, and
.github/workflows/ai-fix.yml points agents at it by name. Every path, table,
symbol and command in it is a hand copy of something elsewhere in the tree,
and nothing checked any of them. `.coveragerc` measures Python modules, so
Markdown can never lower a percentage, and the only test that opened this file
before this one is the repo-wide table aligner in
tests/test_format_markdown_tables.py, which checks column padding and never
reads a claim. A rename of `contrib/aib`, a patcher that stops matching
`patch_*_workflow`, a path filter added to ci.yml, or a snapshot directory
moved out from under Tier 3 all left the document silently wrong.

The literals are parsed out of the document rather than repeated here: a test
that restates them is a second copy to keep in step, and deleting the line it
copied would still pass. What is hard-coded is the *shape* of a claim -- the
classifier below and its case table -- plus the small set of literals the
document must still name, because an assertion computed only over what the
document happens to say is vacuous once the document says nothing.
"""

import ast
import fnmatch
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _workflow_steps import step_command, step_run_body  # noqa: E402

DOC = ROOT / "docs/risk-tiers.md"
CI = ROOT / ".github/workflows/ci.yml"
TOOL = ROOT / "atomic_image_builder.py"
AUDIT = ROOT / "maintenance_audit.py"
WORKFLOWS = ROOT / ".github/workflows"

BACKTICKED = re.compile(r"`([^`]+)`")
TIER_HEADING = re.compile(r"^## Tier (\d+) [-—]")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# Literals the document has to keep naming. Each is the anchor of an assertion
# below; without this set, deleting the sentence that carries one would turn
# the assertion into a no-op rather than a failure.
REQUIRED_MENTIONS = (
    "contrib/aib",
    "container/entrypoint.sh",
    "tests/e2e/smoke.sh",
    "tests/test_contrib_aib.sh",
    "tests/test_entrypoint.sh",
    "shellcheck -x",
    "template_snapshots/",
    "ACTION_PINS",
    "ACTION_REF_PINS",
    "atomic_image_builder.py",
    "maintenance_audit.py",
    "--skip-upstream",
    "homebrew_formula.py",
    ".github/workflows/publish-image.yml",
    ".github/workflows/publish-wrapper.yml",
    ".github/workflows/update-homebrew-formula.yml",
)


def doc_text() -> str:
    return DOC.read_text()


def tracked_paths() -> set[str]:
    """Every file git would ship, tracked or merely not ignored.

    `git ls-files` alone lists only the index, so a claim checked against it
    stays true when the file it names is deleted from the worktree but not yet
    staged -- and a new untracked file that a path claim now covers is
    invisible. Both are states a contributor is in while editing.
    """
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {name for name in out.split("\0") if name}


def tool_function_names() -> set[str]:
    """Every function and method defined in the tool, at any nesting depth."""
    tree = ast.parse(TOOL.read_text())
    return {
        node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def tool_module_names() -> set[str]:
    """Module-level assignment targets in the tool (the pin tables live here)."""
    names = set()
    for node in ast.parse(TOOL.read_text()).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def workflow_text() -> str:
    return "\n".join(path.read_text() for path in workflow_paths())


def workflow_paths() -> list[Path]:
    """Every GitHub Actions workflow, including either supported suffix."""
    return sorted(path for path in WORKFLOWS.iterdir() if path.suffix in {".yml", ".yaml"})


def workflow_trigger_names(path: Path) -> set[str]:
    """Top-level event names from a workflow's ``on`` declaration.

    CI deliberately installs no YAML dependency. This accepts the three forms
    Actions supports -- a scalar, a sequence, or a mapping -- and rejects an
    inline mapping rather than guessing at YAML syntax it does not parse.
    """
    lines = path.read_text().splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("on:"):
            continue
        inline = line.removeprefix("on:").split("#", 1)[0].strip()
        if inline:
            if inline.startswith("[") and inline.endswith("]"):
                return {
                    item.strip().strip("'\"")
                    for item in inline[1:-1].split(",")
                    if item.strip()
                }
            if inline.startswith("{"):
                raise AssertionError(f"unsupported inline `on` mapping in {path}")
            return {inline.strip("'\"")}

        triggers: set[str] = set()
        for child in lines[index + 1 :]:
            stripped = child.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(child) - len(child.lstrip())
            if indent == 0:
                break
            if indent != 2:
                continue
            if stripped.startswith("- "):
                triggers.add(stripped.removeprefix("- ").split("#", 1)[0].strip("'\" "))
                continue
            match = re.match(r"([A-Za-z_][A-Za-z0-9_-]*):", stripped)
            if match is None:
                raise AssertionError(f"unsupported `on` entry in {path}: {child!r}")
            triggers.add(match.group(1))
        return triggers
    raise AssertionError(f"{path} has no top-level `on` declaration")


def release_workflow_paths() -> set[str]:
    """Repository-relative paths of every workflow triggered by a release."""
    return {
        str(path.relative_to(ROOT))
        for path in workflow_paths()
        if "release" in workflow_trigger_names(path)
    }


def tier_sections() -> dict[int, list[str]]:
    """`## Tier N -- ...` headings mapped to the lines beneath them."""
    sections: dict[int, list[str]] = {}
    current: int | None = None
    for line in doc_text().splitlines():
        match = TIER_HEADING.match(line)
        if match:
            current = int(match.group(1))
            if current in sections:
                raise AssertionError(f"docs/risk-tiers.md declares Tier {current} twice")
            sections[current] = []
        elif line.startswith("## "):
            current = None
        elif current is not None:
            sections[current].append(line)
    return sections


def paths_paragraph(body: list[str]) -> str:
    """The `**Paths:**` paragraph of a tier section, joined into one string.

    Bounded by the blank line that ends the paragraph so a later paragraph's
    backticks cannot leak into the path claims: Tier 2's prose names
    `aib-tool`, which is a command and not a path.
    """
    collected: list[str] = []
    for line in body:
        if not collected and line.startswith("**Paths:**"):
            collected.append(line[len("**Paths:**") :])
        elif collected:
            if not line.strip():
                break
            collected.append(line)
    if not collected:
        raise AssertionError("a tier section has no **Paths:** paragraph")
    return " ".join(part.strip() for part in collected)


def literals(text: str) -> list[str]:
    return BACKTICKED.findall(text)


def classify(literal: str) -> str:
    """The kind of thing a path claim names, or ValueError.

    Ordered, and deliberately total: an unrecognised shape raises rather than
    being skipped, so a literal added to the document in a form this test
    cannot check fails here instead of passing unexamined.
    """
    if "*" in literal:
        return "tracked-glob" if "/" in literal or "." in literal else "function-glob"
    if literal.endswith("/"):
        return "tracked-dir"
    # Path-shaped before tracked: a literal that looks like a path but names
    # nothing has to reach resolve(), which reports the rot, rather than
    # falling through to the unclassifiable branch and blaming its shape.
    if "/" in literal or "." in literal:
        return "tracked-file"
    if literal in tracked_paths():
        return "tracked-file"
    if literal.isidentifier() and literal.isupper():
        return "python-name"
    raise ValueError(f"unclassifiable path literal in docs/risk-tiers.md: {literal!r}")


def matches_tracked_glob(literal: str, paths: set[str]) -> set[str]:
    """Tracked paths a documented glob covers, by full path or by basename.

    `*.md` is written the way a reader reads it -- "any Markdown file" -- not
    as a repository-root pathspec, so a bare pattern is matched against the
    basename too.
    """
    return {
        path
        for path in paths
        if fnmatch.fnmatch(path, literal) or fnmatch.fnmatch(path.rsplit("/", 1)[-1], literal)
    }


def resolve(literal: str, kind: str) -> set[str]:
    """What a path claim actually covers in the tree. Empty means it rotted."""
    paths = tracked_paths()
    if kind == "tracked-glob":
        return matches_tracked_glob(literal, paths)
    if kind == "function-glob":
        return {name for name in tool_function_names() if fnmatch.fnmatch(name, literal)}
    if kind == "tracked-dir":
        return {path for path in paths if path.startswith(literal)}
    if kind == "tracked-file":
        return {literal} & paths
    if kind == "python-name":
        return {literal} if literal in tool_module_names() or literal in workflow_text() else set()
    raise AssertionError(f"unhandled kind {kind!r}")


def quick_classification_rows() -> list[tuple[str, int]]:
    """The (description, tier) pairs of the summary table at the end."""
    rows: list[tuple[str, int]] = []
    in_table = False
    for line in doc_text().splitlines():
        if line.startswith("## Quick classification"):
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2 or set(cells[1]) <= set("- :"):
            continue
        if not cells[1].isdigit():
            continue
        rows.append((cells[0], int(cells[1])))
    return rows


def shellcheck_arguments() -> list[str]:
    """The argv of ci.yml's shellcheck step, continuations joined."""
    body = step_command(CI, "Run shellcheck")
    return shlex.split(body.replace("\\\n", " "))


def container_build_pathspec() -> list[str]:
    """The pathspec that decides whether the container image is rebuilt."""
    body = step_run_body(CI, "Check for container-related changes")
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("elif git diff --quiet") and " -- " in stripped:
            return stripped.split(" -- ", 1)[1].split(";")[0].split()
    raise AssertionError("ci.yml's container-change gate no longer runs `git diff --quiet ... -- <paths>`")


def help_text(script: str) -> str:
    result = subprocess.run(
        [sys.executable, script, "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


class ClassifierTests(unittest.TestCase):
    """The classifier is the join; untested, every assertion built on it is
    only as good as a shape it silently mis-sorted."""

    def test_a_glob_with_a_path_shape_is_a_file_glob(self) -> None:
        self.assertEqual(classify("*.md"), "tracked-glob")
        self.assertEqual(classify("docs/*.md"), "tracked-glob")

    def test_a_glob_without_a_path_shape_is_a_function_glob(self) -> None:
        self.assertEqual(classify("patch_*_workflow"), "function-glob")
        self.assertEqual(classify("generate_*_workflow"), "function-glob")

    def test_a_trailing_slash_is_a_directory(self) -> None:
        self.assertEqual(classify("docs/"), "tracked-dir")

    def test_a_tracked_path_is_a_file(self) -> None:
        self.assertEqual(classify("Containerfile"), "tracked-file")
        self.assertEqual(classify(".editorconfig"), "tracked-file")

    def test_a_path_shaped_literal_is_a_file_even_when_it_names_nothing(self) -> None:
        self.assertEqual(classify("contrib/aib-wrapper"), "tracked-file")
        self.assertEqual(resolve("contrib/aib-wrapper", "tracked-file"), set())

    def test_a_shouting_identifier_is_a_python_or_workflow_name(self) -> None:
        self.assertEqual(classify("ACTION_PINS"), "python-name")
        self.assertEqual(classify("GH_TOKEN"), "python-name")

    def test_an_unrecognised_shape_raises_rather_than_passing(self) -> None:
        with self.assertRaises(ValueError):
            classify("some prose the document started backticking")
        # A bare command name: Tier 2's prose carries one, and it is not a
        # path claim even though it is backticked like one.
        with self.assertRaises(ValueError):
            classify("aib-tool")

    def test_a_directory_claim_needs_a_file_under_it(self) -> None:
        self.assertEqual(resolve("template_snapshots/", "tracked-dir") and True, True)
        self.assertEqual(resolve("no_such_directory/", "tracked-dir"), set())

    def test_a_basename_glob_matches_nested_files(self) -> None:
        self.assertIn("docs/risk-tiers.md", matches_tracked_glob("*.md", tracked_paths()))


class TierStructureTests(unittest.TestCase):
    def test_tiers_are_numbered_from_one_without_a_gap(self) -> None:
        tiers = sorted(tier_sections())
        self.assertTrue(tiers, "docs/risk-tiers.md declares no tiers")
        self.assertEqual(tiers, list(range(1, len(tiers) + 1)))

    def test_the_quick_classification_table_covers_every_tier_exactly_once(self) -> None:
        rows = quick_classification_rows()
        self.assertTrue(rows, "the Quick classification table parsed as empty")
        self.assertEqual(sorted(tier for _, tier in rows), sorted(tier_sections()))

    def test_every_tier_states_its_paths_and_its_evidence(self) -> None:
        for tier, body in sorted(tier_sections().items()):
            with self.subTest(tier=tier):
                self.assertTrue(paths_paragraph(body).strip())
                self.assertTrue(
                    any(line.startswith("**Evidence:**") for line in body),
                    f"Tier {tier} says how far a change reaches but not what proves it",
                )


class PathClaimTests(unittest.TestCase):
    def test_every_path_literal_in_every_tier_resolves(self) -> None:
        seen = 0
        for tier, body in sorted(tier_sections().items()):
            for literal in literals(paths_paragraph(body)):
                with self.subTest(tier=tier, literal=literal):
                    kind = classify(literal)
                    self.assertTrue(
                        resolve(literal, kind),
                        f"Tier {tier} claims {literal!r} ({kind}), which matches nothing in the tree",
                    )
                    seen += 1
        self.assertGreaterEqual(seen, len(REQUIRED_MENTIONS) // 2, "the path paragraphs parsed as near-empty")

    def test_the_document_classifies_its_own_directory_as_tier_one(self) -> None:
        # Tier 1 is "this repository only"; the document is in it, and saying
        # so is what makes `docs/` load-bearing rather than illustrative.
        covered: set[str] = set()
        for literal in literals(paths_paragraph(tier_sections()[1])):
            covered |= resolve(literal, classify(literal))
        self.assertIn(str(DOC.relative_to(ROOT)), covered)

    def test_tier_three_names_the_writers_that_produce_other_peoples_ci(self) -> None:
        globs = [
            literal
            for literal in literals(paths_paragraph(tier_sections()[3]))
            if classify(literal) == "function-glob"
        ]
        self.assertTrue(globs, "Tier 3 no longer names the generated-output writers by pattern")
        for glob in globs:
            with self.subTest(glob=glob):
                self.assertTrue(
                    resolve(glob, "function-glob"),
                    f"no function in atomic_image_builder.py matches {glob!r} any more",
                )

    def test_the_load_bearing_literals_are_still_named(self) -> None:
        text = doc_text()
        for literal in REQUIRED_MENTIONS:
            with self.subTest(literal=literal):
                self.assertIn(literal, text)

    def test_every_relative_link_resolves(self) -> None:
        for target in MARKDOWN_LINK.findall(doc_text()):
            if target.startswith(("http://", "https://", "#")):
                continue
            with self.subTest(target=target):
                self.assertTrue((DOC.parent / target.split("#", 1)[0]).exists())


class TierOneEvidenceTests(unittest.TestCase):
    def test_a_documentation_change_still_runs_the_unit_suite(self) -> None:
        # Tier 1: "A documentation change still runs it, because several tests
        # read the documents and fail when one drifts." That is only true while
        # CI has no path filter -- a `paths-ignore: ['**.md']` would make the
        # sentence false without touching this document.
        lines = CI.read_text().splitlines()
        start = lines.index("on:")
        for line in lines[start + 1 :]:
            if line and not line.startswith((" ", "\t", "#")):
                break
            self.assertFalse(
                line.strip().startswith(("paths:", "paths-ignore:")),
                "ci.yml filters by path, so a docs-only change no longer runs the unit suite",
            )

    def test_the_end_to_end_suites_trigger_the_container_build(self) -> None:
        # Tier 1's note: tests/e2e/ is Tier 1 by content but triggers the
        # build, "since a change to a suite that never runs the suite reads as
        # covered".
        self.assertIn("tests/e2e/", container_build_pathspec())


class TierTwoEvidenceTests(unittest.TestCase):
    def test_the_wrapper_is_not_in_the_image_the_smoke_test_proves(self) -> None:
        # Tier 2's stated exception: contrib/aib "is **not** baked into the
        # image, so `smoke.sh` never touches it and a wrapper change can look
        # verified while nothing has run it".
        containerfile = (ROOT / "Containerfile").read_text()
        copied = [line for line in containerfile.splitlines() if line.startswith(("COPY ", "ADD "))]
        self.assertTrue(copied, "the Containerfile COPYs nothing; this assertion proves nothing")
        self.assertFalse([line for line in copied if "contrib/aib" in line])
        self.assertNotIn("contrib/aib", (ROOT / "tests/e2e/smoke.sh").read_text())
        self.assertNotIn("contrib/aib", container_build_pathspec())

    def test_shellcheck_covers_every_shell_file_the_tier_names(self) -> None:
        # "with `shellcheck -x` for the static half" -- so the files this tier
        # names have to be the files the gate actually passes to shellcheck.
        body = paths_paragraph(tier_sections()[2]) + " " + " ".join(tier_sections()[2])
        # Path-shaped only: the tier also refers to `smoke.sh` by bare name
        # once it has introduced it, and a bare name is not something a lint
        # command can be asked to contain.
        named = {
            literal
            for literal in literals(body)
            if "/" in literal and (literal.endswith(".sh") or literal == "contrib/aib")
        }
        self.assertIn("contrib/aib", named, "Tier 2 no longer names the wrapper it calls the exception")
        arguments = shellcheck_arguments()
        self.assertEqual(arguments[0], "shellcheck")
        self.assertIn("-x", arguments, "the document promises `shellcheck -x`, and ci.yml no longer passes -x")
        for path in sorted(named):
            with self.subTest(path=path):
                self.assertIn(path, arguments)

    def test_the_behavioural_shell_suites_the_tier_names_exist(self) -> None:
        for name in ("tests/test_contrib_aib.sh", "tests/test_entrypoint.sh", "tests/e2e/smoke.sh"):
            with self.subTest(name=name):
                self.assertIn(name, doc_text())
                self.assertIn(name, tracked_paths())


class TierThreeEvidenceTests(unittest.TestCase):
    def test_the_audit_has_the_flag_the_tier_warns_against(self) -> None:
        # "`python3 maintenance_audit.py` (not `--skip-upstream`, which skips
        # the drift checks that matter here)".
        self.assertIn("--skip-upstream", help_text("maintenance_audit.py"))

    def test_the_audit_reads_the_pin_tables(self) -> None:
        # "it checks source metadata and that the pin tables agree with the
        # snapshot workflows".
        source = AUDIT.read_text()
        for table in ("ACTION_PINS", "ACTION_REF_PINS"):
            with self.subTest(table=table):
                self.assertIn(table, source)

    def test_the_audit_never_runs_the_patchers(self) -> None:
        # The tier's whole argument for demanding the patcher tests on top of
        # a green audit: "It never runs the patchers ... so a silently broken
        # patcher passes a green audit."
        patchers = {name for name in tool_function_names() if name.startswith("patch_")}
        self.assertTrue(patchers, "atomic_image_builder.py defines no patchers; Tier 3 describes nothing")
        referenced = set()
        for node in ast.walk(ast.parse(AUDIT.read_text())):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                referenced.update(alias.name for alias in node.names)
        self.assertEqual(
            patchers & referenced,
            set(),
            "maintenance_audit.py now calls a patcher, so Tier 3's reason for demanding the patcher tests is stale",
        )


class TierFourEvidenceTests(unittest.TestCase):
    def test_every_release_workflow_is_named_in_tier_four(self) -> None:
        named = set(literals(paths_paragraph(tier_sections()[4])))
        release_workflows = release_workflow_paths()
        self.assertTrue(release_workflows, "the repository has no release-triggered workflows")
        self.assertEqual(
            release_workflows - named,
            set(),
            "release-triggered workflows missing from Tier 4's **Paths:** paragraph",
        )

    def test_each_release_workflow_holds_a_write_permission(self) -> None:
        # The tier is "credentials and the release path". A workflow here that
        # had dropped to read-only would no longer be what the tier describes.
        for name in sorted(release_workflow_paths()):
            with self.subTest(name=name):
                text = (ROOT / name).read_text()
                self.assertTrue(
                    re.search(r"^\s+\w[\w-]*: write$", text, re.MULTILINE),
                    f"{name} declares no write permission any more",
                )

    def test_the_formula_updater_has_the_update_path_and_the_workflow_runs_it(self) -> None:
        # "`homebrew_formula.py` is here because it is what that workflow
        # runs: its `--update` path downloads the release tarball, computes the
        # digest, and rewrites the formula."
        self.assertIn("--update", help_text("homebrew_formula.py"))
        workflow = (ROOT / ".github/workflows/update-homebrew-formula.yml").read_text()
        self.assertIn("homebrew_formula.py --update", workflow)

    def test_the_formula_the_tier_protects_is_tracked(self) -> None:
        formulae = {path for path in tracked_paths() if path.startswith("Formula/")}
        self.assertTrue(formulae, "Tier 4 protects Formula/, which holds nothing")


if __name__ == "__main__":
    unittest.main()
