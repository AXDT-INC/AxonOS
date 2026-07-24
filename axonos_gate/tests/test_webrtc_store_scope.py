"""SQL-contract tests for compute-scoped WebRTC signaling persistence."""

from __future__ import annotations

import inspect
import os
import re
import sys
import unittest
from typing import Any
from unittest import mock


_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)


_WALLET = "0x" + ("ab" * 20)


def _sql(text: str) -> str:
    return " ".join(text.split()).lower()


class _RecordingCursor:
    def __init__(self, fetch_rows: list[Any] | None = None, rowcount: int = 1):
        self.executions: list[tuple[str, tuple[Any, ...] | None]] = []
        self.fetch_rows = list(fetch_rows or [])
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        return False

    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> None:
        self.executions.append((_sql(statement), params))

    def fetchone(self):
        return self.fetch_rows.pop(0) if self.fetch_rows else None


class _RecordingConnection:
    def __init__(self, cursor: _RecordingCursor):
        self.recording_cursor = cursor
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self) -> _RecordingCursor:
        return self.recording_cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class WebrtcStoreScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        from webrtc import store

        self.store = store

    def test_additive_migration_keeps_owner_nullable_and_adds_partial_scope_index(self) -> None:
        cursor = _RecordingCursor()
        conn = _RecordingConnection(cursor)

        with mock.patch.object(self.store, "_pg_init_done", False), \
             mock.patch.object(self.store, "_db_url", return_value="postgresql://test"), \
             mock.patch.object(self.store, "_conn", return_value=conn):
            self.assertTrue(self.store.ensure_table())

        statements = [statement for statement, _params in cursor.executions]
        create = next(statement for statement in statements if statement.startswith("create table"))
        alter = next(statement for statement in statements if statement.startswith("alter table"))
        scope_index = next(
            statement
            for statement in statements
            if "idx_axgt_webrtc_signaling_compute_state_updated" in statement
        )

        self.assertRegex(create, r"compute_session_id\s+integer\s*,")
        self.assertNotRegex(create, r"compute_session_id\s+integer\s+not null")
        self.assertIn("add column if not exists compute_session_id integer", alter)
        self.assertNotIn("not null", alter)
        self.assertIn("(compute_session_id, state, updated_at)", scope_index)
        self.assertIn("where compute_session_id is not null", scope_index)
        self.assertEqual(conn.commits, 1)

    def test_create_session_persists_compute_owner_and_normalized_wallet(self) -> None:
        cursor = _RecordingCursor()
        conn = _RecordingConnection(cursor)
        with mock.patch.object(self.store, "ensure_table", return_value=True), \
             mock.patch.object(self.store, "_conn", return_value=conn), \
             mock.patch.object(self.store, "_new_id", return_value="signal-id"), \
             mock.patch.object(self.store.time, "time", return_value=1000.0), \
             mock.patch.object(self.store.config, "session_timeout_seconds", return_value=600):
            signal_id = self.store.create_session(_WALLET.upper(), 248)

        self.assertEqual(signal_id, "signal-id")
        statement, params = cursor.executions[0]
        self.assertIn("(id, wallet_address, compute_session_id, state", statement)
        self.assertEqual(params, ("signal-id", _WALLET, 248, 1000.0, 1000.0, 1600.0))
        self.assertEqual(conn.commits, 1)

    def test_next_queue_requires_scope_in_signature_locked_select_and_guarded_update(self) -> None:
        cursor = _RecordingCursor(
            fetch_rows=[
                ("signal-id",),
                ("signal-id", _WALLET, 248, "v=0\r\n", "offer"),
            ]
        )
        conn = _RecordingConnection(cursor)

        parameters = list(inspect.signature(self.store.fetch_next_pending_offer_for_agent).parameters)
        self.assertEqual(parameters, ["compute_session_id", "wallet_norm"])

        with mock.patch.object(self.store, "ensure_table", return_value=True), \
             mock.patch.object(self.store, "prune_expired"), \
             mock.patch.object(self.store, "_conn", return_value=conn), \
             mock.patch.object(self.store.time, "time", return_value=1000.0), \
             mock.patch.object(self.store.config, "agent_claim_lease_seconds", return_value=210):
            job = self.store.fetch_next_pending_offer_for_agent(248, _WALLET.upper())

        self.assertEqual(len(cursor.executions), 3)
        lease_recovery, lease_params = cursor.executions[0]
        locked_select, select_params = cursor.executions[1]
        guarded_update, update_params = cursor.executions[2]

        self.assertIn("state = 'scoped_offer_received'", lease_recovery)
        self.assertIn("state = 'agent_processing'", lease_recovery)
        self.assertIn("compute_session_id = %s", lease_recovery)
        self.assertIn("wallet_address = %s", lease_recovery)
        self.assertEqual(lease_params, (1000.0, 248, _WALLET, 790.0, 1000.0))

        self.assertIn("for update skip locked", locked_select)
        self.assertIn("state = 'scoped_offer_received'", locked_select)
        self.assertIn("compute_session_id = %s", locked_select)
        self.assertIn("wallet_address = %s", locked_select)
        self.assertEqual(select_params, (248, _WALLET, 1000.0))

        self.assertIn("where id = %s and state = 'scoped_offer_received'", guarded_update)
        self.assertIn("compute_session_id = %s", guarded_update)
        self.assertIn("wallet_address = %s", guarded_update)
        self.assertIn("returning id, wallet_address, compute_session_id", guarded_update)
        self.assertEqual(update_params, (1000.0, "signal-id", 248, _WALLET))
        self.assertEqual(job["compute_session_id"], 248)
        self.assertEqual(job["wallet_address"], _WALLET)
        self.assertEqual(conn.commits, 1)

    def test_legacy_null_owner_cannot_be_claimed_by_scoped_agent(self) -> None:
        # The fake has no row matching the scoped predicate, representing a DB
        # containing only a historical compute_session_id=NULL offer.
        cursor = _RecordingCursor(fetch_rows=[None])
        conn = _RecordingConnection(cursor)
        with mock.patch.object(self.store, "ensure_table", return_value=True), \
             mock.patch.object(self.store, "prune_expired"), \
             mock.patch.object(self.store, "_conn", return_value=conn), \
             mock.patch.object(self.store.time, "time", return_value=1000.0), \
             mock.patch.object(self.store.config, "agent_claim_lease_seconds", return_value=210):
            job = self.store.fetch_next_pending_offer_for_agent(248, _WALLET)

        self.assertIsNone(job)
        self.assertEqual(len(cursor.executions), 2)
        locked_select, params = cursor.executions[1]
        self.assertIn("compute_session_id = %s", locked_select)
        self.assertNotIn("compute_session_id is null", locked_select)
        self.assertNotIn("coalesce(compute_session_id", locked_select)
        self.assertEqual(params, (248, _WALLET, 1000.0))
        self.assertEqual(conn.rollbacks, 1)

    def test_agent_row_sql_is_scoped_by_signal_compute_and_wallet(self) -> None:
        stored_row = (
            "signal-id",
            _WALLET,
            248,
            "agent_processing",
            "offer",
            "offer",
            None,
            None,
            "[]",
            None,
            None,
            1.0,
            2.0,
            3.0,
        )
        cursor = _RecordingCursor(fetch_rows=[stored_row])
        conn = _RecordingConnection(cursor)
        with mock.patch.object(self.store, "ensure_table", return_value=True), \
             mock.patch.object(self.store, "_conn", return_value=conn):
            row = self.store.get_row_for_agent("signal-id", 248, _WALLET.upper())

        statement, params = cursor.executions[0]
        self.assertIn(
            "where id = %s and compute_session_id = %s and wallet_address = %s",
            statement,
        )
        self.assertIn("for update", statement)
        self.assertEqual(params, ("signal-id", 248, _WALLET))
        self.assertEqual(row["compute_session_id"], 248)
        self.assertEqual(len(cursor.executions), 2)
        lease_touch, touch_params = cursor.executions[1]
        self.assertIn("state = 'agent_processing'", lease_touch)
        self.assertEqual(touch_params[1:], ("signal-id", 248, _WALLET))
        self.assertEqual(conn.commits, 1)

    def test_answer_and_fail_updates_are_scoped_by_compute_and_wallet(self) -> None:
        answer_cursor = _RecordingCursor(rowcount=1)
        answer_conn = _RecordingConnection(answer_cursor)
        with mock.patch.object(self.store, "ensure_table", return_value=True), \
             mock.patch.object(self.store, "_conn", return_value=answer_conn), \
             mock.patch.object(self.store.time, "time", return_value=1000.0):
            self.assertTrue(
                self.store.set_answer("signal-id", 248, _WALLET.upper(), "v=0\r\n", "answer")
            )

        answer_sql, answer_params = answer_cursor.executions[0]
        self.assertIn("where id = %s and compute_session_id = %s and wallet_address = %s", answer_sql)
        self.assertIn("state = 'agent_processing'", answer_sql)
        self.assertEqual(
            answer_params,
            ("v=0\r\n", "answer", 1000.0, "signal-id", 248, _WALLET, 1000.0),
        )

        fail_cursor = _RecordingCursor(rowcount=1)
        fail_conn = _RecordingConnection(fail_cursor)
        with mock.patch.object(self.store, "ensure_table", return_value=True), \
             mock.patch.object(self.store, "_conn", return_value=fail_conn), \
             mock.patch.object(self.store.time, "time", return_value=1000.0):
            self.assertTrue(self.store.mark_failed("signal-id", 248, _WALLET.upper(), "failed"))

        fail_sql, fail_params = fail_cursor.executions[0]
        self.assertIn("where id = %s and compute_session_id = %s and wallet_address = %s", fail_sql)
        self.assertIn("state in ('scoped_offer_received', 'agent_processing')", fail_sql)
        self.assertEqual(fail_params, ("failed", 1000.0, "signal-id", 248, _WALLET))

    def test_server_ice_read_and_update_are_both_compute_wallet_scoped(self) -> None:
        cursor = _RecordingCursor(fetch_rows=[("[]",)], rowcount=1)
        conn = _RecordingConnection(cursor)
        candidate = {"candidate": "candidate:1", "sdpMid": "0", "sdpMLineIndex": 0}
        with mock.patch.object(self.store, "ensure_table", return_value=True), \
             mock.patch.object(self.store, "_conn", return_value=conn), \
             mock.patch.object(self.store.time, "time", return_value=1000.0):
            self.assertTrue(
                self.store.append_server_ice("signal-id", 248, _WALLET.upper(), [candidate])
            )

        self.assertEqual(len(cursor.executions), 2)
        select_sql, select_params = cursor.executions[0]
        update_sql, update_params = cursor.executions[1]
        for statement in (select_sql, update_sql):
            self.assertIn("id = %s", statement)
            self.assertIn("compute_session_id = %s", statement)
            self.assertIn("wallet_address = %s", statement)
        self.assertIn("for update", select_sql)
        self.assertEqual(select_params, ("signal-id", 248, _WALLET, 1000.0))
        self.assertEqual(update_params[2:], ("signal-id", 248, _WALLET))
        self.assertIn('"candidate": "candidate:1"', update_params[0])


if __name__ == "__main__":
    unittest.main()
