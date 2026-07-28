import unittest
from unittest.mock import MagicMock, patch


class TestSessionStatusOwnerMetadata(unittest.TestCase):
    wallet = "0x1234567890123456789012345678901234567890"

    def _connection(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        return conn

    def test_active_owner_fields_include_real_session_identity(self):
        from axonos_gate import session_manager

        owned = {
            "id": 73,
            "wallet_address": self.wallet,
            "requested_profile": "medium",
            "gpu_ids": [2, 3],
            "allocation_status": "allocated",
            "started_at": 900.0,
            "expires_at": 2000.0,
            "ssh_enabled": False,
        }
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=self._connection()), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_stale_paused_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[owned]), \
             patch.object(session_manager, "_get_gpu_reserved_rows", return_value=[owned]), \
             patch.object(session_manager, "_free_gpu_ids", return_value=[0, 1]), \
             patch.object(session_manager, "_gpu_device_ids", return_value=[0, 1, 2, 3]), \
             patch.object(session_manager, "_multi_session_enabled", return_value=False), \
             patch.object(session_manager, "_gpu_profiles_enabled", return_value=True), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=owned), \
             patch.object(session_manager, "_transition_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_paused_session_for_wallet", return_value=None), \
             patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.session_status(self.wallet)

        self.assertTrue(result["is_owner"])
        self.assertEqual(result["owner_session_id"], 73)
        self.assertEqual(result["owner_allocation_status"], "allocated")
        self.assertEqual(result["owner_started_at"], 900.0)

    def test_paused_ssh_owner_field_is_wallet_scoped(self):
        from axonos_gate import session_manager

        paused = {
            "id": 91,
            "wallet_address": self.wallet,
            "requested_profile": "small",
            "gpu_ids": [0],
            "allocation_status": "allocated",
            "started_at": 800.0,
            "last_heartbeat": 950.0,
            "paused_at": 975.0,
            "pause_reason": "heartbeat_timeout",
            "runtime_paused": True,
            "expires_at": 2000.0,
            "container_id": "axgt-session-91",
            "ssh_enabled": True,
        }
        ledger = MagicMock()
        ledger.init_once.return_value = False
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=self._connection()), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_stale_paused_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[]), \
             patch.object(session_manager, "_get_gpu_reserved_rows", return_value=[paused]), \
             patch.object(session_manager, "_free_gpu_ids", return_value=[1, 2, 3]), \
             patch.object(session_manager, "_gpu_device_ids", return_value=[0, 1, 2, 3]), \
             patch.object(session_manager, "_multi_session_enabled", return_value=False), \
             patch.object(session_manager, "_gpu_profiles_enabled", return_value=True), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_transition_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_paused_session_for_wallet", return_value=paused), \
             patch.object(session_manager, "_preserve_session_on_credit_exhaust", return_value=True), \
             patch.object(session_manager, "_session_paused_max_seconds", return_value=7200), \
             patch.object(session_manager, "_billing_gpu_count", return_value=1), \
             patch.object(session_manager, "_gpu_billing_enabled", return_value=False), \
             patch.object(session_manager, "_import_deposit_ledger", return_value=ledger), \
             patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.session_status(self.wallet)

        self.assertTrue(result["paused"])
        self.assertTrue(result["can_resume"])
        self.assertEqual(result["paused_session_id"], 91)
        self.assertEqual(result["paused_reason"], "heartbeat_timeout")
        self.assertEqual(result["paused_resume_seconds"], 7175)
        self.assertTrue(result["paused_ssh_enabled"])

    def test_transition_owner_fields_keep_lifecycle_visible(self):
        from axonos_gate import session_manager

        transition = {
            "id": 105,
            "wallet_address": self.wallet,
            "requested_profile": "medium",
            "gpu_ids": [2, 3],
            "container_id": "axgt-session-105",
            "allocation_status": "allocated",
            "started_at": 900.0,
            "expires_at": 2000.0,
            "ssh_enabled": False,
            "status": "pausing",
            "transition_token": "current-generation",
        }
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=self._connection()), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_stale_paused_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[]), \
             patch.object(session_manager, "_get_gpu_reserved_rows", return_value=[transition]), \
             patch.object(session_manager, "_free_gpu_ids", return_value=[0, 1]), \
             patch.object(session_manager, "_gpu_device_ids", return_value=[0, 1, 2, 3]), \
             patch.object(session_manager, "_multi_session_enabled", return_value=False), \
             patch.object(session_manager, "_gpu_profiles_enabled", return_value=True), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_transition_session_for_wallet", return_value=transition), \
             patch.object(session_manager, "_paused_session_for_wallet", return_value=None), \
             patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.session_status(self.wallet)

        self.assertFalse(result["active"])
        self.assertTrue(result["is_owner"])
        self.assertTrue(result["lifecycle_in_progress"])
        self.assertEqual(result["owner_lifecycle_state"], "pausing")
        self.assertEqual(result["owner_session_id"], 105)
        self.assertEqual(result["owner_container_id"], "axgt-session-105")
        self.assertEqual(result["owner_requested_profile"], "medium")
        self.assertEqual(result["owner_assigned_gpu_ids"], [2, 3])
        self.assertNotIn("paused", result)


if __name__ == "__main__":
    unittest.main()
