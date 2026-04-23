#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Iterable, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path

if sys.version_info < (3, 10):  # noqa: UP036
    raise SystemExit("Python 3.10 or newer is required.")

# This file intentionally keeps the whole beginner-focused tool in one module.
# The runtime model is:
# 1. collect choices from the user into Config
# 2. validate and normalize that Config
# 3. write a canonical state file so future updates do not need fragile parsing
# 4. render a GitHub repo from a pinned template snapshot plus generated files
# 5. let GitHub Actions build and sign the image
#
# A future refactor could split UI, GitHub operations, and rendering into
# separate modules, but the comments below aim to make the current layout easier
# to understand for anyone reading it now.
VERSION = "0.8-beta"
TOOL_NAME = "Atomic Image Builder"
TOOL_SLUG = "atomic-image-builder"
STATE_FILE = f".{TOOL_SLUG}.json"
DEFAULT_REPO_NAME = "my-atomic-image"
DEFAULT_GITHUB_BUILD_CRON = "05 10 * * *"
FEDORA_ATOMIC_FALLBACK_TAG = "43"
UNIVERSAL_BLUE_BREW_IMAGE = "ghcr.io/ublue-os/brew:latest"
MAX_UI_WIDTH = 120
ACCENT_COLOR = 117
CONTROLS_COLOR = 10
PACKAGE_SEARCH_LIMIT = 40
MANAGED_REPO_WARNING = "If you hand-edit a repo after this tool creates or manages it, stop using this tool for that repo."
MANAGED_REPO_HINT_CONTAINERFILE = (
    f"Future updates use {STATE_FILE} as the source of truth and rewrite managed files such as README.md and build_files/build.sh."
)
MANAGED_REPO_HINT_BLUEBUILD = (
    f"Future updates use {STATE_FILE} as the source of truth and rewrite managed files such as README.md and recipes/recipe.yml."
)
CONTAINERFILE_TEMPLATE_REPO = "ublue-os/image-template"
BLUEBUILD_TEMPLATE_REPO = "blue-build/template"
TEMPLATE_SNAPSHOT_DIR = Path(__file__).resolve().parent / "template_snapshots"
CONTAINERFILE_TEMPLATE_DIR = TEMPLATE_SNAPSHOT_DIR / "containerfile"
BLUEBUILD_TEMPLATE_DIR = TEMPLATE_SNAPSHOT_DIR / "bluebuild"
ALLOWED_METHODS = {"containerfile", "bluebuild"}
METHOD_DISPLAY = {"containerfile": "Containerfile", "bluebuild": "BlueBuild"}
BLUEBUILD_RECIPE_SCHEMA = "https://schema.blue-build.org/recipe-v1.json"
# These regexes are our low-cost safety rails. They do not prove a package or
# service is real, but they do stop obviously unsafe values from becoming shell
# script content later.
PACKAGE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._+:-]+$")
COPR_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
SERVICE_TOKEN_RE = re.compile(r"^[A-Za-z0-9@._:+-]+$")
FROM_LINE_RE = re.compile(r"^(\s*FROM(?:\s+--platform=\S+)?\s+)(\S+)(.*)$", flags=re.IGNORECASE)
INSTALLER_SWITCH_RE = re.compile(r"^(\s*bootc switch --mutate-in-place --transport registry )(\S+)(.*)$")
DNF5_MISSING_MARKERS = (
    "no matches found",
    "no package matched",
    "no packages to list",
    "matched no packages",
    "no matching packages",
)
# GitHub Actions should be pinned to immutable SHAs instead of floating tags.
# The human-readable tag is kept as a comment so maintainers can still tell what
# upstream version the pin came from.
ACTION_PINS: dict[str, tuple[str, str]] = {
    "actions/checkout": ("de0fac2e4500dabe0009e67214ff5f5447ce83dd", "v6"),
    "ublue-os/remove-unwanted-software": ("cc0becac701cf642c8f0a6613bbdaf5dc36b259e", "v9"),
    "docker/metadata-action": ("030e881283bb7a6894de51c315a6bfe6a94e05cf", "v6.0.0"),
    "redhat-actions/buildah-build": ("7a95fa7ee0f02d552a32753e7414641a04307056", "v2"),
    "docker/login-action": ("b45d80f862d83dbcd57f89517bcf500b2ab88fb2", "v4.0.0"),
    "redhat-actions/push-to-registry": ("5ed88d269cf581ea9ef6dd6806d01562096bee9c", "v2"),
    "sigstore/cosign-installer": ("cad07c2e89fa2edd6e2d7bab4c1aa38e53f76003", "v4.1.1"),
    "actions/upload-artifact": ("bbbca2ddaa5d8feaa63e36b76fdaad77386f024f", "v7.0.0"),
    "blue-build/github-action": ("01902d3bbdcd2196dbd3830af6a5344884f0ef0a", "v1.11"),
}
ACTION_REF_PINS: dict[str, tuple[str, str]] = {
    "ublue-os/remove-unwanted-software@v8": ("695eb75bc387dbcd9685a8e72d23439d8686cba6", "v8"),
    "ublue-os/remove-unwanted-software@695eb75bc387dbcd9685a8e72d23439d8686cba6": ("695eb75bc387dbcd9685a8e72d23439d8686cba6", "v8"),
    "ublue-os/remove-unwanted-software@v9": ACTION_PINS["ublue-os/remove-unwanted-software"],
    "ublue-os/remove-unwanted-software@cc0becac701cf642c8f0a6613bbdaf5dc36b259e": ACTION_PINS["ublue-os/remove-unwanted-software"],
    "osbuild/bootc-image-builder-action@main": ("5fc2ef0c4689b43ba959a10e3dfed3a889810ba1", "main"),
    "osbuild/bootc-image-builder-action@5fc2ef0c4689b43ba959a10e3dfed3a889810ba1": ("5fc2ef0c4689b43ba959a10e3dfed3a889810ba1", "main"),
    "actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f": ACTION_PINS["actions/upload-artifact"],
    "sigstore/cosign-installer@v4.0.0": ("faadad0cce49287aee09b3a48701e75088a2c6ad", "v4.0.0"),
    "sigstore/cosign-installer@faadad0cce49287aee09b3a48701e75088a2c6ad": ("faadad0cce49287aee09b3a48701e75088a2c6ad", "v4.0.0"),
}
PRECHECK_REQUIRED_TOOLS: tuple[str, ...] = ("gum", "git", "gh", "cosign")
BREW_INSTALLABLE_TOOLS: tuple[str, ...] = ("gum", "git", "gh", "cosign")
HOST_REQUIRED_TOOLS: tuple[str, ...] = ("dnf5", "rpm-ostree")


@dataclass(frozen=True)
class BaseImage:
    # This is the small curated image list shown to beginners. Keeping it as a
    # dataclass instead of plain dicts gives the rest of the code typed fields
    # and avoids magic string lookups.
    key: str
    provider: str
    name: str
    description: str
    image_uri: str


def read_os_release_fields(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    try:
        text = path.read_text()
    except OSError:
        return {}
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        fields[key] = value
    return fields


def determine_fedora_atomic_default_tag(
    *,
    fallback: str = FEDORA_ATOMIC_FALLBACK_TAG,
    os_release_path: Path = Path("/etc/os-release"),
) -> str:
    data = read_os_release_fields(os_release_path)
    if data.get("ID") != "fedora":
        return fallback
    version_id = data.get("VERSION_ID", "")
    if not version_id.isdigit():
        return fallback
    return str(max(int(fallback), int(version_id)))


FEDORA_ATOMIC_DEFAULT_TAG = determine_fedora_atomic_default_tag()


def universal_blue_image(key: str, name: str, description: str, image_uri: str) -> BaseImage:
    return BaseImage(key=key, provider="Universal Blue", name=name, description=description, image_uri=image_uri)


def fedora_atomic_image(key: str, name: str, description: str, variant: str) -> BaseImage:
    return BaseImage(
        key=key,
        provider="Fedora Atomic",
        name=name,
        description=description,
        image_uri=f"quay.io/fedora-ostree-desktops/{variant}:{FEDORA_ATOMIC_DEFAULT_TAG}",
    )


BASE_IMAGES: tuple[BaseImage, ...] = (
    universal_blue_image("bazzite", "Bazzite (KDE)", "KDE desktop for gaming systems and handheld-style setups", "ghcr.io/ublue-os/bazzite:stable"),
    universal_blue_image("bazzite-gnome", "Bazzite (GNOME)", "GNOME desktop for gaming systems and handheld-style setups", "ghcr.io/ublue-os/bazzite-gnome:stable"),
    universal_blue_image("bazzite-dx", "Bazzite DX (KDE)", "Bazzite plus extra developer tools on KDE", "ghcr.io/ublue-os/bazzite-dx:stable"),
    universal_blue_image("bazzite-dx-gnome", "Bazzite DX (GNOME)", "Bazzite plus extra developer tools on GNOME", "ghcr.io/ublue-os/bazzite-dx-gnome:stable"),
    universal_blue_image("aurora", "Aurora (KDE)", "KDE desktop for everyday use", "ghcr.io/ublue-os/aurora:stable"),
    universal_blue_image("aurora-dx", "Aurora DX", "Aurora plus extra developer tools", "ghcr.io/ublue-os/aurora-dx:stable"),
    universal_blue_image("bluefin", "Bluefin (GNOME)", "GNOME desktop for everyday use", "ghcr.io/ublue-os/bluefin:stable"),
    universal_blue_image("bluefin-dx", "Bluefin DX", "Bluefin plus extra developer tools", "ghcr.io/ublue-os/bluefin-dx:stable"),
    fedora_atomic_image("silverblue", "Fedora Silverblue", "GNOME desktop built from the official Fedora Atomic desktop image", "silverblue"),
    fedora_atomic_image("kinoite", "Fedora Kinoite", "KDE Plasma desktop built from the official Fedora Atomic desktop image", "kinoite"),
    fedora_atomic_image("sway-atomic", "Fedora Sway Atomic", "Sway desktop built from the official Fedora Atomic desktop image", "sway-atomic"),
    fedora_atomic_image("budgie-atomic", "Fedora Budgie Atomic", "Budgie desktop built from the official Fedora Atomic desktop image", "budgie-atomic"),
    fedora_atomic_image("cosmic-atomic", "Fedora COSMIC Atomic", "COSMIC desktop built from the official Fedora Atomic desktop image", "cosmic-atomic"),
)


def supported_base_image_names() -> str:
    return ", ".join(image.name for image in BASE_IMAGES)

COMMON_SERVICES: tuple[tuple[str, str], ...] = (
    ("SSH remote access", "sshd.service"),
    ("Tailscale VPN", "tailscaled.service"),
    ("Cockpit web admin", "cockpit.socket"),
)


@dataclass
class Config:
    # Config is the single source of truth for what the user wants to build.
    # Most of the app mutates this object in memory, then state_payload()
    # serializes it to STATE_FILE before repo files are written.
    method: str = ""
    base_image_uri: str = ""
    base_image_name: str = ""
    repo_name: str = ""
    image_desc: str = "My custom bootc image"
    packages: list[str] = field(default_factory=list)
    copr_repos: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    removed_packages: list[str] = field(default_factory=list)
    brew_enabled: bool = False
    signing_enabled: bool = False
    github_user: str = ""
    scanned_packages: list[str] = field(default_factory=list)
    scanned_removed: list[str] = field(default_factory=list)

    def normalize(self) -> None:
        # Every menu appends to lists over time. Normalizing here keeps ordering
        # stable for humans while still removing duplicates and empty values.
        self.packages = unique(self.packages)
        self.copr_repos = unique(self.copr_repos)
        self.services = unique(self.services)
        self.removed_packages = unique(self.removed_packages)


def unique(values: Iterable[str]) -> list[str]:
    # This preserves first-seen order, which makes generated files and review
    # screens stable and easier for users to reason about.
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        stripped = value.strip()
        if stripped and stripped not in seen:
            output.append(stripped)
            seen.add(stripped)
    return output


def sanitize_slug(value: str, default: str = DEFAULT_REPO_NAME) -> str:
    # GitHub repo names cannot contain spaces, so we translate user-friendly
    # input into a slug before running stricter validation.
    cleaned = re.sub(r"[^a-z0-9._-]", "-", value.lower()).strip("-")
    return cleaned or default


def is_valid_repo_name(value: str) -> bool:
    # Keep this aligned with the subset of GitHub naming rules we want to
    # support in the beginner UI. We are intentionally stricter than necessary
    # so error messages stay simple.
    if not value or len(value) > 100:
        return False
    if value.endswith(".git"):
        return False
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,98}[a-z0-9])?", value):
        return False
    return True


def yaml_scalar(value: str) -> str:
    # JSON string quoting is valid YAML 1.2 and saves us from bringing in a
    # YAML library just to safely escape a single scalar value.
    return json.dumps(value)


def ensure_trailing_newline(text: str) -> str:
    return text.rstrip("\n") + "\n"


def normalize_container_image_reference(container_ref: str) -> str:
    # rpm-ostree reports image origins with different prefixes depending on how
    # the deployment was created. Normalize those into a plain image reference
    # so scan results can be matched against our curated base-image list.
    base = container_ref.strip()
    if base.startswith("ostree-image-signed:docker://"):
        return base[len("ostree-image-signed:docker://") :]
    if base.startswith("ostree-unverified-registry:"):
        return base[len("ostree-unverified-registry:") :]
    if base.startswith("ostree-remote-image:") or base.startswith("ostree-remote-registry:"):
        parts = base.split(":", 2)
        if len(parts) == 3:
            base = parts[2]
    if base.startswith("docker://"):
        return base[len("docker://") :]
    return base


def format_daily_rebuild_note(
    cron: str,
    *,
    now_utc: datetime | None = None,
    local_tz: tzinfo | None = None,
) -> str:
    parts = cron.split()
    if len(parts) != 5:
        return "Scheduled rebuilds also run automatically on GitHub."
    minute_text, hour_text, day_of_month, month, day_of_week = parts
    if day_of_month != "*" or month != "*" or day_of_week != "*" or not minute_text.isdigit() or not hour_text.isdigit():
        return f"Scheduled rebuilds also run automatically on GitHub using the configured schedule ({cron} UTC)."
    hour = int(hour_text)
    minute = int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return f"Scheduled rebuilds also run automatically on GitHub using the configured schedule ({cron} UTC)."

    base_utc = now_utc.astimezone(timezone.utc) if now_utc else datetime.now(timezone.utc)
    scheduled_utc = base_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    tz = local_tz or datetime.now().astimezone().tzinfo or timezone.utc
    scheduled_local = scheduled_utc.astimezone(tz)
    local_time = scheduled_local.strftime("%I:%M %p").lstrip("0")
    local_zone = scheduled_local.tzname() or "local time"
    utc_time = scheduled_utc.strftime("%H:%M")
    if local_zone == "UTC":
        return f"Scheduled rebuilds also run daily at about {local_time} UTC."
    return f"Scheduled rebuilds also run daily at about {local_time} {local_zone} on this system ({utc_time} UTC)."


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def shell_quote(value: str) -> str:
    return shlex.quote(value)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def validate_string_list(value: object, field_name: str) -> list[str]:
    # State files are user-editable JSON. Strict type checks here keep a broken
    # or hand-edited state file from turning into confusing runtime errors.
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    invalid = [item for item in value if not isinstance(item, str)]
    if invalid:
        raise ValueError(f"{field_name} must contain only strings")
    return list(value)


def config_from_state_payload(data: object) -> Config:
    # Older repo updates depend on this loader being defensive. If the state
    # file is wrong, we would rather fail loudly with a helpful message than
    # quietly write a damaged repo back to GitHub.
    if not isinstance(data, dict):
        raise ValueError("state file must contain a JSON object")
    state_version = data.get("state_version")
    if state_version is not None:
        if not isinstance(state_version, int):
            raise ValueError("state_version must be an integer")
        if state_version > 1:
            raise ValueError(f"unsupported state_version: {state_version}")

    cfg = Config()
    list_fields = {
        "packages",
        "copr_repos",
        "services",
        "removed_packages",
        "scanned_packages",
        "scanned_removed",
    }
    string_fields = {
        "method",
        "base_image_uri",
        "base_image_name",
        "repo_name",
        "image_desc",
        "github_user",
    }
    for name in list_fields:
        if name in data:
            setattr(cfg, name, validate_string_list(data[name], name))
    for name in string_fields:
        if name in data:
            value = data[name]
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            setattr(cfg, name, value)
    for bool_field in ("brew_enabled", "signing_enabled"):
        if bool_field in data:
            value = data[bool_field]
            if not isinstance(value, bool):
                raise ValueError(f"{bool_field} must be a boolean")
            setattr(cfg, bool_field, value)
    if cfg.method and cfg.method not in ALLOWED_METHODS:
        raise ValueError(f"unsupported build method: {cfg.method}")
    cfg.normalize()
    return cfg


def pin_action_uses_line(line: str) -> str:
    # When patching upstream workflow text, we rewrite "uses:" lines to pinned
    # SHAs. This avoids supply-chain drift if an upstream tag ever changes.
    match = re.fullmatch(r"(\s*uses:\s+)([^@\s]+)@([^\s#]+)(.*)", line)
    if not match:
        return line
    prefix, action, _ref, suffix = match.groups()
    pin = ACTION_REF_PINS.get(f"{action}@{_ref}") or ACTION_PINS.get(action)
    if not pin:
        return line
    sha, label = pin
    suffix = re.sub(r"\s+#.*$", "", suffix)
    comment = f" # {label}"
    return f"{prefix}{action}@{sha}{comment}"


def pinned_action(action: str) -> str:
    # New workflows are generated directly from these pinned references instead
    # of floating tags for the same reason as pin_action_uses_line().
    sha, label = ACTION_PINS[action]
    return f"{action}@{sha} # {label}"


def patch_signing_step_block(step_lines: Sequence[str], *, branch_if: str, sign_if: str) -> list[str]:
    # Signing-related steps are identified by behavior rather than display
    # names so template renames do not silently bypass our signing guard.
    is_cosign_install = any("uses:" in line and "sigstore/cosign-installer@" in line for line in step_lines)
    is_cosign_sign = any(re.search(r"\bcosign\s+sign\b", line) for line in step_lines)
    if not (is_cosign_install or is_cosign_sign):
        return list(step_lines)

    patched: list[str] = []
    has_if = False
    for line in step_lines:
        stripped = line.lstrip()
        if stripped.startswith("if: "):
            has_if = True
            if branch_if in stripped and sign_if not in stripped:
                indent = line[: len(line) - len(stripped)]
                condition = stripped.replace(branch_if, sign_if, 1)
                patched.append(f"{indent}{condition}")
                continue
        patched.append(line)
    if has_if or not patched:
        return patched

    first_line = patched[0]
    indent = first_line[: len(first_line) - len(first_line.lstrip())] + "  "
    return [patched[0], f"{indent}if: {sign_if}", *patched[1:]]


def patch_workflow_signing_steps(workflow_text: str, *, branch_if: str, sign_if: str) -> str:
    lines = workflow_text.splitlines()
    output: list[str] = []
    current_step: list[str] = []
    in_steps = False
    steps_indent: int | None = None

    def flush_step() -> None:
        nonlocal current_step
        if not current_step:
            return
        output.extend(patch_signing_step_block(current_step, branch_if=branch_if, sign_if=sign_if))
        current_step = []

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if in_steps and steps_indent is not None and indent <= steps_indent and stripped and not stripped.startswith("#"):
            flush_step()
            in_steps = False
            steps_indent = None

        if stripped == "steps:":
            flush_step()
            in_steps = True
            steps_indent = indent
            output.append(line)
            continue

        if in_steps and steps_indent is not None and indent == steps_indent + 2 and stripped.startswith("- "):
            flush_step()
            current_step = [line]
            continue

        if current_step:
            current_step.append(line)
        else:
            output.append(line)

    flush_step()
    return "\n".join(output)


def ensure_workflow_job_env_entries(workflow_text: str, entries: Sequence[tuple[str, str]]) -> str:
    lines = workflow_text.splitlines()
    missing_lines: list[str] = []
    # Job-level env is at 6 spaces (4 for job indent + 2 for key).  We must
    # check at this exact indentation, otherwise a step-level env entry with
    # the same key fools the check into thinking the job-level one exists.
    job_env_prefix = "      "  # 6 spaces
    for name, value in entries:
        wanted = f"{name}: {value}"
        if not any(line == f"{job_env_prefix}{wanted}" for line in lines):
            missing_lines.append(f"{job_env_prefix}{wanted}")
    if not missing_lines:
        return workflow_text

    insertion = "".join(f"{line}\n" for line in missing_lines)
    if re.search(r"^    env:\n", workflow_text, flags=re.MULTILINE):
        return re.sub(
            r"^    env:\n",
            "    env:\n" + insertion,
            workflow_text,
            count=1,
            flags=re.MULTILINE,
        )
    if re.search(r"^    steps:\n", workflow_text, flags=re.MULTILINE):
        return re.sub(
            r"^    steps:\n",
            "    env:\n" + insertion + "    steps:\n",
            workflow_text,
            count=1,
            flags=re.MULTILINE,
        )
    return workflow_text


class CommandError(RuntimeError):
    pass


class ScreenBack(RuntimeError):
    pass


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    # This is the single subprocess helper used by most of the file. Keeping
    # command execution here centralizes our "raise CommandError with useful
    # text" behavior instead of repeating it around the app.
    proc = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        env=env,
        input=stdin,
        text=True,
        capture_output=capture,
        check=False,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.strip() if proc.stderr else ""
        stdout = proc.stdout.strip() if proc.stdout else ""
        body = stderr or stdout
        detail = f"{body} (command: {' '.join(args)})" if body else f"command failed: {' '.join(args)}"
        raise CommandError(detail)
    return proc


class Gum:
    # Gum is used as a lightweight TUI toolkit. This wrapper smooths out a few
    # rough edges for the rest of the app:
    # - it normalizes Ctrl+C vs Esc/back behavior
    # - it computes widths consistently
    # - it hides the exact gum command lines from the workflow code
    def terminal_width(self) -> int:
        return shutil.get_terminal_size((MAX_UI_WIDTH, 24)).columns

    def content_width(self, *, max_width: int = MAX_UI_WIDTH, min_width: int = 40, reserve: int = 4) -> int:
        return max(min_width, min(max_width, self.terminal_width() - reserve))

    def form_width(self, *, max_width: int = 96, min_width: int = 40, reserve: int = 6) -> int:
        return max(min_width, min(max_width, self.terminal_width() - reserve))

    def table_widths(self, left: int, *, max_width: int = MAX_UI_WIDTH, min_right: int = 24) -> str:
        right = max(min_right, self.content_width(max_width=max_width, reserve=0) - left - 4)
        return f"{left},{right}"

    def require_interactive_success(self, proc: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
        # gum uses exit code 130 for Ctrl+C and non-zero for "cancel/back".
        # Converting those to Python exceptions lets the rest of the app reason
        # about navigation instead of raw exit codes.
        if proc.returncode == 130:
            raise KeyboardInterrupt()
        if proc.returncode != 0:
            raise ScreenBack()
        return proc

    def clear(self) -> None:
        if sys.stdout.isatty() and os.environ.get("TERM"):
            run(["clear"], capture=False, check=False)

    def interactive_stdout(self, args: Sequence[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        # We capture stdout for chooser/input widgets because that is how gum
        # returns the selected value. stderr is left attached to the terminal so
        # interactive drawing still appears on screen.
        return subprocess.run(
            list(args),
            input=stdin,
            text=True,
            stdout=subprocess.PIPE,
            stderr=None,
            check=False,
        )

    def ensure_available(self) -> None:
        if not command_exists("gum"):
            raise SystemExit("gum is required. Install it with: brew install gum")

    def style(self, *lines: str, **opts: str | int | bool) -> str:
        args = ["gum", "style"]
        for key, value in opts.items():
            flag = "--" + key.replace("_", "-")
            if isinstance(value, bool):
                if value:
                    args.append(flag)
            else:
                args.extend([flag, str(value)])
        args.extend(lines)
        output = run(args).stdout.rstrip("\n")
        if output and not ANSI_RE.search(output):
            output = self.apply_ansi_fallback(output, **opts)
        return output

    def apply_ansi_fallback(self, text: str, **opts: str | int | bool) -> str:
        # gum style disables ANSI when we capture stdout through a pipe. Reapply
        # the basic text styling ourselves so headings and helper text remain
        # visible in normal terminals.
        if not sys.stdout.isatty() or not os.environ.get("TERM"):
            return text
        codes: list[str] = []
        if opts.get("bold"):
            codes.append("1")
        if opts.get("faint"):
            codes.append("2")
        if opts.get("italic"):
            codes.append("3")
        if opts.get("underline"):
            codes.append("4")
        if opts.get("strikethrough"):
            codes.append("9")
        foreground = self.ansi_color_code(opts.get("foreground"), background=False)
        if foreground:
            codes.append(foreground)
        background = self.ansi_color_code(opts.get("background"), background=True)
        if background:
            codes.append(background)
        if not codes:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"

    def ansi_color_code(self, value: str | int | bool | None, *, background: bool) -> str | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return f"{48 if background else 38};5;{value}"
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return f"{48 if background else 38};5;{text}"
        return None

    def log(self, level: str, message: str) -> None:
        run(["gum", "log", "--level", level, message], capture=False)

    def success(self, message: str) -> None:
        self.log("info", message)

    def warn(self, message: str) -> None:
        self.log("warn", message)

    def error(self, message: str) -> None:
        self.log("error", message)

    def header(self, title: str, *, clear_screen: bool = True) -> None:
        if clear_screen:
            self.clear()
        print()
        print(self.style(f"━━━  {title}  ━━━", foreground=ACCENT_COLOR, bold=True))
        print()

    def hint(self, message: str) -> None:
        print(self.style(message, width=self.content_width()))

    def instruction(self, message: str) -> None:
        print(self.style(message, foreground=ACCENT_COLOR, bold=True, width=self.content_width()))

    def controls(self, *parts: str) -> None:
        label = self.style("Keys:", foreground=CONTROLS_COLOR, bold=True)
        print(f"{label} {' | '.join(parts)}")
        print()

    def confirm(self, prompt: str, *, default: bool = True) -> bool:
        args = ["gum", "confirm", "--no-show-help", prompt]
        args.append("--default=true" if default else "--default=false")
        proc = run(args, check=False, capture=False)
        if proc.returncode == 130:
            raise KeyboardInterrupt()
        return proc.returncode == 0

    def input(
        self,
        *,
        prompt: str,
        value: str | None = None,
        placeholder: str | None = None,
        width: int | None = None,
    ) -> str:
        args = ["gum", "input", "--no-show-help", "--prompt", prompt]
        args.extend(["--prompt.foreground", str(ACCENT_COLOR), "--cursor.foreground", str(ACCENT_COLOR)])
        if value is not None:
            args.extend(["--value", value])
        if placeholder is not None:
            args.extend(["--placeholder", placeholder])
            args.extend(["--placeholder.foreground", "248"])
        if width is not None:
            args.extend(["--width", str(width)])
        return self.require_interactive_success(self.interactive_stdout(args)).stdout.rstrip("\n")

    def write(self, *, placeholder: str, height: int, width: int) -> str:
        return self.require_interactive_success(
            self.interactive_stdout(
                [
                    "gum",
                    "write",
                    "--no-show-help",
                    "--placeholder",
                    placeholder,
                    "--placeholder.foreground",
                    "248",
                    "--cursor.foreground",
                    str(ACCENT_COLOR),
                    "--height",
                    str(height),
                    "--width",
                    str(width),
                ]
            )
        ).stdout.rstrip("\n")

    def choose(
        self,
        options: Sequence[str],
        *,
        height: int = 10,
        no_limit: bool = False,
        selected: Sequence[str] | None = None,
        header: str | None = None,
        label_delimiter: str | None = None,
        cursor_prefix: str | None = None,
        selected_prefix: str | None = None,
        unselected_prefix: str | None = None,
    ) -> list[str]:
        args = ["gum", "choose", "--no-show-help", "--height", str(height)]
        args.extend(
            [
                "--cursor.foreground",
                str(ACCENT_COLOR),
                "--header.foreground",
                str(ACCENT_COLOR),
                "--selected.foreground",
                str(ACCENT_COLOR),
            ]
        )
        if no_limit:
            args.append("--no-limit")
        if selected:
            args.extend(["--selected", ",".join(selected)])
        if header:
            args.extend(["--header", header])
        if label_delimiter is not None:
            args.extend(["--label-delimiter", label_delimiter])
        if cursor_prefix is not None:
            args.extend(["--cursor-prefix", cursor_prefix])
        if selected_prefix is not None:
            args.extend(["--selected-prefix", selected_prefix])
        if unselected_prefix is not None:
            args.extend(["--unselected-prefix", unselected_prefix])
        proc = self.require_interactive_success(self.interactive_stdout(args, stdin="\n".join(options) + "\n"))
        output = proc.stdout.strip("\n")
        return [line for line in output.splitlines() if line]

    def filter(self, options: Sequence[str], *, height: int = 20, placeholder: str = "Search...") -> str:
        proc = self.require_interactive_success(
            self.interactive_stdout(
                [
                    "gum",
                    "filter",
                    "--no-show-help",
                    "--height",
                    str(height),
                    "--placeholder",
                    placeholder,
                    "--prompt.foreground",
                    str(ACCENT_COLOR),
                    "--header.foreground",
                    str(ACCENT_COLOR),
                    "--selected-indicator.foreground",
                    str(ACCENT_COLOR),
                    "--match.foreground",
                    str(ACCENT_COLOR),
                    "--placeholder.foreground",
                    "248",
                ],
                stdin="\n".join(options) + "\n",
            )
        )
        return proc.stdout.strip()

    def pager(self, text: str) -> None:
        run(["gum", "pager"], capture=False, stdin=text)

    def table(self, rows: Sequence[Sequence[str]], *, columns: str, widths: str) -> None:
        text = "\n".join("\t".join(row) for row in rows) + "\n"
        run(["gum", "table", "--separator", "\t", "--columns", columns, "--widths", widths], capture=False, stdin=text)

    def spinner(self, title: str, command: Sequence[str], *, cwd: Path | None = None) -> None:
        run(["gum", "spin", "--spinner", "dot", "--title", title, "--", *command], cwd=cwd, capture=False)

    def spinner_capture(self, title: str, command: Sequence[str], *, cwd: Path | None = None) -> str:
        # gum spin does not give us structured output directly, so we capture the
        # command's stdout through a temporary file and then read it back.
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            output_path = tmp.name
        try:
            shell_command = f"{shlex.join(command)} > {shlex.quote(output_path)}"
            run(
                ["gum", "spin", "--spinner", "dot", "--title", title, "--", "bash", "-c", shell_command],
                cwd=cwd,
                capture=False,
            )
            return Path(output_path).read_text()
        finally:
            Path(output_path).unlink(missing_ok=True)

    def spinner_result(self, title: str, command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        # Same idea as spinner_capture(), but this version keeps stdout, stderr,
        # and exit status so callers can inspect a command result after the
        # spinner closes.
        with ExitStack() as stack:
            with tempfile.NamedTemporaryFile(delete=False) as stdout_tmp:
                stdout_path = stdout_tmp.name
            stack.callback(Path(stdout_path).unlink, missing_ok=True)
            with tempfile.NamedTemporaryFile(delete=False) as stderr_tmp:
                stderr_path = stderr_tmp.name
            stack.callback(Path(stderr_path).unlink, missing_ok=True)
            with tempfile.NamedTemporaryFile(delete=False) as status_tmp:
                status_path = status_tmp.name
            stack.callback(Path(status_path).unlink, missing_ok=True)
            shell_command = (
                f"{shlex.join(command)} > {shlex.quote(stdout_path)} 2> {shlex.quote(stderr_path)}; "
                f"printf '%s' $? > {shlex.quote(status_path)}"
            )
            run(
                ["gum", "spin", "--spinner", "dot", "--title", title, "--", "bash", "-c", shell_command],
                cwd=cwd,
                capture=False,
            )
            stdout = Path(stdout_path).read_text()
            stderr = Path(stderr_path).read_text()
            status_text = Path(status_path).read_text().strip()
            try:
                returncode = int(status_text)
            except ValueError:
                returncode = 1
            return subprocess.CompletedProcess(list(command), returncode, stdout, stderr)

    def enter_to_continue(self, placeholder: str = "Press Enter to continue...") -> None:
        self.instruction(placeholder)
        self.require_interactive_success(
            self.interactive_stdout(
                [
                    "gum",
                    "input",
                    "--no-show-help",
                    "--prompt",
                    "> ",
                    "--prompt.foreground",
                    str(ACCENT_COLOR),
                    "--cursor.foreground",
                    str(ACCENT_COLOR),
                    "--width",
                    "3",
                ]
            )
        )


class App:
    def __init__(self) -> None:
        # The app keeps a small amount of session state beyond Config:
        # - GitHub login information discovered during preflight
        # - temporary signing/public-key data while creating or updating a repo
        # - memoized host package lookups so repeated manual checks are faster
        self.gum = Gum()
        self.config = Config()
        self.github_available = False
        self.github_user = ""
        self.generated_cosign_pub: str | None = None
        self.package_lookup_cache: dict[str, bool | None] = {}
        self.package_search_cache: dict[str, list[tuple[str, str]]] = {}
        self.package_lookup_warning_shown = False
        self.last_manual_package_check_had_missing = False
        self.removed_package_lookup_warning_shown = False
        self.last_manual_removed_package_check_had_missing = False

    def fresh_config(self) -> Config:
        # Starting a new create/scan flow should not inherit stale repo names or
        # package picks from the previous action the user ran in this session.
        return Config(github_user=self.github_user)

    def landing_panel_width(self) -> int:
        return self.gum.content_width(max_width=92, reserve=10)

    def landing_card(
        self,
        title: str,
        lines: Sequence[str],
        *,
        width: int,
        border_foreground: int,
        foreground: int = 252,
        background: int = 236,
    ) -> None:
        # Keep the landing screen visually consistent without repeating the
        # same gum style options for each intro card.
        print(
            self.gum.style(
                title,
                "",
                *lines,
                align="left",
                width=width,
                margin="0 2",
                padding="1 2",
                foreground=foreground,
                background=background,
                border_foreground=border_foreground,
                border="rounded",
            )
        )

    def banner(self) -> None:
        panel_width = self.landing_panel_width()
        print()
        print(self.gum.style(f"{TOOL_NAME}  v{VERSION}", align="center", width=panel_width, foreground=117, bold=True))
        print(self.gum.style("GitHub-backed bootc image repo builder", align="center", width=panel_width, foreground=252))
        print(self.gum.style("for Universal Blue and Fedora Atomic desktops", align="center", width=panel_width, foreground=252))
        print()

    def startup_requirements(self) -> None:
        # This screen exists because GitHub is not optional for the beginner
        # tool. Telling users that up front is better than failing halfway
        # through the wizard after they already entered data.
        info_width = self.landing_panel_width()
        self.landing_card(
            "Before You Start",
            [
                "GitHub account required",
                "Log in first: gh auth login",
                "",
                "Official template repos",
                "Containerfile: ublue-os/image-template",
                "BlueBuild: blue-build/template",
                "Generated repos start from bundled snapshots",
                "of these official template repositories.",
                "Those templates work across this tool's",
                "supported images and change infrequently.",
            ],
            width=info_width,
            border_foreground=117,
        )
        print()
        self.landing_card(
            "Important",
            [
                "Third-party tool",
                "Not an official Universal Blue utility",
                "Not an official Fedora Project utility",
                "",
                "Provided as-is",
                "Review changes before you push",
                "Keep backups where appropriate",
                "Repository damage, data loss, failed builds,",
                "and system changes are your risk.",
            ],
            width=info_width,
            border_foreground=214,
        )
        print()
        self.gum.enter_to_continue("Press Enter to start the preflight checks...")

    def clear(self) -> None:
        self.gum.clear()

    def gh_json(self, args: Sequence[str]) -> object:
        # Small helper around "gh ... --json" style commands.
        proc = run(["gh", *args])
        return json.loads(proc.stdout or "null")

    def gh_json_with_spinner(self, title: str, args: Sequence[str]) -> object:
        # Networked GitHub queries can feel frozen without a spinner.
        output = self.gum.spinner_capture(title, ["gh", *args])
        return json.loads(output or "null")

    def github_login_name(self) -> str:
        try:
            data = self.gh_json(["api", "user"])
        except (CommandError, json.JSONDecodeError) as exc:
            raise CommandError(f"Unable to determine GitHub username: {exc}") from exc
        if not isinstance(data, dict):
            raise CommandError("Unable to determine GitHub username: unexpected API response.")
        login = data.get("login")
        if not isinstance(login, str) or not login.strip():
            raise CommandError("Unable to determine GitHub username: login field missing from API response.")
        return login.strip()

    def show_step_header(self, title: str, *, step: int, total_steps: int, next_hint: str | None = None) -> None:
        self.gum.header(title)
        if next_hint:
            self.gum.hint(next_hint)
        self.gum.hint(f"Step {step} of {total_steps}.")
        print()

    def format_task_choice(self, title: str, status: str) -> str:
        return f"{title:<24} {self.truncate_label(status, limit=56)}"

    def truncate_label(self, value: str, limit: int = 36) -> str:
        clean = " ".join(value.split())
        if len(clean) <= limit:
            return clean
        return clean[: limit - 3] + "..."

    def preview_values(self, values: Sequence[str], *, limit: int = 2, item_limit: int = 24) -> str:
        if not values:
            return ""
        shown = [self.truncate_label(value, limit=item_limit) for value in values[:limit]]
        remaining = len(values) - len(shown)
        if remaining > 0:
            shown.append(f"{remaining} more")
        return ", ".join(shown)

    def summarize_selection(self, values: Sequence[str], *, empty: str, verb: str, limit: int = 2) -> str:
        if not values:
            return empty
        preview = self.preview_values(values, limit=limit)
        if len(values) <= limit:
            return preview
        return f"{len(values)} {verb}: {preview}"

    def software_status(self) -> str:
        parts: list[str] = []
        if self.config.packages:
            parts.append(f"{len(self.config.packages)} pkg")
        if self.config.copr_repos:
            parts.append(f"{len(self.config.copr_repos)} COPR")
        if self.config.services:
            parts.append(f"{len(self.config.services)} svc")
        if self.config.removed_packages:
            parts.append(f"{len(self.config.removed_packages)} removed")
        if self.config.brew_enabled:
            parts.append("brew")
        return ", ".join(parts) or "No software changes yet"

    def repository_status(self) -> str:
        repo = f"{self.github_user}/{self.config.repo_name}" if self.github_user else self.config.repo_name or "(not set)"
        if not self.config.image_desc:
            return repo
        return f"{repo} | {self.truncate_label(self.config.image_desc, limit=28)}"

    def requested_packages_note(self) -> str:
        return "Selected packages are what this repo will attempt to add, even if some are already present in the chosen base image."

    def published_image_ref(self, owner: str | None = None) -> str:
        image_owner = (owner or self.config.github_user or self.github_user or "your-user").lower()
        return f"ghcr.io/{image_owner}/{self.config.repo_name}:latest"

    def render_preflight_failure(
        self,
        *,
        missing_tools: Sequence[str],
        missing_host_tools: Sequence[str] = (),
        github_login_missing: bool = False,
        github_account_error: bool = False,
    ) -> None:
        brew_tools = [name for name in PRECHECK_REQUIRED_TOOLS if name in missing_tools and name in BREW_INSTALLABLE_TOOLS]
        host_tools = list(missing_host_tools)

        if command_exists("gum"):
            self.gum.ensure_available()
            self.gum.header("Preflight Failed", clear_screen=False)
            self.gum.warn("This tool requires all startup checks to pass before it can continue.")
            print()
            if missing_tools:
                self.menu_section("Missing Tools", ", ".join(missing_tools))
                print()
            if brew_tools:
                self.menu_section("Install With Homebrew", f"brew install {' '.join(brew_tools)}")
                print()
            if github_login_missing:
                self.menu_section("GitHub Login", "Run: gh auth login")
                print()
            if github_account_error:
                self.menu_section("GitHub Account Check", "Run: gh auth status && gh auth login")
                print()
            if host_tools:
                self.menu_section(
                    "Host Requirements",
                    f"Missing host tools: {', '.join(host_tools)}",
                    "This tool expects a supported rpm-ostree / bootc desktop image with dnf5 and rpm-ostree available.",
                )
                print()
            self.gum.enter_to_continue("Press Enter to exit to the terminal...")
            return

        print()
        print("Preflight Failed")
        print()
        print("This tool requires all startup checks to pass before it can continue.")
        if missing_tools:
            print()
            print(f"Missing tools: {', '.join(missing_tools)}")
        if brew_tools:
            print()
            print(f"Install with Homebrew: brew install {' '.join(brew_tools)}")
        if github_login_missing:
            print()
            print("Run: gh auth login")
        if github_account_error:
            print()
            print("Run: gh auth status && gh auth login")
        if host_tools:
            print()
            print(f"Missing host tools: {', '.join(host_tools)}")
            print()
            print("This tool expects a supported rpm-ostree / bootc desktop image with dnf5 and rpm-ostree available.")
        print()

    def menu_section(self, title: str, *lines: str) -> None:
        label = title if title.endswith((":", "?", "!")) else f"{title}:"
        self.gum.instruction(label)
        for line in lines:
            self.gum.hint(line)

    def render_package_menu_intro(
        self,
        *,
        packages_empty: str,
        packages_label: str = "Packages",
        include_copr: bool = False,
        include_services: bool = False,
        next_step_hint: str,
    ) -> None:
        self.menu_section(
            "Package Entry",
            "Search package names when you only know part of the RPM name. Use exact-name entry when you already know it.",
            self.requested_packages_note(),
        )
        print()
        current_lines = [
            f"{packages_label}: {self.summarize_selection(self.config.packages, empty=packages_empty, verb='selected')}"
        ]
        if include_copr:
            current_lines.append(f"COPR repositories: {self.summarize_selection(self.config.copr_repos, empty='None', verb='added')}")
        if include_services:
            current_lines.append(f"Services: {self.summarize_selection(self.config.services, empty='None', verb='enabled')}")
        self.menu_section("Current Selections", *current_lines)
        print()
        self.menu_section("Next Step", next_step_hint)

    def update_task_choices(self) -> list[tuple[str, str]]:
        choices = [
            ("Packages", self.summarize_selection(self.config.packages, empty="No packages", verb="selected")),
            ("Base image", self.config.base_image_name or "(not set)"),
            ("Description", self.truncate_label(self.config.image_desc or "(empty)")),
            ("COPR repositories", self.summarize_selection(self.config.copr_repos, empty="No COPRs", verb="added")),
            ("Services", self.summarize_selection(self.config.services, empty="No services", verb="enabled")),
            ("Removed base packages", self.summarize_selection(self.config.removed_packages, empty="None", verb="selected")),
        ]
        if not self.is_universal_blue_base():
            choices.append(("Homebrew", "Enabled" if self.config.brew_enabled else "Not included"))
        return choices

    def show_managed_repo_warning(self) -> None:
        self.gum.warn(MANAGED_REPO_WARNING)
        if self.config.method == "bluebuild":
            self.gum.hint(MANAGED_REPO_HINT_BLUEBUILD)
        else:
            self.gum.hint(MANAGED_REPO_HINT_CONTAINERFILE)

    def preflight(self) -> None:
        # Preflight is intentionally blunt: it checks the tools this app depends
        # on before we let the user invest time in the wizard.
        missing_tools = [name for name in PRECHECK_REQUIRED_TOOLS if not command_exists(name)]
        missing_host_tools = [name for name in HOST_REQUIRED_TOOLS if not command_exists(name)]
        github_login_missing = False
        github_account_error = False

        if "gh" not in missing_tools:
            if run(["gh", "auth", "status"], check=False).returncode != 0:
                github_login_missing = True
            else:
                try:
                    self.github_user = self.github_login_name()
                    self.github_available = True
                    self.config.github_user = self.github_user
                except CommandError:
                    self.github_available = False
                    github_account_error = True
        else:
            self.github_available = False

        if missing_tools or missing_host_tools or github_login_missing or github_account_error:
            self.render_preflight_failure(
                missing_tools=missing_tools,
                missing_host_tools=missing_host_tools,
                github_login_missing=github_login_missing,
                github_account_error=github_account_error,
            )
            raise SystemExit(1)

        self.gum.ensure_available()
        self.gum.header("Preflight Checks", clear_screen=False)
        self.gum.hint("Checking required tools and the runtime environment...")
        print()

        self.gum.success("git found")
        self.gum.success(f"GitHub CLI authenticated as: {self.github_user}")
        self.gum.success("cosign found (new repos can configure signing automatically)")

        print()
        self.gum.enter_to_continue("Press Enter to continue...")

    def require_github(self) -> bool:
        try:
            return self._require_github()
        except ScreenBack:
            return False

    def _require_github(self) -> bool:
        # Many flows call this right before making networked changes. It either
        # confirms GitHub is ready or walks the user through login first.
        if self.github_available and self.github_user:
            return True
        if not command_exists("gh"):
            self.gum.error("GitHub CLI is required for this action.")
            print()
            self.gum.hint("Install it with: brew install gh")
            return False
        if run(["gh", "auth", "status"], check=False).returncode != 0:
            self.github_setup_guide()
        try:
            self.github_user = self.github_login_name()
        except CommandError:
            self.gum.error("Unable to determine GitHub username after login.")
            return False
        self.config.github_user = self.github_user
        self.github_available = True
        self.gum.success(f"GitHub ready: {self.github_user}")
        return True

    def github_setup_guide(self) -> None:
        print(
            self.gum.style(
                "GitHub Account Required",
                "",
                "This tool stores your image configuration on GitHub",
                "and uses GitHub Actions to build it automatically.",
                align="left",
                width=self.gum.content_width(max_width=100, reserve=8),
                margin="0 2",
                padding="1 2",
                foreground=117,
                border_foreground=117,
                border="rounded",
            )
        )
        print()
        self.gum.hint("Choose one option below, then press Enter.")
        print()
        choice = self.gum.choose(
            [
                "I already have a GitHub account - log me in",
                "I need to create a GitHub account first",
                "Quit",
            ],
            height=5,
        )
        selected = choice[0] if choice else "Quit"
        if selected.startswith("Quit"):
            raise SystemExit(0)
        if selected.startswith("I need to create"):
            print(
                self.gum.style(
                    "Create a GitHub Account",
                    "",
                    "Go to https://github.com/signup in a browser,",
                    "create an account, then return here for login.",
                    align="left",
                    width=self.gum.content_width(max_width=100, reserve=8),
                    margin="0 2",
                    padding="1 2",
                    foreground=11,
                    border_foreground=11,
                    border="rounded",
                )
            )
            print()
            if self.gum.confirm("Open github.com/signup now?", default=True):
                if command_exists("xdg-open"):
                    run(["xdg-open", "https://github.com/signup"], check=False, capture=False)
                elif command_exists("open"):
                    run(["open", "https://github.com/signup"], check=False, capture=False)
            self.gum.enter_to_continue("Press Enter after you've created the account...")

        print(
            self.gum.style(
                "Log In to GitHub",
                "",
                "The GitHub CLI will now guide you through login.",
                "Use GitHub.com, HTTPS, and browser login.",
                align="left",
                width=self.gum.content_width(max_width=100, reserve=8),
                margin="0 2",
                padding="1 2",
                foreground=117,
                border_foreground=117,
                border="rounded",
            )
        )
        print()
        if not self.gum.confirm("Ready to log in?", default=True):
            raise SystemExit(0)
        if run(["gh", "auth", "login"], check=False, capture=False).returncode != 0:
            raise SystemExit("GitHub login failed. Try: gh auth login")

    def main_menu(self) -> None:
        # The main menu loops forever so the app drops the user back here after
        # create/update flows instead of exiting after one action.
        while True:
            self.gum.header("Main Menu")
            self.gum.controls("Up/Down move", "Enter choose", "Esc quit", "Ctrl+C quit")
            try:
                action = self.gum.choose(
                    [
                        "Create New Image",
                        "Scan OS & Migrate Layered Packages",
                        "Update Existing Image",
                        "View Build Status",
                        "Quit",
                    ],
                    height=8,
                )
            except ScreenBack:
                raise SystemExit(0)
            selected = action[0] if action else "Quit"
            if selected == "Quit":
                raise SystemExit(0)
            if selected == "Create New Image":
                self.create_new_image()
                continue
            if selected == "Scan OS & Migrate Layered Packages":
                if self.scan_os():
                    self.create_new_image(scanned=True)
                continue
            if selected == "Update Existing Image":
                self.update_existing_image()
                continue
            if selected == "View Build Status":
                self.view_build_status()
                continue

    def create_new_image(self, *, scanned: bool = False) -> None:
        # This is a simple step-by-step wizard. "step" is an integer instead of
        # a stack because the beginner flow is intentionally linear.
        if scanned:
            self.config.github_user = self.github_user
        else:
            self.config = self.fresh_config()
        total_steps = 5
        step = 1
        while True:
            try:
                if step == 1:
                    self.choose_method(step=step, total_steps=total_steps)
                    step = 2
                    continue
                if step == 2:
                    self.choose_base_image(step=step, total_steps=total_steps)
                    step = 3
                    continue
                if step == 3:
                    self.configure_repo(step=step, total_steps=total_steps)
                    step = 4
                    continue
                if step == 4:
                    self.select_packages(step=step, total_steps=total_steps)
                    step = 5
                    continue
                action = self.review_new_image(step=step, total_steps=total_steps)
            except ScreenBack:
                if step == 1:
                    return
                step -= 1
                continue
            if action == "build":
                if self.do_build():
                    return
                continue
            if action == "method":
                step = 1
            elif action == "base":
                step = 2
            elif action == "repo":
                step = 3
            elif action == "software":
                step = 4
            else:
                return

    def choose_method(self, *, step: int | None = None, total_steps: int | None = None) -> None:
        if step is not None and total_steps is not None:
            self.show_step_header("Build Method", step=step, total_steps=total_steps)
        else:
            self.gum.header("Build Method")
        self.gum.controls("Up/Down move", "Enter choose", "Esc back", "Ctrl+C quit")
        self.menu_section(
            "Tip",
            "Containerfile uses a standard Containerfile and shell script.",
            "BlueBuild uses a YAML recipe and the BlueBuild GitHub Action.",
        )
        print()
        options = [
            "Containerfile    Standard Containerfile with build script (recommended for beginners)",
            "BlueBuild        YAML recipe with the BlueBuild GitHub Action",
        ]
        choice = self.gum.choose(options, height=5)
        selected = choice[0] if choice else options[0]
        if selected.startswith("BlueBuild"):
            self.config.method = "bluebuild"
        else:
            self.config.method = "containerfile"
        self.gum.success(f"Build method: {METHOD_DISPLAY[self.config.method]}")

    def choose_base_image(self, *, step: int | None = None, total_steps: int | None = None) -> None:
        # Supported base images are intentionally limited. The point of this tool
        # is a predictable beginner path across curated Universal Blue and
        # Fedora Atomic desktop images, not every possible bootc image variant.
        if step is not None and total_steps is not None:
            self.show_step_header("Base Image", step=step, total_steps=total_steps)
        else:
            self.gum.header("Base Image")
        self.gum.controls("Up/Down move", "Enter choose", "Esc back", "Ctrl+C quit")
        self.menu_section(
            "Tip",
            f"Universal Blue DX images start with extra developer tools. Fedora Atomic options use the official Fedora {FEDORA_ATOMIC_DEFAULT_TAG} desktop images.",
        )
        print()
        if self.config.base_image_uri:
            matched = self.match_base_image(self.config.base_image_uri)
            if matched:
                print(f"  Detected base image: {self.gum.style(self.config.base_image_name or self.config.base_image_uri, bold=True)}")
                print(f"  Image: {self.gum.style(self.config.base_image_uri, foreground=117)}")
                print()
                if self.gum.confirm("Use this base image?", default=True):
                    self.offer_brew_if_applicable()
                    return
            else:
                self.gum.warn(f"This tool supports only the curated images listed below: {supported_base_image_names()}.")
                self.gum.hint("Choose one of those supported starting images below.")
                print()
                self.config.base_image_uri = ""
                self.config.base_image_name = ""

        options = [
            f"[{image.provider}] {image.name:<24} {image.description}  [{image.image_uri}]"
            for image in BASE_IMAGES
        ]
        choice = self.gum.choose(options, height=18)
        selected = choice[0] if choice else options[0]
        for image in BASE_IMAGES:
            if image.name in selected:
                self.config.base_image_uri = image.image_uri
                self.config.base_image_name = image.name
                break
        self.gum.success(f"Base image: {self.config.base_image_name} ({self.config.base_image_uri})")
        self.offer_brew_if_applicable()

    def is_universal_blue_base(self) -> bool:
        matched = self.match_base_image(self.config.base_image_uri)
        return matched is not None and matched.provider == "Universal Blue"

    def offer_brew_if_applicable(self) -> None:
        if self.is_universal_blue_base():
            self.config.brew_enabled = False
            return
        print()
        self.menu_section(
            "Homebrew",
            "Universal Blue images include Homebrew (brew) for installing extra command-line tools.",
            "Since you chose a Fedora Atomic base image, Homebrew is not included by default.",
            "You can add it now using the Universal Blue Homebrew OCI layer.",
        )
        print()
        self.config.brew_enabled = self.gum.confirm("Include Homebrew (brew) in this image?", default=False)
        if self.config.brew_enabled:
            self.gum.success("Homebrew will be included in your image.")
        else:
            self.gum.hint("You can add Homebrew later from the update menu.")

    def configure_repo(self, *, step: int | None = None, total_steps: int | None = None) -> None:
        # We collect repo name and description together because those two values
        # become both GitHub metadata and generated file content later.
        while True:
            if step is not None and total_steps is not None:
                self.show_step_header("Repository Configuration", step=step, total_steps=total_steps)
            else:
                self.gum.header("Repository Configuration")
            self.menu_section(
                "Repository Rules",
                "Repository names use letters, numbers, dashes, and dots. Spaces are turned into dashes.",
            )
            print()
            default_name = self.config.repo_name or DEFAULT_REPO_NAME
            raw_name = self.gum.input(
                prompt="Repository name: ",
                placeholder=default_name,
                width=self.gum.form_width(max_width=72),
            )
            candidate_name = sanitize_slug(raw_name or default_name, default_name)
            if not is_valid_repo_name(candidate_name):
                self.gum.error("Repository names must start and end with a letter or number, and they cannot end with .git.")
                self.gum.enter_to_continue("Press Enter to try another repository name...")
                continue
            self.config.repo_name = candidate_name
            self.config.image_desc = self.gum.input(
                prompt="Description: ",
                placeholder=self.config.image_desc,
                width=self.gum.form_width(max_width=110),
            ) or self.config.image_desc
            print()
            self.menu_section("Visibility", "Repositories created by this tool are public.")
            print()
            if self.github_user:
                self.gum.success(f"Repo: {self.github_user}/{self.config.repo_name}")
            else:
                self.gum.success(f"Repo name: {self.config.repo_name}")
            return

    def select_packages(self, *, step: int | None = None, total_steps: int | None = None) -> None:
        # "Software" is a menu of smaller editing tasks. Each option mutates the
        # same Config object, so the review screen can always show current state.
        while True:
            if step is not None and total_steps is not None:
                self.show_step_header("Software Selection", step=step, total_steps=total_steps)
            else:
                self.gum.header("Software Selection")
            self.gum.controls("Up/Down move", "Enter choose", "Esc back", "Ctrl+C quit")
            self.render_package_menu_intro(
                packages_empty="No packages yet",
                include_copr=True,
                include_services=True,
                next_step_hint="Choose Continue to review when you are finished, or use the remove options to undo package, COPR, or service choices.",
            )
            print()
            selection = self.gum.choose(
                [
                    "Search package names",
                    "Type exact package names",
                    "Remove selected packages",
                    "Add a COPR repository",
                    "Remove COPR repositories",
                    "Add systemd services to enable",
                    "Remove enabled services",
                    "Review current selections",
                    "Continue to review",
                ],
                height=12,
            )
            selected = selection[0] if selection else "Continue to review"
            if selected == "Continue to review":
                self.config.normalize()
                return
            try:
                if selected == "Search package names":
                    self.search_packages()
                elif selected == "Type exact package names":
                    self.manual_packages()
                elif selected == "Remove selected packages":
                    self.config.packages = self.choose_to_remove(self.config.packages, "Remove Packages")
                elif selected == "Add a COPR repository":
                    self.add_copr()
                elif selected == "Remove COPR repositories":
                    self.config.copr_repos = self.choose_to_remove(self.config.copr_repos, "Remove COPR Repositories")
                elif selected == "Add systemd services to enable":
                    self.add_services()
                elif selected == "Remove enabled services":
                    self.config.services = self.choose_to_remove(self.config.services, "Remove Services")
                elif selected == "Review current selections":
                    self.view_selections()
            except ScreenBack:
                continue

    def manual_packages(self) -> None:
        # Package entry is intentionally simple now: the user types the RPM
        # package names they want, and the tool does a lightweight local check
        # for obvious mistakes before the GitHub build does the final check.
        self.gum.header("Add Packages")
        print()
        self.menu_section(
            "What To Enter",
            "Enter exact RPM package names separated by spaces or newlines.",
            "Use package search instead if you only know part of the name.",
        )
        print()
        self.menu_section(
            "Validation",
            "This tool will try to catch obvious package-name mistakes here first.",
            "The GitHub build is still the final check.",
            "Leave this empty if you want to go back without adding anything.",
        )
        print()
        raw = self.gum.write(placeholder="Enter package names...", height=6, width=self.gum.form_width(max_width=110))
        packages = [token.strip(",") for token in raw.split() if token.strip(",")]
        if not packages:
            return
        before_count = len(self.config.packages)
        added = self.add_packages_to_config(packages, source_label="manual entry")
        added_count = len(self.config.packages) - before_count
        if added and not self.last_manual_package_check_had_missing:
            self.gum.enter_to_continue(f"Added {added_count} package(s). Press Enter to return to the package menu...")
            return
        if added and self.last_manual_package_check_had_missing:
            self.gum.enter_to_continue("Finished checking package names. Press Enter to return to the package menu...")
            return
        self.gum.enter_to_continue("No packages were added. Press Enter to return to the package menu...")

    def search_packages(self) -> None:
        while True:
            self.gum.header("Search Packages")
            self.menu_section(
                "Search Tips",
                "Search package names when you only know part of the RPM name.",
                "Search uses local DNF metadata. If it is unavailable here, use exact-name entry instead.",
            )
            print()
            term = self.gum.input(
                prompt="Search term: ",
                placeholder="tmux, podman, tailscale",
                width=self.gum.form_width(max_width=72),
            ).strip()
            if not term:
                return

            results, truncated, unavailable_message = self.search_host_packages(term)
            if unavailable_message:
                self.gum.warn(unavailable_message)
                self.gum.enter_to_continue("Press Enter to return to the package menu...")
                return
            if not results:
                self.gum.warn(f"No package names matched '{term}'.")
                self.gum.hint("Try a shorter or more specific term, or use exact-name entry if you already know the package name.")
                self.gum.enter_to_continue("Press Enter to search again...")
                continue

            self.gum.header("Package Search Results")
            self.gum.controls("Up/Down move", "x select", "Enter add", "Esc back", "Ctrl+C quit")
            if truncated:
                self.gum.hint(f"Showing the first {PACKAGE_SEARCH_LIMIT} matches. Narrow the search term if you need something else.")
            print()

            options: list[str] = []
            for name, summary in results:
                label = f"{name:<30} {self.truncate_label(summary or '(no summary available)', limit=60)}"
                options.append(f"{label}\t{name}")

            try:
                picked = self.gum.choose(
                    options,
                    height=20,
                    no_limit=True,
                    selected=self.config.packages,
                    label_delimiter="\t",
                    selected_prefix="[x] ",
                    unselected_prefix="[ ] ",
                )
            except ScreenBack:
                return

            picked_names = picked
            matching_current = [name for name, _summary in results if name in self.config.packages]
            removed_names = {name for name in matching_current if name not in picked_names}
            if removed_names:
                self.config.packages = [pkg for pkg in self.config.packages if pkg not in removed_names]
                self.config.normalize()

            new_packages = [name for name in picked_names if name not in self.config.packages]
            added_count = 0
            if new_packages:
                before_count = len(self.config.packages)
                added = self.add_packages_to_config(new_packages, source_label=f"search '{term}'")
                if added:
                    added_count = len(self.config.packages) - before_count

            removed_count = len(removed_names)
            if added_count and removed_count:
                self.gum.enter_to_continue(
                    f"Added {added_count} and removed {removed_count} package(s). Press Enter to return to the package menu..."
                )
            elif added_count:
                self.gum.enter_to_continue(f"Added {added_count} package(s). Press Enter to return to the package menu...")
            elif removed_count:
                self.gum.enter_to_continue(f"Removed {removed_count} package(s). Press Enter to return to the package menu...")
            else:
                self.gum.enter_to_continue("No package changes were made. Press Enter to return to the package menu...")
            return

    def add_copr(self) -> None:
        # COPR is powerful but advanced. The UI copy here tries to frame it as
        # optional so new users do not feel forced to understand it immediately.
        self.gum.header("Add COPR Repository")
        self.menu_section(
            "When To Use COPR",
            "COPR is an extra community package source outside the normal Fedora and image-provider repos.",
            "Most users can skip this. Only use it if you know a package you need comes from that COPR.",
            "Example: kwizart/fedy. Leave the repo field empty if you want to go back.",
        )
        print()
        repo = self.gum.input(
            prompt="COPR repo: ",
            placeholder="owner/project",
            width=self.gum.form_width(max_width=60),
        )
        repo = repo.strip()
        if not repo:
            return
        if not COPR_REPO_RE.fullmatch(repo):
            self.gum.error("Enter the COPR repo as owner/project.")
            return
        proposed_copr_repos = unique([*self.config.copr_repos, repo])
        print()
        self.menu_section(
            "Optional Package Entry",
            "Enter the package names you want from this COPR. Leave it empty if you only want to add the repo.",
        )
        pkgs = self.gum.input(
            prompt="Packages: ",
            placeholder="package1 package2",
            width=self.gum.form_width(max_width=80),
        )
        packages = [pkg.strip(",") for pkg in pkgs.split()]
        if packages and not self.add_packages_to_config(packages, source_label=f"COPR {repo}"):
            return
        self.config.copr_repos = proposed_copr_repos
        self.config.normalize()
        self.gum.success(f"Added COPR: {repo}")
        self.gum.hint("The GitHub build will confirm that the COPR repo and package names are valid.")

    def add_services(self) -> None:
        # Service enabling is another advanced-ish option, so this menu starts
        # with common examples before dropping to raw systemd unit names.
        while True:
            self.gum.header("Enable Services")
            self.menu_section(
                "What This Does",
                "Services are background features that start automatically when the image boots.",
                "Most users can skip this unless they know they want something like SSH or Tailscale always on.",
                "Choose a common service, type another one manually, or go back.",
            )
            print()
            try:
                choice = self.gum.choose(
                    [
                        "Choose from common services",
                        "Type service names manually (advanced)",
                        "Back",
                    ],
                    height=6,
                )
            except ScreenBack:
                return
            selected = choice[0] if choice else "Back"
            if selected == "Back":
                return
            if selected.startswith("Choose from common services"):
                self.select_common_services()
            elif selected.startswith("Type service names manually"):
                self.add_services_manually()

    def select_common_services(self) -> None:
        self.gum.header("Common Services")
        self.gum.controls("Up/Down move", "x select", "Enter save", "Esc back", "Ctrl+C quit")
        label_to_service = {f"{label} ({service})": service for label, service in COMMON_SERVICES}
        options = list(label_to_service)
        selected = [label for label, service in label_to_service.items() if service in self.config.services]
        try:
            picked = self.gum.choose(
                options,
                height=10,
                no_limit=True,
                selected=selected,
                selected_prefix="[x] ",
                unselected_prefix="[ ] ",
            )
        except ScreenBack:
            return
        remaining = [service for service in self.config.services if service not in label_to_service.values()]
        chosen_services = [label_to_service[label] for label in picked]
        self.config.services = unique([*remaining, *chosen_services])
        self.gum.success(f"Total services configured: {len(self.config.services)}")

    def add_services_manually(self) -> None:
        self.gum.header("Add Services Manually")
        print()
        self.menu_section(
            "What To Enter",
            "Type systemd service names like sshd.service or tailscaled.service.",
            "Leave this empty if you want to go back without adding anything.",
        )
        raw = self.gum.write(
            placeholder="Enter service names, one per line...",
            height=5,
            width=self.gum.form_width(max_width=80),
        )
        services = unique(line.strip() for line in raw.splitlines())
        if not services:
            return
        try:
            self.validate_token_list(services, SERVICE_TOKEN_RE, "systemd service")
        except CommandError as exc:
            self.gum.error(str(exc))
            return
        self.config.services.extend(services)
        self.config.normalize()
        self.gum.success(f"Total services configured: {len(self.config.services)}")

    def view_selections(self) -> None:
        sections = [
            ("Packages", self.config.packages),
            ("COPR Repositories", self.config.copr_repos),
            ("Services", self.config.services),
            ("Removed Base Packages", self.config.removed_packages),
        ]
        lines = ["This is a read-only summary.", ""]
        for index, (title, values) in enumerate(sections):
            if index:
                lines.append("")
            lines.append(title)
            if values:
                lines.extend(f"- {value}" for value in values)
            else:
                lines.append("- (none)")
        if not self.is_universal_blue_base():
            lines.append("")
            lines.append("Homebrew")
            lines.append(f"- {'Enabled' if self.config.brew_enabled else 'Not included'}")
        self.gum.pager(self.read_only_pager_text("Current Selections", lines))

    def show_summary(
        self,
        *,
        step: int | None = None,
        total_steps: int | None = None,
        next_hint: str | None = None,
    ) -> None:
        # Use a pager instead of rendering a table directly so long summaries
        # stay readable on shorter terminals and close cleanly with q.
        intro_lines: list[str] = []
        if next_hint:
            intro_lines.append(next_hint)
        if step is not None and total_steps is not None:
            intro_lines.append(f"Step {step} of {total_steps}.")
        intro_lines.append("This is a read-only summary of the current settings.")
        rows = [
            ("Build Method", METHOD_DISPLAY.get(self.config.method, "(not set)")),
            ("Repository", f"{self.github_user}/{self.config.repo_name}" if self.github_user else self.config.repo_name),
            ("Description", self.config.image_desc),
            ("Base Image", self.config.base_image_name or self.config.base_image_uri),
            ("Image URI", self.config.base_image_uri),
            ("Packages", self.summarize_selection(self.config.packages, empty="None selected", verb="selected", limit=3)),
            ("COPR Repos", self.summarize_selection(self.config.copr_repos, empty="None", verb="added", limit=3)),
            ("Services", self.summarize_selection(self.config.services, empty="None", verb="enabled", limit=3)),
            ("Removed Base Packages", self.summarize_selection(self.config.removed_packages, empty="None", verb="selected", limit=3)),
        ]
        if not self.is_universal_blue_base():
            rows.append(("Homebrew", "Enabled" if self.config.brew_enabled else "Not included"))
        body = self.format_key_value_rows(rows)
        lines = [*intro_lines, "", *body]
        self.gum.pager(self.read_only_pager_text("Review Build Configuration", lines))

    def review_new_image(self, *, step: int, total_steps: int) -> str:
        while True:
            self.show_step_header("Review and Create Image", step=step, total_steps=total_steps)
            self.gum.hint("Choose a section to review or change, or start the GitHub build.")
            print()
            method_label = self.format_task_choice("Build method", METHOD_DISPLAY.get(self.config.method, "(not set)"))
            software_label = self.format_task_choice("Software", self.software_status())
            repo_label = self.format_task_choice("Repository settings", self.repository_status())
            base_label = self.format_task_choice("Base image", self.config.base_image_name or "(not set)")
            full_label = "View full configuration"
            local_build_label = "Test build locally (podman)"
            build_label = "Start GitHub build"
            cancel_label = "Cancel and return to the main menu"
            options = [method_label, software_label, repo_label, base_label, full_label]
            if self.config.method == "containerfile":
                options.append(local_build_label)
            options.extend([build_label, cancel_label])
            choice = self.gum.choose(options, height=10)
            selected = choice[0] if choice else cancel_label
            if selected == build_label:
                return "build"
            if selected == local_build_label:
                self.test_build_locally()
                continue
            if selected == method_label:
                return "method"
            if selected == software_label:
                return "software"
            if selected == repo_label:
                return "repo"
            if selected == base_label:
                return "base"
            if selected == full_label:
                self.show_summary(step=step, total_steps=total_steps, next_hint="This is the full build summary.")
                continue
            return "cancel"

    def scan_os(self) -> bool:
        # This is the one place where the beginner tool looks at the running
        # host. It only reads rpm-ostree state so it can carry layered packages
        # and base-package removals into a new GitHub-backed image repo.
        self.config = self.fresh_config()
        self.gum.header("Scanning Running OS")
        if not command_exists("rpm-ostree"):
            self.gum.error("rpm-ostree not found. OS scanning is unavailable.")
            return False

        proc = run(["rpm-ostree", "status", "--json", "--booted"], check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            proc = run(["rpm-ostree", "status", "--json"], check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            self.gum.error("Failed to read rpm-ostree status.")
            return False

        try:
            status = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.gum.error("Failed to read rpm-ostree status.")
            return False
        deployments = status.get("deployments", [])
        booted = next((item for item in deployments if item.get("booted")), deployments[0] if deployments else {})
        if not booted:
            self.gum.error("No deployment information found.")
            return False

        container_ref = (
            booted.get("container-image-reference")
            or booted.get("origin")
            or ""
        )
        if not container_ref.strip():
            # A booted deployment without a container-image-reference or origin
            # (e.g. a legacy ostree-commit deployment) cannot be carried into an
            # image repo. Bail instead of proceeding with an empty base image.
            self.gum.error(
                "This deployment has no container image reference; scanning only supports bootc / image-based deployments."
            )
            return False
        base = normalize_container_image_reference(container_ref)
        self.config.scanned_packages = unique(booted.get("requested-packages", []))
        self.config.scanned_removed = unique(booted.get("requested-base-removals", []))
        self.config.removed_packages = list(self.config.scanned_removed)

        self.config.base_image_uri = base
        self.config.base_image_name = base
        matched = self.match_base_image(base)
        if matched:
            self.config.base_image_name = matched.name
            # Warn when the host is running a non-standard tag (e.g. :testing,
            # :44) that differs from the curated image_uri.  Offer to use the
            # curated tag so the generated repo tracks a known-good stream.
            scanned_tag = base.rsplit(":", 1)[-1] if ":" in base else ""
            curated_tag = matched.image_uri.rsplit(":", 1)[-1] if ":" in matched.image_uri else ""
            if scanned_tag and curated_tag and scanned_tag != curated_tag:
                self.gum.warn(
                    f"Your system is running :{scanned_tag}, but this tool recommends :{curated_tag} for {matched.name}."
                )
                if self.gum.confirm(f"Use the recommended :{curated_tag} tag instead?", default=True):
                    self.config.base_image_uri = matched.image_uri

        self.gum.header("Scan Results")
        rows = [
            ("Base Image", self.config.base_image_name),
            ("Image URI", self.config.base_image_uri),
            ("Layered Packages", str(len(self.config.scanned_packages))),
            ("Removed Base Packages", str(len(self.config.scanned_removed))),
        ]
        self.gum.table(rows, columns="Setting,Value", widths=self.gum.table_widths(22))
        print()

        if self.config.scanned_packages:
            self.gum.controls("Up/Down move", "x select", "Enter continue", "Esc back", "Ctrl+C quit")
            self.menu_section("Selection", "Leave everything unselected if you want to skip carrying these packages over.")
            print()
            try:
                selected = self.gum.choose(
                    self.config.scanned_packages,
                    height=20,
                    no_limit=True,
                    selected=self.config.scanned_packages,
                    header="Layered Packages",
                    selected_prefix="[x] ",
                    unselected_prefix="[ ] ",
                )
            except ScreenBack:
                return False
            self.config.packages = selected
        else:
            self.gum.warn("No layered packages found.")
            if not self.gum.confirm("Continue to create a custom image anyway?", default=True):
                return False

        if self.config.scanned_removed:
            self.gum.controls("Up/Down move", "x select", "Enter continue", "Esc back", "Ctrl+C quit")
            self.menu_section("Selection", "Leave everything unselected if you do not want to remove any base packages.")
            print()
            try:
                selected_removed = self.gum.choose(
                    self.config.scanned_removed,
                    height=20,
                    no_limit=True,
                    selected=self.config.scanned_removed,
                    header="Base Packages To Remove",
                    selected_prefix="[x] ",
                    unselected_prefix="[ ] ",
                )
            except ScreenBack:
                return False
            self.config.removed_packages = selected_removed

        self.config.normalize()
        return True

    def match_base_image(self, value: str) -> BaseImage | None:
        for image in BASE_IMAGES:
            image_repo = image.image_uri.rsplit(":", 1)[0]
            if value == image.image_uri or value == image_repo or value.startswith(f"{image_repo}:") or value.startswith(f"{image_repo}@"):
                return image
        return None

    def carried_scan_customizations(self) -> bool:
        scanned_packages = set(self.config.scanned_packages)
        scanned_removed = set(self.config.scanned_removed)
        return any(pkg in scanned_packages for pkg in self.config.packages) or any(pkg in scanned_removed for pkg in self.config.removed_packages)

    def scheduled_rebuild_note(self) -> str:
        return format_daily_rebuild_note(DEFAULT_GITHUB_BUILD_CRON)

    def repo_secret_exists(self, owner: str, repo: str, secret_name: str) -> bool:
        # We probe for the secret before trying to generate or upload a new key.
        # That keeps updates idempotent and avoids silently rotating keys.
        if not command_exists("gh"):
            return False
        proc = run(["gh", "secret", "list", "-R", f"{owner}/{repo}"], check=False)
        if proc.returncode != 0:
            return False
        return any(line.split()[0] == secret_name for line in proc.stdout.splitlines() if line.strip())

    def repo_file_exists(self, owner: str, repo: str, path: str) -> bool:
        proc = run(["gh", "api", f"/repos/{owner}/{repo}/contents/{path}"], check=False)
        return proc.returncode == 0

    def repo_has_state_file(self, owner: str, repo: str) -> bool:
        return self.repo_file_exists(owner, repo, STATE_FILE)

    def batch_check_state_files(self, owner: str, repos: list[dict[str, str]]) -> set[str]:
        """Check which repos contain STATE_FILE using a single GraphQL query.

        Falls back to serial REST calls if GraphQL fails (e.g. token scopes).
        Returns a set of repo names that have the state file.
        """
        if not repos:
            return set()
        # GraphQL aliases must start with a letter and contain only [A-Za-z0-9_]
        alias_map: dict[str, str] = {}
        for i, item in enumerate(repos):
            alias_map[f"r{i}"] = item["name"]
        fragments: list[str] = []
        safe_owner = json.dumps(owner)
        for alias, name in alias_map.items():
            safe_name = json.dumps(name)
            fragments.append(
                f'{alias}: repository(owner: {safe_owner}, name: {safe_name}) '
                f'{{ object(expression: "HEAD:{STATE_FILE}") {{ id }} }}'
            )
        query = "query { " + " ".join(fragments) + " }"
        proc = run(["gh", "api", "graphql", "-f", f"query={query}"], check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            # Fall back to serial REST checks
            return {
                item["name"] for item in repos
                if self.repo_has_state_file(owner, item["name"])
            }
        try:
            data = json.loads(proc.stdout).get("data", {})
        except (json.JSONDecodeError, AttributeError):
            return {
                item["name"] for item in repos
                if self.repo_has_state_file(owner, item["name"])
            }
        found: set[str] = set()
        for alias, name in alias_map.items():
            repo_data = data.get(alias)
            if repo_data and repo_data.get("object"):
                found.add(name)
        return found

    def ensure_signing_ready(self, owner: str, repo: str, *, repo_dir: Path | None = None) -> bool:
        # Signed images are required for this tool, so "ready" means:
        # - the repo already has a compatible SIGNING_SECRET, or
        # - we can create a cosign keypair and upload the needed secrets now
        self.generated_cosign_pub = None
        bluebuild_signing = self.config.method == "bluebuild"
        if self.repo_secret_exists(owner, repo, "SIGNING_SECRET"):
            if repo_dir is not None and not (repo_dir / "cosign.pub").exists():
                # GitHub secrets are write-only, so the public half of the key
                # cannot be recovered from SIGNING_SECRET. Rotation is the only
                # way to restore signing when cosign.pub has gone missing.
                raise CommandError(
                    "SIGNING_SECRET is configured on GitHub but cosign.pub is missing from this repo. "
                    "The public key cannot be recovered; use 'Rotate signing key (cosign)' from the update menu to restore signing."
                )
            return True
        if not command_exists("cosign"):
            raise CommandError("cosign is required for signed images. Install it with: brew install cosign")
        cosign_password = "" if bluebuild_signing else secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory(prefix=f"{TOOL_SLUG}-signing.") as tmp:
            tmpdir = Path(tmp)
            env = os.environ.copy()
            env["COSIGN_PASSWORD"] = cosign_password
            proc = run(["cosign", "generate-key-pair"], cwd=tmpdir, env=env, check=False)
            key_path = tmpdir / "cosign.key"
            pub_path = tmpdir / "cosign.pub"
            if proc.returncode != 0 or not key_path.exists() or not pub_path.exists():
                raise CommandError("Unable to generate a cosign keypair. Fix cosign first, then try again.")
            if not bluebuild_signing:
                password_proc = run(
                    ["gh", "secret", "set", "COSIGN_PASSWORD", "-R", f"{owner}/{repo}"],
                    cwd=tmpdir,
                    stdin=cosign_password,
                    check=False,
                )
                if password_proc.returncode != 0:
                    raise CommandError("Unable to upload COSIGN_PASSWORD to GitHub. Check your gh login and repo access, then try again.")
            while True:
                secret_proc = run(
                    ["gh", "secret", "set", "SIGNING_SECRET", "-R", f"{owner}/{repo}"],
                    cwd=tmpdir,
                    stdin=key_path.read_text(),
                    check=False,
                )
                if secret_proc.returncode == 0:
                    break
                if bluebuild_signing:
                    self.gum.error("Could not upload SIGNING_SECRET to GitHub. Check your gh login and repo access, then try again.")
                    if not self.gum.confirm("Retry uploading SIGNING_SECRET now?", default=True):
                        raise CommandError("SIGNING_SECRET upload was not completed.")
                    continue
                # COSIGN_PASSWORD already uploaded — GitHub is now half-configured.
                self.gum.error(
                    "Could not upload SIGNING_SECRET to GitHub. Signing setup is half-complete — "
                    "COSIGN_PASSWORD is set but SIGNING_SECRET is not, and image builds will fail signing until this finishes."
                )
                if not self.gum.confirm("Retry uploading SIGNING_SECRET now?", default=True):
                    raise CommandError(
                        "Aborting with signing setup half-complete. Re-run this tool to finish "
                        "uploading SIGNING_SECRET before pushing new commits."
                    )
            self.generated_cosign_pub = pub_path.read_text()
        # The public key is kept in memory for the current run so it can be
        # written into the repo files that we are about to generate.
        if bluebuild_signing:
            self.gum.success("Configured SIGNING_SECRET for BlueBuild image signing.")
        else:
            self.gum.success("Configured SIGNING_SECRET and COSIGN_PASSWORD for image signing.")
        return True

    def rotate_signing_key(self, repo_dir: Path | None = None) -> None:
        repo_dir = Path.cwd() if repo_dir is None else repo_dir
        owner = self.config.github_user or self.github_user
        repo = self.config.repo_name
        if not owner or not repo:
            self.gum.warn("Run this from a configured image repo so the GitHub repository can be identified.")
            return
        if not self.gum.confirm(
            "Rotate the cosign signing key? Old signatures remain valid in the registry; re-pull or re-verify after rotation.",
            default=False,
        ):
            return
        missing = [name for name in ("cosign", "gh") if not command_exists(name)]
        if missing:
            verb = "is" if len(missing) == 1 else "are"
            self.gum.warn(f"{', '.join(missing)} {verb} required to rotate the signing key.")
            self.gum.hint("Install the missing tool, then try this again.")
            return

        bluebuild_signing = self.config.method == "bluebuild"
        cosign_password = "" if bluebuild_signing else secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory(prefix=f"{TOOL_SLUG}-signing.") as tmp:
            tmpdir = Path(tmp)
            env = os.environ.copy()
            env["COSIGN_PASSWORD"] = cosign_password
            proc = run(["cosign", "generate-key-pair"], cwd=tmpdir, env=env, check=False)
            key_path = tmpdir / "cosign.key"
            pub_path = tmpdir / "cosign.pub"
            if proc.returncode != 0 or not key_path.exists() or not pub_path.exists():
                self.gum.error("Unable to generate a cosign keypair. Fix cosign first, then try again.")
                return
            if not bluebuild_signing:
                password_proc = run(
                    ["gh", "secret", "set", "COSIGN_PASSWORD", "-R", f"{owner}/{repo}"],
                    cwd=tmpdir,
                    stdin=cosign_password,
                    check=False,
                )
                if password_proc.returncode != 0:
                    self.gum.error("Unable to upload COSIGN_PASSWORD to GitHub. Check your gh login and repo access, then try again.")
                    return
            while True:
                secret_proc = run(
                    ["gh", "secret", "set", "SIGNING_SECRET", "-R", f"{owner}/{repo}"],
                    cwd=tmpdir,
                    stdin=key_path.read_text(),
                    check=False,
                )
                if secret_proc.returncode == 0:
                    break
                if bluebuild_signing:
                    self.gum.error("Could not upload SIGNING_SECRET to GitHub. Rotation was not applied.")
                    if not self.gum.confirm("Retry uploading SIGNING_SECRET now?", default=True):
                        return
                    continue
                # COSIGN_PASSWORD already uploaded — GitHub is now half-rotated.
                # Pressing on without the new key would leave signing broken.
                self.gum.error(
                    "Could not upload SIGNING_SECRET to GitHub. Rotation is half-complete — "
                    "your next image build will fail signing until this finishes."
                )
                if not self.gum.confirm("Retry uploading SIGNING_SECRET now?", default=True):
                    self.gum.warn(
                        "Aborting with rotation half-complete. Re-run 'Rotate signing key (cosign)' "
                        "before pushing new commits, or your GitHub Actions signing step will fail."
                    )
                    return
            try:
                shutil.copy2(pub_path, repo_dir / "cosign.pub")
                self.configure_temp_repo_git_identity(repo_dir)
                run(["git", "add", "cosign.pub"], cwd=repo_dir)
                run(["git", "commit", "-m", "Rotate cosign signing key"], cwd=repo_dir)
                commit_sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir).stdout.strip()
                run(["git", "push", "origin", "HEAD"], cwd=repo_dir, capture=False)
            except (CommandError, OSError) as exc:
                # GitHub already has the new secrets. Surface the split state so the
                # user knows to re-run rotation instead of trusting a silent failure.
                self.gum.error(
                    f"Uploaded new signing secrets to GitHub but could not write, commit, or push cosign.pub: {exc}"
                )
                self.gum.warn(
                    "Rotation is half-complete — GitHub secrets and the repo's cosign.pub are out of sync. "
                    "Re-run 'Rotate signing key (cosign)' before pushing new commits."
                )
                return
        self.gum.success(f"Rotated cosign signing key in commit {commit_sha}. GitHub secrets were updated.")
        self.gum.warn("Pre-rotation signatures remain valid in the registry; re-pull or re-verify after rotation.")
        self.gum.enter_to_continue("Press Enter to return to the update menu...")

    def clone_repo(self, owner: str, repo: str, target: Path) -> None:
        self.gum.spinner(f"Cloning {owner}/{repo}...", ["gh", "repo", "clone", f"{owner}/{repo}", str(target)])

    def configure_temp_repo_git_identity(self, repo_dir: Path) -> None:
        # Temp repos are created in scratch directories, so they cannot rely on
        # the user's global git config already being set. We always configure a
        # local author identity before committing so first-time users do not hit
        # "please tell me who you are" commit failures.
        login = self.github_user or self.config.github_user or TOOL_SLUG
        name = self.github_user or self.config.github_user or TOOL_NAME
        email = f"{login}@users.noreply.github.com"
        run(["git", "config", "user.name", name], cwd=repo_dir)
        run(["git", "config", "user.email", email], cwd=repo_dir)

    def copy_template_snapshot(self, target: Path, *, repo: str, source_dir: Path) -> None:
        # We copy from a bundled snapshot instead of pulling a live template from
        # GitHub at runtime. That makes the tool deterministic and avoids breakage
        # if upstream template repos change unexpectedly.
        target = target.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not source_dir.is_dir():
            raise CommandError(f"Bundled template snapshot not found for {repo}.")
        if target.exists():
            if any(target.iterdir()):
                raise CommandError(f"{target} already exists and is not empty.")
            target.rmdir()
        try:
            shutil.copytree(source_dir, target, ignore=shutil.ignore_patterns(
                ".template-source", "renovate.json5",
            ))
        except (OSError, shutil.Error) as exc:
            raise CommandError(f"Unable to copy bundled template snapshot for {repo}: {exc}") from exc

    def clone_container_template(self, target: Path) -> None:
        self.copy_template_snapshot(target, repo=CONTAINERFILE_TEMPLATE_REPO, source_dir=CONTAINERFILE_TEMPLATE_DIR)

    def clone_bluebuild_template(self, target: Path) -> None:
        self.copy_template_snapshot(target, repo=BLUEBUILD_TEMPLATE_REPO, source_dir=BLUEBUILD_TEMPLATE_DIR)

    def repo_default_branch(self, owner: str, repo: str) -> str:
        try:
            data = self.gh_json(["repo", "view", f"{owner}/{repo}", "--json", "defaultBranchRef"])
        except (CommandError, json.JSONDecodeError):
            data = None
        branch = data.get("defaultBranchRef", {}).get("name") if isinstance(data, dict) else None
        if branch:
            return branch
        proc = run(["gh", "api", f"/repos/{owner}/{repo}"], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                rest_data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                rest_data = None
            if isinstance(rest_data, dict):
                rest_branch = rest_data.get("default_branch")
                if isinstance(rest_branch, str) and rest_branch:
                    return rest_branch
        # Both detection paths failed. Warn the user instead of silently picking
        # "main", because a non-main default would otherwise leave the workflow's
        # branch filter pointing at a branch that does not exist on the repo.
        self.gum.warn("Could not detect the GitHub default branch; using 'main'.")
        self.gum.hint(
            "If your repo's default branch is not 'main', edit .github/workflows/build.yml after the first push."
        )
        return "main"

    def seed_project_template(self, target: Path) -> None:
        if self.config.method == "bluebuild":
            self.clone_bluebuild_template(target)
        else:
            self.clone_container_template(target)

    def add_packages_to_config(
        self,
        candidates: Iterable[str],
        *,
        source_label: str,
    ) -> bool:
        packages = unique(candidates)
        if not packages:
            return False
        try:
            self.validate_token_list(packages, PACKAGE_TOKEN_RE, "package")
        except CommandError as exc:
            self.gum.error(str(exc))
            return False
        if source_label == "manual entry":
            packages = self.filter_available_manual_packages(packages)
            if not packages:
                return False
        self.config.packages.extend(packages)
        self.config.normalize()
        self.gum.success(f"Added {len(packages)} package(s) from {source_label}")
        return True

    def add_removed_packages_to_config(
        self,
        candidates: Iterable[str],
        *,
        source_label: str,
    ) -> bool:
        packages = unique(candidates)
        if not packages:
            return False
        try:
            self.validate_token_list(packages, PACKAGE_TOKEN_RE, "removed package")
        except CommandError as exc:
            self.gum.error(str(exc))
            return False
        if source_label == "manual entry":
            packages = self.filter_available_manual_removed_packages(packages)
            if not packages:
                return False
        self.config.removed_packages.extend(packages)
        self.config.normalize()
        self.gum.success(f"Added {len(packages)} package removal(s) from {source_label}")
        return True

    def _filter_manual_packages(self, packages: Sequence[str], *, mode: str) -> list[str]:
        if mode == "available":
            missing_attr = "last_manual_package_check_had_missing"
            warning_attr = "package_lookup_warning_shown"
            missing_hint = "They were skipped because no RPM package with that name was found."
            unchecked_warn = "Could not fully check some package names on this system."
            unchecked_hint = "The GitHub build will do the final package check."
        else:
            missing_attr = "last_manual_removed_package_check_had_missing"
            warning_attr = "removed_package_lookup_warning_shown"
            missing_hint = "They were skipped because no RPM package with that name was found in your current host repos."
            unchecked_warn = "Could not fully check some package removals on this system."
            unchecked_hint = "The build will skip removals that are not installed in the base image."

        setattr(self, missing_attr, False)
        accepted: list[str] = []
        missing: list[str] = []
        missing_but_copr_may_provide: list[str] = []
        unchecked: list[str] = []
        for package in packages:
            available = self.lookup_host_package(package)
            if available is True:
                accepted.append(package)
            elif available is False:
                if mode == "available" and self.config.copr_repos:
                    accepted.append(package)
                    missing_but_copr_may_provide.append(package)
                else:
                    missing.append(package)
            else:
                accepted.append(package)
                unchecked.append(package)
        if missing:
            setattr(self, missing_attr, True)
            joined = ", ".join(missing)
            self.gum.error(f"These package names were not found: {joined}")
            self.gum.hint(missing_hint)
        if missing_but_copr_may_provide:
            joined = ", ".join(missing_but_copr_may_provide)
            self.gum.warn("Some package names were not found in your current host repos.")
            self.gum.hint(f"Keeping for now because configured COPRs may provide them: {joined}")
            self.gum.hint("The GitHub build will do the final package check.")
        if unchecked and not getattr(self, warning_attr):
            joined = ", ".join(unchecked)
            self.gum.warn(unchecked_warn)
            self.gum.hint(f"Keeping for now: {joined}")
            self.gum.hint(unchecked_hint)
            setattr(self, warning_attr, True)
        return accepted

    def filter_available_manual_packages(self, packages: Sequence[str]) -> list[str]:
        # Manual package entry is intentionally forgiving:
        # - known good packages are accepted
        # - clearly missing packages are skipped
        # - packages that might come from configured COPRs are kept
        # - unknown/uncheckable cases are kept, but the user is warned that the
        #   GitHub build is the final authority
        return self._filter_manual_packages(packages, mode="available")

    def filter_available_manual_removed_packages(self, packages: Sequence[str]) -> list[str]:
        # Removed packages are checked locally for obvious typos before we save
        # them into the repo state file. The generated build script still skips
        # removals that are not installed in the chosen base image.
        return self._filter_manual_packages(packages, mode="removed")

    def lookup_host_package(self, package: str) -> bool | None:
        # Host-side dnf5 checks are a lightweight "spellcheck" for manual RPM
        # names. They are not a perfect model of the final image build, but they
        # catch obvious mistakes like typos before we create a repo.
        if package in self.package_lookup_cache:
            return self.package_lookup_cache[package]
        if not command_exists("dnf5"):
            self.package_lookup_cache[package] = None
            return None
        state_dir = Path(tempfile.gettempdir()) / f"{TOOL_SLUG}-dnf5"
        state_dir.mkdir(parents=True, exist_ok=True)
        proc = self.gum.spinner_result(
            f"Checking package name: {package}",
            [
                "env",
                f"XDG_STATE_HOME={state_dir}",
                "dnf5",
                "repoquery",
                "--available",
                "--qf",
                "%{name}",
                "--latest-limit",
                "1",
                package,
            ],
        )
        names = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
        if package in names:
            self.package_lookup_cache[package] = True
            return True
        detail = "\n".join(part for part in [proc.stdout, proc.stderr] if part).lower()
        if any(marker in detail for marker in DNF5_MISSING_MARKERS):
            self.package_lookup_cache[package] = False
            return False
        if proc.returncode == 0 and not names:
            self.package_lookup_cache[package] = False
            return False
        self.package_lookup_cache[package] = None
        return None

    def search_host_packages(self, term: str) -> tuple[list[tuple[str, str]], bool, str | None]:
        normalized = " ".join(term.split())
        if not normalized:
            return [], False, None
        if not command_exists("dnf5"):
            return [], False, "dnf5 is not installed, so package search is unavailable on this system."

        cache_key = normalized.lower()
        cached = self.package_search_cache.get(cache_key)
        if cached is None:
            state_dir = Path(tempfile.gettempdir()) / f"{TOOL_SLUG}-dnf5"
            state_dir.mkdir(parents=True, exist_ok=True)
            pattern = f"*{normalized.replace(' ', '*')}*"
            proc = self.gum.spinner_result(
                f"Searching package names for: {normalized}",
                [
                    "env",
                    f"XDG_STATE_HOME={state_dir}",
                    "dnf5",
                    "-C",
                    "repoquery",
                    "--available",
                    "--latest-limit",
                    "1",
                    "--qf",
                    "%{name}\t%{summary}\n",
                    pattern,
                ],
            )
            detail = "\n".join(part for part in [proc.stdout, proc.stderr] if part).lower()
            if proc.returncode != 0:
                if "cache-only enabled but no cache" in detail:
                    return [], False, "Package search needs local DNF metadata. Run 'dnf5 makecache' first, or use exact-name entry."
                if any(marker in detail for marker in DNF5_MISSING_MARKERS):
                    return [], False, None
                return [], False, "Package search is unavailable right now. Use exact-name entry instead."

            by_name: dict[str, str] = {}
            for raw_line in proc.stdout.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if "\t" in line:
                    name, summary = line.split("\t", 1)
                else:
                    name, summary = line, ""
                if name not in by_name:
                    by_name[name] = summary.strip()

            needle = normalized.lower()
            cached = sorted(
                by_name.items(),
                key=lambda item: (
                    item[0].lower() != needle,
                    not item[0].lower().startswith(needle),
                    needle not in item[0].lower(),
                    item[0].lower(),
                ),
            )
            self.package_search_cache[cache_key] = cached

        return cached[:PACKAGE_SEARCH_LIMIT], len(cached) > PACKAGE_SEARCH_LIMIT, None

    def do_build(self) -> bool:
        # "Build" in this app really means "create or update the GitHub repo that
        # will trigger the real build on GitHub Actions."
        if not self.require_github():
            return False
        owner = self.github_user
        repo = self.config.repo_name
        self.config.github_user = owner
        self.validate_config()
        self.gum.header("Building Image")
        exists = run(["gh", "repo", "view", f"{owner}/{repo}", "--json", "name"], check=False).returncode == 0
        if exists:
            self.gum.error(f"{owner}/{repo} already exists on GitHub.")
            if self.repo_has_state_file(owner, repo):
                self.gum.hint("That repo was already created by this tool. Use 'Update Existing Image' to change it, or pick a new repo name.")
            else:
                self.gum.hint("That repo was not created by this tool.")
                self.gum.hint("This tool only updates repos it created itself. Pick a new repo name or manage that repo manually.")
            self.gum.enter_to_continue("Press Enter to go back to the review screen...")
            return False
        if not command_exists("cosign"):
            raise CommandError(
                "cosign is required to create a new repo because this tool must generate SIGNING_SECRET. Install it with: brew install cosign"
            )
        self.gum.spinner(
            f"Creating {owner}/{repo}...",
            ["gh", "repo", "create", repo, "--description", self.config.image_desc, "--public"],
        )
        pushed = False
        try:
            self.config.signing_enabled = self.ensure_signing_ready(owner, repo)

            with tempfile.TemporaryDirectory(prefix=f"{TOOL_SLUG}.") as tmp:
                tmpdir = Path(tmp)
                # We build the initial commit locally from our bundled template
                # snapshot and only then push it to the brand-new remote repo.
                self.seed_project_template(tmpdir)
                branch = self.repo_default_branch(owner, repo)
                run(["git", "init", "-b", branch], cwd=tmpdir)
                run(["git", "remote", "add", "origin", f"https://github.com/{owner}/{repo}.git"], cwd=tmpdir)
                self.configure_temp_repo_git_identity(tmpdir)
                self.write_project_files(tmpdir, include_workflow=True, default_branch=branch)
                run(["git", "add", "-A"], cwd=tmpdir)
                run(["git", "commit", "-m", f"Initial image configuration via {TOOL_SLUG}"], cwd=tmpdir)
                run(["git", "push", "origin", "HEAD"], cwd=tmpdir, capture=False)
                pushed = True
        except BaseException:
            if not pushed:
                # Repo creation is the only irreversible network step before the
                # first push. If anything later fails, we delete that empty repo
                # so the user can retry cleanly instead of dealing with leftovers.
                self.gum.warn("Setup failed after the GitHub repo was created. Removing the empty repo so you can try again cleanly.")
                delete_proc = run(["gh", "repo", "delete", f"{owner}/{repo}", "--yes"], check=False)
                if delete_proc.returncode != 0:
                    self.gum.warn("I could not remove the new GitHub repo automatically.")
                    detail = "\n".join(part for part in [delete_proc.stdout, delete_proc.stderr] if part).strip()
                    if "delete_repo" in detail:
                        self.gum.hint("Your GitHub token needs the delete_repo scope to remove repos automatically.")
                        self.gum.hint("Delete the repo manually, or run: gh auth refresh -h github.com -s delete_repo")
                    else:
                        self.gum.hint("Delete the repo manually on GitHub before trying again.")
            raise

        image_uri = self.published_image_ref(owner)
        summary_lines = [
            "Repository Created",
            "",
            f"Repository: https://github.com/{owner}/{repo}",
            f"Image:      {image_uri}",
            "",
            "GitHub Actions is building your image now.",
            self.scheduled_rebuild_note(),
            "After the first build finishes, switch with:",
            f"sudo bootc switch {image_uri}",
            f"Track the build: https://github.com/{owner}/{repo}/actions",
        ]
        if self.carried_scan_customizations():
            summary_lines.extend(
                [
                    "",
                    "This repo carries over package changes from your current system.",
                    "Before rebooting, run this first in the same session:",
                    "sudo rpm-ostree reset",
                    "Then run the bootc switch command above.",
                ]
            )
        print(
            self.gum.style(
                *summary_lines,
                align="center",
                width=self.gum.content_width(reserve=8),
                margin="1",
                padding="1 2",
                foreground=10,
                border_foreground=10,
                border="double",
            )
        )
        print()
        self.show_managed_repo_warning()
        self.gum.enter_to_continue("Press Enter to return to the main menu...")
        return True

    def test_build_locally(self) -> None:
        if self.config.method != "containerfile":
            self.gum.hint("Local test build is Containerfile-only for now.")
            return
        if not command_exists("podman"):
            self.gum.warn("podman is required to run a local test build.")
            self.gum.hint("Install podman, then try this again.")
            return

        tag = f"{TOOL_SLUG}-local-test:dryrun"
        with tempfile.TemporaryDirectory(prefix=f"{TOOL_SLUG}-local-build.") as tmp:
            tmpdir = Path(tmp)
            self.seed_project_template(tmpdir)
            self.write_project_files(tmpdir, include_workflow=False)
            proc = self.gum.spinner_result(
                "Testing local podman build...",
                ["podman", "build", "-t", tag, str(tmpdir)],
            )
            if proc.returncode == 0:
                self.gum.success(f"Local test build succeeded: {tag}")
                self.gum.hint(f"Try inspecting it with: podman image inspect {tag}")
            else:
                self.gum.error(f"Local podman build failed with exit status {proc.returncode}.")
                stderr = (proc.stderr or "").strip()
                if stderr:
                    tail = "\n".join(stderr.splitlines()[-8:])
                    self.gum.hint(tail)
        self.gum.enter_to_continue("Press Enter to return to the menu...")

    def select_repo(self, *, require_state_file: bool = False) -> tuple[str, str]:
        # This helper centralizes repo picking for update flows. The
        # require_state_file flag is what prevents the normal update path from
        # accidentally operating on unrelated repos.
        if not self.require_github():
            raise ScreenBack()
        while True:
            try:
                repo_data = self.gh_json_with_spinner(
                    "Fetching repositories from GitHub...",
                    ["repo", "list", self.github_user, "--json", "name,description", "--limit", "100"],
                )
            except (CommandError, json.JSONDecodeError):
                self.gum.warn("I couldn't load your repository list from GitHub right now.")
                self.gum.hint("Type a repository name manually if you know one, or press Esc to go back.")
                repo_data = []
            repos = [
                item
                for item in (repo_data if isinstance(repo_data, list) else [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
            visible_repos = repos
            if require_state_file:
                self.gum.hint("Checking which repos were created by this tool...")
                managed = self.batch_check_state_files(self.github_user, repos)
                visible_repos = [item for item in repos if item["name"] in managed]
            if not visible_repos:
                if require_state_file:
                    self.gum.warn("I couldn't find any GitHub repos on your account that were created by this tool yet.")
                    self.gum.hint("Type a repository name manually if you know one, or press Esc to go back.")
                else:
                    self.gum.warn("No repositories found on your GitHub account.")
                    self.gum.hint("Type a repository name manually if you want to check one by name, or press Esc to go back.")
            labels: list[str] = []
            mapping: dict[str, tuple[str, str]] = {}
            for item in visible_repos:
                description = item.get("description") or "(no description)"
                if len(description) > 40:
                    description = description[:37] + "..."
                label = f"{item['name']:<30} {description}"
                labels.append(label)
                mapping[label] = (self.github_user, item["name"])
            manual_label = "Type a repository name manually"
            labels.append(manual_label)
            self.gum.controls("Type to search", "Up/Down move", "Enter choose", "Esc back", "Ctrl+C quit")
            self.menu_section(
                "Next Step",
                "Choose the last option if you want to type a repository name yourself.",
            )
            print()
            choice = self.gum.filter(labels, height=20, placeholder="Search repos...")
            if choice == manual_label:
                repo_input = self.gum.input(
                    prompt="Repository name: ",
                    placeholder=DEFAULT_REPO_NAME,
                    width=self.gum.form_width(max_width=72),
                ).strip()
                if not repo_input:
                    continue
                repo = sanitize_slug(repo_input)
                try:
                    repo_data = self.gh_json(["repo", "view", f"{self.github_user}/{repo}", "--json", "name"])
                except CommandError:
                    self.gum.error(f"{self.github_user}/{repo} was not found on GitHub.")
                    self.gum.enter_to_continue("Press Enter to choose a different repository...")
                    continue
                except json.JSONDecodeError:
                    self.gum.error(f"Unable to confirm {self.github_user}/{repo} on GitHub right now.")
                    self.gum.enter_to_continue("Press Enter to choose a different repository...")
                    continue
                if not isinstance(repo_data, dict) or not isinstance(repo_data.get("name"), str):
                    self.gum.error(f"Unable to confirm {self.github_user}/{repo} on GitHub right now.")
                    self.gum.enter_to_continue("Press Enter to choose a different repository...")
                    continue
                if require_state_file and not self.repo_has_state_file(self.github_user, repo):
                    self.gum.error(f"{self.github_user}/{repo} was not created by this tool.")
                    self.gum.hint(f"This tool can only update repos with `{STATE_FILE}`.")
                    self.gum.hint("Create a new repo with this tool instead, or manage that repo manually.")
                    self.gum.enter_to_continue("Press Enter to choose a different repository...")
                    continue
                return self.github_user, repo
            if choice in mapping:
                return mapping[choice]
            raise ScreenBack()

    def update_existing_image(self) -> None:
        # Update is deliberately limited to repos that already have the tool's
        # canonical state file.
        if not self.require_github():
            return
        try:
            owner, repo = self.select_repo(require_state_file=True)
        except ScreenBack:
            return
        self.config.repo_name = repo
        self.config.github_user = owner
        with tempfile.TemporaryDirectory(prefix=f"{TOOL_SLUG}-update.") as tmp:
            tmpdir = Path(tmp)
            self.clone_repo(owner, repo, tmpdir)
            self.load_repo_config(tmpdir)
            self.config.repo_name = repo
            self.config.github_user = owner
            if self.update_menu(repo_dir=tmpdir):
                self.show_summary()
                print()
                self.push_update(owner, repo, tmpdir)

    def view_build_status(self) -> None:
        repo_dir = Path.cwd()
        try:
            self.load_repo_config(repo_dir)
        except CommandError:
            self.gum.warn(f"Run this from a configured repo that contains `{STATE_FILE}`.")
            return
        owner = self.config.github_user or self.github_user
        repo = self.config.repo_name
        if not owner or not repo:
            self.gum.warn(f"Run this from a configured repo that contains `{STATE_FILE}`.")
            return
        fields = "databaseId,displayTitle,status,conclusion,createdAt,updatedAt,url,workflowName"
        proc = run(["gh", "run", "list", "-R", f"{owner}/{repo}", "--limit", "5", "--json", fields])
        try:
            runs = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            self.gum.error("Unable to read GitHub Actions run data.")
            return
        if not isinstance(runs, list) or not runs:
            self.gum.warn(f"No recent GitHub Actions runs found for {owner}/{repo}.")
            self.gum.enter_to_continue("Press Enter to return to the main menu...")
            return
        self.gum.header("Build Status")
        self.gum.hint(f"Recent GitHub Actions runs for {owner}/{repo}:")
        self.gum.hint("Status  Workflow                Title                                   When         URL")
        now = datetime.now(timezone.utc)
        for item in runs:
            if not isinstance(item, dict):
                continue
            conclusion = item.get("conclusion")
            icon = "✓" if conclusion == "success" else "✗" if conclusion else "●"
            workflow = self.truncate_label(str(item.get("workflowName") or "(workflow)"), limit=22)
            title = self.truncate_label(str(item.get("displayTitle") or f"Run {item.get('databaseId') or ''}").strip(), limit=38)
            created_at = item.get("createdAt")
            when = "unknown"
            if isinstance(created_at, str):
                try:
                    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    delta = now - created
                    if delta.days:
                        when = f"{delta.days}d ago"
                    else:
                        hours = delta.seconds // 3600
                        minutes = (delta.seconds % 3600) // 60
                        when = f"{hours}h ago" if hours else f"{minutes}m ago"
                except ValueError:
                    when = "unknown"
            self.gum.hint(f"{icon:<7} {workflow:<22} {title:<38} {when:<12} {item.get('url') or ''}")
        self.gum.enter_to_continue("Press Enter to return to the main menu...")

    def load_repo_config(self, repo_dir: Path) -> None:
        # Prefer the canonical JSON state file whenever possible. That is what
        # lets update flows be stable instead of reparsing generated shell.
        state_path = repo_dir / STATE_FILE
        if not state_path.exists():
            raise CommandError(
                f"This repo does not contain `{STATE_FILE}`, so it was not created by this tool. "
                "Only repos created by this tool are supported for updates."
            )
        try:
            data = json.loads(state_path.read_text())
            cfg = config_from_state_payload(data)
        except ValueError as exc:
            raise CommandError(
                f"This repo's saved settings file `{STATE_FILE}` is missing or broken. "
                "Restore it from Git, or stop using this tool for this repo."
            ) from exc
        except (json.JSONDecodeError, TypeError, OSError) as exc:
            raise CommandError(
                f"This repo's saved settings file `{STATE_FILE}` is missing or broken. "
                "Restore it from Git, or stop using this tool for this repo."
            ) from exc
        self.config = cfg
        if not self.github_user:
            self.github_user = cfg.github_user

    def update_menu(self, repo_dir: Path | None = None) -> bool:
        # Update uses a task-list style menu instead of the linear create wizard,
        # because returning users usually want to jump straight to one section.
        while True:
            self.gum.header("Update Image")
            self.menu_section(
                "Next Step",
                "Choose a section to review or change.",
                "Save and push changes when you are finished, or cancel to go back.",
            )
            print()
            mapping: dict[str, str] = {}
            options: list[str] = []
            for title, status in self.update_task_choices():
                label = self.format_task_choice(title, status)
                mapping[label] = title
                options.append(label)
            review_label = "Review current configuration"
            local_build_label = "Test build locally (podman)"
            rotate_label = "Rotate signing key (cosign)"
            save_label = "Save and push changes"
            cancel_label = "Cancel and go back"
            options.append(review_label)
            if self.config.method == "containerfile":
                options.append(local_build_label)
            options.append(rotate_label)
            options.extend([save_label, cancel_label])
            try:
                choice = self.gum.choose(options, height=14)
            except ScreenBack:
                return False
            selected = choice[0] if choice else cancel_label
            if selected == save_label:
                self.config.normalize()
                return True
            if selected == cancel_label:
                return False
            if selected == review_label:
                self.show_summary(next_hint="This is the full configuration summary.")
                continue
            if selected == local_build_label:
                self.test_build_locally()
                continue
            if selected == rotate_label:
                self.rotate_signing_key(repo_dir)
                continue
            task = mapping[selected]
            try:
                if task == "Packages":
                    self.manage_packages()
                elif task == "Base image":
                    previous_base_uri = self.config.base_image_uri
                    previous_base_name = self.config.base_image_name
                    self.config.base_image_uri = ""
                    self.config.base_image_name = ""
                    try:
                        self.choose_base_image()
                    except ScreenBack:
                        self.config.base_image_uri = previous_base_uri
                        self.config.base_image_name = previous_base_name
                        raise
                elif task == "Description":
                    self.edit_description()
                elif task == "COPR repositories":
                    self.manage_copr_repos()
                elif task == "Services":
                    self.manage_services()
                elif task == "Removed base packages":
                    self.manage_removed_packages()
                elif task == "Homebrew":
                    self.offer_brew_if_applicable()
            except ScreenBack:
                continue

    def manage_packages(self) -> None:
        while True:
            self.gum.header("Edit Packages")
            self.gum.hint("Choose how you want to change packages.")
            self.render_package_menu_intro(
                packages_empty="None selected",
                next_step_hint="Choose Back to return to the update menu and keep the changes you already made here.",
            )
            print()
            try:
                choice = self.gum.choose(
                    ["Search package names", "Type exact package names", "Remove packages", "Back"],
                    height=8,
                )
            except ScreenBack:
                return
            selected = choice[0] if choice else "Back"
            if selected == "Back":
                return
            try:
                if selected == "Search package names":
                    self.search_packages()
                elif selected == "Type exact package names":
                    self.manual_packages()
                elif selected == "Remove packages":
                    self.config.packages = self.choose_to_remove(self.config.packages, "Remove Packages")
            except ScreenBack:
                continue

    def manage_copr_repos(self) -> None:
        while True:
            self.gum.header("Edit COPR Repositories")
            self.menu_section(
                "Next Step",
                "Choose how you want to change COPR repositories.",
                "Choose Back to return to the update menu and keep the changes you already made here.",
            )
            print()
            try:
                choice = self.gum.choose(
                    ["Add a COPR repository", "Remove a COPR repository", "Back"],
                    height=6,
                )
            except ScreenBack:
                return
            selected = choice[0] if choice else "Back"
            if selected == "Back":
                return
            try:
                if selected == "Add a COPR repository":
                    self.add_copr()
                elif selected == "Remove a COPR repository":
                    self.config.copr_repos = self.choose_to_remove(self.config.copr_repos, "Remove COPR Repos")
            except ScreenBack:
                continue

    def edit_description(self) -> None:
        self.gum.header("Edit Description")
        self.menu_section(
            "Description",
            "Enter a short description for this image.",
            "Leave it empty if you want to keep the current description.",
        )
        print()
        value = self.gum.input(
            prompt="New description: ",
            placeholder=self.config.image_desc,
            width=self.gum.form_width(max_width=110),
        )
        if value:
            self.config.image_desc = value

    def choose_to_remove(self, values: list[str], header: str) -> list[str]:
        if not values:
            self.gum.warn("Nothing to remove.")
            return values
        self.gum.header(header)
        self.gum.controls("Up/Down move", "x select", "Enter save", "Esc back", "Ctrl+C quit")
        self.menu_section("Selection", "Leave everything unselected if you want to keep everything.")
        print()
        selected = set(
            self.gum.choose(
                values,
                no_limit=True,
                height=20,
                selected_prefix="[x] ",
                unselected_prefix="[ ] ",
            )
        )
        return [value for value in values if value not in selected]

    def manage_services(self) -> None:
        self.gum.header("Edit Services")
        self.gum.controls("Up/Down move", "Enter choose", "Esc back", "Ctrl+C quit")
        self.menu_section(
            "Next Step",
            "Choose Back to return to the previous menu and keep the changes you already made here.",
        )
        print()
        try:
            choice = self.gum.choose(["Add services", "Remove services", "Back"], height=5)
        except ScreenBack:
            return
        selected = choice[0] if choice else "Back"
        if selected == "Add services":
            self.add_services()
        elif selected == "Remove services":
            self.config.services = self.choose_to_remove(self.config.services, "Remove Services")

    def manage_removed_packages(self) -> None:
        self.gum.header("Edit Removed Base Packages")
        self.menu_section(
            "What This Does",
            "These are packages you want removed from the base image.",
            "Choose Add to type package names to remove, or Remove to stop removing packages you already listed.",
            "Choose Back to return to the update menu. Changes are kept automatically.",
        )
        print()
        try:
            choice = self.gum.choose(["Add package names to remove", "Stop removing listed packages", "Back"], height=5)
        except ScreenBack:
            return
        selected = choice[0] if choice else "Back"
        if selected == "Add package names to remove":
            self.menu_section(
                "What To Enter",
                "Enter exact RPM package names separated by spaces or newlines.",
                "Leave this empty if you want to go back.",
            )
            raw = self.gum.write(
                placeholder="Enter package names, one per line...",
                height=6,
                width=self.gum.form_width(max_width=90),
            )
            packages = [token.strip(",") for token in raw.split() if token.strip(",")]
            if not packages:
                return
            before_count = len(self.config.removed_packages)
            added = self.add_removed_packages_to_config(packages, source_label="manual entry")
            added_count = len(self.config.removed_packages) - before_count
            if added and not self.last_manual_removed_package_check_had_missing:
                self.gum.enter_to_continue(f"Added {added_count} package removal(s). Press Enter to return to the update menu...")
                return
            if added and self.last_manual_removed_package_check_had_missing:
                self.gum.enter_to_continue("Finished checking package removals. Press Enter to return to the update menu...")
                return
            self.gum.enter_to_continue("No package removals were added. Press Enter to return to the update menu...")
        elif selected == "Stop removing listed packages":
            self.config.removed_packages = self.choose_to_remove(self.config.removed_packages, "Remove Base Package Removals")

    def push_update(self, owner: str, repo: str, repo_dir: Path) -> None:
        # The update path rewrites files in a temporary clone, shows the diff,
        # and only then asks for confirmation before pushing.
        #
        # write_project_files is called twice intentionally:
        #   1) First write: generates files WITHOUT signing so the user can
        #      preview the diff before any secrets are created or rotated.
        #   2) Second write: after ensure_signing_ready() uploads the cosign
        #      keypair, regenerates files WITH signing enabled so the workflow
        #      includes the cosign steps.  If the diff changes, the user is
        #      asked to re-confirm before pushing.
        self.generated_cosign_pub = None
        self.config.signing_enabled = True
        default_branch = self.repo_default_branch(owner, repo)
        self.write_project_files(repo_dir, include_workflow=True, default_branch=default_branch)
        diff = self.repo_diff_summary(repo_dir)
        if not diff:
            self.gum.warn("No changes detected.")
            return
        print(diff)
        print()
        self.show_managed_repo_warning()
        print()
        if self.gum.confirm("View full diff?", default=False):
            full_diff = self.repo_full_diff(repo_dir)
            self.gum.pager(self.pager_text_with_hint(full_diff))
        if not self.gum.confirm(f"Push changes to {owner}/{repo}?", default=True):
            return
        self.config.signing_enabled = self.ensure_signing_ready(owner, repo, repo_dir=repo_dir)
        self.write_project_files(repo_dir, include_workflow=True, default_branch=default_branch)
        final_diff = self.repo_diff_summary(repo_dir)
        if not final_diff:
            self.gum.warn("No changes detected.")
            return
        if final_diff != diff:
            self.gum.warn("The final update changed after signing was prepared.")
            print(final_diff)
            print()
            if self.gum.confirm("View final full diff?", default=False):
                final_full_diff = self.repo_full_diff(repo_dir)
                self.gum.pager(self.pager_text_with_hint(final_full_diff))
            if not self.gum.confirm(f"Push final changes to {owner}/{repo}?", default=True):
                return
        self.configure_temp_repo_git_identity(repo_dir)
        run(["git", "add", "-A"], cwd=repo_dir)
        run(["git", "commit", "-m", f"Update image configuration via {TOOL_SLUG} v{VERSION}"], cwd=repo_dir)
        run(["git", "push", "origin", "HEAD"], cwd=repo_dir, capture=False)
        self.gum.success(f"Pushed changes to {owner}/{repo}.")
        self.gum.enter_to_continue("Press Enter to return to the main menu...")

    def repo_diff_summary(self, repo_dir: Path) -> str:
        diff = run(["git", "diff", "--stat"], cwd=repo_dir, check=False).stdout.strip()
        if not diff:
            diff = run(["git", "status", "--porcelain"], cwd=repo_dir, check=False).stdout.strip()
        return diff

    def repo_full_diff(self, repo_dir: Path) -> str:
        parts: list[str] = []
        tracked_diff = run(["git", "diff"], cwd=repo_dir, check=False).stdout.strip()
        if tracked_diff:
            parts.append(tracked_diff)
        status = run(["git", "status", "--porcelain"], cwd=repo_dir, check=False).stdout
        for line in status.splitlines():
            if not line.startswith("?? "):
                continue
            path = line[3:]
            untracked_diff = run(["git", "diff", "--no-index", "--", "/dev/null", path], cwd=repo_dir, check=False).stdout.strip()
            if untracked_diff:
                parts.append(untracked_diff)
        if not parts:
            return ""
        return "\n\n".join(parts).rstrip("\n") + "\n"

    def pager_text_with_hint(self, text: str) -> str:
        hint = "Press q to close this diff and return to the previous screen."
        body = text.rstrip("\n")
        if not body:
            return hint + "\n"
        return f"{hint}\n\n{body}\n"

    def read_only_pager_text(self, title: str, lines: Sequence[str]) -> str:
        hint = "Press q to close this screen and return to the previous menu."
        body = "\n".join(lines).rstrip()
        if not body:
            return f"{title}\n\n{hint}\n"
        return f"{title}\n\n{hint}\n\n{body}\n"

    def format_key_value_rows(self, rows: Sequence[tuple[str, str]]) -> list[str]:
        if not rows:
            return []
        label_width = max(len(label) for label, _value in rows)
        return [f"{label:<{label_width}}  {value}" for label, value in rows]

    def validate_token_list(self, values: list[str], pattern: re.Pattern[str], label: str) -> None:
        # Validation happens before generating shell/YAML so bad values fail here
        # instead of turning into broken repo files or command injection risks.
        invalid = [value for value in values if not pattern.fullmatch(value)]
        if invalid:
            sample = ", ".join(invalid[:3])
            raise CommandError(f"Invalid {label} value(s): {sample}")

    def validate_config(self) -> None:
        # This is the final safety gate before any files are rendered or pushed.
        # It combines structural checks (repo name, base image) with token-level
        # checks for anything that will land in scripts or workflows.
        self.config.normalize()
        if self.config.method not in ALLOWED_METHODS:
            raise CommandError("Choose a supported build method before writing project files.")
        if not self.config.base_image_uri or re.search(r"\s", self.config.base_image_uri):
            raise CommandError("Base image URI is missing or invalid.")
        if not self.match_base_image(self.config.base_image_uri):
            raise CommandError(f"Choose one of the supported base images: {supported_base_image_names()}.")
        self.validate_token_list(self.config.packages, PACKAGE_TOKEN_RE, "package")
        self.validate_token_list(self.config.removed_packages, PACKAGE_TOKEN_RE, "removed package")
        self.validate_token_list(self.config.copr_repos, COPR_REPO_RE, "COPR repository")
        self.validate_token_list(self.config.services, SERVICE_TOKEN_RE, "systemd service")
        if not is_valid_repo_name(self.config.repo_name):
            raise CommandError(
                "Repository name is invalid. It must start and end with a letter or number, and it cannot end with .git."
            )

    def state_payload(self) -> dict[str, object]:
        # The JSON state file is the canonical source of truth for future
        # updates. Generated files are considered outputs, not the primary state.
        self.validate_config()
        payload = asdict(self.config)
        payload["tool_version"] = VERSION
        payload["state_version"] = 1
        return payload

    def render_containerfile(self, existing_text: str | None = None) -> str:
        # If the template already has a Containerfile, replace the FROM line and
        # inject or remove the brew block so we preserve upstream formatting and
        # comments where possible.
        if existing_text:
            lines = existing_text.splitlines()
            for index, line in enumerate(lines):
                match = FROM_LINE_RE.match(line)
                if not match:
                    continue
                prefix, image, suffix = match.groups()
                if image.lower() == "scratch":
                    continue
                lines[index] = f"{prefix}{self.config.base_image_uri}{suffix}"
                lines = self._patch_brew_block(lines, from_index=index)
                return ensure_trailing_newline("\n".join(lines))
            return ensure_trailing_newline(existing_text)
        return self.generate_containerfile()

    def _patch_brew_block(self, lines: list[str], *, from_index: int) -> list[str]:
        # Find existing brew COPY line if present.
        brew_start: int | None = None
        brew_end: int | None = None
        for i, line in enumerate(lines):
            if line.strip().startswith("COPY --from=") and "brew" in line.lower() and "/system_files" in line:
                brew_start = i
                # The block includes a following RUN for systemctl preset.
                brew_end = i + 1
                for j in range(i + 1, len(lines)):
                    stripped = lines[j].strip()
                    if not stripped:
                        brew_end = j + 1
                        break
                    if stripped.endswith("\\"):
                        brew_end = j + 1
                        continue
                    brew_end = j + 1
                    break
                # Consume a trailing blank line so removal/replacement
                # does not leave a double blank.
                if brew_end < len(lines) and not lines[brew_end].strip():
                    brew_end += 1
                break

        if not self.config.brew_enabled:
            # Remove the brew block if it exists.
            if brew_start is not None:
                return lines[:brew_start] + lines[brew_end:]
            return lines

        # brew_enabled: inject or replace the block.
        brew_lines = [
            f"COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /",
            "RUN --mount=type=cache,dst=/var/cache \\",
            "    --mount=type=cache,dst=/var/log \\",
            "    --mount=type=tmpfs,dst=/tmp \\",
            "    /usr/bin/systemctl preset brew-setup.service && \\",
            "    /usr/bin/systemctl preset brew-update.timer && \\",
            "    /usr/bin/systemctl preset brew-upgrade.timer",
            "",
        ]
        if brew_start is not None:
            return lines[:brew_start] + brew_lines + lines[brew_end:]
        # Insert after the FROM line (and any blank line following it).
        insert_at = from_index + 1
        if insert_at < len(lines) and not lines[insert_at].strip():
            insert_at += 1
        return lines[:insert_at] + brew_lines + lines[insert_at:]

    def patch_container_justfile(self, existing_text: str) -> str:
        # The template Justfile already has sensible defaults; we only patch the
        # image name so the local build target matches the chosen repo name.
        updated = re.sub(
            r'^export image_name := env\("IMAGE_NAME",\s*"[^"]*"\)(.*)$',
            f'export image_name := env("IMAGE_NAME", "{self.config.repo_name}")\\1',
            existing_text,
            count=1,
            flags=re.MULTILINE,
        )
        return ensure_trailing_newline(updated)

    def patch_container_workflow(self, existing_text: str, *, default_branch: str = "main") -> str:
        # This patcher updates the bundled template workflow in place. The main
        # goals are:
        # - pin actions to SHAs
        # - keep our state file out of push triggers
        # - wire in image description and signing conditions safely
        branch_if = "github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
        sign_if = f"{branch_if} && env.COSIGN_PRIVATE_KEY != ''"
        lines = existing_text.splitlines()
        output: list[str] = []
        state_ignore_present = any(STATE_FILE in line for line in lines)
        paths_ignore_inserted = False
        for line in lines:
            line = pin_action_uses_line(line)
            stripped = line.strip()
            if stripped.startswith("- cron:"):
                indent = line[: len(line) - len(line.lstrip())]
                output.append(f"{indent}- cron: '{DEFAULT_GITHUB_BUILD_CRON}'")
                continue
            if stripped.startswith("paths-ignore:") and not state_ignore_present and not paths_ignore_inserted:
                output.append(line)
                inline_match = re.match(r"^(\s*paths-ignore:\s*)\[(.*)\](\s*)$", line)
                if inline_match:
                    prefix, items, suffix = inline_match.groups()
                    items = items.strip()
                    if items:
                        output[-1] = f"{prefix}[{items}, '{STATE_FILE}']{suffix}"
                    else:
                        output[-1] = f"{prefix}['{STATE_FILE}']{suffix}"
                    paths_ignore_inserted = True
                    continue
                paths_ignore_indent = line[: len(line) - len(line.lstrip())] + "  "
                output.append(f"{paths_ignore_indent}- '{STATE_FILE}'")
                paths_ignore_inserted = True
                continue
            if stripped in {"- '**/README.md'", '- "**/README.md"'} and not state_ignore_present and not paths_ignore_inserted:
                output.append(line)
                output.append(f"{line[: len(line) - len(line.lstrip())]}- '{STATE_FILE}'")
                paths_ignore_inserted = True
                continue
            if stripped.startswith("IMAGE_DESC:"):
                output.append(f"  IMAGE_DESC: {yaml_scalar(self.config.image_desc)}")
                continue
            output.append(line)
        text = patch_workflow_signing_steps("\n".join(output), branch_if=branch_if, sign_if=sign_if)
        text = self.patch_workflow_branch_filters(text, default_branch)
        text = ensure_workflow_job_env_entries(
            text,
            [
                ("COSIGN_PRIVATE_KEY", "${{ secrets.SIGNING_SECRET }}"),
                ("COSIGN_PASSWORD", "${{ secrets.COSIGN_PASSWORD }}"),
            ],
        )
        return ensure_trailing_newline(text)

    def patch_container_disk_workflow(self, existing_text: str, *, default_branch: str = "main") -> str:
        lines = [pin_action_uses_line(line) for line in existing_text.splitlines()]
        return self.patch_workflow_branch_filters("\n".join(lines), default_branch)

    def patch_workflow_branch_filters(self, workflow_text: str, default_branch: str) -> str:
        lines = workflow_text.splitlines()
        output: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if indent == 2 and stripped in {"pull_request:", "push:"}:
                output.append(line)
                index += 1
                block_start = index
                while index < len(lines):
                    block_line = lines[index]
                    block_stripped = block_line.strip()
                    block_indent = len(block_line) - len(block_line.lstrip())
                    if block_indent <= 2 and block_stripped.endswith(":"):
                        break
                    index += 1
                block = lines[block_start:index]
                branch_block = ["    branches:", f"      - {default_branch}"]
                patched_block: list[str] = []
                branches_found = False
                block_index = 0
                while block_index < len(block):
                    block_line = block[block_index]
                    block_stripped = block_line.strip()
                    block_indent = len(block_line) - len(block_line.lstrip())
                    if block_indent == 4 and block_stripped == "branches:":
                        prefix = block_line[: len(block_line) - len(block_line.lstrip())]
                        patched_block.append(block_line)
                        patched_block.append(f"{prefix}  - {default_branch}")
                        branches_found = True
                        block_index += 1
                        while block_index < len(block):
                            branch_line = block[block_index]
                            branch_stripped = branch_line.strip()
                            branch_indent = len(branch_line) - len(branch_line.lstrip())
                            if branch_indent == 6 and branch_stripped.startswith("- "):
                                block_index += 1
                                continue
                            break
                        continue
                    patched_block.append(block_line)
                    block_index += 1
                if branches_found:
                    output.extend(patched_block)
                else:
                    output.extend(branch_block + block)
                continue
            output.append(line)
            index += 1
        return ensure_trailing_newline("\n".join(output))

    def patch_bluebuild_action_inputs(self, workflow_text: str) -> str:
        lines = workflow_text.splitlines()
        output: list[str] = []
        current_step: list[str] = []
        in_steps = False
        steps_indent: int | None = None

        def patch_step(step_lines: list[str]) -> list[str]:
            if not step_lines:
                return []
            if not any(re.search(r"uses:\s+blue-build/github-action@", line) for line in step_lines):
                return step_lines
            with_index: int | None = None
            entry_prefix = ""
            for idx, step_line in enumerate(step_lines):
                if step_line.strip() == "with:":
                    with_index = idx
                    entry_prefix = step_line[: len(step_line) - len(step_line.lstrip())] + "  "
                    break
            if with_index is None:
                return step_lines
            wanted_lines = [
                f"{entry_prefix}push: ${{{{ github.event_name != 'pull_request' && github.ref == format('refs/heads/{{0}}', github.event.repository.default_branch) && 'true' || 'false' }}}}",
                f"{entry_prefix}build_opts: ${{{{ github.event_name == 'pull_request' && '--no-sign' || '' }}}}",
            ]
            missing = [line for line in wanted_lines if line not in step_lines]
            if not missing:
                return step_lines
            return step_lines[: with_index + 1] + missing + step_lines[with_index + 1 :]

        def flush_step() -> None:
            nonlocal current_step
            if not current_step:
                return
            output.extend(patch_step(current_step))
            current_step = []

        for line in lines:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())

            if in_steps and steps_indent is not None and indent <= steps_indent and stripped and not stripped.startswith("#"):
                flush_step()
                in_steps = False
                steps_indent = None

            if stripped == "steps:":
                flush_step()
                in_steps = True
                steps_indent = indent
                output.append(line)
                continue

            if in_steps and steps_indent is not None and indent == steps_indent + 2 and stripped.startswith("- "):
                flush_step()
                current_step = [line]
                continue

            if current_step:
                current_step.append(line)
            else:
                output.append(line)

        flush_step()
        return ensure_trailing_newline("\n".join(output))

    def patch_bluebuild_workflow(self, existing_text: str, *, default_branch: str = "main") -> str:
        # The BlueBuild workflow is simpler than the Containerfile one: a single
        # monolithic action handles the build. We pin the action, update the
        # schedule, add state-file ignore, and fix branch filters.
        lines = existing_text.splitlines()
        output: list[str] = []
        state_ignore_present = any(STATE_FILE in line for line in lines)
        paths_ignore_inserted = False
        for line in lines:
            line = pin_action_uses_line(line)
            stripped = line.strip()
            if stripped.startswith("- cron:"):
                indent = line[: len(line) - len(line.lstrip())]
                output.append(f"{indent}- cron: '{DEFAULT_GITHUB_BUILD_CRON}'")
                continue
            if stripped.startswith("paths-ignore:") and not state_ignore_present and not paths_ignore_inserted:
                output.append(line)
                paths_ignore_indent = line[: len(line) - len(line.lstrip())] + "  "
                output.append(f"{paths_ignore_indent}- '{STATE_FILE}'")
                paths_ignore_inserted = True
                continue
            if stripped in {'- "**.md"', "- '**.md'"} and not state_ignore_present and not paths_ignore_inserted:
                output.append(line)
                output.append(f"{line[: len(line) - len(line.lstrip())]}- '{STATE_FILE}'")
                paths_ignore_inserted = True
                continue
            output.append(line)
        text = "\n".join(output)
        text = self.patch_workflow_branch_filters(text, default_branch)
        text = self.patch_bluebuild_action_inputs(text)
        return ensure_trailing_newline(text)

    def installer_profile(self) -> str:
        matched = self.match_base_image(self.config.base_image_uri)
        if matched and matched.key in {"bazzite", "bazzite-dx", "aurora", "aurora-dx", "kinoite"}:
            return "kde"
        return "gnome"

    def installer_config_name(self) -> str:
        return f"iso-{self.installer_profile()}.toml"

    def patch_installer_config(self, existing_text: str) -> str:
        lines = existing_text.splitlines()
        image_ref = self.published_image_ref()
        for index, line in enumerate(lines):
            match = INSTALLER_SWITCH_RE.match(line)
            if not match:
                continue
            prefix, _current_ref, suffix = match.groups()
            lines[index] = f"{prefix}{image_ref}{suffix}"
            break
        return ensure_trailing_newline("\n".join(lines))

    def write_installer_configs(self, base_dir: Path) -> None:
        disk_dir = base_dir / "disk_config"
        if not disk_dir.exists():
            return
        for name in ("iso-gnome.toml", "iso-kde.toml"):
            path = disk_dir / name
            if path.exists():
                path.write_text(self.patch_installer_config(path.read_text()))
        selected_name = self.installer_config_name()
        selected_path = disk_dir / selected_name
        if selected_path.exists():
            iso_text = selected_path.read_text()
        else:
            template_path = CONTAINERFILE_TEMPLATE_DIR / "disk_config" / selected_name
            if not template_path.is_file():
                raise CommandError(f"Bundled installer config not found: {selected_name}")
            iso_text = template_path.read_text()
        (disk_dir / "iso.toml").write_text(self.patch_installer_config(iso_text))

    def _split_image_ref(self, uri: str) -> tuple[str, str]:
        # BlueBuild recipes separate "base-image" and "image-version" so we need
        # to split a combined URI like "ghcr.io/ublue-os/bazzite:stable".
        if ":" in uri:
            base, tag = uri.rsplit(":", 1)
            return base, tag
        return uri, "latest"

    def generate_recipe(self) -> str:
        # Generate a BlueBuild recipe YAML from Config without needing pyyaml.
        # Values that might need quoting use yaml_scalar(); plain tokens like
        # package names are safe because validate_config() already checked them.
        base_image, image_version = self._split_image_ref(self.config.base_image_uri)
        lines = [
            "---",
            f"# yaml-language-server: $schema={BLUEBUILD_RECIPE_SCHEMA}",
            f"name: {self.config.repo_name}",
            f"description: {yaml_scalar(self.config.image_desc)}",
            "",
            f"base-image: {base_image}",
            f"image-version: {yaml_scalar(image_version)}",
            "",
            "modules:",
            "  - type: files",
            "    files:",
            "      - source: system",
            "        destination: /",
        ]

        if self.config.brew_enabled:
            lines.extend([
                "",
                "  - type: containerfile",
                "    snippets:",
                f"      - COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /",
                "      - |",
                "        RUN --mount=type=cache,dst=/var/cache \\",
                "            --mount=type=cache,dst=/var/log \\",
                "            --mount=type=tmpfs,dst=/tmp \\",
                "            /usr/bin/systemctl preset brew-setup.service && \\",
                "            /usr/bin/systemctl preset brew-update.timer && \\",
                "            /usr/bin/systemctl preset brew-upgrade.timer",
            ])

        if self.config.copr_repos or self.config.packages or self.config.removed_packages:
            lines.extend(["", "  - type: dnf"])
            if self.config.copr_repos:
                lines.extend(["    repos:", "      copr:"])
                for repo in self.config.copr_repos:
                    lines.append(f"        - {repo}")
            if self.config.packages:
                lines.extend(["    install:", "      packages:"])
                for pkg in self.config.packages:
                    lines.append(f"        - {pkg}")
            if self.config.removed_packages:
                lines.extend(["    remove:", "      packages:"])
                for pkg in self.config.removed_packages:
                    lines.append(f"        - {pkg}")

        if self.config.services:
            lines.extend(["", "  - type: systemd", "    system:", "      enabled:"])
            for svc in self.config.services:
                lines.append(f"        - {svc}")

        lines.extend(["", "  - type: signing"])
        return "\n".join(lines) + "\n"

    def write_bluebuild_project_files(self, base_dir: Path, *, include_workflow: bool, default_branch: str = "main") -> None:
        # This is the "materialize the repo" step for BlueBuild mode. The recipe
        # YAML replaces the Containerfile + build.sh used by Containerfile mode.
        readme_path = base_dir / "README.md"
        gitignore_path = base_dir / ".gitignore"
        recipe_path = base_dir / "recipes" / "recipe.yml"
        workflow_path = base_dir / ".github/workflows/build.yml"

        readme_path.write_text(self.generate_readme())

        existing_gitignore = gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
        for entry in ["cosign.key", "cosign.private"]:
            if entry not in existing_gitignore:
                existing_gitignore.append(entry)
        gitignore_path.write_text(ensure_trailing_newline("\n".join(existing_gitignore)))

        recipe_path.parent.mkdir(parents=True, exist_ok=True)
        recipe_path.write_text(self.generate_recipe())

        if self.generated_cosign_pub is not None:
            (base_dir / "cosign.pub").write_text(ensure_trailing_newline(self.generated_cosign_pub))

        if include_workflow:
            workflow_path.parent.mkdir(parents=True, exist_ok=True)
            if not workflow_path.exists():
                # Restore from the bundled template snapshot so updates can
                # recreate a workflow that was manually deleted.
                template_workflow = BLUEBUILD_TEMPLATE_DIR / ".github/workflows/build.yml"
                if template_workflow.exists():
                    shutil.copy2(template_workflow, workflow_path)
            if workflow_path.exists():
                workflow_path.write_text(self.patch_bluebuild_workflow(workflow_path.read_text(), default_branch=default_branch))

    def write_container_project_files(self, base_dir: Path, *, include_workflow: bool, default_branch: str = "main") -> None:
        # This is the "materialize the repo" step for Containerfile mode. It
        # patches template-owned files where possible and generates tool-owned
        # files where needed.
        readme_path = base_dir / "README.md"
        gitignore_path = base_dir / ".gitignore"
        justfile_path = base_dir / "Justfile"
        containerfile_path = base_dir / "Containerfile"
        workflow_path = base_dir / ".github/workflows/build.yml"

        readme_path.write_text(self.generate_readme())

        existing_gitignore = gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
        for entry in ["cosign.key", "_build*/", "output/"]:
            if entry not in existing_gitignore:
                existing_gitignore.append(entry)
        gitignore_path.write_text(ensure_trailing_newline("\n".join(existing_gitignore)))

        (base_dir / "build_files").mkdir(parents=True, exist_ok=True)
        existing_containerfile = containerfile_path.read_text() if containerfile_path.exists() else None
        containerfile_path.write_text(self.render_containerfile(existing_containerfile))
        build_sh = base_dir / "build_files/build.sh"
        build_sh.write_text(self.generate_build_sh())
        build_sh.chmod(0o755)
        if self.generated_cosign_pub is not None:
            (base_dir / "cosign.pub").write_text(ensure_trailing_newline(self.generated_cosign_pub))

        if justfile_path.exists():
            justfile_path.write_text(self.patch_container_justfile(justfile_path.read_text()))
        else:
            justfile_path.write_text(self.generate_justfile())

        self.write_installer_configs(base_dir)

        if include_workflow:
            workflow_path.parent.mkdir(parents=True, exist_ok=True)
            if workflow_path.exists():
                workflow_path.write_text(self.patch_container_workflow(workflow_path.read_text(), default_branch=default_branch))
            else:
                workflow_path.write_text(self.generate_container_workflow(default_branch=default_branch))
            disk_workflow_path = base_dir / ".github/workflows/build-disk.yml"
            if disk_workflow_path.exists():
                disk_workflow_path.write_text(self.patch_container_disk_workflow(disk_workflow_path.read_text(), default_branch=default_branch))

    def write_project_files(self, base_dir: Path, *, include_workflow: bool, default_branch: str = "main") -> None:
        # Always write the canonical state file first. That way the repo can be
        # updated later even if a human edits generated files by hand.
        self.validate_config()
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / STATE_FILE).write_text(json.dumps(self.state_payload(), indent=2) + "\n")
        if self.config.method == "bluebuild":
            self.write_bluebuild_project_files(base_dir, include_workflow=include_workflow, default_branch=default_branch)
        else:
            self.write_container_project_files(base_dir, include_workflow=include_workflow, default_branch=default_branch)

    def generate_containerfile(self) -> str:
        # The Containerfile is intentionally small. Most customization lives in
        # build_files/build.sh so users can inspect a simpler mutation layer.
        lines = [
            "FROM scratch AS ctx",
            "COPY build_files /",
            "",
            f"FROM {self.config.base_image_uri}",
            "",
        ]
        if self.config.brew_enabled:
            lines.extend([
                f"COPY --from={UNIVERSAL_BLUE_BREW_IMAGE} /system_files /",
                "RUN --mount=type=cache,dst=/var/cache \\",
                "    --mount=type=cache,dst=/var/log \\",
                "    --mount=type=tmpfs,dst=/tmp \\",
                "    /usr/bin/systemctl preset brew-setup.service && \\",
                "    /usr/bin/systemctl preset brew-update.timer && \\",
                "    /usr/bin/systemctl preset brew-upgrade.timer",
                "",
            ])
        lines.extend([
            "RUN --mount=type=bind,from=ctx,source=/,target=/ctx \\",
            "    --mount=type=cache,dst=/var/cache \\",
            "    --mount=type=cache,dst=/var/log \\",
            "    --mount=type=tmpfs,dst=/tmp \\",
            "    /ctx/build.sh",
            "",
            "RUN bootc container lint",
            "",
        ])
        return "\n".join(lines)

    def generate_build_sh(self) -> str:
        # build.sh is where user selections become actual package/service
        # changes inside the image. Values are shell-quoted before this point.
        lines = ["#!/bin/bash", "", "set -ouex pipefail", ""]
        if self.config.copr_repos:
            lines.append("# Enable COPR repositories")
            for repo in self.config.copr_repos:
                lines.append(f"dnf5 -y copr enable {shell_quote(repo)}")
            lines.append("")
        if self.config.removed_packages:
            lines.append("# Remove packages from the base image when they are installed")
            lines.append("packages_to_remove=()")
            lines.append("for pkg in \\")
            for pkg in self.config.removed_packages:
                lines.append(f"    {shell_quote(pkg)} \\")
            lines[-1] = lines[-1].removesuffix(" \\")
            lines.extend(
                [
                    "do",
                    '    if rpm -q --quiet "$pkg"; then',
                    '        packages_to_remove+=("$pkg")',
                    "    else",
                    '        echo "Skipping removal of $pkg because it is not installed in the base image."',
                    "    fi",
                    "done",
                    'if ((${#packages_to_remove[@]})); then',
                    '    dnf5 remove -y "${packages_to_remove[@]}"',
                    "fi",
                    "",
                ]
            )
        if self.config.packages:
            lines.append("# Install packages")
            lines.append("dnf5 install -y \\")
            for index, pkg in enumerate(self.config.packages):
                suffix = " \\" if index < len(self.config.packages) - 1 else ""
                lines.append(f"    {shell_quote(pkg)}{suffix}")
            lines.append("")
        else:
            lines.extend(["# dnf5 install -y <your-packages-here>", ""])
        if self.config.copr_repos:
            lines.append("# Disable COPRs so they do not persist in the final image")
            for repo in self.config.copr_repos:
                lines.append(f"dnf5 -y copr disable {shell_quote(repo)}")
            lines.append("")
        if self.config.copr_repos or self.config.removed_packages or self.config.packages:
            lines.append("# Clean dnf metadata before the final image is committed")
            lines.append("dnf5 clean all")
            lines.append("")
        if self.config.services:
            lines.append("# Enable systemd services")
            for service in self.config.services:
                lines.append(f"systemctl enable {shell_quote(service)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def generate_container_workflow(self, *, default_branch: str = "main") -> str:
        # This is the GitHub Actions workflow for repos generated from scratch
        # instead of patched from an existing template copy.
        sign_if = "github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && env.COSIGN_PRIVATE_KEY != ''"
        lines = [
            "---",
            "name: Build container image",
            "on:",
            "  pull_request:",
            "    branches:",
            f"      - {default_branch}",
            "  schedule:",
            f"    - cron: '{DEFAULT_GITHUB_BUILD_CRON}'",
            "  push:",
            "    branches:",
            f"      - {default_branch}",
            f"    paths-ignore: ['**/README.md', '{STATE_FILE}']",
            "  workflow_dispatch:",
            "",
            "env:",
            f"  IMAGE_DESC: {yaml_scalar(self.config.image_desc)}",
            '  IMAGE_NAME: "${{ github.event.repository.name }}"',
            '  IMAGE_REGISTRY: "ghcr.io/${{ github.repository_owner }}"',
            '  DEFAULT_TAG: "latest"',
            "",
            "concurrency:",
            "  group: ${{ github.workflow }}-${{ github.ref || github.run_id }}",
            "  cancel-in-progress: true",
            "",
            "jobs:",
            "  build_push:",
            "    runs-on: ubuntu-24.04",
            "    permissions:",
            "      contents: read",
            "      packages: write",
            "      id-token: write",
            "    env:",
            "      COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}",
            "      COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}",
            "    steps:",
            "      - name: Prepare environment",
            "        run: |",
            '          echo "IMAGE_REGISTRY=${IMAGE_REGISTRY,,}" >> $GITHUB_ENV',
            '          echo "IMAGE_NAME=${IMAGE_NAME,,}" >> $GITHUB_ENV',
            "",
            "      - name: Checkout",
            f"        uses: {pinned_action('actions/checkout')}",
            "",
            "      - name: Maximize build space",
            f"        uses: {pinned_action('ublue-os/remove-unwanted-software')}",
            "",
            "      - name: Get current date",
            "        id: date",
            '        run: echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> $GITHUB_OUTPUT',
            "",
            "      - name: Image Metadata",
            f"        uses: {pinned_action('docker/metadata-action')}",
            "        id: metadata",
            "        with:",
            "          tags: |",
            "            type=raw,value=${{ env.DEFAULT_TAG }}",
            "            type=raw,value=${{ env.DEFAULT_TAG }}.{{date 'YYYYMMDD'}}",
            "            type=raw,value={{date 'YYYYMMDD'}}",
            "            type=sha,enable=${{ github.event_name == 'pull_request' }}",
            "            type=ref,event=pr",
            "          labels: |",
            "            org.opencontainers.image.created=${{ steps.date.outputs.date }}",
            "            org.opencontainers.image.description=${{ env.IMAGE_DESC }}",
            "            org.opencontainers.image.title=${{ env.IMAGE_NAME }}",
            "            containers.bootc=1",
            '          sep-tags: " "',
            "",
            "      - name: Build Image",
            f"        uses: {pinned_action('redhat-actions/buildah-build')}",
            "        with:",
            "          containerfiles: ./Containerfile",
            "          image: ${{ env.IMAGE_NAME }}",
            "          tags: ${{ steps.metadata.outputs.tags }}",
            "          labels: ${{ steps.metadata.outputs.labels }}",
            "          oci: false",
            "",
            "      - name: Login to GHCR",
            f"        uses: {pinned_action('docker/login-action')}",
            "        if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
            "        with:",
            "          registry: ghcr.io",
            "          username: ${{ github.actor }}",
            "          password: ${{ secrets.GITHUB_TOKEN }}",
            "",
            "      - name: Push to GHCR",
            f"        uses: {pinned_action('redhat-actions/push-to-registry')}",
            "        if: github.event_name != 'pull_request' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
            "        with:",
            "          registry: ${{ env.IMAGE_REGISTRY }}",
            "          image: ${{ env.IMAGE_NAME }}",
            "          tags: ${{ steps.metadata.outputs.tags }}",
            "          username: ${{ github.actor }}",
            "          password: ${{ github.token }}",
        ]
        if self.config.signing_enabled:
            lines.extend(
                [
                    "",
                    "      - name: Install Cosign",
                    f"        uses: {pinned_action('sigstore/cosign-installer')}",
                    f"        if: {sign_if}",
                    "        with:",
                    "          cosign-release: 'v2.6.1'",
                    "",
                    "      - name: Sign container image",
                    f"        if: {sign_if}",
                    "        run: |",
                    '          IMAGE_FULL="${{ env.IMAGE_REGISTRY }}/${{ env.IMAGE_NAME }}"',
                    "          for tag in ${{ steps.metadata.outputs.tags }}; do",
                    "            cosign sign -y --key env://COSIGN_PRIVATE_KEY $IMAGE_FULL:$tag",
                    "          done",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def generate_readme(self) -> str:
        # The generated project README is intentionally brief and practical:
        # what base image was chosen, what package requests were configured, and
        # how to use the resulting image once GitHub finishes building it.
        base_name = self.config.base_image_name or self.config.base_image_uri
        owner = self.config.github_user or "your-user"
        image_ref = self.published_image_ref(owner)
        packages = "\n".join(f"- `{pkg}`" for pkg in self.config.packages) or "- None selected yet."
        copr_repos = "\n".join(f"- `{repo}`" for repo in self.config.copr_repos) or "- None."
        services = "\n".join(f"- `{service}`" for service in self.config.services) or "- None."
        removed_packages = "\n".join(f"- `{pkg}`" for pkg in self.config.removed_packages) or "- None."
        using_image_lines = [
            "## Using The Image",
            "",
            "After the first successful GitHub Actions build finishes, switch to it with:",
            "",
            "```bash",
            f"sudo bootc switch {image_ref}",
            "systemctl reboot",
            "```",
        ]
        if self.carried_scan_customizations():
            using_image_lines = [
                "## Using The Image",
                "",
                "This repo carries over package changes scanned from your current system.",
                "Run these commands in the same session before rebooting:",
                "",
                "```bash",
                "sudo rpm-ostree reset",
                f"sudo bootc switch {image_ref}",
                "systemctl reboot",
                "```",
                "",
                "Do not reboot between `rpm-ostree reset` and `bootc switch`.",
            ]

        sections = [
            f"# Custom {base_name} Image",
            "",
            self.config.image_desc,
            "",
            "This repository builds a custom bootc image on GitHub Actions.",
            "",
            "| Setting | Value |",
            "|---------|-------|",
            f"| Repository | `{owner}/{self.config.repo_name}` |",
            f"| Base Image | `{base_name}` |",
            f"| Base Image URI | `{self.config.base_image_uri}` |",
            f"| Published Image | `{image_ref}` |",
            f"| Build Method | `{METHOD_DISPLAY.get(self.config.method, self.config.method)}` |",
            "",
            f"## Managed By {TOOL_NAME}",
            "",
            f"This repo is managed by `{TOOL_SLUG}`. `{STATE_FILE}` is the saved settings file and source of truth for future updates.",
            "",
            f"If you hand-edit this repo after `{TOOL_SLUG}` creates or manages it, stop using `{TOOL_SLUG}` for this repo.",
            "",
            f"Later tool-driven updates rewrite managed files and can overwrite manual changes, especially `README.md` and `{'recipes/recipe.yml' if self.config.method == 'bluebuild' else 'build_files/build.sh'}`.",
            "",
            "## Requested Packages",
            "",
            f"These are the package names requested by this repo's {'recipe' if self.config.method == 'bluebuild' else 'generated build script'}.",
            self.requested_packages_note(),
            "",
            packages,
            "",
            "## COPR Repositories",
            "",
            copr_repos,
            "",
            "## Enabled Services",
            "",
            services,
            "",
            "## Removed Base Packages",
            "",
            removed_packages,
            "",
            *using_image_lines,
        ]
        return "\n".join(sections).rstrip() + "\n"

    def generate_justfile(self) -> str:
        # just is included by the upstream template ecosystem, so we keep a tiny
        # helper target for local builds even though this beginner tool no longer
        # manages local-install workflows itself.
        return textwrap.dedent(
            f"""\
            export image_name := env("IMAGE_NAME", "{self.config.repo_name}")
            export default_tag := env("DEFAULT_TAG", "latest")

            [private]
            default:
                @just --list

            build $target_image=image_name $tag=default_tag:
                #!/usr/bin/env bash
                podman build \\
                    --pull=newer \\
                    --tag "${{target_image}}:${{tag}}" \\
                    .
            """
        )

    def run_main(self) -> None:
        if not command_exists("gum"):
            self.preflight()
            return
        self.clear()
        self.banner()
        self.startup_requirements()
        self.preflight()
        self.main_menu()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"{TOOL_SLUG} {VERSION}")
        raise SystemExit(0)
    app = App()
    try:
        app.run_main()
    except ScreenBack:
        print()
        raise SystemExit(0)
    except CommandError as exc:
        app.gum.error(str(exc))
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)


if __name__ == "__main__":
    main()
