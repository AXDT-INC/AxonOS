"""Browser interaction checks; opt in with AXONOS_RUN_BROWSER_TESTS=1.

Uses a local HTML fixture and intercepted API calls, never a deployed service.
"""
import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.getenv('AXONOS_RUN_BROWSER_TESTS') == '1', 'browser checks not enabled')
class SessionScheduleBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.requests = []
        self.reject_update = False

        def api(route):
            body = route.request.post_data_json
            self.requests.append(body)
            route.fulfill(status=409 if self.reject_update else 200, content_type='application/json', body=json.dumps(
                {'ok': False, 'error': 'Session has ended'} if self.reject_update else
                {'ok': True, 'session_id': body['session_id'], 'scheduled_stop_at': body['stop_at'],
                 'hard_cap_remaining_seconds': None if body['stop_at'] is None else 3600}))

        self.page.route('http://axonos.test/api/session/deadline', api)
        self.page.route('http://axonos.test/', lambda route: route.fulfill(body='''<!doctype html><html><body>
            <p id="axonos_ssh_card_ttl"></p><p id="axonos_ssh_compute_estimate"></p>
            </body></html>''', content_type='text/html'))
        self.page.goto('http://axonos.test/')
        self.page.add_style_tag(content=(ROOT / 'novnc-theme/axonos-theme.css').read_text())
        source = (ROOT / 'novnc-theme/ui.js').read_text()
        methods = source[source.index('    _axonosUpdateSshCardCap(payload) {'):source.index('    hideAxonosSshCard()')]
        self.page.evaluate('window.UI = {' + methods + '};')
        self.page.evaluate("""() => {
            window.verifiedWalletAddress = '0x1234567890123456789012345678901234567890';
            window.verifiedWalletAuthToken = 'fixture-token';
            window.UI._axonosSshClaim = {session_id: 529, scheduled_stop_at: null};
        }""")

    def test_set_extend_remove_and_warning_target_the_exact_session(self):
        self.page.evaluate("UI.openSessionSchedule({session_id: 529, scheduled_stop_at: null})")
        self.page.get_by_role('button', name='Save stop time').click()
        self.page.wait_for_function("!document.querySelector('dialog').open")
        first = self.requests[-1]
        self.assertEqual(first['session_id'], 529)
        self.assertGreater(first['stop_at'], 0)
        self.assertIn('Scheduled stop:', self.page.locator('#axonos_ssh_card_ttl').inner_text())
        # A different session approaching its deadline gets a visible warning.
        self.page.evaluate("""() => {
            UI.updateScheduledStopWarnings([
                {session_id: 529, scheduled_stop_at: Date.now()/1000 + 7200},
                {session_id: 530, scheduled_stop_at: Date.now()/1000 + 420}
            ], true);
        }""")
        banner = self.page.get_by_role('alert').filter(has_text='Session #530')
        self.assertTrue(banner.is_visible())
        banner.get_by_role('button').click()
        self.page.get_by_role('button', name='Extend 1 hour', exact=True).click()
        self.page.wait_for_function("!document.querySelector('dialog').open")
        self.assertEqual(self.requests[-1]['session_id'], 530)
        self.assertTrue(self.page.locator('#axonos_scheduled_stop_warning').is_hidden())
        self.page.evaluate("UI.openSessionSchedule(UI._axonosSshClaim)")
        self.page.get_by_role('button', name='Remove stop time').click()
        self.page.wait_for_function("!document.querySelector('dialog').open")
        self.assertIsNone(self.requests[-1]['stop_at'])
        self.assertIn('Scheduled stop: none', self.page.locator('#axonos_ssh_card_ttl').inner_text())

    def test_error_does_not_claim_schedule_was_changed(self):
        self.reject_update = True
        self.page.evaluate("UI.openSessionSchedule({session_id: 529, scheduled_stop_at: null})")
        self.page.get_by_role('button', name='Save stop time').click()
        self.page.get_by_text('Session has ended', exact=True).wait_for()
        self.assertTrue(self.page.locator('dialog').is_visible())
        self.assertIsNone(self.page.evaluate('UI._axonosSshClaim.scheduled_stop_at'))

    def test_wallet_switch_does_not_submit_old_session_action(self):
        self.page.evaluate("UI.openSessionSchedule({session_id: 529, scheduled_stop_at: null})")
        self.page.evaluate("window.verifiedWalletAddress = '0x2234567890123456789012345678901234567890'")
        self.page.get_by_role('button', name='Save stop time').click()
        self.page.get_by_text('Session identity is unavailable. Reconnect your wallet.', exact=True).wait_for()
        self.assertEqual(self.requests, [])

    def test_no_warning_for_funded_unscheduled_session_or_guest(self):
        self.page.evaluate("UI.updateScheduledStopWarnings([{session_id:529,scheduled_stop_at:null}],true)")
        self.assertEqual(self.page.locator('#axonos_scheduled_stop_warning').count(), 0)
        self.page.evaluate("UI.updateScheduledStopWarnings([{session_id:529,guest_session:true,scheduled_stop_at:Date.now()/1000+30}],true)")
        self.assertEqual(self.page.locator('#axonos_scheduled_stop_warning').count(), 0)

    def _install_lifecycle_handlers(self):
        source = (ROOT / 'novnc-theme/ui.js').read_text()
        method = source[source.index('    addAxonosSessionLifecycleHandlers() {'):source.index('    persistAxonosSelectedTemplate()')]
        self.page.evaluate('Object.assign(UI, {' + method + '}); UI.addAxonosSessionLifecycleHandlers();')

    def test_attached_viewer_requests_native_confirmation_without_release(self):
        self._install_lifecycle_handlers()
        self.page.evaluate("""() => {
            UI.connected = true;
            window.releaseCalls = 0;
            UI._axonosReleaseSessionBeacon = () => window.releaseCalls++;
            document.body.insertAdjacentHTML('beforeend','<button id="activate">Activate</button>');
        }""")
        self.page.locator('#activate').click()
        dialogs = []
        def confirm(dialog):
            dialogs.append(dialog.type)
            dialog.dismiss()
        self.page.on('dialog', confirm)
        # Reload surfaces the real Chromium confirmation. Dismissing keeps
        # the page alive so we can verify no release was requested.
        self.page.evaluate('setTimeout(() => location.reload(), 0)')
        self.page.wait_for_timeout(100)
        self.assertEqual(dialogs, ['beforeunload'])
        self.assertFalse(self.page.is_closed())
        self.assertEqual(self.page.evaluate('window.releaseCalls'), 0)
        self.page.remove_listener('dialog', confirm)
        self.page.evaluate('UI.connected = false')

    def test_confirmation_only_for_attached_desktop_or_terminal(self):
        self._install_lifecycle_handlers()
        for connected, terminal, detached, ending, expected in [
            (True, 'idle', False, False, True),
            (False, 'connected', False, False, True),
            (False, 'idle', True, False, False),
            (False, 'idle', False, False, False),
            (True, 'idle', False, True, False),
        ]:
            blocked = self.page.evaluate("""([connected, terminal, detached, ending]) => {
                UI.connected=connected; UI.terminalState=terminal;
                window.axonosSessionDetached=detached; UI._axgtEndingSession=ending;
                const event=new Event('beforeunload',{cancelable:true});
                window.dispatchEvent(event); return event.defaultPrevented;
            }""", [connected, terminal, detached, ending])
            self.assertEqual(blocked, expected)

    def test_numeric_credit_fields_are_eligible_only_and_validate_amounts(self):
        source = (ROOT / 'novnc-theme/vnc.html').read_text()
        helpers = source[source.index('        function axonosSyncTestCreditControls()'):source.index('        function axonosPaymentIdentityIsCurrent')]
        self.page.evaluate("""() => {
            ['axonos_dashboard_topup_btn','axonos_sidebar_topup_btn','axonos_test_credit_btn','axonos_wizard_test_credit_btn'].forEach(id => {
                const button=document.createElement('button'); button.id=id; document.body.appendChild(button);
            });
        }""")
        self.page.evaluate('() => {' + helpers + '; window.axonosSyncTestCreditControls=axonosSyncTestCreditControls; window.axonosReadTestCreditAmount=axonosReadTestCreditAmount; }')
        self.page.evaluate('window.axonosTestCreditEligible=false; axonosSyncTestCreditControls()')
        self.assertEqual(self.page.locator('.axonos-test-credit-amount').count(), 0)
        self.page.evaluate('window.axonosTestCreditEligible=true; window.axonosTestCreditGrantMinutes=60; window.axonosTestCreditMaxGrantMinutes=1440; axonosSyncTestCreditControls()')
        self.assertEqual(self.page.locator('.axonos-test-credit-amount').count(), 4)
        field = self.page.locator('#axonos_dashboard_topup_btn_amount')
        field.fill('500')
        field.press('e')
        self.assertEqual(field.input_value(), '500')
        self.assertEqual(self.page.evaluate("axonosReadTestCreditAmount('axonos_dashboard_topup_btn')"), 500)
        for value in ['', '0', '1441']:
            field.fill(value)
            self.assertIsNone(self.page.evaluate("axonosReadTestCreditAmount('axonos_dashboard_topup_btn')"))
        self.page.evaluate('window.axonosTestCreditEligible=false; axonosSyncTestCreditControls()')
        self.assertTrue(field.is_disabled())
        self.assertTrue(field.is_hidden())
        self.assertEqual(self.page.locator('#axonos_dashboard_topup_btn').inner_text(), 'Top up credits')
