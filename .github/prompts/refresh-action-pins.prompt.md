---
mode: agent
description: Refresh a GitHub Actions SHA pin without downgrading it or breaking the audit
---

Read [.github/copilot-instructions.md](../copilot-instructions.md) first, then
`maintainer_docs/MAINTAINER.md`'s *Refreshing action pins* section, which is
the authoritative procedure. This prompt only frames the task.

These pins ship to every repo the tool generates, and generated repos carry
`.github/dependabot.yml`. A stale pin becomes a Dependabot PR in a stranger's
new repo within a minute of it being created, so this matters more than it
looks.

## What to do

1. **Establish the direction of travel before changing anything.** Compare the
   pinned SHA against the tag or branch it names. If the comparison says the
   pin is *behind*, refreshing moves forward; if *ahead*, refreshing is a
   downgrade and you should stop and say so. MAINTAINER.md gives the exact
   `gh api ... /compare/` invocation.
2. Update the SHA in **both** places. The pin tables in
   `atomic_image_builder.py` and the workflow file under `template_snapshots/`
   have to agree, or the audit fails.
3. Leave a ref-pin entry mapping the old SHA to the new one, so repos already
   carrying the old pin get rewritten.
4. Run `python3 maintenance_audit.py --skip-upstream` and the test suite.

## What not to do

- Do not edit `template_snapshots/` for anything beyond the pin itself. It is
  a vendored copy refreshed as a unit.
- Do not refresh a pin because a version number looks old. The comparison in
  step 1 is the evidence; a label can be misleading in both directions.

## Done when

Both locations agree, the audit passes locally, and the report says which
direction the comparison showed.
