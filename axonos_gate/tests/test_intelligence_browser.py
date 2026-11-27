"""Opt-in Chromium checks using the gate template, packaged assets and simulated API replies."""
from copy import deepcopy
import os
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit
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
        if url.netloc != 'axonos.test':
            return route.abort()
        if url.path == '/intelligence':
            return route.fulfill(content_type='text/html', body=self.template.render(wallet=self.wallet))
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
            return route.fulfill(status=self.status, json={'text': self.answer, 'error': 'Try again'})
        if url.path == '/api/auth/wallet-status':
            return route.fulfill(status=self.status, json={})
        asset = THEME / url.path.lstrip('/')
        if url.path == '/app/styles/axonos-theme.css':
            asset = THEME / 'axonos-theme.css'
        return route.fulfill(path=asset) if asset.is_file() else route.fulfill(status=404, body='')

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
                                     {'role': 'assistant', 'content': self.answer}])
            return route.fulfill(json=chat)
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

    def send(self, text):
        self.page.locator('#prompt').fill(text)
        self.page.locator('#send').click()

    def test_public_entry_and_wallet_change(self):
        self.page.goto(BASE + '/intelligence')
        self.assertEqual(self.page.locator('textarea').count(), 0)
        self.assertFalse(any(u.path.startswith(('/api/', '/app/vendor/intelligence')) for u in self.requests))
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
        self.page.goto(BASE + '/intelligence')
        self.send('first study')
        self.page.locator('#thread article:nth-child(2) strong').first.wait_for()
        self.page.reload()
        self.page.locator('#thread .assistant .content strong').wait_for()
        self.assertEqual(self.page.locator('#chat-title').inner_text(), 'first study')
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
        self.page.locator('#thread .assistant').wait_for()
        self.assertEqual(len(self.chats), 1)
        self.assertEqual(self.turn_requests[0], self.turn_requests[1])
        self.assertEqual(UUID(self.turn_requests[0]['request_id']).version, 4)
