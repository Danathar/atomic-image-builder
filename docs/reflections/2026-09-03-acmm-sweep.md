# 2026-09-03 — the ACMM sweep

Twenty-odd ACMM criteria were worked in one day, from L0 through L4. The
interesting part is not the files that resulted. It is what the exercise
revealed about the repository, which was not what it looked like at the start.

For the current rules, read
[`.github/copilot-instructions.md`](../../.github/copilot-instructions.md).
This is the retrospective, not the reference.

## Most of the early gaps were not gaps

Of the five L0 criteria, three were false negatives. The repo already had a
ruff config, a coverage gate, and end-to-end tests. What it did not have were
the *filenames* the evaluation looks for: the config was `.ruff.toml` rather
than `ruff.toml`, and the gate and the tests were written inline in `ci.yml`
rather than in files of their own.

The tempting response is a placeholder that quiets the check. The useful one
turned out to be asking what real defect sat behind each missing name, and in
every case there was one:

- `.ruff.toml` silently wins over `ruff.toml`, so the dotted name is a trap for
  whoever adds the plain one later.
- The gate's threshold was written out in five places with nothing tying them
  together.
- The end-to-end scenarios could not be run locally, were not linted, and used
  a bare `:ro` mount that fails on any SELinux host — which is every machine
  this tool targets.

None of those would have been found by adding a file.

## The checks written along the way were wrong more often than the code

Four guards were added during the sweep. Three of them were wrong, and none of
the three was caught by its own test suite. Each was revealed by the next file
it had to judge:

1. A blocklist of words that pointer files must not contain, which would have
   forced a prompt about refreshing the action pins to avoid naming
   `ACTION_PINS`.
2. The replacement, comparing sentences, which then flagged a *command block*
   that a "run these commands" skill has to contain.
3. A scan of the working tree rather than tracked files, which failed on a
   maintainer's personal untracked file — green in CI, red on their machine.
4. A threshold check that read `04` out of the clock time `04:00` because the
   same line mentioned the gate.

The pattern is worth stating plainly: **a guard written before the things it
guards is a hypothesis.** It passes because the only inputs it has seen are
the ones it was written against. The tempting fix each time was to reword the
*content* so the guard stopped complaining, which would have taught the docs to
be evasive rather than correct.

## The most valuable review findings were about scope, not code

An automated reviewer raised 24 findings across the sweep's 24 pull requests.
Roughly a third were real defects; most of the rest were accurate observations
about a single commit whose remedy was merge order rather than a code change.
The two sharpest were not bugs in a function:

- A path-scoped CI job did not fire on changes to the very end-to-end suites it
  runs, while lint still passed — so a change to a test read as covered.
- A "complete local gate" skill omitted the two behavioural shell harnesses and
  ran coverage without a threshold, so it could have declared a change ready
  that CI rejects.

Both were about a check not covering what it appeared to cover. That is the
category worth looking for first.

## The measurements say less than they appear to

Documenting the metrics made the ranking obvious in a way the numbers alone
never did. Unit coverage sits at 100% and is the *weakest* of the coverage
signals: it measures the source tree with the network and subprocesses mocked.
The end-to-end tier, which measures a real run of the built image, sits near
10%. And the merge rate, which sits above 90%, is not an acceptance rate at
all, since nearly every pull request is opened by the maintainer or on the
maintainer's instruction.

A dashboard would have placed all of those side by side as though comparable.
That is why there is not one.

## What would have saved the most time

Checking whether a capability already existed, under another name or inline
somewhere, before treating an issue as new work. That single check would have
changed the shape of the first five issues, and it is now the first thing the
`ai-fix-requested` intake comment says.
