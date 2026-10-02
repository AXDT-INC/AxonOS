"""Pure inspect-fixture checks; no Docker, service, secret or network access."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / 'scripts/deploy_production_resources.py'
SPEC = importlib.util.spec_from_file_location('deploy_resources_test_module', PATH)
resources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resources)
SECRET = 'SYNTHETIC_ONLY_DO_NOT_PRINT'


def fixture():
    """Docker Engine style fields, including generated IPAM and resource labels."""
    db_env = {'POSTGRES_USER': 'synthetic', 'POSTGRES_PASSWORD': SECRET, 'POSTGRES_DB': 'synthetic'}
    url = 'postgresql://synthetic:' + SECRET + '@postgres:5432/synthetic'
    launcher_env = {'AXGT_SESSION_LAUNCHER_TOKEN': SECRET,
                    'AXGT_SESSION_LAUNCHER_BIND_HOST': '0.0.0.0',
                    'AXGT_SESSION_LAUNCHER_BIND_PORT': '8090',
                    'AXGT_CHALLENGE_DB_URL': url,
                    'AXGT_HOST_SESSION_CONTAINER_IMAGE': 'axonos:latest',
                    'AXGT_HOST_SESSION_NETWORK_ISOLATION': 'true'}
    capi_env = {'X_CAPI_MODE': 'off'}
    gate_env = dict(capi_env, AXGT_SESSION_LAUNCHER_TOKEN=SECRET,
                    AXGT_SESSION_LAUNCHER_URL='http://axonos-launcher:8090',
                    AXGT_SESSION_LAUNCHER_MODE='http', AXGT_CHALLENGE_DB_URL=url)
    document = {'name': 'axonos', 'services': {
        'axonos': {'environment': gate_env, 'networks': {'axonos_control': None}}, 'x-capi-worker': {'environment': capi_env},
        'postgres': {'image': 'postgres:15-alpine', 'environment': db_env,
                     'networks': {'axonos_control': None},
                     'volumes': [{'type': 'volume', 'source': 'axonos_postgres_data',
                                  'target': '/var/lib/postgresql/data'}]},
        'axonos-launcher': {'environment': launcher_env, 'networks': {'axonos_control': None},
                           'privileged': True,
                           'volumes': [{'type': 'bind', 'source': '/var/run/docker.sock',
                                        'target': '/var/run/docker.sock'}]}},
        'networks': {'x_capi_db': {'name': 'axonos_x_capi_db', 'internal': True, 'driver': 'bridge'},
                     'axonos_control': {'name': 'axonos_control'}},
        'volumes': {'axonos_postgres_data': {'name': 'axonos_axonos_postgres_data'}}}
    volumes = []
    for name in resources.CAPI_VOLUMES:
        actual_name = 'axonos_' + name
        document['volumes'][name] = {'name': actual_name}
        volumes.append({'Name': actual_name, 'Driver': 'local', 'Scope': 'local', 'Options': None,
                        'Mountpoint': '/var/lib/docker/volumes/' + actual_name + '/_data',
                        'Labels': {'com.docker.compose.project': 'axonos',
                                   'com.docker.compose.volume': name,
                                   'com.docker.compose.version': '2.39.2'}})
    dependencies = {}
    for index, service in enumerate(('postgres', 'axonos-launcher', *resources.CAPI_SERVICES), 1):
        desired = document['services'].get(service, {})
        networks = {'axonos_control': {}} if service in ('postgres', 'axonos-launcher') else {'axonos_x_capi_db': {}}
        mounts = []
        for mount in desired.get('volumes', []):
            actual = {'Type': mount['type'], 'Destination': mount['target'], 'RW': True}
            if mount['type'] == 'volume':
                actual['Name'] = document['volumes'][mount['source']]['name']
            else:
                actual['Source'] = mount['source']
            mounts.append(actual)
        config = {'Image': desired.get('image', 'axonos-' + service), 'User': '',
                  'Env': [key + '=' + value for key, value in desired.get('environment', {}).items()],
                  'Labels': {'com.docker.compose.project': 'axonos', 'com.docker.compose.service': service}}
        if service == 'postgres':
            config.update(Cmd=['postgres'], Entrypoint=['docker-entrypoint.sh'])
            config['Env'].append('PGDATA=/var/lib/postgresql/data')
        elif service == 'axonos-launcher':
            config.update(Cmd=['python3', '/app/session_launcher_service.py'], Entrypoint=None)
        dependencies[service] = [{'Id': format(index, '064x'), 'Config': config,
                                  'Mounts': mounts, 'NetworkSettings': {'Networks': networks},
                                  'HostConfig': {'Privileged': service == 'axonos-launcher', 'ReadonlyRootfs': False}}]
    network = {'Name': 'axonos_x_capi_db', 'Id': 'e' * 64, 'Driver': 'bridge', 'Scope': 'local',
               'Internal': True, 'Attachable': False, 'Ingress': False, 'EnableIPv6': False,
               'IPAM': {'Driver': 'default', 'Options': None,
                        'Config': [{'Subnet': '172.29.0.0/16', 'Gateway': '172.29.0.1'}]},
               'Options': {}, 'Labels': {'com.docker.compose.project': 'axonos',
                                         'com.docker.compose.network': 'x_capi_db'},
               'Containers': {dependencies[name][0]['Id']: {'Name': 'axonos_' + name.replace('-', '_')}
                              for name in resources.CAPI_SERVICES}}
    control = copy.deepcopy(network)
    control.update(Name='axonos_control', Internal=False, Containers={})
    control['Labels']['com.docker.compose.network'] = 'axonos_control'
    core_volume = copy.deepcopy(volumes[0])
    core_volume.update(Name='axonos_axonos_postgres_data', Mountpoint='/var/lib/docker/volumes/axonos_axonos_postgres_data/_data')
    core_volume['Labels']['com.docker.compose.volume'] = 'axonos_postgres_data'
    return {'config': document, 'networks': [network, control], 'volumes': [*volumes, core_volume], 'dependencies': dependencies}


class ResourceAttestationTests(unittest.TestCase):
    def setUp(self):
        self.payload = fixture()

    def validate(self):
        resources.validate(self.payload, ROOT)

    def rejected(self):
        with self.assertRaises((resources.Refusal, KeyError, ValueError)):
            self.validate()

    def test_reviewed_resources_and_dependencies_pass(self):
        self.validate()

    def test_real_host_null_startup_inherits_reviewed_image_defaults(self):
        services = self.payload['config']['services']
        services['axonos-launcher']['command'] = None
        services['postgres'].update(command=None, entrypoint=None)
        # Actual Config is deliberately independent of the desired nulls.
        self.validate()
        services['axonos-launcher']['entrypoint'] = None
        self.validate()

    def test_null_inheritance_does_not_accept_actual_startup_drift(self):
        for service in ('axonos-launcher', 'postgres'):
            for field in ('Cmd', 'Entrypoint'):
                for actual in (None, [], ['unexpected', '--argument']):
                    if service == 'axonos-launcher' and field == 'Entrypoint' and actual in (None, []):
                        continue  # Both represent the reviewed absence of an entrypoint.
                    with self.subTest(service=service, field=field, actual=actual):
                        self.payload = fixture()
                        self.payload['config']['services'][service].update(command=None, entrypoint=None)
                        self.payload['dependencies'][service][0]['Config'][field] = actual
                        self.rejected()

    def test_explicit_empty_command_never_inherits_image_command(self):
        for service in ('axonos-launcher', 'postgres'):
            with self.subTest(service=service):
                self.payload = fixture()
                self.payload['config']['services'][service]['command'] = []
                # Even if Engine's len-zero merge restores the image CMD, it
                # must not satisfy the explicitly empty Compose contract.
                self.rejected()
                for actual in ([], None):
                    self.payload['dependencies'][service][0]['Config']['Cmd'] = actual
                    self.validate()

    def test_explicit_empty_entrypoint_never_inherits_image_entrypoint(self):
        desired = self.payload['config']['services']['postgres']
        desired.update(command=['postgres'], entrypoint=[])
        self.rejected()
        for actual in ([], None):
            self.payload['dependencies']['postgres'][0]['Config']['Entrypoint'] = actual
            self.validate()

    def test_explicit_entrypoint_suppresses_inherited_command(self):
        for entrypoint in ([], ['/reviewed-wrapper', '--flag']):
            for command in ({}, {'command': None}):
                with self.subTest(entrypoint=entrypoint, command=command):
                    self.payload = fixture()
                    desired = self.payload['config']['services']['postgres']
                    desired.update(entrypoint=entrypoint, **command)
                    actual = self.payload['dependencies']['postgres'][0]['Config']
                    actual['Entrypoint'] = entrypoint
                    # An image-default CMD is not an explicit command override.
                    self.rejected()
                    for empty in (None, []):
                        actual['Cmd'] = empty
                        self.validate()

    def test_explicit_startup_overrides_require_exact_argv(self):
        for service in ('axonos-launcher', 'postgres'):
            with self.subTest(service=service):
                self.payload = fixture()
                desired = self.payload['config']['services'][service]
                desired.update(command=['reviewed-command', 'one argument'],
                               entrypoint=['/reviewed-wrapper', '--flag'])
                self.rejected()
                actual = self.payload['dependencies'][service][0]['Config']
                actual.update(Cmd=list(desired['command']), Entrypoint=list(desired['entrypoint']))
                self.validate()
                for field in ('Cmd', 'Entrypoint'):
                    saved = actual[field]
                    for value in (None, [], [*saved, 'extra'], list(reversed(saved))):
                        actual[field] = value
                        self.rejected()
                    actual[field] = saved

    def test_non_normalized_startup_metadata_refused(self):
        for field, inspect_field in (('command', 'Cmd'), ('entrypoint', 'Entrypoint')):
            for value in ('', 'postgres', False, 0, {}, [None], [False]):
                with self.subTest(field=field, value=value):
                    self.payload = fixture()
                    self.payload['config']['services']['postgres'][field] = value
                    self.payload['dependencies']['postgres'][0]['Config'][inspect_field] = value
                    self.rejected()

    def test_missing_actual_startup_metadata_refused_even_when_empty_expected(self):
        for field in ('Cmd', 'Entrypoint'):
            with self.subTest(field=field):
                self.payload = fixture()
                self.payload['config']['services']['axonos-launcher'].update(command=[], entrypoint=[])
                actual = self.payload['dependencies']['axonos-launcher'][0]['Config']
                actual.update(Cmd=[], Entrypoint=[])
                del actual[field]
                self.rejected()

    def test_compose_hash_wire_representation(self):
        import hashlib
        # Fixed expected byte representations, not hashes self-generated from
        # the implementation. Go IPAM is a value struct, even when omitted.
        expected = b'{"name":"axonos_x_capi_db","driver":"bridge","ipam":{},"internal":true}'
        self.assertEqual(resources.compose_resource_hash(self.payload['config']['networks']['x_capi_db'], 'network'),
                         hashlib.sha256(expected).hexdigest())
        expected = b'{"name":"axonos_x_capi_runtime","driver":"local"}'
        self.assertEqual(resources.compose_resource_hash(self.payload['config']['volumes']['x_capi_runtime'], 'volume'),
                         hashlib.sha256(expected).hexdigest())

    def test_matching_compose_hashes_pass_and_mismatch_cannot_reconcile(self):
        for kind, desired, actual in [
            ('network', self.payload['config']['networks']['x_capi_db'], self.payload['networks'][0]),
            *[('volume', self.payload['config']['volumes'][key], actual)
              for key, actual in zip(resources.CAPI_VOLUMES, self.payload['volumes'])]]:
            label = 'com.docker.compose.config-hash'
            expected = resources.compose_resource_hash(desired, kind)
            actual['Labels'][label] = expected
            self.validate()
            actual['Labels'][label] = '0' * 64
            self.rejected()
            actual['Labels'][label] = expected

    def test_empty_legacy_network_hash_allowed_empty_volume_hash_refused(self):
        self.payload['networks'][0]['Labels']['com.docker.compose.config-hash'] = ''
        self.validate()
        self.payload['volumes'][0]['Labels']['com.docker.compose.config-hash'] = ''
        self.rejected()

    def test_unknown_hash_configuration_fails_closed(self):
        self.payload['config']['networks']['x_capi_db']['future_compose_option'] = True
        self.rejected()

    def test_preserved_control_network_and_core_volume_hash_drift_refused(self):
        for kind, index in (('networks', 1), ('volumes', 3)):
            self.payload = fixture()
            self.payload[kind][index]['Labels']['com.docker.compose.config-hash'] = '0' * 64
            self.rejected()

    def test_gate_requires_control_network_and_cannot_join_capi(self):
        self.payload['config']['services']['axonos']['networks'] = {'axonos_stack': None}
        self.rejected()
        self.payload['config']['services']['axonos']['networks'] = {'axonos_control': None, 'x_capi_db': None}
        self.rejected()

    @unittest.skipUnless(shutil.which('docker'), 'requires offline Compose CLI')
    def test_real_compose_and_bake_rendering_with_synthetic_env_no_daemon(self):
        # The service-level env_file is relative to this synthetic project, so
        # neither the repository .env nor operator Docker configuration is read.
        with tempfile.TemporaryDirectory(prefix='.deploy-compose-', dir=ROOT) as directory:
            project = Path(directory)
            (project / '.env').write_text('X_CAPI_MODE=off\nPASSWORD=SYNTHETIC_ONLY\n')
            (project / 'docker-config').mkdir()
            for filename in ('docker-compose.yml', 'docker-compose.x-capi.yml'):
                shutil.copyfile(ROOT / filename, project / filename)
            env = {'PATH': os.environ['PATH'], 'DOCKER_CONFIG': str(project / 'docker-config'),
                   'COMPOSE_REMOVE_ORPHANS': 'false', 'COMPOSE_IGNORE_ORPHANS': 'true'}
            command = ['docker', '--host', 'unix:///DO_NOT_CONTACT_DAEMON.sock', 'compose',
                       '--project-directory', str(project), '--project-name', 'axonos',
                       '--env-file', str(project / '.env'), '-f', str(project / 'docker-compose.yml')]
            def output(args):
                result = subprocess.run(args, env=env, cwd=project, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 0, 'Offline Compose rendering failed')
                return result.stdout
            output([*command, 'config', '--quiet'])
            combined = [*command, '-f', str(project / 'docker-compose.x-capi.yml'), '--profile', 'x-capi']
            output([*combined, 'config', '--quiet'])
            document = json.loads(output([*combined, 'config', '--format', 'json']))
            resources.validate_shared(document, ROOT)
            for service in ('axonos-launcher', 'postgres'):
                for field in ('command', 'entrypoint'):
                    self.assertIsNone(document['services'][service].get(field))
                    self.payload['config']['services'][service][field] = document['services'][service].get(field)
            self.validate()  # Real rendered nulls against image-default inspect fixtures.
            for kind in ('network', 'volume'):
                for desired in document[kind + 's'].values():
                    if not desired.get('external'):
                        self.assertEqual(len(resources.compose_resource_hash(desired, kind)), 64)
            plan = json.loads(output([*combined, 'build', '--print', '--build-arg', 'AXONOS_SKIP_HEAVY=0', 'axonos']))
            self.assertEqual((project / plan['target']['axonos']['context']).resolve(), project)
            self.assertEqual(plan['target']['axonos']['args']['AXONOS_SKIP_HEAVY'], '0')

    @unittest.skipUnless(shutil.which('docker'), 'requires offline Compose CLI')
    def test_real_compose_startup_null_empty_and_override_rendering_no_daemon(self):
        variants = ({}, {'value': None}, {'value': []}, {'value': ''},
                    {'value': ['executable', 'one argument']}, {'value': 'executable "one argument"'})
        normalized = (None, None, [], [], ['executable', 'one argument'], ['executable', 'one argument'])
        with tempfile.TemporaryDirectory(prefix='.deploy-startup-compose-', dir=ROOT) as directory:
            project = Path(directory)
            (project / 'docker-config').mkdir()
            services = {}
            for cmd_index, command in enumerate(variants):
                for ep_index, entrypoint in enumerate(variants):
                    service = {'image': 'synthetic-never-pulled:local'}
                    if command:
                        service['command'] = command['value']
                    if entrypoint:
                        service['entrypoint'] = entrypoint['value']
                    services[f'case-{cmd_index}-{ep_index}'] = service
            source = project / 'compose.json'
            source.write_text(json.dumps({'services': services}))
            result = subprocess.run(
                ['docker', '--host', 'unix:///DO_NOT_CONTACT_DAEMON.sock', 'compose',
                 '--project-directory', str(project), '--project-name', 'startup-contract',
                 '--env-file', '/dev/null', '-f', str(source), 'config', '--format', 'json'],
                env={'PATH': os.environ['PATH'], 'DOCKER_CONFIG': str(project / 'docker-config')},
                cwd=project, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, 'Offline startup Compose rendering failed')
            rendered = json.loads(result.stdout)['services']
            for cmd_index in range(len(variants)):
                for ep_index in range(len(variants)):
                    actual = rendered[f'case-{cmd_index}-{ep_index}']
                    self.assertEqual(actual['command'], normalized[cmd_index])
                    self.assertEqual(actual['entrypoint'], normalized[ep_index])

    def test_network_drift_rejected(self):
        for field, value in (('Driver', 'overlay'), ('Scope', 'swarm'), ('Internal', False),
                             ('Attachable', True), ('EnableIPv6', True), ('EnableIPv4', False),
                             ('Ingress', True), ('ConfigOnly', True),
                             ('Options', {'encrypted': ''}), ('ConfigFrom', {'Network': 'other'})):
            with self.subTest(field=field):
                self.payload = fixture()
                self.payload['networks'][0][field] = value
                self.rejected()

    def test_network_ipam_drift_rejected(self):
        for value in ({'Driver': 'custom'}, {'Driver': 'default', 'Options': {'secret': SECRET}},
                      {'Driver': 'default', 'Config': []},
                      {'Driver': 'default', 'Config': [{'Subnet': '172.29.0.0/16', 'Gateway': '10.0.0.1'}]},
                      {'Driver': 'default', 'Config': [{'Subnet': '172.29.0.0/16', 'Gateway': '172.29.0.1',
                                                        'AuxiliaryAddresses': {'other': '172.29.0.2'}}]}):
            with self.subTest(value=value):
                self.payload = fixture()
                self.payload['networks'][0]['IPAM'] = value
                self.rejected()

    def test_automatic_subnet_is_not_treated_as_configured_drift(self):
        self.payload['networks'][0]['IPAM']['Config'] = [{'Subnet': '10.33.0.0/16', 'Gateway': '10.33.0.1'}]
        self.validate()

    def test_explicit_ipam_pool_mismatch_rejected(self):
        self.payload['config']['networks']['x_capi_db']['ipam'] = {
            'driver': 'default', 'config': [{'subnet': '172.29.0.0/16', 'gateway': '172.29.0.1'}]}
        self.validate()
        self.payload['config']['networks']['x_capi_db']['ipam']['config'][0]['gateway'] = '172.29.0.2'
        self.rejected()

    def test_unreviewed_network_endpoint_rejected_even_with_capi_name(self):
        self.payload['networks'][0]['Containers']['f' * 64] = {'Name': 'axonos_x_capi_worker'}
        self.rejected()

    def test_core_endpoint_rejected(self):
        self.payload['networks'][0]['Containers'][self.payload['dependencies']['postgres'][0]['Id']] = {'Name': 'axonos_postgres'}
        self.rejected()

    def test_legitimate_stopped_capi_services_need_not_have_live_endpoints(self):
        self.payload['networks'][0]['Containers'] = {}
        self.payload['dependencies']['x-capi-worker'] = []
        self.validate()

    def test_legitimate_endpoint_extra_network_rejected(self):
        self.payload['dependencies']['x-capi-worker'][0]['NetworkSettings']['Networks']['axonos_control'] = {}
        self.rejected()

    def test_fake_endpoint_project_or_service_label_rejected(self):
        for label in ('project', 'service'):
            self.payload = fixture()
            self.payload['dependencies']['x-capi-worker'][0]['Config']['Labels']['com.docker.compose.' + label] = 'postgres'
            self.rejected()

    def test_volume_driver_storage_or_ownership_drift_rejected(self):
        for field, value in (('Driver', 'nfs'), ('Scope', 'global'),
                             ('Options', {'type': 'none', 'device': '/production', 'o': 'bind'}),
                             ('ClusterVolume', {'ID': 'unexpected'}),
                             ('Mountpoint', '/var/lib/docker/volumes/other/_data'),
                             ('Labels', {'com.docker.compose.project': 'other', 'com.docker.compose.volume': 'x_capi_runtime'}),
                             ('Labels', {'com.docker.compose.project': 'axonos', 'com.docker.compose.volume': 'other'})):
            with self.subTest(field=field):
                self.payload = fixture()
                self.payload['volumes'][0][field] = value
                self.rejected()

    def test_resource_custom_labels_compare_but_engine_compose_version_does_not(self):
        self.payload['volumes'][0]['Labels']['com.docker.compose.version'] = '2.40.1'
        self.validate()
        self.payload['volumes'][0]['Labels']['unreviewed'] = 'true'
        self.rejected()

    def test_missing_or_duplicate_resource_rejected_no_implicit_creation(self):
        for kind in ('networks', 'volumes'):
            self.payload = fixture()
            self.payload[kind].pop()
            self.rejected()
            self.payload = fixture()
            self.payload[kind].append(copy.deepcopy(self.payload[kind][0]))
            self.rejected()

    def test_target_cannot_adopt_external_storage(self):
        self.payload['config']['volumes']['x_capi_postgres_data']['external'] = True
        self.rejected()

    def test_shared_scope_off_mode_each_exclusion_source_checked(self):
        for key in resources.EXCLUSIONS:
            with self.subTest(key=key):
                self.payload = fixture()
                self.payload['config']['services']['axonos']['environment'][key] = '0x' + 'a' * 40
                self.rejected()

    def test_same_exclusion_union_different_source_not_accepted(self):
        self.payload['config']['services']['axonos']['environment']['AXGT_REVENUE_WALLET'] = '0x' + 'a' * 40
        self.payload['config']['services']['x-capi-worker']['environment']['X_CAPI_EXCLUDED_WALLETS'] = '0x' + 'a' * 40
        self.rejected()

    def test_shared_normalized_config_accepts_equivalent_values(self):
        gate = self.payload['config']['services']['axonos']['environment']
        worker = self.payload['config']['services']['x-capi-worker']['environment']
        gate.update(X_CAPI_PRODUCTION_CHAIN_IDS='8453, 1', X_CAPI_SEND_VALUES='no',
                    X_CAPI_ATTRIBUTION_TTL_DAYS='07', X_CAPI_ALLOWED_ORIGIN='https://app.example/',
                    X_CAPI_EXCLUDED_WALLETS='0x' + 'A' * 40 + ', 0x' + 'b' * 40)
        worker.update(X_CAPI_PRODUCTION_CHAIN_IDS='1,8453', X_CAPI_SEND_VALUES='false',
                      X_CAPI_ATTRIBUTION_TTL_DAYS='7', X_CAPI_ALLOWED_ORIGIN='https://app.example',
                      X_CAPI_EXCLUDED_WALLETS='0x' + 'b' * 40 + ',0x' + 'a' * 40)
        self.validate()

    def test_shared_capi_policy_deployment_capacity_and_lifetime_mismatch_rejected(self):
        for key, value in (('X_CAPI_CONSENT_POLICY_VERSION', 'v2'), ('X_CAPI_CONSENT_POLICY_EPOCH', '2'),
                           ('X_CAPI_DEPLOYMENT_ID', 'elsewhere'), ('X_CAPI_PIXEL_ID', 'pixel2'),
                           ('X_CAPI_ATTRIBUTION_TTL_DAYS', '8'), ('X_CAPI_MAX_EVENT_AGE_HOURS', '25'),
                           ('X_CAPI_QUEUE_LIMIT', '20000'), ('X_CAPI_CONTEXT_LIMIT', '20000'),
                           ('X_CAPI_PRIVACY_RECOVERY_EPOCH', 'changed')):
            with self.subTest(key=key):
                self.payload = fixture()
                self.payload['config']['services']['x-capi-worker']['environment'][key] = value
                self.rejected()

    def test_shared_dry_run_audience_normalization(self):
        for name in ('axonos', 'x-capi-worker'):
            self.payload['config']['services'][name]['environment'].update(
                X_CAPI_MODE='dry_run', X_CAPI_PIXEL_ID='pixel1', X_CAPI_EVENT_SESSION_STARTED='event1',
                X_CAPI_DEPLOYMENT_ID='staging', X_CAPI_ALLOWED_ORIGIN='https://app.example',
                X_CAPI_CONSENT_POLICY_EPOCH='1')
        self.validate()
        self.payload['config']['services']['x-capi-worker']['environment']['X_CAPI_ALLOWED_ORIGIN'] = 'https://other.example'
        self.rejected()

    def set_actual_env(self, service, key, value):
        env = self.payload['dependencies'][service][0]['Config']['Env']
        env[:] = [entry for entry in env if not entry.startswith(key + '=')]
        env.append(key + '=' + value)

    def test_launcher_token_drift_rejected(self):
        self.set_actual_env('axonos-launcher', 'AXGT_SESSION_LAUNCHER_TOKEN', 'old-secret')
        self.rejected()

    def test_intended_gate_token_must_match_even_if_launcher_matches_own_config(self):
        self.payload['config']['services']['axonos']['environment']['AXGT_SESSION_LAUNCHER_TOKEN'] = 'different'
        self.rejected()

    def test_launcher_config_drift_or_hidden_prior_override_rejected(self):
        for key, value in (('AXGT_HOST_SESSION_NETWORK_ISOLATION', 'false'),
                           ('AXGT_HOST_SESSION_CONTAINER_IMAGE', 'unexpected:latest'),
                           ('WEBRTC_AGENT_INTERNAL_KEY', SECRET),
                           ('AXGT_SESSION_LAUNCHER_BIND_PORT', '9999')):
            self.payload = fixture()
            self.set_actual_env('axonos-launcher', key, value)
            self.rejected()

    def test_gate_wrong_launcher_endpoint_or_mode_rejected(self):
        for key, value in (('AXGT_SESSION_LAUNCHER_URL', 'http://other:8090'),
                           ('AXGT_SESSION_LAUNCHER_URL', 'http://axonos-launcher:9999'),
                           ('AXGT_SESSION_LAUNCHER_MODE', 'local')):
            self.payload = fixture()
            self.payload['config']['services']['axonos']['environment'][key] = value
            self.rejected()

    def test_core_credentials_drift_rejected(self):
        for key in ('POSTGRES_PASSWORD', 'POSTGRES_USER', 'POSTGRES_DB', 'POSTGRES_HOST_AUTH_METHOD', 'PGDATA'):
            with self.subTest(key=key):
                self.payload = fixture()
                self.set_actual_env('postgres', key, 'different')
                self.rejected()

    def test_gate_core_url_mismatch_rejected(self):
        for url in ('postgresql://synthetic:wrong@postgres:5432/synthetic',
                     'postgresql://synthetic:' + SECRET + '@core-other:5432/synthetic',
                     'postgresql://synthetic:' + SECRET + '@postgres:5433/synthetic',
                     'postgresql://synthetic:' + SECRET + '@postgres:5432/other',
                     'postgresql://synthetic:' + SECRET + '@postgres:5432/synthetic?options=-c'):
            self.payload = fixture()
            self.payload['config']['services']['axonos']['environment']['AXGT_CHALLENGE_DB_URL'] = url
            self.rejected()

    def test_core_url_percent_encoding_compared_semantically(self):
        self.payload['config']['services']['axonos']['environment']['AXGT_CHALLENGE_DB_URL'] = (
            'postgresql://%73ynthetic:' + SECRET + '@postgres/synthetic')
        self.validate()

    def test_core_mount_or_network_or_startup_drift_rejected(self):
        core = self.payload['dependencies']['postgres'][0]
        core['Mounts'][0]['Name'] = 'unrelated-volume'
        self.rejected()
        self.payload = fixture()
        self.payload['dependencies']['postgres'][0]['NetworkSettings']['Networks']['extra'] = {}
        self.rejected()
        self.payload = fixture()
        self.payload['dependencies']['postgres'][0]['Config']['Cmd'].extend(['-c', 'password_encryption=md5'])
        self.rejected()

    def test_secret_safe_cli_rejects_malformed_and_drift_without_echo(self):
        examples = ['{ malformed: ' + SECRET, json.dumps({'config': SECRET})]
        self.set_actual_env('axonos-launcher', 'AXGT_SESSION_LAUNCHER_TOKEN', 'other_' + SECRET)
        examples.append(json.dumps(self.payload))
        for source in examples:
            result = subprocess.run([sys.executable, str(PATH), str(ROOT)], input=source, text=True,
                                    capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(SECRET, result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_successful_cli_emits_only_fixed_summary(self):
        result = subprocess.run([sys.executable, str(PATH), str(ROOT)], input=json.dumps(self.payload), text=True,
                                capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'Resource and dependency contracts verified\n')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
