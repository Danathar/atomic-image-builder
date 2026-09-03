---
mode: agent
description: Cut a release, in the one order that works
---

Read [.github/copilot-instructions.md](../copilot-instructions.md) first, then
`maintainer_docs/MAINTAINER.md`'s *Cutting a release*, which is the
authoritative procedure and explains why each step is where it is.

The order is not a preference. The Homebrew formula records a release tarball
**and its sha256**, and a sha256 is a fact about a published tag, so it cannot
be recorded before the tag exists. That single constraint sets the whole
sequence.

## What to do

1. Branch from a current `origin/main`, naming the remote ref explicitly so a
   stale local `main` cannot put unrelated reversions in the pull request.
2. Bump the one constant. Everything else derives from it — MAINTAINER.md has
   the table of what reads it.
3. Open a PR for the bump and merge it once CI is green.
4. Tag and publish the release, targeting `main`, so the tag points at a commit
   that already carries the new version.
5. Watch the formula-update workflow, then confirm on `main` rather than on the
   release branch: the bot's commit landed there.

## What not to do

- Do not tag before the bump has merged. The workflow compares the formula's
  tag against the constant and will fail. That is the check working.
- Do not edit the formula by hand to get ahead of the automation.
- Do not run `brew audit` against a real Homebrew prefix. MAINTAINER.md's
  *Before pushing* explains what that breaks and what to run instead.
- Do not be surprised by two image publishes. A release produces one from the
  release event and one from the formula-update push to `main`. Same content.

## Done when

The formula check reports the pin is current, and the published image carries
the new version tag.
