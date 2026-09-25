"""Explicit schedules, independent credit estimates, and durable expiry reasons.

Postgres cases require AXONOS_TEST_DB_URL pointing at a disposable test database.
Each case uses its own schema; no production environment is loaded.
"""
import os
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from axonos_gate import session_manager as sm, deposit_ledger as ledger, guest_mode

WALLET = '0x1234567890123456789012345678901234567890'
OTHER = '0x2234567890123456789012345678901234567890'


class DeadlineValidationTests(unittest.TestCase):
    def test_gpu_estimate_is_not_an_operator_cap(self):
        with patch.object(sm, '_gpu_billing_enabled', return_value=True), patch.dict(
            os.environ, {'AXGT_SSH_MAX_SESSION_MINUTES': '240'}
        ):
            self.assertAlmostEqual(sm._estimated_compute_seconds(15775.62, 8) / 3600, 32.865875)
            self.assertEqual(sm._estimated_compute_seconds(0, 8), 0)
            self.assertEqual(sm._estimated_compute_seconds(-10, 8), 0)
            self.assertIsNone(sm._estimated_compute_seconds(None, 8))
        with patch.object(sm, '_gpu_billing_enabled', return_value=False):
            self.assertEqual(sm._estimated_compute_seconds(60, 8), 3600)

    def test_invalid_deadlines_never_touch_database(self):
        with patch.object(sm, '_get_connection') as connect:
            for value in [True, '2000', float('nan'), float('inf'), 10**1000, [], {}, 0, time.time() + 366 * 86400]:
                with self.subTest(value=value):
                    self.assertFalse(sm.set_session_deadline(WALLET, 529, value)['ok'])
            for sid in [True, 0, -1, '529']:
                self.assertFalse(sm.set_session_deadline(WALLET, sid, None)['ok'])
            connect.assert_not_called()

    def test_guest_cannot_remove_deadline(self):
        with patch.object(sm, '_get_connection') as connect:
            self.assertFalse(sm.set_session_deadline(guest_mode.new_guest_identity(), 529, None)['ok'])
            connect.assert_not_called()

    def test_flask_route_requires_auth_and_explicit_stop_field(self):
        import gate_server
        client = gate_server.app.test_client()
        with patch.object(gate_server, '_session_mgr_available', True):
            with patch.object(gate_server, '_require_auth_token', return_value=('Unauthorized', 401)), patch.object(
                gate_server, 'set_session_deadline'
            ) as setter:
                response = client.post('/api/session/deadline', json={'wallet_address': WALLET, 'session_id': 529, 'stop_at': None})
                self.assertEqual(response.status_code, 401)
                setter.assert_not_called()
            with patch.object(gate_server, '_require_auth_token', return_value=None), patch.object(
                gate_server, 'set_session_deadline', return_value={'ok': True, 'scheduled_stop_at': None}
            ) as setter:
                response = client.post('/api/session/deadline', json={'wallet_address': WALLET, 'session_id': 529})
                self.assertEqual(response.status_code, 400)
                setter.assert_not_called()
                response = client.post('/api/session/deadline', json={'wallet_address': WALLET, 'session_id': 529, 'stop_at': None})
                self.assertEqual(response.status_code, 200)
                setter.assert_called_once_with(WALLET, 529, None)


@unittest.skipUnless(os.getenv('AXONOS_TEST_DB_URL'), 'isolated Postgres URL not set')
class DeadlinePostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg2
        self.schema = 'session_policy_' + uuid.uuid4().hex
        self.admin = psycopg2.connect(os.environ['AXONOS_TEST_DB_URL'])
        self.admin.autocommit = True
        with self.admin.cursor() as cur:
            cur.execute('CREATE SCHEMA ' + self.schema)
        self.addCleanup(self.cleanup_schema)
        self.connect = lambda: psycopg2.connect(os.environ['AXONOS_TEST_DB_URL'], options='-c search_path=' + self.schema)
        conn = self.connect()
        sm._ensure_tables(conn)
        ledger._ensure_tables(conn)
        conn.close()
        for obj, name, kwargs in [
            (sm, '_get_connection', {'side_effect': self.connect}),
            (sm, '_init_once', {'return_value': True}),
            (sm, '_run_stale_session_maintenance', {}),
            (sm, '_cleanup_session_container', {}),
            (sm, '_run_reset_script', {}),
            (ledger, '_get_connection', {'side_effect': self.connect}),
            (ledger, 'init_once', {'return_value': True}),
        ]:
            p = patch.object(obj, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'AXGT_GPU_WEIGHTED_BILLING': 'true', 'AXGT_GPU_PROFILES_ENABLED': 'true',
            'AXGT_HEARTBEAT_TIMEOUT_SECONDS': '120', 'AXGT_SESSION_GRACE_SECONDS': '60'})
        p.start()
        self.addCleanup(p.stop)

    def cleanup_schema(self):
        with self.admin.cursor() as cur:
            cur.execute('DROP SCHEMA ' + self.schema + ' CASCADE')
        self.admin.close()

    def query(self, sql, params=()):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows
        finally:
            conn.close()

    def session(self, sid=529, *, stop=None, kind=None, ssh=True, status='active', heartbeat=None):
        now = time.time()
        self.query("""INSERT INTO axgt_sessions
            (id,wallet_address,started_at,last_heartbeat,last_billed_at,expires_at,hard_expires_at,
             deadline_kind,ssh_enabled,status,gpu_ids,requested_profile,credit_grace_started_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'0,1,2,3,4,5,6,7','max',%s)""",
            (sid, WALLET, now-24000, heartbeat if heartbeat is not None else now, now-60, now+3600,
             stop, kind, ssh, status, now))

    def test_upgrade_clears_only_legacy_live_ssh_caps_and_is_repeatable(self):
        deadline = time.time() + 600
        self.session(stop=deadline)
        self.session(530, stop=deadline, kind='scheduled')
        self.session(531, stop=deadline, ssh=False)  # demo
        self.session(532, stop=deadline, status='ended')
        conn = self.connect()
        try:
            sm._ensure_tables(conn)
            sm._ensure_tables(conn)
        finally:
            conn.close()
        self.assertEqual(self.query('SELECT id,hard_expires_at FROM axgt_sessions ORDER BY id'),
                         [(529, None), (530, deadline), (531, deadline), (532, deadline)])

    def test_disconnected_eight_gpu_session_bills_without_implicit_deadline(self):
        self.session()
        self.assertTrue(ledger.credit_wallet_minutes(WALLET, 15775.62)[0])
        with patch.dict(os.environ, {'AXGT_SSH_MAX_SESSION_MINUTES': '240'}):
            result = sm.heartbeat(WALLET, ssh_active=False, session_id=529)
        self.assertTrue(result['ok'], result)
        self.assertIsNone(result['scheduled_stop_at'])
        self.assertAlmostEqual(result['estimated_wall_minutes_remaining'], (15775.62-8)/8, delta=.1)
        self.assertEqual(self.query('SELECT status,hard_expires_at,ssh_present FROM axgt_sessions'), [('active', None, False)])
        # A browser heartbeat must not overwrite daemon presence or add a cap.
        sm.heartbeat(WALLET, session_id=529)
        self.assertEqual(self.query('SELECT ssh_present FROM axgt_sessions'), [(False,)])

    def test_presence_and_reattach_preserve_explicit_stop(self):
        deadline = time.time() + 1800
        self.session(stop=deadline, kind='scheduled')
        ledger.credit_wallet_minutes(WALLET, 15000)
        self.assertTrue(sm.heartbeat(WALLET, ssh_active=True, session_id=529)['ok'])
        with patch.dict(os.environ, {'AXGT_USER_CONTAINER_ENABLED': 'true'}), patch.object(sm, '_run_stale_session_maintenance_locked'):
            claim = sm.try_claim_session(WALLET, expected_session_id=529)
        self.assertTrue(claim['granted'], claim)
        self.assertEqual(self.query('SELECT hard_expires_at FROM axgt_sessions'), [(deadline,)])

    def test_owner_can_set_extend_remove_but_not_change_other_wallet(self):
        self.session()
        deadline = time.time() + 3600
        self.assertFalse(sm.set_session_deadline(OTHER, 529, deadline)['ok'])
        self.assertTrue(sm.set_session_deadline(WALLET, 529, deadline)['ok'])
        self.assertTrue(sm.set_session_deadline(WALLET, 529, deadline+3600)['ok'])
        self.assertEqual(self.query('SELECT hard_expires_at FROM axgt_sessions'), [(deadline+3600,)])
        self.assertTrue(sm.set_session_deadline(WALLET, 529, None)['ok'])
        self.assertEqual(self.query('SELECT hard_expires_at,deadline_kind FROM axgt_sessions'), [(None, 'scheduled')])

    def test_topup_grace_schedule_can_change_without_extending_credit_grace(self):
        self.session(status='credit_grace')
        original = self.query('SELECT credit_grace_started_at FROM axgt_sessions')
        deadline = time.time() + 3600
        self.assertTrue(sm.set_session_deadline(WALLET, 529, deadline)['ok'])
        self.assertTrue(sm.set_session_deadline(WALLET, 529, deadline + 3600)['ok'])
        self.assertEqual(self.query('SELECT credit_grace_started_at FROM axgt_sessions'), original)

    def test_wallet_estimate_counts_all_active_sessions_with_and_without_gpu_weighting(self):
        self.session()
        self.session(530)
        context = sm.billing_context_for_wallet(WALLET)
        self.assertEqual(context['billing_gpu_count'], 16)
        self.assertEqual(context['compute_billing_rate'], 16)
        with patch.object(sm, '_gpu_billing_enabled', return_value=False):
            context = sm.billing_context_for_wallet(WALLET)
        self.assertEqual(context['billing_gpu_count'], 16)
        self.assertEqual(context['compute_billing_rate'], 2)

    def test_due_schedule_cannot_be_revived_and_expires_even_with_healthy_heartbeat(self):
        now = time.time()
        self.session(stop=now-1, kind='scheduled')
        self.assertFalse(sm.set_session_deadline(WALLET, 529, now+3600)['ok'])
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                ended, _ = sm._expire_stale_session(cur, now, gate_absent_seconds=0)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(ended, [(WALLET,529)])
        self.assertEqual(self.query('SELECT termination_reason FROM axgt_sessions'), [('scheduled_expiry',)])

    def test_runtime_timeout_reason_is_separate(self):
        now = time.time()
        self.session(heartbeat=now-121)
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                sm._expire_stale_session(cur, now, gate_absent_seconds=0)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.query('SELECT termination_reason FROM axgt_sessions'), [('heartbeat_timeout',)])

    def test_scheduled_stop_still_applies_during_topup_grace(self):
        now = time.time()
        self.session(status='credit_grace', stop=now-1, kind='scheduled')
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                ended = sm._expire_credit_grace_sessions(cur, now)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(ended, [(WALLET,529)])
        self.assertEqual(self.query('SELECT termination_reason FROM axgt_sessions'), [('scheduled_expiry',)])

    def test_manual_end_and_credit_exhaustion_have_distinct_audit_reasons(self):
        self.session()
        result = sm.release_session(WALLET, expected_session_id=529)
        self.assertTrue(result['released'], result)
        self.assertEqual(self.query("SELECT notes FROM axgt_ledger WHERE event_type='session_expiry'"), [('termination_reason=manual_stop',)])
        self.session(530)
        ledger.credit_wallet_minutes(WALLET, 1)
        with patch.object(sm, '_preserve_for_wallet', return_value=False):
            result = sm.heartbeat(WALLET, session_id=530)
        self.assertEqual(result['reason'], 'Credit exhausted')
        self.assertEqual(self.query('SELECT termination_reason FROM axgt_sessions WHERE id=530'), [('credit_exhaustion',)])
