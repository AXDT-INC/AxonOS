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


def _axgt_direct_deposits_enabled() -> bool:
    """Direct AXGT-as-payment deposits.

    Default: ``False`` for the ETH-first tokenomics model. AXGT is used for
    holder discounts only; users pay in ETH. Operators can opt in to legacy
    behavior with ``AXGT_ENABLE_AXGT_DEPOSITS=true`` for backward compatibility.
    """
    raw = (os.getenv("AXGT_ENABLE_AXGT_DEPOSITS") or "").strip().lower()
    if not raw:
        return False
    return raw in ("1", "true", "yes", "on")


def _import_discount():
    """Import the discount module across both flat and packaged sys.path layouts."""
    try:
        from . import discount as _disc
        return _disc
    except ImportError:
        pass
    try:
        from axonos_gate import discount as _disc
        return _disc
    except ImportError:
        pass
    try:
        import discount as _disc
        return _disc
    except ImportError:
        return None


def _import_price_oracle():
    """Import the price oracle across flat and packaged sys.path layouts."""
    try:
        from . import price_oracle as _po
        return _po
    except ImportError:
        pass
    try:
        from axonos_gate import price_oracle as _po
        return _po
    except ImportError:
        pass
    try:
        import price_oracle as _po
        return _po
    except ImportError:
        return None


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


def verify_deposit_is_pending(result: Dict[str, Any]) -> bool:
    """True when verify_deposit returned a pollable wait state (HTTP 200, not an error)."""
    return result.get("pending") is True


def _pending_result(
    wallet: str,
    tx_hash: str,
    message: str,
    *,
    confirmations: Optional[int] = None,
    required: Optional[int] = None,
) -> Dict[str, Any]:
    """Not verified yet; client should poll until verified or a hard error."""
    out: Dict[str, Any] = {
        "verified": False,
        "pending": True,
        "wallet_address": wallet,
        "tx_hash": tx_hash,
        "error": message,
    }
    if confirmations is not None:
        out["confirmations"] = confirmations
    if required is not None:
        out["required"] = required
    return out


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


def _axgt_bonus_pct_fixed() -> Decimal:
    """AXGT payment bonus % (Model B) for the fixed-rate fallback path."""
    raw = (os.getenv("AXGT_USD_BONUS_PERCENT") or "").strip()
    if raw:
        try:
            v = Decimal(raw)
            if v >= 0:
                return v
        except Exception:
            pass
    return Decimal("25")


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
    min_conf = _min_confirmations()
    if not tx_obj:
        # No ledger row: client may poll immediately after broadcast (tx not indexed yet).
        return _pending_result(
            wallet,
            tx_hash,
            "Transaction not found yet — wait a few seconds if you just submitted.",
            confirmations=0,
            required=min_conf,
        )

    # Fetch receipt (None while pending)
    receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx])
    if not receipt:
        return _pending_result(
            wallet,
            tx_hash,
            "Transaction pending — waiting for inclusion in a block.",
            confirmations=0,
            required=min_conf,
        )

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
    if confirmations < min_conf:
        # No ledger row: UI polls until min confirmations (avoids audit spam).
        return _pending_result(
            wallet,
            tx_hash,
            f"Insufficient confirmations (have {confirmations}, need {min_conf})",
            confirmations=confirmations,
            required=min_conf,
        )

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
            base_min_eth = _min_eth_deposit()

            # Re-check on-chain AXGT balance and resolve tier server-side. Client
            # cannot manipulate the discount: if RPC fails, we default safely to
            # no discount (user paid full price; their discount simply isn't
            # applied — they aren't blocked).
            disc_mod = _import_discount()
            tier_info: Dict[str, Any] = {
                "tier_index": 0,
                "tier_label": "Tier 0",
                "discount_percent": 0.0,
                "axgt_balance_axgt": "0",
                "balance_check_ok": False,
            }
            discount_pct = Decimal("0")
            if disc_mod is not None:
                try:
                    bal = disc_mod.fetch_axgt_balance(wallet)
                    floor_axgt = bal.floor_axgt() if bal.ok else 0
                    tier = disc_mod.resolve_tier(floor_axgt)
                    tier_info = {
                        "tier_index": tier.index,
                        "tier_label": tier.label,
                        "tier_min_axgt": tier.min_axgt,
                        "discount_percent": tier.discount_percent if bal.ok else 0.0,
                        "axgt_balance_axgt": format(bal.balance_axgt.normalize(), "f") if bal.ok and bal.balance_axgt > 0 else "0",
                        "balance_check_ok": bal.ok,
                        "balance_check_error": bal.error if not bal.ok else None,
                    }
                    if bal.ok:
                        discount_pct = Decimal(str(tier.discount_percent)) / Decimal("100")
                except Exception as exc:  # noqa: BLE001 — never block payment on tier lookup
                    logger.warning("Discount tier lookup failed for %s: %s", wallet, exc)
                    tier_info["balance_check_error"] = "Tier lookup failed"

            min_eth = (base_min_eth * (Decimal("1") - discount_pct)).quantize(Decimal("0.000000000000000001"))
            if min_eth <= 0:
                min_eth = Decimal("0.000000000000000001")
            if eth_amount < min_eth:
                deposit_ledger.record_verification_reject(
                    wallet,
                    notes=(
                        f"ETH amount {eth_amount} below tier-adjusted minimum {min_eth} "
                        f"(base {base_min_eth}, discount {tier_info.get('discount_percent', 0)}%)"
                    ),
                )
                return fail(
                    f"ETH deposit below minimum ({min_eth} ETH after AXGT discount; base {base_min_eth} ETH)"
                )

            # Base minutes: live USD-equivalent pricing when the oracle is enabled
            # and a fresh price is available; otherwise the fixed ETH rate.
            base_minutes = None
            _oracle = _import_price_oracle()
            if _oracle is not None and _oracle.oracle_enabled():
                m = _oracle.minutes_for_eth(eth_amount)
                if m is not None:
                    base_minutes = Decimal(str(m))
            if base_minutes is None:
                base_minutes = eth_amount * Decimal(str(_eth_credit_per_eth_minutes()))
            # AXGT-holder discount still applies on the ETH rail: a d discount means
            # the same ETH buys 1/(1-d) more minutes.
            if discount_pct >= 1:
                credited_minutes = float(base_minutes)
            else:
                credited_minutes = float(base_minutes / (Decimal("1") - discount_pct))
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
                "base_eth_min": str(base_min_eth),
                "applied_min_eth": str(min_eth),
                "tier": tier_info,
                "credited_minutes": round(credited_minutes, 2),
                "remaining_minutes": round(remaining, 2),
                "confirmations": confirmations,
            }

    # AXGT direct deposit path (legacy / opt-in only). New ETH-first tokenomics
    # treats AXGT as a discount asset, not a payment token.
    if not _axgt_direct_deposits_enabled():
        deposit_ledger.record_verification_reject(
            wallet,
            notes="AXGT direct deposits disabled (ETH-first tokenomics)",
        )
        return fail(
            "Direct AXGT payments are disabled. Pay in ETH to the revenue wallet — "
            "your AXGT holdings automatically apply a discount on the ETH amount."
        )

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

    # Model B: paying in AXGT is simply the best deal — a flat USD-equivalent rate
    # PLUS a fixed bonus (default +25%), with NO holder-tier discount on the AXGT
    # rail. AXGT's two utilities are kept distinct: holding AXGT discounts ETH/USDC
    # payments (tiers, elsewhere); paying IN AXGT gets the bonus rate here.
    #
    # When dynamic pricing is enabled, minutes are priced off AXGT's live USD value
    # (so the user pays the USD-equivalent); otherwise the fixed per-100 rate.
    bonus_pct = Decimal("0")
    credited_minutes = None
    _oracle = _import_price_oracle()
    if _oracle is not None and _oracle.oracle_enabled():
        m = _oracle.minutes_for_axgt(axgt_amount)  # already includes the bonus
        if m is not None:
            credited_minutes = float(m)
            bonus_pct = _oracle.axgt_bonus_pct()
    if credited_minutes is None:
        # Fixed-rate fallback: (axgt_amount/100)*credit_per_100, then apply bonus.
        credit_per_100 = _credit_per_100_minutes()
        bonus_pct = _axgt_bonus_pct_fixed()
        base = axgt_amount / Decimal("100") * Decimal(str(credit_per_100))
        credited_minutes = float(base * (Decimal("1") + bonus_pct / Decimal("100")))

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
        "axgt_bonus_percent": float(bonus_pct),
        "credited_minutes": round(credited_minutes, 2),
        "remaining_minutes": round(remaining, 2),
        "confirmations": confirmations,
    }
