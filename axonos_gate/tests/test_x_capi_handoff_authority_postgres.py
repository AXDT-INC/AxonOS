"""Imported F2 eligibility with real PostgreSQL transactions and synthetic keys.

Uses only the explicit disposable-cluster guard in the store suite. No X client,
financial service, real wallet credential, or business transaction is involved.
"""
import dataclasses
import contextlib
import json
import tempfile
import threading
import time
from pathlib import Path
import os
import sys
import unittest
import uuid
from unittest.mock import patch

from axonos_gate import x_capi, x_capi_worker as worker
from axonos_gate import x_capi_handoff as handoff
from axonos_gate.x_capi_handoff_worker import WorkerHandoffRuntime
from axonos_gate.tests import test_x_capi_handoff_store as fixtures
from axonos_gate.tests import test_x_capi as config_fixtures


@unittest.skipUnless(fixtures.DSN and fixtures.CONFIRMED, "explicit disposable PG15 handoff database required")
class ImportedAuthorityPostgresTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.HandoffPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.HandoffPostgresTests.tearDownClass.__func__)
    tearDown = fixtures.HandoffPostgresTests.tearDown
    source_req = fixtures.HandoffPostgresTests.source_req
    capture = fixtures.HandoffPostgresTests.capture
    prepare = fixtures.HandoffPostgresTests.prepare
    begin = fixtures.HandoffPostgresTests.begin
    approved = fixtures.HandoffPostgresTests.approved
    wait_for_database_lock = fixtures.HandoffPostgresTests.wait_for_database_lock
    database_now = fixtures.HandoffPostgresTests.database_now

    def setUp(self):
        fixtures.HandoffPostgresTests.setUp(self)
        # Synthetic identifiers stay synthetic; deriving their fixture epoch
        # from this disposable database keeps real-clock contention tests valid
        # after the originally frozen example date has passed.
        self.now = self.database_now()
        self.env = config_fixtures.ConfigTests.enabled_env(
            X_CAPI_CONSENT_POLICY_VERSION="x-capi-handoff-v2", X_CAPI_CONSENT_POLICY_EPOCH="42",
            X_CAPI_HASH_KEY="synthetic-handoff-worker-hash-key-0001")
        self.env_patch = patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        self.cfg = dataclasses.replace(x_capi.load_config(), audience_scope="a" * 64)
        self.cfg_patch = patch.object(x_capi, "load_config", return_value=self.cfg)
        self.cfg_patch.start(); self.addCleanup(self.cfg_patch.stop)
        self.runtime = WorkerHandoffRuntime(worker, store=self.store,
            keyring=handoff.ServiceKeyring("synthetic", {"synthetic": bytes(range(32))}),
            lease=handoff.FreshnessLease(42, fixtures.GENERATION, elapsed_ms=lambda: self.now),
            privacy_path="/synthetic/unused", clock_assured=lambda: True)
        self.runtime_patch = patch.object(worker, "_handoff_runtime", self.runtime)
        self.runtime_patch.start(); self.addCleanup(self.runtime_patch.stop)
        with self.admin.cursor() as cur:
            cur.execute("TRUNCATE x_capi_dedup,x_capi_counters")
            cur.execute("UPDATE x_capi_config_guard SET deployment_id_hash=%s,hash_key_fingerprint=%s,context_key_fingerprint=%s WHERE singleton", (worker._deployment_id_hash(self.cfg),worker._hash_key_fingerprint(),worker._context_key_fingerprint()))
        self.h = self.approved()
        with self.conn.cursor() as cur:
            self.result = self.store.redeem(cur, self.owner, self.h, self.runtime.create_context, self.runtime.recover_context)
        self.conn.commit()
        self.wallet = "0x" + "1" * 40
        self.now += 10

    def envelope(self, action="bind", *, owner=None, wallet=None, milestone="wallet_verified", timestamp=None):
        body = dict(v=2,action=action,context_token=self.result["context"],wallet_address=wallet or self.wallet,
                    browser_owner_hash=owner or self.owner,event_timestamp_ms=timestamp or self.now)
        if action == "event":
            body.update(milestone=milestone,source_key=wallet or self.wallet,
                        metadata=dict(allow_context_binding=True,credit_source=None,payment_rail=None,chain_id=None))
            if milestone == "deposit_completed":
                body.update(source_key="0x"+"b"*64,metadata=dict(allow_context_binding=False,credit_source="onchain",payment_rail="usdc",chain_id=8453))
            elif milestone == "session_started":
                body.update(source_key="123",metadata=dict(allow_context_binding=False,credit_source=None,payment_rail=None,chain_id=None))
        body["wallet_authority"] = x_capi._seal_wallet_authority(body, self.cfg, self.now/1000)
        return body

    def ingest(self, body):
        return worker.process_ingest_envelope(self.conn, body, now=self.now/1000)

    def counts(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM x_capi_outbox"); outbox = cur.fetchone()[0]
            cur.execute("SELECT wallet_hash FROM x_capi_attribution_contexts"); wallet = cur.fetchone()[0]
        self.conn.commit()
        return outbox,wallet

    def test_v4_exact_fields_recovery_and_no_wallet_on_import(self):
        ticket = x_capi.decode_context_ticket(self.result["context"])
        self.assertEqual(len(ticket),14)
        self.assertEqual(ticket["v"],4)
        self.assertEqual(self.counts(),(0,None))
        with self.conn.cursor() as cur:
            same = self.store.recover(cur,self.owner,self.h,self.runtime.recover_context)
        self.assertEqual(same,self.result)
        self.assertNotIn("browser_owner_hash",ticket)

    def test_bind_owner_proof_then_only_future_deposit_and_session(self):
        self.assertEqual(self.ingest(self.envelope()),"bound")
        self.assertEqual(self.counts()[0],0)
        self.assertEqual(self.ingest(self.envelope("event",milestone="deposit_completed")),"handoff_binding_order_rejected")
        self.now += 1
        self.assertEqual(self.ingest(self.envelope("event",milestone="deposit_completed")),"dry_run")
        self.assertEqual(self.ingest(self.envelope("event",milestone="session_started")),"dry_run")
        self.assertEqual(self.counts()[0],2)
        with self.conn.cursor() as cur:
            cur.execute("SELECT DISTINCT status FROM x_capi_outbox")
            self.assertEqual(cur.fetchall(),[("dry_run",)])

    def test_wallet_verified_is_independent_real_event_not_import_or_bind(self):
        self.assertEqual(self.ingest(self.envelope("event")),"dry_run")
        self.assertEqual(self.ingest(self.envelope("event")),"duplicate")
        self.assertEqual(self.counts()[0],1)

    def test_stolen_context_valid_other_browser_proof_cannot_bind(self):
        self.assertEqual(self.ingest(self.envelope(owner="b"*64)),"handoff_owner_or_source_rejected")
        self.assertEqual(self.counts(),(0,None))

    def test_owner_tamper_invalidates_exact_f2_digest(self):
        body=self.envelope();body["browser_owner_hash"]="b"*64
        self.assertEqual(self.ingest(body),"invalid_wallet_authority")
        self.assertEqual(self.counts(),(0,None))

    def test_missing_owner_and_positive_legacy_envelopes_fail_closed(self):
        body=self.envelope();body.pop("browser_owner_hash")
        self.assertEqual(self.ingest(body),"invalid_envelope")
        body["v"]=1;body["wallet_authority"]=x_capi._seal_wallet_authority(body,self.cfg,self.now/1000)
        self.assertEqual(self.ingest(body),"positive_envelope_version_mismatch")
        self.assertEqual(self.counts(),(0,None))

    def test_deposit_during_first_binding_lock_wait_is_not_retroactive(self):
        # Exercise production clock_timestamp(), with genuine PostgreSQL locks.
        self.store.clock_ms = None
        self.now = self.database_now()
        blocker = self.pg.connect(**self.worker_dsn)
        waiting = self.pg.connect(**self.worker_dsn)
        result = []
        bind = self.envelope(); started = self.now
        wallet_hash = x_capi.keyed_internal_hash("wallet",self.wallet)
        def run():
            result.append(worker.process_ingest_envelope(waiting,bind,now=started/1000))
        thread = threading.Thread(target=run)
        try:
            with blocker.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (worker._wallet_advisory_key(wallet_hash),))
            thread.start(); self.wait_for_database_lock(waiting)
            # A legitimate synthetic deposit occurs while the bind is waiting.
            # Its original financial timestamp must survive the eventual retry.
            self.now = self.database_now()
            deposit = self.envelope("event",milestone="deposit_completed")
            event_timestamp = deposit["event_timestamp_ms"]
            time.sleep(0.02)
            decision_lower_bound = self.database_now()
            blocker.rollback(); thread.join(3)
            self.assertFalse(thread.is_alive()); self.assertEqual(result,["bound"])
            with self.conn.cursor() as cur:
                cur.execute("SELECT binding_cutoff_ms FROM x_capi_handoff_imports")
                self.assertGreaterEqual(cur.fetchone()[0],decision_lower_bound)
            self.assertEqual(self.ingest(deposit),"handoff_binding_order_rejected")
            self.assertEqual(deposit["event_timestamp_ms"],event_timestamp)
            time.sleep(0.005)
            self.now = self.database_now()
            future = self.envelope("event",milestone="deposit_completed")
            self.assertEqual(self.ingest(future),"dry_run")
            with self.conn.cursor() as cur:
                cur.execute("SELECT conversion_timestamp_ms FROM x_capi_outbox")
                self.assertEqual(cur.fetchall(),[(future["event_timestamp_ms"],)])
        finally:
            blocker.rollback(); thread.join(3)
            waiting.close(); blocker.close()

    def assert_expired_admission_rolls_back(self, body, deadline_ms, lock_sql,
                                           lock_parameters, expected):
        """Real DB lock wait, real DB time; no positive mutation may survive."""
        self.store.clock_ms = None
        blocker = self.pg.connect(**self.worker_dsn)
        waiting = self.pg.connect(**self.worker_dsn)
        result = []
        started = self.database_now()
        original_timestamp = body["event_timestamp_ms"]
        with self.conn.cursor() as cur:
            cur.execute("SELECT context_count,outbox_active_count FROM x_capi_capacity")
            capacity_before = cur.fetchone()
            cur.execute("SELECT reason,count,updated_at FROM x_capi_counters ORDER BY reason")
            counters_before = cur.fetchall()
            cur.execute("SELECT id,wallet_hash,wallet_bound_at FROM x_capi_attribution_contexts ORDER BY id")
            bindings_before = cur.fetchall()
            cur.execute("SELECT context_id,binding_cutoff_ms FROM x_capi_handoff_imports ORDER BY context_id")
            cutoffs_before = cur.fetchall()
        self.conn.commit()
        def run():
            result.append(worker.process_ingest_envelope(waiting, body, now=started/1000))
        thread = threading.Thread(target=run)
        try:
            with blocker.cursor() as cur:
                cur.execute(lock_sql, lock_parameters)
            thread.start()
            self.wait_for_database_lock(waiting)
            self.assertLess(self.database_now(), deadline_ms,
                            "fixture must reach admission while authority remains current")
            while self.database_now() <= deadline_ms:
                time.sleep(0.005)
            blocker.rollback()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [expected])
            self.assertEqual(body["event_timestamp_ms"], original_timestamp)
            with self.conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM x_capi_outbox")
                self.assertEqual(cur.fetchone(), (0,))
                cur.execute("SELECT count(*) FROM x_capi_dedup")
                self.assertEqual(cur.fetchone(), (0,))
                cur.execute("SELECT reason,count,updated_at FROM x_capi_counters ORDER BY reason")
                self.assertEqual(cur.fetchall(), counters_before)
                cur.execute("SELECT context_count,outbox_active_count FROM x_capi_capacity")
                self.assertEqual(cur.fetchone(), capacity_before)
                cur.execute("SELECT id,wallet_hash,wallet_bound_at FROM x_capi_attribution_contexts ORDER BY id")
                self.assertEqual(cur.fetchall(), bindings_before)
                cur.execute("SELECT context_id,binding_cutoff_ms FROM x_capi_handoff_imports ORDER BY context_id")
                self.assertEqual(cur.fetchall(), cutoffs_before)
        finally:
            blocker.rollback()
            thread.join(3)
            waiting.close()
            blocker.close()

    def expiring_authority_event(self, action="event"):
        self.now = self.database_now()
        deadline = self.now + 1000
        body = self.envelope(action)
        body["wallet_authority"] = x_capi._seal_wallet_authority(
            body, self.cfg, (deadline - x_capi._WALLET_AUTHORITY_SECONDS * 1000)/1000)
        self.assertTrue(x_capi.wallet_authority_valid(body, self.now/1000))
        return body, deadline

    def test_f2_expiry_during_outbox_capacity_lock_rolls_back_every_positive_write(self):
        body, deadline = self.expiring_authority_event()
        self.assert_expired_admission_rolls_back(body, deadline,
            "SELECT singleton FROM x_capi_capacity WHERE singleton FOR UPDATE", (),
            "invalid_wallet_authority")

    def test_deposit_f2_expiry_during_capacity_wait_preserves_prior_binding_and_financial_time(self):
        self.assertEqual(self.ingest(self.envelope()), "bound")
        time.sleep(0.015)
        self.now = self.database_now()
        deadline = self.now + 1000
        body = self.envelope("event", milestone="deposit_completed")
        body["wallet_authority"] = x_capi._seal_wallet_authority(
            body, self.cfg, (deadline - x_capi._WALLET_AUTHORITY_SECONDS * 1000)/1000)
        self.assert_expired_admission_rolls_back(body, deadline,
            "SELECT singleton FROM x_capi_capacity WHERE singleton FOR UPDATE", (),
            "invalid_wallet_authority")

    def test_f2_expiry_during_dedup_admission_lock_rolls_back_every_positive_write(self):
        body, deadline = self.expiring_authority_event()
        logical_hash = x_capi.keyed_internal_hash("event:wallet_verified", self.wallet)
        self.assert_expired_admission_rolls_back(body, deadline,
            "INSERT INTO x_capi_dedup(milestone,source_key_hash,first_seen_at,expires_at) VALUES(%s,%s,%s,%s)",
            ("wallet_verified", logical_hash, self.now/1000, self.now/1000+86400),
            "invalid_wallet_authority")

    def test_f2_expiry_during_final_counter_lock_rolls_back_every_positive_write(self):
        body, deadline = self.expiring_authority_event()
        self.assert_expired_admission_rolls_back(body, deadline,
            "INSERT INTO x_capi_counters(reason,count,updated_at) VALUES(%s,1,%s)",
            ("enqueued_dry_run", self.now/1000), "invalid_wallet_authority")

    def test_f2_expiry_during_bind_counter_lock_rolls_back_binding(self):
        body, deadline = self.expiring_authority_event("bind")
        self.assert_expired_admission_rolls_back(body, deadline,
            "INSERT INTO x_capi_counters(reason,count,updated_at) VALUES(%s,1,%s)",
            ("context_bound", self.now/1000), "invalid_wallet_authority")

    def test_f2_expiry_during_unmapped_counter_lock_rolls_back_binding(self):
        self.cfg = dataclasses.replace(self.cfg, event_ids={
            key: value for key, value in self.cfg.event_ids.items()
            if key != "wallet_verified"})
        with patch.object(x_capi, "load_config", return_value=self.cfg):
            body, deadline = self.expiring_authority_event()
            self.assert_expired_admission_rolls_back(body, deadline,
                "INSERT INTO x_capi_counters(reason,count,updated_at) VALUES(%s,1,%s)",
                ("mapping_disabled_wallet_verified", self.now/1000), "invalid_wallet_authority")

    def test_source_expiry_during_outbox_capacity_lock_rolls_back_every_positive_write(self):
        # Build an honest synthetic source first observed almost seven days ago,
        # then independently consent/import now. No deadline is rewritten in SQL.
        current = self.database_now()
        deadline = current + 1200
        self.source = handoff.token_hash(handoff.new_token(), "landing-owner")
        self.owner = handoff.token_hash(handoff.new_token(), "app-owner")
        self.now = deadline - fixtures.SOURCE_WINDOW_MS + 1000
        self.capture()
        self.now = current
        self.prepare()
        _, begun = self.begin()
        self.h = begun["handoff"]
        with self.conn.cursor() as cur:
            self.store.authorize(cur, self.source_req("authorize", owner_hash=self.source, handoff=self.h))
            self.result = self.store.redeem(cur, self.owner, self.h,
                self.runtime.create_context, self.runtime.recover_context)
        self.conn.commit()
        self.assertEqual(self.result["expires_at_ms"], deadline)
        body = self.envelope("event")
        self.assert_expired_admission_rolls_back(body, deadline,
            "SELECT singleton FROM x_capi_capacity WHERE singleton FOR UPDATE", (),
            "invalid_or_stale_ticket")
        with self.conn.cursor() as cur:
            cur.execute("SELECT original_expires_at_ms,effective_expires_at_ms FROM x_capi_handoff_imports WHERE app_owner_hash=%s",
                        (bytes.fromhex(self.owner),))
            self.assertEqual(cur.fetchone(), (deadline, deadline))

    def test_second_wallet_cannot_replace_first_binding(self):
        self.assertEqual(self.ingest(self.envelope()),"bound")
        first=self.counts()[1];self.now+=1
        self.assertEqual(self.ingest(self.envelope(wallet="0x"+"2"*40)),"wallet_or_lifecycle_mismatch")
        self.assertEqual(self.counts(),(0,first))

    def test_business_event_cannot_create_first_binding(self):
        self.assertEqual(self.ingest(self.envelope("event",milestone="deposit_completed")),"handoff_binding_order_rejected")
        self.assertEqual(self.counts(),(0,None))

    def test_source_withdrawal_cancels_work_and_recovery(self):
        self.assertEqual(self.ingest(self.envelope("event")),"dry_run")
        with self.conn.cursor() as cur:
            result=self.store.revoke(cur,self.source_req("revoke",owner_hash=self.source,reason="withdrawn"),self.runtime.close_context)
        self.conn.commit();self.assertEqual(result["state"],"withdrawn")
        self.assertEqual(self.ingest(self.envelope()),"handoff_owner_or_source_rejected")
        with self.conn.cursor() as cur:
            cur.execute("SELECT status,twclid FROM x_capi_outbox");self.assertEqual(cur.fetchall(),[("cancelled",None)])
            with self.assertRaises(handoff.HandoffError):self.store.recover(cur,self.owner,self.h,self.runtime.recover_context)

    def test_existing_privacy_capability_closes_import_without_owner_or_runtime(self):
        self.assertEqual(self.ingest(self.envelope("event")),"dry_run")
        with patch.object(worker,"_handoff_runtime",None):
            result=worker.process_consent_request(self.conn,dict(v=1,action="consent",operation="revoke",context_token=self.result["context"],csrf_token=self.result["csrf"],request_timestamp_ms=self.now),now=self.now/1000)
        self.assertTrue(result["ok"],result)
        with self.conn.cursor() as cur:
            cur.execute("SELECT state,context_ciphertext FROM x_capi_handoff_imports");self.assertEqual(cur.fetchone(),("closed",None))
            cur.execute("SELECT state,twclid_ciphertext FROM x_capi_handoff_sources");self.assertEqual(cur.fetchone(),("closed",None))
        self.assertEqual(self.ingest(self.envelope()),"handoff_owner_or_source_rejected")

    def test_missing_runtime_and_untrusted_clock_cannot_ingest(self):
        with patch.object(worker,"_handoff_runtime",None):
            self.assertEqual(self.ingest(self.envelope()),"handoff_authority_unavailable")
        self.runtime.clock_assured=lambda:False
        self.assertEqual(self.ingest(self.envelope()),"handoff_authority_unavailable")
        self.assertEqual(self.counts(),(0,None))

    def test_off_mode_cannot_create_work(self):
        off=dataclasses.replace(self.cfg,mode="off")
        with patch.object(x_capi,"load_config",return_value=off):
            self.assertEqual(self.ingest(self.envelope()),"disabled")
        self.assertEqual(self.counts(),(0,None))

    def test_import_expiry_is_original_and_never_renewed_by_wallet_binding(self):
        expires=self.result["expires_at_ms"]
        self.assertEqual(self.ingest(self.envelope()),"bound")
        with self.conn.cursor() as cur:
            cur.execute("SELECT original_expires_at_ms,effective_expires_at_ms FROM x_capi_handoff_imports")
            self.assertEqual(cur.fetchone(),(expires,expires))
        self.now=expires
        self.assertEqual(self.ingest(self.envelope()),"invalid_or_stale_ticket")

    @contextlib.contextmanager
    def real_rpc(self):
        from axonos_gate.x_capi_handoff_rpc import HandoffRpcServer,HandoffRpcClient
        with tempfile.TemporaryDirectory(prefix="axon-handoff-integration-") as directory:
            root=Path(directory);privacy=root/"privacy";runtime_dir=root/"rpc"
            privacy.mkdir(mode=0o700);runtime_dir.mkdir(mode=0o700)
            fence=worker.PrivacyFence(str(privacy));fence.open()
            self.runtime.privacy_path=str(privacy)
            self.runtime.connection_factory=lambda:self.pg.connect(**self.worker_dsn)
            env={"X_CAPI_WORKER_DB_ROLE":"handoff_worker","X_CAPI_OWNER_DB_ROLE":"handoff_owner",
                 "X_CAPI_WORKER_UID":str(os.geteuid()),"X_CAPI_ALLOW_TEST_DB_URL":"1",
                 "X_HANDOFF_ENABLED":"true"}
            with patch.dict(os.environ,env),patch.object(worker,"CONFIG_GUARD_ATTESTATION",str(runtime_dir/"guard.json")):
                worker._schema_attestation_key=None;worker._schema_attestation_until=0
                # Disposable fixture models the initial activation sentinel.
                self.assertTrue(worker._write_config_guard_attestation(None))
                self.assertTrue(worker._publish_durable_config_guard(self.conn))
                self.assertTrue(worker._schema_ready(self.conn))
                server=HandoffRpcServer(str(runtime_dir/"handoff.sock"),self.runtime.service.dispatch,allowed_uid=os.geteuid()).start()
                try:
                    yield HandoffRpcClient(str(runtime_dir/"handoff.sock"),expected_uid=os.geteuid()),fence
                finally:
                    server.close();fence.close()
                    worker._schema_attestation_key=None;worker._schema_attestation_until=0

    def source_rpc(self,client,operation,**values):
        request=self.source_req(operation,**values);raw=handoff.canonical_json(request)
        result=client.request(dict(kind="source",raw_body=raw.decode("ascii"),kid="synthetic",mac=self.runtime.keyring.request_mac(raw,"synthetic")))
        self.runtime.keyring.verify_response(raw,result["status"],handoff.canonical_json(result["body"]),"synthetic",result["headers"]["X-AxonOS-Handoff-MAC"])
        return result

    def test_real_rpc_mac_schema_guard_privacy_fence_and_lease(self):
        with self.real_rpc() as (client,fence):
            status=self.source_rpc(client,"status",owner_hash=self.source)
            self.assertEqual(status["status"],200,status)
            self.assertEqual(status["body"]["state"],"consumed")
            challenge=self.source_rpc(client,"lease_challenge")
            self.assertEqual(challenge["status"],200,challenge)
            body=challenge["body"]
            self.now+=29000
            lease=self.source_rpc(client,"lease_attest",worker_boot=body["worker_boot"],challenge=body["challenge"],control_version=0,ready=True)
            self.assertEqual(lease["status"],200,lease)
            self.assertEqual(lease["body"]["remaining_ms"],1000)
            self.assertTrue(self.runtime.dispatch_fresh())
            self.now+=1000;self.assertFalse(self.runtime.dispatch_fresh())
            # The old send boundary wins only while already held. A conflicting
            # withdrawal cannot falsely acknowledge commit; retry closes it.
            with fence.dispatch_boundary() as acquired:
                self.assertTrue(acquired)
                result=self.source_rpc(client,"revoke",owner_hash=self.source,reason="withdrawn")
                self.assertEqual(result["status"],503,result)
            result=self.source_rpc(client,"revoke",owner_hash=self.source,reason="withdrawn")
            self.assertEqual(result["status"],200,result)
            self.assertEqual(result["body"]["state"],"withdrawn")
            self.assertEqual(self.source_rpc(client,"status",owner_hash=self.source)["body"]["state"],"closed")

    def test_all_browser_operations_with_real_rpc_and_postgres(self):
        from axonos_gate import x_capi_handoff_http as http
        with self.real_rpc() as (client,_):
            source="e"*64
            captured=self.source_rpc(client,"capture",owner_hash=source,observation_id=str(uuid.uuid4()),
                observed_at_ms=self.now-1000,original_expires_at_ms=self.now-1000+fixtures.SOURCE_WINDOW_MS,
                landing_consented_at_ms=self.now,policy="landing-x-v2",gpc=False,twclid="synthetic_second_Qq98-Yy51")
            self.assertEqual(captured["status"],200,captured)
            cookie=None
            def call(op,payload,form=False):
                raw=("v=2&handoff="+payload["handoff"]).encode() if form else handoff.canonical_json(payload)
                headers={"Host":"app.axonos.io","Content-Type":"application/x-www-form-urlencoded" if form else "application/json",
                    "Content-Length":str(len(raw)),"Origin":"https://axonos.io" if form else "https://app.axonos.io",
                    "Sec-Fetch-Site":"same-site" if form else "same-origin","Sec-Fetch-Mode":"navigate" if form else "cors",
                    "Sec-Fetch-Dest":"document" if form else "empty","X-AxonOS-Handoff":"2"}
                if cookie:headers["Cookie"]=cookie
                return http.handle("POST","/attribution/callback" if form else http.PREFIX+op,headers,raw,https=True,call=client.request,now_ms=self.now)
            prepared=call("prepare",fixtures.request(choice="grant",policy="app-x-v2"))
            self.assertEqual(prepared.status,200,prepared.body)
            cookie=prepared.headers["Set-Cookie"].split(";",1)[0]
            begun=call("begin",fixtures.request(choice="grant",policy="app-x-v2",destination="desktop"))
            self.assertEqual(begun.status,200,begun.body);h=json.loads(begun.body)["handoff"]
            assigned=self.source_rpc(client,"authorize",owner_hash=source,handoff=h)
            self.assertEqual(assigned["status"],200,assigned)
            callback=call("redeem",dict(handoff=h),form=True)
            self.assertEqual(callback.status,200,callback.body)
            self.assertNotIn(h.encode(),callback.body)
            recovered=call("recover",fixtures.request(handoff=h))
            self.assertEqual(recovered.status,200,recovered.body)
            first=json.loads(recovered.body)
            again=call("recover",fixtures.request(handoff=h))
            self.assertEqual(json.loads(again.body),first)
            self.assertIsNotNone(x_capi.decode_context_ticket(first["context"]))
            withdrawn=call("withdraw",fixtures.request(reason="withdrawn"))
            self.assertEqual(withdrawn.status,200,withdrawn.body)
            lost=call("recover",fixtures.request(handoff=h))
            self.assertEqual(lost.status,410,lost.body)


if __name__ == '__main__':
    unittest.main()
