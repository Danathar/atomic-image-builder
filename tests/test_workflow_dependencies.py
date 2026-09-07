import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkflowDependencyTests(unittest.TestCase):
    def test_python_ci_tool_installs_are_exactly_pinned(self) -> None:
        expected_commands = {
            ".github/workflows/ai-fix.yml": [
                "pip install coverage==7.16.0 ruff==0.16.5",
            ],
            ".github/workflows/ci.yml": [
                "pip install coverage==7.16.0 ruff==0.16.5",
                "pip install coverage==7.16.0",
            ],
            ".github/workflows/maintenance-audit.yml": [
                "pip install coverage==7.16.0",
            ],
            ".github/workflows/nightly-compliance.yml": [
                "pip install coverage==7.16.0",
            ],
            ".github/workflows/update-homebrew-formula.yml": [
                "pip install coverage==7.16.0",
            ],
        }

        actual_commands = {}
        workflow_dir = ROOT / ".github/workflows"
        for workflow_path in sorted(workflow_dir.iterdir()):
            if workflow_path.suffix not in {".yml", ".yaml"}:
                continue
            commands = []
            for line in workflow_path.read_text().splitlines():
                command = line.strip()
                if command.startswith("run: pip install "):
                    command = command.removeprefix("run: ")
                if command.startswith("pip install "):
                    commands.append(command)
            if commands:
                relative_path = str(workflow_path.relative_to(ROOT))
                actual_commands[relative_path] = commands

        self.assertEqual(actual_commands, expected_commands)


# Tools fetched as release binaries rather than installed from a package
# index. Keyed by the GitHub repository they come from, which is what makes
# two workflows fetching "the same tool" comparable.
_RELEASE_URL = re.compile(
    r"https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/releases/download/(?P<tag>[^/]+)/(?P<asset>\S+)"
)
# `curl … -o <dest> <url>`, after backslash continuations are joined.
_CURL = re.compile(r"curl\s[^\n]*?-o\s+(?P<dest>\S+)\s+(?P<url>https://\S+)")
# `echo "<digest>  <path>" | sha256sum -c -`
_CHECKSUM = re.compile(r'(?P<digest>[0-9a-f]{64})\s+(?P<path>[^"\n]+)"\s*\|\s*sha256sum\s+-c')

# Tools the unit suite skips tests without, so a workflow that runs the suite
# without them reports a weaker result under the same name. bash is left out
# deliberately: every runner has one and there is nothing in a workflow to
# assert about it.
UNIT_SUITE_TOOLS = {"rhysd/actionlint", "casey/just"}
UNIT_SUITE = "unittest discover -s tests"


def _joined(path: Path) -> str:
    """Workflow text with backslash continuations folded onto one line.

    `curl -o <dest>` and its URL are written on separate lines, so nothing
    below can match until they are joined.
    """
    return re.sub(r"\\\n\s*", " ", path.read_text())


def _workflows() -> list[Path]:
    return sorted(
        path
        for path in (ROOT / ".github/workflows").iterdir()
        if path.suffix in {".yml", ".yaml"}
    )


def _downloads(path: Path) -> dict[str, dict[str, str]]:
    """Release downloads in one workflow, keyed by source repository."""
    text = _joined(path)
    verified = {
        match.group("path").strip(): match.group("digest")
        for match in _CHECKSUM.finditer(text)
    }
    found: dict[str, dict[str, str]] = {}
    for match in _CURL.finditer(text):
        url = _RELEASE_URL.match(match.group("url"))
        if url is None:
            continue
        dest = match.group("dest").strip('"\'')
        found[url.group("repo")] = {
            "dest": dest,
            "tag": url.group("tag"),
            "digest": verified.get(dest, ""),
        }
    return found


class ReleaseBinaryPinTests(unittest.TestCase):
    """Three linters and a task runner are fetched by URL and checksum.

    `pip install` pins are covered above and by a test that reads ci.yml's own
    version string. These are not: hadolint, actionlint and just are curled
    from a release page, and until this existed a fourth added without a
    checksum -- or with one that verified the wrong file -- passed every check
    in the repo. Two of them are also fetched by more than one workflow, so
    bumping a version in one place and not the other passed too. That is the
    same shape as #112: a pinned dependency is not pinned if only some of it
    is.
    """

    def test_every_release_download_is_checksum_verified(self) -> None:
        # Tied by destination path rather than by proximity, because the
        # realistic mistake when adding the next tool is copying the block
        # above it and leaving the previous file name in the checksum line.
        # That reads fine and verifies nothing.
        unverified = [
            f"{path.name}: {repo} -> {entry['dest']}"
            for path in _workflows()
            for repo, entry in _downloads(path).items()
            if not entry["digest"]
        ]
        self.assertEqual(unverified, [], "release downloads with no matching sha256sum -c")

    def test_release_pins_agree_across_workflows(self) -> None:
        # hadolint and actionlint are fetched by both ci.yml and ai-fix.yml,
        # whose comment says it pins "the same versions and checksums ci.yml
        # pins". Nothing made that true until here.
        seen: dict[str, set[tuple[str, str]]] = {}
        for path in _workflows():
            for repo, entry in _downloads(path).items():
                seen.setdefault(repo, set()).add((entry["tag"], entry["digest"]))
        drifted = {repo: pins for repo, pins in seen.items() if len(pins) > 1}
        self.assertEqual(drifted, {}, "the same tool is pinned differently in different workflows")

    def test_release_versions_are_documented(self) -> None:
        # CONTRIBUTING.md tells a contributor which versions to install to
        # match CI. A version bumped in the workflow and not there sends
        # everyone to a different build than the gate runs.
        contributing = (ROOT / "CONTRIBUTING.md").read_text()
        missing = sorted(
            {
                f"{repo} {entry['tag']}"
                for path in _workflows()
                for repo, entry in _downloads(path).items()
                if entry["tag"].lstrip("v") not in contributing
            }
        )
        self.assertEqual(missing, [], "release versions absent from CONTRIBUTING.md")

    def test_workflows_running_the_unit_suite_install_what_it_needs(self) -> None:
        # Without these the suite still passes -- it skips instead, and a
        # skipped test is counted in "Ran N tests". So the same command
        # reported under the same name means different things in different
        # workflows, and nothing says so. Both tools cover generated output,
        # which is the part that reaches other people's repositories.
        short = {}
        for path in _workflows():
            if UNIT_SUITE not in path.read_text():
                continue
            absent = UNIT_SUITE_TOOLS - set(_downloads(path))
            if absent:
                short[path.name] = sorted(absent)
        self.assertEqual(short, {}, "workflow runs the unit suite without the tools it needs")
