"""Ownership regressions for both public x402 session listeners.

Execute Flask's routes and websockify's actual handler/auth functions. Only
external storage, the session launcher, and payment settlement are replaced;
authentication decisions use the production token-verification implementation.
No live server, blockchain payment, or compute session is used.
"""

import ast
import base64
from contextlib import ExitStack, contextmanager
from http.cookies import SimpleCookie
import json
import logging
import os
import secrets
import sqlite3
from pathlib import Path
import struct
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "axonos_gate"))
import gate_server as gate
from session_manager import validate_launch_request_id

WALLET = "0x" + "ab" * 20
OTHER_WALLET = "0x" + "cd" * 20
SSH_KEY = "ssh-ed25519 " + base64.b64encode(
    struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"a" * 32
).decode() + " attacker-key"
BODY = {"wallet_address": WALLET, "ssh_pubkey": SSH_KEY}


class _TokenDatabase:
    """Return actual token rows only when the SQL's identity scope matches."""

    def __init__(self):
        now = time.time()
        self.rows = {
            "owner-current": (WALLET, "current", now + 300, now + 300),
            "owner-grace": (WALLET, "grace", now - 10, now + 300),
            "owner-expired": (WALLET, "current", now - 1, now + 300),
            "owner-expired-grace": (WALLET, "grace", now + 300, now - 1),
            "owner-revoked": (WALLET, "revoked", now + 300, now + 300),
            "attacker-current": (OTHER_WALLET, "current", now + 300, now + 300),
        }

    def connect(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value

        def execute(sql, params):
            assert "SELECT" in sql and "WHERE token = %s" in sql, sql
            row = self.rows.get(params[0])
            if "AND wallet_address = %s" in sql:
                row = row[1:] if row and row[0] == params[1] else None
            cursor.fetchone.return_value = row

        cursor.execute.side_effect = execute
        return connection


class _OwnershipCases:
    """Run the same attack and compatibility cases against each listener."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.database = _TokenDatabase()
        self.sequence = []
        self.claim_result = {
            "granted": True, "remaining_seconds": 3600,
            "session_id": 42, "ssh_host": "ssh.example.test", "ssh_port": 2222,
        }

        def claim(*args, **kwargs):
            self.sequence.append("claim")
            return dict(self.claim_result)

        def issue(wallet, **kwargs):
            self.sequence.append("issue")
            token = "newly-issued-owner-token"
            self.database.rows[token] = (wallet, "current", time.time() + 300, time.time() + 300)
            return token, 300

        self.claim = MagicMock(side_effect=claim)
        self.issue = MagicMock(side_effect=issue)
        self.settle = MagicMock(return_value={
            "verified": True, "credited_minutes": 60,
            "settlement_tx_hash": "0x" + "f" * 64,
        })
        self.prepaid = MagicMock(return_value={"verified": True, "remaining_minutes": 120})
        self.stack.enter_context(patch.dict(os.environ, {
            "AXGT_AGENTLINK_ENABLED": "false", "AXGT_PUBLIC_BASE_URL": "https://app.example.test",
            "AXGT_AUTH_COOKIE_NAME": "axgt_auth_token",
        }))
        self.stack.enter_context(patch.object(gate, "_gate_pg_init_once", return_value=True))
        self.stack.enter_context(patch.object(gate, "_gate_pg_get_connection", side_effect=self.database.connect))
        gate.app.testing = True
        self.client = gate.app.test_client(use_cookies=False)
        self.prepare_listener()

    def test_funded_address_and_attacker_ssh_cannot_claim_or_mint_token(self):
        # Everything here is public or attacker-owned, including a visible tx hash.
        body = dict(BODY, tx_hash="0x" + "f" * 64, auth_token="invented-proof")
        status, result = self.post("/api/x402/session", body)
        self.assertEqual(status, 401)
        self.assertFalse(result.get("granted", False))
        self.assertNotIn("auth_token", result)
        self.claim.assert_not_called()
        self.issue.assert_not_called()
        self.settle.assert_not_called()

    def test_invalid_expired_revoked_or_other_wallet_tokens_cannot_claim(self):
        for token in ("invented-proof", "owner-expired", "owner-expired-grace", "owner-revoked", "attacker-current"):
            with self.subTest(token=token):
                status, result = self.post("/api/x402/session", BODY, {"X-AXGT-Auth-Token": token})
                self.assertEqual(status, 401)
                self.assertFalse(result.get("granted", False))
                self.assertNotIn("auth_token", result)
        self.claim.assert_not_called()
        self.issue.assert_not_called()
        self.settle.assert_not_called()

    def test_auth_database_outage_fails_closed_without_claim_or_token(self):
        self.disable_auth_database()
        status, result = self.post("/api/x402/session", BODY, {"X-AXGT-Auth-Token": "owner-current"})
        self.assertEqual(status, 503)
        self.assertFalse(result["granted"])
        self.assertNotIn("auth_token", result)
        self.claim.assert_not_called()
        self.issue.assert_not_called()

    def test_empty_unpaid_discovery_still_returns_payment_required(self):
        status, result = self.post("/api/x402/session", {})
        self.assertEqual(status, 402)
        self.assertNotIn("auth_token", result)
        self.claim.assert_not_called()
        self.issue.assert_not_called()

    def test_current_and_grace_tokens_authorize_exact_wallet_and_mint_only_after_claim(self):
        for token in ("owner-current", "owner-grace"):
            with self.subTest(token=token):
                self.sequence.clear()
                self.claim.reset_mock()
                status, result = self.post("/api/x402/session", BODY, {"X-AXGT-Auth-Token": token})
                self.assertEqual(status, 200)
                self.assertTrue(result["granted"])
                self.assertEqual(self.sequence, ["claim", "issue"])
                self.assertEqual(self.claim.call_args.args[0], WALLET)
                self.assertEqual(self.claim.call_args.kwargs["ssh_pubkey"], SSH_KEY)
                self.assertTrue(self.claim.call_args.kwargs["requested_ssh"])
        self.settle.assert_not_called()

    def test_mixed_case_wallet_is_matched_to_normalized_token_identity(self):
        status, result = self.post("/api/x402/session", dict(BODY, wallet_address="0x" + "Ab" * 20),
                                   {"X-AXGT-Auth-Token": "owner-current"})
        self.assertEqual(status, 200)
        self.assertTrue(result["granted"])
        self.assertEqual(self.claim.call_args.args[0], WALLET)

    def test_cookie_and_query_ownership_and_stale_header_fallback_survive(self):
        cases = (
            ("/api/x402/session", {"Cookie": "axgt_auth_token=owner-current"}),
            ("/api/x402/session?auth_token=owner-current", {}),
            ("/api/x402/session", {"X-AXGT-Auth-Token": "owner-expired", "Cookie": "axgt_auth_token=owner-current"}),
        )
        for path, headers in cases:
            with self.subTest(path=path, headers=headers):
                status, result = self.post(path, BODY, headers)
                self.assertEqual(status, 200)
                self.assertTrue(result["granted"])

    def test_denied_claim_never_issues_or_returns_token_on_prepaid_or_signed_path(self):
        self.claim_result = {"granted": False, "reason": "No capacity"}
        for headers in ({"X-AXGT-Auth-Token": "owner-current"},
                        {"X-PAYMENT": "signed-payment", "X-AXGT-Auth-Token": "owner-current"}):
            with self.subTest(headers=headers):
                status, result = self.post("/api/x402/session", BODY, headers)
                self.assertEqual(status, 409)
                self.assertFalse(result["granted"])
                self.assertNotIn("auth_token", result)
        self.assertEqual(self.claim.call_count, 2)
        self.issue.assert_not_called()

    def test_signed_x402_payment_with_wallet_proof_preserves_settlement_and_claim(self):
        for payment_header in ("X-PAYMENT", "PAYMENT-SIGNATURE"):
            with self.subTest(header=payment_header):
                self.sequence.clear()
                status, result = self.post("/api/x402/session", BODY, {
                    payment_header: "signed-payment", "X-AXGT-Auth-Token": "owner-current",
                })
                self.assertEqual(status, 200)
                self.assertTrue(result["granted"])
                self.assertTrue(result["payment"]["verified"])
                self.assertEqual(self.sequence, ["claim", "issue"])
                self.settle.assert_called_with(authenticated_wallet=WALLET, x_payment_header="signed-payment",
                                               attribution_context=None)

    def test_public_valid_signed_payment_without_wallet_proof_cannot_settle_or_claim(self):
        # Even a valid payment signature is public/replayable authorization of a
        # payment, not proof that this caller may bind an SSH key to the wallet.
        for header in ("X-PAYMENT", "PAYMENT-SIGNATURE"):
            for token in (None, "attacker-current", "owner-revoked", "owner-expired"):
                headers = {header: "public-valid-payment-authorization"}
                if token:
                    headers["X-AXGT-Auth-Token"] = token
                with self.subTest(header=header, token=token):
                    status, result = self.post("/api/x402/session", BODY, headers)
                    self.assertEqual(status, 401)
                    self.assertFalse(result["granted"])
                    self.assertNotIn("auth_token", result)
        self.settle.assert_not_called()
        self.claim.assert_not_called()
        self.issue.assert_not_called()

    def test_token_revoked_or_expired_during_settlement_cannot_claim_or_mint(self):
        settlement = dict(self.settle.return_value)
        for changed_status, expiry, grace in (
            ("revoked", time.time() + 300, time.time() + 300),
            ("current", time.time() - 1, time.time() + 300),
            ("grace", time.time() + 300, time.time() - 1),
        ):
            with self.subTest(status=changed_status):
                self.database.rows["owner-current"] = (
                    WALLET, "current", time.time() + 300, time.time() + 300,
                )

                def settle_and_invalidate(**kwargs):
                    self.database.rows["owner-current"] = (WALLET, changed_status, expiry, grace)
                    return dict(settlement)

                self.settle.side_effect = settle_and_invalidate
                status, result = self.post("/api/x402/session", BODY, {
                    "X-PAYMENT": "signed-payment", "X-AXGT-Auth-Token": "owner-current",
                })
                self.assertEqual(status, 401)
                self.assertFalse(result["granted"])
                self.assertTrue(result["payment"]["verified"])
                self.assertNotIn("auth_token", result)
        self.assertEqual(self.settle.call_count, 3)
        self.claim.assert_not_called()
        self.issue.assert_not_called()

    def test_invalid_signed_payment_does_not_fall_back_to_wallet_balance(self):
        self.settle.return_value = {"verified": False, "error": "Invalid EIP-3009 signature"}
        status, result = self.post("/api/x402/session", BODY, {
            "X-PAYMENT": "public-replayed-or-invalid-data", "X-AXGT-Auth-Token": "owner-current",
        })
        self.assertEqual(status, 400)
        self.assertFalse(result.get("granted", False))
        self.assertNotIn("auth_token", result)
        self.claim.assert_not_called()
        self.issue.assert_not_called()

    def test_public_pending_payment_cannot_authorize_prepaid_wallet_without_token(self):
        self.settle.return_value = {
            "verified": False, "pending": True, "reason": "awaiting_confirmations",
            "settlement_tx_hash": "0x" + "f" * 64,
        }
        status, result = self.post("/api/x402/session", BODY, {"X-PAYMENT": "public-pending-authorization"})
        self.assertEqual(status, 401)
        self.assertFalse(result["granted"])
        self.assertNotIn("auth_token", result)
        self.settle.assert_not_called()
        self.claim.assert_not_called()
        self.issue.assert_not_called()

    def test_pending_payment_with_independent_wallet_token_can_reclaim_prepaid_credit(self):
        self.settle.return_value = {
            "verified": False, "pending": True, "reason": "awaiting_confirmations",
            "settlement_tx_hash": "0x" + "f" * 64,
        }
        status, result = self.post("/api/x402/session", BODY, {
            "X-PAYMENT": "pending-authorization", "X-AXGT-Auth-Token": "owner-current",
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["granted"])
        self.assertEqual(self.sequence, ["claim", "issue"])

    def test_pending_authenticated_payment_cannot_mint_token_when_claim_is_denied(self):
        self.settle.return_value = {"verified": False, "pending": True}
        self.claim_result = {"granted": False, "reason": "No capacity"}
        status, result = self.post("/api/x402/session", BODY, {
            "X-PAYMENT": "pending-authorization", "X-AXGT-Auth-Token": "owner-current",
        })
        self.assertEqual(status, 409)
        self.assertFalse(result["granted"])
        self.assertNotIn("auth_token", result)
        self.claim.assert_called_once()
        self.issue.assert_not_called()

    def test_existing_authenticated_session_reattachment_is_preserved(self):
        body = {"wallet_address": WALLET, "resume_only": True, "expected_session_id": 42}
        status, result = self.post("/api/session/claim", body, {"X-AXGT-Auth-Token": "owner-current"})
        self.assertEqual(status, 200)
        self.assertTrue(result["granted"])
        self.assertEqual(result["session_id"], 42)
        self.assertTrue(self.claim.call_args.kwargs["resume_only"])
        self.assertEqual(self.claim.call_args.kwargs["expected_session_id"], 42)


class TestFlaskX402Ownership(_OwnershipCases, unittest.TestCase):
    def prepare_listener(self):
        for name, value in (("_session_mgr_available", True), ("get_wallet_access_status", self.prepaid),
                            ("try_claim_session", self.claim), ("_issue_gate_auth_token", self.issue),
                            ("settle_x402_payment", self.settle)):
            self.stack.enter_context(patch.object(gate, name, value))
        self.stack.enter_context(patch.object(gate, "_request_attribution_context", return_value=None))

    def disable_auth_database(self):
        self.stack.enter_context(patch.object(gate, "_gate_pg_init_once", return_value=False))

    def post(self, path, body, headers=None):
        response = self.client.post(path, json=body, headers=headers or {})
        return response.status_code, response.get_json()


class TestWebsockifyX402Ownership(_OwnershipCases, unittest.TestCase):
    def prepare_listener(self):
        tree = ast.parse((ROOT / "axonos_gate/websockify_gate.py").read_text())
        names = {"do_POST", "_is_auth_token_valid", "_auth_cookie_name",
                 "_auth_token_candidates_from_path_and_headers", "_extract_auth_token_from_path_and_headers",
                 "_valid_auth_token_from_path_and_headers", "_x402_402_body", "_x402_v2_headers"}
        functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
        self.namespace = {
            "os": os, "time": time, "json": json, "logger": logging.getLogger(__name__),
            "urlsplit": urlsplit, "parse_qs": parse_qs, "SimpleCookie": SimpleCookie,
            "_AUTH_TABLE": gate._AUTH_TABLE, "_auth_pg_init_once": lambda: True,
            "_auth_pg_get_connection": self.database.connect, "_is_guest_shaped": gate._is_guest_shaped,
            "client_ip_for_rate_limit": lambda *_args: "192.0.2.1", "webrtc_service": None,
            "_session_mgr_available": True, "_request_attribution_context": lambda *_args: None,
            "_guest_rejection": lambda *_args: None, "get_wallet_access_status": self.prepaid,
            "unpaid_session_requires_402": gate.unpaid_session_requires_402,
            "payment_required_body": gate.payment_required_body,
            "payment_required_for_version": gate.payment_required_for_version,
            "resolve_x402_body_version": gate.resolve_x402_body_version, "validate_wallet_address": gate.validate_wallet_address,
            "validate_ssh_public_key": gate.validate_ssh_public_key, "settle_x402_payment": self.settle,
            "verify_usdc_deposit_is_pending": gate.verify_usdc_deposit_is_pending,
            "try_claim_session": self.claim, "_issue_auth_token": self.issue,
            "validate_launch_request_id": validate_launch_request_id,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), "websockify_gate.py", "exec"), self.namespace)

    def test_checksummed_issued_identity_reconnects_rotates_and_validates_on_both_listeners(self):
        # Exercise the actual helpers' SQL against a relational store. SQLite
        # needs only PostgreSQL's placeholder/GREATEST spellings adapted here;
        # wallet equality, status transitions and expiry queries remain real.
        raw_db = sqlite3.connect(":memory:")
        self.addCleanup(raw_db.close)
        raw_db.create_function("GREATEST", -1, max)
        raw_db.execute(f"""CREATE TABLE {gate._AUTH_TABLE} (
            token TEXT PRIMARY KEY, wallet_address TEXT NOT NULL,
            issued_at REAL NOT NULL, expires_at REAL NOT NULL,
            status TEXT NOT NULL, grace_until REAL NOT NULL
        )""")

        class Connection:
            @contextmanager
            def cursor(inner):
                cursor = raw_db.cursor()
                try:
                    yield SimpleNamespace(
                        execute=lambda sql, params: cursor.execute(sql.replace("%s", "?"), params),
                        fetchone=cursor.fetchone,
                    )
                finally:
                    cursor.close()

            commit = staticmethod(raw_db.commit)
            rollback = staticmethod(raw_db.rollback)
            close = staticmethod(lambda: None)

        connection = Connection()
        self.namespace["_auth_pg_get_connection"] = lambda: connection
        self.stack.enter_context(patch.object(gate, "_gate_pg_get_connection", return_value=connection))
        self.namespace.update({"secrets": secrets, "_auth_ttl_seconds": lambda: 300,
                               "_auth_grace_seconds": lambda: 60})
        helper_names = {"_issue_auth_token", "_current_wallet_token_and_remaining",
                        "_auth_token_remaining_seconds", "_rotate_auth_token"}
        tree = ast.parse((ROOT / "axonos_gate/websockify_gate.py").read_text())
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in helper_names]
        exec(compile(ast.Module(body=helpers, type_ignores=[]), "websockify_gate.py", "exec"), self.namespace)

        checksummed_wallet = "0x" + "Ab" * 20
        token, ttl = self.namespace["_issue_auth_token"](" " + checksummed_wallet + " ")
        self.assertEqual(ttl, 300)
        stored_wallet = raw_db.execute(
            f"SELECT wallet_address FROM {gate._AUTH_TABLE} WHERE token = ?", (token,),
        ).fetchone()[0]
        self.assertEqual(stored_wallet, WALLET)
        for wallet in (WALLET, checksummed_wallet):
            with self.subTest(wallet=wallet):
                self.assertTrue(self.namespace["_is_auth_token_valid"](token, wallet))
                self.assertTrue(gate._is_gate_auth_token_valid(token, wallet))
                current, remaining = self.namespace["_current_wallet_token_and_remaining"](wallet)
                self.assertEqual(current, token)
                self.assertGreater(remaining, 0)
                self.assertGreater(self.namespace["_auth_token_remaining_seconds"](token, wallet), 0)

        rotated, _ = self.namespace["_rotate_auth_token"](token, checksummed_wallet)
        self.assertTrue(rotated)
        self.assertNotEqual(rotated, token)
        self.assertEqual(raw_db.execute(
            f"SELECT wallet_address, status FROM {gate._AUTH_TABLE} WHERE token = ?", (rotated,),
        ).fetchone(), (WALLET, "current"))
        self.assertTrue(self.namespace["_is_auth_token_valid"](token, checksummed_wallet))
        self.assertEqual(self.namespace["_current_wallet_token_and_remaining"](checksummed_wallet)[0], rotated)
        self.assertEqual(self.namespace["_rotate_auth_token"](rotated, OTHER_WALLET), (None, 0))

        status, result = self.post("/api/session/claim", {
            "wallet_address": checksummed_wallet, "resume_only": True, "expected_session_id": 42,
        }, {"X-AXGT-Auth-Token": rotated})
        self.assertEqual(status, 200)
        self.assertTrue(result["granted"])
        self.assertEqual(result["session_id"], 42)

        raw_db.execute(f"UPDATE {gate._AUTH_TABLE} SET status = 'revoked' WHERE token = ?", (rotated,))
        raw_db.commit()
        self.assertFalse(self.namespace["_is_auth_token_valid"](rotated, checksummed_wallet))
        self.assertFalse(gate._is_gate_auth_token_valid(rotated, checksummed_wallet))
        self.assertIsNone(self.namespace["_auth_token_remaining_seconds"](rotated, checksummed_wallet))

        # The public refresh/status route must reject a revoked credential
        # before the internal rotation helper can consider issuing a successor.
        status_helpers = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                          and node.name in {"do_GET", "_extract_wallet_from_path_and_headers"}]
        exec(compile(ast.Module(body=status_helpers, type_ignores=[]), "websockify_gate.py", "exec"), self.namespace)
        rotate_spy = MagicMock(wraps=self.namespace["_rotate_auth_token"])
        self.namespace["_rotate_auth_token"] = rotate_spy
        handler = SimpleNamespace(
            path="/api/auth/wallet-status?wallet_address=" + checksummed_wallet,
            headers={"X-AXGT-Auth-Token": rotated},
            _observe_request_gpc_early=lambda *_args: None,
            _send_json=lambda status, payload, **_kwargs: (status, payload),
        )
        status, result = self.namespace["do_GET"](handler)
        self.assertEqual(status, 401)
        self.assertNotIn("auth_token", result)
        rotate_spy.assert_not_called()
        with patch.object(gate, "_rotate_gate_auth_token") as flask_rotate:
            response = self.client.get(handler.path, headers=handler.headers)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("auth_token", response.get_json())
        flask_rotate.assert_not_called()

    def disable_auth_database(self):
        self.namespace["_auth_pg_init_once"] = lambda: False

    def post(self, path, body, headers=None):
        raw = json.dumps(body).encode()
        handler = SimpleNamespace(
            path=path, headers={"Content-Type": "application/json", "Content-Length": str(len(raw)), **(headers or {})},
            client_address=("192.0.2.1", 10000),
            _read_json_body=lambda: body, _observe_request_gpc_early=lambda *_args: None,
            _send_json=lambda status, payload, **_kwargs: (status, payload),
            send_error=lambda status, message: (status, {"error": message}),
        )
        return self.namespace["do_POST"](handler)
