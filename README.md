[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Danathar/atomic-image-builder)
[![Maintenance assisted by KubeStellar Hive](https://img.shields.io/badge/maintenance%20assisted%20by-KubeStellar%20Hive-1f6feb)](https://github.com/kubestellar/hive)
[![ACMM L4 Security-Aware](https://img.shields.io/badge/ACMM-L4%20Security--Aware-2da44e)](https://github.com/kubestellar/hive#acmm-levels)
[![Unit coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FDanathar%2Fatomic-image-builder%2Fcoverage-data%2Fcoverage-unit.json)](https://raw.githubusercontent.com/Danathar/atomic-image-builder/coverage-data/coverage-trend.csv)

# Atomic Image Builder

A guided terminal tool for creating and updating GitHub-backed bootc image repositories — for people who want a custom image without learning the full template and workflow setup first. (The author RECOMMENDS YOU LEARN anyway!) Works with [Universal Blue](https://universal-blue.org) and [Fedora Atomic desktops](https://fedoraproject.org/atomic-desktops/).

> [!TIP]
> **Safe to explore — it won't touch the system you're running on.** Everything happens on GitHub: it creates a new repo and lets GitHub Actions build your image. It never modifies, rebases, or removes packages from your current install. Switching your machine to the built image is a separate, deliberate step you take later.

> [!WARNING]
> **0.9 beta, not fully tested.** Review the changes it makes before applying them.

## Quick start

**Have Homebrew?** ([Bazzite](https://bazzite.gg), [Bluefin](https://projectbluefin.io), and [Aurora](https://getaurora.dev) ship with it.)

```bash
brew tap danathar/aib https://github.com/Danathar/atomic-image-builder
brew install danathar/aib/atomic-image-builder
aib-tool
```

**Have Podman?**

```bash
curl -fsSL https://raw.githubusercontent.com/Danathar/atomic-image-builder/main/contrib/aib -o ~/.local/bin/aib
chmod +x ~/.local/bin/aib
aib
```

Either one launches the guided menu. **YOU WILL NEED A GITHUB ACCOUNT** — if you are not already logged in, the tool walks you through `gh auth login` on first run.

Other options — plain `podman run`, distrobox, running from source — are in [Installing](docs/installing.md).

## What it does

Creates and maintains a **separate GitHub repository** that builds a custom bootc image for you through GitHub Actions. From the guided menu you can:

- Create a repo using either build method — **Containerfile** (from a bundled snapshot of [`ublue-os/image-template`](https://github.com/ublue-os/image-template)) or **BlueBuild** (from [`blue-build/template`](https://github.com/blue-build/template)).
- Add packages, COPR repos, systemd services, and base-package removals.
- Scan your running system and carry layered packages into a new repo.
- Update repos it created, view build status, and rotate the cosign signing key.
- Test-build a Containerfile image locally with Podman before pushing.

Supported bases: **[Universal Blue](https://universal-blue.org)** — [Bazzite](https://bazzite.gg) (also GNOME, DX, DX GNOME), [Aurora](https://getaurora.dev) (also DX), [Bluefin](https://projectbluefin.io) (also DX) — and **[Fedora Atomic](https://fedoraproject.org/atomic-desktops/)**: Silverblue, Kinoite, Sway, Budgie, COSMIC.

### What it does not do

- **Leaves the system you run it on alone** — no in-place changes, no automatic rebase, never removes layered packages from your current install.
- Does not adopt repos it did not create — a repo without `.atomic-image-builder.json` is not treated as managed.
- Local test builds are Containerfile-only.
- Advanced BlueBuild modules beyond the guided wizard are out of scope.

## Documentation

- [Installing](docs/installing.md) — every install path, container limitations, command-line options
- [Using the tool](docs/using.md) — the guided menu, migrating layered packages, Homebrew in your built image

## Who it's for

Beginner and intermediate atomic-desktop users who want a guided path to a custom image repo. Bootc desktops are powerful, but the normal setup assumes you are comfortable with image templates, GitHub Actions, signing, and image maintenance. This tool trades that setup cost for a guided workflow with stricter defaults — it is intentionally **not** aimed at exposing every advanced workflow.

## Feedback

Bugs, confusing behavior, and rough edges are all welcome: [open an issue](https://github.com/Danathar/atomic-image-builder/issues).

## About this project

> [!NOTE]
> This project was created with AI assistance and should be treated cautiously.
>
> This is a third-party tool. It is not an official Universal Blue utility, is not sanctioned by the Universal Blue project, is not an official Fedora utility, and is not sanctioned by the Fedora Project.
>
> It is provided as-is, without any promise that it will be safe for your repositories, data, systems, or build pipeline. Use it carefully, review its changes before applying them, and keep backups where appropriate. The maintainer is not responsible for repository damage, data loss, failed builds, system changes, or other consequences that may result from using this software.

> [!NOTE]
> **Maintenance on this repository is assisted by [KubeStellar Hive](https://github.com/kubestellar/hive) at ACMM level 4.**
>
> Hive orchestrates a fleet of AI agents that continuously review this codebase and publish what they find to a living [advisory report](https://github.com/Danathar/atomic-image-builder/issues/11).
>
> At **L4 (Security-Aware)** all agents may file issues, and the quality, security and CI agents may additionally open pull requests that carry a hold label. The rest stay advisory: they report, they do not act. Every change is still reviewed and merged by a human maintainer.
>
> Learn more: [KubeStellar](https://kubestellar.io) · [Hive](https://github.com/kubestellar/hive) · [Hive Hub](https://hive.kubestellar.io) · [full ACMM policy matrix](https://github.com/kubestellar/hive/blob/v4/src/docs/acmm-policy-matrix.md)

## License

GPL-3.0-only. See [LICENSE](LICENSE).
