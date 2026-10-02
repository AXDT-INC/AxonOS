#!/usr/bin/env python3
"""Read-only, secret-safe attestation of supplied Compose and Docker JSON.

No Docker execution, network access, secret-file reads, or filesystem metadata
inspection. The caller supplies the rendered target and actual inspect objects
through stdin, never argv or an on-disk credential-bearing fixture.
"""

from dataclasses import asdict
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path, PurePosixPath
import sys
from urllib.parse import unquote, urlsplit


class Refusal(Exception):
    pass


def require(ok, message):
    if not ok:
        raise Refusal(message)


EXCLUSIONS = ('AXGT_REVENUE_WALLET', 'AXONOS_TEST_CREDIT_WALLETS',
              'AXONOS_WHITELISTED_WALLETS', 'AXONOS_GUEST_INVITE_MINTERS',
              'X_CAPI_EXCLUDED_WALLETS')
CAPI_SERVICES = ('x-capi-postgres', 'x-capi-worker', 'x-capi-db-init')
CAPI_VOLUMES = ('x_capi_runtime', 'x_capi_privacy_fence', 'x_capi_postgres_data')
COMPOSE_PREFIX = 'com.docker.compose.'


def compose_resource_hash(desired, kind):
    """Compose NetworkHash/VolumeHash, restricted to reviewed JSON fields.

    Source: docker/compose v2.39.2 and v5.1.4 pkg/compose/hash.go;
    compose-go v2.11.0 types/types.go. Go struct field order matters, IPAM is
    a value struct (always serialized), and VolumeHash defaults driver=local.
    This guards Compose reconciliation, NOT resource ownership/semantics.
    Unknown fields fail closed rather than guessing their hash representation.
    """
    fields = (('name', 'driver', 'driver_opts', 'ipam', 'external', 'internal',
               'attachable', 'labels', 'enable_ipv4', 'enable_ipv6') if kind == 'network'
              else ('name', 'driver', 'driver_opts', 'external', 'labels'))
    require(set(desired) <= set(fields), 'Unreviewed Compose resource hash configuration')
    serialized = {}
    for field in fields:
        value = desired.get(field)
        if field == 'driver' and kind == 'volume':
            value = value or 'local'
        if field == 'ipam':
            value = value or {}
            require(set(value) <= {'driver', 'config'}, 'Unreviewed Compose IPAM hash configuration')
            ipam = {}
            if value.get('driver'):
                ipam['driver'] = value['driver']
            if value.get('config'):
                pools = []
                for pool in value['config']:
                    keys = ('subnet', 'gateway', 'ip_range', 'aux_addresses')
                    require(set(pool) <= set(keys), 'Unreviewed Compose IPAM pool hash configuration')
                    pools.append({key: (dict(sorted(pool[key].items())) if key == 'aux_addresses' else pool[key])
                                  for key in keys if pool.get(key)})
                ipam['config'] = pools
            serialized[field] = ipam
        elif value or (field in ('enable_ipv4', 'enable_ipv6') and value is not None):
            serialized[field] = dict(sorted(value.items())) if isinstance(value, dict) else value
    # Match encoding/json's UTF-8 and HTML/line-separator escaping.
    encoded = json.dumps(serialized, separators=(',', ':'), ensure_ascii=False)
    for char in ('<', '>', '&', '\u2028', '\u2029'):
        encoded = encoded.replace(char, '\\u%04x' % ord(char))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def attest_resource_hash(desired, actual, kind):
    expected = compose_resource_hash(desired, kind)
    labels = actual.get('Labels') or {}
    key = COMPOSE_PREFIX + 'config-hash'
    # Compose accepts legacy absent hashes. A present empty volume hash, unlike
    # a network's empty hash, triggers reconciliation and must be rejected.
    if key in labels and not (kind == 'network' and labels[key] == ''):
        require(labels[key] == expected, 'Compose resource configuration hash drift; reconciliation refused')


def validate_shared(document, root):
    """Compare semantics using the application's own normalization, even off."""
    sys.path.insert(0, str(Path(root).resolve()))
    from axonos_gate import x_capi

    normalized = []
    for name in ('axonos', 'x-capi-worker'):
        env = document['services'][name]['environment']
        config = x_capi.load_config(env)
        require(not config.errors, 'Shared CAPI configuration is invalid')
        values = asdict(config)
        values['production_chain_ids'] = sorted(config.production_chain_ids)
        # Config.audience_scope can be empty while off; it also unions exclusion
        # sources. Preserve the revenue-wallet and each exclusion source's scope
        # separately so a future mode switch cannot expose a latent mismatch.
        values['exclusion_sources'] = {
            key: sorted({part.strip().lower() for part in str(env.get(key) or '').split(',')
                         if part.strip()}) for key in EXCLUSIONS}
        for key in ('X_CAPI_PRIVACY_RECOVERY_EPOCH', 'X_CAPI_CONTEXT_KEY_FILE',
                    'X_CAPI_PRIVACY_FENCE_DIR', 'X_CAPI_INGEST_SOCKET', 'X_CAPI_CONSENT_SOCKET'):
            values[key] = str(env.get(key) or '').strip()
        normalized.append(values)
    require(normalized[0] == normalized[1], 'Gate and worker shared CAPI configuration differs')


def owned_labels(actual, desired, kind, key):
    actual = actual or {}
    require(actual.get(COMPOSE_PREFIX + 'project') == 'axonos' and
            actual.get(COMPOSE_PREFIX + kind) == key,
            'Existing CAPI resource has unexpected Compose ownership')
    custom = {name: value for name, value in actual.items() if not name.startswith(COMPOSE_PREFIX)}
    require(custom == (desired.get('labels') or {}), 'Existing CAPI resource labels differ')


def by_name(items):
    require(isinstance(items, list), 'Resource inspection must be a list')
    result = {item['Name']: item for item in items}
    require(len(result) == len(items), 'Duplicate inspected resource')
    return result


def container(dependencies, service):
    items = dependencies.get(service, [])
    require(isinstance(items, list) and len(items) == 1, 'Required preserved dependency is missing or ambiguous')
    item = items[0]
    labels = item['Config'].get('Labels') or {}
    require(labels.get(COMPOSE_PREFIX + 'project') == 'axonos' and
            labels.get(COMPOSE_PREFIX + 'service') == service,
            'Preserved dependency has unexpected Compose ownership')
    return item


def environment(item):
    entries = item['Config'].get('Env') or []
    require(all(isinstance(entry, str) and '=' in entry for entry in entries),
            'Invalid dependency environment metadata')
    env = dict(entry.split('=', 1) for entry in entries)
    require(len(env) == len(entries), 'Duplicate dependency environment keys')
    return env


def network_check(document, actual, dependencies, key='x_capi_db'):
    desired = document['networks'][key]
    name = desired['name']
    capi = key == 'x_capi_db'
    require(name == ('axonos_x_capi_db' if capi else key) and not desired.get('external'), 'Unexpected managed network target')
    require(desired.get('driver', 'bridge') == 'bridge' and (not capi or desired.get('internal') is True),
            'Managed networks must remain bridge networks; CAPI must remain internal')
    require(actual.get('Name') == name and actual.get('Driver') == 'bridge' and
            actual.get('Scope') == 'local' and actual.get('Internal') == desired.get('internal', False),
            'Existing managed network isolation/driver differs')
    owned_labels(actual.get('Labels'), desired, 'network', key)
    attest_resource_hash(desired, actual, 'network')
    for compose_key, inspect_key, default in (('attachable', 'Attachable', False),
                                             ('enable_ipv6', 'EnableIPv6', False),
                                             ('enable_ipv4', 'EnableIPv4', True)):
        require(bool(actual.get(inspect_key, default)) == bool(desired.get(compose_key, default)),
                'Existing CAPI network address/attachment policy differs')
    require(not actual.get('Ingress', False) and not actual.get('ConfigOnly', False) and
            not (actual.get('ConfigFrom') or {}).get('Network'), 'Unsupported CAPI network configuration')
    require((actual.get('Options') or {}) == (desired.get('driver_opts') or {}),
            'Existing CAPI network driver options differ')
    target_ipam = desired.get('ipam') or {}
    ipam = actual.get('IPAM') or {}
    require(ipam.get('Driver', 'default') == target_ipam.get('driver', 'default') and
            (ipam.get('Options') or {}) == (target_ipam.get('options') or {}),
            'Existing CAPI network IPAM driver/options differ')
    target_pools = target_ipam.get('config') or []
    pools = ipam.get('Config') or []
    if target_pools:
        aliases = {'subnet': 'Subnet', 'ip_range': 'IPRange', 'gateway': 'Gateway',
                   'aux_addresses': 'AuxiliaryAddresses'}
        target_pools = [{aliases[key]: value for key, value in pool.items()} for pool in target_pools]
        require(sorted(json.dumps(pool, sort_keys=True) for pool in pools) ==
                sorted(json.dumps(pool, sort_keys=True) for pool in target_pools),
                'Existing CAPI network IPAM pools differ')
    else:
        # Docker allocates a subnet/gateway for an unspecified default bridge.
        # These generated values are not desired-config drift. Extra pools,
        # allocation ranges, auxiliary addresses or non-default IPAM are.
        require(len(pools) == 1 and set(pools[0]) <= {'Subnet', 'Gateway'} and
                'Subnet' in pools[0] and 'Gateway' in pools[0],
                'Existing CAPI network has unexpected auto-IPAM configuration')
        subnet = ipaddress.ip_network(pools[0]['Subnet'], strict=True)
        gateway = ipaddress.ip_address(pools[0]['Gateway'])
        require(subnet.version == 4 and gateway in subnet, 'Invalid CAPI network auto-IPAM pool')
    if not capi:
        return  # Legitimate tenants may use the existing stack network.
    allowed = {}
    for service in CAPI_SERVICES:
        items = dependencies.get(service, [])
        if items:
            item = container(dependencies, service)
            require(item['Id'] not in allowed, 'Duplicate CAPI endpoint identity')
            allowed[item['Id']] = item
    for identity in (actual.get('Containers') or {}):
        require(identity in allowed, 'Unreviewed container is attached to the CAPI network')
        require(set(allowed[identity]['NetworkSettings']['Networks']) == {name},
                'CAPI endpoint is attached to an unreviewed network')


def volume_check(key, desired, actual):
    name = 'axonos_' + key
    require(desired.get('name') == name and not desired.get('external'), 'Unexpected CAPI volume target')
    require(desired.get('driver', 'local') == 'local' and not desired.get('driver_opts'),
            'Unreviewed CAPI storage driver/options')
    attest_resource_hash(desired, actual, 'volume')
    require(actual.get('Name') == name and actual.get('Driver') == 'local' and actual.get('Scope') == 'local',
            'Existing CAPI volume identity/driver differs')
    owned_labels(actual.get('Labels'), desired, 'volume', key)
    require(not actual.get('Options') and not actual.get('ClusterVolume'),
            'Existing CAPI volume has unreviewed storage options')
    mount = PurePosixPath(actual.get('Mountpoint') or '')
    require(mount.is_absolute() and '..' not in mount.parts and mount.name == '_data' and
            mount.parent.name == name, 'Existing CAPI volume storage metadata differs')


def mount_contract(document, service, item):
    expected = []
    for mount in service.get('volumes', []):
        source = (document['volumes'][mount['source']]['name'] if mount['type'] == 'volume'
                  else mount['source'])
        expected.append((mount['target'], mount['type'], source, not mount.get('read_only', False)))
    actual = [(mount['Destination'], mount['Type'],
               mount.get('Name') if mount['Type'] == 'volume' else mount.get('Source'), mount['RW'])
              for mount in item.get('Mounts', []) if mount['Type'] != 'tmpfs']
    require(sorted(expected) == sorted(actual), 'Preserved dependency mount configuration differs')


def startup_argv(value):
    # `compose config --format json` normalizes shell-form strings (including
    # "") to argv lists. Do not guess at unnormalized or malformed metadata.
    require(value is None or (isinstance(value, list) and all(isinstance(arg, str) for arg in value)),
            'Unsupported dependency startup metadata')
    return [] if value is None else value


def dependency_structure(document, service, item, command, entrypoint):
    desired = document['services'][service]
    require(item['Config'].get('Image') == desired.get('image', 'axonos-' + service),
            'Preserved dependency image configuration differs')
    desired_command = desired.get('command')
    desired_entrypoint = desired.get('entrypoint')
    # Only absent/null means inheritance. Non-null entrypoint suppresses image
    # CMD under the Compose contract; an explicit command still overrides it.
    # Keep [] distinct until inheritance is resolved. If Engine restores image
    # CMD for an explicit-empty override, refuse that mismatch rather than
    # silently accepting startup different from the declared Compose intent.
    expected_command = desired_command
    if desired_command is None:
        expected_command = command if desired_entrypoint is None else []
    expected_entrypoint = entrypoint if desired_entrypoint is None else desired_entrypoint
    require(startup_argv(item['Config']['Cmd']) == startup_argv(expected_command) and
            startup_argv(item['Config']['Entrypoint']) == startup_argv(expected_entrypoint),
            'Preserved dependency startup configuration differs')
    expected_networks = {document['networks'][name]['name'] for name in desired.get('networks', {})}
    require(set(item['NetworkSettings']['Networks']) == expected_networks,
            'Preserved dependency networks differ')
    mount_contract(document, desired, item)
    require(bool(item.get('HostConfig', {}).get('ReadonlyRootfs', False)) == bool(desired.get('read_only', False)) and
            bool(item.get('HostConfig', {}).get('Privileged', False)) == bool(desired.get('privileged', False)) and
            (item['Config'].get('User') or '') == (desired.get('user') or ''),
            'Preserved dependency access configuration differs')


def db_identity(url):
    parsed = urlsplit(url)
    require(parsed.scheme in ('postgres', 'postgresql') and parsed.hostname == 'postgres' and
            (parsed.port or 5432) == 5432 and not parsed.query and not parsed.fragment and
            parsed.username is not None and parsed.password is not None,
            'Core database URL has an unsupported connection/authentication boundary')
    database = unquote(parsed.path[1:])
    require(bool(database) and '/' not in database, 'Core database URL has an unsupported database target')
    return unquote(parsed.username), unquote(parsed.password), database


def preserved_dependencies(document, dependencies):
    gate_env = document['services']['axonos']['environment']
    gate_networks = set(document['services']['axonos'].get('networks', {}))
    require('axonos_control' in gate_networks and gate_networks <= {'axonos_control', 'axonos_stack'},
            'Gate must share the reviewed control network without CAPI database access')
    launcher = container(dependencies, 'axonos-launcher')
    core = container(dependencies, 'postgres')
    dependency_structure(document, 'axonos-launcher', launcher,
                         ['python3', '/app/session_launcher_service.py'], [])
    dependency_structure(document, 'postgres', core, ['postgres'], ['docker-entrypoint.sh'])
    expected_launcher = document['services']['axonos-launcher']['environment']
    actual_launcher = environment(launcher)
    # Inspect both directions, including old nonempty settings absent from the
    # new target. Do not compare unrelated image-default PATH/Python settings.
    prefixes = ('AXGT_', 'AXONOS_', 'WEBRTC_', 'X_CAPI_', 'POSTGRES_')
    keys = {key for key in set(expected_launcher) | set(actual_launcher) if key.startswith(prefixes)}
    require(all(str(expected_launcher.get(key) or '') == str(actual_launcher.get(key) or '') for key in keys),
            'Preserved launcher authentication/configuration differs')
    token = str(gate_env.get('AXGT_SESSION_LAUNCHER_TOKEN') or '').strip()
    require(bool(token) and hmac.compare_digest(token.encode('utf-8'), str(actual_launcher.get('AXGT_SESSION_LAUNCHER_TOKEN') or '').strip().encode('utf-8')),
            'Gate and preserved launcher authentication differs')
    require(gate_env.get('AXGT_SESSION_LAUNCHER_MODE', 'http') == 'http', 'Unsupported launcher connection mode')
    url = urlsplit(str(gate_env.get('AXGT_SESSION_LAUNCHER_URL') or '').rstrip('/'))
    require(url.scheme == 'http' and url.hostname == 'axonos-launcher' and
            (url.port or 80) == int(actual_launcher.get('AXGT_SESSION_LAUNCHER_BIND_PORT', '8090')) and
            not (url.username or url.password or url.query or url.fragment or url.path) and
            actual_launcher.get('AXGT_SESSION_LAUNCHER_BIND_HOST') == '0.0.0.0',
            'Gate and preserved launcher endpoint configuration differs')
    expected_core = document['services']['postgres']['environment']
    actual_core = environment(core)
    keys = {key for key in set(expected_core) | set(actual_core) if key.startswith('POSTGRES_')}
    require(all(str(expected_core.get(key) or '') == str(actual_core.get(key) or '') for key in keys),
            'Preserved core database authentication/configuration differs')
    require(not actual_core.get('POSTGRES_PASSWORD_FILE') and not actual_core.get('POSTGRES_HOST_AUTH_METHOD') and
            bool(actual_core.get('POSTGRES_PASSWORD')), 'Unsupported core database authentication configuration')
    require(actual_core.get('PGDATA', '/var/lib/postgresql/data') == expected_core.get('PGDATA', '/var/lib/postgresql/data'),
            'Preserved core database data directory differs')
    expected_identity = tuple(str(actual_core.get(key) or '') for key in ('POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB'))
    require(all(expected_identity) and db_identity(str(gate_env.get('AXGT_CHALLENGE_DB_URL') or '')) == expected_identity and
            db_identity(str(actual_launcher.get('AXGT_CHALLENGE_DB_URL') or '')) == expected_identity,
            'Gate/launcher and preserved core database credentials differ')


def validate(payload, root):
    document = payload['config']
    require(document.get('name') == 'axonos', 'Unexpected deployment project')
    validate_shared(document, root)
    volumes = by_name(payload['volumes'])
    managed_volumes = {key: value for key, value in document['volumes'].items() if not value.get('external')}
    require(set(managed_volumes) == {*CAPI_VOLUMES, 'axonos_postgres_data'}, 'Unreviewed managed volume set')
    require(set(volumes) == {value['name'] for value in managed_volumes.values()},
            'Existing managed volumes are missing, duplicated or unexpected')
    for key, desired in managed_volumes.items():
        volume_check(key, desired, volumes[desired['name']])
    networks = by_name(payload['networks'])
    managed_networks = {key: value for key, value in document['networks'].items() if not value.get('external')}
    require({'x_capi_db', 'axonos_control'} <= set(managed_networks) <= {'x_capi_db', 'axonos_control', 'axonos_stack'},
            'Unreviewed managed network set')
    require(set(networks) == {value['name'] for value in managed_networks.values()}, 'Existing managed networks missing or unexpected')
    for key, desired in managed_networks.items():
        network_check(document, networks[desired['name']], payload['dependencies'], key)
    preserved_dependencies(document, payload['dependencies'])


def main():
    try:
        require(len(sys.argv) == 2, 'Invalid resource-check invocation')
        validate(json.load(sys.stdin), sys.argv[1])
    except Refusal as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        return 1
    except Exception:
        # JSON, URL, metadata and dependency errors may contain secrets. Never
        # emit exception representations, offending inputs or a traceback.
        print('ERROR: Cannot attest resource/dependency configuration', file=sys.stderr)
        return 1
    print('Resource and dependency contracts verified')
    return 0


if __name__ == '__main__':
    sys.exit(main())
