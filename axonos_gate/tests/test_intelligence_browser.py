"""Opt-in Chromium checks using the gate template, packaged assets and simulated API replies."""
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit

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
        self.send('follow up')
        self.page.locator('#thread article:nth-child(4)').wait_for()
        self.assertEqual(self.prompts[-1], f'User: {question}\n\nAxonAI: {self.answer}\n\nUser: follow up')
        self.page.locator('#reset').click()
        self.assertEqual(self.page.locator('#thread article').count(), 0)
