"""Readiness must observe the running handoff, never fabricate a fresh lease."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest import mock

from axonos_gate import x_capi_worker as worker


class WorkerReadinessTests(unittest.TestCase):
    def readiness(self, mode, fresh):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(worker.os.environ, {
                'X_CAPI_DB_URL_FILE': '/synthetic/db',
                'X_CAPI_INGEST_ALLOWED_UID': '0',
            }, clear=True))
            for name, result in {
                'load_config': SimpleNamespace(policy_version='x-capi-handoff-v2'),
                'config_status': {'mode': mode, 'producer_ready': True},
                '_db_url': 'synthetic', 'get_connection': mock.MagicMock(),
                '_context_cipher': (object(), None), 'keyed_internal_hash': 'synthetic',
                'production_transport_enabled': True,
            }.items():
                stack.enter_context(mock.patch.object(worker.x_capi, name, return_value=result))
            for name, result in {
                '_worker_db_target_is_isolated': True, '_configure_connection': None,
                '_schema_ready': True, 'verify_config_guard': True,
                'read_token': ('synthetic', None), '_ingest_runtime_state': (True, True, True),
                '_privacy_fence_runtime_secure': True,
            }.items():
                stack.enter_context(mock.patch.object(worker, name, return_value=result))
            probe = stack.enter_context(mock.patch.object(worker, '_handoff_readiness', return_value={
                'ready': fresh, 'clock_trusted': fresh, 'lease_fresh': fresh, 'positive_allowed': True,
            }))
            result = worker.worker_readiness()
            self.assertEqual(probe.call_count, 0 if mode == 'off' else 1)
            return result

    def test_missing_or_stale_handoff_blocks_live_and_dry_run(self):
        for mode in ('live', 'dry_run'):
            with self.subTest(mode=mode):
                result = self.readiness(mode, False)
                self.assertTrue(result['handoff_required'])
                self.assertFalse(result['handoff_ready'])
                self.assertFalse(result['worker_ready'])
                self.assertFalse(result[mode + '_ready'])
                self.assertTrue(self.readiness(mode, True)['worker_ready'])

    def test_off_privacy_maintenance_does_not_require_handoff(self):
        result = self.readiness('off', False)
        self.assertTrue(result['off_ready'])
        self.assertFalse(result['handoff_required'])
        self.assertIsNone(result['handoff_ready'])

    def test_unavailable_socket_returns_only_safe_false_flags(self):
        with mock.patch('axonos_gate.x_capi_handoff_rpc.HandoffRpcClient.request',
                        side_effect=RuntimeError('synthetic-secret')):
            self.assertEqual(worker._handoff_readiness(), {
                'ready': False, 'clock_trusted': False,
                'lease_fresh': False, 'positive_allowed': False,
            })
