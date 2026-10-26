#!/usr/bin/env python3
"""Mutation-test the lines a change touches, and report the survivors.

Line coverage says a rule ran; it does not say a test would notice the rule
breaking. A mutant is a copy of a module with one small deliberate break (a
comparison flipped, a constant changed, a loop that never iterates); when the
tests still pass against it, the rule it broke is unprotected. This script
runs cosmic-ray (https://github.com/sixty-north/cosmic-ray) over only the
lines a change touches in the modules listed in TARGETS, runs only the test
files that exercise each one, and prints a Markdown report of every surviving
mutant the ledger in .mutation-survivors.json has not already accepted.

It mutates the files in place, one mutant at a time, and cosmic-ray restores
each file afterwards. Run it in a checkout you are not editing.

Exit status: 0 when no unaccepted mutant survives, 1 when one does, 2 when
cosmic-ray itself fails.
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / ".mutation-survivors.json"

# Each module this script can mutate, and the test modules (under tests/,
# imported the way `unittest discover -s tests` imports them) that exercise
# it. A root module's list is every test file that imports it; the hook is
# loaded by path, so its list is the two files that drive it.
# tests/test_mutation_check.py derives both from the repository and fails
# when this table falls behind.
TARGETS = {
    ".claude/hooks/gate_git_diff.py": ("test_git_diff_gate", "test_gate_refusal_corpus"),
    "atomic_image_builder.py": (
        "test_architecture_doc",
        "test_atomic_image_builder",
        "test_brew_login_fragments",
        "test_brew_setup_staging",
        "test_homebrew_formula",
        "test_homebrew_release_workflow",
        "test_issue_templates",
        "test_maintenance_audit",
        "test_roadmap_doc",
        "test_using_doc",
        "test_variant_boot_test",
        "test_workflow_dependencies",
    ),
    "coverage_badge.py": ("test_ci_coverage_workflow", "test_coverage_badge", "test_metrics_doc", "test_strategy_doc"),
    "format_markdown_tables.py": ("test_architecture_doc", "test_atomic_image_builder", "test_format_markdown_tables"),
    "homebrew_formula.py": ("test_homebrew_formula", "test_homebrew_release_workflow", "test_network_error_contract"),
    "maintenance_audit.py": (
        "test_architecture_doc",
        "test_correction_memory",
        "test_maintenance_audit",
        "test_network_error_contract",
        "test_security_ai_doc",
        "test_snapshot_drift_issue",
    ),
    "mutation_check.py": ("test_mutation_check",),
    "snapshot_drift_issue.py": ("test_snapshot_drift_issue",),
    "variant_boot_test.py": ("test_variant_boot_test",),
}

# cosmic-ray's core operators replace each binary operator with every other
# one and each comparison with every other one. Most of those replacements
# raise on the first call and say nothing a single swap would not, while
# multiplying the run time by ten. Keep one swap per binary operator and the
# boundary and negation swaps per comparison; skip every other one.
KEPT_SWAPS = {
    "ReplaceBinaryOperator": {
        "Add_Sub", "Sub_Add", "Mul_Div", "Div_Mul", "FloorDiv_Div", "Mod_FloorDiv",
        "Pow_Mul", "LShift_RShift", "RShift_LShift", "BitOr_BitAnd", "BitAnd_BitOr",
        "BitXor_BitOr",
    },
    "ReplaceComparisonOperator": {
        "Eq_NotEq", "NotEq_Eq", "Lt_LtE", "Lt_GtE", "LtE_Lt", "LtE_Gt", "Gt_GtE",
        "Gt_LtE", "GtE_Gt", "GtE_Lt", "Is_IsNot", "IsNot_Is", "In_NotIn", "NotIn_In",
    },
}

TIMEOUT_SECONDS = 120.0
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def kept_operator(name: str) -> bool:
    """Whether a mutation by the cosmic-ray operator `name` is worth running."""
    family, _, swap = name.removeprefix("core/").partition("_")
    return family not in KEPT_SWAPS or swap in KEPT_SWAPS[family]


def changed_lines(diff: str) -> set[int]:
    """The new-side line numbers a `git diff -U0` of one file adds or changes."""
    lines = set()
    for line in diff.splitlines():
        match = HUNK.match(line)
        if match:
            start = int(match.group(1))
            count = 1 if match.group(2) is None else int(match.group(2))
            lines.update(range(start, start + count))
    return lines


def annotation_spans(source: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """The (start, end) positions of every type annotation in `source`.

    A mutant inside an annotation is equivalent under `from __future__ import
    annotations`, which every module here uses: the annotation is never
    evaluated. Without that import it fails at import time, which every test
    run notices, so skipping it there loses nothing either.
    """
    spans = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations = [node.returns]
        elif isinstance(node, ast.arg):
            annotations = [node.annotation]
        elif isinstance(node, ast.AnnAssign):
            annotations = [node.annotation]
        else:
            continue
        spans.extend(
            ((a.lineno, a.col_offset), (a.end_lineno, a.end_col_offset)) for a in annotations if a is not None
        )
    return spans


def worth_running(mutation, lines: set[int], spans) -> bool:
    """Whether one cosmic-ray mutation touches a changed line and can matter."""
    start, end = tuple(mutation.start_pos), tuple(mutation.end_pos)
    return (
        kept_operator(mutation.operator_name)
        and not lines.isdisjoint(range(start[0], end[0] + 1))
        and not any(low <= start and end <= high for low, high in spans)
    )


def skip_unwanted(session: Path, lines: set[int], spans) -> None:
    """Mark every pending mutant that is not worth running as skipped."""
    from cosmic_ray.work_db import WorkDB, use_db
    from cosmic_ray.work_item import WorkerOutcome, WorkResult

    with use_db(str(session), WorkDB.Mode.open) as work_db:
        skipped = [
            item.job_id
            for item in work_db.pending_work_items
            if not all(worth_running(m, lines, spans) for m in item.mutations)
        ]
        if skipped:
            work_db.set_multiple_results(
                skipped, WorkResult(output="not worth running", worker_outcome=WorkerOutcome.SKIPPED)
            )


def config_text(target: str, tests: tuple[str, ...]) -> str:
    """A cosmic-ray config that mutates `target` and runs `tests` against it."""
    command = ["env", "PYTHONPATH=tests", sys.executable, "-m", "unittest", "-f", *tests]
    return "\n".join(
        [
            "[cosmic-ray]",
            f"module-path = {json.dumps(target)}",
            f"timeout = {TIMEOUT_SECONDS}",
            "excluded-modules = []",
            f"test-command = {json.dumps(' '.join(command))}",
            "",
            "[cosmic-ray.distributor]",
            'name = "local"',
            "",
        ]
    )


def load_ledger(path: Path) -> set[tuple[str, str, str]]:
    """The (module, operator, stripped source line) keys a maintainer accepted."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {(entry["module"], entry["operator"], entry["line"]) for entry in data["accepted"]}


def survivors(dump: str, root: Path) -> list[dict]:
    """Each surviving mutant in `cosmic-ray dump` output, keyed for the ledger."""
    found = []
    sources = {}
    for row in dump.splitlines():
        item, result = json.loads(row)
        if not result or result["test_outcome"] != "survived":
            continue
        for mutation in item["mutations"]:
            module = mutation["module_path"]
            if module not in sources:
                sources[module] = (root / module).read_text(encoding="utf-8").splitlines()
            line_number = mutation["start_pos"][0]
            found.append(
                {
                    "module": module,
                    "line_number": line_number,
                    "operator": mutation["operator_name"],
                    "line": sources[module][line_number - 1].strip(),
                }
            )
    return found


def outcomes(dump: str) -> dict[str, int]:
    """How many mutants were killed, survived, timed out, or were skipped."""
    counts = {}
    for row in dump.splitlines():
        _, result = json.loads(row)
        if result is None:
            outcome = "not run"
        elif result["worker_outcome"] == "normal":
            outcome = result["test_outcome"]
        else:
            outcome = result["worker_outcome"]
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def report(results: dict[str, tuple[dict[str, int], list[dict]]], accepted) -> tuple[str, int]:
    """Markdown for every target that ran, and how many survivors are new."""
    out = ["## Mutation check", ""]
    if not results:
        out.append("No module in the mutation table changed.")
        return "\n".join(out) + "\n", 0
    new = []
    for target, (counts, found) in results.items():
        summary = ", ".join(f"{counts[k]} {k}" for k in sorted(counts) if k != "skipped")
        out.append(f"- `{target}`: {summary or 'no mutant on a changed line'}")
        new.extend(s for s in found if (s["module"], s["operator"], s["line"]) not in accepted)
    out.append("")
    if not new:
        out.append("No surviving mutant outside the ledger.")
        return "\n".join(out) + "\n", 0
    out += [
        f"{len(new)} surviving mutant(s) are not in `.mutation-survivors.json`. "
        "Add a test that fails against each one, or add it to the ledger with the reason it cannot matter.",
        "",
        "| Where | Operator | Line |",
        "|---|---|---|",
    ]
    for s in new:
        line = s["line"].replace("|", "\\|")
        out.append(f"| `{s['module']}:{s['line_number']}` | `{s['operator']}` | {code_span(line)} |")
    return "\n".join(out) + "\n", len(new)


def code_span(text: str) -> str:
    """`text` as one Markdown code span, whatever backticks it holds.

    A survivor's line is source, and this repository's sources quote names in
    backticks all through their messages; inside a single-backtick span the
    first one ends it and the cell renders as broken code. CommonMark closes
    a span only on a backtick run as long as the one that opened it, so the
    fence is one longer than the longest run inside, and a line that starts
    or ends with a backtick is padded with a space, which the renderer strips.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    padded = f" {text} " if text.startswith("`") or text.endswith("`") else text
    return f"{fence}{padded}{fence}"


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout


def cosmic_ray(*args: str) -> str:
    return subprocess.run(
        [sys.executable, "-m", "cosmic_ray.cli", *args], check=True, capture_output=True, text=True
    ).stdout


def check(target: str, base: str, workdir: Path) -> tuple[dict[str, int], list[dict]]:
    """Mutate the lines of `target` that differ from `base`; return the outcome."""
    config = workdir / (target.replace("/", "_") + ".toml")
    session = config.with_suffix(".sqlite")
    config.write_text(config_text(target, TARGETS[target]), encoding="utf-8")
    cosmic_ray("init", str(config), str(session))
    lines = changed_lines(git("diff", "-U0", base, "--", target))
    skip_unwanted(session, lines, annotation_spans((ROOT / target).read_text(encoding="utf-8")))
    cosmic_ray("exec", str(config), str(session))
    dump = cosmic_ray("dump", str(session))
    return outcomes(dump), survivors(dump, ROOT)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="the git revision the change is compared against")
    parser.add_argument("--ledger", type=Path, default=LEDGER, help="the accepted-survivor ledger")
    args = parser.parse_args(argv)

    accepted = load_ledger(args.ledger)
    # --diff-filter=d leaves out a module the change deleted or renamed away: it no longer exists to mutate.
    changed = set(git("diff", "--name-only", "--diff-filter=d", args.base, "--", *TARGETS).splitlines())
    results = {}
    os.chdir(ROOT)
    try:
        with tempfile.TemporaryDirectory() as workdir:
            for target in sorted(changed & TARGETS.keys()):
                results[target] = check(target, args.base, Path(workdir))
    except subprocess.CalledProcessError as error:
        print(f"mutation check failed: {' '.join(error.cmd)}\n{error.stderr}", file=sys.stderr)
        return 2
    text, new = report(results, accepted)
    print(text, end="")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
