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

> [!WARNING]
> **This is 0.9 beta and not fully tested.** Review the changes it makes before applying them, and do not assume every workflow or edge case has already been exercised.

## Quick start

The fastest way to try it is as a container — [Podman](https://podman.io/) is the only thing you need installed:

```bash
curl -fsSL https://raw.githubusercontent.com/Danathar/atomic-image-builder/main/contrib/aib -o ~/.local/bin/aib
chmod +x ~/.local/bin/aib
aib
```

This launches the guided menu, where you create a GitHub repo that builds your custom image. You will need a GitHub account; if you are not already logged in on the host, the tool walks you through `gh auth login` on first run.

Prefer to run from a source checkout instead? See [Run from source](#run-from-source). For the full set of container options (plain `podman run`, distrobox, and their limitations), see [Run with Podman](#run-with-podman-containerized).

## What it does

Atomic Image Builder creates and maintains a **separate GitHub repository** that builds a custom bootc image for you through GitHub Actions. It does not change your running system. From the guided menu you can:

- Create a new public GitHub repo for a custom image, using either build method:
  - **Containerfile** — a standard Containerfile and shell build script, generated from a bundled snapshot of [`ublue-os/image-template`](https://github.com/ublue-os/image-template).
  - **BlueBuild** — a YAML recipe and the BlueBuild GitHub Action, generated from a bundled snapshot of [`blue-build/template`](https://github.com/blue-build/template).
- Add packages, COPR repos, systemd services, and base-package removals.
- Include optional Homebrew integration on Fedora Atomic images (see [Homebrew on Fedora Atomic Images](#homebrew-on-fedora-atomic-images)).
- Update repos it previously created, and view their recent GitHub Actions build status.
- Rotate the cosign signing key for repos it created.
- Test-build a Containerfile image locally with Podman before pushing.
- Scan your running rpm-ostree / bootc system and carry layered packages into a new repo.

Generated images are rechunked with Chunkah by default for smaller, more resumable updates. The bundled template snapshots keep repo generation predictable — the upstream templates change rarely, but this tool pins its own copies rather than fetching them live.

Supported base images:

- **Universal Blue:** Bazzite, Bazzite GNOME, Bazzite DX, Bazzite DX GNOME, Aurora, Aurora DX, Bluefin, Bluefin DX
- **Fedora Atomic:** Silverblue, Kinoite, Sway Atomic, Budgie Atomic, COSMIC Atomic

### What it does not do

- Does not modify your currently running system in place, or rebase your machine automatically.
- Does not remove layered packages from your current install.
- Does not adopt existing repos that were not created by this tool — a repo without `.atomic-image-builder.json` is not treated as managed.

## Who it's for

Beginner and intermediate atomic-desktop users — on Universal Blue (Bazzite, Aurora, Bluefin) or Fedora Atomic (Silverblue, Kinoite, Sway, Budgie, COSMIC) — who want a guided path to a custom image repo that GitHub Actions builds automatically.

Bootc-based desktop images are powerful, but the normal setup path assumes you are comfortable with image templates, GitHub Actions, signing, and image maintenance. This tool trades that setup cost for a guided workflow with stricter defaults and guardrails. It is intentionally **not** aimed at exposing every advanced image workflow in the beginner UI.

## Run with Podman (containerized)

You can run the tool as a container — no local clone or dependency install needed, with `gum`, `git`, `gh`, `cosign`, and the `rpm-ostree` client all bundled in. Podman is the only prerequisite. To run from a source checkout instead, see [Run from source](#run-from-source).

### Using the wrapper script

```bash
curl -fsSL https://raw.githubusercontent.com/Danathar/atomic-image-builder/main/contrib/aib -o ~/.local/bin/aib
chmod +x ~/.local/bin/aib
aib
```

The wrapper forwards your host's `gh` login when there is one (otherwise it persists an in-container login across runs in a podman-managed volume), makes your host's `rpm-ostree` state available to the Scan OS menu, and mounts your local timezone. See the comments at the top of [`contrib/aib`](contrib/aib) for exactly what it mounts and why.

### Plain `podman run`

If you would rather not use the wrapper script:

```bash
podman run --rm -it \
  -e GH_TOKEN="$(gh auth token)" \
  ghcr.io/danathar/atomic-image-builder:latest
```

### Distrobox

[Distrobox](https://distrobox.it/) integrates the container with your host: it shares your home directory, so your host `gh` login is reused directly, and host system access, so the Scan OS menu can read the host's `rpm-ostree` state — no wrapper or manual mounts needed:

```bash
distrobox create --name aib --image ghcr.io/danathar/atomic-image-builder:latest
distrobox enter aib -- atomic-image-builder
```

### Limitations of running in a container

| Feature | Containerized (`podman run` or distrobox) |
|---|---|
| Create/update image repos, view build status, rotate signing key | Full fidelity |
| Scan OS & Migrate Layered Packages | Works via the `aib` wrapper or distrobox; unavailable with a bare `podman run` (no host state) |
| Local Podman test build | Not available — the option reports this and does nothing; see below |

The image includes `podman` only because `rpm-ostree` (a required dependency the tool checks for at startup) pulls it in transitively. A nested build inside the container is not supported, so the image tells the tool to make its "Test build locally (podman)" option show a clean "not available in this environment" message rather than attempt a build that would fail. Run the tool [from source](#run-from-source) if you need local test builds.

## Run from source

If you would rather run the script directly, you will need these on your host:

- Python 3.10 or newer
- `gum`, `git`, `gh`, and `cosign`
- `dnf5` (used for package-name validation) and `rpm-ostree` (used for system scanning)
- Optional: `podman`, for local Containerfile test builds

The app checks for the required tools at startup and exits if any are missing. On Universal Blue and Fedora Atomic desktop images, `dnf5` and `rpm-ostree` are already present; install the rest with Homebrew:

```bash
brew install gum git gh cosign
```

Then log in to GitHub, clone the repo, and run the tool:

```bash
gh auth login
git clone https://github.com/Danathar/atomic-image-builder.git
cd atomic-image-builder
./atomic_image_builder.py
```

If the script is not already executable on your system, make it executable once with `chmod +x atomic_image_builder.py`.

## Using the tool

However you launch it, the guided menu is the same. What to expect:

- The tool creates a public GitHub repo under your account, and GitHub Actions builds the image after creation. Scheduled rebuilds also run daily on GitHub.
- The main menu can show recent GitHub Actions build status for a configured repo.
- Containerfile repos can be test-built locally with Podman before you push — from a source checkout (see the [container limitations](#limitations-of-running-in-a-container) above).
- The update menu can rotate the repo's cosign signing key and update `cosign.pub`.
- The scan option reads your current rpm-ostree / bootc state and can carry layered packages into the new repo.

### Migrating layered packages from your current system

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

## Project scope

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
