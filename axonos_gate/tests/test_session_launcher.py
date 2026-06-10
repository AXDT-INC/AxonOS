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
    def test_launch_via_docker_cli_with_template(self, mock_check_output: MagicMock) -> None:
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
            gpu_ids=[0],
            template="pytorch"
        )
        
        self.assertTrue(ok)
        mock_check_output.assert_called_once()
        cmd = mock_check_output.call_args[0][0]
        
        # Verify requested template is passed as environment variable
        self.assertIn("-e", cmd)
        self.assertIn("AXONOS_SELECTED_TEMPLATE=pytorch", cmd)

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

    def test_service_build_launch_cmd_with_template(self) -> None:
        os.environ["AXGT_HOST_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "false"
        
        from session_launcher_service import _build_launch_cmd
        payload = {
            "session_id": 42,
            "wallet_address": "0xAbC123",
            "requested_profile": "small",
            "assigned_gpu_ids": [0],
            "requested_template": "gromacs",
        }
        cmd, err = _build_launch_cmd(payload)
        self.assertIsNone(err)
        self.assertIsNotNone(cmd)
        
        self.assertIn("-e", cmd)
        self.assertIn("AXONOS_SELECTED_TEMPLATE=gromacs", cmd)

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
