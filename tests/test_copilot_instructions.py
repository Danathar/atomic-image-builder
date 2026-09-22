"""Read `.github/copilot-instructions.md` as a subject rather than as a source.

Script: tests/test_copilot_instructions.py
What: Reads the canonical agent brief against the machine it describes --
      `.github/workflows/ci.yml`, `.coverage-thresholds.json`, `.coveragerc`,
      `.claude/settings.json`, `.claude/hooks/gate_git_diff.py`,
      `.editorconfig`, `atomic_image_builder.py`, the `.template-source`
      files under `template_snapshots/`, and the four reference documents its
      opening bullets point at.
Doing: Splits the document on its headings so a claim is checked against the
       section that makes it and cannot be satisfied by the same token
       somewhere else in the file. Parses the *Before you push* fence into
       commands and joins the linters in it to the `Run <tool>` step names of
       ci.yml's `test` job in both directions; expands the fence's
       `tests/e2e/*.sh` glob and compares the shellcheck and hadolint operand
       sets to the workflow's; matches every fence command against
       `.claude/settings.json`'s allow rows with Claude Code's own exact/`:*`
       prefix semantics, with the one command no row covers exempted by name
       and the exemption asserted in both directions. Reads the permission
       keys and the registered hook out of the settings file and requires the
       *Mechanical limits* section to name each one, and partitions the deny
       rows exhaustively so a new one has to be classified before it can be
       described or omitted. Joins the document's number words -- five
       coverage tiers, four mirrored paragraphs, both e2e suites -- to counts
       computed from the files that decide them.
Why: This file is the canonical brief every other agent-facing file in the
     repository defers to, and it was read only as a source. The no-copy and
     mirror halves of test_agent_guidance_has_one_canonical_file compare other
     documents *to* it, tests/test_verify_change_skill.py asserts the skill's
     Run block equals its fence, and tests/test_pull_request_template.py
     asserts the template's checklist is contained in it. Every one of those
     passes when this file is wrong -- they only get quieter, because the
     wrong claim is now agreed with in four places. No coverage tier reaches
     it either: `.coveragerc` measures Python modules, ruff does not read
     Markdown, and the Containerfile never copies `.github/` into the image.
Goal: Make a moved gate, a new linter in CI, a renamed constant or helper, a
      re-pinned template upstream, a new permission layer, a new deny rule and
      a changed mirror fail here, instead of leaving the one document that is
      supposed to be corrected first describing a repository that has moved
      on.

The section the document got wrong was *Mechanical limits*, and it got it
wrong by omission in the way a closed enumeration invites. It said
`.claude/settings.json` enforces "in three layers" and listed allow, ask and
deny -- but the file also registers a `PreToolUse` hook,
`.claude/hooks/gate_git_diff.py`, whose whole purpose is to refuse spellings
the allow rows permit. An agent reading the canonical brief, seeing
`Bash(git diff:*)` allowed and nothing about a hook, learns the wrong rule
about the one mechanism that will actually stop it. The deny bullet was the
same shape one level down: it described "reading a signing key or `.env`" and
said nothing of the three rows denying PEM and SSH private keys. Both
enumerations are now joined to the settings file in both directions, so a row
added there fails here until the prose names it, and prose naming a rule that
no longer exists fails too.
"""

from __future__ import annotations

import ast
import configparser
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

COPILOT_RELATIVE = ".github/copilot-instructions.md"
COPILOT = ROOT / COPILOT_RELATIVE
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
WORKFLOW_DIR = ROOT / ".github/workflows"
THRESHOLDS = ROOT / ".coverage-thresholds.json"
COVERAGERC = ROOT / ".coveragerc"
SETTINGS = ROOT / ".claude/settings.json"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
CLAUDE_MD = ROOT / "CLAUDE.md"
EDITORCONFIG = ROOT / ".editorconfig"
BUILDER = ROOT / "atomic_image_builder.py"

MIRROR_START = "<!-- mirror:copilot-instructions start -->"
MIRROR_END = "<!-- mirror:copilot-instructions end -->"

# Number words as this repository's prose spells them. A count is compared
# against the word rather than a digit because the digit is not what the
# document writes, and a test that only accepted digits would pass on every
# sentence here.
NUMBER_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
}

# The one *Before you push* command no allow row in `.claude/settings.json`
# matches. `Bash(python3 -m coverage report)` is an exact row, so the gated
# form with `--fail-under=` does not match it, and widening the row to `:*`
# would also mean adding the command to the hook's GATED_PREFIXES -- an
# output redirection inside an allowed command is what that table exists to
# refuse. Exempted by name, and the exemption is asserted in both directions
# below so it cannot quietly outlive the row it describes.
UNALLOWED_FENCE_COMMAND_PREFIX = "python3 -m coverage report --fail-under="

# Every `Bash(...)` deny row in `.claude/settings.json`, paired with the
# wording the *Mechanical limits* deny bullet is allowed to describe it with.
# The partition is asserted to be exhaustive, so a new deny row fails here
# until it is classified -- which is the step that was skipped when the three
# `Read(...)` key rows were added and the bullet was not.
BASH_DENY_WORDING = {
    "git push --force": r"force-push",
    "git push -f": r"force-push",
    "git reset --hard": r"hard reset",
    "podman system prune": r"\bPodman\b[^.]*cleanup",
    "podman image prune": r"\bPodman\b[^.]*cleanup",
    "podman rmi -a": r"\bPodman\b[^.]*cleanup",
    "podman rm -a": r"\bPodman\b[^.]*cleanup",
    "buildah rm --all": r"\bBuildah\b[^.]*cleanup",
    "buildah rmi --all": r"\bBuildah\b[^.]*cleanup",
    "gh repo delete": r"repo deletion",
    "rpm-ostree reset": r"rebases the host",
    "bootc switch": r"rebases the host",
}

# The same partition for the `Read(...)` deny rows. `.env.*` and `.env` share
# a description; the key patterns do not, and describing a signing key is not
# describing an SSH one.
READ_DENY_WORDING = {
    "./cosign.key": r"signing key",
    "./.env": r"`\.env`",
    "./.env.*": r"`\.env`",
    "**/*.pem": r"\bPEM\b",
    "**/id_rsa": r"SSH private key",
    "**/id_ed25519": r"SSH private key",
}


def squashed(text: str) -> str:
    """Collapse runs of whitespace, so a wrapped sentence matches as one line.

    Every phrase this module looks for in the document is wrapped at the
    repository's prose width, which puts a newline in the middle of
    "Podman or Buildah cleanup" and of the paths in the hook paragraph. A
    match against the raw text would depend on where the wrap happened to
    fall.
    """
    return re.sub(r"\s+", " ", text)


def sections(text: str) -> dict[str, str]:
    """Map every heading in a Markdown document to the body beneath it.

    Keyed by the heading text alone so a `##` promoted to `###`, or the other
    way, does not silently drop a section from the checks. The prologue above
    the first heading is stored under the empty string.
    """
    found: dict[str, str] = {}
    current = ""
    body: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        heading = None if in_fence else re.match(r"^#+\s+(.*)$", line)
        if heading:
            found[current] = "\n".join(body)
            current = heading.group(1).strip()
            body = []
            continue
        body.append(line)
    found[current] = "\n".join(body)
    return found


def fenced_blocks(text: str) -> list[list[str]]:
    """The content lines of every fenced code block, in document order."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
            continue
        if current is not None:
            current.append(line)
    return blocks


def tracked_files() -> set[str]:
    """Paths git has in the index, which is what the repository actually ships."""
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {name for name in listing.split("\0") if name}


def workflow_steps(workflow_text: str, job: str) -> list[str]:
    """The `- name:` values of one job, read by indentation rather than by YAML.

    PyYAML is not a dependency of this suite, and the jobs here are laid out
    uniformly enough to walk: a job key sits at two spaces, and every step
    name under it is deeper than that.
    """
    names: list[str] = []
    in_job = False
    for line in workflow_text.splitlines():
        stripped = line.strip()
        if re.match(r"^\S", line) or not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key = re.match(r"^([A-Za-z_-]+):", stripped)
        if indent == 2 and key:
            in_job = key.group(1) == job
            continue
        if not in_job:
            continue
        name = re.match(r"^-?\s*name:\s*(.*)$", stripped)
        if name:
            names.append(name.group(1).strip())
    return names


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def step_body(workflow_text: str, step_name: str) -> str:
    """The `run:` body of the named step.

    Walked by indentation for the same reason workflow_steps is: the step is
    bounded by the next line at or above the indent of its own `- name:`, and
    a block scalar's body by the next line at or above the indent of `run:`.
    Reading it any more loosely would let a neighbouring step's commands
    satisfy an assertion about this one.
    """
    lines = workflow_text.splitlines()
    block: list[str] | None = None
    for index, line in enumerate(lines):
        if not re.fullmatch(rf"-\s+name:\s*{re.escape(step_name)}", line.strip()):
            continue
        step_indent = indent_of(line)
        block = []
        for following in lines[index + 1 :]:
            if following.strip() and indent_of(following) <= step_indent:
                break
            block.append(following)
        break
    if block is None:
        raise AssertionError(f"{CI_WORKFLOW.name} has no step named {step_name!r}")

    for position, line in enumerate(block):
        if not re.match(r"^\s*run:", line):
            continue
        run_indent = indent_of(line)
        first = line.strip()[len("run:") :].strip()
        body = [] if first in {"", "|", "|-", ">", ">-"} else [first]
        for following in block[position + 1 :]:
            if following.strip() and indent_of(following) <= run_indent:
                break
            body.append(following.strip())
        return "\n".join(body)
    raise AssertionError(f"step {step_name!r} has no run: body")


def allow_rule_matches(rule: str, command: str) -> bool:
    """Claude Code's Bash permission semantics, as this repo relies on them.

    `Bash(cmd)` matches that command and nothing else; `Bash(prefix:*)`
    matches any command starting with the prefix. The distinction is the whole
    reason the hook exists, so it is spelled out here rather than approximated
    with a substring test.
    """
    inner = re.fullmatch(r"Bash\((.*)\)", rule)
    if not inner:
        return False
    pattern = inner.group(1)
    if pattern.endswith(":*"):
        return command.startswith(pattern[: -len(":*")])
    return command == pattern


class CopilotDocumentTestCase(unittest.TestCase):
    """Shared readings of the document and the files it describes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = COPILOT.read_text()
        cls.squashed = squashed(cls.text)
        cls.sections = sections(cls.text)
        cls.ci = CI_WORKFLOW.read_text()
        cls.settings = json.loads(SETTINGS.read_text())
        cls.thresholds = json.loads(THRESHOLDS.read_text())

    def section(self, heading: str) -> str:
        self.assertIn(
            heading,
            self.sections,
            f"{COPILOT_RELATIVE} no longer has a {heading!r} section; the "
            f"claims checked against it have moved or gone",
        )
        return self.sections[heading]

    def fence_commands(self) -> list[str]:
        blocks = fenced_blocks(self.section("Before you push"))
        self.assertEqual(
            len(blocks),
            1,
            f"{COPILOT_RELATIVE}'s *Before you push* section no longer holds "
            f"exactly one fence",
        )
        commands = [line.strip() for line in blocks[0] if line.strip()]
        self.assertGreater(len(commands), 1, "the *Before you push* fence is empty")
        return commands


class PointerTests(CopilotDocumentTestCase):
    """The document is mostly pointers, so the pointers have to land."""

    def test_every_relative_link_resolves(self) -> None:
        # A brief that defers to four other documents is worth exactly as much
        # as its links. `../` because this file lives in `.github/`.
        targets = re.findall(r"\]\((?!https?:)([^)#]+)(?:#[^)]*)?\)", self.text)
        self.assertGreater(len(targets), 3, "the document links nothing relative")
        for target in targets:
            resolved = (COPILOT.parent / target).resolve()
            self.assertTrue(
                resolved.exists(),
                f"{COPILOT_RELATIVE} links {target}, which does not exist",
            )

    def test_backticked_repository_paths_exist(self) -> None:
        # The document names most of its machine in backticks rather than as
        # links, and those are the claims that rot silently: a renamed config
        # leaves a sentence that reads correctly and sends an agent to a file
        # that is not there.
        candidates = set(re.findall(r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)`", self.text))
        # Filenames the document names as things that must NOT exist, or that
        # belong to a generated repository rather than this one. Asserted the
        # other way below so the exemption cannot go stale.
        deliberately_absent = {
            ".ruff.toml",
            ".claude/settings.local.json",
            ".atomic-image-builder.json",
        }
        self.assertTrue(
            deliberately_absent <= candidates,
            f"{COPILOT_RELATIVE} no longer names "
            f"{sorted(deliberately_absent - candidates)}; drop it from the "
            f"exemption instead of leaving a dead entry",
        )
        for name in deliberately_absent:
            self.assertFalse(
                (ROOT / name).exists(),
                f"{name} now exists but is exempted from the path check as "
                f"absent",
            )
        for name in sorted(candidates - deliberately_absent):
            if "/" not in name and not (ROOT / name).exists():
                # A bare filename may be prose about a generated repo's file
                # rather than a path in this checkout; only assert the ones
                # this repository is supposed to hold.
                continue
            self.assertTrue(
                (ROOT / name).exists(),
                f"{COPILOT_RELATIVE} names `{name}`, which is not in the "
                f"checkout",
            )

    def test_named_sections_of_other_documents_exist(self) -> None:
        # Each of these is a "see X's Y" pointer. A heading that was renamed
        # leaves the reader hunting, which is the failure mode the whole
        # points-elsewhere design is trading against.
        expected = {
            ROOT / "ARCHITECTURE.md": ["Why one file"],
            ROOT / "AGENTS.md": ["Pull request explanations"],
            CONTRIBUTING: ["Tests", "Coverage"],
        }
        for path, headings in expected.items():
            document = sections(path.read_text())
            for heading in headings:
                self.assertIn(
                    heading,
                    document,
                    f"{COPILOT_RELATIVE} points at {path.name}'s *{heading}* "
                    f"section, which no longer exists",
                )
                self.assertRegex(
                    self.squashed,
                    rf"\*{re.escape(heading)}\*",
                    f"{COPILOT_RELATIVE} no longer names {path.name}'s "
                    f"*{heading}* section; drop it from this check",
                )

    def test_coverage_tier_count_matches_the_thresholds_file(self) -> None:
        # "the five coverage tiers" is a count of what
        # `.coverage-thresholds.json` declares: one gated measurement and the
        # advisory list beside it. Adding a sixth tier there has to change the
        # word here.
        tiers = len(self.thresholds["gated"]) + len(self.thresholds["advisory"])
        word = NUMBER_WORDS[tiers]
        self.assertRegex(
            self.squashed,
            rf"\b{word}\s+coverage\s+tiers\b",
            f"{COPILOT_RELATIVE} does not say {word!r} coverage tiers, which "
            f"is what .coverage-thresholds.json declares ({tiers})",
        )
        # And the document it defers to for them has to describe that many.
        contributing = squashed(CONTRIBUTING.read_text())
        self.assertRegex(
            contributing,
            rf"\b{word}\s+separate\s+coverage\s+measurements\b",
            f"CONTRIBUTING.md does not describe {tiers} coverage "
            f"measurements, so {COPILOT_RELATIVE} sends the reader to the "
            f"wrong count",
        )


class BeforeYouPushFenceTests(CopilotDocumentTestCase):
    """The fence is the part of the brief an agent actually executes."""

    def test_fence_linters_match_the_ci_gate_both_ways(self) -> None:
        # Derived from the `test` job's step names rather than from a list
        # here: a linter added to CI and not to the fence is precisely the
        # drift that leaves an agent pushing a red branch, and a fence naming
        # a linter CI dropped wastes a local run on a gate that is gone.
        ci_linters = set()
        for name in workflow_steps(self.ci, "test"):
            match = re.fullmatch(r"Run (\S+)", name)
            if match and match.group(1) != "tests":
                ci_linters.add(match.group(1))
        self.assertGreater(len(ci_linters), 1, "ci.yml's test job runs no named linter")

        fence_tools = set()
        for command in self.fence_commands():
            tool = command.split()[0]
            if tool.startswith(("python3", "tests/")):
                continue
            fence_tools.add(tool)
        self.assertEqual(
            fence_tools,
            ci_linters,
            f"{COPILOT_RELATIVE}'s *Before you push* fence and ci.yml's "
            f"gated linters disagree",
        )

    def test_fence_reads_the_gate_from_the_thresholds_file(self) -> None:
        # The document's own loudest trap, applied to itself: the fence must
        # read the number, not write it. `jq` expression compared to the one
        # ci.yml uses so both move together.
        gate_commands = [c for c in self.fence_commands() if "--fail-under" in c]
        self.assertEqual(
            len(gate_commands),
            1,
            f"{COPILOT_RELATIVE}'s fence no longer runs the coverage gate "
            f"exactly once",
        )
        report = step_body(self.ci, "Report coverage")
        expression = re.search(r"jq -er '([^']+)' (\S+)", report)
        self.assertIsNotNone(expression, "ci.yml no longer reads the gate with jq")
        self.assertIn(
            f"jq -er '{expression.group(1)}' {expression.group(2)}",
            gate_commands[0],
            f"{COPILOT_RELATIVE}'s fence reads the gate differently from "
            f"ci.yml",
        )
        self.assertNotRegex(
            gate_commands[0],
            r"--fail-under=\d",
            f"{COPILOT_RELATIVE}'s fence writes the threshold as a literal, "
            f"which is the thing it tells the reader not to do",
        )

    def test_coveragerc_sets_no_fail_under(self) -> None:
        # The sentence after the fence explains why the gate has to be passed
        # explicitly: a bare `coverage report` exits 0 however far coverage
        # has fallen. That is only true while `.coveragerc` sets no
        # `fail_under`, and adding one would make the advice wrong in the
        # dangerous direction -- a reader who trusts it would stop passing the
        # flag.
        parser = configparser.ConfigParser()
        parser.read_string(COVERAGERC.read_text())
        for section in parser.sections():
            self.assertNotIn(
                "fail_under",
                parser[section],
                f"{COPILOT_RELATIVE} says .coveragerc sets no fail_under, but "
                f"[{section}] does",
            )
        self.assertRegex(
            self.squashed,
            r"`\.coveragerc` sets no `fail_under`",
            f"{COPILOT_RELATIVE} no longer makes the claim this checks",
        )

    def test_shellcheck_operands_match_ci_after_expanding_the_glob(self) -> None:
        # The fence abbreviates the suite list with `tests/e2e/*.sh` where
        # ci.yml spells the three files out. Equal sets after expansion, so a
        # fourth suite -- which the glob would pick up and the workflow would
        # not -- fails here rather than being linted locally and not in CI.
        fence = [c for c in self.fence_commands() if c.startswith("shellcheck")]
        self.assertEqual(len(fence), 1, "the fence no longer runs shellcheck once")
        documented: set[str] = set()
        for operand in fence[0].split()[1:]:
            if operand.startswith("-"):
                continue
            if "*" in operand:
                matched = sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(operand))
                self.assertTrue(matched, f"the fence's {operand} matches no file")
                documented.update(matched)
                continue
            documented.add(operand)
        body = step_body(self.ci, "Run shellcheck")
        gated = {
            operand
            for operand in squashed(body.replace("\\", " ")).split()[1:]
            if not operand.startswith("-")
        }
        self.assertEqual(
            documented,
            gated,
            f"{COPILOT_RELATIVE}'s shellcheck command and ci.yml's cover "
            f"different files",
        )
        for operand in sorted(documented):
            self.assertTrue(
                (ROOT / operand).is_file(),
                f"{COPILOT_RELATIVE} shellchecks {operand}, which is gone",
            )

    def test_hadolint_operands_match_ci(self) -> None:
        fence = [c for c in self.fence_commands() if c.startswith("hadolint")]
        self.assertEqual(len(fence), 1, "the fence no longer runs hadolint once")
        documented = set(fence[0].split()[1:])
        gated = set(step_body(self.ci, "Run hadolint").split()[1:])
        self.assertEqual(
            documented,
            gated,
            f"{COPILOT_RELATIVE}'s hadolint command and ci.yml's cover "
            f"different Containerfiles",
        )
        for operand in sorted(documented):
            self.assertTrue(
                (ROOT / operand).is_file(),
                f"{COPILOT_RELATIVE} hadolints {operand}, which is gone",
            )

    def test_hadolint_is_pinned_as_a_binary_not_as_an_action(self) -> None:
        # The *easy to get wrong* section explains that a linter this repo
        # alone runs is pinned by version and checksum instead of appearing in
        # ACTION_PINS, "the way `ci.yml` does with hadolint". Both halves are
        # checkable, and the wrong half landing is what would put a linter
        # action into a table that ships to generated repos.
        install = step_body(self.ci, "Install test tooling")
        self.assertRegex(
            install,
            r"releases/download/v[\d.]+/hadolint-",
            "ci.yml no longer fetches the hadolint binary by version",
        )
        self.assertRegex(
            install,
            r"hadolint\"?\n?.*sha256sum -c -|[0-9a-f]{64}\s+\"?\$RUNNER_TEMP/bin/hadolint",
            "ci.yml no longer checksums the hadolint binary",
        )
        uses = set()
        for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
            for line in workflow.read_text().splitlines():
                match = re.match(r"^\s*-?\s*uses:\s*(\S+)", line)
                if match:
                    uses.add(match.group(1).split("@")[0])
        self.assertNotIn(
            "hadolint/hadolint-action",
            uses,
            f"{COPILOT_RELATIVE} says hadolint is pinned as a binary rather "
            f"than through its action, but a workflow now uses the action",
        )

    def test_every_fence_command_is_allow_listed_or_exempt(self) -> None:
        # The *Mechanical limits* section promises the allow layer covers "the
        # repo's own read-only gate, so running the checks does not cost a
        # prompt every time". That is a claim about this fence, and it is
        # checkable against the rows.
        allow = self.settings["permissions"]["allow"]
        exempt = []
        for command in self.fence_commands():
            if any(allow_rule_matches(rule, command) for rule in allow):
                continue
            exempt.append(command)
        self.assertEqual(
            [UNALLOWED_FENCE_COMMAND_PREFIX],
            [c[: len(UNALLOWED_FENCE_COMMAND_PREFIX)] for c in exempt],
            f"{COPILOT_RELATIVE}'s fence has a command no allow row in "
            f".claude/settings.json matches, beyond the one exempted here",
        )
        # The other direction. If a row is ever added that does match the
        # exempted command, this exemption is stale and has to go rather than
        # sit here asserting nothing.
        self.assertFalse(
            any(allow_rule_matches(rule, exempt[0]) for rule in allow),
            f"{exempt[0]!r} is now allow-listed; drop "
            f"UNALLOWED_FENCE_COMMAND_PREFIX",
        )

    def test_pinned_tool_versions_match_what_ci_installs(self) -> None:
        # "Pinned tool versions are in CONTRIBUTING.md's *Tests* section, and
        # match what CI installs." Both pins, both directions.
        def pins(text: str) -> dict[str, str]:
            found: dict[str, str] = {}
            for line in text.splitlines():
                if "pip install" not in line:
                    continue
                for package, version in re.findall(r"([A-Za-z0-9_-]+)==([\d.]+)", line):
                    found[package] = version
            return found

        ci_pins = pins(step_body(self.ci, "Install test tooling"))
        self.assertTrue(ci_pins, "ci.yml no longer pins its pip tooling")
        documented = pins(sections(CONTRIBUTING.read_text())["Tests"])
        self.assertEqual(
            documented,
            ci_pins,
            f"CONTRIBUTING.md's *Tests* section and ci.yml pin different tool "
            f"versions, and {COPILOT_RELATIVE} says they match",
        )


class EasyToGetWrongTests(CopilotDocumentTestCase):
    """The traps section names constants, helpers and upstreams by hand."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.module = ast.parse(BUILDER.read_text())

    def assigned(self, name: str) -> ast.expr:
        for node in self.module.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == name:
                    self.assertIsNotNone(node.value, f"{name} has no value")
                    return node.value
        raise AssertionError(
            f"{COPILOT_RELATIVE} names {name}, which atomic_image_builder.py "
            f"no longer assigns at module level"
        )

    def test_named_action_pin_tables_are_dictionaries(self) -> None:
        # The trap is that a `uses:` outside both tables fails the build. That
        # is enforced by `maintenance_audit.py`, which the fence runs -- so
        # what is worth asserting here is that the two names the document
        # tells an agent to edit are still the names of the tables, and that
        # the audit still reads both.
        for name in ("ACTION_PINS", "ACTION_REF_PINS"):
            self.assertIsInstance(
                self.assigned(name),
                ast.Dict,
                f"{name} is no longer a dict, and {COPILOT_RELATIVE} tells an "
                f"agent to add an entry to it",
            )
        audit = (ROOT / "maintenance_audit.py").read_text()
        for name in ("ACTION_PINS", "ACTION_REF_PINS"):
            # Anchored both ends. A plain substring search for ACTION_PINS is
            # satisfied by ACTION_REF_PINS, and one for either is satisfied by
            # a renamed ACTION_PINS_V2 -- which is the rename this is supposed
            # to catch.
            self.assertRegex(
                audit,
                rf"\b{name}\b",
                f"{COPILOT_RELATIVE} says maintenance_audit.py fails the "
                f"build on a `uses:` missing from {name}, but it no longer "
                f"reads that table",
            )

    def test_tool_slug_feeds_the_state_file_name_the_document_writes(self) -> None:
        # The document spells the state file out as `.atomic-image-builder.json`
        # and says TOOL_SLUG feeds it. Both halves: the literal has to be what
        # the constants actually produce, and STATE_FILE has to still be built
        # from TOOL_SLUG rather than written out beside it -- the second is
        # what makes the rename dangerous in the way the trap describes.
        slug = self.assigned("TOOL_SLUG")
        self.assertIsInstance(slug, ast.Constant, "TOOL_SLUG is no longer a literal")
        state = self.assigned("STATE_FILE")
        self.assertIsInstance(
            state,
            ast.JoinedStr,
            "STATE_FILE is no longer built from TOOL_SLUG, so renaming the "
            "slug no longer orphans managed repos the way "
            f"{COPILOT_RELATIVE} describes",
        )
        self.assertIn(
            "TOOL_SLUG",
            {
                node.id
                for node in ast.walk(state)
                if isinstance(node, ast.Name)
            },
            "STATE_FILE no longer interpolates TOOL_SLUG",
        )
        self.assertRegex(
            self.squashed,
            rf"`\.{re.escape(slug.value)}\.json`",
            f"{COPILOT_RELATIVE} names a state file other than the one "
            f"TOOL_SLUG produces",
        )
        # TOOL_COMMAND is called out as the safe one to change, which is only
        # advice worth following while it is a separate constant.
        command = self.assigned("TOOL_COMMAND")
        self.assertIsInstance(command, ast.Constant)
        self.assertNotEqual(
            command.value,
            slug.value,
            f"{COPILOT_RELATIVE} says TOOL_COMMAND is separate from TOOL_SLUG",
        )

    def test_patchers_are_silent_and_the_one_named_exception_is_loud(self) -> None:
        # `patch_workflow_*` is a wildcard in the prose, so at least one
        # function has to match it. The claim worth asserting is the next
        # sentence's, though: the patchers no-op when their anchors move,
        # "with one exception" that fails the update loudly, because the
        # alternative is an unsigned image. That is a property of the code --
        # whether the function can raise -- so it is read off the tree rather
        # than taken on trust. A second loud patcher, or a silent
        # ensure_workflow_job_env_entries, makes the paragraph wrong in the
        # direction that ships a broken repository.
        functions = {
            node.name: node
            for node in self.module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        loud = "ensure_workflow_job_env_entries"
        self.assertIn(
            loud,
            functions,
            f"{COPILOT_RELATIVE} names {loud} as the one patcher that fails "
            f"loudly, and it is gone",
        )
        patchers = sorted(name for name in functions if name.startswith("patch_workflow_"))
        self.assertTrue(
            patchers,
            f"{COPILOT_RELATIVE} warns about the `patch_workflow_*` helpers, "
            f"and atomic_image_builder.py now defines none",
        )

        def raises(name: str) -> bool:
            return any(
                isinstance(node, ast.Raise) for node in ast.walk(functions[name])
            )

        self.assertTrue(
            raises(loud),
            f"{COPILOT_RELATIVE} says {loud} fails the update loudly, and it "
            f"now raises nothing",
        )
        for name in patchers:
            self.assertFalse(
                raises(name),
                f"{COPILOT_RELATIVE} says the patch_workflow_* helpers no-op "
                f"rather than erroring, with {loud} the one exception, and "
                f"{name} now raises",
            )

    def test_vendored_template_upstreams_match_the_snapshots_both_ways(self) -> None:
        # The document names the two upstreams in parentheses. They are
        # recorded in the snapshots themselves, so the join is exact -- and
        # both directions matter: a re-pinned upstream that the prose still
        # names sends a reader to the wrong repository, and an upstream added
        # without a mention leaves a vendored tree nothing warns about.
        sources = sorted(ROOT.glob("template_snapshots/*/.template-source"))
        self.assertTrue(sources, "template_snapshots/ records no upstream")
        slugs = set()
        for source in sources:
            repo = re.search(r"^repo=(\S+)$", source.read_text(), re.MULTILINE)
            self.assertIsNotNone(repo, f"{source} records no repo=")
            slugs.add(
                repo.group(1).removesuffix(".git").removeprefix("https://github.com/")
            )
        # Read out of the vendoring paragraph alone. The traps section names
        # other slash-separated tokens -- paths, helper names -- and a
        # document-wide search would be satisfied by one of those rather than
        # by the sentence that has to carry the upstreams.
        paragraphs = self.section("Things that are easy to get wrong here").split("\n\n")
        vendored = [p for p in paragraphs if "is vendored" in p]
        self.assertEqual(
            len(vendored),
            1,
            f"{COPILOT_RELATIVE} no longer has exactly one vendoring "
            f"paragraph",
        )
        named = set(re.findall(r"`([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)`", squashed(vendored[0])))
        self.assertEqual(
            named,
            slugs,
            f"{COPILOT_RELATIVE} names {sorted(named)} as the vendored "
            f"upstreams where template_snapshots/ records {sorted(slugs)}",
        )

    def test_e2e_suite_count_matches_the_word_both(self) -> None:
        # "Add a scenario to both scripts" is a count. It is two only while
        # `tests/e2e/` holds two suites that ci.yml runs; a third would make
        # the instruction quietly incomplete.
        suites = sorted(
            path.name
            for path in (ROOT / "tests/e2e").glob("*.sh")
            if path.name != "lib.sh"
        )
        self.assertEqual(
            len(suites),
            2,
            f"tests/e2e/ now holds {len(suites)} suites, so "
            f"{COPILOT_RELATIVE}'s \"both scripts\" is wrong",
        )
        for suite in suites:
            self.assertIn(
                f"tests/e2e/{suite}",
                self.ci,
                f"ci.yml no longer runs tests/e2e/{suite}, which "
                f"{COPILOT_RELATIVE} says it does rather than duplicating",
            )
        self.assertRegex(
            self.squashed,
            r"\bboth scripts\b",
            f"{COPILOT_RELATIVE} no longer says \"both scripts\"",
        )

    def test_only_the_undotted_ruff_config_is_tracked(self) -> None:
        # The claim is specifically that adding the dotted variant orphans the
        # real one, so the tracked set is what matters -- an untracked local
        # `.ruff.toml` is the contributor's own problem and not a repository
        # defect.
        tracked = tracked_files()
        self.assertIn("ruff.toml", tracked, "ruff.toml is no longer tracked")
        self.assertNotIn(
            ".ruff.toml",
            tracked,
            f"{COPILOT_RELATIVE} says only the undotted config exists, and "
            f".ruff.toml is tracked again",
        )


class StyleClaimTests(CopilotDocumentTestCase):
    """The *Style* section delegates the mechanical half to two files."""

    def test_editorconfig_carries_the_three_named_rules_and_points_back(self) -> None:
        # The document names indent width, line endings and a final newline as
        # `.editorconfig`'s share. Each is a property, and the division of
        # labour is only real while `.editorconfig` defers the rest back here
        # -- which it says it does, so both halves are asserted.
        editorconfig = EDITORCONFIG.read_text()
        for prop in ("indent_size", "end_of_line", "insert_final_newline"):
            self.assertRegex(
                editorconfig,
                rf"(?m)^{prop}\s*=",
                f"{COPILOT_RELATIVE} says .editorconfig carries {prop}, and it "
                f"no longer sets it",
            )
        # From the header comment specifically. `.editorconfig` names this
        # document twice -- once to defer the non-mechanical half to it and
        # once inside the template_snapshots section -- so a whole-file search
        # is satisfied by the copy that did not drift.
        header = editorconfig.split("[", 1)[0]
        self.assertIn(
            COPILOT_RELATIVE,
            header,
            f".editorconfig's header no longer defers its non-mechanical half "
            f"to {COPILOT_RELATIVE}, which claims the split",
        )

    def test_table_formatter_exists_with_the_check_mode_ci_uses(self) -> None:
        # "run `python3 format_markdown_tables.py` rather than padding cells
        # by hand" is only useful advice while the script is there, and the
        # `--check` mode is what makes the enforcement claim true.
        formatter = ROOT / "format_markdown_tables.py"
        self.assertTrue(
            formatter.is_file(),
            f"{COPILOT_RELATIVE} tells the reader to run {formatter.name}",
        )
        self.assertIn(
            "--check",
            formatter.read_text(),
            f"{formatter.name} no longer offers --check, which is how the "
            f"alignment {COPILOT_RELATIVE} calls enforced is enforced",
        )


class MechanicalLimitsTests(CopilotDocumentTestCase):
    """The section that describes `.claude/settings.json` to an agent."""

    def setUp(self) -> None:
        self.body = squashed(self.section("Mechanical limits"))

    def test_settings_is_tracked_and_the_local_override_is_ignored(self) -> None:
        # "committed and shared" and "stays gitignored" are the two halves
        # that decide whether a personal allowance can reach another
        # contributor. Both are git questions, so both are asked of git.
        self.assertIn(
            ".claude/settings.json",
            tracked_files(),
            f"{COPILOT_RELATIVE} says .claude/settings.json is committed",
        )
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", ".claude/settings.local.json"],
            capture_output=True,
        )
        self.assertEqual(
            ignored.returncode,
            0,
            f"{COPILOT_RELATIVE} says .claude/settings.local.json stays "
            f"gitignored, and .gitignore no longer excludes it",
        )

    def test_section_names_every_permission_layer_and_the_registered_hook(self) -> None:
        # The enumeration that drifted. `.claude/settings.json` has two
        # top-level keys, not one: `permissions`, whose three lists the
        # bullets describe, and `hooks`, which registers a PreToolUse gate
        # that refuses spellings the allow rows permit. A closed enumeration
        # of the first that says nothing of the second teaches an agent the
        # wrong rule about the mechanism that will actually stop it, so both
        # are joined to the file here.
        layers = list(self.settings["permissions"])
        for layer in layers:
            self.assertIn(
                f"- **{layer}**",
                squashed(self.section("Mechanical limits").replace("\n", " ")),
                f"{COPILOT_RELATIVE}'s *Mechanical limits* section does not "
                f"describe the {layer!r} permission layer",
            )
        # The other direction: a bullet naming a list the file does not have.
        described = set(re.findall(r"- \*\*([a-z]+)\*\*", self.body))
        self.assertEqual(
            described,
            set(layers),
            f"{COPILOT_RELATIVE} describes permission layers "
            f"{sorted(described)} where .claude/settings.json has "
            f"{sorted(layers)}",
        )
        self.assertRegex(
            self.body,
            rf"\b{NUMBER_WORDS[len(layers)]}\s+permission\s+layers\b",
            f"{COPILOT_RELATIVE} does not count "
            f"{len(layers)} permission layers",
        )

        # And the hook. Named by event and by the file that runs, because
        # either one moving alone is enough to make the paragraph wrong.
        events = list(self.settings.get("hooks", {}))
        self.assertTrue(events, ".claude/settings.json registers no hook")
        commands = {
            hook["command"]
            for event in events
            for matcher in self.settings["hooks"][event]
            for hook in matcher["hooks"]
        }
        for event in events:
            self.assertIn(
                event,
                self.body,
                f"{COPILOT_RELATIVE}'s *Mechanical limits* section does not "
                f"name the {event} hook .claude/settings.json registers",
            )
        for command in commands:
            script = re.search(r"(\.claude/hooks/\S+\.py)", command)
            self.assertIsNotNone(
                script, f"the registered hook command {command!r} names no script"
            )
            self.assertTrue(
                (ROOT / script.group(1)).is_file(),
                f".claude/settings.json registers {script.group(1)}, which is "
                f"not in the checkout",
            )
            self.assertIn(
                script.group(1),
                self.body,
                f"{COPILOT_RELATIVE}'s *Mechanical limits* section does not "
                f"name {script.group(1)}, the hook that refuses commands the "
                f"allow rows permit",
            )

    def test_deny_bullet_describes_every_denied_command_both_ways(self) -> None:
        # The deny bullet is a closed list of categories, so every row has to
        # fall into one. The partition is asserted exhaustive first: a new
        # deny row fails here until someone decides which words describe it,
        # which is the step that was skipped when the key rows were added.
        rows = self.settings["permissions"]["deny"]
        bash = {
            re.fullmatch(r"Bash\((.*?)(?::\*)?\)", row).group(1)
            for row in rows
            if row.startswith("Bash(")
        }
        self.assertEqual(
            bash,
            set(BASH_DENY_WORDING),
            "the Bash deny rows and this test's partition of them disagree; "
            "classify the new row rather than widening the test",
        )
        for row, wording in sorted(BASH_DENY_WORDING.items()):
            self.assertRegex(
                self.body,
                wording,
                f"{COPILOT_RELATIVE}'s deny bullet does not describe the "
                f"{row!r} rule",
            )

    def test_deny_bullet_describes_every_denied_read_both_ways(self) -> None:
        # The half that had gone stale. Three rows deny reading a PEM file or
        # an SSH private key; the bullet described a signing key and `.env`.
        rows = self.settings["permissions"]["deny"]
        reads = {
            re.fullmatch(r"Read\((.*)\)", row).group(1)
            for row in rows
            if row.startswith("Read(")
        }
        self.assertEqual(
            reads,
            set(READ_DENY_WORDING),
            "the Read deny rows and this test's partition of them disagree; "
            "classify the new row rather than widening the test",
        )
        for row, wording in sorted(READ_DENY_WORDING.items()):
            self.assertRegex(
                self.body,
                wording,
                f"{COPILOT_RELATIVE}'s deny bullet does not describe the "
                f"{row!r} rule",
            )

    def test_ask_bullet_describes_every_outward_facing_rule(self) -> None:
        # Lighter than the deny checks on purpose: the ask rows are a prompt,
        # not a refusal, so what matters is that none of them is a surprise.
        # Each row's own verb has to appear in the bullet.
        wording = {
            "git push": r"\bpush\b",
            "gh pr merge": r"\bPR\b",
            "gh pr create": r"\bPR\b",
            "gh release create": r"\brelease\b",
            "gh workflow run": r"workflow\s+dispatch",
            "podman build": r"image\s+build",
        }
        rows = {
            re.fullmatch(r"Bash\((.*?)(?::\*)?\)", row).group(1)
            for row in self.settings["permissions"]["ask"]
        }
        self.assertEqual(
            rows,
            set(wording),
            "the ask rows and this test's partition of them disagree",
        )
        for row, pattern in sorted(wording.items()):
            self.assertRegex(
                self.body,
                pattern,
                f"{COPILOT_RELATIVE}'s ask bullet does not describe {row!r}",
            )


class CanonicalFileClaimTests(CopilotDocumentTestCase):
    """The closing section's claims about its own status."""

    def test_mirror_paragraph_count_matches_the_word_in_the_prose(self) -> None:
        # test_agent_guidance_has_one_canonical_file asserts the mirror block
        # *is* the four canonical paragraphs. It does not read the sentence
        # here that says four, so mirroring a fifth paragraph would leave this
        # document miscounting its own exception.
        block = CLAUDE_MD.read_text().split(MIRROR_START)[1].split(MIRROR_END)[0]
        mirrored = [
            paragraph
            for paragraph in block.strip().split("\n\n")
            if paragraph.startswith("**")
        ]
        self.assertTrue(mirrored, "CLAUDE.md's mirror block holds no paragraph")
        word = NUMBER_WORDS[len(mirrored)]
        self.assertRegex(
            self.squashed,
            rf"mirrors {word} of the paragraphs above",
            f"{COPILOT_RELATIVE} does not say it mirrors {word} paragraphs, "
            f"which is how many CLAUDE.md's block holds ({len(mirrored)})",
        )

    def test_every_pointer_directory_the_closing_section_names_exists(self) -> None:
        # "Cursor rules, a prompt catalog, packaged skills" is a list of the
        # pointer families the enforcing test walks. A family named here and
        # absent from the repository means the sentence is describing a
        # convention nothing holds it to any more.
        section = squashed(self.section("Other agent entry points"))
        families = {
            "Cursor rules": ".cursor/rules",
            "prompt catalog": ".github/prompts",
            "packaged skills": ".claude/skills",
        }
        tracked = tracked_files()
        for phrase, directory in sorted(families.items()):
            self.assertIn(
                phrase,
                section,
                f"{COPILOT_RELATIVE} no longer names the {phrase!r} pointer "
                f"family; drop it from this check",
            )
            self.assertTrue(
                any(name.startswith(f"{directory}/") for name in tracked),
                f"{COPILOT_RELATIVE} names {phrase!r}, and {directory}/ holds "
                f"no tracked file",
            )

    def test_claude_md_is_the_only_file_allowed_to_mirror(self) -> None:
        # The stated exception is exactly one file. Asserting the count keeps
        # the sentence honest if a second auto-loaded file ever opens a mirror
        # block -- the enforcing test would fail too, but this is the sentence
        # a reader is trusting.
        mirroring = sorted(
            name
            for name in tracked_files()
            if name.endswith((".md", ".mdc")) and MIRROR_START in (ROOT / name).read_text()
        )
        self.assertEqual(
            mirroring,
            ["CLAUDE.md"],
            f"{COPILOT_RELATIVE} says CLAUDE.md is the single exception that "
            f"mirrors it, and {mirroring} carry the markers",
        )


if __name__ == "__main__":
    unittest.main()
