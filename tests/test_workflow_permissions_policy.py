"""Hold every workflow's token permissions to .github/policies/workflow-permissions.json.

Script: tests/test_workflow_permissions_policy.py
What: Reads each workflow's top-level and job-level `permissions:` blocks and
      compares them with the policy file, in both directions: every workflow is
      listed and every listed workflow exists; each declared block matches the
      policy exactly; the jobs that declare a block are exactly the jobs the
      policy lists.
Why: A workflow's permissions decide what its GITHUB_TOKEN can do -- push to
     `main`, publish a package, mint an OIDC token for signing. The only record
     of what each one is meant to hold was the workflow itself, so widening a
     token was a one-line edit inside a file a reviewer may read for the step
     that changed. docs/SECURITY-AI.md says "a workflow needing more declares
     it explicitly"; this makes the declaration a second, separate edit.
Goal: A workflow that asks for one more scope fails here until the policy
      changes in the same pull request. The policy file is Tier 4 in
      docs/risk-tiers.md, beside the release workflows.

CI installs no third-party packages for the unit suite, so the blocks are
sliced out by indentation. The parser is checked against block, inline and
job-level shapes below, so a block it cannot read fails as a mismatch rather
than passing as "no permissions".
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
POLICY_PATH = ROOT / ".github" / "policies" / "workflow-permissions.json"
POLICY = json.loads(POLICY_PATH.read_text(encoding="utf-8"))["workflows"]

# The scopes GitHub accepts in a permissions block. Actions ignores a misspelt
# scope without complaint, so the policy is refused one here instead.
SCOPES = {
    "actions",
    "attestations",
    "checks",
    "contents",
    "deployments",
    "discussions",
    "id-token",
    "issues",
    "models",
    "packages",
    "pages",
    "pull-requests",
    "repository-projects",
    "security-events",
    "statuses",
}
LEVELS = {"read", "write", "none"}

KEY = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_-]+):\s*(?P<value>[^#]*?)\s*(?:#.*)?$")


def permissions_block(lines: list[str], indent: int) -> dict[str, str] | str | None:
    """The `permissions:` value written at *indent* spaces in *lines*.

    A mapping for a block, the raw string for an inline value (`read-all`,
    `{}`), or None when no such key sits at that indent. Callers pass a whole
    file for indent 0 and one job's lines for indent 4.
    """
    for i, line in enumerate(lines):
        match = KEY.match(line)
        if not match or len(match["indent"]) != indent or match["key"] != "permissions":
            continue
        if match["value"]:
            return match["value"]
        block: dict[str, str] = {}
        for entry in lines[i + 1 :]:
            if not entry.strip() or entry.lstrip().startswith("#"):
                continue
            inner = KEY.match(entry)
            if not inner or len(inner["indent"]) <= indent:
                break
            block[inner["key"]] = inner["value"]
        return block
    return None


def job_lines(lines: list[str]) -> dict[str, list[str]]:
    """Each job under the top-level `jobs:` key, as the lines of its block."""
    starts = [i for i, line in enumerate(lines) if line.rstrip() == "jobs:"]
    if len(starts) != 1:
        raise AssertionError("expected exactly one top-level jobs: key")
    jobs: dict[str, list[str]] = {}
    current = None
    for line in lines[starts[0] + 1 :]:
        if line.strip() and not line.startswith(" "):
            break
        match = KEY.match(line)
        if match and len(match["indent"]) == 2 and not match["value"]:
            current = match["key"]
            jobs[current] = []
            continue
        if current is not None:
            jobs[current].append(line)
    return jobs


def declared(text: str) -> dict[str, object]:
    """What a workflow declares: its top-level block and each job's own block."""
    lines = text.splitlines()
    jobs = {}
    for job, body in job_lines(lines).items():
        block = permissions_block(body, 4)
        if block is not None:
            jobs[job] = block
    return {"workflow": permissions_block(lines, 0), "jobs": jobs}


def workflow_files() -> list[Path]:
    return sorted(p for p in WORKFLOWS.iterdir() if p.suffix in {".yml", ".yaml"})


class PolicyMatchesWorkflowsTests(unittest.TestCase):
    def test_every_workflow_is_in_the_policy_and_every_entry_is_a_workflow(self) -> None:
        present = {p.name for p in workflow_files()}
        self.assertTrue(present, f"no workflow files under {WORKFLOWS}")
        self.assertEqual(
            present,
            set(POLICY),
            f"not in the policy: {sorted(present - set(POLICY))}; "
            f"in the policy but no such workflow: {sorted(set(POLICY) - present)}",
        )

    def test_each_workflow_declares_exactly_what_the_policy_allows(self) -> None:
        for path in workflow_files():
            with self.subTest(workflow=path.name):
                found = declared(path.read_text(encoding="utf-8"))
                expected = POLICY[path.name]
                self.assertEqual(
                    found["workflow"],
                    expected["workflow"],
                    f"{path.name}'s top-level permissions are {found['workflow']}; "
                    f"{POLICY_PATH.name} allows {expected['workflow']}. Change both, or neither.",
                )
                self.assertEqual(
                    found["jobs"],
                    expected["jobs"],
                    f"{path.name}'s job-level permissions are {found['jobs']}; "
                    f"{POLICY_PATH.name} allows {expected['jobs']}. Change both, or neither.",
                )

    def test_the_policy_names_only_real_scopes_and_levels(self) -> None:
        for name, entry in POLICY.items():
            for block in [entry["workflow"], *entry["jobs"].values()]:
                if block is None:
                    continue
                for scope, level in block.items():
                    with self.subTest(workflow=name, scope=scope):
                        self.assertIn(scope, SCOPES, f"{name}: {scope!r} is not a GitHub token scope")
                        self.assertIn(level, LEVELS, f"{name}: {scope} has level {level!r}")

    def test_every_workflow_declares_permissions_somewhere(self) -> None:
        # If the parser stopped seeing blocks, both sides could read as
        # "nothing declared" and agree. Every workflow here declares at least
        # one block today, at the top or on a job.
        for name, entry in POLICY.items():
            with self.subTest(workflow=name):
                self.assertTrue(entry["workflow"] or entry["jobs"], f"{name} declares no permissions")


class ParserTests(unittest.TestCase):
    def test_a_top_level_block_with_a_trailing_comment(self) -> None:
        text = "permissions:\n  contents: read\n  issues: write  # why\n\njobs:\n  a:\n    runs-on: x\n"
        self.assertEqual(declared(text), {"workflow": {"contents": "read", "issues": "write"}, "jobs": {}})

    def test_an_inline_top_level_value_and_a_job_block(self) -> None:
        text = "permissions: read-all\njobs:\n  a:\n    permissions:\n      contents: write\n    steps: []\n"
        self.assertEqual(declared(text), {"workflow": "read-all", "jobs": {"a": {"contents": "write"}}})

    def test_an_empty_job_block_and_a_job_without_one(self) -> None:
        text = "on: push\njobs:\n  a:\n    permissions: {}\n  b:\n    runs-on: x\n"
        self.assertEqual(declared(text), {"workflow": None, "jobs": {"a": "{}"}})

    def test_a_step_input_named_permissions_is_not_a_block(self) -> None:
        text = "jobs:\n  a:\n    steps:\n      - with:\n          permissions: write\n"
        self.assertEqual(declared(text), {"workflow": None, "jobs": {}})

    def test_a_widened_job_is_caught(self) -> None:
        # The regression this module exists for: one more scope on a job.
        text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        widened = text.replace("    permissions:\n      contents: read\n", "    permissions:\n      contents: read\n      packages: write\n", 1)
        self.assertNotEqual(widened, text, "ci.yml no longer has the job block this case widens")
        self.assertNotEqual(declared(widened)["jobs"], POLICY["ci.yml"]["jobs"])


if __name__ == "__main__":
    unittest.main()
