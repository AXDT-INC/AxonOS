"""Exercise the public noVNC listener against the real Flask conversation routes."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from werkzeug.serving import make_server, WSGIRequestHandler
from axonos_gate.tests import test_intelligence as fixtures


class QuietHandler(WSGIRequestHandler):
    def log(self, *args):
        pass


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
        gate = make_server('127.0.0.1', 0, fixtures.gate.app, request_handler=QuietHandler)
        server_thread = threading.Thread(target=gate.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(gate.server_close)
        self.addCleanup(server_thread.join)
        self.addCleanup(gate.shutdown)
        with socket.socket() as port:
            port.bind(('127.0.0.1', 0))
            self.port = port.getsockname()[1]
        env = dict(os.environ, WEBSOCKIFY_PORT=str(self.port), WEBSOCKIFY_HOST='127.0.0.1',
                   GATE_PORT=str(gate.server_port), AXGT_CHALLENGE_DB_URL='')
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
