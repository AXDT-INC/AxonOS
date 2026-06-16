"""
USD price oracle for ETH and AXGT (CoinGecko free API).

Minutes are priced in USD ($1/hour by default). ETH and AXGT payment amounts are
derived from live USD prices polled ~8x/day and cached in Postgres (shared across
both gate processes, survives restart). USDC is a stablecoin and is NOT priced
here — it stays at its fixed rate.

Design / safety:
  - Last-known cached price is used if a poll fails (payments never block), but a
    max-staleness cap (PRICE_MAX_STALE_SECONDS, default 24h) refuses to price off
    a price older than that.
  - Verification uses the price at verification time (the current cached value).
  - AXGT gets a configurable bonus (AXGT_USD_BONUS_PERCENT, default 25%): paying in
    AXGT yields that much more desktop time per USD-equivalent than ETH/USDC.

This introduces a price oracle (a deliberate change from the prior "no oracle"
stance) ONLY for converting USD-priced minutes into ETH/AXGT amounts. It never
trusts client-reported prices: the server reads the cache, never the request.
"""

import logging
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# CoinGecko free API (no key). ids verified live: ETH + current AXGT token.
_CG_URL = "https://api.coingecko.com/api/v3/simple/price"
_ETH_ID = "ethereum"
_AXGT_ID_DEFAULT = "axondao-governance-token-2"

_PRICE_TABLE = "axgt_price_cache"
_DEFAULT_USD_PER_HOUR = Decimal("1.0")
_DEFAULT_AXGT_BONUS_PCT = Decimal("25")
_DEFAULT_MAX_STALE_SECONDS = 24 * 3600
_DEFAULT_POLL_INTERVAL_SECONDS = 3 * 3600  # ~8x/day

_init_done = False


def _db_url() -> Optional[str]:
    return os.getenv("AXGT_CHALLENGE_DB_URL") or None


def _get_conn():
    url = _db_url()
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as exc:
        logger.warning("price_oracle: Postgres connect failed: %s", exc)
        return None


def _axgt_cg_id() -> str:
    return (os.getenv("AXGT_COINGECKO_ID") or "").strip() or _AXGT_ID_DEFAULT


def usd_per_hour() -> Decimal:
    raw = (os.getenv("AXGT_USD_PER_HOUR") or "").strip()
    if raw:
        try:
            v = Decimal(raw)
            if v > 0:
                return v
        except (InvalidOperation, ValueError):
            pass
    return _DEFAULT_USD_PER_HOUR


def usd_per_minute() -> Decimal:
    return usd_per_hour() / Decimal("60")


def axgt_bonus_pct() -> Decimal:
    raw = (os.getenv("AXGT_USD_BONUS_PERCENT") or "").strip()
    if raw:
        try:
            v = Decimal(raw)
            if v >= 0:
                return v
        except (InvalidOperation, ValueError):
            pass
    return _DEFAULT_AXGT_BONUS_PCT


def max_stale_seconds() -> int:
    raw = (os.getenv("PRICE_MAX_STALE_SECONDS") or "").strip()
    try:
        n = int(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    return _DEFAULT_MAX_STALE_SECONDS


def poll_interval_seconds() -> int:
    raw = (os.getenv("PRICE_POLL_INTERVAL_SECONDS") or "").strip()
    try:
        n = int(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    return _DEFAULT_POLL_INTERVAL_SECONDS


def oracle_enabled() -> bool:
    """USD-equivalent dynamic pricing for ETH/AXGT. Default off for safety —
    operators opt in with AXGT_DYNAMIC_PRICING=true once the DB is reachable."""
    raw = (os.getenv("AXGT_DYNAMIC_PRICING") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _init_once() -> bool:
    global _init_done
    if _init_done:
        return True
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {_PRICE_TABLE} (
                    asset TEXT PRIMARY KEY,
                    usd_price NUMERIC NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL
                )"""
            )
        conn.commit()
        _init_done = True
        return True
    except Exception as exc:
        conn.rollback()
        logger.warning("price_oracle: table init failed: %s", exc)
        return False
    finally:
        conn.close()


def _store_price(asset: str, usd_price: Decimal, ts: float) -> None:
    if not _init_once():
        return
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {_PRICE_TABLE} (asset, usd_price, updated_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (asset) DO UPDATE SET
                      usd_price = EXCLUDED.usd_price, updated_at = EXCLUDED.updated_at""",
                (asset, str(usd_price), ts),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning("price_oracle: store %s failed: %s", asset, exc)
    finally:
        conn.close()


def _read_price(asset: str) -> Optional[Tuple[Decimal, float]]:
    """Return (usd_price, updated_at) from cache, or None."""
    if not _init_once():
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT usd_price, updated_at FROM {_PRICE_TABLE} WHERE asset = %s",
                (asset,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return Decimal(str(row[0])), float(row[1])
    except Exception as exc:
        logger.warning("price_oracle: read %s failed: %s", asset, exc)
        return None
    finally:
        conn.close()


def poll_prices() -> bool:
    """Fetch ETH + AXGT USD prices from CoinGecko and cache them. Returns success."""
    axgt_id = _axgt_cg_id()
    try:
        resp = requests.get(
            _CG_URL,
            params={"ids": f"{_ETH_ID},{axgt_id}", "vs_currencies": "usd"},
            timeout=15,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("price_oracle: CoinGecko poll failed: %s", exc)
        return False

    now = time.time()
    ok = False
    eth = (data.get(_ETH_ID) or {}).get("usd")
    axgt = (data.get(axgt_id) or {}).get("usd")
    try:
        if eth and Decimal(str(eth)) > 0:
            _store_price("ETH", Decimal(str(eth)), now)
            ok = True
        if axgt and Decimal(str(axgt)) > 0:
            _store_price("AXGT", Decimal(str(axgt)), now)
            ok = True
    except (InvalidOperation, ValueError) as exc:
        logger.warning("price_oracle: bad price payload: %s", exc)
        return False
    if ok:
        logger.info("price_oracle: updated ETH=%s AXGT=%s USD", eth, axgt)
    return ok


_last_poll_attempt = 0.0


def _maybe_refresh() -> None:
    """Lazy poller: refresh prices if the cache is older than the poll interval.

    Avoids needing a separate cron/supervisor job — any pricing call triggers a
    refresh at most once per interval. Throttled in-process so concurrent requests
    don't stampede CoinGecko.
    """
    global _last_poll_attempt
    now = time.time()
    interval = poll_interval_seconds()
    if (now - _last_poll_attempt) < interval:
        return
    # Check the cache age (cheap) before hitting the network.
    rec = _read_price("ETH")
    if rec and (now - rec[1]) < interval:
        return
    _last_poll_attempt = now
    poll_prices()


def get_usd_price(asset: str) -> Optional[Decimal]:
    """Cached USD price for 'ETH' or 'AXGT', or None if missing/too stale."""
    _maybe_refresh()
    rec = _read_price(asset)
    if not rec:
        return None
    price, updated_at = rec
    if (time.time() - updated_at) > max_stale_seconds():
        logger.warning(
            "price_oracle: %s price stale (%.0fh old) — refusing to use",
            asset, (time.time() - updated_at) / 3600,
        )
        return None
    return price if price > 0 else None


# --- Conversions: USD-priced minutes <-> crypto amounts ---

def minutes_for_eth(eth_amount: Decimal) -> Optional[float]:
    """Minutes credited for an ETH deposit at the live USD price ($/hour baseline)."""
    price = get_usd_price("ETH")
    if price is None:
        return None
    usd_value = eth_amount * price
    return float(usd_value / usd_per_minute())


def minutes_for_axgt(axgt_amount: Decimal) -> Optional[float]:
    """Minutes for an AXGT deposit at live USD price, plus the AXGT bonus (+25%)."""
    price = get_usd_price("AXGT")
    if price is None:
        return None
    usd_value = axgt_amount * price
    base_minutes = usd_value / usd_per_minute()
    boosted = base_minutes * (Decimal("1") + axgt_bonus_pct() / Decimal("100"))
    return float(boosted)


def eth_amount_for_usd(usd: Decimal) -> Optional[Decimal]:
    price = get_usd_price("ETH")
    if price is None or price <= 0:
        return None
    return usd / price


def axgt_amount_for_usd(usd: Decimal) -> Optional[Decimal]:
    """AXGT needed to cover `usd` of value AFTER the AXGT bonus (so it's cheaper)."""
    price = get_usd_price("AXGT")
    if price is None or price <= 0:
        return None
    # Bonus means the user needs less AXGT for the same minutes: divide by (1+bonus).
    effective = usd / (Decimal("1") + axgt_bonus_pct() / Decimal("100"))
    return effective / price


def price_snapshot() -> Dict[str, object]:
    """Diagnostic snapshot for /api/config or admin."""
    out: Dict[str, object] = {
        "dynamic_pricing_enabled": oracle_enabled(),
        "usd_per_hour": float(usd_per_hour()),
        "axgt_bonus_percent": float(axgt_bonus_pct()),
    }
    for asset in ("ETH", "AXGT"):
        rec = _read_price(asset)
        if rec:
            price, ts = rec
            out[f"{asset.lower()}_usd"] = float(price)
            out[f"{asset.lower()}_price_age_seconds"] = round(time.time() - ts, 1)
        else:
            out[f"{asset.lower()}_usd"] = None
    return out
