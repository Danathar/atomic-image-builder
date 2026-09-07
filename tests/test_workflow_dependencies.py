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
            "url": match.group("url"),
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

    def test_every_release_url_is_in_a_recognised_download_form(self) -> None:
        # Everything below keys on `curl ... -o <dest> <url>`. Written any
        # other way -- the URL before the flag, or --output instead of -o --
        # a download becomes invisible to the parser and passes by never
        # being seen, which is precisely the failure this module exists to
        # prevent. So the parser has to account for every release URL in the
        # file and say so when it cannot, rather than quietly finding fewer
        # downloads than there are.
        unparsed = []
        for path in _workflows():
            parsed = {entry["url"] for entry in _downloads(path).values()}
            for match in _RELEASE_URL.finditer(_joined(path)):
                if match.group(0) not in parsed:
                    unparsed.append(f"{path.name}: {match.group(0)}")
        self.assertEqual(
            unparsed,
            [],
            "release download not written as `curl ... -o <dest> <url>`, so the "
            "checks in this class cannot see it",
        )

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
                if entry["tag"].removeprefix("v") not in contributing
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


# `curl … -o <dest> <url>` and the `"<digest>  <dest>"` line pinning it, in a
# Containerfile rather than a workflow. Written separately from the workflow
# patterns above because the Containerfile records its digests through a file
# (`echo "…" > f` / `printf '%s\n' "…" > f`, then `sha256sum -c f`) rather
# than through the pipeline the workflows use -- see the comment on the cosign
# install for why a pipeline is the wrong shape there.
_CF_CURL = re.compile(r"curl\s[^\n]*?-o\s+(?P<dest>\S+)\s+(?P<url>https?://\S+)")
_CF_DIGEST = re.compile(r'"(?P<digest>[0-9a-f]{64})\s+(?P<dest>[^"\s]+)"')
# A repo file's `gpgkey=`, a bare `rpm --import`, and dnf's own repo-file
# fetch: the three ways a signing key or the configuration naming it can enter
# the image from the network.
_GPGKEY = re.compile(r"gpgkey=(?P<target>\S+?)\\n")
_RPM_IMPORT = re.compile(r"rpm\s+--import\s+(?P<targets>[^&|\n]+)")
_REPOFILE = re.compile(r"--from-repofile=(?P<url>\S+)")

_CONTAINERFILES = ("Containerfile", "container/Containerfile.coverage")


def _containerfile(name: str) -> str:
    """The file's instructions, comments dropped and continuations folded.

    Comments go first because these checks look for URLs in places a URL must
    not appear, and the comments here legitimately quote the very forms being
    forbidden -- explaining what `--from-repofile=https://…` used to do is not
    the same as doing it. A Dockerfile comment is a line whose first non-blank
    character is `#`, and none of the `printf` bodies below start with one.
    """
    lines = [
        line
        for line in (ROOT / name).read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    return re.sub(r"\\\n\s*", " ", "\n".join(lines))


class ContainerImageTrustRootTests(unittest.TestCase):
    """The published image's trust roots may not be fetched mutably.

    This is the image `contrib/aib` verifies and then hands the user's GitHub
    credential to, so what goes into it matters more than the usual. cosign
    signs the result, but a signature answers "did this project's workflow
    build this" and not "was what it built trustworthy" -- the second question
    is only answerable if every trust root the build consumes is pinned to a
    value recorded in this repository.

    `gpgcheck=1` against a `gpgkey=` URL on the package origin is the specific
    trap: it proves whoever served the package also served the key, which is
    not a second opinion at all. These read the file rather than building it,
    so they run in the ordinary unit suite with no network and no podman.
    """

    def test_every_containerfile_download_is_checksum_verified(self) -> None:
        # Tied by destination path, not by proximity: the realistic mistake
        # when adding the next pinned download is copying the block above and
        # leaving the previous file name in the checksum line, which reads
        # fine and verifies the wrong file -- or nothing.
        unverified = []
        for name in _CONTAINERFILES:
            text = _containerfile(name)
            digests = {m.group("dest") for m in _CF_DIGEST.finditer(text)}
            for match in _CF_CURL.finditer(text):
                dest = match.group("dest").strip("\"'")
                if dest not in digests:
                    unverified.append(f"{name}: {match.group('url')} -> {dest}")
        self.assertEqual(
            unverified,
            [],
            "downloaded into the published image with no sha256 recorded "
            "against its destination path",
        )

    def test_every_recorded_digest_is_actually_checked(self) -> None:
        # A digest written to a file nothing runs `sha256sum -c` against is
        # documentation, not verification, and looks identical in review.
        for name in _CONTAINERFILES:
            text = _containerfile(name)
            with self.subTest(containerfile=name):
                if not _CF_DIGEST.search(text):
                    continue
                self.assertIn(
                    "sha256sum -c",
                    text,
                    "digests are recorded but never verified",
                )

    def test_no_signing_key_is_trusted_straight_off_the_network(self) -> None:
        # The whole of the fix for #268: a key has to come from a file this
        # build already checked, never from a URL resolved at install time.
        offenders = []
        for name in _CONTAINERFILES:
            text = _containerfile(name)
            for match in _GPGKEY.finditer(text):
                target = match.group("target")
                if not target.startswith("file://"):
                    offenders.append(f"{name}: gpgkey={target}")
            for match in _RPM_IMPORT.finditer(text):
                for target in match.group("targets").split():
                    if target.startswith(("http://", "https://")):
                        offenders.append(f"{name}: rpm --import {target}")
        self.assertEqual(
            offenders,
            [],
            "a signing key is fetched over the network instead of being read "
            "from a checksum-verified local copy",
        )

    def test_no_repository_configuration_is_fetched_at_build_time(self) -> None:
        # A .repo file pulled from the network carries its own gpgkey= line,
        # so fetching the configuration hands away the choice of trust root
        # even when every key in the image is otherwise pinned.
        offenders = []
        for name in _CONTAINERFILES:
            for match in _REPOFILE.finditer(_containerfile(name)):
                if match.group("url").startswith(("http://", "https://")):
                    offenders.append(f"{name}: --from-repofile={match.group('url')}")
        self.assertEqual(offenders, [], "repository configuration is fetched at build time")

    def test_the_guards_above_can_see_the_file_they_read(self) -> None:
        # Every check here passes vacuously on an empty or renamed file, and
        # two of them pass vacuously on a file that simply has no repos. Assert
        # the fixture: the published image really does configure two
        # third-party repositories and really does pin three downloads.
        text = _containerfile("Containerfile")
        self.assertEqual(len(_GPGKEY.findall(text)), 2, "expected the charm and gh-cli repos")
        self.assertEqual(len(_CF_CURL.findall(text)), 3, "expected two keys and the cosign RPM")
        self.assertEqual(len(_CF_DIGEST.findall(text)), 3)
