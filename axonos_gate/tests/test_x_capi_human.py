"""Candidate human payment/F2 integration; RPC, financial storage and sends mocked."""
import ast
import base64
import copy
import os
import time
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from eth_utils import keccak
from axonos_gate import gate_server as gate, x_capi, x_capi_human as human, deposit_ledger as ledger
from axonos_gate import x402_verifier as verifier
from axonos_gate import x_capi_handoff_http as handoff
from axonos_gate.tests import test_x_capi as capi_fixtures
from axonos_gate.tests.test_x402_verifier import _env, _SIGNER, _USDC, _REVENUE

WALLET = _SIGNER.lower()
TX = '0x' + 'ab' * 32
TOKEN = 'captured-original-bearer'
CONTEXT = 'c' * 64
HEADERS = {'X-AXGT-Auth-Token': TOKEN, 'X-AxonOS-Attribution': CONTEXT,
           'Origin': 'https://app.axonos.io', 'Sec-Fetch-Site': 'same-origin',
           'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty',
           'Cookie': handoff.APP_COOKIE + '=' + base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')}
ROOT = Path(__file__).resolve().parents[2]


def receipt(agent=False):
    logs = [{'address': _USDC, 'topics': [
        '0x' + keccak(text='Transfer(address,address,uint256)').hex(),
        '0x' + WALLET[2:].rjust(64, '0'), '0x' + _REVENUE[2:].rjust(64, '0')],
        'data': '0x' + hex(1_000_000)[2:].zfill(64)}]
    if agent:
        logs.insert(0, {'address': _USDC, 'topics': [
            '0x' + keccak(text='AuthorizationUsed(address,bytes32)').hex()]})
    return {'status': '0x1', 'blockNumber': '0x64', 'logs': logs}


@pytest.mark.parametrize('chain,logs,eligible', [
    (8453, receipt()['logs'], True), (1, receipt()['logs'], False),
    (None, receipt()['logs'], False), (8453, receipt(True)['logs'], False),
    (8453, [{'address': _USDC, 'topics': None}], False), (8453, [None], False),
])
def test_receipt_classification_only_affects_attribution(chain, logs, eligible):
    marker = object()
    assert (human.manual_usdc_context(marker, chain, _USDC, logs) is marker) == eligible


def test_default_off_never_connects_for_optional_authority():
    with patch.dict(os.environ, {}, clear=True), gate.app.test_request_context(
            '/api/auth/verify-usdc-deposit', method='POST', headers=HEADERS), \
            patch.object(gate, '_gate_pg_get_connection') as connect:
        assert gate._authenticated_attribution_context(CONTEXT, WALLET) is None
        connect.assert_not_called()


@pytest.mark.parametrize('listener', ['flask', 'websockify'])
def test_exact_request_bearer_is_captured_without_issuance_or_auth_io(listener):
    env = capi_fixtures.ConfigTests.enabled_env(X_CAPI_ALLOWED_ORIGIN='https://app.axonos.io', X_CAPI_CONSENT_POLICY_VERSION='x-capi-handoff-v2')
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        authorize = stack.enter_context(patch.object(human, 'authorize_exact_bearer', return_value=True))
        if listener == 'flask':
            stack.enter_context(gate.app.test_request_context('/api/auth/verify-usdc-deposit', method='POST', headers=HEADERS))
            authority = gate._authenticated_attribution_context(CONTEXT, WALLET)
        else:
            import axonos_gate.websockify_gate as web
            authority = web._authenticated_attribution_context(CONTEXT, WALLET, TOKEN, HEADERS)
        assert authority is not None
        assert authority._browser_owner_hash == handoff.trusted_browser_owner_hash(HEADERS)
        authorize.assert_not_called()
        assert authority._authorize() is True
        assert authorize.call_args.args[:2] == (TOKEN, WALLET)
        authorize.return_value = False
        assert authority._authorize() is False  # No cookie/new-token fallback.


@pytest.mark.parametrize('rail,agent,status,duplicate,commit_fails,authorized,expected', [
    (rail, *case) for rail in ('usdc', 'eth', 'axgt') for case in [
    (False, '0x1', False, False, True, 1),
    (True, '0x1', False, False, True, 0),
    (False, '0x0', False, False, True, 0),
    (False, None, False, False, True, 0),
    (False, 'pending', False, False, True, 0),
    (False, 'unconfirmed', False, False, True, 0),
    (False, 'disabled', False, False, True, 0),
    (False, '0x1', True, False, True, 0),
    (False, '0x1', False, True, True, 0),
    (False, '0x1', False, False, False, 0),
] if (not case[0] or rail == 'usdc') and (case[1] != 'disabled' or rail == 'axgt')
])
def test_human_http_to_confirmed_ledger_to_f2_event(rail, agent, status, duplicate, commit_fails, authorized, expected):
    env = _env()
    env.update(AXGT_CHAIN_ID="8453", AXGT_CONTRACT_ADDRESS=_USDC, AXGT_RPC_URL="https://synthetic.invalid", AXGT_REVENUE_WALLET=_REVENUE, AXGT_ENABLE_AXGT_DEPOSITS="true", AXGT_MIN_DEPOSIT="0.000001")
    env.update(capi_fixtures.ConfigTests.enabled_env(X_CAPI_ALLOWED_ORIGIN='https://app.axonos.io', X_CAPI_CONSENT_POLICY_VERSION='x-capi-handoff-v2'))
    env.update(GATE_AGENT_ONLY='false', GATE_AGENT_API_ENABLED='false')
    if status == 'disabled':
        env['AXGT_ENABLE_AXGT_DEPOSITS'] = 'false'
    observed = []
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (60.0,)
    def commit():
        observed.append('commit')
        if commit_fails:
            raise RuntimeError('synthetic unknown commit outcome')
    conn.commit.side_effect = commit
    conn.close.side_effect = lambda: observed.append('close')
    rec = receipt(agent); rec['status'] = '0x1' if status in ('unconfirmed', 'disabled') else status
    def rpc(_url, method, _args):
        if method == 'eth_getTransactionByHash':
            # Smart-account wrapper is not required to equal the token sender.
            return {'chainId': hex(8453), 'from': '0x' + '9' * 40 if rail == 'usdc' else WALLET, 'to': _REVENUE if rail == 'eth' else _USDC, 'value': hex(10**18) if rail == 'eth' else '0x0'}
        if method == 'eth_getTransactionReceipt':
            return None if status == 'pending' else rec
        if method == 'eth_blockNumber':
            return '0x63' if status == 'unconfirmed' else '0x69'
        raise AssertionError(method)
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch.object(gate, '_require_auth_token', return_value=None))
        stack.enter_context(patch.object(gate, '_issue_gate_auth_token', return_value=('normal-production-refresh', 300)))
        stack.enter_context(patch.object(gate, 'verify_usdc_deposit', verifier.verify_usdc_deposit))
        stack.enter_context(patch.object(gate, 'verify_deposit', verifier._dv.verify_deposit))
        stack.enter_context(patch.object(human, 'authorize_exact_bearer', return_value=authorized))
        stack.enter_context(patch.object(verifier._dv, '_rpc', side_effect=rpc))
        stack.enter_context(patch.object(verifier._dv, '_token_decimals', return_value=6))
        stack.enter_context(patch.object(verifier._dv, '_import_discount', return_value=None))
        stack.enter_context(patch.object(ledger, 'tx_hash_already_credited', return_value=duplicate))
        stack.enter_context(patch.object(ledger, 'record_verification_reject'))
        stack.enter_context(patch.object(ledger, 'init_once', return_value=True))
        stack.enter_context(patch.object(ledger, '_get_connection', return_value=conn))
        stack.enter_context(patch.object(ledger, '_funding_ledger', return_value=MagicMock()))
        sends = []
        def send(envelope):
            observed.append('emit'); sends.append(envelope); return True
        stack.enter_context(patch.object(x_capi, '_send_local_envelope', side_effect=send))
        stack.enter_context(patch.object(x_capi, '_queue_authenticated_envelope', side_effect=
            lambda ctx, event, cfg: x_capi._authorize_and_send(ctx, event, cfg, time.monotonic())))
        response = gate.app.test_client(use_cookies=False).post('/api/auth/verify-usdc-deposit' if rail == 'usdc' else '/api/auth/verify-deposit',
            json={'wallet_address': WALLET, 'tx_hash': TX}, headers=HEADERS)
        assert len(sends) == expected
        if expected:
            assert observed == ['commit', 'close', 'emit']
            assert sends[0]['metadata']['payment_rail'] == rail
            assert sends[0]['metadata']['chain_id'] == 8453
            assert sends[0]['metadata']['allow_context_binding'] is False
            assert x_capi.wallet_authority_valid(sends[0], time.time())
            assert response.get_json()['verified'] is True
        if status in ('0x0', None, 'pending', 'unconfirmed', 'disabled') or duplicate:
            assert 'commit' not in observed
            assert not response.get_json().get('verified', False)
        if agent or not authorized:
            assert response.get_json()['verified'] is True
            assert 'commit' in observed  # Marketing exclusion never rejects credit.


def test_current_x402_settlement_body_diff_is_only_context_exclusion():
    # Compared source is the pinned current supported production implementation.
    import subprocess
    baseline = subprocess.check_output(['git', 'show', 'ddb7c99197bbc9b19588ccb3f1f9c4ba60915855:axonos_gate/x402_verifier.py'], cwd=ROOT, text=True)
    def settlement(source):
        return next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'settle_x402_payment')
    original = settlement(baseline)
    current = settlement((ROOT / 'axonos_gate/x402_verifier.py').read_text())
    exclusions = [n for n in current.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'attribution_context' for t in n.targets)]
    assert len(exclusions) == 1 and isinstance(exclusions[0].value, ast.Constant) and exclusions[0].value.value is None
    current.body.remove(exclusions[0])
    assert ast.dump(original, include_attributes=False) == ast.dump(current, include_attributes=False)

@pytest.mark.parametrize('path', ['/api/auth/verify-usdc-deposit', '/api/auth/verify-deposit-auto', '/api/auth/verify-deposit'])
def test_websockify_manual_route_passes_original_authority_without_refresh(path):
    import axonos_gate.websockify_gate as web
    request = object.__new__(web.AxonOSProxyRequestHandler)
    request.path = path
    request.headers = dict(HEADERS)
    request.client_address = ('127.0.0.1', 12345)
    request._observe_request_gpc_early = lambda *_: None
    request._read_json_body = lambda: {'wallet_address': WALLET, 'tx_hash': TX}
    request._send_json = lambda status, body, **_: (status, body)
    env = capi_fixtures.ConfigTests.enabled_env(X_CAPI_ALLOWED_ORIGIN='https://app.axonos.io',
                                               X_CAPI_CONSENT_POLICY_VERSION='x-capi-handoff-v2')
    result = {'verified': True, 'credited_minutes': 60}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(web, '_is_auth_token_valid', return_value=True), \
         patch.object(web, 'verify_deposit', return_value=result) as native, \
         patch.object(web, 'verify_usdc_deposit', return_value=result) as verify, \
         patch.object(web, 'verify_deposit_auto', return_value=(result, False)) as auto, \
         patch.object(human, 'authorize_exact_bearer', return_value=True) as authorize, \
         patch.object(web, '_issue_auth_token') as issue:
        assert request.do_POST() == (200, result)
        call = auto.call_args if path.endswith('-auto') else native.call_args if path.endswith('/verify-deposit') else verify.call_args
        authority = call.kwargs['attribution_context']
        assert authority._browser_owner_hash == handoff.trusted_browser_owner_hash(HEADERS)
        authorize.assert_not_called()
        assert authority._authorize() is True
        assert authorize.call_args.args[:2] == (TOKEN, WALLET)
        issue.assert_not_called()
