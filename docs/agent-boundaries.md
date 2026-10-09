# Agent boundaries

Which limits on an AI agent working here are enforced by a tool, and which
are only asked of it.

Most of what this repository tells an agent is a request: get consent before
a push, keep a change to one thing, bring the evidence its risk tier needs. A
structural gate is different. It is a setting or a check that refuses the
action whatever the agent decides, so a mistaken or misled agent is stopped
rather than trusted. This page lists the gates, says what each one stops, and
links the document that explains it. It restates none of them: the linked
document is the reference, and most of those documents are checked against
the files they describe by a test of their own.

Start from [`.github/copilot-instructions.md`](../.github/copilot-instructions.md)
for how the repo works. This page is a map of the limits, not a brief.

## The gates

| Gate                                                                                          | What it stops                                                                                                                                                                                                                                                                                                              | Holds for        | Explained in                                                                                                            |
| --------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------- |
| [`.github/rulesets/main.json`](../.github/rulesets/main.json)                                 | Any change reaching `main` except as a pull request; deleting or rewriting `main`. It has no bypass, for Actions or for anyone else.                                                                                                                                                                                       | every agent      | [branch-protection.md](branch-protection.md#the-ruleset)                                                                |
| The required `test` check in [`ci.yml`](../.github/workflows/ci.yml)                          | Merging a pull request whose unit suite, coverage floor, linters or shell harnesses fail. Many tests read the documents, so a doc that drifts from the files it describes fails here too.                                                                                                                                  | every agent      | [CONTRIBUTING.md](../CONTRIBUTING.md#submitting-a-change), [quality.md](quality.md#how-quality-is-actually-enforced)    |
| [`.claude/settings.json`](../.claude/settings.json)                                           | Reading a signing key, `.env`, a PEM file or an SSH private key; force-push, hard reset, broad Podman or Buildah cleanup, repository deletion, host rebase. It asks before anything outward-facing.                                                                                                                        | Claude Code only | [SECURITY-AI.md](SECURITY-AI.md#what-is-enforced-rather-than-trusted)                                                   |
| [`.claude/hooks/gate_git_diff.py`](../.claude/hooks/gate_git_diff.py)                         | An allowed command, such as `git diff` or `shellcheck`, being spelled so it reads or writes a file the deny rules protect.                                                                                                                                                                                                 | Claude Code only | [SECURITY-AI.md](SECURITY-AI.md#what-is-enforced-rather-than-trusted), [gate-refusal-corpus.md](gate-refusal-corpus.md) |
| [`.github/policies/workflow-permissions.json`](../.github/policies/workflow-permissions.json) | A workflow asking for a broader `GITHUB_TOKEN` than this file grants it. Widening one takes an edit to both files, side by side in the diff.                                                                                                                                                                               | every agent      | [risk-tiers.md](risk-tiers.md#tier-4--credentials-and-the-release-path)                                                 |
| [`agent-audit.yml`](../.github/workflows/agent-audit.yml)                                     | A merged agent pull request with a commit lacking a `Signed-off-by` trailer going unnoticed. It runs monthly, after the merge, and fails red. It sees only pull requests the Hive app opened or whose body carries the `— hive:` line, so one pushed under the maintainer's identity without that line is invisible to it. | every agent      | [multi-agent.md](multi-agent.md#who-merges)                                                                             |

"Claude Code only" matters. The settings file and its hook are Claude Code's
format, and an agent on another backend does not read them. The gates that
hold for every backend are the ones on GitHub's side.

## What is asked, not enforced

These are real rules, and nothing mechanical stops an agent breaking them.
Review is what catches it.

- **Consent before a gated action.** [`AGENTS.md`](../AGENTS.md#consent-standard)
  lists eight, each its own gate: creating or switching a branch, commit,
  push, opening or editing a pull request, replying to or resolving a review
  thread, re-running, cancelling or dispatching a workflow, merge, and
  cleanup.
- **Signing an agent pull request.** The `— hive:` line is what lets the
  monthly audit find a pull request pushed under the maintainer's identity.
  Nothing makes an agent write it, and the audit cannot see the one that
  leaves it out.
- **Who merges.** The ruleset needs no approval, because a sole
  maintainer cannot approve their own pull request. So a token that can write
  contents could merge a green pull request through the API. The `hold` label
  and the merge policy in [`multi-agent.md`](multi-agent.md#who-merges) are
  what let a person stop a merge.
- **Evidence in proportion to reach.** [`risk-tiers.md`](risk-tiers.md) and
  [`.claude/risk-config.json`](../.claude/risk-config.json) classify every
  path. They say how much proof a change needs; they do not block one that
  brings less.
- **Saying which files a pull request owns.** The `Claims` line in
  [`multi-agent.md`](multi-agent.md#staying-out-of-each-others-way) is a habit.

## Why there is no CODEOWNERS file

A `CODEOWNERS` file is the usual way to put a gate on part of a repository,
and here it would gate nothing. The ruleset sets
`require_code_owner_review` to `false` and needs no approval, for the reason
above. A `CODEOWNERS` file would only ask the one maintainer to review every
pull request, which is already the policy. When there is a second reviewer,
[branch-protection.md](branch-protection.md#when-there-is-a-second-reviewer)
is where that changes, and code-owner review belongs in the same change.
