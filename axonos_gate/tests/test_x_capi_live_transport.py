"""Production controls with generated credentials and no network access."""
import os
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from axonos_gate import x_capi, x_capi_worker as worker
from axonos_gate.tests import test_x_capi as fixtures


def production_env():
    env = fixtures.ConfigTests.enabled_env('live',
        X_CAPI_PRODUCTION_TRANSPORT='pixel-token-v1',
        X_CAPI_CONSENT_POLICY_VERSION='x-capi-handoff-v2',
        X_CAPI_ALLOWED_ORIGIN='https://app.axonos.io')
    return {k: v for k, v in env.items() if not k.startswith('X_CAPI_ALLOW_TEST_')
            and k != 'X_CAPI_CONTEXT_KEY'}


class ProductionControls(unittest.TestCase):
    def test_live_requires_explicit_opt_in_and_handoff_policy(self):
        env = production_env()
        self.assertTrue(x_capi.load_config(env).producer_ready)
        for extra in ({'X_CAPI_PRODUCTION_TRANSPORT': ''},
                      {'X_CAPI_PRODUCTION_TRANSPORT': 'true'},
                      {'X_CAPI_CONSENT_POLICY_VERSION': 'x-capi-v1'},
                      {'X_CAPI_ALLOWED_ORIGIN': 'https://evil.axonos.io'}):
            self.assertFalse(x_capi.load_config({**env, **extra}).producer_ready)
        self.assertFalse(x_capi.load_config({}).producer_ready)

    def test_browser_policy_is_explicit_and_cannot_mix_with_server_handoff(self):
        env = {**production_env(), 'X_CAPI_CONSENT_POLICY_VERSION': 'x-capi-browser-v1',
               'X_CAPI_TWCLID_CONTRACT_VERSION': 'browser-click-v1', 'X_HANDOFF_ENABLED': 'false'}
        self.assertTrue(x_capi.load_config(env).producer_ready)
        for changes in ({'X_HANDOFF_ENABLED': 'true'},
                        {'X_CAPI_TWCLID_CONTRACT_VERSION': 'handoff-v2-observation'},
                        {'X_CAPI_CONSENT_POLICY_VERSION': 'x-capi-v1'},
                        {'X_CAPI_ALLOWED_ORIGIN': 'https://axonos.io'},
                        {'X_CAPI_SEND_VALUES': 'true'}):
            self.assertFalse(x_capi.load_config({**env, **changes}).producer_ready)
        self.assertNotEqual(x_capi.load_config(env).audience_scope,
                            x_capi.load_config(production_env()).audience_scope)

    def test_every_test_authority_switch_blocks_real_adapter_before_secret_read(self):
        for name in ('SECRETS', 'DB_URL', 'CONFIG_GUARD_BYPASS', 'PRIVACY_FENCE',
                     'SOCKET', 'FUTURE_SWITCH'):
            with self.subTest(name=name), patch.dict(os.environ,
                    {**production_env(), 'X_CAPI_ALLOW_TEST_' + name: '1'}, clear=True), \
                    patch.object(worker, 'read_token') as secret, \
                    patch.object(worker, '_post_conversion') as network:
                self.assertFalse(x_capi.load_config().producer_ready)
                with self.assertRaises(RuntimeError):
                    worker.RequestsTransport().send('source-id', 'synthetic', {})
                secret.assert_not_called(); network.assert_not_called()

    def test_long_retry_after_and_durable_global_cooldown(self):
        result = worker.classify_response(worker.TransportResponse(429, {'Retry-After': '7200'}, b''), 100)
        self.assertEqual(result['retry_at'], 7300)
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ('rate_limited', 7300)
        self.assertTrue(worker._is_paused(conn, 7299))
        self.assertFalse(worker._is_paused(conn, 7300))
        cursor.fetchone.return_value = ('authentication_failed', 100)
        self.assertTrue(worker._is_paused(conn, 10000))

    def test_normal_worker_selects_adapter_and_fails_closed_without_token(self):
        self.assert_worker_delivery(production_env())

    def test_browser_policy_delivers_without_a_handoff_runtime(self):
        self.assert_worker_delivery({**production_env(),
            'X_CAPI_CONSENT_POLICY_VERSION': 'x-capi-browser-v1',
            'X_CAPI_TWCLID_CONTRACT_VERSION': 'browser-click-v1',
            'X_HANDOFF_ENABLED': 'false'})

    def assert_worker_delivery(self, env):
        with patch.dict(os.environ, env, clear=True), ExitStack() as stack:
            conn = MagicMock()
            stack.enter_context(patch.object(x_capi, '_db_url', return_value='synthetic'))
            stack.enter_context(patch.object(x_capi, 'get_connection', return_value=conn))
            stack.enter_context(patch.object(x_capi, '_context_cipher', return_value=(object(), None)))
            stack.enter_context(patch.object(x_capi, 'keyed_internal_hash', return_value='a' * 64))
            for name in ('_worker_db_target_is_isolated', '_schema_ready', 'verify_config_guard'):
                stack.enter_context(patch.object(worker, name, return_value=True))
            for name in ('_configure_connection', 'drain_ingest'):
                stack.enter_context(patch.object(worker, name))
            stack.enter_context(patch.object(worker, 'drain_ingest_until_empty', return_value=({}, True)))
            stack.enter_context(patch.object(worker, '_is_paused', return_value=False))
            stack.enter_context(patch.object(worker, '_handoff_runtime',
                MagicMock() if env['X_CAPI_CONSENT_POLICY_VERSION'] == 'x-capi-handoff-v2' else None))
            fence = MagicMock()
            fence.prepare.return_value = fence.consume.return_value = 'clear'
            fence.dispatch_boundary.return_value.__enter__.return_value = True
            claim = stack.enter_context(patch.object(worker, 'claim_job', return_value=None))
            with patch.object(worker, 'read_token', return_value=(None, 'absent')):
                self.assertEqual(worker.run_once(privacy_fence=fence, perform_cleanup=False), 'disabled')
                claim.assert_not_called()
            with patch.object(worker, 'read_token', return_value=('synthetic-token', None)), \
                    patch.object(worker.RequestsTransport, 'send', return_value=worker.TransportResponse(
                        200, {}, b'{"data":{"conversions_processed":1}}')) as send, \
                    patch.object(worker, 'begin_dispatch', return_value='allowed'), \
                    patch.object(worker, 'finish_job', return_value=True):
                claim.return_value = {'pixel_id': 'source-id', 'event_id': 'deposit-event',
                    'twclid': 'syntheticclick123456789', 'conversion_timestamp_ms': 1791936000000,
                    'conversion_id': '11111111-1111-4111-8111-111111111111'}
                self.assertEqual(worker.run_once(privacy_fence=fence, perform_cleanup=False), 'accepted')
                send.assert_called_once()
                self.assertEqual(send.call_args.args[2]['conversions'][0]['conversion_id'],
                                 claim.return_value['conversion_id'])


class BrowserConsentLifecycle(unittest.TestCase):
    def test_live_browser_requires_app_grant_and_keeps_durable_revocation(self):
        from cryptography.fernet import Fernet
        from time import time
        cipher = Fernet(Fernet.generate_key())
        env = {**production_env(), 'X_CAPI_CONSENT_POLICY_VERSION': 'x-capi-browser-v1',
               'X_CAPI_TWCLID_CONTRACT_VERSION': 'browser-click-v1', 'X_HANDOFF_ENABLED': 'false'}
        click = 'syntheticclick1234567890'
        with patch.dict(os.environ, env, clear=True), \
                patch.object(x_capi, '_context_ciphers', return_value=((cipher,), None)), \
                patch.object(x_capi, 'config_guard_current', return_value=True), \
                patch.object(x_capi, '_request_worker_consent', return_value={'v': 1, 'ok': True, 'state': 'revoked'}) as revoke, \
                patch.object(x_capi, '_persist_privacy_fence_status', return_value='published'):
            self.assertNotIn('context', x_capi.attribution_status())
            initial = x_capi.attribution_status(landing_twclid=click)
            ticket = x_capi.decode_context_ticket(initial['context'])
            self.assertIsNone(ticket['twclid'])
            self.assertFalse(worker._event_ticket_is_current(ticket, x_capi.load_config(), int(time()*1000), time()))
            granted, code = x_capi.update_consent(context_token=initial['context'],
                csrf_token=initial['csrf'], action='grant', landing_twclid=click, origin='https://app.axonos.io')
            self.assertEqual(code, 200)
            ticket = x_capi.decode_context_ticket(granted['context'])
            self.assertEqual(ticket['twclid'], click)
            self.assertTrue(worker._event_ticket_is_current(ticket, x_capi.load_config(), int(time()*1000)+1, time()))
            revoke.assert_not_called()
            revoked, code = x_capi.update_consent(context_token=granted['context'],
                csrf_token=granted['csrf'], action='revoke', origin='https://app.axonos.io')
            self.assertEqual(code, 200)
            self.assertEqual(revoked['state'], 'revoked')
            self.assertIsNone(x_capi.decode_context_ticket(revoked['context'])['twclid'])
            revoke.assert_called_once()
