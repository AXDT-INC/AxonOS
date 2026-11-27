"""Exercise the public noVNC listener against the real Flask conversation routes."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
from queue import Queue
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from axonos_gate.tests import test_intelligence as fixtures


@unittest.skipUnless(importlib.util.find_spec('websockify'), 'requires the noVNC runtime')
class ConversationProxyTests(fixtures.GateIntegrationCase):
    def setUp(self):
        super().setUp()
        import intelligence_store
        def storage(wallet, operation, **data):
            return intelligence_store.dispatch(self.root, operation, data)

        for override in (patch.dict(os.environ, {'AXGT_SESSION_LAUNCHER_TOKEN': 'synthetic-share-secret'}),
                         patch.object(fixtures.gate, 'conversation_rpc', side_effect=storage)):
            override.start()
            self.addCleanup(override.stop)
        ready = Queue()
        stop_gate = threading.Event()

        def serve_gate():
            import gevent
            from gevent import pywsgi
            from geventwebsocket.handler import WebSocketHandler
            handler = fixtures.gate._deadline_websocket_handler(WebSocketHandler, gevent)
            listener = pywsgi.WSGIServer(('127.0.0.1', 0), fixtures.gate._application,
                                         handler_class=handler, log=None)
            listener.start()
            ready.put(listener.server_port)
            try:
                while not stop_gate.is_set():
                    gevent.sleep(.02)
            finally:
                listener.stop(timeout=2)

        server_thread = threading.Thread(target=serve_gate, daemon=True)
        server_thread.start()
        self.addCleanup(server_thread.join, 3)
        self.addCleanup(stop_gate.set)
        gate_port = ready.get(timeout=5)
        with socket.socket() as port:
            port.bind(('127.0.0.1', 0))
            self.port = port.getsockname()[1]
        env = dict(os.environ, WEBSOCKIFY_PORT=str(self.port), WEBSOCKIFY_HOST='127.0.0.1',
                   GATE_PORT=str(gate_port), AXGT_CHALLENGE_DB_URL='')
        self.log_path = self.root / 'proxy.log'
        log = self.log_path.open('wb')
        self.addCleanup(log.close)
        proxy = subprocess.Popen([sys.executable, str(Path(fixtures.gate.__file__).with_name('websockify_gate.py'))],
                                 env=env, stdout=log, stderr=log)
        self.addCleanup(proxy.wait, 10)
        self.addCleanup(proxy.terminate)
        for _ in range(100):
            if proxy.poll() is not None:
                self.fail('noVNC listener stopped during startup')
            try:
                with socket.create_connection(('127.0.0.1', self.port), .1):
                    break
            except OSError:
                time.sleep(.05)
        else:
            self.fail('noVNC listener failed to start')

    def exchange(self, method, target, data=None, *, owner=True):
        headers = {'Content-Type': 'application/json'}
        if owner:
            headers.update({'Cookie': fixtures.gate._auth_cookie_name() + '=alice',
                            'X-Wallet-Address': fixtures.ALICE})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request(method, target, json.dumps(data) if data is not None else None, headers)
            reply = conn.getresponse()
            self.assertIn('no-store', reply.getheader('Cache-Control', ''))
            self.assertEqual(len(reply.headers.get_all('Cache-Control')), 1)
            body = reply.read()
            if reply.headers.get_content_type() == 'application/json':
                body = json.loads(body)
            return reply.status, body, reply.headers
        finally:
            conn.close()

    def test_partial_response_reaches_browser_before_stop_at_other_connection(self):
        row = self.exchange('POST', '/api/intelligence/conversations', {})[1]
        item = '/api/intelligence/conversations/' + row['id']
        packet = {'prompt': 'count', 'revision': 0, 'request_id': str(uuid4())}
        self.server.pause = True
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('POST', item + '/messages', json.dumps(packet), {
            'Content-Type': 'application/json', 'Accept': 'application/x-ndjson',
            'Cookie': fixtures.gate._auth_cookie_name() + '=alice', 'X-Wallet-Address': fixtures.ALICE})
        with conn.getresponse() as response:
            self.assertEqual(response.headers.get_content_type(), 'application/x-ndjson')
            self.assertEqual(response.headers['X-Accel-Buffering'], 'no')
            self.assertNotIn('Content-Length', response.headers)
            self.assertEqual(json.loads(response.readline()), {'type': 'start'})
            self.assertEqual(json.loads(response.readline()), {'type': 'delta', 'text': 'Early Δ'})
            # The fixture refuses to finish until a separate HTTP request stops it.
            stop = '/api/intelligence/generations/' + packet['request_id'] + '/stop'
            self.assertEqual(self.exchange('POST', stop, {})[0], 200)
            self.assertEqual(json.loads(response.readline())['finish_reason'], 'cancelled')
            self.assertEqual(response.read(), b'')
        self.assertEqual(self.exchange('GET', item)[1]['messages'][1]['finish_reason'], 'cancelled')

    def test_saved_chat_and_shared_snapshot_through_proxy(self):
        base = '/api/intelligence/conversations'
        self.assertEqual(self.exchange('GET', base, owner=False)[0], 401)
        code, row, _ = self.exchange('POST', base, {})
        self.assertEqual(code, 201)
        item = base + '/' + row['id']
        self.assertEqual(self.exchange('GET', '/intelligence?chat=' + row['id'])[0], 200)
        self.assertEqual(self.exchange('POST', item + '/messages', {
            'prompt': 'fixture Galaxy', 'revision': 0, 'request_id': str(uuid4())})[0], 200)
        self.assertEqual(self.exchange('PATCH', item, {'title': 'Night sky'})[1]['title'], 'Night sky')
        self.assertEqual(self.exchange('GET', base + '?q=fixture%20Galaxy')[1]['conversations'][0]['id'], row['id'])
        self.assertEqual(len(self.exchange('GET', item)[1]['messages']), 2)
        share = self.exchange('POST', item + '/share', {})[1]['url']
        code, html, headers = self.exchange('GET', share, owner=False)
        self.assertEqual(code, 200)
        self.assertIn(b'fixture Galaxy', html)
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertIn('noindex', headers['X-Robots-Tag'])
        self.assertEqual(self.exchange('DELETE', item + '/share')[0], 200)
        self.assertEqual(self.exchange('GET', share, owner=False)[0], 404)
        self.assertEqual(self.exchange('DELETE', item)[0], 200)
        self.assertEqual(self.exchange('GET', item)[0], 404)
        self.assertNotIn(share.encode(), self.log_path.read_bytes())


if __name__ == '__main__':
    unittest.main()
