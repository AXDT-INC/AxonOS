#!/usr/bin/env python3
"""
AXGT Wallet Verification Module

Deposit-credit access: wallet must have remaining_minutes > 0 from verified
AXGT deposits to the revenue wallet. Wallet signature verification (challenge)
remains for authentication. No hold-based balance checks.
"""

import logging
import os
import re
import secrets
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ERC-20 balanceOf(address) and decimals() selectors (for optional UI balance display)
BALANCE_OF_SIGNATURE = "0x70a08231"
DECIMALS_SIGNATURE = "0x313ce567"
DEFAULT_TOKEN_DECIMALS = 18

# One-time wallet-bound challenge config.
# When AXGT_CHALLENGE_DB_URL is set, challenges are stored in Postgres (shared across backends).
# Otherwise in-memory only (sticky sessions required for multiple backends).
_CHALLENGE_PREFIX = "AxonOS verify\n"
_CHALLENGE_TTL_SECONDS_DEFAULT = 180
_challenge_registry: Dict[str, Dict[str, Any]] = {}
_challenge_lock = None
_postgres_init_lock = None

# Use threading.Lock for concurrency (in-memory challenge registry)
from threading import Lock as _ThreadLock
_challenge_lock = _ThreadLock()
_postgres_init_lock = _ThreadLock()

_CHALLENGE_TABLE = "axgt_challenges"
_postgres_init_done = False

DEFAULT_MIN_DEPOSIT = 100
DEFAULT_CREDIT_PER_100_AXGT_MINUTES = 60
DEFAULT_WARNING_THRESHOLD_MINUTES = 10
DEFAULT_ETH_MIN_DEPOSIT = "0.01"
DEFAULT_ETH_CREDIT_PER_ETH_MINUTES = 60


def mask_wallet_address(address: str) -> str:
    if not address or len(address) < 10:
        return "***"
    return f"{address[:6]}...{address[-4:]}"


def validate_wallet_address(address: str) -> bool:
    if not address:
        return False
    return bool(re.match(r"^0x[a-fA-F0-9]{40}$", address))


def _challenge_ttl_seconds() -> int:
    raw = (os.getenv("AXGT_CHALLENGE_TTL_SECONDS") or "").strip()
    if not raw:
        return _CHALLENGE_TTL_SECONDS_DEFAULT
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError("must be positive")
        return value
    except ValueError:
        logger.warning(
            "Invalid AXGT_CHALLENGE_TTL_SECONDS value '%s'; using default %s",
            raw,
            _CHALLENGE_TTL_SECONDS_DEFAULT,
        )
        return _CHALLENGE_TTL_SECONDS_DEFAULT


def get_challenge_ttl_seconds() -> int:
    return _challenge_ttl_seconds()


def _challenge_db_url() -> Optional[str]:
    return os.getenv("AXGT_CHALLENGE_DB_URL") or None


def _postgres_ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_CHALLENGE_TABLE} (
                nonce TEXT PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                used BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )
    conn.commit()


def _postgres_get_connection():
    url = _challenge_db_url()
    if not url:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        return conn
    except Exception as e:
        logger.warning("Postgres challenge DB connect failed: %s", e)
        return None


def _postgres_init_once() -> bool:
    """Ensure challenge table exists. Call when DB URL is set. Returns True if init succeeded."""
    global _postgres_init_done
    url = _challenge_db_url()
    if not url:
        return False
    with _postgres_init_lock:
        if _postgres_init_done:
            return True
        conn = _postgres_get_connection()
        if not conn:
            return False
        try:
            _postgres_ensure_table(conn)
            _postgres_init_done = True
            return True
        except Exception as e:
            logger.warning("Postgres challenge table init failed: %s", e)
            return False
        finally:
            conn.close()


def _prune_expired_challenges(now_ts: float) -> None:
    expired = [
        nonce
        for nonce, record in _challenge_registry.items()
        if float(record.get("expires_at", 0)) <= now_ts
    ]
    for nonce in expired:
        _challenge_registry.pop(nonce, None)


def get_challenge_message(wallet_address: str) -> str:
    normalized_wallet = (wallet_address or "").strip().lower()
    if not validate_wallet_address(normalized_wallet):
        raise ValueError("wallet_address is invalid")

    now_ts = time.time()
    issued_at = int(now_ts)
    nonce = secrets.token_urlsafe(24)
    challenge = (
        f"{_CHALLENGE_PREFIX}"
        f"Wallet: {normalized_wallet}\n"
        f"Nonce: {nonce}\n"
        f"IssuedAt: {issued_at}"
    )

    if _challenge_db_url():
        if not _postgres_init_once():
            raise RuntimeError("Challenge DB unavailable (Postgres init failed)")
        from datetime import datetime, timezone
        conn = _postgres_get_connection()
        if not conn:
            raise RuntimeError("Challenge DB unavailable (could not connect)")
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {_CHALLENGE_TABLE} WHERE expires_at <= %s", (datetime.fromtimestamp(now_ts, tz=timezone.utc),))
                cur.execute(
                    f"INSERT INTO {_CHALLENGE_TABLE} (nonce, wallet_address, expires_at, used) VALUES (%s, %s, %s, FALSE)",
                    (nonce, normalized_wallet, datetime.fromtimestamp(now_ts + _challenge_ttl_seconds(), tz=timezone.utc)),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning("Postgres challenge insert failed: %s", e)
            raise RuntimeError("Challenge DB write failed") from e
        finally:
            conn.close()
        return challenge

    with _challenge_lock:
        _prune_expired_challenges(now_ts)
        _challenge_registry[nonce] = {
            "wallet_address": normalized_wallet,
            "expires_at": now_ts + _challenge_ttl_seconds(),
            "used": False,
        }
    return challenge


def _normalize_challenge_message(message: str) -> str:
    """Normalize line endings to \\n so parsing and recovery match what wallets sign."""
    if not message:
        return message
    return message.replace("\r\n", "\n").replace("\r", "\n").strip()


def _extract_challenge_fields(message: str) -> tuple[Optional[str], Optional[str]]:
    if not message or not message.startswith(_CHALLENGE_PREFIX):
        return None, None
    parts = message.splitlines()
    if len(parts) < 4:
        return None, None
    wallet_line = parts[1].strip()
    nonce_line = parts[2].strip()
    issued_line = parts[3].strip()
    if not wallet_line.lower().startswith("wallet: "):
        return None, None
    if not nonce_line.lower().startswith("nonce: "):
        return None, None
    if not issued_line.lower().startswith("issuedat: "):
        return None, None
    wallet = wallet_line.split(":", 1)[1].strip().lower()
    nonce = nonce_line.split(":", 1)[1].strip()
    return wallet, nonce


def recover_signer_from_signature(message: str, signature_hex: str) -> Optional[str]:
    if not message or not signature_hex:
        return None
    sig = (signature_hex.strip() or "").lower()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    if len(sig) < 132 or len(sig) > 134:
        return None
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        recovered = Account.recover_message(encode_defunct(text=message), signature=sig)
        return recovered if recovered else None
    except Exception as e:
        logger.warning("Signature recovery failed: %s", e)
        return None


def verify_signed_challenge(wallet_address: str, message: str, signature_hex: str) -> bool:
    if not validate_wallet_address(wallet_address):
        logger.warning("verify_signed_challenge: invalid wallet address")
        return False
    message_normalized = _normalize_challenge_message(message)
    expected_wallet = wallet_address.lower()
    challenge_wallet, challenge_nonce = _extract_challenge_fields(message_normalized)
    if not challenge_wallet or not challenge_nonce:
        logger.warning(
            "verify_signed_challenge: could not parse challenge (prefix=%s, parts_ok=%s)",
            (message_normalized or "")[:50],
            bool(message_normalized and message_normalized.startswith(_CHALLENGE_PREFIX)),
        )
        return False
    if challenge_wallet != expected_wallet:
        logger.warning("verify_signed_challenge: wallet in challenge does not match")
        return False

    now_ts = time.time()

    if _challenge_db_url():
        if not _postgres_init_once():
            logger.warning("verify_signed_challenge: Postgres init failed")
            return False
        from datetime import datetime, timezone
        conn = _postgres_get_connection()
        if not conn:
            logger.warning("verify_signed_challenge: Postgres unavailable")
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT wallet_address, expires_at, used FROM {_CHALLENGE_TABLE} WHERE nonce = %s",
                    (challenge_nonce,),
                )
                row = cur.fetchone()
                if not row:
                    logger.warning(
                        "verify_signed_challenge: challenge not found or expired (nonce=%s)",
                        challenge_nonce[:12] + "..." if challenge_nonce else "?",
                    )
                    return False
                stored_wallet, expires_at, used = row
                if (stored_wallet or "").lower() != expected_wallet:
                    logger.warning("verify_signed_challenge: challenge wallet mismatch")
                    return False
                if used:
                    logger.warning("verify_signed_challenge: challenge already used")
                    return False
                if expires_at and expires_at.timestamp() <= now_ts:
                    logger.warning("verify_signed_challenge: challenge expired")
                    return False
                cur.execute(
                    f"UPDATE {_CHALLENGE_TABLE} SET used = TRUE WHERE nonce = %s AND used = FALSE",
                    (challenge_nonce,),
                )
                if cur.rowcount != 1:
                    logger.warning("verify_signed_challenge: challenge already used (race)")
                    return False
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning("verify_signed_challenge: Postgres error: %s", e)
            return False
        finally:
            conn.close()
        # Signature check after we've claimed the challenge
        recovered = recover_signer_from_signature(message, signature_hex)
        if not recovered and message != message_normalized:
            recovered = recover_signer_from_signature(message_normalized, signature_hex)
        if not recovered or recovered.lower() != expected_wallet:
            logger.warning(
                "verify_signed_challenge: signature recovery failed (recovered=%s)",
                mask_wallet_address(recovered) if recovered else "None",
            )
            return False
        return True

    with _challenge_lock:
        _prune_expired_challenges(now_ts)
        challenge_record = _challenge_registry.get(challenge_nonce)
        if not challenge_record:
            logger.warning(
                "verify_signed_challenge: challenge not found or expired (nonce=%s)",
                challenge_nonce[:12] + "..." if challenge_nonce else "?",
            )
            return False
        if challenge_record.get("wallet_address") != expected_wallet:
            logger.warning("verify_signed_challenge: challenge wallet mismatch")
            return False
        if bool(challenge_record.get("used")):
            logger.warning("verify_signed_challenge: challenge already used")
            return False
        if float(challenge_record.get("expires_at", 0)) <= now_ts:
            _challenge_registry.pop(challenge_nonce, None)
            logger.warning("verify_signed_challenge: challenge expired")
            return False

    # Recover signer: try original message first, then normalized (some wallets normalize line endings)
    recovered = recover_signer_from_signature(message, signature_hex)
    if not recovered and message != message_normalized:
        recovered = recover_signer_from_signature(message_normalized, signature_hex)
    if not recovered or recovered.lower() != expected_wallet:
        logger.warning(
            "verify_signed_challenge: signature recovery failed (recovered=%s)",
            mask_wallet_address(recovered) if recovered else "None",
        )
        return False

    with _challenge_lock:
        challenge_record = _challenge_registry.get(challenge_nonce)
        if not challenge_record or bool(challenge_record.get("used")):
            return False
        challenge_record["used"] = True
    return True


# --- Deposit-credit policy and access (no hold-based logic) ---

def _get_min_deposit_display() -> str:
    raw = (os.getenv("AXGT_MIN_DEPOSIT") or "").strip()
    if not raw:
        return str(DEFAULT_MIN_DEPOSIT)
    try:
        n = int(float(raw))
        if n > 0:
            return str(n)
    except ValueError:
        pass
    return str(DEFAULT_MIN_DEPOSIT)


def _get_credit_per_100_axgt_minutes() -> int:
    raw = (os.getenv("AXGT_CREDIT_PER_100_AXGT_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_CREDIT_PER_100_AXGT_MINUTES
    try:
        minutes = int(float(raw))
        if minutes <= 0:
            raise ValueError("must be positive")
        return minutes
    except ValueError:
        logger.warning(
            "Invalid AXGT_CREDIT_PER_100_AXGT_MINUTES value '%s'; using default %s",
            raw,
            DEFAULT_CREDIT_PER_100_AXGT_MINUTES,
        )
        return DEFAULT_CREDIT_PER_100_AXGT_MINUTES


def _get_eth_min_deposit_display() -> str:
    raw = (os.getenv("ETH_MIN_DEPOSIT") or "").strip()
    if not raw:
        return DEFAULT_ETH_MIN_DEPOSIT
    try:
        n = float(raw)
        if n > 0:
            return str(n)
    except ValueError:
        pass
    return DEFAULT_ETH_MIN_DEPOSIT


def _get_eth_credit_per_eth_minutes() -> float:
    raw = (os.getenv("ETH_CREDIT_PER_ETH_MINUTES") or "").strip()
    if not raw:
        return float(DEFAULT_ETH_CREDIT_PER_ETH_MINUTES)
    try:
        n = float(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    return float(DEFAULT_ETH_CREDIT_PER_ETH_MINUTES)


def _get_warning_threshold_minutes() -> int:
    raw = (os.getenv("AXGT_WARNING_THRESHOLD_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_WARNING_THRESHOLD_MINUTES
    try:
        minutes = int(raw)
        if minutes <= 0:
            raise ValueError("must be positive")
        return minutes
    except ValueError:
        logger.warning(
            "Invalid AXGT_WARNING_THRESHOLD_MINUTES value '%s'; using default %s",
            raw,
            DEFAULT_WARNING_THRESHOLD_MINUTES,
        )
        return DEFAULT_WARNING_THRESHOLD_MINUTES


def _eth_call(rpc_url: str, contract_address: str, data: str) -> Optional[str]:
    """Single eth_call for optional on-chain balance display. Returns hex result or None."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": contract_address, "data": data}, "latest"],
        "id": 1,
    }
    try:
        resp = requests.post(
            rpc_url,
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        result = resp.json()
        if "error" in result or "result" not in result:
            return None
        return result["result"]
    except Exception:
        return None


def _get_axgt_balance_display(wallet_address: str) -> Optional[str]:
    """
    Optional on-chain AXGT balance for UI display (e.g. wallet verification dialog).
    Returns a formatted string (e.g. "100" or "50.5") or None if not configured or on error.
    Does not affect access logic (deposit-credit only).
    """
    if not validate_wallet_address(wallet_address):
        return None
    contract = (os.getenv("AXGT_CONTRACT_ADDRESS") or "").strip()
    rpc_url = (os.getenv("AXGT_RPC_URL") or "").strip()
    if not contract or not rpc_url:
        return None
    try:
        padded = wallet_address[2:].lower().zfill(64)
        balance_hex = _eth_call(rpc_url, contract, BALANCE_OF_SIGNATURE + padded)
        if not balance_hex or balance_hex == "0x":
            return None
        balance_units = int(balance_hex, 16)
        decimals_hex = _eth_call(rpc_url, contract, DECIMALS_SIGNATURE)
        decimals = int(decimals_hex, 16) if decimals_hex and decimals_hex != "0x" else DEFAULT_TOKEN_DECIMALS
        if decimals < 0 or decimals > 255:
            decimals = DEFAULT_TOKEN_DECIMALS
        divisor = Decimal(10) ** decimals
        balance_axgt = Decimal(balance_units) / divisor
        normalized = format(balance_axgt.normalize(), "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return normalized or "0"
    except (InvalidOperation, ValueError, TypeError):
        return None


def get_credit_policy() -> Dict[str, Any]:
    """Deposit-credit policy: min deposit (AXGT/ETH), credit rates, warning threshold."""
    return {
        "min_deposit": _get_min_deposit_display(),
        "credit_per_100_axgt_minutes": _get_credit_per_100_axgt_minutes(),
        "eth_min_deposit": _get_eth_min_deposit_display(),
        "eth_credit_per_eth_minutes": _get_eth_credit_per_eth_minutes(),
        "warning_threshold_minutes": _get_warning_threshold_minutes(),
    }


def get_wallet_access_status(wallet_address: str, consume_usage: bool = False) -> Dict[str, Any]:
    """
    Access status from deposit ledger only. consume_usage is ignored (billing is heartbeat-based).
    Returns verified, access_type, remaining_minutes, consumed_minutes, credited_minutes, etc.
    """
    warning_threshold = _get_warning_threshold_minutes()
    base_response: Dict[str, Any] = {
        "verified": False,
        "access_type": None,
        "remaining_minutes": 0.0,
        "consumed_minutes": 0.0,
        "credited_minutes": 0.0,
        "warning_threshold_minutes": warning_threshold,
        "min_deposit": _get_min_deposit_display(),
        "credit_per_100_axgt_minutes": _get_credit_per_100_axgt_minutes(),
        "reason": "No deposit record or zero balance.",
    }

    if not validate_wallet_address(wallet_address):
        base_response["reason"] = "Invalid wallet address format."
        return base_response

    try:
        from . import deposit_ledger
    except ImportError:
        from axonos_gate import deposit_ledger

    if not deposit_ledger.init_once():
        base_response["reason"] = "Ledger unavailable."
        return base_response

    status = deposit_ledger.get_deposit_status(wallet_address)
    remaining = status["remaining_minutes"]
    consumed = status["consumed_minutes"]
    credited = status["credited_minutes_total"]

    verified = remaining > 0
    response: Dict[str, Any] = {
        "verified": verified,
        "access_type": "deposit_credit" if verified else None,
        "remaining_minutes": round(remaining, 2),
        "consumed_minutes": round(consumed, 2),
        "credited_minutes": round(credited, 2),
        "warning_threshold_minutes": warning_threshold,
        "min_deposit": _get_min_deposit_display(),
        "credit_per_100_axgt_minutes": _get_credit_per_100_axgt_minutes(),
        "reason": None,
    }
    if not verified:
        response["reason"] = (
            "No prepaid credit. Deposit AXGT to the revenue wallet and submit the transaction hash to get usage minutes."
        )
    elif remaining <= warning_threshold:
        response["reason"] = (
            f"Warning: less than {warning_threshold} minutes of prepaid credit remaining."
        )
    # Optional on-chain balance for UI (wallet dialog); None if RPC not configured or on error
    balance_display = _get_axgt_balance_display(wallet_address)
    if balance_display is not None:
        response["balance_axgt"] = balance_display
    return response


def has_access(wallet_address: str) -> Tuple[bool, Optional[str], Optional[float]]:
    """Returns (allowed, access_type, remaining_minutes). Access only if remaining_minutes > 0."""
    if not validate_wallet_address(wallet_address):
        return False, None, None
    status = get_wallet_access_status(wallet_address)
    if status.get("verified"):
        return True, status.get("access_type"), status.get("remaining_minutes")
    return False, None, status.get("remaining_minutes")
