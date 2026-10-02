#!/usr/bin/python3
"""Fixed, metadata-only root helper; install separately, never sudo the checkout.

Invoke the installed copy with /usr/bin/python3 -I -S -B and NO script arguments.
No caller-selected paths, environment configuration, stdin, secret reads, child
commands, or returned descriptors. Imports must remain standard-library only.
"""

import json
import os
import signal
import stat
import sys


VERSION = 1
DIRECTORIES = ('/', '/etc', '/etc/axonos')
FILES = {
    '/etc/axonos/x-capi-context-key': (10001, 44, 4096),
    '/etc/axonos/x-capi-db-url': (10001, 1, 8192),
    '/etc/axonos/x-capi-hash-key': (10001, 32, 256),
    '/etc/axonos/x-capi-postgres-bootstrap-password': (0, 16, 8192),
    '/etc/axonos/x-capi-postgres-worker-password': (0, 16, 8192),
}
FIELDS = ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink', 'size', 'mtime_ns', 'ctime_ns')


class MetadataRefusal(Exception):
    pass


def require(condition):
    if not condition:
        raise MetadataRefusal('Secret metadata contract cannot be attested')


def record(info):
    return {field: getattr(info, 'st_' + field) for field in FIELDS}


def validate_record(value):
    require(isinstance(value, dict) and set(value) == set(FIELDS))
    require(all(type(item) is int and 0 <= item < 2 ** 64 for item in value.values()))
    require(value['ino'] > 0 and value['nlink'] > 0 and value['mode'] <= 0o177777)


def validate_snapshot(snapshot):
    """Also used by the unprivileged client; accept no unexpected output fields."""
    require(isinstance(snapshot, dict) and set(snapshot) == {'version', 'directories', 'files'})
    require(type(snapshot['version']) is int and snapshot['version'] == VERSION)
    require(isinstance(snapshot['directories'], dict) and set(snapshot['directories']) == set(DIRECTORIES))
    require(isinstance(snapshot['files'], dict) and set(snapshot['files']) == set(FILES))
    for path, info in snapshot['directories'].items():
        validate_record(info)
        require(stat.S_ISDIR(info['mode']) and info['uid'] == info['gid'] == 0)
        require(stat.S_IMODE(info['mode']) & 0o7022 == 0)
        if path == '/etc/axonos':
            require(stat.S_IMODE(info['mode']) == 0o700)
    for path, (uid, low, high) in FILES.items():
        info = snapshot['files'][path]
        validate_record(info)
        require(stat.S_ISREG(info['mode']) and info['nlink'] == 1)
        require(info['uid'] == info['gid'] == uid and stat.S_IMODE(info['mode']) in (0o400, 0o600))
        require(low <= info['size'] <= high)
    # Aliasing reviewed roles to one inode is not an acceptable five-file set.
    require(len({(item['dev'], item['ino']) for item in snapshot['files'].values()}) == len(FILES))
    return snapshot


def _open_snapshot(handles):
    flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
    directories, files = {}, {}
    parent = None
    for path in DIRECTORIES:
        name = '/' if parent is None else path.rsplit('/', 1)[1]
        descriptor = os.open(name, flags | os.O_DIRECTORY, dir_fd=parent)
        handles.append((descriptor, directories, path))
        directories[path] = record(os.fstat(descriptor))
        parent = descriptor
    for path in FILES:
        # O_PATH never grants a content-readable descriptor. O_NOFOLLOW can
        # return a symlink descriptor, so the regular-file check is essential.
        descriptor = os.open(path.rsplit('/', 1)[1], flags, dir_fd=parent)
        handles.append((descriptor, files, path))
        files[path] = record(os.fstat(descriptor))
    return validate_snapshot({'version': VERSION, 'directories': directories, 'files': files})


def collect_snapshot():
    handles = []
    try:
        first = _open_snapshot(handles)
        # Re-walk names from / while retaining original O_PATH descriptors.
        # Refuse parent/name replacement or metadata changes during the sample.
        second = _open_snapshot(handles)
        require(first == second)
        for descriptor, values, path in handles:
            require(record(os.fstat(descriptor)) == values[path])
        return first
    finally:
        for descriptor, _, _ in reversed(handles):
            os.close(descriptor)


def main():
    try:
        require(len(sys.argv) == 1 and os.geteuid() == 0)
        require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode)
        # The privileged child has its own short deadline; it never prompts or
        # waits on another process. Controller timeouts remain an outer bound.
        # An ignored signal disposition can survive exec; do not inherit that
        # as a way to disable the privileged child's independent deadline.
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
        signal.alarm(5)
        result = collect_snapshot()
        output = json.dumps(result, sort_keys=True, separators=(',', ':'))
        require(len(output) <= 16384)
        print(output)
        return 0
    except Exception:
        # Do not include exceptions, paths, syscall results or caller input.
        print('Secret metadata cannot be attested by the privileged helper', file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    sys.exit(main())
