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

    def test_storage_preference_is_returned_when_gpu_billing_is_disabled(self):
        from axonos_gate import session_manager

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        gib = 1024**3
        cur.fetchone.return_value = (200, 250 * gib)
        wallet = "0x1234567890123456789012345678901234567890"

        with patch.object(session_manager, "_gpu_billing_enabled", return_value=False), \
             patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_active_session_for_wallet") as mock_active:
            context = session_manager.billing_context_for_wallet(wallet)

        self.assertEqual(context["requested_storage_gb"], 200)
        self.assertEqual(context["provisioned_storage_gb"], 250)
        self.assertEqual(context["minimum_storage_gb"], 250)
        self.assertTrue(context["storage_growth_only"])
        self.assertFalse(context["gpu_billing_enabled"])
        mock_active.assert_not_called()
        storage_query = cur.execute.call_args
        self.assertIn("axgt_storage_volumes", storage_query.args[0])
        self.assertEqual(storage_query.args[1], (wallet, wallet))
        conn.close.assert_called_once()

    def test_verifier_supports_deployed_flat_module_billing_import(self):
        verifier_path = os.path.join(_repo_root, "axonos_gate", "axgt_verifier.py")
        with open(verifier_path, "r", encoding="utf-8") as handle:
            source = handle.read()
        billing_block = source.split("billing_ctx = _sm.billing_context_for_wallet", 1)[0]
        billing_block = billing_block.rsplit("try:", 3)[-1]
        self.assertIn("from . import session_manager as _sm", source)
        self.assertIn("from axonos_gate import session_manager as _sm", source)
        self.assertIn("import session_manager as _sm", billing_block)

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

    def _release_with_rows(self, rows, expected_session_id=None):
        from axonos_gate import session_manager

        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = list(rows)
        conn.cursor.return_value.__enter__.return_value = cur
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_acquire_allocation_scheduler_lock"), \
             patch.object(session_manager, "_on_session_ended") as ended, \
             patch.object(session_manager, "_cleanup_session_container") as cleanup:
            result = session_manager.release_session(
                "0x1234567890123456789012345678901234567890",
                expected_session_id=expected_session_id,
            )
        return result, cur, ended, cleanup

    def test_release_exact_session_ends_only_the_matching_row(self):
        result, cur, ended, _cleanup = self._release_with_rows(
            [(91, "medium", "0,1", "axgt-session-91")],
            expected_session_id=91,
        )

        self.assertTrue(result["released"])
        update = next(
            call for call in cur.execute.call_args_list
            if "UPDATE" in call.args[0] and "status IN" in call.args[0]
        )
        self.assertIn("AND id = %s", update.args[0])
        self.assertEqual(
            update.args[1],
            ("0x1234567890123456789012345678901234567890", 91),
        )
        ended.assert_called_once_with(
            "0x1234567890123456789012345678901234567890", 91
        )

    def test_release_exact_session_does_not_end_a_newer_row(self):
        result, _cur, ended, _cleanup = self._release_with_rows(
            [None, (92,)], expected_session_id=91
        )

        self.assertFalse(result["released"])
        self.assertTrue(result["session_mismatch"])
        self.assertEqual(result["expected_session_id"], 91)
        self.assertEqual(result["active_session_id"], 92)
        ended.assert_not_called()

    def test_release_exact_session_is_idempotent_when_target_is_absent(self):
        result, _cur, ended, cleanup = self._release_with_rows(
            [None, None, None], expected_session_id=91
        )

        self.assertTrue(result["released"])
        self.assertTrue(result["already_absent"])
        self.assertEqual(result["expected_session_id"], 91)
        cleanup.assert_not_called()
        ended.assert_not_called()

    def test_release_exact_ended_session_reconfirms_runtime_cleanup(self):
        result, _cur, ended, cleanup = self._release_with_rows(
            [None, None, ("ended",)], expected_session_id=91
        )

        self.assertTrue(result["released"])
        self.assertTrue(result["already_absent"])
        cleanup.assert_called_once_with(91)
        ended.assert_not_called()

    def test_release_exact_session_keeps_warning_when_cleanup_retry_fails(self):
        from axonos_gate import session_manager

        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [None, None, ("ended",)]
        conn.cursor.return_value.__enter__.return_value = cur
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_acquire_allocation_scheduler_lock"), \
             patch.object(
                 session_manager,
                 "_cleanup_session_container",
                 side_effect=RuntimeError("not confirmed"),
             ):
            result = session_manager.release_session(
                "0x1234567890123456789012345678901234567890",
                expected_session_id=91,
            )

        self.assertFalse(result["released"])
        self.assertTrue(result["cleanup_pending"])

    def test_legacy_wallet_scoped_release_retains_absent_response(self):
        result, cur, ended, _cleanup = self._release_with_rows([None])

        self.assertFalse(result["released"])
        self.assertEqual(
            result["reason"], "No active or credit-grace session for this wallet"
        )
        update = next(
            call for call in cur.execute.call_args_list
            if "UPDATE" in call.args[0] and "status IN" in call.args[0]
        )
        self.assertNotIn("AND id = %s", update.args[0])
        ended.assert_not_called()

    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_calls_deduct_usage(self, mock_conn):
        from axonos_gate import session_manager
        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            (True,),                       # opportunistic maintenance lock
            (1, 1000.0, 2000.0, 500.0, "small", "0", "shared-desktop", None, False),   # SELECT session row
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
            (True,),
            (1, 1000.0, 2000.0, 500.0, "small", "0", "axgt-session-1", None, False),
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
                (True,),
                (1, 1000.0, 2000.0, 500.0, "small", "0", "axgt-session-1", hard, True),
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
            (True,),
            (1, 1000.0, 2000.0, 1000.0, "large", "0,1,2,3", "cid", None, False),
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

    @patch("axonos_gate.session_manager._on_session_credit_grace")
    @patch("axonos_gate.session_manager._on_session_ended")
    @patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor")
    @patch("axonos_gate.deposit_ledger.init_once", return_value=True)
    @patch("axonos_gate.session_manager.time.time", return_value=1500.0)
    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_credit_exhaust_enters_grace_without_stopping_runtime(
        self, mock_conn, _mock_time, _mock_init, mock_deduct, mock_ended, mock_grace
    ):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            (True,),
            (1, 1000.0, 2000.0, 1000.0, "small", "0", "axgt-session-1", None, False),  # active session
            ("0x1234567890123456789012345678901234567890",),  # grace UPDATE RETURNING
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
        self.assertTrue(result.get("credit_grace"))
        self.assertTrue(result.get("paused_for_resume"))
        self.assertEqual(result.get("credit_grace_requested_profile"), "small")
        self.assertEqual(result.get("credit_grace_assigned_gpu_ids"), [0])
        self.assertEqual(result.get("credit_grace_gpu_count"), 1)
        self.assertFalse(result.get("credit_grace_ssh_enabled"))
        self.assertEqual(result.get("paused_session_id"), 1)
        mock_grace.assert_called_once()
        mock_ended.assert_not_called()
        grace_updates = [
            call
            for call in cur.execute.call_args_list
            if call.args and "SET status = 'credit_grace'" in str(call.args[0])
        ]
        self.assertEqual(len(grace_updates), 1)
        grace_sql, grace_params = grace_updates[0].args
        self.assertIn("last_billed_at = %s", grace_sql)
        self.assertIn("credit_grace_started_at = %s", grace_sql)
        self.assertEqual(grace_params, (1500.0, 1500.0, 1500.0, 1))

    @patch("axonos_gate.session_manager.time.time", return_value=1500.0)
    @patch("axonos_gate.session_manager._get_connection")
    def test_later_credit_grace_heartbeat_returns_full_restore_context(
        self, mock_conn, _mock_time
    ):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [(True,), None]
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        mock_conn.return_value = conn
        grace = {
            "id": 91,
            "requested_profile": "medium",
            "gpu_ids": [2, 3],
            "container_id": "axgt-session-91",
            "last_heartbeat": 1400.0,
            "credit_grace_started_at": 1400.0,
            "ssh_enabled": True,
        }

        with patch.object(
            session_manager,
            "_credit_grace_session_for_wallet",
            return_value=grace,
        ), patch.object(
            session_manager, "_session_credit_grace_max_seconds", return_value=7200
        ), patch.object(session_manager, "_gpu_billing_enabled", return_value=True):
            result = session_manager.heartbeat(
                "0x1234567890123456789012345678901234567890"
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["credit_grace_requested_profile"], "medium")
        self.assertEqual(result["credit_grace_assigned_gpu_ids"], [2, 3])
        self.assertEqual(result["credit_grace_gpu_count"], 2)
        self.assertTrue(result["credit_grace_ssh_enabled"])
        self.assertEqual(result["credit_grace_remaining_seconds"], 7100)
        self.assertEqual(result["resume_minutes_required"], 2)
        self.assertEqual(result["paused_requested_profile"], "medium")
        self.assertEqual(result["requested_profile"], "medium")
        self.assertEqual(result["session_id"], 91)

    @patch("axonos_gate.session_manager._on_session_credit_grace")
    @patch("axonos_gate.session_manager._on_session_ended")
    @patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor")
    @patch("axonos_gate.deposit_ledger.init_once", return_value=True)
    @patch("axonos_gate.session_manager.time.time", return_value=1500.0)
    @patch("axonos_gate.session_manager._get_connection")
    def test_heartbeat_credit_exhaust_can_tear_down_when_disabled(
        self, mock_conn, _mock_time, _mock_init, mock_deduct, mock_ended, mock_grace
    ):
        from axonos_gate import session_manager

        session_manager._pg_init_done = True
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            (True,),
            (1, 1000.0, 2000.0, 1000.0, "small", "0", "axgt-session-1", None, False),
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
        mock_grace.assert_not_called()

    def test_gpu_allocation_insufficient_capacity(self):
        from axonos_gate import session_manager
        active_rows = [
            {"gpu_ids": [0, 1, 2], "wallet_address": "0xaaa"},
        ]
        with patch.dict(os.environ, {"AXGT_GPU_DEVICE_IDS": "0,1,2,3"}):
            alloc = session_manager._choose_allocation(active_rows, 2)
        self.assertIsNone(alloc)

    def test_polling_maintenance_skips_when_scheduler_teardown_is_busy(self):
        from axonos_gate import session_manager

        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (False,)
        with patch.object(session_manager, "_expire_stale_session") as expire, \
             patch.object(session_manager, "_expire_credit_grace_sessions") as grace, \
             patch.object(session_manager, "_cleanup_after_stale_maintenance") as cleanup:
            ran = session_manager._run_stale_session_maintenance(conn, cur)

        self.assertFalse(ran)
        expire.assert_not_called()
        grace.assert_not_called()
        cleanup.assert_not_called()
        cur.execute.assert_called_once_with(
            "SELECT pg_try_advisory_lock(%s)",
            (session_manager._ALLOCATION_ADVISORY_LOCK_KEY,),
        )

    def test_session_end_stops_runtime_before_ledger_bookkeeping(self):
        from axonos_gate import session_manager

        events = []
        launcher = MagicMock()
        launcher.stop_session.side_effect = lambda **kwargs: (
            events.append("stop") or True
        )
        ledger = MagicMock()
        ledger.init_once.side_effect = lambda: events.append("ledger") or True
        ledger.get_remaining_minutes.return_value = 12.0
        with patch.object(
            session_manager, "_import_session_launcher", return_value=launcher
        ), patch.object(
            session_manager, "_import_deposit_ledger", return_value=ledger
        ), patch.object(session_manager, "_run_reset_script"):
            session_manager._on_session_ended("0xwallet", 41)

        self.assertEqual(events[:2], ["stop", "ledger"])
        ledger.record_session_expiry.assert_called_once()

    def test_session_end_surfaces_unconfirmed_stop_for_reconciliation(self):
        from axonos_gate import session_manager

        launcher = MagicMock()
        launcher.stop_session.return_value = False
        ledger = MagicMock()
        ledger.init_once.return_value = False
        with patch.object(
            session_manager, "_import_session_launcher", return_value=launcher
        ), patch.object(
            session_manager, "_import_deposit_ledger", return_value=ledger
        ), patch.object(session_manager, "_run_reset_script") as reset:
            with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                session_manager._on_session_ended("0xwallet", 41)

        reset.assert_called_once()

    @patch("axonos_gate.session_manager._get_connection")
    @patch("axonos_gate.session_manager._init_once", return_value=True)
    @patch("axonos_gate.session_manager._expire_stale_session", return_value=(None, []))
    @patch("axonos_gate.session_manager._expire_credit_grace_sessions", return_value=[])
    @patch("axonos_gate.session_manager._active_session_for_wallet", return_value=None)
    @patch("axonos_gate.session_manager._credit_grace_session_for_wallet", return_value=None)
    @patch("axonos_gate.session_manager._prepaid_credit_allows_profile", return_value=(True, None))
    @patch("axonos_gate.session_manager._gpu_device_ids", return_value=[0, 1, 2, 3])
    def test_try_claim_session_all_gpus_used_up(
        self, mock_gpus, mock_credit, mock_grace_w, mock_active_w, mock_exp_g, mock_exp, mock_init, mock_conn
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
             patch("axonos_gate.session_manager._get_credit_grace_rows", return_value=[]):
            result = session_manager.try_claim_session("0x123", "small")
            
        self.assertFalse(result["granted"])
        self.assertEqual(result["reason"], "Desktop is in use by another researcher.")
        claim_cur = conn.cursor.return_value.__enter__.return_value
        allocation_locks = [
            invocation
            for invocation in claim_cur.execute.call_args_list
            if invocation.args
            and invocation.args[0] == "SELECT pg_advisory_lock(%s)"
        ]
        self.assertEqual(len(allocation_locks), 1)
        self.assertEqual(
            allocation_locks[0].args[1],
            (session_manager._ALLOCATION_ADVISORY_LOCK_KEY,),
        )
        allocation_unlocks = [
            invocation
            for invocation in claim_cur.execute.call_args_list
            if invocation.args
            and invocation.args[0] == "SELECT pg_advisory_unlock(%s)"
        ]
        self.assertEqual(len(allocation_unlocks), 1)
        self.assertLess(
            claim_cur.execute.call_args_list.index(allocation_locks[0]),
            claim_cur.execute.call_args_list.index(allocation_unlocks[0]),
        )

    @patch("axonos_gate.session_manager._get_connection")
    @patch("axonos_gate.session_manager._init_once", return_value=True)
    @patch("axonos_gate.session_manager._expire_stale_session", return_value=(None, []))
    @patch("axonos_gate.session_manager._expire_credit_grace_sessions", return_value=[])
    @patch("axonos_gate.session_manager._active_session_for_wallet", return_value=None)
    @patch("axonos_gate.session_manager._credit_grace_session_for_wallet", return_value=None)
    @patch("axonos_gate.session_manager._prepaid_credit_allows_profile", return_value=(True, None))
    @patch("axonos_gate.session_manager._gpu_device_ids", return_value=[0, 1, 2, 3])
    def test_try_claim_session_some_gpus_free_but_insufficient(
        self, mock_gpus, mock_credit, mock_grace_w, mock_active_w, mock_exp_g, mock_exp, mock_init, mock_conn
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
             patch("axonos_gate.session_manager._get_credit_grace_rows", return_value=[]), \
             patch.dict(os.environ, {"AXGT_GPU_PROFILES_ENABLED": "true"}, clear=False):
            result = session_manager.try_claim_session("0x123", "medium")
            
        self.assertFalse(result["granted"])
        self.assertEqual(result["reason"], "No GPUs available for profile \"Dual\" (2 GPU(s) required)")

    def test_explicit_storage_request_below_observed_capacity_is_rejected(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur

        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_MULTI_SESSION_ENABLED": "true",
            },
            clear=False,
        ), patch.object(session_manager, "_init_once", return_value=True), patch.object(
            session_manager, "_get_connection", return_value=conn
        ), patch.object(
            session_manager, "_run_stale_session_maintenance_locked"
        ), patch.object(
            session_manager, "_get_active_rows", return_value=[]
        ), patch.object(
            session_manager, "_get_credit_grace_rows", return_value=[]
        ), patch.object(
            session_manager, "_active_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_credit_grace_session_for_wallet", return_value=None
        ), patch.object(
            session_manager,
            "_prepaid_credit_allows_profile",
            return_value=(True, None),
        ), patch.object(
            session_manager, "_provisioned_storage_gb_for_wallet", return_value=250
        ), patch.object(
            session_manager, "_choose_allocation"
        ) as choose, patch.object(
            session_manager, "_spawn_session_container"
        ) as spawn:
            result = session_manager.try_claim_session(
                wallet,
                "small",
                requested_storage_gb=100,
            )

        self.assertFalse(result["granted"])
        self.assertEqual(result["allocation_status"], "rejected")
        self.assertEqual(result["error_code"], "storage_below_provisioned")
        self.assertEqual(result["requested_storage_gb"], 100)
        self.assertEqual(result["provisioned_storage_gb"], 250)
        self.assertEqual(result["minimum_storage_gb"], 250)
        self.assertTrue(result["storage_growth_only"])
        self.assertIn("cannot be reduced from 250 GB to 100 GB", result["reason"])
        choose.assert_not_called()
        spawn.assert_not_called()
        self.assertFalse(
            any(
                invocation.args and "INSERT INTO axgt_sessions" in invocation.args[0]
                for invocation in cur.execute.call_args_list
            )
        )

    def test_omitted_storage_request_preserves_observed_capacity_floor(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        primary = MagicMock()
        primary_cur = MagicMock()
        primary_cur.__enter__.return_value = primary_cur
        primary_cur.fetchone.return_value = (73,)
        primary.cursor.return_value = primary_cur

        finalizer = MagicMock()
        finalizer_cur = MagicMock()
        finalizer_cur.__enter__.return_value = finalizer_cur
        finalizer_cur.rowcount = 1
        finalizer.cursor.return_value = finalizer_cur

        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_MULTI_SESSION_ENABLED": "true",
                "WEBRTC_ENABLED": "true",
            },
            clear=False,
        ), patch.object(session_manager, "_init_once", return_value=True), patch.object(
            session_manager, "_get_connection", side_effect=(primary, finalizer)
        ), patch.object(
            session_manager, "_run_stale_session_maintenance_locked"
        ), patch.object(
            session_manager, "_get_active_rows", return_value=[]
        ), patch.object(
            session_manager, "_get_credit_grace_rows", return_value=[]
        ), patch.object(
            session_manager, "_active_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_credit_grace_session_for_wallet", return_value=None
        ), patch.object(
            session_manager,
            "_prepaid_credit_allows_profile",
            return_value=(True, None),
        ), patch.object(
            session_manager, "_provisioned_storage_gb_for_wallet", return_value=250
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
        ) as spawn:
            result = session_manager.try_claim_session(wallet, "small")

        self.assertTrue(result["granted"])
        self.assertEqual(spawn.call_args.kwargs["requested_storage_gb"], 250)
        insert_call = next(
            invocation
            for invocation in primary_cur.execute.call_args_list
            if invocation.args
            and "INSERT INTO axgt_sessions" in invocation.args[0]
            and "requested_storage_gb" in invocation.args[0]
        )
        self.assertEqual(insert_call.args[1][-1], 250)

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
            session_manager, "_expire_credit_grace_sessions", return_value=[]
        ), patch.object(session_manager, "_get_active_rows", return_value=[]), patch.object(
            session_manager, "_get_credit_grace_rows", return_value=[]
        ), patch.object(
            session_manager, "_active_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_credit_grace_session_for_wallet", return_value=None
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

        events = []
        primary = MagicMock()
        primary_cur = MagicMock()
        primary_cur.__enter__.return_value = primary_cur
        primary_cur.fetchone.return_value = (73,)
        primary.cursor.return_value = primary_cur

        def record_primary_execute(sql, *args):
            if sql == "SELECT pg_advisory_lock(%s)":
                events.append("lock")
            elif sql == "SELECT pg_advisory_unlock(%s)":
                events.append("unlock")

        primary_cur.execute.side_effect = record_primary_execute

        finalizer = MagicMock()
        finalizer_cur = MagicMock()
        finalizer_cur.__enter__.return_value = finalizer_cur
        finalizer_cur.rowcount = 0
        finalizer.cursor.return_value = finalizer_cur
        launcher = MagicMock()
        launcher.stop_session.side_effect = lambda **kwargs: (
            events.append("stop") or True
        )
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
            session_manager, "_expire_credit_grace_sessions", return_value=[]
        ), patch.object(session_manager, "_get_active_rows", return_value=[]), patch.object(
            session_manager, "_get_credit_grace_rows", return_value=[]
        ), patch.object(
            session_manager, "_active_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_credit_grace_session_for_wallet", return_value=None
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
            side_effect=lambda **kwargs: (
                events.append("spawn") or (True, "container-id", None)
            ),
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
        self.assertEqual(
            events,
            ["lock", "unlock", "spawn", "lock", "stop", "unlock"],
        )

    def test_claim_cleans_stale_runtime_then_unlocks_before_spawn(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        events = []
        primary = MagicMock()
        primary_cur = MagicMock()
        primary_cur.__enter__.return_value = primary_cur
        primary_cur.fetchone.return_value = (73,)
        primary.cursor.return_value = primary_cur

        def record_execute(sql, *args):
            if sql == "SELECT pg_advisory_lock(%s)":
                events.append("lock")
            elif sql == "SELECT pg_advisory_unlock(%s)":
                events.append("unlock")

        primary_cur.execute.side_effect = record_execute
        finalizer = MagicMock()
        finalizer_cur = MagicMock()
        finalizer_cur.__enter__.return_value = finalizer_cur
        finalizer_cur.rowcount = 1
        finalizer.cursor.return_value = finalizer_cur

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
            session_manager,
            "_expire_stale_session",
            side_effect=([([("0xold", 41)], []), ([], [])]),
        ), patch.object(
            session_manager,
            "_expire_credit_grace_sessions",
            return_value=[],
        ), patch.object(
            session_manager,
            "_cleanup_after_stale_maintenance",
            side_effect=lambda *args: events.append("cleanup"),
        ), patch.object(session_manager, "_get_active_rows", return_value=[]), patch.object(
            session_manager, "_get_credit_grace_rows", return_value=[]
        ), patch.object(
            session_manager, "_active_session_for_wallet", return_value=None
        ), patch.object(
            session_manager, "_credit_grace_session_for_wallet", return_value=None
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
            side_effect=lambda **kwargs: (
                events.append("spawn") or (True, "container-id", None)
            ),
        ):
            result = session_manager.try_claim_session(wallet, "small")

        self.assertTrue(result["granted"])
        self.assertEqual(events, ["lock", "cleanup", "unlock", "spawn"])

    def test_resume_only_expired_session_never_allocates_replacement(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur

        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(
                 session_manager,
                 "_expire_credit_grace_sessions",
                 side_effect=([(wallet, 91)], []),
             ), \
             patch.object(session_manager, "_cleanup_after_stale_maintenance"), \
             patch.object(session_manager, "_get_active_rows", return_value=[]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_choose_allocation") as allocate, \
             patch.object(session_manager, "_spawn_session_container") as spawn:
            result = session_manager.try_claim_session(
                wallet,
                "small",
                resume_only=True,
                expected_session_id=91,
            )

        self.assertFalse(result["granted"])
        self.assertTrue(result["resume_expired"])
        self.assertFalse(result["session_mismatch"])
        self.assertEqual(result["expected_session_id"], 91)
        allocate.assert_not_called()
        spawn.assert_not_called()

    def test_resume_only_different_current_session_reports_mismatch_without_spawn(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        current = {
            "id": 92,
            "wallet_address": wallet,
            "requested_profile": "small",
            "gpu_ids": [0],
            "expires_at": 9000.0,
            "ssh_enabled": False,
        }
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur

        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_credit_grace_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[current]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=current), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_prepaid_credit_allows_profile") as credit, \
             patch.object(session_manager, "_spawn_session_container") as spawn:
            result = session_manager.try_claim_session(
                wallet,
                "small",
                resume_only=True,
                expected_session_id=91,
            )

        self.assertFalse(result["granted"])
        self.assertTrue(result["session_mismatch"])
        self.assertFalse(result["resume_expired"])
        self.assertEqual(result["current_session_id"], 92)
        credit.assert_not_called()
        spawn.assert_not_called()

    def test_resume_only_matching_active_session_is_idempotent(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        current = {
            "id": 91,
            "wallet_address": wallet,
            "requested_profile": "medium",
            "gpu_ids": [2, 3],
            "container_id": "axgt-session-91",
            "expires_at": 9000.0,
            "hard_expires_at": None,
            "ssh_enabled": False,
        }
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur

        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_credit_grace_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[current]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=current), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_prepaid_credit_allows_profile") as credit, \
             patch.object(session_manager, "_spawn_session_container") as spawn, \
             patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.try_claim_session(
                wallet,
                "small",
                resume_only=True,
                expected_session_id=91,
            )

        self.assertTrue(result["granted"])
        self.assertTrue(result["already_active"])
        self.assertTrue(result["resume_only"])
        self.assertEqual(result["session_id"], 91)
        self.assertEqual(result["requested_profile"], "medium")
        credit.assert_not_called()
        spawn.assert_not_called()

    def test_resume_only_matching_credit_grace_reactivates_exact_row(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        grace = {
            "id": 91,
            "wallet_address": wallet,
            "requested_profile": "medium",
            "gpu_ids": [2, 3],
            "container_id": "axgt-session-91",
            "ssh_enabled": False,
        }
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur

        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_credit_grace_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[grace]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=grace), \
             patch.object(
                 session_manager,
                 "_prepaid_credit_allows_profile",
                 return_value=(True, None),
             ), \
             patch.object(
                 session_manager,
                 "_resume_credit_grace_session",
                 return_value={"granted": True, "session_id": 91, "resumed": True},
             ) as reactivate, \
             patch.object(session_manager, "_spawn_session_container") as spawn, \
             patch.object(
                 session_manager.time,
                 "time",
                 side_effect=(900.0, 1000.0, 1125.0),
             ):
            result = session_manager.try_claim_session(
                wallet,
                "small",
                resume_only=True,
                expected_session_id=91,
            )

        self.assertTrue(result["granted"])
        self.assertTrue(result["resume_only"])
        self.assertEqual(result["expected_session_id"], 91)
        # Reactivation uses a timestamp captured after maintenance and both lock
        # waits, so those delays cannot be backcharged on the next heartbeat.
        reactivate.assert_called_once_with(cur, wallet, grace, 1125.0)
        spawn.assert_not_called()

    def test_runtime_heartbeat_key_remains_valid_during_credit_grace(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        cur.fetchone.return_value = ("runtime-secret",)

        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_session_credit_grace_max_seconds", return_value=7200), \
             patch.object(session_manager.time, "time", return_value=10000.0):
            valid = session_manager.validate_session_files_key(
                wallet,
                "runtime-secret",
            )

        self.assertTrue(valid)
        sql, params = cur.execute.call_args.args
        self.assertIn("status = 'credit_grace'", sql)
        self.assertIn("credit_grace_started_at", sql)
        self.assertEqual(params, (wallet, 2800.0))
        conn.close.assert_called_once()

    def test_credit_grace_reactivation_keeps_allocation_and_resets_billing_atomically(self):
        from axonos_gate import session_manager

        wallet = "0x1234567890123456789012345678901234567890"
        grace = {
            "id": 91,
            "requested_profile": "medium",
            "gpu_ids": [2, 3],
            "container_id": "axgt-session-91",
            "ssh_enabled": False,
        }
        cur = MagicMock()
        cur.fetchone.return_value = (
            91,
            "2,3",
            "axgt-session-91",
            8200.0,
            "medium",
        )

        with patch.object(
            session_manager, "_session_max_seconds", return_value=7200
        ), patch.object(
            session_manager, "_session_credit_grace_max_seconds", return_value=7200
        ), patch.object(session_manager, "_import_session_launcher") as launcher:
            result = session_manager._resume_credit_grace_session(
                cur, wallet, grace, 1000.0
            )

        self.assertTrue(result["granted"])
        self.assertTrue(result["credit_grace_reactivated"])
        self.assertEqual(result["session_id"], 91)
        self.assertEqual(result["container_id"], "axgt-session-91")
        self.assertEqual(result["requested_profile"], "medium")
        self.assertEqual(result["assigned_gpu_ids"], [2, 3])
        sql, params = cur.execute.call_args.args
        self.assertIn("SET status = 'active'", sql)
        self.assertIn("last_heartbeat = %s", sql)
        self.assertIn("last_billed_at = %s", sql)
        self.assertIn("credit_grace_started_at = NULL", sql)
        self.assertIn("status = 'credit_grace'", sql)
        self.assertIn("COALESCE(credit_grace_started_at, last_heartbeat) >= %s", sql)
        self.assertEqual(
            params,
            (1000.0, 1000.0, 8200.0, 91, wallet, -6200.0),
        )
        launcher.assert_not_called()

    def test_credit_grace_duration_prefers_new_config_and_supports_old_name(self):
        from axonos_gate import session_manager

        with patch.dict(
            os.environ,
            {
                "AXGT_SESSION_CREDIT_GRACE_MINUTES": "45",
                "AXGT_SESSION_PAUSED_MAX_MINUTES": "99",
            },
            clear=True,
        ):
            self.assertEqual(session_manager._session_credit_grace_max_seconds(), 2700)
        with patch.dict(
            os.environ,
            {"AXGT_SESSION_PAUSED_MAX_MINUTES": "30"},
            clear=True,
        ):
            self.assertEqual(session_manager._session_credit_grace_max_seconds(), 1800)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(session_manager._session_credit_grace_max_seconds(), 7200)

    def test_schema_migrates_legacy_paused_rows_to_credit_grace(self):
        from axonos_gate import session_manager

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        # Existing columns: no ALTER/backfill branches are needed for this
        # contract test; only the generated migration SQL matters.
        cur.fetchone.return_value = (1,)
        conn.cursor.return_value = cur

        session_manager._ensure_tables(conn)

        migration_calls = [
            call
            for call in cur.execute.call_args_list
            if call.args and "SET status = 'credit_grace'" in str(call.args[0])
        ]
        self.assertEqual(len(migration_calls), 1)
        migration_sql = migration_calls[0].args[0]
        self.assertIn("SET status = 'credit_grace'", migration_sql)
        self.assertIn("credit_grace_started_at", migration_sql)
        self.assertIn("last_heartbeat", migration_sql)
        conn.commit.assert_called_once()

    def test_schema_reactivates_funded_legacy_browser_timeout_rows(self):
        from axonos_gate import session_manager

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = (1,)
        conn.cursor.return_value = cur

        with patch.object(session_manager.time, "time", return_value=1000.0), \
             patch.object(session_manager, "_session_max_seconds", return_value=7200), \
             patch.object(session_manager, "_session_credit_grace_max_seconds", return_value=7200):
            session_manager._ensure_tables(conn)

        recovery_calls = [
            call
            for call in cur.execute.call_args_list
            if call.args
            and "SET status = 'active'" in str(call.args[0])
            and "axgt_deposits" in str(call.args[0])
        ]
        self.assertEqual(len(recovery_calls), 1)
        recovery_sql, recovery_params = recovery_calls[0].args
        self.assertIn("deposit.remaining_minutes > 0", recovery_sql)
        self.assertIn("last_billed_at = %s", recovery_sql)
        self.assertIn("credit_grace_started_at = NULL", recovery_sql)
        self.assertIn("session.last_heartbeat >= %s", recovery_sql)
        self.assertIn("session.hard_expires_at IS NULL", recovery_sql)
        self.assertEqual(
            recovery_params,
            (1000.0, 1000.0, 8200.0, -6200.0),
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
