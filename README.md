[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Danathar/atomic-image-builder)

# Atomic Image Builder

Beta terminal tool for creating and updating GitHub-backed bootc image repositories.

This project is a guided terminal app for people who want a custom image repo without learning the full upstream template and workflow setup first. It supports both curated Universal Blue desktop images and the official Fedora Atomic desktop images.

> [!NOTE]
> This project was created with AI assistance and should be treated cautiously.
>
> This is a third-party tool. It is not an official Universal Blue utility, is not sanctioned by the Universal Blue project, is not an official Fedora utility, and is not sanctioned by the Fedora Project.
>
> This project is provided as-is, without any promise that it will be safe for your repositories, data, systems, or build pipeline. Use it carefully, review its changes before applying them, and keep backups where appropriate. The maintainer is not responsible for repository damage, data loss, failed builds, system changes, or other consequences that may result from using this software.

## Status

This project is currently **0.8 beta** and is **not fully tested yet**. Use it carefully, review the changes it makes, and do not assume every workflow or edge case has already been exercised.

## What This Is

This tool creates and maintains a GitHub repository that builds a custom bootc image from a curated supported base image.

Supported base images currently include:

- Universal Blue: Bazzite, Bazzite GNOME, Bazzite DX, Bazzite DX GNOME, Aurora, Aurora DX, Bluefin, and Bluefin DX
- Fedora Atomic desktops: Silverblue, Kinoite, Sway Atomic, Budgie Atomic, and COSMIC Atomic

It supports two build methods:

- **Containerfile** — Uses a standard Containerfile and shell build script. Generated repos start from a bundled snapshot of the official `ublue-os/image-template` repository: https://github.com/ublue-os/image-template
- **BlueBuild** — Uses a YAML recipe and the BlueBuild GitHub Action. Generated repos start from a bundled snapshot of the official `blue-build/template` repository: https://github.com/blue-build/template

Both upstream templates work across this tool's supported Universal Blue and Fedora Atomic images. They do not change very often, but this utility still uses bundled snapshots so repo generation stays predictable.

## What It Does

- Creates a new public GitHub repo for a custom bootc image
- Supports curated Universal Blue desktop images
- Supports the official Fedora Atomic desktop images
- Lets users choose between Containerfile and BlueBuild build methods
- Writes the repo files needed for a GitHub Actions build
- Lets users add packages, COPR repos, services, and base-package removals
- Offers optional Homebrew integration for Fedora Atomic base images using the Universal Blue brew OCI layer
- Updates repos that were previously created by this tool
- Shows recent GitHub Actions build status for a configured image repo
- Can run a local Podman test build for Containerfile-based images before pushing
- Can rotate the cosign signing key for repos created by this tool
- Can scan a running rpm-ostree / bootc system and carry layered packages into a new image repo

## What It Does Not Do

- Does not modify your currently running system in place
- Does not rebase your machine automatically
- Does not remove layered packages from your current install
- Does not adopt arbitrary existing repos that were not created by this tool

It creates and manages a separate GitHub repository that builds your custom image through GitHub Actions.

## Why It Exists

Bootc-based desktop images are powerful, but the normal setup path assumes users are comfortable with image templates, GitHub Actions, signing, and image maintenance.

This project exists to reduce that setup cost for newer users by turning the common path into a guided terminal workflow with stricter defaults and guardrails.

## Who It Is For

This is for:

- beginner and intermediate desktop-atomic users
- Universal Blue users who want a custom repo on GitHub
- Fedora Atomic desktop users who want a custom repo on GitHub
- Bazzite, Aurora, Bluefin, Silverblue, Kinoite, Sway Atomic, Budgie Atomic, and COSMIC Atomic users who want a guided path
- people who want GitHub Actions to build their image automatically

This is not aimed at:

- people who want every advanced image workflow exposed in the beginner UI

## Requirements

You need:

- Python 3.10 or newer
- `gum`
- `git`
- `gh`
- `cosign`
- `dnf5` (used for package-name validation)
- `rpm-ostree` (used for system scanning)

The app checks all required tools at startup and exits if any are missing.

On supported Universal Blue and Fedora Atomic desktop images, `dnf5` and `rpm-ostree` are expected to already be present. If helper CLI tools such as `gum`, `git`, `gh`, or `cosign` are missing, install them with Homebrew.

Optional:

- `podman` for local Containerfile test builds

You also need a GitHub account and should log in first:

```bash
gh auth login
```

On Universal Blue and Fedora Atomic desktop systems, missing CLI tools are typically installed with Homebrew:

```bash
brew install gum git gh cosign
```

## Installation

Clone this repo locally and enter the project directory:

```bash
git clone https://github.com/Danathar/atomic-image-builder.git
cd atomic-image-builder
```

If the script is not already executable on your system, make it executable once:

```bash
chmod +x atomic_image_builder.py
```

## Usage

Run the beginner app:

```bash
./atomic_image_builder.py
```

What to expect:

- The tool creates a public GitHub repo under your account
- GitHub Actions builds the image for you after repo creation
- Scheduled rebuilds also run daily on GitHub
- The main menu can show recent GitHub Actions build status for a configured repo
- Containerfile repos can be test-built locally with Podman before you push changes
- The update menu can rotate the repo's cosign signing key and update `cosign.pub`
- The scan option reads your current rpm-ostree / bootc state and can carry layered packages into the new repo

If you use the scan flow to carry layered packages from your current system into the new image, run these in the same session before rebooting:

```bash
sudo rpm-ostree reset
sudo bootc switch ghcr.io/<your-user>/<your-repo>:latest
systemctl reboot
```

That clears the old layered package state from the current deployment before you switch to the image-based version of those changes. You do not need to reboot between `rpm-ostree reset` and `bootc switch`.

## Homebrew on Fedora Atomic Images

Universal Blue images ship with Homebrew (brew) already integrated. Fedora Atomic images do not.

When you choose a Fedora Atomic base image (Silverblue, Kinoite, etc.), the tool offers to include Homebrew using the Universal Blue brew OCI layer (`ghcr.io/ublue-os/brew:latest`). This adds:

- The Homebrew installation and shell integration files
- `brew-setup.service` for first-boot initialization
- `brew-update.timer` and `brew-upgrade.timer` for automatic maintenance

This option is skipped automatically for Universal Blue base images since they already include Homebrew. You can also toggle it later through the update menu.

## Project Scope

This repo intentionally keeps the beginner tool narrow:

- Containerfile and BlueBuild repo creation and updates are supported
- Local test builds are currently Containerfile-only
- Existing repos that do not contain `.atomic-image-builder.json` are not supported for adoption or import
- Advanced BlueBuild modules and features beyond the guided wizard are out of scope

## Feedback

If you test this and hit bugs, confusing behavior, or rough edges, please open an issue:

https://github.com/Danathar/atomic-image-builder/issues

## Contributing

Development workflows (tests, linting, maintainer audit) are documented in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

GPL-3.0-only. See [LICENSE](LICENSE).
