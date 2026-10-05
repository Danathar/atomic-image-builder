# Roadmap

This is a living summary of where the project stands and the gaps its own
docs already name, not a commitment list. It exists so contributors and
adopters have one place to see direction instead of piecing it together from
issues and the README.

## Current state

- **0.11, beta.** The README says outright: "not fully tested. Review the
  changes it makes before applying them." Leaving beta is the top-level
  milestone everything else sits under. **The beta-exit bar (decided
  2026-10-01, #578):** three consecutive tagged releases with no
  scan/carry/build regression reported against either build method, the
  coverage gate holding at or above its current threshold across those
  releases, and zero open correctness issues against the five-stage runtime
  at the time of the third release.
- Supported bases: Universal Blue (Bazzite, Aurora, Bluefin and their
  variants) and Fedora Atomic (Silverblue, Kinoite, Sway, Budgie, COSMIC).
- Two build methods: Containerfile (from `ublue-os/image-template`) and
  BlueBuild (from `blue-build/template`).

## Gaps the README already states

These are pulled directly from the "What it does not do" section — they are
scope boundaries the maintainer chose, not necessarily things that must
change, but they are the concrete candidates for "what's next" when someone
asks:

- **Advanced BlueBuild modules beyond the guided wizard are out of scope.**
  This is a decision, not a TODO (#468): the wizard only offers choices that
  produce the same image under either build method, and keeping the two
  methods symmetric matters more during beta than reaching further into
  BlueBuild's module system. Adding a module by hand means editing the
  generated recipe, which the tool rewrites on every update, so a repo edited
  that way should no longer be updated with the tool.
- **Repos not created by the tool are never adopted.** A repo without
  `.atomic-image-builder.json` stays untouched, by design — worth restating
  here since it shapes what "supporting an existing repo" can ever mean for
  this project.

## Near-term priorities

1. **Beta exit criteria: decided.** The bar is recorded under "Current state"
   above (#578). What's left is meeting it: three regression-free releases in
   a row, with the coverage gate holding and no open five-stage correctness
   issues when the third one ships. Each release's result is recorded under
   "Beta-exit progress" below (#620).

## Beta-exit progress

How close the project is to the bar under "Current state", one row per
tagged release. Only releases tagged after the bar was decided on 2026-10-01
count. v0.10.0 (2026-09-21) came before it, so it does not.

**Clean releases in a row: 0 of 3.**

| Release | Date | Containerfile regressions | BlueBuild regressions | Unit coverage | Open five-stage correctness issues | Counts |
| ------- | ---- | ------------------------- | --------------------- | ------------- | ---------------------------------- | ------ |

No release has been recorded yet. After each tagged release, add a row and
update the count above. [docs/strategy.md](docs/strategy.md) has the command
that reads each column:

- **Release** and **Date**: the tag and the day it was published.
- **Containerfile regressions** and **BlueBuild regressions**: the issue
  numbers of any scan, carry or build regression reported against that build
  method in this release, or "none". If one is reported later against a
  release already in the table, add it to that row.
- **Unit coverage**: the unit coverage percentage at the tagged commit, from
  the history described in docs/metrics.md. The gate it has to meet is the
  unit threshold in .coverage-thresholds.json, the same one CI enforces.
- **Open five-stage correctness issues**: how many open bug issues there are
  against the five-stage runtime on the release date.
- **Counts**: "yes" when both regression columns say "none" and unit coverage
  is at or above the gate, otherwise "no".

The count above is the number of "yes" rows at the bottom of the table, in a
row. A "no" sets it back to 0. The bar is met when that count reaches three
and the newest of those three releases has 0 open five-stage correctness
issues. Mark this section "met" then, not before.

## Longer-term / open questions

- **Homebrew distribution beyond the custom tap (#482).** The README's
  fastest install path is a personal Homebrew tap (danathar/aib), not
  homebrew-core. That's the right fit for beta software; homebrew-core has
  its own bar (stability, notability) this project doesn't clear yet. This
  entry just records that homebrew-core submission is a candidate milestone
  for *after* the beta-exit criteria above are met, not a decision to pursue
  it now or a gap to close today.
- **A ujust recipe as a discovery channel (#486).** Bazzite, Bluefin, and
  Aurora — the same bases the README already leans on for shipping Homebrew —
  also expose a ujust menu as their built-in way for users to find guided
  recipes. Nothing here is listed there, so someone on exactly the bases this
  tool targets still has to already know the project exists before the
  Homebrew quick-start helps them. This entry records a ujust recipe
  submission (e.g. via the ublue-os/bling repo or an image's own
  custom-recipes convention) as a candidate post-beta-exit discovery
  channel — not a decision to pursue it now.

The last two entries here were closed: whether advanced BlueBuild module
support belongs in the guided wizard was decided in #468 and is recorded
under the gaps above, and BlueBuild local test builds reached parity with
the Containerfile path in #463, so that gap is gone from the README.

---

This document tracks what the project's own README and issues already say
about direction; it does not add new commitments on the maintainer's behalf.
Update it as priorities shift — it should stay accurate, not aspirational.
