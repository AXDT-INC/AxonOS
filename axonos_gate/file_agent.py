#!/usr/bin/env python3
"""
AxonOS per-session file transfer agent.

Runs inside every desktop container (axgt-session-* and the shared
single-instance image) as the desktop user and exposes a small HTTP API that
the gate proxies browser file transfers to over the docker network:

  GET  /healthz                            liveness (no auth)
  GET  /list?path=REL                      directory listing inside the root
  GET  /download?path=REL                  stream a file (Range / If-Range)
  GET  /upload-status?path=REL&total=N     resume offset for a partial upload
  PUT  /upload?path=REL&offset=N&total=N   append one chunk (octet-stream)
  POST /mkdir?path=REL                     create a directory
  POST /cancel-upload?path=REL             drop a partial upload

Auth: every request except /healthz must carry X-AxonOS-Files-Key matching
AXGT_SESSION_FILES_KEY (unique per session, injected at docker run; see
session_manager/session_launcher). Session containers share a docker network,
so a per-session key is what prevents one tenant from reaching another
tenant's agent. When the env key is unset (shared single-instance mode) a
random key is persisted to AXGT_FILES_KEY_FILE for the same-container gate to
read.

Uploads are resumable: chunks append to "<name>.axup-partial" beside the
target and the partial is atomically renamed over the final path once
offset+chunk == total. Downloads support byte ranges so the browser's native
download manager can pause and resume.
"""

import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [file-agent] %(message)s")
logger = logging.getLogger(__name__)

PARTIAL_SUFFIX = ".axup-partial"
META_SUFFIX = ".axup-meta"
STREAM_CHUNK = 256 * 1024
UPLOAD_READ_CHUNK = 1024 * 1024


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def files_root() -> str:
    raw = (os.getenv("AXGT_FILES_ROOT") or "/home/aXonian").strip() or "/home/aXonian"
    return os.path.realpath(raw)


def _key_file_path() -> str:
    return (os.getenv("AXGT_FILES_KEY_FILE") or "/tmp/.axgt_files_key").strip()


def load_or_create_key() -> str:
    """Per-session env key when launched by the gate; else a local random key
    shared with the gate processes in the same container (single-instance mode)."""
    key = (os.getenv("AXGT_SESSION_FILES_KEY") or "").strip()
    if key:
        return key
    path = _key_file_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    key = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def min_free_bytes() -> int:
    return _int_env("AXGT_FILES_MIN_FREE_BYTES", 1024 * 1024 * 1024)


def max_file_bytes() -> int:
    """0 disables the per-file cap (persistent storage is billed per GB-hour)."""
    return _int_env("AXGT_FILES_MAX_FILE_BYTES", 0)


_upload_locks: dict = {}
_upload_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _upload_locks_guard:
        lock = _upload_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _upload_locks[path] = lock
        return lock


class PathError(Exception):
    pass


def resolve_rel(rel: str, for_write: bool = False) -> str:
    """Map a client-relative path to an absolute path confined to the root.

    Reads resolve the full path (symlinks cannot escape); writes resolve the
    parent so the final component is created as a regular file/dir.
    """
    rel = (rel or "").strip().lstrip("/")
    if "\x00" in rel or any(ord(c) < 0x20 for c in rel):
        raise PathError("invalid characters in path")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathError("path traversal rejected")
    root = files_root()
    abs_path = os.path.join(root, *parts) if parts else root
    probe = os.path.realpath(os.path.dirname(abs_path) if for_write else abs_path)
    if probe != root and not probe.startswith(root + os.sep):
        raise PathError("path escapes storage root")
    return abs_path


def _is_internal_name(name: str) -> bool:
    return name.endswith(PARTIAL_SUFFIX) or name.endswith(META_SUFFIX)


def _read_meta(partial_path: str) -> dict:
    try:
        with open(partial_path + META_SUFFIX, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(partial_path: str, meta: dict) -> None:
    with open(partial_path + META_SUFFIX, "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _drop_partial(partial_path: str) -> None:
    for p in (partial_path, partial_path + META_SUFFIX):
        try:
            os.remove(p)
        except OSError:
            pass


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str, size: int):
    """Single byte-range only; returns (start, end_inclusive) or None for full body."""
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        # suffix range: last N bytes
        n = int(end_s)
        if n <= 0:
            return None
        start = max(0, size - n)
        return (start, size - 1)
    start = int(start_s)
    if start >= size:
        return "unsatisfiable"
    end = int(end_s) if end_s else size - 1
    return (start, min(end, size - 1))


class FileAgentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AxonOSFileAgent/1.0"
    agent_key = ""  # set on the class at startup

    def log_message(self, fmt, *args):  # route through logging, drop paths/keys
        logger.info("%s %s", self.command, fmt % args)

    # -- plumbing ----------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorized(self) -> bool:
        supplied = (self.headers.get("X-AxonOS-Files-Key") or "").strip()
        return bool(supplied) and hmac.compare_digest(supplied, self.agent_key)

    def _query(self) -> dict:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        return {k: v[0] for k, v in qs.items() if v}

    def _route(self) -> str:
        return urllib.parse.urlparse(self.path).path

    def _drain_body(self) -> None:
        """Consume any unread request body so keep-alive framing stays valid."""
        try:
            remaining = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            remaining = 0
        while remaining > 0:
            chunk = self.rfile.read(min(UPLOAD_READ_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    # -- verbs -------------------------------------------------------------

    def do_GET(self):
        route = self._route()
        if route == "/healthz":
            return self._send_json(200, {"ok": True})
        if not self._authorized():
            return self._send_json(403, {"ok": False, "error": "forbidden"})
        if route == "/list":
            return self._handle_list()
        if route == "/download":
            return self._handle_download()
        if route == "/upload-status":
            return self._handle_upload_status()
        return self._send_json(404, {"ok": False, "error": "unknown route"})

    def do_PUT(self):
        if not self._authorized():
            # Don't drain unauthenticated bodies (could be huge); drop the link.
            self.close_connection = True
            return self._send_json(403, {"ok": False, "error": "forbidden"})
        if self._route() == "/upload":
            return self._handle_upload()
        self._drain_body()
        return self._send_json(404, {"ok": False, "error": "unknown route"})

    def do_POST(self):
        if not self._authorized():
            self.close_connection = True
            return self._send_json(403, {"ok": False, "error": "forbidden"})
        route = self._route()
        self._drain_body()
        if route == "/mkdir":
            return self._handle_mkdir()
        if route == "/cancel-upload":
            return self._handle_cancel_upload()
        return self._send_json(404, {"ok": False, "error": "unknown route"})

    # -- handlers ----------------------------------------------------------

    def _handle_list(self):
        q = self._query()
        try:
            abs_path = resolve_rel(q.get("path", ""))
        except PathError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        if not os.path.isdir(abs_path):
            return self._send_json(404, {"ok": False, "error": "directory not found"})
        entries = []
        try:
            with os.scandir(abs_path) as it:
                for entry in it:
                    if _is_internal_name(entry.name):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    entries.append({
                        "name": entry.name,
                        "type": "dir" if is_dir else "file",
                        "size": 0 if is_dir else int(st.st_size),
                        "mtime": int(st.st_mtime),
                    })
        except OSError as exc:
            return self._send_json(500, {"ok": False, "error": str(exc)})
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        usage = shutil.disk_usage(files_root())
        rel = os.path.relpath(abs_path, files_root())
        return self._send_json(200, {
            "ok": True,
            "path": "" if rel == "." else rel,
            "entries": entries,
            "disk_free_bytes": usage.free,
            "disk_total_bytes": usage.total,
        })

    def _handle_download(self):
        q = self._query()
        try:
            abs_path = resolve_rel(q.get("path", ""))
        except PathError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        if not os.path.isfile(abs_path):
            return self._send_json(404, {"ok": False, "error": "file not found"})
        try:
            st = os.stat(abs_path)
            f = open(abs_path, "rb")
        except OSError as exc:
            return self._send_json(500, {"ok": False, "error": str(exc)})

        size = st.st_size
        etag = f'"{size:x}-{int(st.st_mtime):x}"'
        range_header = self.headers.get("Range") or ""
        if_range = (self.headers.get("If-Range") or "").strip()
        if if_range and if_range != etag:
            range_header = ""  # file changed since first chunk: restart full
        rng = parse_range(range_header, size) if size else None

        with f:
            if rng == "unsatisfiable":
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if rng:
                start, end = rng
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                start, end = 0, size - 1
                self.send_response(200)
            length = (end - start + 1) if size else 0
            filename = os.path.basename(abs_path)
            quoted = urllib.parse.quote(filename)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quoted}",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if length == 0:
                return
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(STREAM_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # client paused/aborted; Range lets it resume

    def _handle_upload_status(self):
        q = self._query()
        try:
            abs_path = resolve_rel(q.get("path", ""), for_write=True)
        except PathError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        try:
            total = int(q.get("total", "-1"))
        except ValueError:
            return self._send_json(400, {"ok": False, "error": "total must be an integer"})
        partial = abs_path + PARTIAL_SUFFIX
        offset = 0
        if os.path.isfile(partial):
            meta = _read_meta(partial)
            if int(meta.get("total", -2)) == total:
                offset = os.path.getsize(partial)
            else:
                _drop_partial(partial)  # different file under same name: restart
        exists = os.path.isfile(abs_path)
        return self._send_json(200, {
            "ok": True,
            "offset": offset,
            "exists": exists,
            "existing_size": os.path.getsize(abs_path) if exists else 0,
        })

    def _handle_upload(self):
        q = self._query()
        try:
            abs_path = resolve_rel(q.get("path", ""), for_write=True)
        except PathError as exc:
            self._drain_body()
            return self._send_json(400, {"ok": False, "error": str(exc)})
        if not q.get("path") or _is_internal_name(os.path.basename(abs_path)):
            self._drain_body()
            return self._send_json(400, {"ok": False, "error": "invalid target name"})
        if os.path.isdir(abs_path):
            self._drain_body()
            return self._send_json(409, {"ok": False, "error": "target is a directory"})
        try:
            offset = int(q.get("offset", "0"))
            total = int(q.get("total", "-1"))
            content_length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._drain_body()
            return self._send_json(400, {"ok": False, "error": "offset/total must be integers"})
        overwrite = q.get("overwrite", "0").strip() in ("1", "true", "yes")
        if total < 0 or offset < 0 or offset + content_length > total:
            self._drain_body()
            return self._send_json(400, {"ok": False, "error": "inconsistent offset/total"})
        cap = max_file_bytes()
        if cap and total > cap:
            self._drain_body()
            return self._send_json(413, {"ok": False, "error": f"file exceeds limit of {cap} bytes"})
        usage = shutil.disk_usage(files_root())
        if usage.free - content_length < min_free_bytes():
            self._drain_body()
            return self._send_json(507, {"ok": False, "error": "insufficient disk space"})

        partial = abs_path + PARTIAL_SUFFIX
        with _lock_for(abs_path):
            if offset == 0:
                if os.path.exists(abs_path) and not overwrite:
                    self._drain_body()
                    return self._send_json(409, {
                        "ok": False, "error": "exists",
                        "detail": "file already exists; pass overwrite=1 to replace",
                    })
                os.makedirs(os.path.dirname(partial), exist_ok=True)
                with open(partial, "wb"):
                    pass
                _write_meta(partial, {"total": total})
            else:
                if not os.path.isfile(partial) or int(_read_meta(partial).get("total", -2)) != total:
                    self._drain_body()
                    return self._send_json(409, {"ok": False, "error": "no matching partial", "offset": 0})
                current = os.path.getsize(partial)
                if current != offset:
                    self._drain_body()
                    return self._send_json(409, {"ok": False, "error": "offset mismatch", "offset": current})

            remaining = content_length
            try:
                with open(partial, "ab") as out:
                    while remaining > 0:
                        chunk = self.rfile.read(min(UPLOAD_READ_CHUNK, remaining))
                        if not chunk:
                            raise ConnectionError("client stream ended early")
                        out.write(chunk)
                        remaining -= len(chunk)
                    out.flush()
                    os.fsync(out.fileno())
            except (ConnectionError, OSError) as exc:
                # Partial stays on disk at a consistent size for resume.
                logger.warning("upload chunk failed: %s", exc)
                try:
                    return self._send_json(500, {
                        "ok": False, "error": "chunk write failed",
                        "offset": os.path.getsize(partial) if os.path.isfile(partial) else 0,
                    })
                except Exception:
                    return

            new_size = os.path.getsize(partial)
            complete = new_size == total
            if complete:
                os.replace(partial, abs_path)
                try:
                    os.remove(partial + META_SUFFIX)
                except OSError:
                    pass
        return self._send_json(200, {"ok": True, "offset": new_size, "complete": complete})

    def _handle_mkdir(self):
        q = self._query()
        try:
            abs_path = resolve_rel(q.get("path", ""), for_write=True)
        except PathError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        if not q.get("path"):
            return self._send_json(400, {"ok": False, "error": "path required"})
        try:
            os.makedirs(abs_path, exist_ok=True)
        except OSError as exc:
            return self._send_json(500, {"ok": False, "error": str(exc)})
        return self._send_json(200, {"ok": True})

    def _handle_cancel_upload(self):
        q = self._query()
        try:
            abs_path = resolve_rel(q.get("path", ""), for_write=True)
        except PathError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        _drop_partial(abs_path + PARTIAL_SUFFIX)
        return self._send_json(200, {"ok": True})


def main():
    host = (os.getenv("AXGT_FILES_BIND_HOST") or "0.0.0.0").strip()
    port = _int_env("AXGT_FILES_PORT", 8767)
    FileAgentHandler.agent_key = load_or_create_key()
    root = files_root()
    os.makedirs(root, exist_ok=True)
    server = ThreadingHTTPServer((host, port), FileAgentHandler)
    server.daemon_threads = True
    logger.info("file agent listening on %s:%d root=%s", host, port, root)
    server.serve_forever()


if __name__ == "__main__":
    main()
