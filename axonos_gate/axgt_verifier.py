#!/usr/bin/env python3
"""
AXGT Wallet Verification Module

Checks hold-based access and off-chain usage credits for AXGT wallets.
"""

import json
import logging
import os
import re
import secrets
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from threading import Lock
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

BALANCE_OF_SIGNATURE = "0x70a08231"
DECIMALS_SIGNATURE = "0x313ce567"

DEFAULT_AXGT_MIN_HOLD = Decimal("100")
DEFAULT_TOKEN_DECIMALS = 18
DEFAULT_CREDIT_PER_100_AXGT_MINUTES = 60
DEFAULT_WARNING_THRESHOLD_MINUTES = 10

_USAGE_DB_PATH_DEFAULT = "/var/lib/axonos_gate/usage.json"
_USAGE_RETENTION_DAYS_DEFAULT = 180
_HUNDRED_AXGT = Decimal("100")

_usage_registry: Dict[str, Dict[str, float]] = {}
_usage_lock = Lock()
_usage_db_loaded = False

# One-time wallet-bound challenge config.
_CHALLENGE_PREFIX = "AxonOS verify\n"
_CHALLENGE_TTL_SECONDS_DEFAULT = 180
_CHALLENGE_DB_PATH_DEFAULT = "/var/lib/axonos_gate/challenges.json"
_challenge_lock = Lock()


def _challenge_db_path() -> str:
    return os.getenv("AXGT_CHALLENGE_DB_PATH", _CHALLENGE_DB_PATH_DEFAULT)


def _load_challenge_registry() -> Dict[str, Dict[str, Any]]:
    """Load challenge registry from shared file (cross-process safe)."""
    path = _challenge_db_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f, fcntl.LOCK_UN)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.warning("Failed to load challenge registry: %s", e)
    return {}


def _save_challenge_registry(registry: Dict[str, Dict[str, Any]]) -> None:
    """Persist challenge registry to shared file (cross-process safe)."""
    path = _challenge_db_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(registry, f)
            fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("Failed to save challenge registry: %s", e)


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


def _prune_expired_challenges(registry: Dict[str, Dict[str, Any]], now_ts: float) -> None:
    expired = [
        nonce
        for nonce, record in registry.items()
        if float(record.get("expires_at", 0)) <= now_ts
    ]
    for nonce in expired:
        registry.pop(nonce, None)


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
    with _challenge_lock:
        registry = _load_challenge_registry()
        _prune_expired_challenges(registry, now_ts)
        registry[nonce] = {
            "wallet_address": normalized_wallet,
            "expires_at": now_ts + _challenge_ttl_seconds(),
            "used": False,
        }
        _save_challenge_registry(registry)
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
    with _challenge_lock:
        registry = _load_challenge_registry()
        _prune_expired_challenges(registry, now_ts)
        challenge_record = registry.get(challenge_nonce)
        if not challenge_record:
            logger.warning(
                "verify_signed_challenge: challenge not found or expired (nonce=%s)",
                challenge_nonce[:12] + "..." if challenge_nonce else "?",
            )
            _save_challenge_registry(registry)
            return False
        if challenge_record.get("wallet_address") != expected_wallet:
            logger.warning("verify_signed_challenge: challenge wallet mismatch")
            return False
        if bool(challenge_record.get("used")):
            logger.warning("verify_signed_challenge: challenge already used")
            return False
        if float(challenge_record.get("expires_at", 0)) <= now_ts:
            registry.pop(challenge_nonce, None)
            _save_challenge_registry(registry)
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
        registry = _load_challenge_registry()
        challenge_record = registry.get(challenge_nonce)
        if not challenge_record or bool(challenge_record.get("used")):
            return False
        challenge_record["used"] = True
        _save_challenge_registry(registry)
    return True


def _usage_db_path() -> str:
    return os.getenv("AXGT_USAGE_DB_PATH", _USAGE_DB_PATH_DEFAULT)


def _usage_retention_days() -> int:
    raw = (os.getenv("AXGT_USAGE_RETENTION_DAYS") or "").strip()
    if not raw:
        return _USAGE_RETENTION_DAYS_DEFAULT
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError("must be positive")
        return value
    except ValueError:
        logger.warning(
            "Invalid AXGT_USAGE_RETENTION_DAYS value '%s'; using default %s",
            raw,
            _USAGE_RETENTION_DAYS_DEFAULT,
        )
        return _USAGE_RETENTION_DAYS_DEFAULT


def _ensure_usage_db_loaded() -> None:
    global _usage_db_loaded
    if _usage_db_loaded:
        return
    path = _usage_db_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            if isinstance(data, dict):
                for wallet, record in data.items():
                    if not isinstance(wallet, str) or not isinstance(record, dict):
                        continue
                    consumed = record.get("consumed_minutes")
                    last_update = record.get("last_update_ts")
                    if isinstance(consumed, (int, float)) and isinstance(last_update, (int, float)):
                        _usage_registry[wallet.lower()] = {
                            "consumed_minutes": float(max(0.0, consumed)),
                            "last_update_ts": float(last_update),
                        }
    except Exception as e:
        logger.warning("Failed to load usage registry from disk: %s", e)
    finally:
        _usage_db_loaded = True


def _persist_usage_best_effort() -> None:
    path = _usage_db_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_usage_registry, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("Failed to persist usage registry to disk: %s", e)


def _cleanup_usage_registry(now_ts: float) -> None:
    retention_seconds = _usage_retention_days() * 86400
    stale_wallets = [
        wallet
        for wallet, record in _usage_registry.items()
        if (now_ts - float(record.get("last_update_ts", now_ts))) > retention_seconds
    ]
    for wallet in stale_wallets:
        _usage_registry.pop(wallet, None)


def _eth_call(rpc_url: str, contract_address: str, data: str) -> Optional[str]:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": contract_address, "data": data}, "latest"],
        "id": 1,
    }
    response = requests.post(
        rpc_url,
        json=payload,
        timeout=10,
        headers={"Content-Type": "application/json"},
    )
    response.raise_for_status()
    result = response.json()
    if "error" in result:
        logger.error("RPC eth_call error: %s", result["error"])
        return None
    if "result" not in result:
        logger.error("No result in eth_call RPC response")
        return None
    return result["result"]


def _get_min_hold_amount() -> Decimal:
    raw = (os.getenv("AXGT_MIN_HOLD_AMOUNT") or "").strip()
    if not raw:
        return DEFAULT_AXGT_MIN_HOLD
    try:
        parsed = Decimal(raw)
        if parsed < 0:
            raise InvalidOperation("negative not allowed")
        return parsed
    except (InvalidOperation, ValueError):
        logger.warning(
            "Invalid AXGT_MIN_HOLD_AMOUNT value '%s'; using default %s",
            raw,
            str(DEFAULT_AXGT_MIN_HOLD),
        )
        return DEFAULT_AXGT_MIN_HOLD


def _get_credit_per_100_axgt_minutes() -> int:
    raw = (os.getenv("AXGT_CREDIT_PER_100_AXGT_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_CREDIT_PER_100_AXGT_MINUTES
    try:
        minutes = int(raw)
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


def get_min_hold_amount_display() -> str:
    amount = _get_min_hold_amount()
    normalized = format(amount.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _decimal_display(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def get_credit_policy() -> Dict[str, Any]:
    return {
        "min_hold_amount": get_min_hold_amount_display(),
        "credit_per_100_axgt_minutes": _get_credit_per_100_axgt_minutes(),
        "warning_threshold_minutes": _get_warning_threshold_minutes(),
    }


def _get_token_decimals(contract_address: str, rpc_url: str) -> int:
    try:
        decimals_hex = _eth_call(rpc_url, contract_address, DECIMALS_SIGNATURE)
        if not decimals_hex or decimals_hex == "0x":
            raise ValueError("empty decimals result")
        decimals = int(decimals_hex, 16)
        if decimals < 0 or decimals > 255:
            raise ValueError(f"invalid decimals value: {decimals}")
        return decimals
    except Exception as e:
        logger.warning(
            "Failed to read token decimals for AXGT contract %s: %s. Using default decimals=%s",
            contract_address,
            e,
            DEFAULT_TOKEN_DECIMALS,
        )
        return DEFAULT_TOKEN_DECIMALS


def _required_balance_base_units(decimals: int) -> int:
    min_hold_amount = _get_min_hold_amount()
    base_multiplier = Decimal(10) ** decimals
    required_units_decimal = (min_hold_amount * base_multiplier).to_integral_value(
        rounding=ROUND_CEILING
    )
    return int(required_units_decimal)


def _get_axgt_balance_info(wallet_address: str) -> Optional[Dict[str, Any]]:
    if not validate_wallet_address(wallet_address):
        logger.warning("Invalid wallet address format: %s", mask_wallet_address(wallet_address))
        return None

    contract_address = (os.getenv("AXGT_CONTRACT_ADDRESS") or "").strip()
    rpc_url = (os.getenv("AXGT_RPC_URL") or "").strip()
    chain_id = (os.getenv("AXGT_CHAIN_ID") or "").strip()
    if not contract_address or not rpc_url or not chain_id:
        logger.error(
            "AXGT verification not configured. Set AXGT_CONTRACT_ADDRESS, AXGT_RPC_URL, and AXGT_CHAIN_ID."
        )
        return None

    expected_contract = (os.getenv("AXGT_EXPECTED_CONTRACT_ADDRESS") or "").strip()
    if expected_contract and contract_address.lower() != expected_contract.lower():
        logger.error(
            "Contract address mismatch. Expected: %s, Got: %s",
            expected_contract,
            contract_address,
        )
        return None

    try:
        padded_address = wallet_address[2:].lower().zfill(64)
        data = BALANCE_OF_SIGNATURE + padded_address
        balance_hex = _eth_call(rpc_url, contract_address, data)
        if not balance_hex or balance_hex == "0x":
            logger.warning("Empty balance result from RPC for %s", mask_wallet_address(wallet_address))
            return None
        balance_base_units = int(balance_hex, 16)
        decimals = _get_token_decimals(contract_address, rpc_url)
        divisor = Decimal(10) ** decimals
        balance_axgt = Decimal(balance_base_units) / divisor
        return {
            "contract_address": contract_address,
            "balance_base_units": balance_base_units,
            "decimals": decimals,
            "balance_axgt": balance_axgt,
            "required_base_units": _required_balance_base_units(decimals),
        }
    except requests.exceptions.RequestException as e:
        logger.error("RPC request failed for %s: %s", mask_wallet_address(wallet_address), e)
        return None
    except (ValueError, KeyError) as e:
        logger.error("Error parsing balance response for %s: %s", mask_wallet_address(wallet_address), e)
        return None
    except Exception as e:
        logger.error("Unexpected error checking balance for %s: %s", mask_wallet_address(wallet_address), e)
        return None


def has_axgt_balance(wallet_address: str) -> bool:
    details = _get_axgt_balance_info(wallet_address)
    if not details:
        return False
    return details["balance_axgt"] >= _get_min_hold_amount()


def get_wallet_access_status(wallet_address: str, consume_usage: bool = True) -> Dict[str, Any]:
    warning_threshold = _get_warning_threshold_minutes()
    min_hold_amount = _get_min_hold_amount()
    base_response: Dict[str, Any] = {
        "verified": False,
        "access_type": None,
        "locked": True,
        "reason": "Wallet verification failed.",
        "remaining_minutes": 0.0,
        "consumed_minutes": 0.0,
        "capacity_minutes": 0.0,
        "warning_threshold_minutes": warning_threshold,
        "min_hold_amount": get_min_hold_amount_display(),
        "credit_per_100_axgt_minutes": _get_credit_per_100_axgt_minutes(),
        "balance_axgt": "0",
    }

    if not validate_wallet_address(wallet_address):
        base_response["reason"] = "Invalid wallet address format."
        return base_response

    details = _get_axgt_balance_info(wallet_address)
    if not details:
        base_response["reason"] = "Unable to verify AXGT balance."
        return base_response

    balance_axgt: Decimal = details["balance_axgt"]
    base_response["balance_axgt"] = _decimal_display(balance_axgt)

    if balance_axgt < min_hold_amount:
        base_response["reason"] = (
            f"Access requires holding at least {get_min_hold_amount_display()} AXGT."
        )
        return base_response

    credit_per_100 = _get_credit_per_100_axgt_minutes()
    bucket_count = int(balance_axgt // _HUNDRED_AXGT)
    capacity_minutes = float(bucket_count * credit_per_100)
    now_ts = time.time()

    with _usage_lock:
        _ensure_usage_db_loaded()
        _cleanup_usage_registry(now_ts)
        wallet_key = wallet_address.lower()
        record = _usage_registry.get(wallet_key)
        if record is None:
            record = {"consumed_minutes": 0.0, "last_update_ts": now_ts}
            _usage_registry[wallet_key] = record
        elif consume_usage:
            last_update_ts = float(record.get("last_update_ts", now_ts))
            elapsed_minutes = max(0.0, now_ts - last_update_ts) / 60.0
            record["consumed_minutes"] = float(record.get("consumed_minutes", 0.0)) + elapsed_minutes
            record["last_update_ts"] = now_ts
        consumed_minutes = float(record.get("consumed_minutes", 0.0))
        if consume_usage:
            _persist_usage_best_effort()

    remaining_minutes = max(0.0, capacity_minutes - consumed_minutes)
    locked = remaining_minutes <= 0.0
    response: Dict[str, Any] = {
        "verified": not locked,
        "access_type": "holding_credit" if not locked else None,
        "locked": locked,
        "reason": None,
        "remaining_minutes": round(remaining_minutes, 2),
        "consumed_minutes": round(consumed_minutes, 2),
        "capacity_minutes": round(capacity_minutes, 2),
        "warning_threshold_minutes": warning_threshold,
        "min_hold_amount": get_min_hold_amount_display(),
        "credit_per_100_axgt_minutes": credit_per_100,
        "balance_axgt": _decimal_display(balance_axgt),
    }
    if locked:
        response["reason"] = (
            "Usage credit exhausted. Increase held AXGT to raise capacity and unlock access."
        )
    elif remaining_minutes <= warning_threshold:
        response["reason"] = (
            f"Warning: less than {warning_threshold} minutes of AXGT usage credit remaining."
        )
    return response


def has_access(wallet_address: str) -> Tuple[bool, Optional[str], Optional[float]]:
    if not validate_wallet_address(wallet_address):
        return False, None, None
    status = get_wallet_access_status(wallet_address, consume_usage=False)
    if status.get("verified"):
        return True, status.get("access_type"), status.get("remaining_minutes")
    return False, None, status.get("remaining_minutes")
