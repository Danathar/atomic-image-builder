# Architecture

An orientation map for anyone about to change the code. Development workflows —
tests, coverage, linting, how to submit a change — are in
[CONTRIBUTING.md](CONTRIBUTING.md). Release procedure and the traps worth
knowing are in [MAINTAINER.md](maintainer_docs/MAINTAINER.md).

This describes where things live, not line numbers, which go stale. Every name
below is greppable.

## Repository map

| Path | What it is |
|---|---|
| `atomic_image_builder.py` | The whole tool. One module, standard library only. |
| `template_snapshots/` | Pinned copies of `ublue-os/image-template` and `blue-build/template`. Inputs, not examples — each carries a `.template-source` recording its upstream revision. |
| `maintenance_audit.py` | Weekly consistency check: snapshot drift, action pin coverage and freshness, formula pin. |
| `snapshot_drift_issue.py` | Keeps the audit's sub-threshold drift tracking issue in sync. |
| `homebrew_formula.py` | Points `Formula/atomic-image-builder.rb` at a release and verifies the pin. |
| `coverage_badge.py` | Writes the coverage badge endpoint and trend CSV. |
| `contrib/aib` | Host-side wrapper that runs the published container image. |
| `container/entrypoint.sh` | Entrypoint baked into that image. |
| `tests/` | `unittest` suites; the shell entrypoints have their own `.sh` harnesses. |
| `maintenance_notes.txt` | Operational knowledge that outlives any one change — read it before touching the patchers or base-image detection. |

## Why one file

The tool is distributed three ways — Homebrew, a container, and a bare source
checkout — and depends on nothing outside the standard library, so a single
executable module can be copied anywhere and run. Splitting it into a package
would buy structure at the cost of that property. The file's own header comment
says the same thing and invites a future split; this document is the interim
map.

## The runtime model

Everything in the file serves one of five stages:

1. Collect the user's choices into a `Config`.
2. Validate and normalize that `Config`.
3. Write a canonical state file (`.atomic-image-builder.json`) so later updates
   read structured data instead of re-parsing generated output.
4. Render a GitHub repo from a pinned template snapshot plus generated files.
5. Let GitHub Actions build and sign the image.

Stage 3 is the load-bearing one. It is why the tool can update a repo it made
months earlier, and why a repo without that state file is never adopted.

## Inside `atomic_image_builder.py`

Regions, in file order:

| Region | Contents |
|---|---|
| Constants | `VERSION`, `TOOL_SLUG`/`STATE_FILE` identity, UI colors and widths, template repo paths, `ACTION_PINS` and `ACTION_REF_PINS`, the validation regexes. |
| Base images | `BaseImage`, the `BASE_IMAGES` catalog, and `determine_fedora_atomic_default_tag()`, which reads the host's `/etc/os-release` and falls back to `FEDORA_ATOMIC_FALLBACK_TAG`. |
| `Config` | The dataclass every screen writes into, plus `config_from_state_payload()` and the `validate_string_list`/`string_list`/`unique` helpers that read it back from a state file. |
| Small helpers | Slug and repo-name validation, YAML scalar quoting, image-reference normalization, `ghcr_package_exists()`, `command_exists()`. |
| Action pinning | `pin_action_uses_line()` and `pinned_action()`, which rewrite `uses:` lines against the pin tables. |
| Workflow patchers (module level) | `patch_workflow_steps()` and the `workflow_key`/`workflow_block_key` line classifiers underneath it, then `patch_workflow_signing_steps()`, `patch_cosign_compatibility()`, and `ensure_workflow_job_env_entries()`. |
| Process plumbing | `CommandError`, `ScreenBack` (raised to pop back a screen), and `run()`. |
| `Gum` | Thin wrapper over the `gum` CLI: `choose`, `filter`, `input`, `confirm`, `write`, `pager`, `table`, `spinner*`, plus the styling and ANSI-fallback layer. Every prompt in the tool goes through here. |
| `App` | The application. See below. |
| Entry point | `usage_text()` and `main()`. |

### The `App` class

~130 methods, grouped by what they do rather than by where they sit:

| Group | Representative methods |
|---|---|
| Startup and preflight | `startup_requirements`, `preflight`, `render_preflight_failure`, `require_github`, `github_setup_guide` |
| Menus and navigation | `main_menu`, `create_image`, `create_new_image`, `update_menu`, `run_screen_action`, `review_new_image` |
| Wizard screens | `choose_method`, `choose_base_image`, `configure_repo`, `select_packages`, `search_packages`, `add_copr`, `add_services`, `view_selections` |
| System scan | `scan_os`, `match_base_image`, `carried_scan_customizations`, `scanned_image_is_managed` |
| Host package queries | `lookup_host_packages`, `search_host_packages`, `refresh_package_metadata`, `dnf5_state_dir` |
| GitHub operations | `gh_json`, `select_repo`, `repo_file_exists`, `batch_check_state_files`, `repo_default_branch`, `do_build`, `push_update`, `render_build_status` |
| Signing | `generate_and_upload_signing_key`, `ensure_signing_ready`, `rotate_signing_key` |
| Config and state | `load_repo_config`, `validate_config`, `state_payload`, `add_packages_to_config` |
| Template patchers | `patch_container_workflow`, `patch_bluebuild_workflow`, `patch_container_justfile`, `patch_image_template_env`, `patch_container_rechunk_step`, `patch_bluebuild_action_inputs`, `patch_installer_config` |
| Generators | `generate_containerfile`, `generate_recipe`, `generate_container_workflow`, `generate_build_sh`, `generate_readme` |
| Project writers | `write_project_files` and its `write_container_project_files` / `write_bluebuild_project_files` halves, `copy_template_snapshot`, `seed_project_template` |

The patcher and generator groups are the ones to be careful with. A patcher
edits text copied from a pinned upstream snapshot; a generator writes a file the
tool owns outright. Patchers match on upstream's literal text and indentation
and **return the input unchanged when they do not match**, by design — so a
template refresh that shifts an anchor produces a silent no-op, not an error.
`maintenance_notes.txt` covers which anchors matter and what breaks when they
move.

## Where to start

| To change | Look at |
|---|---|
| A prompt's wording or behavior | The `Gum` method it calls, or the wizard screen in `App` |
| What a generated repo contains | The `generate_*` and `write_*_project_files` methods |
| How a bundled template is adapted | The `patch_*` methods, and `template_snapshots/` for the input they match against |
| Which Fedora version an un-detectable host gets | `FEDORA_ATOMIC_FALLBACK_TAG` and `determine_fedora_atomic_default_tag()` |
| A pinned GitHub Action | `ACTION_PINS` / `ACTION_REF_PINS`, **and** the snapshot workflow under `template_snapshots/` — the audit fails if they disagree |
| What the tool remembers about a repo | `Config`, `state_payload()`, `config_from_state_payload()` |

Two build methods run through most of this, and passing one proves nothing
about the other. Containerfile repos are patched from the `ublue-os` snapshot;
BlueBuild repos are patched from the `blue-build` one. Test the path you
changed.

The patcher and generator tests in `tests/test_atomic_image_builder.py` run
against the real bundled snapshots, which is what catches a silent no-op. Keep
it that way.
