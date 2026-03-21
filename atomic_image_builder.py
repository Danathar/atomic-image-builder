#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timezone, tzinfo
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

if sys.version_info < (3, 10):
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
VERSION = "0.8 beta"
TOOL_NAME = "Atomic Image Builder"
TOOL_SLUG = "atomic-image-builder"
STATE_FILE = ".ublue-builder.json"
DEFAULT_REPO_NAME = "my-atomic-image"
DEFAULT_GITHUB_BUILD_CRON = "05 10 * * *"
FEDORA_ATOMIC_DEFAULT_TAG = "43"
MAX_UI_WIDTH = 120
ACCENT_COLOR = 117
CONTROLS_COLOR = 10
PACKAGE_SEARCH_LIMIT = 40
MANAGED_REPO_WARNING = "If you hand-edit a repo after this tool creates or manages it, stop using this tool for that repo."
MANAGED_REPO_HINT = (
    f"Future updates use {STATE_FILE} as the source of truth and rewrite managed files such as README.md and build_files/build.sh."
)
CONTAINERFILE_TEMPLATE_REPO = "ublue-os/image-template"
TEMPLATE_SNAPSHOT_DIR = Path(__file__).resolve().parent / "template_snapshots"
CONTAINERFILE_TEMPLATE_DIR = TEMPLATE_SNAPSHOT_DIR / "containerfile"
ALLOWED_METHODS = {"containerfile"}
# These regexes are our low-cost safety rails. They do not prove a package or
# service is real, but they do stop obviously unsafe values from becoming shell
# script content later.
PACKAGE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._+:-]+$")
COPR_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
SERVICE_TOKEN_RE = re.compile(r"^[A-Za-z0-9@._:+-]+$")
FROM_LINE_RE = re.compile(r"^(\s*FROM(?:\s+--platform=\S+)?\s+)(\S+)(.*)$", flags=re.IGNORECASE)
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
    "ublue-os/remove-unwanted-software": ("695eb75bc387dbcd9685a8e72d23439d8686cba6", "v8"),
    "docker/metadata-action": ("c299e40c65443455700f0fdfc63efafe5b349051", "v5"),
    "redhat-actions/buildah-build": ("7a95fa7ee0f02d552a32753e7414641a04307056", "v2"),
    "docker/login-action": ("c94ce9fb468520275223c153574b00df6fe4bcc9", "v3"),
    "redhat-actions/push-to-registry": ("5ed88d269cf581ea9ef6dd6806d01562096bee9c", "v2"),
    "sigstore/cosign-installer": ("faadad0cce49287aee09b3a48701e75088a2c6ad", "v4.0.0"),
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


def ublue_image(key: str, name: str, description: str, image_uri: str) -> BaseImage:
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
    ublue_image("bazzite", "Bazzite (KDE)", "KDE desktop for gaming systems and handheld-style setups", "ghcr.io/ublue-os/bazzite:stable"),
    ublue_image("bazzite-gnome", "Bazzite (GNOME)", "GNOME desktop for gaming systems and handheld-style setups", "ghcr.io/ublue-os/bazzite-gnome:stable"),
    ublue_image("bazzite-dx", "Bazzite DX (KDE)", "Bazzite plus extra developer tools on KDE", "ghcr.io/ublue-os/bazzite-dx:stable"),
    ublue_image("bazzite-dx-gnome", "Bazzite DX (GNOME)", "Bazzite plus extra developer tools on GNOME", "ghcr.io/ublue-os/bazzite-dx-gnome:stable"),
    ublue_image("aurora", "Aurora (KDE)", "KDE desktop for everyday use", "ghcr.io/ublue-os/aurora:stable"),
    ublue_image("aurora-dx", "Aurora DX", "Aurora plus extra developer tools", "ghcr.io/ublue-os/aurora-dx:stable"),
    ublue_image("bluefin", "Bluefin (GNOME)", "GNOME desktop for everyday use", "ghcr.io/ublue-os/bluefin:stable"),
    ublue_image("bluefin-dx", "Bluefin DX", "Bluefin plus extra developer tools", "ghcr.io/ublue-os/bluefin-dx:stable"),
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
    # serializes it to .ublue-builder.json before repo files are written.
    method: str = ""
    base_image_uri: str = ""
    base_image_name: str = ""
    repo_name: str = ""
    image_desc: str = "My custom bootc image"
    packages: list[str] = field(default_factory=list)
    copr_repos: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    removed_packages: list[str] = field(default_factory=list)
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
    if "signing_enabled" in data:
        value = data["signing_enabled"]
        if not isinstance(value, bool):
            raise ValueError("signing_enabled must be a boolean")
        cfg.signing_enabled = value
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
    pin = ACTION_PINS.get(action)
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
        detail = stderr or stdout or f"command failed: {' '.join(args)}"
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
                ["gum", "spin", "--spinner", "dot", "--title", title, "--", "bash", "-lc", shell_command],
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
        with tempfile.NamedTemporaryFile(delete=False) as stdout_tmp:
            stdout_path = stdout_tmp.name
        with tempfile.NamedTemporaryFile(delete=False) as stderr_tmp:
            stderr_path = stderr_tmp.name
        with tempfile.NamedTemporaryFile(delete=False) as status_tmp:
            status_path = status_tmp.name
        try:
            shell_command = (
                f"{shlex.join(command)} > {shlex.quote(stdout_path)} 2> {shlex.quote(stderr_path)}; "
                f"printf '%s' $? > {shlex.quote(status_path)}"
            )
            run(
                ["gum", "spin", "--spinner", "dot", "--title", title, "--", "bash", "-lc", shell_command],
                cwd=cwd,
                capture=False,
            )
            stdout = Path(stdout_path).read_text()
            stderr = Path(stderr_path).read_text()
            status_text = Path(status_path).read_text().strip()
            return subprocess.CompletedProcess(list(command), int(status_text or "0"), stdout, stderr)
        finally:
            Path(stdout_path).unlink(missing_ok=True)
            Path(stderr_path).unlink(missing_ok=True)
            Path(status_path).unlink(missing_ok=True)

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

    def fresh_config(self) -> Config:
        # Starting a new create/scan flow should not inherit stale repo names or
        # package picks from the previous action the user ran in this session.
        return Config(method="containerfile", github_user=self.github_user)

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
                "Official template repo",
                "https://github.com/ublue-os/image-template",
                "Generated repos start from a bundled snapshot",
                "of that official template repository.",
                "That snapshot may lag behind upstream.",
                "The maintainer aims to keep it aligned.",
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
        return [
            ("Packages", self.summarize_selection(self.config.packages, empty="No packages", verb="selected")),
            ("Base image", self.config.base_image_name or "(not set)"),
            ("Description", self.truncate_label(self.config.image_desc or "(empty)")),
            ("COPR repositories", self.summarize_selection(self.config.copr_repos, empty="No COPRs", verb="added")),
            ("Services", self.summarize_selection(self.config.services, empty="No services", verb="enabled")),
            ("Removed base packages", self.summarize_selection(self.config.removed_packages, empty="None", verb="selected")),
        ]

    def show_managed_repo_warning(self) -> None:
        self.gum.warn(MANAGED_REPO_WARNING)
        self.gum.hint(MANAGED_REPO_HINT)

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
                    self.github_user = str(self.gh_json(["api", "user"])["login"])
                    self.github_available = True
                    self.config.github_user = self.github_user
                except Exception:
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
            self.github_user = str(self.gh_json(["api", "user"])["login"])
        except Exception:
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

    def create_new_image(self, *, scanned: bool = False) -> None:
        # This is a simple step-by-step wizard. "step" is an integer instead of
        # a stack because the beginner flow is intentionally linear.
        if scanned:
            self.config.method = "containerfile"
            self.config.github_user = self.github_user
        else:
            self.config = self.fresh_config()
        total_steps = 4
        step = 1
        while True:
            try:
                if step == 1:
                    self.choose_base_image(step=step, total_steps=total_steps)
                    step = 2
                    continue
                if step == 2:
                    self.configure_repo(step=step, total_steps=total_steps)
                    step = 3
                    continue
                if step == 3:
                    self.select_packages(step=step, total_steps=total_steps)
                    step = 4
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
            if action == "base":
                step = 1
            elif action == "repo":
                step = 2
            elif action == "software":
                step = 3
            else:
                return

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
        self.config.services.extend(line.strip() for line in raw.splitlines())
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
            ("Repository", f"{self.github_user}/{self.config.repo_name}" if self.github_user else self.config.repo_name),
            ("Description", self.config.image_desc),
            ("Base Image", self.config.base_image_name or self.config.base_image_uri),
            ("Image URI", self.config.base_image_uri),
            ("Packages", self.summarize_selection(self.config.packages, empty="None selected", verb="selected", limit=3)),
            ("COPR Repos", self.summarize_selection(self.config.copr_repos, empty="None", verb="added", limit=3)),
            ("Services", self.summarize_selection(self.config.services, empty="None", verb="enabled", limit=3)),
            ("Removed Base Packages", self.summarize_selection(self.config.removed_packages, empty="None", verb="selected", limit=3)),
        ]
        body = self.format_key_value_rows(rows)
        lines = [*intro_lines, "", *body]
        self.gum.pager(self.read_only_pager_text("Review Build Configuration", lines))

    def review_new_image(self, *, step: int, total_steps: int) -> str:
        while True:
            self.show_step_header("Review and Create Image", step=step, total_steps=total_steps)
            self.gum.hint("Choose a section to review or change, or start the GitHub build.")
            print()
            software_label = self.format_task_choice("Software", self.software_status())
            repo_label = self.format_task_choice("Repository settings", self.repository_status())
            base_label = self.format_task_choice("Base image", self.config.base_image_name or "(not set)")
            full_label = "View full configuration"
            build_label = "Start GitHub build"
            cancel_label = "Cancel and return to the main menu"
            options = [software_label, repo_label, base_label, full_label, build_label, cancel_label]
            choice = self.gum.choose(options, height=9)
            selected = choice[0] if choice else cancel_label
            if selected == build_label:
                return "build"
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

        status = json.loads(proc.stdout)
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
        base = normalize_container_image_reference(container_ref)
        self.config.scanned_packages = unique(booted.get("requested-packages", []))
        self.config.scanned_removed = unique(booted.get("requested-base-removals", []))
        self.config.removed_packages = list(self.config.scanned_removed)

        self.config.base_image_uri = base
        self.config.base_image_name = base
        matched = self.match_base_image(base)
        if matched:
            self.config.base_image_name = matched.name

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

    def ensure_signing_ready(self, owner: str, repo: str) -> bool:
        # Signed images are required for this tool, so "ready" means:
        # - the repo already has SIGNING_SECRET, or
        # - we can create a cosign keypair and upload the private key now
        self.generated_cosign_pub = None
        if self.repo_secret_exists(owner, repo, "SIGNING_SECRET"):
            return True
        if not command_exists("cosign"):
            raise CommandError("cosign is required for signed images. Install it with: brew install cosign")
        with tempfile.TemporaryDirectory(prefix="ublue-signing.") as tmp:
            tmpdir = Path(tmp)
            env = os.environ.copy()
            env["COSIGN_PASSWORD"] = ""
            proc = run(["cosign", "generate-key-pair"], cwd=tmpdir, env=env, check=False)
            key_path = tmpdir / "cosign.key"
            pub_path = tmpdir / "cosign.pub"
            if proc.returncode != 0 or not key_path.exists() or not pub_path.exists():
                raise CommandError("Unable to generate a cosign keypair. Fix cosign first, then try again.")
            with key_path.open("rb") as key_handle:
                secret_proc = subprocess.run(
                    ["gh", "secret", "set", "SIGNING_SECRET", "-R", f"{owner}/{repo}"],
                    cwd=str(tmpdir),
                    stdin=key_handle,
                    text=False,
                    capture_output=True,
                    check=False,
                )
            if secret_proc.returncode != 0:
                raise CommandError("Unable to upload SIGNING_SECRET to GitHub. Check your gh login and repo access, then try again.")
            self.generated_cosign_pub = pub_path.read_text()
        # The public key is kept in memory for the current run so it can be
        # written into the repo files that we are about to generate.
        self.gum.success("Configured SIGNING_SECRET for image signing.")
        return True

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
            shutil.copytree(source_dir, target, ignore=shutil.ignore_patterns(".template-source"))
        except (OSError, shutil.Error) as exc:
            raise CommandError(f"Unable to copy bundled template snapshot for {repo}: {exc}") from exc

    def clone_container_template(self, target: Path) -> None:
        self.copy_template_snapshot(target, repo=CONTAINERFILE_TEMPLATE_REPO, source_dir=CONTAINERFILE_TEMPLATE_DIR)

    def repo_default_branch(self, owner: str, repo: str) -> str:
        data = self.gh_json(["repo", "view", f"{owner}/{repo}", "--json", "defaultBranchRef"])
        branch = data.get("defaultBranchRef", {}).get("name")
        if branch:
            return branch
        proc = run(["gh", "api", f"/repos/{owner}/{repo}"], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                return "main"
            branch = data.get("default_branch")
            if isinstance(branch, str) and branch:
                return branch
        return "main"

    def seed_project_template(self, target: Path) -> None:
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

    def filter_available_manual_packages(self, packages: Sequence[str]) -> list[str]:
        # Manual package entry is intentionally forgiving:
        # - known good packages are accepted
        # - clearly missing packages are skipped
        # - packages that might come from configured COPRs are kept
        # - unknown/uncheckable cases are kept, but the user is warned that the
        #   GitHub build is the final authority
        self.last_manual_package_check_had_missing = False
        accepted: list[str] = []
        missing: list[str] = []
        missing_but_copr_may_provide: list[str] = []
        unchecked: list[str] = []
        for package in packages:
            available = self.lookup_host_package(package)
            if available is True:
                accepted.append(package)
            elif available is False:
                if self.config.copr_repos:
                    accepted.append(package)
                    missing_but_copr_may_provide.append(package)
                else:
                    missing.append(package)
            else:
                accepted.append(package)
                unchecked.append(package)
        if missing:
            self.last_manual_package_check_had_missing = True
            joined = ", ".join(missing)
            self.gum.error(f"These package names were not found: {joined}")
            self.gum.hint("They were skipped because no RPM package with that name was found.")
        if missing_but_copr_may_provide:
            joined = ", ".join(missing_but_copr_may_provide)
            self.gum.warn("Some package names were not found in your current host repos.")
            self.gum.hint(f"Keeping for now because configured COPRs may provide them: {joined}")
            self.gum.hint("The GitHub build will do the final package check.")
        if unchecked and not self.package_lookup_warning_shown:
            joined = ", ".join(unchecked)
            self.gum.warn("Could not fully check some package names on this system.")
            self.gum.hint(f"Keeping for now: {joined}")
            self.gum.hint("The GitHub build will do the final package check.")
            self.package_lookup_warning_shown = True
        return accepted

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
        except Exception:
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
                    "Before switching, reset the current deployment with:",
                    "sudo rpm-ostree reset",
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

    def select_repo(self, *, require_state_file: bool = False) -> tuple[str, str]:
        # This helper centralizes repo picking for update flows. The
        # require_state_file flag is what prevents the normal update path from
        # accidentally operating on unrelated repos.
        if not self.require_github():
            raise ScreenBack()
        while True:
            repos = self.gh_json_with_spinner(
                "Fetching repositories from GitHub...",
                ["repo", "list", self.github_user, "--json", "name,description", "--limit", "100"],
            )
            visible_repos = repos
            if require_state_file:
                self.gum.hint("Checking which repos were created by this tool...")
                visible_repos = [item for item in repos if self.repo_has_state_file(self.github_user, item["name"])]
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
                    self.gh_json(["repo", "view", f"{self.github_user}/{repo}", "--json", "name"])
                except CommandError:
                    self.gum.error(f"{self.github_user}/{repo} was not found on GitHub.")
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
        with tempfile.TemporaryDirectory(prefix="ublue-update.") as tmp:
            tmpdir = Path(tmp)
            self.clone_repo(owner, repo, tmpdir)
            self.load_repo_config(tmpdir)
            self.config.repo_name = repo
            self.config.github_user = owner
            if self.update_menu():
                self.show_summary()
                print()
                self.push_update(owner, repo, tmpdir)

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
            if "unsupported build method" in str(exc):
                raise CommandError("This repo uses BlueBuild, which is no longer supported by this tool.") from exc
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

    def update_menu(self) -> bool:
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
            save_label = "Save and push changes"
            cancel_label = "Cancel and go back"
            options.extend([review_label, save_label, cancel_label])
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
                "Enter one package name per line. Leave this empty if you want to go back.",
            )
            raw = self.gum.write(
                placeholder="Enter package names, one per line...",
                height=6,
                width=self.gum.form_width(max_width=90),
            )
            self.config.removed_packages.extend(line.strip() for line in raw.splitlines())
            self.config.normalize()
        elif selected == "Stop removing listed packages":
            self.config.removed_packages = self.choose_to_remove(self.config.removed_packages, "Remove Base Package Removals")

    def push_update(self, owner: str, repo: str, repo_dir: Path) -> None:
        # The update path rewrites files in a temporary clone, shows the diff,
        # and only then asks for confirmation before pushing.
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
        self.config.signing_enabled = self.ensure_signing_ready(owner, repo)
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
        return run(["git", "diff"], cwd=repo_dir, check=False).stdout

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
        # If the template already has a Containerfile, only replace the FROM line
        # so we preserve upstream formatting and comments where possible.
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
                return ensure_trailing_newline("\n".join(lines))
            return ensure_trailing_newline(existing_text)
        return self.generate_containerfile()

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
        current_step = ""
        has_job_cosign = any(re.fullmatch(r" {6}COSIGN_PRIVATE_KEY: \$\{\{ secrets\.SIGNING_SECRET \}\}", line) for line in lines)
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
            if stripped.startswith("- name: "):
                current_step = stripped[len("- name: ") :]
            if current_step in {"Install Cosign", "Sign container image"} and stripped.startswith("if: ") and branch_if in stripped:
                indent = line[: len(line) - len(line.lstrip())]
                output.append(f"{indent}if: {sign_if}")
                continue
            output.append(line)
        text = self.patch_workflow_branch_filters("\n".join(output), default_branch)
        if not has_job_cosign:
            if re.search(r"^    env:\n", text, flags=re.MULTILINE):
                text = re.sub(
                    r"^    env:\n",
                    "    env:\n      COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}\n",
                    text,
                    count=1,
                    flags=re.MULTILINE,
                )
            elif re.search(r"^    steps:\n", text, flags=re.MULTILINE):
                text = re.sub(
                    r"^    steps:\n",
                    "    env:\n      COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}\n    steps:\n",
                    text,
                    count=1,
                    flags=re.MULTILINE,
                )
        return ensure_trailing_newline(text)

    def patch_workflow_branch_filters(self, workflow_text: str, default_branch: str) -> str:
        lines = workflow_text.splitlines()
        output: list[str] = []
        current_event: str | None = None
        index = 0
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if indent == 2 and stripped in {"pull_request:", "push:"}:
                current_event = stripped[:-1]
                output.append(line)
                index += 1
                continue
            if current_event in {"pull_request", "push"} and indent == 4 and stripped == "branches:":
                output.append(line)
                output.append(f"{line[: len(line) - len(line.lstrip())]}  - {default_branch}")
                index += 1
                while index < len(lines):
                    branch_line = lines[index]
                    branch_stripped = branch_line.strip()
                    branch_indent = len(branch_line) - len(branch_line.lstrip())
                    if branch_indent == 6 and branch_stripped.startswith("- "):
                        index += 1
                        continue
                    break
                continue
            if indent <= 2 and stripped.endswith(":"):
                current_event = None
            output.append(line)
            index += 1
        return ensure_trailing_newline("\n".join(output))

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

        if include_workflow:
            workflow_path.parent.mkdir(parents=True, exist_ok=True)
            if workflow_path.exists():
                workflow_path.write_text(self.patch_container_workflow(workflow_path.read_text(), default_branch=default_branch))
            else:
                workflow_path.write_text(self.generate_container_workflow(default_branch=default_branch))

    def write_project_files(self, base_dir: Path, *, include_workflow: bool, default_branch: str = "main") -> None:
        # Always write the canonical state file first. That way the repo can be
        # updated later even if a human edits generated files by hand.
        self.validate_config()
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / STATE_FILE).write_text(json.dumps(self.state_payload(), indent=2) + "\n")
        self.write_container_project_files(base_dir, include_workflow=include_workflow, default_branch=default_branch)

    def generate_containerfile(self) -> str:
        # The Containerfile is intentionally small. Most customization lives in
        # build_files/build.sh so users can inspect a simpler mutation layer.
        return textwrap.dedent(
            f"""\
            FROM scratch AS ctx
            COPY build_files /

            FROM {self.config.base_image_uri}

            RUN --mount=type=bind,from=ctx,source=/,target=/ctx \\
                --mount=type=cache,dst=/var/cache \\
                --mount=type=cache,dst=/var/log \\
                --mount=type=tmpfs,dst=/tmp \\
                /ctx/build.sh

            RUN bootc container lint
            """
        )

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
            lines.append("# Remove packages from the base image")
            lines.append("dnf5 remove -y \\")
            for index, pkg in enumerate(self.config.removed_packages):
                suffix = " \\" if index < len(self.config.removed_packages) - 1 else ""
                lines.append(f"    {shell_quote(pkg)}{suffix}")
            lines.append("")
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
            "            type=sha,enable=${{ github.event_name == 'pull_request' }}",
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
            "| Build Method | `Containerfile` |",
            "",
            f"## Managed By {TOOL_NAME}",
            "",
            f"This repo is managed by `{TOOL_SLUG}`. `{STATE_FILE}` is the saved settings file and source of truth for future updates.",
            "",
            f"If you hand-edit this repo after `{TOOL_SLUG}` creates or manages it, stop using `{TOOL_SLUG}` for this repo.",
            "",
            "Later tool-driven updates rewrite managed files and can overwrite manual changes, especially `README.md` and `build_files/build.sh`.",
            "",
            "## Requested Packages",
            "",
            "These are the package names requested by this repo's generated build script.",
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
            "## Using The Image",
            "",
            "After the first successful GitHub Actions build finishes, switch to it with:",
            "",
            "```bash",
            f"sudo bootc switch {image_ref}",
            "systemctl reboot",
            "```",
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
