"""
Deposit-credit ledger for AxonOS AXGT prepaid billing.

Postgres-backed source of truth: axgt_deposits (balance per wallet),
axgt_ledger (audit log for every balance change), axgt_verified_deposits
(replay protection for tx-hash deposits).

Access rule: wallet allowed only if remaining_minutes > 0.
"""

import logging
import math
import os
import re
import time
from decimal import Decimal
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEPOSITS_TABLE = "axgt_deposits"
_LEDGER_TABLE = "axgt_ledger"
_VERIFIED_TABLE = "axgt_verified_deposits"

_ALLOWED_EVENT_TYPES = frozenset({
    "deposit_credit",
    "test_credit",
    "guest_credit",
    "usage_deduction",
    "refund",
    "admin_adjustment",
    "session_expiry",
    "verification_reject",
})

_pg_init_done = False
_pg_init_lock = Lock()


def _db_url() -> Optional[str]:
    return os.getenv("AXGT_CHALLENGE_DB_URL") or None


def _get_connection():
    url = _db_url()
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as exc:
        logger.warning("deposit_ledger: Postgres connect failed: %s", exc)
        return None


def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_DEPOSITS_TABLE} (
                wallet_address TEXT PRIMARY KEY,
                deposited_amount_axgt NUMERIC NOT NULL DEFAULT 0,
                credited_minutes_total DOUBLE PRECISION NOT NULL DEFAULT 0,
                consumed_minutes_total DOUBLE PRECISION NOT NULL DEFAULT 0,
                remaining_minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
                last_billed_at DOUBLE PRECISION,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_deposits_wallet ON {_DEPOSITS_TABLE}(wallet_address)"
        )

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_LEDGER_TABLE} (
                id SERIAL PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                event_type TEXT NOT NULL,
                minutes_delta DOUBLE PRECISION NOT NULL DEFAULT 0,
                axgt_delta NUMERIC NOT NULL DEFAULT 0,
                balance_after_minutes DOUBLE PRECISION NOT NULL,
                reference_tx_hash TEXT,
                reference_session_id TEXT,
                notes TEXT,
                created_at DOUBLE PRECISION NOT NULL,
                created_by TEXT NOT NULL
            )
        """)
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_ledger_wallet ON {_LEDGER_TABLE}(wallet_address)"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_ledger_txhash ON {_LEDGER_TABLE}(reference_tx_hash)"
        )

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_VERIFIED_TABLE} (
                tx_hash TEXT PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                sender_wallet TEXT NOT NULL,
                recipient_wallet TEXT NOT NULL,
                axgt_amount NUMERIC NOT NULL,
                credited_minutes DOUBLE PRECISION NOT NULL,
                block_number BIGINT NOT NULL,
                credit_source TEXT NOT NULL DEFAULT 'onchain',
                payment_rail TEXT NOT NULL DEFAULT 'unknown',
                created_at DOUBLE PRECISION NOT NULL
            )
        """)
        # Safe in-place migration for databases created before test-credit
        # provenance existed. Existing deposits remain explicitly "unknown" rail
        # rather than being guessed from amount/chain data.
        cur.execute(
            f"ALTER TABLE {_VERIFIED_TABLE} "
            "ADD COLUMN IF NOT EXISTS credit_source TEXT NOT NULL DEFAULT 'onchain'"
        )
        cur.execute(
            f"ALTER TABLE {_VERIFIED_TABLE} "
            "ADD COLUMN IF NOT EXISTS payment_rail TEXT NOT NULL DEFAULT 'unknown'"
        )
        # Rows created by the retired whitelist/sentinel mechanism predate the
        # provenance columns. Tag only its exact shape so they are never reported
        # as paid on-chain deposits after inheriting the migration default.
        cur.execute(
            f"""UPDATE {_VERIFIED_TABLE}
                SET credit_source = 'legacy_test_credit', payment_rail = 'unknown'
                WHERE credit_source = 'onchain'
                  AND tx_hash ~ '^0xffffffff[0-9A-Fa-f]{{56}}$'
                  AND block_number = 0
                  AND axgt_amount = 0"""
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_verified_wallet ON {_VERIFIED_TABLE}(wallet_address)"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_verified_credit_source "
            f"ON {_VERIFIED_TABLE}(credit_source)"
        )
    conn.commit()


def init_once() -> bool:
    """Ensure tables exist. Returns True if DB is available."""
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
            logger.warning("deposit_ledger: table init failed: %s", exc)
            return False
        finally:
            conn.close()


def get_remaining_minutes(wallet_address: str) -> float:
    """Return remaining_minutes for wallet. 0.0 if no record or not initialized."""
    wallet = (wallet_address or "").strip().lower()
    if not wallet:
        return 0.0
    if not init_once():
        return 0.0
    conn = _get_connection()
    if not conn:
        return 0.0
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT remaining_minutes FROM {_DEPOSITS_TABLE} WHERE wallet_address = %s",
                (wallet,),
            )
            row = cur.fetchone()
            return float(row[0]) if row else 0.0
    except Exception as exc:
        logger.warning("get_remaining_minutes failed: %s", exc)
        return 0.0
    finally:
        conn.close()


def get_deposit_status(wallet_address: str) -> Dict[str, Any]:
    """
    Return full deposit-credit status for a wallet.
    Keys: remaining_minutes, consumed_minutes, credited_minutes_total,
          deposited_amount_axgt, has_deposit (bool).
    """
    wallet = (wallet_address or "").strip().lower()
    empty = {
        "remaining_minutes": 0.0,
        "consumed_minutes": 0.0,
        "credited_minutes_total": 0.0,
        "deposited_amount_axgt": Decimal("0"),
        "has_deposit": False,
    }
    if not wallet or not init_once():
        return empty
    conn = _get_connection()
    if not conn:
        return empty
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT deposited_amount_axgt, credited_minutes_total,
                           consumed_minutes_total, remaining_minutes
                    FROM {_DEPOSITS_TABLE} WHERE wallet_address = %s""",
                (wallet,),
            )
            row = cur.fetchone()
            if not row:
                return empty
            deposited, credited, consumed, remaining = row
            return {
                "remaining_minutes": float(remaining),
                "consumed_minutes": float(consumed),
                "credited_minutes_total": float(credited),
                "deposited_amount_axgt": deposited,
                "has_deposit": True,
            }
    except Exception as exc:
        logger.warning("get_deposit_status failed: %s", exc)
        return empty
    finally:
        conn.close()


def tx_hash_already_credited(tx_hash: str) -> bool:
    """True if tx_hash is already in axgt_verified_deposits (replay protection).

    Fails closed: DB unavailability reads as "already credited" so a rail never
    double-credits during an outage. Callers that must distinguish "duplicate"
    from "can't tell right now" should use tx_hash_already_credited_strict.
    """
    result = tx_hash_already_credited_strict(tx_hash)
    return True if result is None else result


def tx_hash_already_credited_strict(tx_hash: str) -> Optional[bool]:
    """Like tx_hash_already_credited, but returns None when the DB is unreachable
    instead of failing closed, so callers can surface a retryable state."""
    if not (tx_hash or "").strip():
        return True
    if not init_once():
        return None
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM {_VERIFIED_TABLE} WHERE tx_hash = %s",
                (tx_hash.strip().lower(),),
            )
            return cur.fetchone() is not None
    except Exception as exc:
        logger.warning("tx_hash_already_credited failed: %s", exc)
        return None
    finally:
        conn.close()


def _ledger_write(
    cur,
    wallet_address: str,
    event_type: str,
    minutes_delta: float,
    axgt_delta: Decimal,
    balance_after_minutes: float,
    reference_tx_hash: Optional[str] = None,
    reference_session_id: Optional[str] = None,
    notes: Optional[str] = None,
    created_by: str = "system",
) -> None:
    if event_type not in _ALLOWED_EVENT_TYPES:
        raise ValueError(f"Invalid event_type: {event_type}")
    now = time.time()
    cur.execute(
        f"""INSERT INTO {_LEDGER_TABLE}
            (wallet_address, event_type, minutes_delta, axgt_delta, balance_after_minutes,
             reference_tx_hash, reference_session_id, notes, created_at, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            wallet_address,
            event_type,
            minutes_delta,
            axgt_delta,
            balance_after_minutes,
            reference_tx_hash,
            reference_session_id,
            notes,
            now,
            created_by,
        ),
    )


def credit_deposit(
    wallet_address: str,
    axgt_amount: Decimal,
    credited_minutes: float,
    tx_hash: str,
    block_number: int,
) -> Tuple[bool, Optional[float], Optional[str]]:
    """
    In one transaction: insert verified deposit, upsert deposits, write ledger.
    Returns (success, remaining_minutes_after, error_message).
    """
    wallet = (wallet_address or "").strip().lower()
    tx_hash_norm = (tx_hash or "").strip().lower()
    if not wallet or not tx_hash_norm:
        return False, None, "Invalid wallet or tx_hash"
    if not init_once():
        return False, None, "Ledger DB unavailable"
    conn = _get_connection()
    if not conn:
        return False, None, "Ledger DB unavailable"
    try:
        now = time.time()
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {_VERIFIED_TABLE}
                    (tx_hash, wallet_address, sender_wallet, recipient_wallet,
                     axgt_amount, credited_minutes, block_number, credit_source,
                     payment_rail, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    tx_hash_norm,
                    wallet,
                    wallet,
                    _revenue_wallet().lower(),
                    axgt_amount,
                    credited_minutes,
                    block_number,
                    "onchain",
                    "axgt",
                    now,
                ),
            )
            cur.execute(
                f"""INSERT INTO {_DEPOSITS_TABLE}
                    (wallet_address, deposited_amount_axgt, credited_minutes_total,
                     consumed_minutes_total, remaining_minutes, last_billed_at, created_at, updated_at)
                    VALUES (%s, %s, %s, 0, %s, NULL, %s, %s)
                    ON CONFLICT (wallet_address) DO UPDATE SET
                    deposited_amount_axgt = {_DEPOSITS_TABLE}.deposited_amount_axgt + EXCLUDED.deposited_amount_axgt,
                    credited_minutes_total = {_DEPOSITS_TABLE}.credited_minutes_total + EXCLUDED.credited_minutes_total,
                    remaining_minutes = {_DEPOSITS_TABLE}.remaining_minutes + EXCLUDED.remaining_minutes,
                    updated_at = EXCLUDED.updated_at""",
                (wallet, axgt_amount, credited_minutes, credited_minutes, now, now),
            )
            cur.execute(
                f"SELECT remaining_minutes FROM {_DEPOSITS_TABLE} WHERE wallet_address = %s",
                (wallet,),
            )
            row = cur.fetchone()
            remaining = float(row[0]) if row else 0.0
            _ledger_write(
                cur,
                wallet,
                "deposit_credit",
                credited_minutes,
                axgt_amount,
                remaining,
                reference_tx_hash=tx_hash_norm,
                created_by="deposit_verifier",
            )
        conn.commit()
        return True, remaining, None
    except Exception as exc:
        conn.rollback()
        logger.warning("credit_deposit failed: %s", exc)
        return False, None, str(exc)
    finally:
        conn.close()


_TEST_CREDIT_REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_TEST_CREDIT_WALLET_RE = re.compile(r"^0x[a-f0-9]{40}$")
# "guest" funds a wallet-free demo session; the others are real payment rails.
_TEST_CREDIT_RAILS = frozenset({"axgt", "eth", "usdc", "guest"})
_MAX_TEST_CREDIT_GRANT_MINUTES = 1440.0
_MAX_TEST_CREDIT_BALANCE_MINUTES = 10080.0


def credit_test_grant(
    wallet_address: str,
    grant_minutes: float,
    max_balance_minutes: float,
    request_id: str,
    payment_rail: str,
    *,
    credit_source: str = "test_credit",
    event_type: str = "test_credit",
    created_by: str = "test_credit_api",
    reference_prefix: str = "test-credit",
    notes_label: str = "Test credit",
    additive: bool = False,
) -> Dict[str, Any]:
    """Atomically grant bounded credit with replay and provenance tracking.

    Provenance is parameterised so the wallet-free demo rail can reuse this exact
    locking and replay logic while staying distinguishable in the ledger: demo
    minutes are written as ``guest_credit`` rather than ``test_credit``, so free
    compute handed to a prospect is never confused with a team member's own test
    credit. The defaults preserve the original test-credit behaviour byte for byte.

    ``additive=True`` grants the full configured amount on every new request;
    this is reserved for explicitly whitelisted operator wallets. Guest funding
    keeps the bounded default so a demo can never extend its wall-clock budget.

    The wallet balance row is locked before the cap is checked and remains locked
    through the credit update. Concurrent requests for one wallet therefore cannot
    all observe a stale pre-credit balance. ``request_id`` is globally idempotent;
    replaying it for another wallet or rail is rejected rather than reinterpreted.
    """
    wallet = (wallet_address or "").strip().lower()
    request_norm = (request_id or "").strip().lower()
    rail = (payment_rail or "").strip().lower()
    try:
        grant = float(grant_minutes)
        cap = float(max_balance_minutes)
    except (TypeError, ValueError):
        grant = 0.0
        cap = 0.0

    if not _TEST_CREDIT_WALLET_RE.fullmatch(wallet):
        return {"ok": False, "error_code": "invalid_wallet", "error": "Invalid wallet"}
    if rail not in _TEST_CREDIT_RAILS:
        return {"ok": False, "error_code": "invalid_rail", "error": "Invalid test-credit rail"}
    if not _TEST_CREDIT_REQUEST_RE.fullmatch(request_norm):
        return {
            "ok": False,
            "error_code": "invalid_request_id",
            "error": "request_id must be 8-128 safe characters",
        }
    if (
        not math.isfinite(grant)
        or grant <= 0
        or grant > _MAX_TEST_CREDIT_GRANT_MINUTES
        or not math.isfinite(cap)
        or cap <= 0
        or cap > _MAX_TEST_CREDIT_BALANCE_MINUTES
    ):
        return {
            "ok": False,
            "error_code": "invalid_credit_config",
            "error": "Test-credit grant or cap is outside the allowed bounds",
        }
    if not init_once():
        return {
            "ok": False,
            "error_code": "ledger_unavailable",
            "retryable": True,
            "error": "Ledger unavailable",
        }
    conn = _get_connection()
    if not conn:
        return {
            "ok": False,
            "error_code": "ledger_unavailable",
            "retryable": True,
            "error": "Ledger unavailable",
        }

    reference = f"{reference_prefix}:{request_norm}"
    try:
        now = time.time()
        with conn.cursor() as cur:
            # Serialize a request ID globally (including malicious cross-wallet
            # reuse) and serialize all grants for this wallet via its balance row.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (reference,))
            cur.execute(
                f"""INSERT INTO {_DEPOSITS_TABLE}
                    (wallet_address, deposited_amount_axgt, credited_minutes_total,
                     consumed_minutes_total, remaining_minutes, last_billed_at,
                     created_at, updated_at)
                    VALUES (%s, 0, 0, 0, 0, NULL, %s, %s)
                    ON CONFLICT (wallet_address) DO NOTHING""",
                (wallet, now, now),
            )
            cur.execute(
                f"SELECT remaining_minutes FROM {_DEPOSITS_TABLE} "
                "WHERE wallet_address = %s FOR UPDATE",
                (wallet,),
            )
            balance_row = cur.fetchone()
            if not balance_row:
                conn.rollback()
                return {
                    "ok": False,
                    "error_code": "ledger_unavailable",
                    "retryable": True,
                    "error": "Could not lock wallet balance",
                }
            remaining_now = float(balance_row[0])

            cur.execute(
                f"""SELECT wallet_address, credited_minutes, credit_source, payment_rail
                    FROM {_VERIFIED_TABLE} WHERE tx_hash = %s""",
                (reference,),
            )
            previous = cur.fetchone()
            if previous:
                previous_wallet = str(previous[0] or "").lower()
                previous_source = str(previous[2] or "").lower()
                previous_rail = str(previous[3] or "").lower()
                if (
                    previous_wallet != wallet
                    or previous_source != credit_source
                    or previous_rail != rail
                ):
                    conn.commit()
                    return {
                        "ok": False,
                        "error_code": "request_mismatch",
                        "request_mismatch": True,
                        "remaining_minutes": remaining_now,
                        "error": "request_id was already used for a different wallet or rail",
                    }
                conn.commit()
                previous_credited = float(previous[1])
                return {
                    "ok": True,
                    "replayed": True,
                    "capped": previous_credited == 0.0,
                    "no_op": previous_credited == 0.0,
                    "request_id": request_norm,
                    "payment_rail": rail,
                    "credited_minutes": previous_credited,
                    "remaining_minutes": remaining_now,
                }

            available = grant if additive else max(0.0, cap - remaining_now)
            if available <= 0:
                # Record even a no-op request so retries are idempotent. A wallet
                # already holding credit at the configured cap is still allowed
                # to continue; it simply receives no additional minutes.
                cur.execute(
                    f"""INSERT INTO {_VERIFIED_TABLE}
                        (tx_hash, wallet_address, sender_wallet, recipient_wallet,
                         axgt_amount, credited_minutes, block_number, credit_source,
                         payment_rail, created_at)
                        VALUES (%s, %s, %s, %s, 0, 0, 0, %s, %s, %s)""",
                    (
                        reference,
                        wallet,
                        wallet,
                        _revenue_wallet().lower(),
                        credit_source,
                        rail,
                        now,
                    ),
                )
                _ledger_write(
                    cur,
                    wallet,
                    event_type,
                    0.0,
                    Decimal("0"),
                    remaining_now,
                    reference_tx_hash=reference,
                    notes=f"{notes_label} no-op at cap rail={rail} request_id={request_norm}",
                    created_by=created_by,
                )
                conn.commit()
                return {
                    "ok": True,
                    "capped": True,
                    "no_op": True,
                    "replayed": False,
                    "request_id": request_norm,
                    "payment_rail": rail,
                    "credited_minutes": 0.0,
                    "remaining_minutes": remaining_now,
                }

            credited = min(grant, available)
            remaining_after = remaining_now + credited
            cur.execute(
                f"""INSERT INTO {_VERIFIED_TABLE}
                    (tx_hash, wallet_address, sender_wallet, recipient_wallet,
                     axgt_amount, credited_minutes, block_number, credit_source,
                     payment_rail, created_at)
                    VALUES (%s, %s, %s, %s, 0, %s, 0, %s, %s, %s)""",
                (
                    reference,
                    wallet,
                    wallet,
                    _revenue_wallet().lower(),
                    credited,
                    credit_source,
                    rail,
                    now,
                ),
            )
            cur.execute(
                f"""UPDATE {_DEPOSITS_TABLE}
                    SET credited_minutes_total = credited_minutes_total + %s,
                        remaining_minutes = %s,
                        updated_at = %s
                    WHERE wallet_address = %s""",
                (credited, remaining_after, now, wallet),
            )
            _ledger_write(
                cur,
                wallet,
                event_type,
                credited,
                Decimal("0"),
                remaining_after,
                reference_tx_hash=reference,
                notes=f"{notes_label} rail={rail} request_id={request_norm}",
                created_by=created_by,
            )
        conn.commit()
        return {
            "ok": True,
            "replayed": False,
            "request_id": request_norm,
            "payment_rail": rail,
            "credited_minutes": credited,
            "remaining_minutes": remaining_after,
        }
    except Exception as exc:
        conn.rollback()
        logger.warning("credit_test_grant failed: %s", exc)
        return {"ok": False, "error_code": "credit_failed", "error": f"{notes_label} failed"}
    finally:
        conn.close()


def _revenue_wallet() -> str:
    return (os.getenv("AXGT_REVENUE_WALLET") or "").strip() or ""


def credit_eth_deposit(
    wallet_address: str,
    eth_amount: Decimal,
    credited_minutes: float,
    tx_hash: str,
    block_number: int,
) -> Tuple[bool, Optional[float], Optional[str]]:
    """
    Credit minutes from a verified native ETH deposit (replay-safe).
    Does not update deposited_amount_axgt. Returns (success, remaining_minutes_after, error).
    """
    wallet = (wallet_address or "").strip().lower()
    tx_hash_norm = (tx_hash or "").strip().lower()
    if not wallet or not tx_hash_norm:
        return False, None, "Invalid wallet or tx_hash"
    if not init_once():
        return False, None, "Ledger DB unavailable"
    conn = _get_connection()
    if not conn:
        return False, None, "Ledger DB unavailable"
    try:
        now = time.time()
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {_VERIFIED_TABLE}
                    (tx_hash, wallet_address, sender_wallet, recipient_wallet,
                     axgt_amount, credited_minutes, block_number, credit_source,
                     payment_rail, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    tx_hash_norm,
                    wallet,
                    wallet,
                    _revenue_wallet().lower() if _revenue_wallet() else "",
                    Decimal("0"),
                    credited_minutes,
                    block_number,
                    "onchain",
                    "eth",
                    now,
                ),
            )
            cur.execute(
                f"""INSERT INTO {_DEPOSITS_TABLE}
                    (wallet_address, deposited_amount_axgt, credited_minutes_total,
                     consumed_minutes_total, remaining_minutes, last_billed_at, created_at, updated_at)
                    VALUES (%s, 0, %s, 0, %s, NULL, %s, %s)
                    ON CONFLICT (wallet_address) DO UPDATE SET
                    credited_minutes_total = {_DEPOSITS_TABLE}.credited_minutes_total + EXCLUDED.credited_minutes_total,
                    remaining_minutes = {_DEPOSITS_TABLE}.remaining_minutes + EXCLUDED.remaining_minutes,
                    updated_at = EXCLUDED.updated_at""",
                (wallet, credited_minutes, credited_minutes, now, now),
            )
            cur.execute(
                f"SELECT remaining_minutes FROM {_DEPOSITS_TABLE} WHERE wallet_address = %s",
                (wallet,),
            )
            row = cur.fetchone()
            remaining = float(row[0]) if row else 0.0
            _ledger_write(
                cur,
                wallet,
                "deposit_credit",
                credited_minutes,
                Decimal("0"),
                remaining,
                reference_tx_hash=tx_hash_norm,
                notes=f"ETH deposit {eth_amount}",
                created_by="deposit_verifier",
            )
        conn.commit()
        return True, remaining, None
    except Exception as exc:
        conn.rollback()
        logger.warning("credit_eth_deposit failed: %s", exc)
        return False, None, str(exc)
    finally:
        conn.close()


def credit_usdc_deposit(
    wallet_address: str,
    usdc_amount: Decimal,
    credited_minutes: float,
    tx_hash: str,
    block_number: int,
) -> Tuple[bool, Optional[float], Optional[str]]:
    """
    Credit minutes from a verified USDC (x402 rail) deposit (replay-safe).
    Does not update deposited_amount_axgt. Returns (success, remaining_minutes_after, error).
    """
    wallet = (wallet_address or "").strip().lower()
    tx_hash_norm = (tx_hash or "").strip().lower()
    if not wallet or not tx_hash_norm:
        return False, None, "Invalid wallet or tx_hash"
    if not init_once():
        return False, None, "Ledger DB unavailable"
    conn = _get_connection()
    if not conn:
        return False, None, "Ledger DB unavailable"
    try:
        now = time.time()
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {_VERIFIED_TABLE}
                    (tx_hash, wallet_address, sender_wallet, recipient_wallet,
                     axgt_amount, credited_minutes, block_number, credit_source,
                     payment_rail, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    tx_hash_norm,
                    wallet,
                    wallet,
                    _revenue_wallet().lower() if _revenue_wallet() else "",
                    Decimal("0"),
                    credited_minutes,
                    block_number,
                    "onchain",
                    "usdc",
                    now,
                ),
            )
            cur.execute(
                f"""INSERT INTO {_DEPOSITS_TABLE}
                    (wallet_address, deposited_amount_axgt, credited_minutes_total,
                     consumed_minutes_total, remaining_minutes, last_billed_at, created_at, updated_at)
                    VALUES (%s, 0, %s, 0, %s, NULL, %s, %s)
                    ON CONFLICT (wallet_address) DO UPDATE SET
                    credited_minutes_total = {_DEPOSITS_TABLE}.credited_minutes_total + EXCLUDED.credited_minutes_total,
                    remaining_minutes = {_DEPOSITS_TABLE}.remaining_minutes + EXCLUDED.remaining_minutes,
                    updated_at = EXCLUDED.updated_at""",
                (wallet, credited_minutes, credited_minutes, now, now),
            )
            cur.execute(
                f"SELECT remaining_minutes FROM {_DEPOSITS_TABLE} WHERE wallet_address = %s",
                (wallet,),
            )
            row = cur.fetchone()
            remaining = float(row[0]) if row else 0.0
            _ledger_write(
                cur,
                wallet,
                "deposit_credit",
                credited_minutes,
                Decimal("0"),
                remaining,
                reference_tx_hash=tx_hash_norm,
                notes=f"USDC deposit {usdc_amount}",
                created_by="x402_verifier",
            )
        conn.commit()
        return True, remaining, None
    except Exception as exc:
        conn.rollback()
        logger.warning("credit_usdc_deposit failed: %s", exc)
        return False, None, str(exc)
    finally:
        conn.close()


def deduct_usage(
    wallet_address: str,
    minutes_delta: float,
    session_id: Optional[str] = None,
) -> Tuple[bool, float, Optional[str]]:
    """
    Deduct minutes from wallet (heartbeat billing). Idempotent: uses row lock.
    Returns (success, remaining_minutes_after, error_message).
    Uses its own connection; for use within an existing transaction use deduct_usage_on_cursor.
    """
    wallet = (wallet_address or "").strip().lower()
    if not wallet or minutes_delta <= 0:
        return False, 0.0, "Invalid wallet or non-positive delta"
    if not init_once():
        return False, 0.0, "Ledger DB unavailable"
    conn = _get_connection()
    if not conn:
        return False, 0.0, "Ledger DB unavailable"
    try:
        with conn.cursor() as cur:
            ok, remaining, err = _deduct_usage_on_cursor(cur, wallet, minutes_delta, session_id)
            if not ok:
                return False, remaining, err
        conn.commit()
        return True, remaining, None
    except Exception as exc:
        conn.rollback()
        logger.warning("deduct_usage failed: %s", exc)
        return False, 0.0, str(exc)
    finally:
        conn.close()


def _deduct_usage_on_cursor(
    cur,
    wallet_address: str,
    minutes_delta: float,
    session_id: Optional[str] = None,
) -> Tuple[bool, float, Optional[str]]:
    """
    Deduct minutes using the given cursor (same transaction, no commit).
    Caller must commit or rollback. Returns (success, remaining_minutes_after, error_message).
    """
    wallet = (wallet_address or "").strip().lower()
    if not wallet or minutes_delta <= 0:
        return False, 0.0, "Invalid wallet or non-positive delta"
    cur.execute(
        f"""SELECT remaining_minutes, consumed_minutes_total
             FROM {_DEPOSITS_TABLE} WHERE wallet_address = %s FOR UPDATE""",
        (wallet,),
    )
    row = cur.fetchone()
    if not row:
        return False, 0.0, "No deposit record"
    remaining, consumed = float(row[0]), float(row[1])
    deduct = min(minutes_delta, remaining)
    if deduct <= 0:
        return True, remaining, None
    new_remaining = remaining - deduct
    new_consumed = consumed + deduct
    now = time.time()
    cur.execute(
        f"""UPDATE {_DEPOSITS_TABLE}
            SET consumed_minutes_total = %s, remaining_minutes = %s,
                last_billed_at = %s, updated_at = %s
            WHERE wallet_address = %s""",
        (new_consumed, new_remaining, now, now, wallet),
    )
    _ledger_write(
        cur,
        wallet,
        "usage_deduction",
        -deduct,
        Decimal("0"),
        new_remaining,
        reference_session_id=session_id,
        created_by="session_manager",
    )
    return True, new_remaining, None


def record_verification_reject(wallet_address: str, notes: Optional[str] = None) -> None:
    """Write ledger event for rejected deposit (e.g. below min, wrong tx)."""
    wallet = (wallet_address or "").strip().lower()
    if not wallet or not init_once():
        return
    conn = _get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT remaining_minutes FROM {_DEPOSITS_TABLE} WHERE wallet_address = %s",
                (wallet,),
            )
            row = cur.fetchone()
            balance_after = float(row[0]) if row else 0.0
            _ledger_write(
                cur,
                wallet,
                "verification_reject",
                0.0,
                Decimal("0"),
                balance_after,
                notes=notes or "Rejected",
                created_by="deposit_verifier",
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning("record_verification_reject failed: %s", exc)
    finally:
        conn.close()


def record_session_expiry(
    wallet_address: str,
    minutes_deducted: float,
    balance_after_minutes: float,
    session_id: Optional[str] = None,
) -> None:
    """Write ledger event when session expires (usage up to last checkpoint already deducted)."""
    wallet = (wallet_address or "").strip().lower()
    if not wallet or not init_once():
        return
    conn = _get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            _ledger_write(
                cur,
                wallet,
                "session_expiry",
                -minutes_deducted if minutes_deducted else 0.0,
                Decimal("0"),
                balance_after_minutes,
                reference_session_id=session_id,
                notes="Session expired or heartbeat timeout",
                created_by="session_manager",
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning("record_session_expiry failed: %s", exc)
    finally:
        conn.close()


# --- Admin helpers (all write ledger, never bypass audit) ---

def credit_wallet_minutes(
    wallet_address: str,
    minutes: float,
    notes: Optional[str] = None,
    created_by: str = "admin",
) -> Tuple[bool, Optional[float], Optional[str]]:
    """Add minutes to wallet (e.g. manual credit). Returns (success, remaining_after, error)."""
    wallet = (wallet_address or "").strip().lower()
    if not wallet or minutes <= 0:
        return False, None, "Invalid wallet or non-positive minutes"
    if not init_once():
        return False, None, "Ledger DB unavailable"
    conn = _get_connection()
    if not conn:
        return False, None, "Ledger DB unavailable"
    try:
        now = time.time()
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {_DEPOSITS_TABLE}
                    (wallet_address, credited_minutes_total, remaining_minutes, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (wallet_address) DO UPDATE SET
                    credited_minutes_total = {_DEPOSITS_TABLE}.credited_minutes_total + EXCLUDED.credited_minutes_total,
                    remaining_minutes = {_DEPOSITS_TABLE}.remaining_minutes + EXCLUDED.remaining_minutes,
                    updated_at = EXCLUDED.updated_at""",
                (wallet, minutes, minutes, now, now),
            )
            cur.execute(
                f"SELECT remaining_minutes FROM {_DEPOSITS_TABLE} WHERE wallet_address = %s",
                (wallet,),
            )
            row = cur.fetchone()
            remaining = float(row[0]) if row else 0.0
            _ledger_write(
                cur,
                wallet,
                "admin_adjustment",
                minutes,
                Decimal("0"),
                remaining,
                notes=notes or "Admin credit",
                created_by=created_by,
            )
        conn.commit()
        return True, remaining, None
    except Exception as exc:
        conn.rollback()
        logger.warning("credit_wallet_minutes failed: %s", exc)
        return False, None, str(exc)
    finally:
        conn.close()


def refund_wallet_minutes(
    wallet_address: str,
    minutes: float,
    notes: Optional[str] = None,
    created_by: str = "admin",
) -> Tuple[bool, Optional[float], Optional[str]]:
    """Refund minutes (increase remaining). Returns (success, remaining_after, error)."""
    wallet = (wallet_address or "").strip().lower()
    if not wallet or minutes <= 0:
        return False, None, "Invalid wallet or non-positive minutes"
    if not init_once():
        return False, None, "Ledger DB unavailable"
    conn = _get_connection()
    if not conn:
        return False, None, "Ledger DB unavailable"
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT remaining_minutes, credited_minutes_total
                     FROM {_DEPOSITS_TABLE} WHERE wallet_address = %s FOR UPDATE""",
                (wallet,),
            )
            row = cur.fetchone()
            if not row:
                return False, None, "No deposit record"
            remaining, credited = float(row[0]), float(row[1])
            new_remaining = remaining + minutes
            new_credited = credited + minutes
            now = time.time()
            cur.execute(
                f"""UPDATE {_DEPOSITS_TABLE}
                    SET credited_minutes_total = %s, remaining_minutes = %s, updated_at = %s
                    WHERE wallet_address = %s""",
                (new_credited, new_remaining, now, wallet),
            )
            _ledger_write(
                cur,
                wallet,
                "refund",
                minutes,
                Decimal("0"),
                new_remaining,
                notes=notes or "Admin refund",
                created_by=created_by,
            )
        conn.commit()
        return True, new_remaining, None
    except Exception as exc:
        conn.rollback()
        logger.warning("refund_wallet_minutes failed: %s", exc)
        return False, None, str(exc)
    finally:
        conn.close()


def deduct_wallet_minutes_admin(
    wallet_address: str,
    minutes: float,
    notes: Optional[str] = None,
    created_by: str = "admin",
) -> Tuple[bool, Optional[float], Optional[str]]:
    """Deduct minutes as admin (e.g. adjust-balance). Writes admin_adjustment ledger entry.
    Returns (success, remaining_after, error)."""
    wallet = (wallet_address or "").strip().lower()
    if not wallet or minutes <= 0:
        return False, None, "Invalid wallet or non-positive minutes"
    if not init_once():
        return False, None, "Ledger DB unavailable"
    conn = _get_connection()
    if not conn:
        return False, None, "Ledger DB unavailable"
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT remaining_minutes, consumed_minutes_total
                     FROM {_DEPOSITS_TABLE} WHERE wallet_address = %s FOR UPDATE""",
                (wallet,),
            )
            row = cur.fetchone()
            if not row:
                return False, None, "No deposit record"
            remaining, consumed = float(row[0]), float(row[1])
            deduct = min(minutes, remaining)
            if deduct <= 0:
                return False, None, "Insufficient balance to deduct"
            new_remaining = remaining - deduct
            new_consumed = consumed + deduct
            now = time.time()
            cur.execute(
                f"""UPDATE {_DEPOSITS_TABLE}
                    SET consumed_minutes_total = %s, remaining_minutes = %s,
                        updated_at = %s WHERE wallet_address = %s""",
                (new_consumed, new_remaining, now, wallet),
            )
            _ledger_write(
                cur,
                wallet,
                "admin_adjustment",
                -deduct,
                Decimal("0"),
                new_remaining,
                notes=notes or "Admin deduction",
                created_by=created_by,
            )
        conn.commit()
        return True, new_remaining, None
    except Exception as exc:
        conn.rollback()
        logger.warning("deduct_wallet_minutes_admin failed: %s", exc)
        return False, None, str(exc)
    finally:
        conn.close()


def adjust_wallet_balance(
    wallet_address: str,
    minutes_delta: float,
    notes: Optional[str] = None,
    created_by: str = "admin",
) -> Tuple[bool, Optional[float], Optional[str]]:
    """Adjust balance by delta (positive or negative). Returns (success, remaining_after, error)."""
    if minutes_delta > 0:
        return refund_wallet_minutes(wallet_address, minutes_delta, notes, created_by)
    if minutes_delta < 0:
        return deduct_wallet_minutes_admin(
            wallet_address, -minutes_delta, notes=notes, created_by=created_by
        )
    return False, None, "minutes_delta must be non-zero"


def get_wallet_ledger(
    wallet_address: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return ledger entries for wallet (newest first)."""
    wallet = (wallet_address or "").strip().lower()
    if not wallet or not init_once():
        return []
    conn = _get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, wallet_address, event_type, minutes_delta, axgt_delta,
                           balance_after_minutes, reference_tx_hash, reference_session_id, notes, created_at, created_by
                    FROM {_LEDGER_TABLE} WHERE wallet_address = %s ORDER BY id DESC LIMIT %s""",
                (wallet, limit),
            )
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "wallet_address": r[1],
                "event_type": r[2],
                "minutes_delta": float(r[3]),
                "axgt_delta": str(r[4]),
                "balance_after_minutes": float(r[5]),
                "reference_tx_hash": r[6],
                "reference_session_id": r[7],
                "notes": r[8],
                "created_at": float(r[9]),
                "created_by": r[10],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("get_wallet_ledger failed: %s", exc)
        return []
    finally:
        conn.close()
