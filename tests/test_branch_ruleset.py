"""Join .github/rulesets/main.json to the docs and to the workflows it relies on.

Script: tests/test_branch_ruleset.py
What: Reads the committed ruleset and checks that it keeps `main` behind a
      pull request (default branch, active, no bypass, no deletion or
      force-push, 0 approvals); that docs/branch-protection.md explains every
      rule, target and required check in the file and names none the file
      lacks; that every required check is a job that runs on every pull
      request; that no workflow pushes to `main`; that the formula workflow
      needs no secrets; and that the doc names one live ruleset id.
Why: `main` had no branch protection, so any token with `contents: write`
     could push to it, and `publish-image.yml` signs and ships whatever `main`
     holds. A pull request cannot apply a ruleset, so what can be checked here
     is the definition an admin applies from. A required check that some pull
     requests never get leaves them waiting forever, and the first fix anyone
     reaches for then is deleting the ruleset.
Goal: A bypass actor, a renamed or skipped required job, a path filter on its
      trigger, or a doc that drifts from the file fails here.

CI installs no third-party packages for the unit suite, so workflows are read
by indentation. The reader handles the shapes these workflows write and raises
on anything else rather than reading it as "no filter".
"""

import fnmatch
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULESET_PATH = ROOT / ".github" / "rulesets" / "main.json"
RULESET = json.loads(RULESET_PATH.read_text(encoding="utf-8"))
DOC = (ROOT / "docs" / "branch-protection.md").read_text(encoding="utf-8")
WORKFLOWS = ROOT / ".github" / "workflows"
GITHUB_ACTIONS_APP_ID = 15368

# Trigger keys that narrow which pull requests start a run. `branches` is
# allowed only when a plain pattern covers the default branch and no pattern
# is negated; the rest are refused.
NARROWING_FILTERS = ("paths", "paths-ignore", "branches-ignore", "types")
# Git's global options (`-C <dir>`, `-c <key=value>`, `--no-pager`) can sit
# between `git` and `push`; ci.yml already writes `git -C "$dir" push`. The
# branch a refspec updates is the part after its `:`, so `release:main`
# pushes to main and `main:release` does not.
DIRECT_PUSH_TO_MAIN = re.compile(
    r"\bgit(?:\s+-[Cc]\s+\S+|\s+--?[\w-]+(?:=\S+)?)*\s+push\b[^\n]*"
    r"\s(?:[^\s:]*:)?(?:refs/heads/)?main(?=[\s\"';]|$)",
    re.M,
)
BOLD_LEAD = re.compile(r"^- \*\*(.+?)\*\*", re.M)
BACKTICKED = re.compile(r"`([^`]+)`")


def rules() -> dict[str, dict]:
    return {rule["type"]: rule.get("parameters", {}) for rule in RULESET["rules"]}


def required_contexts() -> list[str]:
    return [check["context"] for check in rules()["required_status_checks"]["required_status_checks"]]


def doc_section(heading: str) -> str:
    """The body of `## <heading>` in docs/branch-protection.md."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", DOC, re.M | re.S)
    if match is None:
        raise AssertionError(f"docs/branch-protection.md has no `## {heading}` section")
    return match.group(1)


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def children(lines: list[str], start: int) -> tuple[list[str], str]:
    """The lines nested under lines[start], and its inline value.

    Comments and blank lines are dropped. The inline value is whatever follows
    the key's colon on its own line, so `pull_request: {branches: [x]}` is seen
    as a filter rather than as an empty mapping.
    """
    head = lines[start]
    inline = head.split(":", 1)[1].split(" #", 1)[0].strip() if ":" in head else ""
    base = indent_of(head)
    body = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if indent_of(line) <= base:
            break
        body.append(line)
    return body, inline


def keyed(lines: list[str]) -> dict[str, tuple[list[str], str]]:
    """The direct child keys of an already-sliced block, with their bodies."""
    if not lines:
        return {}
    depth = indent_of(lines[0])
    out = {}
    for index, line in enumerate(lines):
        if indent_of(line) != depth:
            if indent_of(line) < depth:
                raise AssertionError(f"inconsistent indentation at {line!r}")
            continue
        match = re.match(r"\s*([A-Za-z0-9_-]+|\"[^\"]+\"|'[^']+'):(?:\s|$)", line)
        if match is None:
            raise AssertionError(f"unsupported entry {line!r}")
        out[match.group(1).strip("'\"")] = children(lines, index)
    return out


def top_level(text: str) -> dict[str, tuple[list[str], str]]:
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    out = {}
    for index, line in enumerate(lines):
        if indent_of(line) == 0 and line != "---":
            key = line.split(":", 1)[0].strip("'\"")
            out[key] = children(lines, index)
    return out


def pull_request_trigger(text: str) -> tuple[bool, list[str]]:
    """Whether a workflow runs on pull requests, and the filters that narrow it."""
    doc = top_level(text)
    if "on" not in doc:
        raise AssertionError("workflow has no top-level `on:`")
    body, inline = doc["on"]
    if inline:
        if inline.startswith("["):
            names = {item.strip().strip("'\"") for item in inline.strip("[]").split(",")}
            return "pull_request" in names, []
        if inline.startswith("{"):
            raise AssertionError(f"unsupported inline `on:` mapping {inline!r}")
        return inline.strip("'\"") == "pull_request", []
    if body and body[0].lstrip().startswith("- "):
        names = {line.strip().removeprefix("- ").strip("'\"") for line in body}
        return "pull_request" in names, []
    triggers = keyed(body)
    if "pull_request" not in triggers:
        return False, []
    pr_body, pr_inline = triggers["pull_request"]
    if pr_inline and pr_inline not in ("{}", "null", "~"):
        return True, [f"inline value {pr_inline!r}"]
    narrowing = []
    for key, (value_body, value_inline) in keyed(pr_body).items():
        if key == "branches":
            patterns = [item.strip().strip("'\"") for item in value_inline.strip("[]").split(",") if item.strip()]
            patterns += [line.strip().removeprefix("- ").strip("'\"") for line in value_body]
            # GitHub reads branch patterns in order and a `!` pattern removes
            # what earlier ones matched, so `['**', '!main']` excludes main.
            if any(pattern.startswith("!") for pattern in patterns):
                narrowing.append("a negated branch pattern")
            elif not any(fnmatch.fnmatchcase("main", pattern) for pattern in patterns):
                narrowing.append("branches without main")
        elif key in NARROWING_FILTERS:
            narrowing.append(key)
        else:
            raise AssertionError(f"unknown pull_request key {key!r}")
    return True, narrowing


def jobs(text: str) -> dict[str, dict[str, tuple[list[str], str]]]:
    """Each job id mapped to its direct keys."""
    body, _ = top_level(text).get("jobs", ([], ""))
    return {job: keyed(job_body) for job, (job_body, _) in keyed(body).items()}


def reported_name(job_id: str, keys: dict[str, tuple[list[str], str]]) -> str:
    """The check name a job reports under: its `name:` if set, else its id."""
    if "name" in keys:
        return keys["name"][1].strip("'\"")
    return job_id


def skip_reasons(job_id: str, all_jobs: dict, seen: frozenset = frozenset()) -> list[str]:
    """Why a job might not run on a pull request its workflow starts for.

    A job skips on its own `if:`, reports under a suffixed name with a matrix,
    and skips when any job it `needs:` skips.
    """
    keys = all_jobs[job_id]
    reasons = []
    if "if" in keys:
        reasons.append(f"`{job_id}` has an `if:`")
    if "strategy" in keys:
        reasons.append(f"`{job_id}` has a `strategy:`, and a matrix renames its check")
    if "needs" in keys:
        body, inline = keys["needs"]
        needed = [item.strip().strip("'\"") for item in inline.strip("[]").split(",") if item.strip()]
        needed += [line.strip().removeprefix("- ").strip("'\"") for line in body]
        for need in needed:
            if need in seen or need not in all_jobs:
                reasons.append(f"`{job_id}` needs `{need}`, which could not be followed")
                continue
            reasons.extend(skip_reasons(need, all_jobs, seen | {job_id}))
    return reasons


def providers(context: str) -> dict[str, list[str]]:
    """Every workflow job reporting as `context`, mapped to why it might not run."""
    found = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        on_pull_request, narrowing = pull_request_trigger(text)
        if not on_pull_request:
            continue
        all_jobs = jobs(text)
        for job_id, keys in all_jobs.items():
            if reported_name(job_id, keys) == context:
                reasons = [f"its pull_request trigger has {item}" for item in narrowing]
                found[f"{path.name}/{job_id}"] = reasons + skip_reasons(job_id, all_jobs)
    return found


class RulesetTests(unittest.TestCase):
    def test_the_ruleset_keeps_the_default_branch_behind_a_pull_request(self) -> None:
        self.assertEqual(RULESET["target"], "branch")
        self.assertEqual(RULESET["enforcement"], "active")
        self.assertEqual(RULESET["conditions"]["ref_name"], {"include": ["~DEFAULT_BRANCH"], "exclude": []})
        # A bypass for Actions or an App hands back the direct push.
        self.assertEqual(RULESET["bypass_actors"], [])
        self.assertEqual(
            sorted(rules()),
            ["deletion", "non_fast_forward", "pull_request", "required_status_checks"],
        )
        # Nobody can approve their own pull request, so on a single-maintainer
        # repository 1 would stop everything merging.
        self.assertEqual(rules()["pull_request"]["required_approving_review_count"], 0)

    def test_every_required_check_comes_from_github_actions(self) -> None:
        checks = rules()["required_status_checks"]["required_status_checks"]
        self.assertTrue(checks, "the ruleset requires no check")
        for check in checks:
            with self.subTest(context=check["context"]):
                self.assertEqual(check["integration_id"], GITHUB_ACTIONS_APP_ID)

    def test_every_required_check_is_a_job_every_pull_request_gets(self) -> None:
        for context in required_contexts():
            with self.subTest(context=context):
                found = providers(context)
                self.assertTrue(
                    found,
                    f"no workflow that runs on pull requests has a job reporting as {context!r}; "
                    "every pull request would wait for it forever",
                )
                unconditional = [job for job, reasons in found.items() if not reasons]
                self.assertTrue(
                    unconditional,
                    f"every job reporting as {context!r} can be skipped on some pull request: {found}",
                )


class WorkflowReaderTests(unittest.TestCase):
    """The reader is what the check above trusts; a shape it misreads as
    unfiltered would let a path filter through."""

    def test_a_bare_trigger_is_unfiltered(self) -> None:
        self.assertEqual(pull_request_trigger("on:\n  push:\n  pull_request:\n"), (True, []))
        self.assertEqual(pull_request_trigger("on: [push, pull_request]\n"), (True, []))
        self.assertEqual(pull_request_trigger("on:\n  - pull_request\n"), (True, []))

    def test_a_path_filter_is_seen(self) -> None:
        text = "on:\n  pull_request:\n    # docs skip CI\n    paths-ignore:\n      - '**.md'\n"
        self.assertEqual(pull_request_trigger(text), (True, ["paths-ignore"]))

    def test_an_inline_filter_is_seen(self) -> None:
        on_pull_request, narrowing = pull_request_trigger("on:\n  pull_request: {paths: [src/**]}\n")
        self.assertTrue(on_pull_request)
        self.assertTrue(narrowing)

    def test_a_branch_filter_is_allowed_only_with_main(self) -> None:
        self.assertEqual(pull_request_trigger("on:\n  pull_request:\n    branches: [main]\n"), (True, []))
        self.assertEqual(pull_request_trigger("on:\n  pull_request:\n    branches: ['**']\n"), (True, []))
        self.assertEqual(
            pull_request_trigger("on:\n  pull_request:\n    branches:\n      - release\n"),
            (True, ["branches without main"]),
        )

    def test_a_negated_branch_pattern_is_seen_even_after_main(self) -> None:
        self.assertEqual(
            pull_request_trigger("on:\n  pull_request:\n    branches: ['**', '!main']\n"),
            (True, ["a negated branch pattern"]),
        )

    def test_an_unknown_trigger_key_raises(self) -> None:
        with self.assertRaises(AssertionError):
            pull_request_trigger("on:\n  pull_request:\n    pathz: [x]\n")

    def test_a_skipped_dependency_skips_the_job(self) -> None:
        text = (
            "jobs:\n"
            "  lint:\n"
            "    if: github.event_name == 'push'\n"
            "    runs-on: x\n"
            "  test:\n"
            "    needs: [lint]\n"
            "    runs-on: x\n"
        )
        self.assertTrue(skip_reasons("test", jobs(text)))

    def test_a_name_override_changes_the_reported_check(self) -> None:
        keys = jobs("jobs:\n  test:\n    name: Unit tests\n    runs-on: x\n")["test"]
        self.assertEqual(reported_name("test", keys), "Unit tests")


class DocTests(unittest.TestCase):
    def doc_leads(self) -> list[str]:
        return BOLD_LEAD.findall(doc_section("The ruleset"))

    def test_the_doc_explains_every_rule_the_file_has_and_no_other(self) -> None:
        explained = {literal for lead in self.doc_leads() for literal in BACKTICKED.findall(lead)}
        in_file = set(rules()) | set(required_contexts()) | set(RULESET["conditions"]["ref_name"]["include"])
        self.assertEqual(explained, in_file)

    def test_the_doc_states_the_bypass_list_and_approval_count_the_file_has(self) -> None:
        leads = " ".join(self.doc_leads())
        self.assertEqual("No bypass actors." in leads, RULESET["bypass_actors"] == [])
        approvals = re.search(r"`pull_request` with (\d+) approvals?", leads)
        self.assertIsNotNone(approvals, "the doc no longer says how many approvals pull_request needs")
        self.assertEqual(int(approvals.group(1)), rules()["pull_request"]["required_approving_review_count"])

    def test_the_doc_says_how_to_apply_and_check_it(self) -> None:
        self.assertIn("(../.github/rulesets/main.json)", DOC)
        self.assertIn("gh api --method POST repos/Danathar/atomic-image-builder/rulesets", DOC)
        self.assertIn("--input .github/rulesets/main.json", DOC)
        self.assertIn("gh api repos/Danathar/atomic-image-builder/branches/main --jq .protected", DOC)

    def test_no_workflow_pushes_to_main(self) -> None:
        # The ruleset has no bypass, so it refuses these pushes, the job
        # fails, and for the formula workflow `brew upgrade` keeps installing
        # the previous release. The formula change goes through a pull request
        # instead; see the doc's "How a release reaches main".
        pushers = [
            path.name
            for path in sorted(WORKFLOWS.glob("*.y*ml"))
            if DIRECT_PUSH_TO_MAIN.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(pushers, [], "these workflows push straight to main, which the ruleset refuses")

    def test_the_formula_workflow_needs_no_secrets(self) -> None:
        # The doc tells an admin there is nothing to set up for a release. A
        # secret the workflow starts reading would fail the next release on a
        # repository where nobody created it.
        workflow = "update-homebrew-formula.yml"
        text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"\bsecrets\.\w+", text), [])
        flow = doc_section("How a release reaches main")
        self.assertIn(f"(../.github/workflows/{workflow})", flow)
        self.assertIn("needs no secrets", " ".join(flow.split()))

    def test_the_doc_names_one_ruleset_id(self) -> None:
        # The id in Status is what someone checks the live ruleset against,
        # and the PUT command is how the file reaches it. Two different ids
        # would send an update to a ruleset that is not the one described.
        status = re.search(r"as ruleset `(\d+)`", doc_section("Status"))
        self.assertIsNotNone(status, "the Status section no longer names the live ruleset's id")
        ids = set(re.findall(r"repos/Danathar/atomic-image-builder/rulesets/(\d+)", DOC))
        ids |= set(re.findall(r"id `(\d+)`", DOC))
        self.assertEqual(ids, {status.group(1)})

    def test_the_push_detector_sees_the_forms_a_workflow_would_write(self) -> None:
        for command in (
            "git push origin HEAD:main",
            "git push origin main",
            "git push -f origin refs/heads/main",
            'git -C "$worktree" push origin HEAD:main',
            "git -c http.extraheader=x --no-pager push origin main",
            "git push origin release:main",
            "git push origin +HEAD:refs/heads/main",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(DIRECT_PUSH_TO_MAIN.search(command))
        self.assertIsNone(DIRECT_PUSH_TO_MAIN.search("git push origin HEAD:coverage-data"))
        self.assertIsNone(DIRECT_PUSH_TO_MAIN.search('git -C "$badge_worktree" push origin HEAD:coverage-data'))
        self.assertIsNone(DIRECT_PUSH_TO_MAIN.search("git push origin main:release"))
        self.assertIsNone(DIRECT_PUSH_TO_MAIN.search('git push --force origin "HEAD:refs/heads/formula/${TAG}"'))

    def test_the_risk_tiers_route_a_ruleset_change_to_tier_four(self) -> None:
        tiers = (ROOT / "docs" / "risk-tiers.md").read_text(encoding="utf-8")
        tier4 = tiers.split("## Tier 4", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`.github/rulesets/**`", tier4)


if __name__ == "__main__":
    unittest.main()
