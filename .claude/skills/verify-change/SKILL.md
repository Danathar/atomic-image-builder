---
name: verify-change
description: Run this repo's complete local gate before pushing, in the order that fails fastest, and interpret what each check means. Use before opening a PR or when asked whether a change is ready.
---

# Verify a change

The checks CI runs, in the order that surfaces the cheapest failure first.
`.github/copilot-instructions.md` has the same list; this adds the ordering and
what to do with each result.

## Run

```bash
python3 -m unittest discover -s tests

# .coveragerc sets no fail_under, so a bare `coverage report` exits 0 however
# far coverage has fallen. Read the gate the way ci.yml does instead of
# writing the number here, which would be one more copy to keep in step.
python3 -m coverage run -m unittest discover -s tests
python3 -m coverage report --fail-under="$(jq -er '.gated.unit' .coverage-thresholds.json)"

ruff check

# shellcheck is static analysis only. The behavioral assertions for the two
# shell entrypoints live in these harnesses, and CI runs them (under bashcov,
# which adds coverage but is not needed to run them).
shellcheck -x contrib/aib container/entrypoint.sh tests/test_contrib_aib.sh tests/test_entrypoint.sh tests/e2e/*.sh
tests/test_contrib_aib.sh
tests/test_entrypoint.sh

# No file arguments: given none, actionlint lints every workflow under
# .github/workflows, so this cannot fall out of step with the directory.
actionlint
hadolint Containerfile container/Containerfile.coverage
python3 maintenance_audit.py --skip-upstream
```

Install the pinned versions first if you have not; the command is in
`CONTRIBUTING.md`'s *Tests* section. An unpinned `ruff` disagrees with CI in
both directions, which wastes a round trip either way.

## Reading the results

**Coverage below the gate.** The threshold comes from
`.coverage-thresholds.json`, not from the command line. Do not pass a
`--fail-under` value to make it agree; find the uncovered lines in the report's
`Missing` column.

**A test asserting a doc says something.** This repo has several tests that tie
a document to the thing it describes. They fail because the doc is now wrong,
not because the test is brittle. Fix the doc.

**`maintenance_audit.py` failing.** A failure means the repo contradicts
itself and is fixable here. An advisory means something upstream moved and is
not yours to clear. `MAINTAINER.md`'s *Reading the weekly audit* draws the line.

**A docs-only change still runs the suite.** Several tests here read the
documents and fail when one drifts from what it describes -- the coverage
threshold is checked against CONTRIBUTING.md, MAINTAINER.md, docs/coverage.md
and the PR template. Editing any of those without running the tests is the
case most likely to pass locally and fail on the push. What a docs-only change
can skip is the image work below; say which you skipped rather than reporting
a gate you did not run.

## What a missing tool hides

A test that skips rather than fails means a green local run does not always
mean what it looks like. Two tools do that here, and both cover generated
output -- the part that reaches other people's repositories:

- **`just`** runs the generated `spawn-vm` recipe. It is the only check that
  catches a wrong Just parameter binding; `just --fmt --check` and a dry run
  both pass on one.
- **`actionlint`** lints the generated workflows. It is what would have caught
  the invalid `./`-prefixed path filters in #237.

CI installs both at pinned versions, so a skip here becomes a real run there.
`python3 -m unittest discover -s tests -v 2>&1 | grep -i skipped` says which
ones you are missing.

## What this does not cover

The container image. `--skip-upstream` and the suite above never build it, so
a change to `Containerfile`, `container/`, `atomic_image_builder.py` or
`template_snapshots/` is unverified until the image is built and
`tests/e2e/smoke.sh` runs against it. `tests/e2e/README.md` has the two build
commands. CI does this on a path trigger; locally it is a deliberate step.
