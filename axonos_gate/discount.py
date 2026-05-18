"""
AXGT holder-based discount module for AxonOS ETH-first tokenomics.

ETH is the primary payment currency for compute/session credits. AXGT is no
longer required as a payment token; instead, holders of AXGT receive a usage
discount based on the size of their AXGT holdings.

Responsibilities of this module:

1. Load the discount tier configuration (env-driven, with safe defaults).
2. Fetch the on-chain AXGT balance for a wallet via direct JSON-RPC
   ``eth_call(balanceOf)`` against the configured AXGT contract / RPC URL.
3. Resolve the eligible tier and discount percentage for a given balance.
4. Compute discount-adjusted ETH quotes (base price → final price).

Design notes:

- The tier config is the single source of truth on the backend. The frontend
  must never compute the user's discount from balances it has fetched itself
  — it always asks the backend (``GET /api/discount/quote``) and the backend
  re-checks the on-chain balance again at credit time. This prevents
  client-side manipulation of discount values.
- RPC failures default safely to **no discount** rather than blocking the
  whole flow. The user can still pay full ETH price; their discount simply
  isn't applied. Operators can monitor RPC errors via logs.
- Tier thresholds are expressed in **whole AXGT units** (not wei). The lookup
  uses the *floor* (truncated integer) of the user's AXGT balance — so
  ``99.999`` AXGT is Tier 0, exactly ``100`` is Tier 1, etc.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ERC-20 selectors
_BALANCE_OF_SELECTOR = "0x70a08231"
_DECIMALS_SELECTOR = "0x313ce567"

DEFAULT_TOKEN_DECIMALS = 18
_RPC_TIMEOUT_SECONDS = 10

# Suggested tier defaults (mirrors docs/TOKENOMICS.md):
#   Tier 0:        0 –     99 AXGT  →  0%  discount
#   Tier 1:      100 –    999 AXGT  →  5%  discount
#   Tier 2:    1,000 –  9,999 AXGT  → 10%  discount
#   Tier 3:   10,000 – 99,999 AXGT  → 15%  discount
#   Tier 4:  100,000+         AXGT  → 25%  discount
DEFAULT_TIERS: List[Dict[str, Any]] = [
    {"index": 0, "label": "Tier 0", "min_axgt": 0,       "discount_percent": 0},
    {"index": 1, "label": "Tier 1", "min_axgt": 100,     "discount_percent": 5},
    {"index": 2, "label": "Tier 2", "min_axgt": 1000,    "discount_percent": 10},
    {"index": 3, "label": "Tier 3", "min_axgt": 10000,   "discount_percent": 15},
    {"index": 4, "label": "Tier 4", "min_axgt": 100000,  "discount_percent": 25},
]


@dataclass
class Tier:
    index: int
    label: str
    min_axgt: int
    discount_percent: float

    def to_public(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "min_axgt": self.min_axgt,
            "discount_percent": self.discount_percent,
        }


@dataclass
class BalanceResult:
    """Result of an on-chain balanceOf query."""

    ok: bool
    balance_wei: int
    decimals: int
    balance_axgt: Decimal
    error: Optional[str] = None

    def floor_axgt(self) -> int:
        """Whole-AXGT count used for tier lookup (always truncates downward)."""
        if self.balance_axgt <= 0:
            return 0
        return int(self.balance_axgt)


# ---------------------------------------------------------------------------
# Tier configuration
# ---------------------------------------------------------------------------


def _parse_tier_list(raw: Any) -> Optional[List[Tier]]:
    """Convert a list of dicts (from JSON) into a sorted, validated list of Tier."""
    if not isinstance(raw, list) or not raw:
        return None
    parsed: List[Tier] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        try:
            min_axgt_raw = entry.get("min_axgt", entry.get("min"))
            disc_raw = entry.get("discount_percent", entry.get("discount"))
            if min_axgt_raw is None or disc_raw is None:
                return None
            min_axgt = int(Decimal(str(min_axgt_raw)))
            disc = float(disc_raw)
            if min_axgt < 0 or disc < 0 or disc > 100:
                return None
            label = str(entry.get("label") or f"Tier {len(parsed)}")
            parsed.append(Tier(index=len(parsed), label=label, min_axgt=min_axgt, discount_percent=disc))
        except (InvalidOperation, ValueError, TypeError):
            return None
    parsed.sort(key=lambda t: t.min_axgt)
    # Re-index so lowest threshold is index 0 even if the source listed them out of order.
    for new_index, tier in enumerate(parsed):
        tier.index = new_index
        tier.label = tier.label or f"Tier {new_index}"
    return parsed


def _parse_tier_compact(raw: str) -> Optional[List[Tier]]:
    """Parse compact env format ``min:percent[,min:percent...]`` (e.g. ``0:0,100:5,1000:10``)."""
    pieces = [p.strip() for p in raw.split(",") if p.strip()]
    if not pieces:
        return None
    parsed: List[Tier] = []
    for idx, piece in enumerate(pieces):
        if ":" not in piece:
            return None
        m_raw, d_raw = piece.split(":", 1)
        try:
            min_axgt = int(Decimal(m_raw.strip()))
            disc = float(d_raw.strip())
        except (InvalidOperation, ValueError):
            return None
        if min_axgt < 0 or disc < 0 or disc > 100:
            return None
        parsed.append(Tier(index=idx, label=f"Tier {idx}", min_axgt=min_axgt, discount_percent=disc))
    parsed.sort(key=lambda t: t.min_axgt)
    for new_index, tier in enumerate(parsed):
        tier.index = new_index
        tier.label = f"Tier {new_index}"
    return parsed


def _load_default_tiers() -> List[Tier]:
    return [
        Tier(
            index=int(t["index"]),
            label=str(t["label"]),
            min_axgt=int(t["min_axgt"]),
            discount_percent=float(t["discount_percent"]),
        )
        for t in DEFAULT_TIERS
    ]


def _load_tiers_from_file(path: str) -> Optional[List[Tier]]:
    try:
        with open(path, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("AXGT_DISCOUNT_TIERS_FILE '%s' invalid: %s", path, exc)
        return None
    if isinstance(raw, dict) and "tiers" in raw:
        raw = raw["tiers"]
    return _parse_tier_list(raw)


def get_tiers() -> List[Tier]:
    """Load tier configuration with the following precedence:

    1. ``AXGT_DISCOUNT_TIERS_JSON`` (full JSON list of objects)
    2. ``AXGT_DISCOUNT_TIERS_FILE`` (path to a JSON config file)
    3. ``AXGT_DISCOUNT_TIERS`` (compact ``min:percent,...`` list)
    4. Built-in defaults (the suggested 0/100/1000/10000/100000 tiers).

    Any malformed override falls back to defaults with a logged warning.
    """
    raw_json = (os.getenv("AXGT_DISCOUNT_TIERS_JSON") or "").strip()
    if raw_json:
        try:
            decoded = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            logger.warning("AXGT_DISCOUNT_TIERS_JSON invalid JSON: %s; using defaults", exc)
        else:
            tiers = _parse_tier_list(decoded)
            if tiers:
                return tiers
            logger.warning("AXGT_DISCOUNT_TIERS_JSON has invalid entries; using defaults")

    raw_file = (os.getenv("AXGT_DISCOUNT_TIERS_FILE") or "").strip()
    if raw_file:
        tiers = _load_tiers_from_file(raw_file)
        if tiers:
            return tiers
        logger.warning("AXGT_DISCOUNT_TIERS_FILE '%s' invalid; using defaults", raw_file)

    raw_compact = (os.getenv("AXGT_DISCOUNT_TIERS") or "").strip()
    if raw_compact:
        tiers = _parse_tier_compact(raw_compact)
        if tiers:
            return tiers
        logger.warning("AXGT_DISCOUNT_TIERS invalid format; using defaults")

    return _load_default_tiers()


def resolve_tier(balance_axgt: int, tiers: Optional[List[Tier]] = None) -> Tier:
    """Return the highest tier whose ``min_axgt`` is <= ``balance_axgt``."""
    cfg = tiers if tiers is not None else get_tiers()
    if not cfg:
        return Tier(index=0, label="Tier 0", min_axgt=0, discount_percent=0.0)
    chosen = cfg[0]
    for tier in cfg:
        if balance_axgt >= tier.min_axgt:
            chosen = tier
        else:
            break
    return chosen


# ---------------------------------------------------------------------------
# On-chain AXGT balance lookup
# ---------------------------------------------------------------------------


def _get_rpc_url() -> str:
    return (os.getenv("AXGT_RPC_URL") or "").strip()


def _get_contract_address() -> str:
    return (os.getenv("AXGT_CONTRACT_ADDRESS") or "").strip()


def _eth_call(rpc_url: str, to: str, data: str) -> Tuple[Optional[str], Optional[str]]:
    """Direct JSON-RPC eth_call. Returns (result_hex, error_string)."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    }
    try:
        resp = requests.post(
            rpc_url,
            json=payload,
            timeout=_RPC_TIMEOUT_SECONDS,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = str(data["error"])
            logger.warning("balanceOf RPC error: %s", err)
            return None, err
        result = data.get("result")
        if result is None:
            return None, "Empty RPC result"
        return result, None
    except requests.RequestException as exc:
        logger.warning("balanceOf RPC request failed: %s", exc)
        return None, f"RPC request failed: {exc}"
    except ValueError as exc:
        logger.warning("balanceOf RPC parse failed: %s", exc)
        return None, f"RPC parse failed: {exc}"


def _validate_wallet(address: str) -> bool:
    if not address or not isinstance(address, str):
        return False
    if not address.startswith("0x") or len(address) != 42:
        return False
    try:
        int(address[2:], 16)
        return True
    except ValueError:
        return False


def fetch_axgt_balance(
    wallet_address: str,
    rpc_url: Optional[str] = None,
    contract_address: Optional[str] = None,
) -> BalanceResult:
    """Fetch the on-chain AXGT balance for ``wallet_address`` via JSON-RPC.

    On any RPC error returns ``BalanceResult(ok=False, ...)`` so callers can
    safely default to *no discount* without raising.
    """
    rpc = rpc_url if rpc_url is not None else _get_rpc_url()
    contract = contract_address if contract_address is not None else _get_contract_address()
    zero = BalanceResult(ok=False, balance_wei=0, decimals=DEFAULT_TOKEN_DECIMALS, balance_axgt=Decimal("0"))

    if not _validate_wallet(wallet_address):
        return BalanceResult(ok=False, balance_wei=0, decimals=DEFAULT_TOKEN_DECIMALS,
                             balance_axgt=Decimal("0"), error="Invalid wallet address")
    if not rpc:
        return BalanceResult(ok=False, balance_wei=0, decimals=DEFAULT_TOKEN_DECIMALS,
                             balance_axgt=Decimal("0"), error="AXGT_RPC_URL not configured")
    if not contract:
        return BalanceResult(ok=False, balance_wei=0, decimals=DEFAULT_TOKEN_DECIMALS,
                             balance_axgt=Decimal("0"), error="AXGT_CONTRACT_ADDRESS not configured")

    padded_addr = wallet_address[2:].lower().zfill(64)
    bal_hex, bal_err = _eth_call(rpc, contract, _BALANCE_OF_SELECTOR + padded_addr)
    if bal_hex is None or bal_hex == "0x":
        result = BalanceResult(ok=False, balance_wei=0, decimals=DEFAULT_TOKEN_DECIMALS,
                               balance_axgt=Decimal("0"), error=bal_err or "balanceOf returned empty")
        return result
    try:
        balance_wei = int(bal_hex, 16)
    except (ValueError, TypeError):
        return BalanceResult(ok=False, balance_wei=0, decimals=DEFAULT_TOKEN_DECIMALS,
                             balance_axgt=Decimal("0"), error="balanceOf returned non-integer hex")

    decimals = DEFAULT_TOKEN_DECIMALS
    dec_hex, _ = _eth_call(rpc, contract, _DECIMALS_SELECTOR)
    if dec_hex and dec_hex != "0x":
        try:
            parsed = int(dec_hex, 16)
            if 0 <= parsed <= 36:
                decimals = parsed
        except (ValueError, TypeError):
            pass

    divisor = Decimal(10) ** decimals
    balance_axgt = Decimal(balance_wei) / divisor if divisor > 0 else Decimal("0")
    return BalanceResult(ok=True, balance_wei=balance_wei, decimals=decimals, balance_axgt=balance_axgt)


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------


def apply_discount(base_eth: Decimal, discount_percent: float) -> Decimal:
    """Apply a percentage discount to ``base_eth``. Clamps result to [0, base_eth]."""
    if base_eth <= 0:
        return Decimal("0")
    if discount_percent <= 0:
        return base_eth
    pct = Decimal(str(discount_percent)) / Decimal("100")
    if pct >= 1:
        return Decimal("0")
    return (base_eth * (Decimal("1") - pct)).quantize(Decimal("1.000000000000000000"))


def quote_for_balance(
    base_eth: Decimal,
    balance_axgt: int,
    tiers: Optional[List[Tier]] = None,
) -> Dict[str, Any]:
    """Return the discount quote for a given AXGT balance and ETH price."""
    cfg = tiers if tiers is not None else get_tiers()
    tier = resolve_tier(balance_axgt, cfg)
    final_eth = apply_discount(base_eth, tier.discount_percent)
    return {
        "tier_index": tier.index,
        "tier_label": tier.label,
        "tier_min_axgt": tier.min_axgt,
        "discount_percent": tier.discount_percent,
        "base_eth": format(base_eth.normalize(), "f") if base_eth > 0 else "0",
        "final_eth": format(final_eth.normalize(), "f") if final_eth > 0 else "0",
        "axgt_balance_floor": balance_axgt,
    }


def public_tiers() -> List[Dict[str, Any]]:
    """Tier list as plain dicts for JSON serialization."""
    return [t.to_public() for t in get_tiers()]


__all__ = [
    "BalanceResult",
    "DEFAULT_TIERS",
    "Tier",
    "apply_discount",
    "fetch_axgt_balance",
    "get_tiers",
    "public_tiers",
    "quote_for_balance",
    "resolve_tier",
]
