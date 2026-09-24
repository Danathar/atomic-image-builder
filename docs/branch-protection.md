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

The ruleset has no bypass, so once it is applied it refuses every direct push
to `main`. The one workflow that used to push there,
[`update-homebrew-formula.yml`](../.github/workflows/update-homebrew-formula.yml),
no longer does. On each published release it commits the formula change on a
`formula/<tag>` branch, pushes that branch, and opens a pull request against
`main`.

It does that with a token for a GitHub App, not with `GITHUB_TOKEN`. Actions
is not allowed to open pull requests in this repository, and GitHub starts no
workflow runs for a pull request opened with `GITHUB_TOKEN` anyway, so the
required `test` check would never report and the pull request would wait
forever. A pull request opened by an App does start CI. Danathar chose this
over the two other options: a person opening the pull request by hand, which
makes the formula a manual step again, or a bypass for GitHub Actions, which
hands the direct push back to every workflow with `contents: write`.

The App does not exist yet. Until it does and its two secrets are set, the
workflow fails on every release with an error naming the secrets. That is on
purpose: a formula left on the previous release keeps installing the old
version, so it has to show up red rather than be skipped.

What is left, in order:

1. **Create the App.** On GitHub: Settings → Developer settings → GitHub Apps
   → New GitHub App. Any name works, for example `aib-formula`. Use the
   repository URL as the homepage. Untick **Webhook → Active**. Under
   **Repository permissions** set **Contents** to *Read and write* and **Pull
   requests** to *Read and write*. Leave every other permission at *No
   access*; GitHub adds *Metadata: Read-only* itself. Under **Where can this
   GitHub App be installed?** pick *Only on this account*.
2. **Generate a private key** on the App's page. A `.pem` file downloads.
3. **Install the App on this repository only.** On the App's page: Install
   App → your account → *Only select repositories* →
   `atomic-image-builder`.
4. **Set the two repository secrets.** The App ID is on the App's General
   page. (Its Client ID works in the same place.) Then delete the downloaded
   key file.

   ```bash
   gh secret set FORMULA_APP_ID --repo Danathar/atomic-image-builder --body "<App ID>"
   gh secret set FORMULA_APP_PRIVATE_KEY --repo Danathar/atomic-image-builder < ~/Downloads/<key file>.pem
   ```

5. **Check the token works.** Run the workflow for the tag the formula already
   points at, which is the latest release:

   ```bash
   gh workflow run update-homebrew-formula.yml -f tag=v0.10.0
   ```

   The run should be green, with **Mint the formula App token** passing, and
   its summary should say the formula is already pointed at that tag. That
   proves the App ID, the key, the installation and both permissions. It opens
   no pull request, because nothing changed. An older tag cannot be used to
   rehearse the pull request: `homebrew_formula.py --check` refuses a formula
   whose tag is not the tool's `VERSION`, so that run fails before it pushes
   anything.
6. **Wait for the next release, and check its pull request.** Publishing a
   release runs the workflow for real. Expect a pull request titled "Point the
   Homebrew formula at <tag>", opened by the App (`<app name>[bot]`), with
   `test` running on it. Once `test` passes, merge it. Step 5 only proves the
   credentials; this is the first run that shows a pull request opened by the
   App actually gets `test`. If the ruleset were applied first and `test`
   never started, that pull request could not merge.
7. **Apply the ruleset**, as in [Applying it](#applying-it) below.

The workflow also asks GitHub to merge the pull request by itself once `test`
passes. That needs **Settings → General → Allow auto-merge**, which is off.
While it is off, the pull request stays open until someone merges it, and the
run summary says so; the job does not fail. Turning it on is optional. It only
works once the ruleset is applied, because GitHub will not turn on auto-merge
for a pull request that nothing is holding back.

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

A pull request cannot change repository settings. Once a release's formula
pull request has been opened by the App and passed `test` (step 6 above), a
repository admin applies the ruleset once:

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
