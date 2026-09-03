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
python3 -m coverage run -m unittest discover -s tests && python3 -m coverage report
ruff check
shellcheck -x contrib/aib container/entrypoint.sh tests/test_contrib_aib.sh tests/test_entrypoint.sh tests/e2e/*.sh
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

**Nothing to run for a docs-only change.** Say which checks you skipped and
why, rather than reporting a gate you did not run.

## What this does not cover

The container image. `--skip-upstream` and the suite above never build it, so
a change to `Containerfile`, `container/`, `atomic_image_builder.py` or
`template_snapshots/` is unverified until the image is built and
`tests/e2e/smoke.sh` runs against it. `tests/e2e/README.md` has the two build
commands. CI does this on a path trigger; locally it is a deliberate step.
