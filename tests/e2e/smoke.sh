#!/usr/bin/env bash
#
# Behavioural end-to-end tests for the packaged container image.
#
# Every non-interactive path the packaged entrypoint has. The guided wizard
# needs a TTY, so the only other end-to-end reachable behaviour is the
# preflight failure, which is checked last.
#
# Usage: tests/e2e/smoke.sh [IMAGE]
#
# IMAGE defaults to aib-local, the tag CONTRIBUTING.md's local build produces.
# CI passes its own tag. See tests/e2e/README.md.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tests/e2e/lib.sh
source "$here/lib.sh"

image="${1:-aib-local}"
e2e_require_local_image "$image"

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

echo "e2e: smoke testing $image"

# --version and its short form both name the command, which is how the image
# and the Homebrew formula are known to agree on it.
podman run --rm "$image" --version | grep -F "aib-tool"
podman run --rm "$image" -V | grep -F "aib-tool"
podman run --rm "$image" --help | grep -F -- "--version"

# The image exposes the same command name the Homebrew formula does.
podman run --rm --entrypoint aib-tool "$image" --version | grep -F "aib-tool"

# With gum missing the tool must degrade to the plain-text preflight failure
# and exit 1, not block on a prompt.
no_gum="$(e2e_masking_file "$scratch")"
status=0
output="$(podman run --rm -v "$no_gum:/usr/bin/gum:$E2E_MOUNT_SUFFIX" "$image")" || status=$?
if [ "$status" -ne 1 ]; then
    e2e_fail "preflight with gum masked exited $status, expected 1"
fi
printf '%s\n' "$output" | grep -F "Preflight Failed"

echo "e2e: smoke tests passed"
