"""Operator storage policy without Docker, filesystem or database side effects."""

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import psycopg2
from axonos_gate import session_launcher_service as service


POLICY = "AXGT_STORAGE_MAINTENANCE_ENABLED"
WALLET = "0x" + "a" * 40
OTHER_WALLET = "0x" + "b" * 40
GIB = 1024**3
VOLUME = "axgt-user-storage-" + WALLET
IMAGE_PATH = "/mock-storage/" + VOLUME + ".ext4"


class _StopLoop(Exception):
    """Bound a periodic-loop test even if a regression ignores the policy."""


class StorageMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "AXGT_PERSISTENT_STORAGE_ENABLED": "true",
                "AXGT_PERSISTENT_STORAGE_DIR": "/mock-storage",
                "AXGT_HOST_SESSION_CONTAINER_IMAGE": "test-session-image",
                "AXGT_HOST_SESSION_NETWORK_ISOLATION": "false",
                "AXGT_HOST_SESSION_CONTAINER_NETWORK": "test-session-network",
                "AXGT_SESSION_LAUNCHER_TOKEN": "test-launcher-bearer",
                "AXGT_CHALLENGE_DB_URL": "postgresql://unused-test-db/unused",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    @contextmanager
    def no_background_io(self):
        """Spy on real side-effect boundaries rather than replacing sweeps."""
        with ExitStack() as stack:
            spies = {}
            for name in ("check_output", "check_call", "run", "Popen"):
                spies["subprocess." + name] = stack.enter_context(
                    patch.object(service.subprocess, name)
                )
            spies["subprocess.check_output"].return_value = ""
            for name in ("scandir", "makedirs", "remove", "unlink"):
                spies["os." + name] = stack.enter_context(
                    patch.object(service.os, name)
                )
            spies["os.scandir"].return_value = []
            spies["psycopg2.connect"] = stack.enter_context(
                patch.object(psycopg2, "connect")
            )
            yield spies

    def assert_no_io(self, spies):
        for name, spy in spies.items():
            with self.subTest(side_effect=name):
                spy.assert_not_called()

    def test_unset_defaults_to_enabled_without_changing_persistent_storage(self):
        self.assertNotIn(POLICY, os.environ)
        self.assertTrue(service._storage_maintenance_enabled())
        self.assertTrue(service._persistent_storage_enabled())

    def test_true_values_are_normalized(self):
        for value in ("true", "1", "yes", "on", " TRUE ", "\tYes\n"):
            with self.subTest(value=value), patch.dict(os.environ, {POLICY: value}):
                self.assertTrue(service._storage_maintenance_enabled())

    def test_false_values_preserve_persistent_storage(self):
        for value in ("false", "0", "no", "off", " FALSE ", "\tOff\n"):
            with self.subTest(value=value), patch.dict(os.environ, {POLICY: value}):
                self.assertFalse(service._storage_maintenance_enabled())
                self.assertTrue(service._persistent_storage_enabled())

    def test_invalid_or_empty_values_never_enable_maintenance(self):
        for value in ("", " ", "tru", "disabled", "2", "none", "false,true"):
            with self.subTest(value=value), patch.dict(os.environ, {POLICY: value}):
                with self.assertRaisesRegex(ValueError, POLICY):
                    service._storage_maintenance_enabled()

    def test_unset_and_true_preserve_startup_sync_and_cleanup_thread(self):
        for value in (None, "true"):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=False):
                if value is None:
                    os.environ.pop(POLICY, None)
                else:
                    os.environ[POLICY] = value
                with patch.object(service, "_sync_persistent_storage_capacity_records", return_value=2) as sync, \
                     patch.object(service.threading, "Thread") as thread, \
                     patch.object(service.app, "run") as run:
                    service.main()
                sync.assert_called_once_with()
                thread.assert_called_once_with(
                    target=service._prune_inactive_volumes_loop, daemon=True
                )
                thread.return_value.start.assert_called_once_with()
                run.assert_called_once()

    def test_disabled_startup_does_not_touch_storage_or_start_sweeper(self):
        os.environ[POLICY] = "false"
        with self.no_background_io() as spies, \
             patch.object(service.threading, "Thread") as thread, \
             patch.object(service.app, "run") as run:
            service.main()
        self.assert_no_io(spies)
        thread.assert_not_called()
        run.assert_called_once()
        self.assertTrue(service._persistent_storage_enabled())

    def test_invalid_startup_aborts_before_storage_threads_or_server(self):
        os.environ[POLICY] = "typo"
        with self.no_background_io() as spies, \
             patch.object(service.threading, "Thread") as thread, \
             patch.object(service.app, "run") as run:
            with self.assertRaisesRegex(ValueError, POLICY):
                service.main()
        self.assert_no_io(spies)
        thread.assert_not_called()
        run.assert_not_called()

    def test_invalid_policy_makes_health_unavailable_without_storage_io(self):
        os.environ[POLICY] = "typo"
        with self.no_background_io() as spies, \
             patch.object(service, "_unmanaged_session_container_names", return_value=[]):
            response = service.app.test_client().get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn(POLICY, " ".join(response.get_json()["errors"]))
        self.assert_no_io(spies)

    def test_invalid_policy_rejects_launch_before_allocation_or_home_access(self):
        os.environ[POLICY] = "typo"
        with self.no_background_io() as spies, \
             patch.object(service, "_unmanaged_session_container_names", return_value=[]):
            response = service.app.test_client().post(
                "/launch", json=self.payload(),
                headers={"Authorization": "Bearer test-launcher-bearer"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn(POLICY, " ".join(response.get_json()["errors"]))
        self.assert_no_io(spies)

    def test_disabled_capacity_sync_leaves_all_storage_mappings_untouched(self):
        os.environ[POLICY] = "false"
        with self.no_background_io() as spies:
            self.assertEqual(service._sync_persistent_storage_capacity_records(), 0)
        self.assert_no_io(spies)

    def test_disabled_cleanup_cannot_probe_prune_resize_delete_or_bill(self):
        os.environ[POLICY] = "false"
        # Give cleanup a real-looking debt policy: the operator flag, rather
        # than an empty DB setting or forgiving threshold, must stop the work.
        os.environ["AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES"] = "0"
        with self.no_background_io() as spies:
            self.assertIsNone(service._run_volume_cleanup())
        self.assert_no_io(spies)

    def test_disabled_size_probe_never_mounts_an_inactive_volume(self):
        os.environ[POLICY] = "false"
        with self.no_background_io() as spies:
            self.assertEqual(
                service._get_volume_size_kb("axgt-user-storage-" + OTHER_WALLET),
                0.0,
            )
        self.assert_no_io(spies)

    def test_disabled_periodic_loop_returns_before_sleep_or_io(self):
        os.environ[POLICY] = "false"
        with self.no_background_io() as spies, \
             patch.object(service.time, "sleep", side_effect=_StopLoop) as sleep:
            service._prune_inactive_volumes_loop()
        self.assert_no_io(spies)
        sleep.assert_not_called()

    def test_true_periodic_loop_retains_warmup_and_sweep(self):
        os.environ[POLICY] = "true"
        with patch.object(service, "_run_volume_cleanup") as cleanup, \
             patch.object(service.time, "sleep", side_effect=(None, _StopLoop)) as sleep:
            with self.assertRaises(_StopLoop):
                service._prune_inactive_volumes_loop()
        cleanup.assert_called_once_with()
        self.assertEqual(sleep.call_args_list[0].args, (30,))
        self.assertEqual(sleep.call_args_list[1].args, (3600,))

    def test_periodic_loop_rechecks_policy_before_another_sweep(self):
        os.environ[POLICY] = "true"

        def disable_after_sweep():
            os.environ[POLICY] = "false"

        with patch.object(service, "_run_volume_cleanup", side_effect=disable_after_sweep) as cleanup, \
             patch.object(service.time, "sleep", side_effect=(None, None, _StopLoop)):
            service._prune_inactive_volumes_loop()
        cleanup.assert_called_once_with()

    def test_disabling_during_warmup_prevents_first_sweep(self):
        os.environ[POLICY] = "true"

        def disable_during_sleep(_seconds):
            os.environ[POLICY] = "false"

        with self.no_background_io() as spies, \
             patch.object(service, "_run_volume_cleanup") as cleanup, \
             patch.object(service.time, "sleep", side_effect=disable_during_sleep) as sleep:
            service._prune_inactive_volumes_loop()
        self.assert_no_io(spies)
        cleanup.assert_not_called()
        sleep.assert_called_once_with(30)

    def test_invalid_policy_fails_before_direct_background_io(self):
        os.environ[POLICY] = "not-a-boolean"
        calls = (
            (service._sync_persistent_storage_capacity_records, ()),
            (service._run_volume_cleanup, ()),
            (service._get_volume_size_kb, (VOLUME,)),
            (service._prune_inactive_volumes_loop, ()),
        )
        for function, args in calls:
            with self.subTest(function=function.__name__), \
                 self.no_background_io() as spies, \
                 patch.object(service.time, "sleep", side_effect=_StopLoop) as sleep:
                with self.assertRaisesRegex(ValueError, POLICY):
                    function(*args)
            self.assert_no_io(spies)
            sleep.assert_not_called()

    @contextmanager
    def existing_home_launch(self, requested_storage_gb=250):
        """Exercise real authorization and provisioning with low-level fakes."""
        os.environ.update({
            POLICY: "false",
            "AXGT_REAL_STORAGE_TEST": "1",
            "AXGT_HOST_SESSION_ENV_PASSTHROUGH": POLICY,
        })
        auth_conn, storage_conn = MagicMock(), MagicMock()
        auth_cur, storage_cur = MagicMock(), MagicMock()
        auth_conn.cursor.return_value.__enter__.return_value = auth_cur
        storage_conn.cursor.return_value.__enter__.return_value = storage_cur

        def authorize_query(_sql, params):
            auth_cur.fetchone.return_value = (
                ("0", "test-session-key", False, "small", requested_storage_gb)
                if params[:2] == (42, WALLET) else None
            )

        auth_cur.execute.side_effect = authorize_query
        storage_cur.fetchone.return_value = (250 * GIB,)

        def inspect_storage(command, **_kwargs):
            if command == ["losetup", "-j", IMAGE_PATH]:
                return "/dev/loop7: []: (" + IMAGE_PATH + ")\n"
            if command == ["dumpe2fs", "-h", "/dev/loop7"]:
                return f"Block count: {(250 * GIB) // 4096}\nBlock size: 4096\n"
            if command == ["docker", "volume", "inspect", VOLUME]:
                return json.dumps([{"Options": {"type": "ext4", "device": "/dev/loop7"}}])
            raise AssertionError("unexpected storage command: " + repr(command))

        with ExitStack() as stack:
            connect = stack.enter_context(patch.object(
                psycopg2, "connect", side_effect=(auth_conn, storage_conn)
            ))
            stack.enter_context(patch.object(service, "_unmanaged_session_container_names", return_value=[]))
            stack.enter_context(patch.object(service, "_inspect_managed_container_contract", return_value=("absent", None, "")))
            stack.enter_context(patch.object(service, "_cleanup_session_network", return_value=(True, "")))
            stack.enter_context(patch.object(service, "_ensure_session_network", return_value=(True, "")))
            run_cmd = stack.enter_context(patch.object(service, "_run_cmd", return_value=(True, "c" * 64)))
            stack.enter_context(patch.object(service, "_tool_path", side_effect=lambda name: name))
            mkdir = stack.enter_context(patch.object(service.os, "makedirs"))
            stack.enter_context(patch.object(service.os.path, "exists", return_value=True))
            getsize = stack.enter_context(patch.object(service.os.path, "getsize", return_value=250 * GIB))
            output = stack.enter_context(patch.object(service.subprocess, "check_output", side_effect=inspect_storage))
            check_call = stack.enter_context(patch.object(service.subprocess, "check_call"))
            run = stack.enter_context(patch.object(service.subprocess, "run"))
            remove = stack.enter_context(patch.object(service.os, "remove"))
            unlink = stack.enter_context(patch.object(service.os, "unlink"))
            scandir = stack.enter_context(patch.object(service.os, "scandir"))
            yield SimpleNamespace(
                connect=connect, auth_cur=auth_cur, storage_cur=storage_cur,
                storage_conn=storage_conn, run_cmd=run_cmd, output=output,
                mkdir=mkdir, getsize=getsize, check_call=check_call,
                run=run, remove=remove, unlink=unlink, scandir=scandir,
            )

    @staticmethod
    def payload(wallet=WALLET):
        return {
            "session_id": 42,
            "wallet_address": wallet,
            "assigned_gpu_ids": [0],
            "requested_profile": "small",
            "requested_storage_gb": 250,
            "files_key": "test-session-key",
            "webrtc_agent_token": "test-scoped-capability",
            "storage_maintenance_enabled": True,
            POLICY: "true",
        }

    def test_authenticated_launch_recovers_only_its_existing_home(self):
        with self.existing_home_launch() as io:
            response = service.app.test_client().post(
                "/launch", json=self.payload(),
                headers={"Authorization": "Bearer test-launcher-bearer"},
            )
            self.assertFalse(service._storage_maintenance_enabled())
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["ok"])
        auth_sql, auth_params = io.auth_cur.execute.call_args.args
        self.assertIn("wallet_address = %s", auth_sql)
        self.assertEqual(auth_params[:2], (42, WALLET))
        io.getsize.assert_called_once_with(IMAGE_PATH)
        io.run_cmd.assert_called_once()
        command = io.run_cmd.call_args.args[0]
        mounts = [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "-v"]
        self.assertEqual(mounts, [VOLUME + ":/home/aXonian"])
        self.assertNotIn(POLICY, " ".join(command))
        self.assertNotIn(OTHER_WALLET, " ".join(command))
        self.assertEqual(io.output.call_count, 3)
        self.assertTrue(all(OTHER_WALLET not in repr(c) for c in io.output.call_args_list))
        capacity_write = next(c for c in io.storage_cur.execute.call_args_list if "INSERT INTO" in c.args[0])
        self.assertEqual(capacity_write.args[1][:3], (WALLET, VOLUME, 250 * GIB))
        io.storage_conn.commit.assert_called_once()
        for spy in (io.check_call, io.run, io.remove, io.unlink, io.scandir):
            spy.assert_not_called()

    def test_wrong_wallet_allocation_rejected_before_home_access(self):
        with self.existing_home_launch() as io:
            response = service.app.test_client().post(
                "/launch", json=self.payload(OTHER_WALLET),
                headers={"Authorization": "Bearer test-launcher-bearer"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])
        io.connect.assert_called_once()
        for spy in (io.output, io.getsize, io.mkdir, io.check_call, io.run, io.run_cmd):
            spy.assert_not_called()

    def test_missing_launcher_auth_rejected_before_database_or_home_access(self):
        with self.existing_home_launch() as io:
            response = service.app.test_client().post("/launch", json=self.payload())
        self.assertEqual(response.status_code, 401)
        for spy in (io.connect, io.output, io.getsize, io.mkdir, io.check_call, io.run, io.run_cmd):
            spy.assert_not_called()

    def test_disabled_maintenance_does_not_weaken_existing_home_capacity_floor(self):
        payload = self.payload()
        payload["requested_storage_gb"] = 100
        with self.existing_home_launch(requested_storage_gb=100) as io:
            response = service.app.test_client().post(
                "/launch", json=payload,
                headers={"Authorization": "Bearer test-launcher-bearer"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be reduced from 250 GB to 100 GB", response.get_json()["error"])
        io.getsize.assert_called_once_with(IMAGE_PATH)
        for spy in (io.check_call, io.run, io.run_cmd, io.remove, io.unlink, io.scandir):
            spy.assert_not_called()

    def test_disabled_maintenance_still_allows_explicit_new_home_provisioning(self):
        os.environ.update({POLICY: "false", "AXGT_REAL_STORAGE_TEST": "1"})
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = (100 * GIB,)

        def check_output(command, **_kwargs):
            if command == ["losetup", "-j", IMAGE_PATH]:
                return ""
            if command == ["losetup", "-f", "--show", IMAGE_PATH]:
                return "/dev/loop7\n"
            if command == ["dumpe2fs", "-h", "/dev/loop7"]:
                return f"Block count: {(100 * GIB) // 4096}\nBlock size: 4096\n"
            if command == ["docker", "volume", "inspect", VOLUME]:
                raise subprocess.CalledProcessError(1, command)
            raise AssertionError("unexpected storage command: " + repr(command))

        with patch.object(service, "_tool_path", side_effect=lambda name: name), \
             patch.object(service.os, "makedirs"), \
             patch.object(service.os.path, "exists", return_value=False), \
             patch.object(service.subprocess, "check_output", side_effect=check_output), \
             patch.object(service.subprocess, "check_call") as check_call, \
             patch.object(service.subprocess, "run") as run, \
             patch.object(psycopg2, "connect", return_value=conn), \
             patch.object(service.os, "scandir") as scandir, \
             patch.object(service.os, "remove") as remove, \
             patch.object(service.os, "unlink") as unlink:
            ok, error = service._ensure_persistent_storage_volume(VOLUME, 100, WALLET)
        self.assertTrue(ok, error)
        self.assertEqual([c.args[0] for c in check_call.call_args_list], [
            ["truncate", "-s", "100G", IMAGE_PATH],
            ["mkfs.ext4", "-F", "-E", "root_owner=1000:1000", IMAGE_PATH],
            ["docker", "volume", "create", "--driver", "local", "--opt", "type=ext4", "--opt", "device=/dev/loop7", VOLUME],
        ])
        for spy in (run, scandir, remove, unlink):
            spy.assert_not_called()
        conn.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
