"""
Single-active-session lock and FIFO queue for AxonOS demo deployments.

Only one wallet address may hold the desktop at a time.  Other verified
wallets join a queue and are admitted in order when the active session ends
(explicit release, credit exhaustion, or heartbeat timeout).

All state is stored in the same Postgres database used for auth tokens and
challenges (AXGT_CHALLENGE_DB_URL).
"""

import logging
import os
import subprocess
import time
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SESSION_TABLE = "axgt_sessions"
_QUEUE_TABLE = "axgt_queue"

_pg_init_done = False
_pg_init_lock = Lock()


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _db_url() -> Optional[str]:
    return os.getenv("AXGT_CHALLENGE_DB_URL") or None


def _session_max_seconds() -> int:
    raw = (os.getenv("AXGT_SESSION_MAX_MINUTES") or "").strip()
    try:
        val = int(raw)
        if val > 0:
            return val * 60
    except (ValueError, TypeError):
        pass
    return 60 * 60  # default 60 min


def _heartbeat_timeout_seconds() -> int:
    raw = (os.getenv("AXGT_HEARTBEAT_TIMEOUT_SECONDS") or "").strip()
    try:
        val = int(raw)
        if val > 0:
            return val
    except (ValueError, TypeError):
        pass
    return 120  # default 2 min


def _session_cooldown_seconds() -> int:
    """Grace period after session release before the same wallet can reclaim."""
    raw = (os.getenv("AXGT_SESSION_COOLDOWN_SECONDS") or "").strip()
    try:
        val = int(raw)
        if val >= 0:
            return val
    except (ValueError, TypeError):
        pass
    return 0


def _reset_script_path() -> Optional[str]:
    raw = (os.getenv("AXGT_SESSION_RESET_SCRIPT") or "").strip()
    if raw:
        return raw
    # Default path in container (Feature 2 Option A desktop reset)
    default = "/usr/local/bin/reset_session.sh"
    return default if os.path.isfile(default) else None


# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------

def _get_connection():
    url = _db_url()
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as exc:
        logger.warning("session_manager: Postgres connect failed: %s", exc)
        return None


def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_SESSION_TABLE} (
                id          SERIAL PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                started_at  DOUBLE PRECISION NOT NULL,
                last_heartbeat DOUBLE PRECISION NOT NULL,
                last_billed_at DOUBLE PRECISION,
                expires_at  DOUBLE PRECISION NOT NULL,
                status      TEXT NOT NULL DEFAULT 'active'
            )
        """)
        # Add last_billed_at if table existed from before migration
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s AND column_name = 'last_billed_at'
        """, (_SESSION_TABLE,))
        if cur.fetchone() is None:
            cur.execute(f"ALTER TABLE {_SESSION_TABLE} ADD COLUMN last_billed_at DOUBLE PRECISION")
        # Ensure no NULL last_billed_at: bill from session start (fixes pre-migration or old migrations)
        cur.execute(
            f"UPDATE {_SESSION_TABLE} SET last_billed_at = started_at WHERE last_billed_at IS NULL"
        )
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{_SESSION_TABLE}_status
            ON {_SESSION_TABLE} (status)
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_QUEUE_TABLE} (
                id             SERIAL PRIMARY KEY,
                wallet_address TEXT NOT NULL UNIQUE,
                queued_at      DOUBLE PRECISION NOT NULL,
                notified_at    DOUBLE PRECISION
            )
        """)
    conn.commit()


def _init_once() -> bool:
    global _pg_init_done
    if not _db_url():
        return False
    with _pg_init_lock:
        if _pg_init_done:
            return True
        conn = _get_connection()
        if not conn:
            return False
        try:
            _ensure_tables(conn)
            _pg_init_done = True
            return True
        except Exception as exc:
            logger.warning("session_manager: table init failed: %s", exc)
            return False
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _expire_stale_session(cur, now: float) -> Optional[tuple]:
    """End the active session if heartbeat timed out or hard limit exceeded.

    Returns (wallet_address, session_id) of the ended session, or None.
    """
    hb_cutoff = now - _heartbeat_timeout_seconds()
    cur.execute(
        f"""UPDATE {_SESSION_TABLE}
            SET status = 'ended'
            WHERE status = 'active'
              AND (last_heartbeat < %s OR expires_at <= %s)
            RETURNING wallet_address, id""",
        (hb_cutoff, now),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def _get_active_row(cur) -> Optional[Dict[str, Any]]:
    cur.execute(
        f"""SELECT id, wallet_address, started_at, last_heartbeat, last_billed_at, expires_at
            FROM {_SESSION_TABLE}
            WHERE status = 'active'
            ORDER BY started_at DESC
            LIMIT 1""",
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "wallet_address": row[1],
        "started_at": row[2],
        "last_heartbeat": row[3],
        "last_billed_at": row[4],
        "expires_at": row[5],
    }


def _queue_position(cur, wallet_address: str) -> Optional[int]:
    """1-indexed position, or None if not in queue."""
    cur.execute(
        f"""SELECT COUNT(*) FROM {_QUEUE_TABLE}
            WHERE queued_at <= (
                SELECT queued_at FROM {_QUEUE_TABLE}
                WHERE wallet_address = %s
            )""",
        (wallet_address,),
    )
    row = cur.fetchone()
    if not row or row[0] == 0:
        return None
    return row[0]


def _next_in_queue(cur) -> Optional[str]:
    cur.execute(
        f"""SELECT wallet_address FROM {_QUEUE_TABLE}
            ORDER BY queued_at ASC
            LIMIT 1""",
    )
    row = cur.fetchone()
    return row[0] if row else None


def _remove_from_queue(cur, wallet_address: str) -> bool:
    cur.execute(
        f"DELETE FROM {_QUEUE_TABLE} WHERE wallet_address = %s",
        (wallet_address,),
    )
    return cur.rowcount > 0


def _run_reset_script() -> None:
    script = _reset_script_path()
    if not script:
        return
    try:
        logger.info("session_manager: running reset script %s", script)
        subprocess.Popen(
            ["/bin/bash", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.warning("session_manager: reset script failed: %s", exc)


def _mask(addr: str) -> str:
    if not addr or len(addr) < 10:
        return "***"
    return f"{addr[:6]}...{addr[-4:]}"


def _on_session_ended(wallet_address: str, session_id: int) -> None:
    """Record session expiry in ledger and run reset script."""
    try:
        from . import deposit_ledger
    except ImportError:
        try:
            from axonos_gate import deposit_ledger
        except ImportError:
            import deposit_ledger
    if deposit_ledger.init_once():
        remaining = deposit_ledger.get_remaining_minutes(wallet_address)
        deposit_ledger.record_session_expiry(
            wallet_address,
            minutes_deducted=0.0,
            balance_after_minutes=remaining,
            session_id=str(session_id),
        )
    _run_reset_script()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_active_session() -> Optional[Dict[str, Any]]:
    """Return info about the current active session, or None."""
    if not _init_once():
        return None
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            now = time.time()
            ended = _expire_stale_session(cur, now)
            if ended:
                conn.commit()
                wallet_ended, session_id_ended = ended
                logger.info(
                    "session_manager: auto-ended stale session for %s",
                    _mask(wallet_ended),
                )
                _on_session_ended(wallet_ended, session_id_ended)
            return _get_active_row(cur)
    except Exception as exc:
        logger.warning("get_active_session failed: %s", exc)
        return None
    finally:
        conn.close()


def try_claim_session(wallet_address: str) -> Dict[str, Any]:
    """Attempt to claim the desktop session for *wallet_address*.

    Returns a dict with at least ``granted`` (bool).  On failure, includes
    ``queue_position`` and ``active_wallet`` (masked).
    """
    wallet = wallet_address.lower()
    if not _init_once():
        return {"granted": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"granted": False, "reason": "Session DB unavailable"}
    try:
        with conn.cursor() as cur:
            now = time.time()

            ended = _expire_stale_session(cur, now)
            if ended:
                wallet_ended, session_id_ended = ended
                logger.info(
                    "session_manager: auto-ended stale session for %s",
                    _mask(wallet_ended),
                )

            active = _get_active_row(cur)

            # Deposit-credit: require prepaid minutes for any non-owner (claim or queue position).
            try:
                from . import deposit_ledger
            except ImportError:
                try:
                    from axonos_gate import deposit_ledger
                except ImportError:
                    import deposit_ledger
            is_owner = active and active["wallet_address"] == wallet
            if not is_owner:
                if not deposit_ledger.init_once():
                    conn.commit()
                    if ended:
                        _on_session_ended(ended[0], ended[1])
                    return {"granted": False, "reason": "Billing unavailable. Cannot claim without deposit ledger."}
                if deposit_ledger.get_remaining_minutes(wallet) <= 0:
                    conn.commit()
                    if ended:
                        _on_session_ended(ended[0], ended[1])
                    return {"granted": False, "reason": "No prepaid credit. Deposit AXGT and verify tx hash."}

            # Already the owner?
            if active and active["wallet_address"] == wallet:
                remaining = max(0, active["expires_at"] - now)
                conn.commit()
                if ended:
                    _on_session_ended(ended[0], ended[1])
                return {
                    "granted": True,
                    "session_id": active["id"],
                    "remaining_seconds": int(remaining),
                }

            # Session occupied by someone else
            if active:
                pos = _queue_position(cur, wallet)
                conn.commit()
                if ended:
                    _on_session_ended(ended[0], ended[1])
                return {
                    "granted": False,
                    "reason": "Desktop is in use by another researcher.",
                    "active_wallet": _mask(active["wallet_address"]),
                    "queue_position": pos,
                }

            # No active session — check queue priority
            first = _next_in_queue(cur)
            if first and first != wallet:
                pos = _queue_position(cur, wallet)
                conn.commit()
                if ended:
                    _on_session_ended(ended[0], ended[1])
                return {
                    "granted": False,
                    "reason": "Another researcher is next in the queue.",
                    "queue_position": pos,
                }

            # Grant session (with last_billed_at for heartbeat billing)
            _remove_from_queue(cur, wallet)
            max_secs = _session_max_seconds()
            cur.execute(
                f"""INSERT INTO {_SESSION_TABLE}
                    (wallet_address, started_at, last_heartbeat, last_billed_at, expires_at, status)
                    VALUES (%s, %s, %s, %s, %s, 'active')
                    RETURNING id""",
                (wallet, now, now, now, now + max_secs),
            )
            session_id = cur.fetchone()[0]
        conn.commit()
        if ended:
            _on_session_ended(ended[0], ended[1])
        logger.info("session_manager: session granted to %s", _mask(wallet))
        return {
            "granted": True,
            "session_id": session_id,
            "remaining_seconds": max_secs,
        }
    except Exception as exc:
        conn.rollback()
        logger.warning("try_claim_session failed: %s", exc)
        return {"granted": False, "reason": "Internal error"}
    finally:
        conn.close()


def heartbeat(wallet_address: str) -> Dict[str, Any]:
    """Update heartbeat for the active session owner; bill elapsed time from last_billed_at."""
    wallet = wallet_address.lower()
    if not _init_once():
        return {"ok": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"ok": False, "reason": "Session DB unavailable"}
    try:
        from . import deposit_ledger
    except ImportError:
        try:
            from axonos_gate import deposit_ledger
        except ImportError:
            import deposit_ledger

    try:
        now = time.time()
        cur = conn.cursor()
        try:
            ended = _expire_stale_session(cur, now)
            cur.execute(
                f"""SELECT id, last_billed_at, expires_at, started_at
                    FROM {_SESSION_TABLE}
                    WHERE status = 'active' AND wallet_address = %s
                    FOR UPDATE""",
                (wallet,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                if ended:
                    _on_session_ended(ended[0], ended[1])
                return {"ok": False, "reason": "No active session for this wallet"}

            session_id, last_billed_at, expires_at, started_at = row[0], row[1], row[2], row[3]
            # Bill from last checkpoint, or from session start if never billed (e.g. pre-migration row)
            bill_from = last_billed_at if last_billed_at is not None else started_at
            elapsed_seconds = max(0.0, now - bill_from)
            minutes_delta = elapsed_seconds / 60.0

            # If there is billable time but ledger is unavailable, fail the heartbeat (no silent unbilled use).
            if minutes_delta > 0 and not deposit_ledger.init_once():
                conn.commit()
                if ended:
                    _on_session_ended(ended[0], ended[1])
                return {"ok": False, "reason": "Billing unavailable. Cannot record usage."}

            if minutes_delta > 0 and deposit_ledger.init_once():
                # Use same connection/cursor so one transaction; no separate connection or mid-transaction commit
                ok, remaining, err = deposit_ledger._deduct_usage_on_cursor(
                    cur, wallet, minutes_delta, session_id=str(session_id)
                )
                if not ok:
                    conn.commit()
                    if ended:
                        _on_session_ended(ended[0], ended[1])
                    return {"ok": False, "reason": err or "Billing failed"}
                if remaining <= 0:
                    # Terminate session (same cursor: lock still held). Commit first so session is
                    # ended before _on_session_ended runs (it uses separate DB connections and
                    # reads balance / runs reset script; other call sites in this file do commit then _on_session_ended).
                    cur.execute(
                        f"""UPDATE {_SESSION_TABLE} SET status = 'ended'
                            WHERE id = %s AND status = 'active' RETURNING wallet_address""",
                        (session_id,),
                    )
                    ended_row = cur.fetchone()
                    conn.commit()
                    if ended_row:
                        _on_session_ended(ended_row[0], session_id)
                    if ended:
                        _on_session_ended(ended[0], ended[1])
                    return {"ok": False, "reason": "Credit exhausted", "remaining_minutes": 0.0}
                billed_this_heartbeat = True
            else:
                billed_this_heartbeat = False

            # Only advance last_billed_at when we actually deducted usage; otherwise keep baseline for next run
            last_billed_at_value = now if billed_this_heartbeat else bill_from
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET last_heartbeat = %s, last_billed_at = %s
                    WHERE status = 'active' AND wallet_address = %s AND id = %s
                    RETURNING expires_at""",
                (now, last_billed_at_value, wallet, session_id),
            )
            row2 = cur.fetchone()
            # Single commit: persists both deposit_ledger updates (from _deduct_usage_on_cursor) and session row.
            # No commit happens between deduct and this; if anything fails above, rollback in except unwinds all.
            conn.commit()
            if not row2:
                if ended:
                    _on_session_ended(ended[0], ended[1])
                return {"ok": False, "reason": "Session ended"}
            remaining_secs = max(0, row2[0] - now)
            result = {"ok": True, "remaining_seconds": int(remaining_secs)}
            if minutes_delta > 0 and deposit_ledger.init_once():
                result["remaining_minutes"] = round(deposit_ledger.get_remaining_minutes(wallet), 2)
            if ended:
                _on_session_ended(ended[0], ended[1])
            return result
        finally:
            cur.close()
    except Exception as exc:
        conn.rollback()
        logger.warning("heartbeat failed: %s", exc)
        return {"ok": False, "reason": "Internal error"}
    finally:
        conn.close()


def release_session(wallet_address: str) -> Dict[str, Any]:
    """Explicitly end the active session for *wallet_address*."""
    wallet = wallet_address.lower()
    if not _init_once():
        return {"released": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"released": False, "reason": "Session DB unavailable"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET status = 'ended'
                    WHERE status = 'active' AND wallet_address = %s
                    RETURNING id""",
                (wallet,),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return {"released": False, "reason": "No active session for this wallet"}
        session_id = row[0]
        _on_session_ended(wallet, session_id)
        logger.info("session_manager: session released by %s", _mask(wallet))
        return {"released": True}
    except Exception as exc:
        conn.rollback()
        logger.warning("release_session failed: %s", exc)
        return {"released": False, "reason": "Internal error"}
    finally:
        conn.close()


def session_status(wallet_address: Optional[str] = None) -> Dict[str, Any]:
    """Return current session and queue state visible to *wallet_address*."""
    wallet = wallet_address.lower() if wallet_address else None
    if not _init_once():
        return {"active": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"active": False, "reason": "Session DB unavailable"}
    try:
        now = time.time()
        with conn.cursor() as cur:
            ended = _expire_stale_session(cur, now)
            if ended:
                conn.commit()
                _on_session_ended(ended[0], ended[1])

            active = _get_active_row(cur)

            queue_len = 0
            cur.execute(f"SELECT COUNT(*) FROM {_QUEUE_TABLE}")
            row = cur.fetchone()
            if row:
                queue_len = row[0]

            result: Dict[str, Any] = {
                "active": active is not None,
                "queue_length": queue_len,
            }

            if active:
                remaining = max(0, active["expires_at"] - now)
                result["active_wallet"] = _mask(active["wallet_address"])
                result["session_remaining_seconds"] = int(remaining)
                if wallet and active["wallet_address"] == wallet:
                    result["is_owner"] = True

            if wallet:
                pos = _queue_position(cur, wallet)
                result["queue_position"] = pos

        return result
    except Exception as exc:
        logger.warning("session_status failed: %s", exc)
        return {"active": False, "reason": "Internal error"}
    finally:
        conn.close()


def join_queue(wallet_address: str) -> Dict[str, Any]:
    """Add *wallet_address* to the waiting queue. Idempotent. Requires prepaid credit (deposit-credit policy)."""
    wallet = wallet_address.lower()
    if not _init_once():
        return {"joined": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"joined": False, "reason": "Session DB unavailable"}
    try:
        # Require deposit credit before allowing queue join (deposit-credit access control).
        try:
            from . import deposit_ledger
        except ImportError:
            from axonos_gate import deposit_ledger
        if not deposit_ledger.init_once():
            return {"joined": False, "reason": "Billing unavailable. Cannot join queue without deposit ledger."}
        if deposit_ledger.get_remaining_minutes(wallet) <= 0:
            return {"joined": False, "reason": "No prepaid credit. Deposit AXGT and verify tx hash to join queue."}

        now = time.time()
        with conn.cursor() as cur:
            # Already the active user? No need to queue.
            active = _get_active_row(cur)
            if active and active["wallet_address"] == wallet:
                conn.commit()
                return {
                    "joined": False,
                    "reason": "You already own the active session.",
                    "queue_position": None,
                }
            cur.execute(
                f"""INSERT INTO {_QUEUE_TABLE} (wallet_address, queued_at)
                    VALUES (%s, %s)
                    ON CONFLICT (wallet_address) DO NOTHING""",
                (wallet, now),
            )
            pos = _queue_position(cur, wallet)
        conn.commit()
        logger.info("session_manager: %s joined queue at position %s", _mask(wallet), pos)
        return {"joined": True, "queue_position": pos}
    except Exception as exc:
        conn.rollback()
        logger.warning("join_queue failed: %s", exc)
        return {"joined": False, "reason": "Internal error"}
    finally:
        conn.close()


def leave_queue(wallet_address: str) -> Dict[str, Any]:
    """Remove *wallet_address* from the queue."""
    wallet = wallet_address.lower()
    if not _init_once():
        return {"left": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"left": False, "reason": "Session DB unavailable"}
    try:
        with conn.cursor() as cur:
            removed = _remove_from_queue(cur, wallet)
        conn.commit()
        return {"left": removed}
    except Exception as exc:
        conn.rollback()
        logger.warning("leave_queue failed: %s", exc)
        return {"left": False, "reason": "Internal error"}
    finally:
        conn.close()


def get_queue() -> List[Dict[str, Any]]:
    """Return the full queue (for admin/debug). Wallets are masked."""
    if not _init_once():
        return []
    conn = _get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT wallet_address, queued_at FROM {_QUEUE_TABLE} ORDER BY queued_at ASC",
            )
            rows = cur.fetchall()
        return [
            {"wallet": _mask(r[0]), "queued_at": r[1], "position": i + 1}
            for i, r in enumerate(rows)
        ]
    except Exception as exc:
        logger.warning("get_queue failed: %s", exc)
        return []
    finally:
        conn.close()


def is_session_owner(wallet_address: str) -> bool:
    """Fast check: does *wallet_address* own the active session?"""
    wallet = wallet_address.lower()
    if not _init_once():
        return False
    conn = _get_connection()
    if not conn:
        return False
    try:
        now = time.time()
        with conn.cursor() as cur:
            ended = _expire_stale_session(cur, now)
            active = _get_active_row(cur)
        conn.commit()
        if ended:
            _on_session_ended(ended[0], ended[1])
        if not active:
            return False
        return active["wallet_address"] == wallet
    except Exception as exc:
        logger.warning("is_session_owner failed: %s", exc)
        return False
    finally:
        conn.close()
