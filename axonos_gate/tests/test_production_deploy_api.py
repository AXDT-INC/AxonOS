"""Effective Docker API and read-only image capability admission (no daemon)."""

import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('deploy_api_test', ROOT / 'scripts/deploy_production.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DockerApiAdmissionTests(unittest.TestCase):
    def versions(self, engine='28.5.2', effective='1.51'):
        maximum = '1.51' if engine == '28.5.2' else '1.54'
        return ({'ServerVersion': engine},
                {'Client': {'Version': engine, 'ApiVersion': effective, 'DefaultAPIVersion': maximum},
                 'Server': {'Version': engine, 'ApiVersion': maximum,
                            'MinAPIVersion': '1.24' if engine == '28.5.2' else '1.40', 'Os': 'linux'}})

    def test_api_147_is_rejected_even_when_engine_and_client_default_support_newer(self):
        info, version = self.versions(effective='1.47')
        for override in ('', '1.47'):
            with self.subTest(override=override), self.assertRaises(deploy.Refusal):
                deploy.docker_api_contract(info, version, override)

    def test_reviewed_engines_accept_supported_effective_apis_with_or_without_override(self):
        for engine, versions in (('28.5.2', ('1.48', '1.49', '1.50', '1.51')),
                                 ('29.5.1', ('1.48', '1.49', '1.50', '1.51', '1.52', '1.53', '1.54'))):
            for api in versions:
                for override in ('', api):
                    with self.subTest(engine=engine, api=api, override=override):
                        info, version = self.versions(engine, api)
                        original = copy.deepcopy(version)
                        self.assertEqual(deploy.docker_api_contract(info, version, override), api)
                        self.assertEqual(version, original)

    def test_negotiation_to_older_client_but_supported_api_is_allowed(self):
        info, version = self.versions('29.5.1', '1.48')
        version['Client']['DefaultAPIVersion'] = '1.48'
        self.assertEqual(deploy.docker_api_contract(info, version, ''), '1.48')

    def test_old_client_forced_above_its_supported_api_is_rejected(self):
        info, version = self.versions(effective='1.48')
        version['Client']['DefaultAPIVersion'] = '1.47'
        with self.assertRaises(deploy.Refusal):
            deploy.docker_api_contract(info, version, '1.48')

    def test_override_is_not_silently_ignored_or_normalized(self):
        info, version = self.versions(effective='1.48')
        for override in ('1.47', '1.49', 'v1.48', ' 1.48', '1.048', '1.48\n', 'garbage'):
            with self.subTest(override=override), self.assertRaises(deploy.Refusal):
                deploy.docker_api_contract(info, version, override)

    def test_api_above_daemon_max_or_below_configured_min_is_rejected(self):
        info, version = self.versions(effective='1.52')
        version['Client']['DefaultAPIVersion'] = '1.54'
        with self.assertRaises(deploy.Refusal):
            deploy.docker_api_contract(info, version, '1.52')
        info, version = self.versions(effective='1.48')
        version['Server']['MinAPIVersion'] = '1.49'
        with self.assertRaises(deploy.Refusal):
            deploy.docker_api_contract(info, version, '')

    def test_missing_malformed_or_unknown_version_metadata_fails_closed(self):
        info, baseline = self.versions()
        for side, key in (('Client', 'ApiVersion'), ('Client', 'DefaultAPIVersion'),
                          ('Server', 'ApiVersion'), ('Server', 'MinAPIVersion')):
            for value in (None, '', 'v1.51', 1.51, '1.51.0', '2.00', ['1.51']):
                with self.subTest(side=side, key=key, value=value):
                    version = copy.deepcopy(baseline)
                    version[side][key] = value
                    with self.assertRaises(deploy.Refusal):
                        deploy.docker_api_contract(info, version, '')
        for version in ({}, {'Client': {}}, {'Client': baseline['Client'], 'Server': None}):
            with self.subTest(version=version), self.assertRaises(deploy.Refusal):
                deploy.docker_api_contract(info, version, '')
        for key, value in (('Version', '29.5.1'), ('Os', 'windows'), ('ApiVersion', '1.99')):
            version = copy.deepcopy(baseline)
            version['Server'][key] = value
            with self.subTest(key=key), self.assertRaises(deploy.Refusal):
                deploy.docker_api_contract(info, version, '')


class ImageApiProbeTests(unittest.TestCase):
    def setUp(self):
        self.identity = 'sha256:' + 'a' * 64
        self.item = {'Id': self.identity, 'Config': {'Labels': {'existing': 'image'}},
                     'RepoTags': ['axonos:latest'], 'RepoDigests': ['axonos@' + self.identity],
                     'Descriptor': {'digest': self.identity, 'mediaType': 'application/vnd.oci.image.index.v1+json'}}
        self.response = [self.item]
        self.calls = []
        self.controller = deploy.Deployment.__new__(deploy.Deployment)
        self.controller.docker = self.docker

    def docker(self, *args, **kwargs):
        self.calls.append(args)
        self.assertEqual(args, ('image', 'inspect', self.identity))
        self.assertTrue(kwargs['capture'])
        return json.dumps(self.response)

    def test_probe_uses_existing_immutable_image_no_candidate_or_mutation(self):
        self.controller.probe_image_api(self.identity, 'containerd')
        self.assertEqual(self.calls, [('image', 'inspect', self.identity)])

    def test_probe_accepts_untagged_existing_image_and_optional_empty_labels(self):
        self.item.update(RepoTags=[], RepoDigests=[])
        for labels in (None, {}):
            self.item['Config']['Labels'] = labels
            self.controller.probe_image_api(self.identity, 'containerd')

    def test_probe_accepts_classic_without_descriptor(self):
        self.item.pop('Descriptor')
        self.item.update(GraphDriver={'Name': 'overlay2', 'Data': {}}, RepoDigests=[])
        self.controller.probe_image_api(self.identity, 'classic')

    def test_descriptor_omitted_despite_new_api_text_is_rejected(self):
        self.item.pop('Descriptor')
        with self.assertRaises(deploy.Refusal):
            self.controller.probe_image_api(self.identity, 'containerd')

    def test_missing_unknown_or_inconsistent_required_image_metadata_is_rejected(self):
        original = copy.deepcopy(self.item)
        for key, value in (('Id', 'sha256:' + 'b' * 64), ('Config', None),
                           ('Config', {'Labels': 'not-a-map'}), ('Config', {'Labels': {'owner': 1}}),
                           ('RepoTags', None), ('RepoDigests', []), ('Descriptor', {})):
            self.response = [dict(copy.deepcopy(original), **{key: value})]
            with self.subTest(key=key, value=value), self.assertRaises(deploy.Refusal):
                self.controller.probe_image_api(self.identity, 'containerd')
        for response in ([], [original, original], [None], {}):
            self.response = response
            with self.subTest(response=response), self.assertRaises(deploy.Refusal):
                self.controller.probe_image_api(self.identity, 'containerd')

    def test_tag_or_missing_image_identity_cannot_be_used_as_probe(self):
        for identity in (None, 'axonos:latest', 'a' * 12, 'sha256:bad'):
            with self.subTest(identity=identity), self.assertRaises(deploy.Refusal):
                self.controller.probe_image_api(identity, 'containerd')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
