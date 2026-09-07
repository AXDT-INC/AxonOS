"""One wallet, several concurrent sessions.

A wallet that already runs a 1-GPU session may launch further sessions on the
GPUs that remain free; every session-scoped path (claim reattach, heartbeat,
release, status, runtime-key lookup) must act on the exact row it is bound to
rather than on "the wallet's session".
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

WALLET = "0x1234567890123456789012345678901234567890"


def _row(session_id, profile="small", gpu_ids=(0,), ssh=False):
    return {
        "id": session_id,
        "wallet_address": WALLET,
        "requested_profile": profile,
        "gpu_ids": list(gpu_ids),
        "container_id": f"axgt-session-{session_id}",
        "allocation_status": "allocated",
        "started_at": 900.0 + session_id,
        "last_heartbeat": 990.0,
        "last_billed_at": 990.0,
        "expires_at": 9000.0,
        "hard_expires_at": None,
        "ssh_enabled": ssh,
        "credit_grace_started_at": None,
    }


def _cursor_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    conn.cursor.return_value = cur
    return conn, cur


class AdditionalSessionClaimTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "AXGT_CHALLENGE_DB_URL": "postgresql://test/test",
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_MULTI_SESSION_ENABLED": "true",
                "WEBRTC_ENABLED": "true",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_new_session_allocates_beside_an_owned_session(self):
        """new_session=True must not collapse into a reattach of the owned row."""
        from axonos_gate import session_manager

        owned = _row(91)
        primary, primary_cur = _cursor_conn()
        primary_cur.fetchone.return_value = (92,)
        finalizer, finalizer_cur = _cursor_conn()
        finalizer_cur.rowcount = 1

        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", side_effect=(primary, finalizer)), \
             patch.object(session_manager, "_run_stale_session_maintenance_locked"), \
             patch.object(session_manager, "_get_active_rows", return_value=[owned]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=owned), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_prepaid_credit_allows_profile", return_value=(True, None)) as credit, \
             patch.object(session_manager, "_provisioned_storage_gb_for_wallet", return_value=None), \
             patch.object(session_manager, "_choose_allocation", return_value=[1, 2]) as choose, \
             patch.object(session_manager, "_issue_webrtc_agent_capability", return_value="cap"), \
             patch.object(session_manager, "_spawn_session_container",
                          return_value=(True, "axgt-session-92", None)) as spawn:
            result = session_manager.try_claim_session(WALLET, "medium", new_session=True)

        self.assertTrue(result["granted"])
        self.assertEqual(result["session_id"], 92)
        self.assertEqual(result["requested_profile"], "medium")
        # A second session is a fresh allocation: it is credit-checked and
        # takes only GPUs that are free next to the running sibling.
        credit.assert_called_once()
        choose.assert_called_once()
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.kwargs["session_id"], 92)
        self.assertEqual(spawn.call_args.kwargs["gpu_ids"], [1, 2])

    def test_legacy_claim_without_flag_still_reattaches_to_owned_session(self):
        from axonos_gate import session_manager

        owned = _row(91, profile="small")
        conn, cur = _cursor_conn()
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_run_stale_session_maintenance_locked"), \
             patch.object(session_manager, "_get_active_rows", return_value=[owned]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=owned), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_prepaid_credit_allows_profile") as credit, \
             patch.object(session_manager, "_spawn_session_container") as spawn, \
             patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.try_claim_session(WALLET, "large")

        self.assertTrue(result["granted"])
        self.assertEqual(result["session_id"], 91)
        self.assertEqual(result["requested_profile"], "small")
        credit.assert_not_called()
        spawn.assert_not_called()

    def test_exact_reattach_returns_the_named_sibling_not_the_newest(self):
        from axonos_gate import session_manager

        newest = _row(92, profile="medium", gpu_ids=(1, 2))
        older = _row(91, profile="small", gpu_ids=(0,))

        def by_id(cur, wallet, session_id=None):
            if session_id == 91:
                return older
            if session_id == 92:
                return newest
            return newest if session_id is None else None

        conn, cur = _cursor_conn()
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_run_stale_session_maintenance_locked"), \
             patch.object(session_manager, "_get_active_rows", return_value=[older, newest]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", side_effect=by_id), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_prepaid_credit_allows_profile") as credit, \
             patch.object(session_manager, "_spawn_session_container") as spawn, \
             patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.try_claim_session(WALLET, "small", expected_session_id=91)

        self.assertTrue(result["granted"])
        self.assertEqual(result["session_id"], 91)
        self.assertEqual(result["assigned_gpu_ids"], [0])
        credit.assert_not_called()
        spawn.assert_not_called()

    def test_exact_reattach_to_ended_session_is_a_clean_mismatch_without_spawn(self):
        from axonos_gate import session_manager

        newest = _row(92)

        def by_id(cur, wallet, session_id=None):
            return newest if session_id in (None, 92) else None

        conn, cur = _cursor_conn()
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_run_stale_session_maintenance_locked"), \
             patch.object(session_manager, "_get_active_rows", return_value=[newest]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", side_effect=by_id), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_prepaid_credit_allows_profile") as credit, \
             patch.object(session_manager, "_choose_allocation") as choose, \
             patch.object(session_manager, "_spawn_session_container") as spawn:
            result = session_manager.try_claim_session(WALLET, "small", expected_session_id=77)

        self.assertFalse(result["granted"])
        self.assertTrue(result["session_mismatch"])
        self.assertEqual(result["expected_session_id"], 77)
        self.assertEqual(result["current_session_id"], 92)
        credit.assert_not_called()
        choose.assert_not_called()
        spawn.assert_not_called()

    def test_new_session_rejects_invalid_expected_session_id(self):
        from axonos_gate import session_manager

        result = session_manager.try_claim_session(WALLET, "small", expected_session_id=0)
        self.assertFalse(result["granted"])
        self.assertTrue(result["invalid_resume_request"])


class SessionScopedHeartbeatTests(unittest.TestCase):
    def test_heartbeat_bills_the_named_session_only(self):
        from axonos_gate import session_manager

        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.side_effect = [
            (True,),
            (92, 1000.0, 2000.0, 500.0, "medium", "1,2", "axgt-session-92", None, False),
            (2000.0,),
        ]
        conn.cursor.return_value = cur
        with patch.dict(os.environ, {"AXGT_CHALLENGE_DB_URL": "postgresql://test/test"}), \
             patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch("axonos_gate.deposit_ledger.init_once", return_value=True), \
             patch("axonos_gate.deposit_ledger._deduct_usage_on_cursor",
                   return_value=(True, 58.5, None)), \
             patch("axonos_gate.deposit_ledger.get_remaining_minutes", return_value=50.0), \
             patch.object(session_manager.time, "time", return_value=1060.0):
            session_manager._pg_init_done = True
            result = session_manager.heartbeat(WALLET, session_id=92)

        self.assertTrue(result["ok"])
        select = next(
            call for call in cur.execute.call_args_list
            if call.args and "FOR UPDATE" in call.args[0]
        )
        self.assertIn("AND id = %s", select.args[0])
        self.assertEqual(select.args[1], (WALLET, 92))

    def test_heartbeat_rejects_a_malformed_session_id(self):
        from axonos_gate import session_manager

        result = session_manager.heartbeat(WALLET, session_id=-4)
        self.assertFalse(result["ok"])
        self.assertIn("session_id", result["reason"])


class UnscopedReleaseTests(unittest.TestCase):
    def test_wallet_wide_release_tears_down_every_session(self):
        from axonos_gate import session_manager

        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (91, "small", "0", "axgt-session-91")
        cur.fetchall.return_value = [(92, "medium", "1,2", "axgt-session-92")]
        conn.cursor.return_value.__enter__.return_value = cur
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_acquire_allocation_scheduler_lock"), \
             patch.object(session_manager, "_on_session_ended") as ended:
            result = session_manager.release_session(WALLET)

        self.assertTrue(result["released"])
        self.assertEqual(result["released_session_ids"], [91, 92])
        self.assertEqual(
            [call.args[1] for call in ended.call_args_list],
            [91, 92],
        )


class OwnedSessionsStatusTests(unittest.TestCase):
    def test_status_lists_every_owned_session_with_free_gpu_count(self):
        from axonos_gate import session_manager

        older = _row(91, profile="small", gpu_ids=(0,))
        newest = _row(92, profile="medium", gpu_ids=(1, 2), ssh=True)
        conn, cur = _cursor_conn()
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_credit_grace_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[older, newest]), \
             patch.object(session_manager, "_get_gpu_reserved_rows", return_value=[older, newest]), \
             patch.object(session_manager, "_free_gpu_ids", return_value=[3, 4, 5, 6, 7]), \
             patch.object(session_manager, "_gpu_device_ids", return_value=list(range(8))), \
             patch.object(session_manager, "_multi_session_enabled", return_value=True), \
             patch.object(session_manager, "_gpu_profiles_enabled", return_value=True), \
             patch.object(session_manager, "_active_sessions_for_wallet", return_value=[newest, older]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=newest), \
             patch.object(session_manager, "_credit_grace_sessions_for_wallet", return_value=[]), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_ssh_connection_fields",
                          return_value={"ssh_host": "h", "ssh_port": 42042, "ssh_user": "aXonian"}), \
             patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.session_status(WALLET)

        self.assertEqual(result["free_gpu_count"], 5)
        self.assertEqual(result["total_gpu_count"], 8)
        self.assertEqual(result["owned_session_ids"], [92, 91])
        self.assertEqual(result["owned_gpu_count"], 3)
        by_id = {row["session_id"]: row for row in result["owned_sessions"]}
        self.assertEqual(set(by_id), {91, 92})
        self.assertEqual(by_id[91]["gpu_count"], 1)
        self.assertEqual(by_id[92]["gpu_count"], 2)
        self.assertEqual(by_id[92]["ssh_port"], 42042)
        self.assertNotIn("ssh_port", by_id[91])
        # The wallet-wide owner_* scalars still describe the newest row for
        # single-session clients.
        self.assertEqual(result["owner_session_id"], 92)
        # Fleet rows for the caller's own wallet carry per-row SSH endpoints so
        # each dashboard card can act on itself.
        own_ssh = [row for row in result["active_sessions"] if row["session_id"] == 92][0]
        self.assertEqual(own_ssh["ssh_port"], 42042)


if __name__ == "__main__":
    unittest.main()


class SshPortPoolTests(unittest.TestCase):
    def test_lowest_free_port_is_chosen_and_legacy_rows_keep_their_derived_port(self):
        from axonos_gate import session_manager

        conn, cur = _cursor_conn()
        # Session 7 predates pool allocation (NULL port) and still occupies
        # 42007; sessions 91 and 92 hold pool ports 42000 and 42001.
        cur.fetchall.return_value = [(7, None), (91, 42000), (92, 42001)]
        with patch.object(session_manager, "_session_credit_grace_max_seconds", return_value=7200):
            port = session_manager._allocate_ssh_port(cur, 10000.0)
        self.assertEqual(port, 42002)
        # Every port up to the legacy one is taken: the derived port is skipped.
        cur.fetchall.return_value = [(7, None)] + [(100 + i, 42000 + i) for i in range(7)]
        with patch.object(session_manager, "_session_credit_grace_max_seconds", return_value=7200):
            port = session_manager._allocate_ssh_port(cur, 10000.0)
        self.assertEqual(port, 42008)

    def test_exhausted_pool_returns_none(self):
        from axonos_gate import session_manager

        conn, cur = _cursor_conn()
        cur.fetchall.return_value = [(100 + i, 42000 + i) for i in range(50)]
        with patch.object(session_manager, "_session_credit_grace_max_seconds", return_value=7200):
            self.assertIsNone(session_manager._allocate_ssh_port(cur, 10000.0))

    def test_ssh_connection_fields_prefer_the_stored_port(self):
        from axonos_gate import session_manager

        self.assertEqual(session_manager._ssh_connection_fields(57, 42031)["ssh_port"], 42031)
        self.assertEqual(session_manager._ssh_connection_fields(57)["ssh_port"], 42000 + 57 % 50)

    def test_ssh_claim_stores_pool_port_and_template_and_hands_port_to_launcher(self):
        from axonos_gate import session_manager

        primary, primary_cur = _cursor_conn()
        primary_cur.fetchone.return_value = (93,)
        finalizer, finalizer_cur = _cursor_conn()
        finalizer_cur.rowcount = 1
        with patch.dict(os.environ, {
            "AXGT_CHALLENGE_DB_URL": "postgresql://test/test",
            "AXGT_USER_CONTAINER_ENABLED": "true",
            "AXGT_MULTI_SESSION_ENABLED": "true",
        }), \
             patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", side_effect=(primary, finalizer)), \
             patch.object(session_manager, "_run_stale_session_maintenance_locked"), \
             patch.object(session_manager, "_get_active_rows", return_value=[]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_prepaid_credit_allows_profile", return_value=(True, None)), \
             patch.object(session_manager, "_provisioned_storage_gb_for_wallet", return_value=None), \
             patch.object(session_manager, "_choose_allocation", return_value=[3]), \
             patch.object(session_manager, "_allocate_ssh_port", return_value=42011) as allocate, \
             patch.object(session_manager, "_ssh_hard_cap_seconds", return_value=None), \
             patch.object(session_manager, "_spawn_session_container",
                          return_value=(True, "axgt-session-93", None)) as spawn:
            result = session_manager.try_claim_session(
                WALLET,
                "small",
                requested_template="pytorch",
                requested_ssh=True,
                ssh_pubkey="ssh-ed25519 AAAA test",
            )

        self.assertTrue(result["granted"])
        self.assertEqual(result["ssh_port"], 42011)
        allocate.assert_called_once()
        self.assertEqual(spawn.call_args.kwargs["ssh_port"], 42011)
        insert = next(
            call for call in primary_cur.execute.call_args_list
            if call.args and "INSERT INTO axgt_sessions" in call.args[0]
        )
        self.assertIn("ssh_port, requested_template", insert.args[0])
        self.assertIn(42011, insert.args[1])
        self.assertIn("pytorch", insert.args[1])

    def test_ssh_claim_is_denied_when_the_pool_is_exhausted(self):
        from axonos_gate import session_manager

        conn, cur = _cursor_conn()
        with patch.dict(os.environ, {
            "AXGT_CHALLENGE_DB_URL": "postgresql://test/test",
            "AXGT_USER_CONTAINER_ENABLED": "true",
            "AXGT_MULTI_SESSION_ENABLED": "true",
        }), \
             patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_run_stale_session_maintenance_locked"), \
             patch.object(session_manager, "_get_active_rows", return_value=[]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_prepaid_credit_allows_profile", return_value=(True, None)), \
             patch.object(session_manager, "_provisioned_storage_gb_for_wallet", return_value=None), \
             patch.object(session_manager, "_choose_allocation", return_value=[3]), \
             patch.object(session_manager, "_allocate_ssh_port", return_value=None), \
             patch.object(session_manager, "_ssh_hard_cap_seconds", return_value=None), \
             patch.object(session_manager, "_spawn_session_container") as spawn:
            result = session_manager.try_claim_session(
                WALLET, "small", requested_ssh=True, ssh_pubkey="ssh-ed25519 AAAA test"
            )

        self.assertFalse(result["granted"])
        self.assertEqual(result["error_code"], "ssh_ports_exhausted")
        spawn.assert_not_called()
        self.assertFalse(any(
            call.args and "INSERT INTO axgt_sessions" in call.args[0]
            for call in cur.execute.call_args_list
        ))


class OlderPausedSiblingResumeTests(unittest.TestCase):
    def test_resume_only_reactivates_an_older_paused_sibling_by_exact_id(self):
        from axonos_gate import session_manager

        newest_grace = dict(_row(92), status="credit_grace", credit_grace_started_at=950.0)
        older_grace = dict(_row(91), status="credit_grace", credit_grace_started_at=940.0)

        def grace_by_id(cur, wallet, now, session_id=None):
            if session_id == 91:
                return older_grace
            if session_id == 92 or session_id is None:
                return newest_grace
            return None

        conn, cur = _cursor_conn()
        with patch.dict(os.environ, {"AXGT_CHALLENGE_DB_URL": "postgresql://test/test"}), \
             patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_run_stale_session_maintenance_locked"), \
             patch.object(session_manager, "_get_active_rows", return_value=[]), \
             patch.object(session_manager, "_get_credit_grace_rows", return_value=[older_grace, newest_grace]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", side_effect=grace_by_id), \
             patch.object(session_manager, "_preserve_for_wallet", return_value=True), \
             patch.object(session_manager, "_prepaid_credit_allows_profile", return_value=(True, None)), \
             patch.object(session_manager, "_resume_credit_grace_session",
                          return_value={"granted": True, "session_id": 91}) as resume, \
             patch.object(session_manager, "_spawn_session_container") as spawn:
            result = session_manager.try_claim_session(
                WALLET, "small", resume_only=True, expected_session_id=91
            )

        self.assertTrue(result["granted"])
        self.assertEqual(result["session_id"], 91)
        self.assertEqual(resume.call_args.args[2]["id"], 91)
        spawn.assert_not_called()

    def test_status_lists_every_paused_sibling_with_its_resume_requirement(self):
        from axonos_gate import session_manager

        newest_grace = dict(_row(92, profile="medium", gpu_ids=(1, 2)), credit_grace_started_at=950.0)
        older_grace = dict(_row(91, profile="small", gpu_ids=(0,)), credit_grace_started_at=940.0)
        conn, cur = _cursor_conn()
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_credit_grace_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[]), \
             patch.object(session_manager, "_get_gpu_reserved_rows", return_value=[older_grace, newest_grace]), \
             patch.object(session_manager, "_free_gpu_ids", return_value=[3]), \
             patch.object(session_manager, "_gpu_device_ids", return_value=[0, 1, 2, 3]), \
             patch.object(session_manager, "_multi_session_enabled", return_value=True), \
             patch.object(session_manager, "_gpu_profiles_enabled", return_value=True), \
             patch.object(session_manager, "_gpu_billing_enabled", return_value=True), \
             patch.object(session_manager, "_preserve_for_wallet", return_value=True), \
             patch.object(session_manager, "_session_credit_grace_max_seconds", return_value=7200), \
             patch.object(session_manager, "_active_sessions_for_wallet", return_value=[]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=None), \
             patch.object(session_manager, "_credit_grace_sessions_for_wallet",
                          return_value=[newest_grace, older_grace]), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=newest_grace), \
             patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.session_status(WALLET)

        paused = {row["session_id"]: row for row in result["owned_sessions"] if row["state"] == "credit_grace"}
        self.assertEqual(set(paused), {91, 92})
        self.assertEqual(paused[91]["resume_minutes_required"], 1)
        self.assertEqual(paused[92]["resume_minutes_required"], 2)
        self.assertEqual(paused[91]["credit_grace_remaining_seconds"], 7140)
        # The wallet-wide scalars still describe the newest paused row.
        self.assertEqual(result["credit_grace_session_id"], 92)


class SessionAnnotationTests(unittest.TestCase):
    def test_annotate_updates_only_the_owners_live_row(self):
        from axonos_gate import session_manager

        conn, cur = _cursor_conn()
        cur.fetchone.return_value = (91, "Protein run A", "fold batch 3")
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn):
            result = session_manager.annotate_session(
                WALLET, 91, title="  Protein run A \n", notes="fold batch 3\r\n"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "Protein run A")
        sql, params = cur.execute.call_args.args
        self.assertIn("status IN ('active', 'credit_grace')", sql)
        self.assertIn("title = %s", sql)
        self.assertIn("notes = %s", sql)
        self.assertEqual(params, ("Protein run A", "fold batch 3", WALLET, 91))

    def test_annotate_omits_untouched_fields_and_clears_empty_ones(self):
        from axonos_gate import session_manager

        conn, cur = _cursor_conn()
        cur.fetchone.return_value = (91, None, "kept")
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn):
            result = session_manager.annotate_session(WALLET, 91, title="")

        self.assertTrue(result["ok"])
        sql, params = cur.execute.call_args.args
        self.assertIn("title = %s", sql)
        self.assertNotIn("notes = %s", sql)
        self.assertEqual(params, (None, WALLET, 91))
        self.assertEqual(result["title"], "")
        self.assertEqual(result["notes"], "kept")

    def test_annotate_rejects_missing_fields_and_foreign_sessions(self):
        from axonos_gate import session_manager

        self.assertFalse(session_manager.annotate_session(WALLET, 91)["ok"])
        self.assertFalse(session_manager.annotate_session(WALLET, 0, title="x")["ok"])
        conn, cur = _cursor_conn()
        cur.fetchone.return_value = None
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn):
            result = session_manager.annotate_session(WALLET, 404, title="x")
        self.assertFalse(result["ok"])
        self.assertIn("No live session", result["reason"])

    def test_annotation_and_region_ride_the_status_payload(self):
        from axonos_gate import session_manager

        titled = dict(_row(91), title="Protein run A", notes="fold batch 3")
        conn, cur = _cursor_conn()
        with patch.dict(os.environ, {"AXONOS_HOST_REGION": "US-East"}), \
             patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_expire_stale_session", return_value=(None, [])), \
             patch.object(session_manager, "_expire_credit_grace_sessions", return_value=[]), \
             patch.object(session_manager, "_get_active_rows", return_value=[titled]), \
             patch.object(session_manager, "_get_gpu_reserved_rows", return_value=[titled]), \
             patch.object(session_manager, "_free_gpu_ids", return_value=[1]), \
             patch.object(session_manager, "_gpu_device_ids", return_value=[0, 1]), \
             patch.object(session_manager, "_multi_session_enabled", return_value=True), \
             patch.object(session_manager, "_gpu_profiles_enabled", return_value=True), \
             patch.object(session_manager, "_active_sessions_for_wallet", return_value=[titled]), \
             patch.object(session_manager, "_active_session_for_wallet", return_value=titled), \
             patch.object(session_manager, "_credit_grace_sessions_for_wallet", return_value=[]), \
             patch.object(session_manager, "_credit_grace_session_for_wallet", return_value=None), \
             patch.object(session_manager.time, "time", return_value=1000.0):
            result = session_manager.session_status(WALLET)

        self.assertEqual(result["host_region"], "US-East")
        row = result["owned_sessions"][0]
        self.assertEqual(row["title"], "Protein run A")
        self.assertEqual(row["notes"], "fold batch 3")
        self.assertEqual(result["active_sessions"][0]["title"], "Protein run A")
