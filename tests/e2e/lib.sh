#!/usr/bin/env bash
# Shared helpers for the end-to-end suites in this directory.
#
# Sourced, never executed. Callers set `set -euo pipefail` themselves so a
# failure inside a helper stops the suite rather than being reported and
# stepped over.

# Bind-mount option for every host path these suites hand to podman.
#
# `z` relabels the source for container access, which an SELinux host --
# every atomic desktop this tool targets -- requires and where a bare `ro`
# mount is denied outright. It is a no-op where SELinux is off, including the
# ubuntu-24.04 runner: the end-to-end coverage mount in ci.yml has always
# passed `:z` there without trouble.
# shellcheck disable=SC2034  # read by the suites that source this file.
E2E_MOUNT_SUFFIX="ro,z"

# Path to an empty, non-executable file for masking a binary inside the image.
#
# The tool's command_exists() goes through shutil.which, which requires the
# executable bit, so a plain empty file mounted over a binary is enough to
# make the tool see it as absent. Written once per run into a temp directory
# the caller is expected to clean up.
e2e_masking_file() {
    local scratch="$1"
    local path="$scratch/masked-binary"
    : > "$path"
    printf '%s\n' "$path"
}

# Fail with a message on stderr.
e2e_fail() {
    printf 'e2e: %s\n' "$*" >&2
    exit 1
}

# Refuse to run against an image tag that does not exist locally, rather than
# letting podman try to pull something from a registry under that name.
e2e_require_local_image() {
    local image="$1"
    podman image exists "$image" ||
        e2e_fail "image '$image' not found locally. See tests/e2e/README.md for the build commands."
}
