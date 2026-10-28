#!/usr/bin/env python3
"""Build-only, off-server recipe. Builder resource approval is a prerequisite."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(args, *, timeout=30, output=True):
    result = subprocess.run(args, cwd=ROOT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if output else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('command refused or failed')
    return result.stdout


def main():
    if socket.gethostname().split('.')[0].lower() == 'clu1':
        raise RuntimeError('production host forbidden')
    if os.environ.get('AXONOS_OFF_SERVER_BUILD_APPROVED') != 'yes':
        raise RuntimeError('off-server builder/resource approval required')
    revision = os.environ['AXONOS_BUILD_REVISION']
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise RuntimeError('exact revision required')
    if run(['git', 'rev-parse', 'HEAD']).strip() != revision or run(
            ['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=all']).strip():
        raise RuntimeError('revision mismatch or dirty source')
    builder = os.environ['AXONOS_BUILD_BUILDER']
    if builder == 'default' or not re.fullmatch(r'[A-Za-z0-9_.-]+', builder):
        raise RuntimeError('dedicated builder required')
    base = os.environ['AXONOS_GATE_BASE']
    if not re.fullmatch(r'nvidia/cuda@sha256:[a-f0-9]{64}', base):
        raise RuntimeError('reviewed CUDA base digest required')
    driver = os.environ['NVIDIA_DRIVER_PKG_VERSION']
    if not driver:
        raise RuntimeError('reviewed production-compatible driver version required')
    secret = Path(os.environ['AXONOS_BUILD_PASSWORD_FILE'])
    if not secret.is_absolute() or not secret.is_file() or secret.is_symlink():
        raise RuntimeError('external regular secret file required')
    if secret.resolve().is_relative_to(ROOT):
        raise RuntimeError('secret must remain outside build context')
    if secret.stat().st_mode & 0o077:
        raise RuntimeError('secret file must be owner-only')
    # Read only to reject malformed input; never log/hash/record its value.
    value = secret.read_bytes()
    if not value or any(c in value for c in (b'\n', b'\r', b'\x00')):
        raise RuntimeError('secret must contain one nonempty value without newline')
    del value
    contract = ROOT / 'contracts/x-capi-handoff/v2'
    manifest = contract / 'manifest.json'
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != '085953bf44b097d571c99aff4dc18ff92fe539b3cddec6bf2727f2f56ab447d8':
        raise RuntimeError('frozen manifest differs')
    for name, digest in json.loads(manifest.read_text())['files'].items():
        if hashlib.sha256((contract / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('frozen contract differs')
    run(['docker', 'buildx', 'inspect', builder])  # No bootstrap.
    nonce = secrets.token_hex(12)
    # Public cache-key material, independent of the password; fresh per invocation.
    credential_nonce = secrets.token_hex(32)
    cache_mode = os.environ.get('AXONOS_SYNTHETIC_PREFIX_CACHE', '')
    if cache_mode not in ('', 'yes'):
        raise RuntimeError('invalid synthetic prefix cache selection')
    for role, dockerfile in [('gate', 'Dockerfile'), ('worker', 'docker/x-capi-worker/Dockerfile')]:
        tag = f'axonos-capi-{role}:{revision}-{nonce}'
        existing = run(['docker', 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}}']).splitlines()
        if tag in existing:
            raise RuntimeError('tag collision')
        args = ['docker', 'buildx', 'build', '--builder', builder,
                '--platform', 'linux/amd64', '--load',
                '--tag', tag, '--file', dockerfile,
                '--label', f'org.opencontainers.image.revision={revision}']
        if role != 'gate' or cache_mode != 'yes':
            args += ['--no-cache']
        if role == 'gate':
            args += ['--secret', f'id=axonos_password,src={secret}',
                     '--build-arg', f'AXONOS_CREDENTIAL_CACHE_NONCE={credential_nonce}',
                     '--build-arg', f'AXONOS_GATE_BASE={base}',
                     '--build-arg', 'AXONOS_SKIP_HEAVY=0',
                     '--build-arg', f'NVIDIA_DRIVER_PKG_VERSION={driver}']
        print('Building ' + role + ' with unique candidate tag', flush=True)
        run(args + ['.'], timeout=21600, output=False)
        identifier = run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']).strip()
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', identifier):
            raise RuntimeError('invalid image identity')
        print(json.dumps({'role': role, 'tag': tag, 'image_id': identifier,
                          'source_commit': revision, 'platform': 'linux/amd64',
                          'dockerfile_sha256': hashlib.sha256((ROOT / dockerfile).read_bytes()).hexdigest(),
                          'gate_base': base if role == 'gate' else None,
                          'driver_package': driver if role == 'gate' else None,
                          'cache_reuse': role == 'gate' and cache_mode == 'yes',
                          'cache_policy': 'prefix-only-fresh-credential-nonce' if role == 'gate' and cache_mode == 'yes' else 'no-cache'}), flush=True)
    print('BUILD_ONLY_COMPLETE; no push, initialization or deployment')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('BUILD_ONLY_INCOMPLETE; diagnostics withheld; no automatic cleanup', file=sys.stderr)
        sys.exit(1)
