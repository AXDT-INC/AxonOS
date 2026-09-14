#!/usr/bin/env python3
"""Provision the shared roots and fixed, bounded publisher slot pool.

The worker still creates dispatch.lock/global-quarantine itself, which lets it
distinguish a replacement volume from its established volume.  If slots are
missing from a volume that already has those controls, this initializer marks
the existing global control active so the loss cannot be silently repaired.
"""

import fcntl
import math
import os
import re
import stat
import struct


DIRECTORIES = (
    "/run/axonos-x-capi",
    "/run/axonos-x-capi-privacy",
)
DIRECTORY = DIRECTORIES[1]
WORKER_UID = 10001
LOCK_NAME = "dispatch.lock"
LOCK_MAGIC = b"AXCPF001"
GLOBAL_NAME = "global-quarantine"
GLOBAL_STATES = (b"AXCPQ000", b"AXCPQ001")
GLOBAL_ACTIVE = GLOBAL_STATES[1]
SLOT_COUNT = 64
SLOT_RECORD_BYTES = 138
SLOT_INACTIVE = b"I:" + (b"0" * 136)


def valid_slot_record(raw: bytes) -> bool:
    if raw == SLOT_INACTIVE:
        return True
    if (
        len(raw) != SLOT_RECORD_BYTES
        or raw[:2] != b"P:"
        or re.fullmatch(rb"[0-9a-f]{64}", raw[2:66]) is None
        or re.fullmatch(rb"[0-9a-f]{64}", raw[66:130]) is None
    ):
        return False
    try:
        expiry = struct.unpack("!d", raw[130:138])[0]
    except struct.error:
        return False
    return math.isfinite(expiry) and expiry > 0


def fail(message: str) -> None:
    raise SystemExit(message)


for managed_directory in DIRECTORIES:
    directory = os.lstat(managed_directory)
    if not stat.S_ISDIR(directory.st_mode) or stat.S_ISLNK(directory.st_mode):
        fail("unsafe X CAPI runtime volume")
    if directory.st_uid not in (0, WORKER_UID):
        fail("unexpected X CAPI runtime volume owner")
    os.chown(managed_directory, WORKER_UID, WORKER_UID)
    os.chmod(managed_directory, 0o700)
directory_fd = os.open(
    DIRECTORY,
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
)
try:
    flags = (
        os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )

    def open_existing_control(name, allowed_values):
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid not in (0, WORKER_UID)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size != len(allowed_values[0])
                or os.pread(descriptor, info.st_size, 0) not in allowed_values
            ):
                fail("unsafe privacy-fence control file")
            os.fchown(descriptor, WORKER_UID, WORKER_UID)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    lock_fd = open_existing_control(LOCK_NAME, (LOCK_MAGIC,))
    global_fd = open_existing_control(GLOBAL_NAME, GLOBAL_STATES)
    if (lock_fd is None) != (global_fd is None):
        fail("partial privacy-fence primary controls")
    established = lock_fd is not None
    if established:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            fail("privacy-fence controls are in use")

    created_slot = False
    try:
        for index in range(SLOT_COUNT):
            name = f"pending-{index:02d}"
            descriptor = None
            try:
                try:
                    descriptor = os.open(name, flags, dir_fd=directory_fd)
                except FileNotFoundError:
                    descriptor = os.open(
                        name, flags | os.O_CREAT | os.O_EXCL, 0o600,
                        dir_fd=directory_fd,
                    )
                    created_slot = True
                    os.fchown(descriptor, WORKER_UID, WORKER_UID)
                    if os.write(descriptor, SLOT_INACTIVE) != len(SLOT_INACTIVE):
                        fail("short privacy-fence slot initialization")
                    os.fsync(descriptor)
                info = os.fstat(descriptor)
                raw = os.pread(descriptor, len(SLOT_INACTIVE), 0)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid not in (0, WORKER_UID)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size != len(SLOT_INACTIVE)
                    or not valid_slot_record(raw)
                ):
                    fail("unsafe privacy-fence pending slot")
                os.fchown(descriptor, WORKER_UID, WORKER_UID)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if established and created_slot:
            if os.pwrite(global_fd, GLOBAL_ACTIVE, 0) != len(GLOBAL_ACTIVE):
                fail("short privacy-fence quarantine activation")
            os.fsync(global_fd)
        os.fsync(directory_fd)
    finally:
        if established:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        if global_fd is not None:
            os.close(global_fd)
        if lock_fd is not None:
            os.close(lock_fd)
finally:
    os.close(directory_fd)
