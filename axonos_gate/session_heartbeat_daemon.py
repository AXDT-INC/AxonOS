#!/usr/bin/env python3
"""
In-container session heartbeat daemon for headless / SSH-only AxonOS sessions.

Browser desktop sessions are kept alive by the noVNC UI (ui.js) polling
/api/session/heartbeat. Headless sessions (SSH-only, agent SSH) have no browser,
so without this daemon they are reaped after AXGT_HEARTBEAT_TIMEOUT_SECONDS
(default 120s) and are not billed per-minute. This daemon sends those heartbeats
from inside the session container.

Auth: the session's per-session secret (AXGT_SESSION_FILES_KEY), validated by the
gate against the active session row — no browser wallet token needed.

Each heartbeat also reports ``ssh_active`` — whether an ESTABLISHED TCP
connection to the container's sshd (:22) exists — which the gate uses to renew
the SSH hard billing cap while a user is actually connected (presence-based
extension; see session_manager.heartbeat).

Env (injected at session launch):
  AXGT_WALLET_ADDRESS       the session's wallet
  AXGT_SESSION_FILES_KEY    per-session secret (heartbeat credential)
  AXGT_SESSION_ID           session id (for logging)
  AXGT_GATE_HEARTBEAT_URL   gate base URL (default http://127.0.0.1:8889)
  AXGT_HEARTBEAT_INTERVAL_SECONDS   send interval (default 30)
  AXGT_DESKTOP_ENABLED      if "true", a browser drives heartbeats → daemon idles
"""
import json
import logging
import os
import time
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s heartbeat-daemon %(message)s")
log = logging.getLogger("heartbeat")

WALLET = (os.getenv("AXGT_WALLET_ADDRESS") or "").strip()
FILES_KEY = (os.getenv("AXGT_SESSION_FILES_KEY") or "").strip()
SESSION_ID = (os.getenv("AXGT_SESSION_ID") or "?").strip()
GATE = (os.getenv("AXGT_GATE_HEARTBEAT_URL") or "http://127.0.0.1:8889").rstrip("/")


def _interval() -> int:
    raw = (os.getenv("AXGT_HEARTBEAT_INTERVAL_SECONDS") or "").strip()
    try:
        n = int(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    return 30


def _desktop_enabled() -> bool:
    return (os.getenv("AXGT_DESKTOP_ENABLED") or "true").strip().lower() not in ("0", "false", "no", "off")


def _ssh_connection_active() -> bool:
    """True when at least one ESTABLISHED TCP connection to local port 22 exists.

    Read straight from /proc/net/tcp{,6} (hex local_address:port, state 01 =
    ESTABLISHED) so no external tools are needed. This is the "user present"
    signal: the gate renews the SSH hard billing cap while someone is actually
    connected and lets it lapse when nobody is.
    """
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                next(f, None)  # header row
                for line in f:
                    parts = line.split()
                    if len(parts) < 4 or parts[3] != "01":
                        continue
                    try:
                        local_port = int(parts[1].rsplit(":", 1)[1], 16)
                    except (ValueError, IndexError):
                        continue
                    if local_port == 22:
                        return True
        except OSError:
            continue
    return False


def _send_heartbeat() -> dict:
    payload = {"wallet_address": WALLET}
    try:
        payload["ssh_active"] = _ssh_connection_active()
    except Exception:  # noqa: BLE001 — presence is best-effort, never block the heartbeat
        pass
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        GATE + "/api/session/heartbeat", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-AXGT-Session-Key": FILES_KEY},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    if not WALLET or not FILES_KEY:
        log.warning("missing AXGT_WALLET_ADDRESS or AXGT_SESSION_FILES_KEY — daemon idle")
        # Idle forever so supervisor doesn't flap; nothing to heartbeat for.
        while True:
            time.sleep(3600)

    # Desktop sessions are heartbeated by the browser UI; the daemon is a no-op
    # there to avoid double counting. It runs for headless/SSH sessions.
    if _desktop_enabled():
        log.info("session %s has a desktop (browser drives heartbeats) — daemon idle", SESSION_ID)
        while True:
            time.sleep(3600)

    interval = _interval()
    log.info("session %s headless heartbeat daemon: every %ss -> %s", SESSION_ID, interval, GATE)
    fails = 0
    while True:
        try:
            res = _send_heartbeat()
            if res.get("ok") is False:
                # Session ended/no-credit/etc. — log and keep trying briefly; the
                # container will be stopped by the gate when the session truly ends.
                log.info("session %s heartbeat not ok: %s", SESSION_ID, res.get("reason"))
            fails = 0
        except Exception as exc:  # noqa: BLE001 — keep the loop alive across transient errors
            fails += 1
            log.warning("session %s heartbeat failed (%d): %s", SESSION_ID, fails, exc)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
