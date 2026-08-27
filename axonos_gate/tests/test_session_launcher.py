"""Unit tests for session launcher and service persistent storage logic."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import unittest
from unittest.mock import patch, MagicMock

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)

# Mock flask and psycopg2 only if they are not installed in the current environment
try:
    import flask  # noqa: F401
except ImportError:
    from unittest.mock import MagicMock
    sys.modules['flask'] = MagicMock()

try:
    import psycopg2  # noqa: F401
except ImportError:
    from unittest.mock import MagicMock
    sys.modules['psycopg2'] = MagicMock()



class SessionLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        # Save env to restore after tests
        self._orig_env = dict(os.environ)

    def tearDown(self) -> None:
        # Restore environment
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_helpers_defaults(self) -> None:
        # Test default values when environment is clean
        # Clean env related keys
        for k in list(os.environ.keys()):
            if k.startswith("AXGT_PERSISTENT_STORAGE_"):
                del os.environ[k]
        
        from session_launcher import (
            _persistent_storage_enabled,
            _persistent_storage_volume_prefix,
            _persistent_storage_mount_path,
        )
        self.assertTrue(_persistent_storage_enabled())
        self.assertEqual(_persistent_storage_volume_prefix(), "axgt-user-storage-")
        self.assertEqual(_persistent_storage_mount_path(), "/home/aXonian")

    def test_helpers_custom_values(self) -> None:
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "false"
        os.environ["AXGT_PERSISTENT_STORAGE_VOLUME_PREFIX"] = "custom-prefix-@#$!"
        os.environ["AXGT_PERSISTENT_STORAGE_MOUNT_PATH"] = "/custom/mount;rm -rf"
        
        from session_launcher import (
            _persistent_storage_enabled,
            _persistent_storage_volume_prefix,
            _persistent_storage_mount_path,
        )
        self.assertFalse(_persistent_storage_enabled())
        # Sanitization should strip non-alphanumeric/hyphen/underscore
        self.assertEqual(_persistent_storage_volume_prefix(), "custom-prefix-")
        # Sanitization should revert unsafe mount path back to default /home/aXonian
        self.assertEqual(_persistent_storage_mount_path(), "/home/aXonian")

    def test_network_reconciliation_preserves_credit_grace_runtimes(self) -> None:
        from session_launcher import reconcile_session_networks
        from session_launcher_service import _reconcile_session_networks

        for reconcile in (reconcile_session_networks, _reconcile_session_networks):
            source = inspect.getsource(reconcile)
            self.assertIn("status IN ('credit_grace', 'paused')", source)
            self.assertIn("credit_grace_cutoff", source)
            active_clause, grace_clause = source.split(
                "OR (\n                             status IN ('credit_grace', 'paused')",
                1,
            )
            self.assertIn("hard_expires_at", active_clause)
            self.assertNotIn("hard_expires_at", grace_clause)

    def test_credit_grace_duration_prefers_canonical_env_name(self) -> None:
        os.environ["AXGT_SESSION_CREDIT_GRACE_MINUTES"] = "45"
        os.environ["AXGT_SESSION_PAUSED_MAX_MINUTES"] = "30"

        from session_launcher import _credit_grace_max_seconds as client_seconds
        from session_launcher_service import (
            _credit_grace_max_seconds as service_seconds,
        )

        self.assertEqual(client_seconds(), 45 * 60)
        self.assertEqual(service_seconds(), 45 * 60)

    @patch("subprocess.check_output")
    def test_launch_via_docker_cli_enabled(self, mock_check_output: MagicMock) -> None:
        mock_check_output.side_effect = ["", "container_id_123"]
        os.environ["AXGT_SESSION_LAUNCHER_MODE"] = "docker_cli"
        os.environ["AXGT_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_USER_CONTAINER_ENABLED"] = "true"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "true"
        os.environ["AXGT_HEARTBEAT_INTERVAL_SECONDS"] = "17"
        # Network lifecycle has its own focused regression tests.  Keep this
        # storage test's subprocess mock scoped to the final docker run.
        os.environ["AXGT_SESSION_NETWORK_ISOLATION"] = "false"
        os.environ["AXGT_SESSION_CONTAINER_NETWORK"] = "axonos_stack"
        
        from session_launcher import launch_session
        with patch(
            "session_launcher._inspect_managed_container_contract_direct",
            return_value=("absent", None, ""),
        ), patch(
            "session_launcher._cleanup_session_network_direct",
            return_value=(True, None),
        ):
            ok, cid, err = launch_session(
                session_id=42,
                wallet="0xAbC123-xyz_!!",
                profile="small",
                gpu_ids=[0],
                webrtc_agent_token="signed-capability",
            )
        
        self.assertTrue(ok)
        self.assertEqual(cid, "container_id_123")
        self.assertIsNone(err)
        
        self.assertEqual(mock_check_output.call_count, 2)
        cmd = mock_check_output.call_args_list[-1].args[0]
        
        # Check volume mount parameters
        # Sanitized wallet should be 0xabc123-xyz_
        self.assertIn("--cap-drop", cmd)
        self.assertEqual(cmd[cmd.index("--cap-drop") + 1], "NET_RAW")
        self.assertIn("AXGT_HEARTBEAT_INTERVAL_SECONDS=17", cmd)
        self.assertIn("-v", cmd)
        idx = cmd.index("-v")
        self.assertEqual(cmd[idx + 1], "axgt-user-storage-0xabc123-xyz_:/home/aXonian")

    @patch("subprocess.check_output")
    def test_launch_via_docker_cli_with_template(self, mock_check_output: MagicMock) -> None:
        mock_check_output.side_effect = ["", "container_id_123"]
        os.environ["AXGT_SESSION_LAUNCHER_MODE"] = "docker_cli"
        os.environ["AXGT_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_USER_CONTAINER_ENABLED"] = "true"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "false"
        os.environ["AXGT_SESSION_NETWORK_ISOLATION"] = "false"
        os.environ["AXGT_SESSION_CONTAINER_NETWORK"] = "axonos_stack"
        
        from session_launcher import launch_session
        with patch(
            "session_launcher._inspect_managed_container_contract_direct",
            return_value=("absent", None, ""),
        ), patch(
            "session_launcher._cleanup_session_network_direct",
            return_value=(True, None),
        ):
            ok, cid, err = launch_session(
                session_id=42,
                wallet="0xAbC123",
                profile="small",
                gpu_ids=[0],
                template="  PyTorch  ",
                webrtc_agent_token="signed-capability",
            )
        
        self.assertTrue(ok)
        self.assertEqual(mock_check_output.call_count, 2)
        cmd = mock_check_output.call_args_list[-1].args[0]
        
        # Verify requested template is passed as environment variable
        self.assertIn("-e", cmd)
        self.assertIn("AXONOS_SELECTED_TEMPLATE=pytorch", cmd)

    def test_direct_launcher_rejects_unknown_template_before_side_effects(self) -> None:
        os.environ["AXGT_SESSION_LAUNCHER_MODE"] = "docker_cli"
        os.environ["AXGT_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_USER_CONTAINER_ENABLED"] = "true"

        import session_launcher
        with patch.object(session_launcher, "runtime_configuration_error") as preflight, \
             patch.object(session_launcher.subprocess, "check_output") as docker:
            ok, cid, err = session_launcher.launch_session(
                session_id=42,
                wallet="0xAbC123",
                profile="small",
                gpu_ids=[0],
                template="not-deployed",
                webrtc_agent_token="signed-capability",
            )

        self.assertFalse(ok)
        self.assertIsNone(cid)
        self.assertIn("unsupported requested_template", err)
        preflight.assert_not_called()
        docker.assert_not_called()

    @patch("subprocess.check_output")
    def test_launch_via_docker_cli_disabled(self, mock_check_output: MagicMock) -> None:
        mock_check_output.side_effect = ["", "container_id_123"]
        os.environ["AXGT_SESSION_LAUNCHER_MODE"] = "docker_cli"
        os.environ["AXGT_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_USER_CONTAINER_ENABLED"] = "true"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "false"
        os.environ["AXGT_SESSION_NETWORK_ISOLATION"] = "false"
        os.environ["AXGT_SESSION_CONTAINER_NETWORK"] = "axonos_stack"
        
        from session_launcher import launch_session
        with patch(
            "session_launcher._inspect_managed_container_contract_direct",
            return_value=("absent", None, ""),
        ), patch(
            "session_launcher._cleanup_session_network_direct",
            return_value=(True, None),
        ):
            ok, cid, err = launch_session(
                session_id=42,
                wallet="0xAbC123",
                profile="small",
                gpu_ids=[0],
                webrtc_agent_token="signed-capability",
            )
        
        self.assertTrue(ok)
        self.assertEqual(mock_check_output.call_count, 2)
        cmd = mock_check_output.call_args_list[-1].args[0]
        
        self.assertNotIn("-v", cmd)

    @patch("session_launcher_service._ensure_persistent_storage_volume")
    def test_service_build_launch_cmd_with_requested_storage_gb(self, mock_ensure_vol: MagicMock) -> None:
        mock_ensure_vol.return_value = (True, None)
        os.environ["AXGT_HOST_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "true"
        
        from session_launcher_service import _build_launch_cmd
        payload = {
            "session_id": 42,
            "wallet_address": "0xAbC123-xyz_!!",
            "requested_profile": "small",
            "assigned_gpu_ids": [0],
            "webrtc_agent_token": "signed-capability",
            "requested_storage_gb": 200,
        }
        cmd, err = _build_launch_cmd(payload)
        self.assertIsNone(err)
        self.assertIsNotNone(cmd)
        mock_ensure_vol.assert_called_once_with(
            "axgt-user-storage-0xabc123-xyz_",
            200,
            "0xabc123-xyz_!!",
        )

    def test_service_repairs_filesystem_smaller_than_existing_image(self) -> None:
        import session_launcher_service as service

        gib = 1024 * 1024 * 1024
        block_size = 4096
        filesystem_sizes = iter((100 * gib, 200 * gib))

        def check_output(cmd, **_kwargs):
            if cmd[:2] == ["losetup", "-j"]:
                return "/dev/loop7: []: (/storage/user.ext4)\n"
            if cmd[:2] == ["dumpe2fs", "-h"]:
                size = next(filesystem_sizes)
                return f"Block count: {size // block_size}\nBlock size: {block_size}\n"
            if cmd[:2] == ["docker", "ps"]:
                return ""
            if cmd[:3] == ["docker", "volume", "inspect"]:
                return '[{"Options":{"type":"ext4","device":"/dev/loop7"}}]'
            raise AssertionError(f"unexpected check_output command: {cmd}")

        os.environ["AXGT_REAL_STORAGE_TEST"] = "1"
        with patch.object(service, "_tool_path", side_effect=lambda name: name), \
             patch.object(service.os, "makedirs"), \
             patch.object(service.os.path, "exists", return_value=True), \
             patch.object(service.os.path, "getsize", return_value=500 * gib), \
             patch.object(service.subprocess, "check_output", side_effect=check_output), \
             patch.object(service.subprocess, "run", return_value=MagicMock(returncode=1, stdout="repaired")) as mock_run, \
             patch.object(service.subprocess, "check_call") as mock_check_call:
            ok, error = service._ensure_persistent_storage_volume("user", 200)

        self.assertTrue(ok, error)
        self.assertIsNone(error)
        calls = [entry.args[0] for entry in mock_check_call.call_args_list]
        self.assertIn(["losetup", "-c", "/dev/loop7"], calls)
        resize_command = ["resize2fs", "/dev/loop7", str((200 * gib) // block_size)]
        self.assertIn(resize_command, calls)
        mock_run.assert_called_once_with(
            ["e2fsck", "-f", "-p", "/dev/loop7"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertLess(
            calls.index(["losetup", "-c", "/dev/loop7"]),
            calls.index(resize_command),
        )
        self.assertFalse(any(command[0] == "truncate" for command in calls))
        self.assertFalse(any("/storage/user.ext4" in part for command in calls for part in command))

    def test_service_rejects_request_below_observed_filesystem_capacity(self) -> None:
        import session_launcher_service as service

        gib = 1024**3
        block_size = 4096
        wallet = "0x" + "1" * 40

        def check_output(cmd, **_kwargs):
            if cmd[:2] == ["losetup", "-j"]:
                return "/dev/loop7: []: (/storage/user.ext4)\n"
            if cmd[:2] == ["dumpe2fs", "-h"]:
                return (
                    f"Block count: {(250 * gib) // block_size}\n"
                    f"Block size: {block_size}\n"
                )
            raise AssertionError(f"unexpected check_output command: {cmd}")

        os.environ["AXGT_REAL_STORAGE_TEST"] = "1"
        with patch.object(service, "_tool_path", side_effect=lambda name: name), \
             patch.object(service.os, "makedirs"), \
             patch.object(service.os.path, "exists", return_value=True), \
             patch.object(service.os.path, "getsize", return_value=250 * gib), \
             patch.object(service.subprocess, "check_output", side_effect=check_output), \
             patch.object(
                 service,
                 "_record_persistent_storage_capacity",
                 return_value=(True, 250 * gib, None),
             ) as record_capacity, \
             patch.object(service.subprocess, "run") as mock_run, \
             patch.object(service.subprocess, "check_call") as mock_check_call:
            ok, error = service._ensure_persistent_storage_volume(
                "axgt-user-storage-" + wallet,
                100,
                wallet,
            )

        self.assertFalse(ok)
        self.assertIn("cannot be reduced from 250 GB to 100 GB", error or "")
        record_capacity.assert_called_once_with(
            wallet,
            "axgt-user-storage-" + wallet,
            250 * gib,
        )
        mock_run.assert_not_called()
        mock_check_call.assert_not_called()

    def test_service_registry_high_water_is_part_of_host_floor(self) -> None:
        import session_launcher_service as service

        gib = 1024**3
        block_size = 4096
        wallet = "0x" + "2" * 40

        def check_output(cmd, **_kwargs):
            if cmd[:2] == ["losetup", "-j"]:
                return "/dev/loop7: []: (/storage/user.ext4)\n"
            if cmd[:2] == ["dumpe2fs", "-h"]:
                return (
                    f"Block count: {(250 * gib) // block_size}\n"
                    f"Block size: {block_size}\n"
                )
            raise AssertionError(f"unexpected check_output command: {cmd}")

        os.environ["AXGT_REAL_STORAGE_TEST"] = "1"
        with patch.object(service, "_tool_path", side_effect=lambda name: name), \
             patch.object(service.os, "makedirs"), \
             patch.object(service.os.path, "exists", return_value=True), \
             patch.object(service.os.path, "getsize", return_value=250 * gib), \
             patch.object(service.subprocess, "check_output", side_effect=check_output), \
             patch.object(
                 service,
                 "_record_persistent_storage_capacity",
                 return_value=(True, 300 * gib, None),
             ), \
             patch.object(service.subprocess, "run") as mock_run, \
             patch.object(service.subprocess, "check_call") as mock_check_call:
            ok, error = service._ensure_persistent_storage_volume(
                "axgt-user-storage-" + wallet,
                250,
                wallet,
            )

        self.assertFalse(ok)
        self.assertIn("cannot be reduced from 300 GB to 250 GB", error or "")
        mock_run.assert_not_called()
        mock_check_call.assert_not_called()

    def test_capacity_recorder_keeps_monotonic_host_observation(self) -> None:
        import session_launcher_service as service

        gib = 1024**3
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = (300 * gib,)
        os.environ["AXGT_CHALLENGE_DB_URL"] = "postgresql://storage-registry"

        with patch("psycopg2.connect", return_value=conn):
            ok, recorded_bytes, error = service._record_persistent_storage_capacity(
                "0x" + "3" * 40,
                "axgt-user-storage-" + "0x" + "3" * 40,
                250 * gib,
            )

        self.assertTrue(ok, error)
        self.assertEqual(recorded_bytes, 300 * gib)
        sql = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
        self.assertIn("GREATEST(", sql)
        self.assertIn("RETURNING provisioned_bytes", sql)
        self.assertIn("ELSE axgt_storage_volumes.observed_at", sql)
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    def test_startup_sync_backfills_existing_wallet_filesystem_capacity(self) -> None:
        import session_launcher_service as service

        gib = 1024**3
        wallet = "0x" + "4" * 40
        volume_name = "axgt-user-storage-" + wallet
        entry = MagicMock()
        entry.name = volume_name + ".ext4"
        entry.path = "/storage/" + entry.name

        with patch.object(service.os, "scandir", return_value=[entry]), \
             patch.object(
                 service,
                 "_ext4_filesystem_size",
                 return_value=(250 * gib, 4096, None),
             ) as inspect_fs, \
             patch.object(
                 service,
                 "_record_persistent_storage_capacity",
                 return_value=(True, 250 * gib, None),
             ) as record_capacity:
            count = service._sync_persistent_storage_capacity_records()

        self.assertEqual(count, 1)
        inspect_fs.assert_called_once_with(entry.path)
        record_capacity.assert_called_once_with(wallet, volume_name, 250 * gib)

    def test_startup_sync_fails_closed_when_capacity_cannot_be_recorded(self) -> None:
        import session_launcher_service as service

        wallet = "0x" + "5" * 40
        entry = MagicMock()
        entry.name = "axgt-user-storage-" + wallet + ".ext4"
        entry.path = "/storage/" + entry.name

        with patch.object(service.os, "scandir", return_value=[entry]), \
             patch.object(
                 service,
                 "_ext4_filesystem_size",
                 return_value=(250 * 1024**3, 4096, None),
             ), patch.object(
                 service,
                 "_record_persistent_storage_capacity",
                 return_value=(False, None, "database unavailable"),
             ):
            with self.assertRaisesRegex(RuntimeError, entry.name):
                service._sync_persistent_storage_capacity_records()

    def test_service_refuses_resize_while_volume_is_in_use(self) -> None:
        import session_launcher_service as service

        gib = 1024 * 1024 * 1024
        block_size = 4096

        def check_output(cmd, **_kwargs):
            if cmd[:2] == ["losetup", "-j"]:
                return "/dev/loop7: []: (/storage/user.ext4)\n"
            if cmd[:2] == ["dumpe2fs", "-h"]:
                return f"Block count: {(100 * gib) // block_size}\nBlock size: {block_size}\n"
            if cmd[:2] == ["docker", "ps"]:
                return "running-container-id\n"
            raise AssertionError(f"unexpected check_output command: {cmd}")

        os.environ["AXGT_REAL_STORAGE_TEST"] = "1"
        with patch.object(service, "_tool_path", side_effect=lambda name: name), \
             patch.object(service.os, "makedirs"), \
             patch.object(service.os.path, "exists", return_value=True), \
             patch.object(service.os.path, "getsize", return_value=200 * gib), \
             patch.object(service.subprocess, "check_output", side_effect=check_output), \
             patch.object(service.subprocess, "check_call") as mock_check_call:
            ok, error = service._ensure_persistent_storage_volume("user", 200)

        self.assertFalse(ok)
        self.assertIn("while a container is using it", error or "")
        mock_check_call.assert_not_called()

    def test_service_refuses_resize_when_e2fsck_cannot_auto_repair(self) -> None:
        import session_launcher_service as service

        gib = 1024 * 1024 * 1024
        block_size = 4096

        def check_output(cmd, **_kwargs):
            if cmd[:2] == ["losetup", "-j"]:
                return "/dev/loop7: []: (/storage/user.ext4)\n"
            if cmd[:2] == ["dumpe2fs", "-h"]:
                return f"Block count: {(100 * gib) // block_size}\nBlock size: {block_size}\n"
            if cmd[:2] == ["docker", "ps"]:
                return ""
            raise AssertionError(f"unexpected check_output command: {cmd}")

        os.environ["AXGT_REAL_STORAGE_TEST"] = "1"
        with patch.object(service, "_tool_path", side_effect=lambda name: name), \
             patch.object(service.os, "makedirs"), \
             patch.object(service.os.path, "exists", return_value=True), \
             patch.object(service.os.path, "getsize", return_value=200 * gib), \
             patch.object(service.subprocess, "check_output", side_effect=check_output), \
             patch.object(
                 service.subprocess,
                 "run",
                 return_value=MagicMock(returncode=4, stdout="manual repair required"),
             ), \
             patch.object(service.subprocess, "check_call") as mock_check_call:
            ok, error = service._ensure_persistent_storage_volume("user", 200)

        self.assertFalse(ok)
        self.assertIn("e2fsck could not safely prepare /dev/loop7 (exit 4)", error or "")
        calls = [entry.args[0] for entry in mock_check_call.call_args_list]
        self.assertIn(["losetup", "-c", "/dev/loop7"], calls)
        self.assertFalse(any(command[0] == "resize2fs" for command in calls))

    def test_service_fails_closed_when_ext4_size_is_unreadable(self) -> None:
        import session_launcher_service as service

        def check_output(cmd, **_kwargs):
            if cmd[:2] == ["losetup", "-j"]:
                return "/dev/loop7: []: (/storage/user.ext4)\n"
            if cmd[:2] == ["dumpe2fs", "-h"]:
                raise subprocess.CalledProcessError(1, cmd)
            raise AssertionError(f"unexpected check_output command: {cmd}")

        import subprocess

        os.environ["AXGT_REAL_STORAGE_TEST"] = "1"
        with patch.object(service, "_tool_path", side_effect=lambda name: name), \
             patch.object(service.os, "makedirs"), \
             patch.object(service.os.path, "exists", return_value=True), \
             patch.object(service.os.path, "getsize", return_value=200 * 1024**3), \
             patch.object(service.subprocess, "check_output", side_effect=check_output), \
             patch.object(service.subprocess, "check_call") as mock_check_call:
            ok, error = service._ensure_persistent_storage_volume("user", 200)

        self.assertFalse(ok)
        self.assertIn("Could not inspect volume", error or "")
        mock_check_call.assert_not_called()

    def test_service_inspects_existing_runtime_before_storage_side_effects(self) -> None:
        from session_launcher_service import launch

        source = inspect.getsource(launch)
        self.assertLess(
            source.index("_inspect_managed_container_contract("),
            source.index("_build_launch_cmd(payload)"),
        )

    def test_service_build_launch_cmd_enabled(self) -> None:
        os.environ["AXGT_HOST_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "true"
        os.environ["AXGT_HEARTBEAT_INTERVAL_SECONDS"] = "17"
        
        from session_launcher_service import _build_launch_cmd
        payload = {
            "session_id": 42,
            "wallet_address": "0xAbC123-xyz_!!",
            "requested_profile": "small",
            "assigned_gpu_ids": [0],
            "webrtc_agent_token": "signed-capability",
        }
        with patch("session_launcher_service._ensure_persistent_storage_volume", return_value=(True, None)):
            cmd, err = _build_launch_cmd(payload)
        self.assertIsNone(err)
        self.assertIsNotNone(cmd)
        
        # Check volume mount parameters
        self.assertIn("-v", cmd)
        self.assertIn("AXGT_HEARTBEAT_INTERVAL_SECONDS=17", cmd)
        idx = cmd.index("-v")
        self.assertEqual(cmd[idx + 1], "axgt-user-storage-0xabc123-xyz_:/home/aXonian")

    def test_service_build_launch_cmd_with_template(self) -> None:
        os.environ["AXGT_HOST_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "false"
        
        from session_launcher_service import _build_launch_cmd
        payload = {
            "session_id": 42,
            "wallet_address": "0xAbC123",
            "requested_profile": "small",
            "assigned_gpu_ids": [0],
            "requested_template": "  GROMACS  ",
            "webrtc_agent_token": "signed-capability",
        }
        cmd, err = _build_launch_cmd(payload)
        self.assertIsNone(err)
        self.assertIsNotNone(cmd)
        
        self.assertIn("-e", cmd)
        self.assertIn("AXONOS_SELECTED_TEMPLATE=gromacs", cmd)

    def test_service_rejects_unknown_template_before_volume_work(self) -> None:
        os.environ["AXGT_HOST_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "true"

        import session_launcher_service as service
        payload = {
            "session_id": 42,
            "wallet_address": "0xAbC123",
            "requested_profile": "small",
            "assigned_gpu_ids": [0],
            "requested_template": "not-deployed",
            "webrtc_agent_token": "signed-capability",
        }
        with patch.object(service, "_ensure_persistent_storage_volume") as ensure:
            cmd, err = service._build_launch_cmd(payload)
        self.assertIsNone(cmd)
        self.assertIn("unsupported requested_template", err)
        ensure.assert_not_called()

    def test_service_route_rejects_unknown_template_before_contract_inspection(self) -> None:
        import session_launcher_service as service

        service.app.testing = True
        payload = {
            "session_id": 42,
            "wallet_address": "0xAbC123",
            "requested_profile": "small",
            "assigned_gpu_ids": [0],
            "requested_template": "not-deployed",
            "webrtc_agent_token": "signed-capability",
        }
        with patch.object(service, "_require_token", return_value=None), \
             patch.object(service, "_configuration_errors", return_value=[]), \
             patch.object(service, "_launch_row_authorized") as authorized, \
             patch.object(service, "_inspect_managed_container_contract") as inspect_contract:
            response = service.app.test_client().post("/launch", json=payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("unsupported requested_template", response.get_json()["error"])
        authorized.assert_not_called()
        inspect_contract.assert_not_called()

    def test_service_build_launch_cmd_disabled(self) -> None:
        os.environ["AXGT_HOST_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "false"
        
        from session_launcher_service import _build_launch_cmd
        payload = {
            "session_id": 42,
            "wallet_address": "0xAbC123-xyz_!!",
            "requested_profile": "small",
            "assigned_gpu_ids": [0],
            "webrtc_agent_token": "signed-capability",
        }
        cmd, err = _build_launch_cmd(payload)
        self.assertIsNone(err)
        self.assertIsNotNone(cmd)
        
        self.assertNotIn("-v", cmd)

    def test_desktop_launch_requires_scoped_capability(self) -> None:
        os.environ["AXGT_HOST_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "false"

        from session_launcher_service import _build_launch_cmd

        cmd, err = _build_launch_cmd(
            {
                "session_id": 42,
                "wallet_address": "0xabc123",
                "requested_profile": "small",
                "assigned_gpu_ids": [0],
            }
        )
        self.assertIsNone(cmd)
        self.assertEqual(
            err,
            "webrtc_agent_token is required for a desktop session",
        )

    def test_malformed_direct_extra_args_fail_before_network_creation(self) -> None:
        os.environ.update(
            {
                "AXGT_SESSION_LAUNCHER_MODE": "docker_cli",
                "AXGT_SESSION_CONTAINER_IMAGE": "axonos:public-beta",
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_SESSION_NETWORK_ISOLATION": "true",
                "AXGT_SESSION_CONTAINER_EXTRA_ARGS": "'unterminated",
            }
        )
        import session_launcher

        with patch.object(
            session_launcher,
            "runtime_configuration_error",
            return_value=None,
        ), patch.object(
            session_launcher,
            "_ensure_session_network_direct",
        ) as ensure_network, patch.object(
            session_launcher.subprocess,
            "check_output",
        ) as check_output:
            ok, cid, err = session_launcher.launch_session(
                session_id=42,
                wallet="0xabc123",
                profile="small",
                gpu_ids=[0],
                files_key="files-key",
                webrtc_agent_token="signed-capability",
            )

        self.assertFalse(ok)
        self.assertIsNone(cid)
        self.assertIn("invalid AXGT_SESSION_CONTAINER_EXTRA_ARGS", err)
        ensure_network.assert_not_called()
        check_output.assert_not_called()

    def _http_env(self) -> None:
        os.environ["AXGT_USER_CONTAINER_ENABLED"] = "true"
        os.environ["AXGT_SESSION_LAUNCHER_MODE"] = "http"
        os.environ["AXGT_SESSION_LAUNCHER_URL"] = "http://launcher:8090"
        # Keep verify polling instant in tests.
        os.environ["AXGT_SESSION_LAUNCH_VERIFY_INTERVAL_SECONDS"] = "0"
        os.environ["AXGT_SESSION_LAUNCH_VERIFY_ATTEMPTS"] = "3"

    def test_session_claim_timeout_covers_launcher_and_verification_envelope(self) -> None:
        import session_launcher

        with patch.dict(
            os.environ,
            {
                "AXGT_SESSION_LAUNCHER_TIMEOUT_SECONDS": "180",
                "AXGT_SESSION_LAUNCH_VERIFY_ATTEMPTS": "4",
                "AXGT_SESSION_LAUNCH_VERIFY_INTERVAL_SECONDS": "3",
            },
            clear=False,
        ):
            # 180s launch + (4 * 5s) retries + (3 * 3s) sleeps + 15s headroom.
            self.assertEqual(session_launcher.session_claim_timeout_seconds(), 224)

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(session_launcher.session_claim_timeout_seconds(), 150)

    def test_launch_via_http_timeout_but_contract_retry_succeeds(self) -> None:
        """A timeout is recovered only by the host's idempotent contract check."""
        self._http_env()
        import session_launcher

        calls = []

        def fake_http(method, url, payload, timeout_s=None):
            calls.append((method, url, payload, timeout_s))
            if url.endswith("/launch"):
                if len(calls) == 1:
                    return 0, {}, "timed out"  # client-side timeout
                return 200, {"ok": True, "container_id": "abc123def456"}, None
            raise AssertionError("unexpected url " + url)

        with patch.object(session_launcher, "_http_json", side_effect=fake_http):
            ok, cid, err = session_launcher.launch_session(
                session_id=42, wallet="0xabc", profile="small", gpu_ids=[0],
                webrtc_agent_token="signed-capability",
            )
        self.assertTrue(ok)
        self.assertEqual(cid, "abc123def456")
        self.assertIsNone(err)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(method == "POST" for method, *_rest in calls))
        self.assertEqual(calls[0][2], calls[1][2])

    def test_launch_via_http_timeout_and_contract_never_confirms(self) -> None:
        """Repeated inconclusive host responses fail closed."""
        self._http_env()
        import session_launcher

        calls = []

        def fake_http(method, url, payload, timeout_s=None):
            calls.append((method, url))
            if url.endswith("/launch"):
                return 0, {}, "timed out"
            raise AssertionError("unexpected url " + url)

        with patch.object(session_launcher, "_http_json", side_effect=fake_http):
            ok, cid, err = session_launcher.launch_session(
                session_id=42, wallet="0xabc", profile="small", gpu_ids=[0],
                webrtc_agent_token="signed-capability",
            )
        self.assertFalse(ok)
        self.assertIsNone(cid)
        self.assertEqual(err, "timed out")
        self.assertEqual(len(calls), 4)  # initial request plus three bounded retries
        self.assertTrue(all(method == "POST" for method, _url in calls))

    def test_launch_via_http_5xx_never_accepts_name_only_container(self) -> None:
        """A mismatched same-name container cannot bypass host preflight/contract."""
        self._http_env()
        import session_launcher

        calls = []

        def fake_http(method, url, payload, timeout_s=None):
            calls.append((method, url))
            if url.endswith("/launch"):
                return 503, {"error": "legacy or runtime mismatch"}, None
            if url.endswith("/list-containers"):
                return 200, {"ok": True, "containers": [
                    {"name": "axgt-session-42", "short_id": "wrong-contract"}
                ]}, None
            raise AssertionError("unexpected url " + url)

        with patch.object(session_launcher, "_http_json", side_effect=fake_http):
            ok, cid, err = session_launcher.launch_session(
                session_id=42,
                wallet="0xabc",
                profile="small",
                gpu_ids=[0],
                webrtc_agent_token="signed-capability",
            )

        self.assertFalse(ok)
        self.assertIsNone(cid)
        self.assertEqual(err, "legacy or runtime mismatch")
        self.assertTrue(calls)
        self.assertTrue(all(url.endswith("/launch") for _method, url in calls))

    def test_launch_via_http_clean_success_skips_verify(self) -> None:
        self._http_env()
        import session_launcher
        calls = []

        def fake_http(method, url, payload, timeout_s=None):
            calls.append(url)
            if url.endswith("/launch"):
                return 200, {"ok": True, "container_id": "live123"}, None
            raise AssertionError("verify should not run on clean success")

        with patch.object(session_launcher, "_http_json", side_effect=fake_http):
            ok, cid, err = session_launcher.launch_session(
                session_id=7, wallet="0xabc", profile="small", gpu_ids=[0],
                webrtc_agent_token="signed-capability",
            )
        self.assertTrue(ok)
        self.assertEqual(cid, "live123")
        self.assertTrue(all(u.endswith("/launch") for u in calls))

    @patch("subprocess.check_output")
    def test_get_volume_size_kb(self, mock_check_output: MagicMock) -> None:
        mock_check_output.return_value = "102400\t/volume-data"
        from session_launcher_service import _get_volume_size_kb
        size = _get_volume_size_kb("axgt-user-storage-wallet")
        self.assertEqual(size, 102400.0)
        mock_check_output.assert_called_once_with(
            [
                "docker", "run", "--rm",
                "-v", "axgt-user-storage-wallet:/volume-data",
                "alpine", "du", "-s", "/volume-data"
            ],
            stderr=-2, # subprocess.STDOUT
            text=True,
            timeout=15
        )

    @patch("session_launcher_service._get_volume_size_kb")
    @patch("subprocess.check_output")
    @patch("subprocess.run")
    @patch("psycopg2.connect")
    def test_run_volume_cleanup_billing_and_pruning(
        self,
        mock_pg_connect: MagicMock,
        mock_sub_run: MagicMock,
        mock_check_output: MagicMock,
        mock_get_size: MagicMock
    ) -> None:
        # Mock env vars
        os.environ["AXGT_CHALLENGE_DB_URL"] = "postgresql://mock_db"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "true"
        os.environ["AXGT_PERSISTENT_STORAGE_GB_HOUR_COST_MINUTES"] = "0.05"
        os.environ["AXGT_PERSISTENT_STORAGE_CLEANUP_INTERVAL_SECONDS"] = "3600"
        os.environ["AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES"] = "-1440.0"

        # Mock docker volume listing output
        mock_check_output.return_value = "axgt-user-storage-0xabc123\naxgt-user-storage-0xexpired"

        # Mock volume size to 20 GB (20 * 1024 * 1024 KB)
        # 20 GB * 0.05 minutes/GB-hour * 1 hour = 1.0 minutes charge
        mock_get_size.return_value = 20.0 * 1024.0 * 1024.0

        # Mock Database queries
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_pg_connect.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        import time
        now = time.time()
        # Mock psycopg2 fetchall responses:
        # User 0xabc123 has positive balance (5.0), User 0xexpired has debt exceeding threshold (-1500.0)
        mock_cur.fetchall.side_effect = [
            [("0xabc123", 5.0, now), ("0xexpired", -1500.0, now)],
            [("0xabc123", 5.0, now), ("0xexpired", -1500.0, now)]
        ]

        from session_launcher_service import _run_volume_cleanup
        _run_volume_cleanup()

        # Verify psycopg2 connection was committed
        mock_conn.commit.assert_called()

        # Checking if UPDATE query and INSERT query were executed for the charged user
        calls = mock_cur.execute.call_args_list
        db_updates = [c[0][0] for c in calls if "UPDATE axgt_deposits" in c[0][0]]
        ledger_inserts = [c[0][0] for c in calls if "INSERT INTO axgt_ledger" in c[0][0]]
        self.assertEqual(len(db_updates), 1)
        self.assertEqual(len(ledger_inserts), 1)

        # Checking if volume prune was called for 0xexpired
        mock_sub_run.assert_any_call(
            ["docker", "volume", "rm", "axgt-user-storage-0xexpired"],
            stdout=-1, # subprocess.PIPE
            stderr=-1, # subprocess.PIPE
            text=True
        )


if __name__ == "__main__":
    unittest.main()
