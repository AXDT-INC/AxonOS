"""Real-PostgreSQL exact-bearer checks; explicitly disposable database only."""
import os
import threading
import time
import unittest

from axonos_gate.x_capi_human import authorize_exact_bearer


class HumanAuthorityPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        from psycopg2.extensions import parse_dsn
        cls.dsn = os.environ.get('X_CAPI_HUMAN_TEST_DSN', '')
        if not cls.dsn or os.environ.get('X_CAPI_HUMAN_TEST_DISPOSABLE_CONFIRM') != 'DESTROY_TMP_HUMAN_AUTH_DATABASE':
            raise unittest.SkipTest('requires explicitly confirmed disposable human-authority database')
        parsed = parse_dsn(cls.dsn)
        if (not parsed.get('host', '').startswith('/tmp/capi-human-authority-')
                or parsed.get('dbname') != 'human_authority_test'
                or parsed.get('hostaddr')):
            raise RuntimeError('refusing non-disposable database target')
        cls.pg = psycopg2
        cls.wallet = '0x' + 'a' * 40
        cls.admin = cls.pg.connect(cls.dsn)
        cls.admin.autocommit = True
        with cls.admin.cursor() as cur:
            cur.execute('CREATE TABLE axgt_auth_tokens (token text PRIMARY KEY, wallet_address text NOT NULL, status text NOT NULL, expires_at double precision, grace_until double precision)')

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def setUp(self):
        with self.admin.cursor() as cur:
            cur.execute('TRUNCATE axgt_auth_tokens')
            cur.execute("INSERT INTO axgt_auth_tokens VALUES ('original', %s, 'current', EXTRACT(EPOCH FROM clock_timestamp()) + 60, NULL)", (self.wallet,))

    def connect(self):
        return self.pg.connect(self.dsn, application_name='human-authority-probe')

    def rows(self):
        with self.admin.cursor() as cur:
            cur.execute('SELECT * FROM axgt_auth_tokens ORDER BY token')
            return cur.fetchall()

    def wait_for_lock(self):
        until = time.monotonic() + 0.8
        while time.monotonic() < until:
            with self.admin.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'human-authority-probe' AND wait_event_type = 'Lock'")
                if cur.fetchone()[0]:
                    return
            time.sleep(0.005)
        self.fail('authorization did not enter the expected row-lock wait')

    def run_behind_lock(self, before_release=None):
        blocker = self.pg.connect(self.dsn)
        result = []
        thread = None
        try:
            with blocker.cursor() as cur:
                cur.execute("SELECT token FROM axgt_auth_tokens WHERE token='original' FOR UPDATE")
            thread = threading.Thread(target=lambda: result.append(authorize_exact_bearer('original', self.wallet, self.connect)))
            thread.start()
            self.wait_for_lock()
            if before_release:
                before_release(blocker)
            blocker.commit()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(result), 1)
            return result[0]
        finally:
            blocker.rollback()
            blocker.close()
            if thread is not None:
                thread.join(3)

    def test_current_and_grace_are_validation_only_without_issuance(self):
        before = self.rows()
        self.assertTrue(authorize_exact_bearer('original', self.wallet, self.connect))
        self.assertEqual(self.rows(), before)
        with self.admin.cursor() as cur:
            cur.execute("UPDATE axgt_auth_tokens SET status='grace', expires_at=0, grace_until=EXTRACT(EPOCH FROM clock_timestamp())+60")
        before = self.rows()
        self.assertTrue(authorize_exact_bearer('original', self.wallet, self.connect))
        self.assertEqual(self.rows(), before)

    def test_wrong_wallet_token_and_malformed_wallet_do_not_mutate(self):
        before = self.rows()
        for token, wallet in [('other', self.wallet), ('original', '0x'+'b'*40), ('original', 'malformed'), ('', self.wallet)]:
            self.assertFalse(authorize_exact_bearer(token, wallet, self.connect))
        self.assertEqual(self.rows(), before)

    def test_committed_revocation_wins_after_real_row_lock_wait(self):
        def revoke(conn):
            with conn.cursor() as cur:
                cur.execute("UPDATE axgt_auth_tokens SET status='revoked' WHERE token='original'")
        self.assertFalse(self.run_behind_lock(revoke))
        self.assertEqual(self.rows()[0][2], 'revoked')

    def test_expiry_is_checked_after_real_row_lock_wait(self):
        with self.admin.cursor() as cur:
            cur.execute('UPDATE axgt_auth_tokens SET expires_at=EXTRACT(EPOCH FROM clock_timestamp())+0.25')
        self.assertFalse(self.run_behind_lock(lambda conn: time.sleep(0.35)))

    def test_valid_bearer_survives_short_lock_wait_without_mutation(self):
        before = self.rows()
        self.assertTrue(self.run_behind_lock())
        self.assertEqual(self.rows(), before)

    def test_lost_commit_ack_suppresses_attribution(self):
        class LostAck:
            def __init__(self, conn): self.conn = conn
            def __getattr__(self, name): return getattr(self.conn, name)
            def commit(self):
                self.conn.commit()
                raise OSError('synthetic lost commit acknowledgement')
        before = self.rows()
        self.assertFalse(authorize_exact_bearer('original', self.wallet, lambda: LostAck(self.connect())))
        self.assertEqual(self.rows(), before)

    def test_expired_revoked_and_nonfinite_deadlines_fail_closed(self):
        for status, expiry, grace in [('current', 0., None), ('grace', 1e12, 0.), ('revoked', 1e12, None), ('current', float('inf'), None), ('current', float('nan'), None)]:
            with self.admin.cursor() as cur:
                cur.execute('UPDATE axgt_auth_tokens SET status=%s,expires_at=%s,grace_until=%s', (status, expiry, grace))
            self.assertFalse(authorize_exact_bearer('original', self.wallet, self.connect))


if __name__ == '__main__':
    unittest.main()
