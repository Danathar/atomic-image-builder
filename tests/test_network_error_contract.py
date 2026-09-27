"""Every network helper turns every network failure into the error its caller catches.

maintenance_audit.py states the contract twice: "Every urlopen() in this
module passes" NETWORK_TIMEOUT_SECONDS, and NETWORK_ERRORS lists what a
request can raise besides an HTTP status. #350 applied it to the helpers
that existed then; #489 found fetch_bytes() still on the old pattern,
because the #350 regression test times out every urlopen at once and the
release query fails before any asset is downloaded. A per-helper test only
covers the helpers someone remembered to name.

So the helper set here is read out of the source with ``ast``: every
function that calls urlopen() is found, and the table of calls below must
name exactly the ones whose callers rely on a RuntimeError. A helper added
tomorrow fails the join until it is listed, and listing it runs it through
every failure kind NETWORK_ERRORS covers, at every urlopen() it makes.

homebrew_formula.check() is held to the same contract with a ledger of the
exceptions it still lets escape (#496), checked both ways so the fix has to
empty it.
"""

from __future__ import annotations

import ast
import http.client
import ssl
import tempfile
import unittest
import urllib.error
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import homebrew_formula
import maintenance_audit
from homebrew_formula import PLACEHOLDER_SHA, render_formula

ROOT = Path(__file__).resolve().parents[1]
AUDIT_SOURCE = ROOT / "maintenance_audit.py"
REAL_FORMULA = ROOT / "Formula" / "atomic-image-builder.rb"

# Functions that call urlopen() outside any NETWORK_ERRORS handler on
# purpose, with the reason. Each caller must catch for them.
ESCAPES_TO_CALLER = {
    "head_registry_manifest": "its caller treats a 401 as a token challenge, so urllib's errors must reach it",
}

# One instance of each failure a request can raise. Every class in
# NETWORK_ERRORS must be represented, and every instance must be one of them,
# so this list cannot outgrow or fall behind the tuple it samples.
FAILURES = (
    TimeoutError("timed out"),
    ConnectionResetError(104, "Connection reset by peer"),
    urllib.error.URLError("no route to host"),
    ssl.SSLError("record layer failure"),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    http.client.IncompleteRead(b"#!", 40),
    http.client.BadStatusLine("garbage"),
)

# Exceptions homebrew_formula.check() still lets escape, with the issue that
# tracks each. Asserted both ways: a listed one must still escape, and every
# other failure must come back as a finding.
HOMEBREW_CHECK_KNOWN_ESCAPES: dict[type[BaseException], str] = {}


class FakeResponse:
    """A urlopen() result: a context manager with a body and headers."""

    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self, *_size: int) -> bytes:
        body, self._body = self._body, b""
        return body


def unauthorized() -> urllib.error.HTTPError:
    challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:x/y:pull"'
    return urllib.error.HTTPError("https://ghcr.io/v2/x/y/manifests/t", 401, "Unauthorized", {"WWW-Authenticate": challenge}, None)


# The helpers whose callers catch RuntimeError, each with a factory for the
# urlopen() results of a flow that succeeds (fresh each time: a response body
# reads once). A failure is injected at every position in turn, so every read
# the helper makes is reached, not only the first.
HELPER_FLOWS: dict[str, tuple[Callable[[], object], Callable[[], list[object]]]] = {
    "github_api_json": (
        lambda: maintenance_audit.github_api_json("https://api.github.com/x"),
        lambda: [FakeResponse(b"{}")],
    ),
    "fetch_bytes": (
        lambda: maintenance_audit.fetch_bytes("https://github.com/x/releases/download/v1/aib"),
        lambda: [FakeResponse(b"#!")],
    ),
    "fetch_registry_pull_token": (
        lambda: maintenance_audit.fetch_registry_pull_token('Bearer realm="https://ghcr.io/token"', "ghcr.io", "x/y"),
        lambda: [FakeResponse(b'{"token": "t"}')],
    ),
    "resolve_registry_tag_digest": (
        lambda: maintenance_audit.resolve_registry_tag_digest("ghcr.io", "x/y", "t"),
        lambda: [unauthorized(), FakeResponse(b'{"token": "t"}'), FakeResponse(headers={"Docker-Content-Digest": "sha256:" + "0" * 64})],
    ),
}


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    """The bare names an ``except`` clause catches, unpacking tuples and ``*X``."""
    if handler.type is None:
        return set()
    parts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = set()
    for part in parts:
        if isinstance(part, ast.Starred):
            part = part.value
        if isinstance(part, ast.Name):
            names.add(part.id)
    return names


def _calls_with_guard(function: ast.FunctionDef, name: str) -> list[tuple[ast.Call, bool]]:
    """Each call to ``name`` in ``function``, and whether a NETWORK_ERRORS handler covers it."""
    found: list[tuple[ast.Call, bool]] = []

    def visit(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, ast.Try):
            covers = guarded or any("NETWORK_ERRORS" in _handler_names(h) for h in node.handlers)
            for child in node.body:
                visit(child, covers)
            for child in [*node.handlers, *node.orelse, *node.finalbody]:
                visit(child, guarded)
            return
        if isinstance(node, ast.Call) and _call_name(node) == name:
            found.append((node, guarded))
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    for statement in function.body:
        visit(statement, False)
    return found


class NetworkHelperSetTests(unittest.TestCase):
    """The helper set is derived from the source, so a new one cannot go unlisted."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.functions = _functions(ast.parse(AUDIT_SOURCE.read_text()))
        cls.urlopen_callers = {
            name: _calls_with_guard(function, "urlopen")
            for name, function in cls.functions.items()
            if _calls_with_guard(function, "urlopen")
        }

    def test_the_source_still_has_network_helpers_to_check(self) -> None:
        # Guards the derivation itself: an ast walk that matched nothing would
        # pass every test below vacuously.
        self.assertGreaterEqual(len(self.urlopen_callers), 3)

    def test_every_urlopen_passes_the_module_timeout(self) -> None:
        for name, calls in self.urlopen_callers.items():
            for call, _ in calls:
                with self.subTest(function=name, line=call.lineno):
                    timeout = next((kw.value for kw in call.keywords if kw.arg == "timeout"), None)
                    self.assertIsInstance(timeout, ast.Name, "urlopen() without a timeout= name")
                    self.assertEqual(timeout.id, "NETWORK_TIMEOUT_SECONDS")

    def test_every_user_agent_is_the_module_one(self) -> None:
        for node in ast.walk(ast.parse(AUDIT_SOURCE.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "User-Agent":
                    with self.subTest(line=key.lineno):
                        if isinstance(value, ast.Name):
                            self.assertEqual(value.id, "USER_AGENT")
                        else:
                            self.assertIsInstance(value, ast.Constant)
                            self.assertEqual(value.value, maintenance_audit.USER_AGENT)

    def test_escaping_helpers_are_listed_and_really_escape(self) -> None:
        unguarded = {name for name, calls in self.urlopen_callers.items() if not all(g for _, g in calls)}
        self.assertEqual(unguarded, set(ESCAPES_TO_CALLER))

    def test_every_call_to_an_escaping_helper_is_guarded(self) -> None:
        for helper in ESCAPES_TO_CALLER:
            sites = [
                (caller, call, guarded)
                for caller, function in self.functions.items()
                for call, guarded in _calls_with_guard(function, helper)
            ]
            self.assertTrue(sites, f"{helper} is called by nothing")
            for caller, call, guarded in sites:
                with self.subTest(helper=helper, caller=caller, line=call.lineno):
                    self.assertTrue(guarded, f"{caller} calls {helper} outside a NETWORK_ERRORS handler")

    def test_the_flow_table_names_exactly_the_helpers_callers_rely_on(self) -> None:
        # A guarded urlopen() caller, plus every caller of an escaping helper:
        # the functions that promise a RuntimeError.
        expected = {name for name in self.urlopen_callers if name not in ESCAPES_TO_CALLER}
        for helper in ESCAPES_TO_CALLER:
            expected |= {caller for caller, function in self.functions.items() if _calls_with_guard(function, helper)}
        self.assertEqual(set(HELPER_FLOWS), expected)


class FailureSampleTests(unittest.TestCase):
    def test_every_sample_is_a_network_error(self) -> None:
        for failure in FAILURES:
            with self.subTest(failure=type(failure).__name__):
                self.assertIsInstance(failure, maintenance_audit.NETWORK_ERRORS)

    def test_every_network_error_class_is_sampled(self) -> None:
        for kind in maintenance_audit.NETWORK_ERRORS:
            with self.subTest(kind=kind.__name__):
                self.assertTrue(any(isinstance(failure, kind) for failure in FAILURES))

    def test_the_samples_reach_past_oserror(self) -> None:
        # The #350 shape: IncompleteRead is not an OSError. Without a sample
        # like it, a helper catching OSError alone would pass every flow.
        self.assertTrue(any(not isinstance(failure, OSError) for failure in FAILURES))


class HelperFlowTests(unittest.TestCase):
    def test_every_flow_succeeds_when_nothing_fails(self) -> None:
        # Otherwise an injected failure could be masked by an earlier,
        # unrelated RuntimeError and the flows below would prove nothing.
        for name, (call, responses) in HELPER_FLOWS.items():
            with self.subTest(helper=name):
                with patch("urllib.request.urlopen", side_effect=responses()) as opened:
                    call()
                self.assertEqual(opened.call_count, len(responses()))

    def test_every_failure_at_every_read_is_a_runtime_error(self) -> None:
        for name, (call, responses) in HELPER_FLOWS.items():
            for position in range(len(responses())):
                for failure in FAILURES:
                    with self.subTest(helper=name, read=position, failure=type(failure).__name__):
                        side_effect = [*responses()[:position], failure]
                        with patch("urllib.request.urlopen", side_effect=side_effect) as opened:
                            with self.assertRaises(RuntimeError) as raised:
                                call()
                        self.assertEqual(opened.call_count, position + 1)
                        self.assertIs(raised.exception.__cause__, failure)

    def test_every_request_carries_the_module_user_agent(self) -> None:
        for name, (call, responses) in HELPER_FLOWS.items():
            with self.subTest(helper=name):
                with patch("urllib.request.urlopen", side_effect=responses()) as opened:
                    call()
                for request_call in opened.call_args_list:
                    request = request_call.args[0]
                    self.assertEqual(request.get_header("User-agent"), maintenance_audit.USER_AGENT)


class HomebrewCheckTests(unittest.TestCase):
    """check() turns a failed download into a finding, never a traceback (#496)."""

    def _check_with(self, failure: BaseException) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "atomic-image-builder.rb"
            text = render_formula(REAL_FORMULA.read_text(), url=homebrew_formula.tarball_url("v0.9.0"), sha256="a" * 64)
            path.write_text(text)
            with patch("homebrew_formula.urllib.request.urlopen", side_effect=failure):
                return homebrew_formula.check(path, expected_version="0.9.0")

    def test_the_ledger_holds_only_sampled_kinds(self) -> None:
        for kind in HOMEBREW_CHECK_KNOWN_ESCAPES:
            with self.subTest(kind=kind.__name__):
                self.assertTrue(any(type(failure) is kind for failure in FAILURES))

    def test_every_failure_off_the_ledger_is_a_finding(self) -> None:
        for failure in FAILURES:
            if type(failure) in HOMEBREW_CHECK_KNOWN_ESCAPES:
                continue
            with self.subTest(failure=type(failure).__name__):
                findings = self._check_with(failure)
                self.assertEqual(len(findings), 1)
                self.assertIn("Unable to fetch", findings[0])

    def test_every_ledger_entry_still_escapes(self) -> None:
        # When check() is fixed, this goes red: delete the entry with the fix.
        for kind, issue in HOMEBREW_CHECK_KNOWN_ESCAPES.items():
            failure = next(f for f in FAILURES if type(f) is kind)
            with self.subTest(kind=kind.__name__, issue=issue):
                with self.assertRaises(kind):
                    self._check_with(failure)

    def test_the_placeholder_is_not_what_the_fixture_checks(self) -> None:
        # A placeholder pin returns before any download, which would make
        # every case above pass without reaching the network path.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "atomic-image-builder.rb"
            path.write_text(render_formula(REAL_FORMULA.read_text(), url=homebrew_formula.tarball_url("v0.9.0"), sha256="a" * 64))
            self.assertNotEqual(homebrew_formula.parse_formula(path.read_text())[1], PLACEHOLDER_SHA)
            with patch("homebrew_formula.urllib.request.urlopen", side_effect=TimeoutError("timed out")) as opened:
                homebrew_formula.check(path, expected_version="0.9.0")
            self.assertEqual(opened.call_count, 1)


if __name__ == "__main__":
    unittest.main()
