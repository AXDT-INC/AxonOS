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
        self.assertEqual(
            strip_conflicting_gpu_run_flags(
                ["--memory", "8g", "--gpus", "device=0,1", "--read-only"]
            ),
            ["--memory", "8g", "--read-only"],
        )

    def test_docker_run_gpus_device_value_quotes_multi_gpu(self) -> None:
        from docker_gpu_cli import docker_run_gpus_device_value

        self.assertEqual(docker_run_gpus_device_value([0]), "device=0")
        self.assertEqual(docker_run_gpus_device_value([1]), "device=1")
        self.assertEqual(docker_run_gpus_device_value([0, 1]), '"device=0,1"')
        self.assertEqual(docker_run_gpus_device_value([0, 2, 3]), '"device=0,2,3"')

    def test_docker_run_gpus_device_value_rejects_empty(self) -> None:
        from docker_gpu_cli import docker_run_gpus_device_value

        with self.assertRaises(ValueError):
            docker_run_gpus_device_value([])

    def test_subprocess_env_drops_nvidia_visible(self) -> None:
        from docker_gpu_cli import subprocess_env_for_nested_docker

        with patch.dict(os.environ, {"NVIDIA_VISIBLE_DEVICES": "all"}, clear=False):
            env = subprocess_env_for_nested_docker()
        self.assertNotIn("NVIDIA_VISIBLE_DEVICES", env)

    def test_session_container_ompi_mca_env_flags(self) -> None:
        from docker_gpu_cli import session_container_ompi_mca_env_flags

        self.assertEqual(
            session_container_ompi_mca_env_flags(),
            [
                "-e",
                "OMPI_MCA_btl=vader,self,tcp",
                "-e",
                "OMPI_MCA_btl_base_warn_component_unused=0",
            ],
        )

    def test_runtime_config_digest_is_stable_and_identity_bound(self) -> None:
        from docker_gpu_cli import session_runtime_config_digest

        base = {
            "session_id": 37,
            "wallet": "0x1234567890123456789012345678901234567890",
            "profile": "small",
            "gpu_ids": [2, 0],
            "files_key": "per-session-secret",
            "ssh_enabled": False,
            "network_name": "axgt-session-net-37",
            "image_name": "axonos:public-beta",
        }
        first = session_runtime_config_digest(**base)
        self.assertEqual(
            first,
            session_runtime_config_digest(**{**base, "gpu_ids": [0, 2]}),
        )
        self.assertEqual(len(first), 64)
        for key, changed in (
            ("session_id", 38),
            ("wallet", "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            ("files_key", "another-session-secret"),
            ("ssh_enabled", True),
            ("network_name", "axonos_stack"),
            ("requested_template", "chemistry"),
            ("ssh_pubkey", "ssh-ed25519 AAAA-test"),
        ):
            with self.subTest(key=key):
                self.assertNotEqual(
                    first,
                    session_runtime_config_digest(**{**base, key: changed}),
                )

    def test_strip_unsafe_session_run_flags_preserves_resource_limits(self) -> None:
        from docker_gpu_cli import strip_unsafe_session_run_flags

        self.assertEqual(
            strip_unsafe_session_run_flags(
                [
                    "--memory",
                    "8g",
                    "--privileged",
                    "--network=host",
                    "--mount",
                    "type=bind,src=/,dst=/host",
                    "--env-file=/run/secrets/all",
                    "-eDATABASE_URL=secret",
                    "-p6080:6080",
                    "--device",
                    "/dev/sda",
                    "--read-only",
                ]
            ),
            ["--memory", "8g", "--read-only"],
        )

    def test_strip_unsafe_session_run_flags_rejects_clustered_and_api_socket_forms(self) -> None:
        from docker_gpu_cli import strip_unsafe_session_run_flags

        self.assertEqual(
            strip_unsafe_session_run_flags(
                [
                    "--cpus",
                    "2",
                    "-itP",
                    "-itp8080:80",
                    "-itv/tmp:/host",
                    "-iteWEBRTC_AGENT_INTERNAL_KEY",
                    "--use-api-socket",
                    "--cidfile",
                    "/etc/cron.d/tenant",
                    "--label-file=/run/secrets/control",
                    "--read-only",
                ]
            ),
            ["--cpus", "2", "--read-only"],
        )


if __name__ == "__main__":
    unittest.main()
