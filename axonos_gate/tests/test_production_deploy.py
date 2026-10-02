"""Deployment control-flow tests: synthetic PATH, no Docker/network/secrets.

Every subprocess runs in a temporary synthetic checkout under this worktree.
Docker, Git and curl are executable fakes. The actual Python checks run; only
secret filesystem metadata is replaced in orchestration tests, and that small
checker has independent metadata-only tests below. No production state is read.
"""

from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts/deploy-production.sh'
IMAGE = 'sha256:' + 'a' * 64
SECRET = 'SYNTHETIC_SECRET_MUST_NOT_APPEAR'
SERVICES = ('postgres', 'axonos-launcher', 'axonos', 'x-capi-postgres',
            'x-capi-db-init', 'x-capi-privacy-init', 'x-capi-worker')


def volume(target, source, kind='volume', readonly=False):
    item = dict(type=kind, source=source, target=target, read_only=readonly)
    if kind == 'bind':
        item['bind'] = {'create_host_path': False}
    return item


def configuration(root):
    context = '/etc/axonos/x-capi-context-key'
    runtime = volume('/run/axonos-x-capi', 'x_capi_runtime')
    privacy = volume('/run/axonos-x-capi-privacy', 'x_capi_privacy_fence')
    key = volume('/run/secrets/x_capi_context_key', context, 'bind', True)
    env = {'X_CAPI_MODE': 'off', 'X_CAPI_CONTEXT_KEY_FILE': key['target'],
           'X_CAPI_PRIVACY_FENCE_DIR': privacy['target'],
           'X_CAPI_INGEST_SOCKET': '/run/axonos-x-capi/events.sock',
           'X_CAPI_CONSENT_SOCKET': '/run/axonos-x-capi/consent.sock'}
    gate = {'image': 'axonos:latest', 'build': {'context': str(root)},
            'environment': env, 'volumes': [dict(runtime, read_only=True), privacy, key],
            'networks': {'axonos_control': None, 'axonos_stack': None}}
    core_url = 'postgresql://synthetic:' + SECRET + '@postgres:5432/synthetic'
    gate['environment'].update(AXGT_CHALLENGE_DB_URL=core_url,
        AXGT_SESSION_LAUNCHER_MODE='http', AXGT_SESSION_LAUNCHER_URL='http://axonos-launcher:8090',
        AXGT_SESSION_LAUNCHER_TOKEN=SECRET)
    worker = {'environment': dict({k: v for k, v in env.items() if k.startswith('X_CAPI_')},
                                 X_CAPI_DB_URL_FILE='/run/secrets/x_capi_db_url',
                                 X_CAPI_HASH_KEY_FILE='/run/secrets/x_capi_hash_key'),
              'read_only': True, 'user': '10001:10001', 'volumes': [runtime, privacy, key],
              'networks': {'x_capi_db': None}, 'depends_on': {
                  'x-capi-postgres': {'condition': 'service_healthy'},
                  'x-capi-db-init': {'condition': 'service_completed_successfully'},
                  'x-capi-privacy-init': {'condition': 'service_completed_successfully'}}}
    for name in ('x_capi_db_url', 'x_capi_hash_key'):
        worker['volumes'].append(volume('/run/secrets/' + name,
                                        '/etc/axonos/' + name.replace('_', '-'), 'bind', True))
    passwords = [volume('/run/secrets/' + name, '/etc/axonos/' + name.replace('_', '-'),
                        'bind', True) for name in (
                            'x_capi_postgres_bootstrap_password', 'x_capi_postgres_worker_password')]
    return {'name': 'axonos', 'services': {'axonos': gate, 'x-capi-worker': worker,
        'postgres': {'image': 'postgres:15-alpine',
            'environment': {'POSTGRES_USER': 'synthetic', 'POSTGRES_PASSWORD': SECRET,
                            'POSTGRES_DB': 'synthetic'},
            'volumes': [volume('/var/lib/postgresql/data', 'axonos_postgres_data')],
            'networks': {'axonos_control': None}},
        'axonos-launcher': {'image': 'axonos-axonos-launcher',
            'privileged': True,
            'environment': {'AXGT_SESSION_LAUNCHER_TOKEN': SECRET,
                'AXGT_SESSION_LAUNCHER_BIND_HOST': '0.0.0.0',
                'AXGT_SESSION_LAUNCHER_BIND_PORT': '8090', 'AXGT_CHALLENGE_DB_URL': core_url},
            'volumes': [volume('/var/run/docker.sock', '/var/run/docker.sock', 'bind'),
                        volume('/var/lib/docker/axonos_storage', '/var/lib/docker/axonos_storage', 'bind'),
                        volume('/dev', '/dev', 'bind')],
            'networks': {'axonos_control': None}},
        'x-capi-db-init': {'volumes': passwords, 'networks': {'x_capi_db': None}},
        'x-capi-postgres': {'volumes': [passwords[0], volume('/var/lib/postgresql/data',
            'x_capi_postgres_data')], 'networks': {'x_capi_db': None},
            'image': 'postgres:15-alpine@sha256:' + 'c' * 64,
            'environment': {'POSTGRES_DB': 'axonos_x_capi', 'POSTGRES_USER': 'x_capi_bootstrap',
                            'POSTGRES_PASSWORD_FILE': '/run/secrets/x_capi_postgres_bootstrap_password'},
            'command': ['postgres', '-c', 'log_statement=none'], 'read_only': True},
        'x-capi-privacy-init': {'volumes': [runtime, privacy], 'network_mode': 'none'}},
        'networks': {'x_capi_db': {'internal': True, 'name': 'axonos_x_capi_db'},
                     'axonos_control': {'name': 'axonos_control'},
                     'axonos_stack': {'name': 'axonos_stack', 'driver': 'bridge'}},
        'volumes': dict({name: {'name': 'axonos_' + name} for name in (
            'x_capi_runtime', 'x_capi_privacy_fence', 'x_capi_postgres_data')},
            axonos_postgres_data={'name': 'axonos_axonos_postgres_data'})}


# This fake refuses every unknown command: a script change cannot accidentally
# fall through to the real Docker, Git or curl programs.
FAKE = r'''import json, os, pathlib, signal, subprocess, sys, time
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['FAKE_ROOT'])
scenario = json.loads((root / 'scenario.json').read_text())
state_file = root / 'fake-state.json'
state = json.loads(state_file.read_text()) if state_file.exists() else {}
def counter(key):
    state[key] = state.get(key, 0) + 1
    state_file.write_text(json.dumps(state))
    return state[key]
with (root / 'commands.jsonl').open('a') as stream:
    stream.write(json.dumps([tool, *args]) + '\n')
def out(value):
    if value: print(value)
    sys.exit(0)
def fail():
    print('SYNTHETIC_SECRET_MUST_NOT_APPEAR', file=sys.stderr)
    sys.exit(7)
if tool == 'metadata-helper':
    assert not args
    counter('secret_snapshots')
    if state.get('rolled'): counter('secret_snapshots_after_rollout')
    if scenario.get('secret_helper_failure') or (scenario.get('secret_helper_postflight_failure') and state.get('rolled')):
        fail()
    if scenario.get('secret_helper_malformed'):
        out('INVALID_SNAPSHOT_SYNTHETIC_SECRET_MUST_NOT_APPEAR')
    def record(ino, uid, mode, size, nlink=1):
        return dict(dev=1, ino=ino, uid=uid, gid=uid, mode=mode, nlink=nlink,
                    size=size, mtime_ns=3, ctime_ns=4)
    names = ('x-capi-context-key', 'x-capi-db-url', 'x-capi-hash-key',
             'x-capi-postgres-bootstrap-password', 'x-capi-postgres-worker-password')
    snapshot = {'version': 1,
        'directories': {path: record(number, 0, 0o40700 if path == '/etc/axonos' else 0o40755, 4096, 2)
                        for number, path in enumerate(('/', '/etc', '/etc/axonos'), 1)},
        'files': {'/etc/axonos/' + name: record(number, 0 if 'password' in name else 10001, 0o100600, 64)
                  for number, name in enumerate(names, 10)}}
    if scenario.get('secret_snapshot_extra_data'):
        snapshot['files']['/etc/axonos/x-capi-context-key']['contents'] = 'SYNTHETIC_SECRET_MUST_NOT_APPEAR'
    if scenario.get('secret_snapshot_missing_bootstrap'):
        del snapshot['files']['/etc/axonos/x-capi-postgres-worker-password']
    if scenario.get('secret_snapshot_substituted_path'):
        snapshot['files']['/unapproved/secret'] = snapshot['files'].pop('/etc/axonos/x-capi-context-key')
    for key, value in scenario.get('secret_file_metadata', {}).items():
        snapshot['files']['/etc/axonos/x-capi-context-key'][key] = value
    if scenario.get('secret_metadata_drift') and state.get('built'):
        drift = scenario['secret_metadata_drift']
        if drift == 'parent': snapshot['directories']['/etc/axonos']['ino'] += 100
        else: snapshot['files']['/etc/axonos/x-capi-context-key'][drift] += 100
    out(json.dumps(snapshot))
elif tool == 'git':
    if args == ['rev-parse', '--show-toplevel']: out(str(root))
    if args[0] == 'status': out(' M dirty' if scenario.get('dirty') else '')
    if args[0] == 'symbolic-ref':
        if scenario.get('detached'): fail()
        out('reviewed-test-branch')
    if args[0] == 'rev-parse':
        out(('b' if scenario.get('head_change') and state.get('built') else 'a') * 40)
elif tool == 'curl':
    if scenario.get('curl_fail'): fail()
    out(scenario.get('http', '200'))
elif tool == 'docker':
    assert args[:2] == ['--host', 'unix:///var/run/docker.sock'], args
    assert os.environ.get('DOCKER_API_VERSION') == scenario.get('docker_api_version')
    args = args[2:]
    engine_version = scenario.get('image_store_info', {}).get('ServerVersion',
        '29.5.1' if scenario.get('image_store') == 'containerd' else '28.5.2')
    maximum_api = '1.54' if engine_version in ('29.5.1', '29.5.2') else '1.51'
    effective_api = os.environ.get('DOCKER_API_VERSION') or scenario.get('negotiated_api', maximum_api)
    if args[:1] == ['info']:
        if scenario.get('daemon_fail'): fail()
        if args == ['info', '--format', '{{json .}}']:
            info = {'OSType': 'linux', 'ServerVersion': '28.5.2', 'Driver': 'overlay2',
                    'DriverStatus': [['Backing Filesystem', 'extfs'], ['Supports d_type', 'true']]}
            if scenario.get('image_store') == 'containerd':
                info.update(ServerVersion='29.5.1', Driver='overlayfs',
                            DriverStatus=[['driver-type', 'io.containerd.snapshotter.v1']])
            info.update(scenario.get('image_store_info', {}))
            out(json.dumps(info))
        assert args == ['info'], args
        out('')
    if args == ['version', '--format', '{{json .}}']:
        document = {'Client': {'Version': engine_version, 'Os': 'linux', 'ApiVersion': effective_api,
                    'DefaultAPIVersion': scenario.get('client_default_api', maximum_api)},
                    'Server': {'Version': engine_version, 'Os': 'linux', 'ApiVersion': maximum_api,
                    'MinAPIVersion': '1.40' if engine_version in ('29.5.1', '29.5.2') else '1.24'}}
        document['Client'].update(scenario.get('version_client', {}))
        document['Server'].update(scenario.get('version_server', {}))
        out(json.dumps(scenario.get('version_metadata', document)))
    if args == ['buildx', 'version']: out('synthetic buildx')
    if args[0] == 'compose':
        assert args[1:11] == ['--project-directory', str(root), '--project-name', 'axonos',
            '--env-file', str(root / '.env'), '-f', str(root / 'docker-compose.yml'),
            '-f', str(root / 'docker-compose.x-capi.yml')], args
        assert args[11:13] == ['--profile', 'x-capi'], args
        cmd = args[13:]
        override = None
        while cmd[:1] == ['-f']:
            override = json.loads(pathlib.Path(cmd[1]).read_text())
            cmd = cmd[2:]
        with (root / 'compose-events.jsonl').open('a') as stream:
            stream.write(json.dumps({'command': cmd, 'override': override,
                'remove_orphans': os.environ.get('COMPOSE_REMOVE_ORPHANS'),
                'stdin_is_tty': sys.stdin.isatty(), 'stdin_data': sys.stdin.read()}) + '\n')
        if cmd == ['version']:
            if scenario.get('compose_fail'): fail()
            out('fake Compose')
        if cmd == ['config', '--quiet']:
            if scenario.get('config_quiet_fail'): fail()
            out('')
        if cmd == ['config', '--format', 'json']:
            config = json.loads((root / 'config.json').read_text())
            if scenario.get('config_change') and state.get('built'):
                config['x-synthetic-change'] = True
            if scenario.get('bad_json'): out('{ invalid JSON secret SYNTHETIC_SECRET_MUST_NOT_APPEAR')
            out(json.dumps(config))
        if cmd[:3] == ['ps', '-a', '-q']:
            service = cmd[-1]
            if scenario.get('missing') == service or (scenario.get('post_missing') == service and state.get('rolled')): out('')
            if scenario.get('ambiguous') == service: out('a' * 64 + '\n' + 'b' * 64)
            number = SERVICES.index(service) + 1
            if service in ('x-capi-db-init', 'x-capi-privacy-init') and state.get('initializers_prepared'):
                number += 1000
            if state.get('worker_prepared') and (service == 'x-capi-worker' or scenario.get('prerequisite_replaced') == service):
                number += 1000
            out(format(number, '064x'))
        if cmd[0] == 'build':
            if '--print' in cmd:
                out(json.dumps({'target': {'axonos': {'context': '.' if scenario.get('relative_bake_context') else str(root),
                    'dockerfile': 'Dockerfile', 'tags': ['axonos:latest', 'UNSAFE:latest'],
                    'args': {'AXONOS_SKIP_HEAVY': '0', 'PASSWORD': 'SYNTHETIC_SECRET_MUST_NOT_APPEAR'}}}}))
            if scenario.get('build_fail'): fail()
            state['built'] = True
            state_file.write_text(json.dumps(state))
            out('')
        if cmd[0] in ('pull', 'stop', 'up'):
            if scenario.get('backend_pull_fail') and cmd[0] == 'pull': fail()
            if scenario.get('backend_prepare_fail') and '--no-start' in cmd: fail()
            if scenario.get('mutation_fail') == cmd[-1]: fail()
            if cmd[0] == 'up' and cmd[-1] == 'axonos':
                state['rolled'] = True
                state['deployed_image'] = override['services']['axonos']['image'] if override else 'axonos:latest'
                if scenario.get('tag_race_at_rollout'):
                    state['shared_tag'] = 'sha256:' + 'b' * 64
                    assert state['deployed_image'] == 'sha256:' + 'a' * 64
            if cmd[0] == 'up' and cmd[-1] == 'x-capi-worker' and '--no-start' not in cmd:
                state['backend_started'] = True
                if '--no-deps' not in cmd:
                    for initializer in ('x-capi-db-init', 'x-capi-privacy-init'):
                        state.setdefault('initializer_executions', {})[initializer] = state.get('initializer_executions', {}).get(initializer, 0) + 1
            if cmd[0] == 'up' and cmd[-1] == 'x-capi-worker' and '--no-start' in cmd:
                state['worker_prepared'] = True
            if cmd[0] == 'up' and 'x-capi-db-init' in cmd and '--no-start' in cmd:
                state['initializers_prepared'] = True
            if cmd[0] == 'up' and 'x-capi-db-init' in cmd and '--no-start' not in cmd:
                state['initialization_started'] = True
                for initializer in ('x-capi-db-init', 'x-capi-privacy-init'):
                    state.setdefault('initializer_executions', {})[initializer] = state.get('initializer_executions', {}).get(initializer, 0) + 1
            state_file.write_text(json.dumps(state))
            out('')
    if args[:2] == ['buildx', 'bake']:
        spec = json.load(sys.stdin)
        state['bake'] = spec
        if scenario.get('build_fail'): fail()
        if scenario.get('hold_build'):
            (root / 'build-entered').write_text('ready')
            while not (root / 'build-release').exists(): time.sleep(.02)
        state['built'] = True
        tag = spec['target']['axonos']['tags'][0]
        state.setdefault('candidate_images', {})[tag] = {
            'Id': 'sha256:' + 'a' * 64, 'RepoTags': [tag], 'RepoDigests': [],
            'Config': {'Labels': spec['target']['axonos'].get('labels', {})}}
        state_file.write_text(json.dumps(state))
        out('')
    if args[:3] == ['container', 'ls', '-a']:
        target = args[args.index('--filter') + 1].removeprefix('name=^/').removesuffix('$')
        matches = [key for key, value in state.get('validator_containers', {}).items()
                   if value['Name'] == '/' + target]
        out('\n'.join(matches))
    if args[0] == 'ps':
        if args[1:4] == ['-a', '-q', '--no-trunc']:
            out('')
        n = counter('sessions')
        active = scenario.get('active') or (scenario.get('late_active') and n > 1)
        out(json.dumps({'Names': 'axgt-session-synthetic', 'Labels': ''}) if active else '')
    if args[:2] == ['image', 'inspect']:
        if args == ['image', 'inspect', 'sha256:' + 'a' * 64]:
            counter('probe_image_inspects')
            item = {'Id': args[-1], 'RepoTags': ['axonos:latest'], 'RepoDigests': [], 'Config': {'Labels': {}}}
            if scenario.get('image_store') == 'containerd':
                item['RepoDigests'] = ['axonos@' + item['Id']]
                if tuple(int(number) for number in effective_api.split('.')) >= (1, 48):
                    item['Descriptor'] = {'digest': item['Id'], 'mediaType': 'application/vnd.oci.image.index.v1+json'}
                if tuple(int(number) for number in effective_api.split('.')) < (1, 52):
                    item['GraphDriver'] = {'Name': 'overlayfs'}
            else:
                item['GraphDriver'] = {'Name': 'overlay2'}
            item.update(scenario.get('probe_metadata', {}))
            out(json.dumps(scenario.get('probe_response', [item])))
        n = counter('image_inspects')
        if '--format' not in args:
            if args[-1] not in state.get('candidate_images', {}): fail()
            item = dict(state['candidate_images'][args[-1]])
            if scenario.get('image_store') == 'containerd':
                item['Descriptor'] = {'digest': item['Id'],
                    'mediaType': 'application/vnd.oci.image.index.v1+json'}
                if tuple(int(number) for number in effective_api.split('.')) < (1, 52):
                    item['GraphDriver'] = {'Name': 'overlayfs'}
                # Mirror containerd's public inspect result: RepoTags lists
                # actual tag/canonical names; RepoDigests additionally derives
                # a canonical-looking string from each ordinary stored tag.
                item['RepoDigests'] = sorted({name if '@' in name else name.rsplit(':', 1)[0] + '@' + item['Id']
                                             for name in item.get('RepoTags', [])})
            else:
                item['GraphDriver'] = {'Name': 'overlay2'}
            out(json.dumps([item]))
        if 'axonos:latest' in args and state.get('shared_tag'): out(state['shared_tag'])
        out('sha256:' + ('b' if scenario.get('tag_change') and 'axonos:latest' in args and n > 1 else 'a') * 64)
    if args[:2] == ['image', 'ls']:
        assert '--filter' in args and args[args.index('--filter') + 1] == 'reference=axonos-deploy-candidate:*'
        out('\n'.join(state.get('candidate_images', {})))
    if args[:2] == ['image', 'rm']:
        assert args[2] == '--no-prune' and len(args) == 4
        tag = args[-1]
        assert tag.startswith('axonos-deploy-candidate:') and tag in state.get('candidate_images', {})
        if scenario.get('candidate_cleanup_fail'): fail()
        del state['candidate_images'][tag]
        state.setdefault('removed_candidate_tags', []).append(tag)
        state_file.write_text(json.dumps(state))
        out('')
    if args[:2] == ['image', 'tag']:
        state['promoted'] = args[2]
        for item in state.get('candidate_images', {}).values():
            if item['Id'] == args[2]: item['RepoTags'].append(args[3])
        state_file.write_text(json.dumps(state))
        out('')
    if args[0] == 'create':
        n = counter('validators')
        identity = format(n + 100, '064x')
        labels = {}
        for index, arg in enumerate(args):
            if arg == '--label':
                key, value = args[index + 1].split('=', 1)
                labels[key] = value
        if scenario.get('foreign_validator'):
            labels['com.axonos.deploy.validator'] = 'not-this-deployment'
        state.setdefault('validator_containers', {})[identity] = {
            'Id': identity, 'Name': '/' + args[args.index('--name') + 1],
            'Config': {'Image': 'sha256:' + 'a' * 64, 'Labels': labels},
            'Image': 'sha256:' + 'a' * 64,
            'State': {'Status': 'created', 'ExitCode': 0}, 'number': n}
        state_file.write_text(json.dumps(state))
        if scenario.get('validator_create_timeout'):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True: time.sleep(1)
        out(identity)
    if args[0] == 'start':
        identity = args[-1]
        if identity in state.get('validator_containers', {}):
            entry = state['validator_containers'][identity]
            if scenario.get('validator_timeout') or scenario.get('repeated_signals'):
                def received_term(_signal, _frame):
                    (root / 'validator-term-received').write_text('ready')
                signal.signal(signal.SIGTERM, received_term)
                child = subprocess.Popen([sys.executable, '-c',
                    'import pathlib,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(120)',
                    str(root / 'resistant-child-ready')])
                while not (root / 'resistant-child-ready').exists(): time.sleep(.01)
                (root / 'resistant-pids.json').write_text(json.dumps([os.getpid(), child.pid]))
                while True: time.sleep(1)
            entry['State'] = {'Status': 'exited', 'ExitCode': 1 if scenario.get('validator_fail') == entry['number'] else 0}
            state_file.write_text(json.dumps(state))
            if entry['State']['ExitCode']: fail()
        else:
            state['restored_worker'] = identity
            state_file.write_text(json.dumps(state))
        out('')
    if args[0] == 'stop':
        state['stopped_worker'] = args[-1]
        state_file.write_text(json.dumps(state))
        out('')
    if args[0] == 'rm':
        identity = args[-1]
        assert identity in state.get('validator_containers', {}), args
        if scenario.get('cleanup_fail'): fail()
        if scenario.get('repeated_signals'):
            (root / 'validator-cleanup-entered').write_text('ready')
            while not (root / 'validator-cleanup-release').exists(): time.sleep(.02)
        del state['validator_containers'][identity]
        state_file.write_text(json.dumps(state))
        out('')
    if args[:2] == ['network', 'inspect']:
        if scenario.get('resource_fail_after_stop_once') and state.get('stopped_worker') and counter('post_stop_resource') == 1:
            fail()
        entries = []
        for name in args[2:]:
            capi = name == 'axonos_x_capi_db'
            labels = {'com.docker.compose.project': 'axonos',
                      'com.docker.compose.network': 'x_capi_db' if capi else name}
            entry = {'Name': name, 'Id': ('d' if capi else 'e') * 64, 'Driver': 'bridge', 'Scope': 'local',
                     'Internal': capi, 'Attachable': False, 'Ingress': False, 'EnableIPv6': False, 'EnableIPv4': True,
                     'IPAM': {'Driver': 'default', 'Options': None,
                              'Config': [{'Subnet': '172.29.0.0/16', 'Gateway': '172.29.0.1'}]},
                     'Options': {}, 'Labels': labels, 'Containers': {}}
            if scenario.get('network_ipv4_metadata') == 'missing': entry.pop('EnableIPv4')
            elif 'network_ipv4_metadata' in scenario: entry['EnableIPv4'] = scenario['network_ipv4_metadata']
            key = scenario.get('network_drift') if capi else None
            if scenario.get('network_drift_after_stop') and state.get('stopped_worker'):
                key = 'internal'
            if key == 'internal': entry['Internal'] = False
            if key == 'owner': labels['com.docker.compose.project'] = 'other'
            if key == 'driver': entry['Driver'] = 'overlay'
            if key == 'attachment': entry['Containers']['f' * 64] = {'Name': 'unrelated'}
            if key == 'options': entry['Options'] = {'com.docker.network.bridge.enable_icc': 'true'}
            if scenario.get('network_hash_drift') == name:
                labels['com.docker.compose.config-hash'] = '0' * 64
            entries.append(entry)
        out(json.dumps(entries))
    if args[:2] == ['volume', 'inspect']:
        entries = []
        for name in args[2:]:
            logical = name.removeprefix('axonos_')
            entry = {'Name': name, 'Driver': 'local', 'Scope': 'local', 'Options': None,
                'Mountpoint': '/var/lib/docker/volumes/' + name + '/_data',
                'Labels': {'com.docker.compose.project': 'axonos', 'com.docker.compose.volume': logical}}
            key = scenario.get('volume_drift')
            if key == 'owner': entry['Labels']['com.docker.compose.project'] = 'other'
            if key == 'logical': entry['Labels']['com.docker.compose.volume'] = 'other'
            if key == 'driver': entry['Driver'] = 'nfs'
            if key == 'options': entry['Options'] = {'device': '/sensitive', 'o': 'bind', 'type': 'none'}
            if key == 'scope': entry['Scope'] = 'global'
            if key == 'mountpoint': entry['Mountpoint'] = '/sensitive'
            if scenario.get('volume_hash_drift') == name:
                entry['Labels']['com.docker.compose.config-hash'] = '0' * 64
            entries.append(entry)
        out(json.dumps(entries))
    if args[0] == 'exec':
        if scenario.get('mount_check_fail'): fail()
        if 'stat' in args: out('0:0:777:directory' if scenario.get('data_owner_drift') else '70:70:700:directory')
        out('')
    if args[0] == 'inspect':
        if args[-1] in state.get('validator_containers', {}):
            out(json.dumps([state['validator_containers'][args[-1]]]))
        number = int(args[1], 16)
        service = SERVICES[number % 1000 - 1]
        labels = {'com.docker.compose.project': 'axonos', 'com.docker.compose.service': service,
            'com.docker.compose.project.working_dir': str(root),
            'com.docker.compose.project.config_files': str(root / 'docker-compose.yml') + ',' + str(root / 'docker-compose.x-capi.yml')}
        if scenario.get('cross_checkout'):
            labels['com.docker.compose.project.working_dir'] = str(root / 'other-checkout')
        if scenario.get('wrong_project') == service: labels['com.docker.compose.project'] = 'other'
        if scenario.get('wrong_service') == service: labels['com.docker.compose.service'] = 'other'
        is_init = service in ('x-capi-db-init', 'x-capi-privacy-init')
        status = {'Status': 'exited', 'ExitCode': 0} if is_init else {
            'Status': 'running', 'Health': {'Status': 'healthy'}}
        if is_init and state.get('initializers_prepared') and not state.get('initialization_started'):
            status = {'Status': 'created'}
        if scenario.get('unhealthy') == service: status = {'Status': 'running', 'Health': {'Status': 'unhealthy'}}
        if service == 'x-capi-worker' and state.get('stopped_worker') and not state.get('backend_started') and not state.get('restored_worker'):
            status = {'Status': 'exited', 'ExitCode': 0}
        if service == 'axonos' and state.get('rolled') and scenario.get('health_wait'):
            health = scenario['health_wait']
            if health == 'transition': health = 'starting' if counter('gate_waits') == 1 else 'healthy'
            status = {'Status': 'exited', 'ExitCode': 1} if health == 'exited' else {'Status': 'running', 'Health': {'Status': health}}
        if (state.get('backend_started') or state.get('initialization_started')) and scenario.get('init_fail') == service:
            status = {'Status': 'exited', 'ExitCode': 1}
        if state.get('initialization_started') and scenario.get('init_running') and is_init:
            status = {'Status': 'running'} if counter('init_wait_' + service) == 1 else {'Status': 'exited', 'ExitCode': 0}
        if state.get('worker_prepared') and scenario.get('prerequisite_failed') == service:
            status = {'Status': 'exited', 'ExitCode': 1}
        config = json.loads((root / 'config.json').read_text())
        env = dict(config['services'].get(service, {}).get('environment', {}))
        if scenario.get('worker_mode') and service == 'x-capi-worker': env['X_CAPI_MODE'] = 'dry_run'
        if service == 'axonos' and state.get('rolled') and scenario.get('post_mode'):
            env['X_CAPI_MODE'] = 'live'
        mounts = [dict(Destination='/run/axonos-x-capi', Type='volume', Name='axonos_x_capi_runtime', RW=False),
            dict(Destination='/run/axonos-x-capi-privacy', Type='volume', Name='axonos_x_capi_privacy_fence', RW=True),
            dict(Destination='/run/secrets/x_capi_context_key', Type='bind', Source='/etc/axonos/x-capi-context-key', RW=False)]
        if scenario.get('post_mount') and state.get('rolled'): mounts[0]['RW'] = True
        image = 'sha256:' + ('b' if scenario.get('post_image') and state.get('rolled') else 'a') * 64
        configured = config['services'].get(service, {})
        if service in ('x-capi-worker', 'x-capi-postgres', 'x-capi-db-init', 'postgres', 'axonos-launcher'):
            mounts = [dict(Destination=m['target'], Type=m['type'], RW=not m.get('read_only', False),
                **({'Name': config['volumes'][m['source']]['name']} if m['type'] == 'volume' else {'Source': m['source']}))
                for m in configured['volumes']]
        if scenario.get('db_drift') and service == 'x-capi-postgres': env['POSTGRES_DB'] = 'other'
        if scenario.get('worker_drift') and service == 'x-capi-worker': env['X_CAPI_QUEUE_LIMIT'] = '200'
        if scenario.get('launcher_token_drift') and service == 'axonos-launcher': env['AXGT_SESSION_LAUNCHER_TOKEN'] = 'different'
        if scenario.get('core_db_drift') and service == 'postgres': env[scenario['core_db_drift']] = 'different'
        defaults = {'postgres': (['postgres'], ['docker-entrypoint.sh']),
                    'axonos-launcher': (['python3', '/app/session_launcher_service.py'], None)}
        image_command, image_entrypoint = defaults.get(service, (None, None))
        command, entrypoint = configured.get('command'), configured.get('entrypoint')
        # Model Engine's actual image-config merge independently of the
        # validator's declared Compose intent, including Moby's len-zero CMD
        # fallback. Null is NOT the literal runtime argv returned by inspect.
        if not entrypoint:
            if not command:
                command = image_command
            if entrypoint is None:
                entrypoint = image_entrypoint
        if entrypoint == ['']:
            entrypoint = None
        startup = scenario.get('actual_startup', {}).get(service, {})
        command = startup.get('Cmd', command)
        entrypoint = startup.get('Entrypoint', entrypoint)
        networks = {config['networks'][name]['name']: {} for name in configured.get('networks', {})}
        out(json.dumps([{'Id': args[1], 'Config': {'Labels': labels, 'Env': [k + '=' + v for k, v in env.items()],
                         'Image': configured.get('image', ''), 'Cmd': command, 'Entrypoint': entrypoint,
                         'User': configured.get('user', '' if service in ('postgres', 'axonos-launcher') else '10001:10001')},
                         'HostConfig': {'ReadonlyRootfs': configured.get('read_only', False),
                                        'Privileged': configured.get('privileged', False)},
                         'NetworkSettings': {'Networks': networks},
                         'State': status, 'Mounts': mounts, 'Image': image}]))
raise SystemExit('unexpected fake command: ' + repr([tool, *args]))
'''.replace('SERVICES', repr(SERVICES))


PYTHON_SHIM = r'''import importlib.util, json, os, pathlib, sys
path, *args = sys.argv[1:]
assert pathlib.Path(path).name in ('deploy_production.py', 'deploy_production_checks.py'), path
sys.path.insert(0, str(pathlib.Path(path).parent))
import deploy_production_checks as checks
# All secret validation/fingerprinting is real. Only installation attestation
# and the exact privileged command are substituted with synthetic boundaries.
checks.metadata_helper_contract = lambda: None
actual_secret_metadata = checks.secret_metadata
def fixture_secret_metadata(path, root, uid, low, high, snapshot=None):
    # An omitted controller snapshot must fail the test, never fall through to
    # metadata access under the REAL protected production directory.
    assert snapshot is not None, 'orchestration must supply synthetic secret metadata'
    return actual_secret_metadata(path, root, uid, low, high, snapshot)
checks.secret_metadata = fixture_secret_metadata
spec = importlib.util.spec_from_file_location('deployment_checker', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
if pathlib.Path(path).name == 'deploy_production.py':
    # Synthetic-only injection into the copied controller. Production accepts
    # neither environment timeout overrides nor alternate deployment lock paths.
    root = pathlib.Path(os.environ['FAKE_ROOT'])
    scenario = json.loads((root / 'scenario.json').read_text())
    if scenario.get('secret_helper_contract_failure'):
        def missing_helper_contract():
            raise checks.Refusal('Secret metadata helper installation cannot be attested')
        checks.metadata_helper_contract = missing_helper_contract
    module.LOCK_FILE = str(root / 'deploy.lock')
    module.LOCK_UID = os.getuid()
    module.COMMAND_TIMEOUT = 3
    module.run.__kwdefaults__['timeout'] = 3
    module.KILL_GRACE = 1 if scenario.get('repeated_signals') else .15
    module.VALIDATOR_TIMEOUT = 20 if scenario.get('repeated_signals') else (.3 if scenario.get('validator_timeout') else 3)
    module.BUILD_TIMEOUT = 10
    actual_run = module.run
    def fixture_run(command, env, checkout, **kwargs):
        if tuple(command) == tuple(checks.SECRET_METADATA_COMMAND):
            assert env == {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}
            assert kwargs.get('input_text') is None
            command = [str(root / 'fake-bin' / 'metadata-helper')]
            env = dict(env, FAKE_ROOT=str(root))
        return actual_run(command, env, checkout, **kwargs)
    module.run = fixture_run
sys.argv = [path, *args]
try:
    result = module.main()
    if isinstance(result, int): sys.exit(result)
except checks.Refusal as error:
    print('ERROR: ' + str(error), file=sys.stderr)
    sys.exit(1)
except Exception:
    print('ERROR: deployment metadata/configuration check failed (details withheld)', file=sys.stderr)
    sys.exit(1)
'''


class DeploymentOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.deploy-test-', dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'axonos_gate').mkdir()
        for source in [REPO / 'scripts/deploy-production.sh', *REPO.glob('scripts/deploy_production*.py')]:
            shutil.copyfile(source, self.root / 'scripts' / source.name)
        shutil.copyfile(REPO / 'axonos_gate/x_capi.py', self.root / 'axonos_gate/x_capi.py')
        for filename in ('docker-compose.yml', 'docker-compose.x-capi.yml'):
            (self.root / filename).write_text('# Synthetic; fake Docker returns in-memory fixture\n')
        (self.root / '.env').write_text('X_CAPI_MODE=off\nUNRELATED_PASSWORD=' + SECRET + '\n')
        self.config = configuration(self.root)
        self.scenario = {}
        (self.root / 'deploy.lock').write_text('')
        (self.root / 'deploy.lock').chmod(0o444)
        self.bin = self.root / 'fake-bin'
        self.bin.mkdir()
        for name, source in [('docker', FAKE), ('git', FAKE), ('curl', FAKE),
                             ('metadata-helper', FAKE), ('python3', PYTHON_SHIM)]:
            script = self.bin / name
            script.write_text('#!' + sys.executable + '\n' + source)
            script.chmod(0o700)
        # Private PATH has only explicit fake tools plus these non-mutating
        # utilities. Missing docker tests cannot fall back to the real CLI.
        for name in ('dirname', 'flock', 'timeout', 'sleep'):
            (self.bin / name).symlink_to(shutil.which(name))

    def prepare_run(self, *options, trace=False):
        (self.root / 'scenario.json').write_text(json.dumps(self.scenario))
        (self.root / 'config.json').write_text(json.dumps(self.config))
        env = {'PATH': str(self.bin), 'FAKE_ROOT': str(self.root),
               'PYTHONDONTWRITEBYTECODE': '1', 'LC_ALL': 'C',
               'COMPOSE_FILE': '/DO_NOT_USE', 'COMPOSE_PROJECT_NAME': 'wrong',
               'COMPOSE_REMOVE_ORPHANS': '1'}
        if self.scenario.get('remote_host'):
            env['DOCKER_HOST'] = 'tcp://DO_NOT_CONTACT:2375'
        if 'docker_api_version' in self.scenario:
            env['DOCKER_API_VERSION'] = self.scenario['docker_api_version']
        return ['/bin/bash', *(['-x'] if trace else []), str(self.root / 'scripts/deploy-production.sh'), *options], env

    def run_script(self, *options, trace=False):
        command, env = self.prepare_run(*options, trace=trace)
        result = subprocess.run(command, cwd=self.root / 'fake-bin', env=env,
                                capture_output=True, input='y\n', text=True, timeout=30)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        return result

    def commands(self):
        path = self.root / 'commands.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def mutations(self):
        return [cmd for cmd in self.commands() if cmd[0] == 'docker' and (
            cmd[3] in ('create', 'run', 'start', 'stop', 'rm') or cmd[3:5] == ['buildx', 'bake'] or
            cmd[3:5] in (['image', 'tag'], ['image', 'rm']) or (cmd[3] == 'compose' and self.compose_command(cmd)[0] in ('build', 'pull', 'stop', 'up')
                and '--print' not in cmd))]

    def compose_command(self, command):
        result = command[16:]
        while result[:1] == ['-f']:
            result = result[2:]
        return result

    def compose_events(self):
        path = self.root / 'compose-events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def state(self):
        path = self.root / 'fake-state.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def assert_failed(self, result, text=None):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        if text:
            self.assertIn(text, result.stdout + result.stderr)

    def test_help_never_contacts_tools(self):
        result = self.run_script('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('NOT CAPI dry_run', result.stdout)
        self.assertEqual(self.commands(), [])

    def test_check_is_read_only_and_off_needs_no_token_or_event_ids(self):
        result = self.run_script('--check', trace=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CHECK PASSED', result.stdout)
        self.assertIn('branch=reviewed-test-branch', result.stdout)
        self.assertIn('CAPI=off', result.stdout)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(any(cmd[0] == 'curl' for cmd in self.commands()))

    def test_check_with_backend_is_still_non_mutating(self):
        result = self.run_script('--check', '--with-capi-backend')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mutations(), [])

    def test_check_attests_all_protected_secrets_without_host_traversal_or_mutation(self):
        result = self.run_script('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreater(self.state().get('secret_snapshots', 0), 0)
        helpers = [command for command in self.commands() if command[0] == 'metadata-helper']
        self.assertTrue(helpers)
        self.assertTrue(all(command == ['metadata-helper'] for command in helpers))
        self.assertEqual(self.mutations(), [])

    def test_unavailable_or_untrusted_secret_snapshot_refuses_before_build(self):
        variants = ({'secret_helper_contract_failure': True}, {'secret_helper_failure': True},
                    {'secret_helper_malformed': True}, {'secret_snapshot_extra_data': True},
                    {'secret_snapshot_missing_bootstrap': True}, {'secret_snapshot_substituted_path': True})
        for scenario in variants:
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                result = self.run_script('--with-capi-backend')
                self.assert_failed(result)
                self.assertIn('Secret metadata', result.stdout + result.stderr)
                self.assertEqual(self.mutations(), [])
                self.assertNotIn('built', self.state())
                self.assertNotIn('stopped_worker', self.state())

    def test_invalid_privileged_secret_metadata_cannot_bypass_client_attestation(self):
        for metadata in ({'mode': stat.S_IFLNK | 0o600}, {'uid': 0}, {'gid': 0},
                         {'mode': stat.S_IFREG | 0o644}, {'nlink': 2}, {'size': 0}, {'size': 4097}):
            with self.subTest(metadata=metadata):
                self.scenario = {'secret_file_metadata': metadata}
                self.assert_failed(self.run_script('--check'))
                self.assertEqual(self.mutations(), [])

    def test_secret_and_parent_metadata_drift_after_build_blocks_promotion(self):
        for field in ('ino', 'ctime_ns', 'parent'):
            with self.subTest(field=field):
                for name in ('commands.jsonl', 'fake-state.json'):
                    (self.root / name).unlink(missing_ok=True)
                self.scenario = {'secret_metadata_drift': field}
                result = self.run_script()
                self.assert_failed(result, 'Configuration/secret metadata changed')
                self.assertTrue(self.state()['built'])
                self.assertGreaterEqual(self.state()['secret_snapshots'], 2)
                self.assertNotIn('promoted', self.state())
                self.assertNotIn('rolled', self.state())

    def test_gate_postflight_requires_fresh_secret_metadata_attestation(self):
        self.scenario = {'secret_helper_postflight_failure': True}
        self.assert_failed(self.run_script(), 'Secret metadata cannot be attested with current privileges')
        self.assertTrue(self.state()['rolled'])
        self.assertGreater(self.state().get('secret_snapshots_after_rollout', 0), 0)

    def test_check_inherits_null_startup_for_preserved_launcher_and_postgres(self):
        self.scenario = {'image_store': 'containerd', 'image_store_info': {'ServerVersion': '29.5.2'}}
        for service in ('axonos-launcher', 'postgres'):
            self.config['services'][service].update(command=None, entrypoint=None)
        result = self.run_script('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CHECK PASSED', result.stdout)
        self.assertEqual(self.mutations(), [])
        self.assertNotIn('built', self.state())

    def test_null_declared_startup_still_rejects_actual_preserved_dependency_drift(self):
        for service in ('axonos-launcher', 'postgres'):
            self.config['services'][service].update(command=None, entrypoint=None)
        cases = (('axonos-launcher', {'Cmd': None}), ('axonos-launcher', {'Cmd': []}),
                 ('axonos-launcher', {'Cmd': ['python3', '/app/unreviewed.py']}),
                 ('axonos-launcher', {'Entrypoint': ['/unreviewed-entrypoint']}),
                 ('postgres', {'Cmd': ['postgres', '-c', 'log_statement=all']}),
                 ('postgres', {'Entrypoint': None}), ('postgres', {'Entrypoint': []}),
                 ('postgres', {'Entrypoint': ['/unreviewed-entrypoint']}))
        for service, startup in cases:
            with self.subTest(service=service, startup=startup):
                self.scenario = {'actual_startup': {service: startup}}
                self.assert_failed(self.run_script('--with-capi-backend'))
                self.assertEqual(self.mutations(), [])
                self.assertNotIn('built', self.state())
                self.assertNotIn('stopped_worker', self.state())

    def test_explicit_empty_startup_does_not_attest_runtime_image_default(self):
        for field in ('command', 'entrypoint'):
            with self.subTest(field=field):
                self.config['services']['postgres'].update(command=None, entrypoint=None)
                self.config['services']['postgres'][field] = []
                self.assert_failed(self.run_script('--check'))
                self.assertEqual(self.mutations(), [])

    def test_missing_docker_cli_refuses_without_any_external_command(self):
        (self.bin / 'docker').unlink()
        self.assert_failed(self.run_script('--check'), 'Required command unavailable: docker')
        self.assertEqual(self.commands(), [])

    def test_valid_explicit_dry_run_mode_is_validated_not_activated_by_check(self):
        settings = {'X_CAPI_MODE': 'dry_run', 'X_CAPI_PIXEL_ID': 'synthetic-pixel',
                    'X_CAPI_EVENT_SESSION_STARTED': 'synthetic-event',
                    'X_CAPI_ALLOWED_ORIGIN': 'https://app.synthetic.invalid',
                    'X_CAPI_CONSENT_POLICY_EPOCH': '1', 'X_CAPI_DEPLOYMENT_ID': 'synthetic'}
        (self.root / '.env').write_text('X_CAPI_MODE=dry_run\n')
        for service in ('axonos', 'x-capi-worker'):
            self.config['services'][service]['environment'].update(settings)
        result = self.run_script('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CAPI=dry_run', result.stdout)
        self.assertEqual(self.mutations(), [])

    def test_normal_flow_build_validate_gate_only_and_postflight(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.commands()
        mutations = self.mutations()
        compose = [self.compose_command(cmd) for cmd in mutations if cmd[3] == 'compose']
        self.assertEqual(compose, [['up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'axonos']])
        baked = self.state()['bake']['target']['axonos']
        self.assertEqual(baked['args']['AXONOS_SKIP_HEAVY'], '0')
        self.assertEqual(len(baked['tags']), 1)
        self.assertTrue(baked['tags'][0].startswith('axonos-deploy-candidate:'))
        self.assertNotIn('axonos:latest', baked['tags'])
        validators = [cmd for cmd in mutations if cmd[3] == 'create']
        self.assertEqual(len(validators), 2)
        for cmd in validators:
            self.assertIn(IMAGE, cmd)
            self.assertNotIn('axonos:latest', cmd)
            for option in ('--read-only', '--cap-drop', '--security-opt', '--network', 'none', '--runtime', 'runc'):
                self.assertIn(option, cmd)
        promotion = next(cmd for cmd in mutations if cmd[3:5] == ['image', 'tag'])
        rollout = next(cmd for cmd in mutations if cmd[3] == 'compose' and cmd[-1] == 'axonos')
        self.assertEqual(promotion[5:], [IMAGE, 'axonos:latest'])
        self.assertLess(commands.index(validators[1]), commands.index(promotion))
        self.assertLess(commands.index(promotion), commands.index(rollout))
        self.assertEqual(self.state()['deployed_image'], IMAGE)
        self.assertEqual(self.state()['validator_containers'], {})
        self.assertEqual(self.state()['candidate_images'], {})
        self.assertEqual(self.state()['removed_candidate_tags'], baked['tags'])
        self.assertEqual(baked['labels']['com.axonos.deploy.candidate-owner'], 'axonos-production-deploy-v1')
        self.assertEqual(baked['tags'][0], 'axonos-deploy-candidate:' + baked['labels']['com.axonos.deploy.candidate-run'])
        curls = [cmd for cmd in commands if cmd[0] == 'curl']
        self.assertEqual([cmd[-1] for cmd in curls], ['http://127.0.0.1:6080/vnc.html', 'http://127.0.0.1:8889/'])
        self.assertIn('DEPLOYMENT PASSED', result.stdout)

    def test_preflight_errors_make_no_mutations_and_withhold_diagnostics(self):
        for scenario in ({'dirty': True}, {'detached': True}, {'daemon_fail': True},
                         {'compose_fail': True}, {'config_quiet_fail': True}, {'bad_json': True},
                         {'missing': 'x-capi-worker'}, {'unhealthy': 'postgres'},
                         {'ambiguous': 'axonos'}, {'wrong_project': 'axonos'},
                         {'wrong_service': 'x-capi-worker'}, {'worker_mode': True},
                         {'db_drift': True}, {'worker_drift': True}, {'remote_host': True}):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                result = self.run_script('--check', trace=True)
                self.assert_failed(result)
                self.assertEqual(self.mutations(), [])

    def test_live_invalid_missing_duplicate_and_interpolated_modes_refused(self):
        for contents in ('', 'X_CAPI_MODE=live\n', 'X_CAPI_MODE=bogus\n',
                         'X_CAPI_MODE=off\nX_CAPI_MODE=off\n', 'X_CAPI_MODE=${MODE}\n',
                         'X_CAPI_MODE=$(printf off)\n'):
            with self.subTest(contents=contents):
                (self.root / '.env').write_text(contents)
                result = self.run_script('--check')
                self.assert_failed(result)
                self.assertEqual(self.mutations(), [])

    def test_mode_override_in_rendered_compose_is_refused(self):
        self.config['services']['axonos']['environment']['X_CAPI_MODE'] = 'dry_run'
        self.assert_failed(self.run_script('--check'), 'mode differs')
        self.assertEqual(self.mutations(), [])

    def test_active_sessions_block_before_build_unless_explicitly_allowed(self):
        self.scenario = {'active': True}
        self.assert_failed(self.run_script(), 'Active tenants found')
        self.assertEqual(self.mutations(), [])
        result = self.run_script('--allow-active-sessions')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ACCEPTED', result.stdout)
        self.assertIn('axgt-session-synthetic', result.stdout)

    def test_sessions_rechecked_after_build_before_rollout(self):
        self.scenario = {'late_active': True}
        self.assert_failed(self.run_script(), 'Active tenants found')
        self.assertFalse(any(cmd[3] == 'compose' and self.compose_command(cmd)[0] == 'up' for cmd in self.mutations()))

    def test_build_failure_leaves_running_gate_untouched(self):
        self.scenario = {'build_fail': True}
        self.assert_failed(self.run_script(trace=True), 'Build full private candidate failed')
        self.assertEqual(len(self.mutations()), 1)

    def test_each_validator_failure_prevents_rollout(self):
        for failed in (1, 2):
            with self.subTest(validator=failed):
                # Reset fake counters/log between independent deployments.
                for path in ('fake-state.json', 'commands.jsonl'):
                    (self.root / path).unlink(missing_ok=True)
                self.scenario = {'validator_fail': failed}
                self.assert_failed(self.run_script(trace=True), 'image validation failed')
                self.assertFalse(any(cmd[3] == 'compose' and self.compose_command(cmd)[0] == 'up' for cmd in self.mutations()))
                self.assertFalse(any(cmd[3:5] == ['image', 'tag'] for cmd in self.mutations()))
                self.assertEqual(self.state().get('validator_containers', {}), {})
                self.assertEqual(len(self.state()['candidate_images']), 1)
                self.assertNotIn('removed_candidate_tags', self.state())

    def test_candidate_cleanup_failure_warns_without_undoing_healthy_deployment(self):
        self.scenario = {'candidate_cleanup_fail': True}
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('DEPLOYMENT PASSED', result.stdout)
        self.assertIn('WARNING', result.stdout)
        self.assertEqual(self.state()['deployed_image'], IMAGE)
        self.assertEqual(len(self.state()['candidate_images']), 1)
        self.assertNotIn('removed_candidate_tags', self.state())

    def test_config_and_head_change_during_build_block_rollout(self):
        for key in ('config_change', 'head_change'):
            with self.subTest(key=key):
                for path in ('fake-state.json', 'commands.jsonl'):
                    (self.root / path).unlink(missing_ok=True)
                self.scenario = {key: True}
                self.assert_failed(self.run_script())
                self.assertFalse(any(cmd[3] == 'compose' and self.compose_command(cmd)[0] == 'up' for cmd in self.mutations()))

    def test_backend_workflow_stops_worker_prepares_without_start_then_enforces_dependencies(self):
        result = self.run_script('--with-capi-backend')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.commands()
        operations = [self.compose_command(cmd) for cmd in self.mutations() if cmd[3] == 'compose']
        stop = next(cmd for cmd in commands if cmd[0] == 'docker' and cmd[3] == 'stop')
        prepare = next(cmd for cmd in commands if cmd[0] == 'docker' and cmd[3] == 'compose' and '--no-start' in cmd)
        start = next(cmd for cmd in commands if cmd[0] == 'docker' and cmd[3] == 'compose' and
                     self.compose_command(cmd)[0] == 'up' and 'x-capi-db-init' in cmd and '--no-start' not in cmd)
        gate = next(cmd for cmd in commands if cmd[0] == 'docker' and cmd[3] == 'compose' and
                    self.compose_command(cmd)[0] == 'up' and cmd[-1] == 'axonos')
        self.assertLess(commands.index(prepare), commands.index(stop))
        self.assertLess(commands.index(prepare), commands.index(start))
        self.assertLess(commands.index(start), commands.index(gate))
        self.assertNotIn('x-capi-worker', prepare)
        self.assertTrue(any(cmd[3:5] == ['network', 'inspect'] for cmd in commands[:commands.index(stop)] if cmd[0] == 'docker'))
        self.assertFalse(any(word in ('down', 'rm', '-v') for cmd in operations for word in cmd))
        self.assertFalse(any('axonos-launcher' in cmd or 'postgres' in cmd for cmd in operations))

    def test_backend_initializer_failure_prevents_gate_rollout(self):
        self.scenario = {'init_fail': 'x-capi-db-init'}
        self.assert_failed(self.run_script('--with-capi-backend'), 'failed health/init wait')
        self.assertFalse(any(cmd[3] == 'compose' and self.compose_command(cmd)[0] == 'up' and cmd[-1] == 'axonos'
                             for cmd in self.mutations()))

    def test_backend_initializers_execute_once_before_no_deps_worker_start(self):
        result = self.run_script('--with-capi-backend')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state()['initializer_executions'], {
            'x-capi-db-init': 1, 'x-capi-privacy-init': 1})
        worker_starts = [event['command'] for event in self.compose_events()
                         if event['command'][0] == 'up' and event['command'][-1] == 'x-capi-worker'
                         and '--no-start' not in event['command']]
        self.assertEqual(len(worker_starts), 1)
        self.assertIn('--no-deps', worker_starts[0])

    def test_backend_failed_or_replaced_prerequisites_block_worker_start(self):
        for scenario in ('prerequisite_failed', 'prerequisite_replaced'):
            for service in ('x-capi-db-init', 'x-capi-privacy-init', 'x-capi-postgres'):
                with self.subTest(scenario=scenario, service=service):
                    for name in ('commands.jsonl', 'fake-state.json', 'compose-events.jsonl'):
                        (self.root / name).unlink(missing_ok=True)
                    self.scenario = {scenario: service}
                    self.assert_failed(self.run_script('--with-capi-backend'))
                    self.assertTrue(self.state()['initialization_started'])
                    self.assertNotIn('backend_started', self.state())
                    self.assertNotIn('rolled', self.state())
                    self.assertEqual(self.state()['initializer_executions'], {
                        'x-capi-db-init': 1, 'x-capi-privacy-init': 1})

    def test_backend_worker_contract_failure_prevents_gate_rollout(self):
        self.scenario = {'worker_drift': True}
        self.assert_failed(self.run_script('--with-capi-backend'), 'Worker configuration/mounts differ')
        self.assertFalse(any(cmd[3] == 'compose' and self.compose_command(cmd)[0] == 'up' and cmd[-1] == 'axonos'
                             for cmd in self.mutations()))

    def test_backend_database_drift_prevents_even_worker_stop(self):
        self.scenario = {'db_drift': True}
        self.assert_failed(self.run_script('--with-capi-backend'), 'Existing dedicated database image/configuration/mounts differ')
        self.assertEqual(self.mutations(), [])

    def test_gate_health_wait_is_bounded_and_unhealthy_is_fatal(self):
        for health in ('starting', 'unhealthy', 'exited', 'no-healthcheck'):
            with self.subTest(health=health):
                for path in ('fake-state.json', 'commands.jsonl'):
                    (self.root / path).unlink(missing_ok=True)
                self.scenario = {'health_wait': health}
                self.assert_failed(self.run_script('--health-timeout', '1'),
                                   'timeout' if health == 'starting' else 'failed health/init wait')
                self.assertFalse(any(cmd[0] == 'curl' for cmd in self.commands()))

    def test_running_initializers_without_healthchecks_wait_for_successful_exit(self):
        self.scenario = {'init_running': True}
        result = self.run_script('--with-capi-backend', '--health-timeout', '5')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreaterEqual(self.state()['init_wait_x-capi-db-init'], 2)
        self.assertGreaterEqual(self.state()['init_wait_x-capi-privacy-init'], 2)

    def test_gate_starting_then_healthy_completes_and_missing_fails(self):
        self.scenario = {'health_wait': 'transition'}
        result = self.run_script('--health-timeout', '5')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in ('fake-state.json', 'commands.jsonl'):
            (self.root / path).unlink(missing_ok=True)
        self.scenario = {'post_missing': 'axonos'}
        self.assert_failed(self.run_script(), 'Missing/ambiguous service: axonos')

    def test_mode_image_mount_metadata_and_http_postflight_fail_closed(self):
        for key in ('post_mode', 'post_image', 'post_mount', 'mount_check_fail', 'curl_fail', 'http'):
            with self.subTest(failure=key):
                for path in ('fake-state.json', 'commands.jsonl'):
                    (self.root / path).unlink(missing_ok=True)
                self.scenario = {key: '502' if key == 'http' else True}
                result = self.run_script(trace=True)
                self.assert_failed(result)
                self.assertNotIn('DEPLOYMENT PASSED', result.stdout)
                self.assertFalse(any('down' in cmd or 'volume' in cmd and 'rm' in cmd for cmd in self.commands()))

    def test_missing_and_symlinked_files_refused(self):
        for name in ('.env', 'docker-compose.yml', 'docker-compose.x-capi.yml'):
            with self.subTest(file=name):
                path = self.root / name
                saved = path.read_text()
                path.unlink()
                self.assert_failed(self.run_script('--check'), 'Missing or symlinked')
                target = self.root / 'synthetic-file'
                target.write_text(saved)
                path.symlink_to(target)
                self.assert_failed(self.run_script('--check'), 'Missing or symlinked')
                path.unlink()
                path.write_text(saved)
        self.assertEqual(self.mutations(), [])

    def test_shared_tag_race_cannot_change_validated_deployment(self):
        self.scenario = {'tag_race_at_rollout': True}
        result = self.run_script()
        self.assert_failed(result, 'Shared tag changed concurrently')
        self.assertEqual(self.state()['shared_tag'], 'sha256:' + 'b' * 64)
        self.assertEqual(self.state()['deployed_image'], IMAGE)
        rollout = next(event for event in self.compose_events()
                       if event['command'][0] == 'up' and event['command'][-1] == 'axonos')
        self.assertEqual(rollout['override'], {'services': {'axonos': {'image': IMAGE}}})

    def test_relative_bake_context_resolves_against_checkout_not_caller_directory(self):
        self.scenario = {'relative_bake_context': True}
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state()['deployed_image'], IMAGE)

    def test_supported_containerd_derived_digests_do_not_block_successful_cleanup(self):
        self.scenario = {'image_store': 'containerd'}
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state()['deployed_image'], IMAGE)
        self.assertEqual(self.state()['candidate_images'], {})
        self.assertEqual(self.state()['removed_candidate_tags'], self.state()['bake']['target']['axonos']['tags'])

    def test_supported_containerd_prunes_owned_derived_digest_candidates_before_build(self):
        self.scenario = {'image_store': 'containerd'}
        candidates = {}
        for number in range(1, 5):
            run = format(number, '024x')
            tag = 'axonos-deploy-candidate:' + run
            candidates[tag] = {'Id': 'sha256:' + format(number, '064x'), 'RepoTags': [tag],
                'RepoDigests': [], 'Config': {'Labels': {
                    'com.axonos.deploy.candidate-owner': 'axonos-production-deploy-v1',
                    'com.axonos.deploy.candidate-run': run,
                    'com.axonos.deploy.candidate-created-ns': str(number)}}}
        (self.root / 'fake-state.json').write_text(json.dumps({'candidate_images': candidates}))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.commands()
        first_removal = next(command for command in commands if command[0] == 'docker' and command[3:5] == ['image', 'rm'])
        build = next(command for command in commands if command[0] == 'docker' and command[3:5] == ['buildx', 'bake'])
        self.assertLess(commands.index(first_removal), commands.index(build))
        self.assertEqual(first_removal[-1], 'axonos-deploy-candidate:' + format(1, '024x'))
        self.assertEqual(set(self.state()['candidate_images']), set(candidates) - {first_removal[-1]})

    def test_unreviewed_or_contradictory_image_store_refuses_before_build_or_backend_mutation(self):
        cases = (
            {'ServerVersion': '29.5.3'},
            {'ServerVersion': None},
            {'OSType': 'windows'},
            {'Driver': 'unreviewed'},
            {'Driver': 'overlay2', 'DriverStatus': [['driver-type', 'io.containerd.snapshotter.v1']]},
            {'Driver': 'overlayfs', 'DriverStatus': []},
            {'DriverStatus': {'driver-type': 'io.containerd.snapshotter.v1'}},
        )
        for info in cases:
            with self.subTest(info=info):
                self.scenario = {'image_store_info': info}
                result = self.run_script('--with-capi-backend')
                self.assert_failed(result)
                self.assertEqual(self.mutations(), [])
                self.assertNotIn('built', self.state())
                self.assertNotIn('stopped_worker', self.state())

    def test_containerd_28_forced_api_147_refuses_with_empty_candidate_inventory(self):
        self.scenario = {'image_store': 'containerd', 'image_store_info': {'ServerVersion': '28.5.2'},
                         'docker_api_version': '1.47'}
        for options in ((), ('--check',), ('--with-capi-backend',)):
            with self.subTest(options=options):
                result = self.run_script(*options)
                self.assert_failed(result)
                self.assertEqual(self.mutations(), [])
                self.assertEqual(self.state().get('candidate_images', {}), {})
                self.assertNotIn('built', self.state())
                self.assertNotIn('promoted', self.state())
                self.assertNotIn('stopped_worker', self.state())
        self.assertTrue(any(command[3:] == ['version', '--format', '{{json .}}']
                            for command in self.commands() if command[0] == 'docker'))

    def test_supported_containerd_api_and_existing_image_probe_precede_build(self):
        for engine, override in (('28.5.2', '1.48'), ('28.5.2', None), ('29.5.1', None)):
            with self.subTest(engine=engine, override=override):
                self.scenario = {'image_store': 'containerd', 'image_store_info': {'ServerVersion': engine}}
                if override is not None:
                    self.scenario['docker_api_version'] = override
                offset = len(self.commands())
                result = self.run_script()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                commands = self.commands()[offset:]
                probe = next(command for command in commands
                             if command[0] == 'docker' and command[3:] == ['image', 'inspect', IMAGE])
                build = next(command for command in commands
                             if command[0] == 'docker' and command[3:5] == ['buildx', 'bake'])
                self.assertLess(commands.index(probe), commands.index(build))
                self.assertEqual(self.state()['deployed_image'], IMAGE)
                self.assertEqual(self.state()['candidate_images'], {})

    def test_actual_host_2952_containerd_api154_admits_without_override_or_candidates(self):
        self.scenario = {'image_store': 'containerd', 'image_store_info': {'ServerVersion': '29.5.2'}}
        _, environment = self.prepare_run('--check')
        self.assertNotIn('DOCKER_API_VERSION', environment)
        self.assertEqual(self.state().get('candidate_images', {}), {})
        result = self.run_script('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Docker API 1.54:', result.stdout)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.state().get('candidate_images', {}), {})

        offset = len(self.commands())
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.commands()[offset:]
        version = next(command for command in commands
                       if command[0] == 'docker' and command[3:] == ['version', '--format', '{{json .}}'])
        probe = next(command for command in commands
                     if command[0] == 'docker' and command[3:] == ['image', 'inspect', IMAGE])
        build = next(command for command in commands
                     if command[0] == 'docker' and command[3:5] == ['buildx', 'bake'])
        self.assertLess(commands.index(version), commands.index(probe))
        self.assertLess(commands.index(probe), commands.index(build))
        self.assertEqual(self.state()['deployed_image'], IMAGE)
        self.assertEqual(self.state()['candidate_images'], {})

    def test_containerd_2952_rejects_api147_and155_before_mutation(self):
        for version in ('1.47', '1.55'):
            with self.subTest(version=version):
                self.scenario = {'image_store': 'containerd', 'image_store_info': {'ServerVersion': '29.5.2'},
                                 'docker_api_version': version}
                self.assert_failed(self.run_script('--with-capi-backend'))
                self.assertEqual(self.mutations(), [])
                self.assertEqual(self.state().get('candidate_images', {}), {})
                self.assertNotIn('built', self.state())
                self.assertNotIn('promoted', self.state())
                self.assertNotIn('stopped_worker', self.state())

    def test_negotiated_api_downgrade_refuses_even_with_new_client_and_engine(self):
        self.scenario = {'image_store': 'containerd', 'image_store_info': {'ServerVersion': '28.5.2'},
                         'negotiated_api': '1.47', 'client_default_api': '1.54'}
        result = self.run_script('--with-capi-backend')
        self.assert_failed(result)
        self.assertEqual(self.mutations(), [])
        self.assertNotIn('built', self.state())
        self.assertNotIn('stopped_worker', self.state())

    def test_missing_or_contradictory_api_metadata_refuses_before_mutation(self):
        cases = ({'version_metadata': {}}, {'version_client': {'ApiVersion': None}},
                 {'version_client': {'DefaultAPIVersion': None}}, {'version_server': {'MinAPIVersion': None}},
                 {'version_server': {'ApiVersion': '1.51'}}, {'version_server': {'Version': '28.5.2'}},
                 {'docker_api_version': '1.48', 'version_client': {'ApiVersion': '1.49'}},
                 {'docker_api_version': '1.54', 'client_default_api': '1.48'})
        for override in cases:
            with self.subTest(override=override):
                self.scenario = {'image_store': 'containerd', **override}
                self.assert_failed(self.run_script('--with-capi-backend'))
                self.assertEqual(self.mutations(), [])
                self.assertNotIn('built', self.state())

    def test_existing_image_capability_probe_refuses_missing_or_malformed_metadata(self):
        cases = ({'probe_metadata': {'Descriptor': None}},
                 {'probe_metadata': {'Descriptor': {'digest': 'sha256:' + 'b' * 64,
                                                    'mediaType': 'application/vnd.oci.image.index.v1+json'}}},
                 {'probe_metadata': {'Id': 'sha256:' + 'b' * 64}},
                 {'probe_metadata': {'Config': None}}, {'probe_metadata': {'RepoDigests': []}},
                 {'probe_response': []})
        for override in cases:
            with self.subTest(override=override):
                self.scenario = {'image_store': 'containerd', **override}
                previous_probes = self.state().get('probe_image_inspects', 0)
                self.assert_failed(self.run_script('--with-capi-backend'))
                self.assertGreater(self.state().get('probe_image_inspects', 0), previous_probes)
                self.assertEqual(self.mutations(), [])
                self.assertNotIn('built', self.state())
                self.assertNotIn('stopped_worker', self.state())

    def test_network_ipv4_capability_cannot_be_assumed_from_missing_or_malformed_field(self):
        for value in ('missing', None, 'true'):
            with self.subTest(value=value):
                self.scenario = {'network_ipv4_metadata': value}
                self.assert_failed(self.run_script('--check'))
                self.assertEqual(self.mutations(), [])

    def test_orphan_removal_forced_off_despite_shell_and_env_file(self):
        with (self.root / '.env').open('a') as stream:
            stream.write('COMPOSE_REMOVE_ORPHANS=1\n')
        result = self.run_script('--with-capi-backend')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.compose_events()
        self.assertTrue(events)
        self.assertTrue(all(event['remove_orphans'] in ('0', 'false') for event in events))
        self.assertTrue(all(not event['stdin_is_tty'] for event in events))
        self.assertTrue(all(event['stdin_data'] == '' for event in events))
        self.assertFalse(any('--remove-orphans' in event['command'] for event in events))

    def test_actual_network_drift_blocks_all_mutation(self):
        for drift in ('internal', 'owner', 'driver', 'attachment', 'options'):
            with self.subTest(drift=drift):
                self.scenario = {'network_drift': drift}
                self.assert_failed(self.run_script('--with-capi-backend'))
                self.assertEqual(self.mutations(), [])

    def test_actual_volume_drift_blocks_all_mutation(self):
        for drift in ('owner', 'logical', 'driver', 'options', 'scope', 'mountpoint'):
            with self.subTest(drift=drift):
                self.scenario = {'volume_drift': drift}
                self.assert_failed(self.run_script('--with-capi-backend'))
                self.assertEqual(self.mutations(), [])
        self.scenario = {'data_owner_drift': True}
        self.assert_failed(self.run_script('--with-capi-backend'))
        self.assertEqual(self.mutations(), [])

    def test_compose_resource_hash_drift_refused_for_capi_and_preserved_core(self):
        for kind, names in (
            ('network', ('axonos_x_capi_db', 'axonos_control', 'axonos_stack')),
            ('volume', ('axonos_x_capi_postgres_data', 'axonos_axonos_postgres_data'))):
            for name in names:
                with self.subTest(kind=kind, name=name):
                    self.scenario = {kind + '_hash_drift': name}
                    self.assert_failed(self.run_script('--with-capi-backend'))
                    self.assertEqual(self.mutations(), [])

    def test_unexpected_checkout_provenance_refused_before_mutation(self):
        self.scenario = {'cross_checkout': True}
        self.assert_failed(self.run_script())
        self.assertEqual(self.mutations(), [])

    def test_shared_lock_refuses_concurrent_deployment_and_check(self):
        import fcntl
        with (self.root / 'deploy.lock').open('rb') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_failed(self.run_script('--check'))
            self.assertEqual(self.mutations(), [])

    def test_two_checkouts_share_one_project_lock_during_build(self):
        second = DeploymentOrchestrationTests(methodName='test_help_never_contacts_tools')
        second.setUp()
        self.addCleanup(second.doCleanups)
        shim = second.bin / 'python3'
        shim.write_text(shim.read_text().replace("str(root / 'deploy.lock')", repr(str(self.root / 'deploy.lock'))))
        self.scenario = {'hold_build': True}
        command, env = self.prepare_run()
        first = subprocess.Popen(command, cwd=self.root, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while not (self.root / 'build-entered').exists() and first.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root / 'build-entered').exists())
            second.assert_failed(second.run_script('--check'))
            self.assertEqual(second.mutations(), [])
        finally:
            (self.root / 'build-release').write_text('continue')
            stdout, stderr = first.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, stdout + stderr)
        self.assertNotIn(SECRET, stdout + stderr)

    def test_missing_or_insecure_shared_lock_fails_closed(self):
        path = self.root / 'deploy.lock'
        path.chmod(0o666)
        self.assert_failed(self.run_script('--check'))
        path.unlink()
        self.assert_failed(self.run_script('--check'))
        self.assertEqual(self.mutations(), [])

    def test_term_resistant_validator_and_child_killed_container_removed(self):
        self.scenario = {'validator_timeout': True}
        started = time.monotonic()
        self.assert_failed(self.run_script())
        self.assertLess(time.monotonic() - started, 15)
        pids = json.loads((self.root / 'resistant-pids.json').read_text())
        for pid in pids:
            path = Path('/proc') / str(pid) / 'stat'
            # A killed orphan may briefly remain a zombie pending PID1 reaping.
            try:
                self.assertEqual(path.read_text().split()[2], 'Z', pid)
            except FileNotFoundError:
                pass
        self.assertEqual(self.state()['validator_containers'], {})
        self.assertFalse(any(cmd[3:5] == ['image', 'tag'] for cmd in self.mutations()))

    def test_repeated_signals_do_not_bypass_kill_cleanup_or_project_lock(self):
        import fcntl
        self.scenario = {'repeated_signals': True}
        command, env = self.prepare_run()
        process = subprocess.Popen(command, cwd=self.root, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        pids = []

        def wait_for(name):
            deadline = time.monotonic() + 12
            path = self.root / name
            while not path.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(path.exists(), name)
            return path

        def assert_locked():
            with (self.root / 'deploy.lock').open('rb') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def assert_dead(pid):
            try:
                self.assertEqual((Path('/proc') / str(pid) / 'stat').read_text().split()[2], 'Z', pid)
            except FileNotFoundError:
                pass

        try:
            pids = json.loads(wait_for('resistant-pids.json').read_text())
            assert_locked()
            process.send_signal(signal.SIGINT)
            wait_for('validator-term-received')
            assert_locked()
            # This second signal arrives while the TERM-resistant group is
            # still inside its grace period, before the mandatory SIGKILL.
            process.send_signal(signal.SIGTERM)
            wait_for('validator-cleanup-entered')
            for pid in pids:
                assert_dead(pid)
            assert_locked()
            # Cleanup itself must also survive another catchable interruption.
            process.send_signal(signal.SIGINT)
            time.sleep(.05)
            assert_locked()
            (self.root / 'validator-cleanup-release').write_text('continue')
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertNotIn(SECRET, stdout + stderr)
            self.assertEqual(self.state()['validator_containers'], {})
            self.assertNotIn('promoted', self.state())
            with (self.root / 'deploy.lock').open('rb') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            (self.root / 'validator-cleanup-release').write_text('continue')
            # Regression failures must not leave this synthetic process group
            # alive. Every PID below came only from this test's executable fake.
            if pids:
                try:
                    os.killpg(pids[0], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_validator_cleanup_failure_blocks_promotion_and_reports_failure(self):
        self.scenario = {'cleanup_fail': True}
        self.assert_failed(self.run_script())
        self.assertFalse(any(cmd[3:5] == ['image', 'tag'] for cmd in self.mutations()))
        self.assertNotIn('rolled', self.state())

    def test_partial_create_timeout_discovers_and_removes_orphan_by_owned_name(self):
        self.scenario = {'validator_create_timeout': True}
        self.assert_failed(self.run_script())
        self.assertEqual(self.state()['validator_containers'], {})
        self.assertTrue(any(cmd[3:6] == ['container', 'ls', '-a'] for cmd in self.commands() if cmd[0] == 'docker'))
        self.assertFalse(any(cmd[3:5] == ['image', 'tag'] for cmd in self.mutations()))

    def test_validator_cleanup_refuses_foreign_container(self):
        self.scenario = {'foreign_validator': True}
        self.assert_failed(self.run_script())
        self.assertTrue(self.state()['validator_containers'])
        self.assertFalse(any(cmd[3] == 'rm' for cmd in self.mutations()))
        self.assertFalse(any(cmd[3:5] == ['image', 'tag'] for cmd in self.mutations()))

    def test_gate_worker_shared_scope_mismatch_fails_before_build(self):
        valid = copy.deepcopy(self.config)
        variants = {'AXGT_REVENUE_WALLET': '0x' + '1' * 40,
            'AXONOS_TEST_CREDIT_WALLETS': '0x' + '2' * 40,
            'X_CAPI_EXCLUDED_WALLETS': '0x' + '3' * 40,
            'X_CAPI_PRODUCTION_CHAIN_IDS': '1', 'X_CAPI_DEPLOYMENT_ID': 'other',
            'X_CAPI_CONSENT_POLICY_VERSION': 'other-policy',
            'X_CAPI_CONSENT_POLICY_EPOCH': '2', 'X_CAPI_ATTRIBUTION_TTL_DAYS': '8',
            'X_CAPI_MAX_EVENT_AGE_HOURS': '48'}
        for key, value in variants.items():
            with self.subTest(setting=key):
                self.config = copy.deepcopy(valid)
                self.config['services']['x-capi-worker']['environment'][key] = value
                self.assert_failed(self.run_script())
                self.assertEqual(self.mutations(), [])

    def test_preserved_launcher_token_and_database_drift_block_before_mutation(self):
        variants = [{'launcher_token_drift': True}] + [
            {'core_db_drift': key} for key in ('POSTGRES_PASSWORD', 'POSTGRES_USER', 'POSTGRES_DB')]
        for scenario in variants:
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                self.assert_failed(self.run_script('--with-capi-backend'))
                self.assertEqual(self.mutations(), [])

    def test_backend_readiness_and_pull_failures_leave_original_worker_running(self):
        for scenario in ({'unhealthy': 'x-capi-postgres'}, {'backend_pull_fail': True}):
            with self.subTest(scenario=scenario):
                for name in ('commands.jsonl', 'fake-state.json'):
                    (self.root / name).unlink(missing_ok=True)
                self.scenario = scenario
                self.assert_failed(self.run_script('--with-capi-backend', '--health-timeout', '1'))
                self.assertNotIn('stopped_worker', self.state())
                self.assertNotIn('initialization_started', self.state())

    def test_backend_preparation_failure_preserves_original_worker(self):
        self.scenario = {'backend_prepare_fail': True}
        self.assert_failed(self.run_script('--with-capi-backend'))
        self.assertNotIn('stopped_worker', self.state())
        self.assertNotIn('initialization_started', self.state())

    def test_backend_pre_initialization_failure_restores_exact_old_worker(self):
        self.scenario = {'resource_fail_after_stop_once': True}
        self.assert_failed(self.run_script('--with-capi-backend'))
        state = self.state()
        self.assertEqual(state['stopped_worker'], format(SERVICES.index('x-capi-worker') + 1, '064x'))
        self.assertEqual(state['restored_worker'], state['stopped_worker'])
        self.assertNotIn('initialization_started', state)
        self.assertNotIn('rolled', state)

    def test_post_initialization_failure_never_restores_old_worker_or_rolls_gate(self):
        self.scenario = {'init_fail': 'x-capi-db-init'}
        self.assert_failed(self.run_script('--with-capi-backend'))
        state = self.state()
        self.assertTrue(state['initialization_started'])
        self.assertNotIn('restored_worker', state)
        self.assertNotIn('rolled', state)


class DeploymentMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('deployment_checks_test',
            REPO / 'scripts/deploy_production_checks.py')
        cls.checks = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, 'path', [str(REPO / 'scripts'), *sys.path]):
            spec.loader.exec_module(cls.checks)

    def metadata(self, **changes):
        values = dict(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=10001,
                      st_gid=10001, st_size=64, st_dev=1, st_ino=2,
                      st_mtime_ns=3, st_ctime_ns=4)
        return types.SimpleNamespace(**dict(values, **changes))

    def test_secret_metadata_never_reads_contents(self):
        with mock.patch.object(Path, 'resolve', lambda path: path), \
                mock.patch.object(Path, 'lstat', return_value=self.metadata()), \
                mock.patch('builtins.open', side_effect=AssertionError('must not read secret')):
            result = self.checks.secret_metadata('/synthetic/secrets/key', REPO, 10001, 44, 4096)
        self.assertEqual(result[0], '/synthetic/secrets/key')

    def test_secret_metadata_rejects_wrong_type_owner_permissions_links_size(self):
        for changes in ({'st_mode': stat.S_IFLNK | 0o600}, {'st_mode': stat.S_IFDIR | 0o600},
                        {'st_mode': stat.S_IFREG | 0o644}, {'st_uid': 0}, {'st_gid': 0},
                        {'st_nlink': 2}, {'st_size': 0}, {'st_size': 4097}):
            with self.subTest(changes=changes), mock.patch.object(Path, 'resolve', lambda path: path), \
                    mock.patch.object(Path, 'lstat', return_value=self.metadata(**changes)):
                with self.assertRaises(self.checks.Refusal):
                    self.checks.secret_metadata('/synthetic/secrets/key', REPO, 10001, 44, 4096)

    def test_direct_secret_metadata_permission_error_has_sanitized_refusal(self):
        with mock.patch.object(Path, 'resolve', lambda path: path), \
                mock.patch.object(Path, 'lstat', side_effect=PermissionError(SECRET)):
            with self.assertRaisesRegex(self.checks.Refusal, '^Secret metadata cannot be attested with current privileges$'):
                self.checks.secret_metadata('/synthetic/secrets/key', REPO, 10001, 44, 4096)

    def test_secret_metadata_rejects_build_context_and_noncanonical_paths(self):
        with mock.patch.object(Path, 'resolve', lambda path: path):
            with self.assertRaises(self.checks.Refusal):
                self.checks.secret_metadata(REPO / 'secret', REPO, 10001, 44, 4096)
        with mock.patch.object(Path, 'resolve', return_value=Path('/other')):
            with self.assertRaises(self.checks.Refusal):
                self.checks.secret_metadata('/synthetic/link', REPO, 10001, 44, 4096)

    def test_config_real_validator_rejects_security_boundary_changes(self):
        valid = configuration(REPO)
        changes = [
            lambda c: c.update(name='other'),
            lambda c: c['services']['axonos']['build'].update(context='/other'),
            lambda c: c['services']['axonos']['build'].update(args={'AXONOS_SKIP_HEAVY': '1'}),
            lambda c: c['services']['axonos']['environment'].update(X_CAPI_ACCESS_TOKEN=SECRET),
            lambda c: c['services']['x-capi-worker']['environment'].update(X_CAPI_DB_URL=SECRET),
            lambda c: c['services']['axonos']['environment'].update(X_CAPI_ALLOW_TEST_IDS='true'),
            lambda c: c['services']['axonos']['volumes'][0].update(read_only=False),
            lambda c: c['services']['axonos']['volumes'][2]['bind'].update(create_host_path=True),
            lambda c: c['services']['x-capi-worker']['volumes'].append(volume('/x-token', '/token', 'bind', True)),
            lambda c: c['networks']['x_capi_db'].update(internal=False),
            lambda c: c['services']['x-capi-worker'].update(networks={'default': None}),
            lambda c: c['services']['x-capi-worker']['depends_on']['x-capi-db-init'].update(condition='service_started'),
            lambda c: c['volumes']['x_capi_postgres_data'].update(name='core_database'),
        ]
        with mock.patch.object(self.checks, 'secret_metadata', return_value=('synthetic',)), redirect_stdout(io.StringIO()):
            self.checks.config_check(valid, REPO, 'off')
            for change in changes:
                config = copy.deepcopy(valid)
                change(config)
                with self.subTest(config=config), self.assertRaises(self.checks.Refusal):
                    self.checks.config_check(config, REPO, 'off')

    def test_sessions_detect_actual_prefix_or_label_without_printing_other_metadata(self):
        output = io.StringIO()
        records = [{'Names': 'axgt-session-a', 'Labels': '', 'Image': SECRET},
                   {'Names': 'future-name', 'Labels': 'com.axonos.session-container=true', 'Status': SECRET},
                   {'Names': 'axonos', 'Labels': ''}]
        with redirect_stdout(output):
            self.checks.sessions('\n'.join(json.dumps(record) for record in records))
        self.assertEqual(output.getvalue().splitlines(), ['axgt-session-a', 'future-name'])
        with self.assertRaises(self.checks.Refusal):
            self.checks.sessions(json.dumps({'Names': 'axgt-session-\nINJECT', 'Labels': ''}))

    def test_in_container_mount_check_attests_metadata_without_secret_reads(self):
        spec = importlib.util.spec_from_file_location('deployment_mount_test',
            REPO / 'scripts/deploy_production_mount_check.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        runtime = '/run/axonos-x-capi'
        privacy = '/run/axonos-x-capi-privacy'
        key = '/run/secrets/x_capi_context_key'
        correct = {runtime: self.metadata(st_mode=stat.S_IFDIR | 0o700),
                   privacy: self.metadata(st_mode=stat.S_IFDIR | 0o700), key: self.metadata()}
        def check(records, realpath=lambda path: path):
            with mock.patch.object(module.os, 'lstat', side_effect=records.__getitem__), \
                    mock.patch.object(module.os.path, 'realpath', side_effect=realpath), \
                    mock.patch('builtins.open', side_effect=AssertionError('must not read secret')):
                return module.check()
        self.assertTrue(check(correct))
        for path, changes in ((runtime, {'st_mode': stat.S_IFDIR | 0o755}),
                              (privacy, {'st_uid': 0}), (privacy, {'st_gid': 0}),
                              (key, {'st_mode': stat.S_IFREG | 0o644}),
                              (key, {'st_nlink': 2}), (key, {'st_size': 0}),
                              (key, {'st_mode': stat.S_IFLNK | 0o600})):
            with self.subTest(path=path, changes=changes):
                records = copy.deepcopy(correct)
                for attribute, value in changes.items():
                    setattr(records[path], attribute, value)
                self.assertFalse(check(records))
        self.assertFalse(check(correct, lambda path: '/synthetic-other'))

    def test_database_and_worker_contract_checks_reject_runtime_drift(self):
        config = configuration(REPO)
        def inspect(service):
            source = config['services'][service]
            mounts = []
            for entry in source['volumes']:
                mount = {'Destination': entry['target'], 'Type': entry['type'],
                         'RW': not entry.get('read_only', False)}
                if entry['type'] == 'volume':
                    mount['Name'] = config['volumes'][entry['source']]['name']
                else:
                    mount['Source'] = entry['source']
                mounts.append(mount)
            return [{'Config': {'Labels': {'com.docker.compose.project': 'axonos',
                            'com.docker.compose.service': service},
                        'Image': source.get('image'), 'Cmd': source.get('command'), 'User': '10001:10001',
                        'Env': [key + '=' + value for key, value in source['environment'].items()]},
                    'Mounts': mounts, 'HostConfig': {'ReadonlyRootfs': True},
                    'NetworkSettings': {'Networks': {'axonos_x_capi_db': {}}}}]
        for service, get_contract, checker in (
                ('x-capi-postgres', self.checks.configured_database,
                 lambda document, expected: self.checks.database_check(document, expected)),
                ('x-capi-worker', self.checks.configured_worker,
                 lambda document, expected: self.checks.worker_check(document, 'off', expected))):
            output = io.StringIO()
            with redirect_stdout(output):
                get_contract(config)
            expected = output.getvalue().strip()
            document = inspect(service)
            checker(document, expected)
            for mutate in (
                    lambda d: d[0]['HostConfig'].update(ReadonlyRootfs=False),
                    lambda d: d[0]['NetworkSettings'].update(Networks={'core-network': {}}),
                    lambda d: d[0]['Mounts'].append({'Destination': '/extra', 'Type': 'bind', 'Source': '/synthetic', 'RW': True}),
                    lambda d: d[0]['Config']['Env'].append(
                        'POSTGRES_DB=other' if service == 'x-capi-postgres' else 'X_CAPI_QUEUE_LIMIT=200')):
                with self.subTest(service=service):
                    modified = copy.deepcopy(document)
                    mutate(modified)
                    with self.assertRaises(self.checks.Refusal):
                        checker(modified, expected)


if __name__ == '__main__':
    unittest.main()
