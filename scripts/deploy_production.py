#!/usr/bin/env python3
"""Bounded secret-safe deployment control flow; invoked by the Bash entrypoint.

Existing deployment labels authorize the checkout. A root-provisioned host-wide
lock serializes participating operators. Command diagnostics are never replayed.
"""

import argparse
import contextlib
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

import deploy_production_checks as checks
import deploy_production_resources as resources

LOCK_FILE = '/run/lock/axonos-production-deploy.lock'
LOCK_UID = 0
COMMAND_TIMEOUT = 30
KILL_GRACE = 5
VALIDATOR_TIMEOUT = 300
BUILD_TIMEOUT = 21600
DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']
SERVICES = ('postgres', 'axonos-launcher', 'axonos', 'x-capi-postgres',
            'x-capi-db-init', 'x-capi-privacy-init', 'x-capi-worker')
LABEL = 'com.axonos.deploy.validator'
CANDIDATE_PREFIX = 'axonos-deploy-candidate:'
CANDIDATE_OWNER = 'com.axonos.deploy.candidate-owner'
CANDIDATE_RUN = 'com.axonos.deploy.candidate-run'
CANDIDATE_CREATED = 'com.axonos.deploy.candidate-created-ns'
CANDIDATE_TOOL = 'axonos-production-deploy-v1'
CANDIDATE_KEEP = 3
CANDIDATE_SCAN_LIMIT = 32
CANDIDATE_CLEANUP_TIMEOUT = 60
# Reference enumeration/deletion semantics verified in these upstream releases.
# Do not widen to a version range: RepoTags' canonical-name behavior is not an
# Engine API guarantee. See the versioned Moby sources in the runbook.
CANDIDATE_ENGINE_VERSIONS = {'28.5.2', '29.5.1', '29.5.2'}
DOCKER_API_MIN = (1, 48)
DOCKER_API_MAX = {'28.5.2': (1, 51), '29.5.1': (1, 54), '29.5.2': (1, 54)}
Refusal = checks.Refusal
require = checks.require


def docker_api_contract(info, version, override):
    """Validate the CLI's effective API, not just the daemon's maximum API.

    CLI Client.ApiVersion reflects negotiation/DOCKER_API_VERSION handling.
    Never unset/replace an override to pass admission, or assume every CLI
    release negotiates an override identically: require an exact effective match.
    An older CLI forced above its own supported API is not sufficient evidence.
    Actual inspection capabilities are also probed before any image build.
    """
    def api(value):
        require(isinstance(value, str) and re.fullmatch(r'1\.[1-9][0-9]{1,2}', value),
                'Missing or unsupported Docker API version metadata')
        return tuple(int(part) for part in value.split('.'))

    require(isinstance(version, dict) and isinstance(version.get('Client'), dict) and
            isinstance(version.get('Server'), dict), 'Missing Docker client/server version metadata')
    client, server = version['Client'], version['Server']
    effective = api(client.get('ApiVersion'))
    client_max = api(client.get('DefaultAPIVersion'))
    server_max = api(server.get('ApiVersion'))
    server_min = api(server.get('MinAPIVersion'))
    require(server.get('Version') == info.get('ServerVersion') and server.get('Os') == 'linux'
            and server_max == DOCKER_API_MAX.get(server.get('Version')),
            'Unreviewed or inconsistent Docker server API contract')
    require(DOCKER_API_MIN <= effective <= server_max and server_min <= effective <= client_max,
            'Effective Docker API must be at least 1.48 and supported by both client and server')
    if override:
        require(api(override) == effective, 'DOCKER_API_VERSION differs from the effective CLI API')
    return client['ApiVersion']


def candidate_store(info):
    """Fail before building on unreviewed image-store/reference semantics."""
    require(isinstance(info, dict) and info.get('OSType') == 'linux' and
            info.get('ServerVersion') in CANDIDATE_ENGINE_VERSIONS,
            'Candidate retention requires a reviewed Linux Docker Engine version')
    status = info.get('DriverStatus')
    require(isinstance(status, list) and all(isinstance(row, list) and len(row) == 2 and
            all(isinstance(value, str) for value in row) for row in status),
            'Ambiguous Docker image-store status')
    types = [value for key, value in status if key == 'driver-type']
    if info.get('Driver') == 'overlayfs' and types == ['io.containerd.snapshotter.v1']:
        return 'containerd'
    require(info.get('Driver') == 'overlay2' and not types,
            'Unsupported Docker image-store mode for candidate retention')
    return 'classic'


def candidate_reference(value):
    """Return (normalized repository, tag, digest) for a conservative subset.

    Match distribution/reference's Docker Hub normalization. Unsupported names
    (including combined tag@digest, IPv6 registries and non-sha256 digests) are
    refused, never treated as evidence that an image has an unrelated anchor.
    """
    require(isinstance(value, str) and len(value) <= 512, 'Invalid image reference')
    match = re.fullmatch(r'([^@]+)(?:@(sha256:[a-f0-9]{64}))?', value)
    require(match is not None, 'Unsupported image reference')
    name, digest = match.groups()
    tag = None
    if ':' in name.rsplit('/', 1)[-1]:
        name, tag = name.rsplit(':', 1)
        require(not digest and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag),
                'Unsupported tagged image reference')
    require(tag or digest, 'Unqualified image reference')
    first, separator, rest = name.partition('/')
    if separator and ('.' in first or ':' in first or first == 'localhost'):
        domain, path = first, rest
        require(re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?', domain),
                'Unsupported image registry')
        if domain == 'index.docker.io':
            domain = 'docker.io'
    else:
        domain, path = 'docker.io', name
    require(len(path) <= 255 and all(re.fullmatch(r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*', part)
            for part in path.split('/')), 'Unsupported image repository')
    if domain == 'docker.io' and '/' not in path:
        path = 'library/' + path
    return domain + '/' + path, tag, digest


def candidate_references(item, store):
    """Separate actual tag/canonical records from containerd-derived digests.

    In the reviewed containerd engines EVERY actual reference is in RepoTags,
    even a canonical digest name. RepoDigests additionally synthesizes repo@ID
    for each ordinary tag. On classic engines all RepoDigests are real records.
    Check the complete relation; digest == Id alone proves nothing about intent.
    """
    fields = []
    for key in ('RepoTags', 'RepoDigests'):
        values = item.get(key)
        require(isinstance(values, list) and len(values) <= 256, 'Ambiguous image reference inventory')
        fields.append({candidate_reference(value) for value in values})
    names, digests = fields
    require(all(digest and not tag for _, tag, digest in digests), 'Invalid RepoDigests inventory')
    tags = {ref for ref in names if ref[1] is not None}
    canonical = names - tags
    graph = item.get('GraphDriver')
    if store == 'containerd':
        descriptor = item.get('Descriptor') or {}
        require(descriptor.get('digest') == item['Id'] and descriptor.get('mediaType') in {
            'application/vnd.oci.image.index.v1+json', 'application/vnd.oci.image.manifest.v1+json',
            'application/vnd.docker.distribution.manifest.list.v2+json',
            'application/vnd.docker.distribution.manifest.v2+json'}, 'Ambiguous containerd image descriptor')
        # GraphDriver is omitted by Engine 29's API >= 1.52 for snapshotters.
        require(graph is None or graph.get('Name') == 'overlayfs', 'Inconsistent containerd image metadata')
        derived = {(repo, None, item['Id']) for repo, _, _ in tags}
        require(digests == canonical | derived, 'Unexplained containerd digest metadata')
    else:
        require(not canonical and isinstance(graph, dict) and graph.get('Name') == 'overlay2'
                and not item.get('Descriptor'), 'Inconsistent classic image metadata')
        canonical = digests
    return tags, canonical


def note(message):
    print(message, flush=True)


def checked_output(function, *args):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        function(*args)
    return output.getvalue().strip()


@contextlib.contextmanager
def defer_interruptions(*, abort_after=True):
    """Record INT/TERM only inside a bounded critical cleanup/launch window.

    Blocking while swapping handlers avoids a half-installed handler pair.
    Restore the caller's mask/handlers, including for nested cleanup windows.
    Never replay a deferred exception into the middle of TERM -> KILL.
    """
    signals = {signal.SIGINT, signal.SIGTERM}
    received = []
    previous = {}
    def record(signum, _frame):
        received.append(signum)
    record._deploy_deferred = True
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        for number in signals:
            previous[number] = signal.signal(number, record)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    completed = False
    try:
        yield
        completed = True
    finally:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        try:
            for number, handler in previous.items():
                signal.signal(number, handler)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        if received:
            outer = all(getattr(previous[number], '_deploy_deferred', False) for number in received)
            if outer:
                for number in received:
                    previous[number](number, None)
            else:
                note('Interruption requested; protected operation completed before returning control.')
            if completed and abort_after and not outer:
                raise Refusal('Interruption requested; cleanup completed')


def terminate_group(process):
    # Kill the complete private process group, including TERM-resistant children
    # which can outlive their leader and retain its stdout handles.
    with defer_interruptions(abort_after=False):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=KILL_GRACE)


def run(command, env, root, *, timeout=None, input_text=None, capture=False, label='Command'):
    """No TTY/prompt input; bounded TERM+KILL. Never print argv/output/errors."""
    process = None
    try:
        # Register the child before a launch-time signal can unwind this frame.
        with defer_interruptions():
            process = subprocess.Popen(command, cwd=root, env=env, start_new_session=True,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, text=True, close_fds=True)
        output, _ = process.communicate(input=input_text, timeout=COMMAND_TIMEOUT if timeout is None else timeout)
    except BaseException:
        if process is not None:
            terminate_group(process)
        raise Refusal(label + ' interrupted/timed out; process group terminated') from None
    require(process.returncode == 0, label + ' failed (output withheld)')
    return (output or '').strip()


@contextlib.contextmanager
def deployment_lock():
    path = Path(LOCK_FILE)
    require(str(path.resolve()) == str(path), 'Deployment lock path must be canonical')
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid == LOCK_UID and before.st_gid == LOCK_UID
            and stat.S_IMODE(before.st_mode) == 0o444 and before.st_nlink == 1,
            'Deployment lock must be root-owned, single-link, mode 0444; provision it using the runbook')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        require((before.st_dev, before.st_ino, before.st_uid, before.st_mode) ==
                (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode), 'Deployment lock changed')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal('Another deployment holds the shared Docker/project lock') from None
        yield
    finally:
        os.close(descriptor)


class Deployment:
    def __init__(self, options):
        self.options = options
        self.root = Path(__file__).resolve().parents[1]
        self.env = dict(os.environ)
        require(self.env.get('DOCKER_HOST', '') in ('', 'unix:///var/run/docker.sock'), 'Alternate DOCKER_HOST is unsupported')
        require(self.env.get('DOCKER_CONTEXT', '') in ('', 'default'), 'Alternate DOCKER_CONTEXT is unsupported')
        for key in list(self.env):
            if key.startswith('COMPOSE_') or key in ('DOCKER_HOST', 'DOCKER_CONTEXT'):
                del self.env[key]
        # These explicit values override BOTH shell and .env. Never use --yes.
        self.env.update(COMPOSE_REMOVE_ORPHANS='false', COMPOSE_IGNORE_ORPHANS='true',
                        COMPOSE_PROFILES='', PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0')
        self.compose = [*DOCKER, 'compose', '--project-directory', str(self.root), '--project-name', 'axonos',
                        '--env-file', str(self.root / '.env'), '-f', str(self.root / 'docker-compose.yml'),
                        '-f', str(self.root / 'docker-compose.x-capi.yml'), '--profile', 'x-capi']
        self.run_id = secrets.token_hex(12)
        self.validators = {}
        self.worker_stopped = False
        self.persistent_started = False
        self.old_worker = None
        self.image_id = None
        self.candidate = None
        self.deployed = False

    def command(self, args, **kwargs):
        return run(args, self.env, self.root, **kwargs)

    def docker(self, *args, **kwargs):
        return self.command([*DOCKER, *args], **kwargs)

    def compose_run(self, *args, override=None, **kwargs):
        return self.command([*self.compose, *(['-f', str(override)] if override else []), *args], **kwargs)

    def git(self, *args):
        return self.command(['git', *args], capture=True, label='Git preflight')

    def clean_git(self):
        require(self.git('rev-parse', '--show-toplevel') == str(self.root), 'Script must belong to checkout root')
        require(not self.git('status', '--porcelain=v1', '--untracked-files=all'), 'Dirty Git tree; no override is supported')

    def inspect_service(self, service):
        identifier = self.compose_run('ps', '-a', '-q', service, capture=True, label='Service discovery')
        require(re.fullmatch(r'[a-f0-9]{12,64}', identifier), 'Missing/ambiguous service: ' + service)
        document = json.loads(self.docker('inspect', identifier, capture=True, label='Service metadata'))
        checks.inspected(document, service)
        require(document[0]['Id'] == identifier, 'Service identity mismatch')
        return document

    def require_healthy(self, service):
        document = self.inspect_service(service)
        require(checked_output(checks.state_check, document, service) == 'healthy', service + ' must already be healthy')
        return document

    def wait(self, service, wanted):
        deadline = time.monotonic() + self.options.health_timeout
        while True:
            observed = checked_output(checks.state_check, self.inspect_service(service), service)
            if observed == wanted:
                note(service + ': ' + observed)
                return
            pending = ('created', 'starting', 'no-healthcheck') if wanted == 'exited:0' else ('created', 'starting')
            require(observed in pending, service + ' failed health/init wait: ' + observed)
            require(time.monotonic() < deadline, service + ' health/init timeout')
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def provenance(self, dependencies):
        for service in ('axonos', 'axonos-launcher', 'postgres'):
            item = checks.inspected(dependencies[service], service)
            labels = item['Config']['Labels']
            require(labels.get('com.docker.compose.project.working_dir') == str(self.root),
                    'Unauthorized checkout: existing deployment provenance differs')
            files = labels.get('com.docker.compose.project.config_files', '').split(',')
            require(str(self.root / 'docker-compose.yml') in files, 'Deployment Compose provenance is missing')
            if service == 'axonos':
                require(str(self.root / 'docker-compose.x-capi.yml') in files, 'Existing gate lacks CAPI overlay provenance')

    def resource_check(self):
        dependencies = {service: self.inspect_service(service) for service in SERVICES}
        self.provenance(dependencies)
        networks = [value['name'] for value in self.config['networks'].values() if not value.get('external')]
        volumes = [value['name'] for value in self.config['volumes'].values() if not value.get('external')]
        payload = {'config': self.config, 'dependencies': dependencies,
            'networks': json.loads(self.docker('network', 'inspect', *networks, capture=True, label='Managed network inspection')),
            'volumes': json.loads(self.docker('volume', 'inspect', *volumes, capture=True, label='Managed volume inspection'))}
        # Require the API 1.48 field rather than letting the resource check's
        # default mask an incompatible client response, even if version text
        # looks new. Older client representations may omit this field.
        require(isinstance(payload['networks'], list) and all(isinstance(item, dict) and
                type(item.get('EnableIPv4')) is bool for item in payload['networks']),
                'Docker network inspection lacks required API 1.48 metadata')
        resources.validate(payload, self.root)
        self.docker('exec', '-i', dependencies['axonos'][0]['Id'], '/usr/bin/python3', '-',
            input_text=(self.root / 'scripts/deploy_production_mount_check.py').read_text(), label='Existing CAPI runtime metadata')
        metadata = self.docker('exec', dependencies['x-capi-postgres'][0]['Id'], 'stat', '-c', '%u:%g:%a:%F',
            '/var/lib/postgresql/data', capture=True, label='Dedicated database directory metadata')
        require(metadata == '70:70:700:directory', 'Dedicated database directory ownership/permissions differ')
        return dependencies

    def active_sessions(self):
        text = self.docker('ps', '--format', '{{json .}}', capture=True, label='Active session inspection')
        active = checked_output(checks.sessions, text)
        if active:
            note('Active tenant sessions:\n' + active)
            require(self.options.allow_active_sessions, 'Active tenants found; no mutation permitted without --allow-active-sessions')
            note('WARNING: --allow-active-sessions ACCEPTED; viewers/control traffic may be interrupted.')
        else:
            note('Active tenant sessions: none')

    def load_config(self):
        config = json.loads(self.compose_run('config', '--format', 'json', capture=True, label='Combined Compose configuration'))
        fingerprint = checked_output(checks.config_check, config, self.root, self.mode, self.secret_snapshot())
        resources.validate_shared(config, self.root)
        return config, fingerprint

    def secret_snapshot(self):
        checks.metadata_helper_contract()
        try:
            # No operator-controlled helper, arguments, Python import paths,
            # sudo prompts or secret contents. Installation is admin-only.
            text = run(list(checks.SECRET_METADATA_COMMAND),
                       {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}, self.root,
                       capture=True, label='Privileged secret metadata attestation')
        except Refusal:
            raise Refusal('Secret metadata cannot be attested with current privileges; verify reviewed helper provisioning') from None
        return checks.parse_secret_snapshot(text)

    def probe_image_api(self, identifier, store):
        # The existing gate is already mandatory. Inspect its immutable image,
        # never pull/build a probe image or rely on a candidate already existing.
        require(isinstance(identifier, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', identifier),
                'Existing gate has no immutable image identity for API admission')
        items = json.loads(self.docker('image', 'inspect', identifier, capture=True,
                                     label='Read-only Docker image API capability probe'))
        require(isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict)
                and items[0].get('Id') == identifier and isinstance(items[0].get('Config'), dict),
                'Docker image inspection lacks required identity/config metadata')
        labels = items[0]['Config'].get('Labels')
        require(labels is None or (isinstance(labels, dict) and
                all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items())),
                'Docker image inspection has unsupported label metadata')
        candidate_references(items[0], store)

    def preflight(self):
        for tool in ('docker', 'git', 'curl'):
            require(shutil.which(tool, path=self.env['PATH']), 'Required command unavailable: ' + tool)
        for filename in ('docker-compose.yml', 'docker-compose.x-capi.yml', '.env'):
            path = self.root / filename
            require(path.is_file() and not path.is_symlink(), 'Missing or symlinked required file: ' + filename)
        self.clean_git()
        self.branch = self.git('symbolic-ref', '--quiet', '--short', 'HEAD')
        self.head = self.git('rev-parse', '--verify', 'HEAD')
        self.mode = checked_output(checks.env_mode, self.root / '.env')
        info = json.loads(self.docker('info', '--format', '{{json .}}', capture=True, label='Local Docker image store'))
        store = candidate_store(info)
        version = json.loads(self.docker('version', '--format', '{{json .}}', capture=True, label='Effective Docker API'))
        effective_api = docker_api_contract(info, version, self.env.get('DOCKER_API_VERSION', ''))
        self.compose_run('version', label='Docker Compose')
        self.docker('buildx', 'version', label='Docker Buildx')
        self.compose_run('config', '--quiet', label='Combined Compose config --quiet')
        self.config, self.fingerprint = self.load_config()
        self.db_contract = checked_output(checks.configured_database, self.config)
        self.worker_contract = checked_output(checks.configured_worker, self.config)
        dependencies = self.resource_check()
        self.probe_image_api(dependencies['axonos'][0].get('Image'), store)
        note('Docker API ' + effective_api + ': read-only image/resource capability checks passed.')
        for service in ('axonos', 'postgres', 'axonos-launcher', 'x-capi-postgres', 'x-capi-worker'):
            require(checked_output(checks.state_check, dependencies[service], service) == 'healthy', service + ' must already be healthy')
        checks.database_check(dependencies['x-capi-postgres'], self.db_contract)
        self.database_id = dependencies['x-capi-postgres'][0]['Id']
        self.old_worker = dependencies['x-capi-worker'][0]
        if not self.options.with_capi_backend:
            checks.worker_check(dependencies['x-capi-worker'], self.mode, self.worker_contract)
            for service in ('x-capi-db-init', 'x-capi-privacy-init'):
                require(checked_output(checks.state_check, dependencies[service], service) == 'exited:0', service + ' must have completed successfully')
        self.active_sessions()
        note(f'Deployment summary: branch={self.branch} commit={self.head} CAPI={self.mode} backend={self.options.with_capi_backend}')
        note('Target: local Docker/project axonos, authorized checkout; shared project lock held.')
        note('Build private full candidate; validate immutable ID; promote afterward; roll out pinned ID.')
        note('Launcher/core DB are preserved. Admissions still require an operator maintenance window.')

    def recheck(self):
        self.clean_git()
        require(self.git('rev-parse', '--verify', 'HEAD') == self.head
                and self.git('symbolic-ref', '--quiet', '--short', 'HEAD') == self.branch, 'Git revision changed during deployment')
        require(checked_output(checks.env_mode, self.root / '.env') == self.mode, '.env mode changed')
        _, fingerprint = self.load_config()
        require(fingerprint == self.fingerprint, 'Configuration/secret metadata changed during deployment')
        dependencies = self.resource_check()
        checks.database_check(dependencies['x-capi-postgres'], self.db_contract)
        require(dependencies['x-capi-postgres'][0]['Id'] == self.database_id,
                'Dedicated database identity changed during deployment')
        for service in ('postgres', 'axonos-launcher', 'x-capi-postgres'):
            require(checked_output(checks.state_check, dependencies[service], service) == 'healthy', service + ' readiness changed')
        self.active_sessions()
        return dependencies

    def build_candidate(self):
        note('Build private full candidate (output suppressed to protect secrets)')
        bake = json.loads(self.compose_run('build', '--print', '--build-arg', 'AXONOS_SKIP_HEAVY=0', 'axonos', capture=True, label='Compose build plan'))
        target = copy.deepcopy(bake['target']['axonos'])
        require((self.root / target['context']).resolve() == self.root and target.get('dockerfile', 'Dockerfile') == 'Dockerfile', 'Unexpected Bake build context')
        require(not target.get('inherits') and not target.get('contexts'), 'Unreviewed Bake target dependencies')
        self.candidate = CANDIDATE_PREFIX + self.run_id
        target['tags'] = [self.candidate]
        target['output'] = ['type=docker']
        target.setdefault('args', {})['AXONOS_SKIP_HEAVY'] = '0'
        target.setdefault('labels', {}).update({CANDIDATE_OWNER: CANDIDATE_TOOL,
            CANDIDATE_RUN: self.run_id, CANDIDATE_CREATED: str(time.time_ns())})
        plan = {'target': {'axonos': target}, 'group': {'default': {'targets': ['axonos']}}}
        self.docker('buildx', 'bake', '--file', '-', '--load', 'axonos', input_text=json.dumps(plan), timeout=BUILD_TIMEOUT, label='Build full private candidate')
        self.image_id = self.docker('image', 'inspect', self.candidate, '--format', '{{.Id}}', capture=True, label='Candidate image identity')
        require(re.fullmatch(r'sha256:[a-f0-9]{64}', self.image_id), 'Invalid immutable candidate image ID')

    def maintain_candidates(self, *, strict):
        """Bounded tag housekeeping, never image-ID deletion/force/prune.

        Pre-build admission refuses to add another candidate if a prior cleanup
        left more than the retention limit. Terminal housekeeping is best effort
        and cannot turn an already healthy deployment into a failed deployment.
        """
        deadline = time.monotonic() + CANDIDATE_CLEANUP_TIMEOUT
        def command(*args):
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'Candidate housekeeping deadline exceeded')
            return self.docker(*args, capture=True, timeout=min(15, remaining), label='Candidate housekeeping')

        def inspect(tag):
            require(re.fullmatch(re.escape(CANDIDATE_PREFIX) + r'[a-f0-9]{24}', tag), 'Invalid candidate tag')
            documents = json.loads(command('image', 'inspect', tag))
            require(isinstance(documents, list) and len(documents) == 1, 'Ambiguous candidate image metadata')
            item = documents[0]
            labels = (item.get('Config') or {}).get('Labels') or {}
            owned = (labels.get(CANDIDATE_OWNER) == CANDIDATE_TOOL and
                     labels.get(CANDIDATE_RUN) == tag[len(CANDIDATE_PREFIX):])
            if not owned:
                return None
            require(re.fullmatch(r'sha256:[a-f0-9]{64}', item['Id']) and
                    tag in (item.get('RepoTags') or []) and
                    re.fullmatch(r'[0-9]{1,20}', labels.get(CANDIDATE_CREATED, '')) and
                    int(labels[CANDIDATE_CREATED]) > 0, 'Invalid owned candidate metadata')
            candidate_references(item, store)
            return item

        def remove(tag, original):
            current = inspect(tag)  # Reattest immediately before any untagging.
            require(current is not None and current['Id'] == original['Id'] and
                    current['Config']['Labels'] == original['Config']['Labels'], 'Candidate identity changed; removal refused')
            tags, canonical = candidate_references(current, store)
            target = candidate_reference(tag)
            other_tags = tags - {target}
            # Both reviewed stores drop canonical records with the LAST tag in
            # that repository, even if a different repo keeps the image alive.
            if any(ref[0] == target[0] for ref in canonical) and not any(ref[0] == target[0] for ref in other_tags):
                note('WARNING: candidate protects a same-repository digest reference; retained for operator review.')
                return False
            if not other_tags and not canonical:
                # No surviving stored reference: protect even stopped containers.
                # Synthesized containerd RepoDigests are NOT stored references.
                # No-force Docker removal is a second, engine-enforced guard.
                if command('ps', '-a', '-q', '--no-trunc', '--filter', 'ancestor=' + current['Id']):
                    note('WARNING: candidate is the sole reference of a container image; retained for operator review.')
                    return False
            command('image', 'rm', '--no-prune', tag)
            return True

        try:
            # Always attest, even for an empty inventory: unsupported semantics
            # must be discovered before creating a candidate, not during cleanup.
            store = candidate_store(json.loads(command('info', '--format', '{{json .}}')))
            raw = command('image', 'ls', '--filter', 'reference=' + CANDIDATE_PREFIX + '*',
                          '--format', '{{.Repository}}:{{.Tag}}')
            tags = sorted(set(raw.splitlines())) if raw else []
            require(len(tags) <= CANDIDATE_SCAN_LIMIT, 'Candidate inventory exceeds bounded scan limit; manual review required')
            owned = {}
            unknown = False
            for tag in tags:
                if not re.fullmatch(re.escape(CANDIDATE_PREFIX) + r'[a-f0-9]{24}', tag):
                    unknown = True
                    continue
                item = inspect(tag)
                if item is None:
                    unknown = True
                    continue
                owned[tag] = item
            if unknown:
                note('WARNING: unowned/legacy candidate tags were left untouched; manual review required.')
            if self.deployed and self.candidate:
                current = owned.get(self.candidate)
                require(current is not None and current['Id'] == self.image_id, 'Successful candidate ownership/identity unavailable')
                if remove(self.candidate, current):
                    del owned[self.candidate]
                    note('Removed this run\'s temporary candidate tag; production/retention references preserved.')
            order = sorted(owned, key=lambda tag: (int(owned[tag]['Config']['Labels'][CANDIDATE_CREATED]), tag), reverse=True)
            for tag in order[CANDIDATE_KEEP:]:
                if remove(tag, owned[tag]):
                    del owned[tag]
            require(len(owned) <= CANDIDATE_KEEP, 'Candidate retention quota cannot be met safely')
            if not strict and not self.deployed and self.candidate in owned:
                note('Failed/interrupted candidate retained within the newest-three candidate policy.')
        except Exception:
            note('WARNING: candidate-tag housekeeping incomplete; no broad cleanup attempted. Review candidate retention before another build.')
            if strict:
                raise Refusal('Candidate retention could not be attested before build') from None

    def cleanup_validator(self, name):
        with defer_interruptions():
            self._cleanup_validator(name)

    def _cleanup_validator(self, name):
        # create may have succeeded even if the client timed out before its ID.
        try:
            raw = self.docker('container', 'ls', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}', capture=True, label='Validator cleanup discovery')
            if not raw:
                self.validators.pop(name, None)
                return
            require(re.fullmatch(r'[a-f0-9]{12,64}', raw), 'Ambiguous validator cleanup identity')
            item = json.loads(self.docker('inspect', raw, capture=True, label='Validator cleanup inspection'))[0]
            require(item.get('Name') == '/' + name and item['Config'].get('Labels', {}).get(LABEL) == self.run_id
                    and item['Image'] == self.image_id, 'Refusing cleanup of unowned container')
            self.docker('rm', '--force', item['Id'], timeout=15, label='Bounded validator removal')
            self.validators.pop(name, None)
        except Exception:
            raise Refusal('Validator cleanup failed; inspect only this run-owned validator: ' + name) from None

    def validate_image(self, kind, arguments):
        name = 'axonos-deploy-validator-' + self.run_id + '-' + kind.lower()
        self.validators[name] = True
        note(kind + ' image validation (immutable candidate, no network/GPU/production mounts)')
        try:
            identifier = self.docker('create', '--name', name, '--label', LABEL + '=' + self.run_id,
                '--pull', 'never', '--runtime', 'runc', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m',
                '--env', 'PYTHONDONTWRITEBYTECODE=1', '--entrypoint', '/usr/bin/python3', self.image_id,
                *arguments, capture=True, label=kind + ' validator creation')
            require(re.fullmatch(r'[a-f0-9]{12,64}', identifier), 'Invalid validator identity')
            self.docker('start', '--attach', identifier, timeout=VALIDATOR_TIMEOUT, label=kind + ' image validation')
            item = json.loads(self.docker('inspect', identifier, capture=True, label='Validator exit status'))[0]
            require(item['State']['Status'] == 'exited' and item['State']['ExitCode'] == 0, kind + ' image validation failed')
        finally:
            self.cleanup_validator(name)

    def backend(self):
        note('Build/fetch and prepare backend while the original worker stays healthy')
        self.compose_run('build', 'x-capi-privacy-init', 'x-capi-worker', timeout=BUILD_TIMEOUT, label='CAPI image build')
        self.compose_run('pull', 'x-capi-postgres', 'x-capi-db-init', timeout=600, label='Pinned CAPI image fetch')
        self.recheck()
        self.compose_run('up', '--no-start', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate',
            'x-capi-db-init', 'x-capi-privacy-init', timeout=120, label='Prepare CAPI initializers without starting')
        self.recheck()
        initializers = {name: self.inspect_service(name)[0]['Id']
                        for name in ('x-capi-db-init', 'x-capi-privacy-init')}
        require(self.require_healthy('x-capi-worker')[0]['Id'] == self.old_worker['Id'], 'Worker identity changed before stop')
        self.worker_stopped = True
        self.docker('stop', '--time', '60', self.old_worker['Id'], timeout=90, label='Stop original CAPI worker')
        self.recheck()
        note('PERSISTENT BACKEND MUTATION BOUNDARY: initialization may now change DB/privacy state; no rollback afterward.')
        self.persistent_started = True  # BEFORE issuing an ambiguously failing start.
        self.compose_run('up', '-d', '--no-build', '--pull', 'never', '--no-recreate', 'x-capi-db-init',
            'x-capi-privacy-init', timeout=self.options.health_timeout + 30, label='Run CAPI initialization')
        self.wait('x-capi-db-init', 'exited:0')
        self.wait('x-capi-privacy-init', 'exited:0')
        self.verify_initializers(initializers)
        self.compose_run('up', '--no-start', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate',
            'x-capi-worker', timeout=120, label='Prepare replacement CAPI worker')
        self.recheck()
        self.verify_initializers(initializers)
        # Dependencies were executed and verified in THIS attempt. Asking
        # Compose to process them again restarts completed one-shot services.
        self.compose_run('up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--no-recreate', 'x-capi-worker',
            timeout=self.options.health_timeout + 30, label='Start CAPI after verified initialization')
        self.wait('x-capi-worker', 'healthy')
        self.wait('x-capi-db-init', 'exited:0')
        self.wait('x-capi-privacy-init', 'exited:0')
        checks.worker_check(self.inspect_service('x-capi-worker'), self.mode, self.worker_contract)

    def verify_initializers(self, expected):
        require(self.persistent_started and len(expected) == 2, 'Current-run initialization is not established')
        for name, identifier in expected.items():
            document = self.inspect_service(name)
            require(document[0]['Id'] == identifier and
                    checked_output(checks.state_check, document, name) == 'exited:0',
                    'Current-run initializer identity/completion changed')

    def restore_worker_before_boundary(self):
        if not self.worker_stopped or self.persistent_started:
            return
        note('Initialization was NOT started; attempting bounded restoration of the original worker only.')
        current = self.inspect_service('x-capi-worker')[0]
        require(current['Id'] == self.old_worker['Id'] and current['Image'] == self.old_worker['Image']
                and current['Config'] == self.old_worker['Config'] and current['Mounts'] == self.old_worker['Mounts'],
                'Original worker identity/config changed; automatic restoration refused')
        _, fingerprint = self.load_config()
        require(fingerprint == self.fingerprint, 'Secret/configuration metadata changed; original-worker restoration refused')
        dependencies = self.resource_check()
        require(dependencies['x-capi-postgres'][0]['Id'] == self.database_id,
                'Dedicated database identity changed; original-worker restoration refused')
        checks.database_check(dependencies['x-capi-postgres'], self.db_contract)
        require(checked_output(checks.state_check, dependencies['x-capi-postgres'], 'x-capi-postgres') == 'healthy',
                'Dedicated database is not ready; original-worker restoration refused')
        self.docker('start', self.old_worker['Id'], timeout=30, label='Restore unchanged original worker')
        self.wait('x-capi-worker', 'healthy')
        note('Original worker restored; deployment still FAILED. No gate rollout occurred.')

    def rollout(self):
        dependencies = self.recheck()
        checks.worker_check(dependencies['x-capi-worker'], self.mode, self.worker_contract)
        self.require_healthy('x-capi-worker')
        # Only a public ID goes on disk, never resolved config or credentials.
        with tempfile.TemporaryDirectory(prefix='axonos-immutable-rollout-') as directory:
            override = Path(directory) / 'image.json'
            override.write_text(json.dumps({'services': {'axonos': {'image': self.image_id}}}))
            self.compose_run('config', '--quiet', override=override, label='Immutable rollout configuration')
            note('Promote validated ID to shared tag; deploy that SAME ID, never resolve the tag for rollout.')
            self.docker('image', 'tag', self.image_id, 'axonos:latest', label='Validated image promotion')
            self.compose_run('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'axonos',
                override=override, timeout=120, label='Roll out immutable central gate')
        self.wait('axonos', 'healthy')
        document = self.inspect_service('axonos')
        checked_output(checks.gate_check, document, self.root, self.mode, self.image_id, self.secret_snapshot())
        self.docker('exec', '-i', document[0]['Id'], '/usr/bin/python3', '-',
            input_text=(self.root / 'scripts/deploy_production_mount_check.py').read_text(), label='CAPI mount postflight')
        for endpoint in ('http://127.0.0.1:6080/vnc.html', 'http://127.0.0.1:8889/'):
            code = self.command(['curl', '--disable', '--noproxy', '*', '--silent', '--output', '/dev/null',
                '--write-out', '%{http_code}', '--connect-timeout', '3', '--max-time', '10', endpoint],
                timeout=15, capture=True, label='Local HTTP postflight')
            require(re.fullmatch(r'[0-9]{3}', code), 'Invalid HTTP status')
            note(endpoint + ': HTTP ' + code)
            require(code == '200', 'Local HTTP postflight failed')
        checks.worker_check(self.require_healthy('x-capi-worker'), self.mode, self.worker_contract)
        require(self.docker('image', 'inspect', 'axonos:latest', '--format', '{{.Id}}', capture=True) == self.image_id,
            'Shared tag changed concurrently; pinned gate is safe but tenant image selection needs operator review')
        self.deployed = True
        note('DEPLOYMENT PASSED: immutable image, health, configuration, mounts and HTTP postflight verified.')

    def execute(self):
        with deployment_lock():
            try:
                self.preflight()
                if self.options.check:
                    note('CHECK PASSED: read-only Docker/metadata checks; no build, service mutation or initialization.')
                    return
                self.maintain_candidates(strict=True)
                self.build_candidate()
                self.validate_image('Scientific', ['/opt/axonos-build/check_scientific_python.py', '/opt/axonos-build/scientific-python.txt'])
                self.validate_image('NVIDIA', ['/usr/local/bin/plan-nvidia-userspace.py', 'verify', '--manifest', '/usr/local/share/axonos/nvidia-userspace.txt'])
                if self.options.with_capi_backend:
                    self.backend()
                self.rollout()
            except BaseException:
                with defer_interruptions(abort_after=False):
                    try:
                        self.restore_worker_before_boundary()
                    except Exception:
                        note('ERROR: original-worker restoration failed/refused; inspect backend state manually.')
                raise
            finally:
                with defer_interruptions(abort_after=False):
                    try:
                        for name in list(self.validators):
                            self.cleanup_validator(name)
                    finally:
                        if self.candidate is not None:
                            self.maintain_candidates(strict=False)


def main():
    parser = argparse.ArgumentParser(description='Safe production deployment; --check is NOT CAPI dry_run.')
    parser.add_argument('--check', action='store_true', help='Read-only preflight including metadata-only exec checks')
    parser.add_argument('--with-capi-backend', action='store_true', help='Deliberately update the existing CAPI backend')
    parser.add_argument('--allow-active-sessions', action='store_true', help='Explicitly accept tenant interruption')
    parser.add_argument('--health-timeout', type=int, default=300, help='Health/init wait seconds (1..1800)')
    args = parser.parse_args()
    require(1 <= args.health_timeout <= 1800, 'Health timeout must be 1..1800 seconds')
    os.umask(0o077)
    def interrupted(_signum, _frame):
        os.write(2, b'Interruption requested; completing bounded teardown before releasing the deployment lock.\n')
        raise Refusal('Deployment interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    Deployment(args).execute()


if __name__ == '__main__':
    try:
        main()
    except (Refusal, resources.Refusal) as error:
        print('ERROR: ' + str(error) + '. No automatic production rollback; see the runbook.', file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('ERROR: deployment failed; sensitive diagnostics withheld. Inspect state using the runbook.', file=sys.stderr)
        sys.exit(1)
