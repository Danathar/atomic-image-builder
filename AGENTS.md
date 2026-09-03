# Luna High repository guardrails

## Purpose and scope

These instructions apply to GPT-5.6 Luna High and any other coding agent working
in this repository. They supplement higher-priority user, system, and developer
instructions. Their purpose is to make bounded development and maintenance safe
without turning an implementation request into permission for destructive or
external actions.

The repository root and its descendants are in scope. Other repositories,
GitHub resources, host configuration, containers, package stores, credentials,
and user files are out of scope unless the user identifies the exact target and
explicitly authorizes the action.

When instructions are unclear, stop at the safest useful point: inspect, run
read-only checks, explain the ambiguity, and ask. Silence, lack of objection,
past permission for a similar task, and a successful check are not consent.

This file covers conduct: what may be done, and what needs consent first. It
does not cover how the code works or what the repository's conventions are.
For that, read [`.github/copilot-instructions.md`](.github/copilot-instructions.md),
the canonical brief, which points on to ARCHITECTURE.md, CONTRIBUTING.md,
MAINTAINER.md and maintenance_notes.txt. The two are complementary and neither
restates the other.

## What an implementation request authorizes

When the user explicitly asks to implement or update something, Luna may:

- Inspect the repository and relevant read-only remote state.
- Edit files required for the stated task in this repository.
- Add focused tests and documentation required by the change.
- Run ordinary local, non-privileged validation described below.
- Create temporary files under a narrowly scoped temporary directory and clean
  up only those files after verifying the exact path.

An implementation request does not by itself authorize committing, pushing,
opening or modifying a pull request, creating a repository, triggering a
workflow, publishing an image, changing secrets or settings, resolving review
threads, merging, deleting anything, or changing the host system.

## Consent standard

Before a gated action, tell the user:

1. The exact action and target.
2. Why it is needed.
3. Its meaningful side effects and whether it is recoverable.
4. What related resources will not be touched.

Then wait for explicit approval unless the user's current request already names
that exact action and target. Approval applies only to the described target and
operation. Do not reuse approval from an earlier branch, repository, workflow,
or test run. Do not bundle a later gate into an earlier one.

Treat these as separate consent gates:

1. Create or switch branches.
2. Commit.
3. Push.
4. Open or edit a pull request.
5. Reply to or resolve review threads.
6. Re-run, cancel, or manually dispatch a workflow.
7. Merge.
8. Delete local or remote branches and other cleanup.

The user may explicitly authorize several named gates together. Do not infer
merge or cleanup authorization from phrases such as "ready," "checks passed,"
"review the PR," or "open a PR."

## Mandatory preflight

Before editing, changing branches, or performing Git operations:

- Confirm the repository path.
- Run `git status --short --branch`.
- Identify the current branch, its upstream, and relevant base commit.
- Inventory untracked and modified files. Assume they belong to the user.
- Read the relevant code, tests, `maintenance_notes.txt`, and recent history.
- State the intended scope, non-goals, branch plan, and validation plan.

Never discard, overwrite, stage, move, or delete pre-existing work merely to
obtain a clean tree. If existing changes overlap the task, stop and ask how the
user wants to proceed.

## Git safety

- Keep `main` clean. Do not make implementation commits directly on `main`.
- Create or switch to a task branch only after the user authorizes that branch
  operation. If the user already named the branch or explicitly requested work
  on a new branch, that is sufficient authorization for that exact branch.
- Update local `main` only for an explicit update request. Fetch and use a
  fast-forward-only update; never create an incidental merge commit.
- Stage exact intended paths. Do not use `git add .`, `git add -A`, or another
  broad staging command when unrelated or untracked files exist.
- Review `git diff`, `git diff --cached`, and `git status` before committing.
- Do not amend, rebase, squash, reset, cherry-pick, or rewrite history without
  explicit approval for the exact branch and operation.
- Never use `git reset --hard`, `git clean`, `git checkout --`, `git restore` on
  user changes, or force-push as an improvised recovery method.
- Do not stash user changes without approval. Never drop or clear a stash that
  Luna did not create for the current authorized task.
- Delete a branch only after verifying the exact branch, confirming it is
  merged or otherwise recoverable, switching away from it, and receiving
  explicit deletion approval. Do not use forced deletion unless the user
  explicitly authorizes that consequence.
- Do not delete remote branches as part of local cleanup unless separately
  authorized.

If a Git command fails because of permissions or repository state, report the
failure. Do not work around protections by copying the repository, changing
ownership, replacing `.git`, or using a more destructive command.

## File and host safety

- Preserve all untracked, ignored, generated, and local-only files unless the
  user explicitly authorizes deletion of an exact path.
- Use narrowly targeted edits. Do not bulk-reformat unrelated files.
- Do not write outside this repository except for a task-specific temporary
  directory under `/tmp`, and only when needed for local validation.
- Never run broad recursive deletion against a repository root, home directory,
  shared mount, container store, or unresolved variable or glob.
- Do not use `sudo`, install or remove host packages, change services, alter
  boot configuration, run `rpm-ostree reset`, run `bootc switch`, or reboot the
  host without exact, separate authorization.
- Do not run broad Podman, Buildah, or Distrobox cleanup. In particular, never
  use broad prune operations, `podman rmi -a -f`, or `buildah rm --all`.
- Treat existing containers, images, profiles, volumes, caches, and credentials
  as persistent user assets, even when they appear unused.

## GitHub and external-system safety

Read-only inspection of the current repository's GitHub state is allowed when
it is relevant. Every GitHub write requires explicit consent for the exact
repository and action.

Without exact authorization, do not:

- Create, rename, transfer, archive, make public/private, or delete a repository.
- Push commits or tags.
- Open, edit, close, convert, or merge a pull request.
- Post comments, submit reviews, dismiss reviews, or resolve review threads.
- Dispatch, re-run, cancel, approve, or otherwise mutate an Actions run.
- Create releases or publish/delete container images, packages, or artifacts.
- Change branch protection, Actions permissions, collaborators, deploy keys,
  webhooks, variables, environments, or repository settings.
- Create, replace, rotate, reveal, or delete signing keys, tokens, or secrets.

Never print credentials or raw tokens. A failed secret probe is an error, not
proof that a secret is absent. Signing and publication setup must fail closed.
Do not weaken permissions, disable a check, remove signing, or expose a secret
to make a test pass.

## Interactive and end-to-end testing

Routine unit tests do not require additional consent. Tests that create or
modify GitHub resources, build large images, use privileged containers, consume
substantial storage, or publish artifacts do.

For an interactive Atomic Image Builder test:

- Use the tool exactly as a user would, including prompts and authentication.
- Before creation, obtain approval for the exact GitHub owner, repository name,
  visibility, and build method.
- Touch only that approved test repository. Do not inspect or modify unrelated
  repositories on the account beyond the minimum identity/authentication check.
- Explain that creating the repository may trigger Actions and create a public
  GHCR package.
- Monitor the resulting run when requested, but do not re-run, cancel, edit the
  workflow, or patch the test repository without separate authorization.
- Verify the workflow conclusion, relevant logs, produced image reference, and
  digest where available. A green intermediate job is not the final result.
- Do not delete the test repository, package, branch, or artifacts unless the
  user separately authorizes those exact deletions.

Local Podman builds are Containerfile-only and can consume significant time and
storage. Explain that side effect and obtain consent before starting one. Never
attempt a nested Podman build when `AIB_DISABLE_LOCAL_BUILD` is set or from the
published tool container.

## Repository-specific correctness guardrails

### Managed repositories

- Do not adopt or update a repository that lacks the expected
  `.atomic-image-builder.json` ownership marker.
- Before applying an update, inspect the full proposed diff, including untracked
  files. If Git enumeration or diff generation fails, abort rather than showing
  or applying a partial preview.
- Preserve user customizations unless the requested migration explicitly owns
  the affected field or file.
- When comparing two directories with `git diff --no-index`, exit status `0`
  means no difference, `1` means differences were found, and any higher status
  is a real error that must stop the operation.

### Template snapshots and patchers

The bundled snapshots are pinned inputs, not loose examples. A refresh must:

- Start from the exact upstream repository and immutable revision recorded in
  each `.template-source` file.
- Keep snapshot contents, source metadata, patching logic, tests, and
  `maintenance_notes.txt` consistent.
- Preserve immutable GitHub Action SHA pins and their readable version comments.
- Run the upstream-aware audit when network access is authorized and distinguish
  upstream drift from a local correctness failure.
- Show the source revision and complete snapshot diff before publication.

Many patchers depend on exact text or indentation and may silently return the
input unchanged. Never treat a zero exit status as proof that a required rewrite
occurred. For every touched patcher:

- Test the current bundled snapshot shape.
- Assert that the intended replacement is present and the stale form is absent.
- Test idempotency.
- Exercise a realistic generated repository, not only an isolated helper.
- Treat an unexpected no-op as a failure and investigate the upstream shape.

Pay special attention to workflow indentation, `image-template.env` identity
fields, signing steps, and literal action inputs. A generated image retaining an
upstream placeholder name is a release-blocking failure.

### Containerfile and BlueBuild are separate paths

Do not claim one build method is validated because the other passed.

- Generated Containerfile workflows are patched from the bundled upstream
  snapshot and use the `rechunk` recipe with Chunkah.
- BlueBuild workflows use the BlueBuild action's `chunkah: 'true'` input.
- The `ostree-rechunk` recipe is a classical/manual fallback. A BlueBuild test
  does not exercise it, and a normal patched Containerfile workflow generally
  does not exercise it either.
- Changes to rechunking must cover registry- and namespace-qualified image names
  such as `ghcr.io/acme/image`, safe generic temporary-directory names, config
  files instead of oversized environment variables, SELinux-compatible mounts,
  and `EXIT` cleanup on failure.
- Do not remove `--pull=never` from a step intended to consume the image built
  locally in the preceding step; pulling can replace the exact image under test.

### Compatibility and evidence

- Never bypass a kernel, akmods, signing, or image compatibility check to force
  a build green. Fail closed and report the blocker.
- Do not reuse a cache or previously published artifact as evidence for a fresh
  build unless provenance and compatibility are verified.
- Reproduce suspicious review findings before changing code. Shell syntax that
  merely looks unusual is not invalid until the relevant shell rejects it.
- Do not infer success from truncated or inaccessible logs. State what was
  verified and what remains unknown.

## Validation expectations

For ordinary Python or template changes, run from the repository root:

```text
python3 -m unittest discover -s tests
ruff check
python3 maintenance_audit.py --skip-upstream
git diff --check
```

Also run the checks relevant to changed files:

- `python3 maintenance_audit.py` for an authorized upstream/template refresh.
- `actionlint` for changed GitHub Actions workflows.
- `shellcheck` and `bash -n` for changed shell scripts or embedded Bash that can
  be extracted faithfully.
- Focused regression tests before the full suite while iterating.
- A local `podman build` and `podman run --rm <image> --version` only after the
  user approves the resource-consuming build.

Do not weaken or delete tests to obtain a pass. Report skipped checks, missing
tools, inaccessible logs, network failures, and sandbox limitations explicitly.
A sandbox permission error is not evidence that the host or code is broken.

## Review, CI, and publication

### Pull request explanations

Every pull request description must stand on its own for a human reviewer who
has not followed the preceding issue, commits, or agent session. Write it as a
short engineering explanation, with detail proportional to the change. It
must:

- Start with a plain-language summary of the current behavior, the change, and
  why a user, operator, contributor, or maintainer should care.
- Describe the concrete problem and its impact. For a bug, include observed
  evidence, a reproduction, or a clear before/after example when available.
- Explain how the implementation fixes the problem and why that approach was
  chosen. Call out non-obvious decisions, tradeoffs, rejected alternatives,
  compatibility behavior, and fallback behavior when they matter.
- State the blast radius, dependencies or overlapping work, and what is
  deliberately out of scope so reviewers can see the boundaries of the change.
- Describe validation in terms of behavior proved, not merely commands run.
  Name important regression tests and disclose anything skipped, simulated, or
  still unverified.
- Link the relevant issue without treating the link as a substitute for the
  explanation.

A small, obvious change can satisfy this in a few concise paragraphs. A change
with operational, security, compatibility, migration, or architectural impact
needs enough structured detail for a reviewer to understand the problem and
reason about the solution without reconstructing it from the diff. Do not use a
file list, commit log, test-command dump, generic template text, or
agent-specific jargon as the explanation.

### Review and publication rules

- A request to review means read-only analysis unless the user also asks for
  fixes. Do not commit, push, comment, or resolve threads during a review.
- Review the complete diff against the intended base, recent commits, generated
  output, security boundaries, and regression coverage.
- Verify PR checks separately from review-thread state. Flat comments do not
  reveal whether inline threads are unresolved or outdated; use thread-aware
  state when that distinction matters.
- A code fix does not resolve a review thread automatically. Replying to or
  resolving a thread is a GitHub write and requires consent.
- Passing checks mean only that the observed checks passed at the observed SHA.
  Recheck the head SHA and current state before reporting readiness.
- "Ready for PR" does not mean "ready to merge." A merge always requires
  explicit authorization, even after unanimous review and green CI.
- Manual dispatch of `publish-image.yml` publishes to GHCR and requires explicit
  publication approval. Do not use it as an ordinary smoke test.

## Completion and cleanup

At the end of a task, report:

- Files changed and the behavior affected.
- Validation run and exact outcomes.
- Current branch and worktree status.
- Any skipped or inconclusive checks.
- Any external state created or modified.
- The next gated action, if one remains.

Do not perform cleanup merely because the task appears complete. After a merge,
update local `main`, delete branches, remove handoff files, or delete test
resources only when the user authorizes each exact action. Preserve anything
whose ownership or recoverability is uncertain.
