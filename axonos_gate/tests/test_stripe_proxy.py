"""Exercise the noVNC payment transport without sockets or Stripe credentials."""

import ast
import configparser
import http.client
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]


class StripeProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use the actual handler implementation without websockify's system
        # package, import-time servers, or live deployment configuration.
        cls.tree = ast.parse((ROOT / 'axonos_gate/websockify_gate.py').read_text())
        node = next(n for n in ast.walk(cls.tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_proxy_stripe_request')
        namespace = {'urlsplit': urlsplit, 'http': http, 'json': json, 'os': os, 'time': time}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<payment proxy>', 'exec'), namespace)
        cls.proxy = staticmethod(namespace['_proxy_stripe_request'])

    def handler(self, path='/api/payments/stripe/webhook', body=b'{ "id": "evt_fixture" }\n'):
        handler = SimpleNamespace(
            path=path,
            headers={
                'Content-Type': 'application/json; charset=utf-8',
                'Content-Length': str(len(body)),
                'Stripe-Signature': 't=123,v1=signature-fixture',
                'X-AXGT-Auth-Token': 'wallet-proof-fixture',
                'Host': 'app.example.test',
                'Origin': 'https://app.example.test',
            },
            connection=MagicMock(), rfile=io.BytesIO(body),
            _send_json=MagicMock(side_effect=lambda status, data, **kwargs: (status, data)),
        )
        handler.connection.gettimeout.return_value = None
        return handler

    def upstream(self, status=200, body=b'{"ok":true}'):
        connection = MagicMock()
        connection.getresponse.return_value = SimpleNamespace(status=status, read=lambda size: body)
        return connection

    def test_webhook_forwards_exact_signed_bytes_to_fixed_loopback(self):
        raw = b'{ "data": {"object": {}}, "id":"evt_fixture" }\n'
        handler = self.handler(body=raw)
        handler.headers['Authorization'] = 'must-not-forward'
        connection = self.upstream()
        with patch.dict(os.environ, {'GATE_PORT': '8889'}), patch.object(
            http.client, 'HTTPConnection', return_value=connection
        ) as constructor:
            result = self.proxy(handler, 'POST')
        self.assertEqual(result, (200, {'ok': True}))
        constructor.assert_called_once_with('127.0.0.1', 8889, timeout=30)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('POST', '/api/payments/stripe/webhook'))
        self.assertEqual(kwargs['body'], raw)
        self.assertEqual(kwargs['headers']['Stripe-Signature'], handler.headers['Stripe-Signature'])
        self.assertEqual(kwargs['headers']['X-AXGT-Auth-Token'], 'wallet-proof-fixture')
        self.assertNotIn('Authorization', kwargs['headers'])
        self.assertEqual(handler.connection.settimeout.call_args_list[-1].args, (None,))
        connection.close.assert_called_once()

    def test_status_preserves_payment_query_and_backend_authorization_failure(self):
        handler = self.handler('/api/payments/stripe/status?payment_id=fixture')
        connection = self.upstream(403, b'{"error":"Authentication required"}')
        with patch.object(http.client, 'HTTPConnection', return_value=connection):
            result = self.proxy(handler, 'GET')
        self.assertEqual(result[0], 403)
        self.assertEqual(connection.request.call_args.args, (
            'GET', '/api/payments/stripe/status?payment_id=fixture'))
        self.assertIsNone(connection.request.call_args.kwargs['body'])

    def test_checkout_forwards_amount_without_wallet_authority(self):
        handler = self.handler('/api/payments/stripe/checkout', b'{"amount_usd":"50.00"}')
        connection = self.upstream(200, b'{"checkout_url":"https://checkout.stripe.com/fixture","payment_id":"fixture"}')
        with patch.object(http.client, 'HTTPConnection', return_value=connection):
            result = self.proxy(handler, 'POST')
        self.assertEqual(result[0], 200)
        self.assertEqual(json.loads(connection.request.call_args.kwargs['body']), {'amount_usd': '50.00'})

    def test_only_exact_routes_and_methods_reach_gate(self):
        cases = (
            ('/api/payments/stripe/webhook/extra', 'POST', 404),
            ('/api/payments/stripe/webhook;alias', 'POST', 404),
            ('/api/payments/stripe/checkout', 'GET', 405),
            ('/api/payments/stripe/status', 'POST', 405),
        )
        for path, method, expected in cases:
            with self.subTest(path=path), patch.object(http.client, 'HTTPConnection') as constructor:
                self.assertEqual(self.proxy(self.handler(path), method)[0], expected)
                constructor.assert_not_called()

    def test_oversized_unframed_malformed_and_incomplete_bodies_rejected(self):
        cases = (
            ('Content-Length', '262145', 413),
            ('Content-Length', '-1', 400),
            ('Content-Length', 'not-an-int', 400),
            ('Content-Length', '500', 400),
            ('Transfer-Encoding', 'chunked', 400),
            ('Content-Type', 'text/plain', 400),
        )
        for name, value, expected in cases:
            handler = self.handler()
            handler.headers[name] = value
            with self.subTest(name=name, value=value), patch.object(http.client, 'HTTPConnection') as constructor:
                self.assertEqual(self.proxy(handler, 'POST')[0], expected)
                constructor.assert_not_called()
        handler = self.handler('/api/payments/stripe/checkout')
        handler.headers['Content-Length'] = '4097'
        self.assertEqual(self.proxy(handler, 'POST')[0], 413)

    def test_transport_failure_never_returns_exception_details(self):
        handler = self.handler()
        with patch.object(http.client, 'HTTPConnection', side_effect=OSError('sensitive diagnostic')):
            result = self.proxy(handler, 'POST')
        self.assertEqual(result, (503, {'error': 'Card payments temporarily unavailable'}))

    def test_slow_trickle_cannot_reset_total_body_deadline(self):
        handler = self.handler()
        handler.rfile = MagicMock()
        handler.rfile.read1.return_value = b' '
        with patch.object(time, 'monotonic', side_effect=[100.0, 100.0, 104.0, 106.0]), patch.object(
            http.client, 'HTTPConnection'
        ) as constructor:
            result = self.proxy(handler, 'POST')
        self.assertEqual(result[0], 400)
        constructor.assert_not_called()
        self.assertEqual(handler.rfile.read1.call_count, 2)
        self.assertEqual([call.args for call in handler.connection.settimeout.call_args_list],
                         [(5.0,), (1.0,), (None,)])

    def test_backend_response_size_and_json_are_bounded(self):
        for body in (b'x' * 65537, b'not-json', b'[]'):
            connection = self.upstream(body=body)
            with self.subTest(size=len(body)), patch.object(http.client, 'HTTPConnection', return_value=connection):
                self.assertEqual(self.proxy(self.handler(), 'POST')[0], 503)
            connection.close.assert_called_once()

    def test_config_uses_shared_public_configuration(self):
        get_route = next(n for n in ast.walk(self.tree)
                         if isinstance(n, ast.FunctionDef) and n.name == 'do_GET')
        self.assertIn('stripe_payments.public_config()', ast.unparse(get_route))
        self.assertIn("{'card_payments_enabled': False}", ast.unparse(get_route))

    def test_payment_handlers_only_delegate_transport_and_shared_config(self):
        proxy = next(n for n in ast.walk(self.tree)
                     if isinstance(n, ast.FunctionDef) and n.name == '_proxy_stripe_request')
        source = ast.unparse(proxy)
        for forbidden in ('deposit_ledger', 'credit_deposit', 'construct_event',
                          '_is_auth_token_valid', 'stripe.checkout', 'usd_per_hour'):
            self.assertNotIn(forbidden, source)
        delegated = [n.attr for n in ast.walk(self.tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                     and n.value.id == 'stripe_payments']
        self.assertEqual(delegated, ['public_config'])


class StripeCredentialBoundaryTests(unittest.TestCase):
    def test_startup_refuses_secrets_before_any_shared_tenant_or_ssh_service(self):
        startup = (ROOT / 'startup.sh').read_text()
        sanitizer = startup.split('# Stripe credentials belong only', 1)[1].split('# Set hostname at runtime', 1)[0]
        with tempfile.TemporaryDirectory(prefix='axonos-stripe-startup-') as folder:
            script = Path(folder) / 'startup-check.sh'
            script.write_text('#!/bin/bash\n# Stripe credentials belong only' + sanitizer
                              + 'echo unsafe-services-started\n')
            modes = (('', '', ''), ('false', '', ''), ('true', 'session-fixture', ''),
                     ('TRUE', 'session-fixture', ''), ('true', '', 'true'), ('true', '', 'ON'))
            for mode, session, ssh in modes:
                for secret_name in ('STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET'):
                    # Docker healthchecks/exec get this original environment,
                    # independent of any later unset/re-exec in startup.sh.
                    docker_environment = {'PATH': os.defpath, 'AXGT_USER_CONTAINER_ENABLED': mode,
                                          'AXGT_SESSION_ID': session, 'AXGT_SSH_ENABLED': ssh,
                                          secret_name: 'synthetic-must-not-log'}
                    with self.subTest(mode=mode, session=session, ssh=ssh, secret=secret_name):
                        result = subprocess.run(['/bin/bash', str(script)], env=docker_environment,
                                                capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, 1)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('Refusing unsafe Stripe configuration', result.stderr)
                        self.assertNotIn('synthetic-must-not-log', result.stderr)
                        self.assertEqual(docker_environment[secret_name], 'synthetic-must-not-log')

    def test_crypto_only_shared_and_tenant_startup_accepts_absent_or_empty_credentials(self):
        startup = (ROOT / 'startup.sh').read_text()
        sanitizer = startup.split('# Stripe credentials belong only', 1)[1].split('# Set hostname at runtime', 1)[0]
        for credentials in ({}, {'STRIPE_SECRET_KEY': '', 'STRIPE_WEBHOOK_SECRET': ''}):
            for mode, session in (('false', ''), ('true', 'session-fixture')):
                environment = {'PATH': os.defpath, 'AXGT_USER_CONTAINER_ENABLED': mode,
                               'AXGT_SESSION_ID': session, 'AXGT_CARD_GATE_ONLY': 'true', **credentials}
                with self.subTest(mode=mode, credentials=credentials):
                    result = subprocess.run(['/bin/bash', '-c', '# Stripe credentials belong only'
                                             + sanitizer + 'test "$AXGT_CARD_GATE_ONLY" = false && echo services-allowed'], env=environment,
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'services-allowed')

    def test_startup_preserves_secrets_only_for_central_multiuser_gate(self):
        startup = (ROOT / 'startup.sh').read_text()
        sanitizer = startup.split('# Stripe credentials belong only', 1)[1].split('# Set hostname at runtime', 1)[0]
        check = ('test "$STRIPE_SECRET_KEY" = synthetic-secret && '
                 'test "$STRIPE_WEBHOOK_SECRET" = synthetic-signing-secret && '
                 'test "$AXGT_CARD_GATE_ONLY" = true')
        environment = {'PATH': os.defpath, 'AXGT_USER_CONTAINER_ENABLED': 'TrUe',
                       'AXGT_CARD_GATE_ONLY': 'false',
                       'STRIPE_SECRET_KEY': 'synthetic-secret', 'STRIPE_WEBHOOK_SECRET': 'synthetic-signing-secret'}
        result = subprocess.run(['/bin/bash', '-c', '# Stripe credentials belong only' + sanitizer + check],
                                env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_supervisor_clears_secrets_for_every_nonpayment_program(self):
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read(ROOT / 'supervisord.conf')
        for section in config.sections():
            if not section.startswith('program:') or section in ('program:novnc', 'program:axgt-api'):
                continue
            with self.subTest(program=section):
                environment = config[section].get('environment', '')
                self.assertIn('STRIPE_SECRET_KEY=""', environment)
                self.assertIn('STRIPE_WEBHOOK_SECRET=""', environment)

    def test_card_gate_blocks_user_services_before_their_command_executes(self):
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read(ROOT / 'supervisord.conf')
        for program in ('jupyterlab', 'opencode', 'ollama', 'ipfs'):
            command = shlex.split(config['program:' + program]['command'])
            self.assertEqual(command[0], '/bin/bash')
            shell = command[-1]
            # Exercise each real entry guard, substituting the two executable
            # branches to avoid starting production daemons or sleeping.
            guard, service = shell.split('exec sleep infinity;', 1)
            self.assertTrue(guard.startswith('if [ "${AXGT_CARD_GATE_ONLY:-false}" = "true" ]'))
            self.assertTrue(service.startswith((' else', ' fi;')))
            probe = guard + 'printf service-disabled; else printf service-enabled; fi'
            for marker, expected in (('true', 'service-disabled'), ('false', 'service-enabled')):
                with self.subTest(program=program, marker=marker):
                    result = subprocess.run(['/bin/bash', '-c', probe],
                                            env={'PATH': os.defpath, 'AXGT_CARD_GATE_ONLY': marker},
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)

    def test_launcher_cannot_forward_payment_credentials_to_tenants(self):
        from axonos_gate import session_launcher_service as launcher
        with patch.dict(os.environ, {
            'AXGT_HOST_SESSION_ENV_PASSTHROUGH': 'STRIPE_SECRET_KEY,STRIPE_WEBHOOK_SECRET,WEBRTC_CAPTURE_FPS'
        }):
            self.assertEqual(launcher._env_passthrough_names(), ['WEBRTC_CAPTURE_FPS'])
        self.assertTrue({'STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET'} <= launcher._FORBIDDEN_SESSION_ENV_NAMES)

    def test_compose_launcher_erases_secrets_from_shared_env_file(self):
        compose = (ROOT / 'docker-compose.yml').read_text()
        launcher = compose.split('  axonos-launcher:', 1)[1].split('  coturn:', 1)[0]
        self.assertIn('STRIPE_SECRET_KEY: ""', launcher)
        self.assertIn('STRIPE_WEBHOOK_SECRET: ""', launcher)


if __name__ == '__main__':
    unittest.main()
