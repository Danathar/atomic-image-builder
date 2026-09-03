#!/usr/bin/env bash
#
# End-to-end coverage collection for the packaged container image.
#
# Runs the same non-interactive paths smoke.sh asserts on, but against the
# coverage-instrumented image built from container/Containerfile.coverage, so
# what the packaged entrypoint actually executes is measured rather than
# inferred from the unit suite's view of the source tree.
#
# This is a measurement, not a second set of assertions: each scenario checks
# only that the exit status is the expected one, because a scenario that
# crashed early would still write a data file and quietly understate coverage.
# The behaviour itself is smoke.sh's job.
#
# Usage: tests/e2e/coverage_scenarios.sh [IMAGE] [DATA_DIR]
#
# IMAGE defaults to aib-local-cov and DATA_DIR to e2e-coverage/data, which is
# where CONTRIBUTING.md's `coverage combine --rcfile=.coveragerc.e2e` looks.
# See tests/e2e/README.md.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tests/e2e/lib.sh
source "$here/lib.sh"

image="${1:-aib-local-cov}"
data_dir="${2:-e2e-coverage/data}"

e2e_require_local_image "$image"
mkdir -p "$data_dir"
data_dir="$(cd "$data_dir" && pwd)"

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
no_gum="$(e2e_masking_file "$scratch")"

# Each scenario writes its own data file, named after the scenario, so the
# artifact shows which lines a given invocation reached rather than only the
# union of all of them.
scenario() {
    local name="$1"
    local expected_status="$2"
    shift 2
    local status=0
    podman run --rm \
        -e COVERAGE_FILE="/cov/.coverage.${name}" \
        -v "$data_dir:/cov:z" \
        "$@" || status=$?
    if [ "$status" -ne "$expected_status" ]; then
        e2e_fail "scenario '$name' exited $status, expected $expected_status"
    fi
}

echo "e2e: collecting coverage from $image into $data_dir"

scenario version 0 "$image" --version
scenario version-short 0 "$image" -V
scenario help 0 "$image" --help
scenario preflight-no-gum 1 -v "$no_gum:/usr/bin/gum:$E2E_MOUNT_SUFFIX" "$image"

echo "e2e: coverage data written to $data_dir"
