#!/usr/bin/env python3
"""Build and boot what the tool generates for each supported base image.

The unit suite proves the tool writes the files it means to write. Nothing
proved those files still build into an image that boots: the bases in
BASE_IMAGES change upstream every day, and a Universal Blue or Fedora change
that breaks generated projects was first noticed by a user whose build or boot
failed (#756).

Each night the workflow runs two slots from NIGHTLY_SLOTS, so every base is
covered once a week. A slot generates a project the way the menu's "Test build
locally" does -- seed_project_template() then write_project_files() -- with a
fixed test configuration, builds it with podman, and boots the image in a VM
with bcvk. No GitHub repository is created, and nothing is pushed or signed.

A failing slot keeps one issue per base, reused, and closed only when that same
base passes again: the next night tests different bases, so a green run says
nothing about tonight's red one.

CI-only. Not COPY'd into the published image and never run by the tool. It
also runs on a Linux workstation with KVM, podman and bcvk:

    python3 variant_boot_test.py run --base bazzite --work /var/tmp/aib-boot
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from atomic_image_builder import BASE_IMAGES, App, BaseImage, CommandError, Config, Gum
from snapshot_drift_issue import ISSUE_LIST_LIMIT, run_gh, written_by_this_workflow

CONTAINERFILE = "containerfile"
BLUEBUILD = "bluebuild"
METHODS = (CONTAINERFILE, BLUEBUILD)

# Monday first, matching date.weekday(). Each night pairs a Universal Blue base
# with a Fedora Atomic one where the counts allow, so every family comes up
# every two or three days even though each base comes up once a week.
# Sunday's second job builds one base with the BlueBuild method instead, so the
# tool's other generated path is built weekly too, on a base that moves on one
# place a week (bluebuild_base()). A base added to BASE_IMAGES has to be given
# a night here: test_every_base_is_scheduled_exactly_once_a_week fails until it is.
NIGHTLY_SLOTS: tuple[tuple[str, str], ...] = (
    ("bazzite", "silverblue"),
    ("aurora", "kinoite"),
    ("bluefin", "sway-atomic"),
    ("bazzite-gnome", "budgie-atomic"),
    ("aurora-dx", "cosmic-atomic"),
    ("bluefin-dx", "bazzite-dx"),
    ("bazzite-dx-gnome", BLUEBUILD),
)

# What the generated project is asked to add, chosen to exercise the two
# customizations every user reaches for: a package, installed with dnf5 by the
# generated build.sh, and a service, enabled by it. tmux is in the Fedora
# repositories every base builds from, and podman.socket ships in every base
# and is disabled by default, so "enabled and active" is the tool's doing.
# sshd.service is deliberately not the test service: the VM needs it running
# for bcvk to connect at all (see ROOTHOME_UNIT), which would pass the
# check whatever the tool generated.
TEST_PACKAGES = ("tmux",)
TEST_SERVICES = ("podman.socket",)
TEST_OWNER = "aib-nightly"

# Units that fail in an ephemeral VM for a reason that is not a defect of the
# image. Every entry carries the reason, and nothing is added without one: an
# allowlist that grows silently is how a real failure gets waved through.
# The upstream base is booted the same way when a check fails, but only to say
# whose failure it is -- a unit failing in both is still a failure.
ALLOWED_FAILED_UNITS: dict[str, str] = {
    "bootloader-update.service": (
        "bcvk boots the image's kernel directly from a virtiofs root, so there is no "
        "/boot or /sysroot block device for bootupd to find"
    ),
    "mcelog.service": (
        "seen in a VM on an AMD host: mcelog exits with 'AMD Processor family 25: mcelog does not support "
        "this processor'. The guest sees the host's CPU, so this follows the machine, not the image"
    ),
    # Seen on bazzite in the hosted run: both call rpm-ostree, which refuses
    # with "This system was not booted via libostree". bcvk boots the image's
    # tree directly, with no ostree deployment, so these fail on every boot.
    "greenboot-healthcheck.service": (
        "greenboot's required checks run rpm-ostree and grub2-editenv; an ephemeral VM is not booted via "
        "libostree and has no /boot/grub2"
    ),
    "bazzite-hardware-setup.service": (
        "it applies kernel arguments with rpm-ostree, which refuses on a system not booted via libostree, "
        "as a bcvk ephemeral VM is not"
    ),
}

# Two things bcvk needs from the guest that an installed desktop has or does
# not need, injected into every boot (generated image and unmodified base
# alike), so they cannot be the difference between the two:
#
# - bcvk provisions the root SSH key through a systemd credential that tmpfiles
#   writes to /root/.ssh. On an ostree image /root is a symlink to
#   /var/roothome, which an installed system has from its deployment but an
#   ephemeral /var built from the image does not, so the key was never written.
#   This unit creates the directory before tmpfiles runs.
# - bcvk connects to TCP port 22 through QEMU's user network, and the desktop
#   bases ship sshd.service disabled (systemd's ssh generator only opens vsock
#   and local sockets). It is started through a default.target.wants link.
#
# Without both, every boot of a desktop base timed out waiting for SSH. bcvk
# copies injected units into /etc/systemd/system and keeps only
# default.target.wants links; that is enough, because a unit in the boot
# transaction keeps its Before= ordering whatever pulled it in.
ROOTHOME_UNIT = """\
[Unit]
Description=Create /var/roothome so systemd credentials can provision /root/.ssh
DefaultDependencies=no
After=local-fs.target
Before=systemd-tmpfiles-setup.service
ConditionPathExists=!/var/roothome

[Service]
Type=oneshot
ExecStart=/usr/bin/mkdir -p -m 0700 /var/roothome

[Install]
WantedBy=default.target
"""

VM_MEMORY = "4G"
VM_VCPUS = "2"
# Seconds for the whole of one boot, checks included. bcvk gives up on SSH by
# itself after four minutes and the guest waits at most SETTLE_TIMEOUT_SECONDS
# for boot to settle; this bounds a VM that is reachable but hangs.
BOOT_TIMEOUT_SECONDS = 12 * 60
# Seconds the guest waits for boot to settle before it reports what it has.
SETTLE_TIMEOUT_SECONDS = 300
# The largest base builds in minutes on a hosted runner; this only stops a
# hung build.
BUILD_TIMEOUT_SECONDS = 45 * 60
# A slot can boot three times: the generated image twice, then the unmodified
# base. The job's timeout-minutes has to cover all of it plus the runner setup,
# or the job is cancelled before it uploads the logs and reports the issue --
# exactly on the slow failures those exist for. test_the_job_outlives_the_
# slowest_slot holds the workflow to this.
WORST_CASE_SLOT_SECONDS = BUILD_TIMEOUT_SECONDS + 3 * BOOT_TIMEOUT_SECONDS
LOG_TAIL_LINES = 50
# A Containerfile FROM line's image, past any --platform style flags.
FROM_RE = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", re.IGNORECASE)

# The guest writes one marker per line so the host can read results out of an
# SSH session that also carries whatever the image prints at login.
MARKER = "AIB-"
CONSOLE_FAILURE_RE = re.compile(r"Kernel panic|\bOops\b|\bBUG:|Unknown symbol in module")
# What bcvk prints when the host, not the guest, failed: the VM's file sharing,
# QEMU or the container around them never started. Only these count as a
# runner failure; a boot that is merely silent stays a failure of the base, so
# a guest that dies before its journal reaches the host is not filed away as
# the runner's.
HOST_FAILURE_RE = re.compile(r"virtiofsd failed to start|Failed to spawn (?:QEMU|virtiofsd)|OCI runtime error|bwrap: .*Permission denied")
# bcvk mounts a --ro-bind directory at /run/virtiofs-mnt-<name> in the guest.
GUEST_SCRIPT_MOUNT = "aib-checks"

ISSUE_TITLE_PREFIX = "Nightly boot test failing: "
# Failures before any image ran say nothing about a base, and both of a night's
# jobs would report the same one; they share this issue instead.
HOST_ISSUE_TITLE = "Nightly boot test cannot run on the runner"
HOST_STAGES = ("setup", "host")


@dataclass(frozen=True)
class Slot:
    base: BaseImage
    method: str

    @property
    def label(self) -> str:
        return self.base.key if self.method == CONTAINERFILE else f"{self.base.key} (BlueBuild)"

    @property
    def tag(self) -> str:
        return f"localhost/aib-variant-{self.base.key}-{self.method}:boot-test"


@dataclass
class Result:
    base: str
    method: str
    image_uri: str
    # One of: setup, generate, build, boot, checks, passed.
    stage: str = "setup"
    passed: bool = False
    summary: str = ""
    fault: str = ""
    base_digest: str = ""
    checks: list[dict[str, str]] = field(default_factory=list)
    failed_units: list[str] = field(default_factory=list)
    unexpected_units: list[str] = field(default_factory=list)
    upstream_failed_units: list[str] | None = None
    attempts: int = 0
    log_tail: str = ""

    @property
    def label(self) -> str:
        return self.base if self.method == CONTAINERFILE else f"{self.base} (BlueBuild)"


class StageFailure(Exception):
    def __init__(self, stage: str, summary: str, fault: str, log_tail: str = "") -> None:
        super().__init__(summary)
        self.stage = stage
        self.summary = summary
        self.fault = fault
        self.log_tail = log_tail


class HeadlessGum(Gum):
    """The tool's UI, minus the terminal.

    render_bluebuild_containerfile() runs its podman and bluebuild steps through
    gum spin and reports failures through gum log. Both need the gum binary and
    a terminal, and CI has neither; the steps themselves are what this test is
    for, so they run as plain subprocesses here and the messages go to stderr.
    """

    def log(self, level: str, message: str) -> None:
        print(f"{level}: {message}", file=sys.stderr)

    def hint(self, message: str) -> None:
        print(message, file=sys.stderr)

    def spinner_result(self, title: str, command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        print(title, file=sys.stderr)
        return subprocess.run(list(command), cwd=cwd, capture_output=True, text=True, errors="replace", check=False)


def base_by_key(key: str) -> BaseImage:
    for base in BASE_IMAGES:
        if base.key == key:
            return base
    known = ", ".join(base.key for base in BASE_IMAGES)
    raise ValueError(f"unknown base {key!r}; expected one of: {known}")


def bluebuild_base(day: dt.date) -> BaseImage:
    # A count of weeks that never resets. The ISO week number wraps from 52
    # or 53 to 1 at New Year, which skipped a base in the rotation each year.
    return BASE_IMAGES[day.toordinal() // 7 % len(BASE_IMAGES)]


def plan(day: dt.date) -> list[Slot]:
    slots = []
    for key in NIGHTLY_SLOTS[day.weekday()]:
        if key == BLUEBUILD:
            slots.append(Slot(bluebuild_base(day), BLUEBUILD))
        else:
            slots.append(Slot(base_by_key(key), CONTAINERFILE))
    return slots


def test_config(slot: Slot) -> Config:
    return Config(
        method=slot.method,
        base_image_uri=slot.base.image_uri,
        base_image_name=slot.base.name,
        repo_name=f"variant-{slot.base.key}",
        image_desc=f"Nightly boot test of {slot.base.name}",
        packages=list(TEST_PACKAGES),
        services=list(TEST_SERVICES),
        signing_enabled=True,
        github_user=TEST_OWNER,
    )


def generate(slot: Slot, context: Path) -> None:
    """Write the project the tool would push, into `context` (which must not exist yet)."""
    app = App()
    app.gum = HeadlessGum()
    app.config = test_config(slot)
    try:
        app.seed_project_template(context)
        app.write_project_files(context, include_workflow=False)
    except CommandError as exc:
        raise StageFailure("generate", f"The tool could not write the project: {exc}", fault_for("generate")) from exc
    if slot.method == BLUEBUILD and not app.render_bluebuild_containerfile(context, None):
        raise StageFailure("generate", "The BlueBuild CLI could not render the recipe into a Containerfile.", fault_for("generate"))


def fault_for(stage: str, *, upstream_also_fails: bool | None = None) -> str:
    if stage == "generate":
        return "atomic-image-builder: the tool failed to write or render the project, before anything was built."
    if stage in HOST_STAGES:
        return "The runner or bcvk, not the base: the image never ran, so nothing here says anything about it."
    if stage == "build":
        return (
            "Unclear from the stage alone: the failing build step below says whose. A step running "
            "/ctx/build.sh runs what the tool generated (its package and service commands, against the "
            "base's repositories); a FROM step is the registry or the base image; `bootc container lint` "
            "is either the base or what the tool added."
        )
    if upstream_also_fails:
        return "Upstream: the unmodified base image fails the same way, so this is not something the tool can fix. Comment and wait for upstream."
    if upstream_also_fails is False:
        return "atomic-image-builder: the unmodified base image does not fail this way, so the difference comes from what the tool generated."
    return "Unknown: the unmodified base image could not be compared."


def tail(text: str, lines: int = LOG_TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


def last_build_step(log: str) -> str:
    steps = [line.strip() for line in log.splitlines() if re.search(r"\bSTEP \d+/\d+:", line)]
    return steps[-1] if steps else ""


def build(slot: Slot, context: Path, log_path: Path) -> None:
    # --no-cache because the generated Containerfile runs build.sh through a
    # bind mount of another stage, and podman's layer cache keys that RUN step
    # on its command line, not on the mounted script: a workstation that had
    # built the same base before reused the old layer and booted an image
    # without tonight's packages and services. --pull=always so the base is
    # tonight's, not whatever this machine pulled last.
    command = ["podman", "build", "--pull=always", "--no-cache", "-t", slot.tag, str(context)]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False, timeout=BUILD_TIMEOUT_SECONDS)
        output = proc.stdout + proc.stderr
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = f"{exc.stdout or ''}{exc.stderr or ''}\npodman build timed out after {BUILD_TIMEOUT_SECONDS}s"
        output = output if isinstance(output, str) else output.decode(errors="replace")
        returncode = -1
    log_path.write_text(output)
    if returncode != 0:
        step = last_build_step(output)
        summary = f"podman build failed (exit {returncode})" + (f" at `{step}`" if step else "") + "."
        raise StageFailure("build", summary, fault_for("build"), tail(output))


def base_reference(base: BaseImage, context: Path) -> str:
    """The reference the build pulled the base by.

    The generated Containerfile names the base by its tag, but `bluebuild
    generate` pins it by digest (`FROM <repository>@sha256:... AS ...`). A base
    pulled only by digest has no tag in local storage, so inspecting the tag
    found nothing and a BlueBuild slot recorded no base digest.
    """
    repository = re.sub(r":[^/]*$", "", base.image_uri)
    for line in (context / "Containerfile").read_text().splitlines():
        match = FROM_RE.match(line)
        if match and (match.group(1) == base.image_uri or match.group(1).startswith(f"{repository}@")):
            return match.group(1)
    return base.image_uri



def image_digest(reference: str) -> str:
    proc = subprocess.run(
        ["podman", "image", "inspect", "--format", "{{.Digest}}", reference],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def guest_script(packages: Sequence[str], services: Sequence[str]) -> str:
    """The checks, as a bash script the guest runs once SSH is up."""
    return "\n".join(
        [
            "set -u",
            f'state="$(timeout {SETTLE_TIMEOUT_SECONDS} systemctl is-system-running --wait 2>/dev/null)"',
            f'echo "{MARKER}STATE ${{state:-timeout}}"',
            f"for p in {' '.join(shlex.quote(p) for p in packages)}; do",
            f'  if rpm -q --quiet "$p"; then echo "{MARKER}CHECK package:$p pass"; else echo "{MARKER}CHECK package:$p fail not-installed"; fi',
            "done",
            f"for s in {' '.join(shlex.quote(s) for s in services)}; do",
            '  enabled="$(systemctl is-enabled "$s" 2>&1)"; active="$(systemctl is-active "$s" 2>&1)"',
            '  if [ "$enabled" = enabled ] && [ "$active" = active ]; then',
            f'    echo "{MARKER}CHECK service:$s pass"',
            "  else",
            f'    echo "{MARKER}CHECK service:$s fail enabled=$enabled,active=$active"',
            "  fi",
            "done",
            "systemctl --failed --plain --no-legend --full | while read -r unit _; do",
            f'  echo "{MARKER}FAILED-UNIT $unit"',
            "done",
            f'echo "{MARKER}DONE"',
        ]
    )


@dataclass
class Boot:
    reached: bool
    state: str
    checks: list[dict[str, str]]
    failed_units: list[str]
    console_errors: list[str]
    output: str
    # Set when the guest never wrote a journal line and bcvk's output names a
    # host-side failure (HOST_FAILURE_RE): the image never ran at all.
    host_error: str = ""
    console: str = ""


def parse_guest_output(output: str) -> tuple[bool, str, list[dict[str, str]], list[str]]:
    done = False
    state = ""
    checks: list[dict[str, str]] = []
    units: list[str] = []
    for line in output.splitlines():
        # A login shell can print terminal queries ahead of the first line
        # (bazzite's sends OSC colour and cursor-position requests), so the
        # marker is looked for anywhere in the line, not only at its start.
        start = line.find(MARKER)
        if start < 0:
            continue
        kind, _, rest = line[start + len(MARKER) :].strip().partition(" ")
        if kind == "DONE":
            done = True
        elif kind == "STATE":
            state = rest.strip()
        elif kind == "CHECK":
            name, _, outcome = rest.partition(" ")
            status, _, detail = outcome.partition(" ")
            checks.append({"name": name, "status": status, "detail": detail})
        elif kind == "FAILED-UNIT" and rest.strip():
            units.append(rest.strip())
    return done, state, checks, units


def write_units(directory: Path) -> Path:
    """The --systemd-units directory bcvk injects: see ROOTHOME_UNIT."""
    system = directory / "system"
    wants = system / "default.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    (system / "aib-roothome.service").write_text(ROOTHOME_UNIT)
    for name, target in (
        ("aib-roothome.service", "../aib-roothome.service"),
        ("sshd.service", "/usr/lib/systemd/system/sshd.service"),
    ):
        link = wants / name
        if not link.is_symlink():
            link.symlink_to(target)
    return directory


def boot(image: str, log_dir: Path, *, packages: Sequence[str], services: Sequence[str]) -> Boot:
    log_dir.mkdir(parents=True, exist_ok=True)
    units = write_units(log_dir.parent / "units")
    # The script travels as a file on a read-only virtiofs mount rather than
    # inside the SSH command line, so what the guest ran is a file anyone can
    # read in the run's artifact.
    script_dir = log_dir / "checks"
    script_dir.mkdir(exist_ok=True)
    (script_dir / "checks.sh").write_text(guest_script(packages, services) + "\n")
    command = [
        "bcvk", "ephemeral", "run-ssh",
        "--memory", VM_MEMORY,
        "--vcpus", VM_VCPUS,
        "--systemd-units", str(units),
        "--ro-bind", f"{script_dir}:{GUEST_SCRIPT_MOUNT}",
        "--log-dir", f"journal,console={log_dir}/",
        image,
        "--",
        f"bash /run/virtiofs-mnt-{GUEST_SCRIPT_MOUNT}/checks.sh",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False, timeout=BOOT_TIMEOUT_SECONDS)
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        partial = f"{exc.stdout or ''}{exc.stderr or ''}"
        output = (partial if isinstance(partial, str) else partial.decode(errors="replace")) + f"\nbcvk timed out after {BOOT_TIMEOUT_SECONDS}s"
    (log_dir / "bcvk.log").write_text(output)
    console_path = log_dir / "console.txt"
    console = console_path.read_text(errors="replace") if console_path.exists() else ""
    console_errors = sorted({match.group(0) for match in CONSOLE_FAILURE_RE.finditer(console)})
    journal = log_dir / "journal.json"
    journal_written = journal.exists() and journal.stat().st_size > 0
    done, state, checks, units_failed = parse_guest_output(output)
    return Boot(done, state, checks, units_failed, console_errors, output, host_failure(output, journal_written=journal_written), console)


def host_failure(output: str, *, journal_written: bool) -> str:
    """bcvk's line naming a host-side failure, when the guest never wrote a journal line."""
    if journal_written:
        return ""
    return next((line.strip() for line in output.splitlines() if HOST_FAILURE_RE.search(line)), "")


def boot_log_tail(boot_result: Boot) -> str:
    """bcvk's own output (the host side, where a startup error shows) and the guest console."""
    half = LOG_TAIL_LINES // 2
    parts = [tail(boot_result.output, half)]
    if boot_result.console:
        parts += ["--- console ---", tail(boot_result.console, half)]
    return "\n".join(parts)


def judge(boot_result: Boot) -> tuple[str, str, list[str]]:
    """(stage, summary, unexpected failed units); stage is "passed" when nothing failed."""
    unexpected = [unit for unit in boot_result.failed_units if unit not in ALLOWED_FAILED_UNITS]
    if boot_result.host_error:
        return "host", f"bcvk never started the VM, so the image was not tested ({boot_result.host_error}).", unexpected
    if not boot_result.reached:
        return "boot", "The VM never reached the point where the checks run (no SSH, or the checks did not finish).", unexpected
    if boot_result.console_errors:
        return "boot", "The console shows: " + ", ".join(boot_result.console_errors) + ".", unexpected
    if boot_result.state not in ("running", "degraded"):
        return "boot", f"Boot did not settle within {SETTLE_TIMEOUT_SECONDS}s (systemd state: {boot_result.state or 'unknown'}).", unexpected
    failed_checks = [check["name"] for check in boot_result.checks if check["status"] != "pass"]
    problems = []
    if failed_checks:
        problems.append("failed checks: " + ", ".join(failed_checks))
    if unexpected:
        problems.append("failed units: " + ", ".join(unexpected))
    if problems:
        return "checks", "Booted, but " + "; ".join(problems) + ".", unexpected
    return "passed", "Built and booted; every check passed.", unexpected


def boot_failure_signature(boot_result: Boot) -> str:
    """How a boot failed, specific enough that two different failures differ.

    judge() calls all of these "boot"; a generated image that never answers
    SSH and a base that answers but panics are not failing "the same way".
    """
    if not boot_result.reached:
        return "unreachable"
    if boot_result.console_errors:
        return "console: " + ", ".join(boot_result.console_errors)
    return f"state: {boot_result.state or 'unknown'}"


def compare_with_upstream(booted: Boot, upstream: Boot) -> str:
    """Whose failure this is, from booting the unmodified base the same way.

    The comparison only assigns blame; it never turns a failure into a pass.
    A boot failure is upstream's only when the base fails with the same
    signature. A failed package or service check is always the generated
    project's, since the unmodified base was never asked for either. Units
    count as upstream's only when every unexpected one also fails in the base.
    """
    stage, _, unexpected = judge(booted)
    upstream_stage, _, upstream_unexpected = judge(upstream)
    if upstream_stage == "host":
        # bcvk never started the base's VM, so the base was not compared at
        # all; "does not fail this way" would blame the tool for the runner.
        return fault_for(stage, upstream_also_fails=None)
    if stage == "boot":
        same = upstream_stage == "boot" and boot_failure_signature(upstream) == boot_failure_signature(booted)
        return fault_for("boot", upstream_also_fails=same)
    if not upstream.reached:
        return fault_for("checks", upstream_also_fails=None)
    failed_checks = any(check["status"] != "pass" for check in booted.checks)
    shared = not failed_checks and bool(unexpected) and set(unexpected) <= set(upstream_unexpected)
    return fault_for("checks", upstream_also_fails=shared)


def run_slot(slot: Slot, work: Path, *, keep_image: bool = False) -> Result:
    result = Result(base=slot.base.key, method=slot.method, image_uri=slot.base.image_uri)
    work.mkdir(parents=True, exist_ok=True)
    built = False
    try:
        context = work / "project"
        result.stage = "generate"
        generate(slot, context)
        result.stage = "build"
        build(slot, context, work / "build.log")
        built = True
        result.base_digest = image_digest(base_reference(slot.base, context))
        booted = None
        for attempt in (1, 2):
            # A second attempt absorbs the nested-VM failures that have nothing
            # to do with the image (a slow runner missing bcvk's SSH deadline).
            # It is a retry of the boot only; the build is not repeated.
            result.attempts = attempt
            retry = boot(slot.tag, work / f"boot-{attempt}", packages=TEST_PACKAGES, services=TEST_SERVICES)
            judged = judge(retry)
            if booted is not None and judged[0] == "host":
                # A retry bcvk never started says nothing about the image, so
                # the first attempt's failure stands. Reporting the retry would
                # file a real failure of this base under the runner issue.
                break
            booted = retry
            stage, summary, unexpected = judged
            if stage == "passed":
                break
        assert booted is not None
        result.checks = booted.checks
        result.failed_units = booted.failed_units
        result.unexpected_units = unexpected
        result.stage = stage
        result.summary = summary
        if stage == "passed":
            result.passed = True
            return result
        result.log_tail = boot_log_tail(booted)
        if stage == "host":
            # The guest never ran, so booting the unmodified base would only
            # fail the same way and say nothing about either image.
            result.fault = fault_for("host")
            return result
        upstream = boot(slot.base.image_uri, work / "upstream", packages=(), services=())
        result.upstream_failed_units = upstream.failed_units if upstream.reached else None
        result.fault = compare_with_upstream(booted, upstream)
    except StageFailure as failure:
        result.stage = failure.stage
        result.summary = failure.summary
        result.fault = failure.fault
        result.log_tail = failure.log_tail
    finally:
        if built and not keep_image:
            subprocess.run(["podman", "rmi", "-f", slot.tag], capture_output=True, check=False)
    return result


def issue_title(label: str) -> str:
    return f"{ISSUE_TITLE_PREFIX}{label}"


def render_issue_body(result: Result, *, run_url: str | None) -> str:
    rows = [
        "| | |",
        "|---|---|",
        f"| Base | `{result.base}` (`{result.image_uri}`) |",
        f"| Base digest | `{result.base_digest or 'not recorded'}` |",
        f"| Method | {result.method} |",
        f"| Failed at | {result.stage} |",
        f"| Boot attempts | {result.attempts} |",
    ]
    lines = [
        f"The nightly variant boot test failed for **{result.label}**.",
        "",
        result.summary or "No result was recorded: the job failed before the test ran (runner setup).",
        "",
        *rows,
        "",
        f"**Whose fault:** {result.fault or 'Runner setup, not the base: check the job log.'}",
    ]
    if result.unexpected_units:
        lines += ["", "Failed units: " + ", ".join(f"`{unit}`" for unit in result.unexpected_units)]
    if result.upstream_failed_units is not None:
        shown = ", ".join(f"`{unit}`" for unit in result.upstream_failed_units) or "none"
        lines += [f"Failed units in the unmodified base, booted the same way: {shown}"]
    if result.log_tail:
        lines += ["", "<details><summary>Last lines of the log</summary>", "", "```text", result.log_tail, "```", "", "</details>"]
    if result.stage in HOST_STAGES:
        # A workstation run would not reproduce a runner that failed to set up
        # or to start bcvk, so the issue points at where that happened.
        lines += [
            "",
            "This is the runner, not the base. Read the failed job's setup steps (\"Prepare the host the way "
            "bcvk's CI does\", \"Install the VM stack\") and bcvk's output in the log above.",
        ]
    else:
        method_args = "" if result.method == CONTAINERFILE else f" --method {result.method}"
        lines += [
            "",
            "Reproduce on a Linux workstation with KVM, podman and bcvk:",
            "",
            "```bash",
            f"python3 variant_boot_test.py run --base {result.base}{method_args} --work /var/tmp/aib-boot-{result.base}",
            "```",
        ]
    if run_url:
        lines += ["", f"Run: {run_url} (logs are in its `variant-boot-*` artifact)"]
    return "\n".join(lines) + "\n"


def find_issue(title: str, repo: str | None) -> int | None:
    args = ["issue", "list", "--state", "open", "--limit", str(ISSUE_LIST_LIMIT), "--json", "number,title,author"]
    if repo:
        args += ["--repo", repo]
    try:
        issues = json.loads(run_gh(args) or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from gh issue list: {exc}") from exc
    if not isinstance(issues, list):
        raise RuntimeError("Unexpected issue list payload")
    for item in issues:
        if not isinstance(item, dict) or item.get("title") != title or not written_by_this_workflow(item.get("author")):
            continue
        if isinstance(item.get("number"), int):
            return item["number"]
    return None


def open_or_comment(title: str, body: str, *, repo_args: list[str], repo: str | None) -> str:
    existing = find_issue(title, repo)
    if existing is None:
        return "opened " + run_gh(["issue", "create", *repo_args, "--title", title, "--body", body]).strip()
    run_gh(["issue", "comment", str(existing), *repo_args, "--body", body])
    return f"commented on #{existing}"


def close_if_open(title: str, note: str, *, repo_args: list[str], repo: str | None) -> int | None:
    existing = find_issue(title, repo)
    if existing is not None:
        run_gh(["issue", "comment", str(existing), *repo_args, "--body", note])
        run_gh(["issue", "close", str(existing), *repo_args])
    return existing


def sync_issue(result: Result, *, repo: str | None, run_url: str | None) -> str:
    """Keep this base's issue, or the one runner issue, in step with the result.

    A failure before the image ran (runner setup, or bcvk failing on the host)
    is not this base's: it goes to HOST_ISSUE_TITLE, which both of a night's
    jobs share, and leaves the base's own issue alone. Any slot that passes
    proves the runner works, so it closes the runner issue too.
    """
    repo_args = ["--repo", repo] if repo else []
    if result.passed:
        note = f"Passed again{f': {run_url}' if run_url else '.'} Closing automatically."
        closed = [
            number
            for number in (
                close_if_open(issue_title(result.label), note, repo_args=repo_args, repo=repo),
                close_if_open(HOST_ISSUE_TITLE, note, repo_args=repo_args, repo=repo),
            )
            if number is not None
        ]
        return f"{result.label} passed" + (f"; closed {', '.join(f'#{n}' for n in closed)}." if closed else "; no open issue.")
    body = render_issue_body(result, run_url=run_url)
    title = HOST_ISSUE_TITLE if result.stage in HOST_STAGES else issue_title(result.label)
    return f"{result.label} failed at {result.stage}; " + open_or_comment(title, body, repo_args=repo_args, repo=repo) + "."


def load_result(path: Path, slot: Slot) -> Result:
    """The result the run step wrote, or a setup failure when it wrote none."""
    if not path.exists():
        return Result(base=slot.base.key, method=slot.method, image_uri=slot.base.image_uri)
    data = json.loads(path.read_text())
    return Result(**data)


def slot_from_args(base: str, method: str) -> Slot:
    return Slot(base_by_key(base), method)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and boot what the tool generates for a base image.")
    commands = parser.add_subparsers(dest="command", required=True)

    plan_cmd = commands.add_parser("plan", help="print tonight's slots as a JSON matrix")
    plan_cmd.add_argument("--date", type=dt.date.fromisoformat, help="the night to plan (default: today, UTC)")
    plan_cmd.add_argument("--base", help="plan this one base instead of the night's pair")
    plan_cmd.add_argument("--method", choices=METHODS, default=CONTAINERFILE)

    run_cmd = commands.add_parser("run", help="generate, build and boot one base")
    run_cmd.add_argument("--base", required=True)
    run_cmd.add_argument("--method", choices=METHODS, default=CONTAINERFILE)
    run_cmd.add_argument("--work", type=Path, required=True, help="directory for the project, logs and result.json")
    run_cmd.add_argument("--keep-image", action="store_true", help="keep the built image instead of removing it")

    report_cmd = commands.add_parser("report", help="open, comment on or close this base's issue")
    report_cmd.add_argument("--base", required=True)
    report_cmd.add_argument("--method", choices=METHODS, default=CONTAINERFILE)
    report_cmd.add_argument("--result", type=Path, required=True)
    report_cmd.add_argument("--repo", help="owner/name; defaults to the current checkout's")
    report_cmd.add_argument("--run-url")

    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            if args.base:
                slots = [slot_from_args(args.base, args.method)]
            else:
                slots = plan(args.date or dt.datetime.now(dt.timezone.utc).date())
            print(json.dumps([{"base": slot.base.key, "method": slot.method} for slot in slots]))
            return 0
        slot = slot_from_args(args.base, args.method)
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "run":
        if (args.work / "project").exists():
            # Never delete a caller's directory: --work is free-form on a workstation.
            parser.error(f"{args.work / 'project'} already exists; pass a new --work directory")
        result = run_slot(slot, args.work, keep_image=args.keep_image)
        (args.work / "result.json").write_text(json.dumps(asdict(result), indent=2) + "\n")
        print(f"{result.label}: {result.stage}: {result.summary}")
        if result.fault:
            print(f"Whose fault: {result.fault}")
        return 0 if result.passed else 1
    try:
        print(sync_issue(load_result(args.result, slot), repo=args.repo, run_url=args.run_url))
    except RuntimeError as exc:
        # The run step already failed the job when the base failed; failing it
        # again over issue bookkeeping would only hide which step went wrong.
        print(f"Could not sync the issue for {slot.label}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
