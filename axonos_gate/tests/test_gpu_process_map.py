"""Host-PID -> container-PID mapping behind the in-session nvidia-smi wrapper."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_axonos_gate_root = os.path.dirname(_tests_dir)
_repo_root = os.path.dirname(_axonos_gate_root)
if _axonos_gate_root not in sys.path:
    sys.path.insert(0, _axonos_gate_root)

try:
    import flask  # noqa: F401
except ImportError:
    from unittest.mock import MagicMock
    sys.modules["flask"] = MagicMock()
try:
    import psycopg2  # noqa: F401
except ImportError:
    from unittest.mock import MagicMock
    sys.modules["psycopg2"] = MagicMock()

import gpu_process_map  # noqa: E402

DOCKER_TOP = """PID                 COMMAND             COMMAND
598371              startup.sh          /bin/bash /startup.sh
657572              python3             /usr/bin/python3 -u ddp_resnet152_bench.py --steps 2000
657573              python3             /usr/bin/python3 -u ddp_resnet152_bench.py --steps 2000
"""


def _write_status(proc_dir: str, pid: int, nspid: str) -> None:
    os.makedirs(os.path.join(proc_dir, str(pid)), exist_ok=True)
    with open(os.path.join(proc_dir, str(pid), "status"), "w", encoding="utf-8") as f:
        f.write(f"Name:\tpython3\nPid:\t{pid}\nNSpid:\t{nspid}\n")


class ParseDockerTopTests(unittest.TestCase):
    def test_parses_pid_comm_and_full_args(self) -> None:
        rows = gpu_process_map.parse_docker_top(DOCKER_TOP)
        self.assertEqual(set(rows), {598371, 657572, 657573})
        self.assertEqual(rows[657572]["comm"], "python3")
        self.assertEqual(
            rows[657572]["args"], "/usr/bin/python3 -u ddp_resnet152_bench.py --steps 2000"
        )

    def test_empty_and_garbage_lines_are_ignored(self) -> None:
        self.assertEqual(gpu_process_map.parse_docker_top(""), {})
        self.assertEqual(gpu_process_map.parse_docker_top("PID COMMAND\nabc foo\n"), {})


class ContainerProcessMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.proc = self._tmp.name
        _write_status(self.proc, 598371, "598371\t1")
        _write_status(self.proc, 657572, "657572\t6578")
        # Nested namespace inside the container: the container-level PID is
        # the middle entry, not the innermost one.
        _write_status(self.proc, 657573, "657573\t6579\t3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_maps_host_pids_to_container_pids_at_init_depth(self) -> None:
        with patch.object(gpu_process_map.subprocess, "check_output", return_value=DOCKER_TOP) as co:
            result = gpu_process_map.container_process_map("abc", proc_dir=self.proc)
        self.assertEqual(co.call_args[0][0][:3], ["docker", "top", "abc"])
        self.assertEqual(result["598371"]["pid"], 1)
        self.assertEqual(result["657572"]["pid"], 6578)
        self.assertEqual(result["657573"]["pid"], 6579)
        self.assertEqual(result["657572"]["comm"], "python3")

    def test_missing_host_proc_keeps_names_without_pids(self) -> None:
        with patch.object(gpu_process_map.subprocess, "check_output", return_value=DOCKER_TOP):
            result = gpu_process_map.container_process_map(
                "abc", proc_dir=os.path.join(self.proc, "nope")
            )
        self.assertEqual(result["657572"]["pid"], None)
        self.assertEqual(result["657572"]["comm"], "python3")

    def test_docker_failure_yields_empty_map(self) -> None:
        with patch.object(
            gpu_process_map.subprocess,
            "check_output",
            side_effect=subprocess.CalledProcessError(1, "docker"),
        ):
            self.assertEqual(gpu_process_map.container_process_map("abc", proc_dir=self.proc), {})


class LauncherServiceRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_env = dict(os.environ)
        os.environ["AXGT_SESSION_LAUNCHER_TOKEN"] = "tok"
        import session_launcher_service as svc

        self.svc = svc
        self.client = svc.app.test_client()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_requires_token_and_valid_session_id(self) -> None:
        self.assertEqual(self.client.get("/session-process-map?session_id=5").status_code, 401)
        hdr = {"Authorization": "Bearer tok"}
        self.assertEqual(
            self.client.get("/session-process-map?session_id=x", headers=hdr).status_code, 400
        )
        self.assertEqual(
            self.client.get("/session-process-map?session_id=0", headers=hdr).status_code, 400
        )

    def test_only_the_running_managed_container_is_inspected(self) -> None:
        hdr = {"Authorization": "Bearer tok"}
        with patch.object(self.svc, "_managed_container_id", return_value=None) as mid:
            resp = self.client.get("/session-process-map?session_id=7", headers=hdr)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(mid.call_args.kwargs, {"require_running": True})

        with patch.object(self.svc, "_managed_container_id", return_value="cid"), patch.object(
            self.svc, "container_process_map", return_value={"1": {"pid": 1, "comm": "a", "args": "a"}}
        ) as cpm:
            resp = self.client.get("/session-process-map?session_id=7", headers=hdr)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["processes"]["1"]["pid"], 1)
        self.assertEqual(cpm.call_args[0][0], "cid")


class GateClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_http_mode_asks_launcher_service(self) -> None:
        import session_launcher as sl

        os.environ["AXGT_SESSION_LAUNCHER_MODE"] = "http"
        os.environ["AXGT_SESSION_LAUNCHER_URL"] = "http://launcher:8090"
        with patch.object(sl, "_container_mode_enabled", return_value=True), patch.object(
            sl, "_http_json", return_value=(200, {"ok": True, "processes": {"9": {"pid": 2}}}, None)
        ) as hj:
            self.assertEqual(sl.session_process_map(42), {"9": {"pid": 2}})
        self.assertIn("/session-process-map?session_id=42", hj.call_args[0][1])
        with patch.object(sl, "_container_mode_enabled", return_value=True), patch.object(
            sl, "_http_json", return_value=(503, {"ok": False}, None)
        ):
            self.assertIsNone(sl.session_process_map(42))


class WrapperScriptTests(unittest.TestCase):
    """The in-image script: pure formatting + pass-through contract."""

    @classmethod
    def setUpClass(cls) -> None:
        path = os.path.join(_repo_root, "scripts", "axonos-gpu-ps")
        cls.path = path
        spec = importlib.util.spec_from_loader("axonos_gpu_ps", loader=None)
        mod = importlib.util.module_from_spec(spec)
        with open(path, "r", encoding="utf-8") as f:
            exec(compile(f.read(), path, "exec"), mod.__dict__)
        cls.mod = mod

    def test_script_is_executable_and_installed_by_dockerfile(self) -> None:
        self.assertTrue(os.access(self.path, os.X_OK))
        with open(os.path.join(_repo_root, "Dockerfile"), "r", encoding="utf-8") as f:
            dockerfile = f.read()
        self.assertIn("COPY scripts/axonos-gpu-ps /usr/local/bin/axon-gpu-ps", dockerfile)
        self.assertIn("ln -sf axon-gpu-ps /usr/local/bin/nvidia-smi", dockerfile)

    def test_rows_fit_nvidia_smi_box_width(self) -> None:
        rows = [
            {"gpu": 0, "pid": 6578, "host_pid": 657572, "name": "/usr/bin/python3 " * 20, "used_mib": 4566},
            {"gpu": 1, "pid": None, "host_pid": 657573, "name": "x", "used_mib": None},
        ]
        lines = self.mod._fmt_rows(rows, 79)
        self.assertEqual([len(ln) for ln in lines], [79, 79])
        self.assertIn("6578", lines[0])
        self.assertIn("4566MiB", lines[0])
        self.assertIn("h657573", lines[1])  # unmapped: host pid, flagged

    def test_joined_output_replaces_only_the_marker_line(self) -> None:
        fake = "+---+\n|  No running processes found  |\n+---+\n"
        rows = [{"gpu": 0, "pid": 7, "host_pid": 1, "name": "python3", "used_mib": 10}]

        class Out:
            stdout, stderr, returncode = fake, "", 0

        with patch.object(self.mod, "_run", return_value=Out()), patch.object(
            self.mod, "joined_processes", return_value=rows
        ), patch("sys.stdout") as so:
            self.mod._wrap_plain_output()
        printed = "".join(c.args[0] for c in so.write.call_args_list)
        self.assertNotIn("No running processes found", printed)
        self.assertIn("python3", printed)
        self.assertTrue(printed.startswith("+---+\n|"))

    def test_arguments_pass_through_to_real_binary(self) -> None:
        with patch.object(self.mod.os, "execv") as ex, patch.object(
            self.mod.os.path, "exists", return_value=True
        ):
            self.mod.main(["nvidia-smi", "--query-gpu=index", "--format=csv"])
        self.assertEqual(
            ex.call_args[0], ("/usr/bin/nvidia-smi", ["/usr/bin/nvidia-smi", "--query-gpu=index", "--format=csv"])
        )

    def test_json_mode(self) -> None:
        with patch.object(self.mod, "joined_processes", return_value=[]), patch("sys.stdout") as so:
            self.mod.main(["axon-gpu-ps", "--json"])
        self.assertEqual(json.loads("".join(c.args[0] for c in so.write.call_args_list)), [])


if __name__ == "__main__":
    unittest.main()
