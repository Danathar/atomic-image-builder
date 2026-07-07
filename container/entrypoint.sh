#!/bin/bash
# Entrypoint for the atomic-image-builder container image.
#
# If GitHub auth is available (either GH_TOKEN is set, or a persisted `gh`
# config was mounted in — see contrib/aib), configure git to authenticate
# pushes through gh's credential helper. This is best-effort: a failure here
# does not stop the tool from starting, since the TUI's own preflight check
# already reports missing GitHub auth clearly.
set -eu

if [ -n "${GH_TOKEN:-}" ] || gh auth status >/dev/null 2>&1; then
    gh auth setup-git || true
fi

exec atomic-image-builder "$@"
