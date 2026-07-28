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
        self.env = patch.dict(
            os.environ,
            {
                "AXGT_CHALLENGE_DB_URL": "postgresql://test/test",
                "AXGT_USER_CONTAINER_ENABLED": "true",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_preserved_pause_requires_isolated_user_container(self):
        from axonos_gate import session_manager

        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "false",
                "AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST": "true",
            },
            clear=False,
        ):
            self.assertFalse(session_manager._preserve_session_on_credit_exhaust())

        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST": "true",
            },
            clear=False,
        ):
            self.assertTrue(session_manager._preserve_session_on_credit_exhaust())

    def test_claim_rejects_shared_desktop_before_db_or_credit_work(self):
        from axonos_gate import session_manager

        with patch.dict(
            os.environ,
            {"AXGT_USER_CONTAINER_ENABLED": "false"},
            clear=False,
        ), patch.object(session_manager, "_init_once") as mock_init, patch.object(
            session_manager, "_get_connection"
        ) as mock_connection, patch.object(
            session_manager, "_prepaid_credit_allows_profile"
        ) as mock_credit, patch.object(
            session_manager, "_spawn_session_container"
        ) as mock_spawn:
            result = session_manager.try_claim_session(
                "0x1234567890123456789012345678901234567890"
            )

        self.assertFalse(result["granted"])
        self.assertTrue(result["configuration_error"])
        self.assertIn("AXGT_USER_CONTAINER_ENABLED=true", result["reason"])
        mock_init.assert_not_called()
        mock_connection.assert_not_called()
        mock_credit.assert_not_called()
        mock_spawn.assert_not_called()

    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_calls_deduct_usage(self, mock_conn):
        from axonos_gate import session_manager
        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            (1, 1000.0, 2000.0, 500.0, "small", "0", "shared-desktop", None),   # SELECT session row
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
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            (1, 1000.0, 2000.0, 500.0, "small", "0", "axgt-session-1", None),
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
            and "SET last_heartbeat" in str(c[0][0])
            and "last_billed_at" in str(c[0][0])
            and "expires_at" in str(c[0][0])
        ]
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][0][1][2], 1500.0 + 60 * 60)

    @patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor")
    @patch("axonos_gate.deposit_ledger.init_once", return_value=True)
    @patch("axonos_gate.session_manager.time.time", return_value=1500.0)
    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_ssh_active_renews_hard_cap(
        self, mock_conn, _mock_time, _mock_init, mock_deduct
    ):
        """A daemon-reported live SSH connection slides hard_expires_at forward
        (extend-only, min(affordable, ceiling)); without the flag it must not move."""
        from axonos_gate import session_manager

        session_manager._pg_init_done = True

        def run(ssh_active, hard):
            conn = MagicMock()
            cur = MagicMock()
            cur.fetchall.return_value = []
            cur.fetchone.side_effect = [
                (1, 1000.0, 2000.0, 500.0, "small", "0", "axgt-session-1", hard),
                (5100.0,),
            ]
            conn.cursor.return_value = cur
            mock_conn.return_value = conn
            with patch.dict(
                os.environ, {"AXGT_SSH_MAX_SESSION_MINUTES": "240"}, clear=False
            ), patch(
                "axonos_gate.session_manager._remaining_minutes_for", return_value=999.0
            ):
                result = session_manager.heartbeat(
                    "0x1234567890123456789012345678901234567890", ssh_active=ssh_active
                )
            update = [
                c for c in cur.execute.call_args_list
                if c[0] and "SET last_heartbeat" in str(c[0][0]) and "hard_expires_at" in str(c[0][0])
            ][0]
            return result, update[0][1][3]  # (now, last_billed, expires, hard, ...)

        mock_deduct.return_value = (True, 58.5, None)

        # Presence renews: cap 100s away -> now + 240 min.
        result, new_hard = run(ssh_active=True, hard=1600.0)
        self.assertEqual(new_hard, 1500.0 + 240 * 60)
        self.assertEqual(result.get("hard_cap_remaining_seconds"), 240 * 60)

        # No presence: cap untouched, still reported.
        result, new_hard = run(ssh_active=False, hard=1600.0)
        self.assertEqual(new_hard, 1600.0)
        self.assertEqual(result.get("hard_cap_remaining_seconds"), 100)

        # Uncapped session stays uncapped even with a (spoofed) presence flag.
        result, new_hard = run(ssh_active=True, hard=None)
        self.assertIsNone(new_hard)
        self.assertNotIn("hard_cap_remaining_seconds", result)

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
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            (1, 1000.0, 2000.0, 1000.0, "large", "0,1,2,3", "cid", None),
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
    @patch(
        "axonos_gate.session_manager._new_transition_token",
        return_value="pause-generation",
    )
    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_credit_exhaust_pauses_session(
        self,
        mock_conn,
        _mock_token,
        _mock_time,
        _mock_init,
        mock_deduct,
        mock_ended,
        mock_paused,
    ):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            (1, 1000.0, 2000.0, 1000.0, "small", "0", "axgt-session-1", None),  # active session
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
        transition_calls = [
            call for call in cur.execute.call_args_list
            if call.args and "SET status = 'pausing'" in str(call.args[0])
        ]
        self.assertEqual(len(transition_calls), 1)
        transition_sql, transition_params = transition_calls[0].args
        self.assertIn("last_billed_at = %s", transition_sql)
        self.assertIn("runtime_paused = FALSE", transition_sql)
        self.assertEqual(
            transition_params,
            (1500.0, 1500.0, 1500.0, "pause-generation", 1),
        )
        lock_index = next(
            index
            for index, execute_call in enumerate(cur.execute.call_args_list)
            if "pg_advisory_xact_lock" in str(execute_call.args[0])
        )
        transition_index = cur.execute.call_args_list.index(transition_calls[0])
        self.assertLess(lock_index, transition_index)
        mock_paused.assert_called_once_with(
            "0x1234567890123456789012345678901234567890",
            1,
            "axgt-session-1",
            "credit_exhausted",
            "pause-generation",
        )

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
            (1, 1000.0, 2000.0, 1000.0, "small", "0", "axgt-session-1", None),
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

    @patch("axonos_gate.session_manager._on_session_credit_paused")
    @patch("axonos_gate.session_manager._on_session_ended")
    @patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor")
    @patch("axonos_gate.deposit_ledger.init_once", return_value=True)
    @patch("axonos_gate.session_manager.time.time", return_value=1500.0)
    @patch("axonos_gate.session_manager._get_connection")
    def test_shared_legacy_heartbeat_never_advertises_resumable_pause(
        self, mock_conn, _mock_time, _mock_init, mock_deduct, mock_ended, mock_paused
    ):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            (1, 1000.0, 2000.0, 1000.0, "small", "0", "shared-desktop", None),
            ("0x1234567890123456789012345678901234567890",),
        ]
        conn.cursor.return_value = cur
        mock_conn.return_value = conn
        mock_deduct.return_value = (True, 0.0, None)

        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "false",
                "AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST": "true",
            },
            clear=False,
        ):
            result = session_manager.heartbeat(
                "0x1234567890123456789012345678901234567890"
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["paused_for_resume"])
        mock_paused.assert_not_called()
        mock_ended.assert_called_once()

    @patch("axonos_gate.session_manager._session_max_seconds", return_value=3600)
    def test_resume_paused_session_atomically_resets_billing_checkpoint(self, _mock_max):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        paused = {
            "id": 17,
            "wallet_address": wallet,
            "requested_profile": "small",
            "gpu_ids": [0],
            "container_id": "axgt-session-17",
            "last_billed_at": 1000.0,
            "ssh_enabled": False,
        }
        cur = MagicMock()
        cur.fetchone.return_value = (
            17,
            "0",
            "axgt-session-17",
            5600.0,
            "small",
        )

        result = session_manager._resume_paused_session(
            cur,
            wallet,
            paused,
            2000.0,
            "resume-generation",
        )

        self.assertTrue(result["resumed"])
        resume_sql, resume_params = cur.execute.call_args.args
        self.assertIn("status = 'resuming'", resume_sql)
        self.assertIn("last_heartbeat = %s", resume_sql)
        self.assertIn("last_billed_at = %s", resume_sql)
        self.assertEqual(
            resume_params,
            (2000.0, 2000.0, 5600.0, 17, wallet, "resume-generation"),
        )

    def test_first_heartbeat_after_resume_excludes_paused_wall_time(self):
        """Both genuinely frozen pause sources exclude suspended wall time."""
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        for pause_origin, old_checkpoint in (
            ("credit_exhaustion", 1000.0),
            ("heartbeat_stale", 1200.0),
        ):
            with self.subTest(pause_origin=pause_origin):
                paused = {
                    "id": 17,
                    "wallet_address": wallet,
                    "requested_profile": "small",
                    "gpu_ids": [0],
                    "container_id": "axgt-session-17",
                    "last_billed_at": old_checkpoint,
                    "ssh_enabled": False,
                }
                resume_cur = MagicMock()
                resume_cur.fetchone.return_value = (
                    17,
                    "0",
                    "axgt-session-17",
                    5600.0,
                    "small",
                )

                with patch.object(session_manager, "_session_max_seconds", return_value=3600):
                    session_manager._resume_paused_session(
                        resume_cur,
                        wallet,
                        paused,
                        2000.0,
                        "resume-generation",
                    )
                resume_params = resume_cur.execute.call_args.args[1]
                resumed_billing_checkpoint = resume_params[1]

                conn = MagicMock()
                heartbeat_cur = MagicMock()
                heartbeat_cur.fetchone.side_effect = [
                    (
                        17,
                        resumed_billing_checkpoint,
                        5600.0,
                        500.0,
                        "small",
                        "0",
                        "axgt-session-17",
                        None,
                    ),
                    (5601.0,),
                ]
                conn.cursor.return_value = heartbeat_cur

                with patch.object(session_manager, "_init_once", return_value=True), \
                     patch.object(session_manager, "_get_connection", return_value=conn), \
                     patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
                     patch.object(session_manager, "_expire_stale_paused_sessions", return_value=[]), \
                     patch.object(session_manager.time, "time", return_value=2001.0), \
                     patch("axonos_gate.deposit_ledger.init_once", return_value=True), \
                     patch("axonos_gate.deposit_ledger.get_remaining_minutes", return_value=59.0), \
                     patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor") as mock_deduct, \
                     patch.dict(os.environ, {"AXGT_GPU_PROFILES_ENABLED": "false"}, clear=False):
                    mock_deduct.return_value = (True, 59.0, None)
                    result = session_manager.heartbeat(wallet)

                self.assertTrue(result["ok"])
                billed_minutes = mock_deduct.call_args.args[2]
                self.assertAlmostEqual(billed_minutes, 1.0 / 60.0, places=6)

    def test_pause_hook_freezes_runtime_before_recording_unbilled_state(self):
        from axonos_gate import session_manager

        launcher = MagicMock()
        launcher.pause_session.return_value = True
        ledger = MagicMock()
        ledger.init_once.return_value = True
        ledger.get_remaining_minutes.return_value = 0.0
        wallet = "0x1234567890123456789012345678901234567890"
        pause_conn = MagicMock()
        pause_cur = MagicMock()
        pause_cur.__enter__.return_value = pause_cur
        pause_cur.fetchone.return_value = (17,)
        pause_conn.cursor.return_value = pause_cur

        with patch.object(session_manager, "_new_transition_token", return_value="pause-operation"), \
             patch.object(session_manager, "_import_session_launcher", return_value=launcher), \
             patch.object(session_manager, "_get_connection", return_value=pause_conn), \
             patch.object(session_manager, "_import_deposit_ledger", return_value=ledger), \
             patch.object(session_manager, "_end_after_runtime_pause_failure") as mock_fail_closed:
            paused = session_manager._on_session_credit_paused(
                wallet,
                17,
                "axgt-session-17",
                "credit_exhausted",
                "pause-generation",
            )

        self.assertTrue(paused)
        launcher.pause_session.assert_called_once_with(
            session_id=17,
            container_id="axgt-session-17",
            transition_token="pause-operation",
        )
        ledger.record_session_expiry.assert_called_once()
        mock_fail_closed.assert_not_called()
        finalize_sql, finalize_params = pause_cur.execute.call_args.args
        self.assertIn("SET status = 'paused'", finalize_sql)
        self.assertIn("runtime_paused = TRUE", finalize_sql)
        self.assertEqual(
            finalize_params[2:],
            (17, wallet, "pause-operation"),
        )

    def test_stale_pause_generation_does_not_touch_runtime(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = None
        conn.cursor.return_value = cur

        with patch.object(
            session_manager, "_new_transition_token", return_value="new-operation"
        ), patch.object(
            session_manager, "_get_connection", return_value=conn
        ), patch.object(
            session_manager, "_ensure_session_runtime_paused"
        ) as runtime_pause, patch.object(
            session_manager, "_end_after_runtime_pause_failure"
        ) as fail_closed:
            paused = session_manager._on_session_credit_paused(
                wallet,
                17,
                "axgt-session-17",
                "credit_exhausted",
                "stale-generation",
            )

        self.assertFalse(paused)
        runtime_pause.assert_not_called()
        fail_closed.assert_not_called()
        claim_sql, claim_params = cur.execute.call_args.args
        self.assertIn("transition_token = %s", claim_sql)
        self.assertEqual(claim_params[-1], "stale-generation")

    def test_failed_pause_stop_is_generation_fenced_before_runtime_removal(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        claim_conn = MagicMock()
        claim_cur = MagicMock()
        claim_cur.__enter__.return_value = claim_cur
        claim_cur.fetchone.return_value = (17,)
        claim_conn.cursor.return_value = claim_cur
        finalize_conn = MagicMock()
        finalize_cur = MagicMock()
        finalize_cur.__enter__.return_value = finalize_cur
        finalize_cur.fetchone.return_value = (wallet,)
        finalize_conn.cursor.return_value = finalize_cur
        launcher = MagicMock()
        launcher.stop_session.return_value = True

        with patch.object(
            session_manager,
            "_new_transition_token",
            return_value="stop-generation",
        ), patch.object(
            session_manager,
            "_get_connection",
            side_effect=[claim_conn, finalize_conn],
        ), patch.object(
            session_manager,
            "_import_session_launcher",
            return_value=launcher,
        ), patch.object(
            session_manager,
            "_on_session_ended",
        ) as ended_hook:
            ended = session_manager._end_after_runtime_pause_failure(
                wallet,
                17,
                "pause-operation",
            )

        self.assertTrue(ended)
        launcher.stop_session.assert_called_once_with(
            session_id=17,
            container_id=None,
            transition_token="stop-generation",
        )
        ended_hook.assert_called_once_with(wallet, 17)
        self.assertIn(
            "transition_token = %s",
            claim_cur.execute.call_args.args[0],
        )
        self.assertEqual(claim_cur.execute.call_args.args[1][-1], "pause-operation")
        self.assertEqual(finalize_cur.execute.call_args.args[1], (17, "stop-generation"))

    def test_pause_hook_ends_runtime_when_freeze_cannot_be_verified(self):
        from axonos_gate import session_manager

        launcher = MagicMock()
        launcher.pause_session.return_value = False
        wallet = "0x1234567890123456789012345678901234567890"
        pause_conn = MagicMock()
        pause_cur = MagicMock()
        pause_cur.__enter__.return_value = pause_cur
        pause_cur.fetchone.return_value = (17,)
        pause_conn.cursor.return_value = pause_cur
        with patch.object(session_manager, "_new_transition_token", return_value="pause-operation"), \
             patch.object(session_manager, "_get_connection", return_value=pause_conn), \
             patch.object(session_manager, "_import_session_launcher", return_value=launcher), \
             patch.object(session_manager, "_end_after_runtime_pause_failure") as mock_fail_closed, \
             patch.object(session_manager, "_import_deposit_ledger") as mock_ledger:
            paused = session_manager._on_session_credit_paused(
                wallet,
                17,
                "axgt-session-17",
                "credit_exhausted",
                "pause-generation",
            )

        self.assertFalse(paused)
        mock_fail_closed.assert_called_once_with(wallet, 17, "pause-operation")
        mock_ledger.assert_not_called()

    def test_claim_unfreezes_preserved_runtime_before_db_reactivation(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        paused_row = {
            "id": 17,
            "wallet_address": wallet,
            "requested_profile": "small",
            "gpu_ids": [0],
            "container_id": "axgt-session-17",
            "pause_reason": "credit_exhausted",
            "runtime_paused": True,
        }
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        launcher = MagicMock()
        launcher.resume_session.return_value = True
        resumed = {"granted": True, "resumed": True, "session_id": 17}

        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_stale_paused_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[]), \
             patch.object(session_manager, "_get_paused_rows", return_value=[paused_row]), \
             patch.object(session_manager, "_get_transition_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_paused_session_for_wallet", return_value=paused_row), \
             patch.object(session_manager, "_prepaid_credit_allows_profile", return_value=(True, "")), \
             patch.object(session_manager, "_new_transition_token", return_value="resume-generation"), \
             patch.object(session_manager, "_import_session_launcher", return_value=launcher), \
             patch.object(session_manager, "_resume_paused_session", return_value=resumed) as mock_db_resume:
            result = session_manager.try_claim_session(wallet)

        self.assertEqual(result, resumed)
        launcher.resume_session.assert_called_once_with(
            session_id=17,
            container_id="axgt-session-17",
            transition_token="resume-generation",
        )
        mock_db_resume.assert_called_once()
        conn.commit.assert_called()

    def test_claim_leaves_db_paused_when_runtime_cannot_resume(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        paused_row = {
            "id": 17,
            "wallet_address": wallet,
            "requested_profile": "small",
            "gpu_ids": [0],
            "container_id": "axgt-session-17",
            "runtime_paused": True,
        }
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        launcher = MagicMock()
        launcher.resume_session.return_value = False

        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_stale_paused_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[]), \
             patch.object(session_manager, "_get_paused_rows", return_value=[paused_row]), \
             patch.object(session_manager, "_get_transition_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_paused_session_for_wallet", return_value=paused_row), \
             patch.object(session_manager, "_prepaid_credit_allows_profile", return_value=(True, "")), \
             patch.object(session_manager, "_import_session_launcher", return_value=launcher), \
             patch.object(session_manager, "_resume_paused_session") as mock_db_resume:
            result = session_manager.try_claim_session(wallet)

        self.assertFalse(result["granted"])
        self.assertTrue(result["paused_for_resume"])
        mock_db_resume.assert_not_called()

    def test_failed_resume_ends_transition_if_runtime_cannot_be_refrozen(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = (17,)
        conn.cursor.return_value = cur
        with patch.object(
            session_manager, "_ensure_session_runtime_paused", return_value=False
        ), patch.object(
            session_manager, "_end_after_runtime_pause_failure", return_value=False
        ) as mock_end, patch.object(
            session_manager, "_get_connection", return_value=conn
        ), patch.object(
            session_manager,
            "_new_transition_token",
            side_effect=("compensation-generation", "pause-operation"),
        ):
            restored = session_manager._restore_paused_transition(
                wallet,
                17,
                "axgt-session-17",
                "resume-generation",
            )

        self.assertFalse(restored)
        mock_end.assert_called_once_with(wallet, 17, "pause-operation")

    def test_stale_resume_compensation_does_not_touch_runtime(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = None
        conn.cursor.return_value = cur

        with patch.object(
            session_manager,
            "_new_transition_token",
            return_value="compensation-generation",
        ), patch.object(
            session_manager, "_get_connection", return_value=conn
        ), patch.object(
            session_manager, "_on_session_credit_paused"
        ) as pause_transition, patch.object(
            session_manager, "_ensure_session_runtime_paused"
        ) as runtime_pause:
            restored = session_manager._restore_paused_transition(
                wallet,
                17,
                "axgt-session-17",
                "stale-resume-generation",
            )

        self.assertFalse(restored)
        pause_transition.assert_not_called()
        runtime_pause.assert_not_called()
        compensation_sql, compensation_params = cur.execute.call_args.args
        self.assertIn("status = 'resuming'", compensation_sql)
        self.assertEqual(compensation_params[-1], "stale-resume-generation")

    def test_same_wallet_lifecycle_transition_blocks_duplicate_claim(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        for lifecycle_state in ("pausing", "resuming"):
            with self.subTest(lifecycle_state=lifecycle_state):
                transition = {
                    "id": 17,
                    "wallet_address": wallet,
                    "requested_profile": "small",
                    "gpu_ids": [0],
                    "container_id": "axgt-session-17",
                    "status": lifecycle_state,
                    "transition_token": "current-generation",
                }
                conn = MagicMock()
                cur = MagicMock()
                cur.__enter__.return_value = cur
                conn.cursor.return_value = cur

                with patch.object(session_manager, "_init_once", return_value=True), \
                     patch.object(session_manager, "_get_connection", return_value=conn), \
                     patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
                     patch.object(session_manager, "_expire_stale_paused_sessions", return_value=[]), \
                     patch.object(session_manager, "_get_active_rows", return_value=[]), \
                     patch.object(session_manager, "_get_paused_rows", return_value=[]), \
                     patch.object(session_manager, "_get_transition_rows", return_value=[transition]), \
                     patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
                     patch.object(session_manager, "_paused_session_for_wallet") as paused_lookup, \
                     patch.object(session_manager, "_spawn_session_container") as spawn:
                    result = session_manager.try_claim_session(wallet)

                self.assertFalse(result["granted"])
                self.assertTrue(result["lifecycle_in_progress"])
                self.assertEqual(result["lifecycle_state"], lifecycle_state)
                self.assertEqual(result["session_id"], 17)
                paused_lookup.assert_not_called()
                spawn.assert_not_called()
                self.assertFalse(
                    any(
                        "INSERT INTO" in str(call.args[0])
                        for call in cur.execute.call_args_list
                        if call.args
                    )
                )

    def test_heartbeat_exposes_owned_lifecycle_transition(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        transition = {
            "id": 17,
            "wallet_address": wallet,
            "container_id": "axgt-session-17",
            "status": "resuming",
        }
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn.cursor.return_value = cur

        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_stale_paused_sessions", return_value=[]), \
             patch.object(session_manager, "_transition_session_for_wallet", return_value=transition), \
             patch.object(session_manager, "_paused_session_for_wallet") as paused_lookup:
            result = session_manager.heartbeat(wallet)

        self.assertFalse(result["ok"])
        self.assertTrue(result["lifecycle_in_progress"])
        self.assertEqual(result["lifecycle_state"], "resuming")
        self.assertEqual(result["session_id"], 17)
        self.assertEqual(result["container_id"], "axgt-session-17")
        paused_lookup.assert_called_once()

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
        self.assertEqual(result["reason"], "No GPUs available for profile \"Dual\" (2 GPU(s) required)")

    def test_desktop_claim_fails_before_spawn_when_capability_cannot_be_issued(self):
        from axonos_gate import session_manager

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = (73,)
        conn.cursor.return_value = cur
        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_MULTI_SESSION_ENABLED": "true",
                "WEBRTC_ENABLED": "true",
            },
            clear=False,
        ), patch.object(session_manager, "_init_once", return_value=True), patch.object(
            session_manager, "_get_connection", return_value=conn
        ), patch.object(
            session_manager, "_expire_stale_session", return_value=(None, [])
        ), patch.object(
            session_manager, "_expire_stale_paused_sessions", return_value=[]
        ), patch.object(session_manager, "_get_active_rows", return_value=[]), patch.object(
            session_manager, "_get_paused_rows", return_value=[]
        ), patch.object(
            session_manager, "_active_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_paused_session_for_wallet", return_value=None
        ), patch.object(
            session_manager,
            "_prepaid_credit_allows_profile",
            return_value=(True, None),
        ), patch.object(
            session_manager, "_choose_allocation", return_value=[0]
        ), patch.object(
            session_manager, "_issue_webrtc_agent_capability", return_value=None
        ), patch.object(session_manager, "_spawn_session_container") as spawn:
            result = session_manager.try_claim_session(
                "0x1234567890123456789012345678901234567890",
                "small",
            )

        self.assertFalse(result["granted"])
        self.assertEqual(result["allocation_status"], "failed")
        self.assertIn("isolated desktop agent identity", result["reason"])
        spawn.assert_not_called()
        self.assertTrue(
            any(
                "allocation_status = 'failed'" in str(invocation.args[0])
                for invocation in cur.execute.call_args_list
            )
        )

    def test_spawn_finalization_requires_live_row_and_cleans_race_loser(self):
        from axonos_gate import session_manager

        primary = MagicMock()
        primary_cur = MagicMock()
        primary_cur.__enter__.return_value = primary_cur
        primary_cur.fetchone.return_value = (73,)
        primary.cursor.return_value = primary_cur

        finalizer = MagicMock()
        finalizer_cur = MagicMock()
        finalizer_cur.__enter__.return_value = finalizer_cur
        finalizer_cur.rowcount = 0
        finalizer.cursor.return_value = finalizer_cur
        launcher = MagicMock()
        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_MULTI_SESSION_ENABLED": "true",
                "WEBRTC_ENABLED": "true",
            },
            clear=False,
        ), patch.object(session_manager, "_init_once", return_value=True), patch.object(
            session_manager,
            "_get_connection",
            side_effect=(primary, finalizer),
        ), patch.object(
            session_manager, "_expire_stale_session", return_value=(None, [])
        ), patch.object(
            session_manager, "_expire_stale_paused_sessions", return_value=[]
        ), patch.object(session_manager, "_get_active_rows", return_value=[]), patch.object(
            session_manager, "_get_paused_rows", return_value=[]
        ), patch.object(
            session_manager, "_active_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_paused_session_for_wallet", return_value=None
        ), patch.object(
            session_manager,
            "_prepaid_credit_allows_profile",
            return_value=(True, None),
        ), patch.object(
            session_manager, "_choose_allocation", return_value=[0]
        ), patch.object(
            session_manager,
            "_issue_webrtc_agent_capability",
            return_value="signed-capability",
        ), patch.object(
            session_manager,
            "_spawn_session_container",
            return_value=(True, "container-id", None),
        ), patch.object(
            session_manager,
            "_import_session_launcher",
            return_value=launcher,
        ):
            result = session_manager.try_claim_session(
                "0x1234567890123456789012345678901234567890",
                "small",
            )

        self.assertFalse(result["granted"])
        self.assertEqual(result["allocation_status"], "failed")
        self.assertIn("finaliz", result["container_error"].lower())
        launcher.stop_session.assert_called_once_with(
            session_id=73,
            container_id=None,
        )


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
        ), patch.object(session_manager, "_detect_nvidia_smi_gpu_indices", return_value=None), patch(
            "axonos_gate.session_launcher.enumerate_host_gpus_via_http",
            return_value=None,
        ):
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
