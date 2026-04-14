"""
Deposit verification via transaction hash for AxonOS AXGT prepaid billing.

Verifies on-chain AXGT transfer to revenue wallet and credits minutes.
No escrow, no oracle, no trust in client-reported amounts.
"""

import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ERC20 Transfer(address,address,uint256) topic0
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

DEFAULT_MIN_CONFIRMATIONS = 6
DEFAULT_MIN_DEPOSIT = 100
DEFAULT_CREDIT_PER_100_AXGT_MINUTES = 60
DEFAULT_TOKEN_DECIMALS = 18
# Native ETH deposit (optional)
DEFAULT_ETH_MIN_DEPOSIT = Decimal("0.0005")
DEFAULT_ETH_CREDIT_PER_ETH_MINUTES = 120000.0


def _eth_deposits_enabled() -> bool:
    raw = (os.getenv("AXGT_ENABLE_ETH_DEPOSITS") or "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def _rpc(url: str, method: str, params: List[Any]) -> Optional[Any]:
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    try:
        r = requests.post(url, json=payload, timeout=15, headers={"Content-Type": "application/json"})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            logger.warning("RPC %s error: %s", method, data["error"])
            return None
        return data.get("result")
    except Exception as e:
        logger.warning("RPC %s failed: %s", method, e)
        return None


def _get_revenue_wallet() -> str:
    return (os.getenv("AXGT_REVENUE_WALLET") or "").strip().lower()


def _get_contract_address() -> str:
    return (os.getenv("AXGT_CONTRACT_ADDRESS") or "").strip().lower()


def _get_rpc_url() -> str:
    return (os.getenv("AXGT_RPC_URL") or "").strip()


def _min_confirmations() -> int:
    raw = (os.getenv("AXGT_DEPOSIT_MIN_CONFIRMATIONS") or "").strip()
    try:
        n = int(raw)
        if n >= 0:
            return n
    except ValueError:
        pass
    return DEFAULT_MIN_CONFIRMATIONS


def _min_deposit() -> Decimal:
    raw = (os.getenv("AXGT_MIN_DEPOSIT") or "").strip()
    if not raw:
        return Decimal(str(DEFAULT_MIN_DEPOSIT))
    try:
        val = Decimal(raw)
        if val > 0:
            return val
        logger.warning(
            "Invalid AXGT_MIN_DEPOSIT value '%s' (must be positive); using default %s",
            raw,
            DEFAULT_MIN_DEPOSIT,
        )
    except Exception:
        logger.warning(
            "Invalid AXGT_MIN_DEPOSIT value '%s'; using default %s",
            raw,
            DEFAULT_MIN_DEPOSIT,
        )
    return Decimal(str(DEFAULT_MIN_DEPOSIT))


def _credit_per_100_minutes() -> float:
    raw = (os.getenv("AXGT_CREDIT_PER_100_AXGT_MINUTES") or "").strip()
    try:
        n = float(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    return float(DEFAULT_CREDIT_PER_100_AXGT_MINUTES)


def _min_eth_deposit() -> Decimal:
    raw = (os.getenv("ETH_MIN_DEPOSIT") or "").strip()
    if not raw:
        return DEFAULT_ETH_MIN_DEPOSIT
    try:
        val = Decimal(raw)
        if val > 0:
            return val
        logger.warning(
            "Invalid ETH_MIN_DEPOSIT value '%s' (must be positive); using default %s",
            raw,
            DEFAULT_ETH_MIN_DEPOSIT,
        )
    except Exception:
        logger.warning(
            "Invalid ETH_MIN_DEPOSIT value '%s'; using default %s",
            raw,
            DEFAULT_ETH_MIN_DEPOSIT,
        )
    return DEFAULT_ETH_MIN_DEPOSIT


def _eth_credit_per_eth_minutes() -> float:
    raw = (os.getenv("ETH_CREDIT_PER_ETH_MINUTES") or "").strip()
    try:
        n = float(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    return float(DEFAULT_ETH_CREDIT_PER_ETH_MINUTES)


def _token_decimals(rpc_url: str, contract: str) -> int:
    # decimals() selector
    dec_hex = _rpc(rpc_url, "eth_call", [{"to": contract, "data": "0x313ce567"}, "latest"])
    if not dec_hex or dec_hex == "0x":
        return DEFAULT_TOKEN_DECIMALS
    try:
        return int(dec_hex, 16)
    except Exception:
        return DEFAULT_TOKEN_DECIMALS


def _parse_transfer_logs(
    logs: List[Dict],
    contract_address: str,
    revenue_wallet: str,
    sender_wallet: str,
    decimals: int,
) -> Decimal:
    """Sum AXGT amount from Transfer logs: from sender_wallet to revenue_wallet, contract_address."""
    contract = contract_address.lower()
    rev = revenue_wallet.lower()
    sender = sender_wallet.lower()
    divisor = Decimal(10) ** decimals
    total = Decimal("0")
    for log in logs or []:
        addr = (log.get("address") or "").strip().lower()
        if addr != contract:
            continue
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0]
        t0_hex = (t0.hex() if isinstance(t0, bytes) else (t0 or "").strip().lower())
        if not t0_hex.startswith("0x"):
            t0_hex = "0x" + t0_hex
        if t0_hex != TRANSFER_TOPIC:
            continue
        if len(topics) < 3:
            continue
        t1, t2 = topics[1], topics[2]
        def _to_addr(t):
            if t is None:
                return ""
            h = t.hex() if isinstance(t, bytes) else (t or "").strip().lower().replace("0x", "")
            return ("0x" + h[-40:]).lower() if len(h) >= 40 else ""
        from_addr = _to_addr(t1)
        to_addr = _to_addr(t2)
        if from_addr != sender or to_addr != rev:
            continue
        data = log.get("data") or "0x0"
        if isinstance(data, bytes):
            data = "0x" + data.hex()
        data = (data or "").strip().lower()
        if data.startswith("0x"):
            data = data[2:]
        if len(data) < 64:
            continue
        try:
            amount_wei = int(data[:64], 16)
            total += Decimal(amount_wei) / divisor
        except (ValueError, TypeError):
            continue
    return total


def verify_deposit(
    authenticated_wallet: str,
    tx_hash: str,
) -> Dict[str, Any]:
    """
    Verify tx_hash as AXGT transfer from authenticated_wallet to revenue wallet.
    Wallet must match authenticated session; never trust wallet_address from body alone.

    Returns dict with:
      verified (bool), wallet_address, tx_hash, axgt_amount, credited_minutes,
      remaining_minutes, confirmations, error (if not verified).
    """
    wallet = (authenticated_wallet or "").strip().lower()
    tx = (tx_hash or "").strip()
    if not tx.startswith("0x"):
        tx = "0x" + tx
    tx = tx.lower()

    revenue = _get_revenue_wallet()
    contract = _get_contract_address()
    rpc_url = _get_rpc_url()

    fail = lambda msg: {
        "verified": False,
        "wallet_address": wallet,
        "tx_hash": tx_hash,
        "error": msg,
    }

    if not revenue or not rpc_url:
        return fail("Deposit verification not configured (AXGT_REVENUE_WALLET, AXGT_RPC_URL)")

    if not wallet:
        return fail("Wallet address required")

    # Replay protection
    try:
        from . import deposit_ledger
    except ImportError:
        try:
            from axonos_gate import deposit_ledger
        except ImportError:
            import deposit_ledger
    if deposit_ledger.tx_hash_already_credited(tx):
        deposit_ledger.record_verification_reject(wallet, notes="Duplicate tx_hash")
        return fail("Transaction already credited")

    # Fetch transaction
    tx_obj = _rpc(rpc_url, "eth_getTransactionByHash", [tx])
    if not tx_obj:
        # No ledger row: client may poll immediately after broadcast (tx not indexed yet).
        return fail("Transaction not found yet — wait a few seconds if you just submitted.")

    # Fetch receipt (None while pending)
    receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx])
    if not receipt:
        return fail("Transaction pending — waiting for inclusion in a block.")

    status = receipt.get("status")
    if status is None:
        deposit_ledger.record_verification_reject(wallet, notes="Receipt status missing")
        return fail("Receipt status missing")
    if isinstance(status, str) and status != "0x1" and status != "1":
        deposit_ledger.record_verification_reject(wallet, notes="Transaction failed")
        return fail("Transaction failed")
    if isinstance(status, int) and status != 1:
        deposit_ledger.record_verification_reject(wallet, notes="Transaction failed")
        return fail("Transaction failed")

    block_hash = receipt.get("blockNumber")
    if block_hash is None:
        deposit_ledger.record_verification_reject(wallet, notes="Block number missing")
        return fail("Receipt block number missing")
    try:
        block_number = int(block_hash, 16) if isinstance(block_hash, str) else int(block_hash)
    except (ValueError, TypeError):
        deposit_ledger.record_verification_reject(wallet, notes="Invalid block number")
        return fail("Invalid block number")

    # Confirmations
    latest_hex = _rpc(rpc_url, "eth_blockNumber", [])
    if latest_hex is None:
        deposit_ledger.record_verification_reject(wallet, notes="Could not get latest block")
        return fail("Could not get latest block")
    try:
        latest = int(latest_hex, 16) if isinstance(latest_hex, str) else int(latest_hex)
    except (ValueError, TypeError):
        deposit_ledger.record_verification_reject(wallet, notes="Invalid latest block")
        return fail("Invalid latest block")
    confirmations = latest - block_number + 1
    min_conf = _min_confirmations()
    if confirmations < min_conf:
        # No ledger row: UI polls until min confirmations (avoids audit spam).
        return fail(f"Insufficient confirmations (have {confirmations}, need {min_conf})")

    to_addr = (tx_obj.get("to") or "").strip().lower()
    if not to_addr:
        to_addr = (receipt.get("to") or "").strip().lower()
    from_hex = (tx_obj.get("from") or "").strip().lower()
    if from_hex != wallet:
        deposit_ledger.record_verification_reject(wallet, notes="Sender does not match authenticated wallet")
        return fail("Transaction sender does not match authenticated wallet")

    # Native ETH deposit: tx.to == revenue, tx.value >= min
    value_hex = tx_obj.get("value")
    if value_hex is not None and to_addr == revenue:
        try:
            value_wei = int(value_hex, 16) if isinstance(value_hex, str) else int(value_hex)
        except (ValueError, TypeError):
            value_wei = 0
        if value_wei > 0:
            if not _eth_deposits_enabled():
                deposit_ledger.record_verification_reject(wallet, notes="ETH deposits disabled by AXGT_ENABLE_ETH_DEPOSITS")
                return fail("ETH deposits are currently disabled")
            eth_amount = Decimal(value_wei) / Decimal(10 ** 18)
            min_eth = _min_eth_deposit()
            if eth_amount >= min_eth:
                credit_per_eth = _eth_credit_per_eth_minutes()
                credited_minutes = float(eth_amount * Decimal(str(credit_per_eth)))
                ok, remaining, err = deposit_ledger.credit_eth_deposit(
                    wallet,
                    eth_amount,
                    credited_minutes,
                    tx,
                    block_number,
                )
                if not ok:
                    return fail(err or "Failed to credit ETH deposit")
                return {
                    "verified": True,
                    "wallet_address": wallet,
                    "tx_hash": tx,
                    "deposit_currency": "ETH",
                    "eth_amount": str(eth_amount),
                    "axgt_amount": None,
                    "credited_minutes": round(credited_minutes, 2),
                    "remaining_minutes": round(remaining, 2),
                    "confirmations": confirmations,
                }
            deposit_ledger.record_verification_reject(
                wallet,
                notes=f"ETH amount {eth_amount} below minimum {min_eth}",
            )
            return fail(f"ETH deposit below minimum ({min_eth} ETH)")

    # AXGT: transaction must be to AXGT contract (transfer call)
    if not contract:
        deposit_ledger.record_verification_reject(wallet, notes="AXGT contract not configured")
        return fail("AXGT deposit requires AXGT_CONTRACT_ADDRESS")
    if to_addr != contract:
        deposit_ledger.record_verification_reject(wallet, notes="Wrong token contract")
        return fail("Transaction is not for the AXGT contract")

    logs = receipt.get("logs") or []
    decimals = _token_decimals(rpc_url, contract)
    axgt_amount = _parse_transfer_logs(logs, contract, revenue, wallet, decimals)

    if axgt_amount <= 0:
        deposit_ledger.record_verification_reject(
            wallet,
            notes="No valid AXGT transfer to revenue wallet from this sender",
        )
        return fail("No valid AXGT transfer to revenue wallet from this sender")

    min_dep = _min_deposit()
    if axgt_amount < min_dep:
        deposit_ledger.record_verification_reject(
            wallet,
            notes=f"Amount {axgt_amount} below minimum {min_dep}",
        )
        return fail(f"Deposit amount below minimum ({min_dep} AXGT)")

    # Credit: (axgt_amount / 100) * credit_per_100
    credit_per_100 = _credit_per_100_minutes()
    credited_minutes = float(axgt_amount / Decimal("100") * Decimal(str(credit_per_100)))

    ok, remaining, err = deposit_ledger.credit_deposit(
        wallet,
        axgt_amount,
        credited_minutes,
        tx,
        block_number,
    )
    if not ok:
        return fail(err or "Failed to credit deposit")

    return {
        "verified": True,
        "wallet_address": wallet,
        "tx_hash": tx,
        "deposit_currency": "AXGT",
        "axgt_amount": str(axgt_amount),
        "eth_amount": None,
        "credited_minutes": round(credited_minutes, 2),
        "remaining_minutes": round(remaining, 2),
        "confirmations": confirmations,
    }
