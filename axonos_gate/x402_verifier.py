"""
USDC deposit verification via transaction hash for AxonOS prepaid billing (x402 rail).

Verifies an on-chain USDC transfer to the revenue wallet (on Base by default) and
credits minutes. Self-verified — no facilitator, no oracle, no trust in
client-reported amounts. Mirrors deposit_verifier.py (the AXGT/ETH rail) and lands
minutes in the same deposit ledger.

Configuration (env):
  USDC_RPC_URL                   RPC endpoint for the USDC chain (e.g. Base mainnet)
  USDC_CONTRACT_ADDRESS          USDC token contract on that chain
  AXGT_REVENUE_WALLET            receive address (same EOA as the AXGT/ETH rail)
  USDC_MIN_DEPOSIT               minimum USDC per deposit (default 1)
  USDC_CREDIT_PER_USDC_MINUTES   minutes credited per 1 USDC (default 60)
  USDC_DEPOSIT_MIN_CONFIRMATIONS confirmations before crediting (default 6)
  AXGT_ENABLE_USDC_DEPOSITS      enable/disable the rail (default enabled)
"""

import logging
import os
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

# Reuse the AXGT/ETH rail's primitives: JSON-RPC, log parsing, token decimals,
# confirmation gating, and pending-poll result shape are token-agnostic.
try:
    from . import deposit_verifier as _dv
except ImportError:
    try:
        from axonos_gate import deposit_verifier as _dv
    except ImportError:
        import deposit_verifier as _dv  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

DEFAULT_MIN_CONFIRMATIONS = 6
DEFAULT_USDC_MIN_DEPOSIT = Decimal("1")
DEFAULT_USDC_CREDIT_PER_USDC_MINUTES = 60.0


def usdc_deposits_enabled() -> bool:
    raw = (os.getenv("AXGT_ENABLE_USDC_DEPOSITS") or "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def _get_revenue_wallet() -> str:
    # Same EOA as the AXGT/ETH rail.
    return (os.getenv("AXGT_REVENUE_WALLET") or "").strip().lower()


def _get_usdc_contract() -> str:
    return (os.getenv("USDC_CONTRACT_ADDRESS") or "").strip().lower()


def _get_usdc_rpc_url() -> str:
    return (os.getenv("USDC_RPC_URL") or "").strip()


def _min_confirmations() -> int:
    raw = (os.getenv("USDC_DEPOSIT_MIN_CONFIRMATIONS") or "").strip()
    try:
        n = int(raw)
        if n >= 0:
            return n
    except ValueError:
        pass
    return DEFAULT_MIN_CONFIRMATIONS


def _min_deposit() -> Decimal:
    raw = (os.getenv("USDC_MIN_DEPOSIT") or "").strip()
    if not raw:
        return DEFAULT_USDC_MIN_DEPOSIT
    try:
        val = Decimal(raw)
        if val > 0:
            return val
        logger.warning(
            "Invalid USDC_MIN_DEPOSIT value '%s' (must be positive); using default %s",
            raw,
            DEFAULT_USDC_MIN_DEPOSIT,
        )
    except Exception:
        logger.warning(
            "Invalid USDC_MIN_DEPOSIT value '%s'; using default %s",
            raw,
            DEFAULT_USDC_MIN_DEPOSIT,
        )
    return DEFAULT_USDC_MIN_DEPOSIT


def _credit_per_usdc_minutes() -> float:
    raw = (os.getenv("USDC_CREDIT_PER_USDC_MINUTES") or "").strip()
    try:
        n = float(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    return DEFAULT_USDC_CREDIT_PER_USDC_MINUTES


def verify_usdc_deposit_is_pending(result: Dict[str, Any]) -> bool:
    """True when verify_usdc_deposit returned a pollable wait state (HTTP 200, not error)."""
    return result.get("pending") is True


def verify_usdc_deposit(
    authenticated_wallet: str,
    tx_hash: str,
) -> Dict[str, Any]:
    """
    Verify tx_hash as a USDC transfer from authenticated_wallet to the revenue
    wallet, then credit minutes. Wallet must match the authenticated session;
    never trust wallet_address from the request body alone.

    Returns dict with:
      verified (bool), wallet_address, tx_hash, deposit_currency, usdc_amount,
      tier, credited_minutes, remaining_minutes, confirmations, error (if not verified).
    """
    wallet = (authenticated_wallet or "").strip().lower()
    tx = (tx_hash or "").strip()
    if not tx.startswith("0x"):
        tx = "0x" + tx
    tx = tx.lower()

    revenue = _get_revenue_wallet()
    contract = _get_usdc_contract()
    rpc_url = _get_usdc_rpc_url()

    fail = lambda msg: {
        "verified": False,
        "wallet_address": wallet,
        "tx_hash": tx_hash,
        "error": msg,
    }

    if not usdc_deposits_enabled():
        return fail("USDC deposits are currently disabled")
    if not revenue or not rpc_url or not contract:
        return fail(
            "USDC verification not configured (USDC_RPC_URL, USDC_CONTRACT_ADDRESS, AXGT_REVENUE_WALLET)"
        )
    if not wallet:
        return fail("Wallet address required")

    # Replay protection — shared ledger across all rails (AXGT/ETH/USDC).
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

    min_conf = _min_confirmations()

    tx_obj = _dv._rpc(rpc_url, "eth_getTransactionByHash", [tx])
    if not tx_obj:
        return _dv._pending_result(
            wallet,
            tx_hash,
            "Transaction not found yet — wait a few seconds if you just submitted.",
            confirmations=0,
            required=min_conf,
        )

    receipt = _dv._rpc(rpc_url, "eth_getTransactionReceipt", [tx])
    if not receipt:
        return _dv._pending_result(
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
    if isinstance(status, str) and status not in ("0x1", "1"):
        deposit_ledger.record_verification_reject(wallet, notes="Transaction failed")
        return fail("Transaction failed")
    if isinstance(status, int) and status != 1:
        deposit_ledger.record_verification_reject(wallet, notes="Transaction failed")
        return fail("Transaction failed")

    block_raw = receipt.get("blockNumber")
    if block_raw is None:
        deposit_ledger.record_verification_reject(wallet, notes="Block number missing")
        return fail("Receipt block number missing")
    try:
        block_number = int(block_raw, 16) if isinstance(block_raw, str) else int(block_raw)
    except (ValueError, TypeError):
        deposit_ledger.record_verification_reject(wallet, notes="Invalid block number")
        return fail("Invalid block number")

    latest_raw = _dv._rpc(rpc_url, "eth_blockNumber", [])
    if latest_raw is None:
        deposit_ledger.record_verification_reject(wallet, notes="Could not get latest block")
        return fail("Could not get latest block")
    try:
        latest = int(latest_raw, 16) if isinstance(latest_raw, str) else int(latest_raw)
    except (ValueError, TypeError):
        deposit_ledger.record_verification_reject(wallet, notes="Invalid latest block")
        return fail("Invalid latest block")
    confirmations = latest - block_number + 1
    if confirmations < min_conf:
        return _dv._pending_result(
            wallet,
            tx_hash,
            f"Insufficient confirmations (have {confirmations}, need {min_conf})",
            confirmations=confirmations,
            required=min_conf,
        )

    # Authoritative check: the USDC contract's Transfer event log, with
    # from == authenticated wallet and to == revenue wallet. We deliberately do
    # NOT check tx.from / tx.to here: with smart-account / delegated execution
    # (EIP-7702, MetaMask Smart Accounts, account abstraction, or any relayer),
    # tx.from is the relayer and tx.to is the delegation/entrypoint contract —
    # neither is the token sender. The Transfer log is the real money movement
    # and is what we must verify. _parse_transfer_logs already enforces the
    # contract address, the sender (authenticated wallet), and the recipient
    # (revenue wallet), so a positive amount here is a fully verified payment.
    decimals = _dv._token_decimals(rpc_url, contract)
    logs = receipt.get("logs") or []
    usdc_amount = _dv._parse_transfer_logs(logs, contract, revenue, wallet, decimals)
    if usdc_amount <= 0:
        deposit_ledger.record_verification_reject(
            wallet,
            notes="No valid USDC transfer from authenticated wallet to revenue wallet in this tx",
        )
        return fail(
            "No USDC transfer from your wallet to the revenue wallet found in this transaction"
        )

    # AXGT holder discount: resolve tier server-side from on-chain AXGT balance.
    # Client cannot manipulate it; on RPC failure we default safely to no discount.
    base_min = _min_deposit()
    disc_mod = _dv._import_discount()
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

    min_usdc = (base_min * (Decimal("1") - discount_pct)).quantize(Decimal("0.000001"))
    if min_usdc <= 0:
        min_usdc = Decimal("0.000001")
    if usdc_amount < min_usdc:
        deposit_ledger.record_verification_reject(
            wallet,
            notes=(
                f"USDC amount {usdc_amount} below tier-adjusted minimum {min_usdc} "
                f"(base {base_min}, discount {tier_info.get('discount_percent', 0)}%)"
            ),
        )
        return fail(
            f"USDC deposit below minimum ({min_usdc} USDC after AXGT discount; base {base_min} USDC)"
        )

    credit_per_usdc = Decimal(str(_credit_per_usdc_minutes()))
    # Discount-adjusted credit rate: a discount means the user pays less USDC for
    # the same minutes ⇒ effective minutes per USDC scale up by 1/(1-d).
    if discount_pct >= 1:
        effective_rate = credit_per_usdc
    else:
        effective_rate = credit_per_usdc / (Decimal("1") - discount_pct)
    credited_minutes = float(usdc_amount * effective_rate)

    ok, remaining, err = deposit_ledger.credit_usdc_deposit(
        wallet,
        usdc_amount,
        credited_minutes,
        tx,
        block_number,
    )
    if not ok:
        return fail(err or "Failed to credit USDC deposit")

    return {
        "verified": True,
        "wallet_address": wallet,
        "tx_hash": tx,
        "deposit_currency": "USDC",
        "usdc_amount": str(usdc_amount),
        "axgt_amount": None,
        "eth_amount": None,
        "base_usdc_min": str(base_min),
        "applied_min_usdc": str(min_usdc),
        "tier": tier_info,
        "credited_minutes": round(credited_minutes, 2),
        "remaining_minutes": round(remaining, 2),
        "confirmations": confirmations,
    }


# ---------------------------------------------------------------------------
# x402 protocol: HTTP 402 + X-PAYMENT (EIP-3009) self-settlement.
#
# This is the "true x402" rail. Instead of the user broadcasting their own USDC
# transfer (the tx-hash rail above), the client signs an EIP-3009
# transferWithAuthorization (gasless for them) and sends it in the X-PAYMENT
# header. We verify the signature, submit the authorization on-chain ourselves
# (paying gas), and credit minutes through the same ledger path. No facilitator.
#
# Scheme implemented: "exact" / EIP-3009, matching the x402 v1 wire format.
# ---------------------------------------------------------------------------

import base64
import json
import time

# transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)
_TWA_SELECTOR = "0xe3ee160e"
_X402_VERSION = 1
_X402_SCHEME = "exact"


def _x402_settlement_enabled() -> bool:
    raw = (os.getenv("AXGT_ENABLE_X402_SETTLEMENT") or "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def _usdc_network() -> str:
    return (os.getenv("USDC_NETWORK") or "base").strip().lower()


def _usdc_chain_id() -> int:
    raw = (os.getenv("USDC_CHAIN_ID") or "").strip()
    try:
        n = int(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    return 8453  # Base mainnet


def _usdc_eip712_name() -> str:
    # USDC's EIP-712 domain name. "USD Coin" on Base mainnet.
    return os.getenv("USDC_EIP712_NAME") or "USD Coin"


def _usdc_eip712_version() -> str:
    return os.getenv("USDC_EIP712_VERSION") or "2"


def _decode_abi_string(hex_result: Optional[str]) -> Optional[str]:
    """Decode an ABI-encoded `string` return value (offset, length, bytes)."""
    if not hex_result or hex_result in ("0x", "0x0"):
        return None
    h = hex_result[2:] if hex_result.startswith("0x") else hex_result
    try:
        raw = bytes.fromhex(h)
        if len(raw) < 64:
            return None
        offset = int.from_bytes(raw[0:32], "big")
        length = int.from_bytes(raw[offset:offset + 32], "big")
        start = offset + 32
        return raw[start:start + length].decode("utf-8")
    except Exception:
        return None


def probe_usdc_eip712_domain(rpc_url: str, contract: str) -> Dict[str, Optional[str]]:
    """
    Read name()/version() from the USDC token contract. Returns
    {"name": ..., "version": ...} (values None if a call fails).

    Used to warn when the configured EIP-712 domain disagrees with the on-chain
    contract — a mismatch silently breaks /settle signature recovery.
    """
    name = _decode_abi_string(_dv._rpc(rpc_url, "eth_call", [{"to": contract, "data": "0x06fdde03"}, "latest"]))
    version = _decode_abi_string(_dv._rpc(rpc_url, "eth_call", [{"to": contract, "data": "0x54fd4d50"}, "latest"]))
    return {"name": name, "version": version}


def _warn_on_domain_mismatch(rpc_url: str, contract: str) -> None:
    """Log a WARNING if the configured EIP-712 domain disagrees with on-chain values."""
    try:
        onchain = probe_usdc_eip712_domain(rpc_url, contract)
    except Exception as exc:  # noqa: BLE001 — diagnostic only, never block settlement
        logger.debug("EIP-712 domain probe failed (non-fatal): %s", exc)
        return
    cfg_name, cfg_version = _usdc_eip712_name(), _usdc_eip712_version()
    if onchain.get("name") and onchain["name"] != cfg_name:
        logger.warning(
            "USDC EIP-712 domain name mismatch: configured '%s' but contract reports '%s'. "
            "x402 /settle signature recovery WILL FAIL until USDC_EIP712_NAME is corrected.",
            cfg_name, onchain["name"],
        )
    if onchain.get("version") and onchain["version"] != cfg_version:
        logger.warning(
            "USDC EIP-712 domain version mismatch: configured '%s' but contract reports '%s'. "
            "Set USDC_EIP712_VERSION to match or /settle signature recovery will fail.",
            cfg_version, onchain["version"],
        )


def _settlement_private_key() -> Optional[str]:
    key = (os.getenv("X402_SETTLEMENT_PRIVATE_KEY") or "").strip()
    return key or None


def usdc_base_units_per_minute() -> int:
    """USDC base units (6 decimals) charged per minute, derived from the credit rate."""
    rate = _credit_per_usdc_minutes()  # minutes per 1 USDC
    if rate <= 0:
        rate = DEFAULT_USDC_CREDIT_PER_USDC_MINUTES
    # 1 USDC = 1_000_000 base units; cost per minute = 1e6 / (minutes per USDC).
    return max(1, round(1_000_000 / rate))


def build_payment_requirements(minutes_wanted: float = 0.0) -> Dict[str, Any]:
    """
    Build the x402 'accepts' payment-requirements object returned in a 402 body.

    `maxAmountRequired` is the USDC base-unit cost for `minutes_wanted` (or the
    configured minimum deposit when minutes_wanted <= 0).
    """
    revenue = _get_revenue_wallet()
    contract = _get_usdc_contract()
    per_minute = usdc_base_units_per_minute()
    if minutes_wanted and minutes_wanted > 0:
        amount = max(per_minute, round(per_minute * minutes_wanted))
    else:
        # Default to the minimum deposit (in base units).
        try:
            amount = int((_min_deposit() * Decimal(1_000_000)).to_integral_value())
        except Exception:
            amount = 1_000_000
    return {
        "scheme": _X402_SCHEME,
        "network": _usdc_network(),
        "maxAmountRequired": str(amount),
        "resource": os.getenv("X402_RESOURCE") or "/api/x402/access",
        "description": "AxonOS desktop session minutes",
        "mimeType": "application/json",
        "payTo": revenue,
        "maxTimeoutSeconds": 120,
        "asset": contract,
        "extra": {
            "name": _usdc_eip712_name(),
            "version": _usdc_eip712_version(),
        },
    }


def payment_required_body(minutes_wanted: float = 0.0, error: Optional[str] = None) -> Dict[str, Any]:
    """Full x402 402 response body: x402Version + accepts list (+ optional error)."""
    body: Dict[str, Any] = {
        "x402Version": _X402_VERSION,
        "accepts": [build_payment_requirements(minutes_wanted)],
    }
    if error:
        body["error"] = error
    return body


def discovery_document() -> Dict[str, Any]:
    """
    Machine-readable x402 discovery descriptor (served at /.well-known/x402).

    Tells an agent what AxonOS sells, how to pay, and the agent-native endpoint
    that goes from payment → a usable SSH session in one call. The desktop (GUI)
    rail is intentionally NOT advertised to agents: it's a pixel stream and needs
    a computer-use bridge to be agent-usable.
    """
    return {
        "x402Version": _X402_VERSION,
        "name": "AxonOS GPU compute",
        "description": "On-demand GPU Linux compute, rented by the minute, paid in USDC via x402.",
        "accepts": [build_payment_requirements()],
        "endpoints": [
            {
                "resource": "/api/x402/access",
                "method": "GET",
                "description": "Check access / get payment requirements (returns 402 with terms when unfunded).",
                "query": {"wallet_address": "0x...", "minutes": "optional int"},
            },
            {
                "resource": "/api/x402/settle",
                "method": "POST",
                "description": "Settle an x402 EIP-3009 payment (X-PAYMENT header); credits minutes.",
                "headers": {"X-PAYMENT": "base64 x402 payload"},
            },
            {
                "resource": "/api/x402/session",
                "method": "POST",
                "description": "Agent-native one-shot: pay (X-PAYMENT) AND claim an SSH session. Returns ssh_host/ssh_port + auth_token. No prior wallet sign-in needed.",
                "headers": {"X-PAYMENT": "base64 x402 payload (or omit if pre-funded)"},
                "body": {"wallet_address": "0x...", "ssh_pubkey": "ssh-ed25519 ...", "requested_profile": "optional"},
                "returns": {"granted": "bool", "ssh_host": "str", "ssh_port": "int", "remaining_minutes": "float", "auth_token": "str"},
            },
        ],
        "session_lifecycle": {
            "heartbeat": {"resource": "/api/session/heartbeat", "method": "POST", "note": "send periodically with auth_token to keep the session alive"},
            "release": {"resource": "/api/session/release", "method": "POST"},
        },
        "notes": "SSH is the agent-usable session type (text I/O). The GUI desktop is not exposed to agents.",
    }


def _decode_x402_header(x_payment: str) -> Optional[Dict[str, Any]]:
    """Decode a base64-encoded X-PAYMENT header into its JSON payload."""
    if not x_payment:
        return None
    raw = x_payment.strip()
    try:
        decoded = base64.b64decode(raw, validate=True)
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        # Some clients send raw JSON rather than base64.
        try:
            return json.loads(raw)
        except Exception:
            return None


def _recover_eip3009_signer(
    authorization: Dict[str, Any],
    signature: str,
    contract: str,
) -> Optional[str]:
    """Recover the signer of a TransferWithAuthorization EIP-712 typed message."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except ImportError as exc:
        logger.warning("eth_account unavailable for EIP-3009 recovery: %s", exc)
        return None
    try:
        domain = {
            "name": _usdc_eip712_name(),
            "version": _usdc_eip712_version(),
            "chainId": _usdc_chain_id(),
            "verifyingContract": contract,
        }
        types = {
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        }
        message = {
            "from": authorization["from"],
            "to": authorization["to"],
            "value": int(authorization["value"]),
            "validAfter": int(authorization["validAfter"]),
            "validBefore": int(authorization["validBefore"]),
            "nonce": authorization["nonce"],
        }
        signable = encode_typed_data(
            domain_data=domain,
            message_types=types,
            message_data=message,
        )
        return Account.recover_message(signable, signature=signature)
    except Exception as exc:
        logger.warning("EIP-3009 signer recovery failed: %s", exc)
        return None


def _split_signature(signature: str):
    """Return (v, r_bytes, s_bytes) from a 65-byte hex signature."""
    sig = signature.strip().lower()
    if sig.startswith("0x"):
        sig = sig[2:]
    if len(sig) != 130:
        raise ValueError("signature must be 65 bytes")
    r = bytes.fromhex(sig[0:64])
    s = bytes.fromhex(sig[64:128])
    v = int(sig[128:130], 16)
    if v < 27:
        v += 27
    return v, r, s


def _submit_transfer_with_authorization(
    rpc_url: str,
    contract: str,
    authorization: Dict[str, Any],
    signature: str,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Build, sign (with the settlement key), and broadcast a transferWithAuthorization
    call. Returns (tx_hash, error). We pay gas; the user's USDC moves to payTo.
    """
    pk = _settlement_private_key()
    if not pk:
        return None, "Settlement signer not configured (X402_SETTLEMENT_PRIVATE_KEY)"
    try:
        from eth_account import Account
        from eth_abi import encode as abi_encode
    except ImportError as exc:
        return None, f"Settlement dependencies unavailable: {exc}"
    try:
        v, r, s = _split_signature(signature)
        nonce_hex = authorization["nonce"]
        nonce_bytes = bytes.fromhex(nonce_hex[2:] if nonce_hex.startswith("0x") else nonce_hex)
        if len(nonce_bytes) != 32:
            return None, "Invalid authorization nonce length"
        args = abi_encode(
            [
                "address", "address", "uint256",
                "uint256", "uint256", "bytes32",
                "uint8", "bytes32", "bytes32",
            ],
            [
                authorization["from"],
                authorization["to"],
                int(authorization["value"]),
                int(authorization["validAfter"]),
                int(authorization["validBefore"]),
                nonce_bytes,
                v,
                r,
                s,
            ],
        )
        data = _TWA_SELECTOR + args.hex()

        from eth_utils import to_checksum_address
        contract_cs = to_checksum_address(contract)

        acct = Account.from_key(pk)
        sender = acct.address
        # Nonce + gas price from the chain.
        tx_count = _dv._rpc(rpc_url, "eth_getTransactionCount", [sender, "pending"])
        gas_price = _dv._rpc(rpc_url, "eth_gasPrice", [])
        chain_id = _usdc_chain_id()
        if tx_count is None or gas_price is None:
            return None, "Could not fetch nonce/gas price for settlement"
        gas_est = _dv._rpc(
            rpc_url, "eth_estimateGas",
            [{"from": sender, "to": contract_cs, "data": "0x" + data if not data.startswith("0x") else data}],
        )
        try:
            gas_limit = int(gas_est, 16) if isinstance(gas_est, str) else int(gas_est)
            gas_limit = int(gas_limit * 1.25)
        except (ValueError, TypeError):
            gas_limit = 200000
        tx = {
            "to": contract_cs,
            "value": 0,
            "gas": gas_limit,
            "gasPrice": int(gas_price, 16) if isinstance(gas_price, str) else int(gas_price),
            "nonce": int(tx_count, 16) if isinstance(tx_count, str) else int(tx_count),
            "data": data if data.startswith("0x") else "0x" + data,
            "chainId": chain_id,
        }
        signed = Account.sign_transaction(tx, pk)
        raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
        tx_hash = _dv._rpc(rpc_url, "eth_sendRawTransaction", ["0x" + raw.hex()])
        if not tx_hash:
            return None, "Settlement broadcast failed"
        return tx_hash, None
    except Exception as exc:
        logger.warning("transferWithAuthorization settlement failed: %s", exc)
        return None, f"Settlement error: {exc}"


def settle_x402_payment(authenticated_wallet: str, x_payment_header: str) -> Dict[str, Any]:
    """
    Verify and settle an x402 X-PAYMENT (EIP-3009) payment, then credit minutes.

    Steps: decode header → validate authorization fields against our requirements
    → recover signer (must equal authenticated wallet & authorization.from) →
    broadcast transferWithAuthorization → verify the settled tx by tx-hash via
    verify_usdc_deposit (which credits minutes, with replay protection).

    Returns the verify_usdc_deposit result, plus settlement_tx_hash, or {error}.
    """
    wallet = (authenticated_wallet or "").strip().lower()
    revenue = _get_revenue_wallet()
    contract = _get_usdc_contract()
    rpc_url = _get_usdc_rpc_url()

    fail = lambda msg: {"verified": False, "wallet_address": wallet, "error": msg}

    if not _x402_settlement_enabled():
        return fail("x402 settlement is disabled")
    if not usdc_deposits_enabled():
        return fail("USDC deposits are currently disabled")
    if not revenue or not rpc_url or not contract:
        return fail("USDC verification not configured (USDC_RPC_URL, USDC_CONTRACT_ADDRESS, AXGT_REVENUE_WALLET)")
    if not wallet:
        return fail("Wallet address required")

    payload = _decode_x402_header(x_payment_header)
    if not payload:
        return fail("Malformed X-PAYMENT header")
    if int(payload.get("x402Version", 0)) != _X402_VERSION:
        return fail(f"Unsupported x402Version (need {_X402_VERSION})")
    if (payload.get("scheme") or "").lower() != _X402_SCHEME:
        return fail(f"Unsupported scheme (need '{_X402_SCHEME}')")
    if (payload.get("network") or "").lower() != _usdc_network():
        return fail(f"Unsupported network (need '{_usdc_network()}')")

    pay = payload.get("payload") or {}
    authorization = pay.get("authorization") or {}
    signature = pay.get("signature") or ""
    required = {"from", "to", "value", "validAfter", "validBefore", "nonce"}
    if not required.issubset(authorization.keys()) or not signature:
        return fail("X-PAYMENT missing authorization fields or signature")

    # Authorization must pay our revenue wallet, from the authenticated wallet.
    if (authorization["to"] or "").strip().lower() != revenue:
        return fail("Authorization recipient is not the revenue wallet")
    if (authorization["from"] or "").strip().lower() != wallet:
        return fail("Authorization sender does not match authenticated wallet")

    # Time-window sanity.
    now = int(time.time())
    try:
        if int(authorization["validAfter"]) > now:
            return fail("Authorization not yet valid")
        if int(authorization["validBefore"]) <= now:
            return fail("Authorization expired")
    except (ValueError, TypeError):
        return fail("Invalid authorization validity window")

    # Amount must cover at least the (discount-adjusted) minimum.
    try:
        value_units = int(authorization["value"])
    except (ValueError, TypeError):
        return fail("Invalid authorization value")
    min_units = int((_min_deposit() * Decimal(1_000_000)).to_integral_value())
    if value_units < min_units:
        return fail(f"Authorization value below minimum ({min_units} USDC base units)")

    # Verify the EIP-712 signature recovers to the authenticated wallet.
    # Probe the contract's domain first: on a mismatch, recovery fails silently,
    # so surface the likely cause (misconfigured USDC_EIP712_NAME/VERSION) in logs.
    recovered = _recover_eip3009_signer(authorization, signature, contract)
    if not recovered or recovered.strip().lower() != wallet:
        _warn_on_domain_mismatch(rpc_url, contract)
        return fail("Authorization signature does not match authenticated wallet")

    # Settle on-chain (we pay gas).
    settle_tx, settle_err = _submit_transfer_with_authorization(
        rpc_url, contract, authorization, signature
    )
    if not settle_tx:
        return fail(settle_err or "Settlement failed")

    # We just broadcast the tx, so the deposit verifier would see it as pending
    # (unmined / 0 confirmations) and credit nothing. Wait for the receipt + the
    # required confirmations before verifying, so the agent gets credited in this
    # same call. Bounded wait — on timeout we return a pollable pending result with
    # the settlement tx hash so the client can retry verification.
    _wait_for_confirmations(rpc_url, settle_tx, _min_confirmations())

    # Credit via the same tx-hash verifier (handles confirmations + replay guard).
    result = verify_usdc_deposit(authenticated_wallet=wallet, tx_hash=settle_tx)
    result = dict(result)
    result["settlement_tx_hash"] = settle_tx
    result["x402"] = True
    return result


def _wait_for_confirmations(rpc_url: str, tx_hash: str, min_conf: int, timeout_s: int = 90) -> bool:
    """Poll until tx_hash has >= min_conf confirmations (or timeout). Returns True if reached."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        receipt = _dv._rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash])
        if receipt and receipt.get("blockNumber") is not None:
            try:
                blk = int(receipt["blockNumber"], 16) if isinstance(receipt["blockNumber"], str) else int(receipt["blockNumber"])
                latest_raw = _dv._rpc(rpc_url, "eth_blockNumber", [])
                latest = int(latest_raw, 16) if isinstance(latest_raw, str) else int(latest_raw)
                if (latest - blk + 1) >= max(1, min_conf):
                    return True
            except (ValueError, TypeError):
                pass
        time.sleep(3)
    return False
