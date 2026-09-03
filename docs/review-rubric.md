# Review rubric

What review looks for in this repository, and in what order. Written from what
review here has actually caught, not from a generic checklist: every item
below has a real example behind it.

The mechanical checks are not in this document. CI runs them and
[CONTRIBUTING.md](../CONTRIBUTING.md) lists them. A rubric that repeats
"tests pass" adds nothing a gate has not already decided.

## 1. Does the claim match the code?

The first question for any finding, whoever raised it, including an automated
reviewer. Check it against the code or the docs before acting on it.

This is not scepticism for its own sake. Findings here have been right often
enough to take seriously and wrong often enough to verify, and the cost is
asymmetric: acting on a wrong finding changes working code.

The same applies to a pull request description. A PR asserting that something
was verified should say what was run and what the output was, so the assertion
can be checked rather than trusted.

## 2. Is the number in the right place?

This repo has a recurring failure mode: a value written in several places with
nothing tying them together. The coverage threshold lived in five before it
was consolidated
([#144](https://github.com/Danathar/atomic-image-builder/issues/144)), and a
partial update slipped past the first attempt at guarding it.

So: a new constant, threshold, version pin or path that appears in more than
one file needs a single source and something that fails when the copies
disagree. A comment saying "keep in sync" is not that something.

## 3. Would this check have caught the bug it is for?

A test added alongside a fix should fail without the fix. A guard added to
prevent a class of mistake should be shown failing on an instance of it.

Two guards in this repo were wrong when written and only revealed by the next
file that came along -- one banned words its own subject matter required, the
other compared code blocks that are legitimately repeated. Both passed their
own suite. A guard written before the things it guards is a hypothesis, so ask
what was run to confirm it bites.

## 4. Is the advice true for *this* repo?

Generic good practice can be locally wrong here, and has been:

- Skipping tests for a docs-only change, when several tests read the docs and
  fail on drift.
- Reading a low coverage percentage as a gap, when the tier exists to show
  what a live run cannot reach
  ([#123](https://github.com/Danathar/atomic-image-builder/issues/123)).
- Failing a job on upstream drift, which took the weekly audit red every
  Monday until it was graduated
  ([#129](https://github.com/Danathar/atomic-image-builder/issues/129)).
- `insert_final_newline = false` to mean "leave this file alone", when it
  means the opposite.

## 5. Does a path-scoped job still fire?

Anything gated on changed paths needs asking: after this change, does the job
that exercises it still run? The container job did not run on changes to the
very end-to-end suites it executes, while lint still passed -- so a change to
a test read as covered.

## 6. Is the vendored snapshot untouched?

`template_snapshots/` is a pinned copy of upstream, refreshed as a unit. A
diff that reformats it, fixes its lint, or hand-edits it to make a check pass
is a defect regardless of how the check responds.

## 7. Is the blast radius stated?

[docs/risk-tiers.md](risk-tiers.md) classifies changes by how far they reach,
and how much evidence each tier needs. The tiers are about reach, not size: a
one-line pin change sits in the tier that touches every generated repository.


This tool creates and pushes to GitHub repositories, publishes images, and
rotates a signing key. A change touching any of those should say what it can
affect and what it deliberately leaves alone. `.claude/settings.json` holds the
part of that a tool enforces.

## 8. Is scope honest?

A PR should do the thing it says and not quietly more. Where it turned out
that part of the work was wrong or blocked, that belongs in the description
rather than in a follow-up nobody files.

Related: a review reply that says "fixed" should say what changed and, where
the finding was about behaviour, what now demonstrates it.

## What review does not do here

- It does not re-run the gate. CI has already decided that.
- It does not rewrite prose to taste. Match the surrounding voice; a
  convention nobody asked for is one someone has to undo.
- It does not resolve a thread it did not act on. A fix resolves a thread; a
  disagreement leaves it open for the maintainer.
