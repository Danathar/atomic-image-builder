# Risk tiers

How to tell how far a change here can reach. The tiers are about **blast
radius**, not size: a one-line diff sits in the highest tier and a 300-line
documentation rewrite sits in the lowest.

Use this to decide how much evidence a change needs, and what a reviewer
should look at first. [docs/review-rubric.md](review-rubric.md) is what review
checks; this is how much of it applies.

## Tier 1 — this repository only

**Paths:** `*.md`, `docs/`, `maintainer_docs/`, `tests/`, `.editorconfig`

**Reaches:** contributors and maintainers. Nothing users run.

**Evidence:** the unit suite. A documentation change still runs it, because
several tests read the documents and fail when one drifts from what it
describes.

Note `tests/e2e/` is Tier 1 by content but triggers the container build, since
a change to a suite that never runs the suite reads as covered.

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
tables, and the generated-output writers in `atomic_image_builder.py` — the
`patch_*_workflow` and `generate_*_workflow` functions and the project writers
around them. Those live in the tool but their output is other people's CI, so
they belong here rather than in Tier 2.

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
`.github/workflows/update-homebrew-formula.yml`, `homebrew_formula.py`,
`Formula/`, anything touching signing, `GH_TOKEN`, or cosign

`homebrew_formula.py` is here because it is what that workflow runs: its
`--update` path downloads the release tarball, computes the digest, and
rewrites the formula. A defect in it produces a permanently wrong formula just
as surely as editing `Formula/` by hand would.

**Reaches:** the published image and the Homebrew formula, which is what
`brew upgrade` installs. Homebrew never polls for releases; it reads the
formula file and nothing else, so a formula that is wrong stays wrong
indefinitely rather than catching up.

**Evidence:** everything above, plus stating explicitly what the change can
affect and what it deliberately leaves alone. The order of a release is fixed
by a constraint rather than a preference —
[MAINTAINER.md](../maintainer_docs/MAINTAINER.md) has it.

Never weaken a check in this tier to make a build green. Fail closed and
report the blocker. [docs/SECURITY-AI.md](SECURITY-AI.md) covers the rest.

## Quick classification

| If the change touches | Tier |
|---|---|
| only docs, tests, or editor config | 1 |
| the tool, the wrapper, or the image | 2 |
| bundled snapshots, the pin tables, or the workflow patchers and generators | 3 |
| publishing, signing, `homebrew_formula.py`, the formula, or tokens | 4 |

A change spanning tiers takes the highest one it touches. When it is not
obvious, the question that settles it is: *if this is wrong, who finds out,
and how?* A tier that only inconveniences a contributor is not a tier that
reaches a stranger's CI.
