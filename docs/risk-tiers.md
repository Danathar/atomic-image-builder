# Risk tiers

How to tell how far a change here can reach. The tiers are about **blast
radius**, not size: a one-line diff sits in the highest tier and a 300-line
documentation rewrite sits in the lowest.

Use this to decide how much evidence a change needs, and what a reviewer
should look at first. [docs/review-rubric.md](review-rubric.md) is what review
checks; this is how much of it applies.

[`.claude/risk-config.json`](../.claude/risk-config.json) is the same map in
a form a program can read: each tier's path claims, reach and evidence
commands, copied from this page. `tests/test_risk_config.py` fails when the
two disagree, so a path added or moved here has to move there in the same
change, and the other way round.

## Tier 1 — this repository only

**Paths:** `*.md`, `docs/`, `maintainer_docs/`, `tests/`, `.editorconfig`,
`.cursor/rules/atomic-image-builder.mdc`, `.claude/risk-config.json`,
`.gitignore`, `LICENSE`, `maintenance_notes.txt`, `format_markdown_tables.py`;
the lint and coverage configuration — `ruff.toml`, `.coveragerc`, `.coveragerc.e2e`,
`.coveragerc.maintenance-audit`, `.simplecov`, `.coverage-thresholds.json`,
`.github/auto-qa-tuning.json`; the issue forms in `.github/ISSUE_TEMPLATE/`;
and the workflows whose token can only read the repository or write to its
issue tracker — `.github/workflows/nightly-compliance.yml`,
`.github/workflows/agent-audit.yml`, `.github/workflows/triage.yml`,
`.github/workflows/ai-fix.yml`, `.github/workflows/maintenance-audit.yml` —
with `snapshot_drift_issue.py`, which the audit workflow runs to open its
issues

**Reaches:** contributors and maintainers. Nothing users run.

**Evidence:** the unit suite. A documentation change still runs it, because
several tests read the documents and fail when one drifts from what it
describes.

Note `tests/e2e/` is Tier 1 by content but triggers the container build, since
a change to a suite that never runs the suite reads as covered.

The lint and coverage configuration is here by reach: a lowered floor or a
narrowed measurement is found by the next contributor, not by a user. It is
still not a change to make quietly. `.coverage-thresholds.json` is the gate
`ci.yml` enforces and `.github/auto-qa-tuning.json` is the standing record
that no machine moves it, so a diff that lowers the number needs to say why in
words, and a diff that shrinks what `.coveragerc` measures needs to say what
it stopped measuring.

The issue-tracker workflows are here by reach too: what `triage.yml`,
`ai-fix.yml` and `maintenance-audit.yml` can touch with their `issues: write`
token is this repository's issues and the labels the agents act on,
`nightly-compliance.yml` holds `contents: read`, and `agent-audit.yml` holds
`contents: read` beside the `pull-requests: read` it needs to list merged pull
requests; it writes nothing but its own run summary. Their permission blocks
are pinned by `.github/policies/workflow-permissions.json`, so asking one of
them for a broader token is a Tier 4 change whichever file the diff starts
in. The same holds for `snapshot_drift_issue.py`, which runs under the audit
workflow's token and can open issues and nothing else.

## Tier 2 — the tool users run

**Paths:** `atomic_image_builder.py` (except the generated-output writers,
which are Tier 3), `contrib/aib`, `container/`, `Containerfile`

**Reaches:** everyone who runs `aib-tool` or `aib`, on their own machine,
against their own GitHub account.

**Evidence:** the unit suite plus a real build. Neither `--skip-upstream` nor
the unit suite builds the image, so this tier is unverified locally until
`tests/e2e/smoke.sh` runs against one — see
[`.claude/skills/verify-change`](../.claude/skills/verify-change/SKILL.md).

`contrib/aib` is the exception, and it is easy to get wrong: it is **not**
baked into the image, so `smoke.sh` never touches it and a wrapper change can
look verified while nothing has run it. Its behaviour is proven by
`tests/test_contrib_aib.sh`, with `shellcheck -x` for the static half. The
same applies to `container/entrypoint.sh` and `tests/test_entrypoint.sh`.

## Tier 3 — what every generated repository ships

**Paths:** `template_snapshots/`, the `ACTION_PINS` / `ACTION_REF_PINS`
tables, the generated-output writers in `atomic_image_builder.py` — the
`patch_*_workflow` functions and the project writers around them — and
`maintenance_audit.py`, the audit that is this tier's evidence. The writers
live in the tool but their output is other people's CI, so they belong here
rather than in Tier 2. The audit is here because a defect
in it is what lets a stale pin or a drifted snapshot ship unnoticed: it fails
towards silence, and silence in this tier reads as green.

**Reaches:** every repository the tool has created or will create, including
other people's. A stale pin becomes a Dependabot pull request in a stranger's
new repo within a minute of them running the tool; the snapshots become their
CI.

**Evidence:** `python3 maintenance_audit.py` (not `--skip-upstream`, which
skips the drift checks that matter here), **plus the patcher tests**. The
audit alone is not sufficient and it is important to know why: it checks
source metadata and that the pin tables agree with the snapshot workflows. It
never runs the patchers. They match exact text and indentation and return the
input unchanged when a refresh shifts their anchors, so a silently broken
patcher passes a green audit.

So a change here also needs the unit tests that exercise the patchers against
the current snapshot shape: that the intended replacement is present and the
stale form absent, that re-running is idempotent, and that a realistic
generated repository comes out right rather than only an isolated helper.
Treat an unexpected no-op as a failure, not a pass.

Establish the direction of travel before changing a pin — a refresh can be a
downgrade, and the version label alone does not tell you which.

`template_snapshots/` is vendored and refreshed as a unit. Reformatting it, or
hand-editing it to make a check pass, is a defect in this tier regardless of
what the check then says.

## Tier 4 — credentials and the release path

**Paths:** `.github/workflows/publish-image.yml`,
`.github/workflows/publish-wrapper.yml`,
`.github/workflows/update-homebrew-formula.yml`, `.github/workflows/ci.yml`,
`homebrew_formula.py`, `coverage_badge.py`, `Formula/`,
`.github/policies/workflow-permissions.json`, `.github/rulesets/**`,
`.claude/settings.json`, `.claude/hooks/`, anything touching signing or
cosign, and any workflow token that can write to the repository or its
packages

`ci.yml` is here for one job. `publish-coverage` holds `contents: write` and,
on every push to `main`, runs `coverage_badge.py` and pushes what it writes to
the `coverage-data` branch. That token is not scoped to that branch: the
ruleset keeps it off `main`, and nothing keeps it off `formula/<tag>` or any
other branch. Pointing the push elsewhere, or giving the job anything more to
do with the token, is a change in this tier, and so is `coverage_badge.py`,
for the same reason `homebrew_formula.py` is: it is what the job runs. The
`test` and `container-build` jobs in the same file hold `contents: read`, and
`test` is the required gate; a change to them reaches contributors, but it
shares a file with the job that does not, so it is reviewed here.

`publish-wrapper.yml` is here because it attaches the release-bound `aib` and
`aib.sha256` that the installation instructions download. The wrapper runs on
the user's host and can obtain their GitHub credential before it verifies the
container image, so changing its release path reaches users before the image's
own signature checks do.

`homebrew_formula.py` is here because it is what that workflow runs: its
`--update` path downloads the release tarball, computes the digest, and
rewrites the formula. A defect in it produces a permanently wrong formula just
as surely as editing `Formula/` by hand would.

`update-homebrew-formula.yml` pushes the formula change to a `formula/<tag>`
branch with its own `GITHUB_TOKEN`, which holds `contents: write` and
`issues: write`. Asking it for another permission, or pointing its push at a
branch other than `formula/<tag>`, is a change in this tier.

`.github/rulesets/**` is here because it is what keeps `main`, which
`publish-image.yml` signs and ships on every push, behind a pull request.
Loosening it reopens the direct push; see
[branch protection](branch-protection.md).

`.claude/settings.json` and `.claude/hooks/` are here because they are the
boundary an agent works inside, and the credentials this tier protects are
what that boundary keeps out of reach. The settings file's `deny` rows are
what stop a tool call reading `cosign.key` or `.env` or force-pushing, its
`allow` rows are what run without a prompt, and its `hooks` block is what
registers the `PreToolUse` gate at all. `.claude/hooks/gate_git_diff.py` is
that gate: it is what keeps the allow-listed `git diff`, `git log`,
`shellcheck` and `just --fmt --check` from reading past every `Read(...)`
deny rule. A widened allow row or a relaxed refusal is not read by someone who
then decides what to do; it is executed, unprompted, by the next agent that
runs here, including the one that proposed it. A green unit suite is not the
evidence for such a change either: the suite checks the settings file against
the hook and against [docs/SECURITY-AI.md](SECURITY-AI.md), and one pull
request can change all three, so green says the table is consistent, not that
widening it was safe. A change that only narrows the boundary -- a new
refusal, a removed allow row, a new `deny` row -- is still in this tier, and
its evidence is short: say what it now stops.

**Reaches:** the published image, the release-bound wrapper and its checksum,
and the Homebrew formula, which is what `brew upgrade` installs. Homebrew never
polls for releases; it reads the formula file and nothing else, so a formula
that is wrong stays wrong indefinitely rather than catching up.

**Evidence:** everything above, plus stating explicitly what the change can
affect and what it deliberately leaves alone. The order of a release is fixed
by a constraint rather than a preference —
[MAINTAINER.md](../maintainer_docs/MAINTAINER.md) has it.

Never weaken a check in this tier to make a build green. Fail closed and
report the blocker. [docs/SECURITY-AI.md](SECURITY-AI.md) covers the rest.

## Quick classification

| If the change touches                                                                                                                                                                             | Tier |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---- |
| only docs, tests, editor or agent briefs, lint and coverage configuration, issue forms, or the issue-tracker workflows                                                                            | 1    |
| the tool, the local `contrib/aib` wrapper source, or the image                                                                                                                                    | 2    |
| bundled snapshots, the pin tables, the workflow patchers and generators, or the maintenance audit                                                                                                 | 3    |
| publishing, signing, `homebrew_formula.py`, the formula, `ci.yml` and its coverage publisher, tokens that write to the repository, the `main` ruleset, or the agent permission table and its gate | 4    |

A change spanning tiers takes the highest one it touches. When it is not
obvious, the question that settles it is: *if this is wrong, who finds out,
and how?* A tier that only inconveniences a contributor is not a tier that
reaches a stranger's CI.
