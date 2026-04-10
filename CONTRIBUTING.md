# Contributing

This document covers the development and maintenance workflows for working on Atomic Image Builder itself. End-user installation and usage live in [README.md](README.md).

## Tests

Run the test suite with `unittest`:

```bash
python3 -m unittest discover -s tests
```

## Linting

```bash
ruff check
```

## Maintainer Audit

This repo includes a small maintenance audit for the bundled template snapshot and workflow action pins.

Run the local-only checks without touching the network:

```bash
python3 maintenance_audit.py --skip-upstream
```

Run the full audit, including upstream HEAD drift checks against both bundled template snapshots (`ublue-os/image-template` and `blue-build/template`):

```bash
python3 maintenance_audit.py
```

Run the optional action-update audit when you want proactive pin-refresh signals for GitHub Actions used by generated repos:

```bash
python3 maintenance_audit.py --check-action-updates
```

The full audit also runs weekly and on demand through the repository workflow at `.github/workflows/maintenance-audit.yml`.
