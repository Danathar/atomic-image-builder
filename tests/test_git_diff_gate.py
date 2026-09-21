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

import fnmatch
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
    ("git diff HEAD >cosign.pub", "truncates the file the shell opens before git runs"),
    ("git diff > out.patch", "truncates a file in the checkout the same way"),
    ("git log -1 >> out", "appends to a file the shell opens before git runs"),
    ("git diff 2>err", "opens a file for stderr before git runs"),
    ("git diff &>/dev/null", "opens a path for both streams before git runs"),
    ("git diff HEAD >| x", "opens a path past noclobber before git runs"),
    ("git diff HEAD >&cosign.pub", "opens a path in the older &> spelling"),
    ("git diff HEAD <>cosign.pub", "opens a path read-write, creating it"),
    ("git diff HEAD > .claude/settings.json", "truncates the permission table"),
    ("git diff HEAD 2>&1 >cosign.pub", "hides a writing redirection behind a descriptor one"),
    (">cosign.pub git diff HEAD", "truncates the file with the redirection written first"),
    ("git status; >cosign.pub git diff HEAD", "hides the redirection-first form behind an allowed prefix"),
    ("2>err git log -1", "opens a file for stderr with the redirection written first"),
    (">> out git show HEAD", "appends with the redirection written first"),
    ("FOO=bar >out git diff HEAD", "writes with the redirection between an assignment and git"),
    (">/tmp/anywhere git log", "redirects outside the checkout with the redirection written first"),
    (
        ">cosign.pub git diff --no-index /dev/null ./cosign.key",
        "hid --no-index behind a redirection the scan took for the command name",
    ),
    ("git diff ';' >cosign.pub", "hid the redirection behind a quoted separator"),
    ("git diff '|' >cosign.pub", "hid the redirection behind a quoted pipe"),
    ("git diff \\; >cosign.pub", "hid the redirection behind an escaped separator"),
    ("git diff ';' --no-index /dev/null ./cosign.key", "hid --no-index behind a quoted separator"),
    ("git status; {fd}>cosign.pub git diff HEAD", "hid the redirection behind a {name} descriptor"),
    ("git diff HEAD {fd}>cosign.pub", "writes through a {name} descriptor"),
    ("git status; >$(printf cosign.pub) git diff HEAD", "hid the redirection behind a $(...) target"),
    ("git status; >`printf cosign.pub` git diff HEAD", "hid the redirection behind a backtick target"),
    ("git diff -- ~/.aws/credentials ~/.bashrc", "names two home files through a tilde bash expands"),
    ("git diff ~/.bashrc ~/.aws/credentials", "names two home files through a tilde without --"),
    ("git log -p -- ~/.ssh/config", "names a home file through a tilde in git log"),
    ("git diff -- ~root/.bashrc ./LICENSE", "names another user's home file through a tilde"),
    ("git diff $(echo /dev/null) ./cosign.key", "builds a --no-index operand from a substitution"),
    ("git diff `echo /dev/null` ./cosign.key", "builds a --no-index operand from a backtick"),
    ("git diff `printf -- --no-index` ./LICENSE ./cosign.key", "builds --no-index from a backtick"),
    ("git log --outpu$'\\x74'=cosign.pub -1", "builds --output from an ANSI-C escape"),
    ("G=/dev/null; git diff $G ./cosign.key", "builds a --no-index operand from a variable"),
    ('git diff "$G" ./cosign.key', "builds an operand from a variable double quotes do not quote"),
    ('git diff "$(echo /dev/null)" ./cosign.key', "builds an operand from a quoted substitution"),
    ("git diff ${G} ./cosign.key", "builds an operand from a braced variable"),
    ("echo x;(git diff --no-index /dev/null ./cosign.key)", "hid git behind a ;( shlex glued"),
    ("true&&(git diff --no-index /dev/null ./cosign.key)", "hid git behind an &&( shlex glued"),
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
    ("git diff <(true) ./cosign.key", "substitutes a /dev/fd path, so git diff implies --no-index"),
    ("git diff <(true) /etc/passwd", "hides an absolute operand behind the ) of a substitution"),
    ("git diff HEAD >(cat) -- README.md", "substitutes a /dev/fd path for git to write through"),
    ("git log -p <(true)", "substitutes a /dev/fd path into git log"),
    ("shellcheck contrib/aib >cosign.pub", "truncates the trust anchor through an allow-listed linter"),
    ("hadolint Containerfile >.claude/settings.json", "overwrites the permission table through hadolint"),
    ("python3 maintenance_audit.py --skip-upstream >> out", "appends through the allow-listed audit"),
    ("just --fmt --check 2>cosign.pub", "opens a file for stderr through the allow-listed formatter"),
    ("skopeo inspect docker://x &>cosign.pub", "opens a path for both streams through skopeo"),
    ("podman image exists x >|cosign.pub", "opens a path past noclobber through a three-word prefix"),
    ("gh search prs --repo x <>cosign.pub", "opens a path read-write through gh"),
    (">cosign.pub shellcheck contrib/aib", "truncates with the redirection before the linter's name"),
    ("git status; >cosign.pub podman images", "hides the redirection-first form behind an allowed prefix"),
    ("FOO=bar shellcheck contrib/aib >cosign.pub", "writes with an assignment before the linter"),
    ("time shellcheck contrib/aib >cosign.pub", "writes with a keyword before the linter"),
    ("shellcheck contrib/aib {fd}>cosign.pub", "writes through a {name} descriptor on the linter"),
    ("shellcheck $(git ls-files '*.sh') >cosign.pub", "carries the redirection across a substitution"),
    ("echo $(podman images >cosign.pub)", "writes from inside a substitution"),
    ("/usr/bin/shellcheck contrib/aib >cosign.pub", "names the linter by path"),
    ("shellcheck <(printf x) >cosign.pub", "carries the redirection across a process substitution"),
    (">`printf cosign.pub` shellcheck contrib/aib", "hid the linter behind a backtick target"),
    ("command -p shellcheck contrib/aib >cosign.pub", "hid the linter behind a wrapper's option"),
    ("env -i podman images >cosign.pub", "hid podman behind a wrapper's option"),
    ("shellcheck contrib/aib >cosign.pub # ok", "writes before a comment"),
    ("shellcheck contrib/aib '#' >cosign.pub", "writes past a quoted hash"),
    ("podman images >(cat >cosign.pub)", "writes from inside a process substitution argument"),
    (">(cat >cosign.pub) podman images", "writes from a process substitution before the name"),
    ("gh label list <(true)", "hands gh a process substitution"),
    ("shellcheck $(>cosign.pub)", "writes from inside a command substitution argument"),
    ("podman images `printf x >cosign.pub`", "writes from inside a backtick argument"),
    ("hadolint $F", "hands hadolint a word built at runtime"),
    ("PYTHONPATH=/tmp python3 maintenance_audit.py --skip-upstream", "puts a module ahead of the audit's imports"),
    ("LD_PRELOAD=x.so shellcheck contrib/aib", "loads code before the linter runs"),
    ("GH_HOST=other gh label list", "sends the token to another host"),
    ("CONTAINERS_CONF=f podman ps", "re-points podman through its environment"),
    ("2>`printf err` podman images >cosign.pub", "writes past a backtick target before the name"),
    ("shellcheck contrib/aib >(cat) >cosign.pub", "carries the redirection across an output substitution"),
    ("podman images <(true) 2>cosign.pub", "writes stderr past a process substitution"),
    ("shellcheck ./.env", "prints every unexported NAME=value line of a denied file"),
    ("shellcheck ./cosign.key", "prints the signing key's lines back as source lines"),
    ("shellcheck contrib/aib ./cosign.key", "hides the denied file behind a real script"),
    ("shellcheck secrets/.env", "prints a denied shape one directory down"),
    ("shellcheck deploy/tls.pem", "prints a file the Read(**/*.pem) rule denies"),
    ("shellcheck ~/.ssh/id_rsa", "names a home file through a tilde bash expands"),
    ("shellcheck /etc/shadow", "names an absolute path outside the checkout"),
    ("shellcheck ../sibling/cosign.key", "climbs out of the checkout to a denied file"),
    ("shellcheck {contrib/aib,.env}", "brace-expands into a denied file before shellcheck runs"),
    ("shellcheck -s bash ./.env", "hides the denied file behind an option's value"),
    ("shellcheck -e SC2034 -- ./.env", "hides the denied file after an end-of-options word"),
    ("command -p shellcheck ./.env", "hid the denied file behind a wrapper's option"),
    ("git status && shellcheck ./cosign.key", "hides behind an earlier command"),
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
    "git diff HEAD 2>&1",
    "git diff HEAD >&2",
    "git diff HEAD 1>&2",
    "git diff HEAD >&-",
    "git diff HEAD <<<''",
    "git diff HEAD | jq . > out",
    "echo x > out; git diff HEAD",
    "echo x >> out && git diff HEAD",
    ">out echo x; git diff HEAD",
    ">out cat f | git diff --stat",
    ">$(printf out) echo x; git diff HEAD",
    "{fd}>out echo x; git diff HEAD",
    "echo $(date) *.sh; git status",
    'git commit -m "a; b" | cat',
    "git diff -- 'a;b'",
    'echo "x)" ; git diff HEAD',
    "</dev/null git diff HEAD",
    "2>&1 git diff HEAD",
    ">&2 git diff HEAD",
    "git diff HEAD@{1}",
    "git log --format='%h $x'",
    'git log --format="%h \\$x"',
    "git log -G'\\$x' --oneline",
    "git diff HEAD -- \\$x",
    "echo $HOME; git diff HEAD",
    'echo "$(date)"; git diff HEAD',
    "x=$(git log -1); git diff HEAD",
    "for f in $(git diff --name-only HEAD); do echo $f; done",
    "x=$(git log -1);(git diff HEAD)",
    "git diff -- 'lit~eral'",
    "git diff HEAD -- x~",
    "git show HEAD:~/x",
    "ls ~/.bashrc; git diff HEAD",
    "git diff --output-indicator-new=x",
    "PAGER=cat git log",
    "git diff --stat | head -20",
    "ruff check",
    "python3 -m unittest discover -s tests",
    "shellcheck contrib/aib 2>&1 | tail -5",
    "hadolint Containerfile",
    "python3 maintenance_audit.py --skip-upstream",
    "just --fmt --check",
    "skopeo inspect docker://ghcr.io/x:latest | jq .Digest",
    "podman images --format '{{.Repository}}'",
    "gh search issues --repo Danathar/atomic-image-builder --json number",
    "shellcheck contrib/aib <contrib/aib",
    "podman logs c >&2",
    "podman ps 2>&-",
    # A command no allow rule covers prompts on its own, so a redirection on
    # it is not this hook's to refuse: the two exact rows (`ruff check`,
    # `python3 -m unittest discover -s tests`) carry no `:*`, and the last two
    # are refused by the Bash tool itself before any rule or hook sees them
    # ("does not accept compound statements with redirection", 2.1.267).
    "ruff check >cosign.pub",
    "python3 -m unittest discover -s tests >out",
    "python3 maintenance_audit.py >cosign.pub",
    "echo x >cosign.pub",
    "echo x >out; shellcheck contrib/aib",
    "shellcheck contrib/aib | tee out",
    ">out echo x; podman images",
    "shellcheck contrib/aib; { hadolint Containerfile; } >cosign.pub",
    "(shellcheck contrib/aib) >cosign.pub",
    "cat <(shellcheck contrib/aib) >out",
    "diff <(podman images) <(podman ps)",
    "shellcheck contrib/aib # output > file",
    "podman images # >(cat >cosign.pub)",
    "git diff HEAD # > cosign.pub",
    "shellcheck contrib/aib #comment\nhadolint Containerfile",
    "command -v shellcheck",
    "x=$(podman images); echo $x",
    "echo $(podman images)",
    "for f in $(gh label list --json name -q '.[].name'); do echo $f; done",
    "gh search issues --repo x 'a $b'",
    "FOO=1 echo x; podman images",
    "x=1; podman images",
    # The lint command CONTRIBUTING.md, the pull-request template and
    # ci.yml all name, verbatim. Refusing this would mean the check the
    # contributor is asked to run could not be run.
    "shellcheck -x contrib/aib container/entrypoint.sh tests/test_contrib_aib.sh"
    " tests/test_entrypoint.sh tests/e2e/*.sh",
    "shellcheck contrib/aib",
    "shellcheck -s bash -S warning contrib/aib",
    "shellcheck --shell=bash --severity=warning contrib/aib",
    "shellcheck --rcfile=.shellcheckrc contrib/aib",
    "shellcheck -f diff contrib/aib | git apply",
    "shellcheck -C always contrib/aib",
    "shellcheck - <contrib/aib",
    "shellcheck -e SC2034 -x tests/e2e/lib.sh",
    "shellcheck tests/e2e/*.sh",
    "shellcheck 'tests/e2e/lib.sh'",
    "cat ~/.bashrc; shellcheck contrib/aib",
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


    def test_git_reads_the_file_beside_a_process_substitution(self) -> None:
        # bash replaces `<(true)` with `/dev/fd/N` before git runs. That is a
        # path outside the checkout, so `git diff <(true) ./cosign.key`
        # implies --no-index and prints the key whole, with neither the
        # option nor an absolute path anywhere in the words as typed. shlex
        # emits `<(` as a token of its own and reads the `)` as the end of
        # the command, so before the substitution rule every word of the
        # command passed the tests that catch --no-index and /dev/null.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "cosign.key").write_text("STAND-IN-NOT-A-KEY\n")
            result = subprocess.run(
                ["bash", "--norc", "--noprofile", "-c", "git diff <(true) ./cosign.key"],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            self.assertIn(
                "STAND-IN-NOT-A-KEY",
                result.stdout,
                "git diff no longer prints the file beside a process substitution; the "
                "substitution rule may be more than is needed",
            )
        self.assertIsNotNone(
            gate.refusal("git diff <(true) ./cosign.key"),
            "the command just shown to read an arbitrary file is not refused",
        )


    def test_bash_truncates_the_target_of_a_redirection_written_before_the_command(self) -> None:
        # Bash lets a redirection precede the command name, and the two
        # spellings are the same command: `>victim git diff HEAD HEAD`
        # truncates the file exactly as `git diff HEAD HEAD >victim` does.
        # shlex hands the `>` back as the first token of the segment, and a
        # scan that took the first token for the command name saw no `git`
        # and checked nothing else in the segment either -- so `>cosign.pub
        # git diff --no-index /dev/null ./cosign.key` passed whole. Shown
        # against a stand-in in a throwaway repository.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            victim = repo / "victim"
            victim.write_text("ORIGINAL-CONTENT\n")
            subprocess.run(
                [
                    "bash",
                    "--norc",
                    "--noprofile",
                    "-c",
                    "git status --short >/dev/null; >victim git diff HEAD HEAD",
                ],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            written = victim.read_text()
        self.assertNotIn(
            "ORIGINAL-CONTENT",
            written,
            "bash no longer truncates the target of a redirection written before the "
            "command name; re-derive why split_segment() skips redirections",
        )
        self.assertIsNotNone(
            gate.refusal("git status; >cosign.pub git diff HEAD"),
            "the command just shown to truncate a file is not refused",
        )

    def test_bash_truncates_the_target_of_a_redirection_on_a_command_that_is_not_git(self) -> None:
        # The write primitive is not git's alone. A rule ending in `:*` means
        # "this command with any arguments", and a redirection is part of the
        # string that rule matches, so `python3 maintenance_audit.py
        # --skip-upstream >cosign.pub` was approved on its prefix and bash
        # opened the target before python3 ran. Shown against a stand-in in a
        # throwaway directory where the script does not even exist: the file
        # is emptied although the command then fails.
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim"
            victim.write_text("ORIGINAL-CONTENT\n")
            result = subprocess.run(
                [
                    "bash",
                    "--norc",
                    "--noprofile",
                    "-c",
                    "python3 maintenance_audit.py --skip-upstream >victim",
                ],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
            )
            written = victim.read_text()
        self.assertNotEqual(result.returncode, 0, "the stand-in command was meant to fail")
        self.assertNotIn(
            "ORIGINAL-CONTENT",
            written,
            "bash no longer truncates the target before the command runs; re-derive why "
            "GATED_PREFIXES exists",
        )
        self.assertIsNotNone(
            gate.refusal("python3 maintenance_audit.py --skip-upstream >cosign.pub"),
            "the command just shown to truncate a file is not refused",
        )

    def test_git_reads_a_home_file_named_with_a_tilde(self) -> None:
        # bash expands `~` to $HOME before git runs, so `git diff --
        # ~/.aws/credentials ~/.bashrc` is a two-operand plain-file diff of
        # two files outside the checkout that neither starts with `/` nor
        # carries a `..` as typed. Run with a throwaway HOME, never the real
        # one; the hook reads the `~` lexically and refuses it as an outside
        # operand.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".aws").mkdir(parents=True)
            (home / ".aws" / "credentials").write_text("STAND-IN-NOT-A-SECRET\n")
            (home / ".bashrc").write_text("export FIXTURE=1\n")
            result = subprocess.run(
                ["bash", "--norc", "--noprofile", "-c", "git diff -- ~/.aws/credentials ~/.bashrc"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env={"PATH": os.environ.get("PATH", ""), "HOME": str(home)},
                check=False,
            )
        self.assertIn(
            "STAND-IN-NOT-A-SECRET",
            result.stdout,
            "git diff no longer prints a home file named through ~; the tilde test in "
            "unsafe_operand() may be more than is needed",
        )
        self.assertIsNotNone(
            gate.refusal("git diff -- ~/.aws/credentials ~/.bashrc"),
            "the command just shown to read a home file is not refused",
        )


    def test_git_runs_the_word_bash_rebuilds_not_the_one_typed(self) -> None:
        # bash rebuilds a word from a substitution, a variable or an ANSI-C
        # escape before git runs, so `git diff $(echo /dev/null) ./cosign.key`
        # is `git diff /dev/null ./cosign.key` -- --no-index implied, the
        # key printed whole -- with no absolute path or refused option
        # anywhere in the words as typed, and `--outpu$'\\x74'=` is
        # `--output=`. The key and the target are stand-ins in a throwaway
        # repository; `echo x;(...)` is the glued-punctuation form beside
        # them.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "cosign.key").write_text("STAND-IN-NOT-A-SECRET\n")
            for command in (
                "git diff $(echo /dev/null) ./cosign.key",
                "git diff `echo /dev/null` ./cosign.key",
                "G=/dev/null; git diff $G ./cosign.key",
                "echo x;(git diff --no-index /dev/null ./cosign.key)",
            ):
                with self.subTest(command=command):
                    result = subprocess.run(
                        ["bash", "--norc", "--noprofile", "-c", command],
                        cwd=repo,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertIn(
                        "STAND-IN-NOT-A-SECRET",
                        result.stdout,
                        f"bash no longer rebuilds the word in {command!r}, or git no "
                        "longer diffs the result; re-derive why expands_at_runtime() and "
                        "punctuation_pieces() exist",
                    )
                    self.assertIsNotNone(
                        gate.refusal(command),
                        "the command just shown to print the key is not refused",
                    )
            target = repo / "cosign.pub"
            target.write_text("ORIGINAL-CONTENT\n")
            subprocess.run(["git", "-C", str(repo), "add", "cosign.key"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "x"],
                check=True,
            )
            command = "git log --outpu$'\\x74'=cosign.pub -1"
            subprocess.run(
                ["bash", "--norc", "--noprofile", "-c", command],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            written = target.read_text()
        self.assertNotIn(
            "ORIGINAL-CONTENT",
            written,
            "bash no longer reads $'\\x74' as the letter t, or git log no longer "
            "writes through --output; re-derive why expands_at_runtime() exists",
        )
        self.assertIsNotNone(
            gate.refusal(command),
            "the command just shown to overwrite a file is not refused",
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
                # A process substitution reaches the hook as its opening `<(`
                # or `>(` alone, since shlex breaks the word there, so that
                # opening is how the refusal names it. A quoted word is
                # named by its text, without the quote marks.
                words = [
                    part
                    for word in command.split()
                    if word not in {"git", "&&", "|"}
                    for part in (word, word.split("=", 1)[0], word[:2], word.strip("'\""))
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

    def test_every_allow_rule_with_arguments_is_refused_a_writing_redirection(self) -> None:
        # The list of gated commands lives in the hook; this is what keeps it
        # from drifting. Deriving the commands from the settings file rather
        # than restating them means a rule added there with a trailing `:*`
        # fails here until the hook lists it. The git rows are the hook's
        # first half; the rows with no `:*` need no entry, because a
        # redirection makes the string match none of them and Claude Code
        # prompts -- `test_ordinary_commands_pass_through` holds that half
        # with `ruff check >cosign.pub`.
        allow = json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]["allow"]
        prefixes = [
            rule[len("Bash(") : -len(":*)")]
            for rule in allow
            if rule.startswith("Bash(") and rule.endswith(":*)")
        ]
        gated = [prefix for prefix in prefixes if not prefix.startswith("git ")]
        self.assertGreaterEqual(len(gated), 13, gated)
        self.assertEqual(
            sorted(tuple(prefix.split()) for prefix in gated),
            sorted(gate.GATED_PREFIXES),
            "GATED_PREFIXES and the `:*` allow rows of .claude/settings.json disagree",
        )
        for prefix in gated:
            command = f"{prefix} >cosign.pub"
            with self.subTest(command=command):
                reason = gate.refusal(command)
                self.assertIsNotNone(reason, f"{command!r} was not refused")
                self.assertIn(">cosign.pub", reason or "")
                self.assertIn(prefix, reason or "")

    def test_the_command_words_are_read_past_redirections_and_prefixes(self) -> None:
        # command_words() is what the prefix is matched against, so it has to
        # step over a redirection wherever bash lets it stand, and over an
        # assignment or a keyword before the name, or the prefix is missed and
        # the write goes through.
        for segment, words in (
            (["shellcheck", "x", ">", "out"], ["shellcheck", "x"]),
            ([">", "out", "shellcheck", "x"], ["shellcheck", "x"]),
            (["2", ">", "err", "podman", "images"], ["podman", "images"]),
            (["{fd}", ">", "out", "podman", "images"], ["podman", "images"]),
            (["FOO=bar", "shellcheck", "x", ">", "out"], ["shellcheck", "x"]),
            (["time", "shellcheck", "x"], ["shellcheck", "x"]),
            (["command", "podman", ">", "out", "images"], ["podman", "images"]),
            (["/usr/bin/shellcheck", "x"], ["shellcheck", "x"]),
            (["python3", "maintenance_audit.py", ">", "o", "--skip-upstream"], ["python3", "maintenance_audit.py", "--skip-upstream"]),
            (["env", "-i", "shellcheck", "x"], ["-i", "shellcheck", "x"]),
            ([">", "`printf", "cosign.pub`", "shellcheck", "x"], ["shellcheck", "x"]),
            ([">", "`x`", "shellcheck", "x"], ["shellcheck", "x"]),
        ):
            with self.subTest(segment=segment):
                self.assertEqual(gate.command_words(segment)[0], words)
        self.assertEqual(gate.gated_prefix(["podman", ">", "o", "image", "exists", "x"]), ("podman", "image", "exists"))
        self.assertEqual(gate.gated_prefix(["command", "-p", "/usr/bin/shellcheck", "x"]), ("shellcheck",))
        self.assertIsNone(gate.gated_prefix(["podman", "rmi", "x", ">", "o"]))
        self.assertIsNone(gate.gated_prefix(["python3", "maintenance_audit.py", ">", "o"]))
        self.assertIsNone(gate.gated_prefix(["echo", "shellcheck", "x"]))

    def test_a_comment_is_dropped_only_where_bash_drops_it(self) -> None:
        # A `#` that begins a word after whitespace starts a comment; one
        # inside a word or straight after an operator is kept, and a quoted
        # one is a literal. A comment ends at the newline, so the command on
        # the next line is still read.
        for command, stripped in (
            ("shellcheck x # out > f", "shellcheck x "),
            ("# only a comment", ""),
            ("shellcheck x #c\nhadolint y >o", "shellcheck x \nhadolint y >o"),
            ("git diff HEAD^#x", "git diff HEAD^#x"),
            ("echo x;#c >o", "echo x;#c >o"),
            ("shellcheck '#' >o", "shellcheck '#' >o"),
            ('echo "a # b"', 'echo "a # b"'),
        ):
            with self.subTest(command=command):
                self.assertEqual(gate.strip_comments(command), stripped)
        self.assertIsNotNone(gate.refusal("shellcheck x #c\nhadolint y >o"))
        self.assertIsNotNone(gate.refusal("git diff --stat#x /etc/passwd /dev/null"))

    def test_a_redirection_is_refused_for_what_the_shell_opens(self) -> None:
        # The rule is the operator and the target's shape. Every operator with
        # a `>` in it opens its target for writing, `<>` included; `>&` does
        # too unless the target names a descriptor; the input forms open
        # nothing for writing.
        for operator, target in (
            (">", "cosign.pub"),
            (">>", "out"),
            (">|", "x"),
            ("&>", "/dev/null"),
            ("&>>", "log"),
            (">&", "cosign.pub"),
            ("<>", "cosign.pub"),
        ):
            with self.subTest(operator=operator, target=target):
                self.assertTrue(gate.redirection_writes_a_path(operator, target))
        for operator, target in (
            (">&", "1"),
            (">&", "2"),
            (">&", "-"),
            ("<", "/dev/null"),
            ("<<", "EOF"),
            ("<<<", "x"),
            ("<&", "0"),
        ):
            with self.subTest(operator=operator, target=target):
                self.assertFalse(gate.redirection_writes_a_path(operator, target))

    def test_a_redirection_before_the_command_does_not_hide_the_command(self) -> None:
        # split_segment() has to see past a redirection, its target and a
        # descriptor to the word that names the command, or every other
        # refusal is skipped for the segment.
        for segment, command in (
            ([">", "cosign.pub", "git", "diff", "HEAD"], "git"),
            (["2", ">", "err", "git", "log", "-1"], "git"),
            (["{fd}", ">", "err", "git", "log", "-1"], "git"),
            ([">", "$", "git", "diff", "HEAD"], "git"),
            ([">", "`printf", "cosign.pub`", "git", "diff", "HEAD"], "git"),
            ([">", "`x`", "git", "diff", "HEAD"], "git"),
            (["FOO=bar", ">", "out", "git", "diff"], "git"),
            (["<", "/dev/null", "git", "diff"], "git"),
            ([">&", "2", "git", "diff"], "git"),
            (["git", "diff", ">", "out"], "git"),
            ([">", "out", "echo", "x"], "echo"),
        ):
            with self.subTest(segment=segment):
                names, found, arguments = gate.split_segment(segment)
                self.assertEqual(found, command)
                self.assertEqual(arguments, segment[segment.index(command) + 1 :])
        names, _, _ = gate.split_segment(["FOO=bar", ">", "out", "git", "diff"])
        self.assertEqual(names, ["FOO"])

    def test_a_quoted_separator_is_a_word_of_its_command(self) -> None:
        # shlex hands back the same `;` for `';'` and `;`, and bash treats
        # only the second as a separator. The masked copy tells them apart
        # without changing where the tokens fall.
        for command, expected in (
            ("git diff ';' >out", [["git", "diff", ";", ">", "out"]]),
            ("git diff; >out", [["git", "diff"], [">", "out"]]),
            ("echo 'a|b' | cat", [["echo", "a|b"], ["cat"]]),
            ("x=$(git log -1) ; git diff", [["git", "log", "-1"], ["x=$"], ["git", "diff"]]),
            (">$(printf out) git diff HEAD", [["printf", "out"], [">", "$", "git", "diff", "HEAD"]]),
            ('echo "$(date)"', [["echo", "$(date)"]]),
        ):
            with self.subTest(command=command):
                tokens = gate.tokenize(command)
                masked = gate.tokenize(gate.mask_quotes(command))
                self.assertEqual(len(tokens), len(masked))
                found = gate.segments(tokens, masked)
                self.assertEqual([words for words, _ in found], expected)
                for words, twins in found:
                    self.assertEqual(len(words), len(twins))
        self.assertEqual(gate.mask_quotes(r"""a 'b c' \; "d\"e" f"""), "a QQQQQ QQ QQQQQQ f")

    def test_a_paren_glued_to_a_separator_is_read_as_both(self) -> None:
        # shlex glues adjacent punctuation into one token, so `echo x;(git
        # diff)` arrived with `;(` as a word: not a separator, not a `(`, and
        # the git after it was a word of echo's command that nothing checked.
        # `);` ran the outer command into the inner one the same way. Each
        # paren is a token of its own to bash, and `<(`/`>(` stay whole.
        for run, pieces in (
            (";(", [";", "("]),
            ("&&(", ["&&", "("]),
            ("|(", ["|", "("]),
            (");", [")", ";"]),
            (")&&(", [")", "&&", "("]),
            (")>", [")", ">"]),
            ("((", ["(", "("]),
            ("))", [")", ")"]),
            (";<(", [";", "<("]),
            (">(", [">("]),
            ("<(", ["<("]),
            (";;", [";;"]),
            (">&", [">&"]),
        ):
            with self.subTest(run=run):
                self.assertEqual(gate.punctuation_pieces(run), pieces)
        self.assertEqual(
            gate.tokenize("echo x;(git diff);<(true)"),
            ["echo", "x", ";", "(", "git", "diff", ")", ";", "<(", "true", ")"],
        )
        for command, expected in (
            ("echo x;(git diff HEAD)", [["echo", "x"], ["git", "diff", "HEAD"]]),
            ("x=$(git log -1);echo", [["git", "log", "-1"], ["x=$"], ["echo"]]),
            (
                "for f in $(git diff --name-only); do echo $f; done",
                [["git", "diff", "--name-only"], ["for", "f", "in", "$"], ["do", "echo", "$f"], ["done"]],
            ),
        ):
            with self.subTest(command=command):
                tokens = gate.tokenize(command)
                masked = gate.tokenize(gate.mask_quotes(command))
                self.assertEqual([words for words, _ in gate.segments(tokens, masked)], expected)
        self.assertIsNotNone(gate.refusal("echo x;(git diff --no-index /dev/null ./cosign.key)"))
        self.assertIsNotNone(gate.refusal("true&&(git diff --no-index /dev/null ./cosign.key)"))

    def test_a_revision_range_is_not_a_parent_directory(self) -> None:
        # `..` is a path component in one and a range operator in the other.
        # Conflating them would refuse the commonest git log argument there is.
        self.assertFalse(gate.unsafe_operand("HEAD..main"))
        self.assertFalse(gate.unsafe_operand("v1.0...v2.0"))
        self.assertTrue(gate.unsafe_operand("../sibling/file"))
        self.assertTrue(gate.unsafe_operand("docs/../../etc/passwd"))

    def test_a_leading_tilde_is_outside_the_checkout(self) -> None:
        # bash expands an unquoted leading `~` to a home directory before git
        # runs, so as an operand it names a file outside the checkout without
        # a `/` or a `..` in the spelling. A tilde anywhere else in the word
        # is a character in a revision or a filename. shlex removes quotes,
        # so `'~/x'` reaches the hook as `~/x` and is refused too, which is
        # the over-refusing direction.
        for token in ("~", "~/", "~/.aws/credentials", "~root/.bashrc", "~/.bashrc"):
            with self.subTest(token=token):
                self.assertTrue(gate.unsafe_operand(token))
        for token in ("HEAD~1", "HEAD~2..HEAD~1", "lit~eral", "x~", "HEAD:~/x"):
            with self.subTest(token=token):
                self.assertFalse(gate.unsafe_operand(token))

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

    def test_a_word_bash_rebuilds_is_refused_in_a_git_invocation(self) -> None:
        # The gate reads words as typed; bash would rebuild these before git
        # runs, and the rebuilt word can spell any refused argument. `$G` is
        # a variable, `$(...)` and backticks are substitutions, `$'\\x74'` is
        # the letter t through an ANSI-C escape, and double quotes quote
        # none of them. The refusal names the word and the `$(...)` form.
        for command in (
            "git diff $(echo /dev/null) ./cosign.key",
            "git diff `echo /dev/null` ./cosign.key",
            "git diff `printf -- --no-index` ./LICENSE ./cosign.key",
            "git log --outpu$'\\x74'=cosign.pub -1",
            "G=/dev/null; git diff $G ./cosign.key",
            'git diff "$G" ./cosign.key',
            'git diff "$(echo /dev/null)" ./cosign.key',
            'git diff "`echo /dev/null`" ./cosign.key',
            "git status && git diff $G ./cosign.key",
            "git log -1 $G",
        ):
            with self.subTest(command=command):
                reason = gate.refusal(command)
                self.assertIsNotNone(reason, f"{command!r} is let through")
                self.assertIn("reads words as typed", reason or "")

    def test_a_quoted_or_escaped_dollar_is_a_literal(self) -> None:
        # Single quotes and a backslash quote `$` and the backtick, and
        # double quotes quote an escaped one. shlex hands back the same word
        # for `'$x'` and `$x`, so the rule reads the masked twin, where only
        # the `$` bash would act on survives.
        for command in (
            "git log --format='%h $x'",
            'git log --format="%h \\$x"',
            "git diff HEAD -- \\$x",
            "git log -G'\\$x' --oneline",
            "git log -S'`' -p",
            'git diff -- "a`b"'.replace("`", "\\`"),
        ):
            with self.subTest(command=command):
                self.assertIsNone(gate.refusal(command), f"{command!r} is refused")
        for twin, expands in (
            ("$G", True),
            ("$", True),
            ("Q$QQ", True),
            ("`echo", True),
            ("/dev/null`", True),
            ("QQQQQQQ", False),
            ("HEAD@{1}", False),
            ("--format=QQQQQQQ", False),
        ):
            with self.subTest(twin=twin):
                self.assertEqual(gate.expands_at_runtime(twin), expands)
        self.assertEqual(gate.mask_quotes('"a $b `c` \\$d"'), 'QQQ$QQ`Q`QQQQQ')
        self.assertEqual(gate.mask_quotes("'a $b'"), "QQQQQQ")
        self.assertEqual(gate.mask_quotes("$'\\x74'"), "$QQQQQQ")

    def test_a_dollar_on_another_command_is_not_gated(self) -> None:
        # The rule is scoped to the git invocation: a variable in the echo
        # before it, or a substitution whose *inner* command is git, is
        # bash's business, and the inner git is checked as its own segment.
        for command in (
            "echo $HOME; git diff HEAD",
            'echo "$(date)"; git diff HEAD',
            "x=$(git log -1); git diff HEAD",
            "for f in $(git diff --name-only HEAD); do echo $f; done",
            "git diff HEAD | grep $x",
        ):
            with self.subTest(command=command):
                self.assertIsNone(gate.refusal(command), f"{command!r} is refused")
        self.assertIsNotNone(gate.refusal("x=$(git diff $G ./cosign.key)"))

    # Words bash rebuilds before git runs, in every spelling the rule is
    # for, beside the quoted forms it must leave alone. Each is inserted
    # verbatim into a bash script, so the quoting is bash's; `G` is set so
    # `$G` and its relatives become a word other than the one typed.
    EXPANSION_CORPUS = (
        "$G",
        "${G}",
        "${G:-x}",
        "$(echo /dev/null)",
        "`echo /dev/null`",
        "--outpu$'\\x74'=cosign.pub",
        "--no-inde$G",
        '"$G"',
        '"$(echo /dev/null)"',
        '"`echo /dev/null`"',
        "x$G",
        "$Gx",
        "${G}x",
        "$(printf -- --no-index)",
        "'$G'",
        "'$(echo /dev/null)'",
        "'`echo /dev/null`'",
        "\\$G",
        '"\\$G"',
        "'%h $x'",
        "HEAD@{1}",
        "docs/quality.md",
    )

    # The words of that corpus bash leaves as typed, which the hook must too.
    LITERAL_EXPANSION_WORDS = (
        "'$G'",
        "'$(echo /dev/null)'",
        "'`echo /dev/null`'",
        "\\$G",
        '"\\$G"',
        "'%h $x'",
        "HEAD@{1}",
        "docs/quality.md",
    )

    @staticmethod
    def bash_rebuilds(word: str) -> bool:
        """Whether bash builds this word at runtime rather than reading it as typed.

        Two things bash exposes are read: a word built from a variable comes
        out different when the variable changes, and a command substitution
        shows in the `-x` trace as a second command run before printf. Quote
        removal alone (`'$G'`, `"\\$G"`) does neither. `G` and `Gx` are the
        variable names the corpus uses; both are set so `$Gx` reads a value
        rather than nothing. An ANSI-C escape (`$'\\x74'`) is neither a
        variable nor a command, so this reference does not see it; that
        spelling is held to REFUSED_COMMANDS and to the reach test that
        runs it through bash instead.
        """
        outputs: list[bytes] = []
        for value in ("t", "u"):
            result = subprocess.run(
                ["bash", "--norc", "--noprofile", "-x", "-c", 'printf "%s\\0" ' + word],
                capture_output=True,
                env={"PATH": os.environ.get("PATH", ""), "G": value, "Gx": value},
                check=False,
            )
            if result.returncode != 0:
                raise AssertionError(f"bash could not run {word!r}: {result.stderr!r}")
            if sum(line.startswith(b"+") for line in result.stderr.splitlines()) > 1:
                return True
            outputs.append(result.stdout)
        return outputs[0] != outputs[1]

    def test_the_expansion_rule_against_bash_rather_than_a_label(self) -> None:
        # Bash is the ground truth. Every corpus word bash rebuilds must be
        # refused, and every word of the literal set must be allowed. The
        # counts keep the check from going vacuous if the corpus shrinks.
        self.assertGreaterEqual(len(self.EXPANSION_CORPUS), 22)
        self.assertEqual(len(set(self.EXPANSION_CORPUS)), len(self.EXPANSION_CORPUS))
        rebuilt = 0
        for word in self.EXPANSION_CORPUS:
            with self.subTest(word=word):
                if not self.bash_rebuilds(word):
                    continue
                rebuilt += 1
                reason = gate.refusal(f"git diff {word}")
                self.assertIsNotNone(reason, f"bash rebuilds {word!r}; the hook let it through")
                # `${G}` meets the brace rule first; both refusals say so.
                self.assertIn("as typed", reason or "")
        self.assertGreaterEqual(rebuilt, 12)
        for word in self.LITERAL_EXPANSION_WORDS:
            with self.subTest(word=word):
                self.assertIn(word, self.EXPANSION_CORPUS)
                self.assertFalse(self.bash_rebuilds(word), f"bash rebuilds {word!r}")
                self.assertIsNone(gate.refusal(f"git log {word} -1"))

    def test_a_process_substitution_in_a_git_word_is_refused(self) -> None:
        # bash hands git a `/dev/fd/N` path for `<(...)` and `>(...)`, which
        # is outside the checkout however the word is spelled, and shlex
        # breaks the word at the `(` so `<(` arrives as a token of its own
        # with the `)` read as the end of the command. Both openings are
        # refused wherever they sit in a git invocation, and the refusal
        # names the substitution rather than the argument after it.
        for command in (
            "git diff <(true) ./cosign.key",
            "git diff <(true) /etc/passwd",
            "git diff -- <(true)",
            "git diff HEAD >(cat) -- README.md",
            "git log -p <(true)",
            "git status && git diff <(true) ./cosign.key",
        ):
            with self.subTest(command=command):
                reason = gate.refusal(command)
                self.assertIsNotNone(reason, f"{command!r} is let through")
                self.assertIn("process substitution", reason or "")

    def test_a_process_substitution_outside_a_git_invocation_is_not_gated(self) -> None:
        # The gate is about git's arguments; a diff of two other programs'
        # output is not one, and the allow list never matches it anyway.
        for command in ("diff <(ls a) <(ls b)", "cat <(echo x)"):
            with self.subTest(command=command):
                self.assertIsNone(gate.refusal(command))

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

    def test_a_shellcheck_glob_is_judged_by_the_files_it_names(self) -> None:
        # The one expansion the scan performs instead of refusing, because
        # this repository's own lint command ends in a glob. So the test is
        # what bash would hand shellcheck: the same pattern is refused or
        # allowed by what is on disk beside it, not by how it is spelled.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "ok.sh").write_text("#!/bin/bash\ntrue\n")
            here = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertIsNone(gate.refusal("shellcheck *.sh"))
                self.assertIsNone(gate.refusal("shellcheck .env*"))
                Path(tmp, ".env").write_text("TOKEN=stand-in\n")
                self.assertIsNotNone(
                    gate.refusal("shellcheck .env*"),
                    "a glob that now names a denied file is let through, and bash "
                    "expands it before shellcheck prints the file back",
                )
                Path(tmp, "tls.pem").write_text("-----BEGIN-----\n")
                self.assertIsNotNone(
                    gate.refusal("shellcheck *"),
                    "a bare glob beside a denied file is let through; bash expands it "
                    "to that file, which is not a dotfile and not hidden from it",
                )
                self.assertIsNone(
                    gate.refusal("shellcheck '.env*'"),
                    "a quoted glob is a literal to bash, and no file is named .env*",
                )
            finally:
                os.chdir(here)

    def test_a_denied_shape_is_matched_on_the_basename(self) -> None:
        # `Read(**/*.pem)` denies the shape wherever it sits, and a
        # `cosign.key` one directory down is the same secret as the one at
        # the root, so the scan does not anchor on the path.
        for path in ("cosign.key", "a/b/cosign.key", ".env", "x/.env.local", "a/k.pem"):
            with self.subTest(path=path):
                self.assertTrue(gate.denied_read_shape(path))
        for path in ("contrib/aib", "tests/e2e/lib.sh", "docs/env.md", "pem.sh"):
            with self.subTest(path=path):
                self.assertFalse(gate.denied_read_shape(path))

    def test_the_operand_scan_reads_past_every_value_taking_option(self) -> None:
        # An option whose value the scan does not skip is read as a path, and
        # a path the scan does not reach is read as an option; both spellings
        # of each option are checked because shellcheck accepts both.
        for option in sorted(gate.SHELLCHECK_VALUE_OPTIONS):
            with self.subTest(option=option):
                self.assertIsNone(gate.refusal(f"shellcheck {option} x contrib/aib"))
                self.assertIsNotNone(
                    gate.refusal(f"shellcheck {option} x ./cosign.key"),
                    f"{option}'s value swallowed the operand after it",
                )


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

    def test_every_read_denial_has_a_shape_the_operand_scan_knows(self) -> None:
        # The scan's list is a restatement of the deny rules, and a rule added
        # to the settings file without one here is a file the Read tool
        # refuses and an allow-listed `shellcheck` prints back.
        for rule in self.settings["permissions"]["deny"]:
            if not rule.startswith("Read("):
                continue
            shape = rule[len("Read(") : -1].rsplit("/", 1)[-1]
            with self.subTest(rule=rule):
                self.assertTrue(
                    any(
                        shape == known or fnmatch.fnmatchcase(shape, known)
                        for known in gate.DENIED_READ_SHAPES
                    ),
                    f"{rule} is denied to the Read tool but {shape} is not a shape "
                    "gate_git_diff.py refuses as a shellcheck operand",
                )

    def test_shellcheck_is_still_allowed_with_any_argument(self) -> None:
        # The operand scan exists because this rule matches by prefix. If the
        # row ever names its files, the scan is guarding a path no session
        # takes and this module is measuring nothing.
        self.assertIn("Bash(shellcheck:*)", self.settings["permissions"]["allow"])

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

    def test_the_enforcement_section_names_the_commands_that_are_not_git(self) -> None:
        # The document is where a reader learns that the redirection refusal
        # covers the other allow-listed commands too, and which ones. The
        # audit is named by its flag rather than its file, because
        # tests/test_security_ai_doc.py holds that exactly one bullet of the
        # section names `maintenance_audit.py`, and that bullet is the audit's
        # own.
        section = DOC.read_text().split("## What is enforced rather than trusted", 1)
        self.assertEqual(len(section), 2)
        body = section[1].split("\n## ", 1)[0]
        spellings = {("python3", "maintenance_audit.py", "--skip-upstream"): "`--skip-upstream` audit"}
        for prefix in gate.GATED_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertIn(spellings.get(prefix, " ".join(prefix)), body)

    def test_the_enforcement_section_names_the_shellcheck_operand_scan(self) -> None:
        # A reader who knows only that the redirection is refused will read
        # `shellcheck ./.env` as covered. The section has to say what the
        # operand scan tests, and the shapes it names have to be the ones the
        # hook refuses.
        section = DOC.read_text().split("## What is enforced rather than trusted", 1)
        self.assertEqual(len(section), 2)
        body = section[1].split("\n## ", 1)[0]
        self.assertIn("source line", body)
        for shape in gate.DENIED_READ_SHAPES:
            with self.subTest(shape=shape):
                self.assertIn(f"`{shape}`", body)

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
