#!/usr/bin/env bash
#
# Behavioral tests for container/entrypoint.sh. Plain bash, no test
# framework, mirroring tests/test_contrib_aib.sh: each scenario stubs
# gh/atomic-image-builder as fake executables on PATH so the real ones are
# never invoked, then inspects what each stub was called with.
#
# Run: bash tests/test_entrypoint.sh

set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
entrypoint="$repo_root/container/entrypoint.sh"

pass=0
fail=0

setup_stubs() {
    stub_dir="$(mktemp -d)"
    gh_log="$stub_dir/gh.log"
    tool_log="$stub_dir/tool.log"
    # tool.log joins the arguments with spaces, which cannot distinguish one
    # argument containing a space from two arguments. argv.log brackets each
    # argument so word splitting is visible, and pid.log records the process
    # the tool ends up in so the exec can be checked.
    argv_log="$stub_dir/argv.log"
    pid_log="$stub_dir/pid.log"

    local tool src
    for tool in bash env mktemp rm cat; do
        src="$(command -v "$tool")"
        ln -s "$src" "$stub_dir/$tool"
    done

    cat >"$stub_dir/atomic-image-builder" <<TOOL
#!/usr/bin/env bash
printf '%s ' "\$@" > "$tool_log"
printf '[%s]' "\$@" > "$argv_log"
printf '%s' "\$\$" > "$pid_log"
exit "\${TOOL_EXIT:-0}"
TOOL
    chmod +x "$stub_dir/atomic-image-builder"
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

read_log() {
    if [ -f "$1" ]; then
        cat "$1"
    fi
}

# --- GH_TOKEN set, gh present and logged in: setup-git runs ---------------
test_gh_token_set() {
    setup_stubs
    cat >"$stub_dir/gh" <<GH
#!/usr/bin/env bash
printf '%s ' "\$@" >> "$gh_log"
if [ "\$1" = "auth" ] && [ "\$2" = "setup-git" ]; then
    exit 0
fi
exit 0
GH
    chmod +x "$stub_dir/gh"
    GH_TOKEN=fake-token PATH="$stub_dir" bash "$entrypoint" --version >/dev/null 2>&1
    assert_contains "$(read_log "$gh_log")" "auth setup-git" "GH_TOKEN set: gh auth setup-git invoked"
    cleanup_stubs
}

# --- GH_TOKEN set, gh missing entirely: exec still runs, no fatal error ---
test_gh_token_set_no_gh_binary() {
    setup_stubs
    local status
    GH_TOKEN=fake-token PATH="$stub_dir" bash "$entrypoint" --version >/dev/null 2>&1
    status=$?
    assert_eq "$status" "0" "GH_TOKEN set, no gh binary: entrypoint still execs the tool"
    assert_contains "$(read_log "$tool_log")" "--version" "GH_TOKEN set, no gh binary: args forwarded"
    cleanup_stubs
}

# --- GH_TOKEN unset, gh present and logged in: setup-git runs -------------
test_gh_authenticated() {
    setup_stubs
    cat >"$stub_dir/gh" <<GH
#!/usr/bin/env bash
printf '%s ' "\$@" >> "$gh_log"
if [ "\$1" = "auth" ] && [ "\$2" = "status" ]; then
    exit 0
fi
exit 0
GH
    chmod +x "$stub_dir/gh"
    env -u GH_TOKEN PATH="$stub_dir" bash "$entrypoint" --version >/dev/null 2>&1
    assert_contains "$(read_log "$gh_log")" "auth setup-git" "gh authenticated: gh auth setup-git invoked"
    cleanup_stubs
}

# --- GH_TOKEN unset, gh present but not logged in: setup-git skipped -----
test_gh_not_logged_in() {
    setup_stubs
    cat >"$stub_dir/gh" <<GH
#!/usr/bin/env bash
printf '%s ' "\$@" >> "$gh_log"
exit 1
GH
    chmod +x "$stub_dir/gh"
    env -u GH_TOKEN PATH="$stub_dir" bash "$entrypoint" --version >/dev/null 2>&1
    assert_not_contains "$(read_log "$gh_log")" "setup-git" "gh not logged in: gh auth setup-git NOT invoked"
    cleanup_stubs
}

# --- GH_TOKEN unset, gh missing entirely: setup-git skipped, tool still runs
test_gh_absent() {
    setup_stubs
    local status
    env -u GH_TOKEN PATH="$stub_dir" bash "$entrypoint" --help >/dev/null 2>&1
    status=$?
    assert_eq "$status" "0" "gh absent: entrypoint still execs the tool"
    assert_contains "$(read_log "$tool_log")" "--help" "gh absent: args forwarded"
    cleanup_stubs
}

# --- gh auth setup-git fails: failure is swallowed, tool still runs ------
test_setup_git_failure_swallowed() {
    setup_stubs
    cat >"$stub_dir/gh" <<GH
#!/usr/bin/env bash
printf '%s ' "\$@" >> "$gh_log"
if [ "\$1" = "auth" ] && [ "\$2" = "status" ]; then
    exit 0
fi
if [ "\$1" = "auth" ] && [ "\$2" = "setup-git" ]; then
    exit 1
fi
exit 0
GH
    chmod +x "$stub_dir/gh"
    local status
    env -u GH_TOKEN PATH="$stub_dir" bash "$entrypoint" --version >/dev/null 2>&1
    status=$?
    assert_eq "$status" "0" "setup-git failure: swallowed, entrypoint still execs the tool"
    cleanup_stubs
}

# --- exit code from the wrapped tool is preserved -------------------------
test_exit_code_preserved() {
    setup_stubs
    local status
    TOOL_EXIT=17 env -u GH_TOKEN PATH="$stub_dir" bash "$entrypoint" >/dev/null 2>&1
    status=$?
    assert_eq "$status" "17" "exit code: the wrapped tool's exit status is preserved"
    cleanup_stubs
}

# --- arguments are forwarded as written, not re-split on whitespace -------
# `"$@"` and `$@` are indistinguishable in tool.log, which joins the arguments
# back together with spaces. An unquoted `$@` re-splits an argument containing
# a space into two and drops an empty one entirely, so a project name or a
# commit message with a space in it would reach the tool as several arguments.
test_arguments_are_forwarded_without_word_splitting() {
    setup_stubs
    env -u GH_TOKEN PATH="$stub_dir" bash "$entrypoint" \
        --project "my project" --message "" --version >/dev/null 2>&1
    assert_eq "$(read_log "$argv_log")" \
        "[--project][my project][--message][][--version]" \
        "arguments: spaces and empty strings survive the hand-off to the tool"
    cleanup_stubs
}

# --- the tool replaces the shell rather than running as its child ---------
# `exec` is what makes the tool PID 1 in the container. Without it bash stays
# PID 1 and the tool is its child, so `podman stop` signals bash and the tool
# never sees the SIGTERM. Every scenario above passes either way, because the
# tool still runs and its exit status still propagates.
test_tool_replaces_the_shell() {
    setup_stubs
    local shell_pid
    env -u GH_TOKEN PATH="$stub_dir" bash "$entrypoint" --version >/dev/null 2>&1 &
    shell_pid=$!
    wait "$shell_pid"
    assert_eq "$(read_log "$pid_log")" "$shell_pid" \
        "exec: the tool runs in the entrypoint's own process, not a child"
    cleanup_stubs
}

# --- the gh auth probe is silent ------------------------------------------
# `gh auth status` is only asked a yes/no question here, but it reports on
# both streams, and the tool it hands over to is a full-screen TUI. Anything
# the probe prints lands in the terminal before the TUI takes it over.
test_gh_auth_probe_prints_nothing() {
    setup_stubs
    cat >"$stub_dir/gh" <<GH
#!/usr/bin/env bash
printf '%s ' "\$@" >> "$gh_log"
if [ "\$1" = "auth" ] && [ "\$2" = "status" ]; then
    echo "probe-noise-on-stdout"
    echo "probe-noise-on-stderr" >&2
    exit 0
fi
exit 0
GH
    chmod +x "$stub_dir/gh"
    local output
    output="$(env -u GH_TOKEN PATH="$stub_dir" bash "$entrypoint" --version 2>&1)"
    assert_not_contains "$output" "probe-noise" \
        "gh auth status: neither stream reaches the terminal"
    assert_contains "$(read_log "$gh_log")" "auth setup-git" \
        "gh auth status: the probe still gates setup-git as before"
    cleanup_stubs
}

test_gh_token_set
test_gh_token_set_no_gh_binary
test_gh_authenticated
test_gh_not_logged_in
test_gh_absent
test_setup_git_failure_swallowed
test_exit_code_preserved
test_arguments_are_forwarded_without_word_splitting
test_tool_replaces_the_shell
test_gh_auth_probe_prints_nothing

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
