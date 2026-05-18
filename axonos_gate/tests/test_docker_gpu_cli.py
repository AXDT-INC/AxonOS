"""Unit tests for docker_gpu_cli helpers."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)


class DockerGpuCliTests(unittest.TestCase):
    def test_strip_duplicate_gpus_flags(self) -> None:
        from docker_gpu_cli import strip_conflicting_gpu_run_flags

        inp = ["-p", "6080:6080", "--gpus", "all", "--name", "x"]
        self.assertEqual(
            strip_conflicting_gpu_run_flags(inp),
            ["-p", "6080:6080", "--name", "x"],
        )
        inp2 = ["--gpus=device=1,2"]
        self.assertEqual(strip_conflicting_gpu_run_flags(inp2), [])

    def test_subprocess_env_drops_nvidia_visible(self) -> None:
        from docker_gpu_cli import subprocess_env_for_nested_docker

        with patch.dict(os.environ, {"NVIDIA_VISIBLE_DEVICES": "all"}, clear=False):
            env = subprocess_env_for_nested_docker()
        self.assertNotIn("NVIDIA_VISIBLE_DEVICES", env)


if __name__ == "__main__":
    unittest.main()
