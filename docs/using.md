# Using the tool

However you launch it, the guided menu is the same.

## Which entry to pick

**Create Image From This System** is the normal one. It reads your running
system's image reference and layered packages, so it already knows which base
image you are on — it never asks you to choose one. That matters: Universal Blue
images are not rebase-compatible with each other, so an image built on the wrong
base is one you cannot switch to.

It works whether or not you have layered anything. With no layered packages it
simply starts you from your current base, and you add what you want from there —
packages, COPR repositories, systemd services, base-package removals.

**Create Image From Scratch** is for the case where there is no system to read:
you have not installed an atomic desktop yet, or you are building an image for a
different machine. This is the one place you pick a base image from a list. The
generated repo also builds installable media, so you can install directly from
your own image.

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
