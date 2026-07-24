"""Regression guards for tenant-session credential and network isolation."""

from __future__ import annotations

import configparser
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch


_TESTS_DIR = Path(__file__).resolve().parent
_GATE_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _GATE_ROOT.parent
if str(_GATE_ROOT) not in sys.path:
    sys.path.insert(0, str(_GATE_ROOT))


def _docker_env_assignments(command: list[str]) -> list[str]:
    """Return the values of every ``docker run -e NAME=value`` pair."""
    return [
        command[index + 1]
        for index, token in enumerate(command[:-1])
        if token == "-e"
    ]


class SessionLauncherCredentialBoundaryTests(unittest.TestCase):
    def test_host_launcher_authorizes_exact_live_allocation_with_bounded_db_connect(self) -> None:
        import session_launcher_service as launcher

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = ("0,2", "session-files-key", False, "small")
        conn.cursor.return_value = cur
        payload = {
            "session_id": 37,
            "wallet_address": "0xAbC123",
            "assigned_gpu_ids": [2, 0],
            "files_key": "session-files-key",
            "ssh_enabled": False,
        }
        environment = {
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "true",
            "AXGT_CHALLENGE_DB_URL": "postgresql://control-db/axonos",
            "AXGT_SESSION_DB_CONNECT_TIMEOUT_SECONDS": "7",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "psycopg2.connect",
            return_value=conn,
        ) as connect:
            allowed, error = launcher._launch_row_authorized(payload)

        self.assertTrue(allowed)
        self.assertEqual(error, "")
        connect.assert_called_once_with(
            "postgresql://control-db/axonos",
            connect_timeout=7,
        )
        sql, params = cur.execute.call_args.args
        normalized = " ".join(sql.split()).lower()
        self.assertIn("id = %s", normalized)
        self.assertIn("wallet_address = %s", normalized)
        self.assertIn("status = 'active'", normalized)
        self.assertIn("allocation_status in ('allocating', 'allocated')", normalized)
        self.assertEqual(params[0:2], (37, "0xabc123"))

        cur.fetchone.return_value = ("0,1", "session-files-key", False, "small")
        with patch.dict(os.environ, environment, clear=True), patch(
            "psycopg2.connect",
            return_value=conn,
        ):
            allowed, error = launcher._launch_row_authorized(payload)
        self.assertFalse(allowed)
        self.assertEqual(error, "launch allocation identity mismatch")

    def test_shared_network_mode_still_requires_exact_database_authorization(self) -> None:
        import session_launcher_service as launcher

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.return_value = ("0", "session-files-key", False, "small")
        conn.cursor.return_value = cur
        payload = {
            "session_id": 37,
            "wallet_address": "0xabc123",
            "assigned_gpu_ids": [0],
            "files_key": "session-files-key",
            "ssh_enabled": False,
        }
        environment = {
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "false",
            "AXGT_HOST_SESSION_CONTAINER_NETWORK": "axonos_stack",
            "AXGT_CHALLENGE_DB_URL": "postgresql://control-db/axonos",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "psycopg2.connect",
            return_value=conn,
        ) as connect:
            allowed, error = launcher._launch_row_authorized(payload)

        self.assertTrue(allowed, error)
        connect.assert_called_once()
        sql = " ".join(cur.execute.call_args.args[0].split()).lower()
        self.assertIn("id = %s", sql)
        self.assertIn("wallet_address = %s", sql)
        self.assertIn("status = 'active'", sql)

    def test_host_launcher_duplicate_launch_reuses_managed_container(self) -> None:
        import session_launcher_service as launcher

        payload = {
            "session_id": 37,
            "wallet_address": "0xabc123",
            "assigned_gpu_ids": [0],
            "files_key": "session-files-key",
            "webrtc_agent_token": "signed-capability",
        }
        environment = {
            "AXGT_HOST_SESSION_CONTAINER_IMAGE": "axonos:public-beta",
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "true",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            launcher,
            "_configuration_errors",
            return_value=[],
        ), patch.object(
            launcher,
            "_launch_row_authorized",
            return_value=(True, ""),
        ), patch.object(
            launcher,
            "_inspect_managed_container_contract",
            return_value=("match_running", "a" * 64, ""),
        ), patch.object(
            launcher,
            "_ensure_session_network",
            return_value=(True, ""),
        ) as ensure_network, patch.object(launcher, "_run_cmd") as run_cmd:
            response = launcher.app.test_client().post("/launch", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["reused"])
        ensure_network.assert_called_once_with(
            37,
            allow_tenant=True,
            expected_tenant_id="a" * 64,
        )
        run_cmd.assert_not_called()

    def test_stop_ignores_arbitrary_container_id_and_removes_only_labeled_owner(self) -> None:
        import session_launcher as direct
        import session_launcher_service as launcher

        with patch.dict(os.environ, {}, clear=True), patch.object(
            launcher,
            "_inspect_managed_container_ownership",
            return_value=("owned_running", "owned-container-id", ""),
        ), patch.object(launcher.subprocess, "run") as host_run, patch.object(
            launcher,
            "_cleanup_session_network",
            return_value=(True, ""),
        ):
            host_run.return_value.returncode = 0
            response = launcher.app.test_client().post(
                "/stop",
                json={"session_id": 37, "container_id": "axonos_postgres"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            host_run.call_args.args[0],
            ["docker", "rm", "-f", "owned-container-id"],
        )
        self.assertNotIn("axonos_postgres", host_run.call_args.args[0])

        with patch.object(
            direct,
            "_inspect_managed_container_ownership_direct",
            return_value=("owned_running", "direct-owned-id", ""),
        ), patch.object(direct.subprocess, "run") as direct_run, patch.object(
            direct,
            "_cleanup_session_network_direct",
            return_value=(True, None),
        ):
            direct_run.return_value.returncode = 0
            direct._stop_via_docker_cli(37, "axonos_postgres")
        self.assertEqual(
            direct_run.call_args.args[0],
            ["docker", "rm", "-f", "direct-owned-id"],
        )

    def test_direct_duplicate_launch_reuses_owned_container_without_docker_run(self) -> None:
        import session_launcher as launcher

        environment = {
            "AXGT_SESSION_LAUNCHER_MODE": "docker_cli",
            "AXGT_SESSION_CONTAINER_IMAGE": "axonos:public-beta",
            "AXGT_USER_CONTAINER_ENABLED": "true",
            "AXGT_SESSION_NETWORK_ISOLATION": "true",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            launcher,
            "runtime_configuration_error",
            return_value=None,
        ), patch.object(
            launcher,
            "_inspect_managed_container_contract_direct",
            return_value=("match_running", "b" * 64, ""),
        ), patch.object(
            launcher,
            "_ensure_session_network_direct",
            return_value=(True, None),
        ) as ensure_network, patch.object(
            launcher.subprocess,
            "check_output",
        ) as check_output:
            result = launcher.launch_session(
                session_id=64,
                wallet="0xabc",
                profile="small",
                gpu_ids=[0],
                files_key="session-files-key",
                webrtc_agent_token="signed-session-capability",
            )

        self.assertEqual(result, (True, "b" * 64, None))
        ensure_network.assert_called_once_with(
            64,
            allow_tenant=True,
            expected_tenant_id="b" * 64,
        )
        check_output.assert_not_called()

    def test_launcher_health_fails_when_isolation_cannot_reconcile(self) -> None:
        import session_launcher_service as launcher

        base = {
            "AXGT_HOST_SESSION_CONTAINER_IMAGE": "axonos:public-beta",
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "true",
        }
        with patch.dict(os.environ, base, clear=True), patch.object(
            launcher,
            "_unmanaged_session_container_names",
            return_value=[],
        ):
            response = launcher.app.test_client().get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertIn("AXGT_CHALLENGE_DB_URL", " ".join(response.get_json()["errors"]))

        with patch.dict(
            os.environ,
            {**base, "AXGT_CHALLENGE_DB_URL": "postgresql://control-db/axonos"},
            clear=True,
        ), patch.object(
            launcher,
            "_unmanaged_session_container_names",
            return_value=[],
        ):
            response = launcher.app.test_client().get("/healthz")
        self.assertEqual(response.status_code, 200)

        with patch.dict(
            os.environ,
            {**base, "AXGT_CHALLENGE_DB_URL": "postgresql://control-db/axonos"},
            clear=True,
        ), patch.object(
            launcher,
            "_unmanaged_session_container_names",
            return_value=["axgt-session-45"],
        ):
            response = launcher.app.test_client().get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertIn("axgt-session-45", " ".join(response.get_json()["errors"]))

    def test_http_legacy_container_preflight_uses_docker_label_template(self) -> None:
        import session_launcher_service as launcher

        with patch.object(
            launcher,
            "_run_cmd",
            return_value=(
                True,
                f"axgt-session-37|true|37|{'a' * 64}\n"
                "axgt-session-38|||\n"
                f"axgt-session-39|true|999|{'b' * 64}\n",
            ),
        ) as run_cmd:
            unmanaged = launcher._unmanaged_session_container_names()

        self.assertEqual(unmanaged, ["axgt-session-38", "axgt-session-39"])
        command = run_cmd.call_args.args[0]
        template = command[command.index("--format") + 1]
        self.assertIn('.Label "com.axonos.session-container"', template)
        self.assertIn('.Label "com.axonos.session-id"', template)
        self.assertIn('.Label "com.axonos.session-config-sha256"', template)
        self.assertNotIn("index .Labels", template)

    def test_http_launch_rechecks_fail_closed_preflight(self) -> None:
        import session_launcher_service as launcher

        with patch.dict(os.environ, {}, clear=True), patch.object(
            launcher,
            "_configuration_errors",
            return_value=["legacy session container present"],
        ), patch.object(launcher, "_launch_row_authorized") as authorize:
            response = launcher.app.test_client().post(
                "/launch",
                json={
                    "session_id": 37,
                    "wallet_address": "0xabc",
                    "assigned_gpu_ids": [0],
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("legacy session container", " ".join(response.get_json()["errors"]))
        authorize.assert_not_called()

    def test_direct_legacy_container_preflight_is_fail_closed(self) -> None:
        import session_launcher as launcher

        environment = {
            "AXGT_USER_CONTAINER_ENABLED": "true",
            "AXGT_SESSION_LAUNCHER_MODE": "docker_cli",
            "AXGT_SESSION_NETWORK_ISOLATION": "true",
        }
        output = (
            f"axgt-session-37|true|37|{'a' * 64}\n"
            "axgt-session-38|||\n"
            f"axgt-session-39|true|999|{'b' * 64}\n"
        )
        with patch.dict(os.environ, environment, clear=True), patch.object(
            launcher.subprocess,
            "check_output",
            return_value=output,
        ) as check_output:
            error = launcher.runtime_configuration_error()

        self.assertIn("axgt-session-38", error or "")
        self.assertIn("axgt-session-39", error or "")
        command = check_output.call_args.args[0]
        template = command[command.index("--format") + 1]
        self.assertIn('.Label "com.axonos.session-container"', template)
        self.assertIn('.Label "com.axonos.session-config-sha256"', template)
        self.assertNotIn("index .Labels", template)

    def test_reuse_requires_exact_runtime_digest_and_network_membership(self) -> None:
        import session_launcher as direct
        import session_launcher_service as host

        digest = "a" * 64
        exact = (
            f'true|true|37|{digest}|{{"axgt-session-net-37":{{}}}}|'
            f'{"c" * 64}'
        )
        shared_too = (
            f'true|true|37|{digest}|'
            f'{{"axgt-session-net-37":{{}},"axonos_stack":{{}}}}|{"c" * 64}'
        )

        with patch.object(host, "_run_cmd", return_value=(True, exact)):
            self.assertTrue(
                host._managed_container_runtime_matches(
                    37,
                    digest,
                    "axgt-session-net-37",
                )
            )
        with patch.object(host, "_run_cmd", return_value=(True, shared_too)):
            self.assertFalse(
                host._managed_container_runtime_matches(
                    37,
                    digest,
                    "axgt-session-net-37",
                )
            )

        with patch.object(direct, "_run_docker_direct", return_value=(True, exact)):
            self.assertTrue(
                direct._managed_container_runtime_matches_direct(
                    37,
                    digest,
                    "axgt-session-net-37",
                )
            )
        with patch.object(
            direct,
            "_run_docker_direct",
            return_value=(
                True,
                f'true|true|37|{"b" * 64}|'
                f'{{"axgt-session-net-37":{{}}}}|{"c" * 64}',
            ),
        ):
            self.assertFalse(
                direct._managed_container_runtime_matches_direct(
                    37,
                    digest,
                    "axgt-session-net-37",
                )
            )

    def test_container_inspection_uncertainty_never_mutates_runtime(self) -> None:
        import session_launcher as direct
        import session_launcher_service as host

        payload = {
            "session_id": 37,
            "wallet_address": "0xabc123",
            "assigned_gpu_ids": [0],
            "files_key": "session-files-key",
            "webrtc_agent_token": "signed-capability",
        }
        host_environment = {
            "AXGT_HOST_SESSION_CONTAINER_IMAGE": "axonos:public-beta",
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "true",
        }
        with patch.dict(os.environ, host_environment, clear=True), patch.object(
            host, "_configuration_errors", return_value=[]
        ), patch.object(
            host, "_launch_row_authorized", return_value=(True, "")
        ), patch.object(
            host,
            "_inspect_managed_container_contract",
            return_value=("error", None, "docker context prod: context not found"),
        ), patch.object(host.subprocess, "run") as remove, patch.object(
            host, "_cleanup_session_network"
        ) as cleanup, patch.object(host, "_ensure_session_network") as ensure, patch.object(
            host, "_run_cmd"
        ) as run_cmd:
            response = host.app.test_client().post("/launch", json=payload)

        self.assertEqual(response.status_code, 503)
        remove.assert_not_called()
        cleanup.assert_not_called()
        ensure.assert_not_called()
        run_cmd.assert_not_called()

        direct_environment = {
            "AXGT_SESSION_CONTAINER_IMAGE": "axonos:public-beta",
            "AXGT_SESSION_NETWORK_ISOLATION": "true",
            "AXGT_USER_CONTAINER_ENABLED": "true",
        }
        with patch.dict(os.environ, direct_environment, clear=True), patch.object(
            direct, "runtime_configuration_error", return_value=None
        ), patch.object(
            direct,
            "_inspect_managed_container_contract_direct",
            return_value=("error", None, "malformed inspect output"),
        ), patch.object(direct.subprocess, "run") as remove, patch.object(
            direct, "_cleanup_session_network_direct"
        ) as cleanup, patch.object(
            direct, "_ensure_session_network_direct"
        ) as ensure, patch.object(direct.subprocess, "check_output") as docker_run:
            result = direct.launch_session(
                session_id=37,
                wallet="0xabc123",
                profile="small",
                gpu_ids=[0],
                files_key="session-files-key",
                webrtc_agent_token="signed-capability",
            )

        self.assertFalse(result[0])
        remove.assert_not_called()
        cleanup.assert_not_called()
        ensure.assert_not_called()
        docker_run.assert_not_called()

    def test_generic_not_found_is_inspection_error_not_absence(self) -> None:
        import session_launcher as direct
        import session_launcher_service as host

        with patch.object(
            host,
            "_run_cmd",
            return_value=(False, 'docker context "prod": context not found'),
        ):
            state, container_id, _error = host._inspect_managed_container_contract(
                37, "a" * 64, "axgt-session-net-37"
            )
        self.assertEqual((state, container_id), ("error", None))

        with patch.object(
            direct,
            "_run_docker_direct",
            return_value=(False, 'docker context "prod": context not found'),
        ):
            state, container_id, _error = direct._inspect_managed_container_contract_direct(
                37, "a" * 64, "axgt-session-net-37"
            )
        self.assertEqual((state, container_id), ("error", None))

    def test_network_cleanup_failure_blocks_replacement_launch(self) -> None:
        import session_launcher_service as launcher

        payload = {
            "session_id": 37,
            "wallet_address": "0xabc123",
            "assigned_gpu_ids": [0],
            "files_key": "session-files-key",
            "webrtc_agent_token": "signed-capability",
        }
        environment = {
            "AXGT_HOST_SESSION_CONTAINER_IMAGE": "axonos:public-beta",
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "true",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            launcher, "_configuration_errors", return_value=[]
        ), patch.object(
            launcher, "_launch_row_authorized", return_value=(True, "")
        ), patch.object(
            launcher,
            "_inspect_managed_container_contract",
            return_value=("mismatch", "owned-container-id", "contract mismatch"),
        ), patch.object(launcher.subprocess, "run") as remove, patch.object(
            launcher,
            "_cleanup_session_network",
            return_value=(False, "network still has an endpoint"),
        ), patch.object(launcher, "_ensure_session_network") as ensure, patch.object(
            launcher, "_run_cmd"
        ) as docker_run:
            remove.return_value.returncode = 0
            response = launcher.app.test_client().post("/launch", json=payload)

        self.assertEqual(response.status_code, 500)
        remove.assert_called_once()
        ensure.assert_not_called()
        docker_run.assert_not_called()

    def test_network_cleanup_rejects_unexpected_endpoint_and_rm_failure(self) -> None:
        import session_launcher_service as launcher

        with patch.object(
            launcher,
            "_run_cmd",
            side_effect=(
                (
                    True,
                    'true|37|{"central":{"Name":"axonos"},'
                    '"other":{"Name":"unrelated-service"}}',
                ),
                (True, "central"),
                (True, ""),
            ),
        ) as run_cmd:
            ok, error = launcher._cleanup_session_network(37)
        self.assertFalse(ok)
        self.assertIn("unexpected endpoint", error)
        self.assertEqual(run_cmd.call_count, 3)
        self.assertEqual(
            run_cmd.call_args_list[2].args[0],
            [
                "docker",
                "network",
                "disconnect",
                "-f",
                "axgt-session-net-37",
                "axonos",
            ],
        )

        with patch.object(
            launcher,
            "_run_cmd",
            side_effect=(
                (True, 'true|37|{"central":{"Name":"axonos"}}'),
                (True, "central"),
                (True, ""),
                (False, "network has active endpoints"),
            ),
        ):
            ok, error = launcher._cleanup_session_network(37)
        self.assertFalse(ok)
        self.assertIn("active endpoints", error)

    def test_reuse_requires_tenant_endpoint_on_private_network(self) -> None:
        import session_launcher_service as launcher

        central_only = 'true|37|{"central":{"Name":"axonos"}}'
        with patch.dict(
            os.environ,
            {"AXGT_HOST_SESSION_NETWORK_ISOLATION": "true"},
            clear=True,
        ), patch.object(
            launcher,
            "_run_cmd",
            side_effect=((True, central_only), (False, "already connected"), (True, central_only)),
        ):
            ok, error = launcher._ensure_session_network(
                37,
                allow_tenant=True,
                expected_tenant_id="a" * 64,
            )
        self.assertFalse(ok)
        self.assertIn("missing", error)

    def test_network_reuse_requires_exact_tenant_endpoint_id(self) -> None:
        import session_launcher_service as launcher

        actual_id = "b" * 64
        expected_id = "a" * 64
        endpoints = (
            'true|37|{"central":{"Name":"axonos"},'
            f'"{actual_id}":{{"Name":"axgt-session-37"}}}}'
        )
        with patch.dict(
            os.environ,
            {"AXGT_HOST_SESSION_NETWORK_ISOLATION": "true"},
            clear=True,
        ), patch.object(
            launcher,
            "_run_cmd",
            return_value=(True, endpoints),
        ) as run_cmd:
            ok, error = launcher._ensure_session_network(
                37,
                allow_tenant=True,
                expected_tenant_id=expected_id,
            )

        self.assertFalse(ok)
        self.assertIn("identity mismatch", error)
        self.assertEqual(run_cmd.call_count, 1)

    def test_reconciliation_revokes_central_access_for_untrusted_tenant(self) -> None:
        import session_launcher as direct
        import session_launcher_service as host

        def authorized_connection() -> tuple[MagicMock, MagicMock]:
            connection = MagicMock()
            cursor = MagicMock()
            cursor.__enter__.return_value = cursor
            cursor.fetchall.return_value = [(37,)]
            connection.cursor.return_value = cursor
            return connection, cursor

        host_connection, host_cursor = authorized_connection()
        host_environment = {
            "AXGT_CHALLENGE_DB_URL": "postgresql://control-db/axonos",
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "true",
        }
        with patch.dict(os.environ, host_environment, clear=True), patch.object(
            host,
            "_unmanaged_session_container_names",
            return_value=[],
        ), patch.object(
            host,
            "_run_cmd",
            return_value=(True, "axgt-session-net-37\t37"),
        ), patch(
            "psycopg2.connect",
            return_value=host_connection,
        ), patch.object(
            host,
            "_inspect_managed_container_ownership",
            return_value=("unmanaged", "untrusted-id", "unowned endpoint"),
        ), patch.object(
            host,
            "_cleanup_session_network",
            return_value=(False, "unknown tenant retained after central detach"),
        ) as host_cleanup, patch.object(host, "_ensure_session_network") as host_ensure:
            host._reconcile_session_networks()

        host_cleanup.assert_called_once_with(37)
        host_ensure.assert_not_called()
        host_sql = " ".join(host_cursor.execute.call_args.args[0].split()).lower()
        self.assertIn("expires_at > %s", host_sql)
        self.assertIn("last_heartbeat >= %s", host_sql)
        self.assertIn("hard_expires_at", host_sql)

        direct_connection, direct_cursor = authorized_connection()
        direct_environment = {
            "AXGT_CHALLENGE_DB_URL": "postgresql://control-db/axonos",
            "AXGT_SESSION_LAUNCHER_MODE": "docker_cli",
            "AXGT_SESSION_NETWORK_ISOLATION": "true",
            "AXGT_USER_CONTAINER_ENABLED": "true",
        }
        with patch.dict(os.environ, direct_environment, clear=True), patch.object(
            direct,
            "runtime_configuration_error",
            return_value=None,
        ), patch.object(
            direct.subprocess,
            "check_output",
            return_value="axgt-session-net-37\t37\n",
        ), patch(
            "psycopg2.connect",
            return_value=direct_connection,
        ), patch.object(
            direct,
            "_inspect_managed_container_ownership_direct",
            return_value=("unmanaged", "untrusted-id", "unowned endpoint"),
        ), patch.object(
            direct,
            "_cleanup_session_network_direct",
            return_value=(False, "unknown tenant retained after central detach"),
        ) as direct_cleanup, patch.object(
            direct,
            "_ensure_session_network_direct",
        ) as direct_ensure:
            direct.reconcile_session_networks()

        direct_cleanup.assert_called_once_with(37)
        direct_ensure.assert_not_called()
        direct_sql = " ".join(direct_cursor.execute.call_args.args[0].split()).lower()
        self.assertIn("expires_at > %s", direct_sql)
        self.assertIn("last_heartbeat >= %s", direct_sql)
        self.assertIn("hard_expires_at", direct_sql)

    def test_reconciliation_inspection_error_does_not_detach_central(self) -> None:
        import session_launcher as direct
        import session_launcher_service as host

        def authorized_connection() -> MagicMock:
            connection = MagicMock()
            cursor = MagicMock()
            cursor.__enter__.return_value = cursor
            cursor.fetchall.return_value = [(37,)]
            connection.cursor.return_value = cursor
            return connection

        host_environment = {
            "AXGT_CHALLENGE_DB_URL": "postgresql://control-db/axonos",
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "true",
        }
        with patch.dict(os.environ, host_environment, clear=True), patch.object(
            host,
            "_unmanaged_session_container_names",
            return_value=[],
        ), patch.object(
            host,
            "_run_cmd",
            return_value=(True, "axgt-session-net-37\t37"),
        ), patch(
            "psycopg2.connect",
            return_value=authorized_connection(),
        ), patch.object(
            host,
            "_inspect_managed_container_ownership",
            return_value=("error", None, "Docker daemon unavailable"),
        ), patch.object(host, "_cleanup_session_network") as host_cleanup, patch.object(
            host,
            "_ensure_session_network",
        ) as host_ensure:
            host._reconcile_session_networks()

        host_cleanup.assert_not_called()
        host_ensure.assert_not_called()

        direct_environment = {
            "AXGT_CHALLENGE_DB_URL": "postgresql://control-db/axonos",
            "AXGT_SESSION_LAUNCHER_MODE": "docker_cli",
            "AXGT_SESSION_NETWORK_ISOLATION": "true",
            "AXGT_USER_CONTAINER_ENABLED": "true",
        }
        with patch.dict(os.environ, direct_environment, clear=True), patch.object(
            direct,
            "runtime_configuration_error",
            return_value=None,
        ), patch.object(
            direct.subprocess,
            "check_output",
            return_value="axgt-session-net-37\t37\n",
        ), patch(
            "psycopg2.connect",
            return_value=authorized_connection(),
        ), patch.object(
            direct,
            "_inspect_managed_container_ownership_direct",
            return_value=("error", None, "Docker daemon unavailable"),
        ), patch.object(
            direct,
            "_cleanup_session_network_direct",
        ) as direct_cleanup, patch.object(
            direct,
            "_ensure_session_network_direct",
        ) as direct_ensure:
            direct.reconcile_session_networks()

        direct_cleanup.assert_not_called()
        direct_ensure.assert_not_called()

    def test_compatibility_mode_requires_shared_network(self) -> None:
        import session_launcher as direct
        import session_launcher_service as host

        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "true",
                "AXGT_SESSION_LAUNCHER_MODE": "docker_cli",
                "AXGT_SESSION_NETWORK_ISOLATION": "false",
            },
            clear=True,
        ):
            self.assertIn(
                "AXGT_SESSION_CONTAINER_NETWORK",
                direct.runtime_configuration_error() or "",
            )

        with patch.dict(
            os.environ,
            {
                "AXGT_HOST_SESSION_CONTAINER_IMAGE": "axonos:public-beta",
                "AXGT_HOST_SESSION_NETWORK_ISOLATION": "false",
            },
            clear=True,
        ), patch.object(host, "_unmanaged_session_container_names", return_value=[]):
            self.assertTrue(
                any(
                    "AXGT_HOST_SESSION_CONTAINER_NETWORK" in error
                    for error in host._configuration_errors()
                )
            )

    def test_service_command_injects_only_scoped_identity_and_central_urls(self) -> None:
        import session_launcher_service as launcher

        environment = {
            "AXGT_HOST_SESSION_CONTAINER_IMAGE": "axonos:public-beta",
            "AXGT_PERSISTENT_STORAGE_ENABLED": "false",
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "true",
            "AXGT_HOST_SESSION_ENV_PASSTHROUGH": ",".join(
                (
                    "WEBRTC_CAPTURE_FPS",
                    "AXGT_CHALLENGE_DB_URL",
                    "WEBRTC_AGENT_INTERNAL_KEY",
                    "AXGT_SESSION_LAUNCHER_TOKEN",
                    "POSTGRES_PASSWORD",
                    "AXGT_RPC_URL",
                    "USDC_RPC_URL",
                    "X402_SETTLEMENT_PRIVATE_KEY",
                )
            ),
            "WEBRTC_CAPTURE_FPS": "24",
            "AXGT_CHALLENGE_DB_URL": "postgresql://tenant-must-not-see-this",
            "WEBRTC_AGENT_INTERNAL_KEY": "fleet-signing-key-must-stay-central",
            "AXGT_SESSION_LAUNCHER_TOKEN": "launcher-control-key",
            "POSTGRES_PASSWORD": "database-password",
            "AXGT_RPC_URL": "https://control-plane-rpc.invalid",
            "USDC_RPC_URL": "https://payment-rpc.invalid",
            "X402_SETTLEMENT_PRIVATE_KEY": "settlement-key-must-stay-central",
            "AXGT_HOST_SESSION_CONTAINER_EXTRA_ARGS": " ".join(
                (
                    "--privileged",
                    "--network host",
                    "--cap-add SYS_ADMIN",
                    "--cap-drop ALL",
                    "--pid host",
                    "--ipc host",
                    "-e AXGT_CHALLENGE_DB_URL=attacker-db",
                    "--env=WEBRTC_AGENT_INTERNAL_KEY=attacker-key",
                    "-e AXGT_WALLET_ADDRESS=0xattacker",
                    "--label com.axonos.session-container=false",
                    "--use-api-socket",
                    "--read-only",
                )
            ),
        }
        payload = {
            "session_id": 37,
            "wallet_address": "0xAbC123",
            "requested_profile": "small",
            "assigned_gpu_ids": [0],
            "files_key": "session-files-key",
            "webrtc_agent_token": "signed-session-capability",
        }

        with patch.dict(os.environ, environment, clear=True):
            command, error = launcher._build_launch_cmd(payload)

        self.assertIsNone(error)
        self.assertIsNotNone(command)
        assert command is not None
        assignments = _docker_env_assignments(command)

        self.assertEqual(command[command.index("--cap-drop") + 1], "NET_RAW")
        self.assertIn("com.axonos.session-container=true", command)
        self.assertIn("com.axonos.session-id=37", command)
        self.assertNotIn("com.axonos.session-container=false", command)
        self.assertEqual(command.count("--cap-drop"), 1)
        self.assertEqual(command[command.index("--network") + 1], "axgt-session-net-37")
        self.assertEqual(command.count("--network"), 1)
        self.assertIn("--read-only", command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("--cap-add", command)
        self.assertNotIn("--pid", command)
        self.assertNotIn("--ipc", command)

        self.assertIn("AXGT_SESSION_ID=37", assignments)
        self.assertIn("AXGT_WALLET_ADDRESS=0xabc123", assignments)
        self.assertIn("AXGT_SESSION_FILES_KEY=session-files-key", assignments)
        self.assertIn("AXGT_WEBRTC_AGENT_TOKEN=signed-session-capability", assignments)
        self.assertIn("WEBRTC_ENABLED=true", assignments)
        self.assertIn("WEBRTC_GATE_INTERNAL_URL=http://axonos:8890", assignments)
        self.assertIn("AXGT_GATE_HEARTBEAT_URL=http://axonos:8889", assignments)
        self.assertIn("WEBRTC_CAPTURE_FPS=24", assignments)

        protected_names = (
            "AXGT_CHALLENGE_DB_URL",
            "WEBRTC_AGENT_INTERNAL_KEY",
            "AXGT_SESSION_LAUNCHER_TOKEN",
            "POSTGRES_PASSWORD",
            "AXGT_RPC_URL",
            "USDC_RPC_URL",
            "X402_SETTLEMENT_PRIVATE_KEY",
        )
        for name in protected_names:
            self.assertFalse(
                any(value.startswith(f"{name}=") for value in assignments),
                f"{name} leaked into the tenant docker command",
            )

    def test_passthrough_filter_refuses_control_plane_credentials(self) -> None:
        import session_launcher_service as launcher

        protected = (
            "AXGT_CHALLENGE_DB_URL",
            "AXGT_GATE_HEARTBEAT_URL",
            "AXGT_SESSION_FILES_KEY",
            "AXGT_SESSION_ID",
            "AXGT_SESSION_LAUNCHER_TOKEN",
            "AXGT_WALLET_ADDRESS",
            "AXGT_WEBRTC_AGENT_TOKEN",
            "WEBRTC_AGENT_INTERNAL_KEY",
            "WEBRTC_GATE_INTERNAL_URL",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB",
            "AXGT_RPC_URL",
            "AXGT_REVENUE_WALLET",
            "USDC_RPC_URL",
            "USDC_CONTRACT_ADDRESS",
            "X402_SETTLEMENT_PRIVATE_KEY",
        )
        requested = ("WEBRTC_CAPTURE_FPS", "WEBRTC_STUN_URLS", *protected)
        with patch.dict(
            os.environ,
            {"AXGT_HOST_SESSION_ENV_PASSTHROUGH": ",".join(requested)},
            clear=True,
        ):
            forwarded = launcher._env_passthrough_names()

        self.assertEqual(forwarded, ["WEBRTC_CAPTURE_FPS", "WEBRTC_STUN_URLS"])

    def test_service_creates_labeled_network_and_attaches_only_central_gate(self) -> None:
        import session_launcher_service as launcher

        environment = {
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "true",
            "AXGT_HOST_CENTRAL_GATE_CONTAINER": "axonos",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            launcher,
            "_run_cmd",
            side_effect=(
                (False, "Error: No such network: axgt-session-net-91"),
                (True, "network-id"),
                (True, ""),
                (True, 'true|91|{"central":{"Name":"axonos"}}'),
            ),
        ) as run_cmd:
            ok, error = launcher._ensure_session_network(91)

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertEqual(run_cmd.call_count, 4)
        create_command = run_cmd.call_args_list[1].args[0]
        self.assertEqual(create_command[:4], ["docker", "network", "create", "--driver"])
        self.assertIn("com.axonos.session-network=true", create_command)
        self.assertIn("com.axonos.session-id=91", create_command)
        self.assertEqual(create_command[-1], "axgt-session-net-91")
        self.assertEqual(
            run_cmd.call_args_list[2].args[0],
            [
                "docker",
                "network",
                "connect",
                "--alias",
                "axonos",
                "axgt-session-net-91",
                "axonos",
            ],
        )

    def test_service_rejects_unmanaged_preexisting_network(self) -> None:
        import session_launcher_service as launcher

        with patch.dict(
            os.environ,
            {"AXGT_HOST_SESSION_NETWORK_ISOLATION": "true"},
            clear=True,
        ), patch.object(
            launcher,
            "_run_cmd",
            return_value=(True, "false|13|{}"),
        ) as run_cmd:
            ok, error = launcher._ensure_session_network(13)

        self.assertFalse(ok)
        self.assertEqual(
            error,
            "refusing unmanaged or mismatched session network",
        )
        self.assertFalse(
            any("connect" in invocation.args[0] for invocation in run_cmd.call_args_list)
        )

    def test_service_cleanup_disconnects_central_gate_then_removes_network(self) -> None:
        import session_launcher_service as launcher

        environment = {
            "AXGT_HOST_SESSION_NETWORK_ISOLATION": "true",
            "AXGT_HOST_CENTRAL_GATE_CONTAINER": "axonos",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            launcher,
            "_run_cmd",
            side_effect=(
                (True, 'true|52|{"central":{"Name":"axonos"}}'),
                (True, "central"),
                (True, ""),
                (True, "axgt-session-net-52"),
            ),
        ) as run_cmd:
            ok, error = launcher._cleanup_session_network(52)

        self.assertTrue(ok, error)
        self.assertEqual(run_cmd.call_count, 4)
        self.assertEqual(
            run_cmd.call_args_list[2].args[0],
            [
                "docker",
                "network",
                "disconnect",
                "-f",
                "axgt-session-net-52",
                "axonos",
            ],
        )
        self.assertEqual(
            run_cmd.call_args_list[3].args[0],
            ["docker", "network", "rm", "axgt-session-net-52"],
        )

    def test_service_cleanup_preserves_unmanaged_same_named_network(self) -> None:
        import session_launcher_service as launcher

        with patch.dict(
            os.environ,
            {"AXGT_HOST_SESSION_NETWORK_ISOLATION": "true"},
            clear=True,
        ), patch.object(
            launcher, "_run_cmd", return_value=(True, "false|52|{}")
        ) as run_cmd:
            ok, error = launcher._cleanup_session_network(52)

        self.assertFalse(ok)
        self.assertIn("unmanaged", error)
        self.assertEqual(run_cmd.call_count, 1)

    def test_direct_launcher_uses_isolated_network_and_scoped_capability(self) -> None:
        import session_launcher as launcher

        environment = {
            "AXGT_SESSION_LAUNCHER_MODE": "docker_cli",
            "AXGT_SESSION_CONTAINER_IMAGE": "axonos:public-beta",
            "AXGT_USER_CONTAINER_ENABLED": "true",
            "AXGT_PERSISTENT_STORAGE_ENABLED": "false",
            "AXGT_SESSION_NETWORK_ISOLATION": "true",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            launcher,
            "_inspect_managed_container_contract_direct",
            return_value=("absent", None, ""),
        ), patch.object(
            launcher,
            "_cleanup_session_network_direct",
            return_value=(True, None),
        ), patch.object(
            launcher, "_ensure_session_network_direct", return_value=(True, None)
        ) as ensure_network, patch.object(
            launcher.subprocess, "check_output", return_value="container-id"
        ) as check_output:
            ok, container_id, error = launcher.launch_session(
                session_id=64,
                wallet="0xabc",
                profile="small",
                gpu_ids=[0],
                files_key="session-files-key",
                webrtc_agent_token="signed-session-capability",
            )

        self.assertTrue(ok)
        self.assertEqual(container_id, "container-id")
        self.assertIsNone(error)
        ensure_network.assert_called_once_with(64, allow_tenant=False)
        command = check_output.call_args.args[0]
        assignments = _docker_env_assignments(command)
        self.assertEqual(command[command.index("--network") + 1], "axgt-session-net-64")
        self.assertEqual(command[command.index("--cap-drop") + 1], "NET_RAW")
        self.assertIn("AXGT_WEBRTC_AGENT_TOKEN=signed-session-capability", assignments)
        self.assertIn("WEBRTC_GATE_INTERNAL_URL=http://axonos:8890", assignments)
        self.assertIn("AXGT_GATE_HEARTBEAT_URL=http://axonos:8889", assignments)


class StaticTenantBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        cls.supervisor = (_REPO_ROOT / "supervisord.conf").read_text(encoding="utf-8")
        cls.startup = (_REPO_ROOT / "startup.sh").read_text(encoding="utf-8")

    def _service_block(self, name: str) -> str:
        services = self.compose.split("\nservices:\n", 1)[1].split("\nnetworks:\n", 1)[0]
        markers = list(re.finditer(r"(?m)^  ([a-zA-Z0-9_-]+):\n", services))
        for index, marker in enumerate(markers):
            if marker.group(1) != name:
                continue
            end = markers[index + 1].start() if index + 1 < len(markers) else len(services)
            return services[marker.start() : end]
        self.fail(f"compose service {name!r} not found")

    @staticmethod
    def _service_networks(block: str) -> list[str]:
        match = re.search(r"(?ms)^    networks:\n(?P<body>(?:^      - [^\n]+\n?)+)", block)
        if not match:
            return []
        return re.findall(r"(?m)^      - ([^\s]+)$", match.group("body"))

    def _program_block(self, name: str) -> str:
        match = re.search(
            rf"(?ms)^\[program:{re.escape(name)}\]\n(.*?)(?=^\[program:|\Z)",
            self.supervisor,
        )
        self.assertIsNotNone(match, f"supervisor program {name!r} not found")
        assert match is not None
        return match.group(1)

    def test_compose_separates_control_and_media_networks(self) -> None:
        self.assertEqual(
            self._service_networks(self._service_block("axonos-launcher")),
            ["axonos_control"],
        )
        self.assertEqual(
            self._service_networks(self._service_block("postgres")),
            ["axonos_control"],
        )
        self.assertEqual(
            self._service_networks(self._service_block("coturn")),
            ["axonos_stack"],
        )
        self.assertEqual(
            self._service_networks(self._service_block("axonos")),
            ["axonos_control", "axonos_stack"],
        )

    def test_compose_default_tenant_passthrough_is_media_only(self) -> None:
        launcher = self._service_block("axonos-launcher")
        match = re.search(
            r"AXGT_HOST_SESSION_ENV_PASSTHROUGH:\s*"
            r"\$\{AXGT_HOST_SESSION_ENV_PASSTHROUGH:-([^}]*)\}",
            launcher,
        )
        self.assertIsNotNone(match)
        assert match is not None
        forwarded = set(match.group(1).split(","))

        self.assertIn("WEBRTC_CAPTURE_FPS", forwarded)
        self.assertIn("WEBRTC_STUN_URLS", forwarded)
        self.assertIn("WEBRTC_AUDIO_ENABLED", forwarded)
        self.assertTrue(all(name.startswith("WEBRTC_") for name in forwarded))
        self.assertTrue(
            forwarded.isdisjoint(
                {
                    "AXGT_CHALLENGE_DB_URL",
                    "WEBRTC_AGENT_INTERNAL_KEY",
                    "AXGT_SESSION_LAUNCHER_TOKEN",
                    "AXGT_RPC_URL",
                    "USDC_RPC_URL",
                    "AXGT_CONTRACT_ADDRESS",
                    "AXGT_REVENUE_WALLET",
                    "CDP_FACILITATOR_URL",
                    "POSTGRES_PASSWORD",
                }
            )
        )

    def test_internal_agent_listener_is_not_host_published(self) -> None:
        axonos = self._service_block("axonos")
        ports = axonos.split("    ports:\n", 1)[1].split("    # No GPUs", 1)[0]
        self.assertNotIn(":8890", ports)
        self.assertIn("${AXONOS_PUBLISH_GATE:-8889}:8889", ports)

    def test_session_containers_disable_shared_vnc_and_gate_listeners(self) -> None:
        x11vnc = self._program_block("x11vnc")
        self.assertIn('AXGT_SESSION_ID:-', x11vnc)
        self.assertIn("exec sleep infinity", x11vnc)
        self.assertIn("x11vnc -display :0", x11vnc)

        for program in ("novnc", "axgt-api"):
            block = self._program_block(program)
            self.assertIn('AXGT_SESSION_ID:-', block)
            self.assertIn("exec sleep infinity", block)

        public_gate = self._program_block("axgt-api")
        self.assertIn("GATE_AGENT_API_ENABLED=false", public_gate)
        self.assertIn("GATE_AGENT_ONLY=false", public_gate)

        internal_gate = self._program_block("webrtc-agent-gate")
        self.assertIn('AXGT_SESSION_ID:-', internal_gate)
        self.assertIn("GATE_PORT=8890", internal_gate)
        self.assertIn("GATE_AGENT_API_ENABLED=true", internal_gate)
        self.assertIn("GATE_AGENT_ONLY=true", internal_gate)

    def test_supervisor_boolean_commands_survive_real_inline_comment_parsing(self) -> None:
        parser = configparser.RawConfigParser(
            inline_comment_prefixes=(";", "#"),
        )
        parser.read_string(self.supervisor)
        for program in ("webrtc-agent-gate", "webrtc-agent", "sshd"):
            with self.subTest(program=program):
                command = parser.get(f"program:{program}", "command")
                argv = shlex.split(command)
                self.assertGreaterEqual(len(argv), 3)
                self.assertIn("yes|on", command)
                syntax = subprocess.run(
                    ["/bin/bash", "-n", "-c", argv[2]],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_tenant_ipfs_api_and_gateway_default_to_loopback(self) -> None:
        marker = 'if [ -n "${AXGT_SESSION_ID:-}" ] || _axonos_truthy "${_multi_user}"'
        conditional = self.startup.split(marker, 1)[1]
        tenant_defaults, remainder = conditional.split("\nelse\n", 1)
        standalone_defaults = remainder.split("\nfi\n", 1)[0]

        self.assertIn('IPFS_API_BIND="${IPFS_API_BIND:-127.0.0.1}"', tenant_defaults)
        self.assertIn(
            'IPFS_GATEWAY_BIND="${IPFS_GATEWAY_BIND:-127.0.0.1}"',
            tenant_defaults,
        )
        self.assertIn('IPFS_API_BIND="${IPFS_API_BIND:-0.0.0.0}"', standalone_defaults)
        self.assertIn(
            'IPFS_GATEWAY_BIND="${IPFS_GATEWAY_BIND:-0.0.0.0}"',
            standalone_defaults,
        )
        self.assertIn('/ip4/${IPFS_API_BIND}/tcp/${IPFS_API_PORT}', self.startup)
        self.assertIn('/ip4/${IPFS_GATEWAY_BIND}/tcp/${IPFS_GATEWAY_PORT}', self.startup)
        self.assertIn("1|true|yes|on", self.startup)

    def test_multi_user_runtime_disables_unreachable_vnc_fallback(self) -> None:
        axonos = self._service_block("axonos")
        self.assertIn(
            "WEBRTC_FALLBACK_ENABLED: ${WEBRTC_FALLBACK_ENABLED:-true}",
            axonos,
        )
        self.assertIn(
            "WEBRTC_GATE_INTERNAL_URL: http://127.0.0.1:8890",
            axonos,
        )
        self.assertNotIn(
            "WEBRTC_GATE_INTERNAL_URL: ${WEBRTC_GATE_INTERNAL_URL",
            axonos,
        )

        from webrtc import config

        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "on",
                "WEBRTC_FALLBACK_ENABLED": "true",
            },
            clear=True,
        ):
            self.assertFalse(config.fallback_enabled())
        with patch.dict(
            os.environ,
            {
                "AXGT_USER_CONTAINER_ENABLED": "false",
                "WEBRTC_FALLBACK_ENABLED": "true",
            },
            clear=True,
        ):
            self.assertTrue(config.fallback_enabled())


if __name__ == "__main__":
    unittest.main()
