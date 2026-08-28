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

# Each test gets its own stub bin/ dir and workdir, isolated from the others
# and from the real host tools.
setup_stubs() {
    stub_dir="$(mktemp -d)"
    podman_log="$stub_dir/podman.log"

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
    out="$(PATH="$stub_dir:/usr/bin:/bin" HOME="$stub_dir/home" "$aib" 2>&1)"
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
    PATH="$stub_dir:/usr/bin:/bin" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1
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
    args="$(PATH="$stub_dir:/usr/bin:/bin" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
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
    args="$(PATH="$stub_dir:/usr/bin:/bin" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
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
    args="$(PATH="$stub_dir:/usr/bin:/bin" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
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
    args="$(PATH="$stub_dir:/usr/bin:/bin" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    status=$?
    assert_not_contains "$args" "AIB_RPM_OSTREE_STATUS_FILE" "rpm-ostree failure: no status file mounted"
    assert_eq "$status" "0" "rpm-ostree failure: aib itself still succeeds (silent fallback)"
    cleanup_stubs
}

# --- rpm-ostree absent: no status-related args at all ----------------------
test_rpm_ostree_absent() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir:/usr/bin:/bin" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_not_contains "$args" "AIB_RPM_OSTREE_STATUS_FILE" "rpm-ostree absent: no status env var"
    assert_not_contains "$args" "aib-rpm-ostree-status.json" "rpm-ostree absent: no status file mount"
    cleanup_stubs
}

# --- dnf cache volume and image/args are always present -------------------
test_always_present_args() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir:/usr/bin:/bin" HOME="$stub_dir/home" AIB_IMAGE=test/image:tag "$aib" foo --bar >/dev/null 2>&1; cat "$podman_log")"
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
    args="$(PATH="$stub_dir:/usr/bin:/bin" HOME="$stub_dir/home" env -u AIB_IMAGE "$aib" >/dev/null 2>&1; cat "$podman_log")"
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
    PATH="$stub_dir:/usr/bin:/bin" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1
    status=$?
    assert_eq "$status" "42" "exit code: podman's exit status is preserved"
    cleanup_stubs
}

test_podman_missing
test_gh_authenticated
test_gh_missing
test_gh_not_logged_in
test_rpm_ostree_success
test_rpm_ostree_failure
test_rpm_ostree_absent
test_always_present_args
test_default_image
test_exit_code_preserved

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
