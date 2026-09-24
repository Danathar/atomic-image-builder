# Branch protection

`main` is what `publish-image.yml` builds, signs and pushes to GHCR as
`:latest` on every push. The `aib` wrapper checks that signature and then runs
that image with the user's GitHub login forwarded into it. This file says what
is meant to protect `main`, why each rule is there, and how to check what
GitHub is really enforcing.

## Status

The ruleset in
[`.github/rulesets/main.json`](../.github/rulesets/main.json) has been active
on `main` since 2026-09-24, as ruleset `23966555`. Every change reaches `main`
through a pull request that passed `test`, and nothing can bypass that. Check
it yourself; neither call needs admin rights:

```bash
gh api repos/Danathar/atomic-image-builder/branches/main --jq .protected
gh api repos/Danathar/atomic-image-builder/rulesets
```

The first prints `true`. The second lists `protect main` with id `23966555`
and enforcement `active`.

## How a release reaches main

The ruleset has no bypass, so it refuses every direct push to `main`. The one
workflow that used to push there,
[`update-homebrew-formula.yml`](../.github/workflows/update-homebrew-formula.yml),
no longer does. On each published release it points the formula at the new
tag, checks it with `homebrew_formula.py --check`, commits it on a
`formula/<tag>` branch, and pushes that branch with its own `GITHUB_TOKEN`.
The ruleset covers only `main`, so that push is allowed. The workflow needs no
secrets and no GitHub App.

It does not open the pull request itself. Actions is not allowed to open pull
requests in this repository, and GitHub starts no workflow runs for a pull
request opened with `GITHUB_TOKEN` anyway, so `test` would never report and
the pull request could never merge. So a person opens it, which takes one
click:

1. The run's summary, and a reminder issue titled "Open the Homebrew formula
   PR for <tag>", both carry a link of this form:

   ```text
   https://github.com/Danathar/atomic-image-builder/compare/main...formula/<tag>?expand=1&title=Point%20the%20Homebrew%20formula%20at%20<tag>
   ```

2. Open the link and click **Create pull request**. The title is filled in.
   `test` runs because a person opened it.
3. Merge it once `test` passes, then close the reminder issue.

A re-run for the same tag reuses the open reminder issue instead of filing a
second one. Only an issue the workflow opened itself counts as the reminder.
If the branch already holds the same formula, the re-run leaves it alone, so
a pull request already opened from it keeps the commit `test` ran on. If the
formula differs, the re-run replaces the branch and says so in the summary and
on the issue. A push with `GITHUB_TOKEN` starts no CI, so if that pull request
is already open, close and reopen it to run `test` on the new commit.

A run also deletes the `formula/<tag>` branches of older releases and closes
their reminder issues. `--check` passes only for the tool's `VERSION` on
`main`, so the run is for the newest release, and merging an older formula
branch would point Homebrew back at the previous one. Deleting a branch closes
any pull request opened from it.

## The ruleset

[`.github/rulesets/main.json`](../.github/rulesets/main.json) is the agreed
definition. It is in GitHub's import format, so it applies as-is. What each rule
does:

- **Targets `~DEFAULT_BRANCH`**, so it follows a rename of `main`. The
  `coverage-data` branch that `ci.yml`'s `publish-coverage` job pushes to is
  not covered, and does not need to be.
- **No bypass actors.** A bypass for Actions or for an App hands back the direct
  push this exists to stop.
- **`deletion` and `non_fast_forward`** stop `main` being deleted or rewritten.
- **`pull_request` with 0 approvals.** GitHub does not let anyone approve their
  own pull request. On a single-maintainer repository, requiring one approval
  means nothing can ever merge, including the change that relaxes the rule.
  What 0 still enforces is that every change arrives as a pull request, and the
  required check below makes it one that passed `test`. It does not make the
  merger a person: a token that can write contents could merge a green pull
  request through the API.
- **`required_status_checks` with one check, `test`.** It is the job in
  `ci.yml` that runs the unit suite and the coverage gate. `ci.yml` runs on
  every pull request with no path filter, and the job has no `if:`, no `name:`
  and no matrix, so no pull request waits for a check that never starts.
  `Build container image` is not required, because it only builds when the
  image's files change.
  `Publish coverage badge and trend` runs on pushes to `main` only.
  `integration_id` 15368 is GitHub Actions. `tests/test_branch_ruleset.py`
  fails if the job is renamed, skipped, or gains a filter.

Nothing pushes to `main` outside a pull request, and
`tests/test_branch_ruleset.py` fails if a workflow starts to. The formula
workflow used to push there directly; its last three pushes were the formula
updates for v0.9.1, v0.9.5 and v0.10.0. Every other first-parent
commit on `main` after 2026-06-11 is a pull request merge. There is no
Renovate or Dependabot configuration for this repository (the ones under
`template_snapshots/` are shipped to generated repositories).
`publish-wrapper.yml` attaches release assets and pushes no commits.

## Applying it

A pull request cannot change repository settings, so a repository admin
applies the ruleset. It was created once with

```bash
gh api --method POST repos/Danathar/atomic-image-builder/rulesets \
  --input .github/rulesets/main.json
```

That call returns the id. The first time, it was run with `enforcement` set
to `disabled`, only to get the id into this page before the pull request that
added it merged; the `PUT` below then turned it on from the merged file.

To change the ruleset later, edit the file through a pull request, and once
it merges, update the live ruleset from `main`:

```bash
gh api --method PUT repos/Danathar/atomic-image-builder/rulesets/23966555 \
  --input .github/rulesets/main.json
```

## When there is a second reviewer

Set `required_approving_review_count` to 1. Consider
`require_last_push_approval`, so a push after approval needs a fresh one.
