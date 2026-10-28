"""Preparation admission and provenance regressions; no daemon or secret reads."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import types
import unittest
from unittest import mock

from axonos_gate import x_capi, x_capi_handoff_http


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('deploy_preparation_test', ROOT / 'scripts/deploy_production.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class PreparationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.controller = deploy.Deployment.__new__(deploy.Deployment)
        self.controller.root = ROOT
        self.controller.head = 'a' * 40
        self.controller.image_id = 'sha256:' + 'b' * 64
        self.controller.options = types.SimpleNamespace(
            nvidia_package=deploy.PRODUCTION_NVIDIA, password_secret_file=None)
        self.controller.docker = mock.Mock(side_effect=AssertionError('Unexpected Docker operation'))

    def test_both_credential_layers_require_buildkit_secret_and_nonce(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        self.assertEqual(dockerfile.count('--mount=type=secret,id=axonos_password,required=true'), 2)
        self.assertEqual(dockerfile.count('${AXONOS_CREDENTIAL_CACHE_NONCE:?fresh build nonce required}'), 2)
        self.assertNotIn('ARG PASSWORD', dockerfile)

    def provenance(self):
        return [{'Id': self.controller.image_id, 'Config': {'Labels': {
            deploy.CANDIDATE_OWNER: deploy.CANDIDATE_TOOL,
            deploy.REVISION_LABEL: self.controller.head,
            deploy.NVIDIA_LABEL: deploy.PRODUCTION_NVIDIA,
            deploy.NONCE_LABEL: 'c' * 64}}}]

    def test_matching_immutable_provenance_is_accepted(self):
        self.controller.docker.side_effect = None
        self.controller.docker.return_value = json.dumps(self.provenance())
        self.controller.attest_image()
        self.assertEqual(self.controller.docker.call_args.args,
                         ('image', 'inspect', self.controller.image_id))

    def test_missing_and_mismatched_provenance_fields_fail_closed(self):
        for field in self.provenance()[0]['Config']['Labels']:
            for value in (None, 'wrong'):
                with self.subTest(field=field, value=value):
                    document = copy.deepcopy(self.provenance())
                    labels = document[0]['Config']['Labels']
                    if value is None:
                        del labels[field]
                    else:
                        labels[field] = value
                    self.controller.docker.side_effect = None
                    self.controller.docker.return_value = json.dumps(document)
                    with self.assertRaises(deploy.Refusal):
                        self.controller.attest_image()

    def test_worker_reuse_requires_worker_role_and_same_provenance(self):
        document = self.provenance()
        self.controller.docker.side_effect = None
        self.controller.docker.return_value = json.dumps(document)
        with self.assertRaises(deploy.Refusal):
            self.controller.attest_image('worker')
        document[0]['Config']['Labels'][deploy.ROLE_LABEL] = 'worker'
        self.controller.docker.return_value = json.dumps(document)
        self.controller.attest_image('worker')
        for field in (deploy.REVISION_LABEL, deploy.NVIDIA_LABEL, deploy.NONCE_LABEL):
            bad = copy.deepcopy(document)
            bad[0]['Config']['Labels'][field] = 'wrong'
            self.controller.docker.return_value = json.dumps(bad)
            with self.subTest(field=field), self.assertRaises(deploy.Refusal):
                self.controller.attest_image('worker')

    def test_tag_is_refused_before_docker_inspection(self):
        self.controller.image_id = 'axonos:latest'
        with self.assertRaises(deploy.Refusal):
            self.controller.attest_image()
        self.controller.docker.assert_not_called()

    def test_different_inspected_identity_is_refused(self):
        document = self.provenance()
        document[0]['Id'] = 'sha256:' + 'd' * 64
        self.controller.docker.side_effect = None
        self.controller.docker.return_value = json.dumps(document)
        with self.assertRaises(deploy.Refusal):
            self.controller.attest_image()

    def test_build_only_refuses_unreviewed_engine_and_old_effective_api(self):
        for engine, effective in (('29.5.99', '1.51'), ('28.5.2', '1.47')):
            with self.subTest(engine=engine, effective=effective):
                info = {'OSType': 'linux', 'ServerVersion': engine,
                        'Driver': 'overlay2', 'DriverStatus': []}
                version = {'Client': {'ApiVersion': effective, 'DefaultAPIVersion': '1.51'},
                           'Server': {'Version': engine, 'ApiVersion': '1.51',
                                      'MinAPIVersion': '1.24', 'Os': 'linux'}}
                replies = {('buildx', 'version'): '',
                           ('info', '--format', '{{json .}}'): json.dumps(info),
                           ('version', '--format', '{{json .}}'): json.dumps(version)}
                self.controller.docker = mock.Mock(side_effect=lambda *args, **kw: replies[args])
                self.controller.clean_git = mock.Mock()
                self.controller.git = mock.Mock(return_value='reviewed')
                self.controller.env = {}
                with self.assertRaises(deploy.Refusal):
                    self.controller.preparation_preflight()
                self.assertTrue(all(call.args in replies for call in self.controller.docker.call_args_list))

    def test_absent_noncanonical_and_context_secret_paths_never_build(self):
        for value in (None, 'relative', str(ROOT / 'credential'),
                      '/tmp/not-present-controller-secret', '/tmp/secret,env=OTHER',
                      '/tmp/secret\nextra', '/tmp/secret*', '/tmp/secret?', '/tmp/secret[1]'):
            with self.subTest(path=value):
                self.controller.options.password_secret_file = value
                with self.assertRaises(deploy.Refusal):
                    self.controller.build_candidate()
        self.controller.docker.assert_not_called()

    def test_build_grants_read_access_only_to_validated_secret_file(self):
        self.controller.options.password_secret_file = '/tmp/controller-metadata-only'
        self.controller.options.builder = 'dedicated'
        self.controller.run_id = 'a' * 24
        self.controller.admit_builder = mock.Mock()
        self.controller.docker.side_effect = None
        self.controller.docker.return_value = 'sha256:' + 'b' * 64
        metadata = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=1)
        with mock.patch.object(Path, 'resolve', lambda path: path), \
                mock.patch.object(Path, 'lstat', return_value=metadata), \
                mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('Secret read')), \
                mock.patch.object(Path, 'read_text', side_effect=AssertionError('Secret read')):
            self.controller.build_candidate()
        self.controller.admit_builder.assert_called_once()
        call = self.controller.docker.call_args_list[0]
        self.assertEqual(call.args, ('buildx', 'bake', '--builder', 'dedicated',
            '--file', '-', '--allow', 'fs.read=/tmp/controller-metadata-only', '--load', 'axonos'))
        plan = json.loads(call.kwargs['input_text'])
        self.assertEqual(plan['target']['axonos']['secret'],
                         ['id=axonos_password,src=/tmp/controller-metadata-only'])

    def test_unsafe_secret_metadata_is_rejected_without_reading_contents(self):
        self.controller.options.password_secret_file = '/tmp/controller-metadata-only'
        for mode, size in ((stat.S_IFREG | 0o644, 1), (stat.S_IFREG | 0o600, 0),
                           (stat.S_IFDIR | 0o700, 1), (stat.S_IFLNK | 0o600, 1)):
            with self.subTest(mode=mode, size=size):
                metadata = types.SimpleNamespace(st_mode=mode, st_size=size)
                with mock.patch.object(Path, 'resolve', lambda path: path), \
                        mock.patch.object(Path, 'lstat', return_value=metadata), \
                        mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('Secret read')), \
                        mock.patch.object(Path, 'read_text', side_effect=AssertionError('Secret read')):
                    with self.assertRaises(deploy.Refusal):
                        self.controller.build_candidate()
        self.controller.docker.assert_not_called()

    def test_capi_and_handoff_remain_disabled_without_explicit_configuration(self):
        config = x_capi.load_config({})
        self.assertEqual(config.mode, 'off')
        self.assertFalse(config.producer_ready)
        for environment in ({}, {'X_CAPI_MODE': 'dry_run'}, {'X_HANDOFF_ENABLED': 'true'}):
            with self.subTest(environment=environment), mock.patch.dict(os.environ, environment, clear=True):
                self.assertFalse(x_capi_handoff_http._positive_enabled())


if __name__ == '__main__':
    unittest.main()
