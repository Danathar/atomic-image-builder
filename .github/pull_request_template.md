<!--
Keep the PR scoped to one thing. See CONTRIBUTING.md's "Submitting a change".
Delete any section below that does not apply.
-->

## What changed

<!-- The change itself, in a sentence or two. -->

## Why

<!--
The problem this solves. If it closes an issue, say so here: "Closes #123".
If there is no issue, describe the symptom or the gap.
-->

## How it was checked

<!--
Which of the checks below you ran locally, and anything you verified by hand
that CI cannot -- a real `podman run`, a wizard path that needs a TTY, a
release rehearsal. CI runs the automated ones again on this PR.
-->

- [ ] `python3 -m unittest discover -s tests`
- [ ] `python3 -m coverage run -m unittest discover -s tests && python3 -m coverage report --fail-under=90`
- [ ] `ruff check`
- [ ] `shellcheck -x contrib/aib container/entrypoint.sh tests/test_contrib_aib.sh tests/test_entrypoint.sh tests/e2e/*.sh`
- [ ] `hadolint Containerfile container/Containerfile.coverage`

<!--
The pinned install for the Python tools is in CONTRIBUTING.md's Tests section.
A docs-only change does not need every box ticked -- say which ones you skipped
and why rather than ticking them all.
-->

## Anything a reviewer should look at first

<!--
The part you are least sure about, a decision that could reasonably have gone
the other way, or a follow-up you deliberately left out of scope.
-->
