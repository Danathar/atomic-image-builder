# Corrections

Beliefs that were acted on here and turned out to be wrong. Each cites the
issue or PR that records it. The point is not the history: it is that several
of these are mistakes an agent would make again from a cold start, because the
correct answer looks wrong until you know why.

For the current rules rather than how they were reached, read
[`.github/copilot-instructions.md`](../../.github/copilot-instructions.md).

---

## A low coverage number is not always a gap

**Believed:** the maintenance-audit tier's low coverage percentage was an
untested-code finding to be closed.
**Actually:** that tier measures a live run against the real GitHub API, so
its uncovered lines are the error paths a clean run never touches. Raising the
number would mean manufacturing a rate limit and a DNS failure, destroying the
only signal the tier carries.
**Settled on:** prove those paths in the unit tier against a real loopback
socket instead, and read the percentage as a map of what live runs miss.
**Record:** [#123](https://github.com/Danathar/atomic-image-builder/issues/123),
answered by [#124](https://github.com/Danathar/atomic-image-builder/pull/124).

## A red weekly job is worse than a quiet one

**Believed:** bundled template snapshot drift should fail the audit, since
drift is real.
**Actually:** both upstreams commit constantly, so it failed on 5 of its last
6 scheduled runs, none by more than 4 commits. A job that is red every Monday
cannot tell you about the Monday something is wrong.
**Settled on:** graduated reporting. Ordinary drift is an advisory carrying the
commit count; past a threshold it fails again, because by then it is neglect
rather than upstream movement.
**Record:** [#129](https://github.com/Danathar/atomic-image-builder/issues/129),
fixed by [#130](https://github.com/Danathar/atomic-image-builder/pull/130).

## Uncovered lines are sometimes a measurement artifact

**Believed:** two uncovered lines in `contrib/aib` were an untested path.
**Actually:** Bashcov attributes a hit only to the line a statement *starts*
on, so the continuation lines of a multi-line array literal read as unhit
however thoroughly the statement is asserted.
**Settled on:** write array pushes one per line, so the reported number matches
what the tests prove. Check for this before reading a partial percentage as a
gap.
**Record:** [#118](https://github.com/Danathar/atomic-image-builder/issues/118),
fixed by [#120](https://github.com/Danathar/atomic-image-builder/pull/120).

## A pinned dependency is not pinned if only some of it is

**Believed:** naming the tools CI installs was enough.
**Actually:** unpinned `pip install coverage ruff` meant a lint rule set that
changed under the repo without any commit, and a local run that could disagree
with CI in either direction.
**Settled on:** exact versions in the workflow, the same versions in
CONTRIBUTING.md, and a test reading the workflow's own pin so the doc cannot
go stale silently.
**Record:** [#112](https://github.com/Danathar/atomic-image-builder/issues/112),
fixed by [#115](https://github.com/Danathar/atomic-image-builder/pull/115).

## Checking one mention is not checking consistency

**Believed:** asserting each document mentioned the coverage threshold
somewhere was enough to stop it drifting.
**Actually:** CONTRIBUTING.md names it three times. Updating one and leaving
the others passed the test that existed to prevent exactly that.
**Settled on:** check every line that talks about the gate, and require each
number on such a line to match the single source of truth.
**Record:** review feedback on
[#148](https://github.com/Danathar/atomic-image-builder/pull/148).

## A path-scoped job must be triggered by its own tests

**Believed:** the container job's path filter should list only what is baked
into the image.
**Actually:** that left the end-to-end suites able to change without the job
that runs them ever firing, while lint still passed, so a change to a test
read as covered.
**Settled on:** `tests/e2e/` is a trigger too. It is the one entry in that list
not present in the image, and it is there because it is what proves the image.
**Record:** review feedback on
[#149](https://github.com/Danathar/atomic-image-builder/pull/149).
