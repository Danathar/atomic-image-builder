#!/usr/bin/env python3
"""Keep the Homebrew formula pointed at a real, correctly hashed release.

The formula records a release tarball URL and its sha256. Both are facts about
a specific tag, so they can only be filled in after that tag exists -- which
makes them exactly the kind of thing that gets forgotten and then quietly
serves the wrong version. Two modes:

    --update <tag>   fetch the tarball for <tag>, rewrite url and sha256
    --check          re-download the recorded url and confirm the recorded
                     sha256 still matches it

`--check` runs from the weekly maintenance audit, so a formula left behind
after a release shows up on a schedule instead of when a user hits it.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import urllib.error
import urllib.request
from pathlib import Path

from atomic_image_builder import VERSION

REPO = "Danathar/atomic-image-builder"
FORMULA_PATH = Path("Formula/atomic-image-builder.rb")
URL_RE = re.compile(r'^(\s*url\s+)"([^"]*)"', re.MULTILINE)
SHA_RE = re.compile(r'^(\s*sha256\s+)"([0-9a-f]{64})"', re.MULTILINE)
TAG_RE = re.compile(r"/archive/refs/tags/(.+)\.tar\.gz$")
PLACEHOLDER_SHA = "0" * 64


def tarball_url(tag: str, repo: str = REPO) -> str:
    return f"https://github.com/{repo}/archive/refs/tags/{tag}.tar.gz"


def parse_formula(text: str) -> tuple[str, str]:
    url_match = URL_RE.search(text)
    sha_match = SHA_RE.search(text)
    if url_match is None or sha_match is None:
        raise ValueError("formula is missing a url or sha256 line")
    return url_match.group(2), sha_match.group(2)


def render_formula(text: str, *, url: str, sha256: str) -> str:
    # Rewrites in place rather than regenerating: the formula carries comments
    # and a caveats block that explain decisions, and regenerating would drop
    # them or force them into this file as strings.
    if URL_RE.search(text) is None or SHA_RE.search(text) is None:
        raise ValueError("formula is missing a url or sha256 line")
    text = URL_RE.sub(lambda m: f'{m.group(1)}"{url}"', text, count=1)
    return SHA_RE.sub(lambda m: f'{m.group(1)}"{sha256}"', text, count=1)


def formula_tag(url: str) -> str | None:
    match = TAG_RE.search(url)
    return match.group(1) if match else None


def fetch_sha256(url: str, *, timeout: float = 60.0) -> str:
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "atomic-image-builder-formula"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for chunk in iter(lambda: response.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def update(tag: str, formula_path: Path) -> str:
    url = tarball_url(tag)
    sha256 = fetch_sha256(url)
    formula_path.write_text(render_formula(formula_path.read_text(), url=url, sha256=sha256))
    return sha256


def check(formula_path: Path, *, expected_version: str = VERSION) -> list[str]:
    findings: list[str] = []
    text = formula_path.read_text()
    url, recorded = parse_formula(text)

    # Digest agreement alone cannot catch a forgotten release: after VERSION is
    # bumped and tagged, the formula still points at the PREVIOUS tarball, whose
    # digest still matches it perfectly. The check would report "current" while
    # every brew install served the old version -- the exact failure this is here
    # to prevent. So compare the tag the formula names against the tool's own
    # VERSION first. That needs no network and fires the moment they diverge.
    expected_tag = f"v{expected_version}"
    tag = formula_tag(url)
    if tag is None:
        findings.append(f"Formula url is not a tag archive, so its version cannot be checked: {url}")
    elif tag != expected_tag:
        findings.append(
            f"Formula points at {tag} but the tool's VERSION is {expected_version}. "
            f"Run: python3 {Path(__file__).name} --update {expected_tag}"
        )

    if recorded == PLACEHOLDER_SHA:
        findings.append(f"Formula sha256 is still the placeholder. Run: python3 {Path(__file__).name} --update {expected_tag}")
        return findings

    try:
        actual = fetch_sha256(url)
    except (OSError, ValueError) as exc:
        findings.append(f"Unable to fetch {url}: {exc}")
        return findings
    if actual != recorded:
        findings.append(f"Formula sha256 does not match {url}: recorded {recorded}, actual {actual}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update or verify the Homebrew formula's release pin.")
    parser.add_argument("--formula", type=Path, default=None, help="Path to the formula (default: Formula/atomic-image-builder.rb).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--update", metavar="TAG", help="Point the formula at TAG's tarball and record its sha256.")
    group.add_argument("--check", action="store_true", help="Verify the recorded sha256 still matches the recorded url.")
    args = parser.parse_args(argv)
    formula_path = args.formula or (Path(__file__).resolve().parent / FORMULA_PATH)

    if args.update:
        sha256 = update(args.update, formula_path)
        print(f"Formula updated for {args.update}: sha256 {sha256}")
        return 0

    findings = check(formula_path)
    if not findings:
        print("Homebrew formula pin is current.")
        return 0
    print("Homebrew formula needs attention:")
    for finding in findings:
        print(f"- {finding}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
