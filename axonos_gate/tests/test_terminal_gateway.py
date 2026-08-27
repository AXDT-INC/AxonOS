import ast
import io
import json
import inspect
import errno
import os
import struct
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_TESTS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _TESTS_DIR.parent
_REPO_ROOT = _PKG_DIR.parent
for _path in (str(_PKG_DIR), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from axonos_gate import terminal_gateway
from axonos_gate.security_utils import redact_terminal_websocket_query

try:
    import gate_server
except Exception:  # pragma: no cover - optional Flask/runtime dependencies
    gate_server = None


WALLET = "0x1234567890123456789012345678901234567890"


def _active_dependencies(session_id=45, *, credit=True, ssh=True):
    now = time.time()
    session = {
        "id": session_id,
        "wallet_address": WALLET,
        "allocation_status": "allocated",
        "container_id": f"container-{session_id}",
        "expires_at": now + 600,
        "hard_expires_at": now + 600,
        "files_key": "private-session-key",
        "ssh_enabled": ssh,
    }
    status = {
        "active": True,
        "is_owner": True,
        "owner_session_id": session_id,
        "owner_allocation_status": "allocated",
        "owner_ssh_enabled": ssh,
    }
    access = {"verified": credit, "remaining_minutes": 10 if credit else 0}
    return (
        lambda _wallet, consume_usage=False: dict(access),
        lambda _wallet: dict(session),
        lambda _wallet: dict(status),
    )


class ExactOriginTests(unittest.TestCase):
    def test_exact_scheme_host_and_port_are_required(self):
        exact = terminal_gateway.exact_request_origin
        self.assertEqual(
            exact("https://app.axonos.io", "app.axonos.io", "https", "http"),
            "https://app.axonos.io",
        )
        self.assertEqual(
            exact("http://localhost:8889", "localhost:8889", None, "http"),
            "http://localhost:8889",
        )
        self.assertIsNone(
            exact("https://app.axonos.io.evil", "app.axonos.io", "https", "http")
        )
        self.assertIsNone(
            exact("https://app.axonos.io:444", "app.axonos.io", "https", "http")
        )
        self.assertIsNone(
            exact("http://app.axonos.io", "app.axonos.io", "https", "http")
        )
        self.assertIsNone(exact(None, "app.axonos.io", "https", "http"))

    def test_explicit_public_origin_tolerates_tls_proxy_host_rewrite(self):
        exact = terminal_gateway.exact_request_origin
        public_origin = "https://app.axonos.io"
        self.assertEqual(
            exact(
                public_origin,
                "app.axonos.io",
                None,
                "http",
                public_origin,
            ),
            public_origin,
        )
        # A TLS proxy may rewrite Host to its private upstream. The exact,
        # configured browser Origin is authoritative in this deployment.
        self.assertEqual(
            exact(public_origin, "internal:6080", None, "http", public_origin),
            public_origin,
        )
        self.assertEqual(
            exact(
                public_origin,
                "internal:6080",
                "http, https",
                "http",
                public_origin,
            ),
            public_origin,
        )
        # The configured browser Origin, not a proxy's internal-hop scheme, is
        # authoritative in this branch.
        self.assertEqual(
            exact(public_origin, "app.axonos.io", "http", "http", public_origin),
            public_origin,
        )

    def test_explicit_public_origin_does_not_broaden_mismatched_origins(self):
        exact = terminal_gateway.exact_request_origin
        public_origin = "https://app.axonos.io"
        self.assertIsNone(
            exact(
                "https://other.example",
                "internal:6080",
                None,
                "http",
                public_origin,
            )
        )
        self.assertIsNone(
            exact(
                "https://app.axonos.io.evil",
                "internal:6080",
                "https",
                "http",
                public_origin,
            )
        )
        self.assertIsNone(
            exact(public_origin, "not/a/host", "https", "http", public_origin)
        )
        self.assertIsNone(
            exact(public_origin, "bad host", "https", "http", public_origin)
        )
        self.assertIsNone(
            exact(
                public_origin,
                "internal:6080, app.axonos.io",
                "http",
                "http",
                public_origin,
            )
        )
        self.assertIsNone(
            exact(public_origin, "internal,proxy", "http", "http", public_origin)
        )
        self.assertIsNone(
            exact(public_origin, "", "https", "http", public_origin)
        )
        # Configuration is an exact Origin allowlist; matching a forged Host
        # cannot admit another browser Origin.
        self.assertIsNone(
            exact(
                "https://evil.example",
                "evil.example",
                "https",
                "http",
                public_origin,
            )
        )

    def test_public_origin_comes_from_valid_public_base_url(self):
        with patch.dict(
            os.environ,
            {"AXGT_PUBLIC_BASE_URL": "https://APP.AXONOS.IO/"},
        ):
            self.assertEqual(
                terminal_gateway.configured_public_origin(),
                "https://app.axonos.io",
            )
        with patch.dict(
            os.environ,
            {"AXGT_PUBLIC_BASE_URL": "https://app.axonos.io/not-an-origin"},
        ):
            self.assertIsNone(terminal_gateway.configured_public_origin())

    def test_ipv6_authority_is_parsed_without_suffix_matching(self):
        exact = terminal_gateway.exact_request_origin
        self.assertEqual(
            exact("https://[2001:db8::1]:8443", "[2001:db8::1]:8443", "https"),
            "https://[2001:db8::1]:8443",
        )
        self.assertIsNone(
            exact("https://[2001:db8::1]:8443", "[2001:db8::2]:8443", "https")
        )


class TicketStoreTests(unittest.TestCase):
    def test_ticket_is_single_use_expiring_and_origin_bound(self):
        now = [100.0]
        store = terminal_gateway.OneUseTicketStore(clock=lambda: now[0])
        ticket = store.issue(WALLET, 45, "https://app.axonos.io", 30)
        self.assertNotIn(ticket, store._records)
        record = store.consume(ticket, "https://app.axonos.io")
        self.assertEqual(record.session_id, 45)
        with self.assertRaises(terminal_gateway.TerminalGatewayError):
            store.consume(ticket, "https://app.axonos.io")

        wrong_origin = store.issue(WALLET, 45, "https://app.axonos.io", 30)
        with self.assertRaises(terminal_gateway.TerminalGatewayError):
            store.consume(wrong_origin, "https://evil.example")
        # A wrong-origin presentation burns the capability too.
        with self.assertRaises(terminal_gateway.TerminalGatewayError):
            store.consume(wrong_origin, "https://app.axonos.io")

        expired = store.issue(WALLET, 45, "https://app.axonos.io", 5)
        now[0] += 6
        with self.assertRaises(terminal_gateway.TerminalGatewayError):
            store.consume(expired, "https://app.axonos.io")

    def test_production_store_hashes_and_atomically_consumes_tickets(self):
        source = (_PKG_DIR / "terminal_gateway.py").read_text(encoding="utf-8")
        self.assertIn('ticket_hash TEXT PRIMARY KEY', source)
        self.assertIn('DELETE FROM {self.TABLE} WHERE ticket_hash = %s', source)
        self.assertIn('RETURNING wallet_address, session_id, origin, expires_at', source)
        self.assertNotIn('ticket TEXT PRIMARY KEY', source)


class SessionBindingTests(unittest.TestCase):
    def test_resolves_only_server_derived_active_allocated_funded_ssh_target(self):
        with patch.object(
            terminal_gateway, "_import_dependencies", return_value=_active_dependencies()
        ):
            context = terminal_gateway.resolve_active_terminal_session(WALLET)
        self.assertEqual(context.session_id, 45)
        self.assertEqual(context.target_host, "axgt-session-45")
        self.assertEqual(context.target_port, 8791)
        self.assertEqual(context.agent_secret, "private-session-key")

    def test_credit_grace_shape_is_rejected(self):
        access, get_session, _ = _active_dependencies()
        grace_status = {
            "active": False,
            "is_owner": True,
            "credit_grace": True,
            "credit_grace_session_id": 45,
            "credit_grace_ssh_enabled": True,
        }
        with patch.object(
            terminal_gateway,
            "_import_dependencies",
            return_value=(access, get_session, lambda _wallet: grace_status),
        ), self.assertRaisesRegex(
            terminal_gateway.TerminalGatewayError, "No active SSH session"
        ):
            terminal_gateway.resolve_active_terminal_session(WALLET)

    def test_desktop_unallocated_expired_and_zero_credit_sessions_are_rejected(self):
        variants = []
        variants.append(_active_dependencies(ssh=False))
        access, get_session, get_status = _active_dependencies()
        variants.append(
            (
                access,
                lambda wallet: {**get_session(wallet), "allocation_status": "pending"},
                get_status,
            )
        )
        variants.append(_active_dependencies(credit=False))
        access, get_session, get_status = _active_dependencies()
        variants.append(
            (
                access,
                lambda wallet: {**get_session(wallet), "expires_at": time.time() - 1},
                get_status,
            )
        )
        for dependencies in variants:
            with self.subTest(dependencies=dependencies), patch.object(
                terminal_gateway, "_import_dependencies", return_value=dependencies
            ), self.assertRaises(terminal_gateway.TerminalGatewayError):
                terminal_gateway.resolve_active_terminal_session(WALLET)

    def test_ticket_is_consumed_before_exact_session_revalidation(self):
        store = terminal_gateway.OneUseTicketStore()
        with patch.object(terminal_gateway, "_ticket_store", store), patch.object(
            terminal_gateway, "_import_dependencies", return_value=_active_dependencies()
        ):
            issued = terminal_gateway.issue_terminal_ticket(
                WALLET, "https://app.axonos.io"
            )
            # The session changed after issue but before upgrade.
            with patch.object(
                terminal_gateway,
                "_import_dependencies",
                return_value=_active_dependencies(session_id=46),
            ), self.assertRaises(terminal_gateway.TerminalGatewayError):
                terminal_gateway.consume_terminal_ticket(
                    issued["ticket"], "https://app.axonos.io"
                )
            # Even a failed exact-session revalidation cannot replay the ticket.
            with self.assertRaises(terminal_gateway.TerminalGatewayError):
                terminal_gateway.consume_terminal_ticket(
                    issued["ticket"], "https://app.axonos.io"
                )


class FramingTests(unittest.TestCase):
    def test_client_frame_types_and_size_bounds(self):
        self.assertEqual(
            terminal_gateway.validate_client_frame(
                terminal_gateway.encode_frame("I", b"ls\n")
            ),
            terminal_gateway.encode_frame("I", b"ls\n"),
        )
        resize = terminal_gateway.encode_frame(
            "R", json.dumps({"cols": 120, "rows": 40}).encode()
        )
        self.assertEqual(terminal_gateway.validate_client_frame(resize), resize)
        for bad in (
            b"short",
            struct.pack("!cI", b"I", 2) + b"x",
            terminal_gateway.encode_frame("P", b"x"),
            terminal_gateway.encode_frame("R", b'{"cols":1,"rows":40}'),
            terminal_gateway.encode_frame("X", b"{}"),
        ):
            with self.subTest(frame=bad), self.assertRaises(
                terminal_gateway.TerminalGatewayError
            ):
                terminal_gateway.validate_client_frame(bad)

    def test_agent_tcp_chunks_are_reassembled_into_individual_frames(self):
        first = terminal_gateway.encode_frame("O", b"hello")
        second = terminal_gateway.encode_frame("X", b'{"code":0}')
        buffer = bytearray(first[:3])
        self.assertEqual(list(terminal_gateway.extract_agent_frames(buffer)), [])
        buffer.extend(first[3:] + second)
        self.assertEqual(
            list(terminal_gateway.extract_agent_frames(buffer)), [first, second]
        )
        self.assertEqual(buffer, bytearray())

    def test_handshake_contains_private_secret_but_public_ticket_does_not(self):
        context = terminal_gateway.TerminalContext(
            WALLET, 45, "https://app.axonos.io", "axgt-session-45", 8791, "secret"
        )
        handshake = json.loads(terminal_gateway.agent_handshake_payload(context))
        self.assertEqual(handshake["secret"], "secret")
        self.assertEqual(handshake["session_id"], "45")
        terminal_gateway.validate_agent_handshake_response(
            b'{"ok":true,"version":1}\n'
        )


class AgentConnectRetryTests(unittest.TestCase):
    def _context(self):
        return terminal_gateway.TerminalContext(
            WALLET,
            45,
            "https://app.axonos.io",
            "axgt-session-45",
            terminal_gateway.TERMINAL_AGENT_PORT,
            "secret",
        )

    def test_connection_refused_is_retried_within_one_deadline(self):
        now = [10.0]
        attempts = []
        sleeps = []
        connected = object()

        def create_connection(address, timeout):
            attempts.append((address, timeout))
            if len(attempts) < 3:
                raise ConnectionRefusedError(errno.ECONNREFUSED, "not ready")
            return connected

        def sleep(delay):
            sleeps.append(delay)
            now[0] += delay

        with patch.object(terminal_gateway, "connect_timeout_seconds", return_value=5):
            result = terminal_gateway.connect_terminal_agent(
                self._context(),
                create_connection,
                sleep=sleep,
                monotonic=lambda: now[0],
            )
        self.assertIs(result, connected)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [0.2, 0.2])
        self.assertTrue(all(0 < attempt[1] <= 1 for attempt in attempts))

    def test_repeated_refusal_stops_at_total_deadline(self):
        now = [0.0]
        timeouts = []

        def create_connection(_address, timeout):
            timeouts.append(timeout)
            raise ConnectionRefusedError(errno.ECONNREFUSED, "not ready")

        def sleep(delay):
            now[0] += delay

        with patch.object(terminal_gateway, "connect_timeout_seconds", return_value=1), \
             self.assertRaises(TimeoutError):
            terminal_gateway.connect_terminal_agent(
                self._context(),
                create_connection,
                sleep=sleep,
                monotonic=lambda: now[0],
            )
        self.assertEqual(now[0], 1.0)
        self.assertGreater(len(timeouts), 1)
        self.assertTrue(all(timeout <= 1.0 for timeout in timeouts))

    def test_non_startup_network_errors_are_not_retried(self):
        attempts = []
        sleeps = []

        def create_connection(_address, timeout):
            self.assertGreater(timeout, 0)
            attempts.append(True)
            raise OSError(errno.EHOSTUNREACH, "no route")

        with self.assertRaises(OSError):
            terminal_gateway.connect_terminal_agent(
                self._context(),
                create_connection,
                sleep=sleeps.append,
            )
        self.assertEqual(len(attempts), 1)
        self.assertEqual(sleeps, [])

    def test_handshake_rejection_is_outside_connect_retry_loop(self):
        helper_source = inspect.getsource(terminal_gateway.connect_terminal_agent)
        self.assertNotIn("handshake", helper_source.split('"""', 2)[-1])
        for path in (_PKG_DIR / "gate_server.py", _PKG_DIR / "websockify_gate.py"):
            source = path.read_text(encoding="utf-8")
            self.assertIn("connect_terminal_agent(", source)
            self.assertLess(
                source.index("connect_terminal_agent("),
                source.index("agent_handshake_payload(context)"),
            )
        proxy_source = (_PKG_DIR / "websockify_gate.py").read_text(encoding="utf-8")
        self.assertNotIn("WebSockifyServer.socket(", proxy_source)


@unittest.skipUnless(gate_server is not None, "Flask / gate_server not importable")
class TicketHttpTests(unittest.TestCase):
    def setUp(self):
        gate_server.app.testing = True
        self.client = gate_server.app.test_client()

    def test_ticket_requires_exact_origin_before_authentication(self):
        with patch.object(gate_server, "_session_mgr_available", True), patch.object(
            gate_server, "_require_auth_token"
        ) as require_auth:
            response = self.client.post(
                "/api/terminal/ticket",
                json={"wallet_address": WALLET},
                headers={"Origin": "http://localhost.evil"},
            )
        self.assertEqual(response.status_code, 403)
        require_auth.assert_not_called()

    def test_configured_public_origin_rejects_hostile_proxy_origin(self):
        with patch.dict(
            os.environ,
            {"AXGT_PUBLIC_BASE_URL": "https://app.axonos.io"},
        ), patch.object(
            gate_server, "_session_mgr_available", True
        ), patch.object(
            gate_server, "_require_auth_token"
        ) as require_auth:
            response = self.client.post(
                "/api/terminal/ticket",
                json={"wallet_address": WALLET},
                headers={
                    "Host": "internal-gate:6080",
                    "Origin": "https://evil.example",
                    "X-Forwarded-Proto": "https",
                },
            )
        self.assertEqual(response.status_code, 403)
        require_auth.assert_not_called()

    def test_authenticated_ticket_response_is_no_store(self):
        payload = {
            "ok": True,
            "ticket": "one-use",
            "expires_in_seconds": 30,
            "websocket_path": "/api/terminal/ws?ticket=one-use",
        }
        with patch.object(gate_server, "_session_mgr_available", True), patch.object(
            gate_server, "validate_wallet_address", return_value=True
        ), patch.object(
            gate_server, "_require_auth_token", return_value=None
        ), patch.object(
            gate_server._terminal_gateway,
            "issue_terminal_ticket",
            return_value=payload,
        ) as issue:
            response = self.client.post(
                "/api/terminal/ticket",
                json={"wallet_address": WALLET},
                headers={"Origin": "http://localhost"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), payload)
        self.assertIn("no-store", response.headers["Cache-Control"])
        issue.assert_called_once_with(WALLET, "http://localhost")

    def test_ticket_uses_public_origin_when_tls_proxy_rewrites_host(self):
        payload = {
            "ok": True,
            "ticket": "one-use",
            "expires_in_seconds": 30,
            "websocket_path": "/api/terminal/ws?ticket=one-use",
        }
        with patch.dict(
            os.environ,
            {"AXGT_PUBLIC_BASE_URL": "https://app.axonos.io"},
        ), patch.object(
            gate_server, "_session_mgr_available", True
        ), patch.object(
            gate_server, "validate_wallet_address", return_value=True
        ), patch.object(
            gate_server, "_require_auth_token", return_value=None
        ), patch.object(
            gate_server._terminal_gateway,
            "issue_terminal_ticket",
            return_value=payload,
        ) as issue:
            response = self.client.post(
                "/api/terminal/ticket",
                json={"wallet_address": WALLET},
                headers={
                    "Host": "internal-gate:6080",
                    "Origin": "https://app.axonos.io",
                    "X-Forwarded-Proto": "http, https",
                },
            )
        self.assertEqual(response.status_code, 200)
        issue.assert_called_once_with(WALLET, "https://app.axonos.io")

    def test_ws_environ_accepts_only_ticket_query(self):
        environ = {
            "HTTP_ORIGIN": "https://app.axonos.io",
            "HTTP_HOST": "app.axonos.io",
            "HTTP_X_FORWARDED_PROTO": "https",
            "wsgi.url_scheme": "http",
            "QUERY_STRING": "ticket=one-use",
        }
        expected = object()
        with patch.object(
            gate_server._terminal_gateway,
            "consume_terminal_ticket",
            return_value=expected,
        ) as consume:
            self.assertIs(gate_server._terminal_context_from_environ(environ), expected)
        consume.assert_called_once_with("one-use", "https://app.axonos.io")
        for query in (
            "ticket=one-use&auth_token=broad",
            "ticket=one&ticket=two",
            "wallet=0x123",
            "",
        ):
            with self.subTest(query=query), self.assertRaises(
                gate_server._terminal_gateway.TerminalGatewayError
            ):
                gate_server._terminal_context_from_environ(
                    {**environ, "QUERY_STRING": query}
                )

    def test_ws_uses_public_origin_when_tls_proxy_rewrites_host(self):
        environ = {
            "HTTP_ORIGIN": "https://app.axonos.io",
            "HTTP_HOST": "internal-gate:6080",
            "HTTP_X_FORWARDED_PROTO": "http, https",
            "wsgi.url_scheme": "http",
            "QUERY_STRING": "ticket=one-use",
        }
        expected = object()
        with patch.dict(
            os.environ,
            {"AXGT_PUBLIC_BASE_URL": "https://app.axonos.io"},
        ), patch.object(
            gate_server._terminal_gateway,
            "consume_terminal_ticket",
            return_value=expected,
        ) as consume:
            self.assertIs(
                gate_server._terminal_context_from_environ(environ), expected
            )
        consume.assert_called_once_with("one-use", "https://app.axonos.io")

    def test_ticket_outcome_logs_are_structured_and_redacted(self):
        masked_wallet = "0x1234...7890"
        with patch.object(
            gate_server, "_session_mgr_available", True
        ), patch.object(
            gate_server, "validate_wallet_address", return_value=True
        ), patch.object(
            gate_server, "_rate_limiter", None
        ), patch.object(
            gate_server,
            "_require_auth_token",
            return_value=({"ok": False, "error": "unauthorized"}, 401),
        ), self.assertLogs(gate_server.logger, level="WARNING") as captured:
            response = self.client.post(
                "/api/terminal/ticket",
                json={"wallet_address": WALLET},
                headers={"Origin": "http://localhost"},
            )
        self.assertEqual(response.status_code, 401)
        auth_log = "\n".join(captured.output)
        self.assertIn("terminal_ticket outcome=auth_rejected", auth_log)
        self.assertIn(masked_wallet, auth_log)
        self.assertNotIn(WALLET, auth_log)

        payload = {
            "ok": True,
            "ticket": "must-never-be-logged",
            "expires_in_seconds": 30,
            "websocket_path": "/api/terminal/ws?ticket=must-never-be-logged",
        }
        with patch.object(
            gate_server, "_session_mgr_available", True
        ), patch.object(
            gate_server, "validate_wallet_address", return_value=True
        ), patch.object(
            gate_server, "_rate_limiter", None
        ), patch.object(
            gate_server, "_require_auth_token", return_value=None
        ), patch.object(
            gate_server._terminal_gateway,
            "issue_terminal_ticket",
            return_value=payload,
        ), self.assertLogs(gate_server.logger, level="INFO") as captured:
            response = self.client.post(
                "/api/terminal/ticket",
                json={"wallet_address": WALLET},
                headers={"Origin": "http://localhost"},
            )
        self.assertEqual(response.status_code, 200)
        issued_log = "\n".join(captured.output)
        self.assertIn("terminal_ticket outcome=issued", issued_log)
        self.assertIn(masked_wallet, issued_log)
        self.assertNotIn(WALLET, issued_log)
        self.assertNotIn("must-never-be-logged", issued_log)

        failure = gate_server._terminal_gateway.TerminalGatewayError(
            "private failure detail",
            503,
            "ticket_store_unavailable",
        )
        with patch.object(
            gate_server, "_session_mgr_available", True
        ), patch.object(
            gate_server, "validate_wallet_address", return_value=True
        ), patch.object(
            gate_server, "_rate_limiter", None
        ), patch.object(
            gate_server, "_require_auth_token", return_value=None
        ), patch.object(
            gate_server._terminal_gateway,
            "issue_terminal_ticket",
            side_effect=failure,
        ), self.assertLogs(gate_server.logger, level="WARNING") as captured:
            response = self.client.post(
                "/api/terminal/ticket",
                json={"wallet_address": WALLET},
                headers={"Origin": "http://localhost"},
            )
        self.assertEqual(response.status_code, 503)
        failure_log = "\n".join(captured.output)
        self.assertIn("terminal_ticket outcome=issue_failed", failure_log)
        self.assertIn("code=ticket_store_unavailable", failure_log)
        self.assertNotIn("private failure detail", failure_log)


class WebsockifyTerminalRouteTests(unittest.TestCase):
    @staticmethod
    def _bounded_reader_from_source(proxy_source):
        tree = ast.parse(proxy_source)
        handler = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "AxonOSProxyRequestHandler"
        )
        method = next(
            node
            for node in handler.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_read_bounded_json_body"
        )
        namespace = {"json": json}
        module = ast.fix_missing_locations(
            ast.Module(body=[method], type_ignores=[])
        )
        exec(compile(module, "websockify_gate.py", "exec"), namespace)
        return namespace["_read_bounded_json_body"]

    def test_ticket_body_is_drained_before_any_early_response(self):
        proxy_source = (_PKG_DIR / "websockify_gate.py").read_text(encoding="utf-8")
        terminal_branch = proxy_source.split(
            "if ponly == '/api/terminal/ticket':", 1
        )[1].split("if webrtc_service", 1)[0]
        read_index = terminal_branch.index("self._read_bounded_json_body(")
        self.assertLess(read_index, terminal_branch.index("_terminal_gateway is None"))
        self.assertLess(read_index, terminal_branch.index("_terminal_origin_for_handler"))

        body = json.dumps({"wallet_address": WALLET}).encode("utf-8")
        next_request = b"GET /api/session/status HTTP/1.1\r\n"
        request = SimpleNamespace(
            headers={"Content-Length": str(len(body))},
            rfile=io.BytesIO(body + next_request),
            close_connection=False,
        )
        read_body = self._bounded_reader_from_source(proxy_source)
        payload, error = read_body(request, 16 * 1024)
        self.assertIsNone(error)
        self.assertEqual(payload, {"wallet_address": WALLET})
        self.assertEqual(request.rfile.read(), next_request)
        self.assertFalse(request.close_connection)

    def test_oversized_ticket_body_forces_connection_close(self):
        proxy_source = (_PKG_DIR / "websockify_gate.py").read_text(encoding="utf-8")
        read_body = self._bounded_reader_from_source(proxy_source)
        request = SimpleNamespace(
            headers={"Content-Length": str(16 * 1024 + 1)},
            rfile=io.BytesIO(b"x" * (16 * 1024 + 1)),
            close_connection=False,
        )
        payload, error = read_body(request, 16 * 1024)
        self.assertEqual(payload, {})
        self.assertEqual(error, "too_large")
        self.assertTrue(request.close_connection)

    def test_websockify_ticket_route_logs_safe_outcomes(self):
        proxy_source = (_PKG_DIR / "websockify_gate.py").read_text(encoding="utf-8")
        terminal_branch = proxy_source.split(
            "if ponly == '/api/terminal/ticket':", 1
        )[1].split("if webrtc_service", 1)[0]
        for outcome in (
            "origin_rejected",
            "auth_rejected",
            "issue_failed",
            "issued",
        ):
            self.assertIn(f"terminal_ticket outcome={outcome}", terminal_branch)
        self.assertIn("token_present=%s", terminal_branch)
        self.assertNotIn("ticket=%s", terminal_branch)
        self.assertNotIn("origin=%s", terminal_branch)

    def test_terminal_websocket_query_is_redacted_from_log_forms(self):
        ticket = "one-use_SUPER-SECRET-capability"
        path = f"/api/terminal/ws?ticket={ticket}"
        expected_path = "/api/terminal/ws?[query-redacted]"
        cases = (
            path,
            f"Path: '{path}'",
            f'GET {path} HTTP/1.1',
            f'upgrade failed for https://app.axonos.io{path}',
            f'\"GET {path} HTTP/1.1\" 101 -',
        )
        for value in cases:
            with self.subTest(value=value):
                redacted = redact_terminal_websocket_query(value)
                self.assertIn(expected_path, redacted)
                self.assertNotIn(ticket, redacted)

    def test_query_capabilities_are_redacted_on_every_route(self):
        secret = "guest_SUPER-SECRET-bearer"
        cases = (
            (
                f"/websockify?wallet=0xabc&auth_token={secret}&quality=8",
                "/websockify?wallet=0xabc&auth_token=[redacted]&quality=8",
            ),
            (
                f'GET /?auth_token={secret}&wallet=0xabc HTTP/1.1',
                'GET /?auth_token=[redacted]&wallet=0xabc HTTP/1.1',
            ),
            (
                f"/?AUTH_TOKEN={secret}",
                "/?AUTH_TOKEN=[redacted]",
            ),
            (
                f"/?auth%5Ftoken={secret}",
                "/?auth%5Ftoken=[redacted]",
            ),
            (
                f"/?invite={secret}&utm_source=sales",
                "/?invite=[redacted]&utm_source=sales",
            ),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                redacted = redact_terminal_websocket_query(value)
                self.assertEqual(redacted, expected)
                self.assertNotIn(secret, redacted)

    def test_log_redaction_is_capability_scoped_and_handler_wide(self):
        self.assertEqual(
            redact_terminal_websocket_query("/api/terminal/ticket?mode=issue"),
            "/api/terminal/ticket?mode=issue",
        )
        self.assertEqual(
            redact_terminal_websocket_query("/api/terminal/ws"),
            "/api/terminal/ws",
        )
        self.assertEqual(redact_terminal_websocket_query(101), 101)

        proxy_source = (_PKG_DIR / "websockify_gate.py").read_text(encoding="utf-8")
        handler_source = proxy_source.split(
            "class AxonOSProxyRequestHandler", 1
        )[1].split("def run_server", 1)[0]
        log_method = handler_source.split("def log_message", 1)[1].split(
            "def send_header", 1
        )[0]
        self.assertIn("redact_terminal_websocket_query(format)", log_method)
        self.assertIn("redact_terminal_websocket_query(arg)", log_method)
        self.assertIn("super().log_message(safe_format, *safe_args)", log_method)

    def test_forked_entrypoint_uses_shared_store_and_request_local_target(self):
        gateway_source = (_PKG_DIR / "terminal_gateway.py").read_text(encoding="utf-8")
        proxy_source = (_PKG_DIR / "websockify_gate.py").read_text(encoding="utf-8")
        self.assertIn("PostgresTicketStore", gateway_source)
        self.assertIn("/api/terminal/ticket", proxy_source)
        self.assertIn("_terminal_context_for_handler(self)", proxy_source)
        self.assertIn("def new_websocket_client(self):", proxy_source)
        self.assertIn("context.target_host", gateway_source)
        self.assertIn("context.target_port", gateway_source)
        self.assertIn("connect_terminal_agent(", proxy_source)
        terminal_upgrade = proxy_source.split("def handle_upgrade(self):", 1)[1]
        terminal_branch = terminal_upgrade.split(
            "# Diagnostic: confirms the WebSocket", 1
        )[0]
        self.assertIn("WebSockifyRequestHandler, self", terminal_branch)
        self.assertNotIn("self.server.target_host =", terminal_branch)
        self.assertNotIn("self.server.target_port =", terminal_branch)

    def test_production_websockify_handler_contract_when_package_is_available(self):
        dist_packages = "/usr/lib/python3/dist-packages"
        if dist_packages not in sys.path:
            sys.path.insert(0, dist_packages)
        try:
            from websockify import websocketproxy, websockifyserver
        except ImportError:
            self.skipTest("production websockify package is not installed")

        handler = websocketproxy.ProxyRequestHandler
        mro = handler.__mro__
        self.assertIn(websockifyserver.WebSockifyRequestHandler, mro)
        web_index = mro.index(websockifyserver.WebSockifyRequestHandler)
        next_handler = mro[web_index + 1]
        self.assertTrue(callable(getattr(next_handler, "handle_upgrade", None)))

        # The terminal branch's super(WebSockifyRequestHandler, self) bypasses
        # both target-plugin layers and resolves to this request-local handshake.
        instance = object.__new__(handler)
        bypass = super(
            websockifyserver.WebSockifyRequestHandler, instance
        ).handle_upgrade
        self.assertIs(bypass.__func__, next_handler.handle_upgrade)

        send_parameters = inspect.signature(handler.send_frames).parameters
        self.assertIn("bufs", send_parameters)
        self.assertIsNone(send_parameters["bufs"].default)


if __name__ == "__main__":
    unittest.main()
