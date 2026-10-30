"""Synthetic same-site browser journeys; no live X requests or production data."""
import unittest
import sys
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from axonos_gate import x_capi
from axonos_gate.tests.test_x_capi_live_transport import production_env
from axonos_gate.tests import test_browser_transfer_journey as journey_fixture

BUILD, NODE, CLICK = journey_fixture.BUILD, journey_fixture.NODE, journey_fixture.CLICK


class SharedPrivacyConfiguration(unittest.TestCase):
    def test_exact_same_site_origin_required_and_optional_by_default(self):
        env = {**production_env(), 'X_CAPI_TWCLID_CONTRACT_VERSION': 'browser-click-v1'}
        for origin, expected in [('', False), ('https://axonos.io', True),
                                 ('https://axonos.io/', False), ('https://other.example', False),
                                 ('https://axonos.io.evil.test', False), ('https://[', False),
                                 ('http://axonos.io', False), ('https://user@axonos.io', False)]:
            with self.subTest(origin=origin), patch.dict('os.environ',
                    {**env, 'X_CAPI_PRIVACY_LANDING_ORIGIN': origin}, clear=True):
                self.assertEqual(x_capi.browser_preferences_config()['enabled'], expected)

    def test_only_exact_preferences_document_can_be_framed(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        with patch.dict('os.environ', {}, clear=True):
            from axonos_gate import gate_server
        env = {**production_env(), 'X_CAPI_TWCLID_CONTRACT_VERSION': 'browser-click-v1',
               'X_CAPI_PRIVACY_LANDING_ORIGIN': 'https://axonos.io'}
        with patch.dict('os.environ', env, clear=True):
            client = gate_server.app.test_client()
            for path in ('/', '/privacy-preferences.html', '/privacy-preferences.html?other=1',
                         '/api/x-attribution/status'):
                response = client.get(path, base_url='https://app.axonos.io')
                csp = response.headers['Content-Security-Policy']
                if path == '/privacy-preferences.html':
                    self.assertIn("frame-ancestors 'self' https://axonos.io", csp)
                    self.assertNotIn('X-Frame-Options', response.headers)
                    self.assertIn('no-store', response.headers['Cache-Control'])
                else:
                    self.assertIn("frame-ancestors 'none'", csp)
                    self.assertEqual(response.headers['X-Frame-Options'], 'DENY')

    def test_websocket_listener_frame_exception_is_exact_and_get_only(self):
        source = Path(__file__).resolve().parents[1] / 'websockify_gate.py'
        tree = ast.parse(source.read_text())
        method = next(n for cls in tree.body if isinstance(cls, ast.ClassDef)
                      for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'end_headers')
        class Base:
            def send_header(self, key, value): self.collected[key] = value
            def end_headers(self): pass
        subject = ast.ClassDef(name='Subject', bases=[ast.Name(id='Base', ctx=ast.Load())],
                               keywords=[], body=[method], decorator_list=[])
        namespace = {'Base': Base, 'x_capi': x_capi, 'request_is_effectively_https': lambda *a: False}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[subject], type_ignores=[])),
                     str(source), 'exec'), namespace)
        env = {**production_env(), 'X_CAPI_TWCLID_CONTRACT_VERSION': 'browser-click-v1',
               'X_CAPI_PRIVACY_LANDING_ORIGIN': 'https://axonos.io'}
        with patch.dict('os.environ', env, clear=True):
            for path, verb, allowed in [('/privacy-preferences.html', 'GET', True),
                                        ('/privacy-preferences.html', 'HEAD', True),
                                        ('/privacy-preferences.html', 'POST', False),
                                        ('/privacy-preferences.html?x=1', 'GET', False),
                                        ('/vnc.html', 'GET', False),
                                        ('/%70rivacy-preferences.html', 'GET', False)]:
                instance = namespace['Subject']()
                instance.path, instance.command = path, verb
                instance.server = SimpleNamespace(ssl_only=False)
                instance.collected = {}
                instance.end_headers()
                self.assertEqual('X-Frame-Options' not in instance.collected, allowed)


@unittest.skipUnless(BUILD and NODE, 'requires disposable landing build and Node')
class SharedPrivacyJourney(unittest.TestCase):
    SHARED = True
    setUpClass = classmethod(journey_fixture.BrowserTransferJourney.setUpClass.__func__)
    stop_server = classmethod(journey_fixture.BrowserTransferJourney.stop_server.__func__)
    get_landing = classmethod(journey_fixture.BrowserTransferJourney.get_landing.__func__)
    journey = journey_fixture.BrowserTransferJourney.journey

    def landing(self, page):
        page.goto('https://axonos.io/?twclid=' + CLICK)
        page.wait_for_function('window.axonosLandingHandoff?.snapshot().eligible')
        frame = page.frame_locator('iframe[title="AxonOS privacy preferences"]')
        frame.get_by_role('button', name='Accept optional', exact=True).wait_for()
        return frame

    def launch(self, page):
        page.locator('a[data-axonos-app-entry]:visible').first.click()
        page.wait_for_url('https://app.axonos.io/')
        page.get_by_role('button', name='Privacy settings', exact=True).wait_for()

    def test_accept_on_landing_is_reused_in_app_and_withdraws_back_on_landing(self):
        with self.journey() as (page, seen, grants):
            frame = self.landing(page)
            frame.get_by_role('button', name='Accept optional', exact=True).click()
            frame.get_by_role('button', name='Accept optional', exact=True).wait_for(state='hidden')
            self.launch(page)
            page.wait_for_function('!!window.axonosAttributionContext')
            self.assertEqual(len(grants), 1)
            self.assertEqual(page.get_by_role('button', name='Allow sharing', exact=True).count(), 0)
            self.assertEqual(page.get_by_role('button', name='Accept optional', exact=True).count(), 0)
            self.assertFalse(any(CLICK in url for url in seen if 'app.axonos.io' in url))
            self.assertEqual(len(page.context.cookies('https://app.axonos.io')), 1)
            self.assertNotIn(CLICK, str(page.context.cookies('https://app.axonos.io')))
            page.goto('https://axonos.io/')
            frame = page.frame_locator('iframe[title="AxonOS privacy preferences"]')
            frame.get_by_role('button', name='Privacy settings', exact=True).click()
            frame.get_by_role('button', name='Reject optional', exact=True).click()
            frame.get_by_role('button', name='Reject optional', exact=True).wait_for(state='hidden')
            app_frame = next(f for f in page.frames if 'privacy-preferences.html' in f.url)
            self.assertEqual(app_frame.evaluate('JSON.parse(localStorage.getItem("axonos_privacy_revocations_v1"))'), [])

    def test_reject_on_landing_does_not_prompt_or_grant_in_app(self):
        with self.journey() as (page, seen, grants):
            frame = self.landing(page)
            frame.get_by_role('button', name='Reject optional', exact=True).click()
            frame.get_by_role('button', name='Reject optional', exact=True).wait_for(state='hidden')
            self.launch(page)
            page.wait_for_function('window.axonosAttributionReady')
            self.assertEqual(grants, [])
            self.assertFalse(page.evaluate('!!window.axonosAttributionContext'))
            self.assertEqual(page.get_by_role('button', name='Accept optional', exact=True).count(), 0)

    def test_direct_app_choice_and_cross_tab_withdrawal(self):
        with self.journey() as (page, seen, grants):
            page.goto('https://app.axonos.io/#axonos_x_click=' + CLICK)
            page.get_by_role('button', name='Accept optional', exact=True).click()
            page.wait_for_function('!!window.axonosAttributionContext')
            other = page.context.new_page()
            other.goto('https://axonos.io/')
            frame = other.frame_locator('iframe[title="AxonOS privacy preferences"]')
            frame.get_by_role('button', name='Privacy settings', exact=True).click()
            frame.get_by_role('button', name='Reject optional', exact=True).click()
            frame.get_by_role('button', name='Reject optional', exact=True).wait_for(state='hidden')
            page.wait_for_function('!window.axonosAttributionContext')
            self.assertEqual(len(grants), 1)

    def test_failed_withdrawal_remains_pending_and_can_be_retried(self):
        with self.journey() as (page, seen, grants):
            page.goto('https://app.axonos.io/#axonos_x_click=' + CLICK)
            page.get_by_role('button', name='Accept optional', exact=True).click()
            page.wait_for_function('!!window.axonosAttributionContext')
            self.fail_revoke = True
            page.goto('https://axonos.io/')
            frame = page.frame_locator('iframe[title="AxonOS privacy preferences"]')
            frame.get_by_role('button', name='Privacy settings', exact=True).click()
            frame.get_by_role('button', name='Reject optional', exact=True).click()
            frame.get_by_role('status').wait_for()
            app_frame = next(f for f in page.frames if 'privacy-preferences.html' in f.url)
            self.assertTrue(app_frame.evaluate('JSON.parse(localStorage.getItem("axonos_privacy_revocations_v1"))[0].pending'))
            self.fail_revoke = False
            frame.get_by_role('button', name='Reject optional', exact=True).click()
            app_frame.wait_for_function('JSON.parse(localStorage.getItem("axonos_privacy_revocations_v1")).length === 0')
            frame.get_by_role('status').wait_for(state='hidden')
            self.assertEqual(app_frame.evaluate('JSON.parse(localStorage.getItem("axonos_privacy_revocations_v1"))'), [])

    def test_parent_message_cannot_grant(self):
        with self.journey() as (page, seen, grants):
            self.landing(page)
            page.evaluate('document.querySelector("iframe").contentWindow.postMessage({choice:"allow",action:"grant"},"https://app.axonos.io")')
            self.launch(page)
            page.wait_for_function('window.axonosAttributionReady')
            self.assertEqual(grants, [])

    def test_blocked_preference_cookie_never_grants(self):
        with self.journey() as (page, seen, grants):
            page.add_init_script("Object.defineProperty(document, 'cookie', {get:()=>'',set:()=>{}})")
            page.goto('https://app.axonos.io/#axonos_x_click=' + CLICK)
            page.get_by_role('button', name='Accept optional', exact=True).click()
            page.get_by_role('status').wait_for()
            self.assertEqual(grants, [])
            self.assertFalse(page.evaluate('!!window.axonosAttributionContext'))

    def test_withdrawal_survives_a_cookie_write_failure_and_reload(self):
        with self.journey() as (page, seen, grants):
            page.goto('https://app.axonos.io/#axonos_x_click=' + CLICK)
            page.get_by_role('button', name='Accept optional', exact=True).click()
            page.wait_for_function('!!window.axonosAttributionContext')
            page.evaluate('''const cookie=Object.getOwnPropertyDescriptor(Document.prototype,'cookie');
                Object.defineProperty(document,'cookie',{get:()=>cookie.get.call(document),set:()=>{}});''')
            page.get_by_role('button', name='Privacy settings', exact=True).click()
            page.get_by_role('button', name='Reject optional', exact=True).click()
            page.get_by_role('status').wait_for()
            page.wait_for_function('JSON.parse(localStorage.getItem("axonos_privacy_revocations_v1")).length === 0')
            self.assertEqual(page.evaluate('localStorage.getItem("axonos_privacy_blocked_v1")'), '1')
            page.goto('https://app.axonos.io/#axonos_x_click=Different_Click_456')
            page.wait_for_function('window.axonosAttributionReady')
            self.assertFalse(page.evaluate('!!window.axonosAttributionContext'))
            self.assertEqual(len(grants), 1)

    def test_old_policy_cookie_is_not_reinterpreted_as_new_acceptance(self):
        with self.journey() as (page, seen, grants):
            frame = self.landing(page)
            frame.get_by_role('button', name='Accept optional', exact=True).click()
            frame.get_by_role('button', name='Accept optional', exact=True).wait_for(state='hidden')
            app_frame = next(f for f in page.frames if 'privacy-preferences.html' in f.url)
            app_frame.evaluate('''const value=JSON.parse(decodeURIComponent(document.cookie.split('=')[1]));
                value.epoch += 1;
                document.cookie='__Host-axonos_privacy_v1='+encodeURIComponent(JSON.stringify(value))+'; Path=/; Secure; SameSite=Strict';''')
            self.launch(page)
            page.get_by_role('button', name='Accept optional', exact=True).wait_for()
            self.assertEqual(grants, [])

    def test_unwritable_registry_blocks_grant_after_choice(self):
        with self.journey() as (page, seen, grants):
            page.add_init_script('''const originalSet=Storage.prototype.setItem;
                Storage.prototype.setItem=function(key,value){
                    if(key==='axonos_privacy_revocations_v1') throw new Error('Synthetic quota failure');
                    return originalSet.call(this,key,value);
                };''')
            page.goto('https://app.axonos.io/#axonos_x_click=' + CLICK)
            page.get_by_role('button', name='Accept optional', exact=True).click()
            page.get_by_role('status').wait_for()
            self.assertEqual(grants, [])
            self.assertFalse(page.evaluate('!!window.axonosAttributionContext'))
    def test_gpc_blocks_acceptance(self):
        with self.journey(gpc=True) as (page, seen, grants):
            page.goto('https://app.axonos.io/#axonos_x_click=' + CLICK)
            page.get_by_text('Your browser privacy signal', exact=False).wait_for()
            self.assertEqual(page.get_by_role('button', name='Accept optional', exact=True).count(), 0)
            self.assertEqual(grants, [])
