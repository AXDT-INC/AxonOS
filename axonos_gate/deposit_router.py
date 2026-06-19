"""
Rail auto-detection for manual tx-hash deposit verification.

A pasted tx hash could be an ETH/AXGT payment OR a USDC payment. Rather than make
the client guess (or try rails sequentially and risk polling the wrong rail while
a tx is briefly unconfirmed), this runs both verifiers server-side and returns a
single result with correct precedence.

Precedence (most→least decisive):
  1. verified            — a rail credited minutes → return it.
  2. already credited    — the shared ledger replay guard fired → return it.
  3. pending             — at least one rail is still confirming AND no rail gave
                           a definitive "this isn't my tx" failure → return pending
                           so the client keeps polling (auto-detect).
  4. hard failure        — no rail matched → return the most informative error.

Both verifiers share the same ledger and tx-hash replay guard, so running both is
safe: only the rail that actually contains a transfer from the wallet to the
revenue wallet can credit, and a tx can't be double-credited.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _is_already_credited(result: Dict[str, Any]) -> bool:
    err = str(result.get("error") or "")
    return bool(err) and (
        "already credited" in err.lower() or "duplicate tx_hash" in err.lower()
    )


def verify_deposit_auto(
    authenticated_wallet: str,
    tx_hash: str,
    *,
    verify_eth: Optional[Callable[..., Dict[str, Any]]],
    eth_is_pending: Optional[Callable[[Dict[str, Any]], bool]],
    verify_usdc: Optional[Callable[..., Dict[str, Any]]],
    usdc_is_pending: Optional[Callable[[Dict[str, Any]], bool]],
) -> Tuple[Dict[str, Any], bool]:
    """
    Try the ETH/AXGT and USDC rails and return (result, is_pending).

    `is_pending` is True when the caller should treat this as a pollable 200
    (still confirming) rather than a terminal success/failure.

    Verifier callables are injected (the two gate processes import them under
    different module paths), so this module has no import-path coupling.
    """
    rails: List[Tuple[str, Callable[..., Dict[str, Any]], Optional[Callable[[Dict[str, Any]], bool]]]] = []
    if verify_eth is not None:
        rails.append(("eth", verify_eth, eth_is_pending))
    if verify_usdc is not None:
        rails.append(("usdc", verify_usdc, usdc_is_pending))

    if not rails:
        return {"verified": False, "error": "Deposit verification unavailable"}, False

    results: List[Tuple[str, Dict[str, Any], bool]] = []
    for name, verify, is_pending_fn in rails:
        try:
            res = verify(authenticated_wallet=authenticated_wallet, tx_hash=tx_hash)
        except Exception as exc:  # noqa: BLE001 — one rail failing must not kill the other
            logger.warning("verify_deposit_auto: %s rail raised: %s", name, exc)
            continue
        pending = bool(is_pending_fn(res)) if is_pending_fn else bool(res.get("pending"))
        # 1. A rail credited → done immediately.
        if res.get("verified"):
            return res, False
        results.append((name, res, pending))

    # 2. Already credited (shared ledger replay guard) → surface it.
    for _name, res, _pending in results:
        if _is_already_credited(res):
            return res, False

    # 3. Any rail still confirming → pending (keep polling; rail will resolve).
    for _name, res, pending in results:
        if pending:
            return res, True

    # 4. No match anywhere → most informative hard failure.
    # Prefer a rail whose error names a concrete mismatch over a generic one.
    if results:
        # Last rail's error is usually the more specific (USDC checked after ETH),
        # but prefer any error mentioning the revenue wallet / transfer specifics.
        best = results[-1][1]
        for _name, res, _pending in results:
            err = str(res.get("error") or "").lower()
            if "revenue wallet" in err or "transfer" in err:
                best = res
                break
        return best, False

    return {"verified": False, "error": "Could not verify transaction on any rail"}, False
