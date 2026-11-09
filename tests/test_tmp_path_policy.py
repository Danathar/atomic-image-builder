"""Block fixed-name paths under the shared /tmp in shipped files.

Script: tests/test_tmp_path_policy.py
What: Scans every tracked text file (`git ls-files`) outside `tests/`,
      `.claude/` and the policy file itself for `/tmp/<name>` where `/tmp` starts an absolute path, and
      compares the hits with .github/policies/tmp-paths.json in both
      directions: an unlisted (file, path) pair fails, and a listed entry that
      matches nothing fails.
Why: Any account on a machine can create `/tmp/<name>` before the intended
     user does, then read what is written there or steer what is read back.
     Safe uses (a throwaway build container, text that only describes the
     problem) go on the reviewed allowlist; everything else should use
     `PrivateTmp=yes`, `RuntimeDirectory=`/`StateDirectory=`, `mktemp -d`, or a
     root-owned `/run/<name>`.
Goal: A new fixed `/tmp/<name>` fails here until it is removed or listed with a
      reason in the same pull request.

Limitation: the check reads text only. It cannot see a path built at runtime
(for example `tempfile.gettempdir()` plus a fixed name) or generator output
assembled from fragments, and it skips binary files.
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / ".github" / "policies" / "tmp-paths.json"

TMP_PATH = re.compile(r"/tmp/[A-Za-z0-9._-]+")
# `/tmp` is the start of an absolute path unless a path segment sits just
# before it (`/var/tmp/x`).
SEGMENT_BEFORE = re.compile(r"/[A-Za-z0-9._-]+$")
# Not shipped: tests and agent tooling.
EXCLUDED_PREFIXES = ("tests/", ".claude/")
# The allowlist names every allowed path, so scanning it would flag each entry.
POLICY_FILE = POLICY_PATH.relative_to(ROOT).as_posix()
ADVICE = (
    "Use PrivateTmp=yes, RuntimeDirectory=/StateDirectory=, `mktemp -d`, or a root-owned /run/<name>; "
    f"or list the (file, path) pair with a reason in {POLICY_PATH.name}."
)


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout.decode("utf-8")
    return [name for name in out.split("\0") if name]


def read_text(path: Path) -> str | None:
    """The file's text, or None for a binary, missing or non-UTF-8 file."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def find_hits(root: Path, names: list[str]) -> list[tuple[str, int, str]]:
    """(file, line, /tmp/<name>) for each in-scope match under *root*."""
    hits = []
    for name in names:
        if name.startswith(EXCLUDED_PREFIXES) or name == POLICY_FILE:
            continue
        text = read_text(root / name)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for match in TMP_PATH.finditer(line):
                if SEGMENT_BEFORE.search(line[: match.start()]):
                    continue
                hits.append((name, number, match.group()))
    return hits


def problems(hits: list[tuple[str, int, str]], allowed: list[dict[str, str]]) -> list[str]:
    """Failure messages: unlisted hits first, then entries that match nothing."""
    listed = {(entry["file"], entry["tmp_path"]) for entry in allowed}
    found = {(name, path) for name, _, path in hits}
    messages = [
        f"{name}:{number}: {path} is a fixed name in the shared /tmp. {ADVICE}"
        for name, number, path in hits
        if (name, path) not in listed
    ]
    messages += [
        f"{POLICY_PATH.name}: entry ({name}, {path}) matches nothing; remove it."
        for name, path in sorted(listed - found)
    ]
    return messages


class RealTreeTests(unittest.TestCase):
    def test_the_tree_has_no_unlisted_fixed_tmp_paths(self) -> None:
        allowed = json.loads(POLICY_PATH.read_text(encoding="utf-8"))["allowed"]
        self.assertEqual(problems(find_hits(ROOT, tracked_files(ROOT)), allowed), [])

    def test_every_entry_has_a_reason(self) -> None:
        for entry in json.loads(POLICY_PATH.read_text(encoding="utf-8"))["allowed"]:
            with self.subTest(entry=entry):
                self.assertEqual(set(entry), {"file", "tmp_path", "why"})
                self.assertTrue(entry["why"].strip())
                self.assertRegex(entry["tmp_path"], r"^/tmp/[A-Za-z0-9._-]+$")


class MatcherTests(unittest.TestCase):
    def scan(self, files: dict[str, str | bytes], allowed: list[dict[str, str]] | None = None) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    path.write_bytes(content)
                else:
                    path.write_text(content, encoding="utf-8")
            return problems(find_hits(root, list(files)), allowed or [])

    def test_a_planted_path_fails_with_file_and_line(self) -> None:
        messages = self.scan({"deploy.sh": "echo hi\ncurl -o /tmp/payload x\n"})
        self.assertEqual(len(messages), 1)
        self.assertIn("deploy.sh:2:", messages[0])
        self.assertIn("/tmp/payload", messages[0])
        self.assertIn("mktemp -d", messages[0])

    def test_the_same_text_under_tests_or_claude_passes(self) -> None:
        text = "curl -o /tmp/payload x\n"
        self.assertEqual(self.scan({"tests/fixtures/a.sh": text, ".claude/hooks/b.py": text}), [])

    def test_the_policy_file_itself_is_not_scanned(self) -> None:
        self.assertEqual(self.scan({POLICY_FILE: '{"tmp_path": "/tmp/x"}\n'}), [])

    def test_var_tmp_is_not_the_shared_tmp(self) -> None:
        self.assertEqual(self.scan({"notes.md": "boot in /var/tmp/x and /usr/local/tmp/y\n"}), [])

    def test_a_path_after_other_text_still_counts(self) -> None:
        self.assertEqual(len(self.scan({"a.sh": 'cp "x" -C=/tmp/x; (/tmp/y)\n'})), 2)

    def test_a_binary_file_is_skipped(self) -> None:
        self.assertEqual(self.scan({"blob.bin": b"\0/tmp/payload"}), [])

    def test_a_listed_pair_passes_but_another_name_in_the_same_file_fails(self) -> None:
        allowed = [{"file": "a.sh", "tmp_path": "/tmp/ok", "why": "x"}]
        self.assertEqual(self.scan({"a.sh": "/tmp/ok\n"}, allowed), [])
        messages = self.scan({"a.sh": "/tmp/ok\n/tmp/other\n"}, allowed)
        self.assertEqual(len(messages), 1)
        self.assertIn("a.sh:2:", messages[0])
        self.assertIn("/tmp/other", messages[0])

    def test_the_same_name_in_another_file_fails(self) -> None:
        allowed = [{"file": "a.sh", "tmp_path": "/tmp/ok", "why": "x"}]
        self.assertEqual(len(self.scan({"a.sh": "/tmp/ok\n", "b.sh": "/tmp/ok\n"}, allowed)), 1)

    def test_an_entry_that_matches_nothing_fails(self) -> None:
        allowed = [{"file": "a.sh", "tmp_path": "/tmp/gone", "why": "x"}]
        messages = self.scan({"a.sh": "echo hi\n"}, allowed)
        self.assertEqual(len(messages), 1)
        self.assertIn("matches nothing", messages[0])
        self.assertIn("/tmp/gone", messages[0])


if __name__ == "__main__":
    unittest.main()
