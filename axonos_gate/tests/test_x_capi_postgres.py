"""Optional disposable-Postgres integration checks.

Set X_CAPI_TEST_DB_URL to a throwaway database owned by the test user and set
X_CAPI_TEST_DB_DISPOSABLE_CONFIRM to the confirmation string below.  The suite
also verifies a literal-loopback target, exact disposable cluster identities,
and an empty user catalog before its first write.  Never point it at an AxonOS
deployment database.
"""

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


TEST_URL = os.getenv("X_CAPI_TEST_DB_URL")
DISPOSABLE_CONFIRMATION = "DESTROY_LOCAL_XCAPITEST_CLUSTER_ACLS"
TEST_DISPOSABLE_CONFIRMED = (
    os.getenv("X_CAPI_TEST_DB_DISPOSABLE_CONFIRM") == DISPOSABLE_CONFIRMATION
)
POLICY_EPOCH = 7
AUDIENCE_SCOPE = "a" * 64
_MIGRATION_SEARCH_PATH = "SET LOCAL search_path = public;"


def _migration_text(filename, schema):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise ValueError("unsafe test schema")
    text = (
        Path(__file__).resolve().parents[1] / "migrations" / filename
    ).read_text()
    replacement = 'SET LOCAL search_path = "' + schema + '";'
    if text.count(_MIGRATION_SEARCH_PATH) != 1:
        raise AssertionError("migration must pin its production search_path")
    return text.replace(_MIGRATION_SEARCH_PATH, replacement)

LEGACY_SCHEMA_SQL = """
CREATE TABLE x_capi_attribution_contexts (
    id UUID PRIMARY KEY, handle_hash CHAR(64) NOT NULL UNIQUE,
    csrf_hash CHAR(64) NOT NULL, consent_state TEXT NOT NULL,
    policy_version TEXT NOT NULL, twclid TEXT,
    consented_at DOUBLE PRECISION, declined_at DOUBLE PRECISION,
    revoked_at DOUBLE PRECISION, expires_at DOUBLE PRECISION,
    wallet_hash CHAR(64), wallet_bound_at DOUBLE PRECISION,
    first_seen_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE x_capi_dedup (
    milestone TEXT NOT NULL, source_key_hash CHAR(64) NOT NULL,
    first_seen_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(milestone,source_key_hash)
);
CREATE TABLE x_capi_outbox (
    conversion_id UUID PRIMARY KEY, milestone TEXT NOT NULL,
    source_key_hash CHAR(64) NOT NULL, mode_scope TEXT NOT NULL,
    pixel_id TEXT NOT NULL, event_id TEXT NOT NULL,
    conversion_timestamp_ms BIGINT NOT NULL, twclid TEXT,
    context_id UUID NOT NULL REFERENCES x_capi_attribution_contexts(id),
    consent_policy_version TEXT NOT NULL,
    attribution_expires_at DOUBLE PRECISION NOT NULL, status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at DOUBLE PRECISION NOT NULL,
    lease_owner TEXT, lease_expires_at DOUBLE PRECISION,
    accepted_at DOUBLE PRECISION, last_error_code TEXT, safe_debug_id TEXT,
    created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE(milestone,source_key_hash)
);
CREATE TABLE x_capi_counters (
    reason TEXT PRIMARY KEY, count BIGINT NOT NULL DEFAULT 0,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE x_capi_worker_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE,
    paused_reason TEXT, paused_at DOUBLE PRECISION,
    updated_at DOUBLE PRECISION NOT NULL
);
"""


@unittest.skipUnless(
    TEST_URL and TEST_DISPOSABLE_CONFIRMED,
    "explicitly confirmed disposable PostgreSQL is required",
)
class PostgresOutboxTests(unittest.TestCase):
    @classmethod
    def _assert_disposable_database(cls):
        parsed = cls.psycopg2.extensions.parse_dsn(TEST_URL)
        if (
            set(parsed) - {"user", "password", "dbname", "host", "port"}
            or parsed.get("host") not in {"127.0.0.1", "::1"}
            or parsed.get("dbname") != "xcapitest"
            or parsed.get("user") != "xcapitest"
        ):
            raise RuntimeError(
                "X CAPI PostgreSQL tests require literal-loopback "
                "xcapitest/xcapitest"
            )
        safety_options = (
            "-c search_path=pg_catalog -c statement_timeout=5000 "
            "-c lock_timeout=1000 "
            "-c idle_in_transaction_session_timeout=5000"
        )
        admin = cls.psycopg2.connect(
            TEST_URL,
            connect_timeout=5,
            options=safety_options,
        )
        try:
            with admin.cursor() as cur:
                cur.execute(
                    """SELECT current_database(),current_user,
                              current_setting('server_version_num')::INTEGER,
                              pg_is_in_recovery(),r.rolsuper,
                              d.datdba=r.oid
                         FROM pg_roles r JOIN pg_database d
                           ON d.datname=current_database()
                        WHERE r.rolname=current_user"""
                )
                identity = cur.fetchone()
                if (
                    identity is None
                    or identity[:2] != ("xcapitest", "xcapitest")
                    or identity[3:] != (False, True, True)
                    or not 150000 <= int(identity[2]) <= 159999
                ):
                    raise RuntimeError(
                        "X CAPI PostgreSQL tests require a writable PG15 "
                        "database owned by the disposable superuser"
                    )
                cur.execute("SELECT datname,datallowconn FROM pg_database")
                databases = dict(cur.fetchall())
                if databases != {
                    "postgres": True,
                    "template0": False,
                    "template1": True,
                    "xcapitest": True,
                }:
                    raise RuntimeError(
                        "X CAPI PostgreSQL tests refuse a non-disposable cluster"
                    )
                cur.execute(
                    """SELECT rolname FROM pg_roles
                        WHERE rolname NOT LIKE 'pg\\_%%' ESCAPE '\\'"""
                )
                if {row[0] for row in cur.fetchall()} != {"xcapitest"}:
                    raise RuntimeError(
                        "X CAPI PostgreSQL tests refuse a cluster with other roles"
                    )
                cur.execute(
                    """SELECT EXISTS (
                           SELECT 1 FROM pg_namespace
                            WHERE nspname NOT LIKE 'pg\\_%%' ESCAPE '\\'
                              AND nspname NOT IN ('information_schema','public')
                       ) OR EXISTS (
                           SELECT 1 FROM pg_class c JOIN pg_namespace n
                             ON n.oid=c.relnamespace WHERE n.nspname='public'
                       ) OR EXISTS (
                           SELECT 1 FROM pg_proc p JOIN pg_namespace n
                             ON n.oid=p.pronamespace WHERE n.nspname='public'
                       ) OR EXISTS (
                           SELECT 1 FROM pg_extension WHERE extname<>'plpgsql'
                       )"""
                )
                if bool(cur.fetchone()[0]):
                    raise RuntimeError(
                        "X CAPI PostgreSQL tests require an empty user catalog"
                    )
                cls.database_name = "xcapitest"
                cls.connectable_databases = tuple(
                    sorted(name for name, allowed in databases.items() if allowed)
                )
        finally:
            admin.close()
        for database_name in ("postgres", "template1"):
            probe = cls.psycopg2.connect(
                TEST_URL,
                dbname=database_name,
                connect_timeout=5,
                options=safety_options,
            )
            try:
                with probe.cursor() as cur:
                    cur.execute(
                        """SELECT current_database(),current_user,
                                  EXISTS (
                                    SELECT 1 FROM pg_namespace
                                     WHERE nspname NOT LIKE 'pg\\_%%' ESCAPE '\\'
                                       AND nspname NOT IN (
                                           'information_schema','public'
                                       )
                                  ) OR EXISTS (
                                    SELECT 1 FROM pg_class c
                                      JOIN pg_namespace n
                                        ON n.oid=c.relnamespace
                                     WHERE n.nspname='public'
                                  ) OR EXISTS (
                                    SELECT 1 FROM pg_proc p
                                      JOIN pg_namespace n
                                        ON n.oid=p.pronamespace
                                     WHERE n.nspname='public'
                                  ) OR EXISTS (
                                    SELECT 1 FROM pg_extension
                                     WHERE extname<>'plpgsql'
                                  )"""
                    )
                    if cur.fetchone() != (database_name, "xcapitest", False):
                        raise RuntimeError(
                            "X CAPI PostgreSQL tests refuse a used "
                            "administrative database"
                        )
            finally:
                probe.close()

    @classmethod
    def setUpClass(cls):
        import psycopg2
        from axonos_gate import x_capi_worker

        cls.psycopg2 = psycopg2
        cls._assert_disposable_database()
        cls.created_roles = []
        cls.guard_runtime = tempfile.TemporaryDirectory(prefix="xcapiguard-")
        os.chmod(cls.guard_runtime.name, 0o700)
        cls.guard_path = os.path.join(
            cls.guard_runtime.name, "config-guard.json"
        )
        cls.guard_path_patch = patch.object(
            x_capi_worker, "CONFIG_GUARD_ATTESTATION", cls.guard_path
        )
        cls.guard_path_patch.start()
        cls.worker_secret_patch = patch.dict(
            os.environ,
            {
                "X_CAPI_ALLOW_TEST_SECRETS": "1",
                "X_CAPI_ALLOW_TEST_DB_URL": "1",
                "X_CAPI_HASH_KEY": "postgres-test-hash-key-material-0001",
                "X_CAPI_CONTEXT_KEY": base64.urlsafe_b64encode(
                    b"postgres-context-key-material-01"
                ).decode("ascii"),
            },
            clear=False,
        )
        cls.worker_secret_patch.start()
        cls.schema = "xcapitest_" + uuid.uuid4().hex[:16]
        admin = psycopg2.connect(TEST_URL)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("CREATE SCHEMA " + cls.schema)
        admin.close()
        cls.options = "-c search_path=" + cls.schema
        conn = cls.connect()
        migration = _migration_text("001_x_capi_outbox.sql", cls.schema)
        with conn.cursor() as cur:
            cur.execute(migration)
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        admin = cls.psycopg2.connect(TEST_URL)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("DROP SCHEMA " + cls.schema + " CASCADE")
            for role in cls.created_roles:
                cur.execute("REASSIGN OWNED BY " + role + " TO CURRENT_USER")
                cur.execute("DROP OWNED BY " + role)
                cur.execute("DROP ROLE " + role)
        admin.close()
        cls.worker_secret_patch.stop()
        cls.guard_path_patch.stop()
        cls.guard_runtime.cleanup()

    @classmethod
    def connect(cls):
        return cls.psycopg2.connect(TEST_URL, options=cls.options)

    @classmethod
    def _restore_public_database_acl(cls, snapshot):
        admin = cls.psycopg2.connect(TEST_URL)
        admin.autocommit = True
        try:
            with admin.cursor() as cur:
                for database_name, public_connect, public_temporary in snapshot:
                    quoted = cls.psycopg2.extensions.quote_ident(
                        database_name, admin
                    )
                    cur.execute(
                        ("GRANT" if public_connect else "REVOKE")
                        + " CONNECT ON DATABASE " + quoted
                        + (" TO PUBLIC" if public_connect else " FROM PUBLIC")
                    )
                    if database_name == cls.database_name:
                        cur.execute(
                            ("GRANT" if public_temporary else "REVOKE")
                            + " TEMPORARY ON DATABASE " + quoted
                            + (
                                " TO PUBLIC"
                                if public_temporary else " FROM PUBLIC"
                            )
                        )
        finally:
            admin.close()

    def setUp(self):
        from axonos_gate import x_capi_worker

        for path in (
            self.guard_path,
            os.path.join(
                os.path.dirname(self.guard_path),
                x_capi_worker.CONFIG_GUARD_LOCK_NAME,
            ),
        ):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM x_capi_outbox")
            cur.execute("DELETE FROM x_capi_attribution_contexts")
            cur.execute("DELETE FROM x_capi_revocation_tombstones")
            cur.execute("DELETE FROM x_capi_dedup")
            cur.execute("DELETE FROM x_capi_counters")
            cur.execute("DELETE FROM x_capi_worker_state")
            cur.execute("DELETE FROM x_capi_config_guard")
            cur.execute(
                """UPDATE x_capi_capacity SET context_count=0,
                          tombstone_count=0,outbox_active_count=0,
                          revocation_saturated=FALSE,
                          revocation_saturated_until=0,
                          updated_at=0 WHERE singleton=TRUE"""
            )
            cur.execute(
                """INSERT INTO x_capi_worker_state
                   (singleton,lifecycle_state,lifecycle_token,ticket_not_before,
                    updated_at) VALUES(TRUE,'dirty',%s,0,0)""",
                (str(uuid.uuid4()),),
            )
        conn.commit()
        conn.close()

    def seed_context(self, cur, suffix="1"):
        context_id = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO x_capi_attribution_contexts
               (id,handle_hash,csrf_hash,consent_state,policy_version,twclid,
                consented_at,expires_at,first_seen_at,updated_at,mode_scope,
                policy_epoch,audience_scope,lifecycle_expires_at)
               VALUES(%s,%s,%s,'granted','v1','click_12345678',2,9999999999,
                      1,1,'live',%s,%s,9999999999)""",
            (context_id, suffix * 64, "c" * 64, POLICY_EPOCH, AUDIENCE_SCOPE),
        )
        cur.execute(
            """UPDATE x_capi_capacity SET context_count=context_count+1
                WHERE singleton=TRUE"""
        )
        return context_id

    def seed_outbox(self, cur, context_id, suffix="d", status="pending"):
        conversion_id = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO x_capi_outbox
               (conversion_id,milestone,source_key_hash,mode_scope,pixel_id,event_id,
                conversion_timestamp_ms,twclid,context_id,consent_policy_version,
                consent_policy_epoch,consent_audience_scope,
                attribution_expires_at,status,next_attempt_at,created_at,updated_at)
               VALUES(%s,'session_started',%s,'live','p','e',1,'click_12345678',%s,
                      'v1',%s,%s,9999999999,%s,1,1,1)""",
            (
                conversion_id, suffix * 64, context_id, POLICY_EPOCH,
                AUDIENCE_SCOPE, status,
            ),
        )
        cur.execute(
            """UPDATE x_capi_capacity
                  SET outbox_active_count=outbox_active_count+1
                WHERE singleton=TRUE"""
        )
        return conversion_id

    def worker_cfg(self, *, mapping=""):
        return SimpleNamespace(
            producer_ready=True, mode="live", policy_version="v1",
            policy_epoch=POLICY_EPOCH, audience_scope=AUDIENCE_SCOPE,
            deployment_id="test-deployment",
            max_event_age_hours=24,
            event_ids={"wallet_verified": mapping}, queue_limit=100,
            context_limit=100, pixel_id="pixel", twclid_charset="url_safe",
            twclid_min_length=8, twclid_max_length=256,
        )

    @contextmanager
    def fake_live_http_worker(self, responses, *, on_request=None):
        """Real DB/OS fences, synthetic credentials, and no network socket."""
        from axonos_gate import x_capi, x_capi_worker

        cfg = self.worker_cfg()
        cfg.pixel_id = "p"
        cfg.event_ids = {"session_started": "e"}
        cfg.errors = ()
        conn = self.connect()
        try:
            self.assertTrue(x_capi_worker.enforce_config_guard(conn, cfg, 9.0))
        finally:
            conn.close()
        captured = []
        connections = []
        http_connections = []
        pending_responses = iter(responses)
        synthetic_token = secrets.token_urlsafe(32)

        def connect_worker(*_args, **_kwargs):
            connection = self.connect()
            connections.append(connection)
            return connection

        def fake_https(*_args, **_kwargs):
            connection = MagicMock()
            http_connections.append(connection)
            response = next(pending_responses)

            def request(method, path, *, body, headers):
                connection.set_debuglevel.assert_called_once_with(0)
                captured.append((method, path, body, dict(headers)))
                # begin_dispatch must have committed before the HTTP boundary.
                self.assertEqual(
                    connections[-1].get_transaction_status(),
                    self.psycopg2.extensions.TRANSACTION_STATUS_IDLE,
                )
                if on_request is not None:
                    on_request()
                if isinstance(response, Exception):
                    raise response

            connection.request.side_effect = request
            if not isinstance(response, Exception):
                status, headers, body = response
                reply = MagicMock(status=status)
                reply.read.side_effect = lambda maximum: body[:maximum]
                reply.getheader.side_effect = lambda name: headers.get(name)
                connection.getresponse.return_value = reply
            return connection

        class FakeTransport:
            # The production activation block remains intact. This explicitly
            # injected wrapper exercises the concrete adapter only with the
            # test guard, mocked configuration, file reader, and HTTPS client.
            def send(self, pixel_id, token, payload):
                return x_capi_worker.RequestsTransport().send(
                    pixel_id, token, payload
                )

        with ExitStack() as stack:
            runtime = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="xcapifakehttp-")
            )
            fence = x_capi_worker.PrivacyFence(os.path.join(runtime, "privacy"))
            fence.open()
            stack.callback(fence.close)
            peer = x_capi_worker.PrivacyFence(fence.path)
            peer.open(create_controls=False)
            stack.callback(peer.close)
            for target, name, options in (
                (x_capi, "load_config", {"return_value": cfg}),
                (x_capi, "_db_url", {"return_value": TEST_URL}),
                (x_capi, "get_connection", {"side_effect": connect_worker}),
                (x_capi_worker, "_schema_ready", {"return_value": True}),
                (x_capi_worker, "read_token", {
                    "return_value": (synthetic_token, None)
                }),
                (x_capi_worker.http.client, "HTTPSConnection", {
                    "side_effect": fake_https
                }),
            ):
                stack.enter_context(patch.object(target, name, **options))

            def run(now):
                return x_capi_worker.run_once(
                    FakeTransport(), now_fn=lambda: now, rng=lambda: 0.5,
                    privacy_fence=fence, worker_id="fake-http-worker",
                    perform_cleanup=False,
                )

            yield SimpleNamespace(
                run=run, cfg=cfg, fence=fence, peer=peer, requests=captured,
                connections=http_connections,
            )

    def lifecycle_ticket(self, *, handle="h" * 43, csrf="c" * 43, state="granted"):
        click_id = "click_12345678"
        return {
            "v": 4, "handle": handle, "csrf": csrf, "state": state,
            "mode_scope": "live", "policy_version": "v1",
            "policy_epoch": POLICY_EPOCH, "audience_scope": AUDIENCE_SCOPE,
            "issued_at": 100.0, "lifecycle_expires_at": 10_000.0,
            "landing_commitment": hashlib.sha256(click_id.encode()).hexdigest(),
            "consented_at": 110.0 if state == "granted" else None,
            "expires_at": 10_000.0 if state == "granted" else None,
            "twclid": click_id if state == "granted" else None,
        }

    def seed_tombstones(self, cur, count, expires_at):
        cur.execute(
            """INSERT INTO x_capi_revocation_tombstones
               (handle_hash,csrf_hash,lifecycle_expires_at,created_at,updated_at)
               SELECT md5('handle-' || item::text) || md5('handle2-' || item::text),
                      md5('csrf-' || item::text) || md5('csrf2-' || item::text),
                      %s,1,1
                 FROM generate_series(1,%s) item""",
            (expires_at, count),
        )
        cur.execute(
            """UPDATE x_capi_capacity SET tombstone_count=%s
                WHERE singleton=TRUE""",
            (count,),
        )

    def test_unique_milestone_is_database_enforced(self):
        conn = self.connect()
        with conn.cursor() as cur:
            self.seed_context(cur)
            cur.execute(
                "INSERT INTO x_capi_dedup VALUES('deposit_completed',%s,1,9999999999)",
                ("d" * 64,),
            )
            with self.assertRaises(self.psycopg2.IntegrityError):
                cur.execute(
                    "INSERT INTO x_capi_dedup VALUES('deposit_completed',%s,2,9999999999)",
                    ("d" * 64,),
                )
        conn.rollback()
        conn.close()

    def test_local_listeners_are_singleton_and_credential_checked(self):
        from axonos_gate import x_capi_worker

        with tempfile.TemporaryDirectory(prefix="xcapisock-") as runtime:
            os.chmod(runtime, 0o700)
            event_path = os.path.join(runtime, "events.sock")
            consent_path = os.path.join(runtime, "consent.sock")
            events = x_capi_worker.IngestListener(
                event_path, allowed_uid=os.geteuid()
            )
            consent = x_capi_worker.ConsentListener(
                consent_path, allowed_uid=os.geteuid()
            )
            events.open()
            consent.open()
            try:
                duplicate = x_capi_worker.IngestListener(
                    event_path, allowed_uid=os.geteuid()
                )
                with self.assertRaisesRegex(RuntimeError, "already_running"):
                    duplicate.open()

                datagram = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    datagram.connect(event_path)
                    datagram.send(json.dumps({
                        "v": 1, "action": "revoke",
                        "context_token": "t" * 40,
                        "event_timestamp_ms": 200_000,
                    }).encode("ascii"))
                finally:
                    datagram.close()
                self.assertEqual(events.receive_batch()[0]["action"], "revoke")

                rpc = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                try:
                    rpc.connect(consent_path)
                    rpc.send(json.dumps({
                        "v": 1, "action": "consent", "operation": "revoke",
                        "context_token": "t" * 40,
                        "csrf_token": "c" * 40,
                        "request_timestamp_ms": 200_000,
                    }).encode("ascii"))
                    pending = consent.receive_batch()
                    self.assertEqual(len(pending), 1)
                    document, accepted = pending[0]
                    self.assertEqual(document["operation"], "revoke")
                    accepted.close()
                finally:
                    rpc.close()

                with patch.dict(os.environ, {
                    "X_CAPI_INGEST_SOCKET": event_path,
                    "X_CAPI_CONSENT_SOCKET": consent_path,
                }):
                    self.assertEqual(
                        x_capi_worker._ingest_runtime_state(),
                        (True, True, True),
                    )

                acceptance_fd = os.open(
                    event_path + x_capi_worker.INGEST_ACCEPTANCE_SUFFIX,
                    os.O_RDWR,
                )
                fcntl.flock(acceptance_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                queued = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    queued.connect(event_path)
                    # Every sender, including this ordinary bind rather than
                    # only a privacy hint, retains SH through send+close.  The
                    # worker therefore cannot cross its clean boundary while
                    # a preconnected producer can still publish a datagram.
                    self.assertFalse(events.stop_accepting())
                    queued.send(json.dumps({
                        "v": 1, "action": "bind",
                        "context_token": "q" * 40,
                        "wallet_address": "0x" + "1" * 40,
                        "event_timestamp_ms": 200_000,
                    }).encode("ascii"))
                finally:
                    queued.close()
                    fcntl.flock(acceptance_fd, fcntl.LOCK_UN)
                    os.close(acceptance_fd)
                self.assertTrue(events.stop_accepting())
                self.assertFalse(os.path.lexists(event_path))
                # The receive fd survives pathname removal, so work accepted
                # before the shutdown boundary is still durably drainable.
                self.assertEqual(events.receive_batch()[0]["action"], "bind")
                after = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    with self.assertRaises(OSError):
                        after.connect(event_path)
                finally:
                    after.close()
                after_acceptance = os.open(
                    event_path + x_capi_worker.INGEST_ACCEPTANCE_SUFFIX,
                    os.O_RDWR,
                )
                try:
                    with self.assertRaises((BlockingIOError, OSError)):
                        fcntl.flock(
                            after_acceptance,
                            fcntl.LOCK_SH | fcntl.LOCK_NB,
                        )
                finally:
                    os.close(after_acceptance)
            finally:
                consent.close()
                events.close()

    def test_dispatch_drain_proves_empty_and_prioritizes_late_revoke(self):
        from axonos_gate import x_capi_worker

        events = [
            {"action": "event", "milestone": "session_started"}
            for _unused in range(x_capi_worker.INGEST_BATCH_SIZE)
        ]
        revoke = {"action": "revoke"}

        class Listener:
            def __init__(self):
                self.calls = 0

            def _receive_batch_state(self, _limit):
                self.calls += 1
                if self.calls == 1:
                    return events, False
                return [revoke], True

        order = []
        listener = Listener()
        with patch.object(
            x_capi_worker,
            "process_ingest_envelope",
            side_effect=lambda _conn, item, _now: (
                order.append(item["action"]) or item["action"]
            ),
        ):
            outcomes, empty = x_capi_worker.drain_ingest_until_empty(
                listener, object(), 1.0
            )
        self.assertTrue(empty)
        self.assertEqual(order[0], "revoke")
        self.assertEqual(outcomes, {"revoke": 1, "event": len(events)})

        class NeverEmpty:
            def _receive_batch_state(self, limit):
                return ([{"action": "event"}] * limit, False)

        with patch.object(
            x_capi_worker, "process_ingest_envelope", return_value="event"
        ):
            _outcomes, empty = x_capi_worker.drain_ingest_until_empty(
                NeverEmpty(), object(), 1.0,
                limit=x_capi_worker.INGEST_BATCH_SIZE,
            )
        self.assertFalse(empty)

    def test_dirty_restart_preserves_pending_crash_before_send(self):
        from axonos_gate import x_capi_worker

        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "9")
            conversion_id = self.seed_outbox(cur, context_id, "9", "pending")
        conn.commit()
        # setUp leaves the prior lifecycle DIRTY. A replacement generation
        # records its own token but must not discard a job that crashed before
        # send or sever the attribution needed for its stable retry.
        lifecycle_token = str(uuid.uuid4())
        self.assertFalse(
            x_capi_worker.begin_worker_lifecycle(
                conn, lifecycle_token, 200.0
            )
        )
        with conn.cursor() as cur:
            cur.execute(
                """SELECT status,twclid,lease_owner,lease_token,lease_expires_at,
                          last_error_code FROM x_capi_outbox
                    WHERE conversion_id=%s""",
                (conversion_id,),
            )
            self.assertEqual(
                cur.fetchone(),
                (
                    "pending", "click_12345678", None, None, None, None,
                ),
            )
            cur.execute("SELECT outbox_active_count FROM x_capi_capacity")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute(
                """SELECT consent_state,twclid,wallet_hash,wallet_bound_at
                     FROM x_capi_attribution_contexts WHERE id=%s""",
                (context_id,),
            )
            self.assertEqual(
                cur.fetchone(), ("granted", "click_12345678", None, None)
            )
        conn.commit()
        conn.close()

    def test_privacy_fence_marker_wins_before_dispatch_boundary(self):
        from axonos_gate import x_capi_worker

        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "a")
            conversion_id = self.seed_outbox(cur, context_id, "a")
            cur.execute(
                """UPDATE x_capi_attribution_contexts
                      SET lifecycle_expires_at=10000,expires_at=10000
                    WHERE id=%s""",
                (context_id,),
            )
        conn.commit()
        with tempfile.TemporaryDirectory(prefix="xcapiprivacy-") as runtime:
            path = os.path.join(runtime, "privacy")
            fence = x_capi_worker.PrivacyFence(path)
            fence.open()
            try:
                self.assertEqual(
                    len(fence.pending_slot_fds),
                    x_capi_worker.PRIVACY_PENDING_SLOT_COUNT,
                )
                real_flock = x_capi_worker.fcntl.flock

                def reject_dispatch_lock(descriptor, operation):
                    if descriptor == fence.lock_fd:
                        raise AssertionError("healthy prepare took dispatch lock")
                    return real_flock(descriptor, operation)

                with patch.object(
                    x_capi_worker.fcntl, "flock",
                    side_effect=reject_dispatch_lock,
                ):
                    self.assertEqual(fence.prepare(conn, 200.0), "clear")
                marker = {
                    "v": 1,
                    "handle_hash": "a" * 64,
                    "csrf_hash": "c" * 64,
                    "lifecycle_expires_at": 10000.0,
                }
                marker_path = os.path.join(path, "a" * 64 + ".json")
                with open(marker_path, "x", encoding="ascii") as marker_file:
                    json.dump(
                        marker, marker_file, sort_keys=True, separators=(",", ":")
                    )
                os.chmod(marker_path, 0o600)
                with fence.dispatch_boundary() as acquired:
                    self.assertTrue(acquired)
                    self.assertEqual(fence.consume(conn, 200.0), "clear")
                self.assertFalse(os.path.exists(marker_path))
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT consent_state,twclid
                             FROM x_capi_attribution_contexts WHERE id=%s""",
                        (context_id,),
                    )
                    self.assertEqual(cur.fetchone(), ("revoked", None))
                    cur.execute(
                        """SELECT status,twclid,last_error_code
                             FROM x_capi_outbox WHERE conversion_id=%s""",
                        (conversion_id,),
                    )
                    self.assertEqual(
                        cur.fetchone(), ("cancelled", None, "local_privacy_fence")
                    )
                conn.commit()

                producer_lock = os.open(
                    os.path.join(path, x_capi_worker.PRIVACY_FENCE_LOCK_NAME),
                    os.O_RDWR,
                )
                try:
                    import fcntl

                    fcntl.flock(producer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with fence.dispatch_boundary() as acquired:
                        self.assertFalse(acquired)
                finally:
                    fcntl.flock(producer_lock, fcntl.LOCK_UN)
                    os.close(producer_lock)

                os.pwrite(
                    fence.global_fd, x_capi_worker.PRIVACY_GLOBAL_ACTIVE, 0
                )
                self.assertEqual(
                    fence.prepare(conn, 201.0), "privacy_global_quarantine"
                )
                self.assertEqual(
                    os.pread(fence.global_fd, 8, 0),
                    x_capi_worker.PRIVACY_GLOBAL_ACTIVE,
                )
            finally:
                fence.close()
        conn.close()

    def test_privacy_pending_slot_is_a_complete_targeted_revoke(self):
        from axonos_gate import x_capi_worker
        import fcntl

        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "a")
            conversion_id = self.seed_outbox(cur, context_id, "a")
            cur.execute(
                """UPDATE x_capi_attribution_contexts
                      SET lifecycle_expires_at=10000,expires_at=10000
                    WHERE id=%s""",
                (context_id,),
            )
        conn.commit()
        with tempfile.TemporaryDirectory(prefix="xcapislots-") as runtime:
            path = os.path.join(runtime, "privacy")
            fence = x_capi_worker.PrivacyFence(path)
            fence.open()
            slot_path = os.path.join(path, "pending-00")
            publisher_slot = os.open(slot_path, os.O_RDWR)
            try:
                fcntl.flock(
                    publisher_slot, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
                self.assertEqual(
                    os.pwrite(
                        publisher_slot,
                        b"X" * x_capi_worker.PRIVACY_PENDING_SLOT_RECORD_BYTES,
                        0,
                    ),
                    x_capi_worker.PRIVACY_PENDING_SLOT_RECORD_BYTES,
                )
                self.assertEqual(
                    fence.prepare(conn, 199.0), "privacy_fence_pending"
                )
                pending = (
                    b"P:" + (b"a" * 64) + (b"c" * 64)
                    + struct.pack("!d", 10000.0)
                )
                self.assertEqual(
                    os.pwrite(publisher_slot, pending, 0),
                    x_capi_worker.PRIVACY_PENDING_SLOT_RECORD_BYTES,
                )
                self.assertEqual(
                    fence.prepare(conn, 200.0), "privacy_fence_pending"
                )
                fcntl.flock(publisher_slot, fcntl.LOCK_UN)

                self.assertEqual(fence.prepare(conn, 200.0), "clear")
                with fence.dispatch_boundary() as acquired:
                    self.assertTrue(acquired)
                    self.assertEqual(
                        fence.consume(conn, 200.0), "privacy_fence_pending"
                    )
                self.assertEqual(
                    os.pread(
                        publisher_slot,
                        x_capi_worker.PRIVACY_PENDING_SLOT_RECORD_BYTES,
                        0,
                    ),
                    x_capi_worker.PRIVACY_PENDING_SLOT_INACTIVE,
                )
                with fence.dispatch_boundary() as acquired:
                    self.assertTrue(acquired)
                    self.assertEqual(fence.consume(conn, 200.0), "clear")
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT consent_state,twclid FROM "
                        "x_capi_attribution_contexts WHERE id=%s",
                        (context_id,),
                    )
                    self.assertEqual(cur.fetchone(), ("revoked", None))
                    cur.execute(
                        "SELECT status,twclid FROM x_capi_outbox "
                        "WHERE conversion_id=%s",
                        (conversion_id,),
                    )
                    self.assertEqual(cur.fetchone(), ("cancelled", None))
                conn.commit()

                # A short/invalid pwrite remains fail closed; unlike a complete
                # signed tuple it cannot safely become a broad cancellation.
                self.assertEqual(os.pwrite(publisher_slot, b"P:broken", 0), 8)
                self.assertEqual(
                    fence.prepare(conn, 201.0), "privacy_global_quarantine"
                )
            finally:
                try:
                    fcntl.flock(publisher_slot, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(publisher_slot)
                fence.close()
        conn.close()

    def test_business_gpc_fixed_slot_cancels_dry_run_after_worker_recovery(self):
        from axonos_gate import x_capi, x_capi_worker

        now = time.time()
        producer_env = {
            "X_CAPI_MODE": "dry_run",
            "X_CAPI_PIXEL_ID": "source-id",
            "X_CAPI_EVENT_WALLET_VERIFIED": "wallet-event",
            "X_CAPI_EVENT_DEPOSIT_COMPLETED": "deposit-event",
            "X_CAPI_EVENT_SESSION_STARTED": "session-event",
            "X_CAPI_ALLOWED_ORIGIN": "https://app.example",
            "X_CAPI_DEPLOYMENT_ID": "postgres-recovery-test",
            "X_CAPI_CONSENT_POLICY_VERSION": "v1",
            "X_CAPI_CONSENT_POLICY_EPOCH": str(POLICY_EPOCH),
            "X_CAPI_ATTRIBUTION_TTL_DAYS": "7",
            "X_CAPI_ALLOW_TEST_CONFIG_GUARD_BYPASS": "1",
        }
        with tempfile.TemporaryDirectory(prefix="xcapigpc-recovery-") as runtime:
            privacy_path = os.path.join(runtime, "privacy")
            fence = x_capi_worker.PrivacyFence(privacy_path)
            fence.open()
            producer_env.update({
                "X_CAPI_PRIVACY_FENCE_DIR": privacy_path,
                "X_CAPI_ALLOW_TEST_PRIVACY_FENCE": "1",
                "X_CAPI_WORKER_UID": str(os.geteuid()),
            })
            try:
                with patch.dict(os.environ, producer_env, clear=False):
                    issued = x_capi.attribution_status(
                        landing_twclid="officialclick1234567890"
                    )
                    granted, code = x_capi.update_consent(
                        context_token=issued["context"],
                        csrf_token=issued["csrf"],
                        action="grant",
                        landing_twclid="officialclick1234567890",
                        origin="https://app.example",
                    )
                    self.assertEqual(code, 200)
                    ticket = x_capi.decode_context_ticket(granted["context"])
                self.assertIsNotNone(ticket)

                context_id = str(uuid.uuid4())
                conn = self.connect()
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO x_capi_attribution_contexts
                           (id,handle_hash,csrf_hash,consent_state,policy_version,
                            twclid,consented_at,expires_at,first_seen_at,updated_at,
                            mode_scope,policy_epoch,audience_scope,
                            lifecycle_expires_at)
                           VALUES(%s,%s,%s,'granted',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            context_id,
                            hashlib.sha256(ticket["handle"].encode("ascii")).hexdigest(),
                            hashlib.sha256(ticket["csrf"].encode("ascii")).hexdigest(),
                            ticket["policy_version"],
                            ticket["twclid"],
                            ticket["consented_at"],
                            ticket["expires_at"],
                            now,
                            now,
                            ticket["mode_scope"],
                            ticket["policy_epoch"],
                            ticket["audience_scope"],
                            ticket["lifecycle_expires_at"],
                        ),
                    )
                    cur.execute(
                        "UPDATE x_capi_capacity SET context_count=context_count+1 "
                        "WHERE singleton=TRUE"
                    )
                    conversion_id = self.seed_outbox(
                        cur, context_id, "b", status="dry_run"
                    )
                    cur.execute(
                        """UPDATE x_capi_outbox
                           SET mode_scope=%s,consent_policy_version=%s,
                               consent_policy_epoch=%s,consent_audience_scope=%s
                           WHERE conversion_id=%s""",
                        (
                            ticket["mode_scope"],
                            ticket["policy_version"],
                            ticket["policy_epoch"],
                            ticket["audience_scope"],
                            conversion_id,
                        ),
                    )
                conn.commit()

                # The worker/socket is conceptually unavailable: only the
                # producer's preallocated shared control is allowed to succeed.
                with patch.dict(os.environ, producer_env, clear=False), patch.object(
                    x_capi, "_emit_revocation_hint_nonblocking", return_value=False
                ):
                    self.assertTrue(
                        x_capi.observe_business_gpc_nonblocking(
                            granted["context"]
                        )
                    )

                self.assertEqual(fence.prepare(conn, now + 1), "clear")
                with fence.dispatch_boundary() as acquired:
                    self.assertTrue(acquired)
                    self.assertEqual(
                        fence.consume(conn, now + 1), "privacy_fence_pending"
                    )
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT consent_state,twclid FROM "
                        "x_capi_attribution_contexts WHERE id=%s",
                        (context_id,),
                    )
                    self.assertEqual(cur.fetchone(), ("revoked", None))
                    cur.execute(
                        "SELECT status,twclid,last_error_code FROM "
                        "x_capi_outbox WHERE conversion_id=%s",
                        (conversion_id,),
                    )
                    self.assertEqual(
                        cur.fetchone(),
                        ("cancelled", None, "local_privacy_fence"),
                    )
                conn.commit()
                conn.close()
            finally:
                fence.close()

    def test_consent_service_uses_its_own_connection_and_commits_close(self):
        from axonos_gate import x_capi, x_capi_worker

        now = time.time()
        ticket = self.lifecycle_ticket(state="unset")
        ticket["issued_at"] = now - 10
        ticket["lifecycle_expires_at"] = now + 1_000
        request = {
            "v": 1, "action": "consent", "operation": "decline",
            "context_token": "t" * 40, "csrf_token": ticket["csrf"],
            "request_timestamp_ms": int(now * 1000),
        }
        with tempfile.TemporaryDirectory(prefix="xcapiconsent-") as runtime:
            os.chmod(runtime, 0o700)
            privacy_path = os.path.join(runtime, "privacy")
            privacy = x_capi_worker.PrivacyFence(privacy_path)
            privacy.open()
            self.assertTrue(privacy.activate_global_quarantine())
            privacy.close()
            consent = x_capi_worker.ConsentListener(
                os.path.join(runtime, "consent.sock"),
                allowed_uid=os.geteuid(),
            )
            consent.open()
            service = x_capi_worker.ConsentService(
                consent, privacy_path=privacy_path
            )
            try:
                with patch.object(
                    x_capi_worker, "_worker_db_target_is_isolated",
                    return_value=True,
                ), patch.object(
                    x_capi, "get_connection",
                    side_effect=lambda **_unused: self.connect(),
                ), patch.object(
                    x_capi, "load_config", return_value=self.worker_cfg(),
                ), patch.object(
                    x_capi_worker, "_schema_ready", return_value=True,
                ), patch.object(
                    x_capi_worker, "_ticket_for_ingest", return_value=ticket,
                ):
                    service.start()
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                    try:
                        client.settimeout(2)
                        client.connect(consent.path)
                        client.send(json.dumps(request).encode("ascii"))
                        response = json.loads(client.recv(4096).decode("ascii"))
                    finally:
                        client.close()
                    self.assertEqual(
                        response, {"v": 1, "ok": True, "state": "denied"}
                    )
                    for forbidden_operation, expected_error in (
                        ("new_lifecycle", "lock_unavailable"),
                        ("grant", "invalid_request"),
                    ):
                        rejected = dict(request)
                        rejected["operation"] = forbidden_operation
                        rejected["request_timestamp_ms"] = int(time.time() * 1000)
                        client = socket.socket(
                            socket.AF_UNIX, socket.SOCK_SEQPACKET
                        )
                        try:
                            client.settimeout(2)
                            client.connect(consent.path)
                            client.send(json.dumps(rejected).encode("ascii"))
                            denied_response = json.loads(
                                client.recv(4096).decode("ascii")
                            )
                        finally:
                            client.close()
                        self.assertEqual(
                            denied_response,
                            {"v": 1, "ok": False, "error": expected_error},
                        )
            finally:
                self.assertTrue(service.stop())
                consent.close()
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_revocation_tombstones")
            self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    def test_fresh_migration_refuses_to_merge_with_existing_schema(self):
        conn = self.connect()
        migration = _migration_text("001_x_capi_outbox.sql", self.schema)
        with conn.cursor() as cur, self.assertRaises(self.psycopg2.Error):
            cur.execute(migration)
        conn.rollback()
        conn.close()

    def test_skip_locked_prevents_double_claim(self):
        setup = self.connect()
        with setup.cursor() as cur:
            context_id = self.seed_context(cur, "2")
            self.seed_outbox(cur, context_id, "e")
        setup.commit()
        setup.close()
        first, second = self.connect(), self.connect()
        with first.cursor() as cur1, second.cursor() as cur2:
            cur1.execute("SELECT conversion_id FROM x_capi_outbox FOR UPDATE SKIP LOCKED LIMIT 1")
            self.assertIsNotNone(cur1.fetchone())
            cur2.execute("SELECT conversion_id FROM x_capi_outbox FOR UPDATE SKIP LOCKED LIMIT 1")
            self.assertIsNone(cur2.fetchone())
        first.rollback(); second.rollback(); first.close(); second.close()

    def test_claim_job_commits_an_expiring_lease(self):
        from axonos_gate import x_capi_worker

        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "4")
            conversion_id = self.seed_outbox(cur, context_id, "b")
        conn.commit()
        job = x_capi_worker.claim_job(conn, "integration-worker", 10.0, lease_seconds=30)
        self.assertEqual(str(job["conversion_id"]), conversion_id)
        self.assertEqual(job["lease_owner"], "integration-worker")
        self.assertRegex(str(job["lease_token"]), r"^[0-9a-f-]{36}$")
        with conn.cursor() as cur:
            cur.execute(
                """SELECT status, lease_owner, lease_token, lease_expires_at
                   FROM x_capi_outbox WHERE conversion_id=%s""",
                (conversion_id,),
            )
            row = cur.fetchone()
            self.assertEqual(row[0], "leased")
            self.assertEqual(row[1], "integration-worker")
            self.assertEqual(str(row[2]), str(job["lease_token"]))
            self.assertEqual(row[3], 40.0)
        conn.close()

    def test_terminal_completion_releases_o1_queue_capacity(self):
        from axonos_gate import x_capi_worker

        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "6")
            conversion_id = self.seed_outbox(cur, context_id, "6")
        conn.commit()
        job = x_capi_worker.claim_job(conn, "capacity-worker", 10.0, 30)
        self.assertEqual(str(job["conversion_id"]), conversion_id)
        self.assertTrue(x_capi_worker.finish_job(
            conn, job, {"action": "accepted", "code": "accepted"}, 11.0
        ))
        with conn.cursor() as cur:
            cur.execute("SELECT outbox_active_count FROM x_capi_capacity")
            self.assertEqual(cur.fetchone()[0], 0)
        # A stale duplicate completion cannot decrement the singleton twice.
        self.assertFalse(x_capi_worker.finish_job(
            conn, job, {"action": "accepted", "code": "accepted"}, 12.0
        ))
        with conn.cursor() as cur:
            cur.execute("SELECT outbox_active_count FROM x_capi_capacity")
            self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_crash_after_ambiguous_x_acceptance_reuses_stable_id_and_timestamp(self):
        from axonos_gate import x_capi_worker

        setup = self.connect()
        with setup.cursor() as cur:
            context_id = self.seed_context(cur, "5")
            conversion_id = self.seed_outbox(cur, context_id, "c")
        setup.commit()
        cfg = self.worker_cfg()
        self.assertTrue(x_capi_worker.enforce_config_guard(setup, cfg, 9.0))
        setup.close()

        first_conn = self.connect()
        first = x_capi_worker.claim_job(first_conn, "worker-a", 10.0, lease_seconds=5)
        self.assertEqual(
            x_capi_worker.begin_dispatch(
                first_conn, first, 11.0, "v1", 24, POLICY_EPOCH,
                AUDIENCE_SCOPE, x_capi_worker._deployment_id_hash(cfg),
            ),
            "allowed",
        )
        first_payload = x_capi_worker.build_payload(first)
        # Model process death after X may have accepted the request but before
        # the worker could commit finish_job. The expired lease is the only
        # durable evidence, so retry is at-least-once with identical identity.
        second_conn = self.connect()
        second = x_capi_worker.claim_job(
            second_conn, "worker-b", 57.0, lease_seconds=30
        )
        self.assertEqual(str(first["conversion_id"]), conversion_id)
        self.assertEqual(str(second["conversion_id"]), conversion_id)
        self.assertNotEqual(str(first["lease_token"]), str(second["lease_token"]))
        self.assertEqual(
            x_capi_worker.build_payload(second), first_payload
        )

        self.assertFalse(x_capi_worker.finish_job(
            first_conn, first, {"action": "accepted", "code": "accepted"}, 58.0
        ))
        with second_conn.cursor() as cur:
            cur.execute(
                "SELECT status,lease_owner,lease_token FROM x_capi_outbox WHERE conversion_id=%s",
                (conversion_id,),
            )
            row = cur.fetchone()
        self.assertEqual(row[0:2], ("leased", "worker-b"))
        self.assertEqual(str(row[2]), str(second["lease_token"]))
        second_conn.commit()
        self.assertTrue(x_capi_worker.finish_job(
            second_conn, second, {"action": "retry", "code": "synthetic"},
            59.0, rng=lambda: 0.5,
        ))
        first_conn.close(); second_conn.close()

    def test_fake_http_rate_limit_retries_identical_wire_then_scrubs_click(self):
        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "b")
            conversion_id = self.seed_outbox(cur, context_id, "b")
            cur.execute(
                "UPDATE x_capi_outbox SET conversion_timestamp_ms=10123 "
                "WHERE conversion_id=%s", (conversion_id,),
            )
        conn.commit()
        with self.fake_live_http_worker([
            (429, {"Retry-After": "7"}, b"rate limited"),
            (200, {}, b'{"data":{"conversions_processed":1}}'),
        ]) as worker:
            self.assertEqual(worker.run(20.0), "retry")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status,attempt_count,next_attempt_at,twclid "
                    "FROM x_capi_outbox WHERE conversion_id=%s",
                    (conversion_id,),
                )
                self.assertEqual(
                    cur.fetchone(), ("retrying", 1, 27.0, "click_12345678")
                )
            conn.commit()
            self.assertEqual(worker.run(26.0), "idle")
            self.assertEqual(len(worker.requests), 1)
            self.assertEqual(worker.run(27.0), "accepted")
            self.assertEqual(len(worker.requests), 2)
            self.assertEqual(worker.requests[0], worker.requests[1])
            method, path, body, headers = worker.requests[0]
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/12/measurement/conversions/p")
            self.assertEqual(set(headers), {"Content-Type", "X-Pixel-Token"})
            self.assertEqual(json.loads(body), {"conversions": [{
                "conversion_timestamp": 10123,
                "event_id": "e",
                "identifiers": [{"twclid": "click_12345678"}],
                "conversion_id": conversion_id,
            }]})
            for connection in worker.connections:
                connection.close.assert_called_once()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,twclid,lease_owner,lease_token,accepted_at "
                "FROM x_capi_outbox WHERE conversion_id=%s", (conversion_id,),
            )
            self.assertEqual(cur.fetchone(), ("accepted", None, None, None, 27.0))
            cur.execute("SELECT outbox_active_count FROM x_capi_capacity")
            self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_fake_http_retry_budget_exhausts_by_event_age_not_attempt_count(self):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                context_id = self.seed_context(cur, "c")
                conversion_id = self.seed_outbox(cur, context_id, "c")
                cur.execute(
                    "UPDATE x_capi_outbox SET attempt_count=100 "
                    "WHERE conversion_id=%s", (conversion_id,),
                )
            conn.commit()
            with self.fake_live_http_worker([
                (429, {"Retry-After": "1"}, b"synthetic rate limit"),
            ]) as worker:
                # A fixed attempt ceiling is not this implementation's policy.
                # The immutable event time, however, must stop retries even if
                # retention cleanup has not run and Retry-After is very short.
                self.assertEqual(worker.run(86400.0), "retry")
                self.assertEqual(worker.run(86401.0), "event_too_old")
                self.assertEqual(len(worker.requests), 1)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status,attempt_count,twclid,conversion_timestamp_ms,"
                    "last_error_code FROM x_capi_outbox WHERE conversion_id=%s",
                    (conversion_id,),
                )
                self.assertEqual(
                    cur.fetchone(), ("cancelled", 101, None, 1, "event_too_old")
                )
        finally:
            conn.close()

    def test_fake_http_reflected_click_is_not_retained_as_debug_id(self):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                context_id = self.seed_context(cur, "c")
                conversion_id = self.seed_outbox(cur, context_id, "c")
                # Match the previously accepted response-ID grammar exactly.
                cur.execute(
                    "UPDATE x_capi_attribution_contexts SET twclid='click12345678' "
                    "WHERE id=%s", (context_id,),
                )
                cur.execute(
                    "UPDATE x_capi_outbox SET twclid='click12345678' "
                    "WHERE conversion_id=%s", (conversion_id,),
                )
            conn.commit()
            with self.fake_live_http_worker([
                (200, {}, b'{"data":{"conversions_processed":1,"debug_id":"click12345678"}}'),
            ]) as worker:
                self.assertEqual(worker.run(20.0), "accepted")
                self.assertEqual(len(worker.requests), 1)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status,twclid,safe_debug_id FROM x_capi_outbox "
                    "WHERE conversion_id=%s", (conversion_id,),
                )
                self.assertEqual(cur.fetchone(), ("accepted", None, None))
        finally:
            conn.close()

    def test_fake_http_retry_cannot_outlive_attribution_even_without_cleanup(self):
        from axonos_gate import x_capi_worker

        conn = self.connect()
        try:
            with conn.cursor() as cur:
                context_id = self.seed_context(cur, "c")
                conversion_id = self.seed_outbox(cur, context_id, "c")
                cur.execute(
                    "UPDATE x_capi_attribution_contexts SET expires_at=21,"
                    "lifecycle_expires_at=21 WHERE id=%s", (context_id,),
                )
                cur.execute(
                    "UPDATE x_capi_outbox SET attribution_expires_at=21 "
                    "WHERE conversion_id=%s", (conversion_id,),
                )
            conn.commit()
            with self.fake_live_http_worker([
                (429, {"Retry-After": "1"}, b"synthetic rate limit"),
            ]) as worker:
                self.assertEqual(worker.run(20.0), "retry")
                self.assertEqual(worker.run(21.0), "idle")
                self.assertEqual(len(worker.requests), 1)
                x_capi_worker.expire_and_cleanup(
                    conn, 21.0, 24, "v1", policy_epoch=POLICY_EPOCH,
                    audience_scope=AUDIENCE_SCOPE,
                )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status,twclid FROM x_capi_outbox WHERE conversion_id=%s",
                    (conversion_id,),
                )
                self.assertEqual(cur.fetchone(), ("expired", None))
        finally:
            conn.close()

    def test_fake_http_timeout_retries_same_conversion_and_timestamp(self):
        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "c")
            conversion_id = self.seed_outbox(cur, context_id, "c")
        conn.commit()
        with self.fake_live_http_worker([
            TimeoutError("synthetic transport timeout"),
            (200, {}, b'{"data":{"conversions_processed":1}}'),
        ]) as worker:
            self.assertEqual(worker.run(20.0), "retry")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status,attempt_count,next_attempt_at,last_error_code "
                    "FROM x_capi_outbox WHERE conversion_id=%s",
                    (conversion_id,),
                )
                self.assertEqual(
                    cur.fetchone(), ("retrying", 1, 30.0, "transport_failure")
                )
            conn.commit()
            self.assertEqual(worker.run(30.0), "accepted")
            self.assertEqual(len(worker.requests), 2)
            self.assertEqual(worker.requests[0][2], worker.requests[1][2])
            conversion = json.loads(worker.requests[0][2])["conversions"][0]
            self.assertEqual(conversion["conversion_id"], conversion_id)
            self.assertEqual(conversion["conversion_timestamp"], 1)
            for connection in worker.connections:
                connection.close.assert_called_once()
        conn.close()

    def test_fake_http_late_privacy_marker_prevents_request(self):
        from axonos_gate import x_capi_worker

        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "a")
            conversion_id = self.seed_outbox(cur, context_id, "a")
            cur.execute(
                "UPDATE x_capi_attribution_contexts SET expires_at=10000, "
                "lifecycle_expires_at=10000 WHERE id=%s", (context_id,),
            )
        conn.commit()
        with self.fake_live_http_worker([]) as worker:
            def publish_after_prepare(*_args):
                marker = {
                    "v": 1, "handle_hash": "a" * 64, "csrf_hash": "c" * 64,
                    "lifecycle_expires_at": 10000.0,
                }
                marker_path = os.path.join(worker.fence.path, "a" * 64 + ".json")
                with open(marker_path, "x", encoding="ascii") as output:
                    json.dump(marker, output, separators=(",", ":"))
                os.chmod(marker_path, 0o600)
                return {}, True

            with patch.object(
                x_capi_worker, "drain_ingest_until_empty",
                side_effect=publish_after_prepare,
            ):
                self.assertEqual(worker.run(20.0), "idle")
            self.assertEqual(worker.requests, [])
            self.assertEqual(worker.connections, [])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,twclid,last_error_code FROM x_capi_outbox "
                "WHERE conversion_id=%s", (conversion_id,),
            )
            self.assertEqual(
                cur.fetchone(), ("cancelled", None, "local_privacy_fence")
            )
        conn.close()

    def test_fake_http_stale_lease_before_send_never_opens_http(self):
        from axonos_gate import x_capi_worker

        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "d")
            conversion_id = self.seed_outbox(cur, context_id, "d")
        conn.commit()
        replacement_token = str(uuid.uuid4())
        begin_dispatch = x_capi_worker.begin_dispatch

        def replace_claim(connection, job, *args):
            # Model a stale local job against a newer durable claim token.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE x_capi_outbox SET lease_owner='replacement', "
                    "lease_token=%s WHERE conversion_id=%s",
                    (replacement_token, conversion_id),
                )
            conn.commit()
            return begin_dispatch(connection, job, *args)

        with self.fake_live_http_worker([]) as worker, patch.object(
            x_capi_worker, "begin_dispatch", side_effect=replace_claim
        ):
            self.assertEqual(worker.run(20.0), "lease_lost")
            self.assertEqual(worker.requests, [])
            self.assertEqual(worker.connections, [])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,lease_owner,lease_token FROM x_capi_outbox "
                "WHERE conversion_id=%s", (conversion_id,),
            )
            row = cur.fetchone()
            self.assertEqual(row[:2], ("leased", "replacement"))
            self.assertEqual(str(row[2]), replacement_token)
            cur.execute("SELECT count(*) FROM x_capi_counters")
            self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_fake_http_stale_acceptance_cannot_ack_replacement_lease(self):
        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "e")
            conversion_id = self.seed_outbox(cur, context_id, "e")
        conn.commit()
        replacement_token = str(uuid.uuid4())

        def replace_before_response():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE x_capi_outbox SET lease_owner='replacement', "
                    "lease_token=%s WHERE conversion_id=%s",
                    (replacement_token, conversion_id),
                )
            conn.commit()

        with self.fake_live_http_worker(
            [(200, {}, b'{"data":{"conversions_processed":1}}')],
            on_request=replace_before_response,
        ) as worker:
            self.assertEqual(worker.run(20.0), "lease_lost")
            self.assertEqual(len(worker.requests), 1)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,lease_owner,lease_token,accepted_at,twclid "
                "FROM x_capi_outbox WHERE conversion_id=%s", (conversion_id,),
            )
            row = cur.fetchone()
            self.assertEqual(row[:2], ("leased", "replacement"))
            self.assertEqual(str(row[2]), replacement_token)
            self.assertEqual(row[3:], (None, "click_12345678"))
            cur.execute("SELECT outbox_active_count FROM x_capi_capacity")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT count(*) FROM x_capi_counters")
            self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_fake_http_keeps_privacy_lock_until_response_and_completion(self):
        from axonos_gate import x_capi_worker

        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "f")
            self.seed_outbox(cur, context_id, "f")
        conn.commit()
        conn.close()
        observations = []

        def assert_inflight_boundary():
            with worker.peer.dispatch_boundary() as acquired:
                observations.append(acquired)
            request = {
                "v": 1, "action": "consent", "operation": "revoke",
                "context_token": "t" * 40, "csrf_token": "c" * 43,
                "request_timestamp_ms": 20_000,
            }
            with patch.object(x_capi_worker.time, "time", return_value=20.0):
                result = x_capi_worker.ConsentService(None)._process(
                    request, worker.peer
                )
            self.assertEqual(
                result, {"v": 1, "ok": False, "error": "lock_unavailable"}
            )

        with self.fake_live_http_worker(
            [(200, {}, b'{"data":{"conversions_processed":1}}')],
            on_request=assert_inflight_boundary,
        ) as worker:
            self.assertEqual(worker.run(20.0), "accepted")
            self.assertEqual(observations, [False])
            with worker.peer.dispatch_boundary() as acquired:
                self.assertTrue(acquired)

    def test_clean_and_dirty_lifecycle_transitions_do_not_mutate_work(self):
        from axonos_gate import x_capi_worker

        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "d")
            conversion_id = self.seed_outbox(cur, context_id, "1")
        conn.commit()
        first_token = str(uuid.uuid4())
        # setUp seeds DIRTY, modeling SIGKILL/OOM. Complete targeted P records
        # are the privacy authority; lifecycle metadata must never replace the
        # stable-id retry protocol by deleting unrelated queued work.
        self.assertFalse(
            x_capi_worker.begin_worker_lifecycle(conn, first_token, 10.0)
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,twclid FROM x_capi_outbox WHERE conversion_id=%s",
                (conversion_id,),
            )
            self.assertEqual(cur.fetchone(), ("pending", "click_12345678"))
            cur.execute(
                "SELECT consent_state,twclid,wallet_hash FROM "
                "x_capi_attribution_contexts WHERE id=%s",
                (context_id,),
            )
            self.assertEqual(
                cur.fetchone(), ("granted", "click_12345678", None)
            )
        self.assertTrue(
            x_capi_worker.mark_worker_lifecycle_clean(conn, first_token, 11.0)
        )
        with conn.cursor() as cur:
            clean_context_id = self.seed_context(cur, "e")
            clean_conversion_id = self.seed_outbox(cur, clean_context_id, "3")
        conn.commit()
        second_token = str(uuid.uuid4())
        self.assertTrue(
            x_capi_worker.begin_worker_lifecycle(conn, second_token, 12.0)
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,conversion_id FROM x_capi_outbox WHERE conversion_id=%s",
                (clean_conversion_id,),
            )
            self.assertEqual(
                cur.fetchone(), ("pending", clean_conversion_id)
            )
        conn.close()

    def test_os_dispatch_fence_prevents_stopped_worker_reclaim_before_send(self):
        from axonos_gate import x_capi_worker

        first = self.connect()
        with first.cursor() as cur:
            context_id = self.seed_context(cur, "f")
            conversion_id = self.seed_outbox(cur, context_id, "2")
        first.commit()
        cfg = self.worker_cfg()
        self.assertTrue(x_capi_worker.enforce_config_guard(first, cfg, 9.0))
        job = x_capi_worker.claim_job(first, "worker-a", 10.0, 5)
        self.assertEqual(
            x_capi_worker.begin_dispatch(
                first, job, 11.0, "v1", 24, POLICY_EPOCH,
                AUDIENCE_SCOPE, x_capi_worker._deployment_id_hash(cfg),
            ),
            "allowed",
        )
        with tempfile.TemporaryDirectory(prefix="xcapistop-") as runtime:
            path = os.path.join(runtime, "privacy")
            stopped = x_capi_worker.PrivacyFence(path)
            peer = x_capi_worker.PrivacyFence(path)
            stopped.open()
            peer.open(create_controls=False)
            try:
                with stopped.dispatch_boundary() as acquired:
                    self.assertTrue(acquired)
                    with peer.dispatch_boundary() as peer_acquired:
                        self.assertFalse(peer_acquired)
                with peer.dispatch_boundary() as peer_acquired:
                    self.assertTrue(peer_acquired)
                    peer_conn = self.connect()
                    try:
                        reclaimed = x_capi_worker.claim_job(
                            peer_conn, "worker-b", 100.0, 30
                        )
                        self.assertEqual(
                            str(reclaimed["conversion_id"]), conversion_id
                        )
                        self.assertNotEqual(
                            str(reclaimed["lease_token"]), str(job["lease_token"])
                        )
                    finally:
                        peer_conn.close()
            finally:
                peer.close()
                stopped.close()
        first.close()

    def test_two_workers_racing_one_conversion_only_one_claims(self):
        from axonos_gate import x_capi_worker

        setup = self.connect()
        with setup.cursor() as cur:
            context_id = self.seed_context(cur, "6")
            self.seed_outbox(cur, context_id, "f")
        setup.commit(); setup.close()
        barrier = threading.Barrier(2)
        claims = []
        failures = []

        def claim(worker):
            conn = self.connect()
            try:
                barrier.wait(timeout=5)
                claims.append(x_capi_worker.claim_job(conn, worker, 10.0, 30))
            except Exception as exc:
                failures.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=claim, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(failures)
        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_committed_revocation_fences_dispatch(self):
        from axonos_gate import x_capi_worker

        ticket = self.lifecycle_ticket()
        conn = self.connect()
        with conn.cursor() as cur:
            context_id = self.seed_context(cur, "7")
            conversion_id = self.seed_outbox(cur, context_id, "7")
            cur.execute(
                """UPDATE x_capi_attribution_contexts
                      SET handle_hash=%s,csrf_hash=%s,first_seen_at=100,
                          consented_at=110,expires_at=10000,
                          lifecycle_expires_at=10000 WHERE id=%s""",
                (
                    hashlib.sha256(ticket["handle"].encode()).hexdigest(),
                    hashlib.sha256(ticket["csrf"].encode()).hexdigest(),
                    context_id,
                ),
            )
            cur.execute(
                """UPDATE x_capi_outbox SET conversion_timestamp_ms=200000,
                          attribution_expires_at=10000
                    WHERE conversion_id=%s""",
                (conversion_id,),
            )
        conn.commit()
        cfg = self.worker_cfg()
        self.assertTrue(x_capi_worker.enforce_config_guard(conn, cfg, 199.0))
        job = x_capi_worker.claim_job(conn, "worker-a", 200.0, 30)
        revoke = self.connect()
        request = {
            "v": 1, "action": "consent", "operation": "revoke",
            "context_token": "t" * 40, "csrf_token": ticket["csrf"],
            "request_timestamp_ms": 201_000,
        }
        with patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=ticket
        ):
            response = x_capi_worker.process_consent_request(
                revoke, request, 201.0
            )
        self.assertEqual(response, {"v": 1, "ok": True, "state": "revoked"})
        revoke.close()
        self.assertEqual(
            x_capi_worker.begin_dispatch(
                conn, job, 202.0, "v1", 24, POLICY_EPOCH,
                AUDIENCE_SCOPE, x_capi_worker._deployment_id_hash(cfg),
            ),
            "lease_lost",
        )
        conn.close()

    def test_dispatch_lock_makes_concurrent_revoke_fail_not_claim_success(self):
        from axonos_gate import x_capi_worker

        ticket = self.lifecycle_ticket()
        first = self.connect()
        with first.cursor() as cur:
            context_id = self.seed_context(cur, "3")
            conversion_id = self.seed_outbox(cur, context_id, "3")
            cur.execute(
                """UPDATE x_capi_attribution_contexts
                      SET handle_hash=%s,csrf_hash=%s,first_seen_at=100,
                          consented_at=110,expires_at=10000,
                          lifecycle_expires_at=10000 WHERE id=%s""",
                (
                    hashlib.sha256(ticket["handle"].encode()).hexdigest(),
                    hashlib.sha256(ticket["csrf"].encode()).hexdigest(),
                    context_id,
                ),
            )
            cur.execute(
                """UPDATE x_capi_outbox SET conversion_timestamp_ms=200000,
                          attribution_expires_at=10000
                    WHERE conversion_id=%s""",
                (conversion_id,),
            )
        first.commit()
        cfg = self.worker_cfg()
        self.assertTrue(x_capi_worker.enforce_config_guard(first, cfg, 199.0))
        job = x_capi_worker.claim_job(first, "worker-a", 200.0, 30)
        self.assertEqual(
            x_capi_worker.begin_dispatch(
                first, job, 201.0, "v1", 24, POLICY_EPOCH,
                AUDIENCE_SCOPE, x_capi_worker._deployment_id_hash(cfg),
            ),
            "allowed",
        )

        request = {
            "v": 1, "action": "consent", "operation": "revoke",
            "context_token": "t" * 40, "csrf_token": ticket["csrf"],
            "request_timestamp_ms": 202_000,
        }
        revoke = self.connect()
        with tempfile.TemporaryDirectory(prefix="xcapidispatch-") as runtime:
            path = os.path.join(runtime, "privacy")
            sending_fence = x_capi_worker.PrivacyFence(path)
            consent_fence = x_capi_worker.PrivacyFence(path)
            sending_fence.open()
            consent_fence.open(create_controls=False)
            try:
                with sending_fence.dispatch_boundary() as sending:
                    self.assertTrue(sending)
                    service = x_capi_worker.ConsentService(None)
                    with patch.object(
                        x_capi_worker.time, "time", return_value=202.0
                    ):
                        self.assertEqual(
                            service._process(request, consent_fence),
                            {
                                "v": 1, "ok": False,
                                "error": "lock_unavailable",
                            },
                        )
            finally:
                consent_fence.close()
                sending_fence.close()

        # Once the complete outbound OS boundary is released, the same
        # capability closes durably.
        with patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=ticket
        ):
            response = x_capi_worker.process_consent_request(
                revoke, request, 202.0
            )
        self.assertEqual(response, {"v": 1, "ok": True, "state": "revoked"})
        with revoke.cursor() as cur:
            cur.execute(
                "SELECT status,twclid FROM x_capi_outbox WHERE conversion_id=%s",
                (conversion_id,),
            )
            self.assertEqual(cur.fetchone(), ("cancelled", None))
        first.close(); revoke.close()

    def test_dispatch_lock_prevents_reclaim_even_after_lease_clock_expires(self):
        from axonos_gate import x_capi_worker

        first = self.connect()
        with first.cursor() as cur:
            context_id = self.seed_context(cur, "8")
            self.seed_outbox(cur, context_id, "8")
        first.commit()
        cfg = self.worker_cfg()
        self.assertTrue(x_capi_worker.enforce_config_guard(first, cfg, 9.0))
        job = x_capi_worker.claim_job(first, "worker-a", 10.0, 5)
        self.assertEqual(
            x_capi_worker.begin_dispatch(
                first, job, 11.0, "v1", 24, POLICY_EPOCH,
                AUDIENCE_SCOPE, x_capi_worker._deployment_id_hash(cfg),
            ),
            "allowed",
        )
        second = self.connect()
        reclaimed = x_capi_worker.claim_job(second, "worker-b", 20.0, 30)
        self.assertIsNone(reclaimed)
        first.rollback(); first.close(); second.close()

    def test_dispatch_lock_does_not_block_unrelated_business_commit(self):
        from axonos_gate import x_capi_worker

        first = self.connect()
        with first.cursor() as cur:
            cur.execute("CREATE TABLE core_independence(id INTEGER PRIMARY KEY)")
            context_id = self.seed_context(cur, "0")
            self.seed_outbox(cur, context_id, "0")
        first.commit()
        cfg = self.worker_cfg()
        self.assertTrue(x_capi_worker.enforce_config_guard(first, cfg, 9.0))
        job = x_capi_worker.claim_job(first, "worker-a", 10.0, 30)
        self.assertEqual(
            x_capi_worker.begin_dispatch(
                first, job, 11.0, "v1", 24, POLICY_EPOCH,
                AUDIENCE_SCOPE, x_capi_worker._deployment_id_hash(cfg),
            ),
            "allowed",
        )

        business = self.connect()
        with business.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='100ms'")
            cur.execute("INSERT INTO core_independence VALUES(1)")
        business.commit()
        first.rollback(); first.close(); business.close()

    def test_wallet_first_touch_binds_before_mapping_and_rejects_second_context(self):
        from axonos_gate import x_capi, x_capi_worker

        now = 200.0
        wallet = "0x" + "1" * 40
        other_wallet = "0x" + "2" * 40
        ticket_one = {
            "v": 4, "handle": "h" * 43, "csrf": "c" * 43,
            "state": "granted", "mode_scope": "live", "policy_version": "v1",
            "policy_epoch": POLICY_EPOCH, "audience_scope": AUDIENCE_SCOPE,
            "issued_at": 100.0, "lifecycle_expires_at": 10_000.0,
            "consented_at": 110.0, "expires_at": 10_000.0,
            "landing_commitment": hashlib.sha256(
                b"click_12345678"
            ).hexdigest(),
            "twclid": "click_12345678",
        }
        ticket_two = dict(ticket_one, handle="j" * 43, csrf="d" * 43)
        cfg = SimpleNamespace(
            producer_ready=True, mode="live", policy_version="v1",
            policy_epoch=POLICY_EPOCH, audience_scope=AUDIENCE_SCOPE,
            deployment_id="test-deployment",
            max_event_age_hours=24, event_ids={"wallet_verified": ""},
            queue_limit=100, context_limit=100, pixel_id="pixel",
            twclid_charset="url_safe", twclid_min_length=8,
            twclid_max_length=256,
        )

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        conn = self.connect()
        base_envelope = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "t" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 200_000,
            "metadata": {"allow_context_binding": True, "credit_source": None,
                         "payment_rail": None, "chain_id": None},
        }
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest",
            side_effect=lambda token, **_kwargs: (
                ticket_one if token == "t" * 40 else ticket_two
            ),
        ):
            self.assertTrue(
                x_capi_worker.enforce_config_guard(conn, cfg, now - 1)
            )
            self.assertEqual(
                x_capi_worker._ingest_event(conn, base_envelope, now),
                "mapping_disabled",
            )
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT consent_state,wallet_hash,twclid,policy_epoch,
                              audience_scope FROM x_capi_attribution_contexts"""
                )
                durable = cur.fetchone()
            self.assertEqual(durable[0], "granted")
            self.assertEqual(durable[2:], (
                "click_12345678", POLICY_EPOCH, AUDIENCE_SCOPE,
            ))
            second = dict(base_envelope, context_token="u" * 40)
            self.assertEqual(
                x_capi_worker._ingest_event(conn, second, now),
                "wallet_already_attributed",
            )
            cross_wallet = dict(
                base_envelope, wallet_address=other_wallet, source_key=other_wallet
            )
            self.assertEqual(
                x_capi_worker._ingest_event(conn, cross_wallet, now),
                "wallet_or_lifecycle_mismatch",
            )
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT c.context_count,
                              (SELECT count(*)
                                 FROM x_capi_attribution_contexts)
                         FROM x_capi_capacity c WHERE c.singleton=TRUE"""
                )
                # A rejected sibling lifecycle for the same authenticated
                # wallet must not consume a capacity slot.
                self.assertEqual(cur.fetchone(), (1, 1))
        conn.close()

    def test_queue_capacity_is_serialized_across_concurrent_ingest(self):
        from axonos_gate import x_capi, x_capi_worker

        cfg = self.worker_cfg(mapping="wallet-event")
        cfg.queue_limit = 1
        tickets = {
            "a" * 40: self.lifecycle_ticket(handle="h" * 43, csrf="c" * 43),
            "b" * 40: self.lifecycle_ticket(handle="j" * 43, csrf="d" * 43),
        }
        envelopes = []
        for token, digit in zip(tickets, ("7", "8")):
            wallet = "0x" + digit * 40
            envelopes.append({
                "v": 1, "action": "event", "milestone": "wallet_verified",
                "context_token": token, "wallet_address": wallet,
                "source_key": wallet, "event_timestamp_ms": 200_000,
                "metadata": {
                    "allow_context_binding": True, "credit_source": None,
                    "payment_rail": None, "chain_id": None,
                },
            })
        barrier = threading.Barrier(2)
        outcomes = []
        failures = []

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        def ingest(envelope):
            conn = self.connect()
            try:
                barrier.wait(timeout=5)
                outcomes.append(x_capi_worker._ingest_event(conn, envelope, 200.0))
            except Exception as exc:
                failures.append(exc)
            finally:
                conn.close()

        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest",
            side_effect=lambda token, **_kwargs: tickets[token],
        ):
            guard = self.connect()
            self.assertTrue(
                x_capi_worker.enforce_config_guard(guard, cfg, 199.0)
            )
            guard.close()
            threads = [threading.Thread(target=ingest, args=(envelope,))
                       for envelope in envelopes]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(failures)
        self.assertCountEqual(outcomes, ("pending", "queue_full"))
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT count(conversion_id) FROM x_capi_outbox")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT context_count FROM x_capi_capacity")
            self.assertEqual(cur.fetchone()[0], 2)
            cur.execute("SELECT outbox_active_count FROM x_capi_capacity")
            self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    def test_bind_action_never_fabricates_wallet_conversion(self):
        from axonos_gate import x_capi, x_capi_worker

        cfg = self.worker_cfg(mapping="wallet-event")
        cfg.event_ids = {
            "wallet_verified": "wallet-event",
            "session_started": "session-event",
        }
        ticket = self.lifecycle_ticket()
        wallet = "0x" + "1" * 40
        bind = {
            "v": 1, "action": "bind", "context_token": "t" * 40,
            "wallet_address": wallet, "event_timestamp_ms": 200_000,
        }
        session = {
            "v": 1, "action": "event", "milestone": "session_started",
            "context_token": "t" * 40, "wallet_address": wallet,
            "source_key": "42", "event_timestamp_ms": 201_000,
            "metadata": {
                "allow_context_binding": False, "credit_source": None,
                "payment_rail": None, "chain_id": None,
            },
        }

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        conn = self.connect()
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=ticket
        ):
            self.assertTrue(x_capi_worker.enforce_config_guard(conn, cfg, 199.0))
            self.assertEqual(
                x_capi_worker.process_ingest_envelope(conn, bind, 200.0),
                "bound",
            )
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM x_capi_attribution_contexts")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT count(*) FROM x_capi_outbox")
                self.assertEqual(cur.fetchone()[0], 0)
                cur.execute("SELECT count(*) FROM x_capi_dedup")
                self.assertEqual(cur.fetchone()[0], 0)
            self.assertEqual(
                x_capi_worker.process_ingest_envelope(conn, session, 201.0),
                "pending",
            )
        conn.close()

    def test_paid_deposit_first_touch_binds_once_and_deduplicates(self):
        from axonos_gate import x_capi, x_capi_worker

        cfg = self.worker_cfg()
        cfg.event_ids = {"deposit_completed": "deposit-event"}
        wallet = "0x" + "2" * 40
        other_wallet = "0x" + "3" * 40
        ticket = self.lifecycle_ticket()
        sibling = self.lifecycle_ticket(handle="j" * 43, csrf="d" * 43)
        envelope = {
            "v": 1, "action": "event", "milestone": "deposit_completed",
            "context_token": "t" * 40, "wallet_address": wallet,
            "source_key": "0x" + "a" * 64,
            "event_timestamp_ms": 200_000,
            "metadata": {
                "allow_context_binding": True, "credit_source": "onchain",
                "payment_rail": "eth", "chain_id": 1,
            },
        }
        self.assertIsNotNone(x_capi_worker._validate_ingest_envelope(envelope))

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        conn = self.connect()
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest",
            side_effect=lambda token, **_kwargs: (
                ticket if token == "t" * 40 else sibling
            ),
        ):
            self.assertTrue(x_capi_worker.enforce_config_guard(conn, cfg, 199.0))
            self.assertEqual(x_capi_worker._ingest_event(conn, envelope, 200.0), "pending")
            self.assertEqual(x_capi_worker._ingest_event(conn, envelope, 200.0), "duplicate")
            sibling_event = dict(
                envelope,
                context_token="u" * 40,
                source_key="0x" + "b" * 64,
            )
            self.assertEqual(
                x_capi_worker._ingest_event(conn, sibling_event, 200.0),
                "wallet_already_attributed",
            )
            cross_wallet = dict(
                envelope,
                wallet_address=other_wallet,
                source_key="0x" + "c" * 64,
            )
            self.assertEqual(
                x_capi_worker._ingest_event(conn, cross_wallet, 200.0),
                "wallet_or_lifecycle_mismatch",
            )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_attribution_contexts")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT count(*) FROM x_capi_outbox")
            self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    def test_dry_run_ticket_cannot_be_ingested_after_live_transition(self):
        from axonos_gate import x_capi_worker

        cfg = SimpleNamespace(
            mode="live", policy_version="v1", policy_epoch=POLICY_EPOCH,
            audience_scope=AUDIENCE_SCOPE, max_event_age_hours=24,
            twclid_charset="url_safe",twclid_min_length=8,
            twclid_max_length=256,
        )
        ticket = {
            "state": "granted", "mode_scope": "dry_run", "policy_version": "v1",
            "policy_epoch": POLICY_EPOCH, "audience_scope": AUDIENCE_SCOPE,
            "landing_commitment": hashlib.sha256(
                b"click_12345678"
            ).hexdigest(),
            "twclid": "click_12345678",
            "issued_at": 90.0, "lifecycle_expires_at": 10_000.0,
            "consented_at": 100.0, "expires_at": 10_000.0,
        }
        self.assertFalse(
            x_capi_worker._event_ticket_is_current(ticket, cfg, 200_000, 200.0)
        )

    def test_dry_run_rows_are_cancelled_not_promoted_on_live_rebind(self):
        from axonos_gate import x_capi, x_capi_worker

        wallet = "0x" + "6" * 40

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        dry_values = dict(vars(self.worker_cfg()))
        dry_values.update(mode="dry_run", audience_scope="d" * 64)
        dry_cfg = SimpleNamespace(**dry_values)
        live_values = dict(vars(self.worker_cfg()))
        live_values.update(
            policy_epoch=POLICY_EPOCH + 1, audience_scope="b" * 64
        )
        live_cfg = SimpleNamespace(**live_values)
        ticket = self.lifecycle_ticket(handle="j" * 43, csrf="d" * 43)
        ticket.update(
            policy_epoch=POLICY_EPOCH + 1, audience_scope="b" * 64
        )
        envelope = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "t" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 200_000,
            "metadata": {
                "allow_context_binding": True, "credit_source": None,
                "payment_rail": None, "chain_id": None,
            },
        }
        conn = self.connect()
        with conn.cursor() as cur:
            old_context = self.seed_context(cur, "e")
            old_conversion = self.seed_outbox(cur, old_context, "e")
            cur.execute(
                """UPDATE x_capi_attribution_contexts
                      SET mode_scope='dry_run',audience_scope=%s,
                          wallet_hash=%s,wallet_bound_at=150 WHERE id=%s""",
                ("d" * 64, internal_hash("wallet", wallet), old_context),
            )
            cur.execute(
                """UPDATE x_capi_outbox
                      SET mode_scope='dry_run',status='dry_run',
                          consent_audience_scope=%s WHERE conversion_id=%s""",
                ("d" * 64, old_conversion),
            )
        conn.commit()
        self.assertTrue(x_capi_worker.enforce_config_guard(conn, dry_cfg, 190.0))
        durable_hash_fingerprint = x_capi_worker._hash_key_fingerprint()
        with patch.object(x_capi, "load_config", return_value=live_cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(
            x_capi_worker, "_hash_key_fingerprint",
            return_value=durable_hash_fingerprint,
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=ticket
        ):
            self.assertTrue(
                x_capi_worker.enforce_config_guard(conn, live_cfg, 199.0)
            )
            self.assertEqual(
                x_capi_worker._ingest_event(conn, envelope, 200.0),
                "mapping_disabled",
            )
        with conn.cursor() as cur:
            cur.execute(
                """SELECT consent_state,mode_scope,twclid,wallet_hash
                     FROM x_capi_attribution_contexts WHERE id=%s""",
                (old_context,),
            )
            self.assertEqual(cur.fetchone(), ("stale", "dry_run", None, None))
            cur.execute(
                """SELECT status,twclid FROM x_capi_outbox
                    WHERE conversion_id=%s""",
                (old_conversion,),
            )
            self.assertEqual(cur.fetchone(), ("cancelled", None))
            cur.execute(
                """SELECT count(*) FROM x_capi_attribution_contexts
                    WHERE mode_scope='live' AND consent_state='granted'
                      AND wallet_hash IS NOT NULL"""
            )
            self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    def test_config_guard_allows_advance_then_rejects_epoch_rollback(self):
        from axonos_gate import x_capi, x_capi_worker

        original = self.worker_cfg()
        advanced_values = dict(vars(original))
        advanced_values.update(
            policy_epoch=POLICY_EPOCH + 1,
            deployment_id="replacement-deployment",
            audience_scope="b" * 64,
        )
        advanced = SimpleNamespace(**advanced_values)
        conn = self.connect()
        self.assertTrue(x_capi_worker.enforce_config_guard(conn, original, 100.0))
        self.assertTrue(x_capi_worker.enforce_config_guard(conn, advanced, 101.0))
        self.assertFalse(x_capi_worker.enforce_config_guard(conn, original, 102.0))
        with conn.cursor() as cur:
            cur.execute(
                """SELECT max_policy_epoch,deployment_id_hash,audience_scope
                     FROM x_capi_config_guard"""
            )
            row = cur.fetchone()
        self.assertEqual(row[0], POLICY_EPOCH + 1)
        self.assertEqual(
            row[1], hashlib.sha256(b"replacement-deployment").hexdigest()
        )
        self.assertEqual(row[2], "b" * 64)
        with open(self.guard_path, "rb") as attestation_file:
            raw_attestation = attestation_file.read()
        attestation = json.loads(raw_attestation)
        self.assertEqual(
            attestation,
            {
                "audience_scope": "b" * 64,
                "configured": True,
                "context_key_fingerprint": (
                    x_capi.primary_context_key_fingerprint()
                ),
                "deployment_id_hash": hashlib.sha256(
                    b"replacement-deployment"
                ).hexdigest(),
                "max_policy_epoch": POLICY_EPOCH + 1,
                "mode_scope": "live",
                "policy_version": "v1",
                "v": 1,
            },
        )
        self.assertEqual(
            raw_attestation,
            json.dumps(
                attestation, sort_keys=True, separators=(",", ":")
            ).encode("ascii") + b"\n",
        )
        self.assertLessEqual(
            len(raw_attestation),
            x_capi_worker.CONFIG_GUARD_ATTESTATION_MAX_BYTES,
        )
        self.assertEqual(os.stat(self.guard_path).st_mode & 0o777, 0o600)
        conn.close()

    def test_config_guard_attestation_rejects_symlink_target(self):
        from axonos_gate import x_capi_worker

        with tempfile.TemporaryDirectory(prefix="xcapiguard-link-") as runtime:
            os.chmod(runtime, 0o700)
            target = os.path.join(runtime, "outside")
            with open(target, "wb") as target_file:
                target_file.write(b"must-not-change")
            path = os.path.join(runtime, "config-guard.json")
            os.symlink(target, path)
            with patch.object(
                x_capi_worker, "CONFIG_GUARD_ATTESTATION", path
            ):
                self.assertFalse(
                    x_capi_worker._write_config_guard_attestation(None)
                )
            with open(target, "rb") as target_file:
                self.assertEqual(target_file.read(), b"must-not-change")

    def test_worker_db_target_rejects_libpq_authority_overrides(self):
        from axonos_gate import x_capi, x_capi_worker

        base = "postgresql://axonos_x_capi_worker:secret@x-capi-postgres:5432/db"
        with patch.dict(
            os.environ,
            {
                "X_CAPI_ALLOW_TEST_DB_URL": "0",
                "X_CAPI_WORKER_DB_ROLE": "axonos_x_capi_worker",
                "X_CAPI_POSTGRES_DB": "db",
            },
            clear=False,
        ):
            with patch.object(x_capi, "_db_url", return_value=base):
                self.assertTrue(x_capi_worker._worker_db_target_is_isolated())
            for suffix in (
                "?host=evil.example",
                "?hostaddr=203.0.113.1",
                "?port=9999",
                "?service=attacker",
                "?options=-c%20search_path%3Dattacker",
            ):
                with patch.object(
                    x_capi, "_db_url", return_value=base + suffix
                ):
                    self.assertFalse(
                        x_capi_worker._worker_db_target_is_isolated(), suffix
                    )
            with patch.object(
                x_capi, "_db_url", return_value=base.replace("/db", "/other")
            ):
                self.assertFalse(
                    x_capi_worker._worker_db_target_is_isolated(),
                    "wrong dedicated database",
                )

    def test_live_production_delivery_is_structurally_blocked_before_token_claim(self):
        from axonos_gate import x_capi, x_capi_worker

        cfg = self.worker_cfg()
        conn = MagicMock()
        forbidden = AssertionError("live production crossed delivery block")
        with patch.dict(
            os.environ, {"X_CAPI_ALLOW_TEST_SECRETS": "1"}, clear=False
        ), patch.object(
            x_capi, "load_config", return_value=cfg
        ), patch.object(
            x_capi, "_db_url", return_value="postgresql://isolated/test"
        ), patch.object(
            x_capi_worker, "_worker_db_target_is_isolated", return_value=True
        ), patch.object(
            x_capi, "get_connection", return_value=conn
        ), patch.object(
            x_capi_worker, "_configure_connection"
        ), patch.object(
            x_capi_worker, "_schema_ready", return_value=True
        ), patch.object(
            x_capi_worker, "verify_config_guard", return_value=True
        ), patch.object(
            x_capi_worker, "drain_ingest"
        ), patch.object(
            x_capi_worker, "drain_ingest_until_empty", return_value=({}, True)
        ), patch.object(
            x_capi, "_context_cipher", return_value=(object(), None)
        ), patch.object(
            x_capi, "keyed_internal_hash", return_value="hash"
        ), patch.object(
            x_capi_worker, "read_token", side_effect=forbidden
        ), patch.object(
            x_capi_worker, "claim_job", side_effect=forbidden
        ), patch.object(
            x_capi_worker, "RequestsTransport", side_effect=forbidden
        ):
            outcome = x_capi_worker.run_once(
                transport=None, perform_cleanup=False
            )
        self.assertEqual(outcome, x_capi_worker.LIVE_DELIVERY_BLOCK_REASON)
        conn.close.assert_called_once()

        with patch.object(
            x_capi, "load_config", return_value=cfg
        ), patch.object(
            x_capi, "config_status",
            return_value={"mode": "live", "producer_ready": True},
        ), patch.object(
            x_capi, "_db_url", return_value=None
        ), patch.object(
            x_capi, "_context_cipher", return_value=(object(), None)
        ), patch.object(
            x_capi, "keyed_internal_hash", return_value="hash"
        ), patch.object(
            x_capi_worker, "_ingest_runtime_state",
            return_value=(True, True, True),
        ), patch.object(
            x_capi_worker, "_privacy_fence_runtime_secure", return_value=True
        ), patch.object(
            x_capi_worker, "read_token", side_effect=forbidden
        ):
            readiness = x_capi_worker.worker_readiness()
        self.assertFalse(readiness["live_ready"])
        self.assertFalse(readiness["worker_ready"])
        self.assertEqual(
            readiness["live_delivery_blocked_reason"],
            x_capi_worker.LIVE_DELIVERY_BLOCK_REASON,
        )

    def test_concrete_x_transport_cannot_post_even_when_called_directly(self):
        from axonos_gate import x_capi_worker

        transport = object.__new__(x_capi_worker.RequestsTransport)
        transport._session = MagicMock()
        with self.assertRaisesRegex(
            RuntimeError, x_capi_worker.LIVE_DELIVERY_BLOCK_REASON
        ):
            transport.send(
                "pixel", "token", {"conversions": [{"synthetic": True}]}
            )
        transport._session.post.assert_not_called()

    def test_payload_uses_exact_dedicated_token_unix_milliseconds(self):
        from axonos_gate import x_capi_worker

        job = {
            "conversion_timestamp_ms": 1_645_146_840_603,
            "event_id": "event",
            "twclid": "click_12345678",
            "conversion_id": "00000000-0000-4000-8000-000000000001",
        }
        conversion = x_capi_worker.build_payload(job)["conversions"][0]
        self.assertEqual(
            set(conversion),
            {"conversion_timestamp", "event_id", "identifiers", "conversion_id"},
        )
        self.assertEqual(
            conversion["conversion_timestamp"], 1_645_146_840_603
        )
        self.assertIs(type(conversion["conversion_timestamp"]), int)
        maximum = dict(job, conversion_timestamp_ms=253_402_300_799_999)
        self.assertEqual(
            x_capi_worker.build_payload(maximum)["conversions"][0][
                "conversion_timestamp"
            ],
            253_402_300_799_999,
        )
        for invalid in (
            True, False, 1.0, "1", None, 0, -1, 253_402_300_800_000,
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "invalid_conversion_timestamp_ms"
            ):
                x_capi_worker.build_payload(
                    dict(job, conversion_timestamp_ms=invalid)
                )

    def test_dry_run_maintenance_is_unchanged_by_live_delivery_block(self):
        from axonos_gate import x_capi, x_capi_worker

        cfg = self.worker_cfg()
        cfg.mode = "dry_run"
        conn = MagicMock()
        with patch.dict(
            os.environ, {"X_CAPI_ALLOW_TEST_SECRETS": "1"}, clear=False
        ), patch.object(
            x_capi, "load_config", return_value=cfg
        ), patch.object(
            x_capi, "_db_url", return_value="postgresql://isolated/test"
        ), patch.object(
            x_capi_worker, "_worker_db_target_is_isolated", return_value=True
        ), patch.object(
            x_capi, "get_connection", return_value=conn
        ), patch.object(
            x_capi_worker, "_configure_connection"
        ), patch.object(
            x_capi_worker, "_schema_ready", return_value=True
        ), patch.object(
            x_capi_worker, "verify_config_guard", return_value=True
        ), patch.object(
            x_capi_worker, "drain_ingest"
        ), patch.object(
            x_capi_worker, "drain_ingest_until_empty", return_value=({}, True)
        ), patch.object(
            x_capi_worker, "read_token"
        ) as read_token, patch.object(
            x_capi_worker, "claim_job"
        ) as claim:
            outcome = x_capi_worker.run_once(
                transport=None, perform_cleanup=False
            )
        self.assertEqual(outcome, "maintenance")
        read_token.assert_not_called()
        claim.assert_not_called()

    def test_config_guard_rejects_same_epoch_scope_reuse(self):
        from axonos_gate import x_capi_worker

        original = self.worker_cfg()
        conn = self.connect()
        self.assertTrue(x_capi_worker.enforce_config_guard(conn, original, 100.0))
        mutations = (
            {"deployment_id": "different-deployment"},
            {"mode": "dry_run"},
            {"policy_version": "different-policy"},
            {"audience_scope": "b" * 64},
        )
        for mutation in mutations:
            values = dict(vars(original))
            values.update(mutation)
            self.assertFalse(
                x_capi_worker.enforce_config_guard(
                    conn, SimpleNamespace(**values), 101.0
                ),
                mutation,
            )
        with conn.cursor() as cur:
            cur.execute(
                """SELECT max_policy_epoch,mode_scope,policy_version,
                          audience_scope FROM x_capi_config_guard"""
            )
            self.assertEqual(
                cur.fetchone(),
                (POLICY_EPOCH, "live", "v1", AUDIENCE_SCOPE),
            )
        conn.close()

    def test_config_guard_rejects_hash_key_rotation_after_restart(self):
        from axonos_gate import x_capi_worker

        original = self.worker_cfg()
        advanced_values = dict(vars(original))
        advanced_values["policy_epoch"] = POLICY_EPOCH + 1
        advanced = SimpleNamespace(**advanced_values)
        first = self.connect()
        self.assertTrue(x_capi_worker.enforce_config_guard(first, original, 100.0))
        with first.cursor() as cur:
            cur.execute(
                "SELECT hash_key_fingerprint FROM x_capi_config_guard"
            )
            durable_fingerprint = str(cur.fetchone()[0])
        first.close()

        restarted = self.connect()
        rotated_fingerprint = (
            "b" * 64 if durable_fingerprint != "b" * 64 else "c" * 64
        )
        with patch.object(
            x_capi_worker,
            "_hash_key_fingerprint",
            return_value=rotated_fingerprint,
        ):
            self.assertFalse(
                x_capi_worker.enforce_config_guard(restarted, advanced, 101.0)
            )
        with restarted.cursor() as cur:
            cur.execute(
                "SELECT max_policy_epoch,hash_key_fingerprint "
                "FROM x_capi_config_guard"
            )
            self.assertEqual(
                cur.fetchone(), (POLICY_EPOCH, durable_fingerprint)
            )
        restarted.close()

    def test_config_guard_context_key_change_requires_epoch_and_cannot_rollback(self):
        from axonos_gate import x_capi_worker

        original = self.worker_cfg()
        advanced_values = dict(vars(original))
        advanced_values["policy_epoch"] = POLICY_EPOCH + 1
        advanced = SimpleNamespace(**advanced_values)
        first_key = "1" * 64
        next_key = "2" * 64
        conn = self.connect()
        with patch.object(
            x_capi_worker, "_context_key_fingerprint", return_value=first_key
        ):
            self.assertTrue(
                x_capi_worker.enforce_config_guard(conn, original, 100.0)
            )
        with patch.object(
            x_capi_worker, "_context_key_fingerprint", return_value=next_key
        ):
            self.assertFalse(
                x_capi_worker.enforce_config_guard(conn, original, 101.0)
            )
            self.assertTrue(
                x_capi_worker.enforce_config_guard(conn, advanced, 102.0)
            )
        with patch.object(
            x_capi_worker, "_context_key_fingerprint", return_value=first_key
        ):
            self.assertFalse(
                x_capi_worker.enforce_config_guard(conn, original, 103.0)
            )
            self.assertFalse(
                x_capi_worker.enforce_config_guard(conn, advanced, 104.0)
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max_policy_epoch,context_key_fingerprint "
                "FROM x_capi_config_guard"
            )
            self.assertEqual(cur.fetchone(), (POLICY_EPOCH + 1, next_key))
        conn.close()

    def test_config_guard_file_blocks_restored_database_rollback(self):
        from axonos_gate import x_capi_worker

        original = self.worker_cfg()
        advanced_values = dict(vars(original))
        advanced_values.update(
            policy_epoch=POLICY_EPOCH + 1,
            deployment_id="advanced-deployment",
            audience_scope="b" * 64,
        )
        advanced = SimpleNamespace(**advanced_values)
        conn = self.connect()
        self.assertTrue(x_capi_worker.enforce_config_guard(conn, original, 100.0))
        self.assertTrue(x_capi_worker.enforce_config_guard(conn, advanced, 101.0))
        with open(self.guard_path, "rb") as guard_file:
            durable_file = guard_file.read()

        # Model a point-in-time DB restore while the persistent worker-owned
        # high-water volume survives.
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE x_capi_config_guard
                      SET max_policy_epoch=%s,deployment_id_hash=%s,
                          mode_scope=%s,policy_version=%s,audience_scope=%s,
                          updated_at=102""",
                (
                    POLICY_EPOCH,
                    hashlib.sha256(b"test-deployment").hexdigest(),
                    "live", "v1", AUDIENCE_SCOPE,
                ),
            )
        conn.commit()
        self.assertFalse(x_capi_worker.verify_config_guard(conn, original, 103.0))
        self.assertFalse(x_capi_worker.enforce_config_guard(conn, original, 104.0))
        with open(self.guard_path, "rb") as guard_file:
            self.assertEqual(guard_file.read(), durable_file)
        conn.close()

    def test_epoch_recovery_keeps_gate_unconfigured_until_slots_reset(self):
        from axonos_gate import x_capi, x_capi_worker

        original = self.worker_cfg()
        advanced_values = dict(vars(original))
        advanced_values.update(
            policy_epoch=POLICY_EPOCH + 1,
            deployment_id="recovery-deployment",
            audience_scope="b" * 64,
        )
        advanced = SimpleNamespace(**advanced_values)
        conn = self.connect()
        self.assertTrue(
            x_capi_worker.enforce_config_guard(conn, original, 100.0)
        )
        conn.close()
        with tempfile.TemporaryDirectory(prefix="xcapirecovery-") as runtime:
            path = os.path.join(runtime, "privacy")
            fence = x_capi_worker.PrivacyFence(path)
            fence.open()
            self.assertTrue(fence.activate_global_quarantine())
            observed_transition = []
            real_reset = fence.reset_after_epoch_recovery

            def checked_reset():
                with open(self.guard_path, "rb") as guard_file:
                    document = json.loads(guard_file.read().decode("ascii"))
                observed_transition.append(document)
                self.assertFalse(document["configured"])
                return real_reset()

            with patch.object(
                x_capi, "load_config", return_value=advanced
            ), patch.object(
                x_capi, "_db_url", return_value=TEST_URL
            ), patch.object(
                x_capi_worker, "_worker_db_target_is_isolated",
                return_value=True,
            ), patch.object(
                x_capi, "get_connection",
                side_effect=lambda **_unused: self.connect(),
            ), patch.object(
                x_capi_worker, "_schema_ready", return_value=True
            ), patch.object(
                fence, "reset_after_epoch_recovery",
                side_effect=checked_reset,
            ), patch.dict(
                os.environ,
                {
                    "X_CAPI_PRIVACY_RECOVERY_EPOCH": str(POLICY_EPOCH + 1)
                },
                clear=False,
            ):
                self.assertTrue(
                    x_capi_worker.activate_worker_startup(
                        fence, str(uuid.uuid4())
                    )
                )
            self.assertEqual(len(observed_transition), 1)
            with open(self.guard_path, "rb") as guard_file:
                final_document = json.loads(guard_file.read().decode("ascii"))
            self.assertTrue(final_document["configured"])
            self.assertEqual(
                final_document["max_policy_epoch"], POLICY_EPOCH + 1
            )
            fence.close()

    def test_wallet_derived_click_encoding_is_rejected_before_persistence(self):
        from axonos_gate import x_capi, x_capi_worker

        wallet = "0x" + "9" * 40
        derived = base64.urlsafe_b64encode(
            bytes.fromhex(wallet[2:])
        ).decode("ascii").rstrip("=")
        ticket = self.lifecycle_ticket()
        ticket["twclid"] = derived
        ticket["landing_commitment"] = hashlib.sha256(
            derived.encode()
        ).hexdigest()
        cfg = self.worker_cfg()
        envelope = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "t" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 200_000,
            "metadata": {
                "allow_context_binding": True, "credit_source": None,
                "payment_rail": None, "chain_id": None,
            },
        }
        conn = self.connect()
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=ticket
        ):
            self.assertEqual(
                x_capi_worker._ingest_event(conn, envelope, 200.0),
                "identifier_conflicts_with_wallet",
            )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_attribution_contexts")
            self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_absent_context_decline_tombstones_before_late_wallet_event(self):
        from axonos_gate import x_capi, x_capi_worker

        now = 200.0
        unset = self.lifecycle_ticket(state="unset")
        granted = self.lifecycle_ticket(state="granted")
        cfg = self.worker_cfg()
        request = {
            "v": 1, "action": "consent", "operation": "decline",
            "context_token": "t" * 40, "csrf_token": unset["csrf"],
            "request_timestamp_ms": 200_000,
        }
        wallet = "0x" + "3" * 40
        envelope = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "t" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 200_000,
            "metadata": {"allow_context_binding": True, "credit_source": None,
                         "payment_rail": None, "chain_id": None},
        }

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        conn = self.connect()
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(x_capi_worker, "_ticket_for_ingest", return_value=unset):
            response = x_capi_worker.process_consent_request(conn, request, now)
        self.assertEqual(response, {"v": 1, "ok": True, "state": "denied"})
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(x_capi_worker, "_ticket_for_ingest", return_value=granted):
            self.assertTrue(
                x_capi_worker.enforce_config_guard(conn, cfg, now - 1)
            )
            self.assertEqual(
                x_capi_worker._ingest_event(conn, envelope, now),
                "revoked_lifecycle",
            )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_attribution_contexts")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute(
                """SELECT tombstone_count,revocation_saturated
                     FROM x_capi_capacity"""
            )
            self.assertEqual(cur.fetchone(), (1, False))
        conn.close()

    def test_old_unset_decline_revokes_context_created_from_granted_ticket(self):
        from axonos_gate import x_capi, x_capi_worker

        now = 200.0
        unset = self.lifecycle_ticket(state="unset")
        granted = self.lifecycle_ticket(state="granted")
        cfg = self.worker_cfg()
        wallet = "0x" + "4" * 40
        envelope = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "t" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 200_000,
            "metadata": {"allow_context_binding": True, "credit_source": None,
                         "payment_rail": None, "chain_id": None},
        }

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        conn = self.connect()
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(x_capi_worker, "_ticket_for_ingest", return_value=granted):
            self.assertTrue(
                x_capi_worker.enforce_config_guard(conn, cfg, now - 1)
            )
            self.assertEqual(
                x_capi_worker._ingest_event(conn, envelope, now),
                "mapping_disabled",
            )
        request = {
            "v": 1, "action": "consent", "operation": "decline",
            "context_token": "t" * 40, "csrf_token": unset["csrf"],
            "request_timestamp_ms": 200_000,
        }
        with patch.object(x_capi_worker, "_ticket_for_ingest", return_value=unset):
            response = x_capi_worker.process_consent_request(conn, request, now)
        self.assertEqual(response, {"v": 1, "ok": True, "state": "revoked"})
        with conn.cursor() as cur:
            cur.execute(
                """SELECT consent_state,twclid,expires_at,wallet_hash
                     FROM x_capi_attribution_contexts"""
            )
            self.assertEqual(cur.fetchone(), ("revoked", None, None, None))
        conn.close()

    def test_decrypt_only_ticket_can_revoke_but_cannot_ingest_or_restart_lifecycle(self):
        from cryptography.fernet import Fernet
        from axonos_gate import x_capi_worker

        primary_key = Fernet.generate_key()
        old_key = Fernet.generate_key()
        ticket = self.lifecycle_ticket(state="unset")
        encoded = json.dumps(
            ticket, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        old_token = Fernet(old_key).encrypt(encoded).decode("ascii").rstrip("=")
        env = {
            "X_CAPI_ALLOW_TEST_SECRETS": "1",
            "X_CAPI_CONTEXT_KEY": (
                primary_key.decode("ascii") + "\n" + old_key.decode("ascii")
            ),
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertIsNotNone(
                x_capi_worker._ticket_for_ingest(old_token)
            )
            self.assertIsNone(
                x_capi_worker._ticket_for_ingest(
                    old_token, require_primary=True
                )
            )
            conn = self.connect()
            base_request = {
                "v": 1, "action": "consent", "context_token": old_token,
                "csrf_token": ticket["csrf"], "request_timestamp_ms": 200_000,
            }
            restart = dict(base_request, operation="new_lifecycle")
            self.assertEqual(
                x_capi_worker.process_consent_request(conn, restart, 200.0),
                {"v": 1, "ok": False, "error": "invalid_context"},
            )
            revoke = dict(base_request, operation="revoke")
            self.assertEqual(
                x_capi_worker.process_consent_request(conn, revoke, 200.0),
                {"v": 1, "ok": True, "state": "revoked"},
            )
            conn.close()

    def test_revocation_racing_first_bind_never_leaves_active_attribution(self):
        from axonos_gate import x_capi, x_capi_worker

        unset = self.lifecycle_ticket(state="unset")
        granted = self.lifecycle_ticket(state="granted")
        cfg = self.worker_cfg()
        wallet = "0x" + "6" * 40
        envelope = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "e" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 200_000,
            "metadata": {"allow_context_binding": True, "credit_source": None,
                         "payment_rail": None, "chain_id": None},
        }
        request = {
            "v": 1, "action": "consent", "operation": "decline",
            "context_token": "r" * 40, "csrf_token": unset["csrf"],
            "request_timestamp_ms": 200_000,
        }
        barrier = threading.Barrier(2)
        results = {}
        failures = []

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        def bind():
            conn = self.connect()
            try:
                barrier.wait(timeout=5)
                results["event"] = x_capi_worker._ingest_event(conn, envelope, 200.0)
            except Exception as exc:
                failures.append(exc)
            finally:
                conn.close()

        def revoke():
            conn = self.connect()
            try:
                barrier.wait(timeout=5)
                results["revoke"] = x_capi_worker.process_consent_request(
                    conn, request, 200.0
                )
            except Exception as exc:
                failures.append(exc)
            finally:
                conn.close()

        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest",
            side_effect=lambda token, **_kwargs: (
                granted if token == "e" * 40 else unset
            ),
        ):
            guard = self.connect()
            self.assertTrue(
                x_capi_worker.enforce_config_guard(guard, cfg, 199.0)
            )
            guard.close()
            threads = [threading.Thread(target=bind), threading.Thread(target=revoke)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertFalse(failures)
        self.assertTrue(results["revoke"]["ok"])
        self.assertIn(results["event"], ("mapping_disabled", "revoked_lifecycle"))
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) FROM x_capi_attribution_contexts
                    WHERE consent_state='granted' OR twclid IS NOT NULL"""
            )
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT count(*) FROM x_capi_outbox WHERE twclid IS NOT NULL")
            self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_revocation_capacity_saturation_fails_new_binding_closed(self):
        from axonos_gate import x_capi, x_capi_worker

        cfg = self.worker_cfg()
        ticket = self.lifecycle_ticket(handle="z" * 43, csrf="y" * 43)
        wallet = "0x" + "5" * 40
        envelope = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "z" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 200_000,
            "metadata": {"allow_context_binding": True, "credit_source": None,
                         "payment_rail": None, "chain_id": None},
        }
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE x_capi_capacity SET tombstone_count=400,
                          revocation_saturated=TRUE,
                          revocation_saturated_until=10000"""
            )
        conn.commit()
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash",
            side_effect=lambda label, value: hashlib.sha256(
                (label + "\0" + value).encode()
            ).hexdigest(),
        ), patch.object(x_capi_worker, "_ticket_for_ingest", return_value=ticket):
            self.assertTrue(
                x_capi_worker.enforce_config_guard(conn, cfg, 199.0)
            )
            self.assertEqual(
                x_capi_worker._ingest_event(conn, envelope, 200.0),
                "context_capacity_unavailable",
            )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_attribution_contexts")
            self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_saturation_deadline_survives_cleanup_and_blocks_ticket_replay(self):
        from axonos_gate import x_capi, x_capi_worker

        cfg = self.worker_cfg()
        unset = self.lifecycle_ticket(
            handle="q" * 43, csrf="r" * 43, state="unset"
        )
        granted = self.lifecycle_ticket(
            handle="q" * 43, csrf="r" * 43, state="granted"
        )
        wallet = "0x" + "9" * 40
        request = {
            "v": 1, "action": "consent", "operation": "decline",
            "context_token": "q" * 40, "csrf_token": unset["csrf"],
            "request_timestamp_ms": 200_000,
        }
        envelope = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "q" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 300_000,
            "metadata": {
                "allow_context_binding": True, "credit_source": None,
                "payment_rail": None, "chain_id": None,
            },
        }

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        conn = self.connect()
        with conn.cursor() as cur:
            # Fill the exact configured cap with rows that cleanup can free
            # before the unstored lifecycle expires.
            self.seed_tombstones(cur, 400, 250.0)
        conn.commit()
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=unset
        ):
            response = x_capi_worker.process_consent_request(conn, request, 200.0)
        self.assertEqual(
            response,
            {
                "v": 1, "ok": False,
                "error": "revocation_capacity_unavailable",
            },
        )
        marker = {
            "handle_hash": hashlib.sha256(unset["handle"].encode()).hexdigest(),
            "csrf_hash": hashlib.sha256(unset["csrf"].encode()).hexdigest(),
            "lifecycle_expires_at": unset["lifecycle_expires_at"],
        }
        self.assertTrue(x_capi_worker._privacy_marker_is_applied(conn, marker))
        self.assertFalse(
            x_capi_worker._privacy_marker_is_applied(
                conn, dict(marker, lifecycle_expires_at=10_001.0)
            )
        )

        x_capi_worker.expire_and_cleanup(
            conn, 300.0, 24, "v1", policy_epoch=POLICY_EPOCH,
            audience_scope=AUDIENCE_SCOPE, context_limit=100,
        )
        with conn.cursor() as cur:
            cur.execute(
                """SELECT tombstone_count,revocation_saturated,
                          revocation_saturated_until
                     FROM x_capi_capacity WHERE singleton=TRUE"""
            )
            self.assertEqual(cur.fetchone(), (0, True, 10_000.0))

        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=granted
        ):
            self.assertTrue(x_capi_worker.enforce_config_guard(conn, cfg, 299.0))
            self.assertEqual(
                x_capi_worker._ingest_event(conn, envelope, 300.0),
                "context_capacity_unavailable",
            )

        x_capi_worker.expire_and_cleanup(
            conn, 9_999.0, 24, "v1", policy_epoch=POLICY_EPOCH,
            audience_scope=AUDIENCE_SCOPE, context_limit=100,
        )
        with conn.cursor() as cur:
            cur.execute(
                """SELECT revocation_saturated,revocation_saturated_until
                     FROM x_capi_capacity WHERE singleton=TRUE"""
            )
            self.assertEqual(cur.fetchone(), (True, 10_000.0))
        x_capi_worker.expire_and_cleanup(
            conn, 10_000.0, 24, "v1", policy_epoch=POLICY_EPOCH,
            audience_scope=AUDIENCE_SCOPE, context_limit=100,
        )
        with conn.cursor() as cur:
            cur.execute(
                """SELECT revocation_saturated,revocation_saturated_until
                     FROM x_capi_capacity WHERE singleton=TRUE"""
            )
            self.assertEqual(cur.fetchone(), (False, 0.0))
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=granted
        ):
            expired = dict(envelope, event_timestamp_ms=10_000_000)
            self.assertEqual(
                x_capi_worker._ingest_event(conn, expired, 10_000.0),
                "invalid_or_stale_ticket",
            )
        conn.close()

    def test_cleanup_racing_saturated_close_preserves_exact_or_global_deny(self):
        from axonos_gate import x_capi, x_capi_worker

        cfg = self.worker_cfg()
        unset = self.lifecycle_ticket(
            handle="s" * 43, csrf="t" * 43, state="unset"
        )
        granted = self.lifecycle_ticket(
            handle="s" * 43, csrf="t" * 43, state="granted"
        )
        request = {
            "v": 1, "action": "consent", "operation": "decline",
            "context_token": "s" * 40, "csrf_token": unset["csrf"],
            "request_timestamp_ms": 300_000,
        }
        wallet = "0x" + "8" * 40
        envelope = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "s" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 301_000,
            "metadata": {
                "allow_context_binding": True, "credit_source": None,
                "payment_rail": None, "chain_id": None,
            },
        }
        setup = self.connect()
        with setup.cursor() as cur:
            self.seed_tombstones(cur, 400, 250.0)
        setup.commit()
        setup.close()
        barrier = threading.Barrier(2)
        results = {}
        failures = []

        def close_lifecycle():
            conn = self.connect()
            try:
                barrier.wait(timeout=5)
                results["close"] = x_capi_worker.process_consent_request(
                    conn, request, 300.0
                )
            except Exception as exc:
                failures.append(exc)
            finally:
                conn.close()

        def cleanup():
            conn = self.connect()
            try:
                barrier.wait(timeout=5)
                x_capi_worker.expire_and_cleanup(
                    conn, 300.0, 24, "v1", policy_epoch=POLICY_EPOCH,
                    audience_scope=AUDIENCE_SCOPE, context_limit=100,
                )
            except Exception as exc:
                failures.append(exc)
            finally:
                conn.close()

        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=unset
        ):
            threads = [
                threading.Thread(target=close_lifecycle),
                threading.Thread(target=cleanup),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertFalse(failures)
        self.assertIn(results["close"].get("error"), (
            None, "revocation_capacity_unavailable",
        ))

        marker = {
            "handle_hash": hashlib.sha256(unset["handle"].encode()).hexdigest(),
            "csrf_hash": hashlib.sha256(unset["csrf"].encode()).hexdigest(),
            "lifecycle_expires_at": unset["lifecycle_expires_at"],
        }
        conn = self.connect()
        self.assertTrue(x_capi_worker._privacy_marker_is_applied(conn, marker))
        with conn.cursor() as cur:
            cur.execute(
                """SELECT EXISTS(
                         SELECT 1 FROM x_capi_revocation_tombstones
                          WHERE handle_hash=%s AND csrf_hash=%s
                            AND lifecycle_expires_at=%s
                       ),revocation_saturated,revocation_saturated_until
                     FROM x_capi_capacity WHERE singleton=TRUE""",
                (
                    marker["handle_hash"], marker["csrf_hash"],
                    marker["lifecycle_expires_at"],
                ),
            )
            exact, saturated, deadline = cur.fetchone()
        self.assertTrue(exact or (saturated and deadline >= 10_000.0))

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=granted
        ):
            self.assertTrue(x_capi_worker.enforce_config_guard(conn, cfg, 300.0))
            self.assertIn(
                x_capi_worker._ingest_event(conn, envelope, 301.0),
                ("revoked_lifecycle", "context_capacity_unavailable"),
            )
        conn.close()

    def test_saturation_fences_queue_claim_and_final_dispatch(self):
        from axonos_gate import x_capi, x_capi_worker

        cfg = self.worker_cfg(mapping="wallet-event")
        cfg.event_ids["session_started"] = "session-event"
        ticket = self.lifecycle_ticket()
        wallet = "0x" + "7" * 40
        wallet_event = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "t" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 200_000,
            "metadata": {
                "allow_context_binding": True, "credit_source": None,
                "payment_rail": None, "chain_id": None,
            },
        }
        session_event = {
            "v": 1, "action": "event", "milestone": "session_started",
            "context_token": "t" * 40, "wallet_address": wallet,
            "source_key": "42", "event_timestamp_ms": 203_000,
            "metadata": {
                "allow_context_binding": False, "credit_source": None,
                "payment_rail": None, "chain_id": None,
            },
        }

        def internal_hash(label, value):
            return hashlib.sha256((label + "\0" + value).encode()).hexdigest()

        conn = self.connect()
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash", side_effect=internal_hash
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=ticket
        ):
            self.assertTrue(x_capi_worker.enforce_config_guard(conn, cfg, 199.0))
            self.assertEqual(
                x_capi_worker._ingest_event(conn, wallet_event, 200.0),
                "pending",
            )
            job = x_capi_worker.claim_job(conn, "worker-a", 201.0, 30)
            self.assertIsNotNone(job)
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE x_capi_capacity
                          SET revocation_saturated=TRUE,
                              revocation_saturated_until=10000
                        WHERE singleton=TRUE"""
                )
            conn.commit()
            self.assertEqual(
                x_capi_worker.begin_dispatch(
                    conn, job, 202.0, "v1", 24, POLICY_EPOCH,
                    AUDIENCE_SCOPE, x_capi_worker._deployment_id_hash(cfg),
                ),
                "lease_lost",
            )
            self.assertEqual(
                x_capi_worker._ingest_event(conn, session_event, 203.0),
                "queue_full",
            )
            self.assertIsNone(
                x_capi_worker.claim_job(conn, "worker-b", 1_000.0, 30)
            )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_outbox")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute(
                """SELECT count(*) FROM x_capi_dedup
                    WHERE milestone='session_started'"""
            )
            self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_stateless_grant_is_nonrenewable_and_allocates_no_db_row(self):
        from cryptography.fernet import Fernet
        from axonos_gate import x_capi

        env = {
            "X_CAPI_MODE": "dry_run",
            "X_CAPI_PIXEL_ID": "pixel",
            "X_CAPI_EVENT_WALLET_VERIFIED": "wallet-event",
            "X_CAPI_ALLOWED_ORIGIN": "https://app.example",
            "X_CAPI_CONSENT_POLICY_VERSION": "v1",
            "X_CAPI_CONSENT_POLICY_EPOCH": str(POLICY_EPOCH),
            "X_CAPI_DEPLOYMENT_ID": "test-deployment",
            "X_CAPI_ALLOW_TEST_SECRETS": "1",
            "X_CAPI_CONTEXT_KEY": Fernet.generate_key().decode("ascii"),
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            x_capi, "config_guard_current", return_value=True
        ):
            initial = x_capi.attribution_status(
                None, landing_twclid="click_12345678", gpc=False
            )
            self.assertEqual(initial["state"], "unset")
            granted, code = x_capi.update_consent(
                context_token=initial["context"], csrf_token=initial["csrf"],
                action="grant",
                landing_twclid="click_12345678",
                origin="https://app.example", gpc=False,
            )
            self.assertEqual(code, 200)
            first_expiry = x_capi.decode_context_ticket(granted["context"])["expires_at"]

            repeated, repeated_code = x_capi.update_consent(
                context_token=granted["context"], csrf_token=granted["csrf"],
                action="grant",
                origin="https://app.example", gpc=False,
            )
            self.assertEqual(repeated_code, 409)
            changed, changed_code = x_capi.update_consent(
                context_token=initial["context"], csrf_token=initial["csrf"],
                action="grant", twclid="different_click_1234",
                origin="https://app.example", gpc=False,
            )
            self.assertEqual(changed_code, 400)
            self.assertEqual(repeated["state"], "granted")
            self.assertEqual(changed["state"], "unset")
            active_new, active_new_code = x_capi.update_consent(
                context_token=granted["context"], csrf_token=granted["csrf"],
                action="new_lifecycle", origin="https://app.example", gpc=False,
            )
            self.assertEqual(active_new_code, 409)

        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_attribution_contexts")
            self.assertEqual(cur.fetchone()[0], 0)
        self.assertGreater(first_expiry, 0)
        conn.close()

    def test_cleanup_retains_context_tombstones_for_full_ticket_lifetime(self):
        from axonos_gate import x_capi_worker

        now = 200 * 86400.0
        conn = self.connect()
        with conn.cursor() as cur:
            keep_id = self.seed_context(cur, "9")
            drop_id = self.seed_context(cur, "a")
            cur.execute(
                """UPDATE x_capi_attribution_contexts
                      SET consent_state='revoked',twclid=NULL,expires_at=NULL,
                          wallet_hash=NULL,wallet_bound_at=NULL,updated_at=%s
                    WHERE id=%s""",
                (now - 60 * 86400, keep_id),
            )
            cur.execute(
                """UPDATE x_capi_attribution_contexts
                      SET consent_state='revoked',twclid=NULL,expires_at=NULL,
                          wallet_hash=NULL,wallet_bound_at=NULL,updated_at=%s
                    WHERE id=%s""",
                (now - 92 * 86400, drop_id),
            )
            cur.execute(
                """INSERT INTO x_capi_dedup
                   (milestone,source_key_hash,first_seen_at,expires_at)
                   VALUES('wallet_verified',%s,1,%s)""",
                ("9" * 64, now - 1),
            )
            cur.execute(
                """INSERT INTO x_capi_revocation_tombstones
                   (handle_hash,csrf_hash,lifecycle_expires_at,created_at,updated_at)
                   VALUES(%s,%s,%s,1,1)""",
                ("e" * 64, "f" * 64, now - 1),
            )
            cur.execute(
                """UPDATE x_capi_capacity SET tombstone_count=1,
                          revocation_saturated=TRUE,
                          revocation_saturated_until=%s""",
                (now - 1,),
            )
        conn.commit()
        x_capi_worker.expire_and_cleanup(
            conn, now, 24, "v1", retention_days=30,
            policy_epoch=POLICY_EPOCH, audience_scope=AUDIENCE_SCOPE,
            context_limit=100,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM x_capi_attribution_contexts WHERE id IN (%s,%s) ORDER BY id",
                (keep_id, drop_id),
            )
            remaining = {str(row[0]) for row in cur.fetchall()}
            cur.execute("SELECT count(*) FROM x_capi_dedup")
            dedup_count = cur.fetchone()[0]
            cur.execute(
                """SELECT context_count,tombstone_count,revocation_saturated,
                          revocation_saturated_until
                     FROM x_capi_capacity"""
            )
            capacity = cur.fetchone()
        self.assertIn(keep_id, remaining)
        self.assertNotIn(drop_id, remaining)
        self.assertEqual(dedup_count, 0)
        self.assertEqual(capacity, (1, 0, False, 0.0))
        conn.close()

    def test_hardening_refuses_unverified_legacy_schema_without_erasure(self):
        schema = "xcapilegacy_" + uuid.uuid4().hex[:16]
        admin = self.psycopg2.connect(TEST_URL)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("CREATE SCHEMA " + schema)
        admin.close()
        options = "-c search_path=" + schema
        conn = self.psycopg2.connect(TEST_URL, options=options)
        hardening = _migration_text("003_x_capi_hardening.sql", schema)
        with conn.cursor() as cur:
            cur.execute(LEGACY_SCHEMA_SQL)
            context_id = str(uuid.uuid4())
            cur.execute(
                """INSERT INTO x_capi_attribution_contexts
                   (id,handle_hash,csrf_hash,consent_state,policy_version,twclid,
                    expires_at,first_seen_at,updated_at,wallet_hash,wallet_bound_at)
                   VALUES(%s,%s,%s,'granted','v1','click_12345678',9999999999,
                          1,1,%s,1)""",
                (context_id, "b" * 64, "c" * 64, "d" * 64),
            )
        conn.commit()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO x_capi_dedup VALUES('session_started',%s,1)",
                    ("c" * 64,),
                )
                cur.execute(
                    """INSERT INTO x_capi_outbox
                       (conversion_id,milestone,source_key_hash,mode_scope,
                        pixel_id,event_id,conversion_timestamp_ms,twclid,
                        context_id,consent_policy_version,
                        attribution_expires_at,status,next_attempt_at,
                        created_at,updated_at)
                       VALUES(%s,'session_started',%s,'live','p','e',1,
                              'click_12345678',%s,'v1',9999999999,
                              'pending',1,1,1)""",
                    (str(uuid.uuid4()), "c" * 64, context_id),
                )
            conn.commit()
            with conn.cursor() as cur, self.assertRaises(self.psycopg2.Error):
                cur.execute(hardening)
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM x_capi_attribution_contexts")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT count(*) FROM x_capi_outbox")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT count(*) FROM x_capi_dedup")
                self.assertEqual(cur.fetchone()[0], 1)
                self.assertIsNone(
                    cur.execute("SELECT to_regclass('x_capi_schema_meta')")
                )
                self.assertIsNone(cur.fetchone()[0])
        finally:
            conn.close()
            admin = self.psycopg2.connect(TEST_URL)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute("DROP SCHEMA " + schema + " CASCADE")
            admin.close()

    def test_z_owner_and_column_grants_are_enforced(self):
        suffix = uuid.uuid4().hex[:8]
        owner = "xcap_owner_" + suffix
        worker = "xcap_worker_" + suffix
        leaked = "xcap_leaked_" + suffix
        self.__class__.created_roles.extend((owner, worker, leaked))
        admin = self.psycopg2.connect(TEST_URL)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                """SELECT d.datname,
                          COALESCE(bool_or(
                              acl.grantee=0
                              AND acl.privilege_type='CONNECT'
                          ),FALSE),
                          COALESCE(bool_or(
                              acl.grantee=0
                              AND acl.privilege_type='TEMPORARY'
                          ),FALSE)
                     FROM pg_database d
                     CROSS JOIN LATERAL aclexplode(
                         COALESCE(d.datacl,acldefault('d',d.datdba))
                     ) acl
                    WHERE d.datname=ANY(%s)
                    GROUP BY d.datname ORDER BY d.datname""",
                (list(self.connectable_databases),),
            )
            database_acl_snapshot = tuple(cur.fetchall())
            self.assertEqual(
                {row[0] for row in database_acl_snapshot},
                set(self.connectable_databases),
            )
            # unittest cleanups run even when a later assertion raises.  The
            # hard disposable-cluster guard above also makes process-kill residue
            # incapable of changing an AxonOS or shared PostgreSQL cluster.
            self.addCleanup(
                self._restore_public_database_acl, database_acl_snapshot
            )
            cur.execute("CREATE ROLE " + owner + " NOLOGIN")
            cur.execute("CREATE ROLE " + worker + " LOGIN")
            cur.execute("CREATE ROLE " + leaked + " LOGIN")
            for database_name in self.connectable_databases:
                quoted = self.psycopg2.extensions.quote_ident(
                    database_name, admin
                )
                cur.execute(
                    "REVOKE CONNECT ON DATABASE " + quoted + " FROM PUBLIC"
                )
            quoted_current = self.psycopg2.extensions.quote_ident(
                self.database_name, admin
            )
            cur.execute(
                "REVOKE TEMPORARY ON DATABASE " + quoted_current
                + " FROM PUBLIC"
            )
            cur.execute(
                "GRANT CONNECT ON DATABASE " + quoted_current + " TO " + worker
            )
        admin.close()

        # Simulate an accidental historical grant. Migration 004 must remove
        # it even though the role is not one of its two explicit arguments.
        pregrant = self.connect()
        with pregrant.cursor() as cur:
            cur.execute("GRANT USAGE ON SCHEMA " + self.schema + " TO " + leaked)
            cur.execute("GRANT SELECT ON x_capi_outbox TO " + leaked)
        pregrant.commit()
        pregrant.close()

        grants_template = _migration_text(
            "004_x_capi_roles_and_grants.sql", self.schema
        )

        def rendered_grants(worker_role=worker):
            result = grants_template
            for variable, role in (
                ("owner_role", owner), ("worker_role", worker_role),
            ):
                result = result.replace(
                    ':"' + variable + '"', '"' + role + '"'
                )
                result = result.replace(
                    ":'" + variable + "'", "'" + role + "'"
                )
            return result

        role_probe = self.connect()
        with role_probe.cursor() as cur:
            cur.execute("SELECT current_user")
            bootstrap_role = cur.fetchone()[0]
        role_probe.close()
        unsafe_connection = self.connect()
        with unsafe_connection.cursor() as cur:
            with self.assertRaises(self.psycopg2.errors.RaiseException):
                cur.execute(rendered_grants(bootstrap_role))
        unsafe_connection.rollback()
        unsafe_connection.close()

        # Incoming SET ROLE/inheritance into the credential-bearing worker is
        # also rejected, not merely memberships held by the worker itself.
        admin = self.psycopg2.connect(TEST_URL)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("GRANT " + worker + " TO " + leaked)
        admin.close()
        inherited_connection = self.connect()
        with inherited_connection.cursor() as cur:
            with self.assertRaises(self.psycopg2.errors.RaiseException):
                cur.execute(rendered_grants())
        inherited_connection.rollback(); inherited_connection.close()
        admin = self.psycopg2.connect(TEST_URL)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("REVOKE " + worker + " FROM " + leaked)
        admin.close()

        grants = rendered_grants()
        connection = self.connect()
        with connection.cursor() as cur:
            cur.execute(grants)
        connection.close()

        leaked_conn = self.connect()
        with leaked_conn.cursor() as cur:
            cur.execute("SET ROLE " + leaked)
            with self.assertRaises(self.psycopg2.errors.InsufficientPrivilege):
                cur.execute("SELECT conversion_id FROM x_capi_outbox")
        leaked_conn.rollback(); leaked_conn.close()

        worker_conn = self.connect()
        with worker_conn.cursor() as cur:
            cur.execute("SET ROLE " + worker)
            cur.execute(
                """SELECT id,handle_hash,csrf_hash,wallet_hash,policy_epoch,
                          audience_scope,lifecycle_expires_at
                     FROM x_capi_attribution_contexts WHERE FALSE"""
            )
            # clear_pause() uses last_error_code in this UPDATE predicate.
            # PostgreSQL requires SELECT on a WHERE column even when no row
            # matches, so exercise the exact privilege combination here.
            cur.execute(
                """UPDATE x_capi_outbox
                      SET next_attempt_at=next_attempt_at,updated_at=updated_at
                    WHERE status='retrying'
                      AND last_error_code='authentication_failed'"""
            )
            with self.assertRaises(self.psycopg2.errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE x_capi_attribution_contexts SET lifecycle_expires_at=1"
                )
        worker_conn.rollback()
        from axonos_gate import x_capi_worker
        with worker_conn.cursor() as cur:
            cur.execute("SET ROLE " + worker)
        with patch.dict(
            os.environ,
            {
                "X_CAPI_WORKER_DB_ROLE": worker,
                "X_CAPI_OWNER_DB_ROLE": owner,
            },
            clear=False,
        ):
            self.assertTrue(x_capi_worker._schema_ready(worker_conn))
            # Runtime readiness must detect privilege drift after migration,
            # including sensitive identifier visibility and owner-equivalent
            # table powers.  Positive-grant-only attestation would miss both.
            admin = self.psycopg2.connect(TEST_URL)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(
                    "GRANT SELECT (source_key_hash) ON " + self.schema
                    + ".x_capi_outbox TO " + worker + " WITH GRANT OPTION"
                )
            admin.close()
            worker_conn.rollback()
            with worker_conn.cursor() as cur:
                self.assertFalse(x_capi_worker._database_role_is_isolated(cur))
            admin = self.psycopg2.connect(TEST_URL)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(
                    "REVOKE SELECT (source_key_hash) ON " + self.schema
                    + ".x_capi_outbox FROM " + worker
                )
                cur.execute(
                    "GRANT TRUNCATE ON " + self.schema
                    + ".x_capi_outbox TO " + worker
                )
            admin.close()
            worker_conn.rollback()
            with worker_conn.cursor() as cur:
                self.assertFalse(x_capi_worker._database_role_is_isolated(cur))
            admin = self.psycopg2.connect(TEST_URL)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(
                    "REVOKE TRUNCATE ON " + self.schema
                    + ".x_capi_outbox FROM " + worker
                )
                cur.execute(
                    "GRANT SELECT ON " + self.schema
                    + ".x_capi_outbox TO " + leaked
                )
            admin.close()
            worker_conn.rollback()
            with worker_conn.cursor() as cur:
                self.assertFalse(x_capi_worker._database_role_is_isolated(cur))
            admin = self.psycopg2.connect(TEST_URL)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(
                    "REVOKE SELECT ON " + self.schema
                    + ".x_capi_outbox FROM " + leaked
                )
                cur.execute(
                    "ALTER TABLE " + self.schema
                    + ".x_capi_outbox OWNER TO " + worker
                )
            admin.close()
            worker_conn.rollback()
            with worker_conn.cursor() as cur:
                self.assertFalse(x_capi_worker._database_role_is_isolated(cur))
            admin = self.psycopg2.connect(TEST_URL)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(
                    "ALTER TABLE " + self.schema
                    + ".x_capi_outbox OWNER TO " + owner
                )
            admin.close()
            repair = self.connect()
            with repair.cursor() as cur:
                cur.execute(grants)
            repair.close()
            worker_conn.rollback()
            with worker_conn.cursor() as cur:
                self.assertTrue(x_capi_worker._database_role_is_isolated(cur))

            # A grants-only rerun must not bless an existing v4 label whose
            # catalog contains a hidden side effect. PostgreSQL checks trigger
            # function EXECUTE at CREATE time, so revoke it afterward to prove
            # the exact catalog check catches what privilege probing cannot.
            trigger_function = self.schema + ".xcapitest_hidden_trigger"
            admin = self.psycopg2.connect(TEST_URL)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(
                    "CREATE FUNCTION " + trigger_function
                    + "() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER "
                      "AS 'BEGIN RETURN NEW; END'"
                )
                cur.execute(
                    "CREATE TRIGGER xcapitest_hidden BEFORE INSERT ON "
                    + self.schema + ".x_capi_outbox FOR EACH ROW EXECUTE "
                    "FUNCTION " + trigger_function + "()"
                )
                cur.execute(
                    "REVOKE ALL ON FUNCTION " + trigger_function
                    + "() FROM PUBLIC, " + worker
                )
            admin.close()
            worker_conn.rollback()
            with worker_conn.cursor() as cur:
                self.assertFalse(x_capi_worker._database_role_is_isolated(cur))
            rejected = self.connect()
            with rejected.cursor() as cur:
                with self.assertRaises(self.psycopg2.errors.RaiseException):
                    cur.execute(grants)
            rejected.rollback(); rejected.close()
            admin = self.psycopg2.connect(TEST_URL)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(
                    "DROP TRIGGER xcapitest_hidden ON "
                    + self.schema + ".x_capi_outbox"
                )
                cur.execute("DROP FUNCTION " + trigger_function + "()")
                cur.execute(
                    "ALTER TABLE " + self.schema
                    + ".x_capi_outbox ALTER COLUMN attempt_count SET DEFAULT 7"
                )
            admin.close()
            worker_conn.rollback()
            with worker_conn.cursor() as cur:
                self.assertFalse(x_capi_worker._database_role_is_isolated(cur))
            admin = self.psycopg2.connect(TEST_URL)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(
                    "ALTER TABLE " + self.schema
                    + ".x_capi_outbox ALTER COLUMN attempt_count SET DEFAULT 0"
                )
            admin.close()
            worker_conn.rollback()
            with worker_conn.cursor() as cur:
                self.assertTrue(x_capi_worker._database_role_is_isolated(cur))
        from axonos_gate import x_capi
        cfg = self.worker_cfg()
        ticket = self.lifecycle_ticket(handle="k" * 43, csrf="m" * 43)
        wallet = "0x" + "7" * 40
        envelope = {
            "v": 1, "action": "event", "milestone": "wallet_verified",
            "context_token": "t" * 40, "wallet_address": wallet,
            "source_key": wallet, "event_timestamp_ms": 200_000,
            "metadata": {
                "allow_context_binding": True, "credit_source": None,
                "payment_rail": None, "chain_id": None,
            },
        }
        with patch.object(x_capi, "load_config", return_value=cfg), patch.object(
            x_capi, "keyed_internal_hash",
            side_effect=lambda label, value: hashlib.sha256(
                (label + "\0" + value).encode()
            ).hexdigest(),
        ), patch.object(
            x_capi_worker, "_ticket_for_ingest", return_value=ticket
        ):
            self.assertTrue(
                x_capi_worker.enforce_config_guard(worker_conn, cfg, now=199.0)
            )
            self.assertEqual(
                x_capi_worker._ingest_event(worker_conn, envelope, 200.0),
                "mapping_disabled",
            )
            response = x_capi_worker.process_consent_request(
                worker_conn,
                {
                    "v": 1, "action": "consent", "operation": "revoke",
                    "context_token": "t" * 40, "csrf_token": ticket["csrf"],
                    "request_timestamp_ms": 201_000,
                },
                201.0,
            )
        self.assertEqual(response, {"v": 1, "ok": True, "state": "revoked"})
        x_capi_worker.expire_and_cleanup(
            worker_conn, 202.0, 24, "v1", policy_epoch=POLICY_EPOCH,
            audience_scope=AUDIENCE_SCOPE, context_limit=100,
        )
        worker_conn.close()


if __name__ == "__main__":
    unittest.main()
