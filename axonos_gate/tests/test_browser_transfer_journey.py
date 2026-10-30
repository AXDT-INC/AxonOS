"""Disposable packaged landing -> real app consent code; never contact production.

Set AXONOS_TEST_LANDING_BUILD to a freshly built standalone landing directory and
AXONOS_TEST_NODE to a Node executable. Only synthetic click/key material is used.
X delivery is intentionally outside this browser test's claim.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from axonos_gate import x_capi
from axonos_gate.tests.test_x_capi_live_transport import production_env

BUILD = os.environ.get('AXONOS_TEST_LANDING_BUILD')
NODE = os.environ.get('AXONOS_TEST_NODE')
ROOT = Path(__file__).resolve().parents[2]
CLICK = 'Synthetic_Click_123'


@unittest.skipUnless(BUILD and NODE, 'requires explicit disposable landing build and Node')
class BrowserTransferJourney(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
        cls.addClassCleanup(cls.browser.close)
        with socket.socket() as port:
            port.bind(('127.0.0.1', 0)); cls.port = port.getsockname()[1]
        cls.server = subprocess.Popen([NODE, 'server/launch.mjs'], cwd=BUILD,
            env={'PATH': str(Path(NODE).parent)+':/usr/bin:/bin', 'NODE_ENV': 'production',
                 'HOSTNAME': '127.0.0.1', 'PORT': str(cls.port),
                 'X_ATTRIBUTION_TRANSFER': 'browser-click-v1'},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.stop_server)
        for _ in range(100):
            try:
                cls.get_landing('/'); break
            except OSError:
                if cls.server.poll() is not None: raise RuntimeError('Disposable landing exited')
                time.sleep(.1)
        else: raise RuntimeError('Disposable landing did not start')
        source = (ROOT/'novnc-theme/vnc.html').read_text()
        scripts = re.findall(r'<script>\s*([\s\S]*?)</script>', source)[:3 if getattr(cls, 'SHARED', False) else 2]
        notice = re.search(r'<section id="axonos-attribution-settings"[\s\S]*?</section>', source).group(0)
        cls.app_html = '<!doctype html><html><head><meta name="referrer" content="no-referrer">' + ''.join(
            '<script>'+script+'</script>' for script in scripts) + '</head><body>App test' + \
            '<button id="app-action" onclick="this.textContent=\'App action works\'">App action</button>' + notice + '</body></html>'

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        try: cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired: cls.server.kill(); cls.server.wait()

    @classmethod
    def get_landing(cls, path):
        with urlopen(Request('http://127.0.0.1:'+str(cls.port)+path, headers={'Host': 'axonos.io'}), timeout=5) as response:
            return response.read(), dict(response.headers)

    @contextlib.contextmanager
    def journey(self, gpc=False):
        from cryptography.fernet import Fernet
        cipher = Fernet(Fernet.generate_key())
        env = {**production_env(), 'X_CAPI_CONSENT_POLICY_VERSION': 'x-capi-browser-v1',
            'X_CAPI_TWCLID_CONTRACT_VERSION': 'browser-click-v1', 'X_HANDOFF_ENABLED': 'false',
            'X_CAPI_TWCLID_CHARSET': 'url_safe', 'X_CAPI_TWCLID_MIN_LENGTH': '8', 'X_CAPI_TWCLID_MAX_LENGTH': '256'}
        if getattr(self, 'SHARED', False):
            env['X_CAPI_PRIVACY_LANDING_ORIGIN'] = 'https://axonos.io'
        context = self.browser.new_context(viewport={'width': 1440, 'height': 1000})
        if gpc: context.add_init_script('Object.defineProperty(navigator,"globalPrivacyControl",{get:()=>true})')
        page = context.new_page(); page.set_default_timeout(10000)
        seen = []; grants = []
        def route(request_route):
            request = request_route.request
            url = urlsplit(request.url); seen.append(request.url)
            if url.hostname == 'axonos.io':
                if url.path.startswith('/stats/'):
                    return request_route.fulfill(status=204, body='')
                body, headers = self.get_landing(url.path + ('?'+url.query if url.query else ''))
                return request_route.fulfill(status=200, headers=headers, body=body)
            if url.hostname == 'app.axonos.io':
                headers = request.all_headers()
                if url.path == '/api/x-attribution/status':
                    body = x_capi.attribution_status(headers.get('x-axonos-attribution'),
                        landing_twclid=headers.get('x-axonos-landing-click'), gpc=headers.get('sec-gpc') == '1')
                    return request_route.fulfill(json=body)
                if url.path == '/api/x-attribution/consent':
                    if getattr(self, 'fail_revoke', False) and request.post_data_json['action'] == 'revoke':
                        return request_route.fulfill(status=503, json={'ok': False})
                    body, code = x_capi.update_consent(context_token=headers.get('x-axonos-attribution'),
                        csrf_token=headers.get('x-axonos-csrf'), action=request.post_data_json['action'],
                        origin=headers.get('origin'), landing_twclid=headers.get('x-axonos-landing-click'))
                    if body.get('state') == 'granted': grants.append(x_capi.decode_context_ticket(body['context']))
                    return request_route.fulfill(status=code, json=body)
                if url.path == '/app/x-attribution.js':
                    return request_route.fulfill(content_type='text/javascript', body=(ROOT/'novnc-theme/app/x-attribution.js').read_text())
                if url.path in ('/app/privacy-preferences.js', '/app/privacy-preferences-entry.js'):
                    return request_route.fulfill(content_type='text/javascript', body=(ROOT/'novnc-theme'/url.path.lstrip('/')).read_text())
                if url.path == '/privacy-preferences.html':
                    return request_route.fulfill(content_type='text/html', body=(ROOT/'novnc-theme/privacy-preferences.html').read_text())
                if url.path == '/': return request_route.fulfill(content_type='text/html', body=self.app_html)
            request_route.abort()
        with patch.dict(os.environ, env, clear=True), \
                patch.object(x_capi, '_context_ciphers', return_value=((cipher,), None)), \
                patch.object(x_capi, 'config_guard_current', return_value=True), \
                patch.object(x_capi, '_request_worker_consent', side_effect=lambda action, *_: {
                    'v': 1, 'ok': True, 'state': 'denied' if action == 'decline' else 'revoked'}), \
                patch.object(x_capi, '_persist_privacy_fence_status', return_value='published'):
            context.route('**/*', route)
            try: yield page, seen, grants
            finally: context.close()

    def test_packaged_landing_to_app_requires_single_app_consent_and_revokes(self):
        with self.journey() as (page, seen, grants):
            page.goto('https://axonos.io/?twclid='+CLICK)
            page.wait_for_function('window.axonosLandingHandoff?.snapshot().eligible')
            page.wait_for_function('document.querySelector("a[data-axonos-app-entry]") !== null')
            self.assertEqual(page.locator('dialog:visible, .attribution-panel:visible').count(), 0)
            self.assertNotIn('Provisional consent copy', page.locator('body').inner_text())
            page.locator('a[data-axonos-app-entry]:visible').first.click()
            page.wait_for_url('https://app.axonos.io/')
            page.get_by_role('button', name='Conversion sharing', exact=True).wait_for()
            self.assertEqual(page.locator('#axonos-x-consent').count(), 0)
            self.assertEqual(grants, [])
            page.get_by_role('button', name='Conversion sharing', exact=True).click()
            page.get_by_role('button', name='Allow sharing', exact=True).wait_for()
            self.assertEqual(grants, [])
            self.assertFalse(any(CLICK in value for value in seen if 'app.axonos.io' in value))
            page.get_by_role('button', name='Allow sharing', exact=True).click()
            page.wait_for_function('!!window.axonosAttributionContext')
            self.assertEqual(len(grants), 1)
            self.assertEqual(grants[0]['twclid'], CLICK)
            page.get_by_role('button', name='Conversion sharing', exact=True).click()
            page.get_by_role('button', name='Revoke sharing', exact=True).click()
            page.wait_for_function('!window.axonosAttributionContext')

    def test_declining_in_app_does_not_grant_sharing_or_block_app(self):
        with self.journey() as (page, seen, grants):
            page.goto('https://axonos.io/?twclid='+CLICK)
            page.wait_for_function('window.axonosLandingHandoff?.snapshot().eligible')
            page.locator('a[data-axonos-app-entry]:visible').first.click()
            page.wait_for_url('https://app.axonos.io/')
            page.get_by_role('button', name='Conversion sharing', exact=True).wait_for()
            self.assertEqual(page.locator('#axonos-x-consent').count(), 0)
            self.assertEqual(grants, [])
            page.get_by_role('button', name='Conversion sharing', exact=True).click()
            decline = page.get_by_role('button', name='Decline', exact=True)
            decline.wait_for()
            self.assertEqual(grants, [])
            self.assertFalse(page.evaluate('(click) => Object.values(sessionStorage).some(v => v.includes(click))', CLICK))
            decline.click()
            decline.wait_for(state='hidden')
            self.assertEqual(grants, [])
            self.assertFalse(page.evaluate('!!window.axonosAttributionContext'))
            self.assertIn('App test', page.locator('body').inner_text())
            self.assertFalse(any(CLICK in value for value in seen if 'app.axonos.io' in value))
            page.reload()
            page.wait_for_function('window.axonosAttributionReady === true')
            self.assertEqual(page.locator('#axonos-x-consent').count(), 0)
            self.assertEqual(grants, [])

    def test_notice_and_optional_choice_leave_app_usable_on_desktop_and_mobile(self):
        for width, height in ((1440, 1000), (390, 844), (320, 568), (844, 390)):
            with self.subTest(width=width), self.journey() as (page, seen, grants):
                page.set_viewport_size({'width': width, 'height': height})
                page.goto('https://axonos.io/?twclid='+CLICK)
                page.wait_for_function('window.axonosLandingHandoff?.snapshot().eligible')
                page.locator('#x-attribution-privacy summary').click()
                self.assertTrue(page.locator('#x-attribution-privacy').evaluate('(el) => el.open'))
                self.assertIn('Opening the app does not grant permission', page.locator('#x-attribution-privacy').inner_text())
                page.locator('a[data-axonos-app-entry]:visible').first.click()
                page.wait_for_url('https://app.axonos.io/')
                page.get_by_role('button', name='Conversion sharing', exact=True).wait_for()
                self.assertEqual(page.locator('#axonos-x-consent').count(), 0)
                self.assertEqual(grants, [])
                page.get_by_role('button', name='App action', exact=True).click()
                self.assertEqual(page.locator('#app-action').inner_text(), 'App action works')
                page.get_by_role('button', name='Conversion sharing', exact=True).click()
                page.get_by_role('button', name='Allow sharing', exact=True).wait_for()
                self.assertEqual(page.locator('[aria-modal="true"]').count(), 0)
                self.assertFalse(page.locator('#axonos-x-consent').evaluate('(el) => el.contains(document.activeElement)'))
                control = page.get_by_role('button', name='Conversion sharing', exact=True)
                self.assertEqual(control.get_attribute('aria-expanded'), 'true')
                self.assertTrue(page.locator('#axonos-attribution-preferences #axonos-x-consent').count())
                self.assertTrue(page.locator('#axonos-x-consent').evaluate('''el => {
                    const style = getComputedStyle(el);
                    return style.position === 'static' && style.maxHeight === 'none' &&
                        style.overflowY === 'visible';
                }'''))
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                # Keyboard users reach the choice actions before the optional details.
                control.focus()
                page.keyboard.press('Tab')
                self.assertEqual(page.evaluate('document.activeElement.textContent'), 'Decline')
                page.keyboard.press('Tab')
                self.assertEqual(page.evaluate('document.activeElement.textContent'), 'Allow sharing')
                page.locator('#axonos-x-consent summary').click()
                self.assertIn('Global Privacy Control', page.locator('#axonos-x-consent details').inner_text())
                page.get_by_role('button', name='Not now', exact=True).click()
                self.assertEqual(page.locator('#axonos-x-consent').count(), 0)
                self.assertEqual(grants, [])
                self.assertFalse(page.evaluate('!!window.axonosAttributionContext'))
                self.assertEqual(control.get_attribute('aria-expanded'), 'false')
                self.assertEqual(page.evaluate('document.activeElement.id'), 'axonos-x-privacy-control')
                page.get_by_role('button', name='Conversion sharing', exact=True).click()
                page.get_by_role('button', name='Allow sharing', exact=True).wait_for()
                self.assertEqual(grants, [])
                page.locator('#axonos-attribution-privacy summary').click()
                self.assertTrue(page.locator('#axonos-attribution-privacy').evaluate('(el) => el.open'))

    def test_ordinary_and_gpc_visitors_do_not_see_the_broken_panel(self):
        for gpc, query in ((False, ''), (True, '?twclid='+CLICK)):
            with self.subTest(gpc=gpc), self.journey(gpc) as (page, seen, grants):
                page.goto('https://axonos.io/'+query)
                page.wait_for_function('window.axonosLandingHandoff?.snapshot().ready')
                self.assertEqual(page.locator('dialog:visible, .attribution-panel:visible').count(), 0)
                self.assertNotIn('Retry withdrawal', page.locator('body').inner_text())
                self.assertFalse(any('/attribution/' in value for value in seen))
                self.assertEqual(grants, [])
