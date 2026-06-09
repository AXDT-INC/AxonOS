"""Unit tests for session launcher and service persistent storage logic."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)

# Mock flask for environments without it installed
from unittest.mock import MagicMock
sys.modules['flask'] = MagicMock()



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

    @patch("subprocess.check_output")
    def test_launch_via_docker_cli_enabled(self, mock_check_output: MagicMock) -> None:
        mock_check_output.return_value = "container_id_123"
        os.environ["AXGT_SESSION_LAUNCHER_MODE"] = "docker_cli"
        os.environ["AXGT_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_USER_CONTAINER_ENABLED"] = "true"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "true"
        
        from session_launcher import launch_session
        ok, cid, err = launch_session(
            session_id=42,
            wallet="0xAbC123-xyz_!!",
            profile="small",
            gpu_ids=[0]
        )
        
        self.assertTrue(ok)
        self.assertEqual(cid, "container_id_123")
        self.assertIsNone(err)
        
        mock_check_output.assert_called_once()
        cmd = mock_check_output.call_args[0][0]
        
        # Check volume mount parameters
        # Sanitized wallet should be 0xabc123-xyz_
        self.assertIn("-v", cmd)
        idx = cmd.index("-v")
        self.assertEqual(cmd[idx + 1], "axgt-user-storage-0xabc123-xyz_:/home/aXonian")

    @patch("subprocess.check_output")
    def test_launch_via_docker_cli_disabled(self, mock_check_output: MagicMock) -> None:
        mock_check_output.return_value = "container_id_123"
        os.environ["AXGT_SESSION_LAUNCHER_MODE"] = "docker_cli"
        os.environ["AXGT_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_USER_CONTAINER_ENABLED"] = "true"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "false"
        
        from session_launcher import launch_session
        ok, cid, err = launch_session(
            session_id=42,
            wallet="0xAbC123",
            profile="small",
            gpu_ids=[0]
        )
        
        self.assertTrue(ok)
        mock_check_output.assert_called_once()
        cmd = mock_check_output.call_args[0][0]
        
        self.assertNotIn("-v", cmd)

    def test_service_build_launch_cmd_enabled(self) -> None:
        os.environ["AXGT_HOST_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "true"
        
        from session_launcher_service import _build_launch_cmd
        payload = {
            "session_id": 42,
            "wallet_address": "0xAbC123-xyz_!!",
            "requested_profile": "small",
            "assigned_gpu_ids": [0],
        }
        cmd, err = _build_launch_cmd(payload)
        self.assertIsNone(err)
        self.assertIsNotNone(cmd)
        
        # Check volume mount parameters
        self.assertIn("-v", cmd)
        idx = cmd.index("-v")
        self.assertEqual(cmd[idx + 1], "axgt-user-storage-0xabc123-xyz_:/home/aXonian")

    def test_service_build_launch_cmd_disabled(self) -> None:
        os.environ["AXGT_HOST_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "false"
        
        from session_launcher_service import _build_launch_cmd
        payload = {
            "session_id": 42,
            "wallet_address": "0xAbC123-xyz_!!",
            "requested_profile": "small",
            "assigned_gpu_ids": [0],
        }
        cmd, err = _build_launch_cmd(payload)
        self.assertIsNone(err)
        self.assertIsNotNone(cmd)
        
        self.assertNotIn("-v", cmd)


if __name__ == "__main__":
    unittest.main()
