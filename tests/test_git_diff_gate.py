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
    ("git diff -aOorder1 --name-only", "reads a further path, with -O clustered behind -a"),
    ("git diff -aO order1", "reads a further path, with -O last in a cluster"),
    ("git log -pO order1", "reads a further path from git log, clustered"),
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
    ("git diff --outpu{t,t}=/tmp/out HEAD", "brace-expands into --output before git runs"),
    ("git diff --no-inde{x,x} a b", "brace-expands into --no-index before git runs"),
    ("git diff -aO{,}order1", "brace-expands into a clustered -O before git runs"),
    ("git diff {/etc/shadow,x}", "brace-expands into an absolute path before git runs"),
    ("git diff HEAD^#x /etc/passwd", "hides an outside operand behind a mid-word #"),
    ("git diff --stat#x /etc/passwd /dev/null", "hides --no-index operands behind a mid-word #"),
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
    "git log -SOAuth -p",
    "git log -GOpen --oneline",
    "git log -L:Open:docs/quality.md",
    "git diff -U0 -w",
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


    def test_git_reads_the_order_file_from_a_cluster_of_short_options(self) -> None:
        # `-aOorder1` is one shell word, and git reads it as `-a -O order1`.
        # A gate that matches only the start of the word passes it, and git
        # opens the order file all the same (#329). Staged files, no commit,
        # so no identity is needed in the temporary repository.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "a.txt").write_text("a\n")
            (repo / "b.txt").write_text("b\n")
            (repo / "order1").write_text("b.txt\n")
            subprocess.run(["git", "-C", str(repo), "add", "a.txt", "b.txt"], check=True)
            result = subprocess.run(
                ["git", "-C", str(repo), "diff", "--cached", "-aOorder1", "--name-only"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(
                result.stdout.split(),
                ["b.txt", "a.txt"],
                "git no longer honors -O when it is clustered behind another short "
                "option; the cluster walk in refused_short() may be more than is needed",
            )
        self.assertIsNotNone(
            gate.refusal("git diff --cached -aOorder1 --name-only"),
            "the command just shown to read an order file is not refused",
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

    def test_a_clustered_short_option_is_refused_like_its_bare_spelling(self) -> None:
        # git bundles single-letter options into one word, so `-O` can sit
        # behind any boolean letter and still name an order file.
        for token in ("-O", "-Oorder1", "-aO", "-aOorder1", "-pRO/tmp/order"):
            with self.subTest(token=token):
                self.assertTrue(gate.refused_short(token))

    def test_a_value_that_contains_the_letter_is_not_an_option(self) -> None:
        # The first value-taking letter ends the options; what follows it is
        # that option's value, so the `O` in `-SOAuth` is text to search for.
        for token in ("-SOAuth", "-GOpen", "-L:Open:file", "-U0", "-20", "--output", "-", "a"):
            with self.subTest(token=token):
                self.assertFalse(gate.refused_short(token))

    def test_every_valued_short_option_is_one_git_reads_a_value_for(self) -> None:
        # VALUED_SHORT is the one place a wrong entry opens a gap: a boolean
        # letter listed there would stop the walk before an `O` behind it.
        # Each entry is checked against git's own parsing of `-<letter>Omissing`
        # with no such file present. A boolean letter leaves `-Omissing` to be
        # read next and git fails to open the order file, whatever else the
        # command does; a value-taking one swallows `Omissing` as its value and
        # never looks for the file. The order-file complaint is the signal, not
        # the output or the exit code: a boolean like `-s` also fails for
        # clashing with an output format, and an empty stdout would pass it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "a.txt").write_text("a\n")
            subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)

            def probe(letter: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(repo), "diff", "--cached", f"-{letter}Omissing"],
                    capture_output=True,
                    text=True,
                ).stderr

            # `-a` is a boolean, so this is what the signal looks like when it
            # fires. If git rewords the message, this fails rather than every
            # check below passing for the wrong reason.
            self.assertIn("orderfile", probe("a"))
            for letter in sorted(gate.VALUED_SHORT - {"O"}):
                with self.subTest(letter=letter):
                    self.assertNotIn(
                        "orderfile",
                        probe(letter),
                        f"git looked for an order file after -{letter}, so -{letter} does "
                        "not take a value and must leave VALUED_SHORT",
                    )

    def test_a_brace_word_is_refused_because_bash_expands_it_first(self) -> None:
        # shlex does no brace expansion, so the gate reads `--outpu{t,t}=x` as
        # one word while bash hands git `--output=x --output=x`. Any refused
        # spelling can be reassembled this way, so a brace bash would expand
        # in a git word is refused rather than expanded.
        for token in ("--outpu{t,t}=x", "-aO{,}order", "{/etc/passwd,x}", "--no-inde{x,x}"):
            with self.subTest(token=token):
                self.assertTrue(gate.brace_would_expand(token))
        for token in ("--output=x", "HEAD..main", "-aOorder", "docs/quality.md"):
            with self.subTest(token=token):
                self.assertFalse(gate.brace_would_expand(token))

    def test_a_brace_bash_would_not_expand_is_left_alone(self) -> None:
        # Bash expands a brace only when a comma or a `..` range sits inside
        # it; any other brace is a literal, and git's own `@{...}` revision
        # syntax is spelled with exactly that. `git diff HEAD@{1}` is the
        # ordinary diff against the previous commit and reaches none of the
        # arguments this hook refuses, so a gate that refused it was a false
        # positive with a real cost. The last case pins that a `{` which
        # never closes is a literal too.
        for command in (
            "git diff HEAD@{1}",
            "git diff HEAD@{1} -- docs/quality.md",
            "git log main@{upstream} -1",
            "git rev-parse @{-1}",
            "git log @{2.days.ago} -1",
            "git log HEAD@{1 -1",
        ):
            with self.subTest(command=command):
                self.assertIsNone(
                    gate.refusal(command),
                    f"{command!r} is refused, and bash never expands its brace",
                )

    def test_the_brace_test_is_what_bash_would_expand_not_the_spelling(self) -> None:
        # The line is drawn where bash draws it, and errs toward refusing.
        # `@{1,2}` reads as revision syntax and is two words to bash; `{x..x}`
        # is a one-element sequence that rebuilds the flag; a comma nested one
        # level down still expands (`{{a,b}}` is `{a} {b}`); and `${VAR}` is a
        # runtime-built argument it cannot inspect, refused as before. Then
        # the mismatched form: bash pairs a `{` with the last `}` it can, so
        # `{--src-prefix=x},--no-index}` expands to `--src-prefix=x}` and
        # `--no-index`, and a depth counter that closed the brace at the
        # first `}` never saw the comma. A quoted comma or a quoted operator
        # inside the brace is still part of the word bash expands. Last, a
        # `..` between two reflog entries has the refused shape and is refused
        # although bash would leave it alone; the message names the spelling
        # to use.
        for command in (
            "git diff HEAD@{1,2}",
            "git diff --no-inde{x..x} /dev/null ./LICENSE",
            "git diff {{/dev/null,./cosign.key}}",
            "git diff ${SECRET} HEAD",
            "git diff {--src-prefix=x},--no-index} .env cosign.key",
            "git log {--format=%h},--output=.claude/settings.json} -1",
            'git diff {README.md",",cosign.key}',
            "git diff {/tmp/reference';',./cosign.key}",
            "git log HEAD@{2}..HEAD@{1}",
        ):
            with self.subTest(command=command):
                reason = gate.refusal(command)
                self.assertIsNotNone(reason, f"{command!r} is let through")
                self.assertIn("brace", reason)
        self.assertIn("HEAD~2..HEAD~1", gate.refusal("git log HEAD@{2}..HEAD@{1}") or "")

    # Git's revision syntax. Bash leaves each of these alone and the hook
    # must too; `HEAD@{1` pins that an unclosed brace is a literal as well.
    LITERAL_BRACE_WORDS = (
        "HEAD@{1}",
        "main@{upstream}",
        "@{-1}",
        "@{2.days.ago}",
        "HEAD@{1",
    )

    # The brace rule's corpus: the literal set, the ordinary expansions, the
    # two bypasses found in review of the sibling ports (mismatched braces, a
    # quoted operator inside the brace), quoted and escaped commas, nesting,
    # ranges, `${VAR}`, mismatched forms in both directions, braces after
    # --output, and quoted jq/awk programs that bash leaves alone. Each word
    # is inserted verbatim into a bash script, so the quoting is bash's.
    BRACE_CORPUS = LITERAL_BRACE_WORDS + (
        "HEAD@{2}..HEAD@{1}",
        "{a,b}",
        "{1..3}",
        "x{1..3}y",
        "a{,b}",
        "{{a,b}}",
        "--no-inde{x,x}",
        "--outpu{t,t}=FILE",
        "HEAD@{1,2}",
        "{--src-prefix=x},--no-index}",
        "{a},b}",
        "{/tmp/reference';',./cosign.key}",
        '{a",",b}',
        "{a\\,b,c}",
        '"{a,b}"',
        "'{a,b}'",
        "{a,b",
        "{a,b}}",
        "{{a,b}",
        "${OPERANDS}",
        "--output={a,b}",
        "--output=x{,}",
        "'{print $1}'",
        "'{a:1}'",
        "'{a: .x, b: .y}'",
    )

    @staticmethod
    def bash_expands(word: str) -> bool:
        """Whether bash turns `word` into more than one word.

        The word is inserted verbatim into the script text on purpose: the
        corpus is this file's, and the point is to hand bash the spelling an
        agent would type. `OPERANDS` is set so that `${OPERANDS}` splits into
        two words the way a runtime-built argument would.
        """
        result = subprocess.run(
            ["bash", "--norc", "--noprofile", "-c", 'printf "%s\\0" ' + word],
            capture_output=True,
            env={"PATH": os.environ.get("PATH", ""), "OPERANDS": "/dev/null ./cosign.key"},
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"bash could not run {word!r}: {result.stderr!r}")
        return result.stdout.count(b"\0") > 1

    def test_the_brace_rule_against_bash_rather_than_a_label(self) -> None:
        # Bash is the ground truth. Every corpus word bash expands must be
        # refused, and every word of the literal set must be allowed. A word
        # in neither class is only held to the first rule, so an over-refusal
        # there is not a failure. The counts keep the check from going
        # vacuous if the corpus shrinks or bash reads it differently.
        self.assertGreaterEqual(len(self.BRACE_CORPUS), 25)
        self.assertEqual(len(set(self.BRACE_CORPUS)), len(self.BRACE_CORPUS))
        expanding = 0
        for word in self.BRACE_CORPUS:
            with self.subTest(word=word):
                if not self.bash_expands(word):
                    continue
                expanding += 1
                reason = gate.refusal(f"git diff {word}")
                self.assertIsNotNone(reason, f"bash expands {word!r}; the hook let it through")
                self.assertIn("brace", reason or "")
        self.assertGreaterEqual(expanding, 15)
        for word in self.LITERAL_BRACE_WORDS:
            with self.subTest(word=word):
                self.assertFalse(self.bash_expands(word), f"bash expands {word!r}")
                self.assertIsNone(gate.refusal(f"git log {word} -1"))

    def test_a_brace_outside_a_git_invocation_is_not_gated(self) -> None:
        # The gate is about git's arguments; a jq or awk program is not one.
        for command in ("jq '{a: .x, b: .y}' x.json", "awk '{print $1}' README.md"):
            with self.subTest(command=command):
                self.assertIsNone(gate.refusal(command))

    def test_a_mid_word_comment_char_does_not_hide_later_operands(self) -> None:
        # bash starts a comment only at a `#` that begins a word; shlex's
        # default comment character drops everything after any `#`. If the gate
        # kept that default it would read `git diff HEAD^` and never see the
        # `/etc/passwd` operand bash passes to git.
        tokens = gate.tokenize("git diff HEAD^#x /etc/passwd")
        self.assertIn("/etc/passwd", tokens)
        self.assertIsNotNone(gate.refusal("git diff HEAD^#x /etc/passwd"))

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
