"""CPU-only gate integration with disposable wallet records and an HTTP Unix socket."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gate_server as gate

API = '/api/intelligence/generate'
ALICE, BOB = ('0x' + letter * 40 for letter in ('a', 'b'))
GUEST = '0x6775657374' + '0' * 30


def auth_connection():
    records = {'alice': ALICE, 'bob': BOB, 'guest': GUEST, 'expired': ALICE}
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value

    def select(query, args):
        token = args[0]
        owner = records.get(token)
        row = ('current', time.time() + (-10 if token == 'expired' else 1000), 0)
        if len(args) == 1:
            cur.fetchone.return_value = (owner, *row) if owner else None
        else:
            cur.fetchone.return_value = row if owner == args[1] else None

    cur.execute.side_effect = select
    return conn


class MockInference(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((self.path, dict(self.headers), data))
        prompt = data['prompt']
        if self.server.barrier:
            self.server.barrier.wait(timeout=5)
        status = self.server.status
        payload = ({'text': 'Reply to ' + prompt, 'input_tokens': 7, 'output_tokens': 1}
                   if status == 200 else {'error': 'private upstream diagnostic'})
        body = self.server.bad_reply or json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / 'inference.sock')
        self.server = MockInference(path, Handler)
        self.server.calls = []
        self.server.status = 200
        self.server.bad_reply = None
        self.server.barrier = None
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        for ctx in (patch.object(gate.intelligence, 'SOCKET_PATH', path),
                    patch.object(gate, '_gate_pg_init_once', return_value=True),
                    patch.object(gate, '_gate_pg_get_connection', side_effect=auth_connection)):
            ctx.start()
            self.addCleanup(ctx.stop)

    def client(self, token):
        client = gate.app.test_client()
        client.set_cookie(gate._auth_cookie_name(), token)
        return client

    def test_page_is_public_and_chat_requires_wallet_cookie(self):
        for token in ('', 'expired', 'guest'):
            response = self.client(token).get('/intelligence')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'href="/?view=intelligence"', response.data)
            self.assertNotIn(b'<textarea', response.data)
            self.assertNotIn(b'src="/app/intelligence.js"', response.data)
            self.assertIn('no-store', response.headers['Cache-Control'])
            for link in (b'https://github.com/AXDT-INC/AxonIntelligence', b'https://huggingface.co/AxonDAO/AxonAI-v1-27B'):
                self.assertIn(link, response.data)
        response = self.client('alice').get('/intelligence')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<textarea', response.data)
        self.assertIn(f'data-wallet="{ALICE}"'.encode(), response.data)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertFalse(self.server.calls)

    def test_public_introduction_when_identity_store_unavailable(self):
        with patch.object(gate, '_gate_pg_init_once', return_value=False):
            response = self.client('alice').get('/intelligence')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Wallet verification is temporarily unavailable', response.data)
        self.assertNotIn(b'<textarea', response.data)
        self.assertFalse(self.server.calls)

    def test_unauthorized_requests_never_reach_socket(self):
        for token, wallet in (('', ALICE), ('alice', BOB), ('expired', ALICE), ('guest', GUEST)):
            with self.subTest(token=token):
                response = self.client(token).post(API, headers={'X-Wallet-Address': wallet}, json={'prompt': 'hi'})
                self.assertEqual(response.status_code, 401)
        response = self.client('alice').post(API, headers={'X-Wallet-Address': ALICE, 'Sec-Fetch-Site': 'cross-site'},
                                             json={'prompt': 'hi'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.server.calls)

    def test_concurrent_wallets_only_receive_their_own_replies(self):
        self.server.barrier = threading.Barrier(2)

        def send(token, wallet, prompt):
            reply = self.client(token).post(API, headers={'X-Wallet-Address': wallet},
                                           json={'prompt': prompt, 'max_new_tokens': 3})
            self.assertEqual(reply.status_code, 200)
            self.assertIn('no-store', reply.headers['Cache-Control'])
            self.assertEqual(reply.json, {'text': 'Reply to ' + prompt, 'input_tokens': 7, 'output_tokens': 1})

        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(send, 'alice', ALICE, 'first'), pool.submit(send, 'bob', BOB, 'second')]
            for future in futures:
                future.result()
        self.assertEqual(len(self.server.calls), 2)
        for path, headers, data in self.server.calls:
            self.assertEqual(path, '/generate')
            self.assertTrue({'Cookie', 'X-Wallet-Address', 'X-AXGT-Auth-Token'}.isdisjoint(headers))
            self.assertEqual(set(data), {'prompt', 'max_new_tokens'})

    def test_input_and_upstream_errors(self):
        client = self.client('alice')
        headers = {'X-Wallet-Address': ALICE}
        for payload in ({'prompt': ' '}, {'prompt': 'hi', 'max_new_tokens': True},
                        {'prompt': 'hi', 'max_new_tokens': 257}, {'prompt': 'hi', 'wallet': BOB},
                        {'prompt': 'hi', 'system': 'client instruction'}):
            self.assertEqual(client.post(API, headers=headers, json=payload).status_code, 400)
        self.assertEqual(client.post(API, headers=headers, json={'prompt': 'x' * 65536}).status_code, 413)
        self.assertEqual(client.post(API, headers=headers, data='hi').status_code, 415)
        self.assertFalse(self.server.calls)
        for upstream, expected in ((400, 400), (429, 429), (503, 503), (500, 502)):
            self.server.status = upstream
            response = client.post(API, headers=headers, json={'prompt': 'hello'})
            self.assertEqual(response.status_code, expected)
            self.assertNotIn(b'private upstream diagnostic', response.data)
        self.server.status = 200
        self.server.bad_reply = b'{"unexpected":true}'
        self.assertEqual(client.post(API, headers=headers, json={'prompt': 'hi'}).status_code, 502)
        with patch.object(gate.intelligence, 'SOCKET_PATH', str(Path(self.server.server_address).with_name('absent'))):
            self.assertEqual(client.post(API, headers=headers, json={'prompt': 'hi'}).status_code, 503)
