"""Join docs/SECURITY-AI.md's enforcement claims to what actually enforces them.

The document tells an agent which security properties of this repository are
mechanical rather than advisory: what `.claude/settings.json` denies outright,
what `maintenance_audit.py` fails on, that a workflow needing more than a
read-only token declares it, and which single workflow pushes to `main`. An
agent reads it to know what it must not weaken -- and every one of those claims
is a hand copy of something elsewhere in the tree.

No test opened the file before this one, and none can be made to by accident:
`.coveragerc` measures six Python modules, so Markdown can lower no percentage,
and `.claude/settings.json` is measured by nothing at all. A deny rule deleted
from the settings file, an `allow` entry that blanket-permits a denied command,
a job that stops declaring its permissions, a pin check that stops failing, or
the verification step moved after the push all leave this document describing
a machine that no longer exists, with every check in the repository green.

The literals are parsed out of the document rather than restated here: a test
that restates them is a second copy to keep in step, and deleting the copied
sentence would still pass. What is hard-coded is the *shape* of a claim -- the
two classifiers below and their case tables, each with its own tests -- plus
the small set of literals the document must still name, because an assertion
computed only over what the document happens to say is vacuous once the
document says nothing.
"""

import io
import json
import re
import shlex
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import maintenance_audit  # noqa: E402

DOC = ROOT / "docs/SECURITY-AI.md"
SETTINGS = ROOT / ".claude/settings.json"
RISK_TIERS = ROOT / "docs/risk-tiers.md"
CONTAINERFILE = ROOT / "Containerfile"
WORKFLOWS = ROOT / ".github/workflows"
TEMPLATES = ROOT / "template_snapshots"

BACKTICKED = re.compile(r"`([^`]+)`")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<action>[^@\s]+)@(?P<ref>\S+)")
JOB_KEY = re.compile(r"^  (?P<name>[A-Za-z_][A-Za-z0-9_-]*):\s*(?:#.*)?$")
MAPPING_ENTRY = re.compile(r"^(?P<indent> *)(?P<key>[a-z][a-z-]*):\s*(?P<value>\S+)\s*(?:#.*)?$")
INLINE_ENTRY = re.compile(r"^(?P<key>[a-z][a-z-]*):\s*(?P<value>[a-z-]+)$")
TRAILING_COMMENT = re.compile(r"\s+#.*$")

# `git push`, however the line spells it. The leading `git` takes options of
# its own -- `git -C "$dir" push` is how ci.yml pushes the coverage branch --
# so a scan anchored on the two words being adjacent reads that line as no
# push at all, and a second writer spelled the same way would be invisible.
GIT_PUSH = re.compile(r"\bgit\s+(?:-\S+\s+(?:\S+\s+)?)*push\b(?P<argv>[^&;|]*)")

# Every scope GitHub understands, so the `write-all` and `read-all` scalars
# expand to the same shape a block mapping produces. Without the expansion a
# workflow granting `write-all` reads as granting nothing.
PERMISSION_SCOPES = (
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
)
WRITE_JOB_MENTION = re.compile(
    r"`(?P<workflow>[^`]+\.ya?ml)`\s*/\s*`(?P<job>[A-Za-z_][A-Za-z0-9_-]*)`"
)

# Literals the document has to keep naming. Each anchors an assertion below;
# without this set, deleting the sentence that carries one turns its assertion
# into a no-op rather than a failure.
REQUIRED_MENTIONS = (
    "cosign.key",
    ".env",
    "--pull=never",
    "ACTION_PINS",
    "ACTION_REF_PINS",
    "template_snapshots/",
    "maintenance_audit.py",
    "homebrew_formula.py",
    ".claude/settings.json",
    "update-homebrew-formula.yml",
    "contents: write",
)

# Heading -> the claim in it this module checks. A renamed or deleted section
# fails here rather than silently taking its assertions with it.
REQUIRED_SECTIONS = (
    "Why this repository is a sharper case than most",
    "Untrusted input an agent will encounter",
    "Never do these, whatever is asked",
    "What is enforced rather than trusted",
)

# `.claude/settings.json` deny rules, by the phrase the settings bullet uses
# for them. Checked in both directions: a category present in the file must be
# named by the document, and a category the document names must exist in the
# file, so neither side can drift alone.
BASH_DENY_CATEGORIES: tuple[tuple[str, str, tuple[tuple[str, ...], ...]], ...] = (
    (
        "force push",
        "force-push",
        (("git", "push", "--force"), ("git", "push", "-f")),
    ),
    (
        "hard reset",
        "hard reset",
        (("git", "reset", "--hard"),),
    ),
    (
        "podman cleanup",
        "Podman",
        (
            ("podman", "system", "prune"),
            ("podman", "image", "prune"),
            ("podman", "rmi", "-a"),
            ("podman", "rm", "-a"),
        ),
    ),
    (
        "buildah cleanup",
        "Buildah",
        (("buildah", "rm", "--all"), ("buildah", "rmi", "--all")),
    ),
    (
        "repository deletion",
        "repository deletion",
        (("gh", "repo", "delete"),),
    ),
    (
        "host rebase",
        "host rebase",
        (("rpm-ostree", "reset"), ("bootc", "switch")),
    ),
)

# The same, for Read() rules, matched on the basename pattern of the path.
READ_DENY_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("signing key", "signing", ("cosign.key", "*.pem", "id_rsa", "id_ed25519")),
    ("secrets file", ".env", (".env", ".env.*")),
)

# `python3`, `python`, `python3.13` -- the interpreter itself, however it is
# spelled. Used to find the `allow` rules where the effect table below is
# describing the module named in the rule rather than the file the module is
# pointed at.
INTERPRETER = re.compile(r"python[\d.]*")

# What a permitted command can reach. `mutates-remote` is the document's
# "outward-facing": it changes something other people see. An entry here is a
# claim about the command, so an unrecognised one raises rather than being
# assumed harmless -- a new `allow` rule for a command this table does not
# know fails instead of being waved through.
COMMAND_EFFECTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("python3", "-m", "unittest"), "reads"),
    (("python3", "-m", "coverage"), "reads"),
    (("python3", "maintenance_audit.py", "--skip-upstream"), "reads"),
    (("python3", "format_markdown_tables.py", "--check"), "reads"),
    (("ruff", "check"), "reads"),
    (("shellcheck",), "reads"),
    (("hadolint",), "reads"),
    (("actionlint",), "reads"),
    (("just", "--fmt", "--check"), "reads"),
    (("git", "status"), "reads"),
    # "reads" is true of these two only because `.claude/hooks/gate_git_diff.py`
    # refuses the arguments that make them something else: `--no-index` reads
    # any path on disk, `--output` writes one. Drop the hook and this row
    # becomes the file's own claim that an arbitrary file read is a read of
    # the repository. tests/test_git_diff_gate.py holds the pair together.
    (("git", "diff"), "reads"),
    (("git", "log"), "reads"),
    (("git", "push"), "mutates-remote"),
    (("git", "reset"), "mutates-local"),
    (("tests/test_contrib_aib.sh",), "reads"),
    (("tests/test_entrypoint.sh",), "reads"),
    (("skopeo", "inspect"), "reads"),
    (("podman", "ps"), "reads"),
    (("podman", "logs"), "reads"),
    (("podman", "inspect"), "reads"),
    (("podman", "images"), "reads"),
    (("podman", "image", "exists"), "reads"),
    (("podman", "image", "prune"), "mutates-local"),
    (("podman", "system", "prune"), "mutates-local"),
    (("podman", "rm"), "mutates-local"),
    (("podman", "rmi"), "mutates-local"),
    (("podman", "build"), "mutates-local"),
    (("buildah", "rm"), "mutates-local"),
    (("buildah", "rmi"), "mutates-local"),
    (("rpm-ostree", "reset"), "mutates-local"),
    (("bootc", "switch"), "mutates-local"),
    (("gh", "label", "list"), "reads"),
    (("gh", "search", "issues"), "reads"),
    (("gh", "search", "prs"), "reads"),
    (("gh", "pr", "create"), "mutates-remote"),
    (("gh", "pr", "merge"), "mutates-remote"),
    (("gh", "release", "create"), "mutates-remote"),
    (("gh", "workflow", "run"), "mutates-remote"),
    (("gh", "repo", "delete"), "mutates-remote"),
)

# Actions whose whole purpose is opening a pull request. The document says
# Actions cannot open one here; the tree has to agree.
PR_CREATING_ACTION_SUBSTRINGS = ("create-pull-request", "create-pr", "pull-request-action")


class DocClaimError(AssertionError):
    """A claim in a shape this module cannot check, which is a failure."""


def doc_text() -> str:
    return DOC.read_text()


def flatten(text: str) -> str:
    """Collapse whitespace: the document is hard-wrapped, so a needle that
    reads as one phrase is split across lines in the file."""
    return " ".join(text.split())


def sections() -> dict[str, list[str]]:
    """`## Heading` mapped to the lines beneath it."""
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in doc_text().splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            if current in found:
                raise DocClaimError(f"docs/SECURITY-AI.md declares section {current!r} twice")
            found[current] = []
        elif current is not None:
            found[current].append(line)
    return found


def section(title: str) -> list[str]:
    found = sections()
    if title not in found:
        raise DocClaimError(f"docs/SECURITY-AI.md has no section {title!r}")
    return found[title]


def bullets(lines: list[str]) -> list[str]:
    """Top-level `- ` bullets, continuation lines folded in and flattened."""
    collected: list[str] = []
    for line in lines:
        if line.startswith("- "):
            collected.append(line[2:])
        elif collected and line.startswith("  ") and line.strip():
            collected[-1] += " " + line.strip()
        elif not line.strip():
            continue
    return [flatten(bullet) for bullet in collected]


def enforcement_bullets() -> list[str]:
    found = bullets(section("What is enforced rather than trusted"))
    if len(found) < 4:
        raise DocClaimError(
            "docs/SECURITY-AI.md's enforcement section lists fewer claims than the "
            f"four this module joins to the tree: {found}"
        )
    return found


def bullet_naming(pool: list[str], needle: str) -> str:
    """The one bullet that carries `needle`, or a failure.

    Two bullets carrying it means the claim moved and this module would be
    checking whichever copy came first; none means it is gone.
    """
    matches = [bullet for bullet in pool if needle in bullet]
    if len(matches) != 1:
        raise DocClaimError(
            f"expected exactly one docs/SECURITY-AI.md bullet naming {needle!r}, found {len(matches)}"
        )
    return matches[0]


def settings_rules() -> dict[str, list[str]]:
    data = json.loads(SETTINGS.read_text())
    permissions = data["permissions"]
    return {key: list(permissions.get(key, [])) for key in ("allow", "ask", "deny")}


def parse_rule(rule: str) -> tuple[str, str]:
    """`Bash(git push:*)` -> ("Bash", "git push:*"), or a failure."""
    match = re.fullmatch(r"(?P<tool>[A-Za-z]+)\((?P<argument>.*)\)", rule)
    if match is None:
        raise DocClaimError(f".claude/settings.json rule {rule!r} is not `Tool(argument)`")
    if match.group("tool") not in {"Bash", "Read", "Write", "Edit", "WebFetch"}:
        raise DocClaimError(f".claude/settings.json names an unknown tool in {rule!r}")
    return match.group("tool"), match.group("argument")


def bash_argv(argument: str) -> tuple[str, ...]:
    """The command a Bash rule matches, with its trailing `:*` removed."""
    command = argument[:-2] if argument.endswith(":*") else argument
    return tuple(shlex.split(command))


def read_basename_pattern(argument: str) -> str:
    """The filename half of a Read rule's path pattern."""
    return argument.rsplit("/", 1)[-1]


def classify_deny(rule: str) -> str:
    """The category a deny rule falls in, or a failure.

    Deliberately total: a rule this table does not know is a denial the
    document may or may not describe, and guessing which would make the
    document-to-file join below vacuous for exactly the rules it cannot read.
    """
    tool, argument = parse_rule(rule)
    if tool == "Bash":
        argv = bash_argv(argument)
        for category, _phrase, prefixes in BASH_DENY_CATEGORIES:
            if any(argv[: len(prefix)] == prefix for prefix in prefixes):
                return category
        raise DocClaimError(f"unclassifiable deny rule in .claude/settings.json: {rule!r}")
    if tool == "Read":
        pattern = read_basename_pattern(argument)
        for category, _phrase, patterns in READ_DENY_CATEGORIES:
            if pattern in patterns:
                return category
        raise DocClaimError(f"unclassifiable deny rule in .claude/settings.json: {rule!r}")
    raise DocClaimError(f"unclassifiable deny rule in .claude/settings.json: {rule!r}")


def classify_effect(rule: str) -> str:
    """What a Bash rule's command can reach: reads, mutates-local, mutates-remote."""
    tool, argument = parse_rule(rule)
    if tool != "Bash":
        # Read/Write rules name paths, not commands; only the Bash surface is
        # what "outward-facing" can be read off.
        return "reads" if tool == "Read" else "mutates-local"
    argv = bash_argv(argument)
    best: tuple[int, str] | None = None
    for prefix, effect in COMMAND_EFFECTS:
        if argv[: len(prefix)] == prefix and (best is None or len(prefix) > best[0]):
            best = (len(prefix), effect)
    if best is None:
        raise DocClaimError(
            f".claude/settings.json permits {rule!r}, a command COMMAND_EFFECTS does not describe"
        )
    return best[1]


def workflow_paths() -> list[Path]:
    """Every workflow GitHub loads, both extensions.

    GitHub reads `.github/workflows/*.yaml` as well as `*.yml`. A glob over
    one extension is a blind spot every check built on this function inherits:
    a `.yaml` workflow taking `contents: write` would not appear in the
    inventory below, and so would never have to be documented.
    """
    return sorted(path for suffix in ("yml", "yaml") for path in WORKFLOWS.glob(f"*.{suffix}"))


def mapping_block(lines: list[str], start: int, indent: int) -> dict[str, str]:
    """The `key: value` pairs indented under `lines[start]`."""
    found: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = MAPPING_ENTRY.match(line)
        if match is None or len(match.group("indent")) != indent:
            if len(line) - len(line.lstrip()) <= indent - 2:
                break
            if match is None and len(line) - len(line.lstrip()) >= indent:
                raise DocClaimError(f"unsupported permissions entry: {line!r}")
            break
        found[match.group("key")] = match.group("value")
    return found


def inline_permissions(value: str, path: Path) -> dict[str, str]:
    """`permissions:` written on the key's own line, in any form GitHub takes.

    The block-mapping reader below only sees indented children, so every
    single-line form -- the `read-all` / `write-all` scalars and the flow
    mapping `{contents: write}` -- reaches it as an empty mapping and reads as
    a grant of nothing. Each one is parsed here, and anything this function
    does not recognise raises rather than being quietly treated as empty.
    """
    value = TRAILING_COMMENT.sub("", value).strip()
    if value in {"read-all", "write-all"}:
        return dict.fromkeys(PERMISSION_SCOPES, value.removesuffix("-all"))
    if value.startswith("{") and value.endswith("}"):
        body = value[1:-1].strip()
        if not body:
            return {}
        found: dict[str, str] = {}
        for entry in body.split(","):
            match = INLINE_ENTRY.match(entry.strip())
            if match is None:
                raise DocClaimError(
                    f"{path.name}: unsupported permissions entry {entry.strip()!r}"
                )
            found[match.group("key")] = match.group("value")
        return found
    raise DocClaimError(f"{path.name}: unsupported inline permissions {value!r}")


def permissions_at(lines: list[str], index: int, indent: int, path: Path) -> dict[str, str]:
    """The permissions declared by `lines[index]`, inline or in a block."""
    _, _, inline = lines[index].partition(":")
    if TRAILING_COMMENT.sub("", inline).strip():
        return inline_permissions(inline, path)
    return mapping_block(lines, index, indent)


def workflow_permissions(path: Path) -> dict[str, dict[str, str] | None]:
    """Each job in `path` mapped to the permissions it actually runs with.

    `None` means the job declares none and inherits none -- the state the
    document says cannot happen, because a workflow needing more than the
    read-only default declares it explicitly.
    """
    lines = path.read_text().splitlines()
    top: dict[str, str] | None = None
    jobs: dict[str, dict[str, str] | None] = {}
    current: str | None = None
    in_jobs = False
    for index, line in enumerate(lines):
        if line.startswith("permissions:"):
            top = permissions_at(lines, index, 2, path)
            continue
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if line and not line.startswith(" ") and not line.startswith("#"):
            in_jobs = False
            continue
        if not in_jobs:
            continue
        match = JOB_KEY.match(line)
        if match:
            current = match.group("name")
            jobs[current] = None
            continue
        if current is not None and line.startswith("    permissions:"):
            jobs[current] = permissions_at(lines, index, 6, path)
    if not jobs:
        raise DocClaimError(f"{path.name} declares no jobs")
    return {name: (declared if declared is not None else top) for name, declared in jobs.items()}


def workflow_job_lines(path: Path) -> dict[str, list[str]]:
    """Each job in `path` mapped to its own lines, comments dropped.

    Attribution is by line rather than by step because a step needs no
    `name:`, and `workflow_steps` finds steps by their name. An unnamed step
    runs exactly as much as a named one, so a scan that only walked named
    steps would have a hole shaped like a `- run:` with no `name:` above it.
    """
    lines = path.read_text().splitlines()
    jobs: dict[str, list[str]] = {}
    current: str | None = None
    in_jobs = False
    for line in lines:
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if line and not line.startswith(" ") and not line.startswith("#"):
            in_jobs = False
            current = None
            continue
        if not in_jobs:
            continue
        match = JOB_KEY.match(line)
        if match:
            current = match.group("name")
            jobs[current] = []
            continue
        if current is not None and not line.strip().startswith("#"):
            jobs[current].append(line)
    return jobs


def push_targets(argv: str, path: Path) -> set[str]:
    """The branches one `git push` argv writes to.

    A push with no refspec writes to whatever the current branch tracks, which
    is not readable from the workflow text. That is raised rather than skipped:
    an unreadable destination is the one case where staying quiet would let a
    push to the protected branch pass as a push to nowhere.
    """
    words = [word for word in argv.split() if not word.startswith("-")]
    refspecs = words[1:]
    if not refspecs:
        raise DocClaimError(
            f"{path.name}: `git push{argv}` names no refspec, so the branch it "
            "writes to cannot be read from the workflow"
        )
    return {spec.rpartition(":")[2].removeprefix("refs/heads/") for spec in refspecs}


def branch_pushes(branch: str) -> set[tuple[str, str]]:
    """(workflow, job) for every job in the tree that pushes to `branch`."""
    found: set[tuple[str, str]] = set()
    for path in workflow_paths():
        for job, lines in workflow_job_lines(path).items():
            for line in lines:
                for match in GIT_PUSH.finditer(line):
                    if branch in push_targets(match.group("argv"), path):
                        found.add((path.name, job))
    return found


def workflow_triggers(path: Path) -> str:
    """The `on:` block of a workflow, flattened."""
    lines = path.read_text().splitlines()
    collected: list[str] = []
    started = False
    for line in lines:
        if line.startswith("on:"):
            started = True
            continue
        if started:
            if line and not line.startswith(" ") and not line.startswith("#"):
                break
            collected.append(line)
    if not started:
        raise DocClaimError(f"{path.name} has no `on:` block")
    return flatten(" ".join(collected))


def workflow_steps(path: Path) -> list[tuple[str, list[str]]]:
    """(step name, the step's lines) in file order, comments dropped.

    The comments have to go before anything is matched in a body: this
    workflow explains its own `--check` in prose directly above the line that
    runs it, so a scan that kept comments would still find the verification
    after the command doing it was deleted.
    """
    lines = path.read_text().splitlines()
    starts = [
        (index, match.group("name"))
        for index, match in ((index, re.match(r"^      - name: (?P<name>.+?)\s*$", line)) for index, line in enumerate(lines))
        if match
    ]
    found: list[tuple[str, list[str]]] = []
    for position, (index, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        body = [line for line in lines[index:end] if not line.strip().startswith("#")]
        found.append((name, body))
    return found


def cosign_run_instruction() -> str:
    """The Containerfile RUN that installs cosign, continuations folded.

    Folded here rather than through tests/_containerfile.py: that parser reads
    the subset the generator emits and rejects the root Containerfile's LABEL.
    """
    joined = re.sub(r"\\\n\s*", " ", CONTAINERFILE.read_text())
    for line in joined.splitlines():
        if line.startswith("RUN ") and "cosign.rpm" in line:
            return line
    raise DocClaimError("the Containerfile no longer installs cosign in a RUN instruction")


def audit_copy(mutate) -> tuple[int, str]:
    """Run the offline maintenance audit over a copy of the tree.

    `mutate` gets the copy's root and may edit it. The audit is the claim being
    checked, so it is executed rather than read: what matters is that it still
    *fails*, not that the string that would report a failure is still present.
    """
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(WORKFLOWS, root / ".github/workflows")
        shutil.copytree(TEMPLATES, root / "template_snapshots")
        mutate(root)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            status = maintenance_audit.main(["--repo-root", str(root), "--skip-upstream"])
        return status, buffer.getvalue()


def first_pinned_uses(root: Path) -> tuple[Path, str, str, str]:
    """(path, line, action, ref) of the first SHA-pinned `uses:` in the copy."""
    for path in sorted((root / ".github/workflows").glob("*.yml")):
        for line in path.read_text().splitlines():
            match = USES.match(line)
            if match and re.fullmatch(r"[0-9a-f]{40}", match.group("ref")):
                return path, line, match.group("action"), match.group("ref")
    raise DocClaimError("no SHA-pinned action found in the workflow copy")


class ClassifierTests(unittest.TestCase):
    """The classifiers are the join. Untested, every assertion built on them is
    only as good as a shape they silently mis-sorted."""

    def test_a_force_push_rule_is_the_force_push_category(self) -> None:
        self.assertEqual(classify_deny("Bash(git push --force:*)"), "force push")

    def test_a_plain_push_rule_is_not_a_force_push(self) -> None:
        with self.assertRaises(DocClaimError):
            classify_deny("Bash(git push:*)")

    def test_a_key_read_rule_is_the_signing_key_category(self) -> None:
        self.assertEqual(classify_deny("Read(**/id_ed25519)"), "signing key")

    def test_a_dotenv_read_rule_is_the_secrets_category(self) -> None:
        self.assertEqual(classify_deny("Read(./.env.*)"), "secrets file")

    def test_an_unknown_deny_rule_raises_rather_than_being_skipped(self) -> None:
        with self.assertRaises(DocClaimError):
            classify_deny("Bash(curl:*)")

    def test_a_malformed_rule_raises(self) -> None:
        with self.assertRaises(DocClaimError):
            parse_rule("git push --force")

    def test_an_unknown_tool_raises(self) -> None:
        with self.assertRaises(DocClaimError):
            parse_rule("Telepathy(anything)")

    def test_the_longest_matching_prefix_decides_the_effect(self) -> None:
        # `podman image exists` reads; `podman image prune` does not. A shorter
        # prefix winning would call one of them by the other's effect.
        self.assertEqual(classify_effect("Bash(podman image exists:*)"), "reads")
        self.assertEqual(classify_effect("Bash(podman image prune:*)"), "mutates-local")

    def test_a_command_the_table_does_not_describe_raises(self) -> None:
        with self.assertRaises(DocClaimError):
            classify_effect("Bash(rsync -a . remote:/srv:*)")

    def test_bash_argv_drops_the_trailing_wildcard(self) -> None:
        self.assertEqual(bash_argv("git push --force:*"), ("git", "push", "--force"))
        self.assertEqual(bash_argv("tests/test_entrypoint.sh"), ("tests/test_entrypoint.sh",))

    def test_bullets_fold_continuation_lines_and_flatten(self) -> None:
        folded = bullets(["- first line", "  second line", "", "- other"])
        self.assertEqual(folded, ["first line second line", "other"])

    def test_bullet_naming_rejects_a_needle_carried_twice(self) -> None:
        with self.assertRaises(DocClaimError):
            bullet_naming(["a claim", "another claim"], "claim")


class DocumentShapeTests(unittest.TestCase):
    def test_every_section_this_module_reads_is_present(self) -> None:
        for title in REQUIRED_SECTIONS:
            with self.subTest(title=title):
                self.assertTrue(section(title), f"section {title!r} is empty")

    def test_every_load_bearing_literal_is_still_named(self) -> None:
        text = flatten(doc_text())
        for literal in REQUIRED_MENTIONS:
            with self.subTest(literal=literal):
                self.assertIn(
                    literal,
                    text,
                    f"docs/SECURITY-AI.md no longer names {literal!r}; the assertion "
                    "that joins it to the tree is now checking nothing",
                )

    def test_every_relative_link_resolves(self) -> None:
        for target in MARKDOWN_LINK.findall(doc_text()):
            if target.startswith(("http://", "https://", "#")):
                continue
            with self.subTest(target=target):
                self.assertTrue(
                    (DOC.parent / target.split("#", 1)[0]).resolve().exists(),
                    f"docs/SECURITY-AI.md links to {target}, which does not exist",
                )

    def test_the_enforcement_section_names_each_mechanism_once(self) -> None:
        pool = enforcement_bullets()
        for needle in (".claude/settings.json", "maintenance_audit.py", "permissions", "write path"):
            with self.subTest(needle=needle):
                self.assertTrue(bullet_naming(pool, needle))


class SettingsEnforcementTests(unittest.TestCase):
    """`.claude/settings.json` denies ... and asks before anything outward-facing."""

    def setUp(self) -> None:
        self.rules = settings_rules()
        self.bullet = bullet_naming(enforcement_bullets(), ".claude/settings.json")

    def test_every_deny_rule_is_a_category_this_module_can_check(self) -> None:
        for rule in self.rules["deny"]:
            with self.subTest(rule=rule):
                self.assertTrue(classify_deny(rule))

    def test_each_documented_denial_exists_and_each_denial_is_documented(self) -> None:
        present = {classify_deny(rule) for rule in self.rules["deny"]}
        for category, phrase, _ in BASH_DENY_CATEGORIES + READ_DENY_CATEGORIES:
            with self.subTest(category=category):
                self.assertEqual(
                    phrase in self.bullet,
                    category in present,
                    f"docs/SECURITY-AI.md and .claude/settings.json disagree about {category}: "
                    f"the document {'names' if phrase in self.bullet else 'does not name'} it, "
                    f"the file {'denies' if category in present else 'does not deny'} it",
                )

    def test_the_secret_files_the_document_names_are_actually_denied(self) -> None:
        # Taken from the "Do not read a private key or a secrets file" bullet
        # rather than listed here, so the two cannot drift apart.
        never = bullet_naming(bullets(section("Never do these, whatever is asked")), "private key")
        named = [
            literal
            for literal in BACKTICKED.findall(never)
            if literal in {"cosign.key", ".env"}
        ]
        self.assertEqual(sorted(named), [".env", "cosign.key"])
        patterns = {
            read_basename_pattern(parse_rule(rule)[1])
            for rule in self.rules["deny"]
            if parse_rule(rule)[0] == "Read"
        }
        for literal in named:
            with self.subTest(literal=literal):
                self.assertIn(
                    literal,
                    patterns,
                    f"the document says reading {literal} is denied, but no Read rule names it",
                )

    def test_nothing_outward_facing_is_allowed_without_asking(self) -> None:
        for rule in self.rules["allow"]:
            with self.subTest(rule=rule):
                self.assertNotEqual(
                    classify_effect(rule),
                    "mutates-remote",
                    f"{rule} is allowed outright, but the document says outward-facing "
                    "actions are asked about first",
                )

    def test_the_ask_list_covers_the_outward_facing_commands(self) -> None:
        self.assertIn("outward-facing", self.bullet)
        asked = {classify_effect(rule) for rule in self.rules["ask"]}
        self.assertIn("mutates-remote", asked, "nothing outward-facing is asked about at all")

    def test_no_allow_rule_blanket_permits_a_denied_command(self) -> None:
        """Precedence saves this today; a rule that needs it is still a finding.

        `Bash(git:*)` in `allow` reads as "git is fine" to anyone auditing the
        file, and it is the shape a widened permission takes.
        """
        denied = [bash_argv(parse_rule(rule)[1]) for rule in self.rules["deny"] if parse_rule(rule)[0] == "Bash"]
        for rule in self.rules["allow"]:
            tool, argument = parse_rule(rule)
            if tool != "Bash":
                continue
            argv = bash_argv(argument)
            for forbidden in denied:
                with self.subTest(rule=rule, forbidden=" ".join(forbidden)):
                    self.assertNotEqual(
                        forbidden[: len(argv)],
                        argv,
                        f"{rule} covers the denied command {' '.join(forbidden)}",
                    )

    def test_no_allow_rule_hands_the_interpreter_an_arbitrary_target(self) -> None:
        """A `python3 -m ...` rule has to name its whole argument list.

        `Bash(python3 -m coverage:*)` and `Bash(python3 -m unittest:*)` were
        both on the allow list, and both read as a claim about the tool: the
        effect table above calls them `reads`, because running the test suite
        and reporting on it reads. The trailing `:*` does not permit the tool,
        though, it permits whatever the tool is pointed at.
        `python3 -m coverage run some_script.py` executes that script, and
        `python3 -m unittest some.module` imports it, module-level side
        effects and all -- neither one is confined to `tests/`.

        Arbitrary Python is a superset of every command the deny list names. A
        force push, a hard reset, `podman system prune`, reading `cosign.key`:
        each is a `subprocess.run` or an `open` inside a file the interpreter
        is handed, and no Bash rule ever sees the command it forms. That makes
        the denials advisory, which is the one thing the enforcement section
        of the document says they are not.

        Naming the full argument list is what closes it: the rule then permits
        exactly the unit gate CONTRIBUTING.md documents, and anything else --
        another `--rcfile`, another discovery root, a bare script path -- falls
        through to being asked about. `Bash(python3 format_markdown_tables.py
        --check)` is not in the same position and is deliberately not matched
        here: the interpreter is handed a tracked file that the rule itself
        names, not a target chosen at call time.
        """
        for rule in self.rules["allow"]:
            tool, argument = parse_rule(rule)
            if tool != "Bash":
                continue
            argv = bash_argv(argument)
            if not INTERPRETER.fullmatch(argv[0].rsplit("/", 1)[-1]) or argv[1:2] != ("-m",):
                continue
            with self.subTest(rule=rule):
                self.assertFalse(
                    argument.endswith(":*"),
                    f"{rule} lets the interpreter be pointed at any file, so what it "
                    "permits is arbitrary code rather than the module it names",
                )


class PinTableEnforcementTests(unittest.TestCase):
    """`maintenance_audit.py` fails when a workflow action is not covered by the
    pin tables, or when a pinned SHA disagrees with them."""

    def setUp(self) -> None:
        self.bullet = bullet_naming(enforcement_bullets(), "maintenance_audit.py")

    def test_the_document_states_both_halves_of_the_check(self) -> None:
        self.assertIn("not covered by the pin tables", self.bullet)
        self.assertIn("pinned SHA disagrees", self.bullet)

    def test_the_committed_tree_passes_the_audit(self) -> None:
        status, output = audit_copy(lambda root: None)
        self.assertEqual(status, 0, output)

    def test_an_action_outside_the_pin_tables_fails_the_audit(self) -> None:
        def mutate(root: Path) -> None:
            path, line, action, ref = first_pinned_uses(root)
            replacement = line.replace(action, "attacker/checkout", 1)
            path.write_text(path.read_text().replace(line, replacement, 1))

        status, output = audit_copy(mutate)
        self.assertEqual(status, 1, output)
        self.assertIn("attacker/checkout", output)
        self.assertIn("not covered by ACTION_PINS or ACTION_REF_PINS", output)

    def test_a_workflow_sha_that_disagrees_with_the_table_fails_the_audit(self) -> None:
        def mutate(root: Path) -> None:
            path, line, _action, ref = first_pinned_uses(root)
            other = "0" * 40 if ref != "0" * 40 else "1" * 40
            path.write_text(path.read_text().replace(line, line.replace(ref, other, 1), 1))

        status, output = audit_copy(mutate)
        self.assertEqual(status, 1, output)
        self.assertIn("does not match the pin table SHA", output)

    def test_both_pin_tables_are_what_the_audit_reads(self) -> None:
        # The document names the tables; the audit has to be reading those and
        # not a copy of its own.
        source = (ROOT / "maintenance_audit.py").read_text()
        for table in ("ACTION_PINS", "ACTION_REF_PINS"):
            with self.subTest(table=table):
                self.assertIn(table, source)
                self.assertIn(table, flatten((ROOT / "docs/SECURITY-AI.md").read_text()))


class WorkflowPermissionTests(unittest.TestCase):
    """Default workflow token permissions are read-only; a workflow needing more
    declares it explicitly."""

    def test_every_job_runs_with_declared_permissions(self) -> None:
        for path in workflow_paths():
            for job, permissions in workflow_permissions(path).items():
                with self.subTest(workflow=path.name, job=job):
                    self.assertIsNotNone(
                        permissions,
                        f"{path.name}'s {job} job declares no permissions and inherits none, "
                        "so what it can write is whatever the repository default happens to be",
                    )

    def test_no_job_can_open_a_pull_request(self) -> None:
        bullet = bullet_naming(enforcement_bullets(), "write path")
        self.assertIn("cannot create pull requests", bullet)
        for path in workflow_paths():
            text = path.read_text()
            for job, permissions in workflow_permissions(path).items():
                with self.subTest(workflow=path.name, job=job):
                    self.assertNotEqual((permissions or {}).get("pull-requests"), "write")
            for line in text.splitlines():
                match = USES.match(line)
                if match is None:
                    continue
                with self.subTest(workflow=path.name, action=match.group("action")):
                    self.assertFalse(
                        any(part in match.group("action") for part in PR_CREATING_ACTION_SUBSTRINGS),
                        f"{path.name} uses {match.group('action')}, which opens pull requests",
                    )
            with self.subTest(workflow=path.name):
                self.assertNotIn("gh pr create", text)

    def test_every_contents_write_job_is_named_in_the_enforcement_section(self) -> None:
        bullet = bullet_naming(enforcement_bullets(), "write path")
        documented = {
            (match.group("workflow"), match.group("job"))
            for match in WRITE_JOB_MENTION.finditer(bullet)
        }
        actual = {
            (path.name, job)
            for path in workflow_paths()
            for job, permissions in workflow_permissions(path).items()
            if (permissions or {}).get("contents") == "write"
        }
        self.assertEqual(
            documented,
            actual,
            "docs/SECURITY-AI.md's enforcement section and the workflow jobs "
            "granted contents: write disagree",
        )


class MainWritePathTests(unittest.TestCase):
    """Exactly one automated write path pushes straight to `main`."""

    def setUp(self) -> None:
        self.bullet = bullet_naming(enforcement_bullets(), "write path")
        claim = re.search(
            r"exactly one automated push to `(?P<branch>[^`]+)`: "
            r"`(?P<workflow>[^`]+\.ya?ml)`\s*/\s*`(?P<job>[A-Za-z_][A-Za-z0-9_-]*)`",
            self.bullet,
        )
        if claim is None:
            raise DocClaimError(
                "the write-path bullet does not identify the one automated push "
                "as `branch`: `workflow` / `job`"
            )
        self.branch = claim.group("branch")
        self.workflow = WORKFLOWS / claim.group("workflow")
        self.job = claim.group("job")
        self.permission = next(
            literal for literal in BACKTICKED.findall(self.bullet) if ":" in literal
        )

    def test_the_named_workflow_and_job_exist(self) -> None:
        self.assertTrue(self.workflow.is_file(), f"{self.workflow} does not exist")
        self.assertIn(
            self.job,
            workflow_permissions(self.workflow),
            f"{self.workflow.name} has no {self.job} job",
        )

    def test_it_takes_the_permission_the_document_names(self) -> None:
        key, _, value = self.permission.partition(":")
        granted = workflow_permissions(self.workflow)[self.job] or {}
        self.assertEqual(
            granted.get(key.strip()),
            value.strip(),
            f"{self.workflow.name}'s {self.job} job no longer takes {self.permission}",
        )

    def test_it_runs_on_a_published_release(self) -> None:
        triggers = workflow_triggers(self.workflow)
        self.assertIn("release:", triggers)
        self.assertIn("published", triggers)

    def test_it_pushes_to_the_branch_the_document_names(self) -> None:
        pushes = [
            (line.strip(), push_targets(match.group("argv"), self.workflow))
            for _name, body in workflow_steps(self.workflow)
            for line in body
            for match in [GIT_PUSH.search(line)]
            if match
        ]
        self.assertTrue(pushes, f"{self.workflow.name} pushes nothing")
        for push, targets in pushes:
            with self.subTest(push=push):
                self.assertEqual(
                    targets,
                    {self.branch},
                    f"{self.workflow.name} pushes somewhere other than {self.branch}: {push}",
                )

    def test_no_other_job_pushes_to_the_branch(self) -> None:
        """The claim is "exactly one", which is a claim about every other job.

        Reading only the named workflow leaves the count unchecked: a second
        job can take `contents: write`, be documented in the same bullet as a
        write path, and push to the branch, and every other assertion here
        still passes while "exactly one" has stopped being true.
        """
        self.assertEqual(
            branch_pushes(self.branch),
            {(self.workflow.name, self.job)},
            f"docs/SECURITY-AI.md claims exactly one automated push to "
            f"{self.branch}, but these jobs push to it",
        )

    def test_it_verifies_before_it_pushes(self) -> None:
        self.assertIn("verifies before pushing", self.bullet)
        steps = workflow_steps(self.workflow)
        # The verifier is whatever the workflow itself writes the formula with,
        # read off its own `--update` invocation rather than named here: the
        # claim is that the job re-checks what it just wrote, so the two have
        # to be the same program.
        writers = {
            match.group("script")
            for _name, body in steps
            for line in body
            if (match := re.search(r"(?P<script>\S+\.py)\s+--update\b", line))
        }
        self.assertEqual(
            len(writers),
            1,
            f"{self.workflow.name} writes the formula with {writers or 'nothing'}; "
            "this module joins one writer to its verification",
        )
        checker = re.compile(rf"{re.escape(next(iter(writers)))}\s+--check\b")
        verify = [
            position
            for position, (_name, body) in enumerate(steps)
            if any(checker.search(line) for line in body)
        ]
        push = [
            position
            for position, (_name, body) in enumerate(steps)
            if any(line.strip().startswith("git push ") for line in body)
        ]
        self.assertTrue(
            verify,
            f"{self.workflow.name} never re-checks the formula it wrote, so nothing "
            "verifies the sha256 it pushes",
        )
        self.assertTrue(push, f"{self.workflow.name} has no step pushing")
        self.assertLess(
            min(verify),
            min(push),
            f"{self.workflow.name} verifies the formula after pushing it, not before",
        )

    def test_risk_tiers_agrees_that_it_is_tier_four(self) -> None:
        match = re.search(r"Tier (\d+)", self.bullet)
        self.assertIsNotNone(match, "the write-path bullet no longer states a tier")
        tier = match.group(1)
        heading = re.compile(rf"^## Tier {tier} [-—]")
        collected: list[str] = []
        capturing = False
        for line in RISK_TIERS.read_text().splitlines():
            if heading.match(line):
                capturing = True
                continue
            if capturing and line.startswith("## "):
                break
            if capturing:
                collected.append(line)
        self.assertTrue(collected, f"docs/risk-tiers.md has no Tier {tier} section")
        self.assertIn(
            self.workflow.name,
            flatten(" ".join(collected)),
            f"docs/SECURITY-AI.md says {self.workflow.name} is Tier {tier}, but "
            f"docs/risk-tiers.md's Tier {tier} does not name it",
        )


class UnverifiedDependencyTests(unittest.TestCase):
    """Actions are pinned by SHA; the cosign RPM is verified against a published
    checksum before install."""

    def setUp(self) -> None:
        self.bullet = bullet_naming(
            bullets(section("Never do these, whatever is asked")), "unpinned"
        )

    def test_every_workflow_action_is_pinned_by_sha(self) -> None:
        self.assertIn("pinned by SHA", self.bullet)
        for path in workflow_paths():
            for line in path.read_text().splitlines():
                match = USES.match(line)
                if match is None or match.group("action").startswith("./"):
                    continue
                with self.subTest(workflow=path.name, action=match.group("action")):
                    self.assertRegex(
                        match.group("ref"),
                        r"^[0-9a-f]{40}$",
                        f"{path.name} pins {match.group('action')} to a ref, not a commit",
                    )

    def test_the_cosign_rpm_download_records_a_checksum(self) -> None:
        self.assertIn("cosign RPM is verified", self.bullet)
        pinned = maintenance_audit.iter_pinned_downloads(CONTAINERFILE.read_text())
        cosign = [entry for entry in pinned if "cosign" in entry[0]]
        self.assertTrue(
            cosign,
            "no checksum-pinned cosign download in the Containerfile; the document "
            "says the RPM is verified against a published checksum",
        )
        for url, _dest, digest in cosign:
            with self.subTest(url=url):
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_the_checksum_is_verified_before_the_rpm_is_installed(self) -> None:
        run = cosign_run_instruction()
        verify = run.find("sha256sum -c")
        install = run.find("install /tmp/cosign.rpm")
        self.assertNotEqual(verify, -1, "the cosign RUN no longer verifies a checksum")
        self.assertNotEqual(install, -1, "the cosign RUN no longer installs the RPM it downloaded")
        self.assertLess(
            verify,
            install,
            "the cosign RPM is installed before its checksum is checked, which is the "
            "opposite of what docs/SECURITY-AI.md tells an agent is enforced",
        )


class GeneratedRepositoryTests(unittest.TestCase):
    """What it generates is what other people run."""

    def test_the_pin_tables_the_document_names_are_the_tool_s(self) -> None:
        text = flatten(doc_text())
        source = (ROOT / "atomic_image_builder.py").read_text()
        for table in ("ACTION_PINS", "ACTION_REF_PINS"):
            with self.subTest(table=table):
                self.assertIn(table, text)
                self.assertTrue(
                    re.search(rf"^{table}\s*[:=]", source, re.MULTILINE),
                    f"{table} is not defined at module level in atomic_image_builder.py",
                )

    def test_the_snapshot_directory_the_document_names_is_populated(self) -> None:
        self.assertIn("template_snapshots/", flatten(doc_text()))
        self.assertTrue(any(TEMPLATES.rglob("*.yml")), "template_snapshots/ ships no workflow")

    def test_every_template_workflow_still_signs_what_it_builds(self) -> None:
        self.assertIn("weakens signing", flatten(doc_text()))
        for path in sorted(TEMPLATES.glob("*/.github/workflows/build.yml")):
            with self.subTest(template=path.parent.parent.parent.name):
                text = path.read_text()
                self.assertTrue(
                    re.search(r"\bcosign\s+sign\b", text) or "cosign_private_key" in text,
                    f"{path} no longer signs the image it publishes",
                )

    def test_the_pull_never_flag_the_document_protects_still_exists(self) -> None:
        self.assertIn("--pull=never", flatten(doc_text()))
        carriers = [path for path in TEMPLATES.rglob("*") if path.is_file() and "--pull=never" in path.read_text(errors="ignore")]
        self.assertTrue(
            carriers,
            "no shipped template carries --pull=never, so the document warns against "
            "dropping a flag that is already gone",
        )


class UntrustedInputTests(unittest.TestCase):
    """The scripts the document says parse GitHub API responses still do."""

    def test_each_named_parser_reads_the_github_api(self) -> None:
        bullet = bullet_naming(
            bullets(section("Untrusted input an agent will encounter")), "GitHub API"
        )
        named = [literal for literal in BACKTICKED.findall(bullet) if literal.endswith(".py")]
        self.assertTrue(named, "the GitHub API bullet names no script")
        for script in named:
            with self.subTest(script=script):
                path = ROOT / script
                self.assertTrue(path.is_file(), f"{script} does not exist")
                source = path.read_text()
                # "Reads github.com over urllib" rather than "mentions
                # api.github.com": homebrew_formula.py takes a release tarball
                # rather than a JSON document, and both are the remote input
                # the bullet is about.
                self.assertIn(
                    "urllib.request",
                    source,
                    f"{script} no longer fetches anything remote, so the bullet naming it "
                    "as a parser of GitHub responses describes something else",
                )
                self.assertIn(
                    "github.com",
                    source,
                    f"{script} no longer reads github.com",
                )


if __name__ == "__main__":
    unittest.main()
