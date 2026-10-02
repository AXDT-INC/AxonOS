"""Metadata-only postflight, streamed to the gate's Python via stdin.

No application imports, secret reads, socket opens or writes.
"""
import os
import stat
import sys


def check():
    for path in ('/run/axonos-x-capi', '/run/axonos-x-capi-privacy'):
        info = os.lstat(path)
        if not (os.path.realpath(path) == path and stat.S_ISDIR(info.st_mode)
                and info.st_uid == info.st_gid == 10001 and stat.S_IMODE(info.st_mode) == 0o700):
            return False
    path = '/run/secrets/x_capi_context_key'
    info = os.lstat(path)
    return (os.path.realpath(path) == path and stat.S_ISREG(info.st_mode)
            and info.st_uid == info.st_gid == 10001 and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) in (0o400, 0o600) and 44 <= info.st_size <= 4096)


if __name__ == '__main__':
    try:
        sys.exit(0 if check() else 1)
    except Exception:
        sys.exit(1)
