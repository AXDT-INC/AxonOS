"""Synthetic store/concurrency checks; PostgreSQL tests require an explicit /tmp cluster.

X_HANDOFF_TEST_DSN must name the newly created capi_handoff_test database on a
/tmp/axon-handoff-pg15-*/socket Unix socket. No network or production DSN is
accepted. Migration tests use temporary schemas; store tests own all data in this
explicitly disposable test database and run sequentially.
"""
import hashlib
import os
from pathlib import Path
import threading
import time
import unittest
import uuid

from axonos_gate.x_capi_handoff import HandoffError, new_token, token_hash, validate_schema
from axonos_gate.x_capi_handoff_store import HandoffStore, close_import_by_handle, SOURCE_WINDOW_MS

DSN = os.environ.get("X_HANDOFF_TEST_DSN")
CONFIRMED = os.environ.get("X_HANDOFF_TEST_DISPOSABLE_CONFIRM") == "DESTROY_TMP_HANDOFF_DATABASE"
ROOT = Path(__file__).resolve().parents[1]
GENERATION = "11111111-1111-4111-8111-111111111111"
NOW = 1791936000000


def request(**body):
    return dict(v=2,request_id=str(uuid.uuid4()),**body)


class StoreBoundaryTests(unittest.TestCase):
    def test_capacity_can_only_be_reduced(self):
        with self.assertRaises(ValueError):
            HandoffStore(GENERATION,42,"a"*64,{"test":b"x"*32},"test",limits={"sources":10001})

    def test_clock_rejects_bool_and_overflow(self):
        for now in (True,0,9007199254740991):
            store = HandoffStore(GENERATION,42,"a"*64,{"test":b"x"*32},"test",clock_ms=lambda:now)
            with self.assertRaises(HandoffError) as caught:
                store._now()
            self.assertEqual(caught.exception.code,"CLOCK_UNTRUSTED")

    def test_no_service_keys_or_raw_capabilities_in_storage_schema(self):
        sql = (ROOT/"migrations/008_x_capi_handoff.sql").read_text()
        for forbidden in ("raw_handoff", "wallet_credentials", "service_mac_key", "twclid TEXT"):
            self.assertNotIn(forbidden,sql)
        self.assertIn("UNIQUE(generation,app_owner_hash)",sql)
        self.assertIn("UNIQUE(generation,landing_owner_hash)",sql)


@unittest.skipUnless(DSN and CONFIRMED,"explicit disposable Unix-socket PostgreSQL 15 required")
class HandoffPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.pg = psycopg2
        parts = psycopg2.extensions.parse_dsn(DSN)
        if (set(parts)-{"host","port","dbname","user"} or parts.get("dbname")!="capi_handoff_test"
                or parts.get("user")!="cluadmin" or not parts.get("host","").startswith("/tmp/axon-handoff-pg15-")
                or not parts["host"].endswith("/socket")):
            raise RuntimeError("refuse non-disposable handoff test DSN")
        cls.admin = psycopg2.connect(DSN)
        cls.admin.autocommit=True
        with cls.admin.cursor() as cur:
            cur.execute("SELECT current_setting('server_version_num')::int,current_setting('data_directory'),pg_is_in_recovery(),current_database(),current_user")
            version,directory,recovery,db,user = cur.fetchone()
            if not (150000<=version<160000 and directory.startswith("/tmp/axon-handoff-pg15-")
                    and not recovery and (db,user)==("capi_handoff_test","cluadmin")):
                raise RuntimeError("refuse unverified database")
            cur.execute("SELECT schema_version FROM x_capi_schema_meta WHERE singleton")
            if cur.fetchone()!=(5,):
                raise RuntimeError("reviewed schema 5 required")
        cls.worker_dsn = dict(parts,user="handoff_worker")

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def setUp(self):
        with self.admin.cursor() as cur:
            cur.execute("TRUNCATE x_capi_handoff_generations,x_capi_handoff_sources,x_capi_handoff_app_owners,x_capi_handoff_transactions,x_capi_handoff_imports,x_capi_attribution_contexts,x_capi_outbox,x_capi_config_guard,x_capi_revocation_tombstones CASCADE")
            cur.execute("""INSERT INTO x_capi_config_guard VALUES
                (TRUE,42,%s,'dry_run','x-capi-handoff-v2',%s,%s,%s,1)""", ("d"*64,"a"*64,"b"*64,"c"*64))
            cur.execute("UPDATE x_capi_capacity SET context_count=0,tombstone_count=0,outbox_active_count=0")
        self.now=NOW
        self.store=HandoffStore(GENERATION,42,"a"*64,{"test":b"x"*32},"test",clock_ms=lambda:self.now)
        self.conn=self.pg.connect(**self.worker_dsn)
        with self.conn.cursor() as cur:
            self.store.provision_generation(cur,activate=True)
        self.conn.commit()
        self.owner=token_hash(new_token(),"app-owner")
        self.source=token_hash(new_token(),"landing-owner")
        self.closed=[]

    def tearDown(self):
        self.conn.close()

    def source_req(self,op,**kw):
        return request(epoch=42,generation=GENERATION,op=op,**kw)

    def capture(self,owner=None,req=None,connection=None):
        req=req or self.source_req("capture",owner_hash=owner or self.source,observation_id=str(uuid.uuid4()),
            observed_at_ms=self.now-1000,original_expires_at_ms=self.now-1000+SOURCE_WINDOW_MS,
            landing_consented_at_ms=self.now,policy="landing-x-v2",gpc=False,twclid="synthetic_click_Aa19-Zz72")
        with (connection or self.conn).cursor() as cur:
            out=self.store.capture(cur,req)
        validate_schema("source-capture-success",out)
        return req,out

    def prepare(self,owner=None):
        owner=owner or self.owner
        req=request(choice="grant",policy="app-x-v2")
        with self.conn.cursor() as cur:
            out=self.store.prepare(cur,owner,req,new_owner=True)
        validate_schema("prepared",out)
        return req,out

    def begin(self,owner=None):
        req=request(choice="grant",policy="app-x-v2",destination="desktop")
        with self.conn.cursor() as cur:
            out=self.store.begin(cur,owner or self.owner,req)
        validate_schema("app-granted",out)
        return req,out

    def approved(self):
        self.capture();self.prepare();_,begun=self.begin()
        req=self.source_req("authorize",owner_hash=self.source,handoff=begun["handoff"])
        with self.conn.cursor() as cur:
            result=self.store.authorize(cur,req)
        validate_schema("source-authorize-success",result)
        return begun["handoff"]

    def create_context(self,cur,source,tx,cutoff,effective):
        identifier=str(uuid.uuid4()); handle=hashlib.sha256(identifier.encode()).hexdigest()
        cur.execute("""INSERT INTO x_capi_attribution_contexts
            (id,handle_hash,csrf_hash,consent_state,mode_scope,policy_version,policy_epoch,
             audience_scope,lifecycle_expires_at,twclid,consented_at,expires_at,first_seen_at,updated_at)
            VALUES (%s,%s,%s,'granted','dry_run','x-capi-handoff-v2',42,%s,%s,%s,%s,%s,%s,%s)""",
            (identifier,handle,"c"*64,"a"*64,effective/1000,source["twclid"],cutoff/1000,effective/1000,cutoff/1000,cutoff/1000))
        return dict(context_id=identifier,context_handle_hash=handle,context_ciphertext="A"*128,
                    csrf="c"*43,ownership_id=str(uuid.uuid4()))

    def close(self,cur,context_id):
        self.closed.append(context_id)
        cur.execute("UPDATE x_capi_attribution_contexts SET consent_state='revoked',twclid=NULL,expires_at=NULL,updated_at=%s WHERE id=%s", (self.now/1000,context_id))

    def redeem(self,handoff,connection=None,owner=None):
        with (connection or self.conn).cursor() as cur:
            out=self.store.redeem(cur,owner or self.owner,handoff,self.create_context,lambda _:dict(csrf="c"*43))
        validate_schema("redeemed",out)
        return out

    def test_first_touch_encryption_and_original_expiry(self):
        req,result=self.capture()
        self.now+=500
        _,retry=self.capture(req=req)
        self.assertEqual(result,retry)
        with self.conn.cursor() as cur:
            cur.execute("SELECT twclid_ciphertext,encryption_nonce,original_expires_at_ms FROM x_capi_handoff_sources")
            cipher,nonce,expiry=cur.fetchone()
        self.assertNotIn(b"synthetic_click",bytes(cipher));self.assertEqual(len(nonce),12)
        self.assertEqual(expiry,req["original_expires_at_ms"])
        with self.assertRaises(HandoffError) as caught:self.capture()
        self.assertEqual(caught.exception.code,"FIRST_TOUCH_LOCKED")

    def test_changed_idempotent_capture_rejected(self):
        req,_=self.capture();req["twclid"]="synthetic_other_Zz72"
        with self.assertRaises(HandoffError) as caught:self.capture(req=req)
        self.assertEqual(caught.exception.code,"IDEMPOTENCY_CONFLICT")

    def test_secret_shaped_click_rejected(self):
        req=self.source_req("capture",owner_hash=self.source,observation_id=str(uuid.uuid4()),observed_at_ms=NOW,
            original_expires_at_ms=NOW+SOURCE_WINDOW_MS,landing_consented_at_ms=NOW,policy="landing-x-v2",gpc=False,twclid="0x"+"1"*40)
        with self.assertRaises(HandoffError) as caught:self.capture(req=req)
        self.assertEqual(caught.exception.code,"INVALID_CLICK")

    def test_close_before_capture_is_durable_and_capacity_reserved(self):
        req=self.source_req("revoke",owner_hash=self.source,reason="withdrawn")
        with self.conn.cursor() as cur:result=self.store.revoke(cur,req,self.close)
        self.conn.commit();self.assertEqual(result["state"],"withdrawn")
        with self.assertRaises(HandoffError) as caught:self.capture()
        self.assertEqual(caught.exception.code,"WITHDRAWN")

    def test_unrepresentable_close_quarantines_generation(self):
        self.store.limits["sources"]=1;self.capture();self.conn.commit()
        req=self.source_req("revoke",owner_hash="b"*64,reason="gpc")
        with self.assertRaises(HandoffError) as caught:
            with self.conn.cursor() as cur:self.store.revoke(cur,req,self.close)
        self.assertTrue(caught.exception.durable_error)
        self.conn.commit()
        with self.conn.cursor() as cur:
            cur.execute("SELECT state FROM x_capi_handoff_generations")
            self.assertEqual(cur.fetchone()[0],"quarantined")

    def test_missing_cookie_cannot_recover_prepare(self):
        req,_=self.prepare()
        with self.assertRaises(HandoffError) as caught:
            with self.conn.cursor() as cur:self.store.prepare(cur,"b"*64,req,new_owner=True)
        self.assertEqual(caught.exception.code,"RESULT_UNAVAILABLE")
        with self.conn.cursor() as cur:result=self.store.prepare(cur,self.owner,req,new_owner=False)
        self.assertEqual(result["state"],"prepared")

    def test_hash_only_begin_and_four_attempt_cap(self):
        self.prepare();req,result=self.begin()
        with self.assertRaises(HandoffError) as caught:
            with self.conn.cursor() as cur:self.store.begin(cur,self.owner,req)
        self.assertEqual(caught.exception.code,"RESULT_UNAVAILABLE")
        for _ in range(3):self.begin()
        with self.assertRaises(HandoffError) as caught:self.begin()
        self.assertEqual(caught.exception.code,"RATE_LIMITED")
        with self.conn.cursor() as cur:
            cur.execute("SELECT transaction_hash FROM x_capi_handoff_transactions")
            self.assertTrue(all(len(row[0])==32 for row in cur.fetchall()))
            cur.execute("SELECT transaction_count FROM x_capi_handoff_generations")
            self.assertEqual(cur.fetchone()[0],4)

    def test_redemption_recovery_same_result_and_owner_binding(self):
        handoff=self.approved();result=self.redeem(handoff)
        self.assertEqual(result,self.redeem(handoff))
        with self.conn.cursor() as cur:
            recovered=self.store.recover(cur,self.owner,handoff,lambda _:dict(csrf="c"*43))
            cur.execute("SELECT context_handle_hash FROM x_capi_handoff_imports")
            handle=cur.fetchone()[0]
            imported=self.store.lock_import(cur,handle,self.owner)
        self.assertEqual(result,recovered)
        self.assertEqual(imported["app_owner_hash"].hex(),self.owner)
        with self.assertRaises(HandoffError) as caught:self.redeem(handoff,owner="f"*64)
        self.assertEqual(caught.exception.code,"OWNER_MISMATCH")
        with self.assertRaises(HandoffError):
            with self.conn.cursor() as cur:self.store.lock_import(cur,handle,"f"*64)
        with self.conn.cursor() as cur:
            cur.execute("SELECT twclid_ciphertext FROM x_capi_handoff_sources")
            self.assertIsNone(cur.fetchone()[0])

    def test_callback_failure_rolls_back_all_import_state(self):
        handoff=self.approved();self.conn.commit()
        def fails(cur,*args):
            self.create_context(cur,*args)
            raise RuntimeError("synthetic failure")
        with self.assertRaises(RuntimeError):
            with self.conn.cursor() as cur:self.store.redeem(cur,self.owner,handoff,fails,lambda _: {})
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_handoff_imports");self.assertEqual(cur.fetchone()[0],0)
            cur.execute("SELECT count(*) FROM x_capi_attribution_contexts");self.assertEqual(cur.fetchone()[0],0)
        self.assertEqual(self.redeem(handoff)["state"],"redeemed")

    def test_withdraw_response_and_repeat_are_schema_valid(self):
        handoff=self.approved();self.redeem(handoff)
        req=request(reason="withdrawn")
        with self.conn.cursor() as cur:
            first=self.store.withdraw(cur,self.owner,req,self.close)
            second=self.store.withdraw(cur,self.owner,req,self.close)
        validate_schema("withdrawn",first);self.assertEqual(first,second)
        self.assertEqual(len(self.closed),1)
        with self.assertRaises(HandoffError):self.redeem(handoff)

    def test_retained_privacy_capability_works_without_runtime_or_owner(self):
        self.redeem(self.approved())
        with self.conn.cursor() as cur:
            cur.execute("SELECT context_handle_hash FROM x_capi_handoff_imports");handle=cur.fetchone()[0]
            self.assertTrue(close_import_by_handle(cur,handle,self.close,self.now))
            cur.execute("SELECT state FROM x_capi_handoff_imports");self.assertEqual(cur.fetchone()[0],"closed")
        self.assertEqual(len(self.closed),1)

    def test_expired_handoff_cannot_renew_or_recover(self):
        handoff=self.approved();result=self.redeem(handoff)
        self.now+=300000
        with self.assertRaises(HandoffError) as caught:self.redeem(handoff)
        self.assertEqual(caught.exception.code,"EXPIRED")
        with self.conn.cursor() as cur:
            self.store.cleanup(cur,self.close)
            cur.execute("SELECT context_ciphertext FROM x_capi_handoff_imports");self.assertIsNone(cur.fetchone()[0])

    def test_generation_missing_retired_and_control_regression_fail_closed(self):
        with self.conn.cursor() as cur:
            self.store.control_version(cur,12)
            with self.assertRaises(HandoffError):self.store.control_version(cur,11)
            self.store.retire(cur,self.close)
            with self.assertRaises(HandoffError) as caught:self.store.generation_guard(cur)
            self.assertEqual(caught.exception.code,"GENERATION_RETIRED")
        another=HandoffStore(str(uuid.uuid4()),43,"a"*64,{"test":b"x"*32},"test")
        with self.assertRaises(HandoffError):
            with self.conn.cursor() as cur:another.generation_guard(cur)

    def database_now(self):
        with self.admin.cursor() as cur:
            cur.execute("SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint")
            return cur.fetchone()[0]

    def wait_for_database_lock(self, connection):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.admin.cursor() as cur:
                cur.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (connection.get_backend_pid(),))
                if cur.fetchone() == ("Lock",):
                    return
            time.sleep(0.005)
        self.fail("synthetic operation did not reach its PostgreSQL lock wait")

    def expire_while_locked(self, operation, lock_sql, lock_parameters, deadline):
        blocker = self.pg.connect(**self.worker_dsn)
        waiting = self.pg.connect(**self.worker_dsn)
        result = []
        def run():
            try:
                with waiting.cursor() as cur:
                    result.append(operation(cur))
                waiting.commit()
            except HandoffError as exc:
                result.append(exc.code); waiting.rollback()
            except Exception as exc:
                result.append(type(exc).__name__); waiting.rollback()
        thread = threading.Thread(target=run)
        try:
            with blocker.cursor() as cur:
                cur.execute(lock_sql, lock_parameters)
            thread.start()
            self.wait_for_database_lock(waiting)
            while self.database_now() <= deadline:
                time.sleep(0.005)
            blocker.rollback()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, ["EXPIRED"])
        finally:
            blocker.rollback(); thread.join(3)
            waiting.close(); blocker.close()

    def real_time_store(self):
        self.now = self.database_now()
        self.store = HandoffStore(GENERATION,42,"a"*64,{"test":b"x"*32},"test")

    def test_redemption_deadline_crossed_waiting_on_transaction_lock(self):
        self.real_time_store()
        handoff = self.approved(); self.conn.commit()
        deadline = self.database_now() + 250
        with self.admin.cursor() as cur:
            cur.execute("UPDATE x_capi_handoff_transactions SET expires_at_ms=%s", (deadline,))
        self.expire_while_locked(
            lambda cur:self.store.redeem(cur,self.owner,handoff,self.create_context,lambda _:dict(csrf="c"*43)),
            "SELECT transaction_hash FROM x_capi_handoff_transactions FOR UPDATE", (), deadline)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_handoff_imports"); self.assertEqual(cur.fetchone()[0],0)
            cur.execute("SELECT state,expires_at_ms FROM x_capi_handoff_transactions")
            self.assertEqual(cur.fetchone(),("source_approved",deadline))

    def test_redemption_deadline_crossed_waiting_on_context_capacity(self):
        self.real_time_store()
        handoff = self.approved(); self.conn.commit()
        deadline = self.database_now() + 250
        with self.admin.cursor() as cur:
            cur.execute("UPDATE x_capi_handoff_transactions SET expires_at_ms=%s", (deadline,))
        def creates_after_capacity_lock(cur,*args):
            cur.execute("SELECT singleton FROM x_capi_capacity WHERE singleton FOR UPDATE")
            return self.create_context(cur,*args)
        self.expire_while_locked(
            lambda cur:self.store.redeem(cur,self.owner,handoff,creates_after_capacity_lock,lambda _:dict(csrf="c"*43)),
            "SELECT singleton FROM x_capi_capacity WHERE singleton FOR UPDATE", (), deadline)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_handoff_imports"); self.assertEqual(cur.fetchone()[0],0)
            cur.execute("SELECT count(*) FROM x_capi_attribution_contexts"); self.assertEqual(cur.fetchone()[0],0)

    def test_authorization_deadline_crossed_waiting_on_transaction_lock(self):
        self.real_time_store(); self.capture(); self.prepare(); _, begun = self.begin(); self.conn.commit()
        deadline = self.database_now() + 250
        with self.admin.cursor() as cur:
            cur.execute("UPDATE x_capi_handoff_transactions SET expires_at_ms=%s", (deadline,))
        req = self.source_req("authorize",owner_hash=self.source,handoff=begun["handoff"])
        self.expire_while_locked(lambda cur:self.store.authorize(cur,req),
            "SELECT transaction_hash FROM x_capi_handoff_transactions FOR UPDATE", (), deadline)
        with self.conn.cursor() as cur:
            cur.execute("SELECT state FROM x_capi_handoff_sources"); self.assertEqual(cur.fetchone()[0],"captured")

    def test_capture_observation_expires_waiting_for_admission(self):
        self.real_time_store(); self.conn.commit()
        deadline = self.database_now() + 250
        req = self.source_req("capture",owner_hash=self.source,observation_id=str(uuid.uuid4()),
            observed_at_ms=deadline-110000,original_expires_at_ms=deadline-110000+SOURCE_WINDOW_MS,
            landing_consented_at_ms=self.now,policy="landing-x-v2",gpc=False,twclid="synthetic_click_Aa19-Zz72")
        capacity_lock = int.from_bytes(hashlib.sha256(b"handoff-total-capacity\x00").digest()[:8],"big",signed=True)
        self.expire_while_locked(lambda cur:self.store.capture(cur,req),
            "SELECT pg_advisory_xact_lock(%s)", (capacity_lock,), deadline)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_handoff_sources"); self.assertEqual(cur.fetchone()[0],0)

    def test_existing_prepare_owner_expires_during_owner_lock_wait(self):
        self.real_time_store(); prepared, _ = self.prepare(); self.conn.commit()
        deadline = self.database_now() + 250
        with self.admin.cursor() as cur:
            cur.execute("UPDATE x_capi_handoff_app_owners SET owner_expires_at_ms=%s", (deadline,))
        self.expire_while_locked(lambda cur:self.store.prepare(cur,self.owner,prepared),
            "SELECT app_owner_hash FROM x_capi_handoff_app_owners FOR UPDATE", (), deadline)
        with self.conn.cursor() as cur:
            cur.execute("SELECT owner_expires_at_ms FROM x_capi_handoff_app_owners")
            self.assertEqual(cur.fetchone()[0],deadline)

    def test_begin_owner_expires_during_admission_wait(self):
        self.real_time_store(); self.prepare(); self.conn.commit()
        deadline = self.database_now() + 250
        with self.admin.cursor() as cur:
            cur.execute("UPDATE x_capi_handoff_app_owners SET owner_expires_at_ms=%s", (deadline,))
        capacity_lock = int.from_bytes(hashlib.sha256(b"handoff-total-capacity\x00").digest()[:8],"big",signed=True)
        req = request(choice="grant",policy="app-x-v2",destination="desktop")
        self.expire_while_locked(lambda cur:self.store.begin(cur,self.owner,req),
            "SELECT pg_advisory_xact_lock(%s)", (capacity_lock,), deadline)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_handoff_transactions"); self.assertEqual(cur.fetchone()[0],0)
            cur.execute("SELECT owner_expires_at_ms,open_transaction_count FROM x_capi_handoff_app_owners")
            self.assertEqual(cur.fetchone(),(deadline,0))

    def test_recovery_deadline_crossed_waiting_on_import_lock(self):
        self.real_time_store(); handoff = self.approved(); self.redeem(handoff); self.conn.commit()
        deadline = self.database_now() + 250
        with self.admin.cursor() as cur:
            cur.execute("UPDATE x_capi_handoff_transactions SET expires_at_ms=%s", (deadline,))
            cur.execute("UPDATE x_capi_handoff_imports SET result_recover_until_ms=%s", (deadline,))
        self.expire_while_locked(
            lambda cur:self.store.recover(cur,self.owner,handoff,lambda _:dict(csrf="c"*43)),
            "SELECT context_id FROM x_capi_handoff_imports FOR UPDATE", (), deadline)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_handoff_imports"); self.assertEqual(cur.fetchone()[0],1)
            cur.execute("SELECT state FROM x_capi_handoff_transactions"); self.assertEqual(cur.fetchone()[0],"redeemed")

    def test_parallel_redemption_has_one_import_and_identical_recovery(self):
        handoff=self.approved();self.conn.commit();barrier=threading.Barrier(2);results=[];errors=[]
        def run():
            conn=self.pg.connect(**self.worker_dsn)
            try:
                barrier.wait(timeout=3);results.append(self.redeem(handoff,connection=conn));conn.commit()
            except Exception as exc:errors.append(type(exc).__name__);conn.rollback()
            finally:conn.close()
        threads=[threading.Thread(target=run) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(5)
        self.assertFalse(errors);self.assertEqual(len(results),2);self.assertEqual(results[0],results[1])
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_handoff_imports");self.assertEqual(cur.fetchone()[0],1)

    def test_parallel_capture_and_close_never_reopens(self):
        self.conn.commit();barrier=threading.Barrier(2);errors=[]
        def capture():
            conn=self.pg.connect(**self.worker_dsn)
            try:
                barrier.wait(timeout=3);self.capture(connection=conn);conn.commit()
            except HandoffError as exc:
                if exc.code!="WITHDRAWN":errors.append(exc.code)
                conn.rollback()
            except Exception as exc:errors.append(type(exc).__name__);conn.rollback()
            finally:conn.close()
        def revoke():
            conn=self.pg.connect(**self.worker_dsn)
            try:
                barrier.wait(timeout=3)
                with conn.cursor() as cur:self.store.revoke(cur,self.source_req("revoke",owner_hash=self.source,reason="withdrawn"),self.close)
                conn.commit()
            except Exception as exc:errors.append(type(exc).__name__);conn.rollback()
            finally:conn.close()
        threads=[threading.Thread(target=f) for f in (capture,revoke)]
        for t in threads:t.start()
        for t in threads:t.join(5)
        self.assertFalse(errors)
        with self.conn.cursor() as cur:
            cur.execute("SELECT state,twclid_ciphertext FROM x_capi_handoff_sources")
            self.assertEqual(cur.fetchone(),("closed",None))

    def test_database_rejects_required_null_observation_and_duplicate_source_import(self):
        self.capture();self.conn.commit()
        with self.assertRaises(self.pg.IntegrityError):
            with self.conn.cursor() as cur:cur.execute("UPDATE x_capi_handoff_sources SET landing_consented_at_ms=NULL")
        self.conn.rollback()
        handoff=self.approved_after_capture();self.redeem(handoff)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_handoff_imports");self.assertEqual(cur.fetchone()[0],1)
        with self.assertRaises(HandoffError):self.begin()

    def approved_after_capture(self):
        self.prepare();_,begun=self.begin()
        with self.conn.cursor() as cur:self.store.authorize(cur,self.source_req("authorize",owner_hash=self.source,handoff=begun["handoff"]))
        return begun["handoff"]

    def test_unknown_source_tombstone_requires_reviewed_retirement(self):
        with self.conn.cursor() as cur:
            self.store.revoke(cur,self.source_req("revoke",owner_hash=self.source,reason="withdrawn"),self.close)
        self.now+=7862400001
        with self.conn.cursor() as cur:
            self.store.cleanup(cur,self.close)
            cur.execute("SELECT count(*) FROM x_capi_handoff_sources")
            self.assertEqual(cur.fetchone()[0],1)
            self.store.retire(cur,self.close)
            self.store.cleanup(cur,self.close)
            cur.execute("SELECT count(*) FROM x_capi_handoff_sources")
            self.assertEqual(cur.fetchone()[0],0)

    def test_expired_assignment_retries_preserve_original_source_expiry(self):
        first=self.approved()
        with self.conn.cursor() as cur:
            cur.execute("SELECT original_expires_at_ms FROM x_capi_handoff_sources")
            original=cur.fetchone()[0]
        self.now+=300001
        _,second=self.begin()
        with self.conn.cursor() as cur:
            self.store.authorize(cur,self.source_req("authorize",owner_hash=self.source,handoff=second["handoff"]))
            cur.execute("SELECT original_expires_at_ms FROM x_capi_handoff_sources")
            self.assertEqual(cur.fetchone()[0],original)
        self.assertEqual(self.redeem(second["handoff"])["state"],"redeemed")
        with self.assertRaises(HandoffError):self.redeem(first)

    def test_two_sources_cannot_be_consumed_by_same_owner(self):
        first=self.approved()
        other_source=token_hash(new_token(),"landing-owner")
        self.capture(owner=other_source)
        _,second=self.begin()
        with self.conn.cursor() as cur:self.store.authorize(cur,self.source_req("authorize",owner_hash=other_source,handoff=second["handoff"]))
        self.redeem(first)
        with self.assertRaises(HandoffError) as caught:self.redeem(second["handoff"])
        self.assertEqual(caught.exception.code,"FIRST_TOUCH_LOCKED")
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_handoff_imports")
            self.assertEqual(cur.fetchone()[0],1)

    def test_control_version_regression_stays_quarantined(self):
        with self.conn.cursor() as cur:self.store.control_version(cur,12)
        self.conn.commit()
        with self.assertRaises(HandoffError) as caught:
            with self.conn.cursor() as cur:self.store.control_version(cur,11)
        self.assertTrue(caught.exception.durable_error)
        self.conn.commit()
        with self.assertRaises(HandoffError):
            with self.conn.cursor() as cur:self.store.control_version(cur,13)

    def test_source_request_identity_collision_returns_contract_error(self):
        captured,_=self.capture()
        conflicting=dict(captured,owner_hash="b"*64)
        with self.assertRaises(HandoffError) as caught:self.capture(req=conflicting)
        self.assertEqual(caught.exception.code,"IDEMPOTENCY_CONFLICT")
        close=self.source_req("revoke",owner_hash=self.source,reason="withdrawn")
        with self.conn.cursor() as cur:self.store.revoke(cur,close,self.close)
        with self.assertRaises(HandoffError) as caught:
            with self.conn.cursor() as cur:self.store.revoke(cur,dict(close,owner_hash="b"*64),self.close)
        self.assertEqual(caught.exception.code,"IDEMPOTENCY_CONFLICT")

    def test_authorize_request_identity_collision_returns_contract_error(self):
        self.capture();self.prepare();_,first=self.begin();_,second=self.begin()
        req=self.source_req("authorize",owner_hash=self.source,handoff=first["handoff"])
        with self.conn.cursor() as cur:self.store.authorize(cur,req)
        with self.assertRaises(HandoffError) as caught:
            with self.conn.cursor() as cur:self.store.authorize(cur,dict(req,handoff=second["handoff"]))
        self.assertEqual(caught.exception.code,"IDEMPOTENCY_CONFLICT")

    def test_capacity_counts_retained_rows_across_generations(self):
        self.capture();self.prepare();self.begin()
        with self.conn.cursor() as cur:
            self.store.retire(cur,self.close)
            cur.execute("UPDATE x_capi_config_guard SET max_policy_epoch=43")
            generation=str(uuid.uuid4())
            new=HandoffStore(generation,43,"a"*64,{"test":b"x"*32},"test",clock_ms=lambda:self.now,
                             limits={"sources":2,"app_owners":2,"transactions":2})
            new.provision_generation(cur,activate=True)
            source_req=self.source_req("capture",owner_hash="b"*64,observation_id=str(uuid.uuid4()),
                observed_at_ms=self.now,original_expires_at_ms=self.now+SOURCE_WINDOW_MS,
                landing_consented_at_ms=self.now,policy="landing-x-v2",gpc=False,twclid="synthetic_next_Xy27")
            source_req.update(generation=generation,epoch=43)
            new.capture(cur,source_req)
            new.prepare(cur,"b"*64,request(choice="grant",policy="app-x-v2"),new_owner=True)
            new.begin(cur,"b"*64,request(choice="grant",policy="app-x-v2",destination="desktop"))
            for operation in (
                lambda:new.capture(cur,dict(source_req,request_id=str(uuid.uuid4()),owner_hash="c"*64,observation_id=str(uuid.uuid4()))),
                lambda:new.prepare(cur,"c"*64,request(choice="grant",policy="app-x-v2"),new_owner=True),
                lambda:new.begin(cur,"b"*64,request(choice="grant",policy="app-x-v2",destination="desktop")),
            ):
                with self.assertRaises(HandoffError) as caught:operation()
                self.assertEqual(caught.exception.code,"RATE_LIMITED")
            cur.execute("SELECT sum(source_count),sum(app_owner_count),sum(transaction_count) FROM x_capi_handoff_generations")
            self.assertEqual(cur.fetchone(),(2,2,2))

    def test_runtime_retired_generation_cleanup_reclaims_global_capacity(self):
        from types import SimpleNamespace
        from axonos_gate.x_capi_handoff_worker import WorkerHandoffRuntime
        self.redeem(self.approved())
        with self.conn.cursor() as cur:
            self.store.retire(cur,self.close)
            cur.execute("UPDATE x_capi_config_guard SET max_policy_epoch=43")
            generation=str(uuid.uuid4())
            new=HandoffStore(generation,43,"a"*64,{"test":b"x"*32},"test",clock_ms=lambda:self.now,
                             limits={"sources":1,"app_owners":1,"transactions":1})
            new.provision_generation(cur,activate=True)
            def new_capture():
                req=self.source_req("capture",owner_hash="b"*64,observation_id=str(uuid.uuid4()),
                    observed_at_ms=self.now,original_expires_at_ms=self.now+SOURCE_WINDOW_MS,
                    landing_consented_at_ms=self.now,policy="landing-x-v2",gpc=False,twclid="synthetic_next_Xy27")
                req.update(generation=generation,epoch=43)
                return new.capture(cur,req)
            with self.assertRaises(HandoffError) as caught:new_capture()
            self.assertEqual(caught.exception.code,"RATE_LIMITED")
            self.now+=7862400001
            runtime=SimpleNamespace(store=new,close_context=self.close,_cleanup_generation="")
            WorkerHandoffRuntime.cleanup(runtime,cur)
            cur.execute("SELECT source_count,app_owner_count,transaction_count FROM x_capi_handoff_generations WHERE generation=%s",(GENERATION,))
            self.assertEqual(cur.fetchone(),(0,0,0))
            cur.execute("SELECT count(*) FROM x_capi_handoff_imports")
            self.assertEqual(cur.fetchone()[0],0)
            self.assertEqual(new_capture()["state"],"captured")
            new.prepare(cur,"b"*64,request(choice="grant",policy="app-x-v2"),new_owner=True)
            self.assertEqual(new.begin(cur,"b"*64,request(choice="grant",policy="app-x-v2",destination="desktop"))["state"],"app_granted")

    def test_schema5_postflight_rejects_catalog_and_acl_drift(self):
        pre=(ROOT/"migrations/008_x_capi_v5_postflight.sql").read_text()
        pre=pre.replace(':"owner_role"','"handoff_owner"').replace(':"worker_role"','"handoff_worker"').replace(":'owner_role'","'handoff_owner'").replace(":'worker_role'","'handoff_worker'")
        with self.admin.cursor() as cur:
            cur.execute("BEGIN");cur.execute(pre);cur.execute("ROLLBACK")
            cur.execute("BEGIN");cur.execute("ALTER TABLE x_capi_handoff_sources DROP CONSTRAINT x_capi_handoff_sources_policy_epoch_check")
            with self.assertRaises(self.pg.Error):cur.execute(pre)
            cur.execute("ROLLBACK")
            cur.execute("BEGIN");cur.execute("GRANT TRUNCATE ON x_capi_handoff_sources TO handoff_worker")
            with self.assertRaises(self.pg.Error):cur.execute(pre)
            cur.execute("ROLLBACK")


if __name__=="__main__":
    unittest.main()
