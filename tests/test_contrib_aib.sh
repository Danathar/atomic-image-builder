#!/usr/bin/env bash
#
# Behavioral tests for contrib/aib. Plain bash, no test framework: aib itself
# has no other dependency, and neither does this. Each scenario stubs
# podman/gh/rpm-ostree as fake executables on PATH so the real ones are never
# invoked, then inspects what podman would have been called with.
#
# Run: bash tests/test_contrib_aib.sh

set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
aib="$repo_root/contrib/aib"

pass=0
fail=0
skip=0

# Each test gets its own stub bin/ dir and workdir, isolated from the others
# and from the real host tools. The stub dir is the *entire* PATH aib runs
# with, so a host that happens to have podman/gh/rpm-ostree installed (any
# atomic desktop, for one) cannot satisfy an absence test. Everything aib and
# the stubs genuinely need is symlinked in explicitly.
setup_stubs() {
    stub_dir="$(mktemp -d)"
    podman_log="$stub_dir/podman.log"

    local tool src
    for tool in bash env mktemp rm; do
        src="$(command -v "$tool")"
        ln -s "$src" "$stub_dir/$tool"
    done

    cat >"$stub_dir/podman" <<PODMAN
#!/usr/bin/env bash
printf '%s ' "\$@" > "$podman_log"
exit 0
PODMAN
    chmod +x "$stub_dir/podman"
}

cleanup_stubs() {
    rm -rf "$stub_dir"
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        echo "FAIL: $msg"
        echo "  expected to find: $needle"
        echo "  in: $haystack"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        echo "FAIL: $msg"
        echo "  expected NOT to find: $needle"
        echo "  in: $haystack"
    fi
}

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [[ "$actual" == "$expected" ]]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        echo "FAIL: $msg"
        echo "  expected: $expected"
        echo "  actual:   $actual"
    fi
}

# --- podman missing: exit 1, no podman invocation attempted ---------------
test_podman_missing() {
    setup_stubs
    rm -f "$stub_dir/podman"
    local out status
    out="$(PATH="$stub_dir" HOME="$stub_dir/home" "$aib" 2>&1)"
    status=$?
    assert_eq "$status" "1" "podman missing: exit status"
    assert_contains "$out" "podman is required but was not found on PATH" "podman missing: error message"
    cleanup_stubs
}

# --- gh authenticated: GH_TOKEN forwarded, no aib-gh volume ----------------
test_gh_authenticated() {
    setup_stubs
    cat >"$stub_dir/gh" <<'GH'
#!/usr/bin/env bash
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then
    exit 0
fi
if [ "$1" = "auth" ] && [ "$2" = "token" ]; then
    echo "fake-token-123"
    exit 0
fi
exit 1
GH
    chmod +x "$stub_dir/gh"
    PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1
    local args
    args="$(cat "$podman_log")"
    assert_contains "$args" "-e GH_TOKEN" "gh authenticated: GH_TOKEN forwarded"
    assert_not_contains "$args" "aib-gh:/root/.config/gh" "gh authenticated: aib-gh volume not mounted"
    cleanup_stubs
}

# --- gh missing: aib-gh named volume mounted instead -----------------------
test_gh_missing() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_contains "$args" "aib-gh:/root/.config/gh" "gh missing: aib-gh volume mounted"
    assert_not_contains "$args" "-e GH_TOKEN" "gh missing: GH_TOKEN not forwarded"
    cleanup_stubs
}

# --- gh present but not logged in: same fallback as gh missing ------------
test_gh_not_logged_in() {
    setup_stubs
    cat >"$stub_dir/gh" <<'GH'
#!/usr/bin/env bash
exit 1
GH
    chmod +x "$stub_dir/gh"
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_contains "$args" "aib-gh:/root/.config/gh" "gh not logged in: aib-gh volume mounted"
    assert_not_contains "$args" "-e GH_TOKEN" "gh not logged in: GH_TOKEN not forwarded"
    cleanup_stubs
}

# --- rpm-ostree present and succeeds: status file mounted read-only -------
test_rpm_ostree_success() {
    setup_stubs
    cat >"$stub_dir/rpm-ostree" <<'RPMOSTREE'
#!/usr/bin/env bash
echo '{"deployments": []}'
exit 0
RPMOSTREE
    chmod +x "$stub_dir/rpm-ostree"
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_contains "$args" "/run/aib-rpm-ostree-status.json:ro,Z" "rpm-ostree success: status file mounted read-only"
    assert_contains "$args" "AIB_RPM_OSTREE_STATUS_FILE=/run/aib-rpm-ostree-status.json" "rpm-ostree success: env var set"
    cleanup_stubs
}

# --- rpm-ostree present but its status call fails: silent fallback --------
test_rpm_ostree_failure() {
    setup_stubs
    cat >"$stub_dir/rpm-ostree" <<'RPMOSTREE'
#!/usr/bin/env bash
exit 1
RPMOSTREE
    chmod +x "$stub_dir/rpm-ostree"
    local args status
    PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1
    status=$?
    args="$(cat "$podman_log")"
    assert_not_contains "$args" "AIB_RPM_OSTREE_STATUS_FILE" "rpm-ostree failure: no status file mounted"
    assert_eq "$status" "0" "rpm-ostree failure: aib itself still succeeds (silent fallback)"
    cleanup_stubs
}

# --- rpm-ostree absent: no status-related args at all ----------------------
test_rpm_ostree_absent() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_not_contains "$args" "AIB_RPM_OSTREE_STATUS_FILE" "rpm-ostree absent: no status env var"
    assert_not_contains "$args" "aib-rpm-ostree-status.json" "rpm-ostree absent: no status file mount"
    cleanup_stubs
}

# --- dnf cache volume and image/args are always present -------------------
test_always_present_args() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" AIB_IMAGE=test/image:tag "$aib" foo --bar >/dev/null 2>&1; cat "$podman_log")"
    assert_contains "$args" "aib-dnf-cache:/var/cache/libdnf5" "always present: dnf cache volume"
    assert_contains "$args" "test/image:tag" "always present: custom AIB_IMAGE used"
    assert_contains "$args" "foo" "always present: extra args forwarded"
    assert_contains "$args" "--bar" "always present: extra args forwarded (flag)"
    cleanup_stubs
}

# --- default AIB_IMAGE when unset -------------------------------------------
test_default_image() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" env -u AIB_IMAGE "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_contains "$args" "ghcr.io/danathar/atomic-image-builder:latest" "default image: used when AIB_IMAGE unset"
    cleanup_stubs
}

# --- exit code from podman is preserved ------------------------------------
test_exit_code_preserved() {
    setup_stubs
    cat >"$stub_dir/podman" <<'PODMAN'
#!/usr/bin/env bash
exit 42
PODMAN
    chmod +x "$stub_dir/podman"
    local status
    PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1
    status=$?
    assert_eq "$status" "42" "exit code: podman's exit status is preserved"
    cleanup_stubs
}

# --- base podman flags are on every invocation ----------------------------
# --pull=newer is the one with teeth: podman's default (--pull=missing) runs
# whatever copy was fetched first, forever, and the image bakes in action pins
# and template snapshots, so a stale image quietly generates repos from stale
# pins. Dropping the flag is a silent regression, not a visible one.
test_base_podman_flags() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_contains "$args" "--pull=newer" "base flags: --pull=newer, so a stale image cannot run forever"
    assert_contains "$args" "--rm" "base flags: --rm, so the container is not left behind"
    assert_contains "$args" "-it" "base flags: -it, since the tool is an interactive TUI"
    cleanup_stubs
}

# --- host timezone is mounted read-only -----------------------------------
# The daily-rebuild note the tool prints is in local time; without this mount
# the container defaults to UTC. The mount is conditional on the host having
# /etc/localtime at all, and that path cannot be hidden from the script the
# way PATH hides a command, so assert whichever branch this host takes.
test_localtime_mount() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    if [ -e /etc/localtime ]; then
        assert_contains "$args" "/etc/localtime:/etc/localtime:ro" \
            "localtime: host zone mounted read-only when /etc/localtime exists"
    else
        assert_not_contains "$args" "/etc/localtime" \
            "localtime: nothing mounted when the host has no /etc/localtime"
    fi
    cleanup_stubs
}

# --- host without /etc/localtime: nothing is mounted ----------------------
# The scenario above asserts whichever branch this host takes, and every host
# that can run podman has /etc/localtime, so the false branch is never the one
# taken. That leaves the guard itself unasserted: delete the `if` and mount
# unconditionally, and the suite stays green while `podman run` fails outright
# on a host that has no /etc/localtime -- which is the only reason the guard
# is there. No PATH trick reaches an absolute path, but a mount namespace
# does: mask /etc and the guard genuinely sees no file.
#
# Unprivileged user namespaces are not available everywhere. Where they are
# missing this reports SKIP rather than passing, so the gap stays visible
# instead of being silently reclassified as evidence.
test_localtime_absent() {
    if ! command -v unshare >/dev/null 2>&1 ||
        ! unshare --map-root-user --mount true 2>/dev/null; then
        skip=$((skip + 1))
        echo "SKIP: localtime absent: unprivileged user namespaces unavailable"
        return
    fi
    setup_stubs
    # The masking runs in its own script so the namespace, not this shell,
    # expands the paths -- and so shellcheck reads it as the bash it is.
    cat >"$stub_dir/masked-run" <<'MASKED'
#!/usr/bin/env bash
masked="$(mktemp -d)"
mount --bind "$masked" /etc || exit 3
PATH="$AIB_STUB_DIR" HOME="$AIB_STUB_DIR/home" "$AIB_PATH" >/dev/null 2>&1
MASKED
    chmod +x "$stub_dir/masked-run"
    local args
    args="$(AIB_PATH="$aib" AIB_STUB_DIR="$stub_dir" \
        unshare --map-root-user --mount "$stub_dir/masked-run" >/dev/null 2>&1
        cat "$podman_log")"
    assert_not_contains "$args" "/etc/localtime" \
        "localtime: nothing mounted when the host has no /etc/localtime"
    cleanup_stubs
}

# --- rpm-ostree success: the EXIT trap removes the temp status file --------
# The mount arg proves the file was created; only this proves it was cleaned
# up. Without it, deleting `trap cleanup EXIT` leaves the suite green while
# every real run leaks a temp file into the user's TMPDIR.
test_rpm_ostree_status_file_removed_on_exit() {
    setup_stubs
    local tmp_dir="$stub_dir/tmp"
    mkdir -p "$tmp_dir"
    cat >"$stub_dir/rpm-ostree" <<'RPMOSTREE'
#!/usr/bin/env bash
echo '{"deployments": []}'
exit 0
RPMOSTREE
    chmod +x "$stub_dir/rpm-ostree"
    local args host_path leftover
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" TMPDIR="$tmp_dir" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    # Read the host path back out of the mount argument rather than guessing
    # what mktemp named it. Asserting it sits under our TMPDIR keeps the
    # leftover count below from passing vacuously if mktemp ignored TMPDIR.
    host_path="$(printf '%s\n' "$args" | tr ' ' '\n' | grep -F ':/run/aib-rpm-ostree-status.json:ro,Z' | cut -d: -f1)"
    assert_contains "$host_path" "$tmp_dir" "rpm-ostree success: status file was created under TMPDIR"
    leftover="$(find "$tmp_dir" -mindepth 1 | wc -l)"
    assert_eq "$leftover" "0" "rpm-ostree success: EXIT trap removes the temp status file"
    cleanup_stubs
}

# --- rpm-ostree failure: the fallback path removes the temp file too -------
# The redirect creates the file before rpm-ostree is even invoked, so the
# failure branch has its own `rm` and clears status_file, which leaves the
# EXIT trap with nothing to do. Deleting that `rm` leaks on every failed run.
test_rpm_ostree_failure_removes_temp_file() {
    setup_stubs
    local tmp_dir="$stub_dir/tmp"
    mkdir -p "$tmp_dir"
    cat >"$stub_dir/rpm-ostree" <<RPMOSTREE
#!/usr/bin/env bash
: >"$stub_dir/rpm-ostree.ran"
exit 1
RPMOSTREE
    chmod +x "$stub_dir/rpm-ostree"
    PATH="$stub_dir" HOME="$stub_dir/home" TMPDIR="$tmp_dir" "$aib" >/dev/null 2>&1
    local ran leftover
    ran="no"
    [ -e "$stub_dir/rpm-ostree.ran" ] && ran="yes"
    assert_eq "$ran" "yes" "rpm-ostree failure: the failing status call actually ran"
    leftover="$(find "$tmp_dir" -mindepth 1 | wc -l)"
    assert_eq "$leftover" "0" "rpm-ostree failure: temp status file removed on the fallback path"
    cleanup_stubs
}

test_podman_missing
test_gh_authenticated
test_gh_missing
test_gh_not_logged_in
test_rpm_ostree_success
test_rpm_ostree_failure
test_rpm_ostree_absent
test_rpm_ostree_status_file_removed_on_exit
test_rpm_ostree_failure_removes_temp_file
test_base_podman_flags
test_localtime_mount
test_localtime_absent
test_always_present_args
test_default_image
test_exit_code_preserved

echo
if [ "$skip" -gt 0 ]; then
    echo "$pass passed, $fail failed, $skip skipped"
else
    echo "$pass passed, $fail failed"
fi
[ "$fail" -eq 0 ]
