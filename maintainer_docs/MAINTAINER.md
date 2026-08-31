# Maintainer Guide

What to do, and what to watch out for, when maintaining Atomic Image Builder.
End-user docs are in [README.md](../README.md); development workflows are in
[CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Cutting a release

Four steps, all of them yours. Everything after the tag is automated.

**1. Branch from a current `main`.**

```bash
git fetch origin
git switch -c release/v0.9.1 --no-track origin/main
```

Naming `origin/main` as the base means it does not matter what you had checked
out or how far behind your local `main` was. That matters here: merging on
GitHub leaves the local copy behind, and branching from a stale one puts
unrelated reversions in the pull request.

`--no-track` stops the release branch adopting `main` as its upstream, which
would make a stray `git pull` merge `main` into it.

**2. Bump the version.** One line, in `atomic_image_builder.py` (line 37):

```python
VERSION = "0.9.1"
```

That is the only file to edit. Everything else derives from it:

| Consumer | How |
|---|---|
| `aib-tool --version` and the TUI banner | reads the constant |
| Published image's version tag | `publish-image.yml` runs `--version` and takes field 2 |
| Homebrew formula | `homebrew_formula.py` imports `VERSION` and compares it to the formula's tag |
| `tool_version` in each managed repo's state file | written on create and update |

**3. Commit, push, and merge it.**

```bash
git commit -am "Bump VERSION to 0.9.1"
git push -u origin release/v0.9.1
gh pr create --fill
```

Merge once CI is green, then bring your local `main` up to date again:

```bash
git switch main && git pull
```

`main` is not protected, so a direct push works too — but everything else here
goes through a pull request, and CI on the branch is the only thing that checks
the bump did not break anything.

**4. Tag and publish.**

```bash
gh release create v0.9.1 --target main --title v0.9.1 --notes "..."
```

The tag carries a `v`; `VERSION` does not. `--target main` matters: the tag has
to point at a commit that already carries the new `VERSION`, which is why the
merge comes first.

**Then watch the automation.**

```bash
gh run watch "$(gh run list --workflow update-homebrew-formula.yml --limit 1 \
  --json databaseId --jq '.[0].databaseId')"
```

It checks out `main`, points the formula at the release, verifies the digest,
and pushes. To confirm afterwards, switch back to `main` first — the bot's
commit landed there, not on your release branch:

```bash
git switch main && git pull
python3 homebrew_formula.py --check
```

It should print `Homebrew formula pin is current.`

### Order matters

The formula records a release tarball **and its sha256**, and a sha256 is a
fact about a published tag — it cannot be recorded before the tag exists. That
is why the formula update comes last, after the release, rather than riding
along with the version bump.

Tagging without bumping first is caught, not silently shipped: the workflow's
`--check` compares the formula's tag against `VERSION` and fails. Bump, merge,
then re-dispatch.

### How Homebrew users actually get the new version

In the normal case they just run `brew update && brew upgrade` and it works,
because the automation has already updated the formula. Nothing else is needed.

What is worth understanding is *why*, because it explains the one failure mode.
**Homebrew only ever reads the formula file. It has no knowledge of your
releases at all.** `brew update` git-pulls the tap; `brew upgrade` compares
what is installed against the version parsed out of the formula's `url`:

```
url              .../archive/refs/tags/v0.9.0.tar.gz
formula version  0.9.0
installed        0.9.0
outdated         False
```

Nothing in that chain polls GitHub for releases. So if a release is tagged and
the formula is never updated, brew reports `outdated: False` **indefinitely** —
users do not catch up later, because there is nothing to catch up to. That is
why the formula update is automated, and why the weekly audit re-checks it
independently rather than trusting the automation ran.

---

## Distribution channels

Three, with deliberately different freshness contracts. When someone reports a
bug, ask which one they used.

| Channel | Command | Tracks | Updates when |
|---|---|---|---|
| Container wrapper | `aib` | `main` | Every run — `--pull=newer` |
| Homebrew | `aib-tool` | Tagged releases | `brew upgrade` |
| Source checkout | `./atomic_image_builder.py` | Whatever is checked out | `git pull` |

The container is always the freshest. Homebrew is deliberately behind, moving
only when you cut a release.

---

## What runs automatically

| Workflow | Fires on | Does |
|---|---|---|
| `ci.yml` | push, PR, dispatch | Tests, coverage gate at 90%, ruff; builds the image and collects e2e coverage when image files change |
| `publish-image.yml` | push to `main`, release, dispatch | Builds and pushes to GHCR |
| `update-homebrew-formula.yml` | release published, dispatch | Points the formula at the release and pushes to `main` |
| `maintenance-audit.yml` | Mondays 06:00 UTC, dispatch | Snapshot drift, action pin coverage, pin freshness, formula pin |

Only a build of `main` tags the image `latest` — a release published from an
older commit must not drag `latest` backwards. All events that write image
tags share one concurrency group so they cannot race.

Expect a release to produce **two** image publishes: one from the release, one
from the formula-update push to `main`. Same content, harmless.

---

## Reading the weekly audit

It distinguishes **failures** from **advisories**, and the difference matters.

**Failures** (exit 1) mean the repo is internally inconsistent: a snapshot
action not covered by the pin tables, a SHA that disagrees with them, a
missing or malformed `.template-source`. The repo is contradicting itself and
it is fixable here. Fix these.

**Advisories** do not fail the run. They mean something *outside* the repo
moved — an action pin's tag or branch, or a bundled template snapshot's
upstream. Nothing is wrong at the moment one appears, and it reappears every
time upstream commits, so failing on it would leave the audit permanently red
and train everyone to ignore it — which costs the failures above their only
channel.

Template snapshot drift used to be a failure and is now an advisory (#129).
It had taken the weekly job red on 5 of its last 6 scheduled runs, every one
of them for that reason alone, with a manual refresh pushed hours later. A job
that is red every Monday cannot tell you about the Monday something is
actually wrong.

### Advisory: the bundled template snapshot trails upstream

Both template upstreams are active, so this is normal and says how far behind
you are:

```
Bundled template snapshot trails upstream HEAD: pinned b9783f6a1e2d,
upstream d95b282bd679, 47 commit(s) newer. Refresh the snapshot and review
pin updates.
```

Read the count, not just the presence. A handful of commits is an ordinary
week. Dozens means nobody has refreshed in a while, and repos generated from
it ship that much stale template. Refresh on your own schedule rather than
the cron's.

It reports direction, like the action-pin advisory does, because an upstream
branch can be force-pushed backwards — `N commit(s) OLDER than the snapshot`
means refreshing would roll it back, so look first.

### Advisory: a pin no longer matches its tag or branch

Read the **direction** before acting. The message states it:

- *"N commit(s) newer … refresh it"* — upstream moved ahead. Refresh.
- *"N commit(s) OLDER than the pin … would downgrade"* — upstream moved the
  tag **backwards** onto an older commit. **Do not refresh.** This is real:
  `ublue-os/remove-unwanted-software` is pinned 26 commits ahead of what its
  `v8` tag now points at.
- *"diverged history"* or *"direction unknown"* — look before touching.

### Advisory: the formula pin

`Formula points at v0.9.0 but the tool's VERSION is 0.9.1` means a release was
cut and the formula never followed — so the automation did not run, or ran and
failed. Homebrew users are stuck on the older version until this is fixed; they
will not pick it up on their own. Fix with:

```bash
gh workflow run update-homebrew-formula.yml -f tag=v0.9.1
```

---

## Refreshing action pins

Generated repos ship `.github/dependabot.yml`, so a stale pin here becomes a
Dependabot PR in someone's brand-new repo within a minute of creating it.

Verify the direction of travel **before** changing anything:

```bash
gh api "repos/<owner>/<repo>/compare/<pinned-sha>...<tag-or-branch>" \
  --jq '"status=\(.status) ahead=\(.ahead_by) behind=\(.behind_by)"'
```

`status=ahead` means the upstream ref is ahead of the pin and refreshing moves
forward. `status=behind` means refreshing is a **downgrade**.

To refresh, update the SHA in **both** places or the audit fails:

1. `ACTION_PINS` / `ACTION_REF_PINS` in `atomic_image_builder.py`
2. The workflow file under `template_snapshots/`

Leave an `ACTION_REF_PINS` entry for the old SHA pointing at the new pin, so
`pin_action_uses_line()` rewrites repos already carrying it.

---

## Traps

**Never rename `TOOL_SLUG`.** It feeds `STATE_FILE` — `.atomic-image-builder.json`
— which is written into every managed repo and is how the tool recognises repos
it created. Renaming it orphans every repo the tool has ever made. The command
name is `TOOL_COMMAND`, a separate constant, and is safe to change.

**`aib` vs `aib-tool` is deliberate.** `contrib/aib` installs `aib` into
`~/.local/bin`, which normally precedes Homebrew's `bin` on `PATH`. If both
used the same name, whichever came first would silently win.

**A generated repo failing at its final push with `denied: permission_denied:
write_package`** is almost always a leftover GHCR package from an earlier repo
of the same name. Deleting a repo does not delete its packages, and the orphan
keeps the old repo's Actions access list. The tool now warns before creating
such a repo. Fix by deleting the package, or granting the new repo Write under
*Manage Actions access* — the latter only works once the repo exists.

**Container users get no `dnf5` metadata by default.** The image ends in
`dnf5 clean all`, so the first package search offers to download it. The `aib`
wrapper keeps that in a named volume; a bare `podman run --rm` repeats it.

---

## Repo settings worth knowing

- **`main` is not protected.** No required reviews, no required checks, direct
  pushes allowed. The PR-per-change habit is convention, not enforcement —
  which matters, because anything landing on `main` immediately becomes the
  published image.
- **Actions cannot create pull requests** in this repo, which is why the
  formula update pushes directly instead of opening one.
- **Default workflow token permissions are read-only.** Workflows needing more
  declare it explicitly, as `publish-image.yml` and
  `update-homebrew-formula.yml` do.

---

## Before pushing

```bash
python3 -m unittest discover -s tests
python3 -m coverage run -m unittest discover -s tests && python3 -m coverage report --fail-under=90
ruff check
python3 maintenance_audit.py --skip-upstream
```

Do **not** run `brew audit` against your real Homebrew prefix. It bootstraps
Homebrew's developer gem bundle, and on current versions the vendored `json`
gem conflicts with the built-in one in portable-ruby, breaking `brew info` and
`brew install` until the gem is removed. `ruby -c Formula/atomic-image-builder.rb`
checks syntax without side effects.

### What is deliberately not gated

- **End-to-end coverage** sits around 13% and is not gated. The wizard needs a
  TTY, so it stays low by construction. It exists as evidence for classifying
  gaps, not as a threshold.
- **Pin advisories** never fail the build, for the reason above.
