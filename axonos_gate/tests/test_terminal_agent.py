import asyncio
import inspect
import json
import os
import pwd
import subprocess
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from axonos_gate import session_heartbeat_daemon, terminal_agent


_REPO_ROOT = Path(__file__).resolve().parents[2]


async def _read_frame(reader):
    header = await asyncio.wait_for(
        reader.readexactly(terminal_agent.FRAME_HEADER.size), timeout=3
    )
    frame_type, length = terminal_agent.FRAME_HEADER.unpack(header)
    payload = await asyncio.wait_for(reader.readexactly(length), timeout=3)
    return frame_type, payload


class _MemoryTransport:
    def set_write_buffer_limits(self, high=None, low=None):
        self.high = high
        self.low = low


class _MemoryWriter:
    def __init__(self):
        self.data = bytearray()
        self.transport = _MemoryTransport()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return ("test-gateway", 1234)
        return default

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class TerminalAgentProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = terminal_agent.TerminalServer(
            secret="session-secret",
            session_id="45",
        )

    async def _handle(self, payload, feed_eof=True):
        reader = asyncio.StreamReader(limit=terminal_agent.MAX_HANDSHAKE_BYTES + 1)
        reader.feed_data(payload)
        if feed_eof:
            reader.feed_eof()
        writer = _MemoryWriter()
        await self.server.handle_client(reader, writer)
        return writer

    async def test_bad_secret_is_rejected_before_pty_creation(self):
        message = {
            "version": 1,
            "secret": "wrong-secret",
            "session_id": "45",
            "cols": 80,
            "rows": 24,
        }
        with patch.object(terminal_agent.PtyProcess, "spawn") as spawn:
            writer = await self._handle(json.dumps(message).encode() + b"\n")
            response = json.loads(bytes(writer.data))
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"], "authentication failed")
            spawn.assert_not_called()
        self.assertTrue(writer.closed)
        self.assertEqual(self.server.active_clients, 0)

    async def test_session_mismatch_is_rejected_before_pty_creation(self):
        message = {
            "version": 1,
            "secret": "session-secret",
            "session_id": "46",
        }
        with patch.object(terminal_agent.PtyProcess, "spawn") as spawn:
            writer = await self._handle(json.dumps(message).encode() + b"\n")
            response = json.loads(bytes(writer.data))
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"], "session mismatch")
            spawn.assert_not_called()
        self.assertTrue(writer.closed)

    async def test_authenticated_terminal_round_trip_and_exit(self):
        message = {
            "version": 1,
            "secret": "session-secret",
            "session_id": "45",
            "cols": 90,
            "rows": 30,
        }
        # The production broker starts as root and resolves aXonian.  Use this
        # test runner's unprivileged account to exercise the same PTY mechanics.
        with patch.object(
            terminal_agent, "_terminal_user", return_value=pwd.getpwuid(os.getuid())
        ):
            request = json.dumps(message).encode() + b"\n"
            request += terminal_agent.encode_frame(
                b"I",
                b"if test -t 0; then printf AXONOS_TERMINAL_TTY_OK; "
                b"else printf AXONOS_TERMINAL_NO_TTY; fi; exit\n",
            )
            writer = await self._handle(request, feed_eof=False)
            response_line, framed = bytes(writer.data).split(b"\n", 1)
            response = json.loads(response_line)
            self.assertEqual(response, {"ok": True, "version": 1})
            response_reader = asyncio.StreamReader()
            response_reader.feed_data(framed)
            response_reader.feed_eof()
            output = bytearray()
            exit_payload = None
            for _ in range(20):
                frame_type, payload = await _read_frame(response_reader)
                if frame_type == b"O":
                    output.extend(payload)
                if frame_type == b"X":
                    exit_payload = json.loads(payload)
                    break
            self.assertIn(b"AXONOS_TERMINAL_TTY_OK", output)
            self.assertNotIn(b"no job control", output.lower())
            self.assertEqual(exit_payload, {"code": 0})
        self.assertTrue(writer.closed)

    async def test_oversized_frame_is_rejected_without_reading_payload(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!cI", b"I", terminal_agent.MAX_INPUT_BYTES + 1))
        with self.assertRaisesRegex(terminal_agent.ProtocolError, "exceeds maximum"):
            await terminal_agent.read_client_frame(reader)


class TerminalAgentBoundaryTests(unittest.TestCase):
    def test_client_limit_is_bounded_and_defaults_to_one(self):
        self.assertEqual(terminal_agent.TerminalServer("key", "1").max_clients, 1)
        self.assertEqual(
            terminal_agent.TerminalServer("key", "1", max_clients=999).max_clients,
            4,
        )

    def test_dimensions_are_bounded(self):
        self.assertEqual(terminal_agent.normalize_size(120, 40), (120, 40))
        self.assertEqual(terminal_agent.normalize_size(0, 99999), (80, 24))
        self.assertEqual(terminal_agent.normalize_size(True, False), (80, 24))

    def test_shell_environment_is_explicit_and_contains_no_agent_secret(self):
        user = pwd.getpwuid(os.getuid())
        with patch.dict(
            os.environ,
            {
                "AXGT_SESSION_FILES_KEY": "do-not-leak",
                "AXGT_WEBRTC_AGENT_TOKEN": "do-not-leak",
                "DATABASE_URL": "do-not-leak",
                "UNRELATED_PARENT_VALUE": "do-not-inherit",
            },
            clear=False,
        ):
            env = terminal_agent._shell_environment(user)
        self.assertEqual(
            set(env),
            {
                "HOME",
                "USER",
                "LOGNAME",
                "SHELL",
                "PATH",
                "LANG",
                "LC_ALL",
                "TERM",
                "COLORTERM",
            },
        )
        self.assertNotIn("do-not-leak", env.values())

    def test_child_acquires_controlling_tty_before_privilege_drop(self):
        tty_source = inspect.getsource(terminal_agent._acquire_controlling_tty)
        drop_source = inspect.getsource(terminal_agent._prepare_terminal_child)
        self.assertLess(tty_source.index("os.setsid()"), tty_source.index("termios.TIOCSCTTY"))
        self.assertLess(
            drop_source.index("_acquire_controlling_tty()"),
            drop_source.index("os.setuid"),
        )

    def test_terminal_port_is_private_not_host_published(self):
        from axonos_gate import session_launcher, session_launcher_service

        for publish in (
            session_launcher._publish_args_for_session,
            session_launcher_service._publish_args_for_session,
        ):
            with self.subTest(module=publish.__module__):
                args = publish(7, True)
                self.assertEqual(args, ["-p", "42007:22/tcp"])
                self.assertNotIn(str(terminal_agent.DEFAULT_PORT), " ".join(args))
                self.assertNotIn("8767", " ".join(args))

    def test_file_agent_runs_for_desktop_or_ssh_without_host_publish(self):
        source = (_REPO_ROOT / "supervisord.conf").read_text(encoding="utf-8")
        section = source.split("[program:file-agent]", 1)[1].split(
            "[program:sshd]", 1
        )[0]
        command_line = next(
            line for line in section.splitlines() if line.startswith("command=")
        )
        prefix = 'command=/bin/bash -c "'
        self.assertTrue(command_line.startswith(prefix))
        self.assertTrue(command_line.endswith('"'))
        command = command_line[len(prefix):-1].replace(r'\"', '"')
        command = command.replace(
            "exec /usr/bin/python3 /axonos_gate/file_agent.py",
            "printf file-agent",
        ).replace("exec sleep infinity", "printf disabled")

        cases = (
            ({"AXGT_DESKTOP_ENABLED": "true", "AXGT_SSH_ENABLED": "false"}, "file-agent"),
            ({"AXGT_DESKTOP_ENABLED": "false", "AXGT_SSH_ENABLED": "true"}, "file-agent"),
            ({"AXGT_DESKTOP_ENABLED": "false", "AXGT_SSH_ENABLED": "false"}, "disabled"),
            ({"AXGT_DESKTOP_ENABLED": "0", "AXGT_SSH_ENABLED": "0"}, "disabled"),
        )
        for overrides, expected in cases:
            with self.subTest(**overrides):
                env = dict(os.environ)
                env.update(overrides)
                result = subprocess.run(
                    ["/bin/bash", "-c", command],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.stdout, expected)

        self.assertIn("user=aXonian", section)
        self.assertIn("AXGT_SESSION_FILES_KEY", source)

    def test_supervisor_uses_root_broker_only_for_keyed_ssh_session(self):
        source = (_REPO_ROOT / "supervisord.conf").read_text(encoding="utf-8")
        section = source.split("[program:terminal-agent]", 1)[1].split(
            "[program:heartbeat-daemon]", 1
        )[0]
        self.assertIn("AXGT_SSH_ENABLED", section)
        self.assertIn("AXGT_SESSION_FILES_KEY", section)
        self.assertIn("/axonos_gate/terminal_agent.py", section)
        self.assertNotIn("user=aXonian", section)

    def test_heartbeat_counts_private_terminal_connection_as_ssh_presence(self):
        tcp = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt\n"
            "   0: 0100007F:2257 0200007F:1234 01 00000000:00000000 00:00000000 00000000\n"
        )
        from io import StringIO

        with patch("builtins.open", side_effect=[StringIO(tcp), StringIO("")]):
            self.assertTrue(session_heartbeat_daemon._ssh_connection_active())


if __name__ == "__main__":
    unittest.main()
