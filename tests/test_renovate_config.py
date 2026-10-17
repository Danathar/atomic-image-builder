"""Hold renovate.json to what docs/risk-tiers.md and docs/branch-protection.md say it does.

renovate.json is Tier 4 because it steers a token that writes to the
repository: the Renovate app pushes `renovate/*` branches and opens pull
requests for whatever the file lets it touch. The documents say what that is
today -- the `FROM` lines of the Dockerfiles outside `template_snapshots/`,
with every Fedora bump held on the Dependency Dashboard because Fedora tags
rawhide with the next release's number -- and name the two one-line edits
that would widen it: enabling the `github-actions` manager, or setting
`automerge`. Nothing else in the suite reads the file's contents, so each of
those edits, or dropping the Fedora hold, would pass `test` unseen.

The Fedora rule is joined to the Containerfile's own `FROM` line, so moving
the base image to another registry or name without moving the rule fails
here rather than leaving the hold matching nothing.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "renovate.json"
CONTAINERFILE = ROOT / "Containerfile"
RISK_TIERS = ROOT / "docs/risk-tiers.md"
BRANCH_PROTECTION = ROOT / "docs/branch-protection.md"

FROM_LINE = re.compile(r"^FROM\s+(\S+)", re.M)


def refuse_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    # JSON.parse keeps the last of two equal keys, so a second
    # "enabledManagers" further down would quietly replace the first.
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"renovate.json repeats {duplicates}")
    return dict(pairs)


def config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"), object_pairs_hook=refuse_duplicate_keys)


def walk(node: object, path: str = "$"):
    """Every (path, key, value) in the config, nested rules included."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


def base_image_name() -> str:
    """The Containerfile's base image without its tag or digest."""
    images = FROM_LINE.findall(CONTAINERFILE.read_text(encoding="utf-8"))
    assert len(images) == 1, f"Containerfile has {len(images)} FROM lines"
    return re.split(r"[@:](?=[^/]*$)", images[0])[0]


def fedora_rules() -> list[dict]:
    return [rule for rule in config()["packageRules"] if base_image_name() in rule.get("matchPackageNames", [])]


class ScopeTests(unittest.TestCase):
    def test_only_the_dockerfile_manager_is_enabled(self) -> None:
        # Without enabledManagers, config:recommended turns on every manager,
        # github-actions included, which writes the Tier 4 workflows.
        self.assertEqual(config().get("enabledManagers"), ["dockerfile"])

    def test_the_vendored_snapshots_are_ignored(self) -> None:
        # The snapshots are copied into generated repositories as they are;
        # the weekly audit, not Renovate, moves them.
        self.assertIn("template_snapshots/**", config().get("ignorePaths", []))

    def test_nothing_lets_the_app_merge_its_own_pull_requests(self) -> None:
        for path, key, value in walk(config()):
            with self.subTest(at=f"{path}.{key}"):
                self.assertFalse(key.lower().endswith("automerge") and value, f"{path}.{key} is {value!r}")

    def test_no_preset_brings_automerge_in(self) -> None:
        for preset in config().get("extends", []):
            with self.subTest(preset=preset):
                self.assertNotIn("automerge", preset.lower())


class FedoraHoldTests(unittest.TestCase):
    def test_one_rule_matches_the_containerfiles_base_image(self) -> None:
        self.assertEqual(len(fedora_rules()), 1, f"rules naming {base_image_name()}")

    def test_every_fedora_bump_waits_for_a_maintainer(self) -> None:
        # fedora:<next> is rawhide, so no Fedora bump may open on its own.
        for rule in fedora_rules():
            self.assertIs(rule.get("dependencyDashboardApproval"), True)

    def test_each_major_is_listed_separately(self) -> None:
        # One entry per major lets the maintainer approve the release that has
        # shipped without also approving rawhide's number.
        for rule in fedora_rules():
            self.assertIs(rule.get("separateMultipleMajor"), True)

    def test_the_rule_is_limited_to_container_images(self) -> None:
        for rule in fedora_rules():
            self.assertEqual(rule.get("matchDatasources"), ["docker"])


class DocumentJoinTests(unittest.TestCase):
    def test_the_documents_describe_the_scope_this_file_has(self) -> None:
        # Both documents tell a reviewer the bot only touches FROM lines
        # outside template_snapshots/; if the file widens, they must move too.
        for doc in (RISK_TIERS, BRANCH_PROTECTION):
            text = " ".join(doc.read_text(encoding="utf-8").split())
            with self.subTest(doc=doc.name):
                self.assertIn("`renovate.json`", text)
                self.assertIn("`FROM` lines of the Dockerfiles outside `template_snapshots/`", text)

    def test_the_risk_document_names_the_hold_and_the_widening_edits(self) -> None:
        text = " ".join(RISK_TIERS.read_text(encoding="utf-8").split())
        self.assertIn("held on the Dependency Dashboard", text)
        self.assertIn("`github-actions` manager", text)
        self.assertIn("`automerge`", text)


if __name__ == "__main__":
    unittest.main()
