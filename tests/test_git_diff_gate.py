"""Join `.claude/hooks/gate_git_diff.py` to the permission model it completes.

`.claude/settings.json` allows `Bash(git diff:*)` and `Bash(git log:*)` and
denies reading `cosign.key`, `.env` and the key patterns. The second claim is
only true of the first while `git` cannot open a path itself, and by default
it can -- `--no-index` reads any file on disk and `--output` writes one. The
hook is what closes that, so three things have to hold together and none of
them is visible from the others:

1. the hook refuses the arguments that reach outside the index, and leaves
   ordinary `git diff` / `git log` alone -- a gate that refuses everything
   gets switched off, which is the same as not having one;
2. `.claude/settings.json` still registers it, and `.gitignore` still ships
   the file, because a registration pointing at a file the repository does
   not contain is a check that silently never runs;
3. `docs/SECURITY-AI.md` still names it in the section that lists what is
   enforced rather than trusted.

The first test in ReachTests runs the real `git` to show the primitive the
hook exists for is not hypothetical. If a future git stops diffing arbitrary
paths, that test fails and this module can shrink -- which is worth knowing
rather than assuming.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude/hooks/gate_git_diff.py"
SETTINGS = ROOT / ".claude/settings.json"
GITIGNORE = ROOT / ".gitignore"
DOC = ROOT / "docs/SECURITY-AI.md"


def load_hook():
    """Import the hook by path: it sits outside the package, like the tool
    that runs it does."""
    spec = importlib.util.spec_from_file_location("gate_git_diff", HOOK)
    if spec is None or spec.loader is None:
        raise AssertionError(f"{HOOK} cannot be imported as a module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_hook()


# Commands the hook has to refuse, each with the reach that earns the refusal.
REFUSED_COMMANDS = (
    ("git diff --no-index /dev/null ./cosign.key", "reads any file on disk"),
    ("git diff --no-index a b", "reads any file on disk"),
    ("git diff --output=/tmp/out --no-index a b", "writes any file"),
    ("git log --output=/tmp/out -1", "writes any file"),
    ("git diff -O/tmp/orderfile", "reads a further path"),
    ("git diff --ext-diff", "runs a configured external program"),
    ("git diff -- ../sibling/cosign.key", "names a path outside the checkout"),
    ("git diff -- /etc/shadow", "names an absolute path"),
    ("git log > /tmp/anywhere", "redirects into a path outside the checkout"),
    ("git status && git diff --no-index a b", "hides behind an earlier command"),
    ("echo x | git diff --no-index a b", "hides in a pipeline"),
    ("x=$(git diff --no-index a b)", "hides in a command substitution"),
    ("GIT_EXTERNAL_DIFF=/tmp/evil git diff", "names a program for git to run"),
    ("GIT_DIR=/tmp/other/.git git log", "re-points the repository"),
    ("git -c diff.external=/tmp/evil diff", "injects configuration"),
    ("git --git-dir=/tmp/other/.git log", "re-points the repository"),
    ("git diff --out=/tmp/out", "abbreviates a refused option"),
    ("git diff 'unterminated", "cannot be parsed, so it is not let through"),
)

# Commands it has to leave alone. Everything an ordinary session runs.
ALLOWED_COMMANDS = (
    "git diff",
    "git diff --stat",
    "git diff -- docs/quality.md",
    "git diff --quiet -- tests/",
    "git diff HEAD~1",
    "git diff -C -M",
    "git log --oneline -20",
    "git log -p --stat HEAD..main",
    "git log -c -p",
    "git status --porcelain",
    "git diff > out.patch",
    "git diff --output-indicator-new=x",
    "PAGER=cat git log",
    "git diff --stat | head -20",
    "ruff check",
    "python3 -m unittest discover -s tests",
)


def hook_input(command: str, tool: str = "Bash") -> str:
    return json.dumps({"tool_name": tool, "tool_input": {"command": command}})


def run_main(payload: str) -> tuple[int, str]:
    """main() with `payload` on stdin, returning (exit code, stderr)."""
    stdin = sys.stdin
    sys.stdin = io.StringIO(payload)
    err = io.StringIO()
    try:
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = gate.main()
    finally:
        sys.stdin = stdin
    return code, err.getvalue()


class ReachTests(unittest.TestCase):
    def test_git_really_does_read_an_arbitrary_file(self) -> None:
        # The whole gate rests on this. Written against a stand-in rather
        # than a real key, for the reason docs/SECURITY-AI.md gives.
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "cosign.key"
            secret.write_text("STAND-IN-NOT-A-KEY\n")
            result = subprocess.run(
                ["git", "diff", "--no-index", "/dev/null", str(secret)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertIn(
                "STAND-IN-NOT-A-KEY",
                result.stdout,
                "git diff --no-index no longer prints a file outside the index; the hook's "
                "reason for existing has changed and this module should be revisited",
            )
        self.assertIsNotNone(
            gate.refusal(f"git diff --no-index /dev/null {secret}"),
            "the command just shown to read an arbitrary file is not refused",
        )


class RefusalTests(unittest.TestCase):
    def test_each_reaching_command_is_refused(self) -> None:
        for command, reach in REFUSED_COMMANDS:
            with self.subTest(command=command):
                self.assertIsNotNone(
                    gate.refusal(command),
                    f"{command!r} is let through, and it {reach}",
                )

    def test_every_refusal_says_which_argument_earned_it(self) -> None:
        # A refusal the model cannot act on gets retried differently rather
        # than reported, so the token has to appear in the reason.
        for command, _ in REFUSED_COMMANDS:
            if "unterminated" in command:
                continue
            with self.subTest(command=command):
                reason = gate.refusal(command)
                self.assertTrue(reason)
                # An environment prefix is named by its variable, not by the
                # value assigned to it, so both spellings count as naming it.
                words = [
                    part
                    for word in command.split()
                    if word not in {"git", "&&", "|"}
                    for part in (word, word.split("=", 1)[0])
                ]
                self.assertTrue(
                    any(word in reason for word in words),
                    f"the refusal of {command!r} names none of its arguments: {reason}",
                )

    def test_ordinary_commands_pass_through(self) -> None:
        for command in ALLOWED_COMMANDS:
            with self.subTest(command=command):
                self.assertIsNone(
                    gate.refusal(command),
                    f"{command!r} is refused; a gate that blocks ordinary work gets removed",
                )

    def test_a_revision_range_is_not_a_parent_directory(self) -> None:
        # `..` is a path component in one and a range operator in the other.
        # Conflating them would refuse the commonest git log argument there is.
        self.assertFalse(gate.unsafe_operand("HEAD..main"))
        self.assertFalse(gate.unsafe_operand("v1.0...v2.0"))
        self.assertTrue(gate.unsafe_operand("../sibling/file"))
        self.assertTrue(gate.unsafe_operand("docs/../../etc/passwd"))

    def test_an_abbreviated_option_is_refused_like_its_full_spelling(self) -> None:
        # git resolves any unambiguous prefix, so matching only the full
        # spelling would leave the shortest working abbreviation permitted.
        for token in ("--no-index", "--no-ind", "--output", "--outp", "--ext-diff", "--ext"):
            with self.subTest(token=token):
                self.assertTrue(gate.refused_long(token))

    def test_an_option_that_merely_starts_the_same_is_not_refused(self) -> None:
        for token in ("--output-indicator-new=x", "--no-indent-heuristic", "--", "--stat"):
            with self.subTest(token=token):
                self.assertFalse(gate.refused_long(token))

    def test_a_global_option_is_read_only_before_the_subcommand(self) -> None:
        # `git -c x=y diff` injects configuration; `git log -c` is a diff
        # format. Position is the only thing that separates them.
        self.assertEqual(gate.global_refusal(["-c", "diff.external=x", "diff"]), "-c")
        self.assertIsNone(gate.global_refusal(["log", "-c", "-p"]))
        self.assertIsNone(gate.global_refusal(["diff", "-C"]))


class MainTests(unittest.TestCase):
    def test_a_refused_command_exits_two_and_explains(self) -> None:
        code, err = run_main(hook_input("git diff --no-index /dev/null ./cosign.key"))
        self.assertEqual(code, 2, "2 is the exit code that blocks the call")
        self.assertIn("--no-index", err)

    def test_an_ordinary_command_exits_zero_and_says_nothing(self) -> None:
        code, err = run_main(hook_input("git diff -- docs/quality.md"))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_another_tool_is_not_this_hook_s_business(self) -> None:
        payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}})
        self.assertEqual(run_main(payload)[0], 0)

    def test_input_it_cannot_read_is_refused_rather_than_ignored(self) -> None:
        self.assertEqual(run_main("not json at all")[0], 2)
        self.assertEqual(run_main(json.dumps({"tool_name": "Bash", "tool_input": {"command": 7}}))[0], 2)

    def test_a_bash_call_without_a_command_is_let_through(self) -> None:
        self.assertEqual(run_main(json.dumps({"tool_name": "Bash", "tool_input": {}}))[0], 0)


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = json.loads(SETTINGS.read_text())

    def test_the_hook_file_exists_and_is_tracked(self) -> None:
        self.assertTrue(HOOK.is_file(), f"{HOOK} is missing")
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", ".claude/hooks/"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\0")
        self.assertIn(
            ".claude/hooks/gate_git_diff.py",
            tracked,
            "the hook is not tracked, so a fresh clone registers a check it does not have",
        )

    def test_gitignore_re_includes_the_hook_directory(self) -> None:
        # .gitignore excludes .claude/* wholesale, and git will not descend
        # into an excluded directory, so the re-include is what makes the
        # tracking above possible at all.
        lines = [line.strip() for line in GITIGNORE.read_text().splitlines()]
        self.assertIn(".claude/*", lines)
        self.assertIn("!.claude/hooks/", lines)

    def test_settings_registers_the_hook_for_bash(self) -> None:
        pre = self.settings.get("hooks", {}).get("PreToolUse", [])
        commands = [
            entry.get("command", "")
            for group in pre
            if group.get("matcher") == "Bash"
            for entry in group.get("hooks", [])
        ]
        self.assertTrue(
            any("gate_git_diff.py" in command for command in commands),
            "no PreToolUse hook on Bash runs gate_git_diff.py, so every allowed "
            "git diff argument reaches git unchecked",
        )

    def test_the_registered_path_is_the_file_this_module_tests(self) -> None:
        pre = self.settings["hooks"]["PreToolUse"]
        command = next(
            entry["command"]
            for group in pre
            for entry in group["hooks"]
            if "gate_git_diff.py" in entry["command"]
        )
        self.assertIn(
            ".claude/hooks/gate_git_diff.py",
            command,
            f"the registration runs {command!r}, which is not the path this module checks",
        )
        # $CLAUDE_PROJECT_DIR, not a relative path: the hook runs with the
        # session's working directory, which is not always the checkout root.
        self.assertIn("$CLAUDE_PROJECT_DIR", command)

    def test_the_commands_the_hook_guards_are_still_allowed_outright(self) -> None:
        # If these ever move to `ask` or `deny`, the hook is guarding nothing
        # and this module is measuring a path no session takes.
        allow = self.settings["permissions"]["allow"]
        for rule in ("Bash(git diff:*)", "Bash(git log:*)"):
            self.assertIn(rule, allow)

    def test_the_read_denials_the_hook_protects_are_still_there(self) -> None:
        deny = self.settings["permissions"]["deny"]
        for rule in ("Read(./cosign.key)", "Read(./.env)"):
            self.assertIn(
                rule,
                deny,
                f"{rule} is gone; the hook's stated reason for refusing --no-index "
                "no longer describes this file",
            )

    def test_the_hook_is_executable(self) -> None:
        # It carries a shebang and is run by path in CONTRIBUTING's terms; a
        # non-executable file with a shebang is a trap for the next reader.
        self.assertTrue(os.access(HOOK, os.X_OK), f"{HOOK} is not executable")


class DocumentTests(unittest.TestCase):
    def test_the_enforcement_section_names_the_hook(self) -> None:
        section = DOC.read_text().split("## What is enforced rather than trusted", 1)
        self.assertEqual(len(section), 2, "docs/SECURITY-AI.md's enforcement section is gone")
        body = section[1].split("\n## ", 1)[0]
        self.assertIn(
            ".claude/hooks/gate_git_diff.py",
            body,
            "the enforcement section no longer names the hook, so the document claims a "
            "denial the Bash surface does not have",
        )

    def test_the_options_the_document_names_are_the_ones_refused(self) -> None:
        # The document is where a reader learns what the gate covers. A list
        # there that the code does not implement is worse than no list.
        body = DOC.read_text()
        for option in ("--no-index", "--output", "--ext-diff"):
            with self.subTest(option=option):
                self.assertIn(option, body)
                self.assertTrue(gate.refused_long(option))


if __name__ == "__main__":
    unittest.main()
