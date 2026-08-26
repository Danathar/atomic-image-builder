import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import homebrew_formula
from homebrew_formula import (
    PLACEHOLDER_SHA,
    check,
    main,
    parse_formula,
    render_formula,
    tarball_url,
    update,
)

REAL_FORMULA = Path(__file__).resolve().parents[1] / "Formula" / "atomic-image-builder.rb"
SHA_A = "a" * 64
SHA_B = "b" * 64


class HomebrewFormulaTests(unittest.TestCase):
    def formula_copy(self, tmp: str, *, sha: str = PLACEHOLDER_SHA) -> Path:
        path = Path(tmp) / "atomic-image-builder.rb"
        text = REAL_FORMULA.read_text()
        path.write_text(render_formula(text, url=tarball_url("v0.9.0"), sha256=sha))
        return path

    def test_tarball_url_points_at_the_tag_archive(self) -> None:
        self.assertEqual(
            tarball_url("v1.2.3"),
            "https://github.com/Danathar/atomic-image-builder/archive/refs/tags/v1.2.3.tar.gz",
        )

    def test_parses_the_real_formula_in_this_repo(self) -> None:
        # Guards against the formula being reshaped into something the updater
        # can no longer read -- which would fail open, not closed.
        url, sha = parse_formula(REAL_FORMULA.read_text())
        self.assertTrue(url.startswith("https://github.com/Danathar/atomic-image-builder/archive/"))
        self.assertEqual(len(sha), 64)

    def test_render_replaces_only_the_pin_and_keeps_the_commentary(self) -> None:
        original = REAL_FORMULA.read_text()
        rendered = render_formula(original, url=tarball_url("v2.0.0"), sha256=SHA_A)
        self.assertEqual(parse_formula(rendered), (tarball_url("v2.0.0"), SHA_A))
        # The comments and caveats explain decisions; regenerating would lose them.
        self.assertIn("TEMPLATE_SNAPSHOT_DIR resolves", rendered)
        self.assertIn("dnf5 and rpm-ostree", rendered)
        self.assertIn("depends_on \"gum\"", rendered)

    def test_render_rejects_a_formula_missing_its_pin_lines(self) -> None:
        for broken in (f'class X < Formula\n  sha256 "{SHA_A}"\nend\n', 'class X < Formula\n  url "u"\nend\n'):
            with self.subTest(broken=broken[:24]):
                with self.assertRaises(ValueError):
                    render_formula(broken, url="u", sha256=SHA_A)
                with self.assertRaises(ValueError):
                    parse_formula(broken)

    def test_update_writes_the_fetched_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.formula_copy(tmp)
            with patch("homebrew_formula.fetch_sha256", return_value=SHA_B) as fetch:
                returned = update("v1.5.0", path)
            # Read it back before the temp dir goes away.
            self.assertEqual(parse_formula(path.read_text()), (tarball_url("v1.5.0"), SHA_B))
        fetch.assert_called_once_with(tarball_url("v1.5.0"))
        self.assertEqual(returned, SHA_B)

    def test_check_flags_the_unreplaced_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            findings = check(self.formula_copy(tmp))
        self.assertEqual(len(findings), 1)
        self.assertIn("still the placeholder", findings[0])

    def test_check_is_quiet_when_the_digest_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.formula_copy(tmp, sha=SHA_A)
            with patch("homebrew_formula.fetch_sha256", return_value=SHA_A):
                self.assertEqual(check(path), [])

    def test_check_reports_a_mismatched_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.formula_copy(tmp, sha=SHA_A)
            with patch("homebrew_formula.fetch_sha256", return_value=SHA_B):
                findings = check(path)
        self.assertEqual(len(findings), 1)
        self.assertIn(f"recorded {SHA_A}", findings[0])
        self.assertIn(f"actual {SHA_B}", findings[0])

    def test_check_reports_a_fetch_failure_without_claiming_a_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.formula_copy(tmp, sha=SHA_A)
            with patch("homebrew_formula.fetch_sha256", side_effect=OSError("no route to host")):
                findings = check(path)
        self.assertEqual(len(findings), 1)
        self.assertIn("Unable to fetch", findings[0])
        self.assertNotIn("does not match", findings[0])

    def test_fetch_sha256_streams_the_body(self) -> None:
        class FakeResponse:
            def __init__(self) -> None:
                self._chunks = [b"abc", b"def", b""]

            def read(self, _size):
                return self._chunks.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        import hashlib

        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            self.assertEqual(homebrew_formula.fetch_sha256("https://example/x"), hashlib.sha256(b"abcdef").hexdigest())

    def test_main_update_and_check_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.formula_copy(tmp)
            out = io.StringIO()
            with patch("homebrew_formula.fetch_sha256", return_value=SHA_B):
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(["--formula", str(path), "--update", "v9.9.9"]), 0)
            self.assertIn(SHA_B, out.getvalue())

            out = io.StringIO()
            with patch("homebrew_formula.fetch_sha256", return_value=SHA_B):
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(["--formula", str(path), "--check"]), 0)
            self.assertIn("pin is current", out.getvalue())

            out = io.StringIO()
            with patch("homebrew_formula.fetch_sha256", return_value=SHA_A):
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(["--formula", str(path), "--check"]), 1)
            self.assertIn("needs attention", out.getvalue())


if __name__ == "__main__":
    unittest.main()
