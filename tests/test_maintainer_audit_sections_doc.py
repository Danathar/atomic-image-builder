"""Join three of MAINTAINER.md's weekly audit sections to the code they quote.

Script: tests/test_maintainer_audit_sections_doc.py
What: Reads *Advisory: a pin no longer matches its tag or branch*, *Advisory:
      the formula pin* and *Failure: the released wrapper is not `contrib/aib`*
      in maintainer_docs/MAINTAINER.md and checks what each states against the
      code that produces the message: every quoted direction is in the message
      describe_pin_drift() prints for that comparison, and the pin the section
      calls real is still in the tables; the formula sample is what
      homebrew_formula.check() prints, the step running it cannot fail the job,
      and the repair command names a real dispatch input; the wrapper sample
      and each quoted finding are what audit_wrapper_release() returns for that
      release, as failures, with an unreachable API staying an advisory, and
      the repair the section gives is the one the finding gives for each state
      of the tag.
Why: These are the sections a maintainer opens with an audit message in hand,
     and they quote that message so it can be found on the page.
     tests/test_maintenance_audit.py pins the messages, but nothing read this
     page: it quoted a direction the audit never printed and left out one of
     the findings a broken release produces.
Goal: Changing what the audit says, or which repair it names, fails here until
      the page says the same thing.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _block_yaml
import homebrew_formula
import maintenance_audit
from atomic_image_builder import ACTION_REF_PINS
from maintenance_audit import (
    WRAPPER_ASSET,
    WRAPPER_CHECKSUM_ASSET,
    WRAPPER_RELEASE_REPO,
    WRAPPER_SOURCE,
    audit_wrapper_release,
    describe_pin_drift,
)

ROOT = Path(__file__).resolve().parent.parent
MAINTAINER_PATH = ROOT / "maintainer_docs/MAINTAINER.md"
AUDIT_WORKFLOW = ROOT / ".github/workflows/maintenance-audit.yml"
FORMULA_WORKFLOW = ROOT / ".github/workflows/update-homebrew-formula.yml"
WRAPPER_WORKFLOW = ROOT / ".github/workflows/publish-wrapper.yml"

PIN_HEADING = "### Advisory: a pin no longer matches its tag or branch"
FORMULA_HEADING = "### Advisory: the formula pin"
WRAPPER_HEADING = "### Failure: the released wrapper is not `contrib/aib`"

# The bullet's opening quote -> the comparisons that bullet stands for. `None`
# is a comparison the API could not answer.
PIN_DIRECTIONS = {
    "N commit(s) newer … refresh it": [("ahead", 3, 0)],
    "N commit(s) OLDER than the pin … would downgrade": [("behind", 0, 3)],
    "diverged history": [("diverged", 2, 3)],
    "direction of the difference is unknown": [None],
}

WRAPPER = b"#!/usr/bin/env bash\ncosign verify\n"
OLDER_WRAPPER = b"#!/usr/bin/env bash\npodman run\n"


def _section(heading: str) -> str:
    doc = MAINTAINER_PATH.read_text()
    start = doc.index(heading + "\n")
    ends = [
        found
        for found in (
            doc.find("\n### ", start + len(heading)),
            doc.find("\n## ", start + len(heading)),
        )
        if found != -1
    ]
    return doc[start : min(ends)] if ends else doc[start:]


def _flat(text: str) -> str:
    """Text as a reader sees it: one line, no code-span backticks."""
    return " ".join(text.replace("`", "").split())


def _quotes(section: str) -> list[str]:
    """Every code span of a section, joined across its line wraps."""
    prose = re.sub(r"^```.*?^```$", "", section, flags=re.S | re.M)
    return [" ".join(span.split()) for span in re.findall(r"`([^`]+)`", prose)]


def _pieces(quote: str) -> list[str]:
    """A quote's literal stretches; `…` stands for text the page left out."""
    return [piece.strip() for piece in quote.split("…") if piece.strip()]


def _step(workflow: Path, name: str) -> dict:
    jobs = _block_yaml.parse(workflow.read_text())["jobs"]
    for job in jobs.values():
        for step in job["steps"]:
            if step.get("name") == name:
                return step
    raise AssertionError(f"{workflow.name} has no step named {name!r}")


def _dispatch_inputs(workflow: Path) -> dict:
    document = _block_yaml.parse(workflow.read_text())
    triggers = document.get("on", document.get(True))
    return triggers["workflow_dispatch"]["inputs"]


class PinSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.section = _section(PIN_HEADING)

    def _bullets(self) -> list[str]:
        return re.findall(r'\*"([^"]+)"\*', _flat(self.section))

    def test_the_section_quotes_every_direction_the_audit_can_report(self) -> None:
        self.assertEqual(self._bullets(), list(PIN_DIRECTIONS))

    def test_each_quoted_direction_is_in_the_message_for_that_comparison(self) -> None:
        for quote, comparisons in PIN_DIRECTIONS.items():
            for comparison in comparisons:
                with self.subTest(quote=quote, comparison=comparison):
                    with patch(
                        "maintenance_audit.query_github_comparison",
                        return_value=comparison,
                    ):
                        message = describe_pin_drift("o/a", "v8", "a" * 40, "b" * 40)
                    for piece in _pieces(quote):
                        self.assertIn(piece.replace("N ", "3 "), message)

    def test_the_direction_it_says_not_to_refresh_is_the_one_the_audit_warns_on(self) -> None:
        self.assertIn(
            'would downgrade"* — upstream moved the tag **backwards** onto an '
            "older commit. **Do not refresh.**",
            _flat(self.section),
        )
        with patch(
            "maintenance_audit.query_github_comparison",
            return_value=("behind", 0, 3),
        ):
            message = describe_pin_drift("o/a", "v8", "a" * 40, "b" * 40)
        self.assertIn("Upstream moved the tag backwards", message)
        self.assertIn("Leave the pin alone", message)

    def test_the_real_example_is_a_pin_the_audit_still_watches(self) -> None:
        stated = re.search(
            r"`([\w-]+/[\w-]+)` is pinned \d+ commits ahead of what its `(v\d+)` tag",
            " ".join(self.section.split()),
        )
        self.assertIsNotNone(stated, "the section no longer names its real example")
        action, tag = stated.groups()
        sha, label = ACTION_REF_PINS[f"{action}@{tag}"]
        self.assertEqual(label, tag)
        self.assertIn((action, tag, sha), list(maintenance_audit.iter_pinned_refs()))

    def test_a_moved_pin_is_an_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.multiple(
                "maintenance_audit",
                audit_local_snapshot=lambda _root: [],
                audit_action_update_availability=lambda: [],
                audit_action_pin_freshness=lambda: ["moved"],
                audit_container_trust_roots=lambda _root: [],
                audit_brew_image_pin=lambda: [],
                audit_disk_builder_image_pin=lambda: [],
                audit_bluebuild_cli_image_pin=lambda: [],
                audit_cosign_release=lambda: [],
                audit_wrapper_release=lambda _root: ([], []),
            ):
                findings, advisories = maintenance_audit.run_audit(
                    Path(tmp), skip_upstream=True, check_action_updates=True
                )
        self.assertEqual(findings, [])
        self.assertIn("moved", advisories)


class FormulaSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.section = _section(FORMULA_HEADING)
        stated = re.search(
            r"`Formula points at (v[\d.]+) but the tool's VERSION is ([\d.]+)`",
            self.section,
        )
        self.assertIsNotNone(stated, "the section no longer quotes the finding")
        self.quote = stated.group(0).strip("`")
        self.pinned, self.version = stated.groups()

    def test_the_sample_is_what_the_check_prints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            formula = Path(tmp) / "formula.rb"
            formula.write_text(
                f'url "{homebrew_formula.tarball_url(self.pinned)}"\n'
                f'sha256 "{"a" * 64}"\n'
            )
            with patch("homebrew_formula.fetch_sha256", return_value="a" * 64):
                findings = homebrew_formula.check(
                    formula, expected_version=self.version
                )
        self.assertEqual(len(findings), 1, findings)
        self.assertTrue(findings[0].startswith(self.quote + ". "), findings[0])

    def test_the_step_that_runs_the_check_cannot_fail_the_job(self) -> None:
        step = _step(AUDIT_WORKFLOW, "Check the Homebrew formula pin")
        self.assertIn("homebrew_formula.py --check", step["run"])
        self.assertTrue(step["run"].rstrip().endswith("|| true"), step["run"])

    def test_the_repair_dispatches_the_tag_the_finding_wants(self) -> None:
        command = re.search(
            r"^gh workflow run (\S+) -f (\w+)=(\S+)$", self.section, re.M
        )
        self.assertIsNotNone(command, "the section no longer gives the repair")
        workflow, field, tag = command.groups()
        self.assertEqual(tag, f"v{self.version}")
        self.assertIn(
            field, _dispatch_inputs(ROOT / ".github/workflows" / workflow)
        )
        self.assertEqual(workflow, FORMULA_WORKFLOW.name)

    def test_the_run_leaves_the_pull_request_to_the_person_the_section_tells(self) -> None:
        flat = " ".join(self.section.split())
        self.assertIn(
            "open the pull request from the link in its summary or reminder issue",
            flat,
        )
        self.assertIn("merge it once `test` passes", flat)
        workflow = FORMULA_WORKFLOW.read_text()
        self.assertIn("**[Open the pull request](${COMPARE_URL})**", workflow)
        self.assertIn("Merge it once \\`test\\` passes.", workflow)
        _step(FORMULA_WORKFLOW, "Open the reminder issue")


class WrapperSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.section = _section(WRAPPER_HEADING)
        self.flat = _flat(self.section)
        self.quotes = [_flat(quote) for quote in _quotes(self.section)]

    def _audit(
        self,
        tag: str,
        *,
        assets: tuple[str, ...] = (WRAPPER_ASSET, WRAPPER_CHECKSUM_ASSET),
        aib: bytes = WRAPPER,
        checksum: bytes | None = None,
        tagged: bytes = WRAPPER,
    ) -> tuple[list[str], list[str]]:
        """Audit a checkout of WRAPPER against a release shaped as asked."""
        if checksum is None:
            checksum = f"{hashlib.sha256(aib).hexdigest()}  aib\n".encode()
        served = {f"asset:{WRAPPER_ASSET}": aib, f"asset:{WRAPPER_CHECKSUM_ASSET}": checksum}
        release = (tag, {name: f"asset:{name}" for name in assets})

        def fetch(url: str) -> bytes:
            return served.get(url, tagged)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / WRAPPER_SOURCE).parent.mkdir(parents=True)
            (root / WRAPPER_SOURCE).write_bytes(WRAPPER)
            with patch(
                "maintenance_audit.query_latest_release", return_value=release
            ), patch("maintenance_audit.fetch_bytes", side_effect=fetch):
                return audit_wrapper_release(root)

    def _quoted(self, needle: str) -> str:
        found = [quote for quote in self.quotes if needle in quote]
        self.assertEqual(len(found), 1, f"the section does not quote {needle!r} once")
        return found[0]

    def assertQuoteMatches(self, quote: str, finding: str) -> None:
        position = 0
        flat = _flat(finding)
        for piece in _pieces(quote):
            found = flat.find(piece, position)
            self.assertNotEqual(found, -1, f"{piece!r} is not in: {flat}")
            position = found + len(piece)

    def test_the_sample_is_what_the_audit_fails_with(self) -> None:
        fence = re.search(r"\n```\n(.*?)\n```\n", self.section, re.S)
        self.assertIsNotNone(fence, "the section no longer shows a sample failure")
        sample = _flat(fence.group(1))
        tag = re.search(r"release (v[\d.]+)", sample).group(1)
        findings, advisories = self._audit(tag, aib=OLDER_WRAPPER)
        self.assertEqual(advisories, [], "the section calls this a failure")
        (finding,) = findings
        shape = re.sub(r"[0-9a-f]{8}…", "[0-9a-f]{64}", re.escape(sample))
        self.assertRegex(_flat(finding), "^" + shape)

    def test_every_finding_of_a_broken_release_is_quoted_and_is_a_failure(self) -> None:
        tag = "v0.9.6"
        bad = "0" * 64
        good = hashlib.sha256(WRAPPER).hexdigest()
        cases = {
            "carries no aib asset": {"assets": (WRAPPER_CHECKSUM_ASSET,)},
            "does not record a sha256": {"checksum": b"# empty\n"},
            "but the aib beside it hashes": {"checksum": f"{bad}  aib\n".encode()},
            "also lists contrib/aib": {
                "checksum": f"{good}  aib\n{good}  contrib/aib\n".encode()
            },
        }
        for needle, shape in cases.items():
            with self.subTest(needle=needle):
                findings, advisories = self._audit(tag, **shape)
                self.assertEqual(advisories, [])
                (finding,) = findings
                self.assertQuoteMatches(self._quoted(needle), finding)

    def test_a_release_with_no_checksum_is_the_case_named_in_brackets(self) -> None:
        self.assertIn(f"(or no {WRAPPER_CHECKSUM_ASSET})", self.flat)
        findings, advisories = self._audit("v0.9.6", assets=(WRAPPER_ASSET,))
        self.assertEqual(advisories, [])
        (finding,) = findings
        self.assertIn(f"carries no {WRAPPER_CHECKSUM_ASSET} asset", _flat(finding))

    def test_an_unreachable_api_stays_an_advisory(self) -> None:
        self.assertIn("Being unable to reach the API stays an advisory", self.flat)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / WRAPPER_SOURCE).parent.mkdir(parents=True)
            (root / WRAPPER_SOURCE).write_bytes(WRAPPER)
            with patch(
                "maintenance_audit.query_latest_release",
                side_effect=RuntimeError("HTTP 403"),
            ):
                findings, advisories = audit_wrapper_release(root)
        self.assertEqual(findings, [])
        self.assertEqual(len(advisories), 1)

    def test_the_dispatch_is_the_repair_only_while_the_tag_has_mains_wrapper(self) -> None:
        command = re.search(
            r"^gh workflow run (\S+) -f (\w+)=(\S+)$", self.section, re.M
        )
        self.assertIsNotNone(command, "the section no longer gives the dispatch")
        workflow, field, tag = command.groups()
        self.assertEqual(workflow, WRAPPER_WORKFLOW.name)
        self.assertIn(field, _dispatch_inputs(WRAPPER_WORKFLOW))

        (finding,), _ = self._audit(tag, assets=(WRAPPER_CHECKSUM_ASSET,))
        self.assertIn(f"`{command.group(0)}`", finding)

        (moved,), _ = self._audit(
            tag, assets=(WRAPPER_CHECKSUM_ASSET,), tagged=OLDER_WRAPPER
        )
        self.assertNotIn(command.group(0), moved)
        self.assertIn("Cut a release", moved)
        self.assertIn(hashlib.sha256(WRAPPER).hexdigest(), moved)
        self.assertIn(hashlib.sha256(OLDER_WRAPPER).hexdigest(), moved)
        self.assertIn(
            "When `contrib/aib` has changed since the tag, the finding says so and names the digests",
            " ".join(self.section.split()),
        )
        self.assertIn("Cut a release.", self.flat)

    def test_the_dispatch_attaches_the_wrapper_of_the_tag_it_is_given(self) -> None:
        self.assertIn("checks out the tag it is given", self.flat)
        checkout = _step(WRAPPER_WORKFLOW, "Checkout the release")
        self.assertIn("inputs.tag", checkout["with"]["ref"])
        stage = _step(WRAPPER_WORKFLOW, "Stage the wrapper and its checksum")
        self.assertIn(f"cp {WRAPPER_SOURCE.as_posix()} dist/{WRAPPER_ASSET}", stage["run"])

    def test_the_hand_check_fetches_what_the_audit_compares(self) -> None:
        commands = re.findall(r"^(?:curl|sha256sum) .*$", self.section, re.M)
        base = f"https://github.com/{WRAPPER_RELEASE_REPO}/releases/latest/download"
        self.assertEqual(
            commands,
            [
                f"curl -fsSL {base}/{WRAPPER_ASSET} | sha256sum",
                f"curl -fsSL {base}/{WRAPPER_CHECKSUM_ASSET}",
                f"sha256sum {WRAPPER_SOURCE.as_posix()}",
            ],
        )


if __name__ == "__main__":
    unittest.main()
