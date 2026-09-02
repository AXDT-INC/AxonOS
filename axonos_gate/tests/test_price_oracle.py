import time
import unittest
from decimal import Decimal
from unittest import mock

from axonos_gate import price_oracle


def _word(value):
    return f"{value % (1 << 256):064x}"


def _observe_result(tick_past, tick_now):
    # ABI tuple offsets, then tickCumulatives array and an unused second array.
    return "0x" + "".join(
        _word(value)
        for value in (64, 160, 2, tick_past, tick_now, 2, 0, 0)
    )


class PriceOracleOnChainTests(unittest.TestCase):
    def test_axgt_quote_matches_uniswap_twap_times_chainlink(self):
        # Tick -69,081 is approximately 0.001 WETH/AXGT; multiply by $2,000/ETH.
        window = 1800
        mean_tick = -69081
        token0 = "0x" + ("0" * 24) + price_oracle._AXGT_TOKEN_CONTRACT[2:]
        observe = _observe_result(0, mean_tick * window)
        round_data = "0x" + "".join(
            _word(value)
            for value in (1, 2000 * 10 ** 8, int(time.time()), int(time.time()), 1)
        )

        with mock.patch.object(
            price_oracle,
            "_rpc_eth_call",
            side_effect=[token0, observe, round_data],
        ), mock.patch.dict("os.environ", {"AXGT_TWAP_WINDOW_SECONDS": str(window)}):
            quote = price_oracle._fetch_axgt_usd_price_onchain()

        self.assertIsNotNone(quote)
        expected = float(Decimal("1.0001") ** mean_tick * Decimal("2000"))
        self.assertAlmostEqual(float(quote), expected, places=10)

    def test_stale_chainlink_quote_is_rejected(self):
        token0 = "0x" + ("0" * 24) + price_oracle._AXGT_TOKEN_CONTRACT[2:]
        observe = _observe_result(0, 0)
        stale = int(time.time()) - price_oracle._CHAINLINK_MAX_STALENESS_SECONDS - 1
        round_data = "0x" + "".join(
            _word(value) for value in (1, 2000 * 10 ** 8, stale, stale, 1)
        )

        with mock.patch.object(
            price_oracle,
            "_rpc_eth_call",
            side_effect=[token0, observe, round_data],
        ):
            self.assertIsNone(price_oracle._fetch_axgt_usd_price_onchain())

    def test_spot_quote_matches_dashboard_math(self):
        sqrt_price_x96 = int(Decimal("0.001").sqrt() * Decimal(2 ** 96))
        token0 = "0x" + ("0" * 24) + price_oracle._AXGT_TOKEN_CONTRACT[2:]
        slot0 = "0x" + _word(sqrt_price_x96) + (_word(0) * 6)
        round_data = "0x" + "".join(
            _word(value)
            for value in (1, 2000 * 10 ** 8, int(time.time()), int(time.time()), 1)
        )

        with mock.patch.object(
            price_oracle,
            "_rpc_eth_call",
            side_effect=[token0, slot0, round_data],
        ):
            quote = price_oracle._fetch_axgt_usd_price_spot()

        self.assertIsNotNone(quote)
        self.assertAlmostEqual(float(quote), 2.0, places=10)

    def test_poll_prefers_onchain_axgt_and_keeps_coingecko_for_eth(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ethereum": {"usd": 2500},
            "axondao-governance-token-2": {"usd": 0.25},
        }

        with (
            mock.patch.object(
                price_oracle, "_fetch_axgt_usd_price_onchain", return_value=Decimal("0.42")
            ),
            mock.patch.object(price_oracle, "_read_price", return_value=None),
            mock.patch.object(price_oracle.requests, "get", return_value=response),
            mock.patch.object(price_oracle, "_store_price") as store,
        ):
            self.assertTrue(price_oracle.poll_prices())

        stored = {call.args[0]: call.args[1] for call in store.call_args_list}
        self.assertEqual(stored["ETH"], Decimal("2500"))
        self.assertEqual(stored["AXGT"], Decimal("0.42"))

    def test_large_twap_move_requires_independent_confirmation(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ethereum": {"usd": 2500},
            "axondao-governance-token-2": {"usd": 0.11},
        }

        with (
            mock.patch.object(
                price_oracle, "_fetch_axgt_usd_price_onchain", return_value=Decimal("0.50")
            ),
            mock.patch.object(
                price_oracle, "_read_price", return_value=(Decimal("0.10"), time.time())
            ),
            mock.patch.object(price_oracle.requests, "get", return_value=response),
            mock.patch.object(price_oracle, "_store_price") as store,
        ):
            self.assertTrue(price_oracle.poll_prices())  # ETH still updates.

        axgt_stores = [call for call in store.call_args_list if call.args[0] == "AXGT"]
        self.assertEqual(axgt_stores, [])

    def test_large_twap_move_is_accepted_when_coingecko_confirms(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ethereum": {"usd": 2500},
            "axondao-governance-token-2": {"usd": 0.48},
        }

        with (
            mock.patch.object(
                price_oracle, "_fetch_axgt_usd_price_onchain", return_value=Decimal("0.50")
            ),
            mock.patch.object(
                price_oracle, "_read_price", return_value=(Decimal("0.10"), time.time())
            ),
            mock.patch.object(price_oracle.requests, "get", return_value=response),
            mock.patch.object(price_oracle, "_store_price") as store,
        ):
            self.assertTrue(price_oracle.poll_prices())

        stored = {call.args[0]: call.args[1] for call in store.call_args_list}
        self.assertEqual(stored["AXGT"], Decimal("0.50"))

    def test_spot_fallback_is_accepted_only_when_coingecko_confirms(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ethereum": {"usd": 2500},
            "axondao-governance-token-2": {"usd": 0.0273},
        }

        with (
            mock.patch.object(
                price_oracle, "_fetch_axgt_usd_price_onchain", return_value=None
            ),
            mock.patch.object(
                price_oracle, "_fetch_axgt_usd_price_spot", return_value=Decimal("0.0272")
            ),
            mock.patch.object(
                price_oracle, "_read_price", return_value=(Decimal("0.0270"), time.time())
            ),
            mock.patch.object(price_oracle.requests, "get", return_value=response),
            mock.patch.object(price_oracle, "_store_price") as store,
        ):
            self.assertTrue(price_oracle.poll_prices())

        stored = {call.args[0]: call.args[1] for call in store.call_args_list}
        self.assertEqual(stored["AXGT"], Decimal("0.0272"))

    def test_spot_fallback_is_rejected_when_coingecko_disagrees(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ethereum": {"usd": 2500},
            "axondao-governance-token-2": {"usd": 0.10},
        }

        with (
            mock.patch.object(
                price_oracle, "_fetch_axgt_usd_price_onchain", return_value=None
            ),
            mock.patch.object(
                price_oracle, "_fetch_axgt_usd_price_spot", return_value=Decimal("0.50")
            ),
            mock.patch.object(price_oracle, "_read_price", return_value=None),
            mock.patch.object(price_oracle.requests, "get", return_value=response),
            mock.patch.object(price_oracle, "_store_price") as store,
        ):
            self.assertTrue(price_oracle.poll_prices())  # ETH still updates.

        axgt_stores = [call for call in store.call_args_list if call.args[0] == "AXGT"]
        self.assertEqual(axgt_stores, [])

    def test_default_refresh_interval_matches_dashboard(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(price_oracle.poll_interval_seconds(), 300)


if __name__ == "__main__":
    unittest.main()
