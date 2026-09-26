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
import re
import shutil
import string
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
    ("just --fmt --check --justfile ./.env", "prints a .env line back through the allow-listed formatter"),
    ("just --fmt --check -f ./cosign.key", "prints a signing key line back through the short justfile option"),
    ("just --fmt --check --justfile=/home/me/.aws/credentials", "reads a path outside the checkout through the = spelling"),
    ("just --fmt --check -f/etc/shadow", "reads an absolute path through the attached short option"),
    ("just --fmt --check -d /tmp/other -f /tmp/other/justfile", "formats a justfile in another tree"),
    ("just --fmt --check --justfile /dev/stdin < ./.env", "parses a denied file arriving on standard input"),
    ("just --fmt --check -f - < ./cosign.key", "parses a denied file through the - justfile"),
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
    ("shellcheck - < .env", "feeds the denied file to shellcheck on standard input"),
    ("shellcheck -s bash - <./cosign.key", "feeds the signing key on standard input, operator attached"),
    ("shellcheck - 0< secrets/.env", "feeds a denied shape through an explicit descriptor"),
    ("shellcheck - < ~/.aws/credentials", "redirects from a home file through a tilde"),
    ("shellcheck - < /etc/shadow", "redirects from an absolute path outside the checkout"),
    ("shellcheck - < ../sibling/.env", "redirects from a denied file outside the checkout"),
    ("shellcheck - < {contrib/aib,.env}", "brace-expands the redirection target"),
    ("shellcheck - < secrets/*.pem", "globs the redirection target onto a denied shape"),
    ("< .env shellcheck -", "redirects from the denied file before the command name"),
    ("git log --stdin < .env", "prints the file's first line back as a bad revision"),
    ("git diff --stdin <./cosign.key", "feeds the signing key to git diff's revision list"),
    ("git log --stdin 0< secrets/.env", "feeds a denied shape through an explicit descriptor"),
    ("< .env git log --stdin", "redirects from the denied file before git's name"),
    ("git log --stdin < /etc/shadow", "redirects from an absolute path outside the checkout"),
    ("git log --stdin 3< .env <&3", "opens the file on another descriptor and duplicates it"),
    ("SHELLCHECK_OPTS=./.env shellcheck contrib/aib", "hands the linter a denied operand through its environment"),
    ("env SHELLCHECK_OPTS=./.env shellcheck contrib/aib", "hides that assignment behind a wrapper"),
    ("env -i SHELLCHECK_OPTS=./.env shellcheck contrib/aib", "hides it behind a wrapper's own option"),
    ("env 'SHELLCHECK_OPTS'=./.env shellcheck contrib/aib", "quotes the name bash would not read as an assignment and env does"),
    ("env -S 'SHELLCHECK_OPTS=./.env shellcheck contrib/aib'", "splits the assignment and the command out of one word"),
    ("export SHELLCHECK_OPTS=./.env; shellcheck contrib/aib", "exports it from a segment with no gated command in it"),
    ("declare -x SHELLCHECK_OPTS=./.env; shellcheck contrib/aib", "declares it in an earlier command of the same string"),
    ("SHELLCHECK_OPTS='-s bash' shellcheck contrib/aib", "sets the variable at all, which nothing here does"),
    ("SHELLCHECK_OPTS+=./.env shellcheck contrib/aib", "appends to the variable, which creates it when unset"),
    ("env SHELLCHECK_OPTS+=./.env shellcheck contrib/aib", "appends behind a wrapper"),
    ("export SHELLCHECK_OPTS+=./.env; shellcheck contrib/aib", "appends from an earlier segment"),
    ("declare SHELLCHECK_OPTS+=./.env; shellcheck contrib/aib", "appends in a declare of an earlier segment"),
    ("git status && shellcheck ./cosign.key", "hides behind an earlier command"),
    ("git diff 'unterminated", "cannot be parsed, so it is not let through"),
    ("env -C /tmp/other git diff", "relocates git before its operands are read, past a wrapper's own option"),
    ("env --chdir=/tmp/other git diff", "relocates git through the attached spelling of the same option"),
    ("env -S'FOO=x\\_GIT_EXTERNAL_DIFF=./evil\\_git diff'", "merges past word.split() so only the decoy name in front of it is read"),
    ("export GIT_EXTERNAL_DIFF$'=./evil'; git diff HEAD", "concatenates an ANSI-C quote onto the name so the literal scan misses it"),
    ("env -i {,git} diff --no-index /dev/null ./cosign.key", "hides a brace-expanded name behind a wrapper's own option"),
    (
        "export 'GIT_EXTERNAL_DIFF'$'=./evil'; git diff HEAD",
        "quotes the identifier ahead of the ANSI-C escape, which used to shift an index-based scan out of alignment with its "
        "masked twin and let the assignment through unread",
    ),
    (
        "env -S 'FOO=x\\_GIT_EXTERNAL_DIFF=./evil\\_git diff' HEAD",
        "splits the assignment out of a *detached* -S argument -- its own word, not attached to the option -- one word later",
    ),
    ("env -C/tmp/other git diff", "relocates git through -C's attached short spelling, with no space or ="),
    ("env --chd=/tmp/other git diff", "relocates git through an unambiguous abbreviation of --chdir"),
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
    "shellcheck - < contrib/aib",
    "shellcheck -s bash - <tests/e2e/smoke.sh",
    "shellcheck - <<< 'echo hi'",
    "shellcheck contrib/aib < /dev/null",
    "hadolint Containerfile",
    "python3 maintenance_audit.py --skip-upstream",
    "just --fmt --check",
    "just --fmt --check --justfile template_snapshots/containerfile/Justfile",
    "just --fmt --check -f template_snapshots/containerfile/Justfile < /dev/null",
    "skopeo inspect docker://ghcr.io/x:latest | jq .Digest",
    "podman images --format '{{.Repository}}'",
    "gh search issues --repo Danathar/atomic-image-builder --json number",
    "shellcheck contrib/aib <contrib/aib",
    "podman logs c >&2",
    "podman ps 2>&-",
    # A command no allow rule covers prompts on its own, so a redirection on
    # it is not this hook's to refuse: the two exact rows (`ruff check`,
    # `python3 -m unittest discover -s tests`) carry no `:*`, and Claude Code
    # asks before it runs the brace group and the subshell below, whatever the
    # allow rows say ("Contains compound_statement", "Contains subshell";
    # 2.1.273 and 2.1.280). This test fails if an allow row that could reach
    # them is added:
    # test_no_allow_rule_reaches_a_redirection_written_after_a_group.
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
    # The variable is refused by the word that assigns it, so naming it
    # without the `=` -- which is how it is searched for -- is not an
    # assignment and is not refused.
    "grep -rn SHELLCHECK_OPTS docs/SECURITY-AI.md",
    # `v` sits ahead of a live `$` the same shape `GIT_EXTERNAL_DIFF$'='`
    # does, but reaches no REFUSED_ENVIRONMENT name whatever `$TAG`
    # expands to, and this command is not even gated -- `gh release
    # create` matches no GATED_PREFIXES row -- so runtime_assignment()
    # refusing it outright was a plain command the scan had no reach-based
    # reason to stop.
    "gh release create v$TAG",
)


REFUSED = "refused"
ALLOWED = "allowed"

# The whole corpus of ways a command reaches a tool past an allow rule, from
# #428, as one table rather than as prose: the family, a command spelling it,
# the decision, and why that decision. A new shape is one row.
#
# The decision is the point. `REFUSED` is a gap this hook closes; `ALLOWED` is
# a shape that was looked at and let through, either because it reaches
# nothing under the conditions this hook runs in or because refusing it would
# cost an ordinary command. Both are held here, so a change that quietly
# starts refusing an `ALLOWED` row fails as loudly as one that stops refusing
# a `REFUSED` one -- a gate that blocks ordinary work gets switched off, which
# is the same as not having one.
#
# The rows are the corpus filed across the six repositories this hive manages
# (sensi#250, goodreads-mcp#120, zfs-kinoite-complex#229, aurora-zfs-simple#222,
# arch-bootc#333), so a shape found in any of them can be added here as a row.
REACH_CORPUS = (
    # 1. An environment assignment reaching the tool. None of these appears
    # inside the string an allow rule matches, and all but the pager reach.
    ("environment", "GIT_EXTERNAL_DIFF=/tmp/evil git diff HEAD", REFUSED, "names a program git runs on every file it diffs"),
    ("environment", "GIT_EXTERNAL_DIFF+=/tmp/evil git diff HEAD", REFUSED, "appending to an unset variable creates it"),
    ("environment", "env GIT_EXTERNAL_DIFF=/tmp/evil git diff HEAD", REFUSED, "the same assignment behind a wrapper"),
    ("environment", "env -i GIT_EXTERNAL_DIFF=/tmp/evil git diff HEAD", REFUSED, "behind the wrapper's own option"),
    ("environment", "env 'GIT_EXTERNAL_DIFF'=/tmp/evil git diff HEAD", REFUSED, "a name bash would not read as an assignment and env does"),
    ("environment", "env -S'GIT_EXTERNAL_DIFF=/tmp/evil git diff HEAD'", REFUSED, "assignment and command re-split out of one word"),
    ("environment", "env --split-string='GIT_EXTERNAL_DIFF=/tmp/evil git diff HEAD'", REFUSED, "the long spelling of -S"),
    ("environment", "export GIT_EXTERNAL_DIFF=/tmp/evil; git diff HEAD", REFUSED, "an export applies to every later command in the string"),
    ("environment", "export GIT_EXTERNAL_DIFF+=/tmp/evil; git diff HEAD", REFUSED, "the append spelling of that export"),
    ("environment", "declare -x GIT_EXTERNAL_DIFF=/tmp/evil; git diff HEAD", REFUSED, "declare -x exports like export does"),
    ("environment", "typeset -x GIT_EXTERNAL_DIFF=/tmp/evil; git diff HEAD", REFUSED, "typeset -x is declare -x"),
    ("environment", "readonly GIT_EXTERNAL_DIFF=/tmp/evil; git diff HEAD", REFUSED, "readonly does not export, and is refused with the rest rather than modelled"),
    ("environment", "export GIT_EXTERNAL_DIFF=/tmp/evil\ngit diff HEAD", REFUSED, "a newline separates two commands as a ; does"),
    ("environment", "export GIT_EXTERNAL_DIFF=/tmp/evil", REFUSED, "the Bash tool's shell outlives the call, so the next call's git diff carries it"),
    ("environment", "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=diff.external git diff", REFUSED, "the numbered config family injects diff.external without -c"),
    ("environment", "GIT_DIR=/tmp/other/.git git log", REFUSED, "re-points the repository"),
    ("environment", "GIT_INDEX_FILE=/tmp/x git diff", REFUSED, "re-points the index the diff is taken against"),
    ("environment", "GIT_WORK_TREE=/tmp git diff", REFUSED, "re-points the tree the diff is taken against"),
    ("environment", "GIT_NAMESPACE=x git log", REFUSED, "re-points the refs the command resolves"),
    ("environment", "GIT_OBJECT_DIRECTORY=/tmp git log", REFUSED, "re-points the object store"),
    ("environment", "GIT_ALTERNATE_OBJECT_DIRECTORIES=/tmp git log", REFUSED, "adds one"),
    ("environment", "LD_PRELOAD=/tmp/evil.so git diff HEAD", REFUSED, "loads code into any of these commands, git included"),
    ("environment", "export LD_AUDIT=/tmp/evil.so; shellcheck contrib/aib", REFUSED, "the dynamic linker's own hook for the same"),
    ("environment", "export PYTHONHOME=/tmp; python3 maintenance_audit.py --skip-upstream", REFUSED, "re-points the standard library the audit imports from"),
    ("environment", "export GH_ENTERPRISE_TOKEN=x; gh search issues --repo x", REFUSED, "replaces the token gh authenticates with"),
    ("environment", "export CONTAINERS_REGISTRIES_CONF=/tmp/x; skopeo inspect docker://x", REFUSED, "re-points the registries an image name resolves against"),
    ("environment", "export CONTAINERS_STORAGE_CONF=/tmp/x; podman images", REFUSED, "re-points the image store"),
    ("environment", "export LD_LIBRARY_PATH=/tmp; shellcheck contrib/aib", REFUSED, "re-points the libraries the linter loads"),
    ("environment", "env PYTHONPATH=/tmp python3 maintenance_audit.py --skip-upstream", REFUSED, "puts a module ahead of the audit's imports, behind a wrapper"),
    ("environment", "export PYTHONPATH=/tmp; python3 maintenance_audit.py --skip-upstream", REFUSED, "the same, from a command of its own"),
    ("environment", "export GH_HOST=other; gh label list", REFUSED, "sends the token to another host"),
    ("environment", "export CONTAINERS_CONF=/tmp/x; podman ps", REFUSED, "re-points podman's configuration"),
    ("environment", "export SHELLCHECK_OPTS=./.env; shellcheck contrib/aib", REFUSED, "an operand that arrives through the environment rather than the argv"),
    ("environment", "FOO=bar shellcheck contrib/aib", REFUSED, "any variable set on a gated command, not only a listed one"),
    ("environment", "PAGER=cat git log", ALLOWED, "git spawns a pager only when stdout is a terminal, and a tool-run command has a pipe"),
    ("environment", "GIT_PAGER=cat git log -1", ALLOWED, "the same: the reach test shows the program is never run"),
    ("environment", "x=1; podman images", ALLOWED, "a variable in another command of the string reaches nothing the allow list covers"),
    ("environment", "FOO=1 echo x; podman images", ALLOWED, "an assignment on a command no rule covers"),
    ("environment", "grep -rn SHELLCHECK_OPTS docs/SECURITY-AI.md", ALLOWED, "the name without the = is not an assignment"),
    (
        "environment",
        "export 'GIT_EXTERNAL_DIFF'$'=./evil'; git diff HEAD",
        REFUSED,
        "quotes the identifier ahead of the ANSI-C escape, which used to shift an index-based scan out of alignment with its masked twin",
    ),
    (
        "environment",
        "export $'GIT_EXTERNAL_DIFF=./evil'; git diff HEAD",
        REFUSED,
        "the live $ opens the word with nothing before it, so no identifier prefix is even there to test by content",
    ),
    (
        "environment",
        "env -S 'FOO=x\\_GIT_EXTERNAL_DIFF=./evil\\_git diff' HEAD",
        REFUSED,
        "splits the assignment out of a detached -S argument, one word later",
    ),
    (
        "environment",
        "/usr/bin/env -S 'FOO=x\\_GIT_EXTERNAL_DIFF=./evil\\_git diff' HEAD",
        REFUSED,
        "the same detached -S with env spelled as a path",
    ),
    (
        "environment",
        "gh release create v$TAG",
        ALLOWED,
        "v sits ahead of a live $ the same shape a dangerous name does, but reaches no REFUSED_ENVIRONMENT name and names no gated command",
    ),
    (
        "environment",
        'export PATH="$PATH:/x"; git diff HEAD',
        ALLOWED,
        "the live $ sits at index 5 of an export-family word, not index 0, so opaque_assignment_prefix() does not fire; content-based scoping reads the plain identifier PATH ahead of it and PATH is not a REFUSED_ENVIRONMENT name",
    ),
    (
        "environment",
        'curl "https://x?a=$B"',
        ALLOWED,
        "the same opaque-live-$ shape export's own argument carries, on a command that reads no assignment from any of its words",
    ),
    # 2. Redirection. Output opens a path for writing before the command
    # runs; input hands a tool a file it prints back when it echoes what it
    # reads, which of the gated commands shellcheck, `just --fmt --check`
    # and git's --stdin each do.
    ("redirection", "git diff HEAD >cosign.pub", REFUSED, "truncates the file before git runs"),
    ("redirection", "git log -1 >>out", REFUSED, "appends to it"),
    ("redirection", "git diff HEAD >|x", REFUSED, "opens it past noclobber"),
    ("redirection", "git diff HEAD &>cosign.pub", REFUSED, "opens it for both streams"),
    ("redirection", "git diff HEAD 2>cosign.pub", REFUSED, "opens it for stderr through a descriptor"),
    ("redirection", "git diff HEAD >&cosign.pub", REFUSED, "the older &> spelling"),
    ("redirection", "git diff HEAD <>cosign.pub", REFUSED, "opens it read-write, creating it"),
    ("redirection", ">cosign.pub git diff HEAD", REFUSED, "bash takes a redirection before the command name"),
    ("redirection", "shellcheck contrib/aib >cosign.pub", REFUSED, "the same write through another allow-listed command"),
    ("redirection", "shellcheck - < .env", REFUSED, "a - operand makes the linter read and print back standard input"),
    ("redirection", "< .env shellcheck -", REFUSED, "the same with the redirection first"),
    ("redirection", "git diff HEAD 2>&1", ALLOWED, "a descriptor form touches no path"),
    ("redirection", "git diff HEAD >&-", ALLOWED, "closing a descriptor touches no path"),
    ("redirection", "git diff HEAD </dev/null", ALLOWED, "an empty standard input has nothing to print back, and this is how a session says it has no stdin"),
    ("redirection", "git log --stdin < .env", REFUSED, "--stdin reads revisions from standard input, and git prints the first line that is not one"),
    ("redirection", "< cosign.key git diff --stdin", REFUSED, "the same with the redirection first"),
    ("redirection", "git log --stdin < revs.txt", ALLOWED, "a revision list inside the checkout, of no deny shape"),
    ("redirection", "git log --stdin <<< HEAD~3", ALLOWED, "a here-string carries content, not a path"),
    ("redirection", "shellcheck contrib/aib </dev/null", ALLOWED, "the exempt target on the read side"),
    ("redirection", "shellcheck - <<< 'echo hi'", ALLOWED, "a here-string carries content, not a path"),
    ("redirection", "just --fmt --check -f - < ./.env", REFUSED, "a - justfile makes just read standard input, and it prints the line it could not parse"),
    ("redirection", "hadolint Containerfile < contrib/aib", ALLOWED, "hadolint reports a position and the offending character, never the source line"),
    ("redirection", "ruff check >cosign.pub", ALLOWED, "its allow row carries no :*, so the redirection makes the string match no rule and Claude Code prompts"),
    ("options", "just --fmt --check --justfile ./.env", REFUSED, "an option that hands just a file it prints a line of back"),
    # 3. Word rewriting bash does before the tool sees the word.
    ("word rewriting", "git diff {a,.env}", REFUSED, "a brace is two words to bash and one to a scanner"),
    ("word rewriting", "shellcheck {contrib/aib,.env}", REFUSED, "the same in a lint run"),
    ("word rewriting", "git diff -- ~/.aws/credentials", REFUSED, "an unquoted leading ~ is a home directory"),
    ("word rewriting", "shellcheck ~/.ssh/id_rsa", REFUSED, "the same in a lint run"),
    ("word rewriting", "git diff $(echo /dev/null) ./cosign.key", REFUSED, "a substitution builds the word at runtime"),
    ("word rewriting", "git diff `echo /dev/null` ./cosign.key", REFUSED, "the backtick spelling"),
    ("word rewriting", "git diff <(true) ./cosign.key", REFUSED, "a process substitution is a /dev/fd path outside the checkout"),
    ("word rewriting", "podman images >(cat >cosign.pub)", REFUSED, "the inner command of a substitution is held to no rule"),
    ("word rewriting", "hadolint $F", REFUSED, "a word a gated command receives that bash builds at runtime"),
    ("word rewriting", "git diff HEAD@{1}", ALLOWED, "bash expands a brace only with a comma or a .. in it, and git's reflog syntax has neither"),
    ("word rewriting", "git diff -- *", ALLOWED, "a glob cannot name a file outside the working directory without a /, a .. or a ~, each refused in the pattern"),
    ("word rewriting", "shellcheck tests/e2e/*.sh", ALLOWED, "the one expansion the scan performs, because each file it names is then checked"),
    ("word rewriting", "git log --format='%h $x'", ALLOWED, "single quotes make the $ a literal"),
    # 4. The command name itself.
    ("command name", "/usr/bin/git diff --no-index /dev/null ./cosign.key", REFUSED, "a path names the same tool"),
    ("command name", "env git diff --no-index /dev/null ./cosign.key", REFUSED, "so does a wrapper"),
    ("command name", "nice git diff --no-index /dev/null ./cosign.key", REFUSED, "and any other of them"),
    ("command name", "command -p git diff --no-index /dev/null ./cosign.key", REFUSED, "behind the wrapper's own option"),
    ("command name", "timeout 5 shellcheck ./.env", REFUSED, "a wrapper whose first argument is not the name"),
    ("command name", "{,git} diff --no-index /dev/null ./cosign.key", REFUSED, "bash drops the empty word and runs git"),
    ("command name", "{,shellcheck} ./.env", REFUSED, "the same for the linter"),
    ("command name", "/usr/bin/shellcheck ./.env", REFUSED, "a path to the linter"),
    ("command name", "command -v shellcheck", ALLOWED, "naming a command is not running it"),
    ("command name", "echo git diff --no-index a b", ALLOWED, "the name has to stand in command position"),
    ("command name", "sh -c 'git diff --no-index /dev/null ./cosign.key'", ALLOWED, "the command is not a word of this string; it matches no allow rule either, so Claude Code prompts"),
    ("command name", "noglob git diff --no-index /dev/null ./cosign.key", REFUSED, "zsh's noglob runs the command, and Claude Code steps over it before matching a rule"),
    ("command name", "noglob shellcheck ./.env", REFUSED, "the same wrapper in front of the linter"),
    ("command name", "noglob podman ps >out", REFUSED, "bash has no noglob, but it opens the target before it says so"),
    ("command name", "noglob git diff HEAD", ALLOWED, "stepping over the wrapper finds an ordinary git diff"),
    ("command name", "printf '%s\\n' /dev/null ./cosign.key | xargs git diff", REFUSED, "xargs hands git operands that are not in the string, and the allow rule matches xargs git diff"),
    ("command name", "xargs git diff <list.txt", REFUSED, "the same operands read from a file on standard input"),
    ("command name", "xargs -a list.txt git diff", REFUSED, "the same operands read from a file xargs opens itself"),
    ("command name", "xargs -r -0 git diff", REFUSED, "behind xargs's own options"),
    ("command name", "timeout 5 xargs git diff", REFUSED, "behind a wrapper in front of xargs"),
    ("command name", "printf '%s\\n' ./.env | xargs shellcheck", REFUSED, "the linter prints back a file named only on standard input"),
    ("command name", "xargs -a list.txt shellcheck", REFUSED, "the same file named in a list the string never shows"),
    ("command name", "nice xargs shellcheck", REFUSED, "a wrapper before xargs does not hide the linter behind it"),
    ("command name", "git diff --name-only | xargs echo", ALLOWED, "xargs in front of a command no rule covers: Claude Code prompts for it"),
    ("command name", "git ls-files -z | xargs -0 grep -l shellcheck", ALLOWED, "a gated name among the arguments of the command xargs runs is not the command"),
    ("command name", "git log --grep=xargs -1", ALLOWED, "the word xargs as an argument runs nothing"),
    ("command name", "/usr/bin/noglob podman ps >out", REFUSED, "a wrapper in /usr/bin is the same wrapper, and Claude Code compares its basename"),
    ("command name", "/usr/bin/timeout 5 shellcheck contrib/aib >out", REFUSED, "the same with the wrapper's own argument before the linter"),
    ("command name", "/usr/bin/env shellcheck contrib/aib >out", REFUSED, "env by path hides the linter's redirection no better than env"),
    ("command name", "/usr/bin/nohup podman ps >out", REFUSED, "nohup by path"),
    ("command name", "/usr/bin/timeout 5 shellcheck ./.env", REFUSED, "the operand scan reads past a path-spelled wrapper too"),
    ("command name", "/usr/bin/nohup git diff --no-index /dev/null ./cosign.key", REFUSED, "and so does the git scan"),
    ("command name", "/usr/bin/timeout 5 xargs git diff", REFUSED, "and the xargs refusal"),
    ("command name", "$D/nohup git diff HEAD", REFUSED, "a wrapper path bash builds at runtime can be any program"),
    ("command name", "./shim/nohup git diff HEAD", REFUSED, "the matcher steps over it as nohup while bash runs the file at that path"),
    ("command name", "'./shim\\nohup' git diff HEAD", REFUSED, "the matcher cuts the path at a backslash too"),
    ("command name", "'./shim\\env' git diff --no-index /dev/null ./cosign.key", REFUSED, "the same shim, whatever it is handed"),
    ("command name", "/tmp/timeout 5 shellcheck contrib/aib", REFUSED, "a path outside /usr/bin and /bin is not the wrapper"),
    ("command name", "timeout 5 ./shim/nohup git diff HEAD", REFUSED, "behind a wrapper's own argument"),
    ("command name", "./x\\nohup git diff HEAD", REFUSED, "an unquoted backslash: the matcher reads nohup, bash runs ./xnohup"),
    ("command name", "timeout 5 ./x\\nohup git diff HEAD", REFUSED, "the same behind a wrapper's own argument"),
    ("command name", "/usr/bin\\timeout 5 podman ps >out", REFUSED, "the matcher cuts the typed word at the backslash; bash runs /usr/bintimeout, fails, and has truncated out"),
    ("command name", "/usr/bin\\nohup shellcheck contrib/aib >out", REFUSED, "the same in front of the linter"),
    ("command name", "x\\nohup podman ps >out", REFUSED, "a backslash with no directory in front of it"),
    ("command name", '"nohup" podman ps', REFUSED, "a quoted wrapper name is not the plain spelling, whatever it runs"),
    ("command name", "git log --grep='x\\nohup' -1", ALLOWED, "a wrapper-shaped argument is not a wrapper"),
    ("command name", "/usr/bin/timeout 60 git diff HEAD", ALLOWED, "the wrapper where the distribution installs it, in front of an ordinary git diff"),
    ("command name", "/usr/bin/nohup git diff HEAD", ALLOWED, "stepping over the path-spelled wrapper finds an ordinary git diff"),
    ("command name", "/usr/bin/env echo x >out", ALLOWED, "a redirection on a command no rule covers, behind a path-spelled wrapper"),
    # 5. Options that load or write, per tool.
    ("options", "git -c diff.external=/tmp/evil diff", REFUSED, "the shortest path from a permitted git diff to running a program"),
    ("options", "git -P -c diff.external=/tmp/evil diff", REFUSED, "an unrefused global option ahead of it does not hide it"),
    ("options", "git --exec-path=/tmp diff", REFUSED, "re-points the programs git runs"),
    ("options", "git --git-dir=/tmp/other/.git log", REFUSED, "re-points the repository"),
    ("options", "git --upload-pack=/tmp/evil log", REFUSED, "listed against a git that starts taking it before a subcommand"),
    ("options", "git diff --output=/tmp/out", REFUSED, "writes any file"),
    ("options", "git diff -aOorder1", REFUSED, "reads a further path from a cluster of short options"),
    ("options", "git diff --ext-diff", REFUSED, "runs a configured external program"),
    ("options", "shellcheck -s bash ./.env", REFUSED, "the operand after a value-taking option is still reached"),
    ("options", "git log -c -p", ALLOWED, "-c after the subcommand is a diff format"),
    ("options", "git log -SOAuth -p", ALLOWED, "the O in a value is text to search for"),
    ("options", "git log -S 'foo\\bar' --oneline", ALLOWED, "git's own pickaxe option, not env's -S -- the backslash in the search string is nothing env ever re-splits"),
    ("options", "git diff -S 'a\\b' HEAD", ALLOWED, "the same pickaxe option on git diff, detached the way env's -S is when it belongs to env"),
    ("options", "shellcheck -e SC2034 -x tests/e2e/lib.sh", ALLOWED, "-x is load-bearing in this repository's own lint command"),
    ("options", "env -C/tmp/other git diff", REFUSED, "-C's argument attached with no separator, which env accepts and the exact-match test missed"),
    ("options", "env -iC/etc git diff HEAD", REFUSED, "-C clustered behind env's own -i, its value the rest of the same word"),
    ("options", "env -iC /etc git diff HEAD", REFUSED, "the same cluster with the value a separate word"),
    ("options", "env --chd=/tmp/other git diff", REFUSED, "an unambiguous abbreviation of --chdir"),
    ("options", "env --split='FOO=x GIT_EXTERNAL_DIFF=./evil git diff HEAD'", REFUSED, "an unambiguous abbreviation of --split-string, carrying the same re-splitting"),
    ("options", "env -iS'FOO=x GIT_EXTERNAL_DIFF=./evil git diff HEAD'", REFUSED, "-S clustered behind env's own -i, attached to the rest of the word"),
    ("options", "env -i git diff HEAD", ALLOWED, "env's own -i carries neither letter wrapper_relocates() or env_split_string_value() reads"),
    ("options", "podman images --cpu-profile cosign.pub", REFUSED, "podman opens the path and dumps a CPU profile over it"),
    ("options", "podman images --cpu-profile=cosign.pub", REFUSED, "the attached spelling of the same option"),
    ("options", "podman ps --memory-profile .claude/settings.json", REFUSED, "the memory profile writes the same way"),
    ("options", "podman ps --memory-profile=cosign.pub", REFUSED, "the attached spelling of the memory profile"),
    ("options", "podman inspect --memory-profile=.claude/hooks/gate_git_diff.py x", REFUSED, "overwrites this hook through another allow-listed verb"),
    ("options", "podman image exists x --cpu-profile out", REFUSED, "the three-word prefix takes the option after its operand"),
    ("options", "timeout 5 podman images --cpu-profile cosign.pub", REFUSED, "behind a wrapper"),
    ("options", "git status; podman images '--cpu-profile' cosign.pub", REFUSED, "quoted, in a later command of the string"),
    ("options", "podman images", ALLOWED, "the verb without a profile option writes nothing"),
    ("options", "podman ps -a --no-trunc --format json", ALLOWED, "ordinary options on the same verb"),
    ("options", "echo podman images --cpu-profile x", ALLOWED, "the flag words as operands of a command no rule covers"),
    ("options", "git log --grep=cpu-profile -1", ALLOWED, "the flag's name as text outside a podman command"),
    ("options", "podman images --cpu-pro{f..f}ile cosign.pub", REFUSED, "bash expands the singleton range into --cpu-profile; the word as typed spells no option"),
    ("options", "podman images --{cpu,memory}-profile cosign.pub", REFUSED, "one word to the scanner, both profile options to podman"),
    ("options", "podman ps --memory-profile{,}=cosign.pub", REFUSED, "the attached spelling built by a brace behind the option name"),
    ("options", "podman inspect --format '{{.Names}},{{.Status}}' x", ALLOWED, "a quoted Go template is braces bash leaves alone; podman's --format is made of them"),
    ("options", "podman images --format {{.Repository}}:{{.Tag}}", ALLOWED, "unquoted, but no comma or .. inside a brace, so bash expands nothing"),
    ("options", "podman images --cpu-pro\"{f..f}\"ile cosign.pub", ALLOWED, "the brace is quoted, so podman gets the literal --cpu-pro{f..f}ile and fails on an unknown flag"),
    ("options", "podman images --cpu-profil*", REFUSED, "bash expands the glob to --cpu-profile=cosign.pub once a file of that name exists; run for real, it overwrote cosign.pub"),
    ("options", "podman ps [-]-memory-profile=cosign.pub", REFUSED, "a bracket glob that matches a file named --memory-profile=cosign.pub"),
    ("options", "podman inspect ?-cpu-profile=cosign.pub x", REFUSED, "a ? in the first position builds the option from a file name"),
    ("options", "podman images *", REFUSED, "a bare glob lists the directory, which can hold a file named --cpu-profile=cosign.pub"),
    ("options", "podman images ~/x", REFUSED, "an unquoted leading ~ is also a word bash rewrites before podman runs"),
    ("options", "podman images 'fedora*'", ALLOWED, "a quoted pattern reaches podman as typed and names no file"),
    ("options", "podman images fedora\\*", ALLOWED, "an escaped * is a literal character, not a glob"),
    ("options", "podman images @(--cpu-profile=cosign.pub)", REFUSED, "an extglob pattern is one word under shopt -s extglob and matches a file named like the option"),
    ("options", "podman ps +(--memory-profile=cosign.pub)", REFUSED, "the +(...) extglob form"),
    ("options", "podman images fedora!(x)", REFUSED, "an extglob suffix on an ordinary word"),
    ("options", "podman images '@'(x)", ALLOWED, "the @ is quoted, so it is no pattern; bash then errors on the bare parenthesis"),
    ("options", "podman images --cpu-profile cosign.pub *", REFUSED, "a literal flag beside a glob is refused for the flag"),
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


    def test_git_prints_the_first_line_of_a_file_on_standard_input(self) -> None:
        # Why an input redirection on git is checked at all. `--stdin` reads
        # revisions from standard input, and the first line that does not
        # resolve ends the run with `fatal: bad revision '<line>'` -- the
        # whole line, so a `.env`'s first NAME=value arrives value included.
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / ".env"
            secret.write_text("SECRET_TOKEN=stand-in-not-a-secret\n")
            for subcommand in ("log", "diff"):
                with self.subTest(subcommand=subcommand), secret.open() as stdin:
                    result = subprocess.run(
                        ["git", subcommand, "--stdin"],
                        cwd=ROOT,
                        stdin=stdin,
                        capture_output=True,
                        text=True,
                    )
                    self.assertIn(
                        "stand-in-not-a-secret",
                        result.stdout + result.stderr,
                        f"git {subcommand} --stdin no longer prints the line it could not "
                        "resolve; re-derive why the git branch of refusal() checks an input "
                        "redirection's target",
                    )
        for command in ("git log --stdin < .env", "git diff --stdin < ./.env"):
            with self.subTest(command=command):
                self.assertIsNotNone(
                    gate.refusal(command),
                    "the redirection just shown to print a line of the file is not refused",
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
            "command name; re-derive why command_words() skips redirections",
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

    def test_shellcheck_lints_the_operand_it_is_given_in_its_environment(self) -> None:
        # The primitive the SHELLCHECK_OPTS refusal exists for, shown rather
        # than taken from the manual page: the linter splits the variable and
        # prepends it to its own argument list, operands included, so the file
        # named in it is linted and printed back while the argv names only the
        # script. All three spellings the gate refuses -- the leading
        # assignment, `env NAME=`, and an `export` in an earlier command --
        # reach the linter as this one environment, so running it through bash
        # in each spelling is what shows the gate is not refusing three
        # unrelated things.
        if shutil.which("shellcheck") is None:
            self.skipTest("shellcheck is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "ok.sh").write_text("#!/bin/bash\ntrue\n")
            Path(tmp, ".env").write_text("SECRET_TOKEN=stand-in-not-a-secret\n")
            for command in (
                "SHELLCHECK_OPTS=./.env shellcheck ./ok.sh",
                "env SHELLCHECK_OPTS=./.env shellcheck ./ok.sh",
                "export SHELLCHECK_OPTS=./.env; shellcheck ./ok.sh",
                "SHELLCHECK_OPTS+=./.env shellcheck ./ok.sh",
            ):
                with self.subTest(command=command):
                    result = subprocess.run(
                        ["bash", "--norc", "--noprofile", "-c", command],
                        cwd=tmp,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertIn(
                        "SECRET_TOKEN=stand-in-not-a-secret",
                        result.stdout,
                        "shellcheck no longer reads operands out of SHELLCHECK_OPTS; "
                        "re-derive why assigned_environment() exists",
                    )
                    self.assertIsNotNone(
                        gate.refusal(command),
                        "the command just shown to print the file back is not refused",
                    )


class CorpusTests(unittest.TestCase):
    """The #428 corpus, driven from REACH_CORPUS.

    One table, one test, and a new shape is one row. What this adds over
    REFUSED_COMMANDS is the other half of the decision: a row that is
    deliberately let through carries the reason it is, so "not decided yet"
    and "decided to allow" stop looking the same from the outside.
    """

    def test_every_corpus_row_is_decided_the_way_it_says(self) -> None:
        for shape, command, expected, why in REACH_CORPUS:
            with self.subTest(shape=shape, command=command):
                reason = gate.refusal(command)
                if expected == REFUSED:
                    self.assertIsNotNone(reason, f"{command!r} is let through, and it {why}")
                else:
                    self.assertIsNone(
                        reason,
                        f"{command!r} is refused, and it was decided to allow it: {why}",
                    )

    def test_every_corpus_refusal_names_a_word_of_its_command(self) -> None:
        # A refusal the model cannot act on gets retried differently rather
        # than reported. The variable, not the value, is what names an
        # environment row; a substitution reaches the hook as its opening.
        for shape, command, expected, _ in REACH_CORPUS:
            if expected != REFUSED:
                continue
            with self.subTest(shape=shape, command=command):
                reason = gate.refusal(command) or ""
                words = [
                    part
                    for word in command.replace("\n", " ").split()
                    if word not in {"git", "&&", "|"}
                    for part in (
                        word,
                        word.split("=", 1)[0],
                        word.split("+=", 1)[0],
                        word[:2],
                        word.strip("'\""),
                    )
                ]
                self.assertTrue(
                    any(word and word in reason for word in words),
                    f"the refusal of {command!r} names none of its words: {reason}",
                )

    def test_the_corpus_covers_every_family_the_issue_names(self) -> None:
        # The count is what keeps the table from going vacuous: a family
        # quietly emptied to make a change pass would read as covered.
        families = {shape for shape, _, _, _ in REACH_CORPUS}
        self.assertEqual(
            families,
            {"environment", "redirection", "word rewriting", "command name", "options"},
        )
        for family in families:
            rows = [row for row in REACH_CORPUS if row[0] == family]
            with self.subTest(family=family):
                self.assertGreaterEqual(len(rows), 4, f"{family} is barely covered")
                self.assertTrue(
                    any(row[2] == REFUSED for row in rows)
                    and any(row[2] == ALLOWED for row in rows),
                    f"{family} has no row on one side of the line, so the rule it "
                    "states could be 'refuse everything' or 'refuse nothing'",
                )
        commands = [command for _, command, _, _ in REACH_CORPUS]
        self.assertEqual(len(commands), len(set(commands)), "a command is listed twice")

    def test_every_refused_variable_has_a_row_of_its_own(self) -> None:
        # The table in the hook and the corpus here are deliberately not
        # derived from each other. A test that read its rows from
        # REFUSED_ENVIRONMENT would pass with any row deleted -- it would just
        # check one variable fewer -- so each name is spelled out here, and
        # removing it from the hook fails the row above rather than quietly
        # reopening the hole the row was added for.
        spelled = " ".join(command for _, command, _, _ in REACH_CORPUS)
        for pattern, _ in gate.REFUSED_ENVIRONMENT:
            with self.subTest(pattern=pattern):
                self.assertIn(
                    pattern.rstrip("*"),
                    spelled,
                    f"{pattern} is refused by the hook and no corpus row spells it",
                )


class ReachCorpusTests(unittest.TestCase):
    """What the corpus rows are decided against: bash, git and the linters.

    Every assertion here runs the shape rather than reasoning about it, for
    the reason the rest of this module does -- a refusal derived from a
    manual page outlives the behaviour it was derived from without saying so.
    """

    def test_every_exporting_spelling_reaches_a_later_command(self) -> None:
        # The shapes of #428's first family, against a program that prints
        # what it was given. `readonly` and a bare `declare` are in the corpus
        # as refused although they do not export: this is where that is
        # checked rather than assumed, so the over-refusal stays a known one.
        exports = {
            "VAR=x cmd": ("PROBE=hit ./show.sh", True),
            "VAR+=x cmd": ("PROBE+=hit ./show.sh", True),
            "env VAR=x cmd": ("env PROBE=hit ./show.sh", True),
            "env -i VAR=x cmd": ("env -i PROBE=hit ./show.sh", True),
            "env 'VAR'=x cmd": ("env 'PROBE'=hit ./show.sh", True),
            "env -S'VAR=x cmd'": ("env -S'PROBE=hit ./show.sh'", True),
            "env --split-string": ("env --split-string='PROBE=hit ./show.sh'", True),
            "export VAR=x; cmd": ("export PROBE=hit; ./show.sh", True),
            "export VAR+=x; cmd": ("export PROBE+=hit; ./show.sh", True),
            "declare -x VAR=x; cmd": ("declare -x PROBE=hit; ./show.sh", True),
            "typeset -x VAR=x; cmd": ("typeset -x PROBE=hit; ./show.sh", True),
            "newline as a separator": ("export PROBE=hit\n./show.sh", True),
            "readonly VAR=x; cmd": ("readonly PROBE=hit; ./show.sh", False),
            "declare VAR=x; cmd": ("declare PROBE=hit; ./show.sh", False),
        }
        with tempfile.TemporaryDirectory() as tmp:
            show = Path(tmp, "show.sh")
            show.write_text('#!/bin/bash\necho "PROBE=${PROBE:-unset}"\n')
            show.chmod(0o755)
            for spelling, (command, reaches) in exports.items():
                with self.subTest(spelling=spelling):
                    result = subprocess.run(
                        ["bash", "--norc", "--noprofile", "-c", command],
                        cwd=tmp,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        "PROBE=hit" in result.stdout,
                        reaches,
                        f"{command!r} no longer puts the variable where it did; "
                        "re-derive which spellings assigned_environment() has to read",
                    )
                    self.assertIsNotNone(
                        gate.refusal(command.replace("PROBE", "GIT_EXTERNAL_DIFF").replace("./show.sh", "git diff")),
                        f"{spelling} is not refused for a variable that reaches git",
                    )

    def test_git_runs_the_program_an_earlier_command_exported(self) -> None:
        # The primitive behind the `export` half of the family: bash applies
        # it to every later command in the string, so the git invocation
        # carries no assignment for a leading-assignment scan to find, and the
        # program runs once per changed path all the same.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            evil = repo / "evil"
            evil.write_text("#!/bin/sh\necho EXTERNAL-DIFF-RAN\n")
            evil.chmod(0o755)
            (repo / "a.txt").write_text("a\n")
            subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "x"],
                check=True,
            )
            (repo / "a.txt").write_text("b\n")
            for command in (
                "export GIT_EXTERNAL_DIFF=./evil; git diff",
                "declare -x GIT_EXTERNAL_DIFF=./evil; git diff",
                "env GIT_EXTERNAL_DIFF=./evil git diff",
                "GIT_EXTERNAL_DIFF+=./evil git diff",
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
                        "EXTERNAL-DIFF-RAN",
                        result.stdout,
                        f"git no longer runs the program {command!r} puts in its "
                        "environment; re-derive why REFUSED_ENVIRONMENT is scanned "
                        "across the whole command",
                    )
                    self.assertIsNotNone(gate.refusal(command))

    def test_bash_runs_git_behind_a_wrapper_and_a_brace(self) -> None:
        # The command-name family. `{,git}` expands to an empty word and
        # `git`, and bash drops the empty one and runs git -- so the word
        # naming the command is not the name of the command, and a scan
        # reading only the first word found no gated command at all.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "cosign.key").write_text("STAND-IN-NOT-A-KEY\n")
            for command in (
                "env git diff --no-index /dev/null ./cosign.key",
                "command git diff --no-index /dev/null ./cosign.key",
                "nice git diff --no-index /dev/null ./cosign.key",
                "/usr/bin/env git diff --no-index /dev/null ./cosign.key",
                "{,git} diff --no-index /dev/null ./cosign.key",
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
                        "STAND-IN-NOT-A-KEY",
                        result.stdout,
                        f"{command!r} no longer reaches git; re-derive why "
                        "command_words() steps over a wrapper",
                    )
                    self.assertIsNotNone(
                        gate.refusal(command),
                        "the command just shown to print the key is not refused",
                    )

    def test_git_diffs_the_operands_xargs_reads_rather_than_the_ones_written(self) -> None:
        # xargs appends the words it reads -- from standard input, or from
        # the file its `-a` names -- to the command it runs, so the string
        # names no operand and git still gets the two `--no-index` needs.
        # Claude Code's matcher reads `xargs git diff` as `git diff`, which
        # is why xargs is refused rather than stepped over the way `nice`
        # is: the operands a scan would have to check are not in the string.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "cosign.pub").write_text("STAND-IN-NOT-A-KEY\n")
            (repo / "list.txt").write_text("/dev/null\n./cosign.pub\n")
            for command in (
                "printf '%s\\n' /dev/null ./cosign.pub | xargs git diff",
                "xargs git diff <list.txt",
                "xargs -a list.txt git diff",
                "timeout 5 xargs -r git diff <list.txt",
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
                        "STAND-IN-NOT-A-KEY",
                        result.stdout,
                        f"{command!r} no longer reaches the file; re-derive why "
                        "xargs_fed_command() refuses xargs in front of git",
                    )
                    self.assertIsNotNone(
                        gate.refusal(command),
                        "the command just shown to print the file is not refused",
                    )

    def test_bash_opens_the_target_in_front_of_a_command_it_cannot_run(self) -> None:
        # bash opens a redirection before it looks the command up, so a
        # command that is not found still empties the target. `noglob` is
        # zsh's (zsh runs podman into the file), and `/usr/bin\timeout` is
        # `/usr/bintimeout` to bash; Claude Code steps over both before it
        # matches `Bash(podman ps:*)`.
        for command in ("noglob podman ps >victim", "/usr/bin\\timeout 5 podman ps >victim"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                victim = Path(tmp) / "victim"
                victim.write_text("ORIGINAL-CONTENT\n")
                subprocess.run(
                    ["bash", "--norc", "--noprofile", "-c", command],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotIn(
                    "ORIGINAL-CONTENT",
                    victim.read_text(),
                    f"bash no longer truncates the target of {command!r}; re-derive why "
                    "command_words() reads the word as a wrapper",
                )
                self.assertIsNotNone(
                    gate.refusal(command.replace("victim", "cosign.pub")),
                    "the command just shown to truncate a file is not refused",
                )

    def test_bash_runs_the_file_a_wrapper_path_names(self) -> None:
        # Claude Code's matcher cuts a wrapper word at its last `/` or `\`
        # and steps over `./shim/nohup` as `nohup`, so `./shim/nohup git diff
        # HEAD` matches `Bash(git diff:*)`. bash runs the file at that path --
        # backslash and all when it is quoted, `./xnohup` for an unquoted
        # `./x\nohup` -- and a file the session wrote can ignore the ordinary
        # git diff it is handed and print the key instead.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "cosign.pub").write_text("STAND-IN-NOT-A-KEY\n")
            (repo / "shim").mkdir()
            for shim in (repo / "shim" / "nohup", repo / "shim\\nohup", repo / "xnohup"):
                shim.write_text("#!/bin/sh\ncat ./cosign.pub\n")
                shim.chmod(0o755)
            for command in (
                "./shim/nohup git diff HEAD",
                "'./shim\\nohup' git diff HEAD",
                "./x\\nohup git diff HEAD",
                "timeout 5 ./x\\nohup git diff HEAD",
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
                        "STAND-IN-NOT-A-KEY",
                        result.stdout,
                        f"bash no longer runs the file {command!r} names; re-derive "
                        "why spelled_wrapper_path() refuses it",
                    )
                    self.assertIsNotNone(
                        gate.refusal(command),
                        "the command just shown to print the file is not refused",
                    )

    def test_the_command_xargs_runs_is_the_one_the_gate_reads(self) -> None:
        # xargs_command_starts() models xargs's own options, and an option
        # it reads as taking no value where xargs takes one is a hole: the
        # gate takes the value for the command and never sees the git after
        # it. So every letter, and every long option `xargs --help` lists, is
        # run against the real xargs with a value word that is a program of
        # its own. If xargs runs the word after the value, the option took
        # it, and the gate has to refuse that spelling with git there. If
        # xargs runs the value word, the option took nothing, and the gate
        # has to leave `xargs <option> grep -n git` alone -- without that
        # half, refusing every xargs with a git anywhere after it would pass.
        # An option xargs rejects, or one that needs a terminal (`-p`, `-o`;
        # the new session has none), runs neither program and decides
        # nothing. Two values, because `-d` takes one character and `-s` a
        # limit large enough to fit the command it runs.
        with tempfile.TemporaryDirectory() as tmp:
            for name, marker in (("1", "RAN-VALUE"), ("4096", "RAN-VALUE"), ("after", "RAN-AFTER")):
                program = Path(tmp, name)
                program.write_text(f"#!/bin/sh\necho {marker}\n")
                program.chmod(0o755)
            env = {**os.environ, "PATH": f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}"}

            def ran(option: str, value: str) -> str:
                return subprocess.run(
                    ["xargs", option, value, "after"],
                    input="x\n",
                    cwd=tmp,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    start_new_session=True,
                    timeout=30,
                ).stdout

            usage = subprocess.run(["xargs", "--help"], capture_output=True, text=True, check=False).stdout
            options = [f"-{letter}" for letter in string.ascii_letters + string.digits]
            options += sorted(set(re.findall(r"--[a-z][a-z-]*", usage)))
            took_value: list[str] = []
            took_none: list[str] = []
            for option in options:
                outputs = {value: ran(option, value) for value in ("1", "4096")}
                valued = [value for value, output in outputs.items() if "RAN-AFTER" in output]
                with self.subTest(option=option):
                    if valued:
                        took_value.append(option)
                        self.assertIsNotNone(
                            gate.refusal(f"xargs {option} {valued[0]} git diff"),
                            f"xargs reads the word after {option} as its value, and the "
                            "gate took that value for the command xargs runs",
                        )
                    elif any("RAN-VALUE" in output for output in outputs.values()):
                        took_none.append(option)
                        self.assertIsNone(
                            gate.refusal(f"xargs {option} grep -n git"),
                            f"xargs reads no value after {option}, and the gate refused an "
                            "xargs that runs grep",
                        )
        # The signal fires both ways, or the loop above checked nothing.
        for option in ("-n", "-a", "-I", "--arg-file", "--max-args"):
            self.assertIn(option, took_value)
        for option in ("-0", "-r", "-i", "--null", "--replace"):
            self.assertIn(option, took_none)

    def test_git_does_not_run_a_pager_when_stdout_is_not_a_terminal(self) -> None:
        # Why `PAGER=cat git log` and `GIT_PAGER=prog git log` are corpus rows
        # decided the other way. A command run by the Bash tool has a pipe for
        # stdout, and git spawns a pager only for a terminal -- `--paginate`
        # included. If that ever changes, the pager variables belong in
        # REFUSED_ENVIRONMENT and this test is what says so.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            pager = repo / "pager"
            pager.write_text("#!/bin/sh\necho PAGER-RAN\n")
            pager.chmod(0o755)
            (repo / "a.txt").write_text("a\n")
            subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "x"],
                check=True,
            )
            for command in (
                "GIT_PAGER=./pager git log -1",
                "PAGER=./pager git log -1",
                "GIT_PAGER=./pager git --paginate log -1",
            ):
                with self.subTest(command=command):
                    result = subprocess.run(
                        ["bash", "--norc", "--noprofile", "-c", command],
                        cwd=repo,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotIn(
                        "PAGER-RAN",
                        result.stdout,
                        "git now runs the pager without a terminal, so the pager "
                        "variables reach a program and must join REFUSED_ENVIRONMENT",
                    )
                    self.assertIsNone(gate.refusal(command))

    def test_hadolint_does_not_print_the_line_it_could_not_parse(self) -> None:
        # Why the input-redirection and operand scans are shellcheck's alone.
        # ShellCheck prints the source line above every diagnostic, which makes
        # it a lossy `cat`; hadolint reports a position and the one character
        # it did not expect, and never the line. If that changes, hadolint
        # needs an operand scan of its own.
        if shutil.which("hadolint") is None:
            self.skipTest("hadolint is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp, "stand-in.env")
            secret.write_text("SECRET_TOKEN=stand-in-not-a-secret\n")
            result = subprocess.run(
                ["hadolint", str(secret)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, "the stand-in was meant not to parse")
            self.assertNotIn(
                "stand-in-not-a-secret",
                result.stdout + result.stderr,
                "hadolint now prints the line it could not parse, so an operand "
                "that names a denied file prints it back and hadolint needs the "
                "operand scan shellcheck has",
            )

    def test_just_prints_the_line_it_could_not_parse(self) -> None:
        # Why `just --fmt --check` has a scan of its own. It reports a parse
        # error with the offending source line under it, and a justfile's
        # comments and blank lines parse, so the line it reaches is the first
        # one carrying a value. If that ever stops being true the scan is
        # redundant rather than wrong, so this test records the behaviour the
        # refusal is for.
        if shutil.which("just") is None:
            self.skipTest("just is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp, "stand-in.env")
            secret.write_text("# a comment parses\n\nSECRET_TOKEN=stand-in-not-a-secret\n")
            result = subprocess.run(
                ["just", "--fmt", "--check", "--justfile", str(secret)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, "the stand-in was meant not to parse")
            self.assertIn(
                "stand-in-not-a-secret",
                result.stdout + result.stderr,
                "just no longer prints the line it could not parse; the scan in "
                "just_refusal() is then belt and braces rather than the thing "
                "that keeps a denied file out of the transcript",
            )


# The reserved words that open a compound command, and the two (`time`, `!`)
# that may stand in front of one.
GROUP_OPENERS = (
    "if",
    "for",
    "while",
    "until",
    "case",
    "select",
    "function",
    "coproc",
    "time",
    "!",
)

# Allow-row patterns (the text inside `Bash(...)`) that can reach a grouped
# command, and ones that cannot. They hold reaches_a_group() to its job:
# today's settings have none of the first kind, so a check against the
# settings alone passes whatever it looks for.
GROUP_REACHING_PATTERNS = (
    "",
    "*",
    ":*",
    "{ git diff HEAD; } >out",
    "(git diff HEAD) >out",
    "if true; then git diff HEAD; fi >out",
    "for f in a; do git diff HEAD; done >out",
    "while false; do :; done >out",
    "if true\nthen git diff HEAD\nfi >out",
    "if true & then git diff HEAD & fi >out",
    "if *",
    "if:*",
    "if true:*",
    "for f in a:*",
    "while *",
    "i*",
    "time *",
    "! *",
)
GROUP_PLAIN_PATTERNS = (
    "git diff:*",
    "git diff *",
    "git status*",
    "git diff HEAD >out",
    "git diff HEAD 2>&1",
    "git status && git diff HEAD",
    "git diff HEAD &>out",
    "git diff HEAD |& cat",
    "ruff check",
    "t:*",
    "ifconfig:*",
)


def reaches_a_group(pattern: str) -> bool:
    """Whether a `Bash(...)` pattern can match a command that holds a grouped command.

    An exact pattern that names one has a parenthesis or a brace in it, or, for
    the keyword forms (`if ...; then ...; fi >f`), what ends each part: a `;`, a
    newline or a lone `&` (`if true & then ... & fi >f` is the same `if`). The
    `&` in `&&`, `2>&1`, `&>` and `|&` ends nothing and is not counted. A
    pattern with a `*` can match one when the text before the `*` is empty or
    could begin a compound: `Bash(*)`, `Bash(if *)`, `Bash(i*)`, `Bash(time:*)`.
    """
    if any(character in pattern for character in "(){};\n"):
        return True
    if re.search(r"(?<![&<>|])&(?![&>])", pattern) or not pattern.strip():
        return True
    if "*" not in pattern:
        return False
    if pattern.endswith(":*") and "*" not in pattern[:-2]:
        head = pattern[:-2] + " "  # `:*` ends the word in front of it
    else:
        head = pattern.split("*", 1)[0]
    words = head.split()
    if not words:
        return True
    if len(words) == 1 and not head[-1].isspace():
        # the `*` can finish the word: `Bash(i*)` matches `if ...`
        return any(opener.startswith(words[0]) for opener in GROUP_OPENERS)
    return words[0] in GROUP_OPENERS


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

    def test_no_allow_rule_runs_bash(self) -> None:
        # aurora-zfs-simple and arch-bootc allow `bash -n`, and their hooks
        # refuse what it prints there (aurora-zfs-simple#233,
        # arch-bootc#345): -n stops bash running a script, not printing it,
        # so `bash -n -v ./cosign.key` prints the key, -o history and -i copy
        # it into ~/.bash_history, and a syntax error prints its line. This
        # hook carries none of those rules, because no row here runs bash and
        # each of those spellings prompts. A row that did would pass the test
        # above as soon as GATED_PREFIXES listed it, with none of them, so it
        # fails here until they are ported.
        allow = json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]["allow"]
        shells = [
            rule
            for rule in allow
            if rule.startswith("Bash(")
            and re.split(r"[\s:*)]", rule[len("Bash(") :], maxsplit=1)[0].rsplit("/", 1)[-1]
            in {"bash", "sh"}
        ]
        self.assertEqual(
            shells,
            [],
            "an allow row runs a shell; port the bash -n option and operand rules from "
            "aurora-zfs-simple's gate before allowing it",
        )

    def test_no_allow_rule_reaches_a_redirection_written_after_a_group(self) -> None:
        # `(git diff HEAD) >cosign.pub` and `{ git log --stdin; } <.env` write
        # and read the same files as the refused `git diff HEAD >cosign.pub`
        # and `git log --stdin <cosign.key`, but the redirection stands
        # outside the git command, and the hook does not charge it to git
        # (issue #453). It does not need to while nothing here reaches those
        # strings: Claude Code asks before it runs any command that contains a
        # subshell or a brace group, whatever the allow rows say about the
        # command inside ("Contains subshell", "Contains compound_statement").
        # Checked on 2.1.273 and 2.1.280 with `Bash(git diff:*)` and
        # `Bash(git log:*)` allowed, in the default and acceptEdits modes; the
        # `if`, `for`, `while` and function forms were asked the same way
        # ("Contains if_statement" and so on). The one way such a string ran
        # with no prompt was a row that names the grouped string itself
        # (`Bash({ git diff HEAD; } >out3.txt)` ran exactly that string), or a
        # row that allows everything (`Bash`, `Bash(*)`). A wildcard row whose
        # fixed part opens a compound (`Bash(if true:*)`) matches such a string
        # the same way. This fails if a row like that is added;
        # reaches_a_group() says what counts (aurora-zfs-simple#241).
        allow = json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]["allow"]
        patterns = {rule: rule[len("Bash(") : -1] for rule in allow if rule.startswith("Bash(")}
        self.assertTrue(patterns)
        reaching = [rule for rule in allow if rule == "Bash"] + [
            rule for rule, pattern in patterns.items() if reaches_a_group(pattern)
        ]
        self.assertEqual(
            reaching,
            [],
            f"{reaching} can let a command that contains a subshell, a brace group or an "
            "if/for/while compound run with no prompt, and the hook does not charge a "
            "redirection written after the group to the command inside it. Teach the hook "
            "that before adding the row.",
        )

    def test_the_group_check_tells_grouped_rows_from_plain_ones(self) -> None:
        for pattern in GROUP_REACHING_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertTrue(reaches_a_group(pattern), "a row that reaches a group went unseen")
        for pattern in GROUP_PLAIN_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertFalse(reaches_a_group(pattern), "a plain row was taken for a group")

    def test_the_command_words_are_read_past_redirections_and_prefixes(self) -> None:
        # command_words() is the one walk both halves of the hook read, so it
        # has to step over a redirection wherever bash lets it stand, and over
        # an assignment or a keyword before the name, or the prefix is missed
        # and the write goes through.
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
        def prefix_of(segment: list[str]) -> tuple[str, ...] | None:
            return gate.gated_prefix(gate.command_words(segment))

        self.assertEqual(prefix_of(["podman", ">", "o", "image", "exists", "x"]), ("podman", "image", "exists"))
        self.assertEqual(prefix_of(["command", "-p", "/usr/bin/shellcheck", "x"]), ("shellcheck",))
        self.assertIsNone(prefix_of(["podman", "rmi", "x", ">", "o"]))
        self.assertIsNone(prefix_of(["python3", "maintenance_audit.py", ">", "o"]))
        self.assertIsNone(prefix_of(["echo", "shellcheck", "x"]))

    def test_the_same_walk_finds_git_behind_a_path_or_a_wrapper(self) -> None:
        # git_arguments() and gated_prefix() read the same Invocation, which
        # is what stops `env git diff --no-index a b` from being a git
        # invocation to neither of them. The arguments come back without the
        # shell's own words, so an input redirection's target is not read as
        # an operand.
        for segment, arguments in (
            (["git", "diff", "HEAD"], ["diff", "HEAD"]),
            (["/usr/bin/git", "diff"], ["diff"]),
            ([">", "out", "git", "diff"], ["diff"]),
            (["env", "git", "diff", "--no-index", "a", "b"], ["diff", "--no-index", "a", "b"]),
            (["env", "-i", "git", "log", "-1"], ["log", "-1"]),
            (["nice", "-n", "10", "git", "diff"], ["diff"]),
            (["timeout", "5", "git", "diff"], ["diff"]),
            (["git", "diff", "HEAD", "<", "/dev/null"], ["diff", "HEAD"]),
        ):
            with self.subTest(segment=segment):
                self.assertEqual(gate.git_arguments(gate.command_words(segment)), arguments)
        for segment in (["echo", "git", "diff"], ["shellcheck", "x"], [">", "out"]):
            with self.subTest(segment=segment):
                self.assertIsNone(gate.git_arguments(gate.command_words(segment)))

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
        # command_words() has to see past a redirection, its target and a
        # descriptor to the word that names the command, or every other
        # refusal is skipped for the segment.
        for segment, words in (
            ([">", "cosign.pub", "git", "diff", "HEAD"], ["git", "diff", "HEAD"]),
            (["2", ">", "err", "git", "log", "-1"], ["git", "log", "-1"]),
            (["{fd}", ">", "err", "git", "log", "-1"], ["git", "log", "-1"]),
            ([">", "$", "git", "diff", "HEAD"], ["git", "diff", "HEAD"]),
            ([">", "`printf", "cosign.pub`", "git", "diff", "HEAD"], ["git", "diff", "HEAD"]),
            ([">", "`x`", "git", "diff", "HEAD"], ["git", "diff", "HEAD"]),
            (["FOO=bar", ">", "out", "git", "diff"], ["git", "diff"]),
            (["FOO+=bar", ">", "out", "git", "diff"], ["git", "diff"]),
            (["<", "/dev/null", "git", "diff"], ["git", "diff"]),
            ([">&", "2", "git", "diff"], ["git", "diff"]),
            (["git", "diff", ">", "out"], ["git", "diff"]),
            ([">", "out", "echo", "x"], ["echo", "x"]),
        ):
            with self.subTest(segment=segment):
                self.assertEqual(gate.command_words(segment).words, words)
        # Both of bash's assignment operators are the environment rather than
        # the name: reading `FOO+=bar` as a command left `git` an argument of
        # it, and every other check in the segment unrun.
        self.assertEqual(gate.command_words(["FOO=bar", "git", "diff"]).assignments, ["FOO"])
        self.assertEqual(gate.command_words(["FOO+=bar", "git", "diff"]).assignments, ["FOO"])
        self.assertEqual(gate.command_words(["git", "diff"]).assignments, [])

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

    # Every shape a word's quoting can take that this repository's review
    # has actually found a skew in: a bare quote, a real escape (the quote
    # mark, a backslash), a backslash in front of something shlex does not
    # treat as an escape target at all (an ordinary letter, `$`, a
    # backtick), an ANSI-C quote, an unquoted-then-quoted word, and an
    # empty pair both alone and embedded. mask_quotes_stripped()'s whole
    # point is lining its own tokens up with tokenize(command)'s, character
    # for character, so runtime_assignment() and opaque_assignment_prefix()
    # walk the two at the same index; a length that drifts is the failure
    # mode both bugs shared.
    QUOTING_ALIGNMENT_CORPUS = (
        "'x'",
        '"x"',
        r"a\zb",
        r'"a\zb"',
        r'"a\\b"',
        r'"a\"b"',
        r'"a\$b"',
        r'"a\`b"',
        "$'x'",
        "a'b c'd",
        "'GIT_EXTERNAL_DIFF'$'=./evil'",
        "'GIT_EXTERNAL_DIFF'''$'=./evil'",
    )

    # Tokens whose *real* length is deliberately shorter than their masked
    # one: an empty quoted pair that is the whole word (`''`, `""`)
    # contributes nothing to the real token but one placeholder `Q` to the
    # masked one, so tokenize() does not lose the word entirely. Nothing
    # walks past index 0 of an empty token, so the skew is harmless there
    # and excluded from the equal-length assertion by name rather than by
    # writing a second, weaker test.
    QUOTING_ALIGNMENT_EMPTY_WHOLE_WORD = ("''", '""')

    def test_the_masked_copy_used_for_index_alignment_matches_token_length(self) -> None:
        for command in self.QUOTING_ALIGNMENT_CORPUS + self.QUOTING_ALIGNMENT_EMPTY_WHOLE_WORD:
            with self.subTest(command=command):
                tokens = gate.tokenize(command)
                masked = gate.tokenize(gate.mask_quotes_stripped(command))
                self.assertEqual(
                    len(tokens),
                    len(masked),
                    f"{command!r} split into a different number of tokens once masked",
                )
                for token, twin in zip(tokens, masked, strict=True):
                    if command in self.QUOTING_ALIGNMENT_EMPTY_WHOLE_WORD:
                        continue
                    self.assertEqual(
                        len(token),
                        len(twin),
                        f"{command!r}: token {token!r} and its masked twin {twin!r} "
                        "have different lengths, so an index-based scan over them "
                        "(runtime_assignment(), opaque_assignment_prefix()) drifts",
                    )

    def test_each_word_as_typed_lexes_back_to_its_token(self) -> None:
        # raw_words() is read beside tokenize()'s words by position, so it has
        # to split where tokenize() does, and each typed word has to be the
        # text that lexes to its token -- quote marks and backslashes kept,
        # nothing from a neighbouring word -- or the wrapper check reads one
        # word's spelling for another's.
        for command in (
            *self.QUOTING_ALIGNMENT_CORPUS,
            *self.QUOTING_ALIGNMENT_EMPTY_WHOLE_WORD,
            r"""a 'b c' \; "d\"e" f""",
            r"/usr/bin\timeout 5 podman ps >out",
            r"'./shim\nohup' git diff HEAD",
            "x=$(git diff);(echo ';'|cat) <<<''",
        ):
            with self.subTest(command=command):
                tokens = gate.tokenize(command)
                raws = gate.raw_words(command)
                self.assertEqual(len(raws), len(tokens), raws)
                for token, raw in zip(tokens, raws, strict=True):
                    self.assertEqual(gate.tokenize(raw), [token], f"{raw!r} is not the word typed for {token!r}")
        self.assertEqual(gate.raw_words(r"/usr/bin\timeout 5"), [r"/usr/bin\timeout", "5"])

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

    def test_a_refused_environment_word_is_read_as_a_word_not_a_position(self) -> None:
        # The assignment stands before the command name, so there is no
        # invocation to scope the refusal to when the word is read: the test is
        # the word itself, wherever in the command it sits. A name that merely
        # starts or ends the same is a different variable and is not refused,
        # or the rule would spread to words that set nothing. `GIT_CONFIG*` is
        # the one pattern with a wildcard, because the family is numbered.
        for word, name in (
            ("SHELLCHECK_OPTS=./.env", "SHELLCHECK_OPTS"),
            ("SHELLCHECK_OPTS=", "SHELLCHECK_OPTS"),
            ("SHELLCHECK_OPTS=-s bash", "SHELLCHECK_OPTS"),
            ("SHELLCHECK_OPTS+=./.env", "SHELLCHECK_OPTS"),
            ("SHELLCHECK_OPTS+=", "SHELLCHECK_OPTS"),
            ("-SSHELLCHECK_OPTS+=./.env shellcheck contrib/aib", "SHELLCHECK_OPTS"),
            ("-SSHELLCHECK_OPTS=./.env shellcheck contrib/aib", "SHELLCHECK_OPTS"),
            ("--split-string=SHELLCHECK_OPTS=./.env shellcheck contrib/aib", "SHELLCHECK_OPTS"),
            ("GIT_EXTERNAL_DIFF=/tmp/evil", "GIT_EXTERNAL_DIFF"),
            ("GIT_CONFIG_KEY_0=diff.external", "GIT_CONFIG_KEY_0"),
            ("GIT_CONFIG_COUNT=1", "GIT_CONFIG_COUNT"),
            ("LD_PRELOAD=x.so", "LD_PRELOAD"),
            ("PYTHONPATH+=/tmp", "PYTHONPATH"),
            ("-SGIT_DIR=/tmp/other/.git git log", "GIT_DIR"),
        ):
            with self.subTest(word=word):
                found = gate.assigned_environment(word)
                self.assertIsNotNone(found, f"{word!r} assigns nothing")
                self.assertEqual((found or ("", ""))[0], name)
        for word in (
            "SHELLCHECK_OPTS",
            "MY_SHELLCHECK_OPTS=./.env",
            "SHELLCHECK_OPTS_EXTRA=./.env",
            "SHELLCHECKOPTS=./.env",
            "GIT_EXTERNAL_DIFF",
            "MY_GIT_DIR=/tmp",
            "PAGER=cat",
            "GIT_PAGER=cat",
            "FOO=bar",
            "shellcheck",
            "contrib/aib",
            "-x",
        ):
            with self.subTest(word=word):
                self.assertIsNone(gate.assigned_environment(word))

    def test_every_refused_variable_says_what_it_reaches(self) -> None:
        # The table is the corpus: a variable is one row, and the clause beside
        # it is what the refusal is built from. A row with no clause would
        # refuse with a sentence the model cannot act on.
        for pattern, reach in gate.REFUSED_ENVIRONMENT:
            with self.subTest(pattern=pattern):
                self.assertTrue(pattern.strip(), "a row with no name")
                self.assertGreater(len(reach), 20, f"{pattern} says nothing about its reach")
        names = [pattern for pattern, _ in gate.REFUSED_ENVIRONMENT]
        self.assertEqual(len(names), len(set(names)), "a variable is listed twice")

    def test_every_refused_variable_is_refused_in_every_spelling(self) -> None:
        # What makes dropping a row from the table fail: each name is held in
        # the three positions the corpus is about -- on the command, behind a
        # wrapper, and in a command of its own -- so a row removed here is a
        # failure rather than a silently reopened hole. A `*` in the pattern
        # stands for the rest of a numbered family.
        for pattern, _ in gate.REFUSED_ENVIRONMENT:
            name = pattern.replace("*", "_0")
            for command in (
                f"{name}=x git diff HEAD",
                f"{name}+=x shellcheck contrib/aib",
                f"env {name}=x podman images",
                f"export {name}=x; hadolint Containerfile",
                f"declare -x {name}=x",
            ):
                with self.subTest(command=command):
                    self.assertIsNotNone(
                        gate.refusal(command),
                        f"{pattern} is in REFUSED_ENVIRONMENT and {command!r} is let through",
                    )

    def test_the_shellcheck_environment_refusal_names_the_variable(self) -> None:
        # The model retries a refusal it cannot act on rather than reporting
        # it, and the generic environment refusal for a gated command names
        # only the variable it found; this one has to say what the linter does
        # with that variable, or the retry is "the same thing behind `env`".
        reason = gate.refusal("env SHELLCHECK_OPTS=./.env shellcheck contrib/aib")
        self.assertIsNotNone(reason)
        self.assertIn("SHELLCHECK_OPTS", reason or "")
        self.assertIn("prepends", reason or "")

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
