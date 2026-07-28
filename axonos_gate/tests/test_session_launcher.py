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

    @patch("subprocess.check_output")
    def test_launch_via_docker_cli_enabled(self, mock_check_output: MagicMock) -> None:
        mock_check_output.side_effect = ["", "container_id_123"]
        os.environ["AXGT_SESSION_LAUNCHER_MODE"] = "docker_cli"
        os.environ["AXGT_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_USER_CONTAINER_ENABLED"] = "true"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "true"
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
                template="pytorch",
                webrtc_agent_token="signed-capability",
            )
        
        self.assertTrue(ok)
        self.assertEqual(mock_check_output.call_count, 2)
        cmd = mock_check_output.call_args_list[-1].args[0]
        
        # Verify requested template is passed as environment variable
        self.assertIn("-e", cmd)
        self.assertIn("AXONOS_SELECTED_TEMPLATE=pytorch", cmd)

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

    def test_service_build_launch_cmd_enabled(self) -> None:
        os.environ["AXGT_HOST_SESSION_CONTAINER_IMAGE"] = "axonos:public-beta"
        os.environ["AXGT_PERSISTENT_STORAGE_ENABLED"] = "true"
        
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
            "webrtc_agent_token": "signed-capability",
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

    def test_lifecycle_shared_fail_closed_and_noop_succeeds(self) -> None:
        import session_launcher

        with patch.dict(
            os.environ,
            {"AXGT_USER_CONTAINER_ENABLED": "false"},
            clear=False,
        ):
            self.assertFalse(session_launcher.stop_session(42, "shared-desktop"))
            self.assertFalse(session_launcher.pause_session(42, "shared-desktop"))
            self.assertFalse(session_launcher.resume_session(42, "shared-desktop"))

        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_SESSION_LAUNCHER_MODE": "noop",
            },
            clear=False,
        ):
            self.assertTrue(session_launcher.stop_session(42, "ignored"))
            self.assertTrue(session_launcher.pause_session(42, "ignored"))
            self.assertTrue(session_launcher.resume_session(42, "ignored"))

    def test_pause_resume_http_forward_to_authenticated_host_contract(self) -> None:
        self._http_env()
        import session_launcher

        calls = []

        def fake_http(method, url, payload, timeout_s=None):
            calls.append((method, url, payload, timeout_s))
            return 200, {
                "ok": True,
                "paused": url.endswith("/pause"),
                "transition_token": payload["transition_token"],
            }, None

        with patch.object(session_launcher, "_http_json", side_effect=fake_http):
            self.assertTrue(session_launcher.pause_session(42, "claimed-id", "pause-generation"))
            self.assertTrue(session_launcher.resume_session(42, "claimed-id", "resume-generation"))

        self.assertEqual(
            [(method, url) for method, url, _payload, _timeout in calls],
            [
                ("POST", "http://launcher:8090/pause"),
                ("POST", "http://launcher:8090/resume"),
            ],
        )
        self.assertEqual(
            [payload for _method, _url, payload, _timeout in calls],
            [
                {
                    "session_id": 42,
                    "container_id": "claimed-id",
                    "transition_token": "pause-generation",
                },
                {
                    "session_id": 42,
                    "container_id": "claimed-id",
                    "transition_token": "resume-generation",
                },
            ],
        )

    def test_pause_via_http_timeout_then_verified_retry_succeeds(self) -> None:
        self._http_env()
        import session_launcher

        calls = []

        def fake_http(method, url, payload, timeout_s=None):
            calls.append((method, url, payload, timeout_s))
            if len(calls) == 1:
                return 0, {}, "timed out"
            return 200, {
                "ok": True,
                "paused": True,
                "transition_token": payload["transition_token"],
            }, None

        with patch.object(session_launcher, "_http_json", side_effect=fake_http):
            self.assertTrue(
                session_launcher.pause_session(
                    42, "claimed-id", "pause-generation"
                )
            )

        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0][3])
        self.assertEqual(calls[1][3], 5.0)
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertTrue(all(url.endswith("/pause") for _method, url, _payload, _timeout in calls))

    def test_resume_via_http_persistent_transport_failure_is_bounded(self) -> None:
        self._http_env()
        import session_launcher

        calls = []

        def fake_http(method, url, payload, timeout_s=None):
            calls.append((method, url, payload, timeout_s))
            return 0, {}, "connection reset"

        with patch.object(session_launcher, "_http_json", side_effect=fake_http):
            self.assertFalse(
                session_launcher.resume_session(
                    42, "claimed-id", "resume-generation"
                )
            )

        self.assertEqual(len(calls), 4)  # initial request plus three bounded retries
        self.assertIsNone(calls[0][3])
        self.assertTrue(all(call[3] == 5.0 for call in calls[1:]))
        self.assertTrue(all(url.endswith("/resume") for _method, url, _payload, _timeout in calls))

    def test_pause_via_http_rejects_success_without_desired_state(self) -> None:
        self._http_env()
        import session_launcher

        with patch.object(
            session_launcher,
            "_http_json",
            return_value=(
                200,
                {
                    "ok": True,
                    "paused": False,
                    "transition_token": "pause-generation",
                },
                None,
            ),
        ) as http:
            self.assertFalse(
                session_launcher.pause_session(
                    42, "claimed-id", "pause-generation"
                )
            )

        http.assert_called_once()

    def test_pause_via_http_rejects_unfenced_legacy_host_success(self) -> None:
        self._http_env()
        import session_launcher

        with patch.object(
            session_launcher,
            "_http_json",
            return_value=(200, {"ok": True, "paused": True}, None),
        ) as http:
            self.assertFalse(
                session_launcher.pause_session(
                    42,
                    "claimed-id",
                    "pause-generation",
                )
            )

        http.assert_called_once()

    def test_direct_pause_resume_use_verified_managed_id_and_verify_state(self) -> None:
        import session_launcher

        environment = {
            "AXGT_USER_CONTAINER_ENABLED": "true",
            "AXGT_SESSION_LAUNCHER_MODE": "docker_cli",
        }
        pause_inspections = [
            ("owned_running", "managed-id", ""),
            ("owned_paused", "managed-id", ""),
        ]
        resume_inspections = [
            ("owned_paused", "managed-id", ""),
            ("owned_running", "managed-id", ""),
        ]
        lifecycle_conn = MagicMock()
        lifecycle_cur = MagicMock()
        lifecycle_cur.__enter__.return_value = lifecycle_cur
        lifecycle_cur.fetchone.side_effect = [
            ("pause-generation",),
            ("resume-generation",),
        ]
        lifecycle_conn.cursor.return_value = lifecycle_cur
        with patch.dict(os.environ, environment, clear=False), patch.object(
            session_launcher,
            "_inspect_managed_container_pause_state_direct",
            side_effect=pause_inspections + resume_inspections,
        ), patch.object(
            session_launcher,
            "_get_control_db_connection",
            return_value=lifecycle_conn,
        ), patch.object(session_launcher.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(
                session_launcher.pause_session(
                    42, "axonos_postgres", "pause-generation"
                )
            )
            self.assertTrue(
                session_launcher.resume_session(
                    42, "axonos_postgres", "resume-generation"
                )
            )

        self.assertEqual(run.call_args_list[0].args[0], ["docker", "pause", "managed-id"])
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "unpause", "managed-id"])

    def test_direct_pause_is_idempotent_only_for_verified_managed_state(self) -> None:
        import session_launcher

        environment = {
            "AXGT_USER_CONTAINER_ENABLED": "true",
            "AXGT_SESSION_LAUNCHER_MODE": "docker_cli",
        }
        lifecycle_conn = MagicMock()
        lifecycle_cur = MagicMock()
        lifecycle_cur.__enter__.return_value = lifecycle_cur
        lifecycle_cur.fetchone.return_value = ("pause-generation",)
        lifecycle_conn.cursor.return_value = lifecycle_cur
        with patch.dict(os.environ, environment, clear=False), patch.object(
            session_launcher,
            "_inspect_managed_container_pause_state_direct",
            return_value=("owned_paused", "managed-id", ""),
        ), patch.object(
            session_launcher,
            "_get_control_db_connection",
            return_value=lifecycle_conn,
        ), patch.object(session_launcher.subprocess, "run") as run:
            self.assertTrue(
                session_launcher.pause_session(
                    42, "untrusted-id", "pause-generation"
                )
            )
            run.assert_not_called()

        with patch.dict(os.environ, environment, clear=False), patch.object(
            session_launcher,
            "_inspect_managed_container_pause_state_direct",
            return_value=("unmanaged", "untrusted-id", "unowned"),
        ), patch.object(
            session_launcher,
            "_get_control_db_connection",
            return_value=lifecycle_conn,
        ), patch.object(session_launcher.subprocess, "run") as run:
            self.assertFalse(
                session_launcher.pause_session(
                    42, "untrusted-id", "pause-generation"
                )
            )
            run.assert_not_called()

    def test_direct_stale_generation_is_rejected_before_docker(self) -> None:
        import session_launcher

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = ("new-generation",)
        conn.cursor.return_value = cur
        environment = {
            "AXGT_USER_CONTAINER_ENABLED": "true",
            "AXGT_SESSION_LAUNCHER_MODE": "docker_cli",
        }
        with patch.dict(os.environ, environment, clear=False), patch.object(
            session_launcher,
            "_get_control_db_connection",
            return_value=conn,
        ), patch.object(
            session_launcher,
            "_inspect_managed_container_pause_state_direct",
        ) as inspect_state, patch.object(
            session_launcher.subprocess,
            "run",
        ) as run:
            result = session_launcher.resume_session(
                42,
                "claimed-id",
                "stale-generation",
            )

        self.assertFalse(result)
        inspect_state.assert_not_called()
        run.assert_not_called()
        conn.rollback.assert_called_once()
        sql, params = cur.execute.call_args.args
        self.assertIn("FOR UPDATE", sql)
        self.assertEqual(params, (42, "resuming"))

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
