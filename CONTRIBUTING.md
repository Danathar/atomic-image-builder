# Contributing

This document covers the development and maintenance workflows for working on Atomic Image Builder itself. End-user installation and usage live in [README.md](README.md).

## Tests

Run the test suite with `unittest`:

```bash
python3 -m unittest discover -s tests
```

## Coverage

There are two separate coverage measurements, and they are not interchangeable.

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

Both measurements are uploaded by `.github/workflows/ci.yml` as workflow artifacts — `coverage-unit` and `coverage-e2e` — each containing `coverage.xml`, an HTML report, and the raw coverage data files (one per e2e scenario). Download both and merge them into a single report from any clone:

```bash
gh run download <run-id>
python3 -m coverage combine --rcfile=.coveragerc.e2e coverage-unit/data coverage-e2e/data
python3 -m coverage report --rcfile=.coveragerc.e2e
```

That works off the runner only because the raw data is path-portable, which took two things: `relative_files = True` in both configs, so the unit data names files relative to the checkout rather than by the runner's workspace path, and the `[paths]` mapping in `.coveragerc.e2e`, which rewrites the in-image `/opt/atomic-image-builder` onto the checkout. Removing either one makes the artifacts combinable only in the workspace that produced them.

A commit that changes nothing the image is built from will not produce a `coverage-e2e` artifact, since the `container-build` job is path-scoped. Run the CI workflow manually (`workflow_dispatch`) to force one.

## Linting

```bash
ruff check
```

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

## Container Image

The tool is also published as a container image (see README.md's "Run with Podman" section for end-user usage). `Containerfile` at the repo root defines the image, built from `atomic_image_builder.py` and the bundled `template_snapshots/`. `.github/workflows/publish-image.yml` builds and pushes it to `ghcr.io/danathar/atomic-image-builder` — tagged `latest`, the tool's `VERSION`, and the short commit SHA — on every merge to `main`, when a GitHub release is published, or on manual dispatch. Only a build of `main` tags the image `latest` — a release is published from a tag ref, and one cut from an older commit must not drag `latest` backwards; it still gets the version and SHA tags. All three events share one concurrency group, since they write the same repository tags and must not race. Publishing on merge is deliberate: it previously required a release or a manual dispatch, no releases were ever cut, and `latest` sat seven weeks behind `main`. Everything the tool bakes in — the script, its `ACTION_PINS` table, and the bundled template snapshots — reaches users only through this image, so a stale image silently strands every fix made to any of them. `.github/workflows/ci.yml`'s `container-build` job builds (but does not push) the Containerfile on any push or PR that touches `Containerfile`, `container/`, `atomic_image_builder.py`, or `template_snapshots/`. It then smoke tests every non-interactive path the packaged entrypoint has and collects end-to-end coverage from them (see [Coverage](#coverage)). `contrib/aib` is the host-side wrapper and is not baked into the image, so it is deliberately not a trigger. To build and test the image locally:

```bash
podman build -t aib-local -f Containerfile .
podman run --rm aib-local --version
podman run --rm aib-local --help
```
