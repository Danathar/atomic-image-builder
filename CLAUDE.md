# Atomic Image Builder

Guided CLI tool for creating GitHub-backed bootc image repositories for Universal Blue and Fedora Atomic desktops.

## Architecture

Single-module Python app (`atomic_image_builder.py`, ~3800 lines) with three main classes:

- **App** — Main wizard orchestrating GitHub operations, file generation, and TUI interaction
- **Config** — Dataclass holding all user choices; serialized to `.ublue-builder.json` as the repo's source of truth
- **Gum** — Wrapper around the `gum` CLI for terminal UI (choosers, inputs, spinners, styling)

Supporting file: `maintenance_audit.py` audits bundled template snapshots for upstream drift and action pin staleness.

## Key Design Decisions

- **Single module intentionally.** The comment at line 22 explains this. A future refactor could split it, but the current layout is a pragmatic choice for a solo-author beta.
- **Bundled template snapshots** (`template_snapshots/`) are pinned to specific upstream SHAs rather than cloned at runtime. This makes repo generation deterministic.
- **Config is the source of truth.** Generated files (Containerfile, workflow YAML, README, build.sh, recipe.yml) are always derived from the Config/state file, never parsed back.
- **Layered input validation.** User input is checked by regex at entry, validated by `validate_config()` at the boundary, and shell-quoted by `shlex.quote()` in generated scripts.

## Development

Run tests:
```bash
python3 -m unittest discover -s tests
```

Run linter:
```bash
ruff check
```

Run maintenance audit (local only):
```bash
python3 maintenance_audit.py --skip-upstream
```

## Build Methods

The tool supports two build methods that generate different repo structures:

- **Containerfile** — Standard `Containerfile` + `build_files/build.sh`, built with `buildah-build` action. Template from `ublue-os/image-template`.
- **BlueBuild** — YAML `recipes/recipe.yml`, built with `blue-build/github-action`. Template from `blue-build/template`.

## Testing

215 tests in `tests/`, all using `unittest`. The `GumStub` class in `test_ublue_builder.py` provides the test double for the Gum TUI wrapper. Tests mock subprocess calls and GitHub operations — no real network or GitHub access needed.

## Important Patterns

- `run()` (line ~522) is the central subprocess dispatcher. No `shell=True` anywhere.
- `ScreenBack` exception is used for Esc/back navigation in the wizard.
- `CommandError` exception is used for user-facing operational errors.
- `STATE_FILE` (`.ublue-builder.json`) gates whether a repo can be updated — the tool refuses to operate on repos it didn't create.
- Action SHAs are pinned in `ACTION_PINS` / `ACTION_REF_PINS` dicts and verified by the maintenance audit.
