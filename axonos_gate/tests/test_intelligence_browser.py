"""Opt-in Chromium checks using the gate template, packaged assets and simulated API replies."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time
from queue import Queue
from threading import Event, Thread
from pathlib import Path
import unittest
from urllib.parse import parse_qs, parse_qsl, urlsplit
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[2]
THEME = ROOT / 'novnc-theme'
BASE = 'https://axonos.test'
WALLET = '0x' + 'a' * 40


@unittest.skipUnless(os.getenv('AXONOS_RUN_BROWSER_TESTS') == '1', 'browser checks not enabled')
class IntelligenceBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        cls.addClassCleanup(pw.stop)
        cls.browser = pw.chromium.launch(headless=True, args=['--disable-gpu'])
        cls.addClassCleanup(cls.browser.close)
        cls.template = Environment(loader=FileSystemLoader(ROOT / 'axonos_gate/templates'),
                                   autoescape=select_autoescape()).get_template('intelligence.html')

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 390, 'height': 844})
        self.addCleanup(self.page.close)
        self.wallet = None
        self.status = 200
        self.answer = ''
        self.finish_reason = 'stop'
        self.stream_host = None
        self.storage = False
        self.chats = {}
        self.snapshot = None
        self.turn_requests = []
        self.fail_history = False
        self.prompts = []
        self.requests = []
        self.page.route('**/*', self.route)
        self.page.add_init_script(f"localStorage.setItem('axonos_last_wallet', '{WALLET}')")

    def route(self, route):
        url = urlsplit(route.request.url)
        self.requests.append(url)
        if self.stream_host and url.netloc == self.stream_host:
            if url.path == '/api/intelligence/generate' or url.path.endswith(('/stop', '/messages')):
                return route.continue_()
        elif url.netloc != 'axonos.test':
            return route.abort()
        if url.path == '/intelligence':
            return route.fulfill(content_type='text/html', body=self.template.render(wallet=self.wallet, request={'args': dict(parse_qsl(url.query))}))
        if url.path == '/intelligence/share/example':
            if self.snapshot is None:
                return route.fulfill(status=404, content_type='text/html',
                                     body=self.template.render(shared_error='This link was revoked.'))
            return route.fulfill(content_type='text/html', body=self.template.render(shared_conversation=self.snapshot))
        if url.path.startswith('/api/intelligence/conversations'):
            return self.conversations(route, url)
        if url.path == '/':
            return route.fulfill(content_type='text/html', body='Workspace sign-in')
        if url.path == '/api/intelligence/generate':
            self.prompts.append(route.request.post_data_json['prompt'])
            return self.reply(route)
        if url.path == '/api/auth/wallet-status':
            return route.fulfill(status=self.status, json={})
        asset = THEME / url.path.lstrip('/')
        if url.path == '/app/styles/axonos-theme.css':
            asset = THEME / 'axonos-theme.css'
        return route.fulfill(path=asset) if asset.is_file() else route.fulfill(status=404, body='')

    def reply(self, route, chat=None):
        self.assertEqual(route.request.headers.get('accept'), 'application/x-ndjson')
        if self.status != 200:
            return route.fulfill(status=self.status, json={'error': 'Try again'})
        done = {'type': 'done', 'text': self.answer, 'finish_reason': self.finish_reason}
        if chat:
            done['conversation'] = chat
        events = [{'type': 'start'}, {'type': 'delta', 'text': self.answer}, done]
        return route.fulfill(content_type='application/x-ndjson', body=''.join(json.dumps(e) + '\n' for e in events))

    def conversations(self, route, url):
        self.assertEqual(route.request.headers.get('x-wallet-address'), self.wallet)
        method = route.request.method
        parts = url.path.strip('/').split('/')[3:]
        if not parts:
            if method == 'GET':
                if self.fail_history:
                    return route.fulfill(status=503, json={'error': 'History unavailable'})
                query = parse_qs(url.query).get('q', [''])[0].lower()
                items = [chat for chat in self.chats.values()
                         if query in (chat['title'] + str(chat['messages'])).lower()]
                return route.fulfill(json={'storage_available': self.storage, 'conversations': items})
            ident = str(len(self.chats) + 1)
            chat = self.chats[ident] = {'id': ident, 'title': 'New chat', 'revision': 0,
                                        'messages': [], 'shared': False, 'share_url': None}
            return route.fulfill(status=201, json=chat)
        ident = parts[0]
        chat = self.chats.get(ident)
        if not chat:
            return route.fulfill(status=404, json={'error': 'Conversation unavailable'})
        if parts[1:] == ['messages']:
            turn = route.request.post_data_json
            self.turn_requests.append(turn)
            if self.status != 200:
                return route.fulfill(status=self.status, json={'error': 'Try again'})
            self.assertEqual(turn['revision'], chat['revision'])
            if not chat['messages']:
                chat['title'] = turn['prompt']
            chat['revision'] += 1
            chat['messages'].extend([{'role': 'user', 'content': turn['prompt']},
                                     {'role': 'assistant', 'content': self.answer, 'finish_reason': self.finish_reason}])
            return self.reply(route, chat)
        if parts[1:] == ['share']:
            if method == 'DELETE':
                self.snapshot = chat['share_url'] = None
                chat['shared'] = False
                return route.fulfill(json={})
            chat['share_url'] = '/intelligence/share/example'
            chat['shared'] = True
            self.snapshot = deepcopy(chat)
            return route.fulfill(json={'url': chat['share_url']})
        if method == 'PATCH':
            chat['title'] = route.request.post_data_json['title']
        if method == 'DELETE':
            del self.chats[ident]
            return route.fulfill(json={})
        return route.fulfill(json=chat)

    def send(self, text, wait=True):
        self.page.locator('#prompt').fill(text)
        self.page.locator('#send').click()
        if wait:
            self.page.locator('#stop').wait_for(state='hidden')

    def open_stream(self, **page_options):
        # The browser receives a real HTTP response while the test controls each
        # chunk, including UTF-8 splits, an early EOF and the final record.
        chunks, cancelled = Queue(), Event()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, body, self.headers.get('X-Wallet-Address')))
                self.send_response(200)
                if self.path.endswith('/stop'):
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{}')
                    cancelled.set()
                    return
                self.send_header('Content-Type', 'application/x-ndjson')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    while True:
                        piece = chunks.get(timeout=15)
                        if piece is None:
                            break
                        self.wfile.write(piece)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(chunks.put, None)
        self.wallet = WALLET
        self.stream_host = '127.0.0.1:' + str(server.server_port)
        if page_options:
            self.page.emulate_media(**page_options)
        self.page.goto('http://' + self.stream_host + '/intelligence')
        return chunks, requests, cancelled

    @staticmethod
    def record(**event):
        return (json.dumps(event, ensure_ascii=False) + '\n').encode()

    def test_generation_waits_streams_and_stops(self):
        chunks, requests, cancelled = self.open_stream()
        self.send('Stream a measurement', wait=False)
        waiting = self.page.locator('#thread .assistant .reply-wait')
        waiting.wait_for()
        self.assertEqual(waiting.locator('[aria-hidden]').first.evaluate('(e) => getComputedStyle(e).animationName'), 'reply-pulse')
        stop = self.page.get_by_role('button', name='Stop generation')
        chunks.put(self.record(type='start'))
        chunks.put(self.record(type='ping'))
        delta = self.record(type='delta', text='**Δ** is ')
        cut = delta.index('Δ'.encode()) + 1
        chunks.put(delta[:cut])
        chunks.put(delta[cut:])
        self.page.locator('.assistant .content strong').get_by_text('Δ').wait_for()
        self.assertFalse(waiting.is_visible())
        self.assertTrue(stop.is_visible())
        with self.page.expect_response(lambda response: response.url.endswith('/stop')):
            stop.click()
        self.page.get_by_text('Stopping generation…').wait_for()
        self.assertTrue(stop.is_disabled())
        self.assertTrue(cancelled.wait(3))
        self.assertEqual(requests[1], ('/api/intelligence/generations/' + requests[0][1]['request_id'] + '/stop', {}, WALLET))
        chunks.put(self.record(type='done', text='**Δ** is ', finish_reason='cancelled'))
        chunks.put(None)
        self.page.get_by_text('Generation stopped.', exact=True).wait_for()
        self.assertEqual(self.page.locator('.assistant .content strong').inner_text(), 'Δ')
        self.assertFalse(stop.is_visible())
        self.assertTrue(self.page.locator('#prompt').is_editable())

    def test_stop_before_admission_with_reduced_motion(self):
        chunks, requests, cancelled = self.open_stream(reduced_motion='reduce')
        self.send('Queued question', wait=False)
        dots = self.page.locator('.reply-wait [aria-hidden]').first
        dots.wait_for()
        self.assertEqual(dots.evaluate('(e) => getComputedStyle(e).animationName'), 'none')
        self.page.get_by_role('button', name='Stop generation').click()
        self.page.get_by_text('Stopping generation…').wait_for()
        self.assertFalse(any(path.endswith('/stop') for path, _, _ in requests))
        with self.page.expect_response(lambda response: response.url.endswith('/stop')):
            chunks.put(self.record(type='start'))
        self.assertTrue(cancelled.wait(3))
        chunks.put(self.record(type='done', text='', finish_reason='cancelled'))
        chunks.put(None)
        self.page.get_by_text('Generation stopped.', exact=True).wait_for()
        self.assertFalse(dots.is_visible())

    def test_scroll_following_and_output_limit(self):
        chunks, _, _ = self.open_stream()
        self.send('Long answer', wait=False)
        chunks.put(self.record(type='start'))
        answer = '\n\n'.join('Observation ' + str(i) for i in range(100))
        chunks.put(self.record(type='delta', text=answer))
        self.page.get_by_text('Observation 99', exact=True).wait_for()
        at_bottom = "() => {const s = document.querySelector('#chat-scroll'); return s.scrollHeight - s.clientHeight - s.scrollTop <= 3}"
        self.page.wait_for_function(at_bottom)
        self.page.locator('#chat-scroll').evaluate('(s) => { s.scrollTop = 48 }')
        latest = self.page.get_by_role('button', name='Jump to latest message')
        latest.wait_for()
        chunks.put(self.record(type='delta', text='\n\nOne more observation'))
        self.page.get_by_text('One more observation', exact=True).wait_for()
        self.assertAlmostEqual(self.page.locator('#chat-scroll').evaluate('s => s.scrollTop'), 48, delta=2)
        latest.click()
        self.page.wait_for_function(at_bottom)
        self.page.locator('#chat-scroll').evaluate('(s) => { s.scrollTop = 75 }')
        latest.wait_for()
        self.page.get_by_text('Observation 3', exact=True).evaluate("""p => {
            const range = document.createRange();
            range.selectNodeContents(p);
            const selection = getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
        }""")
        chunks.put(self.record(type='delta', text='\n\nFinal sentence.'))
        chunks.put(self.record(type='done', finish_reason='length', text=answer + '\n\nOne more observation\n\nFinal sentence.'))
        chunks.put(None)
        self.page.get_by_text('Output limit reached.', exact=False).wait_for()
        self.assertAlmostEqual(self.page.locator('#chat-scroll').evaluate('s => s.scrollTop'), 75, delta=2)
        self.assertEqual(self.page.evaluate('getSelection().toString()'), 'Observation 3')
        latest.click()
        self.page.wait_for_function(at_bottom)

    def test_large_fragmented_saved_completion_replay(self):
        self.storage = True
        chunks, _, _ = self.open_stream()
        self.send('Replay this saved answer', wait=False)
        self.page.wait_for_function("new URLSearchParams(location.search).has('chat')")
        # A valid nearly 1 MiB UTF-8 answer can need six JSON bytes per
        # control character. A replay includes the reply twice in one frame.
        answer = '```text\n' + '\x01' * (1024 * 1024 - 64) + '\n```\n\nReplay complete.'
        chat = self.chats['1']
        chat.update(title='Saved response', revision=1, messages=[
            {'role': 'user', 'content': 'Replay this saved answer'},
            {'role': 'assistant', 'content': answer, 'finish_reason': 'length'},
        ])
        frame = self.record(type='done', finish_reason='length', text=answer, conversation=chat)
        self.assertGreater(len(frame), 11 * 1024 * 1024)
        for start in range(0, len(frame), 64 * 1024):
            chunks.put(frame[start:start + 64 * 1024])
        chunks.put(None)
        self.page.get_by_text('Replay complete.', exact=True).wait_for()
        self.assertEqual(self.page.locator('#chat-title').inner_text(), 'Saved response')
        self.assertIn('Output limit reached', self.page.locator('.reply-note').inner_text())
        self.assertFalse(self.page.locator('#stop').is_visible())

    def test_unfinished_stream_preserves_visible_text_and_retry_prompt(self):
        chunks, _, _ = self.open_stream()
        self.send('A partial answer', wait=False)
        chunks.put(self.record(type='start') + self.record(type='delta', text='Only part of this reply'))
        chunks.put(None)
        self.page.get_by_text('Stream interrupted.', exact=False).wait_for()
        self.assertEqual(self.page.locator('#prompt').input_value(), 'A partial answer')
        self.assertEqual(self.page.locator('.assistant .content').inner_text(), 'Only part of this reply')
        self.assertFalse(self.page.locator('#stop').is_visible())

    def test_consent_controls_direct_and_landing_ad_arrivals(self):
        marker = 'Synthetic_Intelligence_Click'
        actions = []
        binds = []
        candidates = []
        def attribution(route):
            req = route.request
            path = urlsplit(req.url).path
            headers = req.all_headers()
            click = headers.get('x-axonos-landing-click')
            if click:
                candidates.append(click)
            data = dict(ok=True, enabled=True, context='C' * 40, csrf='S' * 40,
                        expires_at=int(time.time()) + 600, attribution_ttl_days=7)
            if path.endswith('/status'):
                data.update(state='unset', landing_click_accepted=click == marker,
                            preferences=dict(enabled=True, app_origin=BASE, ttl_days=7, policy_version='test',
                                             policy_epoch=2, funnel_events=['intelligence_page_viewed']))
            elif path.endswith('/bind'):
                binds.append((req.post_data_json, headers.get('x-axgt-auth-token')))
                data.update(accepted=True)
            else:
                action = req.post_data_json['action']
                actions.append(action)
                data.update(state='granted' if action == 'grant' else 'revoked')
            route.fulfill(status=202 if path.endswith('/consent') else 200, json=data)

        self.page.route('**/api/x-attribution/*', attribution)
        self.page.goto(BASE + '/intelligence?twclid=' + marker)
        self.page.wait_for_function('window.axonosAttributionReady === true')
        self.assertEqual(self.page.url, BASE + '/intelligence')
        self.assertEqual(candidates, [])
        self.assertEqual(self.page.get_by_role('dialog').count(), 1)
        self.assertIsNone(self.page.evaluate('sessionStorage.getItem("axonos_x_attribution_tab_owner_v1")'))
        self.assertEqual(actions, [])
        self.page.get_by_role('button', name='Reject optional').click()
        self.page.get_by_role('button', name='Reject optional').wait_for(state='hidden')
        self.assertEqual(candidates, [])
        self.assertEqual(actions, [])

        # A transferred fragment arriving in a signed-in, consenting browser
        # produces the existing app milestone and uses the authenticated cookie.
        self.page.goto(BASE + '/intelligence')
        self.page.get_by_role('button', name='Privacy settings').click()
        self.page.get_by_role('button', name='Accept optional').click()
        self.page.get_by_role('button', name='Accept optional').wait_for(state='hidden')
        self.wallet = WALLET
        self.page.goto(BASE + '/')
        self.page.goto(BASE + '/intelligence#axonos_x_click=' + marker)
        self.page.wait_for_function('!!window.axonosAttributionContext')
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.url, BASE + '/intelligence')
        self.assertEqual(actions, ['grant', 'intelligence_page_viewed'])
        self.assertEqual(candidates, [marker, marker])
        self.assertEqual(self.page.locator('#axonos-x-consent').count(), 0)
        self.assertEqual(binds, [({'wallet_address': WALLET}, None)])
        self.assertTrue(all(marker not in u.geturl() for u in self.requests if u.path != '/intelligence'))

    def test_public_entry_and_wallet_change(self):
        self.page.goto(BASE + '/intelligence')
        self.assertEqual(self.page.locator('textarea').count(), 0)
        self.assertFalse(any(u.path.startswith(('/api/', '/app/vendor/intelligence'))
                             and u.path != '/api/x-attribution/status' for u in self.requests))
        self.page.get_by_role('link', name='Connect wallet to chat').click()
        self.page.wait_for_url(BASE + '/?view=intelligence')
        self.wallet = WALLET
        self.page.goto(BASE + '/intelligence')
        self.send('first')
        self.page.locator('#thread article:nth-child(2)').wait_for()
        self.page.evaluate("localStorage.removeItem('axonos_last_wallet'); window.dispatchEvent(new Event('storage'))")
        self.page.wait_for_url(BASE + '/?view=intelligence')

    def test_expiry_and_retry(self):
        self.wallet = WALLET
        self.page.goto(BASE + '/intelligence')
        self.status = 429
        self.send('retry this')
        self.page.get_by_role('status').get_by_text('Try again').wait_for()
        self.assertEqual(self.page.locator('#prompt').input_value(), 'retry this')
        self.assertEqual(self.page.locator('#thread article').count(), 0)
        self.status = 401
        self.page.locator('#send').click()
        self.page.wait_for_url(BASE + '/?view=intelligence')
        self.wallet = None
        self.page.goto(BASE + '/intelligence')
        self.assertEqual(self.page.locator('#thread').count(), 0)

    def test_visible_page_revalidates_expired_session(self):
        self.wallet = WALLET
        self.page.goto(BASE + '/intelligence')
        self.send('private conversation')
        self.page.locator('#thread article:nth-child(2)').wait_for()
        self.status = 401
        self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        self.page.wait_for_url(BASE + '/?view=intelligence')
        self.wallet = None
        self.page.go_back()
        # location.replace removed the private page from browser history.
        self.assertNotIn('private conversation', self.page.locator('body').inner_text())

    def test_rendering_and_transcript(self):
        self.wallet = WALLET
        self.answer = r"""**Evidence** and $x^2$.

\[\frac{1}{2}\]

```python
print("$literal$ <em>")
```

<em>literal html</em> ![chart](https://external.test/chart)
[allowed](https://example.org) [blocked](ftp://example.org)
$\href{https://external.test}{untrusted}$ and $\unknowncommand$."""
        self.page.goto(BASE + '/intelligence')
        question = '<b>question</b>'
        self.send(question)
        reply = self.page.locator('#thread article:nth-child(2) .content')
        reply.locator('math[display="block"]').wait_for()
        self.assertGreaterEqual(reply.locator('math').count(), 2)
        self.assertEqual(reply.locator('strong').inner_text(), 'Evidence')
        self.assertEqual(reply.locator('pre code').text_content().strip(), 'print("$literal$ <em>")')
        self.assertEqual(reply.locator('pre math, em, img').count(), 0)
        self.assertEqual(reply.locator('a').evaluate_all('(a) => a.map(n => n.href)'), ['https://external.test/chart', 'https://example.org/'])
        self.assertTrue(all(u.netloc == 'axonos.test' for u in self.requests))
        self.assertEqual(self.page.locator('.user .content').inner_text(), question)
        self.assertEqual(self.page.locator('.user b').count(), 0)
        for width in (320, 1280):
            self.page.set_viewport_size({'width': width, 'height': 800})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            self.assertEqual(self.page.locator('#shell').bounding_box()['height'], 800)
        self.send('follow up')
        self.page.locator('#thread article:nth-child(4)').wait_for()
        self.assertEqual(self.prompts[-1], f'User: {question}\n\nAxonAI: {self.answer}\n\nUser: follow up')
        self.page.locator('#reset').click()
        self.assertEqual(self.page.locator('#thread article').count(), 0)

    def test_sidebar_history_and_public_snapshot(self):
        self.wallet, self.storage, self.answer = WALLET, True, '**Referenced** text'
        self.finish_reason = 'length'
        self.page.goto(BASE + '/intelligence')
        self.send('first study')
        self.page.locator('#thread article:nth-child(2) strong').first.wait_for()
        self.page.reload()
        self.page.locator('#thread .assistant .content strong').wait_for()
        self.assertEqual(self.page.locator('#chat-title').inner_text(), 'first study')
        self.assertIn('Output limit reached', self.page.locator('#thread .reply-note').inner_text())
        self.page.get_by_role('button', name='New chat', exact=True).click()
        self.assertEqual(self.page.locator('#thread article').count(), 0)
        self.send('second study')
        self.page.locator('#thread .assistant').wait_for()
        self.page.get_by_role('button', name='Open sidebar').click()
        self.page.get_by_role('searchbox').fill('first')
        self.page.wait_for_function("document.querySelectorAll('.chat-link').length === 1")
        self.page.get_by_role('button', name='first study', exact=True).click()
        self.page.wait_for_function("document.querySelector('#chat-title').textContent === 'first study'")
        self.page.get_by_role('button', name='Open sidebar').click()
        self.page.get_by_role('button', name='Options for first study').click()
        self.page.get_by_role('button', name='Rename', exact=True).click()
        self.page.get_by_label('Conversation title', exact=True).fill('Follow-up notes')
        self.page.get_by_role('button', name='Save changes').click()
        self.page.wait_for_function("document.querySelector('#chat-title').textContent === 'Follow-up notes'")
        self.page.get_by_role('button', name='Close sidebar', exact=True).click()
        self.page.get_by_role('button', name='Share', exact=True).click()
        self.page.get_by_role('button', name='Create link').wait_for()
        self.assertIsNone(self.snapshot)
        self.page.get_by_role('button', name='Create link').click()
        share_url = self.page.get_by_label('Share link', exact=True)
        share_url.wait_for()
        link = share_url.input_value()
        self.page.get_by_role('button', name='Close', exact=True).click()
        self.send('private follow up')
        self.page.locator('#thread article:nth-child(4)').wait_for()
        owner_url = self.page.url
        self.page.goto(link)
        self.assertEqual(self.page.locator('.shared-main h1').inner_text(), 'Follow-up notes')
        self.assertEqual(self.page.locator('.assistant .content strong').inner_text(), 'Referenced')
        self.assertIn('Output limit reached', self.page.locator('.shared-main .reply-note').inner_text())
        self.assertEqual(self.page.locator('textarea, #history').count(), 0)
        self.assertNotIn('private follow up', self.page.locator('body').inner_text())
        self.page.goto(owner_url)
        self.page.get_by_role('button', name='Share', exact=True).click()
        self.page.get_by_role('button', name='Revoke link').click()
        self.page.get_by_text('Share link revoked.', exact=True).wait_for()
        self.assertEqual(self.page.goto(link).status, 404)
        self.assertEqual(self.page.get_by_role('heading', level=1).inner_text(), 'Conversation unavailable')
        self.assertTrue(self.page.get_by_role('link', name='Open Intelligence').is_visible())
        self.page.set_viewport_size({'width': 1280, 'height': 800})
        self.page.goto(owner_url)
        self.page.get_by_role('button', name='Close sidebar', exact=True).click()
        self.assertFalse(self.page.locator('#sidebar').is_visible())
        self.page.get_by_role('button', name='Open sidebar').click()
        self.page.get_by_role('button', name='Options for Follow-up notes').click()
        self.page.get_by_role('button', name='Delete', exact=True).click()
        self.page.get_by_role('button', name='Delete conversation', exact=True).click()
        self.page.wait_for_function("document.querySelector('#chat-title').textContent === 'New chat'")
        self.assertEqual(self.page.locator('.chat-link').all_text_contents(), ['second study'])

    def test_existing_url_stays_neutral_before_scripts_and_until_restored(self):
        self.wallet, self.storage = WALLET, True
        url = BASE + '/intelligence?chat=study'
        self.chats['study'] = {'id': 'study', 'title': 'Saved study', 'revision': 1,
                               'messages': [{'role': 'user', 'content': 'Stored question'}]}

        def neutral():
            self.assertEqual(self.page.locator('#chat-title').inner_text(), 'Conversation')
            for selector in ('#welcome', '#starters', '#composer'):
                self.assertFalse(self.page.locator(selector).is_visible())

        # First paint must also work when the deferred application script is unavailable.
        script = '**/app/intelligence.js*'
        self.page.route(script, lambda route: route.abort())
        self.page.goto(url)
        self.page.get_by_text('Loading conversation…', exact=True).wait_for()
        neutral()
        self.page.unroute(script)
        held = []
        detail = '**/api/intelligence/conversations/study'
        self.page.route(detail, lambda route: held.append(route))
        self.page.reload()
        self.page.get_by_text('Loading conversation…', exact=True).wait_for()
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            self.page.wait_for_timeout(10)
        self.assertEqual(len(held), 1)
        neutral()
        held.pop().fulfill(status=503, json={'error': 'Storage temporarily unavailable'})
        self.page.get_by_text('Storage temporarily unavailable', exact=True).wait_for()
        neutral()
        self.assertEqual(self.page.url, url)
        self.page.unroute(detail)
        self.page.get_by_role('button', name='Retry conversation').click()
        self.page.get_by_text('Stored question', exact=True).wait_for()
        self.assertEqual(self.page.locator('#chat-title').inner_text(), 'Saved study')
        self.assertFalse(self.page.locator('#restore-panel').is_visible())
        self.assertTrue(self.page.locator('#prompt').is_editable())
        self.assertEqual(self.page.url, url)

    def test_slow_history_search_coalesces_typing_and_retries_only_current_query(self):
        self.wallet, self.storage = WALLET, True
        self.page.set_viewport_size({'width': 1100, 'height': 800})
        self.page.goto(BASE + '/intelligence')
        self.page.get_by_text('Your chats will appear here.', exact=True).wait_for()
        queries, pending = [], []

        def hold(route):
            queries.append(parse_qs(urlsplit(route.request.url).query).get('q', [''])[0])
            pending.append(route)

        def next_route():
            # Playwright emits its request event before the routing callback.
            deadline = time.monotonic() + 5
            while not pending and time.monotonic() < deadline:
                self.page.wait_for_timeout(10)
            self.assertTrue(pending, 'Expected a held wallet search request')
            return pending.pop(0)

        self.page.route('**/api/intelligence/conversations*', hold)
        search = self.page.get_by_role('searchbox')
        with self.page.expect_request(lambda r: r.url.endswith('?q=one')):
            search.fill('one')
        search.fill('two')
        self.page.wait_for_timeout(400)  # Expire debounce while the first read holds the lock.
        search.fill('thr')
        search.fill('three')
        self.page.wait_for_timeout(400)
        self.assertEqual(queries, ['one'])
        self.assertEqual(search.input_value(), 'three')
        busy = {'error': 'Wallet storage is busy. Try again shortly.'}
        with self.page.expect_request(lambda r: r.url.endswith('?q=three')):
            next_route().fulfill(status=503, json=busy)
        self.assertFalse(self.page.get_by_text(busy['error'], exact=True).is_visible())
        with self.page.expect_request(lambda r: r.url.endswith('?q=three')):
            next_route().fulfill(status=503, json=busy)
        next_route().fulfill(json={'storage_available': True, 'conversations': [
            {'id': '3', 'title': 'Three studies', 'shared': False}]})
        self.page.get_by_role('button', name='Three studies', exact=True).wait_for()
        with self.page.expect_request(lambda r: r.url.endswith('/conversations')):
            search.fill('')
        next_route().fulfill(json={'storage_available': True, 'conversations': []})
        self.page.get_by_text('Your chats will appear here.', exact=True).wait_for()
        self.assertEqual(queries, ['one', 'three', 'three', ''])

    def test_history_error_and_idempotent_send_retry(self):
        self.wallet, self.storage, self.fail_history = WALLET, True, True
        self.page.add_init_script("crypto.randomUUID = undefined")
        self.page.set_viewport_size({'width': 1100, 'height': 800})
        self.page.goto(BASE + '/intelligence')
        self.page.get_by_text('History unavailable', exact=True).wait_for()
        self.assertFalse(self.page.locator('#temporary').is_visible())
        self.assertFalse(self.page.locator('#prompt').is_editable())
        self.fail_history = False
        self.page.get_by_role('button', name='Retry history').click()
        self.status = 503
        self.send('repeat safely')
        self.page.locator('#status').get_by_text('Try again').wait_for()
        self.assertEqual(self.page.locator('#prompt').input_value(), 'repeat safely')
        self.status, self.answer = 200, 'Done'
        self.page.locator('#send').click()
        self.page.locator('#thread .assistant .content').get_by_text('Done').wait_for()
        self.assertEqual(len(self.chats), 1)
        self.assertEqual(self.turn_requests[0], self.turn_requests[1])
        self.assertEqual(UUID(self.turn_requests[0]['request_id']).version, 4)
