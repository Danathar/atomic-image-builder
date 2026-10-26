"""Tests for mutation_check.py, the diff-scoped mutation run.

The script drives cosmic-ray, which CI installs only for the job that runs
it, so every test here stubs the cosmic-ray processes and the work database.
What is real: the diff parsing, the annotation spans, the report, the
ledger committed at the repository root, and the target table, which is
joined to .coveragerc, to the tracked hooks and to the test files that
import each module.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mutation_check
from mutation_check import (
    TARGETS,
    annotation_spans,
    changed_lines,
    config_text,
    kept_operator,
    load_ledger,
    outcomes,
    report,
    survivors,
    worth_running,
)

ROOT = Path(__file__).resolve().parents[1]


def tracked(*patterns: str) -> list[str]:
    return [
        name
        for name in subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", *patterns], capture_output=True, text=True, check=True
        ).stdout.split("\0")
        if name
    ]


def mutation(operator: str, start: tuple[int, int], end: tuple[int, int]) -> SimpleNamespace:
    return SimpleNamespace(operator_name=operator, start_pos=start, end_pos=end)


def dump_row(operator: str, row: int, outcome: str | None, worker: str = "normal", module: str = "m.py") -> str:
    item = {"job_id": "j", "mutations": [{"module_path": module, "operator_name": operator, "start_pos": [row, 0]}]}
    result = None if outcome is None and worker == "normal" else {"worker_outcome": worker, "test_outcome": outcome}
    return json.dumps([item, result])


class KeptOperatorTests(unittest.TestCase):
    def test_keeps_one_swap_per_binary_operator(self) -> None:
        self.assertTrue(kept_operator("core/ReplaceBinaryOperator_Add_Sub"))
        self.assertFalse(kept_operator("core/ReplaceBinaryOperator_Add_Mul"))
        self.assertTrue(kept_operator("core/ReplaceBinaryOperator_BitOr_BitAnd"))
        self.assertFalse(kept_operator("core/ReplaceBinaryOperator_BitOr_BitXor"))

    def test_keeps_the_boundary_and_negation_swaps_per_comparison(self) -> None:
        for kept in ("Eq_NotEq", "Lt_LtE", "Lt_GtE", "In_NotIn", "Is_IsNot"):
            self.assertTrue(kept_operator(f"core/ReplaceComparisonOperator_{kept}"), kept)
        for dropped in ("Eq_Lt", "Eq_Is", "Lt_Eq", "In_Eq"):
            self.assertFalse(kept_operator(f"core/ReplaceComparisonOperator_{dropped}"), dropped)

    def test_keeps_every_operator_outside_the_two_swap_families(self) -> None:
        for name in ("core/NumberReplacer", "core/AddNot", "core/ZeroIterationForLoop", "core/ReplaceUnaryOperator_USub_UAdd"):
            self.assertTrue(kept_operator(name), name)

    def test_every_kept_swap_names_two_different_operators(self) -> None:
        for family, swaps in mutation_check.KEPT_SWAPS.items():
            for swap in swaps:
                before, after = swap.split("_")
                self.assertNotEqual(before, after, f"{family}_{swap}")
            # One kept swap per source operator for binary operators.
            if family == "ReplaceBinaryOperator":
                sources = [swap.split("_")[0] for swap in swaps]
                self.assertEqual(len(sources), len(set(sources)), sorted(sources))


class ChangedLinesTests(unittest.TestCase):
    def test_reads_every_line_of_each_hunk(self) -> None:
        diff = "--- a/m.py\n+++ b/m.py\n@@ -3,0 +4,2 @@ def f():\n+a\n+b\n@@ -10 +12 @@\n-x\n+y\n"
        self.assertEqual(changed_lines(diff), {4, 5, 12})

    def test_a_pure_deletion_changes_no_line_that_can_be_mutated(self) -> None:
        self.assertEqual(changed_lines("@@ -7,3 +6,0 @@\n-a\n-b\n-c\n"), set())

    def test_a_removed_line_that_looks_like_a_hunk_header_is_not_one(self) -> None:
        self.assertEqual(changed_lines("@@ -1 +1 @@\n-@@ -5 +9 @@\n+x\n"), {1})

    def test_no_diff_changes_nothing(self) -> None:
        self.assertEqual(changed_lines(""), set())


class AnnotationSpanTests(unittest.TestCase):
    SOURCE = (
        "from __future__ import annotations\n"
        "def f(a: int | None, *rest: str, key: bool = True, **extra: list[int]) -> str | None:\n"
        "    total: int | float = 0\n"
        "    plain = a | 1\n"
        "    return None\n"
        "async def g(b) -> dict[str, int]:\n"
        "    return {}\n"
    )

    def text(self, span) -> str:
        (row, col), (end_row, end_col) = span
        self.assertEqual(row, end_row)
        return self.SOURCE.splitlines()[row - 1][col:end_col]

    def test_finds_return_argument_and_variable_annotations(self) -> None:
        self.assertEqual(
            sorted(self.text(span) for span in annotation_spans(self.SOURCE)),
            sorted(["int | None", "str", "bool", "list[int]", "str | None", "int | float", "dict[str, int]"]),
        )

    def test_an_unannotated_function_has_no_span(self) -> None:
        self.assertEqual(annotation_spans("def f(a, b):\n    return a | b\n"), [])


class WorthRunningTests(unittest.TestCase):
    SPANS = [((2, 10), (2, 20))]

    def test_runs_a_kept_operator_on_a_changed_line(self) -> None:
        self.assertTrue(worth_running(mutation("core/AddNot", (5, 4), (5, 9)), {5}, self.SPANS))

    def test_a_mutation_spanning_onto_a_changed_line_runs(self) -> None:
        self.assertTrue(worth_running(mutation("core/AddNot", (4, 4), (6, 1)), {6}, self.SPANS))

    def test_skips_an_unchanged_line(self) -> None:
        self.assertFalse(worth_running(mutation("core/AddNot", (5, 4), (5, 9)), {4, 6}, self.SPANS))

    def test_skips_a_dropped_swap(self) -> None:
        self.assertFalse(worth_running(mutation("core/ReplaceBinaryOperator_Add_Mul", (5, 4), (5, 5)), {5}, self.SPANS))

    def test_skips_a_mutation_inside_an_annotation(self) -> None:
        self.assertFalse(worth_running(mutation("core/ReplaceBinaryOperator_BitOr_BitAnd", (2, 14), (2, 15)), {2}, self.SPANS))
        # Touching the span's edges is still inside it.
        self.assertFalse(worth_running(mutation("core/AddNot", (2, 10), (2, 20)), {2}, self.SPANS))

    def test_a_mutation_outside_the_annotation_on_the_same_line_runs(self) -> None:
        self.assertTrue(worth_running(mutation("core/ReplaceBinaryOperator_BitOr_BitAnd", (2, 24), (2, 25)), {2}, self.SPANS))
        self.assertTrue(worth_running(mutation("core/AddNot", (2, 9), (2, 15)), {2}, self.SPANS))


class SkipUnwantedTests(unittest.TestCase):
    def fake_cosmic_ray(self, items):
        recorded = []

        class FakeDB:
            pending_work_items = items

            def set_multiple_results(self, job_ids, result):
                recorded.append((job_ids, result))

        @contextlib.contextmanager
        def use_db(path, mode):
            recorded.append(("open", path, mode))
            yield FakeDB()

        work_db = types.ModuleType("cosmic_ray.work_db")
        work_db.WorkDB = SimpleNamespace(Mode=SimpleNamespace(open="open-mode"))
        work_db.use_db = use_db
        work_item = types.ModuleType("cosmic_ray.work_item")
        work_item.WorkerOutcome = SimpleNamespace(SKIPPED="skipped")
        work_item.WorkResult = lambda output, worker_outcome: (output, worker_outcome)
        modules = {"cosmic_ray": types.ModuleType("cosmic_ray"), "cosmic_ray.work_db": work_db, "cosmic_ray.work_item": work_item}
        return mock.patch.dict(sys.modules, modules), recorded

    def test_marks_only_the_mutants_not_worth_running_as_skipped(self) -> None:
        items = [
            SimpleNamespace(job_id="keep", mutations=[mutation("core/AddNot", (3, 0), (3, 4))]),
            SimpleNamespace(job_id="unchanged", mutations=[mutation("core/AddNot", (9, 0), (9, 4))]),
            SimpleNamespace(job_id="annotation", mutations=[mutation("core/AddNot", (3, 10), (3, 12))]),
        ]
        patch, recorded = self.fake_cosmic_ray(items)
        with patch:
            mutation_check.skip_unwanted(Path("s.sqlite"), {3}, [((3, 8), (3, 20))])
        self.assertEqual(recorded[0], ("open", "s.sqlite", "open-mode"))
        self.assertEqual(recorded[1], (["unchanged", "annotation"], ("not worth running", "skipped")))
        self.assertEqual(len(recorded), 2)

    def test_writes_nothing_when_every_mutant_is_worth_running(self) -> None:
        patch, recorded = self.fake_cosmic_ray([SimpleNamespace(job_id="keep", mutations=[mutation("core/AddNot", (3, 0), (3, 4))])])
        with patch:
            mutation_check.skip_unwanted(Path("s.sqlite"), {3}, [])
        self.assertEqual(recorded, [("open", "s.sqlite", "open-mode")])


class ConfigTextTests(unittest.TestCase):
    def values(self, text: str) -> dict[str, object]:
        # CI runs Python 3.10, which has no tomllib; every value written is
        # a JSON scalar or the empty list, so JSON reads it.
        return {
            key: json.loads(value)
            for key, value in re.findall(r"^([\w-]+) = (.+)$", text, flags=re.MULTILINE)
        }

    def test_mutates_the_target_with_its_own_tests_failing_fast(self) -> None:
        text = config_text(".claude/hooks/gate_git_diff.py", ("test_a", "test_b"))
        values = self.values(text)
        self.assertEqual(values["module-path"], ".claude/hooks/gate_git_diff.py")
        self.assertEqual(values["timeout"], mutation_check.TIMEOUT_SECONDS)
        self.assertEqual(values["excluded-modules"], [])
        self.assertEqual(values["name"], "local")
        self.assertEqual(
            values["test-command"].split(),
            ["env", "PYTHONPATH=tests", sys.executable, "-m", "unittest", "-f", "test_a", "test_b"],
        )
        self.assertLess(text.index("[cosmic-ray]"), text.index("module-path"))
        self.assertLess(text.index("test-command"), text.index("[cosmic-ray.distributor]"))
        self.assertLess(text.index("[cosmic-ray.distributor]"), text.index('name = "local"'))


class LedgerTests(unittest.TestCase):
    def test_reads_each_accepted_entry_as_a_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            entry = {"module": "m.py", "operator": "core/AddNot", "line": "if x:", "reason": "equivalent"}
            path.write_text(json.dumps({"accepted": [entry]}))
            self.assertEqual(load_ledger(path), {("m.py", "core/AddNot", "if x:")})

    def test_the_committed_ledger_names_real_live_mutation_sites(self) -> None:
        # An entry whose line has since been edited would quietly accept
        # nothing, and one naming a skipped swap could never match; both
        # are rot a reader would trust.
        data = json.loads(mutation_check.LEDGER.read_text(encoding="utf-8"))
        self.assertTrue(data["description"])
        keys = [(entry["module"], entry["operator"], entry["line"]) for entry in data["accepted"]]
        self.assertEqual(len(keys), len(set(keys)), "a ledger entry is listed twice")
        self.assertEqual(load_ledger(mutation_check.LEDGER), set(keys))
        for entry in data["accepted"]:
            with self.subTest(entry=entry):
                self.assertEqual(set(entry), {"module", "operator", "line", "reason"})
                self.assertIn(entry["module"], TARGETS)
                self.assertTrue(kept_operator(entry["operator"]))
                self.assertTrue(entry["reason"].strip())
                lines = [line.strip() for line in (ROOT / entry["module"]).read_text(encoding="utf-8").splitlines()]
                self.assertIn(entry["line"], lines)


class DumpTests(unittest.TestCase):
    def test_keys_each_survivor_by_its_source_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "m.py").write_text("a = 1\n    if a == 1:\n")
            dump = "\n".join(
                [
                    dump_row("core/NumberReplacer", 1, "killed"),
                    dump_row("core/ReplaceComparisonOperator_Eq_NotEq", 2, "survived"),
                    dump_row("core/NumberReplacer", 1, "survived"),
                    dump_row("core/AddNot", 2, None),
                    dump_row("core/AddNot", 2, None, worker="skipped"),
                ]
            )
            self.assertEqual(
                survivors(dump, Path(tmp)),
                [
                    {"module": "m.py", "line_number": 2, "operator": "core/ReplaceComparisonOperator_Eq_NotEq", "line": "if a == 1:"},
                    {"module": "m.py", "line_number": 1, "operator": "core/NumberReplacer", "line": "a = 1"},
                ],
            )

    def test_counts_each_outcome(self) -> None:
        dump = "\n".join(
            [
                dump_row("core/AddNot", 1, "killed"),
                dump_row("core/AddNot", 1, "killed"),
                dump_row("core/AddNot", 1, "survived"),
                dump_row("core/AddNot", 1, None, worker="timeout"),
                dump_row("core/AddNot", 1, None, worker="skipped"),
                dump_row("core/AddNot", 1, None),
            ]
        )
        self.assertEqual(
            outcomes(dump), {"killed": 2, "survived": 1, "timeout": 1, "skipped": 1, "not run": 1}
        )


class ReportTests(unittest.TestCase):
    SURVIVOR = {"module": "m.py", "line_number": 4, "operator": "core/AddNot", "line": "x = a | b"}

    def test_says_so_when_no_target_changed(self) -> None:
        self.assertEqual(report({}, set()), ("## Mutation check\n\nNo module in the mutation table changed.\n", 0))

    def test_lists_each_target_without_its_skipped_count(self) -> None:
        text, new = report({"m.py": ({"killed": 3, "skipped": 90}, []), "n.py": ({"skipped": 5}, [])}, set())
        self.assertEqual(new, 0)
        self.assertIn("- `m.py`: 3 killed\n", text)
        self.assertIn("- `n.py`: no mutant on a changed line\n", text)
        self.assertNotIn("90", text)
        self.assertTrue(text.endswith("No surviving mutant outside the ledger.\n"))

    def test_an_accepted_survivor_is_not_reported(self) -> None:
        text, new = report({"m.py": ({"survived": 1}, [self.SURVIVOR])}, {("m.py", "core/AddNot", "x = a | b")})
        self.assertEqual(new, 0)
        self.assertIn("No surviving mutant outside the ledger.", text)

    def test_tables_each_new_survivor_and_escapes_its_pipes(self) -> None:
        other = dict(self.SURVIVOR, operator="core/NumberReplacer")
        text, new = report(
            {"m.py": ({"killed": 1, "survived": 2}, [self.SURVIVOR, other])}, {("m.py", "core/NumberReplacer", "x = a | b")}
        )
        self.assertEqual(new, 1)
        self.assertIn("- `m.py`: 1 killed, 2 survived\n", text)
        self.assertIn("1 surviving mutant(s) are not in `.mutation-survivors.json`.", text)
        self.assertIn("| Where | Operator | Line |\n|---|---|---|\n", text)
        self.assertIn("| `m.py:4` | `core/AddNot` | `x = a \\| b` |\n", text)
        self.assertNotIn("NumberReplacer", text)


    def test_a_survivor_line_holding_backticks_stays_one_code_span(self) -> None:
        # The sources quote names in backticks; a single-backtick span ended
        # at the first one and the cell rendered as broken code.
        quoted = dict(self.SURVIVOR, line='f"The `{ASSET}` attached to {tag}"')
        text, _new = report({"m.py": ({"survived": 1}, [quoted])}, set())
        self.assertIn('| `m.py:4` | `core/AddNot` | ``f"The `{ASSET}` attached to {tag}"`` |\n', text)

    def test_code_span_fences_past_the_longest_backtick_run(self) -> None:
        self.assertEqual(mutation_check.code_span("x = a"), "`x = a`")
        self.assertEqual(mutation_check.code_span("a ``b`` c"), "```a ``b`` c```")
        self.assertEqual(mutation_check.code_span("`x`"), "`` `x` ``")
        self.assertEqual(mutation_check.code_span("x`"), "`` x` ``")


class CheckTests(unittest.TestCase):
    def test_runs_init_then_skips_then_exec_then_dump(self) -> None:
        calls = []

        def cosmic_ray(*args):
            calls.append(("cosmic-ray", args[0]))
            if args[0] == "dump":
                return dump_row("core/AddNot", 1, "survived", module="coverage_badge.py") + "\n"
            return ""

        def git(*args):
            calls.append(("git", args))
            return "@@ -1 +1,2 @@\n"

        def skip(session, lines, spans):
            calls.append(("skip", session.name, lines, bool(spans)))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.multiple(
            mutation_check, cosmic_ray=cosmic_ray, git=git, skip_unwanted=skip
        ):
            counts, found = mutation_check.check("coverage_badge.py", "origin/main", Path(tmp))
            config = (Path(tmp) / "coverage_badge.py.toml").read_text()
        self.assertEqual(config, config_text("coverage_badge.py", TARGETS["coverage_badge.py"]))
        self.assertEqual(
            calls,
            [
                ("cosmic-ray", "init"),
                ("git", ("diff", "-U0", "origin/main", "--", "coverage_badge.py")),
                ("skip", "coverage_badge.py.sqlite", {1, 2}, True),
                ("cosmic-ray", "exec"),
                ("cosmic-ray", "dump"),
            ],
        )
        self.assertEqual(counts, {"survived": 1})
        self.assertEqual(found[0]["line"], "#!/usr/bin/env python3")

    def test_a_hook_path_becomes_a_flat_session_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.multiple(
            mutation_check, cosmic_ray=mock.Mock(return_value=""), git=mock.Mock(return_value=""), skip_unwanted=mock.Mock()
        ):
            mutation_check.check(".claude/hooks/gate_git_diff.py", "main", Path(tmp))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [".claude_hooks_gate_git_diff.py.toml"])

    def test_the_process_helpers_run_git_and_cosmic_ray_from_this_interpreter(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(stdout="out")) as run:
            self.assertEqual(mutation_check.git("diff", "x"), "out")
            self.assertEqual(mutation_check.cosmic_ray("dump", "s"), "out")
        self.assertEqual(run.call_args_list[0].args[0], ["git", "-C", str(ROOT), "diff", "x"])
        self.assertEqual(run.call_args_list[1].args[0], [sys.executable, "-m", "cosmic_ray.cli", "dump", "s"])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs, {"check": True, "capture_output": True, "text": True})


class MainTests(unittest.TestCase):
    def run_main(self, argv, *, changed="", check=None):
        ledger = Path(self.tmp.name) / "ledger.json"
        ledger.write_text(json.dumps({"accepted": [{"module": "coverage_badge.py", "operator": "core/AddNot", "line": "ok", "reason": "r"}]}))
        stdout, stderr = io.StringIO(), io.StringIO()
        git = mock.Mock(return_value=changed)
        check = check or mock.Mock()
        with mock.patch.multiple(mutation_check, git=git, check=check), mock.patch.object(
            mutation_check.os, "chdir"
        ) as chdir, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = mutation_check.main([*argv, "--ledger", str(ledger)])
        return code, stdout.getvalue(), stderr.getvalue(), git, check, chdir

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_requires_a_base(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mutation_check.main([])

    def test_passes_when_no_target_changed(self) -> None:
        code, out, _, git, check, chdir = self.run_main(["--base", "origin/main"], changed="README.md\n")
        self.assertEqual(code, 0)
        self.assertIn("No module in the mutation table changed.", out)
        self.assertEqual(git.call_args.args, ("diff", "--name-only", "--diff-filter=d", "origin/main", "--", *TARGETS))
        check.assert_not_called()
        chdir.assert_called_once_with(mutation_check.ROOT)

    def test_checks_each_changed_target_in_order_and_fails_on_a_new_survivor(self) -> None:
        found = {
            "coverage_badge.py": [{"module": "coverage_badge.py", "line_number": 1, "operator": "core/AddNot", "line": "ok"}],
            "atomic_image_builder.py": [{"module": "atomic_image_builder.py", "line_number": 9, "operator": "core/AddNot", "line": "new"}],
        }
        check = mock.Mock(side_effect=lambda target, base, workdir: ({"survived": 1}, found[target]))
        code, out, _, _, check, _ = self.run_main(
            ["--base", "b"], changed="coverage_badge.py\natomic_image_builder.py\nREADME.md\n", check=check
        )
        self.assertEqual(code, 1)
        self.assertEqual([c.args[:2] for c in check.call_args_list], [("atomic_image_builder.py", "b"), ("coverage_badge.py", "b")])
        self.assertIn("`atomic_image_builder.py:9`", out)
        self.assertNotIn("`coverage_badge.py:1`", out)

    def test_passes_when_every_survivor_is_accepted(self) -> None:
        found = [{"module": "coverage_badge.py", "line_number": 1, "operator": "core/AddNot", "line": "ok"}]
        code, out, _, _, _, _ = self.run_main(
            ["--base", "b"], changed="coverage_badge.py\n", check=mock.Mock(return_value=({"survived": 1}, found))
        )
        self.assertEqual(code, 0)
        self.assertIn("No surviving mutant outside the ledger.", out)

    def test_a_failing_cosmic_ray_command_exits_2_with_its_stderr(self) -> None:
        error = subprocess.CalledProcessError(1, ["python", "-m", "cosmic_ray.cli", "exec"], stderr="boom\n")
        code, out, err, _, _, _ = self.run_main(["--base", "b"], changed="coverage_badge.py\n", check=mock.Mock(side_effect=error))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "mutation check failed: python -m cosmic_ray.cli exec\nboom\n\n")

    def test_a_target_the_change_deleted_is_not_checked(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            def run(*args: str) -> None:
                subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)

            run("init", "-q")
            (Path(repo) / "coverage_badge.py").write_text("x = 1\n")
            run("add", ".")
            run("commit", "-qm", "base")
            run("rm", "-q", "coverage_badge.py")
            run("commit", "-qm", "delete the target")
            with mock.patch.object(mutation_check, "ROOT", Path(repo)):
                listed = mutation_check.git("diff", "--name-only", "--diff-filter=d", "HEAD~1", "--", "coverage_badge.py")
                plain = mutation_check.git("diff", "--name-only", "HEAD~1", "--", "coverage_badge.py")
            self.assertEqual(plain, "coverage_badge.py\n")
            self.assertEqual(listed, "")


class TargetTableTests(unittest.TestCase):
    def test_targets_every_unit_measured_module_and_every_tracked_hook(self) -> None:
        config = (ROOT / ".coveragerc").read_text()
        source = re.split(r"^\S", config.split("source =", 1)[1], maxsplit=1, flags=re.MULTILINE)[0]
        measured = {f"{name}.py" for name in re.findall(r"^\s+(\S+)$", source, flags=re.MULTILINE)}
        hooks = {name for name in tracked(".claude/hooks/") if name.endswith(".py")}
        self.assertIn("atomic_image_builder.py", measured)
        self.assertIn(".claude/hooks/gate_git_diff.py", hooks)
        self.assertEqual(set(TARGETS), measured | hooks)

    def test_a_root_module_runs_every_test_file_that_imports_it(self) -> None:
        importers: dict[str, set[str]] = {}
        for name in tracked("tests/test_*.py"):
            for node in ast.walk(ast.parse((ROOT / name).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    importers.setdefault(f"{module}.py", set()).add(Path(name).stem)
        for target, tests in TARGETS.items():
            if "/" in target:
                continue
            with self.subTest(target=target):
                self.assertEqual(set(tests), importers.get(target, set()))
                self.assertEqual(list(tests), sorted(tests))

    def test_the_hook_runs_the_test_files_that_drive_it(self) -> None:
        tests = TARGETS[".claude/hooks/gate_git_diff.py"]
        for test in tests:
            with self.subTest(test=test):
                self.assertTrue((ROOT / "tests" / f"{test}.py").is_file())
        loader = (ROOT / "tests" / f"{tests[0]}.py").read_text(encoding="utf-8")
        self.assertIn('HOOK = ROOT / ".claude/hooks/gate_git_diff.py"', loader)
        self.assertIn("spec_from_file_location", loader)
        self.assertIn("gate_git_diff.py", (ROOT / "tests" / f"{tests[1]}.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
