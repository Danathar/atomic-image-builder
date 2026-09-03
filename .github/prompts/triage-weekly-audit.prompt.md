---
mode: agent
description: Triage the weekly maintenance audit and decide what actually needs fixing
---

Read [.github/copilot-instructions.md](../copilot-instructions.md) first.

The weekly `maintenance-audit.yml` job has run. Work out which of its output
needs action and which is expected noise.

## What to do

1. Read the run summary, not just the pass/fail.
2. Separate **failures** from **advisories**. They mean different things, and
   `maintainer_docs/MAINTAINER.md`'s *Reading the weekly audit* section defines
   the difference. Read it before deciding anything.
3. For each failure, fix it. These mean the repo is internally inconsistent,
   and it is fixable here.
4. For each advisory, decide whether it has crossed the line into needing
   action. Report the number, not just its presence.
5. Report what you found, split by category, and what you propose to do.

## What not to do

- Do not treat an advisory as a failure to be cleared. Advisories reappear
  every time something upstream commits. Reacting to each one is how a weekly
  job becomes permanently red and stops being read.
- Do not file the maintenance-audit coverage percentage as a finding. Its low
  number is the expected reading. `CONTRIBUTING.md` explains why at length,
  and issue #123 is the example of getting this wrong.
- Do not refresh a template snapshot or an action pin as part of triage.
  Those have their own prompts and their own verification steps.

## Done when

Every failure is fixed or has an explanation for why it cannot be, every
advisory has a stated read, and nothing has been changed that only needed to
be reported.
