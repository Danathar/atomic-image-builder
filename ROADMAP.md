# Roadmap

This is a living summary of where the project stands and the gaps its own
docs already name, not a commitment list. It exists so contributors and
adopters have one place to see direction instead of piecing it together from
issues and the README.

## Current state

- **0.10, beta.** The README says outright: "not fully tested. Review the
  changes it makes before applying them." Leaving beta — defined as: the
  scan/carry/build path has enough real-world use across the supported bases
  that the warning can come off — is the top-level milestone everything
  else sits under.
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

1. **Beta exit criteria.** Write down, even roughly, what "not beta" means
   for this tool (e.g. N releases without a scan/carry regression, coverage
   gate holding at its current bar, no open correctness issues against the
   five-stage runtime). Right now the beta label has no stated exit
   condition, which makes it hard for adopters to judge how far out
   general-availability is.

   **Candidate bar (proposal, not yet a maintainer decision — see #475):**
   three consecutive tagged releases with no scan/carry/build regression
   reported against either build method, the coverage gate holding at or
   above its current threshold across those releases, and zero open
   correctness issues against the five-stage runtime at time of the third
   release. This is one concrete way to satisfy priority #1 above; picking
   it, adjusting it, or replacing it is the maintainer's call.

## Longer-term / open questions

- **Homebrew distribution beyond the custom tap (#482).** The README's
  fastest install path is `brew tap danathar/aib ...` — a personal tap, not
  homebrew-core. That's the right fit for beta software; homebrew-core has
  its own bar (stability, notability) this project doesn't clear yet. This
  entry just records that homebrew-core submission is a candidate milestone
  for *after* the beta-exit criteria above are met, not a decision to pursue
  it now or a gap to close today.

The last two entries here were closed: whether advanced BlueBuild module
support belongs in the guided wizard was decided in #468 and is recorded
under the gaps above, and BlueBuild local test builds reached parity with
the Containerfile path in #463, so that gap is gone from the README.

---

This document tracks what the project's own README and issues already say
about direction; it does not add new commitments on the maintainer's behalf.
Update it as priorities shift — it should stay accurate, not aspirational.
