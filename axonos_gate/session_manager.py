"""
Session manager for AxonOS.

Default mode preserves private-beta behavior (single active session + FIFO queue).
Public-beta mode (feature-gated) enables profile-aware, multi-session scheduling
with exclusive full-GPU allocation and per-session container lifecycle hooks.
"""

import logging
import os
import subprocess
import time
from threading import Lock
from typing import Any, Dict, List, Optional, Set, Tuple

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


def _truthy(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _multi_session_enabled() -> bool:
    return _truthy("AXGT_MULTI_SESSION_ENABLED", False)


def _gpu_profiles_enabled() -> bool:
    return _truthy("AXGT_GPU_PROFILES_ENABLED", False)


def _default_profile() -> str:
    return (os.getenv("AXGT_DEFAULT_GPU_PROFILE") or "small").strip().lower()


def _configured_profiles() -> Dict[str, int]:
    # Fixed public-beta profiles
    return {
        "small": 1,
        "medium": 2,
        "large": 4,
    }


def _parse_gpu_device_ids() -> List[int]:
    raw = (os.getenv("AXGT_GPU_DEVICE_IDS") or "").strip()
    if raw:
        out: List[int] = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                out.append(int(token))
            except ValueError:
                logger.warning("session_manager: invalid AXGT_GPU_DEVICE_IDS token: %s", token)
        if out:
            return sorted(set(out))
    raw_count = (os.getenv("AXGT_GPU_TOTAL_COUNT") or "").strip()
    try:
        count = int(raw_count) if raw_count else 0
    except ValueError:
        count = 0
    if count > 0:
        return list(range(count))
    return [0]


def _gpu_device_ids() -> List[int]:
    return _parse_gpu_device_ids()


def _resolve_profile(profile: Optional[str]) -> Tuple[str, int]:
    requested = (profile or "").strip().lower() or _default_profile()
    profiles = _configured_profiles()
    if not _gpu_profiles_enabled():
        return "small", 1
    if requested not in profiles:
        requested = _default_profile()
    if requested not in profiles:
        requested = "small"
    return requested, profiles[requested]


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


def _import_deposit_ledger():
    """Works when loaded as package, as axonos_gate.*, or flat on sys.path (websockify_gate)."""
    try:
        from . import deposit_ledger
    except ImportError:
        try:
            from axonos_gate import deposit_ledger
        except ImportError:
            import deposit_ledger
    return deposit_ledger


def _import_session_launcher():
    """Works when loaded as package, as axonos_gate.*, or flat on sys.path."""
    try:
        from . import session_launcher
    except ImportError:
        try:
            from axonos_gate import session_launcher
        except ImportError:
            import session_launcher
    return session_launcher


def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_SESSION_TABLE} (
                id          SERIAL PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                requested_profile TEXT NOT NULL DEFAULT 'small',
                gpu_ids     TEXT,
                container_id TEXT,
                allocation_status TEXT NOT NULL DEFAULT 'allocated',
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
        for col_name, col_sql in (
            ("requested_profile", "TEXT NOT NULL DEFAULT 'small'"),
            ("gpu_ids", "TEXT"),
            ("container_id", "TEXT"),
            ("allocation_status", "TEXT NOT NULL DEFAULT 'allocated'"),
        ):
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
                """,
                (_SESSION_TABLE, col_name),
            )
            if cur.fetchone() is None:
                cur.execute(f"ALTER TABLE {_SESSION_TABLE} ADD COLUMN {col_name} {col_sql}")
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
                requested_profile TEXT NOT NULL DEFAULT 'small',
                requested_gpus INTEGER NOT NULL DEFAULT 1,
                queue_reason TEXT,
                queued_at      DOUBLE PRECISION NOT NULL,
                notified_at    DOUBLE PRECISION
            )
        """)
        for col_name, col_sql in (
            ("requested_profile", "TEXT NOT NULL DEFAULT 'small'"),
            ("requested_gpus", "INTEGER NOT NULL DEFAULT 1"),
            ("queue_reason", "TEXT"),
        ):
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
                """,
                (_QUEUE_TABLE, col_name),
            )
            if cur.fetchone() is None:
                cur.execute(f"ALTER TABLE {_QUEUE_TABLE} ADD COLUMN {col_name} {col_sql}")
        # Legacy DBs may have axgt_queue without UNIQUE(wallet_address); INSERT ... ON CONFLICT
        # requires a unique index on that column.
        cur.execute(
            """
            SELECT 1 FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = %s
              AND indexdef LIKE '%%UNIQUE%%'
              AND indexdef LIKE '%%wallet_address%%'
            """,
            (_QUEUE_TABLE,),
        )
        if cur.fetchone() is None:
            try:
                cur.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS axgt_queue_wallet_address_uq "
                    f"ON {_QUEUE_TABLE} (wallet_address)"
                )
            except Exception as exc:
                logger.warning(
                    "session_manager: could not add unique index on %s.wallet_address "
                    "(queue join needs this; fix duplicates then re-run): %s",
                    _QUEUE_TABLE,
                    exc,
                )
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


def _parse_gpu_ids(text: Optional[str]) -> List[int]:
    if not text:
        return []
    out: List[int] = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return sorted(set(out))


def _serialize_gpu_ids(gpu_ids: List[int]) -> str:
    return ",".join(str(i) for i in sorted(set(gpu_ids)))


def _session_row_to_dict(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "wallet_address": row[1],
        "requested_profile": row[2] or "small",
        "gpu_ids": _parse_gpu_ids(row[3]),
        "container_id": row[4],
        "allocation_status": row[5] or "allocated",
        "started_at": row[6],
        "last_heartbeat": row[7],
        "last_billed_at": row[8],
        "expires_at": row[9],
    }


def _get_active_rows(cur) -> List[Dict[str, Any]]:
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at
            FROM {_SESSION_TABLE}
            WHERE status = 'active'
            ORDER BY started_at ASC""",
    )
    rows = cur.fetchall() or []
    return [_session_row_to_dict(r) for r in rows]


def _get_active_row(cur) -> Optional[Dict[str, Any]]:
    rows = _get_active_rows(cur)
    if not rows:
        return None
    return rows[-1]


def _active_session_for_wallet(cur, wallet: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at
            FROM {_SESSION_TABLE}
            WHERE status = 'active' AND wallet_address = %s
            ORDER BY started_at DESC
            LIMIT 1""",
        (wallet,),
    )
    row = cur.fetchone()
    return _session_row_to_dict(row) if row else None


def _allocated_gpu_ids(rows: List[Dict[str, Any]]) -> Set[int]:
    out: Set[int] = set()
    for row in rows:
        for gid in row.get("gpu_ids", []):
            out.add(int(gid))
    return out


def _free_gpu_ids(rows: List[Dict[str, Any]]) -> List[int]:
    all_gpu_ids = _gpu_device_ids()
    allocated = _allocated_gpu_ids(rows)
    return [gid for gid in all_gpu_ids if gid not in allocated]


def _queue_entries(cur) -> List[Dict[str, Any]]:
    cur.execute(
        f"""SELECT wallet_address, requested_profile, requested_gpus, queued_at, queue_reason
            FROM {_QUEUE_TABLE}
            ORDER BY queued_at ASC""",
    )
    rows = cur.fetchall() or []
    return [
        {
            "wallet_address": r[0],
            "requested_profile": (r[1] or "small"),
            "requested_gpus": int(r[2] or 1),
            "queued_at": r[3],
            "queue_reason": r[4],
        }
        for r in rows
    ]


def _choose_allocation(rows: List[Dict[str, Any]], requested_gpus: int) -> Optional[List[int]]:
    free_ids = _free_gpu_ids(rows)
    if len(free_ids) < requested_gpus:
        return None
    return free_ids[:requested_gpus]


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


def _queue_length(cur) -> int:
    """Number of wallets currently waiting in the FIFO queue."""
    cur.execute(f"SELECT COUNT(*) FROM {_QUEUE_TABLE}")
    row = cur.fetchone()
    if not row or row[0] is None:
        return 0
    return int(row[0])


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
    _cleanup_session_container(session_id)
    _run_reset_script()


def _cleanup_session_container(session_id: int) -> None:
    launcher = _import_session_launcher()
    launcher.stop_session(session_id=session_id, container_id=None)


def _spawn_session_container(session_id: int, wallet: str, profile: str, gpu_ids: List[int]) -> Tuple[bool, Optional[str], Optional[str]]:
    launcher = _import_session_launcher()
    return launcher.launch_session(
        session_id=session_id,
        wallet=wallet,
        profile=profile,
        gpu_ids=gpu_ids,
    )


def _queue_blocks_allocation(cur, wallet: str, requested_gpus: int, rows: List[Dict[str, Any]]) -> bool:
    if not _multi_session_enabled():
        return False
    entries = _queue_entries(cur)
    free_count = len(_free_gpu_ids(rows))
    for entry in entries:
        if entry["wallet_address"] == wallet:
            return False
        # Practical schedulability: only block on requests that are actually schedulable now.
        # This prevents a large unschedulable request from starving smaller ones.
        if entry["requested_gpus"] <= free_count:
            return True
    return False


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
            rows = _get_active_rows(cur)
            if not rows:
                return None
            if _multi_session_enabled():
                return rows[-1]
            return rows[-1]
    except Exception as exc:
        logger.warning("get_active_session failed: %s", exc)
        return None
    finally:
        conn.close()


def try_claim_session(wallet_address: str, requested_profile: Optional[str] = None) -> Dict[str, Any]:
    """Attempt to claim the desktop session for *wallet_address*.

    Returns a dict with at least ``granted`` (bool).  On failure, includes
    ``queue_position`` and ``active_wallet`` (masked).
    """
    wallet = wallet_address.lower()
    profile_name, requested_gpus = _resolve_profile(requested_profile)
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

            active_rows = _get_active_rows(cur)
            active = active_rows[-1] if active_rows else None

            # Deposit-credit: require prepaid minutes for any non-owner (claim or queue position).
            try:
                from . import deposit_ledger
            except ImportError:
                try:
                    from axonos_gate import deposit_ledger
                except ImportError:
                    import deposit_ledger
            is_owner = _active_session_for_wallet(cur, wallet) is not None
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

            # Already owner in multi-session mode
            owned = _active_session_for_wallet(cur, wallet)
            if owned:
                remaining = max(0, owned["expires_at"] - now)
                conn.commit()
                if ended:
                    _on_session_ended(ended[0], ended[1])
                return {
                    "granted": True,
                    "session_id": owned["id"],
                    "requested_profile": owned.get("requested_profile") or profile_name,
                    "assigned_gpu_ids": owned.get("gpu_ids", []),
                    "container_id": owned.get("container_id"),
                    "allocation_status": "allocated",
                    "remaining_seconds": int(remaining),
                }

            if (not _multi_session_enabled()) and active:
                pos = _queue_position(cur, wallet)
                qlen = _queue_length(cur)
                conn.commit()
                if ended:
                    _on_session_ended(ended[0], ended[1])
                return {
                    "granted": False,
                    "reason": "Desktop is in use by another researcher.",
                    "active_wallet": _mask(active["wallet_address"]),
                    "queue_position": pos,
                    "queue_length": qlen,
                }

            if _multi_session_enabled():
                allocated_gpu_ids = _choose_allocation(active_rows, requested_gpus)
                if not allocated_gpu_ids:
                    pos = _queue_position(cur, wallet)
                    if pos is None:
                        cur.execute(
                            f"""INSERT INTO {_QUEUE_TABLE} (wallet_address, requested_profile, requested_gpus, queue_reason, queued_at)
                                VALUES (%s, %s, %s, %s, %s)
                                ON CONFLICT (wallet_address) DO UPDATE SET
                                    requested_profile = EXCLUDED.requested_profile,
                                    requested_gpus = EXCLUDED.requested_gpus,
                                    queue_reason = EXCLUDED.queue_reason""",
                            (wallet, profile_name, requested_gpus, f"insufficient free GPUs for requested profile ({profile_name})", now),
                        )
                        pos = _queue_position(cur, wallet)
                    qlen = _queue_length(cur)
                    conn.commit()
                    if ended:
                        _on_session_ended(ended[0], ended[1])
                    return {
                        "granted": False,
                        "allocation_status": "queued",
                        "requested_profile": profile_name,
                        "requested_gpus": requested_gpus,
                        "reason": f"Queued waiting for {requested_gpus} GPU(s)",
                        "queue_reason": f"insufficient free GPUs for requested profile ({profile_name})",
                        "queue_position": pos,
                        "queue_length": qlen,
                        "free_gpu_count": len(_free_gpu_ids(active_rows)),
                    }
                if _queue_blocks_allocation(cur, wallet, requested_gpus, active_rows):
                    pos = _queue_position(cur, wallet)
                    if pos is None:
                        cur.execute(
                            f"""INSERT INTO {_QUEUE_TABLE} (wallet_address, requested_profile, requested_gpus, queue_reason, queued_at)
                                VALUES (%s, %s, %s, %s, %s)
                                ON CONFLICT (wallet_address) DO UPDATE SET
                                    requested_profile = EXCLUDED.requested_profile,
                                    requested_gpus = EXCLUDED.requested_gpus,
                                    queue_reason = EXCLUDED.queue_reason""",
                            (wallet, profile_name, requested_gpus, "waiting turn behind schedulable queued request", now),
                        )
                        pos = _queue_position(cur, wallet)
                    qlen = _queue_length(cur)
                    conn.commit()
                    if ended:
                        _on_session_ended(ended[0], ended[1])
                    return {
                        "granted": False,
                        "allocation_status": "queued",
                        "requested_profile": profile_name,
                        "requested_gpus": requested_gpus,
                        "reason": f"Queued waiting for {requested_gpus} GPU(s)",
                        "queue_reason": "waiting turn behind schedulable queued request",
                        "queue_position": pos,
                        "queue_length": qlen,
                    }

                _remove_from_queue(cur, wallet)
                max_secs = _session_max_seconds()
                cur.execute(
                    f"""INSERT INTO {_SESSION_TABLE}
                        (wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                         started_at, last_heartbeat, last_billed_at, expires_at, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
                        RETURNING id""",
                    (
                        wallet,
                        profile_name,
                        _serialize_gpu_ids(allocated_gpu_ids),
                        None,
                        "allocating",
                        now,
                        now,
                        now,
                        now + max_secs,
                    ),
                )
                session_id = cur.fetchone()[0]
                conn.commit()

                spawned, container_id, spawn_error = _spawn_session_container(
                    session_id=session_id,
                    wallet=wallet,
                    profile=profile_name,
                    gpu_ids=allocated_gpu_ids,
                )
                conn2 = _get_connection()
                if conn2:
                    try:
                        with conn2.cursor() as cur2:
                            if spawned:
                                cur2.execute(
                                    f"""UPDATE {_SESSION_TABLE}
                                        SET container_id = %s, allocation_status = 'allocated'
                                        WHERE id = %s AND status = 'active'""",
                                    (container_id, session_id),
                                )
                            else:
                                cur2.execute(
                                    f"""UPDATE {_SESSION_TABLE}
                                        SET status = 'ended', allocation_status = 'failed'
                                        WHERE id = %s AND status = 'active'""",
                                    (session_id,),
                                )
                        conn2.commit()
                    finally:
                        conn2.close()
                if not spawned:
                    if ended:
                        _on_session_ended(ended[0], ended[1])
                    return {
                        "granted": False,
                        "allocation_status": "failed",
                        "requested_profile": profile_name,
                        "requested_gpus": requested_gpus,
                        "reason": "Failed to start user container",
                        "container_error": spawn_error,
                    }
                if ended:
                    _on_session_ended(ended[0], ended[1])
                return {
                    "granted": True,
                    "session_id": session_id,
                    "requested_profile": profile_name,
                    "assigned_gpu_ids": allocated_gpu_ids,
                    "container_id": container_id,
                    "allocation_status": "allocated",
                    "remaining_seconds": max_secs,
                }

            # Legacy single-session queue gate
            first = _next_in_queue(cur)
            if first and first != wallet:
                pos = _queue_position(cur, wallet)
                qlen = _queue_length(cur)
                conn.commit()
                if ended:
                    _on_session_ended(ended[0], ended[1])
                return {
                    "granted": False,
                    "reason": "Another researcher is next in the queue.",
                    "queue_position": pos,
                    "queue_length": qlen,
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
                f"""SELECT id, last_billed_at, expires_at, started_at, requested_profile, gpu_ids, container_id
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
            req_profile = row[4] or "small"
            assigned_gpu_ids = _parse_gpu_ids(row[5])
            container_id = row[6]
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
                    return {
                        "ok": False,
                        "reason": "Credit exhausted",
                        "remaining_minutes": 0.0,
                        "requested_profile": req_profile,
                        "assigned_gpu_ids": assigned_gpu_ids,
                        "container_id": container_id,
                    }
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
            result = {
                "ok": True,
                "remaining_seconds": int(remaining_secs),
                "requested_profile": req_profile,
                "assigned_gpu_ids": assigned_gpu_ids,
                "container_id": container_id,
                "allocation_status": "allocated",
            }
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
                    RETURNING id, requested_profile, gpu_ids, container_id""",
                (wallet,),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return {"released": False, "reason": "No active session for this wallet"}
        session_id = row[0]
        _on_session_ended(wallet, session_id)
        logger.info("session_manager: session released by %s", _mask(wallet))
        return {
            "released": True,
            "requested_profile": row[1] or "small",
            "released_gpu_ids": _parse_gpu_ids(row[2]),
            "container_id": row[3],
        }
    except Exception as exc:
        conn.rollback()
        logger.warning("release_session failed: %s", exc)
        return {"released": False, "reason": "Internal error"}
    finally:
        conn.close()


def restart_desktop_session(wallet_address: str) -> Dict[str, Any]:
    """Restart desktop services for the active session owner without releasing ownership."""
    wallet = wallet_address.lower()
    if not _init_once():
        return {"restarted": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"restarted": False, "reason": "Session DB unavailable"}
    try:
        with conn.cursor() as cur:
            now = time.time()
            ended = _expire_stale_session(cur, now)
            active = _get_active_row(cur)
        conn.commit()
        if ended:
            _on_session_ended(ended[0], ended[1])
        if not active:
            return {"restarted": False, "reason": "No active session"}
        if active["wallet_address"] != wallet:
            return {"restarted": False, "reason": "Only the active session owner can restart"}
        _run_reset_script()
        logger.info("session_manager: desktop restart requested by %s", _mask(wallet))
        return {"restarted": True, "session_id": active["id"]}
    except Exception as exc:
        conn.rollback()
        logger.warning("restart_desktop_session failed: %s", exc)
        return {"restarted": False, "reason": "Internal error"}
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

            active_rows = _get_active_rows(cur)
            active = active_rows[-1] if active_rows else None
            queue_len = _queue_length(cur)
            free_gpu_ids = _free_gpu_ids(active_rows)

            result: Dict[str, Any] = {
                "active": active is not None,
                "queue_length": queue_len,
                "multi_session_enabled": _multi_session_enabled(),
                "gpu_profiles_enabled": _gpu_profiles_enabled(),
                "total_gpus": len(_gpu_device_ids()),
                "free_gpus": free_gpu_ids,
                "active_sessions_count": len(active_rows),
            }

            if active:
                remaining = max(0, active["expires_at"] - now)
                result["active_wallet"] = _mask(active["wallet_address"])
                result["session_remaining_seconds"] = int(remaining)
                result["latest_requested_profile"] = active.get("requested_profile") or "small"
                result["latest_assigned_gpu_ids"] = active.get("gpu_ids", [])
                if wallet and active["wallet_address"] == wallet:
                    result["is_owner"] = True

            if _multi_session_enabled():
                result["active_sessions"] = [
                    {
                        "session_id": row["id"],
                        "wallet_address": row["wallet_address"] if wallet and wallet == row["wallet_address"] else _mask(row["wallet_address"]),
                        "requested_profile": row.get("requested_profile") or "small",
                        "assigned_gpu_ids": row.get("gpu_ids", []),
                        "container_id": row.get("container_id"),
                        "allocation_status": row.get("allocation_status") or "allocated",
                        "started_at": row.get("started_at"),
                        "expires_at": row.get("expires_at"),
                        "last_heartbeat": row.get("last_heartbeat"),
                    }
                    for row in active_rows
                ]

            if wallet:
                pos = _queue_position(cur, wallet)
                result["queue_position"] = pos
                cur.execute(
                    f"SELECT requested_profile, requested_gpus, queue_reason FROM {_QUEUE_TABLE} WHERE wallet_address = %s",
                    (wallet,),
                )
                qrow = cur.fetchone()
                if qrow:
                    result["queued_profile"] = qrow[0] or "small"
                    result["queued_gpus"] = int(qrow[1] or 1)
                    result["queue_reason"] = qrow[2]
                    result["allocation_status"] = "queued"
                    result["reason"] = f"Queued waiting for {result['queued_gpus']} GPU(s)"
                owned = _active_session_for_wallet(cur, wallet)
                if owned:
                    result["is_owner"] = True
                    result["session_id"] = owned["id"]
                    result["requested_profile"] = owned.get("requested_profile") or "small"
                    result["assigned_gpu_ids"] = owned.get("gpu_ids", [])
                    result["container_id"] = owned.get("container_id")
                    result["allocation_status"] = owned.get("allocation_status") or "allocated"

        return result
    except Exception as exc:
        logger.warning("session_status failed: %s", exc)
        return {"active": False, "reason": "Internal error"}
    finally:
        conn.close()


def join_queue(wallet_address: str, requested_profile: Optional[str] = None) -> Dict[str, Any]:
    """Add *wallet_address* to the waiting queue. Idempotent. Requires prepaid credit (deposit-credit policy)."""
    wallet = wallet_address.lower()
    profile_name, requested_gpus = _resolve_profile(requested_profile)
    if not _init_once():
        return {"joined": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"joined": False, "reason": "Session DB unavailable"}
    try:
        # Require deposit credit before allowing queue join (deposit-credit access control).
        deposit_ledger = _import_deposit_ledger()
        if not deposit_ledger.init_once():
            return {"joined": False, "reason": "Billing unavailable. Cannot join queue without deposit ledger."}
        if deposit_ledger.get_remaining_minutes(wallet) <= 0:
            return {"joined": False, "reason": "No prepaid credit. Deposit AXGT and verify tx hash to join queue."}

        now = time.time()
        with conn.cursor() as cur:
            # Already the active user? No need to queue.
            active = _active_session_for_wallet(cur, wallet)
            if active:
                conn.commit()
                return {
                    "joined": False,
                    "reason": "You already own the active session.",
                    "queue_position": None,
                }
            cur.execute(
                f"""INSERT INTO {_QUEUE_TABLE} (wallet_address, requested_profile, requested_gpus, queue_reason, queued_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (wallet_address) DO UPDATE SET
                        requested_profile = EXCLUDED.requested_profile,
                        requested_gpus = EXCLUDED.requested_gpus,
                        queue_reason = EXCLUDED.queue_reason""",
                (wallet, profile_name, requested_gpus, f"queued waiting for {requested_gpus} GPU(s)", now),
            )
            pos = _queue_position(cur, wallet)
            qlen = _queue_length(cur)
        conn.commit()
        logger.info("session_manager: %s joined queue at position %s", _mask(wallet), pos)
        return {
            "joined": True,
            "queue_position": pos,
            "queue_length": qlen,
            "requested_profile": profile_name,
            "requested_gpus": requested_gpus,
            "allocation_status": "queued",
            "reason": f"Queued waiting for {requested_gpus} GPU(s)",
        }
    except Exception as exc:
        conn.rollback()
        logger.warning("join_queue failed: %s", exc, exc_info=True)
        return {
            "joined": False,
            "reason": "Could not join the queue. Please try again.",
        }
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
                f"""SELECT wallet_address, requested_profile, requested_gpus, queued_at, queue_reason
                    FROM {_QUEUE_TABLE} ORDER BY queued_at ASC""",
            )
            rows = cur.fetchall()
        return [
            {
                "wallet": _mask(r[0]),
                "requested_profile": r[1] or "small",
                "requested_gpus": int(r[2] or 1),
                "queued_at": r[3],
                "queue_reason": r[4],
                "position": i + 1,
            }
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
            active = _active_session_for_wallet(cur, wallet)
        conn.commit()
        if ended:
            _on_session_ended(ended[0], ended[1])
        return active is not None
    except Exception as exc:
        logger.warning("is_session_owner failed: %s", exc)
        return False
    finally:
        conn.close()
