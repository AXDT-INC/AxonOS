"""Worker boundary integration tests with deterministic private fake connections."""
import contextlib
import types
import unittest
from unittest import mock

from axonos_gate import x_capi_handoff as handoff
from axonos_gate import x_capi_handoff_worker as integration

GENERATION = '11111111-1111-4111-8111-111111111111'


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.inside = False
        self.events = []
        test = self

        class Fence:
            def __init__(self, path):
                test.events.append('fence-created')

            def open(self, create_controls=False):
                test.events.append('fence-open')

            @contextlib.contextmanager
            def dispatch_boundary(self):
                test.inside = True
                test.events.append('boundary-enter')
                try:
                    yield True
                finally:
                    test.events.append('boundary-exit')
                    test.inside = False

            privacy_close_boundary = dispatch_boundary

            def close(self):
                test.events.append('fence-close')

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, query, parameters=None):
                test.events.append(('query', query))

        class Connection:
            def cursor(self):
                return Cursor()

            def commit(self):
                test.events.append(('commit', test.inside))

            def rollback(self):
                test.events.append(('rollback', test.inside))

            def close(self):
                test.events.append('connection-close')

        self.connection = Connection()
        self.worker = types.SimpleNamespace(PrivacyFence=Fence,
            _configure_connection=lambda conn: None, _schema_ready=lambda conn: True,
            verify_config_guard=lambda conn, cfg: True)
        self.store = types.SimpleNamespace(epoch=42, generation=GENERATION, audience=bytes.fromhex('a' * 64))
        self.lease = handoff.FreshnessLease(42, GENERATION, elapsed_ms=lambda: 1000)
        self.runtime = integration.WorkerHandoffRuntime(self.worker, store=self.store,
            keyring=handoff.ServiceKeyring('test-1', {'test-1': bytes(range(32))}),
            lease=self.lease, privacy_path='/synthetic/unused', clock_assured=lambda: True,
            connection_factory=lambda: self.connection)

    def test_success_commit_precedes_exclusive_boundary_release(self):
        with self.runtime.transaction(closure=True) as cursor:
            cursor.execute('synthetic no-op')
        self.assertIn(('commit', True), self.events)
        self.assertLess(self.events.index(('commit', True)), self.events.index('boundary-exit'))

    def test_durable_quarantine_commits_inside_boundary(self):
        with self.assertRaises(handoff.HandoffError):
            with self.runtime.transaction(closure=True):
                raise handoff.HandoffError('QUARANTINED', durable_error=True)
        self.assertIn(('commit', True), self.events)
        self.assertNotIn(('commit', False), self.events)
        self.assertFalse(self.lease.allows_dispatch())

    def test_optional_nonprivacy_failure_does_not_commit(self):
        with self.assertRaises(handoff.HandoffError):
            with self.runtime.transaction():
                raise handoff.HandoffError('RATE_LIMITED')
        self.assertFalse(any(isinstance(event, tuple) and event[0] == 'commit' for event in self.events))
        self.assertIn('connection-close', self.events)

    def test_unexpected_internal_exception_is_sanitized(self):
        with self.assertRaises(handoff.HandoffError) as caught:
            with self.runtime.transaction(closure=True):
                raise RuntimeError('synthetic-secret-do-not-surface')
        self.assertEqual(caught.exception.code, 'DEPENDENCY_UNAVAILABLE')
        self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_privacy_closure_available_while_positive_config_guard_fails(self):
        self.worker.verify_config_guard = lambda conn, cfg: False
        with self.runtime.transaction(closure=True):
            pass
        with self.assertRaises(handoff.HandoffError) as caught:
            with self.runtime.transaction():
                pass
        self.assertEqual(caught.exception.code, 'QUARANTINED')

    def test_missing_schema_never_reaches_operation(self):
        self.worker._schema_ready = lambda conn: False
        with self.assertRaises(handoff.HandoffError):
            with self.runtime.transaction(closure=True):
                self.fail('operation reached missing schema')
        self.assertNotIn(('commit', True), self.events)

    def test_dispatch_requires_mode_clock_and_live_lease(self):
        self.runtime.positive_allowed = lambda: True
        self.assertFalse(self.runtime.dispatch_fresh())
        self.runtime.lease.allows_dispatch = lambda: True
        self.assertTrue(self.runtime.dispatch_fresh())
        self.runtime.clock_assured = lambda: False
        self.assertFalse(self.runtime.dispatch_fresh())
        self.runtime.clock_assured = lambda: True
        self.runtime.positive_allowed = lambda: False
        self.assertFalse(self.runtime.dispatch_fresh())

    def test_readiness_never_renews_a_missing_lease_and_tracks_clock_and_retirement(self):
        self.runtime.positive_allowed = lambda: True
        self.assertFalse(self.runtime.readiness()['ready'])
        self.assertFalse(self.lease.allows_dispatch())
        self.runtime.lease.allows_dispatch = lambda: True
        self.assertTrue(self.runtime.readiness()['ready'])
        self.runtime.clock_assured = lambda: False
        self.assertFalse(self.runtime.readiness()['ready'])
        self.runtime.clock_assured = lambda: True
        self.runtime.positive_allowed = lambda: False
        self.assertFalse(self.runtime.readiness()['ready'])


    def test_high_water_retires_and_drains_without_in_process_boot_replacement(self):
        import uuid
        def req(op, **extra):
            return dict(v=2, epoch=42, generation=GENERATION,
                        request_id=str(uuid.uuid4()), op=op, **extra)
        old = self.lease
        challenge = old.challenge(req('lease_challenge'))
        attestation = req('lease_attest', worker_boot=challenge['worker_boot'],
                          challenge=challenge['challenge'], control_version=1, ready=True)
        old.attest(attestation)
        self.assertTrue(old.allows_dispatch())
        old._seen_challenges.update({str(i): 'synthetic' for i in range(handoff.LEASE_LEDGER_HIGH_WATER)})
        original_count = old.ledger_status()['count']
        original_boot = old.worker_boot
        self.runtime.server = mock.Mock()
        self.runtime.server.close.return_value = True
        old_server = self.runtime.server
        with mock.patch.object(self.runtime, 'start') as start, \
             mock.patch.object(integration, 'FreshnessLease') as factory:
            self.assertEqual(self.runtime.prepare_restart_if_needed(), 'restart_ready')
            self.assertEqual(self.runtime.prepare_restart_if_needed(), 'restart_ready')
            self.assertTrue(self.runtime._restart_required)
            self.assertFalse(old.allows_dispatch())
            self.assertFalse(self.runtime.positive_allowed())
            self.assertIs(self.runtime.lease, old)
            self.assertIs(self.runtime.service.lease, old)
            self.assertEqual(old.worker_boot, original_boot)
            self.assertEqual(old.ledger_status()['count'], original_count)
            old_server.close.assert_called_once_with(timeout=3.0)
            start.assert_not_called()
            factory.assert_not_called()
            with self.assertRaises(handoff.HandoffError):
                old.attest(attestation)
            with self.assertRaises(handoff.HandoffError):
                old.challenge(req('lease_challenge'))

    def test_real_rpc_high_water_closes_old_listener_without_new_boot(self):
        import os
        import tempfile
        import uuid
        from pathlib import Path
        from axonos_gate import x_capi_handoff_rpc as rpc
        def req(op, **extra):
            return dict(v=2, epoch=42, generation=GENERATION,
                        request_id=str(uuid.uuid4()), op=op, **extra)
        def frame(document):
            raw = handoff.canonical_json(document)
            return dict(kind='source', raw_body=raw.decode(), kid='test-1',
                        mac=self.runtime.keyring.request_mac(raw, 'test-1'))
        cfg = types.SimpleNamespace(producer_ready=True, policy_version=integration.POLICY,
                                    policy_epoch=42, audience_scope='a' * 64)
        self.runtime.generation_guard = lambda document: document.get('control_version', 0)
        self.runtime.service.generation_guard = self.runtime.generation_guard
        with tempfile.TemporaryDirectory(prefix='handoff-replace-rpc-') as directory:
            path = str(Path(directory) / 'handoff.sock')
            with mock.patch.object(integration, 'DEFAULT_SOCKET', path), \
                 mock.patch.dict(os.environ, {'X_CAPI_INGEST_ALLOWED_UID': str(os.getuid())}), \
                 mock.patch.object(integration.x_capi, 'load_config', return_value=cfg):
                self.runtime.start()
                try:
                    client = rpc.HandoffRpcClient(path, expected_uid=os.getuid())
                    challenge = client.request(frame(req('lease_challenge')))['body']
                    attest = req('lease_attest', worker_boot=challenge['worker_boot'],
                                 challenge=challenge['challenge'], control_version=1, ready=True)
                    self.assertEqual(client.request(frame(attest))['status'], 200)
                    self.assertTrue(self.runtime.dispatch_fresh())
                    old = self.runtime.lease
                    old._seen_challenges.update({str(i): 'synthetic' for i in range(handoff.LEASE_LEDGER_HIGH_WATER)})
                    self.assertEqual(self.runtime.prepare_restart_if_needed(), 'restart_ready')
                    self.assertIs(self.runtime.lease, old)
                    self.assertEqual(challenge['worker_boot'], old.worker_boot)
                    self.assertFalse(old.allows_dispatch())
                    self.assertFalse(self.runtime.dispatch_fresh())
                    self.assertFalse(Path(path).exists())
                    with self.assertRaises(handoff.HandoffError):
                        client.request(frame(attest))
                finally:
                    self.assertTrue(self.runtime.close())

    def test_failed_restart_drain_is_latched_even_after_later_close_succeeds(self):
        self.lease.retire_boot()
        self.runtime.server = mock.Mock()
        self.runtime.server.close.side_effect = [False, True]
        server = self.runtime.server
        with mock.patch.object(self.runtime, 'start') as start, \
             mock.patch.object(integration, 'FreshnessLease') as factory:
            self.assertEqual(self.runtime.prepare_restart_if_needed(), 'drain_failed')
            self.assertEqual(self.runtime.prepare_restart_if_needed(), 'drain_failed')
            self.assertEqual(server.close.call_count, 1)
            self.assertTrue(self.runtime._restart_required)
            self.assertFalse(self.runtime.lease.allows_dispatch())
            self.assertTrue(self.runtime.close())
            self.assertEqual(self.runtime.prepare_restart_if_needed(), 'drain_failed')
            self.assertIs(self.runtime.lease, self.lease)
            start.assert_not_called()
            factory.assert_not_called()

    def test_restart_drain_exception_is_sanitized_and_latched(self):
        self.lease.retire_boot()
        self.runtime.server = mock.Mock()
        self.runtime.server.close.side_effect = OSError('synthetic-private-detail')
        self.assertEqual(self.runtime.prepare_restart_if_needed(), 'drain_failed')
        self.assertEqual(self.runtime.prepare_restart_if_needed(), 'drain_failed')
        self.runtime.server.close.assert_called_once_with(timeout=3.0)
        self.assertFalse(self.runtime.dispatch_fresh())

    def test_worker_supervisor_polls_lifecycle_before_dispatch_iteration(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / 'x_capi_worker.py').read_text()
        main = text[text.index('def main(argv=None)'):]
        self.assertLess(main.index('_handoff_runtime.prepare_restart_if_needed()'), main.index('outcome = run_once('))

    def test_main_process_replacement_exits_only_after_drain_and_never_marks_clean(self):
        from axonos_gate import x_capi_worker as worker
        cases = (
            ('restart_ready', True, True, True, worker.HANDOFF_RESTART_EXIT_STATUS, 1),
            ('restart_ready', False, True, True, 2, 0),
            ('restart_ready', True, False, True, 2, 0),
            ('restart_ready', True, True, False, 2, 1),
            ('drain_failed', True, True, True, 2, 0),
            (RuntimeError('synthetic'), True, True, True, 2, 0),
        )
        for lifecycle, rpc_stop, consent_stop, drained, status, drain_calls in cases:
            with self.subTest(lifecycle=str(lifecycle), rpc_stop=rpc_stop,
                              consent_stop=consent_stop, drained=drained):
                runtime = mock.Mock()
                if isinstance(lifecycle, Exception):
                    runtime.prepare_restart_if_needed.side_effect = lifecycle
                else:
                    runtime.prepare_restart_if_needed.return_value = lifecycle
                runtime.close.return_value = rpc_stop
                consent = mock.Mock(failure_type=None)
                consent.stop.return_value = consent_stop
                with mock.patch.object(worker, '_stop', False), \
                     mock.patch.object(worker, '_handoff_runtime', None), \
                     mock.patch.object(worker.signal, 'signal'), \
                     mock.patch.object(worker, 'IngestListener'), \
                     mock.patch.object(worker, 'ConsentListener'), \
                     mock.patch.object(worker, 'PrivacyFence'), \
                     mock.patch.object(worker, 'activate_worker_startup', return_value=True), \
                     mock.patch.object(worker, 'ConsentService', return_value=consent), \
                     mock.patch.object(integration.WorkerHandoffRuntime, 'from_environment', return_value=runtime), \
                     mock.patch.object(worker, 'run_once') as iteration, \
                     mock.patch.object(worker, 'complete_worker_shutdown', return_value=drained) as finish:
                    self.assertEqual(worker.main([]), status)
                    iteration.assert_not_called()
                    self.assertEqual(finish.call_count, drain_calls)
                    if drain_calls:
                        self.assertEqual(finish.call_args.kwargs, {'mark_clean': False})
                    consent.stop.assert_called_once_with(drain=True)

    def test_main_failed_real_rpc_drain_never_marks_clean_or_returns_success(self):
        """Exercise the ordinary main() stop and the actual three-second drain."""
        import os
        import tempfile
        import threading
        import time
        import uuid
        from pathlib import Path
        from axonos_gate import x_capi_handoff_rpc as rpc
        from axonos_gate import x_capi_worker as worker

        entered, release = threading.Event(), threading.Event()
        def unresolved(frame):
            entered.set()
            release.wait(15)
            return dict(status=200, headers={}, body=dict(v=2, ok=True,
                state='withdrawn', request_id=frame['payload']['request_id']))
        frame = dict(kind='app', op='withdraw', owner_hash='b' * 64,
                     new_owner=False, payload=dict(v=2, reason='withdrawn',
                                                  request_id=str(uuid.uuid4())))
        with tempfile.TemporaryDirectory(prefix='handoff-main-drain-') as directory:
            path = str(Path(directory) / 'handoff.sock')
            server = rpc.HandoffRpcServer(path, unresolved, allowed_uid=os.getuid())
            self.runtime.server = server
            client = rpc.HandoffRpcClient(path, expected_uid=os.getuid())
            def request():
                try:
                    client.request(frame)
                except handoff.HandoffError:
                    pass  # An unresolved privacy acknowledgment remains pending.
            thread = threading.Thread(target=request)
            def iteration(**kwargs):
                thread.start()
                self.assertTrue(entered.wait(2), 'actual RPC handler was not admitted')
                worker._stop = True
                return 'idle'
            consent = mock.Mock(failure_type=None)
            consent.stop.return_value = True
            try:
                with mock.patch.object(worker, '_stop', False), \
                     mock.patch.object(worker, '_handoff_runtime', None), \
                     mock.patch.object(worker.signal, 'signal'), \
                     mock.patch.object(worker, 'IngestListener'), \
                     mock.patch.object(worker, 'ConsentListener'), \
                     mock.patch.object(worker, 'PrivacyFence'), \
                     mock.patch.object(worker, 'activate_worker_startup', return_value=True), \
                     mock.patch.object(worker, 'ConsentService', return_value=consent), \
                     mock.patch.object(integration.WorkerHandoffRuntime, 'from_environment', return_value=self.runtime), \
                     mock.patch.object(self.runtime, 'start', side_effect=server.start), \
                     mock.patch.object(worker, 'run_once', side_effect=iteration), \
                     mock.patch.object(worker, '_wait_between_iterations'), \
                     mock.patch.object(worker, 'complete_worker_shutdown', return_value=True) as clean:
                    started = time.monotonic()
                    status = worker.main([])
                    elapsed = time.monotonic() - started
                    self.assertFalse(release.is_set())
                    self.assertTrue(self.runtime._closed)
                    self.assertFalse(self.lease.allows_dispatch())
                    self.assertGreaterEqual(elapsed, 2.8)
                    self.assertLess(elapsed, 8)
                    self.assertEqual((status, clean.call_count), (2, 0),
                        'undrained handoff handler must prohibit success and CLEAN')
            finally:
                release.set()
                if thread.ident is not None:
                    thread.join(3)
                self.assertTrue(server.close())


    def test_main_partial_start_with_real_undrained_rpc_exits_dirty(self):
        """A failed replacement start cannot discard an admitted closure handler."""
        import os
        import tempfile
        import threading
        import time
        import uuid
        from pathlib import Path
        from axonos_gate import x_capi_handoff_rpc as rpc
        from axonos_gate import x_capi_worker as worker
        entered, release = threading.Event(), threading.Event()
        def unresolved(frame):
            entered.set()
            release.wait(15)
            return dict(status=200, headers={}, body=dict(v=2, ok=True,
                state='withdrawn', request_id=frame['payload']['request_id']))
        frame = dict(kind='app', op='withdraw', owner_hash='b' * 64,
                     new_owner=False, payload=dict(v=2, reason='withdrawn',
                                                  request_id=str(uuid.uuid4())))
        with tempfile.TemporaryDirectory(prefix='handoff-partial-start-') as directory:
            path = str(Path(directory) / 'handoff.sock')
            server = rpc.HandoffRpcServer(path, unresolved, allowed_uid=os.getuid())
            self.runtime.server = server
            client = rpc.HandoffRpcClient(path, expected_uid=os.getuid())
            def request():
                try:
                    client.request(frame)
                except handoff.HandoffError:
                    pass
            thread = threading.Thread(target=request)
            def partial_start():
                server.start()
                thread.start()
                self.assertTrue(entered.wait(2))
                raise OSError('synthetic partial listener start')
            def unexpected_iteration(**kwargs):
                worker._stop = True
                return 'idle'
            consent = mock.Mock(failure_type=None)
            consent.stop.return_value = True
            try:
                with mock.patch.object(worker, '_stop', False), \
                     mock.patch.object(worker, '_handoff_runtime', None), \
                     mock.patch.object(worker.signal, 'signal'), \
                     mock.patch.object(worker, 'IngestListener'), \
                     mock.patch.object(worker, 'ConsentListener'), \
                     mock.patch.object(worker, 'PrivacyFence'), \
                     mock.patch.object(worker, 'activate_worker_startup', return_value=True), \
                     mock.patch.object(worker, 'ConsentService', return_value=consent), \
                     mock.patch.object(integration.WorkerHandoffRuntime, 'from_environment', return_value=self.runtime), \
                     mock.patch.object(self.runtime, 'start', side_effect=partial_start), \
                     mock.patch.object(worker, 'run_once', side_effect=unexpected_iteration) as iteration, \
                     mock.patch.object(worker, 'complete_worker_shutdown') as clean:
                    started = time.monotonic()
                    self.assertEqual(worker.main([]), 2)
                    self.assertGreaterEqual(time.monotonic() - started, 2.8)
                    self.assertLess(time.monotonic() - started, 6)
                    self.assertFalse(release.is_set())
                    self.assertIs(worker._handoff_runtime, self.runtime)
                    self.assertTrue(self.runtime._closed)
                    self.assertFalse(self.lease.allows_dispatch())
                    iteration.assert_not_called()
                    clean.assert_not_called()
            finally:
                release.set()
                if thread.ident is not None:
                    thread.join(3)
                self.assertTrue(server.close())

    def test_main_clean_marker_requires_both_services_and_successful_closure(self):
        from axonos_gate import x_capi_worker as worker
        cases = ((True, True, True, 0, 1), (False, True, True, 2, 0),
                 (RuntimeError('synthetic private detail'), True, True, 2, 0),
                 (True, False, True, 2, 0), (True, True, False, 2, 1))
        for rpc_stop, consent_stop, clean_result, expected_status, clean_calls in cases:
            with self.subTest(rpc_stop=type(rpc_stop).__name__ if isinstance(rpc_stop, Exception) else rpc_stop,
                              consent_stop=consent_stop, clean_result=clean_result):
                runtime = mock.Mock()
                if isinstance(rpc_stop, Exception):
                    runtime.close.side_effect = rpc_stop
                else:
                    runtime.close.return_value = rpc_stop
                consent = mock.Mock(failure_type=None)
                consent.stop.return_value = consent_stop
                with mock.patch.object(worker, '_stop', True), \
                     mock.patch.object(worker, '_handoff_runtime', None), \
                     mock.patch.object(worker.signal, 'signal'), \
                     mock.patch.object(worker, 'IngestListener'), \
                     mock.patch.object(worker, 'ConsentListener'), \
                     mock.patch.object(worker, 'PrivacyFence'), \
                     mock.patch.object(worker, 'activate_worker_startup', return_value=True), \
                     mock.patch.object(worker, 'ConsentService', return_value=consent), \
                     mock.patch.object(integration.WorkerHandoffRuntime, 'from_environment', return_value=runtime), \
                     mock.patch.object(worker, 'complete_worker_shutdown', return_value=clean_result) as clean:
                    self.assertEqual(worker.main([]), expected_status)
                    self.assertEqual(clean.call_count, clean_calls)
                    consent.stop.assert_called_once_with(drain=True)
                    self.assertEqual(worker._handoff_runtime is None, rpc_stop is True)



class StandalonePackagingTests(unittest.TestCase):
    def test_gate_image_contains_bridge_asset_at_runtime_fallback(self):
        import pathlib
        import shlex
        import tempfile
        from axonos_gate import x_capi_handoff_http as http
        root = pathlib.Path(__file__).resolve().parents[2]
        destination = '/usr/share/novnc/app/x-handoff-bridge.js'
        copies = []
        for line in (root / 'Dockerfile').read_text().splitlines():
            if not line.startswith('COPY '):
                continue
            fields = shlex.split(line, comments=True)
            if fields[:1] == ['COPY'] and fields[-1:] == [destination]:
                copies.append(fields)
        self.assertEqual(len(copies), 1, 'gate image must package the bridge fallback')
        self.assertEqual(len(copies[0]), 3)
        source = root / copies[0][1]
        script = source.read_bytes()
        self.assertGreater(len(script), 100)
        # Materialize the declared image asset and render through the actual
        # reader. Checkout-only source lookup must not conceal missing COPY.
        with tempfile.TemporaryDirectory(prefix='handoff-image-asset-') as directory:
            packaged = pathlib.Path(directory) / destination.lstrip('/')
            packaged.parent.mkdir(parents=True)
            packaged.write_bytes(script)
            with mock.patch.object(http, 'BRIDGE_SCRIPT', packaged):
                response = http._bridge('entry')
            self.assertEqual(response.status, 200)
            self.assertIn(script, response.body)

    def test_supervisor_and_worker_flat_module_imports(self):
        import pathlib
        import subprocess
        import sys
        gate = str(pathlib.Path(__file__).resolve().parents[1])
        code = "import sys;sys.dont_write_bytecode=True;sys.path.insert(0,sys.argv[1]);import x_capi_handoff_http,x_capi_handoff_worker,x_capi_handoff_store"
        result = subprocess.run([sys.executable,"-I","-c",code,gate],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
