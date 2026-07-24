"""Regression guards for the desktop/WebRTC startup critical path."""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


_TESTS_DIR = Path(__file__).resolve().parent
_GATE_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _GATE_ROOT.parent
if str(_GATE_ROOT) not in sys.path:
    sys.path.insert(0, str(_GATE_ROOT))


class StartupFastPathSourceTests(unittest.TestCase):
    def test_supervisor_is_the_only_ipfs_daemon_owner(self) -> None:
        startup = (_REPO_ROOT / "startup.sh").read_text(encoding="utf-8")
        supervisor = (_REPO_ROOT / "supervisord.conf").read_text(encoding="utf-8")

        self.assertNotIn("ipfs daemon", startup)
        self.assertIn("[program:ipfs]", supervisor)
        ipfs_block = supervisor.split("[program:ipfs]", 1)[1].split("[program:", 1)[0]
        self.assertIn("command=/usr/local/bin/ipfs daemon --enable-gc --routing=dht", ipfs_block)
        self.assertIn("user=aXonian", ipfs_block)
        self.assertIn('IPFS_PATH="/home/aXonian/.ipfs"', ipfs_block)
        self.assertNotIn("su - aXonian", ipfs_block)

    def test_webrtc_agent_supervisor_has_no_fixed_readiness_delay(self) -> None:
        source = (_REPO_ROOT / "supervisord.conf").read_text(encoding="utf-8")
        block = source.split("[program:webrtc-agent]", 1)[1].split("[program:", 1)[0]

        self.assertIn("exec /usr/bin/python3 /axonos_gate/webrtc_agent_main.py", block)
        self.assertNotIn("xset q", block)
        self.assertNotIn("sleep 3", block)

    def test_container_lifetime_tracks_supervisord_and_compose_checks_listeners(self) -> None:
        startup = (_REPO_ROOT / "startup.sh").read_text(encoding="utf-8")
        compose = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("SUPERVISOR_PID=$!", startup)
        self.assertIn('wait "$SUPERVISOR_PID"', startup)
        self.assertNotIn("tail -f /dev/null", startup)
        self.assertIn("monitor_critical_supervisor_children", startup)
        for program in (
            "axgt-api",
            "novnc",
            "webrtc-agent-gate",
            "webrtc-agent",
            "x11vnc",
            "xorg-nvidia",
            "xfce4",
        ):
            self.assertIn(program, startup)
        self.assertIn('*" FATAL "*|*" EXITED "*', startup)
        self.assertIn("2>/dev/null || true", startup)
        self.assertIn("SUPERVISOR_CONFIG_PATH", startup)
        self.assertIn("critical_supervisor_listeners_ready", startup)
        self.assertIn("local max_listener_failures=6", startup)
        self.assertIn('listener_failures=0', startup)
        self.assertIn('kill -TERM "$SUPERVISOR_PID"', startup)
        self.assertIn('if [ -z "${AXGT_SESSION_ID:-}" ]; then', startup)
        self.assertIn("ports+=(5901)", startup)
        for port in ("6080", "8889", "8890"):
            self.assertIn(port, compose)
        self.assertIn("WEBRTC_ENABLED", compose)
        self.assertIn("start_period: 60s", compose)

    def _startup_function(self, name: str, next_name: str) -> str:
        startup = (_REPO_ROOT / "startup.sh").read_text(encoding="utf-8")
        function_source = startup.split(
            f"{name}() {{",
            1,
        )[1]
        for boundary in (f"\n}}\n\n{next_name}", f"\n}}\n{next_name}"):
            if boundary in function_source:
                function_source = function_source.split(boundary, 1)[0]
                break
        else:
            self.fail(f"Could not isolate startup function {name}")
        return f"{name}() {{" + function_source + "\n}"

    def test_terminal_detector_keeps_supervisorctl_output_on_nonzero_exit(self) -> None:
        function_source = self._startup_function(
            "critical_supervisor_child_is_terminal",
            "critical_supervisor_listeners_ready",
        )

        for state in ("FATAL", "EXITED"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp_dir:
                fake = Path(temp_dir) / "supervisorctl"
                fake.write_text(
                    "#!/bin/sh\n"
                    f"printf '%s\\n' 'webrtc-agent-gate {state} test state'\n"
                    "exit 3\n",
                    encoding="utf-8",
                )
                fake.chmod(0o700)
                result = subprocess.run(
                    [
                        "/bin/bash",
                        "-c",
                        function_source
                        + "\nSUPERVISORCTL_BIN=\"$1\" "
                        "critical_supervisor_child_is_terminal",
                        "bash",
                        str(fake),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_listener_probe_selects_ports_by_runtime_mode(self) -> None:
        startup = (_REPO_ROOT / "startup.sh").read_text(encoding="utf-8")
        truthy_source = startup.split("_axonos_truthy() {", 1)[1].split(
            "\n}\n_multi_user=", 1
        )[0]
        truthy_source = "_axonos_truthy() {" + truthy_source + "\n}"
        probe_source = self._startup_function(
            "critical_supervisor_listeners_ready",
            "monitor_critical_supervisor_children",
        )

        cases = (
            (
                {"WEBRTC_ENABLED": "true", "AXGT_DESKTOP_ENABLED": "false"},
                ["6080", "8889", "8890"],
            ),
            (
                {"WEBRTC_ENABLED": "false", "AXGT_DESKTOP_ENABLED": "true"},
                ["6080", "8889", "5901"],
            ),
        )
        for environment, expected_ports in cases:
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as temp_dir:
                fake = Path(temp_dir) / "probe"
                fake.write_text(
                    "#!/bin/sh\n"
                    "for argument in \"$@\"; do printf '%s\\n' \"$argument\"; done\n",
                    encoding="utf-8",
                )
                fake.chmod(0o700)
                result = subprocess.run(
                    [
                        "/bin/bash",
                        "-c",
                        truthy_source
                        + "\n"
                        + probe_source
                        + "\nAXONOS_LISTENER_PROBE_BIN=\"$1\" "
                        "critical_supervisor_listeners_ready",
                        "bash",
                        str(fake),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[-len(expected_ports):], expected_ports)

    def test_listener_failures_restart_after_six_consecutive_checks(self) -> None:
        monitor_source = self._startup_function(
            "monitor_critical_supervisor_children",
            "# Launcher-managed tenants",
        )
        harness = """
critical_supervisor_child_is_terminal() { return 1; }
critical_supervisor_listeners_ready() {
    probe_count=$((probe_count + 1))
    return 1
}
sleep() { return 0; }
kill() {
    case "$1" in
        -0) return 0 ;;
        -TERM)
            printf 'terminated-after=%s\\n' "$probe_count"
            return 0
            ;;
    esac
    return 1
}
SUPERVISOR_PID=4242
probe_count=0
monitor_critical_supervisor_children
"""
        result = subprocess.run(
            ["/bin/bash", "-c", monitor_source + harness],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "terminated-after=6")


class IceGatheringFastPathTests(unittest.IsolatedAsyncioTestCase):
    class Peer:
        def __init__(self, state: str) -> None:
            self.iceGatheringState = state
            self.callback = None

        def on(self, _event_name: str):
            def register(callback):
                self.callback = callback
                return callback

            return register

    async def test_already_complete_ice_returns_without_timeout(self) -> None:
        import webrtc_agent_main as agent

        peer = self.Peer("complete")
        completed = await agent._wait_for_ice_gathering_complete(peer, timeout_s=0.01)

        self.assertTrue(completed)

    async def test_ice_transition_resolves_waiter(self) -> None:
        import webrtc_agent_main as agent

        peer = self.Peer("gathering")
        waiter = asyncio.create_task(
            agent._wait_for_ice_gathering_complete(peer, timeout_s=0.1)
        )
        await asyncio.sleep(0)
        peer.iceGatheringState = "complete"
        self.assertIsNotNone(peer.callback)
        peer.callback()

        self.assertTrue(await waiter)

    async def test_ice_timeout_returns_false(self) -> None:
        import webrtc_agent_main as agent

        peer = self.Peer("gathering")

        self.assertFalse(
            await agent._wait_for_ice_gathering_complete(peer, timeout_s=0.001)
        )


if __name__ == "__main__":
    unittest.main()
