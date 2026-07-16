import sys
import unittest
from pathlib import Path
from unittest.mock import patch


_tests_dir = Path(__file__).resolve().parent
_pkg_dir = _tests_dir.parent
_repo_root = _pkg_dir.parent
for _path in (str(_pkg_dir), str(_repo_root)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    import gate_server
except Exception:  # pragma: no cover - optional Flask/runtime dependencies
    gate_server = None


WALLET = "0x1234567890123456789012345678901234567890"


@unittest.skipUnless(gate_server is not None, "Flask / gate_server not importable")
class TestSessionClaimSshOptIn(unittest.TestCase):
    def setUp(self):
        gate_server.app.testing = True
        self.client = gate_server.app.test_client()

    def _claim(self, requested_ssh_marker=...):
        # Supplying a key alone must not opt in; only requested_ssh=true may do so.
        payload = {"wallet_address": WALLET, "ssh_pubkey": "ssh-ed25519 AAAA"}
        if requested_ssh_marker is not ...:
            payload["requested_ssh"] = requested_ssh_marker
        with patch.object(gate_server, "_session_mgr_available", True), \
             patch.object(gate_server, "validate_wallet_address", return_value=True), \
             patch.object(gate_server, "_require_auth_token", return_value=None), \
             patch.object(gate_server, "validate_ssh_public_key", return_value="ssh-ed25519 AAAA") as validate_key, \
             patch.object(gate_server, "try_claim_session", return_value={"granted": True}) as claim:
            response = self.client.post("/api/session/claim", json=payload)
        self.assertEqual(response.status_code, 200)
        return claim.call_args.kwargs, validate_key

    def test_missing_requested_ssh_defaults_to_desktop(self):
        kwargs, validate_key = self._claim()
        self.assertIs(kwargs["requested_ssh"], False)
        self.assertIsNone(kwargs["ssh_pubkey"])
        validate_key.assert_not_called()

    def test_string_false_does_not_enable_ssh(self):
        kwargs, validate_key = self._claim("false")
        self.assertIs(kwargs["requested_ssh"], False)
        self.assertIsNone(kwargs["ssh_pubkey"])
        validate_key.assert_not_called()

    def test_json_true_explicitly_enables_ssh(self):
        kwargs, validate_key = self._claim(True)
        self.assertIs(kwargs["requested_ssh"], True)
        self.assertEqual(kwargs["ssh_pubkey"], "ssh-ed25519 AAAA")
        validate_key.assert_called_once_with("ssh-ed25519 AAAA")


class TestFrontendSshOptInContract(unittest.TestCase):
    def test_mode_is_not_persisted_and_new_wizard_resets_it(self):
        repo = Path(__file__).resolve().parents[2]
        ui_source = (repo / "novnc-theme" / "ui.js").read_text(encoding="utf-8")
        page_source = (repo / "novnc-theme" / "vnc.html").read_text(encoding="utf-8")
        proxy_source = (repo / "axonos_gate" / "websockify_gate.py").read_text(encoding="utf-8")

        self.assertNotIn("setItem('axonosSshEnabled'", ui_source)
        self.assertIn("removeItem('axonosSshEnabled'", ui_source)
        self.assertIn("resetAxonosSshLaunchIntent", page_source)
        self.assertIn("requested_ssh = data.get('requested_ssh') is True", proxy_source)


if __name__ == "__main__":
    unittest.main()
