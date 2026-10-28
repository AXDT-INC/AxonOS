"""Bounded Unix RPC and worker auth tests, using temporary local sockets only."""
import concurrent.futures
import json
import os
import pathlib
import socket
import tempfile
import threading
import time
import unittest
import uuid

from axonos_gate import x_capi_handoff as h
from axonos_gate import x_capi_handoff_rpc as rpc

GENERATION = '11111111-1111-4111-8111-111111111111'


def source(op='revoke', **extra):
    doc = dict(v=2, epoch=42, generation=GENERATION, request_id=str(uuid.uuid4()), op=op)
    if op == 'revoke':
        doc.update(owner_hash='a' * 64, reason='withdrawn')
    doc.update(extra)
    return doc


def source_reply(doc):
    return dict(v=2, ok=True, epoch=doc['epoch'], generation=doc['generation'], request_id=doc['request_id'], state='withdrawn')


def app_frame(op='prepare'):
    body = dict(v=2, request_id=str(uuid.uuid4()), choice='grant', policy='app-x-v2')
    if op == 'withdraw':
        body = dict(v=2, request_id=str(uuid.uuid4()), reason='withdrawn')
    return dict(kind='app', op=op, payload=body, owner_hash='b' * 64, new_owner=op == 'prepare')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.keys = h.ServiceKeyring('test-1', {'test-1': bytes(range(32))})
        self.now = 1000
        self.lease = h.FreshnessLease(42, GENERATION, elapsed_ms=lambda: self.now)
        self.enabled, self.assured = True, True
        self.seen = []
        self.service = rpc.WorkerHandoffService(keyring=self.keys, lease=self.lease,
            source_handler=self.handle_source, app_handler=self.handle_app,
            positive_allowed=lambda: self.enabled, clock_assured=lambda: self.assured,
            generation_guard=lambda req: req.get('control_version', 0))

    def handle_source(self, doc):
        self.seen.append(doc['op'])
        return source_reply(doc)

    def handle_app(self, op, body, owner, new_owner):
        self.seen.append(op)
        if op == 'withdraw':
            return dict(v=2, ok=True, state='withdrawn', request_id=body['request_id'])
        return dict(v=2, ok=True, state='prepared', expires_at_ms=100000000)

    def frame(self, doc):
        raw = h.canonical_json(doc)
        return dict(kind='source', raw_body=raw.decode(), kid='test-1', mac=self.keys.request_mac(raw, 'test-1'))

    def test_success_and_authenticated_error_responses_signed(self):
        doc = source()
        frame = self.frame(doc)
        result = self.service.dispatch(frame)
        self.assertEqual(result['status'], 200)
        self.keys.verify_response(frame['raw_body'].encode(), 200, h.canonical_json(result['body']), 'test-1', result['headers']['X-AxonOS-Handoff-MAC'])
        bad = self.frame(dict(doc, extra='not allowed'))
        result = self.service.dispatch(bad)
        self.assertEqual(result['status'], 400)
        self.keys.verify_response(bad['raw_body'].encode(), 400, h.canonical_json(result['body']), 'test-1', result['headers']['X-AxonOS-Handoff-MAC'])
        self.assertEqual(self.seen, ['revoke'])

    def test_authenticated_admission_failure_signed_without_handler(self):
        frame = self.frame(source())
        response = self.service.reject(frame)
        self.assertEqual(response['status'], 429)
        self.keys.verify_response(frame['raw_body'].encode(), 429, h.canonical_json(response['body']), 'test-1', response['headers']['X-AxonOS-Handoff-MAC'])
        self.assertEqual(self.seen, [])

    def test_unauthenticated_failure_unsigned_and_no_handler(self):
        frame = self.frame(source())
        frame['mac'] = h.new_token()
        result = self.service.dispatch(frame)
        self.assertEqual(result['status'], 401)
        self.assertEqual(result['headers'], {})
        self.assertEqual(self.seen, [])

    def test_off_and_clock_failure_do_not_block_closure(self):
        self.enabled = False
        self.assertEqual(self.service.dispatch(app_frame())['body']['error'], 'ATTRIBUTION_DISABLED')
        self.assertEqual(self.service.dispatch(self.frame(source()))['status'], 200)
        self.assertEqual(self.service.dispatch(app_frame('withdraw'))['status'], 200)
        self.enabled, self.assured = True, False
        self.assertEqual(self.service.dispatch(app_frame())['body']['error'], 'CLOCK_UNTRUSTED')
        self.assertEqual(self.service.dispatch(self.frame(source()))['status'], 200)

    def test_wrong_scope_and_unknown_operation(self):
        frame = self.frame(source(generation=str(uuid.uuid4())))
        self.assertEqual(self.service.dispatch(frame)['body']['error'], 'GENERATION_RETIRED')
        frame = self.frame(source(op='create_wallet'))
        self.assertEqual(self.service.dispatch(frame)['body']['error'], 'INVALID_REQUEST')
        self.assertEqual(self.seen, [])

    def test_source_lease_full_handshake_no_deadline_extension(self):
        frame = self.frame(source('lease_challenge'))
        result = self.service.dispatch(frame)
        self.assertEqual(result['status'], 200)
        challenge = result['body']
        self.now += 29000
        frame = self.frame(source('lease_attest', worker_boot=challenge['worker_boot'], challenge=challenge['challenge'], control_version=7, ready=True))
        result = self.service.dispatch(frame)
        self.assertEqual(result['body']['remaining_ms'], 1000)
        self.assertTrue(self.lease.allows_dispatch())
        self.now += 1000
        self.assertEqual(self.service.dispatch(frame)['body']['error'], 'EXPIRED')
        self.assertFalse(self.lease.allows_dispatch())

    def test_missing_generation_guard_and_failed_commit_close_freshness(self):
        self.service.generation_guard = None
        self.assertEqual(self.service.dispatch(self.frame(source('lease_challenge')))['body']['error'], 'QUARANTINED')
        self.service.generation_guard = lambda req: (_ for _ in ()).throw(RuntimeError('private diagnostic'))
        result = self.service.dispatch(self.frame(source('lease_challenge')))
        self.assertEqual(result['body']['error'], 'DEPENDENCY_UNAVAILABLE')
        self.assertNotIn('private', json.dumps(result))

    def test_internal_errors_sanitized_and_signed(self):
        self.service.source_handler = lambda req: (_ for _ in ()).throw(RuntimeError('raw click secret'))
        frame = self.frame(source())
        result = self.service.dispatch(frame)
        self.assertEqual(result['status'], 503)
        self.assertIn('X-AxonOS-Handoff-MAC', result['headers'])
        self.assertNotIn('raw click', json.dumps(result))

    def test_browser_supplied_scope_missing_or_unknown_fields_rejected(self):
        frame = app_frame()
        for bad in (dict(frame, owner_hash=None), dict(frame, owner_hash='A' * 64), dict(frame, raw_cookie='secret'), dict(frame, op='begin', new_owner=True)):
            self.assertNotEqual(self.service.dispatch(bad)['status'], 200)
        self.assertEqual(self.seen, [])


@unittest.skipUnless(hasattr(socket, 'SOCK_SEQPACKET') and hasattr(socket, 'SO_PEERCRED'), 'Linux credentialled Unix RPC required')
class SocketTests(ServiceTests):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory(prefix='handoff-rpc-')
        self.path = str(pathlib.Path(self.directory.name) / 'handoff.sock')
        self.server = rpc.HandoffRpcServer(self.path, self.service.dispatch, allowed_uid=os.getuid()).start()
        self.client = rpc.HandoffRpcClient(self.path, expected_uid=os.getuid())

    def tearDown(self):
        self.server.close()
        self.directory.cleanup()

    def test_real_unix_peer_roundtrip(self):
        result = self.client.request(self.frame(source()))
        self.assertEqual(result['status'], 200)
        self.assertIn('X-AxonOS-Handoff-MAC', result['headers'])
        self.assertEqual(self.client.request(app_frame())['body']['state'], 'prepared')

    def test_wrong_server_uid_and_socket_mode_fail_closed(self):
        wrong = rpc.HandoffRpcClient(self.path, expected_uid=os.getuid() + 1)
        with self.assertRaises(h.HandoffError):
            wrong.request(self.frame(source()))
        os.chmod(self.path, 0o666)
        with self.assertRaises(h.HandoffError):
            self.client.request(self.frame(source()))

    def test_oversized_and_duplicate_rpc_frames_not_dispatched(self):
        for raw in (b'x' * (h.RPC_MAX + 1), b'{"kind":"app","kind":"source"}'):
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
                client.settimeout(0.5)
                client.connect(self.path)
                client.send(raw)
                self.assertEqual(client.recv(32), b'')
        self.assertEqual(self.seen, [])

    def test_reserved_closure_admission_when_positive_saturated(self):
        self.server.close()
        entered, release = threading.Event(), threading.Event()
        def dispatch(frame):
            if frame['kind'] == 'app' and frame['op'] == 'prepare':
                entered.set()
                release.wait(timeout=1)
            return self.service.dispatch(frame)
        self.server = rpc.HandoffRpcServer(self.path, dispatch, allowed_uid=os.getuid(), positive_slots=1, closure_slots=1).start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.client.request, app_frame())
            self.assertTrue(entered.wait(timeout=0.5))
            try:
                self.assertEqual(self.client.request(app_frame())['status'], 429)
                self.assertEqual(self.client.request(self.frame(source()))['status'], 200)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=1)['status'], 200)


    def test_recycle_drain_waits_for_admitted_handler_and_closes_admission(self):
        self.server.close()
        entered, release = threading.Event(), threading.Event()
        original = self.service.dispatch
        def delayed(frame):
            entered.set()
            release.wait(timeout=2)
            return original(frame)
        self.server = rpc.HandoffRpcServer(self.path, delayed, allowed_uid=os.getuid()).start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(self.client.request, self.frame(source()))
            self.assertTrue(entered.wait(timeout=0.5))
            try:
                self.assertFalse(self.server.close(timeout=0.1))
                self.assertFalse(pathlib.Path(self.path).exists())
                with self.assertRaises(h.HandoffError):
                    self.client.request(app_frame())
            finally:
                release.set()
            self.assertEqual(response.result(timeout=1)['status'], 200)
            self.assertTrue(self.server.close(timeout=1))

    def test_retired_boot_cannot_accept_inflight_attestation_during_drain(self):
        challenge = self.client.request(self.frame(source('lease_challenge')))['body']
        frame = self.frame(source('lease_attest', worker_boot=challenge['worker_boot'],
                                 challenge=challenge['challenge'], control_version=1, ready=True))
        self.server.close()
        entered, release = threading.Event(), threading.Event()
        original = self.service.dispatch
        def delayed(item):
            entered.set()
            release.wait(timeout=2)
            return original(item)
        self.server = rpc.HandoffRpcServer(self.path, delayed, allowed_uid=os.getuid()).start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(self.client.request, frame)
            self.assertTrue(entered.wait(timeout=0.5))
            self.lease.retire_boot()
            try:
                self.assertFalse(self.server.close(timeout=0.1))
            finally:
                release.set()
            self.assertEqual(response.result(timeout=1)['body']['error'], 'EXPIRED')
            self.assertFalse(self.lease.allows_dispatch())
            self.assertTrue(self.server.close(timeout=1))

    def test_private_stale_socket_reclaimed_after_crash(self):
        self.server.close()
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as stale:
            stale.bind(self.path)
            os.chmod(self.path, 0o600)
        self.server = rpc.HandoffRpcServer(self.path, self.service.dispatch, allowed_uid=os.getuid()).start()
        self.assertEqual(self.client.request(self.frame(source()))['status'], 200)

    def test_existing_socket_is_never_unlinked_by_second_server(self):
        other = rpc.HandoffRpcServer(self.path, self.service.dispatch, allowed_uid=os.getuid())
        try:
            with self.assertRaises(RuntimeError):
                other.open()
            self.assertEqual(self.client.request(self.frame(source()))['status'], 200)
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
