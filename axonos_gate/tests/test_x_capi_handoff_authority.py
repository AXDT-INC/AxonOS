"""Independent synthetic adversarial tests for continuing handoff/F2 ownership."""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from axonos_gate import x_capi, x_capi_worker as worker, x_capi_handoff_http as http
from axonos_gate.tests import test_x_capi as capi_fixtures
from axonos_gate.tests.test_x_capi_handoff_http import OWNER, headers

WALLET='0x'+'1'*40
OTHER='0x'+'2'*40
TOKEN='z'*64
OWNER_HASH='a'*64


class HandoffAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        env=capi_fixtures.ConfigTests.enabled_env(X_CAPI_CONSENT_POLICY_VERSION='x-capi-handoff-v2',
            X_CAPI_ALLOWED_ORIGIN='https://app.axonos.io',X_CAPI_CONSENT_POLICY_EPOCH='42')
        self.stack.enter_context(patch.dict(os.environ,env,clear=True))
        x_capi.preload_context_keyring()
        self.sent=self.stack.enter_context(patch.object(x_capi,'_send_local_envelope',return_value=True))
        self.stack.enter_context(patch.object(x_capi,'_queue_authenticated_envelope',side_effect=self.inline))

    def inline(self,authority,envelope,cfg):
        return x_capi._authorize_and_send(authority,envelope,cfg,time.monotonic())

    def authority(self,owner=OWNER_HASH,authorized=True):
        return x_capi.authenticated_context(TOKEN,WALLET,lambda:authorized,browser_owner_hash=owner)

    def emit(self,authority):
        return x_capi.emit_binding_nonblocking(context_token=authority,wallet_address=WALLET,event_timestamp_ms=int(time.time()*1000))

    def signed(self):
        self.assertTrue(self.emit(self.authority()))
        return self.sent.call_args.args[0]

    def test_encrypted_context_and_wallet_authority_without_browser_owner_are_insufficient(self):
        self.assertIsNone(self.authority(None))
        self.assertFalse(self.emit(TOKEN))
        self.sent.assert_not_called()

    def test_browser_owner_is_strict_immutable_server_owned_value(self):
        for invalid in ('a'*63,'a'*64+'\n','A'*64,True,{'browser_owner_hash':OWNER_HASH}):
            with self.subTest(invalid=invalid):self.assertIsNone(self.authority(invalid))
        authority=self.authority()
        with self.assertRaises(FrozenInstanceError):authority._browser_owner_hash='b'*64
        self.assertNotIn(TOKEN,repr(authority));self.assertNotIn(OWNER_HASH,repr(authority))

    def test_v2_positive_proof_covers_owner_wallet_and_action(self):
        signed=self.signed()
        self.assertEqual(signed['v'],2)
        self.assertEqual(signed['browser_owner_hash'],OWNER_HASH)
        self.assertTrue(x_capi.wallet_authority_valid(signed,time.time()))
        for key,value in [('browser_owner_hash','b'*64),('wallet_address',OTHER),('v',1),('action','event')]:
            with self.subTest(key=key):self.assertFalse(x_capi.wallet_authority_valid(dict(signed,**{key:value}),time.time()))

    def test_valid_browser_owner_never_substitutes_independent_wallet_authority(self):
        self.assertFalse(self.emit(self.authority(authorized=False)))
        self.sent.assert_not_called()

    def test_cross_wallet_reuse_is_dropped_before_optional_work(self):
        authority=self.authority()
        result=x_capi.emit_binding_nonblocking(context_token=authority,wallet_address=OTHER,event_timestamp_ms=int(time.time()*1000))
        self.assertFalse(result);self.sent.assert_not_called()

    def test_missing_or_extra_owner_fields_rejected_by_v2_ingestion(self):
        signed=self.signed()
        missing=dict(signed);missing.pop('browser_owner_hash')
        self.assertIsNone(worker._validate_ingest_envelope(missing))
        self.assertIsNone(worker._validate_ingest_envelope(dict(signed,owner_hash=OWNER_HASH)))
        self.assertIsNone(worker._validate_ingest_envelope(dict(signed,browser_owner_hash=OWNER_HASH+'\n')))

    def test_v1_positive_downgrade_is_rejected_before_database_access(self):
        signed=self.signed();signed['v']=1;signed.pop('browser_owner_hash')
        conn=Mock()
        result=worker._ingest_event(conn,signed,time.time(),bind_only=True)
        self.assertEqual(result,'positive_envelope_version_mismatch')
        conn.cursor.assert_not_called()

    def test_v2_cannot_upgrade_legacy_policy_without_matching_audience(self):
        signed=self.signed();conn=Mock()
        with patch.dict(os.environ,{'X_CAPI_CONSENT_POLICY_VERSION':'privacy-v1'}):
            result=worker._ingest_event(conn,signed,time.time(),bind_only=True)
        self.assertEqual(result,'positive_envelope_version_mismatch')
        conn.cursor.assert_not_called()

    def test_missing_runtime_or_clock_assurance_prevents_ingestion(self):
        signed=self.signed();conn=Mock()
        for runtime in (None,SimpleNamespace(positive_allowed=lambda:True,clock_assured=lambda:False)):
            with self.subTest(runtime=runtime),patch.object(worker,'_handoff_runtime',runtime):
                self.assertEqual(worker._ingest_event(conn,signed,time.time(),bind_only=True),'handoff_authority_unavailable')
        conn.cursor.assert_not_called()

    def test_trusted_owner_snapshot_survives_later_request_mutation(self):
        actual=headers();owner_hash=http.trusted_browser_owner_hash(actual)
        authority=self.authority(owner_hash)
        actual['Cookie']=http.APP_COOKIE+'='+'b'*43
        actual['Origin']='https://evil.axonos.io'
        self.assertTrue(self.emit(authority))
        self.assertEqual(self.sent.call_args.args[0]['browser_owner_hash'],owner_hash)
        self.assertIsNone(http.trusted_browser_owner_hash(actual))

    def test_privacy_revoke_envelope_keeps_legacy_suppression_without_owner(self):
        envelope={'v':1,'action':'revoke','context_token':TOKEN,'event_timestamp_ms':int(time.time()*1000)}
        self.assertEqual(worker._validate_ingest_envelope(envelope),envelope)
        self.assertIsNone(worker._validate_ingest_envelope(dict(envelope,v=2,browser_owner_hash=OWNER_HASH)))

    def test_off_mode_never_creates_a_positive_authority(self):
        with patch.dict(os.environ,{'X_CAPI_MODE':'off'}):
            self.assertIsNone(self.authority())
            self.assertFalse(self.emit(TOKEN))
        self.sent.assert_not_called()


if __name__=='__main__':unittest.main()
