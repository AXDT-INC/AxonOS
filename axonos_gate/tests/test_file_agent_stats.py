"""Focused regression tests for in-container storage telemetry."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch
import tempfile

from axonos_gate import file_agent


class FileAgentStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        file_agent._cpu_last_sample["usage_usec"] = None
        file_agent._cpu_last_sample["ts"] = None

    def test_disk_used_excludes_ext4_reserved_blocks(self) -> None:
        gib = 1024**3
        mib = 1024**2
        disk = SimpleNamespace(
            total=200 * gib,
            used=510 * mib,
            # f_bavail excludes ext4's reserved 5%; total-free therefore is
            # deliberately not the user-data usage value.
            free=(190 * gib) - (510 * mib),
        )
        with patch.object(file_agent, "_cgroup_cpu_usage_usec", return_value=None), \
             patch.object(file_agent, "_cgroup_memory_bytes", return_value=(None, None)), \
             patch.object(file_agent, "_cgroup_cpu_limit_count", return_value=1.0), \
             patch.object(file_agent, "files_root", return_value="/home/aXonian"), \
             patch.object(file_agent.shutil, "disk_usage", return_value=disk):
            stats = file_agent.collect_container_stats()

        self.assertEqual(stats["disk_used_bytes"], 510 * mib)
        self.assertEqual(stats["disk_total_bytes"], 200 * gib)
        self.assertEqual(stats["disk_free_bytes"], disk.free)
        self.assertNotEqual(
            stats["disk_used_bytes"],
            stats["disk_total_bytes"] - stats["disk_free_bytes"],
        )

    def test_disk_values_are_unavailable_together_when_stat_fails(self) -> None:
        with patch.object(file_agent, "_cgroup_cpu_usage_usec", return_value=None), \
             patch.object(file_agent, "_cgroup_memory_bytes", return_value=(None, None)), \
             patch.object(file_agent, "_cgroup_cpu_limit_count", return_value=1.0), \
             patch.object(file_agent.shutil, "disk_usage", side_effect=OSError("gone")):
            stats = file_agent.collect_container_stats()

        self.assertIsNone(stats["disk_used_bytes"])
        self.assertIsNone(stats["disk_total_bytes"])
        self.assertIsNone(stats["disk_free_bytes"])

    def test_stats_publish_only_valid_ed25519_host_fingerprint(self) -> None:
        self.assertIn(
            "/run/axonos/ssh-host-ed25519.sha256",
            file_agent.collect_container_stats.__code__.co_consts,
        )
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii") as fingerprint:
            fingerprint.write("SHA256:abcdefghijklmnopqrstuvwxyz0123456789\n")
            fingerprint.flush()
            with patch.dict(
                file_agent.os.environ,
                {"AXGT_SSH_HOST_FINGERPRINT_FILE": fingerprint.name},
            ), patch.object(file_agent, "_cgroup_cpu_usage_usec", return_value=None), \
                 patch.object(file_agent, "_cgroup_memory_bytes", return_value=(None, None)), \
                 patch.object(file_agent, "_cgroup_cpu_limit_count", return_value=1.0), \
                 patch.object(file_agent.shutil, "disk_usage", side_effect=OSError("gone")):
                stats = file_agent.collect_container_stats()

        self.assertEqual(stats["ssh_host_key_algorithm"], "ED25519")
        self.assertEqual(
            stats["ssh_host_key_fingerprint"],
            "SHA256:abcdefghijklmnopqrstuvwxyz0123456789",
        )


if __name__ == "__main__":
    unittest.main()
