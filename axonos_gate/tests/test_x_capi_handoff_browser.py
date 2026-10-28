"""Real browser engines through a loopback-only synthetic HTTPS CONNECT fixture.

The browser executes production bridge bytes and its real cookie/Origin policy.
Landing authorize and worker state are simulated. No external or live ingress calls.
"""
from email.message import Message
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import ssl
import tempfile
import threading
from unittest.mock import patch
import json
from pathlib import Path
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from axonos_gate import x_capi_handoff_http as http
from axonos_gate.tests.test_x_capi_handoff_http import HANDOFF, ID, OWNER, REDEEMED


class SimpleRequest:
    def __init__(self,method,url,headers,body):
        self.method=method;self.url=url;self.headers=headers;self.post_data=body.decode('ascii')
    def all_headers(self):return {name.lower():value for name,value in self.headers.items()}


class _BridgeBrowserCases:
    engine_name = 'chromium'

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls.playwright = sync_playwright().start()
            engine = getattr(cls.playwright, cls.engine_name)
            if not Path(engine.executable_path).exists():
                cls.playwright.stop()
                raise unittest.SkipTest(cls.engine_name + ' executable unavailable; no downloads authorized')
            cls.browser = engine.launch(headless=True, args=['--no-sandbox'] if cls.engine_name == 'chromium' else [])
            cls.certdir = tempfile.TemporaryDirectory(prefix='handoff-browser-tls-', dir='/tmp')
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
            name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'synthetic-handoff')])
            cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                  .serial_number(x509.random_serial_number()).not_valid_before(datetime.utcnow()-timedelta(minutes=1))
                  .not_valid_after(datetime.utcnow()+timedelta(days=1))
                  .add_extension(x509.SubjectAlternativeName([x509.DNSName(host) for host in ('app.axonos.io','axonos.io','evil.axonos.io')]),critical=False)
                  .sign(key,hashes.SHA256()))
            keypath=Path(cls.certdir.name)/'synthetic.key'; certpath=Path(cls.certdir.name)/'synthetic.crt'
            keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            cls.tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);cls.tls.load_cert_chain(str(certpath),str(keypath))
        except unittest.SkipTest:
            raise
        except Exception as exc:
            if hasattr(cls, 'playwright'):
                cls.playwright.stop()
            raise unittest.SkipTest(cls.engine_name + ' engine unavailable: ' + type(exc).__name__)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.certdir.cleanup()

    def setUp(self):
        enabled=patch.dict(os.environ,{'X_HANDOFF_ENABLED':'true','X_CAPI_MODE':'dry_run'})
        enabled.start();self.addCleanup(enabled.stop)
        case=self
        class HTTPS(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self): self.dispatch()
            def do_POST(self): self.dispatch()
            def dispatch(self):
                try:
                    raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
                    origin='https://'+self.headers.get('Host','')
                    request=SimpleRequest(self.command,origin+self.path,self.headers,raw)
                    response=case.response(request)
                    self.send_response(response[0])
                    for name,value in response[1].items():self.send_header(name,value)
                    self.send_header('Content-Length',str(len(response[2])))
                    self.send_header('Connection','close');self.end_headers();self.wfile.write(response[2]);self.close_connection=True
                except Exception as exc:
                    case.fixture_errors.append(type(exc).__name__+': '+str(exc))
                    self.send_error(500,'Synthetic fixture failure')
        class Tunnel(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_CONNECT(self):
                if self.path not in ('app.axonos.io:443','axonos.io:443','evil.axonos.io:443'):
                    case.fixture_errors.append('Unexpected CONNECT destination')
                    self.send_error(403);return
                self.send_response(200);self.end_headers()
                self.close_connection=True
                try:
                    with case.tls.wrap_socket(self.connection,server_side=True,do_handshake_on_connect=False) as tunnel:
                        tunnel.do_handshake()
                        HTTPS(tunnel,self.client_address,self.server)
                except (ssl.SSLError,ConnectionError,OSError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Tunnel)
        self.server.daemon_threads=True
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.fixture_errors=[]
        self.context = self.browser.new_context(service_workers='block',ignore_https_errors=True,
            proxy={'server':'http://127.0.0.1:'+str(self.server.server_port)})
        self.addCleanup(self.context.close)
        self.calls = []
        self.urls = []
        self.forms = []
        self.fail_begin = False
        self.gpc_during_callback = False
        self.drop_callback_ack = False
        self.drop_owner_cookie = False
        self.expiry = int(time.time()*1000) + 600000
        self.cookie_owner = None
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)

    def worker(self, frame):
        self.calls.append(frame)
        if frame['op'] == 'prepare':
            self.cookie_owner = frame['owner_hash']
            body = {'v':2,'ok':True,'state':'prepared','expires_at_ms':self.expiry}
        elif frame['op'] == 'begin':
            self.assertEqual(frame['owner_hash'], self.cookie_owner)
            self.assertFalse(frame['new_owner'])
            if self.fail_begin:
                raise TimeoutError('synthetic ambiguous begin')
            body = {'v':2,'ok':True,'state':'app_granted','handoff':HANDOFF,'expires_at_ms':self.expiry}
        elif frame['op'] in ('redeem','recover'):
            self.assertEqual(frame['owner_hash'], self.cookie_owner)
            body = dict(REDEEMED, expires_at_ms=self.expiry)
        elif frame['op'] == 'withdraw':
            body = {'v':2,'ok':True,'state':'withdrawn','request_id':frame['payload']['request_id']}
        else:
            raise AssertionError('unexpected worker operation')
        return {'status':200,'body':body,'headers':{}}

    def response(self, request):
        self.urls.append(request.url)
        parsed = urlsplit(request.url)
        if parsed.netloc not in ('app.axonos.io', 'axonos.io', 'evil.axonos.io'):
            self.fail('unexpected external request')
        if parsed.netloc == 'app.axonos.io' and parsed.path == '/':
            return 200,{'Content-Type':'text/html'},b'<html><title>Ordinary app</title><body>App available</body></html>'
        if parsed.netloc == 'axonos.io' and parsed.path == '/attribution/authorize':
            self.forms.append((request.url, request.all_headers(), request.post_data))
            payload=parse_qs(request.post_data,strict_parsing=True)
            self.assertEqual(payload,{'v':['2'],'handoff':[HANDOFF]})
            self.assertEqual(request.all_headers().get('origin'),http.APP_ORIGIN)
            # Synthetic landing approves only the H the app actually created.
            body="""<!doctype html><html><head><title>AxonOS</title></head><body>
                <form method=post action='https://app.axonos.io/attribution/callback'>
                <input type=hidden name=v value=2><input type=hidden name=handoff value='%s'></form>
                <script nonce=synthetic>document.forms[0].submit()</script></body></html>""" % HANDOFF
            return 200,{'Content-Type':'text/html','Cache-Control':'no-store, private',
                'Referrer-Policy':'strict-origin','Content-Security-Policy':"default-src 'none'; script-src 'nonce-synthetic'; form-action https://app.axonos.io/attribution/callback; frame-ancestors 'none'"},body.encode()
        if parsed.netloc == 'evil.axonos.io':
            return 200,{'Content-Type':'text/html'},("<html><form method=post action='https://app.axonos.io/attribution/callback'><input name=v value=2><input name=handoff value='"+HANDOFF+"'></form></html>").encode()
        raw=(request.post_data or '').encode()
        hdr=Message()
        for name,value in request.all_headers().items(): hdr[name]=value
        if hdr.get('Host') is None: hdr['Host']=parsed.netloc
        if hdr.get('Content-Length') is None and request.method=='POST': hdr['Content-Length']=str(len(raw))
        if parsed.path == http.CALLBACK:
            self.forms.append((request.url,request.all_headers(),request.post_data))
        result=http.handle(request.method,parsed.path+('?' + parsed.query if parsed.query else ''),hdr,raw,https=True,call=self.worker)
        body=result.body
        if self.drop_callback_ack and parsed.path == http.CALLBACK:
            self.drop_callback_ack = False
            return 503, {'Content-Type':'text/html','Referrer-Policy':'strict-origin','Cache-Control':'no-store, private'}, b'<html><body>Uncertain callback result</body></html>'
        if self.drop_owner_cookie:
            result.headers.pop('Set-Cookie',None)
        if self.gpc_during_callback and parsed.path==http.CALLBACK:
            # Browser GPC may change after the server accepted the callback.
            body=body.replace(b'const AXONOS_HANDOFF_PAGE=',b'Object.defineProperty(Navigator.prototype,"globalPrivacyControl",{get:()=>true});const AXONOS_HANDOFF_PAGE=')
        return result.status,result.headers,body

    def _hold_positive_response(self, operation):
        # The real HTTP response has set its owner cookie and may have committed,
        # but the bridge still awaits the fetch. Aborting it rejects on the next
        # microtask, exactly when a Deny/GPC withdrawal is starting.
        self.context.add_init_script("""((operation) => {
            const original=window.fetch.bind(window);
            window.fetch=async function (url, options) {
                if(String(url).endsWith('/withdraw')) {
                    sessionStorage.setItem('test_withdraw_started','1');
                    if(options.signal) options.signal.addEventListener('abort',()=>{
                        sessionStorage.setItem('test_withdraw_aborted','1');
                    });
                    const response=await original(url,options);
                    sessionStorage.setItem('test_withdraw_completed','1');
                    return response;
                }
                const response=await original(url,options);
                if(String(url).endsWith('/'+operation)) {
                    window.test_positive_waiting=true;
                    await new Promise((resolve,reject)=>{
                        if(options.signal.aborted) reject(new DOMException('Aborted','AbortError'));
                        else options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
                    });
                }
                return response;
            };
        })(%s);""" % json.dumps(operation))

    def _pending_positive_suppression(self, operation, gpc):
        self._hold_positive_response(operation)
        if operation == 'recover':
            self.context.add_cookies([{'name':http.APP_COOKIE,'value':OWNER,'url':http.APP_ORIGIN,
                                      'secure':True,'httpOnly':True,'sameSite':'Strict'}])
            self.cookie_owner=http.protocol.token_hash(OWNER,'app-owner')
            self.context.add_init_script("""if(!sessionStorage.getItem('test_recover_initialized')) {
                sessionStorage.setItem('test_recover_initialized','1');
                sessionStorage.setItem('axonos_x_handoff_pending_v2',%s);
            }""" % json.dumps(json.dumps({'handoff':HANDOFF,'expires_at_ms':self.expiry})))
        self.page.goto(http.APP_ORIGIN + http.ENTRY)
        if operation != 'recover': self.page.locator('#grant').click()
        self.page.wait_for_function('() => window.test_positive_waiting === true')
        if gpc:
            self.page.evaluate("""() => {
                Object.defineProperty(Navigator.prototype,'globalPrivacyControl',{configurable:true,get:()=>true});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            self.page.wait_for_function("() => sessionStorage.getItem('test_withdraw_completed') === '1'",timeout=2000)
        else:
            self.page.locator('#deny').click()
            self.page.wait_for_url(http.APP_ORIGIN + '/')
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('test_withdraw_aborted')"))
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('test_withdraw_completed')"),'1')
        self.assertEqual([frame['op'] for frame in self.calls].count('withdraw'),1)
        self.assertEqual(self.forms,[])
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_context_v1')"))
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('axonos_x_handoff_pending_v2')"))
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_revoke_pending_v1')"),'1')

    def test_pending_prepare_deny_cannot_cancel_withdrawal(self):
        self._pending_positive_suppression('prepare',False)

    def test_pending_begin_deny_cannot_cancel_withdrawal(self):
        self._pending_positive_suppression('begin',False)

    def test_pending_recover_deny_cannot_cancel_withdrawal(self):
        self._pending_positive_suppression('recover',False)

    def test_pending_prepare_gpc_cannot_cancel_withdrawal(self):
        self._pending_positive_suppression('prepare',True)

    def test_pending_begin_gpc_cannot_cancel_withdrawal(self):
        self._pending_positive_suppression('begin',True)

    def test_pending_recover_gpc_cannot_cancel_withdrawal(self):
        self._pending_positive_suppression('recover',True)

    def test_navigation_during_withdrawal_does_not_abort_privacy_request(self):
        self._hold_positive_response('unused')
        self.context.add_cookies([{'name':http.APP_COOKIE,'value':OWNER,'url':http.APP_ORIGIN,
                                  'secure':True,'httpOnly':True,'sameSite':'Strict'}])
        accepted=threading.Event(); release=threading.Event(); completed=threading.Event()
        original_worker=self.worker
        def delayed_worker(frame):
            if frame['op']=='withdraw':
                accepted.set()
                if not release.wait(5): raise AssertionError('Synthetic withdrawal was not released')
            result=original_worker(frame)
            if frame['op']=='withdraw': completed.set()
            return result
        self.worker=delayed_worker
        self.addCleanup(release.set)
        self.page.goto(http.APP_ORIGIN + http.ENTRY)
        self.page.locator('#deny').click()
        self.assertTrue(accepted.wait(2))
        self.page.locator('#continue').click()
        self.page.wait_for_url(http.APP_ORIGIN + '/')
        release.set()
        self.assertTrue(completed.wait(2))
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('test_withdraw_aborted')"))
        self.assertEqual([frame['op'] for frame in self.calls],['withdraw'])
        self.assertEqual(self.forms,[])
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_revoke_pending_v1')"),'1')

    def test_stale_success_after_gpc_cannot_resume_positive_work(self):
        self.context.add_init_script("""(() => {
            const original=window.fetch.bind(window);
            window.fetch=async function(url,options) {
                const response=await original(url,options);
                if(String(url).endsWith('/prepare')) {
                    window.test_positive_waiting=true;
                    // A completed response may still be delivered despite the
                    // controller abort: the generation/stopped checks must win.
                    await new Promise(resolve=>window.test_release_positive=resolve);
                }
                if(String(url).endsWith('/withdraw')) window.test_withdraw_completed=true;
                return response;
            };
        })();""")
        self.page.goto(http.APP_ORIGIN + http.ENTRY)
        self.page.locator('#grant').click()
        self.page.wait_for_function('() => window.test_positive_waiting === true')
        self.page.evaluate("""() => {
            Object.defineProperty(Navigator.prototype,'globalPrivacyControl',{configurable:true,get:()=>true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        self.page.wait_for_function('() => window.test_withdraw_completed === true')
        self.page.evaluate('() => window.test_release_positive()')
        from playwright.sync_api import expect
        expect(self.page.locator('#status')).to_contain_text('unavailable')
        self.assertEqual([frame['op'] for frame in self.calls].count('prepare'),1)
        self.assertNotIn('begin',[frame['op'] for frame in self.calls])
        self.assertEqual(self.forms,[])
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_context_v1')"))
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('axonos_x_handoff_pending_v2')"))

    def test_consent_cookie_confirmation_two_post_bridges_and_stable_owner(self):
        self.page.goto(http.APP_ORIGIN + http.ENTRY)
        self.assertEqual(self.calls,[])
        self.page.locator('#grant').click()
        self.page.wait_for_url(http.APP_ORIGIN + '/')
        self.assertEqual([frame['op'] for frame in self.calls],['prepare','begin','redeem'])
        self.assertEqual(len(self.forms),2)
        self.assertEqual(self.forms[1][1].get('origin'),http.LANDING_ORIGIN)
        for url in self.urls:
            self.assertNotIn(HANDOFF,url)
            self.assertNotIn('twclid',url)
        stored=self.page.evaluate('Object.fromEntries(Object.entries(sessionStorage))')
        self.assertEqual(stored['axonos_x_attribution_tab_owner_v1'],ID)
        self.assertEqual(stored['axonos_x_attribution_context_v1'],REDEEMED['context'])
        self.assertNotIn('axonos_x_handoff_pending_v2',stored)
        cookies=self.context.cookies(http.APP_ORIGIN)
        owner=next(cookie for cookie in cookies if cookie['name']==http.APP_COOKIE)
        self.assertTrue(owner['httpOnly']);self.assertTrue(owner['secure']);self.assertEqual(owner['sameSite'],'Strict')
        self.assertNotIn(http.APP_COOKIE,self.page.evaluate('document.cookie'))

    def test_ambiguous_begin_does_not_retry_or_forward(self):
        self.fail_begin=True
        self.page.goto(http.APP_ORIGIN + http.ENTRY)
        self.page.locator('#grant').click()
        from playwright.sync_api import expect
        expect(self.page.locator('#status')).to_contain_text('unavailable')
        self.assertEqual([frame['op'] for frame in self.calls],['prepare','begin'])
        self.assertEqual(self.forms,[])
        self.page.locator('#continue').click()
        self.page.wait_for_url(http.APP_ORIGIN + '/')

    def test_gpc_at_callback_preserves_private_closure_and_never_installs_public_context(self):
        self.gpc_during_callback=True
        self.page.goto(http.APP_ORIGIN + http.ENTRY)
        self.page.locator('#grant').click()
        from playwright.sync_api import expect
        self.page.wait_for_url(http.APP_ORIGIN + http.CALLBACK)
        expect(self.page.locator('#status')).to_contain_text('unavailable')
        self.assertEqual(self.page.evaluate('window.axonosAttributionContext'),'')
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_context_v1')"))
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_revoke_pending_v1')"),'1')
        self.assertIn('withdraw',[frame['op'] for frame in self.calls])

    def test_entry_header_only_gpc_closes_existing_owner_without_positive_work(self):
        self.context.add_cookies([{'name':http.APP_COOKIE,'value':OWNER,'url':http.APP_ORIGIN,
                                  'secure':True,'httpOnly':True,'sameSite':'Strict'}])
        self.context.set_extra_http_headers({'Sec-GPC':'1'})
        self.context.add_init_script("Object.defineProperty(Navigator.prototype,'globalPrivacyControl',{get:()=>false});")
        # Off mode must retain this suppression-only bridge and its POST path.
        with patch.dict(os.environ, {'X_CAPI_MODE':'off'}):
            self.page.goto(http.APP_ORIGIN + http.ENTRY)
            from playwright.sync_api import expect
            expect(self.page.locator('#status')).to_contain_text('unavailable')
        self.assertEqual(self.page.locator('#grant').count(),0)
        self.assertEqual(self.page.locator('#deny').count(),0)
        self.assertFalse(self.page.evaluate('navigator.globalPrivacyControl'))
        self.assertEqual([frame['op'] for frame in self.calls],['withdraw'])
        self.assertEqual(self.calls[0]['payload']['reason'],'gpc')
        self.assertEqual(self.calls[0]['owner_hash'],http.protocol.token_hash(OWNER,'app-owner'))
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_context_v1')"))
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_revoke_pending_v1')"),'1')
        self.assertEqual(self.forms,[])
        self.page.locator('#continue').click()
        self.page.wait_for_url(http.APP_ORIGIN + '/')

    def test_gpc_observed_during_prepare_response_attempts_durable_withdrawal(self):
        self.context.add_init_script("""(() => {
            const original=window.fetch.bind(window);
            window.fetch=async function (...args) {
                const response=await original(...args);
                if(String(args[0]).endsWith('/prepare')) {
                    Object.defineProperty(Navigator.prototype,'globalPrivacyControl',{configurable:true,get:()=>true});
                }
                return response;
            };
        })();""")
        self.page.goto(http.APP_ORIGIN + http.ENTRY)
        self.page.locator('#grant').click()
        from playwright.sync_api import expect
        expect(self.page.locator('#status')).to_contain_text('unavailable')
        self.assertEqual([frame['op'] for frame in self.calls], ['prepare','withdraw'])
        self.assertEqual(self.forms,[])
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_context_v1')"))
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_revoke_pending_v1')"),'1')

    def test_lost_callback_ack_recovers_identical_result_without_new_begin(self):
        self.drop_callback_ack=True
        self.page.goto(http.APP_ORIGIN + http.ENTRY)
        self.page.locator('#grant').click()
        self.page.wait_for_url(http.APP_ORIGIN + http.CALLBACK)
        self.assertEqual([frame['op'] for frame in self.calls],['prepare','begin','redeem'])
        self.page.goto(http.APP_ORIGIN + http.ENTRY)
        self.page.wait_for_url(http.APP_ORIGIN + '/')
        self.assertEqual([frame['op'] for frame in self.calls],['prepare','begin','redeem','recover'])
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_tab_owner_v1')"),ID)
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('axonos_x_attribution_context_v1')"),REDEEMED['context'])
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('axonos_x_handoff_pending_v2')"))

    def test_unaccepted_cookie_prevents_transaction_and_keeps_ordinary_navigation(self):
        self.drop_owner_cookie=True
        self.page.goto(http.APP_ORIGIN + http.ENTRY)
        self.page.locator('#grant').click()
        from playwright.sync_api import expect
        expect(self.page.locator('#status')).to_contain_text('unavailable')
        self.assertEqual([frame['op'] for frame in self.calls],['prepare'])
        self.assertEqual(self.forms,[])
        self.page.locator('#continue').click()
        self.page.wait_for_url(http.APP_ORIGIN + '/')

    def test_malicious_sibling_form_cannot_redeem_even_with_strict_cookie(self):
        self.context.add_cookies([{'name':http.APP_COOKIE,'value':OWNER,'url':http.APP_ORIGIN,'secure':True,'httpOnly':True,'sameSite':'Strict'}])
        self.page.goto('https://evil.axonos.io/')
        self.page.evaluate('document.forms[0].submit()')
        self.page.wait_for_url(http.APP_ORIGIN + http.CALLBACK)
        self.assertEqual(self.calls,[])
        self.assertNotIn(REDEEMED['context'],self.page.content())
        self.assertIn('Continue to AxonOS',self.page.content())


class ChromiumHandoffTests(_BridgeBrowserCases, unittest.TestCase):
    engine_name='chromium'


class FirefoxHandoffTests(_BridgeBrowserCases, unittest.TestCase):
    engine_name='firefox'


class WebKitHandoffTests(_BridgeBrowserCases, unittest.TestCase):
    engine_name='webkit'


if __name__=='__main__':unittest.main()
