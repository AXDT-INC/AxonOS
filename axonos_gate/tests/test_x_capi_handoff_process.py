"""Actual worker process replacement on the guarded disposable PG15 fixture.

The test supervisor mirrors the committed Compose restart-on-exit boundary.
Child workers run production main/startup/schema/config/generation/privacy code.
Only private filesystem paths, kernel-clock assurance and loop cadence are test
injections. A private stdin control channel supplies synthetic ledger load and
an unresolved handler; these hooks do not exist in production runtime code.
"""
import contextlib
import json
import os
from pathlib import Path
import queue
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from axonos_gate import x_capi
from axonos_gate import x_capi_worker as worker
from axonos_gate import x_capi_handoff as protocol
from axonos_gate import x_capi_handoff_worker as integration
from axonos_gate.x_capi_handoff_rpc import HandoffRpcClient
from axonos_gate.x_capi_handoff_store import HandoffStore
from axonos_gate.tests import test_x_capi_handoff_store as fixtures
from axonos_gate.tests import test_x_capi as config_fixtures

ROOT = Path(__file__).resolve().parents[2]
SERVICE_KEY = bytes(range(32))
DATA_KEY = bytes(reversed(range(32)))


def child_main(directory):
    root = Path(directory)
    if not str(root).startswith('/tmp/axon-handoff-process-') or not fixtures.CONFIRMED:
        raise RuntimeError('refuse non-disposable worker process')
    # Recheck disposable database identity, allowing intentionally damaged schema
    # metadata to reach the production startup rejection being tested.
    import psycopg2
    parts = psycopg2.extensions.parse_dsn(fixtures.DSN)
    if (set(parts)-{'host','port','dbname','user'} or parts.get('dbname')!='capi_handoff_test'
            or parts.get('user')!='cluadmin' or not parts.get('host','').startswith('/tmp/axon-handoff-pg15-')
            or not parts['host'].endswith('/socket')):
        raise RuntimeError('refuse non-disposable child DSN')
    connection = psycopg2.connect(fixtures.DSN)
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT current_setting('server_version_num')::int,current_setting('data_directory'),pg_is_in_recovery(),current_database(),current_user")
            version, data, recovery, database, user = cur.fetchone()
            if not (150000<=version<160000 and data.startswith('/tmp/axon-handoff-pg15-')
                    and not recovery and (database,user)==('capi_handoff_test','cluadmin')):
                raise RuntimeError('refuse non-disposable child database')
    finally:
        connection.close()
    integration.DEFAULT_SOCKET = str(root / 'rpc' / 'handoff.sock')
    worker.CONFIG_GUARD_ATTESTATION = str(root / 'rpc' / 'guard.json')
    reports = threading.Lock()
    holder, release, block_next = [], threading.Event(), threading.Event()
    def report(event, **fields):
        with reports:
            print(json.dumps(dict(event=event, pid=os.getpid(), **fields)), flush=True)
    def observe(name, event):
        original = getattr(worker, name)
        def call(*args, **kwargs):
            result = original(*args, **kwargs)
            report(event, ok=bool(result))
            return result
        setattr(worker, name, call)
    observe('activate_worker_startup', 'startup')
    observe('_schema_ready', 'schema')
    observe('enforce_config_guard', 'config')
    observe('complete_worker_shutdown', 'shutdown_drained')
    original_start = integration.WorkerHandoffRuntime.start
    def start(runtime):
        holder.append(runtime)
        # This is synthetic clock assurance, not evidence for the host's NTP.
        runtime.clock_assured = lambda: True
        runtime.service.clock_assured = runtime.clock_assured
        original_dispatch = runtime.service.dispatch
        def dispatch(frame):
            if block_next.is_set():
                block_next.clear()
                report('rpc_blocked')
                release.wait(20)
            return original_dispatch(frame)
        runtime.service.dispatch = dispatch
        original_guard = runtime.service.generation_guard
        def guard(request):
            value = original_guard(request)
            report('generation', ok=True)
            return value
        runtime.service.generation_guard = guard
        original_start(runtime)
        report('ready', boot=runtime.lease.worker_boot,
               fresh=runtime.dispatch_fresh(), count=runtime.lease.ledger_status()['count'])
    integration.WorkerHandoffRuntime.start = start
    original_iteration = worker.run_once
    def forbidden_transport(*args, **kwargs):
        report('forbidden_transport')
        raise AssertionError('No X transport belongs in this synthetic test')
    def iteration(**kwargs):
        result = original_iteration(transport=forbidden_transport, **kwargs)
        if select.select([sys.stdin], [], [], 0)[0]:
            command = json.loads(sys.stdin.readline())
            runtime = holder[-1]
            if command['op'] == 'snapshot':
                report('snapshot', boot=runtime.lease.worker_boot,
                       fresh=runtime.dispatch_fresh(), count=runtime.lease.ledger_status()['count'])
            elif command['op'] == 'block_next':
                block_next.set()
                report('block_armed')
            elif command['op'] == 'fill':
                # Exercise the public challenge API at the exact real threshold,
                # without shortening TTLs or changing high-water constants.
                index = 0
                while runtime.lease.ledger_status()['count'] < protocol.LEASE_LEDGER_HIGH_WATER:
                    request = dict(v=2, epoch=42, generation=fixtures.GENERATION,
                                   request_id=str(uuid.UUID(int=index, version=4)), op='lease_challenge')
                    runtime.lease.challenge(request)
                    index += 1
                report('filled', count=runtime.lease.ledger_status()['count'],
                       boot=runtime.lease.worker_boot)
            else:
                raise AssertionError('unknown private fixture operation')
        return result
    worker.run_once = iteration
    worker._wait_between_iterations = lambda seconds: time.sleep(min(seconds, 0.1))
    status = worker.main([])
    report('main_return', status=status,
           fresh=holder[-1].dispatch_fresh() if holder else False,
           boot=holder[-1].lease.worker_boot if holder else None)
    # Only a deliberately blocked test handler needs this private post-main
    # release. No real supervisor may start another process before this exits.
    if any(runtime.server and runtime.server._futures for runtime in holder):
        if select.select([sys.stdin], [], [], 10)[0]:
            assert json.loads(sys.stdin.readline()) == {'op': 'release'}
        release.set()
    raise SystemExit(status)


@unittest.skipUnless(fixtures.DSN and fixtures.CONFIRMED,
                     'explicit disposable PG15 handoff database required')
class ProcessReplacementPostgresTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.HandoffPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.HandoffPostgresTests.tearDownClass.__func__)

    def setUp(self):
        fixtures.HandoffPostgresTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory(prefix='axon-handoff-process-')
        self.root = Path(self.temp.name)
        self.children = []
        (self.root / 'rpc').mkdir(mode=0o700)
        fence = worker.PrivacyFence(str(self.root / 'privacy'))
        fence.open(); fence.close()
        for name, key in (('service', SERVICE_KEY), ('data', DATA_KEY)):
            path = self.root / (name + '.json')
            path.write_text(json.dumps({'primary_kid':'synthetic', 'keys':[
                {'kid':'synthetic','key_base64url':protocol.b64url_encode(key)}]}))
            path.chmod(0o600)
        self.env = config_fixtures.ConfigTests.enabled_env(
            X_CAPI_CONSENT_POLICY_VERSION='x-capi-handoff-v2',
            X_CAPI_CONSENT_POLICY_EPOCH='42', X_CAPI_ALLOWED_ORIGIN='https://app.axonos.io',
            X_CAPI_HASH_KEY='synthetic-process-worker-hash-key-0001',
            X_CAPI_ALLOW_TEST_CONFIG_GUARD_BYPASS='0',
            X_CAPI_ALLOW_TEST_DB_URL='1', X_CAPI_DB_URL=self.pg.extensions.make_dsn(**self.worker_dsn),
            X_CAPI_WORKER_DB_ROLE='handoff_worker', X_CAPI_OWNER_DB_ROLE='handoff_owner',
            X_CAPI_POSTGRES_DB='capi_handoff_test', X_CAPI_WORKER_UID=str(os.getuid()),
            X_CAPI_INGEST_ALLOWED_UID=str(os.getuid()),
            X_CAPI_INGEST_SOCKET=str(self.root/'rpc'/'events.sock'),
            X_CAPI_CONSENT_SOCKET=str(self.root/'rpc'/'consent.sock'),
            X_CAPI_PRIVACY_FENCE_DIR=str(self.root/'privacy'),
            X_CAPI_CONFIG_GUARD_FILE=str(self.root/'rpc'/'guard.json'),
            X_CAPI_ALLOW_TEST_CONFIG_GUARD_FILE='1',
            X_HANDOFF_WITHDRAWAL_POLICY='B', X_HANDOFF_SOURCE_GENERATION=fixtures.GENERATION,
            X_HANDOFF_SERVICE_KEYRING_FILE=str(self.root/'service.json'),
            X_HANDOFF_SOURCE_DATA_KEYRING_FILE=str(self.root/'data.json'),
            X_HANDOFF_TEST_DSN=fixtures.DSN,
            X_HANDOFF_TEST_DISPOSABLE_CONFIRM='DESTROY_TMP_HANDOFF_DATABASE',
            PYTHONDONTWRITEBYTECODE='1', PATH=os.environ['PATH'])
        with patch.dict(os.environ, self.env, clear=True):
            self.cfg = x_capi.load_config()
            with self.admin.cursor() as cur:
                cur.execute('TRUNCATE x_capi_handoff_generations,x_capi_worker_state,x_capi_counters,x_capi_dedup CASCADE')
                cur.execute('UPDATE x_capi_config_guard SET deployment_id_hash=%s,audience_scope=%s,hash_key_fingerprint=%s,context_key_fingerprint=%s',
                    (worker._deployment_id_hash(self.cfg), self.cfg.audience_scope,
                     worker._hash_key_fingerprint(), worker._context_key_fingerprint()))
            self.store = HandoffStore(fixtures.GENERATION,42,self.cfg.audience_scope,
                                      {'synthetic':DATA_KEY},'synthetic')
            with self.conn.cursor() as cur:
                self.store.provision_generation(cur, activate=True)
            self.conn.commit()
            with patch.object(worker,'CONFIG_GUARD_ATTESTATION',str(self.root/'rpc'/'guard.json')):
                self.assertTrue(worker._write_config_guard_attestation(None))
        self.keyring = protocol.ServiceKeyring('synthetic',{'synthetic':SERVICE_KEY})
        self.client = HandoffRpcClient(str(self.root/'rpc'/'handoff.sock'),expected_uid=os.getuid())

    def tearDown(self):
        for child in self.children:
            process=child['process']
            if process.poll() is None:
                try:
                    process.stdin.write('{"op":"release"}\n'); process.stdin.flush()
                except (BrokenPipeError,OSError):
                    pass
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)
            child['reader'].join(2)
            process.stdin.close(); process.stdout.close(); child['log'].close()
        self.conn.close()
        self.temp.cleanup()

    def launch(self, *, ready=True):
        log=(self.root/('worker-'+str(len(self.children))+'.log')).open('w+')
        process=subprocess.Popen([sys.executable,'-m',__name__,'--child',str(self.root)],
            cwd=ROOT,env=self.env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=log,text=True,bufsize=1)
        child=dict(process=process,queue=queue.Queue(),events=[],log=log)
        def read():
            for line in process.stdout:
                try: child['queue'].put(json.loads(line))
                except ValueError: child['queue'].put({'event':'invalid_output','text':line})
        child['reader']=threading.Thread(target=read,daemon=True);child['reader'].start()
        self.children.append(child)
        if ready:
            event=self.wait(child,'ready')
            self.assertEqual(event['pid'],process.pid)
            self.assertFalse(event['fresh'])
            self.assertEqual(event['count'],0)
            for kind in ('startup','schema','config'):
                self.assertTrue(any(item['event']==kind and item['ok'] for item in child['events']),kind)
            child['boot']=event['boot']
        return child

    def wait(self,child,event,timeout=12):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:item=child['queue'].get(timeout=min(.1,deadline-time.monotonic()))
            except queue.Empty:continue
            child['events'].append(item)
            self.assertNotIn(item['event'],('invalid_output','forbidden_transport'),item)
            if item['event']==event:return item
        child['log'].flush();child['log'].seek(0)
        self.fail('missing '+event+'; child status='+str(child['process'].poll())+'; '+child['log'].read()[-1500:])

    def command(self,child,op,event):
        child['process'].stdin.write(json.dumps({'op':op})+'\n');child['process'].stdin.flush()
        return self.wait(child,event)

    def source_rpc(self,op,request_id=None,**fields):
        body=dict(v=2,epoch=42,generation=fixtures.GENERATION,request_id=request_id or str(uuid.uuid4()),op=op,**fields)
        raw=protocol.canonical_json(body)
        frame=dict(kind='source',raw_body=raw.decode(),kid='synthetic',
                   mac=self.keyring.request_mac(raw,'synthetic'))
        for _ in range(10):
            result=self.client.request(frame)
            self.keyring.verify_response(raw,result['status'],protocol.canonical_json(result['body']),
                                         'synthetic',result['headers']['X-AxonOS-Handoff-MAC'])
            if result['body'].get('error')!='DEPENDENCY_UNAVAILABLE':
                return result
            # Actual dispatch and RPC use the same nonblocking privacy fence.
            # Retry only the signed transient response with identical bytes/ID.
            time.sleep(.05)
        return result

    def app_rpc(self,op,owner,**payload):
        body=dict(v='2' if op=='redeem' else 2,**payload)
        if op not in ('redeem',):body.setdefault('request_id',str(uuid.uuid4()))
        frame=dict(kind='app',op=op,payload=body,owner_hash=owner,new_owner=op=='prepare')
        for _ in range(10):
            result=self.client.request(frame)
            if result['body'].get('error')!='DEPENDENCY_UNAVAILABLE':break
            time.sleep(.05)
        self.assertEqual(result['status'],200,result)
        return result['body']

    def imported_state(self):
        with self.admin.cursor() as cur:
            cur.execute('SELECT context_id,context_handle_hash,ownership_id,original_expires_at_ms,effective_expires_at_ms,state,context_ciphertext FROM x_capi_handoff_imports ORDER BY context_id')
            imports=cur.fetchall()
            cur.execute('SELECT id,handle_hash,consent_state,twclid,lifecycle_expires_at,expires_at,first_seen_at FROM x_capi_attribution_contexts ORDER BY id')
            contexts=cur.fetchall()
            cur.execute('SELECT transaction_hash,state,created_at_ms,expires_at_ms,app_consented_at_ms FROM x_capi_handoff_transactions ORDER BY transaction_hash')
            transactions=cur.fetchall()
            return imports,contexts,transactions

    def attest(self,challenge,request_id=None):
        return self.source_rpc('lease_attest',request_id=request_id,worker_boot=challenge['worker_boot'],
                           challenge=challenge['challenge'],control_version=0,ready=True)

    def establish_lease(self):
        # A transient generation/privacy-fence failure intentionally invalidates
        # its pending challenge. Start a new handshake; never renew that deadline.
        for _ in range(10):
            response=self.source_rpc('lease_challenge')
            self.assertEqual(response['status'],200,response)
            challenge=response['body'];request_id=str(uuid.uuid4())
            response=self.attest(challenge,request_id=request_id)
            if response['status']==200:return challenge,request_id
            self.assertIn(response['body'].get('error'),('EXPIRED','DEPENDENCY_UNAVAILABLE'),response)
            time.sleep(.05)
        self.fail('could not establish a fresh synthetic lease within bounded retries')

    def lifecycle(self):
        with self.admin.cursor() as cur:
            cur.execute('SELECT lifecycle_state,lifecycle_token FROM x_capi_worker_state WHERE singleton')
            return cur.fetchone()

    def rotate(self,child):
        filled=self.command(child,'fill','filled')
        self.assertEqual(filled['count'],61440)
        outcome=self.wait(child,'main_return')
        self.assertEqual(outcome['status'],75)
        self.assertFalse(outcome['fresh'])
        self.assertEqual(outcome['boot'],child['boot'])
        self.assertEqual(child['process'].wait(timeout=5),75)
        self.assertEqual(self.lifecycle()[0],'dirty')
        self.assertFalse((self.root/'rpc'/'handoff.sock').exists())
        return outcome

    def test_actual_os_replacement_preserves_durable_state_and_requires_new_attestation(self):
        # The production supervisor already watches process exit. The disposable
        # parent uses the same wait-before-start rule; no Docker daemon is probed.
        compose=(ROOT/'docker-compose.x-capi.yml').read_text().split('  x-capi-worker:',1)[1].split('\nnetworks:',1)[0]
        self.assertIn('restart: unless-stopped',compose)
        self.assertIn('ENTRYPOINT ["python3", "/app/x_capi_worker.py"]',
                      (ROOT/'docker/x-capi-worker/Dockerfile').read_text())
        first=self.launch()
        old,old_attest_id=self.establish_lease()
        self.assertTrue(self.command(first,'snapshot','snapshot')['fresh'])
        self.assertTrue(any(e['event']=='generation' for e in first['events']))
        owner='a'*64
        self.assertEqual(self.source_rpc('revoke',owner_hash=owner,reason='withdrawn')['status'],200)
        # Keep a separate consumed source/import/context through replacement;
        # no wallet is bound and no conversion is generated by this fixture.
        observed=int(time.time()*1000)
        captured=self.source_rpc('capture',owner_hash='d'*64,observation_id=str(uuid.uuid4()),
            observed_at_ms=observed,original_expires_at_ms=observed+fixtures.SOURCE_WINDOW_MS,
            landing_consented_at_ms=observed,policy='landing-x-v2',gpc=False,twclid='synthetic_imported_click_002')
        self.assertEqual(captured['status'],200,captured)
        self.app_rpc('prepare','c'*64,choice='grant',policy='app-x-v2')
        begun=self.app_rpc('begin','c'*64,choice='grant',policy='app-x-v2',destination='desktop')
        assigned=self.source_rpc('authorize',owner_hash='d'*64,handoff=begun['handoff'])
        self.assertEqual(assigned['status'],200,assigned)
        redeemed=self.app_rpc('redeem','c'*64,handoff=begun['handoff'])
        durable_import=self.imported_state()
        self.assertEqual(len(durable_import[0]),1)
        with self.admin.cursor() as cur:
            cur.execute('SELECT generation,state,landing_control_version FROM x_capi_handoff_generations')
            generations=cur.fetchall()
            cur.execute('SELECT landing_owner_hash,state,twclid_ciphertext FROM x_capi_handoff_sources ORDER BY landing_owner_hash')
            sources=cur.fetchall()
        token=self.lifecycle()[1]
        self.rotate(first)
        # Simulated failed spawn cannot turn a drained worker into CLEAN.
        with self.assertRaises(FileNotFoundError):
            subprocess.Popen([str(self.root/'missing-worker')],env=self.env)
        self.assertEqual(self.lifecycle(),('dirty',token))
        with self.assertRaises(protocol.HandoffError):self.source_rpc('lease_challenge')
        second=self.launch()
        self.assertNotEqual(first['process'].pid,second['process'].pid)
        self.assertNotEqual(first['boot'],second['boot'])
        self.assertNotEqual(self.lifecycle()[1],token)
        self.assertEqual(self.attest(old,request_id=old_attest_id)['body']['error'],'EXPIRED')
        self.assertEqual(self.attest(dict(old,worker_boot=second['boot']))['body']['error'],'EXPIRED')
        self.assertFalse(self.command(second,'snapshot','snapshot')['fresh'])
        now=int(time.time()*1000)
        closed=self.source_rpc('capture',owner_hash=owner,observation_id=str(uuid.uuid4()),
            observed_at_ms=now,original_expires_at_ms=now+fixtures.SOURCE_WINDOW_MS,
            landing_consented_at_ms=now,policy='landing-x-v2',gpc=False,twclid='synthetic_closed_click_001')
        self.assertEqual(closed['status'],410)
        self.assertEqual(closed['body']['error'],'WITHDRAWN')
        with self.admin.cursor() as cur:
            cur.execute('SELECT generation,state,landing_control_version FROM x_capi_handoff_generations')
            self.assertEqual(cur.fetchall(),generations)
            cur.execute('SELECT landing_owner_hash,state,twclid_ciphertext FROM x_capi_handoff_sources ORDER BY landing_owner_hash')
            self.assertEqual(cur.fetchall(),sources)
        self.assertEqual(self.imported_state(),durable_import)
        self.assertEqual(self.app_rpc('recover','c'*64,handoff=begun['handoff']),redeemed)
        self.establish_lease()
        self.assertTrue(self.command(second,'snapshot','snapshot')['fresh'])
        second['process'].terminate()
        outcome=self.wait(second,'main_return')
        self.assertEqual(outcome['status'],0)
        self.assertEqual(second['process'].wait(timeout=5),0)
        self.assertEqual(self.lifecycle(),('clean',None))
        print('PROCESS_REPLACEMENT_EVIDENCE '+json.dumps(dict(
            old_pid=first['process'].pid, new_pid=second['process'].pid,
            old_boot=first['boot'], new_boot=second['boot'], old_exit=75, new_exit=0,
            restart_state='dirty', new_initial_fresh=False, old_attestation='EXPIRED',
            retained_tombstone='WITHDRAWN', imported_mapping_preserved=True,
            same_context_recovery=True, new_fresh_after_attestation=True,
            schema_config_privacy_startup=True, final_state='clean')),flush=True)

    def test_failed_replacement_startup_remains_dirty_and_quarantined_generation_stays_closed(self):
        first=self.launch();self.rotate(first)
        token=self.lifecycle()[1]
        with self.admin.cursor() as cur:cur.execute('UPDATE x_capi_schema_meta SET schema_version=4')
        try:
            failed=self.launch(ready=False)
            outcome=self.wait(failed,'main_return')
            self.assertEqual(outcome['status'],2)
            self.assertEqual(failed['process'].wait(timeout=5),2)
            self.assertTrue(any(e['event']=='schema' and not e['ok'] for e in failed['events']))
            self.assertFalse(any(e['event']=='ready' for e in failed['events']))
            self.assertEqual(self.lifecycle(),('dirty',token))
        finally:
            with self.admin.cursor() as cur:cur.execute('UPDATE x_capi_schema_meta SET schema_version=5')
        with self.admin.cursor() as cur:cur.execute("UPDATE x_capi_handoff_generations SET state='quarantined'")
        replacement=self.launch()
        result=self.source_rpc('lease_challenge')
        self.assertEqual(result['body']['error'],'QUARANTINED')
        self.assertFalse(self.command(replacement,'snapshot','snapshot')['fresh'])
        with self.admin.cursor() as cur:
            cur.execute('SELECT state FROM x_capi_handoff_generations');self.assertEqual(cur.fetchone(),('quarantined',))
        print('FAILED_REPLACEMENT_EVIDENCE '+json.dumps(dict(
            failed_pid=failed['process'].pid, failed_exit=2, lifecycle='dirty',
            normal_schema_rejection=True, replacement_pid=replacement['process'].pid,
            quarantine_preserved=True, fresh=False)),flush=True)

    def test_failed_real_rpc_drain_cannot_exit_clean_or_start_replacement(self):
        first=self.launch();self.command(first,'block_next','block_armed')
        result=[]
        def request():
            try:result.append(self.source_rpc('revoke',owner_hash='b'*64,reason='withdrawn'))
            except protocol.HandoffError as exc:result.append(exc.code)
        thread=threading.Thread(target=request);thread.start()
        self.wait(first,'rpc_blocked')
        self.command(first,'fill','filled')
        outcome=self.wait(first,'main_return',timeout=12)
        self.assertEqual(outcome['status'],2)
        self.assertFalse(outcome['fresh'])
        self.assertIsNone(first['process'].poll(),'accepted unresolved handler still owns old process')
        self.assertEqual(self.lifecycle()[0],'dirty')
        self.assertFalse(any(e['event']=='shutdown_drained' for e in first['events']))
        first['process'].stdin.write('{"op":"release"}\n');first['process'].stdin.flush()
        self.assertEqual(first['process'].wait(timeout=5),2)
        thread.join(3);self.assertFalse(thread.is_alive())
        self.assertEqual(result,['DEPENDENCY_UNAVAILABLE'])
        self.assertEqual(self.lifecycle()[0],'dirty')
        with self.admin.cursor() as cur:
            cur.execute('SELECT state FROM x_capi_handoff_sources WHERE landing_owner_hash=%s',(bytes.fromhex('b'*64),))
            self.assertEqual(cur.fetchone(),('closed',),'accepted privacy work must not be cancelled')
        print('FAILED_DRAIN_EVIDENCE '+json.dumps(dict(
            pid=first['process'].pid, exit_status=2, lifecycle='dirty',
            no_clean_marker=True, accepted_privacy_work_completed=True)),flush=True)


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--child':child_main(sys.argv[2])
    else:unittest.main()
