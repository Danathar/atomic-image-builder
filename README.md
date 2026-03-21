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

- Universal Blue: Bazzite, Bazzite DX, Aurora, Aurora DX, Bluefin, and Bluefin DX
- Fedora Atomic desktops: Silverblue, Kinoite, Sway Atomic, Budgie Atomic, and COSMIC Atomic

It currently focuses on the beginner-friendly Containerfile path. Generated repos start from a bundled snapshot of the official `ublue-os/image-template` repository:

https://github.com/ublue-os/image-template

That upstream template works across this tool's supported Universal Blue and Fedora Atomic images. It does not change very often, but this utility still uses a bundled snapshot so repo generation stays predictable.

## What It Does

- Creates a new public GitHub repo for a custom bootc image
- Supports curated Universal Blue desktop images
- Supports the official Fedora Atomic desktop images
- Writes the repo files needed for a GitHub Actions build
- Lets users add packages, COPR repos, services, and base-package removals
- Updates repos that were previously created by this tool
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

- people who want full BlueBuild support in the same tool
- people who want every advanced image workflow exposed in the beginner UI

## Requirements

You need:

- Python 3.10 or newer
- `gum`
- `git`
- `gh`
- `cosign`
- `dnf5` for manual package-name checks
- `rpm-ostree`

The app checks the required helper CLI tools at startup and exits if they are missing.

On supported Universal Blue and Fedora Atomic desktop images, core host tools like `dnf5` and `rpm-ostree` are expected to already be present. If helper CLI tools such as `gum`, `git`, `gh`, or `cosign` are missing, install them with Homebrew.

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
- The scan option reads your current rpm-ostree / bootc state and can carry layered packages into the new repo

The legacy `./ublue_builder.py` entrypoint still exists as a compatibility shim, but `./atomic_image_builder.py` is now the primary command.

If you use the scan flow to carry layered packages from your current system into the new image, run this before switching to the newly built image:

```bash
sudo rpm-ostree reset
```

That clears the old layered package state from the current deployment before you switch to the image-based version of those changes.

## Project Scope

This repo intentionally keeps the beginner tool narrow:

- Containerfile-based repo creation and updates are supported
- Existing repos that do not contain `.ublue-builder.json` are not supported for adoption or import
- BlueBuild support was removed from the beginner app to keep the UX and code simpler
- If a BlueBuild-focused workflow is needed later, it should live in a separate tool

## Feedback

If you test this and hit bugs, confusing behavior, or rough edges, please open an issue:

https://github.com/Danathar/atomic-image-builder/issues

## License

GPL-3.0-only. See [LICENSE](LICENSE).
