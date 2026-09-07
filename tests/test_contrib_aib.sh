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

# The stub dir isolates aib's PATH; this isolates its environment, for the
# same reason. aib does `GH_TOKEN="$(gh auth token)"` and then `export
# GH_TOKEN`, and bash keeps a variable exported if it was already in the
# environment -- so on a host that exports GH_TOKEN (anyone using `gh` via a
# token rather than a login, and most CI), the assignment alone leaves the
# variable exported and dropping the `export` is invisible. The scenario
# below that checks the token reaches podman's environment would pass on a
# wrapper that no longer exports it.
unset GH_TOKEN

# Each test gets its own stub bin/ dir and workdir, isolated from the others
# and from the real host tools. The stub dir is the *entire* PATH aib runs
# with, so a host that happens to have podman/gh/rpm-ostree installed (any
# atomic desktop, for one) cannot satisfy an absence test. Everything aib and
# the stubs genuinely need is symlinked in explicitly.
setup_stubs() {
    stub_dir="$(mktemp -d)"
    podman_log="$stub_dir/podman.log"
    # What podman inherits, as opposed to what it is passed. `-e GH_TOKEN`
    # forwards a variable by name, so the argv log alone cannot tell a working
    # forward from one that passes an empty value; this records the value the
    # stub actually received.
    podman_env_log="$stub_dir/podman-env.log"
    cosign_log="$stub_dir/cosign.log"
    # A fixed, well-formed digest so scenarios can assert the exact reference
    # that reaches podman run rather than merely that one is present.
    test_digest="sha256:1111111111111111111111111111111111111111111111111111111111111111"

    local tool src
    for tool in bash env mktemp rm; do
        src="$(command -v "$tool")"
        ln -s "$src" "$stub_dir/$tool"
    done

    # aib now calls podman three times on the verified path -- pull, image
    # inspect, run -- so only `run` is logged; the others would overwrite it.
    # The digest answered here is what the cosign stub is expected to be asked
    # about, which is how the "runs the digest it verified" scenario can tell
    # a real hand-off from a coincidence.
    cat >"$stub_dir/podman" <<PODMAN
#!/usr/bin/env bash
if [ "\$1" = "pull" ]; then
    exit \${AIB_TEST_PULL_STATUS:-0}
fi
if [ "\$1" = "image" ] && [ "\$2" = "inspect" ]; then
    printf '%s' "\${AIB_TEST_DIGEST-$test_digest}"
    exit 0
fi
printf '%s ' "\$@" > "$podman_log"
printf '%s' "\${GH_TOKEN-<unset>}" > "$podman_env_log"
exit 0
PODMAN
    chmod +x "$stub_dir/podman"

    # A cosign that verifies anything, and records what it was asked to
    # verify. Absent by definition on a host without cosign, which is its own
    # scenario below.
    cat >"$stub_dir/cosign" <<COSIGN
#!/usr/bin/env bash
printf '%s ' "\$@" > "$cosign_log"
exit \${AIB_TEST_COSIGN_STATUS:-0}
COSIGN
    chmod +x "$stub_dir/cosign"
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
# --- a digest-shaped value that is not a digest is refused -----------------
# The check is "exactly a sha256 digest", not "starts with sha256:". A
# truncated or padded value could resolve differently for cosign and for
# podman, which would mean verifying one image and running another.
test_malformed_digest_refuses_to_run() {
    setup_stubs
    local out status
    out="$(PATH="$stub_dir" HOME="$stub_dir/home" AIB_TEST_DIGEST="sha256:abc" "$aib" 2>&1)"
    status=$?
    assert_eq "$status" "1" "short digest: exit status"
    assert_contains "$out" "could not determine the digest" "short digest: says what failed"
    assert_eq "$(cat "$podman_log" 2>/dev/null)" "" "short digest: podman run never happens"
    out="$(PATH="$stub_dir" HOME="$stub_dir/home" AIB_TEST_DIGEST="${test_digest}extra" "$aib" 2>&1)"
    assert_contains "$out" "could not determine the digest" "over-long digest: refused too"
    cleanup_stubs
}

# --- signature verification ------------------------------------------------
# The wrapper pulls and executes a mutable tag on every run, as root in the
# container, with the host's GitHub token forwarded in. The published image is
# signed, but until #243 nothing on this path checked the signature, so it
# protected nobody. These assert the check exists, that it is tied to the
# digest actually run, and that every way of not verifying is deliberate.

# --- the digest cosign verified is the digest podman runs ------------------
# The substitution is the whole fix. Verifying the tag and then letting podman
# resolve it again leaves a window for the tag to move in between, so this
# asserts cosign and podman were handed the same reference rather than merely
# that both were called.
test_verify_runs_the_digest_it_verified() {
    setup_stubs
    PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1
    local verified ran
    verified="$(cat "$cosign_log")"
    ran="$(cat "$podman_log")"
    assert_contains "$verified" "ghcr.io/danathar/atomic-image-builder@$test_digest" "verify: cosign is given the resolved digest"
    assert_contains "$ran" "ghcr.io/danathar/atomic-image-builder@$test_digest" "verify: podman runs the digest that was verified"
    assert_not_contains "$ran" "atomic-image-builder:latest" "verify: the mutable tag is not what gets run"
    cleanup_stubs
}

# --- the identity constraints are the ones the image is signed with --------
# A cosign verify with no identity constraints accepts any valid Sigstore
# signature from anyone, which looks like verification and is not.
test_verify_uses_the_publisher_identity() {
    setup_stubs
    PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1
    local verified
    verified="$(cat "$cosign_log")"
    assert_contains "$verified" "--certificate-identity-regexp" "verify: constrains the certificate identity"
    assert_contains "$verified" "publish-image" "verify: identity names the publishing workflow"
    assert_contains "$verified" "refs/(heads/main|tags/.+)" "verify: identity accepts main and release tags"
    assert_contains "$verified" "--certificate-oidc-issuer" "verify: constrains the OIDC issuer"
    assert_contains "$verified" "https://token.actions.githubusercontent.com" "verify: issuer is GitHub Actions"
    cleanup_stubs
}

# --- a failed verification aborts, and runs nothing ------------------------
# The one case where continuing is the wrong default: a signature that does
# not check out is exactly when the image should not execute with the user's
# GitHub token.
test_verify_failure_refuses_to_run() {
    setup_stubs
    local out status
    out="$(PATH="$stub_dir" HOME="$stub_dir/home" AIB_TEST_COSIGN_STATUS=1 "$aib" 2>&1)"
    status=$?
    assert_eq "$status" "1" "verify failure: exit status"
    assert_contains "$out" "SIGNATURE VERIFICATION FAILED" "verify failure: says so plainly"
    assert_contains "$out" "refusing to run" "verify failure: refuses rather than warns"
    assert_eq "$(cat "$podman_log" 2>/dev/null)" "" "verify failure: podman run never happens"
    cleanup_stubs
}

# --- cosign missing: fail closed, and say how to proceed -------------------
test_verify_requires_cosign() {
    setup_stubs
    rm -f "$stub_dir/cosign"
    local out status
    out="$(PATH="$stub_dir" HOME="$stub_dir/home" "$aib" 2>&1)"
    status=$?
    assert_eq "$status" "1" "cosign missing: exit status"
    assert_contains "$out" "cosign is required" "cosign missing: names what is missing"
    assert_contains "$out" "AIB_SKIP_VERIFY=1" "cosign missing: names the escape hatch"
    assert_eq "$(cat "$podman_log" 2>/dev/null)" "" "cosign missing: podman run never happens"
    cleanup_stubs
}

# --- AIB_SKIP_VERIFY: runs, but says loudly that it did not verify ---------
# The escape hatch exists for offline runs and hosts without cosign. Silence
# would make it the path of least resistance; the warning is what keeps it a
# decision.
test_skip_verify_warns_and_runs() {
    setup_stubs
    rm -f "$stub_dir/cosign"
    local out args
    out="$(PATH="$stub_dir" HOME="$stub_dir/home" AIB_SKIP_VERIFY=1 "$aib" 2>&1)"
    args="$(cat "$podman_log")"
    assert_contains "$out" "WARNING" "skip verify: warns"
    assert_contains "$out" "without" "skip verify: says verification did not happen"
    assert_contains "$args" "run" "skip verify: still runs the image"
    assert_eq "$(cat "$cosign_log" 2>/dev/null)" "" "skip verify: cosign is not consulted"
    cleanup_stubs
}

# --- a pull failure is a verification failure, not a fallback --------------
# This is also how an offline run arrives. Failing closed and naming the
# escape hatch is the chosen behaviour; degrading to an unverified run would
# make the check disappear exactly when the registry is unreachable.
test_pull_failure_refuses_to_run() {
    setup_stubs
    local out status
    out="$(PATH="$stub_dir" HOME="$stub_dir/home" AIB_TEST_PULL_STATUS=1 "$aib" 2>&1)"
    status=$?
    assert_eq "$status" "1" "pull failure: exit status"
    assert_contains "$out" "could not pull" "pull failure: says what failed"
    assert_contains "$out" "AIB_SKIP_VERIFY=1" "pull failure: names the offline escape hatch"
    assert_eq "$(cat "$podman_log" 2>/dev/null)" "" "pull failure: podman run never happens"
    cleanup_stubs
}

# --- a user's own image is not forced through our identity -----------------
# Someone running their own build cannot satisfy this repository's certificate
# identity, so verifying theirs would fail every time and the only way out
# would be to disable verification entirely.
test_custom_image_is_not_verified() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" AIB_IMAGE="localhost/my-own-build:dev" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_contains "$args" "localhost/my-own-build:dev" "custom image: run as given"
    assert_eq "$(cat "$cosign_log" 2>/dev/null)" "" "custom image: cosign is not consulted"
    cleanup_stubs
}

# --- a release tag of the published repo IS verified ----------------------
# The identity regexp covers refs/tags/, so pinning to a release is a
# supported, verified way to run -- not a way around the check.
test_published_release_tag_is_verified() {
    setup_stubs
    PATH="$stub_dir" HOME="$stub_dir/home" AIB_IMAGE="ghcr.io/danathar/atomic-image-builder:v0.9.5" "$aib" >/dev/null 2>&1
    assert_contains "$(cat "$cosign_log")" "ghcr.io/danathar/atomic-image-builder@$test_digest" "release tag: verified like latest"
    cleanup_stubs
}

# --- a digest that is not a digest is refused ------------------------------
# The digest is interpolated into the reference cosign and podman are given.
# Anything that is not a sha256 digest must stop the run rather than be
# concatenated into a reference.
test_unparseable_digest_refuses_to_run() {
    setup_stubs
    local out status
    out="$(PATH="$stub_dir" HOME="$stub_dir/home" AIB_TEST_DIGEST="" "$aib" 2>&1)"
    status=$?
    assert_eq "$status" "1" "bad digest: exit status"
    assert_contains "$out" "could not determine the digest" "bad digest: says what failed"
    assert_eq "$(cat "$podman_log" 2>/dev/null)" "" "bad digest: podman run never happens"
    cleanup_stubs
}

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

# --- the token's value goes to podman's environment, never its argv --------
# The header comment's claim for `-e GH_TOKEN` is that the value "is never
# placed in the Podman command line or written to disk by this script", and
# nothing asserted it. `-e GH_TOKEN` forwards the variable by name out of the
# wrapper's exported environment; `-e "GH_TOKEN=$GH_TOKEN"` forwards the same
# value, satisfies the `-e GH_TOKEN` assertion above just as well, and puts
# the token in podman's argv where any local user can read it out of `ps`.
# The two halves need separate assertions: the value must be absent from the
# command line, and present in the environment podman inherits. Dropping the
# `export` leaves the argv assertions passing while the container gets an
# unset variable and silently falls back to no GitHub auth at all.
test_gh_token_forwarded_by_environment_not_argv() {
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
    # `env -u` rather than relying on the unset at the top of the file: this
    # scenario is the one that breaks silently if GH_TOKEN is in the
    # environment, so it states the precondition itself rather than
    # inheriting it from twelve screens away.
    PATH="$stub_dir" HOME="$stub_dir/home" env -u GH_TOKEN "$aib" >/dev/null 2>&1
    local args forwarded
    args="$(cat "$podman_log")"
    forwarded="$(cat "$podman_env_log")"
    assert_contains "$args" "-e GH_TOKEN " "gh token: forwarded as a bare -e GH_TOKEN, with no value attached"
    assert_not_contains "$args" "fake-token-123" "gh token: the value never reaches podman's command line"
    assert_eq "$forwarded" "fake-token-123" "gh token: the value reaches podman through the exported environment"
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
# The default resolves to the published repo, and reaches podman as the digest
# cosign verified rather than as the mutable tag -- see the verification
# scenarios below for why that substitution is the point rather than a detail.
test_default_image() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" env -u AIB_IMAGE "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_contains "$args" "ghcr.io/danathar/atomic-image-builder@$test_digest" "default image: used when AIB_IMAGE unset"
    cleanup_stubs
}

# --- exit code from podman is preserved ------------------------------------
test_exit_code_preserved() {
    setup_stubs
    # Only `run` fails. A stub that failed every subcommand would abort aib at
    # the pull step instead, and the 42 would never come from where this
    # scenario claims it does.
    cat >"$stub_dir/podman" <<PODMAN
#!/usr/bin/env bash
if [ "\$1" = "pull" ]; then
    exit 0
fi
if [ "\$1" = "image" ] && [ "\$2" = "inspect" ]; then
    printf '%s' "$test_digest"
    exit 0
fi
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
test_base_podman_flags() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_contains "$args" "--rm" "base flags: --rm, so the container is not left behind"
    assert_contains "$args" "-it" "base flags: -it, since the tool is an interactive TUI"
    cleanup_stubs
}

# --- staleness: the verified path pulls explicitly instead of --pull=newer --
# --pull=newer is the flag with teeth on the unverified paths: podman's
# default (--pull=missing) runs whatever copy was fetched first, forever, and
# the image bakes in action pins and template snapshots, so a stale image
# quietly generates repos from stale pins. The verified path cannot use it --
# it has to resolve a digest before running -- so it pulls itself, and this
# asserts the staleness protection survived the change rather than being
# dropped along with the flag.
test_verified_path_pulls_before_running() {
    setup_stubs
    local pull_log="$stub_dir/pull.log"
    cat >"$stub_dir/podman" <<PODMAN
#!/usr/bin/env bash
if [ "\$1" = "pull" ]; then
    printf '%s ' "\$@" >> "$pull_log"
    exit 0
fi
if [ "\$1" = "image" ] && [ "\$2" = "inspect" ]; then
    printf '%s' "$test_digest"
    exit 0
fi
printf '%s ' "\$@" > "$podman_log"
exit 0
PODMAN
    chmod +x "$stub_dir/podman"
    PATH="$stub_dir" HOME="$stub_dir/home" "$aib" >/dev/null 2>&1
    assert_contains "$(cat "$pull_log" 2>/dev/null)" "pull" "verified path: pulls before running"
    assert_contains "$(cat "$pull_log" 2>/dev/null)" "ghcr.io/danathar/atomic-image-builder:latest" "verified path: pulls the configured tag"
    cleanup_stubs
}

# --- an unverified path keeps --pull=newer --------------------------------
test_unverified_path_keeps_pull_newer() {
    setup_stubs
    local args
    args="$(PATH="$stub_dir" HOME="$stub_dir/home" AIB_IMAGE="localhost/my-own-build:dev" "$aib" >/dev/null 2>&1; cat "$podman_log")"
    assert_contains "$args" "--pull=newer" "custom image: --pull=newer still applies"
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
test_gh_token_forwarded_by_environment_not_argv
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
test_verified_path_pulls_before_running
test_unverified_path_keeps_pull_newer
test_verify_runs_the_digest_it_verified
test_verify_uses_the_publisher_identity
test_verify_failure_refuses_to_run
test_verify_requires_cosign
test_skip_verify_warns_and_runs
test_pull_failure_refuses_to_run
test_custom_image_is_not_verified
test_published_release_tag_is_verified
test_unparseable_digest_refuses_to_run
test_malformed_digest_refuses_to_run

echo
if [ "$skip" -gt 0 ]; then
    echo "$pass passed, $fail failed, $skip skipped"
else
    echo "$pass passed, $fail failed"
fi
[ "$fail" -eq 0 ]
