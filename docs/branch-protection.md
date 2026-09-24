# Branch protection

`main` is what `publish-image.yml` builds, signs and pushes to GHCR as
`:latest` on every push. The `aib` wrapper checks that signature and then runs
that image with the user's GitHub login forwarded into it. This file says what
is meant to protect `main`, why each rule is there, and how to check what
GitHub is really enforcing.

## Status

**The ruleset is not applied.** It is committed in
[`.github/rulesets/main.json`](../.github/rulesets/main.json), but it is not
active on the repository, and `main` has no branch protection. Any token with
`contents: write` can push to `main` directly. Check it yourself; neither call
needs admin rights:

```bash
gh api repos/Danathar/atomic-image-builder/branches/main --jq .protected
gh api repos/Danathar/atomic-image-builder/rulesets
```

Today they print `false` and `[]`. Once the ruleset is applied, the first
prints `true` and the second lists `protect main`.

## Why it is not applied yet

[`update-homebrew-formula.yml`](../.github/workflows/update-homebrew-formula.yml)
pushes to `main` on every published release (`git push origin HEAD:main`). It
pushes directly because Actions cannot open pull requests in this repository.
The last three such pushes were the formula updates for v0.9.1, v0.9.5 and
v0.10.0.

The ruleset has no bypass actors. With it active, that push is refused, the
job fails, and the formula stays on the previous release. Homebrew only reads
the formula, so `brew upgrade` keeps installing the old version until someone
notices. [MAINTAINER.md](../maintainer_docs/MAINTAINER.md) describes that
failure.

Letting Actions open the pull request instead would not be enough. GitHub
starts no workflow runs for a pull request opened with `GITHUB_TOKEN`, so the
required `test` check never reports and the pull request waits forever.

So the formula workflow has to change before the ruleset can be applied. The
choices, and what each costs:

- **Push a branch, and a person opens the pull request.** No new credential
  and no bypass. The formula update becomes a manual step again, which is what
  the workflow was added to remove.
- **Open the pull request with a GitHub App token.** A pull request opened by
  an App does start CI, so `test` reports and the pull request can merge. It
  needs a new App key stored as a secret, readable by the release job.
- **Add a bypass for GitHub Actions.** This hands the direct push back to every
  workflow that holds `contents: write`, including one a pull request edits.
  That is the push this ruleset exists to stop.

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

Apart from the formula workflow, nothing pushes to `main` outside a pull
request. Every other first-parent commit on `main` after 2026-06-11 is a pull
request merge. There is no Renovate or Dependabot configuration for this
repository (the ones under `template_snapshots/` are shipped to generated
repositories). `publish-wrapper.yml` attaches release assets and pushes no
commits.

## Applying it

A pull request cannot change repository settings. Once the formula workflow no
longer pushes to `main`, a repository admin applies the ruleset once:

```bash
gh api --method POST repos/Danathar/atomic-image-builder/rulesets \
  --input .github/rulesets/main.json
```

Then update the Status section above with the id that call returned. To
change the ruleset later, edit the file through a pull request, then update
the live ruleset from it with `--method PUT` on `rulesets/<id>`.

## When there is a second reviewer

Set `required_approving_review_count` to 1. Consider
`require_last_push_approval`, so a push after approval needs a fresh one.
