# Contributing

This document covers the development and maintenance workflows for working on Atomic Image Builder itself. End-user installation and usage live in [README.md](README.md). Release procedure, what the automation does, and the traps worth knowing are in [MAINTAINER.md](maintainer_docs/MAINTAINER.md).

## Submitting a change

This is a normal public GitHub repo — there's no separate CLA or contributor
setup:

1. Fork the repo and create a branch off `main`.
2. Make your change. There's no fixed branch-naming convention for
   contributions; `maintainer_docs/MAINTAINER.md`'s `release/vX.Y.Z` pattern
   is the maintainer's own release process, not something contributors need
   to follow.
3. Run the checks below locally — [Tests](#tests), [Coverage](#coverage), and
   [Linting](#linting) — before opening a pull request. CI runs the same
   checks (`unittest`, the 90% coverage gate, `ruff`, `shellcheck`, and
   `hadolint`) and has to pass before a PR can be reviewed.
4. Open the PR against `main` and describe what changed and why. Keep it
   scoped to one thing; a PR that mixes an unrelated cleanup with the actual
   fix is harder to review and to revert if something goes wrong.

`main` is not branch-protected, but everything except the maintainer's own
direct fixes goes through review here — see MAINTAINER.md's *Repo settings
worth knowing* if you're curious why.

## Tests

Install the pinned tooling once — this covers both the Coverage and Linting
sections below, and matches the exact versions `ci.yml` installs so a local
run agrees with CI instead of disagreeing in either direction:

```bash
pip install coverage==7.16.0 ruff==0.16.5
```

Run the test suite with `unittest`:

```bash
python3 -m unittest discover -s tests
```

## Coverage

There are five separate coverage measurements, and they are not interchangeable.

**Unit coverage** measures the source tree. `.coveragerc` holds the settings so a local run reports the same numbers CI does, and CI gates on it at 90%:

```bash
python3 -m coverage run -m unittest discover -s tests
python3 -m coverage report
```

**End-to-end coverage** measures what a real run of the *built container image* executes. `container/Containerfile.coverage` layers coverage.py onto the built image and swaps the `atomic-image-builder` launcher for a shim that runs the packaged script under it, so `container/entrypoint.sh` and the packaged path stay in the measured run. `.coveragerc.e2e` maps the in-image path (`/opt/atomic-image-builder`) back onto the checkout. To reproduce a CI run locally:

```bash
podman build -t aib-local -f Containerfile .
podman build -t aib-local-cov -f container/Containerfile.coverage --build-arg BASE_IMAGE=aib-local .
mkdir -p e2e-coverage/data
podman run --rm -e COVERAGE_FILE=/cov/.coverage.version   -v "$PWD/e2e-coverage/data:/cov:z" aib-local-cov --version
python3 -m coverage combine --rcfile=.coveragerc.e2e --keep e2e-coverage/data
python3 -m coverage report --rcfile=.coveragerc.e2e
```

End-to-end coverage is deliberately low and is **not** gated: the guided wizard needs a TTY, so the only end-to-end reachable paths are `--version`, `--help`, and the preflight failure. It exists so coverage gaps can be classified honestly rather than inferred from the unit run alone.

**Shell-entrypoint coverage** measures executable lines in `contrib/aib` and `container/entrypoint.sh`. The same behavioral suites used by CI run under Bashcov, which follows the child Bash processes they launch. `.simplecov` restricts the report to those two user-facing scripts so the test harnesses and temporary command stubs do not inflate the denominator. To reproduce it locally with the version used by CI:

```bash
gem install bashcov -v 4.0.0 --no-document
bashcov --root "$PWD" --command-name contrib -- tests/test_contrib_aib.sh
bashcov --root "$PWD" --command-name entrypoint -- tests/test_entrypoint.sh
```

The report is written to `shell-coverage/` as HTML and JSON. Warnings that a temporary stub was deleted before reporting are expected: the harnesses deliberately remove their fake `gh`, `podman`, `rpm-ostree`, and `atomic-image-builder` commands after every scenario, and `.simplecov` excludes them from the report anyway. This tier is advisory and has no minimum. Bashcov reports line coverage, not branch coverage, so condition outcomes remain proven by the behavioral assertions rather than a branch percentage.

A multi-line array literal also reads as partly uncovered even when the statement it belongs to ran: Bashcov attributes a hit only to the line a statement *starts* on, so the continuation lines of a multi-line `podman_args+=(...)` block read as unhit however thoroughly the statement is asserted on. `contrib/aib` had one such block, the rpm-ostree status mount, and it is now written as one push per line so the reported number matches what the tests actually prove — see [#118](https://github.com/Danathar/atomic-image-builder/issues/118). Keep new pushes single-line for the same reason, and if a partial percentage shows up here anyway, check whether the missing lines are continuation lines of an already-asserted statement before reading them as an untested path.

**Maintenance-audit coverage** measures what a real run of `maintenance_audit.py` and `homebrew_formula.py` executes. Both are unit-tested only against mocked network calls — `FakeResponse`, patched `urlopen` and `subprocess` — and the weekly `.github/workflows/maintenance-audit.yml` job is the only place either one runs against the live GitHub API and a real tarball download. `.coveragerc.maintenance-audit` holds the settings. To reproduce a run locally (`GH_TOKEN` needs to be set for the audit's API calls):

```bash
python3 -m coverage run --rcfile=.coveragerc.maintenance-audit maintenance_audit.py --check-action-updates
python3 -m coverage run --rcfile=.coveragerc.maintenance-audit -a homebrew_formula.py --check
python3 -m coverage report --rcfile=.coveragerc.maintenance-audit
```

Like the end-to-end measurement it is advisory and **not** gated. Its value is the inverse of the unit number: a line that only a real network error reaches — a rate limit, an unreachable host, an unexpected API response shape — stays missing here even on a clean pass, which the unit suite's mocks cannot tell you on their own. A path that is 100% covered by mocks and never touched by a live run is exactly what this exists to make visible.

It follows that **a low percentage here is the expected reading, not a defect, and is not a coverage gap to be closed.** Driving this number up would mean making a live run against GitHub's real API actually hit a rate limit, a DNS failure and a malformed response — manufacturing the failures the tier exists to observe, which would destroy its only signal. Error-handling paths are proven instead in the unit tier, against a real socket rather than a patched `urlopen`: `tests/_local_http_server.py` serves a fixed response on loopback, and `closed_port_url()` returns a port nothing is listening on, so `urllib.request`'s own connect, read and error-parsing code runs for real. See `test_github_api_json_real_rate_limit_response_from_local_server` and its neighbours.

So: read this artifact to learn *which* error paths a live run never touches, and check that each one is covered by a loopback test. Do not file the percentage itself as a finding — see [#123](https://github.com/Danathar/atomic-image-builder/issues/123), which did, and [#124](https://github.com/Danathar/atomic-image-builder/pull/124), which added the loopback tests that are the right answer to it.

**Homebrew-release coverage** measures what a real run of `homebrew_formula.py --update` executes when `.github/workflows/update-homebrew-formula.yml` points the formula at a published release. This is the narrowest and most easily misread of the four. Every test in `tests/test_homebrew_formula.py` that exercises `update()` patches `fetch_sha256`, so the unit suite reports this module at 100% without a single real tarball download ever having been measured; a release is the only place `--update` downloads one and rewrites the formula for real. It reuses `.coveragerc.maintenance-audit`, since that config already scopes exactly these maintainer scripts for the same "real run against live GitHub" purpose. To reproduce locally (`--update` rewrites `Formula/`, so do it on a throwaway branch):

```bash
python3 -m coverage run --rcfile=.coveragerc.maintenance-audit homebrew_formula.py --update <tag>
python3 -m coverage run --rcfile=.coveragerc.maintenance-audit -a homebrew_formula.py --check
python3 -m coverage report --rcfile=.coveragerc.maintenance-audit
```

Because that config also names `maintenance_audit`, which this job never runs, coverage prints a `module-not-imported` warning and then omits the absent module from the report. Both are expected.

Like the maintenance-audit measurement it is advisory and **not** gated, and for a sharper version of the same reason: it only runs when a release is published, so there is no push that could be blocked on it.

All five measurements are uploaded as workflow artifacts — `coverage-unit`, `coverage-shell`, and `coverage-e2e` from `.github/workflows/ci.yml`, `coverage-maintenance-audit` from the weekly audit, and `coverage-homebrew-release` from the release workflow. The Python artifacts contain `coverage.xml`, an HTML report, and raw coverage data files (one per e2e scenario); the shell artifact contains Bashcov's HTML, JSON, and result-set files. Download the two Python CI artifacts and merge them into a single report from any clone:

```bash
gh run download <run-id>
python3 -m coverage combine --rcfile=.coveragerc.e2e coverage-unit/data coverage-e2e/data
python3 -m coverage report --rcfile=.coveragerc.e2e
```

That works off the runner only because the raw data is path-portable, which took two things: `relative_files = True` in all three configs, so the unit data names files relative to the checkout rather than by the runner's workspace path, and the `[paths]` mapping in `.coveragerc.e2e`, which rewrites the in-image `/opt/atomic-image-builder` onto the checkout. Removing either one makes the artifacts combinable only in the workspace that produced them.

The maintenance-audit and homebrew-release data files live under `data/` for the same reason, so either can be folded into that combine as well — these jobs all run on different schedules, so pass whichever pair of downloads you actually want to compare. Every config sets `branch = True`; coverage refuses outright to merge branch data with statement data, so a config that dropped it would silently make its artifact uncombinable with the others.

A commit that changes nothing the image is built from will not produce a `coverage-e2e` artifact, since the `container-build` job is path-scoped. Run the CI workflow manually (`workflow_dispatch`) to force one.

Because artifacts expire after 30 days, `coverage_badge.py` publishes the unit number somewhere durable: given a percentage, date, and SHA, it writes a shields.io endpoint-badge payload and appends a trend row (date, SHA, percentage) to a CSV. After the unit gate passes on a push to `main`, the `publish-coverage` job commits those two files to the dedicated `coverage-data` branch. Pull requests cannot publish, and the separate job is the only part of CI granted `contents: write`. The README badge reads the JSON endpoint and links to the CSV history. See `coverage_badge.py --help` and `tests/test_coverage_badge.py` for the artifact format.

## Linting

Three linters, one per language CI ships. All three gate the `test` job.

```bash
ruff check                                    # Python -- pip install command is in Tests, above
shellcheck contrib/aib container/entrypoint.sh tests/test_contrib_aib.sh tests/test_entrypoint.sh
hadolint Containerfile container/Containerfile.coverage
```

`ruff` is pinned in the install command at the top of [Tests](#tests), which
keeps it in sync with the exact version `ci.yml` installs — an unpinned local
`ruff` can enable or disable different rules release to release and disagree
with CI in either direction. `shellcheck` comes from the runner image; there
is no local pin to match, only whatever version your system package manager
gives you. hadolint is a
static binary; CI pins v2.14.0 by sha256 rather than using
`hadolint/hadolint-action`, because `maintenance_audit.py` requires every
`uses:` in this repo's workflows to appear in `ACTION_PINS` — the table
shipped to generated repos — and a linter those repos never run does not
belong in it. To match CI locally, install the same release from
[hadolint/hadolint](https://github.com/hadolint/hadolint/releases/tag/v2.14.0).

One hadolint finding needed a real fix rather than a suppression: DL4006 on
the cosign checksum, which was `echo "<sha>  <file>" | sha256sum -c -`. Under
`/bin/sh` a pipeline reports only its last command's status. Note that the
usual remedy — a `SHELL` instruction setting `pipefail` — is a trap here:
buildah ignores `SHELL` under the OCI image format this image is built with
(it warns and carries on), so it silences the linter while changing nothing.
The checksum goes through a file instead, which removes the pipe. Keep it
that way; do not reintroduce a pipe in a `RUN` expecting pipefail to catch it.

## Maintainer Audit

This repo includes a small maintenance audit for the bundled template snapshot and workflow action pins.

Run the local-only checks without touching the network:

```bash
python3 maintenance_audit.py --skip-upstream
```

Run the full audit, including upstream HEAD drift checks against both bundled template snapshots (`ublue-os/image-template` and `blue-build/template`):

```bash
python3 maintenance_audit.py
```

Add the action-pin checks to get pin-refresh signals for the GitHub Actions written into generated repos:

```bash
python3 maintenance_audit.py --check-action-updates
```

That flag runs two complementary checks, because a SHA pin can go stale in two different ways:

- **Trailing tags** compares each pin's label against the newest upstream semver tag, at the label's own precision. Catches an exact label like `v4.4.0` when `v4.6.0` exists.
- **Pin freshness** resolves the tag or branch a pin *names* and checks it still points at the pinned SHA. Catches what the first check cannot: a pin labelled `v7` stays "current" against `v7.0.1` by precision rules while its frozen SHA is `v7.0.0`, and a pin labelled `main` is skipped by the first check entirely.

Both report as **advisories**: they print, but they do not fail the run. A pin that names a branch drifts on every upstream commit, so failing on it would leave the weekly audit permanently red. Genuine inconsistencies — a snapshot pin missing from the tables, a SHA that disagrees with them, upstream template drift — still fail.

This matters more than it looks. Generated repos ship `.github/dependabot.yml`, so a stale pin here becomes a Dependabot PR in someone's brand-new repo within a minute of creating it.

The audit runs weekly and on demand through `.github/workflows/maintenance-audit.yml`, with `--check-action-updates` enabled and the output written to the run summary.

## Releases and the Homebrew Formula

`Formula/atomic-image-builder.rb` is a tap-ready Homebrew formula living in this repo, so no separate `homebrew-*` repository is needed — `brew tap user/name <url>` accepts any git URL.

The formula records a release tarball and its sha256, and neither can be known until the tag exists. So the order is fixed:

The formula installs the command as `aib-tool`, which is `TOOL_COMMAND` in `atomic_image_builder.py`. That constant is deliberately separate from `TOOL_SLUG`: `TOOL_SLUG` feeds `STATE_FILE` (`.atomic-image-builder.json`), which is written into every managed repo and is how the tool recognises repos it created, so renaming it would orphan all of them. `TOOL_COMMAND` only has to match what the installers put on PATH, and is `aib-tool` rather than `aib` because `contrib/aib` already claims `aib`.

1. Bump `VERSION` in `atomic_image_builder.py`. It is the single source for the tool's `--version`, the published image's version tag, and the release tag, so they should all agree. Merge it.
2. Tag and publish the release on GitHub.

That is the whole manual process. `.github/workflows/update-homebrew-formula.yml` takes it from there on `release: published` — it checks out `main` (the release event checks out the tag, and the formula lives on `main`), runs `--update` for the released tag, verifies the result with `--check`, and pushes the one-line change. Users get it on their next `brew update && brew upgrade`.

It pushes directly rather than opening a pull request because Actions is not permitted to create pull requests in this repository. The change is a single machine-generated sha256 that the job verifies before pushing.

To fix it up by hand — for a release published before that workflow existed, or after a failed run — either dispatch the workflow with the tag, or do it locally:

```bash
python3 homebrew_formula.py --update v0.9.0
```

Because the formula can only be updated after a release exists, a failure there leaves it pointing at the previous one, which installs the wrong version silently. So the weekly maintenance audit independently runs:

```bash
python3 homebrew_formula.py --check
```

which re-downloads the recorded URL and confirms the recorded sha256 still matches it, reporting as an advisory rather than a failure. It also catches the placeholder digest that ships in the formula before a release has ever been cut.

## Container Image

The tool is also published as a container image (see README.md's "Run with Podman" section for end-user usage). `Containerfile` at the repo root defines the image, built from `atomic_image_builder.py` and the bundled `template_snapshots/`. `.github/workflows/publish-image.yml` builds and pushes it to `ghcr.io/danathar/atomic-image-builder` — tagged `latest`, the tool's `VERSION`, and the short commit SHA — on every merge to `main`, when a GitHub release is published, or on manual dispatch. Only a build of `main` tags the image `latest` — a release is published from a tag ref, and one cut from an older commit must not drag `latest` backwards; it still gets the version and SHA tags. All three events share one concurrency group, since they write the same repository tags and must not race. Publishing on merge is deliberate: it previously required a release or a manual dispatch, no releases were ever cut, and `latest` sat seven weeks behind `main`. Everything the tool bakes in — the script, its `ACTION_PINS` table, and the bundled template snapshots — reaches users only through this image, so a stale image silently strands every fix made to any of them. `.github/workflows/ci.yml`'s `container-build` job builds (but does not push) the Containerfile on any push or PR that touches `Containerfile`, `container/`, `atomic_image_builder.py`, or `template_snapshots/`. It then smoke tests every non-interactive path the packaged entrypoint has and collects end-to-end coverage from them (see [Coverage](#coverage)). `contrib/aib` is the host-side wrapper and is not baked into the image, so it is deliberately not a trigger. To build and test the image locally:

```bash
podman build -t aib-local -f Containerfile .
podman run --rm aib-local --version
podman run --rm aib-local --help
```
