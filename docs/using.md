# Using the tool

However you launch it, the guided menu is the same.

## Creating an image

**Create Image** scans your running system first. It reads the image you are on
and any packages you have layered, so it already knows the base — it never asks
you to choose one. That matters: Universal Blue images are not rebase-compatible
with each other, so an image built on the wrong base is one you cannot switch to.

It works whether or not you have layered anything. With nothing layered it
simply starts from your current base, and you add what you want from there —
packages, COPR repositories, systemd services, base-package removals.

The main menu lists every base image the tool supports, so you can see up front
whether your system is one of them.

**If the scan cannot run**, the tool says so and offers to let you pick a base
image by hand instead. That happens with a bare `podman run`, which has no
access to your host's state — the `aib` wrapper and distrobox both hand it in,
so neither hits this. See
[container limitations](installing.md#limitations-of-running-in-a-container).

## What else to expect

- The tool creates a public GitHub repo under your account, and GitHub Actions
  builds the image after creation. Scheduled rebuilds also run daily on GitHub.
- The main menu can show recent GitHub Actions build status for a configured repo.
- Containerfile repos can be test-built locally with Podman before you push —
  from a source checkout (see
  [container limitations](installing.md#limitations-of-running-in-a-container)).
- The update menu can rotate the repo's cosign signing key and update `cosign.pub`.

## Migrating layered packages from your current system

If you use the scan flow to carry layered packages from your current system into
the new image, run these in the same session before rebooting:

```bash
sudo rpm-ostree reset
sudo bootc switch ghcr.io/<your-user>/<your-repo>:latest
systemctl reboot
```

That clears the old layered package state from the current deployment before you
switch to the image-based version of those changes. You do not need to reboot
between `rpm-ostree reset` and `bootc switch`.

## Homebrew on Fedora Atomic images

This is about adding Homebrew to the image you **build** — not about installing
this tool with Homebrew, which is covered in
[Installing](installing.md#homebrew).

Universal Blue images ship with Homebrew (brew) already integrated. Fedora Atomic
images do not.

When you choose a Fedora Atomic base image (Silverblue, Kinoite, etc.), the tool
offers to include Homebrew using the Universal Blue brew OCI layer
(`ghcr.io/ublue-os/brew:latest`). This adds:

- The Homebrew installation and shell integration files
- `brew-setup.service` for first-boot initialization
- `brew-update.timer` and `brew-upgrade.timer` for automatic maintenance

This option is skipped automatically for Universal Blue base images since they
already include Homebrew. You can also toggle it later through the update menu.
