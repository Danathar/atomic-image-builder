"""Tests for tests/check_coverage_ceilings.py, the missed-statement ceiling.

What is real: the thresholds file this repository commits, and a coverage
JSON report written by coverage.py itself from a small module measured in a
subprocess, so the report shape the checker reads is the one CI produces.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import check_coverage_ceilings as ceilings_check

ROOT = Path(__file__).resolve().parent.parent
THRESHOLDS = ROOT / ".coverage-thresholds.json"


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def report(files: dict[str, list[int]]) -> dict:
    return {"files": {module: {"missing_lines": lines} for module, lines in files.items()}}


class EvaluateTests(unittest.TestCase):
    def test_at_the_ceiling_passes(self) -> None:
        self.assertEqual(ceilings_check.evaluate({"a.py": [3, 7]}, {"a.py": 2}), [])

    def test_under_the_ceiling_passes(self) -> None:
        self.assertEqual(ceilings_check.evaluate({"a.py": [3]}, {"a.py": 2}), [])

    def test_over_the_ceiling_fails_naming_every_missed_line(self) -> None:
        self.assertEqual(
            ceilings_check.evaluate({"a.py": [9, 3, 7]}, {"a.py": 2}),
            ["a.py: 3 statements no test reaches, ceiling 2; missing lines: 9, 3, 7"],
        )

    def test_a_module_without_a_ceiling_allows_nothing_missed(self) -> None:
        self.assertEqual(ceilings_check.evaluate({"a.py": []}, {}), [])
        self.assertEqual(
            ceilings_check.evaluate({"a.py": [4]}, {}),
            ["a.py: 1 statements no test reaches, ceiling 0; missing lines: 4"],
        )

    def test_a_ceiling_for_an_unmeasured_module_fails(self) -> None:
        self.assertEqual(
            ceilings_check.evaluate({"a.py": []}, {"renamed.py": 1}),
            ["renamed.py: has a ceiling of 1 but the coverage run did not measure it"],
        )


class LoadCeilingsTests(unittest.TestCase):
    def load(self, value: object) -> dict[str, int]:
        with tempfile.TemporaryDirectory() as tmp:
            return ceilings_check.load_ceilings(write_json(Path(tmp) / "t.json", value))

    def test_reads_the_ceilings_section(self) -> None:
        self.assertEqual(self.load({"gated": {"unit": 90}, "ceilings": {"a.py": 3}}), {"a.py": 3})

    def test_no_section_means_every_ceiling_is_zero(self) -> None:
        self.assertEqual(self.load({"gated": {"unit": 90}}), {})

    def test_refuses_a_ceiling_that_is_not_a_non_negative_integer(self) -> None:
        for bad in (-1, 1.5, "3", True, None):
            with self.subTest(bad=bad), self.assertRaisesRegex(
                ceilings_check.CeilingError, "must be a non-negative integer"
            ):
                self.load({"ceilings": {"a.py": bad}})

    def test_refuses_a_section_that_is_not_an_object(self) -> None:
        for bad in ({"ceilings": ["a.py"]}, ["ceilings"]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ceilings_check.CeilingError, "must be an object"):
                self.load(bad)

    def test_refuses_an_unreadable_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ceilings_check.CeilingError, "cannot read thresholds"):
                ceilings_check.load_ceilings(path)
            with self.assertRaisesRegex(ceilings_check.CeilingError, "cannot read thresholds"):
                ceilings_check.load_ceilings(Path(tmp) / "absent.json")

    def test_the_committed_ceilings_load(self) -> None:
        loaded = ceilings_check.load_ceilings(THRESHOLDS)
        self.assertTrue(loaded, "the ceilings section is missing from .coverage-thresholds.json")
        source = (ROOT / ".coveragerc").read_text(encoding="utf-8")
        for module in loaded:
            # A ceiling for a module the unit gate does not measure would fail
            # the stale-entry check in CI; catch the rename here instead.
            self.assertIn(f"    {module.removesuffix('.py')}\n", source, module)


class LoadMissingLinesTests(unittest.TestCase):
    def test_reads_a_report_written_by_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "sample.py").write_text(
                "def f(x):\n    if x:\n        raise ValueError(x)\n    return 1\n\nf(0)\n", encoding="utf-8"
            )
            run = {"cwd": work, "check": True, "capture_output": True}
            subprocess.run([sys.executable, "-m", "coverage", "run", "--branch", "sample.py"], **run)
            subprocess.run([sys.executable, "-m", "coverage", "json", "-o", "coverage.json"], **run)
            self.assertEqual(ceilings_check.load_missing_lines(work / "coverage.json"), {"sample.py": [3]})

    def test_sorts_the_missed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp) / "c.json", report({"a.py": [9, 3]}))
            self.assertEqual(ceilings_check.load_missing_lines(path), {"a.py": [3, 9]})

    def test_refuses_anything_but_a_coverage_report(self) -> None:
        for bad in ({}, {"files": []}, {"files": {"a.py": {}}}, {"files": {"a.py": 3}}, []):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ceilings_check.CeilingError, "not a coverage JSON report"):
                    ceilings_check.load_missing_lines(write_json(Path(tmp) / "c.json", bad))
        with self.assertRaisesRegex(ceilings_check.CeilingError, "not a coverage JSON report"):
            ceilings_check.load_missing_lines(Path("/nonexistent/coverage.json"))


class MainTests(unittest.TestCase):
    def run_main(self, files: dict[str, list[int]], ceilings: object) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            coverage_json = write_json(Path(tmp) / "c.json", report(files))
            thresholds = write_json(Path(tmp) / "t.json", {"ceilings": ceilings})
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ceilings_check.main([str(coverage_json), "--thresholds", str(thresholds)])
        return code, out.getvalue(), err.getvalue()

    def test_passes_when_every_module_is_at_or_under_its_ceiling(self) -> None:
        code, out, err = self.run_main({"a.py": [1], "b.py": []}, {"a.py": 1})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("at or under", out)

    def test_under_a_ceiling_reports_it_could_lower_and_still_passes(self) -> None:
        code, out, err = self.run_main({"a.py": [1]}, {"a.py": 3})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("could lower a.py: ceiling 3, missing 1", out)

    def test_at_a_ceiling_reports_nothing_to_lower(self) -> None:
        code, out, err = self.run_main({"a.py": [1]}, {"a.py": 1})
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("could lower", out)

    def test_fails_naming_the_module_and_how_to_fix_it(self) -> None:
        code, out, err = self.run_main({"a.py": [1, 2]}, {"a.py": 1})
        self.assertEqual((code, out), (1, ""))
        self.assertIn("a.py: 2 statements no test reaches, ceiling 1; missing lines: 1, 2", err)
        self.assertIn("raise the module's ceiling", err)

    def test_an_unreadable_input_is_its_own_exit_status(self) -> None:
        code, out, err = self.run_main({"a.py": []}, ["a.py"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("error:", err)

    def test_defaults_to_the_committed_thresholds_file(self) -> None:
        self.assertEqual(ceilings_check.THRESHOLDS, THRESHOLDS)


if __name__ == "__main__":
    unittest.main()
