"""Launcher integration at the wallet volume and isolated worker process boundaries."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from subprocess import CompletedProcess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import intelligence_volume
import session_launcher_service as launcher

WALLET = '0x' + 'b' * 40
PREFIX = 'example-wallet-'
TOKEN = 'synthetic-launcher-token'


class WalletStorageLauncherTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            'AXGT_SESSION_LAUNCHER_TOKEN': TOKEN,
            'AXGT_PERSISTENT_STORAGE_VOLUME_PREFIX': PREFIX,
            'AXGT_PERSISTENT_STORAGE_ENABLED': 'true',
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = launcher.app.test_client()
        run = patch.object(intelligence_volume.subprocess, 'run')
        self.run = run.start()
        self.addCleanup(run.stop)

    def rpc(self, operation, *, bearer=TOKEN, **data):
        return self.client.post('/intelligence/storage', json={
            'wallet': WALLET, 'operation': operation, 'data': data,
        }, headers={'Authorization': 'Bearer ' + bearer})

    def test_authentication_and_disabled_storage_never_touch_docker(self):
        self.assertEqual(self.rpc('list', bearer='invalid').status_code, 401)
        for unavailable in ('', 'change-me-launcher-token'):
            with self.subTest(credential=unavailable), patch.dict(os.environ, {'AXGT_SESSION_LAUNCHER_TOKEN': unavailable}):
                self.assertEqual(self.rpc('list', bearer=unavailable).status_code, 503)
        with patch.dict(os.environ, {'AXGT_PERSISTENT_STORAGE_ENABLED': 'false'}):
            self.assertEqual(self.rpc('list').json, {'conversations': [], 'storage_available': False})
            self.assertEqual(self.rpc('create').status_code, 409)
            self.assertEqual(self.rpc('public').status_code, 404)
        self.run.assert_not_called()

    def test_missing_volume_is_distinct_from_docker_unavailable(self):
        self.run.return_value = CompletedProcess([], 1, '', 'Error: no such volume: ' + PREFIX + WALLET)
        self.assertFalse(self.rpc('list').json['storage_available'])
        response = self.rpc('create')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['code'], 'no_wallet_volume')
        self.assertEqual(self.rpc('public').status_code, 404)
        self.run.return_value = CompletedProcess([], 1, '', 'Cannot connect to the Docker daemon')
        self.assertEqual(self.rpc('list').status_code, 503)
        for call in self.run.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[:3], ['docker', 'volume', 'inspect'])
            self.assertEqual(argv[-1], PREFIX + WALLET)

    def test_worker_receives_only_its_named_home_and_private_packet(self):
        token = 'synthetic-share-capability'
        args = {'id': 'a' * 32, 'token': token}
        self.run.side_effect = [
            CompletedProcess([], 0, 'local\n', ''),
            CompletedProcess([], 0, json.dumps([{'title': 'Shared title', 'messages': []}, 200]), ''),
        ]
        with patch.object(intelligence_volume, '_image', return_value='isolated-image'):
            response = self.rpc('public', **args)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json['title'], 'Shared title')
        command = self.run.call_args_list[-1]
        argv = command.args[0]
        self.assertEqual(argv.count('--mount'), 1)
        mount = argv[argv.index('--mount') + 1].split(',')
        self.assertIn('source=' + PREFIX + WALLET, mount)
        self.assertIn('target=/wallet', mount)
        self.assertIn('readonly', mount)
        self.assertIn('--network=none', argv)
        self.assertIn('--user=1000:1000', argv)
        self.assertIn('--log-driver=none', argv)
        self.assertNotIn(token, ' '.join(argv))
        self.assertEqual(json.loads(command.kwargs['input']), {'operation': 'public', 'data': args})

    def test_wallet_maintenance_cannot_prune_between_volume_inspection_and_mount(self):
        inspected, prune_started, finish = (threading.Event() for _ in range(3))
        events = []

        def storage_process(argv, **kwargs):
            if argv[1:3] == ['volume', 'inspect']:
                events.append('inspect')
                inspected.set()
                self.assertTrue(finish.wait(timeout=3))
                return CompletedProcess(argv, 0, 'local', '')
            if argv[1] == 'run':
                events.append('run')
                return CompletedProcess(argv, 0, '[{"conversations": [], "storage_available": true}, 200]', '')
            self.assertEqual(argv, ['docker', 'volume', 'rm', PREFIX + WALLET])
            events.append('prune')
            return CompletedProcess(argv, 0, '', '')

        def prune():
            prune_started.set()
            launcher._prune_volume(PREFIX + WALLET)

        self.run.side_effect = storage_process
        with patch.object(intelligence_volume, '_image', return_value='isolated-image'), ThreadPoolExecutor(2) as pool:
            reading = pool.submit(self.rpc, 'list')
            self.assertTrue(inspected.wait(timeout=3))
            pruning = pool.submit(prune)
            try:
                self.assertTrue(prune_started.wait(timeout=3))
                self.assertFalse(pruning.done())
            finally:
                finish.set()
            self.assertEqual(reading.result().status_code, 200)
            pruning.result()
        self.assertEqual(events, ['inspect', 'run', 'prune'])
