"""Join docs/using.md to the code that has to keep its promises true.

Script: tests/test_using_doc.py
What: Reads docs/using.md and checks every literal it hand-copies against the
      generator constant, generated artifact or helper that produces it.
Doing: Splits the doc into heading-scoped sections, pulls its inline code
       spans, fenced blocks and links, and compares each claim to the thing it
       describes -- the Homebrew constants, the generated Containerfile parsed
       by tests/_containerfile.py, the payload unit tests/test_brew_setup_staging.py
       reproduces, the disk-builder pin helper, and the switch command the tool
       itself prints.
Why: docs/using.md was opened by no test at any tier. It is the only
     user-facing description of what the generated image does to the brew
     layer, and every path, unit name, flag and image reference in it is a hand
     copy. `.coveragerc` measures Python modules, so no percentage moves when
     one of them stops being true: the doc would keep telling a reader that the
     build deletes three login-shell fragments, or that the drop-in carries
     `PrivateTmp=yes`, after the build stopped doing it.
Goal: Make a rename, a re-pin or a dropped step on the code side fail here
      instead of silently turning the security rationale in this doc into
      fiction.

Scoping is by heading, not by whole-file search: a claim that moved out of the
section that explains it is a different document, and `assertIn` over 183 lines
cannot see the move.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import atomic_image_builder  # noqa: E402
from _containerfile import parse as parse_containerfile  # noqa: E402
from atomic_image_builder import (  # noqa: E402
    BOOTC_IMAGE_BUILDER_IMAGE,
    BOOTC_IMAGE_BUILDER_IMAGE_DIGEST,
    BOOTC_IMAGE_BUILDER_IMAGE_REPO,
    BOOTC_IMAGE_BUILDER_IMAGE_TAG,
    BREW_LOGIN_FRAGMENTS,
    BREW_LOGIN_SHELL_DIRS,
    BREW_PATH_FRAGMENT,
    BREW_SETUP_DROPIN,
    BREW_SETUP_UNIT,
    UNIVERSAL_BLUE_BREW_IMAGE,
    UNIVERSAL_BLUE_BREW_IMAGE_DIGEST,
    UNIVERSAL_BLUE_BREW_IMAGE_REPO,
    UNIVERSAL_BLUE_BREW_IMAGE_TAG,
    App,
    Config,
    ghcr_package_page_url,
    pin_disk_builder_image_line,
)
from test_brew_setup_staging import PAYLOAD_UNIT  # noqa: E402

DOC_PATH = ROOT / "docs/using.md"
DOC = DOC_PATH.read_text()

# Headings this file asserts against. Named rather than discovered so that
# renaming one is a failure here: the section titles are what scopes every
# other assertion, and a silently renamed section would make the assertions
# under it vacuous instead of red.
DELETES_SECTION = "Why the generated Containerfile deletes three of the layer's own files"
DROPIN_SECTION = "Why the generated Containerfile adds a drop-in for `brew-setup.service`"
HOMEBREW_SECTION = "Homebrew on Fedora Atomic images"
MIGRATION_SECTION = "Migrating layered packages from your current system"
EXPECT_SECTION = "What else to expect"
READABLE_SECTION = "Make the package readable before you switch"

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
}


def split_sections(text: str) -> dict[str, str]:
    """Map each heading's exact title to the body beneath it.

    The body stops at the next heading of any level, so a claim that slid
    under a neighbouring heading is no longer in the section that is supposed
    to carry it.
    """
    sections: dict[str, str] = {}
    title: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if match is None:
            body.append(line)
            continue
        if title is not None:
            sections[title] = "\n".join(body)
        title = match.group(2)
        body = []
    if title is not None:
        sections[title] = "\n".join(body)
    return sections


SECTIONS = split_sections(DOC)


def flatten(text: str) -> str:
    """Collapse the doc's hard wrapping so a sentence can be searched for.

    docs/using.md is wrapped at ~79 columns, so most of its sentences contain a
    newline in a place no author chose. Searching the raw text for a phrase
    would pass or fail on where the wrap happened to fall.
    """
    return re.sub(r"\s+", " ", text)


def code_spans(text: str) -> tuple[str, ...]:
    """Every single-backtick span, fenced blocks removed first.

    Fences are stripped so a command inside a ```bash block cannot masquerade
    as an inline claim (and so an odd backtick inside one cannot swallow the
    prose that follows it).
    """
    without_fences = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
    return tuple(match.group(1) for match in re.finditer(r"`([^`\n]+)`", without_fences))


def fenced_blocks(text: str) -> tuple[tuple[str, str], ...]:
    """Every fenced block as ``(language, body)``, in document order."""
    return tuple(
        (match.group(1), match.group(2))
        for match in re.finditer(r"^```(\w*)\n(.*?)^```\s*$", text, flags=re.S | re.M)
    )


def links(text: str) -> tuple[tuple[str, str], ...]:
    """Every inline markdown link as ``(label, target)``."""
    return tuple(
        (match.group(1), match.group(2))
        for match in re.finditer(r"\[([^\]]+)\]\(([^)\s]+)\)", text)
    )


def heading_slug(title: str) -> str:
    """GitHub's anchor for a heading: lowercased, punctuation dropped, spaces hyphenated."""
    slug = title.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s+", "-", slug)


def is_login_fragment(span: str) -> bool:
    """Whether a code span names a login-shell script file.

    The deletion section names directories and prefixes in backticks too, so
    membership is decided by shape -- an absolute path to a shell script --
    rather than by matching the constant this test is supposed to check. The
    replacement fragment is excluded by name: the section names it as what the
    build writes, not as one of the files it deletes.
    """
    return (
        span.startswith("/")
        and span.rpartition("/")[2].endswith((".sh", ".fish"))
        and span != BREW_PATH_FRAGMENT
    )


def exec_starts(unit_text: str) -> tuple[str, ...]:
    return tuple(
        line.strip() for line in unit_text.splitlines() if line.strip().startswith("ExecStart=")
    )


class DocStructureTests(unittest.TestCase):
    """The sections the rest of this file scopes its assertions to."""

    def test_every_asserted_section_is_present(self) -> None:
        for title in (
            DELETES_SECTION,
            DROPIN_SECTION,
            HOMEBREW_SECTION,
            MIGRATION_SECTION,
            EXPECT_SECTION,
            READABLE_SECTION,
        ):
            self.assertIn(title, SECTIONS, f"docs/using.md no longer has a '{title}' section")

    def test_the_extractors_find_something_in_this_document(self) -> None:
        # A regex that stopped matching would make every assertion below pass
        # over an empty tuple. Cheap insurance against a silent green suite.
        self.assertGreater(len(code_spans(DOC)), 20)
        self.assertGreater(len(fenced_blocks(DOC)), 1)
        self.assertGreater(len(links(DOC)), 3)


class LinkTests(unittest.TestCase):
    """The doc routes readers to other files; a dead route is a dead end."""

    def test_every_relative_link_resolves_to_a_file_and_a_heading(self) -> None:
        checked = 0
        for label, target in links(DOC):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_part, _, anchor = target.partition("#")
            if path_part:
                resolved = (DOC_PATH.parent / path_part).resolve()
                self.assertTrue(
                    resolved.is_file(),
                    f"docs/using.md links to {target!r} ({label!r}) but {path_part} does not exist",
                )
            else:
                resolved = DOC_PATH
            if anchor:
                slugs = {heading_slug(title) for title in split_sections(resolved.read_text())}
                self.assertIn(
                    anchor,
                    slugs,
                    f"docs/using.md links to {target!r} but {resolved.name} has no such heading",
                )
            checked += 1
        self.assertGreaterEqual(checked, 3, "no relative links were checked")

    def test_the_readme_still_points_at_this_doc(self) -> None:
        # The doc is reachable only from README's documentation list; an
        # orphaned file is one nobody is told to read.
        targets = {target for _label, target in links((ROOT / "README.md").read_text())}
        self.assertIn("docs/using.md", targets)


class PackageVisibilityTests(unittest.TestCase):
    """The URL the doc tells a reader to open is the one the tool opens."""

    def test_the_package_page_url_matches_the_helper(self) -> None:
        template = next(
            span
            for span in code_spans(SECTIONS[READABLE_SECTION])
            if span.startswith("https://github.com/")
        )
        rendered = (
            template.replace("<your-user>", "example").replace("<your-repo>", "test-image")
        )
        self.assertEqual(rendered, ghcr_package_page_url("example", "test-image"))


class HomebrewLayerTests(unittest.TestCase):
    """What the doc says the Homebrew block does, against what it emits."""

    maxDiff = None

    def brew_containerfile(self):
        app = App()
        app.config = Config(
            method="containerfile",
            base_image_uri="quay.io/fedora-ostree-desktops/silverblue:43",
            base_image_name="Fedora Silverblue",
            repo_name="test-image",
            image_desc="Test image",
            github_user="example",
        )
        app.config.brew_enabled = True
        return parse_containerfile(app.generate_containerfile())

    def brew_run(self, needle: str):
        """The single RUN whose joined command contains ``needle``."""
        matches = [
            item
            for item in self.brew_containerfile()
            if item.keyword == "RUN" and needle in item.argument
        ]
        self.assertEqual(len(matches), 1, f"expected exactly one RUN containing {needle!r}")
        return matches[0]

    def test_the_doc_names_the_layer_the_block_copies_from(self) -> None:
        spans = code_spans(SECTIONS[HOMEBREW_SECTION])
        self.assertIn(UNIVERSAL_BLUE_BREW_IMAGE_REPO, spans)
        copies = [item for item in self.brew_containerfile() if item.keyword == "COPY"]
        brew_copy = next(item for item in copies if item.flags)
        self.assertEqual(brew_copy.flag("from"), UNIVERSAL_BLUE_BREW_IMAGE)

    def test_naming_it_by_digest_rather_than_tag_is_what_the_block_does(self) -> None:
        # "The generated Containerfile names it by digest rather than by tag"
        # is a security claim -- the signature covers what the digest names.
        self.assertIn("by digest rather than by tag", flatten(SECTIONS[HOMEBREW_SECTION]))
        self.assertEqual(
            UNIVERSAL_BLUE_BREW_IMAGE,
            f"{UNIVERSAL_BLUE_BREW_IMAGE_REPO}@{UNIVERSAL_BLUE_BREW_IMAGE_DIGEST}",
        )
        self.assertTrue(UNIVERSAL_BLUE_BREW_IMAGE_DIGEST.startswith("sha256:"))
        brew_copy = next(
            item
            for item in self.brew_containerfile()
            if item.keyword == "COPY" and item.flags
        )
        reference = brew_copy.flag("from")
        self.assertNotIn(f":{UNIVERSAL_BLUE_BREW_IMAGE_TAG}", reference)
        self.assertIn("@sha256:", reference)

    def test_the_doc_lists_exactly_the_units_the_block_presets(self) -> None:
        # The bullet list under "This adds:" is the reader's only inventory of
        # what lands in the image, and the preset RUN is what lands.
        preset = self.brew_run("systemctl preset")
        presets = {
            part.rsplit(" ", 1)[-1]
            for part in preset.argument.split(" && ")
            if "systemctl preset" in part
        }
        section = SECTIONS[HOMEBREW_SECTION]
        named = {span for span in code_spans(section) if span.endswith((".service", ".timer"))}
        self.assertEqual(named, presets)

    def test_the_doc_names_the_one_fragment_the_image_keeps(self) -> None:
        self.assertIn(BREW_PATH_FRAGMENT, code_spans(SECTIONS[HOMEBREW_SECTION]))
        sweep = self.brew_run(BREW_PATH_FRAGMENT)
        self.assertIn(f"> {BREW_PATH_FRAGMENT}", sweep.argument)

    def test_the_heading_counts_the_fragments_the_build_deletes(self) -> None:
        # "deletes three of the layer's own files" is a number a reader
        # trusts. Growing BREW_LOGIN_FRAGMENTS without touching the heading
        # leaves a doc that undercounts what the build removes.
        word = DELETES_SECTION.split()[5]
        self.assertEqual(NUMBER_WORDS[word], len(BREW_LOGIN_FRAGMENTS))

    def test_the_doc_names_every_fragment_the_build_deletes_and_no_others(self) -> None:
        section = SECTIONS[DELETES_SECTION]
        named = {span for span in code_spans(section) if is_login_fragment(span)}
        self.assertEqual(
            named,
            set(BREW_LOGIN_FRAGMENTS),
            "the fragments docs/using.md names are not the ones BREW_LOGIN_FRAGMENTS deletes",
        )

    def test_the_build_removes_the_fragments_the_doc_names(self) -> None:
        sweep = self.brew_run(BREW_PATH_FRAGMENT)
        self.assertTrue(sweep.argument.startswith("rm -f "), sweep.argument)
        named = [span for span in code_spans(SECTIONS[DELETES_SECTION]) if is_login_fragment(span)]
        self.assertTrue(named)
        for fragment in named:
            self.assertIn(fragment, sweep.argument)

    def test_the_replacement_fragment_is_guarded_the_way_the_doc_says(self) -> None:
        # "only when the prefix is owned by the account whose shell it is
        # (`test -O`)" is the whole reason the replacement is safe to keep.
        section = SECTIONS[DELETES_SECTION]
        self.assertIn("test -O", code_spans(section))
        sweep = self.brew_run(BREW_PATH_FRAGMENT)
        self.assertIn("-O /home/linuxbrew/.linuxbrew", sweep.argument)

    def test_the_build_greps_the_login_shell_directories_and_fails(self) -> None:
        # "The build then greps the login-shell directories and fails if
        # anything else still mentions brew" -- the half that covers the next
        # digest, not just today's three names.
        self.assertIn(
            "greps the login-shell directories and fails",
            flatten(SECTIONS[DELETES_SECTION]),
        )
        sweep = self.brew_run(BREW_PATH_FRAGMENT)
        for directory in BREW_LOGIN_SHELL_DIRS:
            self.assertIn(directory, sweep.argument)
        self.assertIn("exit 1", sweep.argument)

    def test_fish_gets_no_path_entry_because_it_reads_no_profile_d(self) -> None:
        # The doc tells fish users to add their own PATH entry. That is only
        # honest while the replacement really is a /etc/profile.d fragment.
        self.assertIn("fish does not read `/etc/profile.d`", flatten(SECTIONS[DELETES_SECTION]))
        self.assertTrue(BREW_PATH_FRAGMENT.startswith("/etc/profile.d/"))


class BrewSetupDropinTests(unittest.TestCase):
    """The drop-in section quotes a unit this repository does not ship."""

    def setUp(self) -> None:
        self.section = SECTIONS[DROPIN_SECTION]

    def test_the_quoted_unit_lines_are_lines_the_payload_really_ships(self) -> None:
        # The ini block is a quotation of ublue-os/brew's brew-setup.service.
        # tests/test_brew_setup_staging.py reproduces that unit at the pinned
        # digest, so the quotation can be checked rather than trusted.
        (language, body), = [
            (language, body) for language, body in fenced_blocks(self.section) if language == "ini"
        ]
        self.assertEqual(language, "ini")
        quoted = exec_starts(body)
        self.assertEqual(len(quoted), 4, body)
        shipped = exec_starts(PAYLOAD_UNIT)
        for line in quoted:
            self.assertIn(line, shipped)
        # Order matters: the doc's argument is that mkdir runs before tar,
        # which runs before the cp and the chown.
        self.assertEqual(quoted, tuple(line for line in shipped if line in set(quoted)))

    def test_the_unit_really_ships_no_private_tmp(self) -> None:
        self.assertIn("The unit ships no `PrivateTmp=`", flatten(self.section))
        self.assertNotIn("PrivateTmp", PAYLOAD_UNIT)

    def test_the_condition_the_doc_leans_on_is_in_the_unit(self) -> None:
        # "the unit's own `ConditionPathExists=!/home/linuxbrew/.linuxbrew`
        # guarantees the destination does not exist yet" is why the `-n` on
        # the cp protects nothing.
        condition = next(
            span for span in code_spans(self.section) if span.startswith("ConditionPathExists=")
        )
        self.assertIn(condition, PAYLOAD_UNIT)

    def test_the_dropin_path_and_its_single_setting_match_the_build(self) -> None:
        spans = code_spans(self.section)
        self.assertIn(BREW_SETUP_DROPIN, spans)
        self.assertIn("PrivateTmp=yes", spans)
        emitted = "\n".join(atomic_image_builder.brew_setup_staging_run_lines())
        self.assertIn(BREW_SETUP_DROPIN, emitted)
        self.assertIn("PrivateTmp=yes", emitted)

    def test_the_build_refuses_a_payload_the_dropin_would_not_protect(self) -> None:
        # "The same build step therefore fails if the layer stops shipping
        # brew-setup.service, or if none of that unit's `ExecStart=` lines
        # stage under `/tmp` or `/var/tmp`".
        self.assertIn("/var/tmp", code_spans(self.section))
        emitted = "\n".join(atomic_image_builder.brew_setup_staging_run_lines())
        self.assertIn(BREW_SETUP_UNIT, emitted)
        self.assertIn("/var/tmp", emitted)
        self.assertIn("exit 1", emitted)

    def test_a_dropin_is_used_rather_than_an_exec_start_override(self) -> None:
        # The doc justifies the drop-in by saying a copied command chain would
        # drift. That is only true while nothing emitted writes ExecStart=.
        self.assertIn("rather than a unit that overrides", flatten(self.section))
        emitted = "\n".join(atomic_image_builder.brew_setup_staging_run_lines())
        self.assertNotIn("ExecStart=/usr/bin/", emitted)


class UniversalBlueTests(unittest.TestCase):
    """The doc's claim that the offer is skipped for Universal Blue bases."""

    def make_app(self, base_image_uri: str) -> App:
        app = App()
        app.config = Config(
            method="containerfile",
            base_image_uri=base_image_uri,
            base_image_name="base",
            repo_name="test-image",
            image_desc="Test image",
            github_user="example",
        )
        return app

    def test_the_offer_is_skipped_and_forced_off_for_a_universal_blue_base(self) -> None:
        self.assertIn(
            "skipped automatically for Universal Blue base images",
            flatten(SECTIONS[HOMEBREW_SECTION]),
        )
        app = self.make_app("ghcr.io/ublue-os/bazzite:stable")
        app.config.brew_enabled = True
        # No gum stub: the claim is that this returns before prompting, so a
        # version that prompted would fail here rather than hang.
        app.offer_brew_if_applicable()
        self.assertFalse(app.config.brew_enabled)

    def test_a_fedora_atomic_base_is_not_treated_as_universal_blue(self) -> None:
        app = self.make_app("quay.io/fedora-ostree-desktops/silverblue:43")
        self.assertFalse(app.is_universal_blue_base())


class DiskBuilderPinTests(unittest.TestCase):
    """`build-disk.yml` and `just build-qcow2` are named as digest-pinned."""

    def test_the_doc_names_both_disk_build_paths(self) -> None:
        spans = code_spans(SECTIONS[EXPECT_SECTION])
        self.assertIn("build-disk.yml", spans)
        self.assertIn("just build-qcow2", spans)
        self.assertIn("bootc-image-builder", flatten(SECTIONS[EXPECT_SECTION]))
        self.assertIn("by digest rather than by tag", flatten(SECTIONS[EXPECT_SECTION]))

    def test_both_spellings_of_the_pin_resolve_to_the_same_digest(self) -> None:
        # The workflow spells it `BIB_IMAGE: "..."` and image-template.env
        # spells it `BIB_IMAGE="..."`. One helper pins both, which is what
        # makes the doc's "both" true.
        workflow_line = f'  BIB_IMAGE: "{BOOTC_IMAGE_BUILDER_IMAGE_REPO}:{BOOTC_IMAGE_BUILDER_IMAGE_TAG}"'
        env_line = f'BIB_IMAGE="{BOOTC_IMAGE_BUILDER_IMAGE_REPO}:{BOOTC_IMAGE_BUILDER_IMAGE_TAG}"'
        self.assertEqual(
            pin_disk_builder_image_line(workflow_line),
            f'  BIB_IMAGE: "{BOOTC_IMAGE_BUILDER_IMAGE}"',
        )
        self.assertEqual(
            pin_disk_builder_image_line(env_line),
            f'BIB_IMAGE="{BOOTC_IMAGE_BUILDER_IMAGE}"',
        )
        self.assertEqual(
            BOOTC_IMAGE_BUILDER_IMAGE,
            f"{BOOTC_IMAGE_BUILDER_IMAGE_REPO}@{BOOTC_IMAGE_BUILDER_IMAGE_DIGEST}",
        )
        self.assertTrue(BOOTC_IMAGE_BUILDER_IMAGE_DIGEST.startswith("sha256:"))


class MigrationTests(unittest.TestCase):
    """The three commands the doc tells a reader to run before rebooting."""

    def setUp(self) -> None:
        self.section = SECTIONS[MIGRATION_SECTION]
        (self.language, self.body), = [
            (language, body)
            for language, body in fenced_blocks(self.section)
            if language == "bash"
        ]
        self.commands = [line for line in self.body.splitlines() if line.strip()]

    def test_the_block_is_reset_then_switch_then_reboot(self) -> None:
        # Order is the claim: the reset clears layered state from the
        # deployment *before* the switch replaces it.
        self.assertEqual(len(self.commands), 3, self.commands)
        self.assertEqual(self.commands[0], "sudo rpm-ostree reset")
        self.assertEqual(self.commands[2], "systemctl reboot")

    def test_the_switch_command_is_the_one_the_tool_itself_prints(self) -> None:
        app = App()
        app.config = Config(
            method="containerfile",
            base_image_uri="quay.io/fedora-ostree-desktops/silverblue:43",
            base_image_name="Fedora Silverblue",
            repo_name="<your-repo>",
            image_desc="Test image",
            github_user="<your-user>",
        )
        expected = App.bootc_switch_command(
            app.published_image_ref(), signing_enabled=True
        )
        self.assertEqual(self.commands[1], expected)

    def test_dropping_the_flag_is_what_turns_verification_off(self) -> None:
        # "Dropping the flag disables signature verification for both the
        # switch and those upgrades" -- true only while the flag is exactly
        # what signing_enabled controls.
        self.assertIn("--enforce-container-sigpolicy", code_spans(self.section))
        unsigned = App.bootc_switch_command("ghcr.io/example/test-image:latest", signing_enabled=False)
        self.assertNotIn("--enforce-container-sigpolicy", unsigned)
        signed = App.bootc_switch_command("ghcr.io/example/test-image:latest", signing_enabled=True)
        self.assertIn("--enforce-container-sigpolicy", signed)

    def test_the_signing_key_section_the_doc_sends_readers_to_exists(self) -> None:
        # "first follow **Trusting The Signing Key** in the generated repo's
        # README" is a pointer into a document this tool writes.
        match = re.search(r"\*\*([^*]+)\*\* in the generated repo's README", flatten(self.section))
        self.assertIsNotNone(match, "the doc no longer points at a README section by name")
        app = App()
        app.config = Config(
            method="containerfile",
            base_image_uri="quay.io/fedora-ostree-desktops/silverblue:43",
            base_image_name="Fedora Silverblue",
            repo_name="test-image",
            image_desc="Test image",
            github_user="example",
        )
        app.config.signing_enabled = True
        titles = set(split_sections(app.generate_readme()))
        self.assertIn(match.group(1), titles)


if __name__ == "__main__":
    unittest.main()
