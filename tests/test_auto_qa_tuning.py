"""Join .github/auto-qa-tuning.json's own claims to the files that make them true.

Script: tests/test_auto_qa_tuning.py
What: Reads the declaration as a SUBJECT. Every key it holds is a claim about
      the machinery: which workflow enforces the gate, that no machine writes
      the threshold, where the trend record lives and that nothing reads it
      back, that `.coveragerc` sets no `fail_under`, that nothing reads the
      declaration itself, and that the advisory tiers are not gated.
Doing: Parses ci.yml with tests/_block_yaml.py, the branch ruleset and
       `.coveragerc` with the standard library, and scans every tracked file
       outside tests/ and the prose for the paths the declaration names, then
       checks each claim against what those files actually do.
Why: The only test that opened this file before
     (`test_auto_qa_declaration_names_the_gate_without_copying_it`) checked
     `source_of_truth` and the advisory tier list. `enforced_by`,
     `what_adjusts_on_its_own`, `if_a_gate_blocks_you` and `$comment` were
     read by nothing, so a workflow renamed, a trend reader added or a
     `fail_under` put into `.coveragerc` would leave the policy the QA agents
     read describing a repository that no longer exists.
Goal: Make each of those changes fail here, naming the key whose sentence it
      falsified.

Every top-level key is classified below, so a key added to the declaration
fails until a test here reads it.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _block_yaml  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DECLARATION_RELATIVE = ".github/auto-qa-tuning.json"
DECLARATION = json.loads((ROOT / DECLARATION_RELATIVE).read_text())
RULESET = json.loads((ROOT / ".github/rulesets/main.json").read_text())
WORKFLOWS = ROOT / ".github/workflows"

# Which test reads which top-level key. A new key is a new claim.
CLASSIFIED_KEYS = {
    "$comment": "test_nothing_executable_reads_the_declaration",
    "policy": "test_policy_is_the_one_the_keys_describe",
    "never_auto_tuned": "test_enforced_by_runs_the_gate_on_the_required_check",
    "advisory_and_deliberately_untuned": "test_no_advisory_tier_is_gated",
    "what_adjusts_on_its_own": "test_trend_record_is_written_by_the_main_push_job",
    "if_a_gate_blocks_you": "test_the_named_config_sets_no_threshold",
}

JQ_READ = re.compile(r"""(\w+)="\$\(jq -er '([^']+)' (\S+)\)\"""")
FAIL_UNDER = re.compile(r"""--fail-under=("?)\$\{?(\w+)\}?\1""")


def tracked_files() -> list[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")[:-1]


def executable_sources() -> list[str]:
    """Tracked files that can act on a file, as opposed to describe it.

    Prose (Markdown, the notes file), the JSON declarations themselves, the
    vendored upstream snapshots and the tests are excluded: none of them runs
    in CI or in a user's checkout.
    """
    prose = (".md", ".mdc", ".txt", ".json", ".csv")
    return [
        name
        for name in tracked_files()
        if not name.startswith(
            ("tests/", "docs/", "template_snapshots/", "maintainer_docs/")
        )
        and not name.endswith(prose)
        and (ROOT / name).is_file()
    ]


def code_lines(text: str) -> list[str]:
    """Non-comment lines, `\\`-continuations folded so a command is one line."""
    folded = re.sub(r"\\\n\s*", " ", text)
    return [
        line
        for line in folded.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def workflow(name: str) -> dict:
    return _block_yaml.parse((WORKFLOWS / name).read_text())


def run_bodies(job: dict) -> list[str]:
    return [
        step["run"]
        for step in job.get("steps", [])
        if isinstance(step, dict) and "run" in step
    ]


class AutoQaTuningTests(unittest.TestCase):
    def test_every_key_is_read_by_a_test(self) -> None:
        self.assertEqual(
            set(DECLARATION),
            set(CLASSIFIED_KEYS),
            f"{DECLARATION_RELATIVE}'s keys and this file's CLASSIFIED_KEYS "
            f"disagree; a new key is a new claim, so add a test that reads it",
        )
        for test_name in CLASSIFIED_KEYS.values():
            self.assertTrue(
                hasattr(self, test_name),
                f"CLASSIFIED_KEYS names {test_name}, which does not exist",
            )

    def test_policy_is_the_one_the_keys_describe(self) -> None:
        # "no-automatic-tuning" is what the rest of the file argues. A
        # different value with the same body would be a policy nobody wrote.
        self.assertEqual(DECLARATION["policy"], "no-automatic-tuning")
        self.assertRegex(DECLARATION["$comment"], r"\bnot a tuner\b")
        self.assertRegex(DECLARATION["$comment"], r"never moved by a machine")

    def test_enforced_by_runs_the_gate_on_the_required_check(self) -> None:
        # `enforced_by` is only true if that workflow reads source_of_truth
        # and fails on it, in a job the ruleset makes a pull request wait for.
        # A workflow that merely prints the number, or a gate job the ruleset
        # does not require, enforces nothing.
        gates = DECLARATION["never_auto_tuned"]
        self.assertEqual(
            list(gates), ["unit_coverage_gate"], "a new gate needs its own join here"
        )
        gate = gates["unit_coverage_gate"]
        source = gate["source_of_truth"]
        enforcer = gate["enforced_by"]
        self.assertTrue(enforcer.startswith(".github/workflows/"), enforcer)
        self.assertIn(
            enforcer,
            tracked_files(),
            f"enforced_by names {enforcer}, which is not tracked",
        )

        thresholds = json.loads((ROOT / source).read_text())
        # unit_coverage_gate -> gated.unit: the gate's name says which value.
        tier = next(iter(gates)).removesuffix("_coverage_gate")
        self.assertIsInstance(thresholds["gated"][tier], int)

        data = workflow(Path(enforcer).name)
        self.assertIn(
            "pull_request", data["on"], f"{enforcer} does not run on pull requests"
        )
        required = {
            check["context"]
            for rule in RULESET["rules"]
            if rule["type"] == "required_status_checks"
            for check in rule["parameters"]["required_status_checks"]
        }
        gating_jobs = [
            key
            for key, job in data["jobs"].items()
            if key in required or job.get("name") in required
        ]
        self.assertTrue(
            gating_jobs,
            f"no job in {enforcer} is a required status check ({sorted(required)})",
        )

        enforced = []
        for key in gating_jobs:
            for body in run_bodies(data["jobs"][key]):
                reads = {var: (path, src) for var, path, src in JQ_READ.findall(body)}
                for _quote, var in FAIL_UNDER.findall(body):
                    if var in reads:
                        enforced.append((key, *reads[var]))
        self.assertIn(
            (gating_jobs[0], f".gated.{tier}", source),
            enforced,
            f"{enforcer}'s required job does not read .gated.{tier} from {source} "
            f"and pass it to --fail-under, so enforced_by is not true",
        )

    def test_no_machine_writes_the_threshold(self) -> None:
        # The declaration's whole point: the gated number moves only in a
        # reviewed commit. Every executable mention of source_of_truth has to
        # be the `jq -er` read or text echoed into a summary; anything else --
        # a redirect, `sed -i`, `tee`, a Python open -- is a machine able to
        # move the gate.
        source = DECLARATION["never_auto_tuned"]["unit_coverage_gate"][
            "source_of_truth"
        ]
        readers = []
        for name in executable_sources():
            for line in code_lines((ROOT / name).read_text(errors="replace")):
                if source not in line:
                    continue
                # An `echo` is text, unless its output lands in the file.
                self.assertNotRegex(
                    line,
                    rf"(>|\btee\b[^|;&]*?|\bsed -i\b.*?|\bmv\b.*?|\bcp\b.*?)\s*\S*{re.escape(source)}",
                    f"{name} writes {source}: {line.strip()!r}",
                )
                remainder = re.sub(
                    rf"\$\(jq -er '[^']+' {re.escape(source)}\)", "", line
                )
                if source in remainder and not remainder.lstrip().startswith("echo "):
                    self.fail(
                        f"{name} touches {source} other than by reading it: {line.strip()!r}"
                    )
                readers.append(name)
        # An empty scan checks nothing: the enforcer at least must be found.
        self.assertIn(
            DECLARATION["never_auto_tuned"]["unit_coverage_gate"]["enforced_by"],
            readers,
        )

    def test_no_advisory_tier_is_gated(self) -> None:
        # "These are advisory on purpose": no workflow fails a run on a
        # coverage number other than the gated one. Every --fail-under takes
        # a variable read from `.gated.*`, never a literal and never another
        # tier's threshold.
        seen = 0
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = re.sub(r"\\\n\s*", " ", path.read_text())
            reads = {var: jq_path for var, jq_path, _src in JQ_READ.findall(text)}
            for line in code_lines(text):
                for option in re.findall(r"--fail-under=\S+", line):
                    seen += 1
                    match = FAIL_UNDER.search(option)
                    self.assertIsNotNone(
                        match, f"{path.name} passes a literal threshold: {option}"
                    )
                    self.assertRegex(
                        reads.get(match.group(2), ""),
                        r"^\.gated\.",
                        f"{path.name} gates on ${match.group(2)}, which is not read from .gated",
                    )
        self.assertGreater(seen, 0, "no workflow passes --fail-under at all")

        # The one tier the reason names by name has to be one of the tiers.
        advisory = DECLARATION["advisory_and_deliberately_untuned"]
        named = re.findall(r"The ([\w-]+) tier", advisory["reason"])
        self.assertTrue(named, "the advisory reason no longer names a tier")
        for tier in named:
            self.assertIn(
                tier,
                advisory["tiers"],
                f"the advisory reason names a {tier!r} tier that is not listed",
            )

    def test_trend_record_is_written_by_the_main_push_job(self) -> None:
        # "coverage-trend.csv on the coverage-data branch, appended per push
        # to main": exactly one step writes the file, in a job that runs only
        # on a push to main, and it pushes to that branch.
        claim = DECLARATION["what_adjusts_on_its_own"]["coverage_trend"]
        match = re.match(
            r"(\S+) on the (\S+) branch, appended per push to (\S+)\.", claim
        )
        self.assertIsNotNone(
            match,
            f"coverage_trend no longer reads '<file> on the <branch> branch, appended per push to <branch>.': {claim!r}",
        )
        trend_file, data_branch, pushed_branch = match.groups()

        writers = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            if trend_file not in path.read_text():
                continue
            for key, job in workflow(path.name)["jobs"].items():
                for body in run_bodies(job):
                    if re.search(rf"--trend-out \S*{re.escape(trend_file)}", body):
                        writers.append((path.name, key, job.get("if"), body))
        self.assertEqual(
            len(writers),
            1,
            f"expected one step writing {trend_file}, found {[w[:2] for w in writers]}",
        )
        _name, _key, job_if, body = writers[0]
        self.assertEqual(
            job_if,
            f"github.event_name == 'push' && github.ref == 'refs/heads/{pushed_branch}'",
            f"the job writing {trend_file} does not run only on a push to {pushed_branch}",
        )
        self.assertIn(f"git fetch origin {data_branch}:", body)
        self.assertIn(f"push origin HEAD:{data_branch}", body)

    def test_nothing_reads_the_trend_record_back(self) -> None:
        # "A record, not a control -- nothing reads it back." Every executable
        # mention of the file is the write (`--trend-out`) or staging it for
        # the commit, and coverage_badge.py opens the path it is given for
        # appending only.
        trend_file = DECLARATION["what_adjusts_on_its_own"]["coverage_trend"].split()[0]
        mentions = 0
        for name in executable_sources():
            for line in code_lines((ROOT / name).read_text(errors="replace")):
                if trend_file not in line:
                    continue
                mentions += 1
                self.assertRegex(
                    line,
                    rf"--trend-out \S*{re.escape(trend_file)}|git -C \S+ add .*{re.escape(trend_file)}",
                    f"{name} does something other than write {trend_file}: {line.strip()!r}",
                )
        self.assertGreater(
            mentions, 0, f"nothing mentions {trend_file}; the claim describes nothing"
        )

        tree = ast.parse((ROOT / "coverage_badge.py").read_text())
        modes = [
            node.args[1].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
            and ast.unparse(node.args[0]) == "args.trend_out"
        ]
        self.assertEqual(
            modes,
            ["a"],
            "coverage_badge.py must open the trend file for appending, and only that",
        )

    def test_the_named_config_sets_no_threshold(self) -> None:
        # "... .coveragerc sets no fail_under, so a bare coverage report exits
        # 0 however far coverage has fallen." The file and the option are read
        # out of the sentence, so rewording it to name another file checks
        # that file instead.
        advice = DECLARATION["if_a_gate_blocks_you"]
        match = re.search(r"(\.\S+) sets no (\w+)", advice)
        self.assertIsNotNone(
            match,
            f"if_a_gate_blocks_you no longer names a config and option: {advice!r}",
        )
        config_name, option = match.groups()
        # "a bare coverage report" reads coverage.py's default rcfile and no
        # other, so the sentence is only about that one.
        self.assertEqual(
            config_name,
            ".coveragerc",
            "a bare `coverage report` reads .coveragerc; naming another "
            "config does not explain why the bare report exits 0",
        )
        self.assertIn(config_name, tracked_files())
        config = configparser.ConfigParser()
        config.read(ROOT / config_name)
        self.assertTrue(config.sections(), f"{config_name} parsed to nothing")
        for section in config.sections():
            self.assertFalse(
                config.has_option(section, option),
                f"{config_name} [{section}] sets {option}; {DECLARATION_RELATIVE} says it does not",
            )

    def test_nothing_executable_reads_the_declaration(self) -> None:
        # "nothing here reads this file and changes a threshold": no workflow,
        # script or module names it. And the two documents it sends a reader
        # to exist.
        basename = Path(DECLARATION_RELATIVE).name
        readers = [
            name
            for name in executable_sources()
            if basename in (ROOT / name).read_text(errors="replace")
        ]
        self.assertEqual(
            readers,
            [],
            f"{readers} read {DECLARATION_RELATIVE}, which says nothing reads it",
        )

        docs = re.findall(r"docs/[\w./-]+\.md", DECLARATION["$comment"])
        self.assertEqual(docs, ["docs/quality.md", "docs/metrics.md"])
        tracked = tracked_files()
        for doc in docs:
            self.assertIn(
                doc,
                tracked,
                f"$comment sends the reader to {doc}, which is not tracked",
            )


if __name__ == "__main__":
    unittest.main()
