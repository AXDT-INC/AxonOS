"""Postgres persistence for WebRTC signaling rows (one per browser negotiation)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Optional

from . import config

logger = logging.getLogger(__name__)

_TABLE = "axgt_webrtc_signaling"
_pg_lock = threading.Lock()
_pg_init_done = False


def _db_url() -> Optional[str]:
    return os.getenv("AXGT_CHALLENGE_DB_URL") or None


_pool = None
_pool_lock = threading.Lock()
_active_pool_conns = set()


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        url = _db_url()
        if not url:
            return None
        try:
            from psycopg2.pool import ThreadedConnectionPool

            _pool = ThreadedConnectionPool(2, 20, url)
            logger.info("Created WebRTC store ThreadedConnectionPool")
            return _pool
        except Exception as e:
            logger.warning("webrtc store: failed to create connection pool: %s", e)
            return None


def _conn():
    pool = _get_pool()
    if pool:
        try:
            conn = pool.getconn()
            with _pool_lock:
                _active_pool_conns.add(id(conn))
            return conn
        except Exception as e:
            logger.warning("webrtc store: getconn failed: %s", e)
    # Fallback to direct connection if pool couldn't be created
    url = _db_url()
    if not url:
        return None
    try:
        import psycopg2

        return psycopg2.connect(url)
    except Exception as e:
        logger.warning("webrtc store: connect fallback failed: %s", e)
        return None


def _close_conn(c):
    if c is None:
        return
    is_pool_conn = False
    with _pool_lock:
        if id(c) in _active_pool_conns:
            _active_pool_conns.remove(id(c))
            is_pool_conn = True
    if is_pool_conn:
        pool = _get_pool()
        if pool:
            try:
                pool.putconn(c)
                return
            except Exception as e:
                logger.warning("webrtc store: putconn failed: %s", e)
    try:
        c.close()
    except Exception:
        pass


def ensure_table() -> bool:
    global _pg_init_done
    if not _db_url():
        return False
    with _pg_lock:
        if _pg_init_done:
            return True
        c = _conn()
        if not c:
            return False
        try:
            with c.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_TABLE} (
                        id TEXT PRIMARY KEY,
                        wallet_address TEXT NOT NULL,
                        compute_session_id INTEGER,
                        state TEXT NOT NULL DEFAULT 'created',
                        offer_sdp TEXT,
                        offer_type TEXT,
                        answer_sdp TEXT,
                        answer_type TEXT,
                        client_ice TEXT,
                        server_ice TEXT,
                        last_error TEXT,
                        created_at DOUBLE PRECISION NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL,
                        expires_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                # Additive rollout migration.  Historical rows deliberately stay
                # NULL: a wallet may have released/reclaimed a different compute
                # session, so wallet-only backfills would recreate the routing bug.
                cur.execute(
                    f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS compute_session_id INTEGER"
                )
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_wallet ON {_TABLE} (wallet_address)"
                )
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_state ON {_TABLE} (state)"
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{_TABLE}_compute_state_updated
                    ON {_TABLE} (compute_session_id, state, updated_at)
                    WHERE compute_session_id IS NOT NULL
                    """
                )
            c.commit()
            _pg_init_done = True
            return True
        except Exception as e:
            logger.warning("webrtc store: init failed: %s", e)
            c.rollback()
            return False
        finally:
            _close_conn(c)


def _new_id() -> str:
    return secrets.token_urlsafe(32)


def create_session(wallet_norm: str, compute_session_id: int) -> Optional[str]:
    if not ensure_table():
        return None
    now = time.time()
    ttl = float(config.session_timeout_seconds())
    sid = _new_id()
    w = (wallet_norm or "").strip().lower()
    try:
        owner_id = int(compute_session_id)
    except (TypeError, ValueError):
        return None
    if owner_id <= 0:
        return None
    c = _conn()
    if not c:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {_TABLE}
                (id, wallet_address, compute_session_id, state, created_at, updated_at, expires_at)
                VALUES (%s, %s, %s, 'created', %s, %s, %s)
                """,
                (sid, w, owner_id, now, now, now + ttl),
            )
        c.commit()
        return sid
    except Exception as e:
        logger.warning("webrtc create_session failed: %s", e)
        c.rollback()
        return None
    finally:
        _close_conn(c)


def get_row(session_id: str) -> Optional[dict[str, Any]]:
    if not ensure_table():
        return None
    c = _conn()
    if not c:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, wallet_address, compute_session_id, state,
                       offer_sdp, offer_type, answer_sdp, answer_type,
                       client_ice, server_ice, last_error, created_at, updated_at, expires_at
                FROM {_TABLE} WHERE id = %s
                """,
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return _row_to_dict(row)
    finally:
        _close_conn(c)


def get_row_for_agent(
    session_id: str,
    compute_session_id: int,
    wallet_norm: str,
) -> Optional[dict[str, Any]]:
    """Return a signaling row only inside the authenticated compute scope."""
    if not ensure_table():
        return None
    c = _conn()
    if not c:
        return None
    now = time.time()
    try:
        with c.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, wallet_address, compute_session_id, state,
                       offer_sdp, offer_type, answer_sdp, answer_type,
                       client_ice, server_ice, last_error, created_at, updated_at, expires_at
                FROM {_TABLE}
                WHERE id = %s AND compute_session_id = %s AND wallet_address = %s
                  AND expires_at > %s
                FOR UPDATE
                """,
                (
                    session_id,
                    int(compute_session_id),
                    (wallet_norm or "").strip().lower(),
                    now,
                ),
            )
            row = cur.fetchone()
            if row and row[3] == "agent_processing":
                cur.execute(
                    f"""UPDATE {_TABLE} SET updated_at = %s
                        WHERE id = %s AND compute_session_id = %s
                          AND wallet_address = %s AND state = 'agent_processing'
                          AND expires_at > %s""",
                    (
                        now,
                        session_id,
                        int(compute_session_id),
                        (wallet_norm or "").strip().lower(),
                        now,
                    ),
                )
            c.commit()
            return _row_to_dict(row) if row else None
    except Exception as e:
        logger.warning("webrtc get_row_for_agent: %s", e)
        c.rollback()
        return None
    finally:
        _close_conn(c)


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": row[0],
        "wallet_address": row[1],
        "compute_session_id": row[2],
        "state": row[3],
        "offer_sdp": row[4],
        "offer_type": row[5],
        "answer_sdp": row[6],
        "answer_type": row[7],
        "client_ice": row[8],
        "server_ice": row[9],
        "last_error": row[10],
        "created_at": row[11],
        "updated_at": row[12],
        "expires_at": row[13],
    }


def set_offer(
    session_id: str,
    wallet_norm: str,
    compute_session_id: int,
    offer_sdp: str,
    offer_type: str,
) -> bool:
    if not ensure_table():
        return False
    now = time.time()
    w = wallet_norm.strip().lower()
    c = _conn()
    if not c:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {_TABLE}
                SET offer_sdp = %s, offer_type = %s, state = 'scoped_offer_received',
                    updated_at = %s
                WHERE id = %s AND wallet_address = %s AND compute_session_id = %s
                  AND state = 'created' AND answer_sdp IS NULL AND expires_at > %s
                """,
                (offer_sdp, offer_type, now, session_id, w, int(compute_session_id), now),
            )
            ok = cur.rowcount == 1
        if ok:
            c.commit()
        else:
            c.rollback()
        return ok
    except Exception as e:
        logger.warning("webrtc set_offer failed: %s", e)
        try:
            c.rollback()
        except Exception:
            pass
        return False
    finally:
        _close_conn(c)


def set_answer(
    session_id: str,
    compute_session_id: int,
    wallet_norm: str,
    answer_sdp: str,
    answer_type: str,
) -> bool:
    if not ensure_table():
        return False
    now = time.time()
    c = _conn()
    if not c:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {_TABLE}
                SET answer_sdp = %s, answer_type = %s, state = 'negotiated',
                    updated_at = %s
                WHERE id = %s AND compute_session_id = %s AND wallet_address = %s
                  AND state = 'agent_processing' AND expires_at > %s
                """,
                (
                    answer_sdp,
                    answer_type,
                    now,
                    session_id,
                    int(compute_session_id),
                    (wallet_norm or "").strip().lower(),
                    now,
                ),
            )
            ok = cur.rowcount == 1
        if ok:
            c.commit()
        else:
            c.rollback()
        return ok
    except Exception as e:
        logger.warning("webrtc set_answer failed: %s", e)
        try:
            c.rollback()
        except Exception:
            pass
        return False
    finally:
        _close_conn(c)


def append_client_ice(
    session_id: str,
    wallet_norm: str,
    compute_session_id: int,
    candidates: list[dict[str, Any]],
) -> bool:
    if not candidates:
        return True
    if not ensure_table():
        return False
    now = time.time()
    w = wallet_norm.strip().lower()
    c = _conn()
    if not c:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(
                f"""SELECT client_ice FROM {_TABLE}
                    WHERE id = %s AND wallet_address = %s AND compute_session_id = %s
                      AND expires_at > %s
                    FOR UPDATE""",
                (session_id, w, int(compute_session_id), now),
            )
            r = cur.fetchone()
            if not r:
                c.rollback()
                return False
            existing = _parse_json_list(r[0])
            existing.extend(candidates)
            cur.execute(
                f"""
                UPDATE {_TABLE}
                SET client_ice = %s, updated_at = %s
                WHERE id = %s AND wallet_address = %s AND compute_session_id = %s
                  AND expires_at > %s
                """,
                (
                    json.dumps(existing[-500:]),
                    now,
                    session_id,
                    w,
                    int(compute_session_id),
                    now,
                ),
            )
            ok = cur.rowcount == 1
        if ok:
            c.commit()
        else:
            c.rollback()
        return ok
    except Exception as e:
        logger.warning("webrtc append_client_ice failed: %s", e)
        try:
            c.rollback()
        except Exception:
            pass
        return False
    finally:
        _close_conn(c)


def append_server_ice(
    session_id: str,
    compute_session_id: int,
    wallet_norm: str,
    candidates: list[dict[str, Any]],
) -> bool:
    if not candidates:
        return True
    if not ensure_table():
        return False
    now = time.time()
    c = _conn()
    if not c:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(
                f"""SELECT server_ice FROM {_TABLE}
                    WHERE id = %s AND compute_session_id = %s AND wallet_address = %s
                      AND expires_at > %s
                    FOR UPDATE""",
                (
                    session_id,
                    int(compute_session_id),
                    (wallet_norm or "").strip().lower(),
                    now,
                ),
            )
            r = cur.fetchone()
            if not r:
                c.rollback()
                return False
            existing = _parse_json_list(r[0])
            existing.extend(candidates)
            cur.execute(
                f"""
                UPDATE {_TABLE}
                SET server_ice = %s, updated_at = %s
                WHERE id = %s AND compute_session_id = %s AND wallet_address = %s
                  AND expires_at > %s
                """,
                (
                    json.dumps(existing[-500:]),
                    now,
                    session_id,
                    int(compute_session_id),
                    (wallet_norm or "").strip().lower(),
                    now,
                ),
            )
            ok = cur.rowcount == 1
        if ok:
            c.commit()
        else:
            c.rollback()
        return ok
    except Exception as e:
        logger.warning("webrtc append_server_ice failed: %s", e)
        try:
            c.rollback()
        except Exception:
            pass
        return False
    finally:
        _close_conn(c)


def _parse_json_list(raw: Optional[str]) -> list[Any]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def mark_failed(
    session_id: str,
    compute_session_id: int,
    wallet_norm: str,
    message: str,
) -> bool:
    if not ensure_table():
        return False
    now = time.time()
    c = _conn()
    if not c:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {_TABLE}
                SET state = 'failed', last_error = %s, updated_at = %s
                WHERE id = %s AND compute_session_id = %s AND wallet_address = %s
                  AND state IN ('scoped_offer_received', 'agent_processing')
                  AND expires_at > %s
                """,
                (
                    message[:2000],
                    now,
                    session_id,
                    int(compute_session_id),
                    (wallet_norm or "").strip().lower(),
                    now,
                ),
            )
            ok = cur.rowcount == 1
        if ok:
            c.commit()
        else:
            c.rollback()
        return ok
    except Exception as e:
        logger.warning("webrtc mark_failed: %s", e)
        c.rollback()
        return False
    finally:
        _close_conn(c)


def close_session(session_id: str, wallet_norm: Optional[str] = None) -> None:
    if not ensure_table():
        return
    now = time.time()
    c = _conn()
    if not c:
        return
    try:
        with c.cursor() as cur:
            if wallet_norm:
                cur.execute(
                    f"UPDATE {_TABLE} SET state = 'closed', updated_at = %s WHERE id = %s AND wallet_address = %s",
                    (now, session_id, wallet_norm.strip().lower()),
                )
            else:
                cur.execute(
                    f"UPDATE {_TABLE} SET state = 'closed', updated_at = %s WHERE id = %s",
                    (now, session_id),
                )
        c.commit()
    except Exception as e:
        logger.warning("webrtc close_session: %s", e)
        c.rollback()
    finally:
        _close_conn(c)


def prune_expired() -> None:
    if not ensure_table():
        return
    now = time.time()
    c = _conn()
    if not c:
        return
    try:
        with c.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE} WHERE expires_at < %s", (now,))
        c.commit()
    finally:
        _close_conn(c)


def fetch_next_pending_offer_for_agent(
    compute_session_id: int,
    wallet_norm: str,
) -> Optional[dict[str, Any]]:
    """Atomically claim the next offer belonging to one authenticated compute."""
    if not ensure_table():
        return None
    prune_expired()
    now = time.time()
    c = _conn()
    if not c:
        return None
    try:
        c.autocommit = False
        with c.cursor() as cur:
            # Recover a claim whose agent vanished after the atomic claim but
            # before it could answer. Scoped row polling refreshes updated_at.
            cur.execute(
                f"""
                UPDATE {_TABLE}
                SET state = 'scoped_offer_received', updated_at = %s
                WHERE compute_session_id = %s AND wallet_address = %s
                  AND state = 'agent_processing' AND answer_sdp IS NULL
                  AND updated_at < %s AND expires_at > %s
                """,
                (
                    now,
                    int(compute_session_id),
                    (wallet_norm or "").strip().lower(),
                    now - float(config.agent_claim_lease_seconds()),
                    now,
                ),
            )
            cur.execute(
                f"""
                SELECT id FROM {_TABLE}
                WHERE state = 'scoped_offer_received' AND offer_sdp IS NOT NULL
                  AND answer_sdp IS NULL AND compute_session_id = %s
                  AND wallet_address = %s AND expires_at > %s
                ORDER BY updated_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (
                    int(compute_session_id),
                    (wallet_norm or "").strip().lower(),
                    now,
                ),
            )
            one = cur.fetchone()
            if not one:
                c.rollback()
                return None
            sid = one[0]
            cur.execute(
                f"""
                UPDATE {_TABLE}
                SET state = 'agent_processing', updated_at = %s
                WHERE id = %s AND state = 'scoped_offer_received'
                  AND compute_session_id = %s AND wallet_address = %s
                  AND expires_at > %s
                RETURNING id, wallet_address, compute_session_id, offer_sdp, offer_type
                """,
                (
                    now,
                    sid,
                    int(compute_session_id),
                    (wallet_norm or "").strip().lower(),
                    now,
                ),
            )
            row = cur.fetchone()
            if not row:
                c.rollback()
                return None
        c.commit()
        return {
            "session_id": row[0],
            "wallet_address": row[1],
            "compute_session_id": row[2],
            "offer_sdp": row[3],
            "offer_type": row[4] or "offer",
        }
    except Exception as e:
        logger.warning("webrtc fetch_next_pending: %s", e)
        c.rollback()
        return None
    finally:
        _close_conn(c)


def reset_agent_stale(sid: str, compute_session_id: int, wallet_norm: str) -> bool:
    """If agent dies mid-flight, allow retry."""
    if not ensure_table():
        return False
    now = time.time()
    c = _conn()
    if not c:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {_TABLE}
                SET state = 'scoped_offer_received', updated_at = %s
                WHERE id = %s AND state = 'agent_processing' AND answer_sdp IS NULL
                  AND compute_session_id = %s AND wallet_address = %s
                  AND expires_at > %s
                """,
                (
                    now,
                    sid,
                    int(compute_session_id),
                    (wallet_norm or "").strip().lower(),
                    now,
                ),
            )
            ok = cur.rowcount == 1
        if ok:
            c.commit()
        else:
            c.rollback()
        return ok
    except Exception as e:
        logger.warning("webrtc reset_agent_stale: %s", e)
        c.rollback()
        return False
    finally:
        _close_conn(c)
