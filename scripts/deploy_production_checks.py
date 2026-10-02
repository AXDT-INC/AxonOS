#!/usr/bin/env python3
"""Secret-safe deployment checks. JSON inputs stay in memory; never log inputs.

No Docker execution, secret-file reads, database connections or HTTP requests.
The caller pipes resolved Compose/inspect JSON here rather than storing it.
"""

import hashlib
import json
from pathlib import Path
import re
import stat
import sys


class Refusal(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refusal(message)


def capi_module(root):
    # Reuse the current implementation's modes and pure config validator.
    sys.path.insert(0, str(root))
    from axonos_gate import x_capi
    return x_capi


def env_mode(path):
    lines = Path(path).read_text().splitlines()
    matches = [line for line in lines if re.match(r"^\s*(?:export\s+)?X_CAPI_MODE\s*=", line)]
    require(len(matches) == 1, '.env must contain exactly one explicit X_CAPI_MODE assignment')
    match = re.fullmatch(r"\s*(?:export\s+)?X_CAPI_MODE\s*=\s*(?:'([a-z_]+)'|\"([a-z_]+)\"|([a-z_]+))\s*(?:#.*)?", matches[0])
    require(match is not None, 'X_CAPI_MODE must be a literal mode, not interpolation or shell code')
    mode = next(value for value in match.groups() if value is not None)
    capi = capi_module(Path(path).resolve().parent)
    require(mode in capi._MODE_VALUES, 'Unsupported X_CAPI_MODE')
    require(mode != 'live', 'Live deployment is structurally blocked by the current provenance gate')
    print(mode)


def secret_metadata(path, root, uid, low, high):
    path = Path(path)
    require(path.is_absolute() and str(path.resolve()) == str(path), 'Secret path must be absolute and canonical')
    require(root not in path.parents, 'Secret files must be outside the repository/build context')
    info = path.lstat()  # Intentionally never open/read the secret.
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Secret must be a single-link regular file')
    require(info.st_uid == uid and info.st_gid == uid, 'Secret ownership does not match the reviewed CAPI contract')
    require(stat.S_IMODE(info.st_mode) in (0o400, 0o600), 'Secret permissions must be 0400 or 0600')
    require(low <= info.st_size <= high, 'Secret size outside reviewed bounds')
    return (str(path), info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def volume(service, target, kind, source=None, readonly=False):
    found = [item for item in service.get('volumes', []) if item.get('target') == target]
    require(len(found) == 1, 'Required CAPI mount missing or duplicated')
    item = found[0]
    require(item.get('type') == kind and bool(item.get('read_only', False)) == readonly,
            'Required CAPI mount type/access differs from reviewed configuration')
    if source is not None:
        require(item.get('source') == source, 'Unexpected CAPI mount source')
    if kind == 'bind':
        require(item.get('bind', {}).get('create_host_path') is False, 'Secret bind must not create host paths')
    return item['source']


def config_check(document, root, mode):
    root = Path(root).resolve()
    require(document.get('name') == 'axonos', 'Unexpected Compose project')
    services = document['services']
    gate, worker = services['axonos'], services['x-capi-worker']
    require(gate.get('image') == 'axonos:latest', 'Unexpected central image name')
    require(gate.get('build', {}).get('context') == str(root), 'Unexpected central build context')
    require(gate.get('build', {}).get('dockerfile', 'Dockerfile') == 'Dockerfile', 'Unexpected central Dockerfile')
    require(str(gate['build'].get('args', {}).get('AXONOS_SKIP_HEAVY', '0')) == '0', 'Production cannot skip heavy applications')
    capi = capi_module(root)
    for service in (gate, worker):
        env = service.get('environment', {})
        require(env.get('X_CAPI_MODE') == mode, 'Resolved CAPI mode differs from .env (check shell overrides)')
        require(not capi.load_config(env).errors, 'CAPI configuration invalid; review settings using docs/X_CAPI.md')
        require(not any(value for key, value in env.items() if key.startswith('X_CAPI_ALLOW_TEST')), 'CAPI test bypass is forbidden')
        for key in ('X_CAPI_ACCESS_TOKEN', 'X_CAPI_DB_URL', 'X_CAPI_CONTEXT_KEY', 'X_CAPI_HASH_KEY'):
            require(not env.get(key), 'CAPI secrets must not be provided through environment values')
        require(not env.get('X_CAPI_ACCESS_TOKEN_FILE'), 'Current deployment must not provision an X access token')
    for key, target in (('X_CAPI_DB_URL_FILE', 'x_capi_db_url'), ('X_CAPI_CONTEXT_KEY_FILE', 'x_capi_context_key'),
                        ('X_CAPI_HASH_KEY_FILE', 'x_capi_hash_key')):
        require(worker['environment'].get(key) == '/run/secrets/' + target, 'Incorrect worker secret-file path')
    require(gate['environment'].get('X_CAPI_CONTEXT_KEY_FILE') == '/run/secrets/x_capi_context_key', 'Incorrect gate context key path')
    require(gate['environment'].get('X_CAPI_PRIVACY_FENCE_DIR') == '/run/axonos-x-capi-privacy', 'Incorrect gate privacy path')
    for target, name, ro in (('/run/axonos-x-capi', 'x_capi_runtime', True),
                             ('/run/axonos-x-capi-privacy', 'x_capi_privacy_fence', False)):
        volume(gate, target, 'volume', name, ro)
        volume(worker, target, 'volume', name)
        volume(services['x-capi-privacy-init'], target, 'volume', name)
        require(document['volumes'][name]['name'] == 'axonos_' + name, 'Unexpected persistent CAPI volume name')
    require(len(gate.get('volumes', [])) == 3, 'Unreviewed central mounts; review the production contract')
    context = volume(gate, '/run/secrets/x_capi_context_key', 'bind', readonly=True)
    require(volume(worker, '/run/secrets/x_capi_context_key', 'bind', readonly=True) == context, 'Gate/worker context keys differ')
    specs = [(context, 10001, 44, 4096)]
    for target, low, high in (('x_capi_db_url', 1, 8192), ('x_capi_hash_key', 32, 256)):
        specs.append((volume(worker, '/run/secrets/' + target, 'bind', readonly=True), 10001, low, high))
    db_init = services['x-capi-db-init']
    for target in ('x_capi_postgres_bootstrap_password', 'x_capi_postgres_worker_password'):
        path = volume(db_init, '/run/secrets/' + target, 'bind', readonly=True)
        specs.append((path, 0, 16, 8192))
        if target.endswith('bootstrap_password'):
            volume(services['x-capi-postgres'], '/run/secrets/' + target, 'bind', path, True)
    require(len(worker.get('volumes', [])) == 5, 'Unreviewed worker mounts; no X token belongs to this deployment')
    for name in ('x-capi-worker', 'x-capi-postgres', 'x-capi-db-init'):
        require(set(services[name].get('networks', {})) == {'x_capi_db'}, 'CAPI database network isolation changed')
    require(document['networks']['x_capi_db'].get('internal') is True, 'CAPI network must remain internal')
    require(services['x-capi-privacy-init'].get('network_mode') == 'none', 'Privacy initializer must be network-isolated')
    for name, condition in (('x-capi-postgres', 'service_healthy'),
                            ('x-capi-db-init', 'service_completed_successfully'),
                            ('x-capi-privacy-init', 'service_completed_successfully')):
        require(worker['depends_on'][name]['condition'] == condition, 'CAPI dependency gate changed')
    volume(services['x-capi-postgres'], '/var/lib/postgresql/data', 'volume', 'x_capi_postgres_data')
    require(document['volumes']['x_capi_postgres_data']['name'] == 'axonos_x_capi_postgres_data', 'Unexpected dedicated DB volume')
    # Runtime metadata is rechecked after the potentially long build. This is a
    # change detector, not a digest of secret contents (none are read).
    metadata = [secret_metadata(path, root, uid, low, high) for path, uid, low, high in specs]
    digest = hashlib.sha256(json.dumps([document, metadata], sort_keys=True).encode()).hexdigest()
    print(digest)


def inspected(document, service):
    require(isinstance(document, list) and len(document) == 1, 'Expected one inspected service container')
    item = document[0]
    labels = item.get('Config', {}).get('Labels', {})
    require(labels.get('com.docker.compose.project') == 'axonos' and labels.get('com.docker.compose.service') == service,
            'Container does not belong to the expected Compose project/service')
    return item


def state_check(document, service):
    item = inspected(document, service)
    state = item['State']
    status = state['Status']
    require(status in ('created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'), 'Unknown container state')
    if status == 'exited':
        print('exited:' + str(int(state['ExitCode'])))
    elif status == 'running':
        health = state.get('Health', {}).get('Status', 'no-healthcheck')
        require(health in ('healthy', 'unhealthy', 'starting', 'no-healthcheck'), 'Unknown health status')
        print(health)
    else:
        print(status)


def environment(item):
    return dict(entry.split('=', 1) for entry in item['Config']['Env'] if '=' in entry)


def database_contract(image, env, command, secret, data, networks, readonly):
    values = [image, [env.get(key) for key in ('POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD_FILE')],
              command, secret, data, sorted(networks), readonly]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def configured_database(document):
    db = document['services']['x-capi-postgres']
    secret = volume(db, '/run/secrets/x_capi_postgres_bootstrap_password', 'bind', readonly=True)
    source = volume(db, '/var/lib/postgresql/data', 'volume')
    data = document['volumes'][source]['name']
    networks = [document['networks'][name]['name'] for name in db['networks']]
    print(database_contract(db['image'], db['environment'], db['command'], secret, data, networks, db['read_only']))


def database_check(document, expected):
    item = inspected(document, 'x-capi-postgres')
    mounts = {mount['Destination']: mount for mount in item['Mounts']}
    require({m['Destination'] for m in item['Mounts'] if m['Type'] != 'tmpfs'} == {
        '/run/secrets/x_capi_postgres_bootstrap_password', '/var/lib/postgresql/data'
    }, 'Existing database has unreviewed mounts')
    secret = mounts.get('/run/secrets/x_capi_postgres_bootstrap_password', {})
    data = mounts.get('/var/lib/postgresql/data', {})
    require(secret.get('Type') == 'bind' and secret.get('RW') is False, 'Existing database secret bind mismatch')
    require(data.get('Type') == 'volume' and data.get('RW') is True, 'Existing database data volume mismatch')
    actual = database_contract(item['Config']['Image'], environment(item), item['Config']['Cmd'],
                               secret['Source'], data['Name'], item['NetworkSettings']['Networks'],
                               item['HostConfig']['ReadonlyRootfs'])
    require(actual == expected, 'Existing dedicated database image/configuration/mounts differ from the reviewed Compose target')


def worker_contract(env, mounts, networks, readonly, user):
    keys = ('AXGT_REVENUE_WALLET', 'AXONOS_TEST_CREDIT_WALLETS', 'AXONOS_WHITELISTED_WALLETS', 'AXONOS_GUEST_INVITE_MINTERS')
    settings = {key: value for key, value in env.items() if key.startswith('X_CAPI_') or key in keys}
    return hashlib.sha256(json.dumps([settings, sorted(mounts), sorted(networks), readonly, user], sort_keys=True).encode()).hexdigest()


def configured_worker(document):
    worker = document['services']['x-capi-worker']
    mounts = [(m['target'], m['type'], document['volumes'][m['source']]['name'] if m['type'] == 'volume' else m['source'],
               not m.get('read_only', False)) for m in worker['volumes']]
    networks = [document['networks'][name]['name'] for name in worker['networks']]
    # USER is defined by the reviewed worker Dockerfile, not Compose.
    print(worker_contract(worker['environment'], mounts, networks, worker['read_only'], worker.get('user', '10001:10001')))


def worker_check(document, mode, expected):
    item = inspected(document, 'x-capi-worker')
    env = environment(item)
    require(env.get('X_CAPI_MODE') == mode, 'Worker CAPI mode mismatch')
    mounts = [(m['Destination'], m['Type'], m['Name'] if m['Type'] == 'volume' else m['Source'], m['RW'])
              for m in item['Mounts'] if m['Type'] != 'tmpfs']
    require(worker_contract(env, mounts, item['NetworkSettings']['Networks'], item['HostConfig']['ReadonlyRootfs'], item['Config']['User']) == expected,
            'Worker configuration/mounts differ; coordinated backend deployment required')


def gate_check(document, root, mode, image):
    item = inspected(document, 'axonos')
    require(item['Image'] == image, 'Central container does not run the validated image')
    env = environment(item)
    require(env.get('X_CAPI_MODE') == mode, 'Central CAPI mode mismatch')
    for key, value in (('X_CAPI_CONTEXT_KEY_FILE', '/run/secrets/x_capi_context_key'),
                       ('X_CAPI_PRIVACY_FENCE_DIR', '/run/axonos-x-capi-privacy'),
                       ('X_CAPI_INGEST_SOCKET', '/run/axonos-x-capi/events.sock'),
                       ('X_CAPI_CONSENT_SOCKET', '/run/axonos-x-capi/consent.sock')):
        require(env.get(key) == value, 'Central CAPI path environment mismatch')
    mounts = {m['Destination']: m for m in item['Mounts']}
    require(len(mounts) == 3, 'Unexpected central container mounts')
    for target, name, writable in (('/run/axonos-x-capi', 'axonos_x_capi_runtime', False),
                                   ('/run/axonos-x-capi-privacy', 'axonos_x_capi_privacy_fence', True)):
        mount = mounts.get(target, {})
        require(mount.get('Type') == 'volume' and mount.get('Name') == name and mount.get('RW') is writable,
                'Central CAPI volume/access mismatch')
    key = mounts.get('/run/secrets/x_capi_context_key', {})
    require(key.get('Type') == 'bind' and key.get('RW') is False, 'Context key read-only bind missing')
    secret_metadata(key['Source'], Path(root).resolve(), 10001, 44, 4096)
    print('Central image, mode and required CAPI mounts verified')


def sessions(lines):
    for line in lines.splitlines():
        item = json.loads(line)
        name = item['Names']
        labels = item.get('Labels', '').split(',')
        if name.startswith('axgt-session-') or 'com.axonos.session-container=true' in labels:
            # No labels, images, command lines or arbitrary status strings are
            # echoed; only bounded safe Docker names identify active tenants.
            require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name), 'Unexpected tenant container name')
            print(name)


def main():
    command, *args = sys.argv[1:]
    if command == 'env-mode':
        env_mode(*args)
    elif command == 'sessions':
        sessions(sys.stdin.read())
    else:
        document = json.load(sys.stdin)
        if command == 'config':
            config_check(document, *args)
        elif command == 'state':
            state_check(document, *args)
        elif command == 'gate':
            gate_check(document, *args)
        elif command == 'worker':
            worker_check(document, *args)
        elif command == 'worker-contract':
            configured_worker(document)
        elif command == 'db-contract':
            configured_database(document)
        elif command == 'database':
            database_check(document, *args)
        else:
            raise Refusal('Unknown deployment check')


if __name__ == '__main__':
    try:
        main()
    except Refusal as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # JSON/config/path exceptions can contain secrets. Do not echo them.
        print('ERROR: deployment metadata/configuration check failed (details withheld)', file=sys.stderr)
        sys.exit(1)
