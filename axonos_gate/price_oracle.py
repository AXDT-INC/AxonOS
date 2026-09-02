"""
USD price oracle for ETH and AXGT.

Minutes are priced in USD ($1/hour by default). ETH and AXGT payment amounts are
derived from live USD prices refreshed every five minutes and cached in Postgres (shared across
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

# AXGT starts from the axondao.io dashboard's Uniswap v3 AXGT/WETH pool and
# Chainlink ETH/USD sources, but billing uses a time-weighted average rather
# than manipulable slot0 spot price. CoinGecko remains an independent fallback.
_DEFAULT_ETHEREUM_RPC_URL = "https://ethereum-rpc.publicnode.com"
_AXGT_UNISWAP_V3_POOL_DEFAULT = "0xf9c56A9CcC1398Bed3C519ef2F0B42CE52AaA440"
_AXGT_TOKEN_CONTRACT = "0x6112c3509a8a787df576028450febb3786a2274d"
_CHAINLINK_ETH_USD_FEED = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"
_CHAINLINK_MAX_STALENESS_SECONDS = 6 * 60 * 60
_TOKEN0_SELECTOR = "0x0dfe1681"
_SLOT0_SELECTOR = "0x3850c7bd"
_OBSERVE_SELECTOR = "0x883bdbfd"
_LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"
_DEFAULT_TWAP_WINDOW_SECONDS = 30 * 60
_DEFAULT_MAX_PRICE_DEVIATION_PERCENT = Decimal("25")

# CoinGecko free API fallback. ids verified live: ETH + current AXGT token.
_CG_URL = "https://api.coingecko.com/api/v3/simple/price"
_ETH_ID = "ethereum"
_AXGT_ID_DEFAULT = "axondao-governance-token-2"

_PRICE_TABLE = "axgt_price_cache"
_DEFAULT_USD_PER_HOUR = Decimal("1.0")
_DEFAULT_AXGT_BONUS_PCT = Decimal("25")
_DEFAULT_MAX_STALE_SECONDS = 24 * 3600
_DEFAULT_POLL_INTERVAL_SECONDS = 5 * 60

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


def _ethereum_rpc_url() -> str:
    # ETHEREUM_RPC_URL matches the dashboard. AXGT_RPC_URL is accepted because
    # it is already the Ethereum-mainnet RPC setting used throughout AxonOS.
    return (
        (os.getenv("ETHEREUM_RPC_URL") or "").strip()
        or (os.getenv("AXGT_RPC_URL") or "").strip()
        or _DEFAULT_ETHEREUM_RPC_URL
    )


def _axgt_uniswap_pool() -> str:
    return (
        (os.getenv("AXGT_UNISWAP_V3_POOL") or "").strip()
        or _AXGT_UNISWAP_V3_POOL_DEFAULT
    )


def _rpc_eth_call(to: str, data: str) -> str:
    response = requests.post(
        _ethereum_rpc_url(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        },
        timeout=15,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message") or "Ethereum RPC error")
    result = payload.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError("Ethereum RPC returned an invalid eth_call result")
    return result


def _abi_word(result: str, index: int) -> int:
    start = 2 + index * 64
    word = result[start:start + 64]
    if len(word) != 64:
        raise ValueError("Ethereum RPC result is shorter than expected")
    return int(word, 16)


def _abi_signed_word(result: str, index: int) -> int:
    value = _abi_word(result, index)
    return value - (1 << 256) if value >= (1 << 255) else value


def twap_window_seconds() -> int:
    raw = (os.getenv("AXGT_TWAP_WINDOW_SECONDS") or "").strip()
    try:
        value = int(raw)
        if value >= 60:
            return value
    except ValueError:
        pass
    return _DEFAULT_TWAP_WINDOW_SECONDS


def max_price_deviation_percent() -> Decimal:
    raw = (os.getenv("AXGT_MAX_PRICE_DEVIATION_PERCENT") or "").strip()
    try:
        value = Decimal(raw)
        if value > 0:
            return value
    except (InvalidOperation, ValueError):
        pass
    return _DEFAULT_MAX_PRICE_DEVIATION_PERCENT


def _observe_call_data(seconds_ago: int) -> str:
    # observe(uint32[]) ABI: dynamic-array offset, length, then [past, now].
    return (
        _OBSERVE_SELECTOR
        + f"{32:064x}"
        + f"{2:064x}"
        + f"{seconds_ago:064x}"
        + f"{0:064x}"
    )


def _mean_tick(observe_result: str, window_seconds: int) -> int:
    # observe() returns (int56[] tickCumulatives, uint160[] secondsPerLiquidity).
    # The first word points to the tick array; its first two values are past/now.
    tick_array_offset = _abi_word(observe_result, 0)
    array_word = tick_array_offset // 32
    if _abi_word(observe_result, array_word) < 2:
        raise ValueError("Uniswap observe returned fewer than two tick values")
    tick_past = _abi_signed_word(observe_result, array_word + 1)
    tick_now = _abi_signed_word(observe_result, array_word + 2)
    delta = tick_now - tick_past
    # Python // rounds toward negative infinity, matching the conservative
    # rounding required for negative arithmetic-mean ticks.
    return delta // window_seconds


def _fetch_axgt_usd_price_onchain() -> Optional[Decimal]:
    """AXGT/USD from a Uniswap v3 TWAP times Chainlink ETH/USD."""
    try:
        pool = _axgt_uniswap_pool()
        window = twap_window_seconds()
        token0_result = _rpc_eth_call(pool, _TOKEN0_SELECTOR)
        observe_result = _rpc_eth_call(pool, _observe_call_data(window))
        round_result = _rpc_eth_call(_CHAINLINK_ETH_USD_FEED, _LATEST_ROUND_DATA_SELECTOR)

        eth_usd = Decimal(_abi_word(round_result, 1)) / Decimal(10 ** 8)
        feed_updated_at = _abi_word(round_result, 3)
        if eth_usd <= 0:
            return None
        if int(time.time()) - feed_updated_at > _CHAINLINK_MAX_STALENESS_SECONDS:
            return None

        token1_per_token0 = Decimal("1.0001") ** _mean_tick(observe_result, window)
        if token1_per_token0 <= 0:
            return None

        token0 = "0x" + token0_result[-40:].lower()
        weth_per_axgt = (
            token1_per_token0
            if token0 == _AXGT_TOKEN_CONTRACT
            else Decimal(1) / token1_per_token0
        )
        usd = weth_per_axgt * eth_usd
        return usd if usd > 0 else None
    except Exception as exc:
        logger.warning("price_oracle: on-chain AXGT price failed: %s", exc)
        return None


def _fetch_axgt_usd_price_spot() -> Optional[Decimal]:
    """Dashboard-compatible slot0 quote; callers must independently confirm it."""
    try:
        pool = _axgt_uniswap_pool()
        token0_result = _rpc_eth_call(pool, _TOKEN0_SELECTOR)
        slot0_result = _rpc_eth_call(pool, _SLOT0_SELECTOR)
        round_result = _rpc_eth_call(_CHAINLINK_ETH_USD_FEED, _LATEST_ROUND_DATA_SELECTOR)

        eth_usd = Decimal(_abi_word(round_result, 1)) / Decimal(10 ** 8)
        feed_updated_at = _abi_word(round_result, 3)
        if eth_usd <= 0:
            return None
        if int(time.time()) - feed_updated_at > _CHAINLINK_MAX_STALENESS_SECONDS:
            return None

        sqrt_price_x96 = Decimal(_abi_word(slot0_result, 0))
        token1_per_token0 = (sqrt_price_x96 / Decimal(2 ** 96)) ** 2
        if token1_per_token0 <= 0:
            return None

        token0 = "0x" + token0_result[-40:].lower()
        weth_per_axgt = (
            token1_per_token0
            if token0 == _AXGT_TOKEN_CONTRACT
            else Decimal(1) / token1_per_token0
        )
        usd = weth_per_axgt * eth_usd
        return usd if usd > 0 else None
    except Exception as exc:
        logger.warning("price_oracle: on-chain AXGT spot price failed: %s", exc)
        return None


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


def _price_deviation_percent(candidate: Decimal, reference: Decimal) -> Decimal:
    if candidate <= 0 or reference <= 0:
        return Decimal("Infinity")
    return abs(candidate - reference) * Decimal("100") / reference


def poll_prices() -> bool:
    """Fetch live prices, applying TWAP and independent-source safety checks."""
    twap_axgt = _fetch_axgt_usd_price_onchain()
    spot_axgt = _fetch_axgt_usd_price_spot() if twap_axgt is None else None
    onchain_axgt = twap_axgt if twap_axgt is not None else spot_axgt
    using_guarded_spot = twap_axgt is None and spot_axgt is not None
    previous = _read_price("AXGT")
    previous_axgt = previous[0] if previous else None
    needs_confirmation = (
        using_guarded_spot
        or (
            onchain_axgt is not None
            and previous_axgt is not None
            and _price_deviation_percent(onchain_axgt, previous_axgt)
            > max_price_deviation_percent()
        )
    )
    axgt_id = _axgt_cg_id()
    now = time.time()
    ok = False

    try:
        coin_ids = (
            f"{_ETH_ID},{axgt_id}"
            if onchain_axgt is None or needs_confirmation
            else _ETH_ID
        )
        resp = requests.get(
            _CG_URL,
            params={"ids": coin_ids, "vs_currencies": "usd"},
            timeout=15,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("price_oracle: CoinGecko fallback poll failed: %s", exc)
        data = {}

    eth = (data.get(_ETH_ID) or {}).get("usd")
    axgt_fallback = (data.get(axgt_id) or {}).get("usd")
    axgt = None
    axgt_source = None
    try:
        if eth and Decimal(str(eth)) > 0:
            _store_price("ETH", Decimal(str(eth)), now)
            ok = True

        fallback_price = Decimal(str(axgt_fallback)) if axgt_fallback else None
        if fallback_price is not None and fallback_price <= 0:
            fallback_price = None

        if onchain_axgt is not None and not needs_confirmation:
            axgt = onchain_axgt
            axgt_source = "onchain-twap"
        elif onchain_axgt is not None and fallback_price is not None:
            cross_deviation = _price_deviation_percent(onchain_axgt, fallback_price)
            if cross_deviation <= max_price_deviation_percent():
                axgt = onchain_axgt
                axgt_source = (
                    "onchain-spot+coingecko-confirmed"
                    if using_guarded_spot
                    else "onchain-twap+coingecko-confirmed"
                )
            else:
                if using_guarded_spot:
                    logger.warning(
                        "price_oracle: rejected AXGT spot price; %.2f%% from CoinGecko",
                        float(cross_deviation),
                    )
                else:
                    logger.warning(
                        "price_oracle: rejected AXGT TWAP change; %.2f%% from cached and "
                        "%.2f%% from CoinGecko",
                        float(_price_deviation_percent(onchain_axgt, previous_axgt)),
                        float(cross_deviation),
                    )
        elif onchain_axgt is not None:
            logger.warning(
                "price_oracle: rejected AXGT %s price without CoinGecko confirmation",
                "spot" if using_guarded_spot else "TWAP",
            )
        elif onchain_axgt is None and fallback_price is not None:
            axgt = fallback_price
            axgt_source = "coingecko-fallback"

        if axgt is not None:
            _store_price("AXGT", axgt, now)
            ok = True
    except (InvalidOperation, ValueError) as exc:
        logger.warning("price_oracle: bad price payload: %s", exc)
        return False
    if ok:
        logger.info(
            "price_oracle: updated ETH=%s AXGT=%s USD (AXGT source=%s)",
            eth, axgt, axgt_source,
        )
    return ok


_last_poll_attempt = 0.0


def _maybe_refresh() -> None:
    """Lazy poller: refresh prices if the cache is older than the poll interval.

    Avoids needing a separate cron/supervisor job — any pricing call triggers a
    refresh at most once per interval. Throttled in-process so concurrent requests
    don't stampede the Ethereum RPC or fallback feed.
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
