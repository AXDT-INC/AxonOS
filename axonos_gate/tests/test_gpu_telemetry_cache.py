"""Trust-boundary tests for the gate's shared GPU telemetry snapshot."""

import ast
import json
import logging
import os
import re
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "axonos_gate" / "websockify_gate.py"
SUPERVISOR_PATH = REPO_ROOT / "supervisord.conf"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"


def _shared_cache_reader(cache_path: Path):
    """Compile only the reader so tests do not require websockify at import time."""
    tree = ast.parse(GATE_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_shared_gpu_cache"
    )
    namespace = {
        "os": os,
        "json": json,
        "logger": logging.getLogger("test.gpu_telemetry_cache"),
        "_gpu_cache_file": str(cache_path),
    }
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, str(GATE_PATH), "exec"), namespace)
    return namespace["_shared_gpu_cache"]


def _trusted_metadata(path: Path, mode: int = 0o644, uid: int = 0):
    metadata = path.stat()
    return SimpleNamespace(
        st_mode=stat.S_IFREG | mode,
        st_size=metadata.st_size,
        st_uid=uid,
    )


class SharedGpuCacheTests(unittest.TestCase):
    def _read_payload(self, payload, *, mode=0o644, uid=0):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "gpu.json"
            cache.write_text(json.dumps(payload), encoding="utf-8")
            reader = _shared_cache_reader(cache)
            metadata = _trusted_metadata(cache, mode=mode, uid=uid)
            with patch.object(os, "fstat", return_value=metadata):
                return reader()

    @staticmethod
    def _valid_payload():
        return {
            "gpus": [
                {
                    "index": 0,
                    "name": "Tesla V100-SXM2-32GB",
                    "utilization_pct": 25.0,
                    "memory_used_mb": 1024.0,
                    "memory_total_mb": 32768.0,
                },
                {
                    "index": 7,
                    "name": "Tesla V100-SXM2-32GB",
                    "utilization_pct": 75.0,
                    "memory_used_mb": 2048.0,
                    "memory_total_mb": 32768.0,
                },
            ],
            "ts": time.time(),
            "monotonic_ts": time.monotonic(),
        }

    def test_accepts_trusted_regular_snapshot(self):
        payload = self._valid_payload()
        gpus, wall_timestamp, monotonic_timestamp = self._read_payload(payload)
        self.assertEqual(gpus, payload["gpus"])
        self.assertEqual(wall_timestamp, payload["ts"])
        self.assertEqual(monotonic_timestamp, payload["monotonic_ts"])

    def test_malformed_or_non_object_top_level_fails_closed(self):
        for payload in (None, [], "telemetry", 123):
            with self.subTest(payload=payload):
                self.assertEqual(self._read_payload(payload), ([], 0.0, 0.0))

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "gpu.json"
            cache.write_text("{not-json", encoding="utf-8")
            reader = _shared_cache_reader(cache)
            with patch.object(
                os,
                "fstat",
                return_value=_trusted_metadata(cache),
            ):
                self.assertEqual(reader(), ([], 0.0, 0.0))

    def test_non_finite_or_future_monotonic_timestamp_fails_closed(self):
        invalid_values = (
            float("nan"),
            float("inf"),
            float("-inf"),
            -1.0,
            time.monotonic() + 60.0,
        )
        for value in invalid_values:
            with self.subTest(monotonic_ts=value):
                payload = self._valid_payload()
                payload["monotonic_ts"] = value
                self.assertEqual(self._read_payload(payload), ([], 0.0, 0.0))

    def test_non_finite_non_positive_or_future_wall_timestamp_fails_closed(self):
        invalid_values = (
            float("nan"),
            float("inf"),
            float("-inf"),
            0.0,
            -1.0,
            time.time() + 60.0,
        )
        for value in invalid_values:
            with self.subTest(ts=value):
                payload = self._valid_payload()
                payload["ts"] = value
                self.assertEqual(self._read_payload(payload), ([], 0.0, 0.0))

    def test_required_metric_ranges_and_finite_values_fail_closed(self):
        invalid_mutations = (
            ("utilization_pct", -0.01),
            ("utilization_pct", 100.01),
            ("utilization_pct", float("nan")),
            ("utilization_pct", float("inf")),
            ("utilization_pct", True),
            ("memory_used_mb", -0.01),
            ("memory_used_mb", 32768.01),
            ("memory_used_mb", float("nan")),
            ("memory_total_mb", 0.0),
            ("memory_total_mb", -1.0),
            ("memory_total_mb", float("inf")),
        )
        for key, value in invalid_mutations:
            with self.subTest(metric=key, value=value):
                payload = self._valid_payload()
                payload["gpus"][0][key] = value
                self.assertEqual(self._read_payload(payload), ([], 0.0, 0.0))

        for missing_key in (
            "utilization_pct",
            "memory_used_mb",
            "memory_total_mb",
        ):
            with self.subTest(missing=missing_key):
                payload = self._valid_payload()
                del payload["gpus"][0][missing_key]
                self.assertEqual(self._read_payload(payload), ([], 0.0, 0.0))

    def test_non_finite_or_non_numeric_optional_metrics_fail_closed(self):
        invalid_values = (float("nan"), float("inf"), float("-inf"), True, "45")
        for key in ("temperature_c", "power_draw_w"):
            for value in invalid_values:
                with self.subTest(metric=key, value=value):
                    payload = self._valid_payload()
                    payload["gpus"][0][key] = value
                    self.assertEqual(self._read_payload(payload), ([], 0.0, 0.0))

            with self.subTest(metric=key, value=None):
                payload = self._valid_payload()
                payload["gpus"][0][key] = None
                self.assertEqual(self._read_payload(payload)[0], payload["gpus"])

    def test_duplicate_negative_or_non_integer_gpu_indices_fail_closed(self):
        invalid_indices = (
            [0, 0],
            [-1],
            [True],
            ["0"],
            [1.0],
            [None],
        )
        for indices in invalid_indices:
            with self.subTest(indices=indices):
                payload = self._valid_payload()
                payload["gpus"] = [
                    {
                        "index": index,
                        "utilization_pct": 0.0,
                        "memory_used_mb": 0.0,
                        "memory_total_mb": 32768.0,
                    }
                    for index in indices
                ]
                self.assertEqual(self._read_payload(payload), ([], 0.0, 0.0))

    def test_requires_root_owner_and_non_writable_group_or_other_mode(self):
        payload = self._valid_payload()
        for uid, mode in ((1000, 0o644), (0, 0o664), (0, 0o646), (0, 0o666)):
            with self.subTest(uid=uid, mode=oct(mode)):
                self.assertEqual(
                    self._read_payload(payload, uid=uid, mode=mode),
                    ([], 0.0, 0.0),
                )

    def test_uses_no_follow_when_platform_supports_it(self):
        source = ast.get_source_segment(
            GATE_PATH.read_text(encoding="utf-8"),
            next(
                node
                for node in ast.parse(GATE_PATH.read_text(encoding="utf-8")).body
                if isinstance(node, ast.FunctionDef)
                and node.name == "_shared_gpu_cache"
            ),
        )
        self.assertIn('getattr(os, "O_NOFOLLOW", 0)', source)


class GpuCollectorSupervisorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.supervisor = SUPERVISOR_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^\[program:gpu-telemetry\]\n(.*?)(?=^\[program:|\Z)",
            cls.supervisor,
        )
        if match is None:
            raise AssertionError("gpu-telemetry supervisor program is missing")
        cls.section = match.group(1)

    def test_collector_is_root_and_central_only(self):
        self.assertNotRegex(self.section, r"(?m)^user=")
        self.assertIn('if [ -n \\"${AXGT_SESSION_ID:-}\\" ]', self.section)
        self.assertIn("/axonos_gate/gpu_telemetry_collector.py", self.section)
        self.assertIn("exec sleep infinity", self.section)

    def test_command_runs_collector_only_without_session_identity(self):
        command_line = next(
            line for line in self.section.splitlines() if line.startswith("command=")
        )
        prefix = 'command=/bin/bash -c "'
        self.assertTrue(command_line.startswith(prefix))
        self.assertTrue(command_line.endswith('"'))
        command = command_line[len(prefix):-1].replace(r'\"', '"')
        command = command.replace(
            "exec /usr/bin/python3 /axonos_gate/gpu_telemetry_collector.py",
            "printf collector",
        ).replace("exec sleep infinity", "printf disabled")

        for session_id, expected in ((None, "collector"), ("", "collector"), ("306", "disabled")):
            with self.subTest(session_id=session_id):
                environment = dict(os.environ)
                if session_id is None:
                    environment.pop("AXGT_SESSION_ID", None)
                else:
                    environment["AXGT_SESSION_ID"] = session_id
                result = subprocess.run(
                    ["/bin/bash", "-c", command],
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.stdout, expected)


class GpuCollectorComposeContractTests(unittest.TestCase):
    def test_central_collector_has_explicit_utility_only_gpu_access(self):
        compose = COMPOSE_PATH.read_text(encoding="utf-8")
        axonos_service = compose.split("\n  axonos:\n", 1)[1].split("\nnetworks:\n", 1)[0]
        self.assertIn("runtime: nvidia", axonos_service)
        self.assertIn("NVIDIA_VISIBLE_DEVICES: all", axonos_service)
        self.assertIn("NVIDIA_DRIVER_CAPABILITIES: utility", axonos_service)
        self.assertIn("AXGT_DESKTOP_ENABLED:", axonos_service)
        self.assertNotIn("NVIDIA_DRIVER_CAPABILITIES: compute", axonos_service)


if __name__ == "__main__":
    unittest.main()
