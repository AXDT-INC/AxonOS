"""Static contract guards for compute-scoped WebRTC route adapters and assets."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (_REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _calls(node: ast.AST, terminal_name: str) -> list[ast.Call]:
    found: list[ast.Call] = []
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Call):
            continue
        func = candidate.func
        if isinstance(func, ast.Name) and func.id == terminal_name:
            found.append(candidate)
        elif isinstance(func, ast.Attribute) and func.attr == terminal_name:
            found.append(candidate)
    return found


def _argument_source(call: ast.Call) -> list[str]:
    return [ast.unparse(argument) for argument in call.args]


def _scope_assignment_count(node: ast.AST, helper_name: str) -> int:
    count = 0
    for candidate in ast.walk(node):
        if not isinstance(candidate, (ast.Assign, ast.AnnAssign)):
            continue
        targets = candidate.targets if isinstance(candidate, ast.Assign) else [candidate.target]
        if not any(isinstance(target, ast.Name) and target.id == "scope" for target in targets):
            continue
        value = candidate.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == helper_name
        ):
            count += 1
    return count


class WebRtcRouteScopeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.flask_source = _read("axonos_gate/gate_server.py")
        cls.websockify_source = _read("axonos_gate/websockify_gate.py")
        cls.flask_tree = ast.parse(cls.flask_source)
        cls.websockify_tree = ast.parse(cls.websockify_source)

    def test_flask_browser_routes_forward_active_compute_identity(self) -> None:
        cases = (
            ("api_webrtc_session", "handle_create_session"),
            ("api_webrtc_offer", "handle_post_offer"),
            ("api_webrtc_status", "handle_get_status"),
            ("api_webrtc_ice", "handle_post_client_ice"),
        )
        for route_name, handler_name in cases:
            with self.subTest(route=route_name):
                route = _function(self.flask_tree, route_name)
                self.assertEqual(len(_calls(route, "_active_webrtc_compute_id")), 1)
                handler_calls = _calls(route, handler_name)
                self.assertEqual(len(handler_calls), 1)
                self.assertIn("active_compute_id", _argument_source(handler_calls[0]))

        create_call = _calls(
            _function(self.flask_tree, "api_webrtc_session"),
            "handle_create_session",
        )[0]
        self.assertIn("data.get('compute_session_id')", _argument_source(create_call))

    def test_websockify_browser_routes_forward_active_compute_identity(self) -> None:
        do_post = _function(self.websockify_tree, "do_POST")
        for handler_name in (
            "handle_create_session",
            "handle_post_offer",
            "handle_post_client_ice",
        ):
            with self.subTest(handler=handler_name):
                handler_calls = _calls(do_post, handler_name)
                self.assertEqual(len(handler_calls), 1)
                self.assertIn("active_compute_id", _argument_source(handler_calls[0]))

        self.assertGreaterEqual(len(_calls(do_post, "_active_webrtc_compute_id")), 3)
        do_get = _function(self.websockify_tree, "do_GET")
        self.assertEqual(len(_calls(do_get, "_active_webrtc_compute_id")), 1)
        status_calls = _calls(do_get, "handle_get_status")
        self.assertEqual(len(status_calls), 1)
        self.assertIn("active_compute_id", _argument_source(status_calls[0]))
        create_call = _calls(do_post, "handle_create_session")[0]
        self.assertIn("data.get('compute_session_id')", _argument_source(create_call))

    def _assert_scope_helper_is_validated(
        self,
        tree: ast.AST,
        source: str,
        helper_name: str,
    ) -> None:
        helper = _function(tree, helper_name)
        helper_source = ast.get_source_segment(source, helper) or ""
        self.assertEqual(len(_calls(helper, "resolve_agent_scope")), 1)
        for required in (
            "X-AXGT-Session-ID",
            "X-Wallet-Address",
            "X-AXGT-Session-Key",
            "validate_webrtc_agent_identity",
        ):
            self.assertIn(required, helper_source)

    def test_flask_all_agent_routes_pass_validated_scope(self) -> None:
        self._assert_scope_helper_is_validated(
            self.flask_tree,
            self.flask_source,
            "_webrtc_agent_scope_from_headers",
        )
        cases = (
            ("api_webrtc_agent_next", "handle_agent_next"),
            ("api_webrtc_agent_row", "handle_agent_row"),
            ("api_webrtc_agent_answer", "handle_agent_answer"),
            ("api_webrtc_agent_fail", "handle_agent_fail"),
        )
        for route_name, handler_name in cases:
            with self.subTest(route=route_name):
                route = _function(self.flask_tree, route_name)
                self.assertEqual(
                    _scope_assignment_count(route, "_webrtc_agent_scope_from_headers"),
                    1,
                )
                handler_calls = _calls(route, handler_name)
                self.assertEqual(len(handler_calls), 1)
                self.assertIn("scope", _argument_source(handler_calls[0]))

    def test_websockify_all_agent_routes_pass_validated_scope(self) -> None:
        self._assert_scope_helper_is_validated(
            self.websockify_tree,
            self.websockify_source,
            "_webrtc_agent_scope_from_headers",
        )
        do_get = _function(self.websockify_tree, "do_GET")
        do_post = _function(self.websockify_tree, "do_POST")
        self.assertEqual(
            _scope_assignment_count(do_get, "_webrtc_agent_scope_from_headers"),
            2,
        )
        self.assertEqual(
            _scope_assignment_count(do_post, "_webrtc_agent_scope_from_headers"),
            2,
        )
        for owner, handler_name in (
            (do_get, "handle_agent_next"),
            (do_get, "handle_agent_row"),
            (do_post, "handle_agent_answer"),
            (do_post, "handle_agent_fail"),
        ):
            with self.subTest(handler=handler_name):
                handler_calls = _calls(owner, handler_name)
                self.assertEqual(len(handler_calls), 1)
                self.assertIn("scope", _argument_source(handler_calls[0]))


class WebRtcBrowserScopeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ui_source = _read("novnc-theme/ui.js")
        cls.webrtc_source = _read("novnc-theme/app/webrtc/axonos-webrtc.js")
        cls.page_source = _read("novnc-theme/vnc.html")

    def test_claim_compute_id_is_required_and_passed_to_both_attempts(self) -> None:
        self.assertIn(
            "const computeSessionId = Number(claim && claim.session_id);",
            self.ui_source,
        )
        self.assertIn("Number.isSafeInteger(computeSessionId)", self.ui_source)
        self.assertIn("computeSessionId <= 0", self.ui_source)

        attempts = re.findall(
            r"connectAxonOSWebRTC\(\{(?P<options>.*?)\}\)",
            self.ui_source,
            flags=re.DOTALL,
        )
        self.assertEqual(len(attempts), 2)
        for options in attempts:
            self.assertRegex(options, r"\bcomputeSessionId\b")

    def test_webrtc_module_requires_compute_id(self) -> None:
        self.assertIn(
            "const computeSessionId = Number(opts.computeSessionId);",
            self.webrtc_source,
        )
        self.assertIn("Number.isSafeInteger(computeSessionId)", self.webrtc_source)
        self.assertIn("computeSessionId <= 0", self.webrtc_source)

    def test_session_offer_and_ice_payloads_send_compute_id(self) -> None:
        session_payload = re.search(
            r"_fetchJson\('\./api/webrtc/session'.*?"
            r"body:\s*JSON\.stringify\(\{(?P<body>.*?)\}\)",
            self.webrtc_source,
            flags=re.DOTALL,
        )
        offer_payload = re.search(
            r"_fetchJson\('\./api/webrtc/offer'.*?"
            r"body:\s*JSON\.stringify\(\{(?P<body>.*?)\}\)",
            self.webrtc_source,
            flags=re.DOTALL,
        )
        ice_payload = re.search(
            r"const body\s*=\s*\{(?P<body>.*?)\};\s*"
            r"pendingIce\.push\(\s*_fetchJson\('\./api/webrtc/ice'",
            self.webrtc_source,
            flags=re.DOTALL,
        )
        for route, match in (
            ("session", session_payload),
            ("offer", offer_payload),
            ("ice", ice_payload),
        ):
            with self.subTest(route=route):
                self.assertIsNotNone(match)
                assert match is not None
                self.assertIn("compute_session_id: computeSessionId", match.group("body"))

        self.assertIn(
            "&compute_session_id=${encodeURIComponent(computeSessionId)}",
            self.webrtc_source,
        )

    def test_ui_and_webrtc_module_cache_versions_match(self) -> None:
        ui_version = re.search(r"app/ui\.js\?v=([^\"']+)", self.page_source)
        webrtc_version = re.search(
            r"webrtc/axonos-webrtc\.js\?v=([^\"']+)",
            self.ui_source,
        )
        self.assertIsNotNone(ui_version)
        self.assertIsNotNone(webrtc_version)
        assert ui_version is not None and webrtc_version is not None
        self.assertEqual(ui_version.group(1), webrtc_version.group(1))


if __name__ == "__main__":
    unittest.main()
