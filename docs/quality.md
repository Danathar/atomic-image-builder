# Quality

Where to look when the question is "is this project in good shape", and what
each signal is worth. This is the index; the detail is in the documents it
links to.

There is no dashboard, and that is a decision rather than a gap. See
*Why there is no dashboard* at the bottom.

## The signals, and how much to trust them

| Signal                           | Gated?       | Where                                | Worth                                                        |
| -------------------------------- | ------------ | ------------------------------------ | ------------------------------------------------------------ |
| Unit coverage                    | **Yes**, 90% | README badge, `coverage-data` branch | Says the gate is not the binding constraint. Flat at 100%.   |
| `ruff`, `shellcheck`, `hadolint` | **Yes**      | `ci.yml` `test` job                  | Binary. Either clean or the build is red.                    |
| End-to-end coverage              | No           | `coverage-e2e` artifact              | The most informative number here, and near 10% by design.    |
| Shell-entrypoint coverage        | No           | `coverage-shell` artifact            | Line, not branch. Read the quirks first.                     |
| Maintenance-audit coverage       | No           | weekly artifact                      | Low is correct. Not a target.                                |
| Homebrew-release coverage        | No           | release artifact                     | Only exists after a release.                                 |
| Weekly audit                     | Partly       | `maintenance-audit.yml` runs         | Failures matter, advisories usually do not.                  |
| Review findings per PR           | No           | inline PR comments                   | The signal that counts problems found rather than work done. |

[CONTRIBUTING.md's Coverage section](../CONTRIBUTING.md#coverage) explains all
five coverage tiers and why exactly one is gated.
[metrics.md](metrics.md) has the command to reproduce each number and, more
usefully, what each one does not mean.
[coverage.md](coverage.md) is the plain-language explainer for the badge.

## The two numbers most often misread

**Unit coverage at 100% is the weakest of the coverage signals, not the
strongest.** It measures the source tree with the network and subprocesses
mocked. What a real run of the built container executes is the end-to-end
tier, and that sits near 10% because the guided wizard needs a TTY. A reader
who takes 100% as "fully tested" has the picture backwards.

**A low advisory percentage is usually correct.** The maintenance-audit tier
is low because a clean live run cannot reach a rate limit, a DNS failure, or a
malformed response. Raising it would mean manufacturing those, destroying the
only thing it measures. This was filed once as a defect
([#123](https://github.com/Danathar/atomic-image-builder/issues/123)) and the
right answer was loopback tests in the unit tier
([#124](https://github.com/Danathar/atomic-image-builder/pull/124)), not a
bigger number.

## How quality is actually enforced

Not by watching numbers. By things that fail:

- **The gate**, on every push and PR. Threshold in
  `.coverage-thresholds.json`, read by the workflow so it cannot drift from
  what the docs say.
- **Consistency tests.** Several tests exist only to fail when a document
  drifts from what it describes -- the coverage threshold across four files,
  the pinned tool versions, the agent-guidance files all pointing at one
  canonical brief. This is why a docs-only change still runs the suite.
- **The weekly audit**, for the things no test can see: a snapshot trailing
  upstream, a pin that no longer matches its tag, the Homebrew formula.
- **[The review rubric](review-rubric.md)**, for what remains: whether a claim
  matches the code, whether a guard would catch the bug it is for, whether a
  path-scoped job still fires.
- **`.claude/settings.json`**, for the part worth enforcing mechanically
  rather than remembering.

## Why there is no dashboard

Every number above is already produced, and the two that matter fail loudly on
their own. A dashboard would add a scheduled job whose output nobody reads
weekly, and it would present the advisory tiers next to the gated one as though
they were comparable -- which is the exact misreading this page and
[metrics.md](metrics.md) exist to prevent.

The trend data that is worth keeping is kept: `coverage-trend.csv` on the
`coverage-data` branch, because artifacts expire after 30 days and the gated
number is the one worth having a history of.
