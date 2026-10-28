"""Synthetic adapter and private loopback listener conformance; no X traffic."""
import ast
import base64
from email.message import Message
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'axonos_gate'))
from axonos_gate import x_capi_handoff_http as http
from axonos_gate import x_capi_handoff as protocol

NOW = 1791936000000
OWNER = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')
HANDOFF = base64.urlsafe_b64encode(bytes(range(32, 64))).decode().rstrip('=')
ID = '11111111-1111-4111-8111-111111111111'
BODY = {'v': 2, 'request_id': ID, 'choice': 'grant', 'policy': 'app-x-v2'}
PREPARED = {'v': 2, 'ok': True, 'state': 'prepared', 'expires_at_ms': NOW + 600000}
REDEEMED = {'v': 2, 'ok': True, 'state': 'redeemed', 'context': 'x' * 64, 'csrf': 'y' * 43,
            'ownership_id': ID, 'expires_at_ms': NOW + 600000}


def headers(body=b'', form=False, cookie=OWNER):
    result = {'Host': 'app.axonos.io', 'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json',
              'Content-Length': str(len(body)), 'Origin': http.LANDING_ORIGIN if form else http.APP_ORIGIN,
              'Sec-Fetch-Site': 'same-site' if form else 'same-origin',
              'Sec-Fetch-Mode': 'navigate' if form else 'cors', 'Sec-Fetch-Dest': 'document' if form else 'empty'}
    if not form:
        result['X-AxonOS-Handoff'] = '2'
    if cookie:
        result['Cookie'] = http.APP_COOKIE + '=' + cookie
    return result


class HandoffHttpTests(unittest.TestCase):
    def setUp(self):
        enabled = patch.dict(os.environ, {'X_HANDOFF_ENABLED':'true','X_CAPI_MODE':'dry_run'})
        enabled.start(); self.addCleanup(enabled.stop)
        self.rpc = Mock(return_value={'status': 200, 'body': PREPARED, 'headers': {}})

    def request(self, op='prepare', payload=None, changes=None, cookie=OWNER, raw=None):
        body = raw if raw is not None else json.dumps(BODY if payload is None else payload).encode()
        hdr = headers(body, cookie=cookie)
        for name, value in (changes or {}).items():
            if value is None:
                hdr.pop(name, None)
            else:
                hdr[name] = value
        return http.handle('POST', http.PREFIX + op, hdr, body, https=True, call=self.rpc, now_ms=NOW)

    def test_query_log_sanitizer_never_treats_quotes_or_angles_as_boundaries(self):
        for delimiter in ("'", '"', '<', '>'):
            with self.subTest(delimiter=delimiter):
                line = 'GET /synthetic-diagnostic?first='+delimiter+'SYNTHETIC_PRIVATE_F3&second=PRIVATE_TAIL HTTP/1.1'
                safe = http.privacy_safe_http_log(line)
                self.assertEqual(safe, 'GET /synthetic-diagnostic?[query-redacted] HTTP/1.1')
                self.assertNotIn('SYNTHETIC_PRIVATE_F3', safe)
                self.assertNotIn('PRIVATE_TAIL', safe)
        self.assertEqual(http.privacy_safe_http_log('GET /synthetic-diagnostic HTTP/1.1'),
                         'GET /synthetic-diagnostic HTTP/1.1')
        self.assertEqual(http.privacy_safe_http_log(404), 404)

    def test_off_mode_blocks_positive_without_worker_but_keeps_withdrawal(self):
        with patch.dict(os.environ, {'X_CAPI_MODE':'off'}):
            result = self.request()
            self.assertEqual(result.status, 503)
            self.assertEqual(json.loads(result.body)['error'], 'ATTRIBUTION_DISABLED')
            self.rpc.assert_not_called()
            self.rpc.return_value={'status':200,'body':{'v':2,'ok':True,'state':'withdrawn','request_id':ID},'headers':{}}
            result=self.request('withdraw',{'v':2,'request_id':ID,'reason':'withdrawn'})
            self.assertEqual(result.status,200)
            self.assertEqual(self.rpc.call_args.args[0]['op'],'withdraw')

    def test_prepare_first_issue_cookie_only_after_committed_success(self):
        with patch.object(protocol, 'new_token', return_value=OWNER):
            result = self.request(cookie=None)
        self.assertEqual(result.status, 200)
        self.assertEqual(json.loads(result.body), PREPARED)
        cookie = result.headers['Set-Cookie']
        self.assertIn(http.APP_COOKIE + '=' + OWNER, cookie)
        for attribute in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/', 'Max-Age=600'):
            self.assertIn(attribute, cookie)
        self.assertNotIn('Domain=', cookie)
        frame = self.rpc.call_args.args[0]
        self.assertEqual(frame['owner_hash'], protocol.token_hash(OWNER, 'app-owner'))
        self.assertTrue(frame['new_owner'])
        self.assertNotIn(OWNER, repr(frame))

    def test_prepare_existing_cookie_never_rotates_or_reissues(self):
        result = self.request()
        self.assertEqual(result.status, 200)
        self.assertNotIn('Set-Cookie', result.headers)
        self.assertFalse(self.rpc.call_args.args[0]['new_owner'])

    def test_lost_prepare_does_not_return_cookie_or_new_success(self):
        error = protocol.HandoffError('RESULT_UNAVAILABLE')
        self.rpc.return_value = {'status': error.status, 'body': error.body(), 'headers': {}}
        result = self.request(cookie=None)
        self.assertEqual(result.status, 409)
        self.assertNotIn('Set-Cookie', result.headers)
        self.assertEqual(self.rpc.call_count, 1)

    def test_wrong_sibling_null_missing_origin_and_metadata_never_rpc(self):
        cases = [('Origin', 'https://evil.axonos.io'), ('Origin', 'null'), ('Origin', None),
                 ('Origin', http.APP_ORIGIN + ', https://evil.axonos.io'),
                 ('Sec-Fetch-Site', 'same-site'), ('Sec-Fetch-Mode', 'navigate'),
                 ('Sec-Fetch-Dest', 'document'), ('X-AxonOS-Handoff', None)]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                self.rpc.reset_mock()
                result = self.request(changes={name: value})
                self.assertEqual(result.status, 403)
                self.rpc.assert_not_called()

    def test_body_unknown_fields_duplicate_keys_and_noninteger_are_rejected(self):
        raws = [b'{"v":2,"v":2}', json.dumps(dict(BODY, owner_hash='a'*64)).encode(),
                json.dumps(dict(BODY, v=2.0)).encode(), json.dumps(dict(BODY, redirect='https://evil.axonos.io')).encode()]
        for raw in raws:
            with self.subTest(raw=raw):
                result = self.request(raw=raw)
                self.assertEqual(result.status, 400)
        self.rpc.assert_not_called()

    def test_duplicate_owner_cookie_rejected(self):
        result = self.request(changes={'Cookie': f'{http.APP_COOKIE}={OWNER}; {http.APP_COOKIE}={OWNER}'})
        self.assertEqual(result.status, 403)
        self.rpc.assert_not_called()

    def test_duplicate_origin_headers_rejected(self):
        raw = json.dumps(BODY).encode()
        repeated = Message()
        for key, value in headers(raw).items(): repeated[key] = value
        repeated['Origin'] = http.APP_ORIGIN
        result = http.handle('POST', http.PREFIX + 'prepare', repeated, raw, https=True, call=self.rpc)
        self.assertEqual(result.status, 400)
        self.rpc.assert_not_called()

    def test_blocked_cookie_prevents_begin(self):
        body = dict(BODY, destination='desktop')
        result = self.request('begin', body, cookie=None)
        self.assertEqual(result.status, 403)
        self.rpc.assert_not_called()

    def test_gpc_closes_existing_owner_but_never_prepares(self):
        self.rpc.return_value = {'status': 200, 'body': {'v': 2, 'ok': True, 'state': 'withdrawn', 'request_id': ID}, 'headers': {}}
        result = self.request(changes={'Sec-GPC': '1'})
        self.assertEqual(result.status, 403)
        frame = self.rpc.call_args.args[0]
        self.assertEqual(frame['op'], 'withdraw')
        self.assertEqual(frame['payload']['reason'], 'gpc')
        self.assertNotIn('Set-Cookie', result.headers)

    def test_gpc_closure_precedes_body_timeout_without_positive_retry(self):
        self.rpc.return_value = {'status': 200, 'body': {'v': 2, 'ok': True, 'state': 'withdrawn', 'request_id': ID}, 'headers': {}}
        raw = json.dumps(BODY).encode()
        hdr = dict(headers(raw), **{'Sec-GPC': '1'})
        def unavailable_body(length):
            self.rpc.assert_called_once()
            self.assertEqual(self.rpc.call_args.args[0]['op'], 'withdraw')
            raise TimeoutError('synthetic body timeout')
        reply = http.handle('POST', http.PREFIX+'prepare', hdr, https=True,
                            call=self.rpc, read_body=unavailable_body)
        self.assertEqual(reply.status, 400)
        self.assertEqual(json.loads(reply.body)['error'], 'INVALID_REQUEST')
        self.rpc.assert_called_once()
        self.assertNotIn('Set-Cookie', reply.headers)

    def test_gpc_without_cookie_has_no_positive_or_new_owner_work(self):
        result = self.request(changes={'Sec-GPC': '1'}, cookie=None)
        self.assertEqual(result.status, 403)
        self.rpc.assert_not_called()

    def test_bounds_chunked_media_and_plain_http_rejected(self):
        for changes, expected in [({'Content-Length': '8193'}, 413), ({'Transfer-Encoding': 'chunked'}, 400),
                                  ({'Content-Type': 'text/plain'}, 415), ({'Host': 'evil.axonos.io'}, 403)]:
            with self.subTest(changes=changes):
                self.assertEqual(self.request(changes=changes).status, expected)
        self.assertEqual(http.handle('POST', http.PREFIX+'prepare', headers(b'{}'), b'{}', https=False, call=self.rpc).status, 403)
        self.rpc.assert_not_called()

    def test_callback_exact_two_fields_peer_origin_and_cookie(self):
        self.rpc.return_value = {'status': 200, 'body': REDEEMED, 'headers': {}}
        raw = ('v=2&handoff=' + HANDOFF).encode()
        result = http.handle('POST', http.CALLBACK, headers(raw, form=True), raw, https=True, call=self.rpc, now_ms=NOW)
        self.assertEqual(result.status, 200)
        self.assertEqual(self.rpc.call_args.args[0]['payload'], {'v': '2', 'handoff': HANDOFF})
        self.assertIn('Set-Cookie', result.headers)
        self.assertEqual(result.headers['Referrer-Policy'], 'strict-origin')
        self.assertIn("form-action 'none'", result.headers['Content-Security-Policy'])
        self.assertNotIn(HANDOFF.encode(), result.body)
        for bad in (raw+b'&x=y', raw+b'&handoff='+HANDOFF.encode(), raw.replace(b'v=2', b'v=02'), b'v=2&handoff=%ZZ'):
            self.rpc.reset_mock()
            response = http.handle('POST', http.CALLBACK, headers(bad, form=True), bad, https=True, call=self.rpc)
            self.assertEqual(response.status, 400)
            self.rpc.assert_not_called()

    def test_form_sibling_or_same_origin_is_not_landing(self):
        raw = ('v=2&handoff='+HANDOFF).encode()
        for origin in (http.APP_ORIGIN, 'https://evil.axonos.io', 'null', ''):
            hdr = headers(raw, form=True); hdr['Origin'] = origin
            self.assertEqual(http.handle('POST', http.CALLBACK, hdr, raw, https=True, call=self.rpc).status, 403)
        self.rpc.assert_not_called()

    def test_no_query_or_arbitrary_handoff_forwarding(self):
        hdr = {'Host': 'app.axonos.io', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Site': 'same-site'}
        for query in ('?handoff='+HANDOFF, '?redirect=https://evil.axonos.io', '?twclid=SYNTHETIC_CLICK'):
            result = http.handle('GET', http.ENTRY+query, hdr, https=True, call=self.rpc)
            self.assertEqual(result.status, 400)
            self.assertNotIn(HANDOFF.encode(), result.body)
        self.rpc.assert_not_called()

    def test_entry_header_gpc_renders_only_post_suppression_even_when_off(self):
        hdr = {'Host': 'app.axonos.io', 'Sec-Fetch-Mode': 'navigate',
               'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Site': 'cross-site', 'Sec-GPC': '1'}
        for mode in ('dry_run', 'off'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'X_CAPI_MODE': mode}):
                result = http.handle('GET', http.ENTRY, hdr, https=True, call=self.rpc)
                self.assertEqual(result.status, 200)
                self.assertNotIn('Set-Cookie', result.headers)
                self.assertIn(b'"kind":"suppressed"', result.body)
                self.assertIn(b'"server_gpc":true', result.body)
                self.assertNotIn(b'<button id=grant', result.body)
                self.assertNotIn(b'<button id=deny', result.body)
                self.assertIn("form-action 'none'", result.headers['Content-Security-Policy'])
                self.assertEqual(result.headers['Referrer-Policy'], 'strict-origin')
                self.rpc.assert_not_called()

    def test_entry_cookie_free_has_strict_csp_and_clean_navigation(self):
        hdr = {'Host': 'app.axonos.io', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Site': 'cross-site'}
        result = http.handle('GET', http.ENTRY, hdr, https=True, call=self.rpc)
        self.assertEqual(result.status, 200)
        self.assertNotIn('Set-Cookie', result.headers)
        self.assertIn('script-src \'nonce-', result.headers['Content-Security-Policy'])
        self.assertIn('form-action https://axonos.io/attribution/authorize', result.headers['Content-Security-Policy'])
        self.assertEqual(result.headers['Cache-Control'], 'no-store, private')
        self.assertFalse(any(key.lower().startswith('access-control') for key in result.headers))
        self.rpc.assert_not_called()

    def test_backend_errors_never_echo_input_and_make_no_retry(self):
        self.rpc.side_effect = RuntimeError('private marker '+OWNER)
        result = self.request()
        self.assertEqual(result.status, 503)
        self.assertNotIn(OWNER.encode(), result.body)
        self.assertEqual(self.rpc.call_count, 1)

    def test_trusted_positive_owner_from_cookie_and_provenance_only(self):
        hdr = headers()
        expected = protocol.token_hash(OWNER, 'app-owner')
        self.assertEqual(http.trusted_browser_owner_hash(hdr), expected)
        hdr.pop('Origin')
        self.assertIsNone(http.trusted_browser_owner_hash(hdr, 'POST'))
        self.assertEqual(http.trusted_browser_owner_hash(hdr, 'GET'), expected)
        for name, value in [('Origin', 'https://evil.axonos.io'), ('Sec-Fetch-Site', 'same-site'),
                            ('Sec-GPC', '1'), ('Cookie', ''), ('Cookie', http.APP_COOKIE+'='+OWNER+'; '+http.APP_COOKIE+'='+OWNER)]:
            with self.subTest(name=name, value=value):
                self.assertIsNone(http.trusted_browser_owner_hash(dict(hdr, **{name:value})))
        forged = {'X-Browser-Owner-Hash': expected, 'browser_owner_hash': expected}
        self.assertIsNone(http.trusted_browser_owner_hash(forged))

    def test_service_rejects_browser_origin_and_metadata_without_rpc(self):
        raw = b'{}'
        for key in ('Origin', 'Sec-Fetch-Site', 'Sec-Fetch-Mode', 'Sec-Fetch-Dest', 'Cookie', 'Authorization'):
            hdr = {'Host':'app.axonos.io','Content-Length':'2','Content-Type':'application/json',key:'same-origin'}
            result = http.handle('POST', http.SOURCE, hdr, raw, https=True, call=self.rpc)
            self.assertEqual(result.status, 403)
        self.rpc.assert_not_called()

    def test_service_exact_bytes_response_mac_and_no_cors(self):
        fixture=json.loads((ROOT/'contracts/x-capi-handoff/v2/vectors/mac.json').read_text())
        golden=json.loads((ROOT/'contracts/x-capi-handoff/v2/fixtures/golden.json').read_text())
        case=next(case for case in golden['schema_cases'] if case['schema']=='schemas/source-revoke.schema.json' and case['expected_valid'])
        raw=protocol.canonical_json(case['value'])
        response={'v':2,'ok':True,'epoch':42,'generation':ID,'request_id':case['value']['request_id'],'state':'withdrawn'}
        reply_headers={'X-AxonOS-Handoff-Key-Id':'test-1','X-AxonOS-Handoff-MAC':HANDOFF}
        self.rpc.return_value={'status':200,'body':response,'headers':reply_headers}
        hdr={'Host':'app.axonos.io','Content-Length':str(len(raw)),'Content-Type':'application/json',
             'X-AxonOS-Handoff-Key-Id':'test-1','X-AxonOS-Handoff-MAC':HANDOFF}
        result=http.handle('POST',http.SOURCE,hdr,raw,https=True,call=self.rpc)
        self.assertEqual(result.status,200)
        self.assertEqual(self.rpc.call_args.args[0]['raw_body'],raw.decode())
        self.assertEqual(result.headers['X-AxonOS-Handoff-MAC'],HANDOFF)
        self.assertFalse(any(name.lower().startswith('access-control') for name in result.headers))


class ListenerParityTests(unittest.TestCase):
    def setUp(self):
        enabled = patch.dict(os.environ, {'X_HANDOFF_ENABLED':'true','X_CAPI_MODE':'dry_run'})
        enabled.start(); self.addCleanup(enabled.stop)

    @classmethod
    def setUpClass(cls):
        import gate_server
        cls.gate=gate_server
        source=(ROOT/'axonos_gate/websockify_gate.py').read_text()
        tree=ast.parse(source)
        nodes=[node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='_handle_handoff_request']
        cls.namespace={'handoff_http':http,'urlsplit':__import__('urllib.parse',fromlist=['urlsplit']).urlsplit,
                       'time':time,'request_is_effectively_https':lambda *args:True}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'websockify_gate.py','exec'),cls.namespace)

    def websock(self,path,raw,hdr):
        captured={}; output=io.BytesIO()
        handler=SimpleNamespace(path=path,headers=hdr,rfile=io.BytesIO(raw),wfile=output,client_address=('127.0.0.1',1),
            send_response=lambda code:captured.update(status=code),
            send_header=lambda key,value:captured.setdefault('headers',{}).update({key:value}),end_headers=lambda:None)
        self.namespace['_handle_handoff_request'](handler,'POST')
        return captured['status'],json.loads(output.getvalue()),captured['headers']

    def test_real_flask_route_and_extracted_6080_dispatch_match(self):
        raw=json.dumps(BODY).encode(); hdr=headers(raw)
        with patch.object(http.rpc,'request',return_value={'status':200,'body':PREPARED,'headers':{}}), \
             patch.dict(os.environ,{'GATE_AGENT_ONLY':'false','GATE_AGENT_API_ENABLED':'false'}):
            response=self.gate.app.test_client(use_cookies=False).post(http.PREFIX+'prepare',data=raw,headers=hdr,base_url=http.APP_ORIGIN)
            status,body,reply_headers=self.websock(http.PREFIX+'prepare',raw,hdr)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.status_code,status)
        self.assertEqual(response.get_json(),body)
        for key in ('Cache-Control','Referrer-Policy','Content-Type'):
            self.assertEqual(response.headers[key],reply_headers[key])
        self.assertNotIn('Access-Control-Allow-Origin',response.headers)
        self.assertNotIn('Access-Control-Allow-Origin',reply_headers)

    def test_flask_missing_owner_does_not_get_legacy_cors(self):
        raw=json.dumps(dict(BODY,destination='desktop')).encode(); hdr=headers(raw,cookie=None)
        with patch.object(http.rpc,'request') as worker, patch.dict(os.environ,{'GATE_AGENT_ONLY':'false','GATE_AGENT_API_ENABLED':'false'}):
            response=self.gate.app.test_client(use_cookies=False).post(http.PREFIX+'begin',data=raw,headers=hdr,base_url=http.APP_ORIGIN)
            worker.assert_not_called()
        self.assertEqual(response.status_code,403)
        self.assertNotIn('Access-Control-Allow-Origin',response.headers)



class ActualListenerRegressionTests(unittest.TestCase):
    """Exercise the real gevent WSGI and Websockify parser/dispatch/access log.

    Dependencies must already be installed or explicitly supplied on PYTHONPATH.
    Both listeners bind private ephemeral loopback ports. Only worker RPC is
    mocked; production forwarding trust is exercised with a synthetic proxy.
    """
    @classmethod
    def setUpClass(cls):
        import importlib.util
        for module in ('gevent', 'geventwebsocket', 'websockify'):
            if importlib.util.find_spec(module) is None:
                raise unittest.SkipTest('actual listeners require installed ' + module)
        import gate_server
        import websockify_gate
        cls.gate, cls.websock = gate_server, websockify_gate

    def setUp(self):
        import logging
        import queue
        import threading
        from http.server import ThreadingHTTPServer
        enabled = patch.dict(os.environ, {
            'X_HANDOFF_ENABLED': 'true', 'X_CAPI_MODE': 'dry_run',
            'GATE_AGENT_ONLY': 'false', 'GATE_AGENT_API_ENABLED': 'false',
            'X_CAPI_TRUSTED_PROXY_HOPS': '1', 'X_CAPI_TRUSTED_PROXY_CIDRS': '127.0.0.1/32',
        })
        enabled.start(); self.addCleanup(enabled.stop)
        self.worker = Mock(side_effect=self.worker_response)
        worker_patch = patch.object(http.rpc, 'request', self.worker)
        worker_patch.start(); self.addCleanup(worker_patch.stop)
        self.logs = io.StringIO()
        self.gevent_logs = io.StringIO()
        logger = logging.Logger('synthetic-handoff-listener', logging.INFO)
        logger.addHandler(logging.StreamHandler(self.logs))
        self.ws_server = ThreadingHTTPServer(('127.0.0.1', 0), self.websock.AxonOSProxyRequestHandler)
        self.ws_server.verbose = True
        self.ws_server.logger = logger
        self.ws_thread = threading.Thread(target=self.ws_server.serve_forever, daemon=True)
        self.ws_thread.start()
        self.addCleanup(self.close_websock)
        ready = queue.Queue()
        self.stop_wsgi = threading.Event()
        def wsgi_run():
            import gevent
            from gevent import pywsgi
            from geventwebsocket.handler import WebSocketHandler
            handler = self.gate._deadline_websocket_handler(WebSocketHandler, gevent)
            server = pywsgi.WSGIServer(('127.0.0.1', 0), self.gate._application,
                                      handler_class=handler, log=self.gevent_logs, error_log=self.gevent_logs)
            server.logger = logging.Logger('synthetic-gevent-listener', logging.INFO)
            server.logger.addHandler(logging.StreamHandler(self.gevent_logs))
            server.start()
            ready.put(server.server_port)
            try:
                while not self.stop_wsgi.is_set():
                    gevent.sleep(.01)
            finally:
                server.stop(timeout=1)
        self.wsgi_thread = threading.Thread(target=wsgi_run, daemon=True)
        self.wsgi_thread.start()
        self.wsgi_port = ready.get(timeout=5)
        self.addCleanup(self.close_wsgi)

    def close_websock(self):
        self.ws_server.shutdown(); self.ws_server.server_close(); self.ws_thread.join(timeout=3)

    def close_wsgi(self):
        self.stop_wsgi.set(); self.wsgi_thread.join(timeout=3)
        self.assertFalse(self.wsgi_thread.is_alive())

    @staticmethod
    def worker_response(frame):
        if frame['op'] == 'withdraw':
            return {'status': 200, 'body': {'v': 2, 'ok': True, 'state': 'withdrawn',
                    'request_id': frame['payload']['request_id']}, 'headers': {}}
        return {'status': 200, 'body': dict(PREPARED, expires_at_ms=int(time.time()*1000)+600000), 'headers': {}}

    def raw_request(self, listener, method, target, raw=b'', changes=None):
        import http.client
        import socket
        hdr = headers(raw)
        hdr.update({'Connection': 'close', 'X-Forwarded-For': '192.0.2.10', 'X-Forwarded-Proto': 'https'})
        if method == 'GET':
            hdr.update({'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Site': 'same-site'})
        for key, value in (changes or {}).items():
            if value is None: hdr.pop(key, None)
            else: hdr[key] = value
        port = self.wsgi_port if listener == 'flask' else self.ws_server.server_port
        request = (method + ' ' + target + ' HTTP/1.1\r\n' +
                   ''.join(key + ': ' + value + '\r\n' for key, value in hdr.items()) + '\r\n').encode('ascii') + raw
        with socket.create_connection(('127.0.0.1', port), timeout=4) as connection:
            connection.sendall(request)
            response = http.client.HTTPResponse(connection, method=method)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def assert_private(self, hdr):
        self.assertEqual(hdr.get('Cache-Control'), 'no-store, private')
        self.assertIn(hdr.get('Referrer-Policy'), ('no-referrer', 'strict-origin'))
        self.assertEqual(hdr.get('X-Content-Type-Options'), 'nosniff')
        self.assertFalse(any(name.lower().startswith('access-control-') for name in hdr))
        self.assertNotIn('Set-Cookie', hdr)

    def test_r2_known_owner_gpc_closes_before_malformed_positive_json(self):
        for listener in ('flask', 'websockify'):
            for op in ('prepare', 'begin', 'recover'):
                for raw in (b'{', b'{"v":2,"v":2}'):
                    with self.subTest(listener=listener, op=op, raw=raw):
                        self.worker.reset_mock()
                        status, hdr, body = self.raw_request(listener, 'POST', http.PREFIX+op, raw, {'Sec-GPC':'1'})
                        self.assertEqual(status, 400)
                        self.assert_private(hdr)
                        self.worker.assert_called_once()
                        frame = self.worker.call_args.args[0]
                        self.assertEqual(frame['op'], 'withdraw')
                        self.assertEqual(frame['owner_hash'], protocol.token_hash(OWNER, 'app-owner'))
                        self.assertFalse(frame['new_owner'])
                        self.assertEqual(frame['payload']['reason'], 'gpc')

    def test_r2_withdraw_gpc_precedes_every_malformed_body(self):
        for listener in ('flask', 'websockify'):
            for raw in (b'{', b'{"v":2,"v":2}', b'[]'):
                with self.subTest(listener=listener, raw=raw):
                    self.worker.reset_mock()
                    status, hdr, body = self.raw_request(listener, 'POST', http.PREFIX+'withdraw', raw, {'Sec-GPC':'1'})
                    self.assertEqual(status, 400)
                    self.assert_private(hdr)
                    self.worker.assert_called_once()
                    frame = self.worker.call_args.args[0]
                    self.assertEqual(frame['op'], 'withdraw')
                    self.assertFalse(frame['new_owner'])
                    self.assertEqual(frame['owner_hash'], protocol.token_hash(OWNER, 'app-owner'))
                    self.assertEqual(frame['payload']['reason'], 'gpc')

    def test_r2_valid_gpc_withdraw_is_one_closure_and_echoes_validated_id(self):
        raw = json.dumps({'v':2, 'request_id':ID, 'reason':'withdrawn'}).encode()
        for listener in ('flask', 'websockify'):
            with self.subTest(listener=listener):
                self.worker.reset_mock()
                status, hdr, body = self.raw_request(listener, 'POST', http.PREFIX+'withdraw', raw, {'Sec-GPC':'1'})
                self.assertEqual(status, 200)
                self.assert_private(hdr)
                self.assertEqual(json.loads(body)['request_id'], ID)
                self.worker.assert_called_once()
                self.assertEqual(self.worker.call_args.args[0]['payload']['reason'], 'gpc')

    def test_r2_withdraw_gpc_failure_and_untrusted_request_are_not_acknowledged(self):
        for listener in ('flask', 'websockify'):
            for changes in ({'Cookie':None}, {'Origin':'https://evil.axonos.io'}):
                with self.subTest(listener=listener, changes=changes):
                    self.worker.reset_mock()
                    status, hdr, body = self.raw_request(listener, 'POST', http.PREFIX+'withdraw', b'{', dict(changes, **{'Sec-GPC':'1'}))
                    self.assertIn(status, (400, 403))
                    self.assert_private(hdr)
                    self.worker.assert_not_called()
            self.worker.side_effect = TimeoutError('synthetic unavailable')
            self.worker.reset_mock()
            status, hdr, body = self.raw_request(listener, 'POST', http.PREFIX+'withdraw', b'{', {'Sec-GPC':'1'})
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)['error'], 'DEPENDENCY_UNAVAILABLE')
            self.assert_private(hdr)
            self.worker.assert_called_once()
            self.worker.side_effect = self.worker_response

    def test_r2_gpc_closes_before_body_is_sent_or_framing_rejected(self):
        import socket
        for listener in ('flask', 'websockify'):
            with self.subTest(listener=listener):
                self.worker.reset_mock()
                port = self.wsgi_port if listener == 'flask' else self.ws_server.server_port
                hdr = headers(b'{')
                hdr.update({'Content-Length': '200', 'Sec-GPC': '1', 'Connection': 'close',
                            'X-Forwarded-For': '192.0.2.10', 'X-Forwarded-Proto': 'https'})
                raw = ('POST '+http.PREFIX+'prepare HTTP/1.1\r\n'+
                       ''.join(key+': '+value+'\r\n' for key,value in hdr.items())+'\r\n').encode()
                with socket.create_connection(('127.0.0.1',port),timeout=3) as connection:
                    connection.sendall(raw)  # No body: suppression cannot await it.
                    deadline=time.monotonic()+1
                    while not self.worker.called and time.monotonic()<deadline:
                        time.sleep(.005)
                    self.worker.assert_called_once()
                    self.assertEqual(self.worker.call_args.args[0]['op'],'withdraw')
                    connection.shutdown(socket.SHUT_WR)
                    while connection.recv(8192):
                        pass
                for changes, expected in (({'Content-Type':'text/plain'},415),
                        ({'Content-Length':'8193'},413), ({'Transfer-Encoding':'chunked'},400)):
                    self.worker.reset_mock()
                    status,hdr,body=self.raw_request(listener,'POST',http.PREFIX+'prepare',b'{',dict(changes,**{'Sec-GPC':'1'}))
                    self.assertEqual(status,expected)
                    self.assert_private(hdr)
                    self.worker.assert_called_once()
                    self.assertEqual(self.worker.call_args.args[0]['op'],'withdraw')

    def test_r2_gpc_closure_unavailable_is_not_acknowledged_or_positive(self):
        self.worker.side_effect = TimeoutError('synthetic unavailable')
        for listener in ('flask', 'websockify'):
            with self.subTest(listener=listener):
                self.worker.reset_mock()
                status, hdr, body = self.raw_request(listener, 'POST', http.PREFIX+'prepare', b'{', {'Sec-GPC':'1'})
                self.assertEqual(status, 503)
                self.assertEqual(json.loads(body)['error'], 'DEPENDENCY_UNAVAILABLE')
                self.assert_private(hdr)
                self.worker.assert_called_once()
                self.assertEqual(self.worker.call_args.args[0]['op'], 'withdraw')

    def test_r2_gpc_does_not_mint_owner_or_trust_sibling(self):
        for listener in ('flask', 'websockify'):
            for change, status in (({'Cookie':None},400), ({'Origin':'https://evil.axonos.io'},403)):
                with self.subTest(listener=listener, change=change):
                    self.worker.reset_mock()
                    actual, hdr, _ = self.raw_request(listener, 'POST', http.PREFIX+'prepare', b'{', dict(change, **{'Sec-GPC':'1'}))
                    self.assertEqual(actual,status); self.assert_private(hdr); self.worker.assert_not_called()

    def test_r6_rejected_query_never_reaches_websockify_access_log(self):
        marker='SYNTHETIC_PRIVATE_QUERY_R6'
        targets=(http.ENTRY, '/%61pi/x-attribution/handoff/prepare',
                 '/%2561pi/x-attribution/handoff/prepare', http.PREFIX+'%ZZ')
        for method in ('GET','POST','PUT','DELETE','HEAD','TRACE','PATCH','OPTIONS'):
            for target in targets:
                with self.subTest(method=method,target=target):
                    status,hdr,body=self.raw_request('websockify',method,target+'?twclid='+marker+'&handoff='+marker+'&H='+marker)
                    self.assertEqual(status,400)
                    self.assert_private(hdr)
        self.assertTrue(self.logs.getvalue())
        self.assertNotIn(marker,self.logs.getvalue())
        self.assertNotIn('twclid',self.logs.getvalue())
        self.worker.assert_not_called()

    def test_r6_malformed_request_line_does_not_log_query_before_path_exists(self):
        marker='SYNTHETIC_MALFORMED_QUERY_R6'
        for method,target in (('GET',http.ENTRY+'?twclid='+marker+' extra'),
                              ('GET EXTRA',http.ENTRY+'?twclid='+marker),
                              ('POST','/%61pi/x-attribution/handoff/prepare?H='+marker+' extra')):
            with self.subTest(method=method,target=target):
                status,hdr,body=self.raw_request('websockify',method,target)
                self.assertEqual(status,400)
                self.assert_private(hdr)
                self.assertNotIn(marker.encode(),body)
        self.assertTrue(self.logs.getvalue())
        self.assertNotIn(marker,self.logs.getvalue())
        self.worker.assert_not_called()

    def test_r6_gevent_and_websockify_parser_errors_hide_all_input(self):
        marker = 'SYNTHETIC_PARSER_PRIVATE_F2'
        for listener in ('flask', 'websockify'):
            for method, target in (('GET', http.ENTRY+'?H='+marker+' extra'),
                    ('GET EXTRA', http.ENTRY+'?H='+marker),
                    ('GET', '/unrelated?H='+marker+' extra')):
                with self.subTest(listener=listener, method=method, target=target):
                    status, hdr, body = self.raw_request(listener, method, target)
                    self.assertEqual(status, 400)
                    self.assert_private(hdr)
                    self.assertNotIn(marker.encode(), body)
        self.assertTrue(self.logs.getvalue())
        self.assertTrue(self.gevent_logs.getvalue())
        self.assertNotIn(marker, self.logs.getvalue())
        self.assertNotIn(marker, self.gevent_logs.getvalue())
        self.worker.assert_not_called()

    def test_r6_deeply_encoded_rejections_never_log_query(self):
        marker = 'SYNTHETIC_DEEP_PRIVATE_F2'
        for listener in ('flask', 'websockify'):
            for depth in (5, 16, 128):
                target = '/%' + '25'*depth + '61pi/x-attribution/handoff/prepare?H=' + marker
                with self.subTest(listener=listener, depth=depth):
                    status, hdr, body = self.raw_request(listener, 'POST', target, b'{}')
                    self.assertEqual(status, 400)
                    self.assert_private(hdr)
                    self.assertNotIn(marker.encode(), body)
        self.assertNotIn(marker, self.logs.getvalue())
        self.assertNotIn(marker, self.gevent_logs.getvalue())
        self.worker.assert_not_called()

    def test_r6_oversized_parser_request_line_keeps_private_transport_rejection(self):
        marker = 'SYNTHETIC_LONG_PRIVATE_F2'
        for listener in ('flask', 'websockify'):
            with self.subTest(listener=listener):
                status, hdr, body = self.raw_request(listener, 'GET', http.ENTRY+'?H='+marker*3000)
                self.assertEqual(status, 414)
                self.assert_private(hdr)
                self.assertNotIn(marker.encode(), body)
        self.assertNotIn(marker, self.logs.getvalue())
        self.assertNotIn(marker, self.gevent_logs.getvalue())
        self.worker.assert_not_called()

    def test_r6_quote_and_angle_queries_never_escape_actual_log_sinks(self):
        marker = 'SYNTHETIC_DELIMITER_PRIVATE_F3'
        for listener in ('flask', 'websockify'):
            for delimiter in ("'", '"', '<', '>'):
                targets = (
                    ('/synthetic-diagnostic?first='+delimiter+marker+'&tail='+marker, 404, False),
                    ('/synthetic-diagnostic?first='+delimiter+marker+' extra', 400, True),
                    ('/%'+'25'*128+'61pi/x-attribution/handoff/prepare?first='+delimiter+marker, 400, True),
                    (http.ENTRY+'?first='+delimiter+marker*3000, 414, True),
                )
                for target, expected, private in targets:
                    with self.subTest(listener=listener, delimiter=delimiter, expected=expected):
                        self.logs.seek(0); self.logs.truncate(0)
                        self.gevent_logs.seek(0); self.gevent_logs.truncate(0)
                        status, hdr, body = self.raw_request(listener, 'GET', target)
                        self.assertEqual(status, expected)
                        if private:
                            self.assert_private(hdr)
                        self.assertNotIn(marker.encode(), body)
                        sink = self.gevent_logs if listener == 'flask' else self.logs
                        deadline = time.monotonic() + 1
                        while not sink.getvalue() and time.monotonic() < deadline:
                            time.sleep(.005)
                        logs = sink.getvalue()
                        self.assertTrue(logs)
                        self.assertNotIn(marker, logs)
                        if expected == 404:
                            self.assertIn('GET /synthetic-diagnostic?[query-redacted] HTTP/1.1', logs)
                            self.assertIn('404', logs)
        self.worker.assert_not_called()

    def test_r6_nonhandoff_logging_preserves_method_path_and_status(self):
        for listener in ('flask', 'websockify'):
            self.raw_request(listener, 'AXONSYNTHETIC', '/synthetic-diagnostic')
        self.assertIn('AXONSYNTHETIC /synthetic-diagnostic', self.logs.getvalue())
        self.assertIn('AXONSYNTHETIC /synthetic-diagnostic', self.gevent_logs.getvalue())
        self.worker.assert_not_called()

    def test_r7_encoded_aliases_are_rejected_before_worker_on_both_listeners(self):
        aliases=(http.PREFIX+'%70repare', '/api/x-attribution%2Fhandoff/prepare',
                 '/api/x-attribution/handoff/%2570repare', '//api/x-attribution/handoff/prepare',
                 '/api/x-attribution/handoff/prepare;alias', 'https://app.axonos.io'+http.PREFIX+'prepare')
        for listener in ('flask','websockify'):
            for target in aliases:
                with self.subTest(listener=listener,target=target):
                    self.worker.reset_mock()
                    status,hdr,body=self.raw_request(listener,'POST',target,json.dumps(BODY).encode())
                    self.assertEqual(status,400)
                    self.assert_private(hdr)
                    self.worker.assert_not_called()

    def test_r7_canonical_request_still_succeeds_on_both_listeners(self):
        for listener in ('flask','websockify'):
            with self.subTest(listener=listener):
                self.worker.reset_mock()
                status,hdr,body=self.raw_request(listener,'POST',http.PREFIX+'prepare',json.dumps(BODY).encode())
                self.assertEqual(status,200); self.assert_private(hdr)
                self.assertEqual(self.worker.call_args.args[0]['op'],'prepare')

    def test_r8_unsupported_verbs_get_consistent_privacy_response(self):
        for path in sorted(http.PATHS):
            for method in ('PUT','DELETE','HEAD','TRACE','PATCH','OPTIONS','AXONSYNTHETIC','post','get'):
                replies=[]
                for listener in ('flask','websockify'):
                    with self.subTest(listener=listener,path=path,method=method):
                        status,hdr,body=self.raw_request(listener,method,path)
                        self.assertEqual(status,400)
                        self.assert_private(hdr)
                        replies.append((status,hdr['Content-Type']))
                self.assertEqual(replies[0],replies[1])
        self.worker.assert_not_called()


if __name__=='__main__':unittest.main()
