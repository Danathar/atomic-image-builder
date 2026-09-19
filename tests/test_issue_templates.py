"""Join `.github/ISSUE_TEMPLATE/*` to the tool and the workflow that read it.

Script: tests/test_issue_templates.py
What: Reads the three issue-form files -- bug_report.yml, feature_request.yml
      and config.yml -- and checks every fact they hand-copy: the version the
      placeholder shows, the install paths the dropdown offers, the base image
      it suggests, the command it tells a reporter to run, the build methods
      the feature form lists, the README sections it cites, the documents its
      links resolve to, and the checkbox wording triage.yml strips out of
      every body.
Doing: Parses each form with `tests/_block_yaml.py`, resolves each claim
       against the thing that decides it -- `main()`'s own `--version` output,
       `BASE_IMAGES`, `METHOD_DISPLAY`, `HOST_REQUIRED_TOOLS`, the Homebrew
       formula's installed command name, docs/installing.md's own path table,
       README's headings and documentation list, the committed tree -- and
       executes triage.yml's labelling step over a body rendered out of
       bug_report.yml itself.
Why: No test at any tier opened these three files. `.coveragerc` measures six
     Python modules and `.coveragerc.e2e` has the same Python-only shape, so
     nothing a form says moves a percentage. They are the first thing a
     stranger meets, they are the only place the project asks for the version
     and install path by name, and triage.yml's `security` rule is written
     against one exact sentence inside one of them -- a reworded checkbox
     re-floods that label, silently, with the suite green.
Goal: Make a renamed install path, a bumped version line, a retired base
     image, a new build method, a moved README section, a dead link or a
     reworded checkbox fail here instead of reaching a reporter as an
     instruction that no longer describes the tool.

The forms are parsed, never grepped. A dropdown option that moved from
`install` to `area`, a `validations: required` that fell off, or a
`render: text` that moved to the wrong textarea all keep a substring search
passing, and each of them changes what a reporter is actually asked for.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _block_yaml import parse as parse_block_yaml  # noqa: E402
from _triage_step import run_label_step  # noqa: E402
from atomic_image_builder import (  # noqa: E402
    BASE_IMAGES,
    HOST_REQUIRED_TOOLS,
    METHOD_DISPLAY,
    TOOL_COMMAND,
    VERSION,
)

TEMPLATE_DIR = ROOT / ".github/ISSUE_TEMPLATE"
BUG_PATH = TEMPLATE_DIR / "bug_report.yml"
FEATURE_PATH = TEMPLATE_DIR / "feature_request.yml"
CONFIG_PATH = TEMPLATE_DIR / "config.yml"

BUG_TEXT = BUG_PATH.read_text()
FEATURE_TEXT = FEATURE_PATH.read_text()
CONFIG_TEXT = CONFIG_PATH.read_text()

BUG = parse_block_yaml(BUG_TEXT)
FEATURE = parse_block_yaml(FEATURE_TEXT)
CONFIG = parse_block_yaml(CONFIG_TEXT)

README = (ROOT / "README.md").read_text()
INSTALLING = (ROOT / "docs/installing.md").read_text()
SECURITY = (ROOT / "SECURITY.md").read_text()
FORMULA = (ROOT / "Formula/atomic-image-builder.rb").read_text()
WRAPPER = (ROOT / "contrib/aib").read_text()
TRIAGE_TEXT = (ROOT / ".github/workflows/triage.yml").read_text()
AUDIT_TEXT = (ROOT / ".github/workflows/maintenance-audit.yml").read_text()
DRIFT_TEXT = (ROOT / "snapshot_drift_issue.py").read_text()

# The forms are the only place a stranger is told what to run, so the text
# they hand-copy is pinned here by field id. A field that disappears takes its
# assertions with it otherwise, and a form that asks for nothing still passes
# every "what it claims is true" check.
BUG_FIELD_IDS = ("area", "version", "install", "base", "what-happened", "repro", "output", "no-secrets")
FEATURE_FIELD_IDS = ("problem", "proposal", "workaround", "build-method")

ITEM_TYPES = frozenset({"markdown", "input", "textarea", "dropdown", "checkboxes"})


def field(form: dict, field_id: str) -> dict:
    """Return the body item with ``field_id``, or fail loudly."""
    for item in form["body"]:
        if item.get("id") == field_id:
            return item
    raise AssertionError(f"no field {field_id!r} in the form")


def options(form: dict, field_id: str) -> list[str]:
    return list(field(form, field_id)["attributes"]["options"])


def tracked_paths() -> set[str]:
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return set(listing.stdout.split())


TRACKED = tracked_paths()

# The slug is read out of README's own CI badge rather than written here, so
# a repository that moves has one place to change and every link below
# follows it.
REPO_SLUG = re.search(r"https://github\.com/([^/]+/[^/]+)/actions/", README).group(1)

LINK_RE = re.compile(r"https://github\.com/[^\s)\"'<>]+")


def heading_anchor(heading: str) -> str:
    """GitHub's anchor slug for a Markdown heading."""
    text = heading.strip().lstrip("#").strip()
    slug = re.sub(r"[^\w\- ]", "", text.replace("`", "").lower())
    return slug.replace(" ", "-")


INSTALLING_ANCHORS = {
    heading_anchor(line) for line in INSTALLING.splitlines() if line.startswith("#")
}


def render_issue_body(form: dict, answers: dict[str, str]) -> str:
    """Render the body GitHub posts when the form is submitted.

    Markdown items are instructions to the reporter and never reach the issue;
    every other item contributes its label as a heading, and a checkbox
    contributes its ticked options. Rendering it here rather than pasting a
    sample body is the point: triage.yml matches against this text, so its
    rules have to be exercised against whatever the form currently says.
    """
    sections: list[str] = []
    for item in form["body"]:
        if item["type"] == "markdown":
            continue
        attributes = item["attributes"]
        sections.append(f"### {attributes['label']}\n")
        if item["type"] == "checkboxes":
            for option in attributes["options"]:
                sections.append(f"- [X] {option['label']}\n")
        else:
            sections.append(f"{answers.get(item['id'], '_No response_')}\n")
    return "\n".join(sections)


def mundane_bug_body() -> str:
    """A bug report with nothing security-sensitive anywhere a human typed."""
    return render_issue_body(
        BUG,
        {
            "area": "The tool itself (the wizard, the scan, the menu)",
            "version": f"{TOOL_COMMAND} {VERSION}",
            "install": "From source",
            "base": BASE_IMAGES[0].image_uri,
            "what-happened": "The menu redrew twice after I resized the window.",
            "repro": "1. Ran the tool\n2. Resized the terminal",
            "output": "no output worth pasting",
        },
    )


class FormShapeTests(unittest.TestCase):
    """The shape GitHub requires, and that the assertions below rely on."""

    def test_both_forms_declare_a_name_description_and_body(self) -> None:
        for path, form in ((BUG_PATH, BUG), (FEATURE_PATH, FEATURE)):
            with self.subTest(form=path.name):
                self.assertTrue(form["name"])
                self.assertTrue(form["description"])
                self.assertIsInstance(form["body"], list)

    def test_every_body_item_declares_a_type_the_forms_schema_defines(self) -> None:
        for path, form in ((BUG_PATH, BUG), (FEATURE_PATH, FEATURE)):
            for index, item in enumerate(form["body"]):
                with self.subTest(form=path.name, index=index):
                    self.assertIn(item["type"], ITEM_TYPES)

    def test_each_form_carries_exactly_the_fields_named_here(self) -> None:
        # The pin that keeps every assertion below non-vacuous: a deleted
        # field fails here rather than turning its own test into a no-op.
        for path, form, expected in (
            (BUG_PATH, BUG, BUG_FIELD_IDS),
            (FEATURE_PATH, FEATURE, FEATURE_FIELD_IDS),
        ):
            with self.subTest(form=path.name):
                found = tuple(item["id"] for item in form["body"] if item["type"] != "markdown")
                self.assertEqual(found, expected)

    def test_markdown_items_carry_no_id_and_no_validations(self) -> None:
        # A markdown block is instruction text. Giving it an id would make it
        # look like a field that collects an answer, and it collects none.
        for path, form in ((BUG_PATH, BUG), (FEATURE_PATH, FEATURE)):
            for item in form["body"]:
                if item["type"] != "markdown":
                    continue
                with self.subTest(form=path.name):
                    self.assertNotIn("id", item)
                    self.assertNotIn("validations", item)
                    self.assertTrue(item["attributes"]["value"].strip())

    def test_every_answered_field_has_a_label(self) -> None:
        for path, form in ((BUG_PATH, BUG), (FEATURE_PATH, FEATURE)):
            for item in form["body"]:
                if item["type"] == "markdown":
                    continue
                with self.subTest(form=path.name, field=item["id"]):
                    self.assertTrue(item["attributes"]["label"].strip())

    def test_every_dropdown_offers_options_and_no_other_field_does(self) -> None:
        for path, form in ((BUG_PATH, BUG), (FEATURE_PATH, FEATURE)):
            for item in form["body"]:
                if item["type"] == "markdown":
                    continue
                with self.subTest(form=path.name, field=item["id"]):
                    has_options = "options" in item["attributes"]
                    self.assertEqual(has_options, item["type"] in {"dropdown", "checkboxes"})

    def test_the_only_optional_answers_are_the_two_that_say_so(self) -> None:
        # Everything else is required, which is what makes a report arrive
        # with the version and install path the triage rules assume.
        optional = {
            item["id"]
            for form in (BUG, FEATURE)
            for item in form["body"]
            if item["type"] not in {"markdown", "checkboxes"}
            and not item.get("validations", {}).get("required")
        }
        self.assertEqual(optional, {"output", "workaround"})

    def test_the_pre_submit_checkbox_is_required(self) -> None:
        checkbox = field(BUG, "no-secrets")
        self.assertEqual(checkbox["type"], "checkboxes")
        self.assertEqual(len(checkbox["attributes"]["options"]), 1)
        self.assertTrue(checkbox["attributes"]["options"][0]["required"])


class RenderedOutputTests(unittest.TestCase):
    def test_the_field_promising_a_code_block_is_the_one_that_renders_as_text(self) -> None:
        # "This is rendered as a code block, so no backticks needed" is only
        # true while `render: text` sits on that same textarea.
        output = field(BUG, "output")
        self.assertEqual(output["attributes"]["render"], "text")
        self.assertIn("rendered as a code block", output["attributes"]["description"])

    def test_no_other_field_renders_as_a_code_block(self) -> None:
        rendered = {
            item["id"]
            for form in (BUG, FEATURE)
            for item in form["body"]
            if item["type"] != "markdown" and "render" in item["attributes"]
        }
        self.assertEqual(rendered, {"output"})


class VersionClaimTests(unittest.TestCase):
    def test_the_tool_accepts_the_flag_the_form_asks_a_reporter_to_run(self) -> None:
        description = field(BUG, "version")["attributes"]["description"]
        self.assertIn(f"`{TOOL_COMMAND} --version`", description)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "atomic_image_builder.py"), "--version"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), f"{TOOL_COMMAND} {VERSION}")

    def test_the_placeholder_shows_what_that_command_prints(self) -> None:
        # The placeholder is a worked example of the answer. It carries the
        # released series, so a 1.0 tool would be asking for "0.9.x" output.
        placeholder = field(BUG, "version")["attributes"]["placeholder"]
        series = ".".join(VERSION.split(".")[:2])
        self.assertEqual(placeholder, f"{TOOL_COMMAND} {series}.x")

    def test_the_readmes_own_beta_warning_names_the_same_series(self) -> None:
        series = ".".join(VERSION.split(".")[:2])
        self.assertIn(f"**{series} beta", README)

    def test_the_wrapper_forwards_the_flag_the_form_offers_as_an_alternative(self) -> None:
        # The form accepts `aib --version` too. That only reaches the tool
        # while the wrapper passes its own arguments through to the image.
        self.assertIn("`aib --version`", field(BUG, "version")["attributes"]["description"])
        self.assertRegex(WRAPPER, r'podman run .*"\$@"')


class InstallPathTests(unittest.TestCase):
    """The install dropdown against docs/installing.md and the two commands."""

    # Ordered, and it raises rather than guessing: an option nobody mapped is
    # a new install path that the documentation may not describe at all,
    # which is exactly the drift this file exists to catch.
    INSTALL_ANCHORS = (
        ("Homebrew", "homebrew"),
        ("contrib/aib wrapper", "installing-the-wrapper"),
        ("Plain podman run", "plain-podman-run"),
        ("Distrobox", "distrobox"),
        ("From source", "from-source"),
    )

    def anchor_for(self, option: str) -> str:
        for needle, anchor in self.INSTALL_ANCHORS:
            if option.startswith(needle):
                return anchor
        raise AssertionError(f"install option {option!r} maps to no section of docs/installing.md")

    def test_every_offered_install_path_has_a_section_in_the_install_doc(self) -> None:
        for option in options(BUG, "install"):
            with self.subTest(option=option):
                self.assertIn(self.anchor_for(option), INSTALLING_ANCHORS)

    def test_every_documented_install_path_is_offered(self) -> None:
        # The other direction. A path documented but not offered leaves its
        # reporters picking something that is not what they did.
        offered = {self.anchor_for(option) for option in options(BUG, "install")}
        self.assertEqual(offered, {anchor for _, anchor in self.INSTALL_ANCHORS})
        for anchor in ("homebrew", "podman", "from-source"):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, INSTALLING_ANCHORS)

    def test_the_three_paths_the_install_doc_tables_are_all_reachable(self) -> None:
        # installing.md opens with a table of paths, each linking to its own
        # section. Every one of them has to be offered by some option.
        table_anchors = set(re.findall(r"\[[^\]]+\]\(#([a-z-]+)\)", INSTALLING.split("## Homebrew")[0]))
        offered = {self.anchor_for(option) for option in options(BUG, "install")}
        self.assertTrue(table_anchors)
        self.assertTrue(
            table_anchors <= offered | {"podman", "limitations-of-running-in-a-container"},
            f"install paths tabled but not offered: {sorted(table_anchors - offered)}",
        )

    def test_the_homebrew_option_names_the_command_the_formula_installs(self) -> None:
        homebrew = next(o for o in options(BUG, "install") if o.startswith("Homebrew"))
        installed = re.search(r'bin\.install_symlink .*=> "([^"]+)"', FORMULA).group(1)
        self.assertEqual(installed, TOOL_COMMAND)
        self.assertEqual(homebrew, f"Homebrew ({installed})")

    def test_the_wrapper_option_names_the_file_and_the_command_it_becomes(self) -> None:
        wrapper = next(o for o in options(BUG, "install") if o.startswith("contrib/aib"))
        self.assertIn("contrib/aib", TRACKED)
        self.assertEqual(wrapper, "contrib/aib wrapper (aib)")

    def test_the_launch_area_option_names_the_same_three_ways_in(self) -> None:
        # The area dropdown's install option is a summary of the install
        # dropdown. Both drift independently unless they are joined.
        launch = next(o for o in options(BUG, "area") if o.startswith("Install or launch"))
        for surface in ("Homebrew", "container image", "contrib/aib"):
            with self.subTest(surface=surface):
                self.assertIn(surface, launch)


class SystemScanTests(unittest.TestCase):
    def test_the_suggested_base_image_is_one_the_tool_offers(self) -> None:
        placeholder = field(BUG, "base")["attributes"]["placeholder"]
        self.assertIn(placeholder, {image.image_uri for image in BASE_IMAGES})

    def test_the_command_the_form_suggests_reads_what_the_scan_reads(self) -> None:
        # The scan runs `rpm-ostree status --json --booted`; the form asks a
        # reporter for the same deployment with the short spelling of the
        # same flag. A scan that stopped scoping itself to the booted
        # deployment would make the answer describe something else.
        description = field(BUG, "base")["attributes"]["description"]
        self.assertIn("`rpm-ostree status -b`", description)
        source = (ROOT / "atomic_image_builder.py").read_text()
        self.assertIn('["rpm-ostree", "status", "--json", "--booted"]', source)

    def test_the_form_expects_no_scan_where_the_tool_requires_none(self) -> None:
        # "container, no host scan" is only a sensible answer while
        # rpm-ostree is a host requirement rather than a bundled one.
        description = field(BUG, "base")["attributes"]["description"]
        self.assertIn("container, no host scan", description)
        self.assertIn("rpm-ostree", HOST_REQUIRED_TOOLS)


class BuildMethodTests(unittest.TestCase):
    def test_every_build_method_the_tool_has_is_offered(self) -> None:
        offered = options(FEATURE, "build-method")
        for display in METHOD_DISPLAY.values():
            with self.subTest(method=display):
                self.assertIn(f"{display} only", offered)

    def test_no_offered_method_is_one_the_tool_does_not_have(self) -> None:
        # The other direction, so a retired method cannot linger in the form.
        named = {option[: -len(" only")] for option in options(FEATURE, "build-method") if option.endswith(" only")}
        self.assertEqual(named, set(METHOD_DISPLAY.values()))

    def test_the_catch_all_options_cover_both_and_neither(self) -> None:
        offered = options(FEATURE, "build-method")
        self.assertIn("Both", offered)
        self.assertTrue(any(option.startswith("Neither") for option in offered))


class ReadmePointerTests(unittest.TestCase):
    def test_the_sections_the_feature_form_sends_readers_to_exist(self) -> None:
        intro = FEATURE["body"][0]["attributes"]["value"]
        # The citations are italicised; `**not**` in the same paragraph is
        # bold, so a single-asterisk span is what names a section.
        cited = re.findall(r"(?<!\*)\*([^*\n]+)\*(?!\*)", intro)
        self.assertEqual(cited, ["Who it's for", "What it does not do"])
        headings = {line.lstrip("#").strip() for line in README.splitlines() if line.startswith("#")}
        for section in cited:
            with self.subTest(section=section):
                self.assertIn(section, headings)

    def test_the_forms_framing_is_the_readmes_own(self) -> None:
        # "not aimed at exposing every advanced workflow" is the README's
        # sentence. The form restates it as the standard a request is judged
        # against, so a softened README would leave the form overstating it.
        intro = " ".join(FEATURE["body"][0]["attributes"]["value"].split())
        self.assertIn("not** aimed at exposing every advanced workflow", intro)
        self.assertIn("not** aimed at exposing every advanced workflow", " ".join(README.split()))


class LinkTests(unittest.TestCase):
    def all_links(self) -> list[tuple[str, str]]:
        return [
            (path.name, url)
            for path, text in (
                (BUG_PATH, BUG_TEXT),
                (FEATURE_PATH, FEATURE_TEXT),
                (CONFIG_PATH, CONFIG_TEXT),
            )
            for url in LINK_RE.findall(text)
        ]

    def test_the_forms_link_only_at_this_repository(self) -> None:
        links = self.all_links()
        self.assertTrue(links)
        for name, url in links:
            with self.subTest(file=name, url=url):
                self.assertTrue(url.startswith(f"https://github.com/{REPO_SLUG}/"), url)

    def test_every_file_a_form_links_to_is_committed(self) -> None:
        prefix = f"https://github.com/{REPO_SLUG}/blob/main/"
        blobs = [(name, url) for name, url in self.all_links() if url.startswith(prefix)]
        self.assertTrue(blobs)
        for name, url in blobs:
            with self.subTest(file=name, url=url):
                self.assertIn(url[len(prefix) :], TRACKED)

    def test_the_security_documents_are_linked_and_committed(self) -> None:
        linked = {url for _, url in self.all_links()}
        self.assertIn(f"https://github.com/{REPO_SLUG}/blob/main/SECURITY.md", linked)
        self.assertIn("SECURITY.md", TRACKED)

    def test_both_files_send_a_vulnerability_to_the_same_place_security_md_does(self) -> None:
        advisory = f"https://github.com/{REPO_SLUG}/security/advisories/new"
        self.assertIn(advisory, BUG_TEXT)
        self.assertIn(advisory, CONFIG_TEXT)
        self.assertIn(advisory, SECURITY)

    def test_the_bug_form_turns_a_vulnerability_away_before_asking_anything(self) -> None:
        # The redirect is only useful while it is the first thing on screen.
        first = BUG["body"][0]
        self.assertEqual(first["type"], "markdown")
        self.assertIn("security vulnerability", first["attributes"]["value"])


class ContactLinkTests(unittest.TestCase):
    def links(self) -> list[dict]:
        return list(CONFIG["contact_links"])

    def test_every_contact_link_has_a_name_url_and_description(self) -> None:
        self.assertTrue(self.links())
        for link in self.links():
            with self.subTest(name=link.get("name")):
                self.assertTrue(link["name"])
                self.assertTrue(link["url"])
                self.assertTrue(link["about"].strip())

    def test_each_documentation_link_describes_it_the_way_the_readme_does(self) -> None:
        # README's documentation list and these links are two hand-written
        # summaries of the same two documents. They are joined so one cannot
        # be updated alone.
        bullets = dict(re.findall(r"- \[[^\]]+\]\((docs/[a-z]+\.md)\) — (.+)", README))
        self.assertEqual(set(bullets), {"docs/installing.md", "docs/using.md"})
        prefix = f"https://github.com/{REPO_SLUG}/blob/main/"
        described = 0
        for link in self.links():
            if not link["url"].startswith(prefix):
                continue
            path = link["url"][len(prefix) :]
            with self.subTest(path=path):
                self.assertIn(path, bullets)
                self.assertEqual(self.normalise(link["about"]), self.normalise(bullets[path]))
                described += 1
        self.assertEqual(described, len(bullets))

    @staticmethod
    def normalise(summary: str) -> str:
        text = " ".join(summary.split()).lower().rstrip(".")
        return text.replace(", and ", ", ")

    def test_the_security_link_names_what_the_security_policy_names(self) -> None:
        security = next(link for link in self.links() if "advisories" in link["url"])
        about = " ".join(security["about"].split())
        self.assertIn("GitHub tokens", about)
        self.assertIn("cosign signing key", about)
        self.assertIn("SECURITY.md", about)
        policy = " ".join(SECURITY.split())
        self.assertIn("GH_TOKEN", policy)
        self.assertIn("cosign signing key", policy)


class BlankIssueTests(unittest.TestCase):
    def comment(self) -> str:
        lines = []
        for line in CONFIG_TEXT.splitlines():
            if not line.startswith("#"):
                break
            lines.append(line.lstrip("# "))
        return " ".join(" ".join(lines).split())

    def test_the_setting_matches_the_reason_written_above_it(self) -> None:
        # The comment is the only record of why blank issues are on. A
        # flipped setting with the paragraph still above it reads as
        # deliberate when it is not.
        self.assertIn("Blank issues stay enabled on purpose", self.comment())
        self.assertIs(CONFIG["blank_issues_enabled"], True)

    def test_the_audit_it_cites_really_does_run_weekly(self) -> None:
        self.assertIn("weekly maintenance audit", self.comment())
        cron = re.search(r"- cron: '([^']+)'", AUDIT_TEXT).group(1)
        self.assertEqual(cron.split()[4], "1")

    def test_the_audit_opens_its_issues_with_a_body_no_template_can_reach(self) -> None:
        # "bypasses templates entirely" is a claim about how the issue is
        # created: gh posts a title and body straight to the API, and an
        # issue created that way never renders a form.
        self.assertIn("bypasses templates", self.comment())
        self.assertIn("snapshot_drift_issue.py", AUDIT_TEXT)
        self.assertRegex(DRIFT_TEXT, r'run_gh\(\s*\n?\s*\["issue", "create", \*repo_args, "--title"')
        self.assertNotIn("--template", DRIFT_TEXT)


class TriageJoinTests(unittest.TestCase):
    """The bug form's wording against the workflow that reads every body."""

    def checkbox_label(self) -> str:
        return field(BUG, "no-secrets")["attributes"]["options"][0]["label"]

    def test_the_line_triage_strips_is_a_line_the_form_produces(self) -> None:
        # triage.yml drops this line with `grep -vF` before matching, so the
        # needle has to be a prefix of what the folded checkbox renders as.
        needle = re.search(r"grep -vF '([^']+)'", TRIAGE_TEXT).group(1)
        self.assertIn(needle, self.checkbox_label())
        self.assertIn(needle, mundane_bug_body())

    def test_the_checkbox_still_carries_the_words_that_made_stripping_necessary(self) -> None:
        # Were they gone, the strip would be dead code and the next person to
        # add them back would re-flood the label.
        label = self.checkbox_label()
        self.assertIn("tokens", label)
        self.assertIn("cosign keys", label)

    def test_an_ordinary_bug_report_off_this_form_is_labelled_nothing(self) -> None:
        result = run_label_step(title="Menu redraws twice on resize", body=mundane_bug_body())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.applied_labels(), [])
        self.assertIn("No label rule matched issue #42.", result.stdout)

    def test_no_answer_this_form_offers_makes_a_report_look_like_a_vulnerability(self) -> None:
        # The checkbox is merely the wording that has bitten. A dropdown
        # option reading "cosign" floods the label just as thoroughly, and
        # only for the reporters who pick it -- so every option a reporter
        # can choose is rendered into a body and run through the rules.
        answers = {
            "version": f"{TOOL_COMMAND} {VERSION}",
            "base": BASE_IMAGES[0].image_uri,
            "what-happened": "The menu redrew twice after I resized the window.",
            "repro": "1. Ran the tool\n2. Resized the terminal",
            "output": "no output worth pasting",
        }
        dropdowns = [item for item in BUG["body"] if item["type"] == "dropdown"]
        self.assertEqual([item["id"] for item in dropdowns], ["area", "install"])
        for dropdown in dropdowns:
            for option in dropdown["attributes"]["options"]:
                with self.subTest(field=dropdown["id"], option=option):
                    body = render_issue_body(BUG, {**answers, dropdown["id"]: option})
                    result = run_label_step(title="Menu redraws twice on resize", body=body)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.applied_labels(), [])

    def test_a_real_security_report_on_this_form_still_reaches_the_label(self) -> None:
        answers = {
            "area": "The tool itself (the wizard, the scan, the menu)",
            "version": f"{TOOL_COMMAND} {VERSION}",
            "install": "From source",
            "base": BASE_IMAGES[0].image_uri,
            "what-happened": "Cosign verification accepts an unsigned image.",
            "repro": "1. Ran the tool\n2. Verified",
            "output": "none",
        }
        result = run_label_step(
            title="Verification accepts unsigned images",
            body=render_issue_body(BUG, answers),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.applied_labels(), ["security"])


class LabelTests(unittest.TestCase):
    def test_each_form_labels_its_issues_and_the_two_differ(self) -> None:
        self.assertEqual(BUG["labels"], ["bug"])
        self.assertEqual(FEATURE["labels"], ["enhancement"])

    def test_the_forms_labels_are_not_ones_triage_applies_by_content(self) -> None:
        # triage.yml only adds labels. A form that pre-applied `security`
        # would make that rule's output unreadable.
        applied = set(re.findall(r'labels\+=\("([^"]+)"\)', TRIAGE_TEXT))
        self.assertTrue(applied)
        self.assertEqual(applied & set(BUG["labels"] + FEATURE["labels"]), set())


if __name__ == "__main__":
    unittest.main()
