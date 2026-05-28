"""
Access control and billing tests: wallet with no deposit denied, with credit allowed,
heartbeat billing. Uses mocked deposit_ledger and session DB.
"""

import os
import subprocess
import sys
import unittest
from unittest.mock import patch, MagicMock

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)


class TestAccessControl(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "AXGT_CHALLENGE_DB_URL": "postgresql://test/test",
                "AXGT_MIN_DEPOSIT": "100",
                "AXGT_CREDIT_PER_100_AXGT_MINUTES": "60",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_get_wallet_access_status_no_deposit(self):
        from axonos_gate import axgt_verifier
        with patch("axonos_gate.deposit_ledger.init_once", return_value=True), \
             patch("axonos_gate.deposit_ledger.get_deposit_status") as mock_get:
            mock_get.return_value = {
                "remaining_minutes": 0.0,
                "consumed_minutes": 0.0,
                "credited_minutes_total": 0.0,
                "deposited_amount_axgt": 0,
                "has_deposit": False,
            }
            status = axgt_verifier.get_wallet_access_status("0x1234567890123456789012345678901234567890")
        self.assertFalse(status["verified"])
        self.assertIsNone(status["access_type"])
        self.assertEqual(status["remaining_minutes"], 0.0)

    def test_get_wallet_access_status_with_credit(self):
        from axonos_gate import axgt_verifier
        with patch("axonos_gate.deposit_ledger.init_once", return_value=True), \
             patch("axonos_gate.deposit_ledger.get_deposit_status") as mock_get:
            mock_get.return_value = {
                "remaining_minutes": 45.0,
                "consumed_minutes": 15.0,
                "credited_minutes_total": 60.0,
                "deposited_amount_axgt": 100,
                "has_deposit": True,
            }
            status = axgt_verifier.get_wallet_access_status("0x1234567890123456789012345678901234567890")
        self.assertTrue(status["verified"])
        self.assertEqual(status["access_type"], "deposit_credit")
        self.assertEqual(status["remaining_minutes"], 45.0)
        self.assertEqual(status["consumed_minutes"], 15.0)
        self.assertEqual(status["credited_minutes"], 60.0)

    def test_has_access_no_credit(self):
        from axonos_gate import axgt_verifier
        with patch("axonos_gate.axgt_verifier.get_wallet_access_status") as mock_status:
            mock_status.return_value = {"verified": False, "remaining_minutes": 0.0}
            allowed, access_type, remaining = axgt_verifier.has_access("0x1234567890123456789012345678901234567890")
        self.assertFalse(allowed)
        self.assertIsNone(access_type)
        self.assertEqual(remaining, 0.0)

    def test_has_access_with_credit(self):
        from axonos_gate import axgt_verifier
        with patch("axonos_gate.axgt_verifier.get_wallet_access_status") as mock_status:
            mock_status.return_value = {
                "verified": True,
                "access_type": "deposit_credit",
                "remaining_minutes": 30.0,
            }
            allowed, access_type, remaining = axgt_verifier.has_access("0x1234567890123456789012345678901234567890")
        self.assertTrue(allowed)
        self.assertEqual(access_type, "deposit_credit")
        self.assertEqual(remaining, 30.0)

    def test_get_credit_policy_deposit_based(self):
        from axonos_gate import axgt_verifier
        with patch.dict(os.environ, {"AXGT_MIN_DEPOSIT": "100", "AXGT_CREDIT_PER_100_AXGT_MINUTES": "60"}):
            policy = axgt_verifier.get_credit_policy()
        self.assertIn("min_deposit", policy)
        self.assertEqual(policy["credit_per_100_axgt_minutes"], 60)


class TestBillingAndSession(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"AXGT_CHALLENGE_DB_URL": "postgresql://test/test"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_calls_deduct_usage(self, mock_conn):
        from axonos_gate import session_manager
        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            None,                          # _expire_stale_session RETURNING
            (1, 1000.0, 2000.0, 500.0, "small", "0", "shared-desktop"),   # SELECT session row
            (2000.0,),                     # UPDATE RETURNING expires_at
        ]
        conn.cursor.return_value = cur
        mock_conn.return_value = conn

        with patch("axonos_gate.deposit_ledger.init_once", return_value=True), \
             patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor") as mock_deduct:
            mock_deduct.return_value = (True, 58.5, None)
            result = session_manager.heartbeat("0x1234567890123456789012345678901234567890")
        self.assertTrue(result.get("ok"))
        mock_deduct.assert_called_once()
        call_args = mock_deduct.call_args[0]
        self.assertEqual(call_args[1], "0x1234567890123456789012345678901234567890".lower())
        self.assertGreater(call_args[2], 0)

    @patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor")
    @patch("axonos_gate.deposit_ledger.init_once", return_value=True)
    @patch("axonos_gate.session_manager.time.time", return_value=1500.0)
    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_extends_expires_at_sliding(
        self, mock_conn, _mock_time, _mock_init, mock_deduct
    ):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            None,
            (1, 1000.0, 2000.0, 500.0, "small", "0", "axgt-session-1"),
            (5100.0,),
        ]
        conn.cursor.return_value = cur
        mock_conn.return_value = conn
        mock_deduct.return_value = (True, 58.5, None)

        with patch.dict(os.environ, {"AXGT_SESSION_MAX_MINUTES": "60"}, clear=False):
            session_manager.heartbeat("0x1234567890123456789012345678901234567890")

        update_calls = [
            c for c in cur.execute.call_args_list
            if c[0]
            and "last_heartbeat" in str(c[0][0])
            and "last_billed_at" in str(c[0][0])
            and "expires_at" in str(c[0][0])
        ]
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][0][1][2], 1500.0 + 60 * 60)

    @patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor")
    @patch("axonos_gate.deposit_ledger.get_remaining_minutes", return_value=50.0)
    @patch("axonos_gate.deposit_ledger.init_once", return_value=True)
    @patch("axonos_gate.session_manager.time.time", return_value=1500.0)
    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_gpu_weighted_billing(
        self, mock_conn, _mock_time, _mock_init, _mock_remaining, mock_deduct
    ):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            None,
            (1, 1000.0, 2000.0, 1000.0, "large", "0,1,2,3", "cid"),
            (2000.0,),
        ]
        conn.cursor.return_value = cur
        mock_conn.return_value = conn

        with patch.dict(
            os.environ,
            {
                "AXGT_GPU_PROFILES_ENABLED": "true",
                "AXGT_GPU_WEIGHTED_BILLING": "true",
            },
            clear=False,
        ):
            mock_deduct.return_value = (True, 50.0, None)
            result = session_manager.heartbeat(
                "0x1234567890123456789012345678901234567890"
            )

        self.assertTrue(result.get("ok"))
        billed = mock_deduct.call_args[0][2]
        # 500s wall between t=1000 and t=1500 → 500/60 min × 4 GPUs
        self.assertAlmostEqual(billed, (500.0 / 60.0) * 4, places=4)
        self.assertEqual(result.get("billing_gpu_count"), 4)

    def test_gpu_weighted_usage_minutes_helper(self):
        from axonos_gate import session_manager

        with patch.dict(
            os.environ,
            {"AXGT_GPU_PROFILES_ENABLED": "true", "AXGT_GPU_WEIGHTED_BILLING": "true"},
            clear=False,
        ):
            self.assertEqual(
                session_manager._usage_minutes_for_interval(10.0, [0, 1], "medium"),
                20.0,
            )
            self.assertEqual(
                session_manager._usage_minutes_for_interval(10.0, [], "max"),
                80.0,
            )

    def test_gpu_allocation_no_overlap(self):
        from axonos_gate import session_manager
        active_rows = [
            {"gpu_ids": [0], "wallet_address": "0xaaa"},
            {"gpu_ids": [2, 3], "wallet_address": "0xbbb"},
        ]
        with patch.dict(os.environ, {"AXGT_GPU_DEVICE_IDS": "0,1,2,3"}):
            alloc = session_manager._choose_allocation(active_rows, 1)
        self.assertEqual(alloc, [1])

    @patch("axonos_gate.session_manager._on_session_credit_paused")
    @patch("axonos_gate.session_manager._on_session_ended")
    @patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor")
    @patch("axonos_gate.deposit_ledger.init_once", return_value=True)
    @patch("axonos_gate.session_manager.time.time", return_value=1500.0)
    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_credit_exhaust_pauses_session(
        self, mock_conn, _mock_time, _mock_init, mock_deduct, mock_ended, mock_paused
    ):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            None,  # _expire_stale_session
            (1, 1000.0, 2000.0, 1000.0, "small", "0", "axgt-session-1"),  # active session
            ("0x1234567890123456789012345678901234567890",),  # pause UPDATE RETURNING
        ]
        conn.cursor.return_value = cur
        mock_conn.return_value = conn
        mock_deduct.return_value = (True, 0.0, None)

        with patch.dict(
            os.environ,
            {"AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST": "true"},
            clear=False,
        ):
            result = session_manager.heartbeat(
                "0x1234567890123456789012345678901234567890"
            )

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "Credit exhausted")
        self.assertTrue(result.get("paused_for_resume"))
        mock_paused.assert_called_once()
        mock_ended.assert_not_called()

    @patch("axonos_gate.session_manager._on_session_credit_paused")
    @patch("axonos_gate.session_manager._on_session_ended")
    @patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor")
    @patch("axonos_gate.deposit_ledger.init_once", return_value=True)
    @patch("axonos_gate.session_manager.time.time", return_value=1500.0)
    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_credit_exhaust_can_tear_down_when_disabled(
        self, mock_conn, _mock_time, _mock_init, mock_deduct, mock_ended, mock_paused
    ):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            None,
            (1, 1000.0, 2000.0, 1000.0, "small", "0", "axgt-session-1"),
            ("0x1234567890123456789012345678901234567890",),
        ]
        conn.cursor.return_value = cur
        mock_conn.return_value = conn
        mock_deduct.return_value = (True, 0.0, None)

        with patch.dict(
            os.environ,
            {"AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST": "false"},
            clear=False,
        ):
            result = session_manager.heartbeat(
                "0x1234567890123456789012345678901234567890"
            )

        self.assertFalse(result.get("ok"))
        mock_ended.assert_called_once()
        mock_paused.assert_not_called()

    def test_gpu_allocation_insufficient_capacity(self):
        from axonos_gate import session_manager
        active_rows = [
            {"gpu_ids": [0, 1, 2], "wallet_address": "0xaaa"},
        ]
        with patch.dict(os.environ, {"AXGT_GPU_DEVICE_IDS": "0,1,2,3"}):
            alloc = session_manager._choose_allocation(active_rows, 2)
        self.assertIsNone(alloc)

    @patch("axonos_gate.session_manager._get_connection")
    @patch("axonos_gate.session_manager._init_once", return_value=True)
    @patch("axonos_gate.session_manager._expire_stale_session", return_value=(None, []))
    @patch("axonos_gate.session_manager._expire_stale_paused_sessions", return_value=[])
    @patch("axonos_gate.session_manager._active_session_for_wallet", return_value=None)
    @patch("axonos_gate.session_manager._paused_session_for_wallet", return_value=None)
    @patch("axonos_gate.session_manager._prepaid_credit_allows_profile", return_value=(True, None))
    @patch("axonos_gate.session_manager._gpu_device_ids", return_value=[0, 1, 2, 3])
    def test_try_claim_session_all_gpus_used_up(
        self, mock_gpus, mock_credit, mock_paused_w, mock_active_w, mock_exp_p, mock_exp, mock_init, mock_conn
    ):
        from axonos_gate import session_manager
        
        # All 4 GPUs are used up
        active_rows = [
            {"id": 1, "wallet_address": "0xaaa", "gpu_ids": [0]},
            {"id": 2, "wallet_address": "0xbbb", "gpu_ids": [1]},
            {"id": 3, "wallet_address": "0xccc", "gpu_ids": [2]},
            {"id": 4, "wallet_address": "0xddd", "gpu_ids": [3]},
        ]
        
        conn = MagicMock()
        mock_conn.return_value = conn
        
        with patch("axonos_gate.session_manager._get_active_rows", return_value=active_rows), \
             patch("axonos_gate.session_manager._get_paused_rows", return_value=[]):
            result = session_manager.try_claim_session("0x123", "small")
            
        self.assertFalse(result["granted"])
        self.assertEqual(result["reason"], "Desktop is in use by another researcher.")

    @patch("axonos_gate.session_manager._get_connection")
    @patch("axonos_gate.session_manager._init_once", return_value=True)
    @patch("axonos_gate.session_manager._expire_stale_session", return_value=(None, []))
    @patch("axonos_gate.session_manager._expire_stale_paused_sessions", return_value=[])
    @patch("axonos_gate.session_manager._active_session_for_wallet", return_value=None)
    @patch("axonos_gate.session_manager._paused_session_for_wallet", return_value=None)
    @patch("axonos_gate.session_manager._prepaid_credit_allows_profile", return_value=(True, None))
    @patch("axonos_gate.session_manager._gpu_device_ids", return_value=[0, 1, 2, 3])
    def test_try_claim_session_some_gpus_free_but_insufficient(
        self, mock_gpus, mock_credit, mock_paused_w, mock_active_w, mock_exp_p, mock_exp, mock_init, mock_conn
    ):
        from axonos_gate import session_manager
        
        # 3 of 4 GPUs are used up, 1 is free (GPU 3)
        active_rows = [
            {"id": 1, "wallet_address": "0xaaa", "gpu_ids": [0]},
            {"id": 2, "wallet_address": "0xbbb", "gpu_ids": [1]},
            {"id": 3, "wallet_address": "0xccc", "gpu_ids": [2]},
        ]
        
        conn = MagicMock()
        mock_conn.return_value = conn
        
        # We request "medium" profile (requires 2 GPUs)
        with patch("axonos_gate.session_manager._get_active_rows", return_value=active_rows), \
             patch("axonos_gate.session_manager._get_paused_rows", return_value=[]), \
             patch.dict(os.environ, {"AXGT_GPU_PROFILES_ENABLED": "true"}, clear=False):
            result = session_manager.try_claim_session("0x123", "medium")
            
        self.assertFalse(result["granted"])
        self.assertEqual(result["reason"], "No GPUs available for profile \"medium\" (2 GPU(s) required)")


class TestGpuDeviceDiscovery(unittest.TestCase):
    """AXGT_GPU_* env overrides vs nvidia-smi auto-detect for session_manager._gpu_device_ids."""

    def tearDown(self) -> None:
        from axonos_gate import session_manager

        session_manager.reset_gpu_device_cache()

    def test_explicit_ids_override_detection(self):
        from axonos_gate import session_manager
        session_manager.reset_gpu_device_cache()
        with patch.dict(
            os.environ,
            {"AXGT_GPU_DEVICE_IDS": "3,1,3"},
            clear=False,
        ), patch.object(
            session_manager,
            "_detect_nvidia_smi_gpu_indices",
            return_value=[9, 8],
        ) as mock_detect:
            gpus = session_manager._gpu_device_ids()
        mock_detect.assert_not_called()
        self.assertEqual(gpus, [1, 3])

    def test_total_count_override(self):
        from axonos_gate import session_manager
        session_manager.reset_gpu_device_cache()
        with patch.dict(
            os.environ,
            {"AXGT_GPU_TOTAL_COUNT": "4"},
            clear=False,
        ), patch.object(
            session_manager,
            "_detect_nvidia_smi_gpu_indices",
            return_value=[99],
        ) as mock_detect:
            self.assertEqual(session_manager._gpu_device_ids(), [0, 1, 2, 3])
        mock_detect.assert_not_called()

    def test_auto_detect_uses_nvidia_smi_when_no_env(self):
        from axonos_gate import session_manager
        session_manager.reset_gpu_device_cache()
        with patch.dict(
            os.environ,
            {
                "AXGT_GPU_DEVICE_IDS": "",
                "AXGT_GPU_TOTAL_COUNT": "",
                "AXGT_GPU_AUTO_DETECT": "true",
                "AXGT_GPU_DEVICE_CACHE_SECONDS": "0",
            },
            clear=False,
        ), patch.object(
            session_manager,
            "_detect_nvidia_smi_gpu_indices",
            return_value=[0, 1, 2, 3, 4, 5, 6, 7],
        ):
            self.assertEqual(
                session_manager._gpu_device_ids(),
                [0, 1, 2, 3, 4, 5, 6, 7],
            )

    def test_auto_detect_fallback_zero_when_detection_fails(self):
        from axonos_gate import session_manager
        session_manager.reset_gpu_device_cache()
        with patch.dict(
            os.environ,
            {
                "AXGT_GPU_DEVICE_IDS": "",
                "AXGT_GPU_TOTAL_COUNT": "",
                "AXGT_GPU_AUTO_DETECT": "true",
                "AXGT_GPU_DEVICE_CACHE_SECONDS": "0",
            },
            clear=False,
        ), patch.object(session_manager, "_detect_nvidia_smi_gpu_indices", return_value=None):
            self.assertEqual(session_manager._gpu_device_ids(), [0])

    def test_auto_detect_disabled_falls_back_to_single_gpu(self):
        from axonos_gate import session_manager
        session_manager.reset_gpu_device_cache()
        with patch.dict(
            os.environ,
            {
                "AXGT_GPU_DEVICE_IDS": "",
                "AXGT_GPU_TOTAL_COUNT": "",
                "AXGT_GPU_AUTO_DETECT": "false",
                "AXGT_GPU_DEVICE_CACHE_SECONDS": "0",
            },
            clear=False,
        ), patch.object(
            session_manager,
            "_detect_nvidia_smi_gpu_indices",
            return_value=[0, 1],
        ) as mock_detect:
            self.assertEqual(session_manager._gpu_device_ids(), [0])
        mock_detect.assert_not_called()

    def test_auto_detect_uses_launcher_when_local_smi_missing(self):
        from axonos_gate import session_manager
        session_manager.reset_gpu_device_cache()
        with patch.dict(
            os.environ,
            {
                "AXGT_GPU_DEVICE_IDS": "",
                "AXGT_GPU_TOTAL_COUNT": "",
                "AXGT_GPU_AUTO_DETECT": "true",
                "AXGT_GPU_DEVICE_CACHE_SECONDS": "0",
                "AXGT_SESSION_LAUNCHER_MODE": "http",
                "AXGT_SESSION_LAUNCHER_URL": "http://axonos-launcher:8090",
            },
            clear=False,
        ), patch.object(session_manager, "_detect_nvidia_smi_gpu_indices", return_value=None), patch(
            "axonos_gate.session_launcher.enumerate_host_gpus_via_http",
            return_value=[0, 1, 2, 3, 4, 5, 6, 7],
        ):
            self.assertEqual(session_manager._gpu_device_ids(), list(range(8)))

    def test_detect_nvidia_smi_parses_stdout(self):
        from axonos_gate import session_manager

        fake = subprocess.CompletedProcess(
            args=["nvidia-smi"],
            returncode=0,
            stdout="0\n 1 \n\n2\n",
            stderr="",
        )
        with patch("axonos_gate.session_manager.subprocess.run", return_value=fake):
            self.assertEqual(session_manager._detect_nvidia_smi_gpu_indices(), [0, 1, 2])


class TestSessionLauncher(unittest.TestCase):
    def test_noop_mode_returns_named_container(self):
        from axonos_gate import session_launcher
        with patch.dict(os.environ, {"AXGT_USER_CONTAINER_ENABLED": "true", "AXGT_SESSION_LAUNCHER_MODE": "noop"}):
            ok, container_id, err = session_launcher.launch_session(
                session_id=7,
                wallet="0x1234567890123456789012345678901234567890",
                profile="small",
                gpu_ids=[0],
            )
        self.assertTrue(ok)
        self.assertEqual(container_id, "axgt-session-7")
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()
