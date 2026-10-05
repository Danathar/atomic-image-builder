# Multi-agent work

Several AI agents work on this repository at the same time, each with one job:
one writes tests, one looks for bugs, one looks for security problems, one
fixes the issues the others file, and so on. They are run and scheduled by
[Hive](https://github.com/hivecommons/hive), outside this repository. This page
is the repository's side of that arrangement: who the agents are, how work
reaches each one, what keeps two of them from undoing each other, and who
decides what lands.

Nothing here tells an agent how the code works. That is
[`.github/copilot-instructions.md`](../.github/copilot-instructions.md), and
it is where every agent is sent first. Like [`.claude/checkpoint.md`](../.claude/checkpoint.md),
this page holds no current-state counts: anything that changes on its own is a
command under [What is in flight right now](#what-is-in-flight-right-now).

## Who works here

Each agent signs the pull requests it writes with a `— hive:` line that names
its role, and names its branches `<role>/<slug>`.
[`docs/agent-tasks/`](agent-tasks/README.md) explains both marks and how to
read them back.

| Role        | Signature           | Branch      | What it does                                                                                                |
| ----------- | ------------------- | ----------- | ----------------------------------------------------------------------------------------------------------- |
| quality     | `agent=quality`     | `quality/`  | Finds behaviour no test pins, files it, and adds the test.                                                  |
| scanner     | `agent=scanner`     | `scanner/`  | Finds bugs, files them, and fixes some.                                                                     |
| security    | `agent=sec-check`   | `sec/`      | Finds security problems, files them, and fixes some.                                                        |
| architect   | `agent=architect`   | `arch/`     | Writes RFCs and opens structural refactors.                                                                 |
| strategist  | `agent=strategist`  | `strategy/` | Keeps `ROADMAP.md` and coordinates work across the other agents.                                            |
| guide       | `agent=guide`       | `guide/`    | Documents behaviour that already exists but is not written down.                                            |
| contributor | no `agent=` field   | `fix/`      | Works one issue end to end. Also branches as `docs/`, `feat/`, `refresh/` or `secfix/` to match the change. |
| reviewer    | none, it opens none | none        | Reviews open pull requests and backs each finding with a file and line. Never merges or approves.           |

An issue an agent files carries an `agent/<role>` label, so the issue list
answers "who found this" the same way the branch answers "who changed this".

## How work reaches an agent

There is no dispatcher in this repository. Hive decides which agent runs when
and on what; the repository only shapes what an agent finds when it gets
there.

1. An issue is opened: by a person, by one of the finding agents above, or by
   Hive's maturity evaluation (the `[ACMM ...]` issues).
2. [`.github/workflows/triage.yml`](../.github/workflows/triage.yml) labels it
   on `opened`, by level for an ACMM issue and `security` for one that names
   signing, a credential or prompt injection. It only adds labels, so a label
   a person set always wins.
3. When an issue gets the `ai-fix-requested` label,
   [`.github/workflows/ai-fix.yml`](../.github/workflows/ai-fix.yml) runs the
   repository's gate on `main` and posts the result on the issue. The agent
   that picks it up starts from that evidence instead of a cold checkout.
4. The issue is worked on a branch of its own and arrives as one pull request.

Three issue labels tell an agent to leave an issue alone:

- `hive/covered-by-pr`: an open pull request already names this issue.
- `hive/likely-done`: a merged pull request names it, pending a person's check.
- `needs-decision`: it waits on a maintainer's choice, and is not contributor
  work until a person removes the label.

## Staying out of each other's way

Two agents can be working at once on files that one test joins together.
Three habits keep that from going wrong.

**Check what is already open before starting.** An issue with an open pull
request is taken. A file that an open pull request changes is contested, and
the second change should start from the first or wait for it:

```bash
gh pr list --repo Danathar/atomic-image-builder --state open --search "<issue> in:body"
gh pr list --repo Danathar/atomic-image-builder --state open --json number,headRefName,files \
  --jq '.[] | select(any(.files[]; .path == "<path>")) | "#\(.number) \(.headRefName)"'
```

**Say which files the pull request owns.** A pull request body that opens with
a `Claims` line naming the files and symbols it changes lets the next agent
see the overlap without reading the diff. Several roles already do this.
Nothing enforces it.

**Expect two green pull requests to be red together.** The `main` ruleset
requires `test` to pass, but `strict_required_status_checks_policy` is `false`
in [`.github/rulesets/main.json`](../.github/rulesets/main.json): a pull
request does not have to be tested against the latest `main` before it merges.
On 2026-10-03, #632 added a path to [`docs/risk-tiers.md`](risk-tiers.md) and
#636 added [`.claude/risk-config.json`](../.claude/risk-config.json) with the
old path list. Each passed on its own; together they turned `test` red on
`main` until #637. When two open pull requests touch the same document and the
test that reads it, update the second from `main` before it merges so `test`
runs on the pair.

## Who merges

Merging is a maintainer's decision. The README's
[*Maintained with Hive*](../README.md#maintained-with-hive-acmm-l5) section
states the policy: nothing an agent opens merges on its own, and a maintainer
reviews agent pull requests in batches and merges the ones that should land.
An agent never merges its own pull request, and the reviewer never merges,
approves or closes one.

The `hold` label is how that shows on a pull request: one that carries it does
not merge until a person removes it. The ruleset needs no approval and lets
nobody bypass `test`, so a red pull request cannot land whoever merges it.

The record each merged
agent pull request leaves is read back every month by
[`.github/workflows/agent-audit.yml`](../.github/workflows/agent-audit.yml),
which fails when a signature line or a Signed-off-by trailer is missing.

## What every agent shares

Every agent, whatever model it runs on, works from the same documents:

- [`.github/copilot-instructions.md`](../.github/copilot-instructions.md): the
  checks and the traps.
- [`AGENTS.md`](../AGENTS.md): what an agent may do without asking.
- [`docs/risk-tiers.md`](risk-tiers.md): how much evidence a change to each
  path needs.
- [`docs/SECURITY-AI.md`](SECURITY-AI.md): what an agent must never do, and
  which of those rules a tool enforces.

The agents do not all run on the same backend: the `backend=` field of the
signature line names it. [`.claude/settings.json`](../.claude/settings.json)
and its hook are Claude Code's format, so they bind a Claude Code session and
not a backend that does not read them. The layers that hold for every backend
are on GitHub's side: the ruleset, the `test` check, a person's merge, and the
monthly audit.

## What this repository does not run

No workflow here starts an agent, runs a model or opens a pull request.
[`.github/workflows/ai-fix.yml`](../.github/workflows/ai-fix.yml) explains why
in its header: Actions cannot open pull requests in this repository, automated
write access to a repository that signs images is not a default anyone should
get from a workflow file, and an AI action would have to be added to
`ACTION_PINS`, which ships to every repository the tool generates. #442
declined a model-running workflow rather than reverse that decision. The orchestration
stays in Hive, and this repository keeps its side of it in issues, labels,
branches and the checks above.

## What is in flight right now

```bash
gh pr list --repo Danathar/atomic-image-builder --state open --json number,headRefName,labels \
  --jq '.[] | "#\(.number) \(.headRefName) \([.labels[].name] | join(","))"'
gh issue list --repo Danathar/atomic-image-builder --state open --label hive/covered-by-pr
gh issue list --repo Danathar/atomic-image-builder --state open --label needs-decision
git log -20 --format='%h %(trailers:key=Hive-Run,valueonly)' | awk 'NF == 2'
```

The first line lists open pull requests by branch, so the prefix says which
agent owns each. The last reads the `Hive-Run:` trailer, which names the issue
each recent commit was made for.
