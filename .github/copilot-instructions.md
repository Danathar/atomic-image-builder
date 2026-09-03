# Working in this repository

Guidance for coding agents. It is deliberately short and mostly points
elsewhere: the reference material already exists, and a second copy of it
would drift from the first.

- [ARCHITECTURE.md](../ARCHITECTURE.md) maps `atomic_image_builder.py`.
- [CONTRIBUTING.md](../CONTRIBUTING.md) has the checks, the five coverage
  tiers, and how to submit a change.
- [maintainer_docs/MAINTAINER.md](../maintainer_docs/MAINTAINER.md) has the
  release process, what the automation does, and the traps.
- [maintenance_notes.txt](../maintenance_notes.txt) has the operational
  knowledge that does not fit in code comments.

Read the relevant one before changing anything it covers. What follows is only
the part an agent gets wrong *before* it thinks to go looking.

## What this is

A guided terminal tool that turns an atomic desktop's existing `rpm-ostree`
customizations into a GitHub-backed bootc image repo. The tool is one file,
`atomic_image_builder.py`, on purpose. See ARCHITECTURE.md's *Why one file*
before proposing a split.

It writes to **GitHub**, never to the running system. A change that makes the
tool modify, rebase, or layer onto the host is out of scope by design, not an
oversight.

## Before you push

```bash
python3 -m unittest discover -s tests
python3 -m coverage run -m unittest discover -s tests
python3 -m coverage report --fail-under="$(jq -er '.gated.unit' .coverage-thresholds.json)"
ruff check
shellcheck -x contrib/aib container/entrypoint.sh tests/test_contrib_aib.sh tests/test_entrypoint.sh tests/e2e/*.sh
tests/test_contrib_aib.sh
tests/test_entrypoint.sh
hadolint Containerfile container/Containerfile.coverage
python3 maintenance_audit.py --skip-upstream
```

`.coveragerc` sets no `fail_under`, so a bare `coverage report` exits 0 however
far coverage has fallen -- read the gate from the JSON as above, the way
`ci.yml` does, rather than writing the number. `shellcheck` is static analysis
only; the behavioral assertions for the two shell entrypoints are in the
harnesses listed after it.

Pinned tool versions are in CONTRIBUTING.md's *Tests* section, and match what
CI installs. An unpinned local `ruff` disagrees with CI in both directions.

## Things that are easy to get wrong here

**The coverage threshold is not a literal.** It lives in
`.coverage-thresholds.json`; `ci.yml` reads it with `jq`. Writing
`--fail-under=<number>` into the workflow fails a test, because a literal
silently wins over the file it reads. If you change the gate, change that file
and run the suite — a doc still quoting the old number fails too.

**Every `uses:` in a workflow must be covered by `ACTION_PINS` or
`ACTION_REF_PINS`** in `atomic_image_builder.py`, or `maintenance_audit.py`
fails the build. Those tables ship to generated repos, so a linter action this
repo alone would run does not belong in them; pin the binary by version and
checksum instead, the way `ci.yml` does with hadolint.

**Never rename `TOOL_SLUG`.** It feeds `STATE_FILE`
(`.atomic-image-builder.json`), which is written into every managed repo and is
how the tool recognises repos it created. Renaming it orphans all of them.
`TOOL_COMMAND` is the separate constant that names the command, and is safe.

**The template patchers are indentation-sensitive and fail silently.** The
`patch_workflow_*` and `ensure_workflow_job_env_entries` helpers match exact
text from the bundled snapshots. When a snapshot refresh shifts their anchors
they no-op rather than erroring. See maintenance_notes.txt.

**`template_snapshots/` is vendored.** It is a pinned copy of upstream
(`ublue-os/image-template`, `blue-build/template`). Do not reformat it, fix its
lint, or hand-edit it to make a check pass. It is refreshed as a unit, and the
weekly audit tracks its drift.

**Keep Bash array pushes one per line.** Bashcov attributes a hit only to the
line a statement starts on, so a multi-line `podman_args+=(...)` reads as
uncovered however well it is tested. See CONTRIBUTING.md and #118.

**The end-to-end suites live in `tests/e2e/`** and are run by `ci.yml`, not
duplicated in it. `tests/e2e/` is a `container-build` path trigger, so a change
to a suite actually runs the job that exercises it. Add a scenario to both
scripts, not just the coverage one — `tests/e2e/README.md` says why.

**A low maintenance-audit coverage percentage is the expected reading**, not a
gap to close. Driving it up would mean manufacturing the live failures the tier
exists to observe. CONTRIBUTING.md's *Coverage* section explains this at
length; #123 filed the percentage as a finding and #124 is the right answer to
it. Do not file it again.

**Lint config is `ruff.toml`, undotted.** ruff prefers `.ruff.toml` if both
exist, so adding the dotted variant silently orphans the real one. A test
asserts only one exists.

## Style

Match the surrounding code. This repo comments the *why* — the trap avoided,
the alternative rejected — rather than restating what a line does, and its
prose commits to a position instead of hedging. A change that adds a
convention nobody asked for is a change reviewers have to undo.

`.editorconfig` carries the mechanical part: indent width, line endings, final
newline. Markdown tables are aligned for a fixed-width reader, because that is
where these documents are actually read -- a terminal, a pager, a diff. A test
enforces it, so run `python3 format_markdown_tables.py` rather than padding
cells by hand.

## Mechanical limits

`.claude/settings.json` is committed and shared. It is the part of this
document a tool enforces rather than asks you to remember, in three layers:

- **allow** the repo's own read-only gate, so running the checks does not cost
  a prompt every time.
- **ask** before anything outward-facing: a push, a PR, a release, a workflow
  dispatch, an image build.
- **deny** outright what has no legitimate use here: force-push, hard reset,
  broad Podman or Buildah cleanup, repo deletion, anything that rebases the
  host, and reading a signing key or `.env`.

The deny list is not advice. Treat a refusal as the answer rather than
something to route around, and if a rule blocks work that is genuinely needed,
change the rule in a reviewed commit instead of working past it.

`.claude/settings.local.json` stays gitignored. That is where personal
allowances belong, and nothing in it is shared or reviewed.

## Other agent entry points

This file is the canonical one. Any other agent-facing file in this repo --
Cursor rules, a prompt catalog, packaged skills -- points back here instead of
restating it, so there is one place to correct when something above turns out
to be wrong. A test enforces that for every such file that exists.

`CLAUDE.md` is the single exception, and it is deliberate. It is auto-loaded
into a Claude Code session while this file is not, so a trap that lives only
here costs a tool call an agent may not spend -- which is the wrong place to
economise for the handful whose consequences land in other people's
repositories. It mirrors four of the paragraphs above inside a marked block,
verbatim. The same test inverts inside that block and requires them to match,
so editing a mirrored paragraph here without updating the mirror fails the
suite. Correct it here; the failure will tell you the mirror is stale.
