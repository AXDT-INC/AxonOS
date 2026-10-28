"""Builder admission negative matrix; no real Docker, mounts or credentials."""
import copy
import json
import types
import unittest
from pathlib import Path
from unittest import mock
from axonos_gate.tests.test_production_deploy_preparation import deploy


class BuilderAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.c = deploy.Deployment.__new__(deploy.Deployment)
        self.c.options = types.SimpleNamespace(builder='isolated', builder_id='f' * 64)
        self.metadata = ('Name: isolated\nDriver: docker-container\nNodes:\nName: isolated0\n'
                         'Endpoint: unix:///var/run/docker.sock\nStatus: running')
        self.item = {'Id': 'f' * 64, 'State': {'Running': True},
            'Config': {'Entrypoint': ['buildkitd'], 'Cmd': ['--config', '/etc/buildkit/buildkitd.toml'], 'Env': []},
            'HostConfig': {'NanoCpus': 2000000000, 'Memory': 8589934592, 'MemorySwap': 8589934592,
                           'PidsLimit': 512, 'CgroupnsMode': 'private'},
            'Mounts': [{'Type': 'volume', 'Name': 'buildx_buildkit_isolated0_state',
                        'Destination': '/var/lib/buildkit', 'RW': True}]}
        self.volume = {'Name': 'buildx_buildkit_isolated0_state', 'Driver': 'local', 'Options': None,
                       'Mountpoint': '/isolated/state'}
        self.users = 'f' * 64
        self.config = deploy.BUILDER_CONFIG
        self.limits = '200000 100000\n8589934592\n0\n512'
        self.c.builder_storage = mock.Mock()
        def docker(*args, **kw):
            if args[:2] == ('buildx', 'inspect'):
                self.assertEqual(args, ('buildx', 'inspect', 'isolated'))
                return self.metadata
            if args[0] == 'inspect': return json.dumps([self.item])
            if args[0] == 'volume': return json.dumps([self.volume])
            if args[0] == 'ps': return self.users
            if args[0] == 'exec':
                return self.config if args[-1].endswith('.toml') else self.limits
            raise AssertionError(args)
        self.c.docker = mock.Mock(side_effect=docker)

    def test_admits_exact_bounded_dedicated_builder(self):
        self.c.admit_builder()
        self.c.builder_storage.assert_called_once_with('/isolated/state')

    def test_missing_default_or_invalid_identity_never_contacts_docker(self):
        for name, identity in [(None, None), ('default', 'f'*64), ('isolated', None), ('isolated', 'short')]:
            self.c.options.builder, self.c.options.builder_id = name, identity
            with self.assertRaises(deploy.Refusal): self.c.admit_builder()
        self.c.docker.assert_not_called()

    def test_unsuitable_builder_metadata(self):
        original = self.metadata
        for value in [original.replace('docker-container', 'docker'),
                      original.replace('unix:///var/run/docker.sock', 'default'),
                      original.replace('running', 'stopped'), original + '\nName: extra',
                      original + '\nError: unavailable', original.replace('isolated0', 'unsafe/node')]:
            self.metadata = value
            with self.subTest(value=value), self.assertRaises(deploy.Refusal): self.c.admit_builder()

    def test_boundary_matrix(self):
        original = copy.deepcopy(self.item)
        for key, value in [('NanoCpus', 0), ('NanoCpus', 5000000000), ('Memory', 0),
                           ('MemorySwap', -1), ('MemorySwap', 17179869184), ('PidsLimit', 0),
                           ('PidsLimit', -1), ('PidsLimit', 1025), ('CgroupnsMode', 'host'),
                           ('OomKillDisable', True), ('PidMode', 'host'), ('Devices', [{}])]:
            self.item = copy.deepcopy(original)
            self.item['HostConfig'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(deploy.Refusal): self.c.admit_builder()

    def test_identity_command_storage_and_concurrency_fail_closed(self):
        for mutation in [lambda: self.item.update(Id='a'*64),
                         lambda: self.item['Config'].update(Cmd=['--oci-worker-max-parallelism=4']),
                         lambda: self.item['Mounts'].append({'Type': 'bind'}),
                         lambda: self.volume.update(Options={'device': '/production'}),
                         lambda: setattr(self, 'users', self.users + '\n' + 'a'*64),
                         lambda: setattr(self, 'config', self.config.replace('= 1', '= 2')),
                         lambda: setattr(self, 'limits', 'max 100000\nmax\nmax\nmax')]:
            self.setUp()
            mutation()
            with self.assertRaises(deploy.Refusal): self.c.admit_builder()

    def test_worker_build_selects_admitted_builder_and_never_compose(self):
        self.c.root = Path('/synthetic/source')
        self.c.head = 'a' * 40
        self.c.run_id = 'a' * 24
        self.c.image_id = 'sha256:' + 'a' * 64
        self.c.options.nvidia_package = deploy.PRODUCTION_NVIDIA
        self.c.maintain_candidates = mock.Mock()
        self.c.admit_builder = mock.Mock()
        self.c.attest_image = mock.Mock()
        self.c.validate_image = mock.Mock()
        self.c.compose_run = mock.Mock(side_effect=AssertionError('Compose during preparation'))
        worker = 'sha256:' + 'b' * 64
        self.c.docker = mock.Mock(side_effect=['', worker])
        self.c.prepare_worker()
        self.c.admit_builder.assert_called_once()
        command = self.c.docker.call_args_list[0]
        self.assertEqual(command.args[:4], ('buildx', 'bake', '--builder', 'isolated'))
        target = json.loads(command.kwargs['input_text'])['target']['worker']
        self.assertEqual(target['dockerfile'], 'docker/x-capi-worker/Dockerfile')
        self.assertEqual(target['labels'][deploy.ROLE_LABEL], 'worker')
        self.assertEqual(self.c.worker_image_id, worker)
        self.assertEqual(self.c.image_id, 'sha256:' + 'a' * 64)
        self.c.compose_run.assert_not_called()

    def test_storage_requires_separate_filesystem(self):
        c = deploy.Deployment.__new__(deploy.Deployment)
        with mock.patch.object(Path, 'resolve', lambda p: p), mock.patch('os.path.ismount', return_value=True), \
             mock.patch.object(Path, 'stat', side_effect=[types.SimpleNamespace(st_dev=2), types.SimpleNamespace(st_dev=1)]):
            c.builder_storage('/isolated/state')
        for mounted in (False, True):
            with mock.patch.object(Path, 'resolve', lambda p: p), mock.patch('os.path.ismount', return_value=mounted), \
                 mock.patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_dev=1)), self.assertRaises(deploy.Refusal):
                c.builder_storage('/shared/state')


if __name__ == '__main__': unittest.main()
