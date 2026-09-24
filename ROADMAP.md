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

- **Local test builds are Containerfile-only.** BlueBuild users can't
  test-build locally before pushing the way Containerfile users can.
- **Advanced BlueBuild modules beyond the guided wizard are out of scope.**
  The wizard covers the common path; anything past it currently means
  editing the generated repo by hand.
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
2. **BlueBuild local test build parity**, or an explicit decision to leave
   it Containerfile-only with the reason written down, so it stops reading
   as a TODO.

## Longer-term / open questions

- Whether advanced BlueBuild module support belongs in the guided wizard at
  all, or is better left to "generate the repo, then edit it by hand" —
  worth a decision either way rather than leaving it as an unstated gap.

---

This document tracks what the project's own README and issues already say
about direction; it does not add new commitments on the maintainer's behalf.
Update it as priorities shift — it should stay accurate, not aspirational.
