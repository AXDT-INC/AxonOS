#!/usr/bin/env python3
"""Publish low-latency host GPU telemetry for forked gate workers.

The gate serves requests from forked workers, so an in-process thread/cache is
not shared reliably.  This supervisor-managed process keeps NVML initialized,
samples every second, and atomically replaces a small JSON snapshot that every
worker can read without starting its own ``nvidia-smi`` process.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import signal
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional


LOGGER = logging.getLogger("axonos.gpu_telemetry")
NVML_SUCCESS = 0
NVML_ERROR_NOT_SUPPORTED = 3
DEFAULT_INTERVAL_SECONDS = 1.0
MIN_INTERVAL_SECONDS = 0.5
MAX_INTERVAL_SECONDS = 60.0
DEFAULT_CACHE_FILE = "/run/axonos/gpu-telemetry.json"


class NvmlError(RuntimeError):
    """An NVML operation failed."""


class NvmlUtilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


def telemetry_interval_seconds(value: Optional[str] = None) -> float:
    raw = value
    if raw is None:
        raw = os.getenv("AXGT_GPU_TELEMETRY_INTERVAL_SECONDS", "")
    try:
        interval = float(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_SECONDS
    if not math.isfinite(interval):
        interval = DEFAULT_INTERVAL_SECONDS
    return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, interval))


def telemetry_cache_file() -> str:
    return os.getenv("AXGT_GPU_TELEMETRY_FILE") or DEFAULT_CACHE_FILE


class NvmlClient:
    """Small ctypes wrapper that initializes NVML once per collector process."""

    def __init__(self, library=None):
        self.lib = library or ctypes.CDLL("libnvidia-ml.so.1")
        self._initialized = False
        self._handles = []
        self._configure_abi()
        self._initialize()

    def _configure_abi(self) -> None:
        handle = ctypes.c_void_p
        uint_p = ctypes.POINTER(ctypes.c_uint)
        handle_p = ctypes.POINTER(handle)

        self.lib.nvmlInit_v2.argtypes = []
        self.lib.nvmlInit_v2.restype = ctypes.c_int
        self.lib.nvmlShutdown.argtypes = []
        self.lib.nvmlShutdown.restype = ctypes.c_int
        self.lib.nvmlErrorString.argtypes = [ctypes.c_int]
        self.lib.nvmlErrorString.restype = ctypes.c_char_p
        self.lib.nvmlDeviceGetCount_v2.argtypes = [uint_p]
        self.lib.nvmlDeviceGetCount_v2.restype = ctypes.c_int
        self.lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [ctypes.c_uint, handle_p]
        self.lib.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        self.lib.nvmlDeviceGetIndex.argtypes = [handle, uint_p]
        self.lib.nvmlDeviceGetIndex.restype = ctypes.c_int
        self.lib.nvmlDeviceGetName.argtypes = [handle, ctypes.c_char_p, ctypes.c_uint]
        self.lib.nvmlDeviceGetName.restype = ctypes.c_int
        self.lib.nvmlDeviceGetUtilizationRates.argtypes = [
            handle,
            ctypes.POINTER(NvmlUtilization),
        ]
        self.lib.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int
        self.lib.nvmlDeviceGetMemoryInfo.argtypes = [handle, ctypes.POINTER(NvmlMemory)]
        self.lib.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
        self.lib.nvmlDeviceGetTemperature.argtypes = [handle, ctypes.c_uint, uint_p]
        self.lib.nvmlDeviceGetTemperature.restype = ctypes.c_int
        self.lib.nvmlDeviceGetPowerUsage.argtypes = [handle, uint_p]
        self.lib.nvmlDeviceGetPowerUsage.restype = ctypes.c_int

    def _error_text(self, rc: int) -> str:
        try:
            raw = self.lib.nvmlErrorString(rc)
            return raw.decode("utf-8", "replace") if raw else "unknown error"
        except Exception:
            return "unknown error"

    def _check(self, rc: int, operation: str) -> None:
        if rc != NVML_SUCCESS:
            raise NvmlError(f"{operation}: {self._error_text(rc)} (NVML {rc})")

    def _initialize(self) -> None:
        self._check(self.lib.nvmlInit_v2(), "nvmlInit_v2")
        self._initialized = True
        try:
            count = ctypes.c_uint()
            self._check(self.lib.nvmlDeviceGetCount_v2(ctypes.byref(count)), "get device count")
            if count.value < 1:
                raise NvmlError("NVML reported no GPU devices")
            handles = []
            for ordinal in range(count.value):
                handle = ctypes.c_void_p()
                self._check(
                    self.lib.nvmlDeviceGetHandleByIndex_v2(ordinal, ctypes.byref(handle)),
                    f"get handle for GPU ordinal {ordinal}",
                )
                handles.append(handle)
            self._handles = handles
        except Exception:
            self.close()
            raise

    def _optional_uint(self, function, handle, operation: str, *args):
        value = ctypes.c_uint()
        rc = function(handle, *args, ctypes.byref(value))
        if rc == NVML_ERROR_NOT_SUPPORTED:
            return None
        self._check(rc, operation)
        return value.value

    def sample(self) -> list[dict]:
        gpus = []
        seen_indices = set()
        for handle in self._handles:
            index = ctypes.c_uint()
            self._check(self.lib.nvmlDeviceGetIndex(handle, ctypes.byref(index)), "get GPU index")
            if index.value in seen_indices:
                raise NvmlError(f"NVML returned duplicate GPU index {index.value}")
            seen_indices.add(index.value)

            name_buffer = ctypes.create_string_buffer(96)
            self._check(
                self.lib.nvmlDeviceGetName(handle, name_buffer, len(name_buffer)),
                f"get GPU {index.value} name",
            )

            utilization = NvmlUtilization()
            self._check(
                self.lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(utilization)),
                f"get GPU {index.value} utilization",
            )

            memory = NvmlMemory()
            self._check(
                self.lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)),
                f"get GPU {index.value} memory",
            )

            temperature = self._optional_uint(
                self.lib.nvmlDeviceGetTemperature,
                handle,
                f"get GPU {index.value} temperature",
                0,
            )
            power_mw = self._optional_uint(
                self.lib.nvmlDeviceGetPowerUsage,
                handle,
                f"get GPU {index.value} power",
            )
            mib = 1024.0 * 1024.0
            memory_used_mb = memory.used / mib
            memory_total_mb = memory.total / mib
            if utilization.gpu > 100:
                raise NvmlError(f"GPU {index.value} utilization is outside 0..100")
            if memory_total_mb <= 0 or memory_used_mb > memory_total_mb:
                raise NvmlError(f"GPU {index.value} memory sample is invalid")
            gpus.append({
                "index": int(index.value),
                "name": name_buffer.value.decode("utf-8", "replace"),
                "utilization_pct": float(utilization.gpu),
                "memory_used_mb": round(memory_used_mb, 3),
                "memory_total_mb": round(memory_total_mb, 3),
                "temperature_c": float(temperature) if temperature is not None else None,
                "power_draw_w": round(power_mw / 1000.0, 3) if power_mw is not None else None,
            })
        return gpus

    def close(self) -> None:
        if self._initialized:
            try:
                self.lib.nvmlShutdown()
            finally:
                self._initialized = False
                self._handles = []


def publish_sample(path: str, gpus: list[dict], sampled_at: Optional[float] = None) -> None:
    """Atomically publish a complete, world-readable snapshot in a trusted path."""
    if not gpus:
        raise ValueError("refusing to publish an empty GPU sample")
    destination = Path(path)
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    parent_metadata = destination.parent.lstat()
    if (not stat.S_ISDIR(parent_metadata.st_mode) or
            parent_metadata.st_uid != os.geteuid() or
            parent_metadata.st_mode & 0o022):
        raise PermissionError("GPU telemetry directory is not trusted")
    wall_timestamp = float(sampled_at if sampled_at is not None else time.time())
    if not math.isfinite(wall_timestamp) or wall_timestamp <= 0:
        raise ValueError("invalid GPU telemetry timestamp")
    payload = {
        "gpus": gpus,
        "ts": wall_timestamp,
        # CLOCK_MONOTONIC is shared across processes on the same host.  Gate
        # workers use it for cache age so an NTP wall-clock correction cannot
        # make an old sample appear fresh.
        "monotonic_ts": time.monotonic(),
    }
    descriptor = None
    temporary_name = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.tmp.", dir=str(destination.parent)
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise PermissionError("GPU telemetry temporary file is not trusted")
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = None
            json.dump(payload, output, separators=(",", ":"), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def run_collector(
    stop_event: Optional[threading.Event] = None,
    client_factory: Callable[[], NvmlClient] = NvmlClient,
) -> None:
    stop = stop_event or threading.Event()
    interval = telemetry_interval_seconds()
    cache_file = telemetry_cache_file()
    client = None
    retry_delay = 1.0
    next_deadline = time.monotonic()

    while not stop.is_set():
        if client is None:
            try:
                client = client_factory()
                LOGGER.info("NVML initialized; publishing every %.3fs", interval)
                retry_delay = 1.0
                next_deadline = time.monotonic()
            except Exception as exc:
                LOGGER.warning("NVML initialization failed: %s", exc)
                stop.wait(retry_delay)
                retry_delay = min(10.0, retry_delay * 2.0)
                continue

        try:
            gpus = client.sample()
            if not gpus:
                raise NvmlError("NVML returned an empty GPU sample")
        except Exception as exc:
            LOGGER.warning("NVML sample failed: %s", exc)
            try:
                client.close()
            except Exception:
                LOGGER.exception("NVML shutdown failed")
            client = None
            # Preserve the last good file and timestamp while NVML recovers;
            # consumers will mark it stale rather than showing false freshness.
            stop.wait(retry_delay)
            retry_delay = min(10.0, retry_delay * 2.0)
            continue

        try:
            publish_sample(cache_file, gpus, time.time())
        except Exception as exc:
            # A filesystem failure is not an NVML failure. Keep the initialized
            # client and retry publication on the next fixed-rate tick.
            LOGGER.warning("GPU telemetry publish failed: %s", exc)

        retry_delay = 1.0
        next_deadline += interval
        stop.wait(max(0.0, next_deadline - time.monotonic()))
        if time.monotonic() - next_deadline > interval:
            next_deadline = time.monotonic()

    if client is not None:
        client.close()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("AXGT_GPU_TELEMETRY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_collector(stop)


if __name__ == "__main__":
    main()
