"""Wallet ownership is required at both VNC listeners, including loopback."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import gate_server


WALLET = "0x" + "a" * 40
OTHER_WALLET = "0x" + "b" * 40
ROOT = Path(__file__).resolve().parents[2]


class VncWalletAuthTests(unittest.TestCase):
    def setUp(self):
        # Compile the actual upgrade method in its normal class context so its
        # super() call executes; the transport superclass records upgrades.
        tree = ast.parse((ROOT / "axonos_gate/websockify_gate.py").read_text())
        handler_class = next(node for node in tree.body
                             if isinstance(node, ast.ClassDef)
                             and node.name == "AxonOSProxyRequestHandler")
        upgrade = next(node for node in handler_class.body
                       if isinstance(node, ast.FunctionDef)
                       and node.name == "handle_upgrade")

        class Transport:
            def handle_upgrade(self):
                self.upgrades += 1
                return "upgraded"

        self.valid = MagicMock(side_effect=lambda token, wallet: (
            token == "victim-proof" and wallet.lower() == WALLET
        ))
        self.balance = MagicMock(return_value={"verified": True})
        self.owner = MagicMock(return_value=True)
        self.namespace = {
            "Transport": Transport,
            "urlsplit": urlsplit,
            "_terminal_gateway": None,
            "logger": MagicMock(),
            "_extract_wallet_from_path_and_headers": lambda path, headers: (
                parse_qs(urlsplit(path).query).get("wallet", [None])[0]
            ),
            "validate_wallet_address": gate_server.validate_wallet_address,
            "_extract_auth_token_from_path_and_headers": lambda path, headers: (
                headers.get("X-AXGT-Auth-Token")
            ),
            "_is_auth_token_valid": self.valid,
            "get_wallet_access_status": self.balance,
            "_session_mgr_available": True,
            "is_session_owner": self.owner,
            "mask_wallet_address": lambda wallet: "masked",
        }
        cls = ast.ClassDef(name="TestHandler", bases=[ast.Name(id="Transport", ctx=ast.Load())],
                           keywords=[], body=[upgrade], decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                     "<actual noVNC upgrade>", "exec"), self.namespace)

    def handler(self, token=None, wallet=WALLET, peer="127.0.0.1"):
        handler = self.namespace["TestHandler"]()
        handler.path = "/websockify?wallet=" + wallet
        handler.headers = {"X-AXGT-Auth-Token": token} if token else {}
        handler.client_address = (peer, 10000)
        handler.upgrades = 0
        handler._observe_request_gpc_early = MagicMock()
        handler.send_error = MagicMock()
        return handler

    def test_local_and_public_address_only_upgrades_are_rejected(self):
        for peer in ("127.0.0.1", "192.0.2.20"):
            with self.subTest(peer=peer):
                handler = self.handler(peer=peer)
                self.assertIsNone(handler.handle_upgrade())
                self.assertEqual(handler.upgrades, 0)
                handler.send_error.assert_called_once_with(403, "AXGT auth token required")
        self.valid.assert_not_called()
        self.balance.assert_not_called()
        self.owner.assert_not_called()

    def test_local_other_wallet_or_invalid_bearer_is_rejected(self):
        for token, wallet in (("attacker-proof", WALLET), ("victim-proof", OTHER_WALLET)):
            with self.subTest(wallet=wallet):
                handler = self.handler(token=token, wallet=wallet)
                self.assertIsNone(handler.handle_upgrade())
                self.assertEqual(handler.upgrades, 0)
                handler.send_error.assert_called_once_with(403, "Invalid or expired AXGT auth token")
        self.balance.assert_not_called()
        self.owner.assert_not_called()

    def test_valid_authenticated_local_and_public_reconnects_succeed(self):
        for peer in ("127.0.0.1", "192.0.2.20"):
            for attempt in range(2):
                with self.subTest(peer=peer, attempt=attempt):
                    handler = self.handler(token="victim-proof", peer=peer)
                    self.assertEqual(handler.handle_upgrade(), "upgraded")
                    self.assertEqual(handler.upgrades, 1)
                    handler.send_error.assert_not_called()

    def test_flask_forwards_validated_bearer_for_loopback_reverification(self):
        ws = MagicMock()
        forwarded = []

        def connect(url, **kwargs):
            handler = self.handler(token=kwargs.get("header", {}).get("X-AXGT-Auth-Token"))
            self.assertNotIn("victim-proof", url)
            self.assertEqual(handler.handle_upgrade(), "upgraded")
            forwarded.append(handler)
            return MagicMock()

        gevent = SimpleNamespace(spawn=MagicMock())
        websocket = SimpleNamespace(create_connection=MagicMock(side_effect=connect))
        for credential in ({"QUERY_STRING": f"wallet={WALLET}&auth_token=victim-proof"},
                           {"QUERY_STRING": f"wallet={WALLET}",
                            "HTTP_COOKIE": "axgt_auth_token=victim-proof"}):
            with self.subTest(cookie="HTTP_COOKIE" in credential), patch.dict(
                "sys.modules", {"gevent": gevent, "websocket": websocket}
            ), patch.object(gate_server, "_is_gate_auth_token_valid", self.valid), patch.object(
                gate_server, "get_wallet_access_status", self.balance
            ), patch.object(gate_server, "_session_mgr_available", True), patch.object(
                gate_server, "is_session_owner", self.owner
            ):
                env = dict(credential, **{"wsgi.websocket": ws})
                self.assertEqual(gate_server._handle_websockify_proxy(env, MagicMock()), [])
        self.assertEqual(len(forwarded), 2)
        ws.close.assert_not_called()

    def test_flask_rejects_missing_or_other_wallet_bearer_before_proxying(self):
        for token in ("", "attacker-proof"):
            ws = MagicMock()
            websocket = SimpleNamespace(create_connection=MagicMock())
            with self.subTest(token=token), patch.dict("sys.modules", {"websocket": websocket}), patch.object(
                gate_server, "_is_gate_auth_token_valid", self.valid
            ):
                env = {"wsgi.websocket": ws,
                       "QUERY_STRING": f"wallet={WALLET}&auth_token={token}"}
                self.assertEqual(gate_server._handle_websockify_proxy(env, MagicMock()), [])
            websocket.create_connection.assert_not_called()
            ws.close.assert_called_once()
