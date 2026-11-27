"""CPU-only gate integration with disposable wallet records and an HTTP Unix socket."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gate_server as gate

API = '/api/intelligence/generate'
CONVERSATIONS = '/api/intelligence/conversations'
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

    def write_event(self, event):
        self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
        self.wfile.flush()

    def serve_stream(self, data):
        key = data["request_id"]
        self.server.streams[key] = cancelled = threading.Event()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.write_event({"type": "start"})
            self.write_event({"type": "delta", "text": self.server.first_text})
            if self.server.interrupt:
                return
            if self.server.pause:
                if not cancelled.wait(timeout=5):
                    raise RuntimeError("stream cancellation timed out")
            reason = "cancelled" if cancelled.is_set() else self.server.reason
            self.write_event({"type": "done", "text": self.server.first_text, "finish_reason": reason,
                              "input_tokens": 4, "output_tokens": 2})
        finally:
            del self.server.streams[key]

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((self.path, dict(self.headers), data))
        if self.path.startswith('/generations/') and self.path.endswith('/stop'):
            cancelled = self.server.streams.get(self.path.split('/')[2])
            if cancelled:
                cancelled.set()
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')
            return
        if self.path == '/generate/stream':
            return self.serve_stream(data)
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


class GateIntegrationCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        path = str(Path(directory.name) / 'inference.sock')
        self.server = MockInference(path, Handler)
        self.server.calls = []
        self.server.status = 200
        self.server.bad_reply = None
        self.server.barrier = None
        self.server.streams = {}
        self.server.pause = self.server.interrupt = False
        self.server.reason = 'stop'
        self.server.first_text = 'Early Δ'
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        for ctx in (patch.dict(os.environ, {'AXONOS_INTELLIGENCE_PUBLIC_ACCESS': 'true'}),
                    patch.object(gate.intelligence, 'SOCKET_PATH', path),
                    patch.object(gate, '_gate_pg_init_once', return_value=True),
                    patch.object(gate, '_gate_pg_get_connection', side_effect=auth_connection)):
            ctx.start()
            self.addCleanup(ctx.stop)

    def client(self, token):
        client = gate.app.test_client()
        client.set_cookie(gate._auth_cookie_name(), token)
        return client


class IntelligenceTests(GateIntegrationCase):
    def test_restricted_routes_use_verified_wallet_membership_before_access(self):
        policies = {'AXONOS_INTELLIGENCE_PUBLIC_ACCESS': 'false', 'AXONOS_TEST_CREDIT_WALLETS': ALICE.upper(),
                    'AXONOS_TEST_CREDITS_ENABLED': 'false'}
        api_paths = [('GET', CONVERSATIONS), ('POST', CONVERSATIONS), ('POST', API),
                     ('POST', '/api/intelligence/generations/' + str(uuid4()) + '/stop')]
        item = CONVERSATIONS + '/' + '1' * 32
        api_paths += [(method, item + suffix) for method, suffix in
                      [('GET', ''), ('PATCH', ''), ('DELETE', ''), ('POST', '/messages'),
                       ('POST', '/share'), ('DELETE', '/share')]]
        with patch.dict(os.environ, policies), patch.object(gate, 'conversation_rpc') as store:
            self.assertEqual(self.client('alice').get('/intelligence').status_code, 200)
            for token, wallet, status in [('bob', BOB, 403), ('expired', ALICE, 401), ('guest', GUEST, 401)]:
                client = self.client(token)
                for path in ('/intelligence', '/intelligence/share/invalid'):
                    response = client.get(path)
                    self.assertEqual(response.status_code, status)
                    self.assertNotIn(b'id="chat-main"', response.data)
                for method, path in api_paths:
                    self.assertEqual(client.open(path, method=method, json={}, headers={'X-Wallet-Address': wallet}).status_code, status)
            self.assertEqual(self.client('bob').get(CONVERSATIONS, headers={'X-Wallet-Address': ALICE}).status_code, 401)
            self.assertFalse(self.server.calls or store.called)
            self.assertEqual(self.client('alice').get('/intelligence/share/invalid').status_code, 404)
            with patch.dict(os.environ, {'AXONOS_INTELLIGENCE_PUBLIC_ACCESS': 'invalid', 'AXONOS_TEST_CREDIT_WALLETS': '',
                                        'AXONOS_WHITELISTED_WALLETS': BOB}):
                self.assertEqual(self.client('alice').get('/intelligence').status_code, 403)
                self.assertEqual(self.client('bob').get('/intelligence').status_code, 200)

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


class ConversationTests(GateIntegrationCase):
    """Use fresh wallet directories, the actual gate, store, and inference protocol."""

    def setUp(self):
        super().setUp()
        import intelligence_store

        for wallet in (ALICE, BOB):
            (self.root / wallet).mkdir()

        def storage(wallet, operation, **data):
            return intelligence_store.dispatch(self.root / wallet, operation, data)

        rpc_patch = patch.object(gate, 'conversation_rpc', side_effect=storage)
        self.rpc = rpc_patch.start()
        self.addCleanup(rpc_patch.stop)
        token_patch = patch.dict(os.environ, {'AXGT_SESSION_LAUNCHER_TOKEN': 'test-share-key-' + 'x' * 40})
        token_patch.start()
        self.addCleanup(token_patch.stop)

    def request(self, method, path='', *, token='alice', wallet=ALICE, **kwargs):
        return self.client(token).open(CONVERSATIONS + path, method=method,
                                       headers={'X-Wallet-Address': wallet}, **kwargs)

    def create(self):
        response = self.request('POST', json={})
        self.assertIn(response.status_code, (200, 201), response.data)
        self.assertEqual(response.json['messages'], [])
        return response.json

    def turn_payload(self, conversation, prompt):
        return dict(prompt=prompt, revision=conversation['revision'], request_id=str(uuid4()))

    def send(self, conversation, prompt):
        response = self.request('POST', '/' + conversation['id'] + '/messages',
                                json=self.turn_payload(conversation, prompt))
        self.assertEqual(response.status_code, 200, response.data)
        return response.json

    def test_history_survives_reopening_and_metadata_changes(self):
        first = self.create()
        original = self.send(first, 'Measure the nebula')
        path = '/' + original['id']
        renamed = self.request('PATCH', path, json={'title': 'Telescope notes'})
        self.assertEqual(renamed.status_code, 200, renamed.data)
        self.assertEqual(renamed.json['messages'], original['messages'])
        another = self.create()
        listing = self.request('GET')
        summaries = listing.json['conversations']
        self.assertEqual({row['id'] for row in summaries}, {original['id'], another['id']})
        self.assertTrue(all('messages' not in row for row in summaries))
        matches = self.request('GET', query_string={'q': 'TELESCOPE'}).json['conversations']
        self.assertEqual([row['id'] for row in matches], [original['id']])
        matches = self.request('GET', query_string={'q': 'nebula'}).json['conversations']
        self.assertEqual([row['id'] for row in matches], [original['id']])
        reply = self.send(renamed.json, 'Explain the result')
        first_prompt, followup_prompt = [call[2]['prompt'] for call in self.server.calls]
        self.assertEqual(first_prompt, 'Measure the nebula')
        self.assertEqual(followup_prompt, 'User: Measure the nebula\n\nAxonAI: Reply to Measure the nebula\n\nUser: Explain the result')
        self.assertEqual([m['role'] for m in reply['messages']], ['user', 'assistant', 'user', 'assistant'])
        opened = self.request('GET', path)
        self.assertEqual(opened.json, reply)
        self.assertIn('no-store', opened.headers['Cache-Control'])
        self.assertIn(self.request('DELETE', path).status_code, (200, 204))
        self.assertEqual(self.request('GET', path).status_code, 404)
        self.assertEqual([row['id'] for row in self.request('GET').json['conversations']], [another['id']])

    def test_verified_wallet_isolates_every_private_operation(self):
        row = self.send(self.create(), 'Alice private question')
        path = '/' + row['id']
        bob = dict(token='bob', wallet=BOB)
        self.assertEqual(self.request('GET', **bob).json['conversations'], [])
        for method, suffix, payload in (
            ('GET', '', None), ('PATCH', '', {'title': 'new title'}), ('DELETE', '', None),
            ('POST', '/messages', self.turn_payload(row, 'follow up')),
            ('POST', '/share', {}), ('DELETE', '/share', None),
        ):
            with self.subTest(method=method, suffix=suffix):
                result = self.request(method, path + suffix, json=payload, **bob)
                self.assertEqual(result.status_code, 404, result.data)
                self.assertNotIn(b'Alice private question', result.data)
        self.assertEqual(self.request('GET', path).json, row)
        self.assertEqual(len(self.server.calls), 1)
        self.rpc.reset_mock()
        for token, wallet in (('', ALICE), ('expired', ALICE), ('guest', GUEST), ('alice', BOB)):
            with self.subTest(token=token):
                result = self.request('GET', token=token, wallet=wallet)
                self.assertEqual(result.status_code, 401, result.data)
        result = self.client('alice').post(CONVERSATIONS + path + '/share', json={},
                    headers={'X-Wallet-Address': ALICE, 'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(result.status_code, 403)
        self.rpc.assert_not_called()

    def test_public_snapshot_survives_later_turns_and_revokes_immediately(self):
        row = self.send(self.create(), 'Visible at sharing time')
        path = '/' + row['id']
        self.assertFalse(self.request('GET').json['conversations'][0]['shared'])
        issued = self.request('POST', path + '/share', json={})
        self.assertEqual(issued.status_code, 200, issued.data)
        url = issued.json['url']
        self.assertTrue(url.startswith('/intelligence/share/'))
        self.assertNotIn(ALICE, url)
        self.assertTrue(self.request('GET').json['conversations'][0]['shared'])
        self.send(self.request('GET', path).json, 'Private followup')
        anonymous = self.client('')
        snapshot = anonymous.get(url)
        self.assertEqual(snapshot.status_code, 200)
        self.assertIn(b'Visible at sharing time', snapshot.data)
        self.assertNotIn(b'Private followup', snapshot.data)
        self.assertNotIn(ALICE.encode(), snapshot.data)
        self.assertNotIn(b'<textarea', snapshot.data)
        self.assertIn('no-store', snapshot.headers['Cache-Control'])
        self.assertEqual(snapshot.headers.get('Referrer-Policy'), 'no-referrer')
        self.assertIn(self.request('DELETE', path + '/share').status_code, (200, 204))
        self.assertEqual(anonymous.get(url).status_code, 404)
        new_url = self.request('POST', path + '/share', json={}).json['url']
        self.assertNotEqual(new_url, url)
        self.assertEqual(anonymous.get(url).status_code, 404)
        self.assertIn(b'Private followup', anonymous.get(new_url).data)
        self.assertIn(self.request('DELETE', path).status_code, (200, 204))
        self.assertEqual(anonymous.get(new_url).status_code, 404)

    def test_concurrent_turns_retry_safely(self):
        row = self.create()
        path = '/' + row['id'] + '/messages'
        attempts = [self.turn_payload(row, prompt) for prompt in ('One', 'Two')]
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.request, 'POST', path, json=turn) for turn in attempts]
            replies = [future.result() for future in futures]
        self.assertEqual(sorted(r.status_code for r in replies), [200, 409])
        winner = next(i for i, response in enumerate(replies) if response.status_code == 200)
        committed = replies[winner].json
        self.assertEqual(len(committed['messages']), 2)
        self.assertEqual(committed['messages'][0]['content'], attempts[winner]['prompt'])
        calls = len(self.server.calls)
        self.assertEqual(self.request('POST', path, json=attempts[winner]).json, committed)
        changed = dict(attempts[winner], prompt='different')
        self.assertEqual(self.request('POST', path, json=changed).status_code, 409)
        self.assertEqual(len(self.server.calls), calls)
        self.server.status = 503
        failed_turn = self.turn_payload(committed, 'Try again soon')
        self.assertEqual(self.request('POST', path, json=failed_turn).status_code, 503)
        self.assertEqual(self.request('GET', '/' + row['id']).json, committed)
        self.server.status = 200
        recovery = self.request('POST', path, json=failed_turn)
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(len(recovery.json['messages']), 4)

    def stream_turn(self, conversation, data):
        return self.client('alice').post(CONVERSATIONS + '/' + conversation['id'] + '/messages',
                                         headers={'X-Wallet-Address': ALICE, 'Accept': gate.intelligence.NDJSON},
                                         json=data, buffered=False)

    def test_live_stop_is_wallet_scoped_and_the_partial_turn_is_saved_once(self):
        row = self.create()
        packet = self.turn_payload(row, 'observe')
        self.server.pause = True
        response = self.stream_turn(row, packet)
        self.addCleanup(response.close)
        events = iter(response.response)
        self.assertEqual(json.loads(next(events)), {'type': 'start'})
        self.assertEqual(json.loads(next(events)), {'type': 'delta', 'text': 'Early Δ'})
        url = '/api/intelligence/generations/' + packet['request_id'] + '/stop'
        worker_id, cancelled = next(iter(self.server.streams.items()))
        self.assertNotEqual(worker_id, packet['request_id'])
        self.assertEqual(self.client('alice').post(url, json={}, headers={'X-Wallet-Address': BOB}).status_code, 401)
        self.assertEqual(self.client('bob').post(url, json={}, headers={'X-Wallet-Address': BOB}).status_code, 200)
        self.assertFalse(cancelled.is_set())
        self.assertEqual(self.client('alice').post(url, json={}, headers={'X-Wallet-Address': ALICE}).status_code, 200)
        done, = map(json.loads, events)
        self.assertEqual(done['type'], 'done')
        self.assertEqual(done['finish_reason'], 'cancelled')
        answer = {'role': 'assistant', 'content': 'Early Δ', 'finish_reason': 'cancelled'}
        self.assertEqual(done['conversation']['messages'][1], answer)
        self.assertEqual(self.request('GET', '/' + row['id']).json, done['conversation'])
        count = len(self.server.calls)
        replay = self.stream_turn(row, packet)
        self.assertEqual(json.loads(replay.data), done)
        self.assertEqual(len(self.server.calls), count)
        for path, headers, data in self.server.calls:
            self.assertTrue({'Cookie', 'X-Wallet-Address', 'X-AXGT-Auth-Token'}.isdisjoint(headers))
        self.assertNotIn('max_new_tokens', self.server.calls[0][2])

    def test_missing_terminal_frame_allows_retry_and_length_survives_reopen(self):
        row = self.create()
        packet = self.turn_payload(row, 'count')
        self.server.interrupt = True
        events = [json.loads(frame) for frame in self.stream_turn(row, packet).response]
        self.assertEqual([event['type'] for event in events], ['start', 'delta', 'error'])
        self.assertEqual(events[-1]['status'], 502)
        self.assertEqual(self.request('GET', '/' + row['id']).json['revision'], 0)
        self.server.reason = 'length'
        self.server.interrupt = False
        done = json.loads(self.stream_turn(row, packet).data.splitlines()[-1])
        self.assertEqual(done['finish_reason'], 'length')
        self.assertEqual(self.request('GET', '/' + row['id']).json['messages'][1]['finish_reason'], 'length')

    def test_existing_volume_adopts_metadata_and_keeps_long_answers(self):
        legacy = self.send(self.create(), 'A previous chat')
        path = '/' + legacy['id']
        # Reproduce the on-volume schema an owner had before streaming.
        db = sqlite3.connect(self.root / ALICE / '.axonos-intelligence/history.sqlite3')
        try:
            db.execute('DROP TABLE turn_completions')
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.request('GET', path).json, legacy)
        self.server.reason = 'length'
        self.server.first_text = 'persistent result ' * 4096
        self.assertGreater(len(self.server.first_text), 65536)
        packet = self.turn_payload(legacy, 'another result')
        done = json.loads(self.stream_turn(legacy, packet).data.splitlines()[-1])
        self.assertEqual(done['text'], self.server.first_text)
        self.assertEqual(self.request('GET', path).json['messages'][-1],
                         {'role': 'assistant', 'content': self.server.first_text, 'finish_reason': 'length'})

    def test_temporary_stream_uses_same_protocol_without_wallet_storage(self):
        response = self.client('alice').post(API, json={'prompt': 'count', 'request_id': str(uuid4())},
                              headers={'X-Wallet-Address': ALICE, 'Accept': gate.intelligence.NDJSON})
        events = [json.loads(frame) for frame in response.data.splitlines()]
        self.assertEqual(events[-1], {'type': 'done', 'text': 'Early Δ', 'finish_reason': 'stop'})
        self.rpc.assert_not_called()

    def test_delete_wins_over_a_reply_still_in_flight(self):
        row = self.create()
        path = '/' + row['id']
        started, resume = threading.Event(), threading.Event()
        generate = gate.intelligence.generate

        def slow_reply(packet):
            started.set()
            self.assertTrue(resume.wait(timeout=5))
            return generate(packet)

        with patch.object(gate.intelligence, 'generate', side_effect=slow_reply), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.request, 'POST', path + '/messages',
                                  json=self.turn_payload(row, 'Wait for the answer'))
            try:
                self.assertTrue(started.wait(timeout=5))
                self.assertIn(self.request('DELETE', path).status_code, (200, 204))
            finally:
                resume.set()
            self.assertEqual(pending.result().status_code, 404)
        self.assertEqual(self.request('GET').json['conversations'], [])
