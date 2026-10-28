"""Cross-repository source TLS -> real PG/F2/outbox -> mocked X HTTP.

Only disposable PG15 fixture DSNs are accepted. Socket substitution and all
synthetic credentials live in this test, never in production configuration.
"""
import contextlib
import dataclasses
import http.server
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from axonos_gate import x_capi, x_capi_worker as worker
from axonos_gate import x_capi_handoff as protocol
from axonos_gate.tests import test_x_capi_handoff_authority_postgres as authority
from axonos_gate.tests import test_x_capi_handoff_store as fixtures
from axonos_gate.tests.test_x_capi_transport import _RecordingHTTPSConnection

LANDING = os.environ.get('X_CAPI_TEST_LANDING_REPO')
NODE = os.environ.get('X_CAPI_TEST_NODE', 'node')


@unittest.skipUnless(fixtures.DSN and fixtures.CONFIRMED and LANDING,
                     'requires disposable PG15 and explicit landing source checkout')
class LiveIntegration(unittest.TestCase):
    setUpClass = authority.ImportedAuthorityPostgresTests.__dict__['setUpClass']
    tearDownClass = authority.ImportedAuthorityPostgresTests.__dict__['tearDownClass']
    setUp = authority.ImportedAuthorityPostgresTests.setUp
    tearDown = authority.ImportedAuthorityPostgresTests.tearDown
    source_req = authority.ImportedAuthorityPostgresTests.source_req
    prepare = authority.ImportedAuthorityPostgresTests.prepare
    begin = authority.ImportedAuthorityPostgresTests.begin
    database_now = authority.ImportedAuthorityPostgresTests.database_now
    envelope = authority.ImportedAuthorityPostgresTests.envelope
    ingest = authority.ImportedAuthorityPostgresTests.ingest
    real_rpc = authority.ImportedAuthorityPostgresTests.real_rpc

    def approved(self):
        self.cfg = dataclasses.replace(self.cfg, mode='live', errors=())
        self.cfg_patch.stop()
        self.cfg_patch = patch.object(x_capi, 'load_config', return_value=self.cfg)
        self.cfg_patch.start(); self.addCleanup(self.cfg_patch.stop)
        with self.admin.cursor() as cur:
            cur.execute("UPDATE x_capi_config_guard SET mode_scope='live'")
            cur.execute('TRUNCATE x_capi_worker_state')
        self.prepare(); _, begun = self.begin(); self.conn.commit()
        self.landing_call('capture', owner_hash=self.source,
            observation_id=str(__import__('uuid').uuid4()), observed_at_ms=self.now-1000,
            original_expires_at_ms=self.now-1000+fixtures.SOURCE_WINDOW_MS,
            landing_consented_at_ms=self.now, policy='landing-x-v2', gpc=False,
            twclid='synthetic_click_Aa19-Zz72')
        self.landing_call('authorize', owner_hash=self.source, handoff=begun['handoff'])
        return begun['handoff']

    def landing_call(self, operation, **fields):
        body = self.source_req(operation, **fields)
        runtime = self.runtime
        with tempfile.TemporaryDirectory(prefix='capi-live-tls-') as directory, self.real_rpc() as (client, _):
            root = Path(directory)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', str(root/'key'), '-out', str(root/'cert'), '-days', '1',
                '-subj', '/CN=app.axonos.io', '-addext', 'subjectAltName=DNS:app.axonos.io'],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_POST(self):
                    raw = self.rfile.read(int(self.headers['Content-Length']))
                    self.server.seen += 1
                    result = client.request(dict(kind='source', raw_body=raw.decode('ascii'),
                        kid=self.headers['X-AxonOS-Handoff-Key-Id'], mac=self.headers['X-AxonOS-Handoff-MAC']))
                    response = protocol.canonical_json(result['body'])
                    self.send_response(result['status'])
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(response)))
                    for key, value in result['headers'].items(): self.send_header(key, value)
                    self.end_headers(); self.wfile.write(response)
            server = http.server.HTTPServer(('127.0.0.1', 0), Handler); server.seen = 0
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(root/'cert', root/'key')
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                output = subprocess.run([NODE, str(Path(LANDING)/'tests/handoff/source-probe.mjs')],
                    input=json.dumps(dict(disposable='SYNTHETIC_LOOPBACK_ONLY', body=body,
                        key=runtime.keyring.keys['synthetic'].hex(), port=server.server_port,
                        ca=(root/'cert').read_text())), text=True, capture_output=True, timeout=10,
                    env={'PATH': os.environ.get('PATH', '')})
                self.assertEqual(output.returncode, 0, output.stderr)
                self.assertEqual(server.seen, 1)
                return json.loads(output.stdout)
            finally:
                server.shutdown(); server.server_close(); thread.join(3)

    def dispatch(self, status=200, headers=(), fail=False):
        raw_body = b'{"data":{"conversions_processed":1}}'
        raw = (f'HTTP/1.1 {status} Synthetic\r\nContent-Length: {len(raw_body)}\r\n'.encode()
               + b''.join(f'{k}: {v}\r\n'.encode() for k,v in headers) + b'\r\n' + raw_body)
        wires = []
        token = secrets.token_urlsafe(32)
        class Transport:
            def send(self, pixel, supplied, payload):
                env = {k:v for k,v in os.environ.items() if not k.startswith('X_CAPI_ALLOW_TEST_')}
                env['X_CAPI_PRODUCTION_TRANSPORT'] = 'pixel-token-v1'
                def connection(*args, **kwargs):
                    if fail: raise TimeoutError('synthetic timeout')
                    conn = _RecordingHTTPSConnection(*args, raw_response=raw, **kwargs)
                    wires.append(conn)
                    return conn
                with patch.dict(os.environ, env, clear=True), patch.object(worker.http.client,
                        'HTTPSConnection', side_effect=connection):
                    return worker.RequestsTransport().send(pixel, supplied, payload)
        with self.real_rpc() as (_, fence), contextlib.ExitStack() as stack:
            challenge = self.landing_call('lease_challenge')
            self.landing_call('lease_attest', worker_boot=challenge['worker_boot'],
                challenge=challenge['challenge'], control_version=0, ready=True)
            stack.enter_context(patch.object(x_capi, '_db_url', return_value='synthetic'))
            stack.enter_context(patch.object(x_capi, 'get_connection', side_effect=lambda **_: self.pg.connect(**self.worker_dsn)))
            stack.enter_context(patch.object(worker, '_worker_db_target_is_isolated', return_value=True))
            stack.enter_context(patch.object(worker, 'read_token', return_value=(token, None)))
            outcome = worker.run_once(Transport(), privacy_fence=fence, now_fn=lambda:self.now/1000,
                                      perform_cleanup=False)
        return outcome, [b''.join(c.recording_socket.writes) for c in wires]

    def test_consented_owned_deposit_retry_ack_and_withdrawal(self):
        self.assertEqual(self.ingest(self.envelope()), 'bound')
        self.now += 1
        event = self.envelope('event', milestone='deposit_completed')
        self.assertEqual(self.ingest(event), 'pending')
        self.assertEqual(self.ingest(event), 'duplicate')
        self.assertEqual(self.dispatch(fail=True)[0], 'retry')
        with self.admin.cursor() as cur:
            cur.execute('UPDATE x_capi_outbox SET next_attempt_at=0')
        outcome, first = self.dispatch(429, [('Retry-After', '7200')])
        self.assertEqual(outcome, 'retry')
        self.assertEqual(self.dispatch()[0], 'paused')
        with self.admin.cursor() as cur:
            cur.execute('UPDATE x_capi_worker_state SET paused_at=0')
            cur.execute('UPDATE x_capi_outbox SET next_attempt_at=0')
        outcome, second = self.dispatch()
        self.assertEqual(outcome, 'accepted')
        self.assertEqual(first[0].split(b'\r\n\r\n')[1], second[0].split(b'\r\n\r\n')[1])
        self.assertEqual(self.dispatch()[0], 'idle')
        with self.conn.cursor() as cur:
            cur.execute('SELECT status,twclid FROM x_capi_outbox')
            self.assertEqual(cur.fetchall(), [('accepted', None)])
        self.conn.commit()
        self.now += 1
        self.assertEqual(self.ingest(self.envelope('event', milestone='session_started')), 'pending')
        self.landing_call('revoke', owner_hash=self.source, reason='withdrawn')
        self.assertEqual(self.dispatch()[0], 'idle')
