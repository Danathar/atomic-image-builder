"""Every environment variable a shipped script reads is named in a document.

Script: tests/test_env_vars_documented.py
What: Reads every tracked Python file and every tracked shell script outside
      `tests/` and `template_snapshots/`, collects the environment variables
      each one reads, and fails for any name that no tracked Markdown file
      (outside the same two folders) mentions and that is not on `ALLOWED`.
      Python reads are `os.environ[...]`, `os.environ.get(...)`,
      `os.getenv(...)` and `"NAME" in os.environ`. A shell read is an
      upper-case `${NAME:-...}`-style expansion, or an upper-case name the
      script expands but never assigns.
Why: A setting that changes what a script does but appears in no document is
     one only someone reading the source can find. Audits kept finding them
     one at a time (Danathar/atomic-image-builder#868); this catches the next
     one in the pull request that adds it.
Goal: Adding a variable fails here until a document names it, or it goes on
      `ALLOWED` with a reason, in the same pull request.

Limitation: the shell reader is a text scan, not a parser. A name built at
runtime (`${!name}`, `eval`) or a read inside a string handed to another
shell is not seen, and a Python read through an alias other than `environ`
or `getenv` is not either.
"""

import ast
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Not shipped, or not ours: the tests themselves, and the pinned upstream
# template copies this repo must not edit.
EXCLUDED_PREFIXES = ("tests/", "template_snapshots/")

# Variables a script may read without any document naming them, each with the
# reason. Use it for names the platform sets (a CI runner, the terminal), not
# for a setting a user or maintainer is meant to change -- that one belongs in
# a document. An entry no script reads any more fails
# test_every_allowed_name_is_still_read.
ALLOWED: dict[str, str] = {}

# Names bash itself sets. A script reads them without assigning them, but
# they are not settings anyone passes in.
SHELL_PROVIDED = frozenset(
    {
        "BASH_REMATCH",
        "BASH_SOURCE",
        "BASHPID",
        "EUID",
        "FUNCNAME",
        "HOME",
        "IFS",
        "LINENO",
        "OLDPWD",
        "PATH",
        "PIPESTATUS",
        "PPID",
        "PWD",
        "RANDOM",
        "SECONDS",
        "UID",
    }
)

SHEBANG = re.compile(r"^#!.*\b(?:ba)?sh\b")
NAME = r"[A-Z_][A-Z0-9_]*"
SHELL_EXPANSION = re.compile(r"\$\{?(" + NAME + r")")
# `${NAME:-x}`, `${NAME-x}`, `${NAME:+x}`, `${NAME:=x}`, ...: an expansion
# written to cope with the name being unset, which under `set -u` is how a
# script reads something it expects from its caller.
SHELL_DEFAULTED = re.compile(r"\$\{(" + NAME + r"):?[-+=?]")
SHELL_ASSIGNMENT = re.compile(
    r"(?:^|[\s;&|(])(?:(?:local|export|readonly|declare)(?:\s+-\w+)*\s+)?("
    + NAME
    + r")(?:\[[^]]*\])?\+?="
)
SHELL_FOR = re.compile(r"\bfor\s+(" + NAME + r")\s+in\b")
SHELL_READ = re.compile(r"\bread\b((?:\s+-\w+(?:\s+\S+)?)*)((?:\s+" + NAME + r")+)")


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout.decode("utf-8")
    return [
        name
        for name in out.split("\0")
        if name and not name.startswith(EXCLUDED_PREFIXES)
    ]


def _is_environ(node: ast.AST) -> bool:
    """`os.environ` or a bare `environ`."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return isinstance(node, ast.Name) and node.id == "environ"


def _constant_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def python_env_reads(source: str) -> set[str]:
    """Names a Python source reads from the environment by literal name."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            is_get = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_environ(func.value)
            )
            is_getenv = (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
                isinstance(func, ast.Name) and func.id == "getenv"
            )
            if is_get or is_getenv:
                name = _constant_str(node.args[0])
                if name is not None:
                    names.add(name)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _is_environ(node.value)
        ):
            name = _constant_str(node.slice)
            if name is not None:
                names.add(name)
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and _is_environ(right):
                    name = _constant_str(node.left)
                    if name is not None:
                        names.add(name)
    return names


def shell_env_reads(source: str) -> set[str]:
    """Upper-case names a shell script reads that its caller may have set."""
    lines = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    code = "\n".join(lines)
    assigned = set(SHELL_ASSIGNMENT.findall(code)) | set(SHELL_FOR.findall(code))
    for _options, targets in SHELL_READ.findall(code):
        assigned.update(targets.split())
    expanded = set(SHELL_EXPANSION.findall(code))
    defaulted = set(SHELL_DEFAULTED.findall(code))
    return (defaulted | (expanded - assigned)) - SHELL_PROVIDED


def env_reads(root: Path) -> dict[str, set[str]]:
    """{variable: {files that read it}} across the scanned scripts."""
    reads: dict[str, set[str]] = {}
    for rel in tracked_files(root):
        path = root / rel
        if not path.is_file():
            continue
        if rel.endswith(".py"):
            names = python_env_reads(path.read_text(encoding="utf-8"))
        else:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if not SHEBANG.match(text):
                continue
            names = shell_env_reads(text)
        for name in names:
            reads.setdefault(name, set()).add(rel)
    return reads


def documented(root: Path, names: set[str]) -> set[str]:
    """The subset of `names` that some tracked Markdown file mentions."""
    text = "\n".join(
        (root / rel).read_text(encoding="utf-8")
        for rel in tracked_files(root)
        if rel.endswith(".md") and (root / rel).is_file()
    )
    return {
        name
        for name in names
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text)
    }


class EnvVarsDocumentedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reads = env_reads(ROOT)

    def test_every_env_var_read_is_documented_or_allowed(self) -> None:
        names = set(self.reads)
        missing = sorted(names - documented(ROOT, names) - set(ALLOWED))
        self.assertEqual(
            missing,
            [],
            "These environment variables are read but no Markdown file names them:\n"
            + "\n".join(
                f"  {name}  (read in {', '.join(sorted(self.reads[name]))})"
                for name in missing
            )
            + "\nSay what each one does in the document a user or maintainer would look in, "
            "or add it to ALLOWED in this file with the reason no document needs it.",
        )

    def test_every_allowed_name_is_still_read(self) -> None:
        stale = sorted(set(ALLOWED) - set(self.reads))
        self.assertEqual(
            stale, [], f"Remove from ALLOWED; no script reads them now: {stale}"
        )

    def test_scan_finds_the_reads_it_is_known_to_make(self) -> None:
        # A scanner that quietly found nothing would pass the check above.
        expected = {
            "AIB_IMAGE": "contrib/aib",
            "AIB_SKIP_VERIFY": "contrib/aib",
            "AIB_ALLOW_UNVERIFIED_AUTH": "contrib/aib",
            "GH_TOKEN": "container/entrypoint.sh",
            "AIB_DISABLE_LOCAL_BUILD": "atomic_image_builder.py",
            "NO_COLOR": "atomic_image_builder.py",
            "GITHUB_TOKEN": "maintenance_audit.py",
        }
        for name, rel in expected.items():
            with self.subTest(name=name):
                self.assertIn(rel, self.reads.get(name, set()))


class PythonEnvReadsTests(unittest.TestCase):
    def test_each_read_form_is_found(self) -> None:
        source = (
            "import os\n"
            "from os import environ, getenv\n"
            "a = os.environ.get('A')\n"
            "b = os.environ['B']\n"
            "c = os.getenv('C', 'x')\n"
            "d = 'D' in os.environ\n"
            "e = environ.get('E')\n"
            "f = getenv('F')\n"
            "g = 'G' not in environ\n"
        )
        self.assertEqual(python_env_reads(source), set("ABCDEFG"))

    def test_writes_and_computed_names_are_not_reads(self) -> None:
        source = (
            "import os\n"
            "os.environ['W'] = '1'\n"
            "del os.environ['X']\n"
            "name = 'Y'\n"
            "os.environ.get(name)\n"
            "env = os.environ.copy()\n"
            "env['Z'] = '1'\n"
            "{}.get('Q')\n"
        )
        self.assertEqual(python_env_reads(source), set())


class ShellEnvReadsTests(unittest.TestCase):
    def test_defaulted_expansion_is_a_read_even_when_assigned(self) -> None:
        source = 'IMAGE="${IMAGE:-default}"\nif [ -n "${SKIP-}" ]; then :; fi\n'
        self.assertEqual(shell_env_reads(source), {"IMAGE", "SKIP"})

    def test_unassigned_expansion_is_a_read(self) -> None:
        self.assertEqual(shell_env_reads('echo "$FROM_CALLER"\n'), {"FROM_CALLER"})

    def test_assigned_names_are_not_reads(self) -> None:
        source = (
            "REPO=x\n"
            "local LOCAL_ONE=y\n"
            "export EXPORTED=z\n"
            "declare -a LIST=()\n"
            "ARR[0]=a\n"
            "COUNT+=1\n"
            "for ITEM in a b; do :; done\n"
            "read -r -p prompt ANSWER OTHER\n"
            'echo "$REPO $LOCAL_ONE $EXPORTED ${LIST[@]} ${ARR[0]} $COUNT $ITEM $ANSWER $OTHER"\n'
        )
        self.assertEqual(shell_env_reads(source), set())

    def test_comments_lowercase_and_shell_names_are_ignored(self) -> None:
        source = (
            "# set $DOCUMENTED_ONLY_IN_A_COMMENT to change things\n"
            'echo "$local_var ${BASH_REMATCH[1]} $HOME"\n'
        )
        self.assertEqual(shell_env_reads(source), set())


if __name__ == "__main__":
    unittest.main()
