"""Join ARCHITECTURE.md's claims to the tree it maps.

The document is the orientation map a contributor reads before touching
anything, and every line of it is a hand copy of something else: a path, a
module-level symbol, an `App` method name, a region's position in
`atomic_image_builder.py`, a workflow step. Nothing opened it. `.coveragerc`
measures Python modules, so Markdown moves no percentage, and the only test
that read this file before this one is the repo-wide table aligner in
tests/test_format_markdown_tables.py, which checks column padding and never
reads a claim. A renamed method, a new root module, a region moved during a
refactor, or a patcher promoted onto `App` all left the map silently wrong --
and a wrong map is worse than none, because it is believed.

Two claims were already false when this file was written and are fixed in the
same change:

* the Repository map listed every tracked root module except
  `format_markdown_tables.py`, which has been tracked, measured by
  `.coveragerc` and gated by its own test suite the whole time;
* the "Workflow patchers (module level)" row said the `workflow_key` /
  `workflow_block_key` classifiers sit under `patch_workflow_steps()` and that
  `patch_workflow_signing_steps()` comes after them. It does not -- it is
  defined between `patch_workflow_steps()` and `workflow_key` -- and the row
  never named `patch_signing_step_block()`, the first function in the region.

The literals are parsed out of the document rather than restated here: a test
that repeats them is a second copy to keep in step, and deleting the line it
copied would still pass. What is hard-coded is the *shape* of a claim, plus
the small set of names the document must still contain, because an assertion
computed only over what the document happens to say is vacuous the moment the
document says nothing.
"""

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import atomic_image_builder as aib  # noqa: E402
import maintenance_audit  # noqa: E402
from _workflow_steps import step_command, step_run_body  # noqa: E402
from format_markdown_tables import is_delimiter, split_row  # noqa: E402

DOC_PATH = ROOT / "ARCHITECTURE.md"
DOC = DOC_PATH.read_text()
TOOL_PATH = ROOT / "atomic_image_builder.py"
AUDIT_WORKFLOW = ROOT / ".github/workflows/maintenance-audit.yml"

BACKTICKED = re.compile(r"`([^`]+)`")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\(\))?$")

# Backticked lowercase words in the document that name something outside this
# repository's own symbol tables: a CLI, a stdlib module, a YAML key. Each is
# asserted below to resolve to no symbol, so an entry that becomes a real name
# fails here instead of quietly exempting itself from the resolution test.
NOT_OUR_SYMBOLS = frozenset({"gum", "unittest"})

# Names the document has to keep saying. Each anchors an assertion further
# down; without this set, deleting the row or sentence that carries one turns
# that assertion into a no-op rather than a failure.
REQUIRED_MENTIONS = (
    "atomic_image_builder.py",
    "maintenance_audit.py",
    "homebrew_formula.py",
    "template_snapshots/",
    "contrib/aib",
    "container/entrypoint.sh",
    "tests/",
    ".atomic-image-builder.json",
    "/etc/os-release",
    "FEDORA_ATOMIC_FALLBACK_TAG",
    "determine_fedora_atomic_default_tag()",
    "patch_cosign_compatibility()",
    "ACTION_PINS",
    "ACTION_REF_PINS",
    "CommandError",
)


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


def reachable_from_main(module_path: Path) -> set[str]:
    """Module-level function names a run of `main()` can reach.

    A duty the document credits to a script is done by whatever `main()` ends
    up calling, not necessarily by a name written in `main()` itself -- the
    audit reaches most of its checks through `run_audit`. Walking the call
    graph means a check moved behind another helper still counts, and a check
    detached from `main` entirely does not.
    """
    tree = ast.parse(module_path.read_text())
    calls: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            calls[node.name] = {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
    seen: set[str] = set()
    pending = ["main"]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(calls.get(name, ()))
    return seen


def tables() -> dict[str, tuple[list[str], list[list[str]]]]:
    """Every Markdown table in the document, keyed by the heading above it.

    Parsed with the repository's own row splitter rather than a second
    hand-rolled one, so a table this document writes in a shape the aligner
    does not recognise is a parse failure here too.
    """
    found: dict[str, tuple[list[str], list[list[str]]]] = {}
    heading = ""
    header: list[str] | None = None
    rows: list[list[str]] = []
    for line in DOC.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            header, rows = None, []
            continue
        cells = split_row(line)
        if cells is None:
            if header is not None:
                found[heading] = (header, rows)
            header, rows = None, []
            continue
        cells = [cell.strip() for cell in cells]
        if header is None:
            header = cells
        elif is_delimiter(cells):
            continue
        else:
            rows.append(cells)
    if header is not None:
        found[heading] = (header, rows)
    return found


TABLES = tables()

REPO_MAP = "Repository map"
REGIONS = "Inside `atomic_image_builder.py`"
APP_TABLE = "The `App` class"
WHERE_TO_START = "Where to start"


def module_symbols() -> dict[str, int]:
    """Every module-level name in the tool, mapped to the line defining it."""
    tree = ast.parse(TOOL_PATH.read_text())
    lines: dict[str, int] = {}
    for node in tree.body:
        name = getattr(node, "name", None)
        if name is not None:
            lines.setdefault(name, node.lineno)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    lines.setdefault(target.id, node.lineno)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            lines.setdefault(node.target.id, node.lineno)
    return lines


def class_methods(class_name: str) -> dict[str, int]:
    tree = ast.parse(TOOL_PATH.read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.name: child.lineno
                for child in node.body
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            }
    raise AssertionError(f"no class {class_name} in {TOOL_PATH}")


MODULE_SYMBOLS = module_symbols()
APP_METHODS = class_methods("App")
GUM_METHODS = class_methods("Gum")


def named_symbols(text: str) -> list[str]:
    """Backticked identifiers in `text`, in the order the text names them."""
    return [name for name in BACKTICKED.findall(text) if IDENTIFIER.match(name)]


def resolve(name: str) -> int | None:
    """The line defining `name`, looking in module, `App`, then `Gum` scope."""
    bare = name.removesuffix("()")
    for table in (MODULE_SYMBOLS, APP_METHODS, GUM_METHODS):
        if bare in table:
            return table[bare]
    return None


class DocumentIsStillMakingItsClaims(unittest.TestCase):
    """The anchors every other test in this file depends on."""

    def test_every_required_name_is_still_written(self) -> None:
        for name in REQUIRED_MENTIONS:
            with self.subTest(name=name):
                self.assertIn(name, DOC)

    def test_the_four_tables_are_present(self) -> None:
        for heading in (REPO_MAP, REGIONS, APP_TABLE, WHERE_TO_START):
            with self.subTest(heading=heading):
                self.assertIn(heading, TABLES)
                self.assertTrue(TABLES[heading][1], f"{heading} table has no rows")

    def test_the_exempt_words_name_nothing_in_the_tool(self) -> None:
        # If one of these becomes a real symbol the exemption is hiding a
        # claim the resolution test should be making.
        for word in sorted(NOT_OUR_SYMBOLS):
            with self.subTest(word=word):
                self.assertIsNone(resolve(word))


class RepositoryMap(unittest.TestCase):
    """"An orientation map" -- every path in it, and every path it owes."""

    def paths(self) -> list[str]:
        rows = TABLES[REPO_MAP][1]
        out = []
        for row in rows:
            found = BACKTICKED.findall(row[0])
            self.assertEqual(len(found), 1, f"map row does not name one path: {row[0]!r}")
            out.append(found[0])
        return out

    def test_every_mapped_path_is_tracked(self) -> None:
        tracked = tracked_files()
        for path in self.paths():
            with self.subTest(path=path):
                if path.endswith("/"):
                    self.assertTrue(
                        any(f.startswith(path) for f in tracked),
                        f"{path} holds no tracked file",
                    )
                else:
                    self.assertIn(path, tracked)

    def test_every_tracked_root_module_is_mapped(self) -> None:
        # The map was missing format_markdown_tables.py. Computing the
        # obligation from `git ls-files` rather than from a list here is what
        # makes the next root module fail instead of going unmentioned.
        mapped = set(self.paths())
        root_modules = {f for f in tracked_files() if f.endswith(".py") and "/" not in f}
        self.assertTrue(root_modules, "no tracked root modules found")
        self.assertEqual(root_modules - mapped, set())

    def test_the_tool_is_described_as_one_stdlib_only_module(self) -> None:
        description = next(
            row[1] for row in TABLES[REPO_MAP][1] if "atomic_image_builder.py" in row[0]
        )
        self.assertIn("standard library only", description)
        tree = ast.parse(TOOL_PATH.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(imported)
        self.assertEqual(imported - set(sys.stdlib_module_names) - {"__future__"}, set())

    def test_each_snapshot_carries_the_template_source_the_map_promises(self) -> None:
        description = next(
            row[1] for row in TABLES[REPO_MAP][1] if "template_snapshots/" in row[0]
        )
        self.assertIn(".template-source", description)
        upstreams = re.findall(r"`([a-z-]+/[a-z-]+)`", description)
        self.assertTrue(upstreams, "the map names no upstream template repos")
        snapshots = sorted(p for p in (ROOT / "template_snapshots").iterdir() if p.is_dir())
        self.assertEqual(len(snapshots), len(upstreams))
        recorded = []
        for snapshot in snapshots:
            source = snapshot / ".template-source"
            self.assertTrue(source.is_file(), f"{snapshot.name} has no .template-source")
            fields = dict(
                line.split("=", 1) for line in source.read_text().splitlines() if "=" in line
            )
            self.assertRegex(fields.get("revision", ""), r"^[0-9a-f]{40}$")
            recorded.append(fields["repo"])
        for upstream in upstreams:
            with self.subTest(upstream=upstream):
                self.assertTrue(
                    any(upstream in repo for repo in recorded),
                    f"no snapshot records {upstream}; .template-source has {recorded}",
                )

    def test_the_audit_leaves_the_homebrew_formula_to_a_separate_step(self) -> None:
        description = next(
            row[1] for row in TABLES[REPO_MAP][1] if "maintenance_audit.py" in row[0]
        )
        self.assertIn("does not touch the Homebrew formula", description)
        self.assertIn("maintenance-audit.yml", description)
        audit_source = (ROOT / "maintenance_audit.py").read_text()
        self.assertNotIn("homebrew_formula", audit_source)
        self.assertNotIn("Formula/", audit_source)
        # ... and the workflow really does check it, in a step of its own.
        audit_step = step_run_body(AUDIT_WORKFLOW, "Run maintenance audit")
        formula_step = step_command(AUDIT_WORKFLOW, "Check the Homebrew formula pin")
        self.assertIn("maintenance_audit.py", audit_step)
        self.assertNotIn("homebrew_formula.py", audit_step)
        self.assertIn("homebrew_formula.py --check", formula_step)

    def test_the_audits_three_duties_each_have_a_function_main_calls(self) -> None:
        description = next(
            row[1] for row in TABLES[REPO_MAP][1] if "maintenance_audit.py" in row[0]
        )
        duties = {
            "snapshot drift": "audit_upstream_drift",
            "action pin coverage": "audit_pin_table_shapes",
            "freshness": "audit_action_pin_freshness",
        }
        reachable = reachable_from_main(ROOT / "maintenance_audit.py")
        for duty, function in duties.items():
            with self.subTest(duty=duty):
                self.assertIn(duty, description)
                self.assertTrue(hasattr(maintenance_audit, function))
                self.assertIn(function, reachable)

    def test_the_shell_entrypoints_have_the_harnesses_the_map_promises(self) -> None:
        description = next(row[1] for row in TABLES[REPO_MAP][1] if row[0] == "`tests/`")
        self.assertIn(".sh` harnesses", description)
        tracked = tracked_files()
        harnesses = [f for f in tracked if f.startswith("tests/") and f.endswith(".sh")]
        for entrypoint in ("contrib/aib", "container/entrypoint.sh"):
            with self.subTest(entrypoint=entrypoint):
                name = Path(entrypoint).name
                self.assertTrue(
                    any(name in (ROOT / h).read_text() for h in harnesses),
                    f"no tests/*.sh harness exercises {entrypoint}",
                )

    def test_the_badge_helper_writes_both_outputs_it_claims(self) -> None:
        description = next(
            row[1] for row in TABLES[REPO_MAP][1] if "coverage_badge.py" in row[0]
        )
        self.assertIn("badge", description)
        self.assertIn("CSV", description)
        badge_main = (ROOT / "coverage_badge.py").read_text().split("def main(", 1)[1]
        self.assertIn("badge_payload", badge_main)
        self.assertIn("trend_row", badge_main)


class Links(unittest.TestCase):
    def test_every_relative_link_resolves_to_a_tracked_file(self) -> None:
        tracked = set(tracked_files())
        targets = [t for t in MARKDOWN_LINK.findall(DOC) if not t.startswith(("http", "#"))]
        self.assertTrue(targets, "the document links to nothing")
        for target in targets:
            with self.subTest(target=target):
                self.assertIn(target.split("#", 1)[0], tracked)


class RuntimeModel(unittest.TestCase):
    def test_the_state_file_is_named_as_the_tool_spells_it(self) -> None:
        self.assertIn(f"`{aib.STATE_FILE}`", DOC)
        self.assertEqual(aib.STATE_FILE, f".{aib.TOOL_SLUG}.json")

    def test_the_stages_are_numbered_contiguously(self) -> None:
        stated = re.search(r"serves one of (\w+) stages", DOC)
        self.assertIsNotNone(stated)
        numbers = [int(n) for n in re.findall(r"^(\d+)\. ", DOC, flags=re.MULTILINE)]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        words = {"three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
        self.assertEqual(len(numbers), words[stated.group(1)])


class RegionTable(unittest.TestCase):
    """"Regions, in file order" -- and what each row says it contains."""

    def region_symbols(self) -> list[tuple[str, list[str]]]:
        out = []
        for region, contents in TABLES[REGIONS][1]:
            names = named_symbols(contents)
            if not names:
                # A row like `App` carries its symbol in the Region column.
                names = named_symbols(region)
            out.append((region, names))
        return out

    def test_every_named_symbol_resolves(self) -> None:
        # "Every name below is greppable" is the document's own promise.
        for region, names in self.region_symbols():
            for name in names:
                if name in NOT_OUR_SYMBOLS:
                    continue
                with self.subTest(region=region, name=name):
                    self.assertIsNotNone(resolve(name), f"{name} is defined nowhere")

    def test_the_regions_appear_in_file_order(self) -> None:
        starts = []
        for region, names in self.region_symbols():
            resolved = [resolve(n) for n in names if n not in NOT_OUR_SYMBOLS]
            resolved = [line for line in resolved if line is not None]
            self.assertTrue(resolved, f"region {region!r} names no resolvable symbol")
            starts.append((region, min(resolved)))
        self.assertEqual(
            [r for r, _ in starts],
            [r for r, _ in sorted(starts, key=lambda pair: pair[1])],
            f"table order does not match file order: {starts}",
        )

    def test_a_row_that_says_then_lists_its_symbols_in_file_order(self) -> None:
        # Only a row that sequences its contents claims an order. This is the
        # claim the "Workflow patchers (module level)" row was getting wrong:
        # patch_workflow_signing_steps() is defined before the workflow_key /
        # workflow_block_key classifiers, not after them.
        sequenced = [
            (region, names)
            for region, names in self.region_symbols()
            if re.search(r"\bthen\b", dict(TABLES[REGIONS][1])[region])
        ]
        self.assertTrue(sequenced, "no region row sequences its contents any more")
        for region, names in sequenced:
            with self.subTest(region=region):
                lines = [resolve(n) for n in names if n not in NOT_OUR_SYMBOLS]
                self.assertNotIn(None, lines)
                self.assertEqual(lines, sorted(lines), f"{names} are not in file order")

    def test_the_module_level_row_names_only_module_level_functions(self) -> None:
        row = next(
            contents
            for region, contents in TABLES[REGIONS][1]
            if "module level" in region
        )
        names = [n.removesuffix("()") for n in named_symbols(row)]
        self.assertTrue(names)
        for name in names:
            with self.subTest(name=name):
                self.assertIn(name, MODULE_SYMBOLS)
                # The distinction the row's own title makes: a patcher moved
                # onto App would still resolve, and would still be wrong here.
                self.assertNotIn(name, APP_METHODS)

    def test_every_module_level_patcher_in_that_region_is_named(self) -> None:
        row = next(
            contents
            for region, contents in TABLES[REGIONS][1]
            if "module level" in region
        )
        named = {n.removesuffix("()") for n in named_symbols(row)}
        bounds = sorted(
            line
            for region, contents in TABLES[REGIONS][1]
            for line in [min(
                (resolve(n) for n in (named_symbols(contents) or named_symbols(region))
                 if n not in NOT_OUR_SYMBOLS and resolve(n) is not None),
                default=None,
            )]
            if line is not None
        )
        start = min(resolve(n) for n in named)
        following = [line for line in bounds if line > max(resolve(n) for n in named)]
        end = following[0] if following else float("inf")
        in_region = {
            name
            for name, line in MODULE_SYMBOLS.items()
            if start <= line < end and name.startswith(("patch_", "ensure_workflow_"))
        }
        self.assertEqual(in_region - named, set())

    def test_the_gum_row_names_gum_methods(self) -> None:
        row = next(contents for region, contents in TABLES[REGIONS][1] if region == "`Gum`")
        for name in named_symbols(row):
            if name in NOT_OUR_SYMBOLS:
                continue
            with self.subTest(name=name):
                self.assertIn(name.removesuffix("()"), GUM_METHODS)
        # `spinner*` is a glob, and a glob standing for one method is a lie.
        globs = [g for g in BACKTICKED.findall(row) if "*" in g]
        self.assertTrue(globs)
        for glob in globs:
            pattern = re.compile(glob.replace("*", "[A-Za-z0-9_]*") + "$")
            with self.subTest(glob=glob):
                self.assertGreaterEqual(len(list(filter(pattern.match, GUM_METHODS))), 2)

    def test_every_prompt_goes_through_gum(self) -> None:
        row = next(contents for region, contents in TABLES[REGIONS][1] if region == "`Gum`")
        self.assertIn("Every prompt in the tool goes through here", row)
        tree = ast.parse(TOOL_PATH.read_text())
        direct = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "input"
        ]
        self.assertEqual(direct, [])
        # The one thing the tool may do with stdin is ask whether it is a
        # terminal: Gum.terminal_available() needs that to tell gum's "no TTY"
        # exit 1 from Esc. Reading from it is still a prompt that bypasses gum.
        self.assertNotRegex(TOOL_PATH.read_text(), r"sys\.stdin(?!\.isatty\(\))")


class AppTable(unittest.TestCase):
    def test_the_column_still_says_representative(self) -> None:
        # Every other table here is checked for completeness. This one is
        # explicitly a sample, and the header is what says so -- drop the word
        # and the table starts claiming to be the whole list.
        self.assertIn("Representative", TABLES[APP_TABLE][0][1])

    def test_every_listed_method_is_an_app_method(self) -> None:
        for group, methods in TABLES[APP_TABLE][1]:
            names = named_symbols(methods)
            self.assertTrue(names, f"group {group!r} lists no method")
            for name in names:
                with self.subTest(group=group, method=name):
                    self.assertIn(name.removesuffix("()"), APP_METHODS)

    def test_the_stated_method_count_still_matches(self) -> None:
        stated = re.search(r"~(\d+) methods", DOC)
        self.assertIsNotNone(stated)
        approximate = int(stated.group(1))
        # "~" buys a tenth either way; past that the number is just wrong.
        self.assertLessEqual(abs(len(APP_METHODS) - approximate), approximate // 10)

    def test_every_generator_and_project_writer_is_placed_somewhere(self) -> None:
        # The two groups the document singles out as "the ones to be careful
        # with" are the ones a reader will look for by name.
        listed = {
            name.removesuffix("()")
            for _, methods in TABLES[APP_TABLE][1]
            for name in named_symbols(methods)
        }
        owed = {
            name
            for name in APP_METHODS
            if name.startswith("generate_") or name.startswith("write_") and "project_files" in name
        }
        self.assertTrue(owed)
        self.assertEqual(owed - listed, set())


class PatcherBehaviour(unittest.TestCase):
    """The claim that a patcher no-ops, and the one exception to it."""

    UNRELATED = "name: build\non:\n  push:\njobs:\n  build:\n    steps:\n      - run: echo hi"

    def test_a_signing_patcher_returns_its_input_when_nothing_matches(self) -> None:
        self.assertIn("return the input unchanged when they do not match", DOC)
        patched = aib.patch_workflow_signing_steps(
            self.UNRELATED,
            branch_if="github.ref == 'refs/heads/main'",
            sign_if="github.event_name != 'pull_request'",
        )
        self.assertEqual(patched, self.UNRELATED)

    def test_the_cosign_patcher_is_the_documented_exception(self) -> None:
        exception = re.search(
            r"The exception is `(\w+)\(\)`, which fails closed", DOC.replace("\n", " ")
        )
        self.assertIsNotNone(exception)
        patcher = getattr(aib, exception.group(1))
        split_verb = "\n".join(
            [
                "        run: |",
                "          cosign \\",
                "            sign -y --key env://COSIGN_PRIVATE_KEY ghcr.io/example/image",
            ]
        )
        with self.assertRaises(aib.CommandError) as raised:
            patcher(split_verb)
        self.assertIn("--new-bundle-format=false", str(raised.exception))
        # It fails closed, not shut: text it can rewrite is still rewritten,
        # and text it does not recognise passes through.
        self.assertEqual(patcher(self.UNRELATED), self.UNRELATED)

    def test_the_snapshot_backed_patcher_tests_still_read_the_real_snapshots(self) -> None:
        self.assertIn("run\nagainst the real bundled snapshots", DOC)
        suite = (ROOT / "tests/test_atomic_image_builder.py").read_text()
        self.assertIn("template_snapshots", suite)


class FedoraDefault(unittest.TestCase):
    """The one claim in the document that names a host file by path."""

    def test_the_default_tag_reads_etc_os_release_and_falls_back(self) -> None:
        defaults = aib.determine_fedora_atomic_default_tag.__kwdefaults__
        self.assertEqual(str(defaults["os_release_path"]), "/etc/os-release")
        self.assertEqual(defaults["fallback"], aib.FEDORA_ATOMIC_FALLBACK_TAG)
        with TemporaryDirectory() as tmp:
            os_release = Path(tmp) / "os-release"
            os_release.write_text('ID=debian\nVERSION_ID="12"\n')
            self.assertEqual(
                aib.determine_fedora_atomic_default_tag(os_release_path=os_release),
                aib.FEDORA_ATOMIC_FALLBACK_TAG,
            )
            os_release.write_text(f'ID=fedora\nVERSION_ID={aib.FEDORA_ATOMIC_FALLBACK_TAG}\n')
            self.assertEqual(
                aib.determine_fedora_atomic_default_tag(os_release_path=os_release),
                aib.FEDORA_ATOMIC_FALLBACK_TAG,
            )


class WhereToStart(unittest.TestCase):
    def test_every_symbol_in_the_table_resolves(self) -> None:
        for change, look_at in TABLES[WHERE_TO_START][1]:
            for name in named_symbols(look_at):
                if name in NOT_OUR_SYMBOLS:
                    continue
                with self.subTest(change=change, name=name):
                    self.assertIsNotNone(resolve(name))

    def test_the_two_build_methods_map_to_the_two_snapshots(self) -> None:
        closing = DOC.split("## Where to start", 1)[1]
        self.assertIn("Containerfile repos are patched from the `ublue-os` snapshot", closing)
        self.assertIn("BlueBuild repos are patched from the `blue-build` one", closing)
        snapshots = {p.name for p in (ROOT / "template_snapshots").iterdir() if p.is_dir()}
        self.assertEqual(snapshots, {"containerfile", "bluebuild"})


if __name__ == "__main__":
    unittest.main()
