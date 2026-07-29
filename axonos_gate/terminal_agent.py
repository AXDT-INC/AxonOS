#!/usr/bin/env python3
"""Private per-session PTY service for the AxonOS browser terminal.

The root-owned broker runs inside an SSH-only tenant container, while every PTY
child drops to ``aXonian`` before exec. It is reachable only on the tenant's
Docker network; the launcher deliberately does not publish its port on the
host. The central gate authenticates with the existing per-session
``AXGT_SESSION_FILES_KEY`` and translates the framed TCP stream to the browser
WebSocket.

Protocol v1
-----------

The client first sends one JSON line (maximum 4096 bytes)::

    {"version":1,"secret":"...","session_id":"45","cols":80,"rows":24}\n

The agent replies with one JSON line.  On success it is
``{"ok":true,"version":1}``; failures are followed by connection close.
Only after successful authentication is a PTY and login shell created.

Subsequent messages are framed as ``type:u8 + length:u32be + payload``:

* ``I``: raw terminal input (client -> agent, at most 64 KiB)
* ``R``: JSON ``{"cols":N,"rows":N}`` resize (client -> agent)
* ``P``: empty keepalive (client -> agent)
* ``C``: empty orderly close (client -> agent)
* ``O``: raw terminal output (agent -> client)
* ``X``: JSON exit status (agent -> client)
* ``E``: JSON protocol/runtime error (agent -> client)

Output is read from the PTY only after the previous frame has drained, which
provides bounded backpressure rather than buffering an unbounded command's
output in memory.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hmac
import json
import logging
import os
import pwd
import signal
import struct
import subprocess
import termios
from dataclasses import dataclass
from typing import Optional, Tuple


logging.basicConfig(level=logging.INFO, format="%(asctime)s terminal-agent %(message)s")
log = logging.getLogger("terminal-agent")

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8791
MAX_HANDSHAKE_BYTES = 4096
MAX_INPUT_BYTES = 64 * 1024
MAX_RESIZE_BYTES = 1024
OUTPUT_CHUNK_BYTES = 16 * 1024
HANDSHAKE_TIMEOUT_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 10.0
FRAME_HEADER = struct.Struct("!cI")

CLIENT_FRAME_LIMITS = {
    b"I": MAX_INPUT_BYTES,
    b"R": MAX_RESIZE_BYTES,
    b"P": 0,
    b"C": 0,
}


class ProtocolError(Exception):
    """A peer supplied a malformed or out-of-bounds terminal message."""


def _bounded_int(value: object, minimum: int, maximum: int, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def normalize_size(cols: object, rows: object) -> Tuple[int, int]:
    """Return a safe PTY size; hostile or nonsensical dimensions use defaults."""
    return (
        _bounded_int(cols, 2, 1000, 80),
        _bounded_int(rows, 1, 1000, 24),
    )


def encode_frame(frame_type: bytes, payload: bytes = b"") -> bytes:
    if not isinstance(frame_type, bytes) or len(frame_type) != 1:
        raise ValueError("frame_type must be one byte")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("frame payload exceeds maximum")
    return FRAME_HEADER.pack(frame_type, len(payload)) + payload


async def read_client_frame(reader: asyncio.StreamReader) -> Tuple[bytes, bytes]:
    header = await reader.readexactly(FRAME_HEADER.size)
    frame_type, length = FRAME_HEADER.unpack(header)
    limit = CLIENT_FRAME_LIMITS.get(frame_type)
    if limit is None:
        raise ProtocolError("unsupported frame type")
    if length > limit:
        raise ProtocolError("frame payload exceeds maximum")
    payload = await reader.readexactly(length) if length else b""
    return frame_type, payload


async def _send_json_line(writer: asyncio.StreamWriter, payload: dict) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    writer.write(encoded)
    await asyncio.wait_for(writer.drain(), timeout=WRITE_TIMEOUT_SECONDS)


async def _send_frame(
    writer: asyncio.StreamWriter,
    frame_type: bytes,
    payload: bytes = b"",
) -> None:
    writer.write(encode_frame(frame_type, payload))
    await asyncio.wait_for(writer.drain(), timeout=WRITE_TIMEOUT_SECONDS)


def _set_pty_size(fd: int, cols: object, rows: object) -> Tuple[int, int]:
    safe_cols, safe_rows = normalize_size(cols, rows)
    packed = struct.pack("HHHH", safe_rows, safe_cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
    return safe_cols, safe_rows


def _terminal_user() -> pwd.struct_passwd:
    return pwd.getpwnam("aXonian")


def _shell_environment(user: pwd.struct_passwd) -> dict:
    """Build a minimal environment; never copy the secret-bearing agent env."""
    home = os.path.realpath(user.pw_dir or "/home/aXonian")
    if not os.path.isabs(home):
        home = "/home/aXonian"
    return {
        "HOME": home,
        "USER": "aXonian",
        "LOGNAME": "aXonian",
        "SHELL": "/bin/bash",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }


def _acquire_controlling_tty() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _prepare_terminal_child(user: pwd.struct_passwd) -> None:
    """Acquire the slave as controlling TTY, then drop to the tenant user."""
    _acquire_controlling_tty()
    os.initgroups(user.pw_name, user.pw_gid)
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)


@dataclass
class PtyProcess:
    process: subprocess.Popen
    master_fd: int

    @classmethod
    def spawn(cls, cols: object, rows: object) -> "PtyProcess":
        """Create the PTY only after the caller has authenticated the gateway."""
        user = _terminal_user()
        effective_uid = os.geteuid() if hasattr(os, "geteuid") else user.pw_uid
        if effective_uid not in (0, user.pw_uid):
            raise PermissionError("terminal agent cannot become aXonian")
        master_fd, slave_fd = os.openpty()
        try:
            _set_pty_size(slave_fd, cols, rows)
            env = _shell_environment(user)
            home = env["HOME"]
            if not os.path.isdir(home):
                home = "/home/aXonian"
            process = subprocess.Popen(
                ["/bin/bash", "--login"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=home,
                env=env,
                close_fds=True,
                preexec_fn=(
                    (lambda: _prepare_terminal_child(user))
                    if effective_uid == 0
                    else _acquire_controlling_tty
                ),
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        os.set_blocking(master_fd, False)
        return cls(process=process, master_fd=master_fd)

    def resize(self, cols: object, rows: object) -> Tuple[int, int]:
        size = _set_pty_size(self.master_fd, cols, rows)
        try:
            os.killpg(self.process.pid, signal.SIGWINCH)
        except (ProcessLookupError, PermissionError):
            pass
        return size

    async def wait(self) -> int:
        while self.process.poll() is None:
            await asyncio.sleep(0.05)
        return int(self.process.returncode or 0)

    async def terminate(self) -> int:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            try:
                return await asyncio.wait_for(self.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            return await asyncio.wait_for(self.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            return -int(signal.SIGKILL)

    def close(self) -> None:
        try:
            os.close(self.master_fd)
        except OSError:
            pass


async def _read_pty(fd: int) -> bytes:
    """Wait for one nonblocking PTY read without building an output queue."""
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def on_readable() -> None:
        if ready.done():
            return
        try:
            data = os.read(fd, OUTPUT_CHUNK_BYTES)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno == errno.EIO:  # Linux PTYs report EIO after slave close.
                data = b""
            else:
                ready.set_exception(exc)
                return
        ready.set_result(data)

    loop.add_reader(fd, on_readable)
    try:
        return await ready
    finally:
        loop.remove_reader(fd)


async def _wait_pty_writable(fd: int) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def on_writable() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_writer(fd, on_writable)
    try:
        await ready
    finally:
        loop.remove_writer(fd)


async def _write_pty(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except BlockingIOError:
            await _wait_pty_writable(fd)
            continue
        if written <= 0:
            raise OSError("PTY input closed")
        remaining = remaining[written:]


async def _pump_output(pty_process: PtyProcess, writer: asyncio.StreamWriter) -> None:
    while True:
        data = await _read_pty(pty_process.master_fd)
        if not data:
            return
        await _send_frame(writer, b"O", data)


async def _pump_input(reader: asyncio.StreamReader, pty_process: PtyProcess) -> str:
    while True:
        frame_type, payload = await read_client_frame(reader)
        if frame_type == b"I":
            await asyncio.wait_for(
                _write_pty(pty_process.master_fd, payload),
                timeout=WRITE_TIMEOUT_SECONDS,
            )
        elif frame_type == b"R":
            try:
                size = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError("invalid resize payload") from exc
            if not isinstance(size, dict):
                raise ProtocolError("invalid resize payload")
            pty_process.resize(size.get("cols"), size.get("rows"))
        elif frame_type == b"P":
            continue
        elif frame_type == b"C":
            return "close"


class TerminalServer:
    """Authenticated, bounded PTY server used by the supervisor and tests."""

    def __init__(
        self,
        secret: str,
        session_id: str,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        max_clients: int = 1,
    ) -> None:
        self.secret = str(secret or "")
        self.session_id = str(session_id or "")
        self.host = host
        self.port = int(port)
        self.max_clients = max(1, min(4, int(max_clients)))
        self.active_clients = 0
        self.server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> asyncio.AbstractServer:
        if not self.secret or not self.session_id:
            raise RuntimeError("terminal agent requires a session id and secret")
        self.server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port,
            limit=MAX_HANDSHAKE_BYTES + 1,
            backlog=max(8, self.max_clients * 2),
        )
        return self.server

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _read_handshake(self, reader: asyncio.StreamReader) -> dict:
        try:
            raw = await asyncio.wait_for(
                reader.readline(), timeout=HANDSHAKE_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, ValueError) as exc:
            raise ProtocolError("invalid handshake") from exc
        if not raw or not raw.endswith(b"\n") or len(raw) > MAX_HANDSHAKE_BYTES:
            raise ProtocolError("invalid handshake")
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("invalid handshake") from exc
        if not isinstance(message, dict):
            raise ProtocolError("invalid handshake")
        supplied_secret = message.get("secret")
        if not isinstance(supplied_secret, str) or not hmac.compare_digest(
            supplied_secret.encode("utf-8"), self.secret.encode("utf-8")
        ):
            raise ProtocolError("authentication failed")
        if message.get("version") != PROTOCOL_VERSION:
            raise ProtocolError("unsupported protocol version")
        if str(message.get("session_id", "")) != self.session_id:
            raise ProtocolError("session mismatch")
        return message

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        pty_process: Optional[PtyProcess] = None
        reserved = False
        tasks = []
        peer = writer.get_extra_info("peername")
        try:
            # Reserve before reading auth so slow/hostile pre-auth peers are
            # bounded by the same client limit and five-second deadline.
            if self.active_clients >= self.max_clients:
                await _send_json_line(writer, {"ok": False, "error": "terminal busy"})
                return
            self.active_clients += 1
            reserved = True
            handshake = await self._read_handshake(reader)

            pty_process = PtyProcess.spawn(
                handshake.get("cols"), handshake.get("rows")
            )
            transport = writer.transport
            if transport is not None:
                transport.set_write_buffer_limits(high=256 * 1024, low=64 * 1024)
            await _send_json_line(writer, {"ok": True, "version": PROTOCOL_VERSION})
            log.info("session %s terminal attached from %s", self.session_id, peer)

            input_task = asyncio.create_task(_pump_input(reader, pty_process))
            output_task = asyncio.create_task(_pump_output(pty_process, writer))
            process_task = asyncio.create_task(pty_process.wait())
            tasks = [input_task, output_task, process_task]
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            protocol_error: Optional[str] = None
            for task in done:
                if task is process_task:
                    continue
                try:
                    task.result()
                except (ProtocolError, asyncio.IncompleteReadError) as exc:
                    protocol_error = str(exc) or "terminal protocol error"
                except (ConnectionError, BrokenPipeError):
                    pass

            if process_task not in done:
                if output_task in done:
                    try:
                        await asyncio.wait_for(process_task, timeout=0.5)
                    except asyncio.TimeoutError:
                        await pty_process.terminate()
                else:
                    await pty_process.terminate()
            elif not output_task.done():
                # Preserve the final prompt/command output already buffered in
                # the PTY after the shell exits, while keeping cleanup bounded.
                try:
                    await asyncio.wait_for(output_task, timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            exit_code = pty_process.process.poll()

            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            if protocol_error:
                await _send_frame(
                    writer,
                    b"E",
                    json.dumps({"error": protocol_error}, separators=(",", ":")).encode(),
                )
            await _send_frame(
                writer,
                b"X",
                json.dumps({"code": int(exit_code or 0)}, separators=(",", ":")).encode(),
            )
        except ProtocolError as exc:
            try:
                await _send_json_line(writer, {"ok": False, "error": str(exc)})
            except (ConnectionError, asyncio.TimeoutError):
                pass
        except (ConnectionError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("session %s terminal connection failed", self.session_id)
            try:
                if pty_process is None:
                    await _send_json_line(
                        writer, {"ok": False, "error": "terminal unavailable"}
                    )
                else:
                    await _send_frame(
                        writer, b"E", b'{"error":"terminal unavailable"}'
                    )
            except Exception:
                pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if pty_process is not None:
                await pty_process.terminate()
                pty_process.close()
            if reserved:
                self.active_clients = max(0, self.active_clients - 1)
            if pty_process is not None:
                log.info("session %s terminal detached from %s", self.session_id, peer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


async def _run() -> None:
    # Keep the session key in a root-owned broker process.  Its PTY child drops
    # groups/gid/uid to aXonian before exec, so the tenant shell cannot inspect
    # or signal the process which holds the credential.
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise RuntimeError("terminal agent broker must run as root")
    secret = (os.getenv("AXGT_SESSION_FILES_KEY") or "").strip()
    session_id = (os.getenv("AXGT_SESSION_ID") or "").strip()
    max_clients = _env_int("AXGT_TERMINAL_MAX_CLIENTS", 1, 1, 4)
    server = TerminalServer(
        secret=secret,
        session_id=session_id,
        host="0.0.0.0",
        # Protocol v1 deliberately uses one fixed internal port. Per-session
        # container IPs avoid collisions and no central/runtime env forwarding
        # can silently put the two ends on different ports.
        port=DEFAULT_PORT,
        max_clients=max_clients,
    )
    await server.start()
    sockets = server.server.sockets if server.server is not None else []
    addresses = ", ".join(str(sock.getsockname()) for sock in sockets)
    log.info(
        "session %s private terminal agent listening on %s (max clients %d)",
        session_id,
        addresses,
        max_clients,
    )
    assert server.server is not None
    async with server.server:
        await server.server.serve_forever()


def main() -> int:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        log.error("terminal agent failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
