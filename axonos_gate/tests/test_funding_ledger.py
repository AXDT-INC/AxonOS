"""Funding invariants, including opt-in PostgreSQL transactions and concurrency.

FUNDING_TEST_DB_URL must point to a dedicated local database named
axonos_funding_test*. Every run creates/removes its own isolated schema.
"""

import json
import math
import multiprocessing
import os
import threading
import unittest
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from decimal import Decimal, ROUND_DOWN
from unittest.mock import MagicMock, patch

from axonos_gate import deposit_ledger as dl, funding_ledger as fl, price_oracle

WALLET = "0x" + "1" * 40
OTHER_WALLET = "0x" + "2" * 40


def _worker_init(dsn, schema, barrier):
    global _WORKER_DSN, _WORKER_SCHEMA, _WORKER_BARRIER
    _WORKER_DSN, _WORKER_SCHEMA, _WORKER_BARRIER = dsn, schema, barrier


def _worker_connect():
    import psycopg2
    return psycopg2.connect(_WORKER_DSN, options="-c search_path=" + _WORKER_SCHEMA)


def _database_worker(task):
    _WORKER_BARRIER.wait(timeout=20)
    if task[0] == "initialize":
        conn = _worker_connect()
        try:
            dl._ensure_tables(conn)
        finally:
            conn.close()
        return True
    _, event_id, event_type, session = task
    with patch.object(dl, "init_once", return_value=True), \
            patch.object(dl, "_get_connection", side_effect=_worker_connect):
        return fl.process_checkout_event(event_id, event_type, session)


def _terminate_settlement(dsn, schema, session, after_commit):
    _worker_init(dsn, schema, None)
    with patch.object(dl, "init_once", return_value=True), \
            patch.object(dl, "_get_connection", side_effect=_worker_connect):
        if after_commit:
            fl.process_checkout_event("evt_process_exit", "checkout.session.completed", session)
        else:
            with patch.object(dl, "_ledger_write", side_effect=lambda *a, **kw: os._exit(29)):
                fl.process_checkout_event("evt_process_exit", "checkout.session.completed", session)
    os._exit(29)


def session_for(payment, suffix="1", **changes):
    session = {"id": "cs_test_" + suffix, "object": "checkout.session", "mode": "payment", "livemode": False,
               "amount_total": payment["amount_cents"], "amount_subtotal": payment["amount_cents"],
               "currency": "usd", "client_reference_id": payment["id"],
               "status": "complete", "payment_status": "paid", "payment_intent": "pi_" + suffix,
               "customer": "cus_" + suffix,
               "metadata": {"wallet_address": OTHER_WALLET, "axonos_funding_id": payment["id"]}}
    session.update(changes)
    return session


class FundingValidationTests(unittest.TestCase):
    def test_invalid_wallet_amount_and_credit_snapshot_fail_before_db(self):
        for wallet, amount, credits, rate in (
            ("bad", 5000, 3000, 60), (WALLET, True, 3000, 60),
            (WALLET, 0, 3000, 60), (WALLET, 5000, "NaN", 60),
            (WALLET, 5000, 9000, 60), (WALLET, 5000, 3000, "Infinity"),
        ):
            with self.subTest(wallet=wallet, amount=amount, credits=credits):
                with patch.object(dl, "init_once") as init, self.assertRaises(fl.PaymentMismatch):
                    fl.create_pending_card(wallet, amount, credits, rate, False)
                init.assert_not_called()

    def test_axgt_price_snapshot_records_exact_price_bonus_and_base_rate(self):
        snapshot = {}
        with patch.object(price_oracle, "get_usd_price", return_value=Decimal("0.42")) as get_price, \
                patch.object(price_oracle, "usd_per_minute", return_value=Decimal("0.05")), \
                patch.object(price_oracle, "axgt_bonus_pct", return_value=Decimal("25")):
            credits = price_oracle.minutes_for_axgt(Decimal("100"), pricing_snapshot=snapshot)
        self.assertEqual(credits, 1050)
        self.assertEqual(snapshot["token_usd_price"], "0.42")
        self.assertEqual(snapshot["usd_value"], "42.00")
        self.assertEqual(snapshot["credits_per_usd"], "2E+1")
        self.assertEqual(snapshot["axgt_bonus_percent"], "25")
        get_price.assert_called_once_with("AXGT")

    def test_crypto_record_contains_amount_rate_and_verifier_snapshot(self):
        cur = MagicMock()
        snapshot = {"source": "fixed_usdc_rate", "usd_value": "50", "credits_per_usd": "60",
                    "holder_discount_percent": "25", "holder_tier": {"tier_index": 4}}
        fl.record_crypto_on_cursor(cur, WALLET, "usdc", Decimal("50"), 4000,
                                   "0xabc", 123, 8453, snapshot)
        args = cur.execute.call_args.args[1]
        self.assertIn(Decimal("50"), args)
        stored = json.loads(args[10])
        self.assertEqual(stored["holder_discount_percent"], "25")
        self.assertEqual(stored["effective_credits_per_token"], "80")

    def test_storage_error_rolls_back_and_redacts_exception(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("sensitive database connection detail")
        with patch.object(dl, "init_once", return_value=True), patch.object(dl, "_get_connection", return_value=conn):
            with self.assertRaises(fl.FundingUnavailable) as error:
                fl.get_card_payment("id", WALLET)
        self.assertNotIn("sensitive", str(error.exception))
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()


@unittest.skipUnless(os.getenv("FUNDING_TEST_DB_URL"), "dedicated FUNDING_TEST_DB_URL not configured")
class FundingPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        from psycopg2.extensions import parse_dsn
        cls.dsn = os.environ["FUNDING_TEST_DB_URL"]
        parsed = parse_dsn(cls.dsn)
        if (not parsed.get("dbname", "").startswith("axonos_funding_test")
                or not (parsed.get("host") in ("localhost", "127.0.0.1") or parsed.get("host", "").startswith("/tmp/"))
                or cls.dsn == os.getenv("AXGT_CHALLENGE_DB_URL")):
            raise RuntimeError("Funding tests require an isolated local axonos_funding_test* database")
        cls.schema = "funding_test_" + uuid.uuid4().hex
        with psycopg2.connect(cls.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA " + cls.schema)
        conn = cls.connect()
        try:
            dl._ensure_tables(conn)
        finally:
            conn.close()

    @classmethod
    def connect(cls):
        import psycopg2
        return psycopg2.connect(cls.dsn, options="-c search_path=" + cls.schema)

    @classmethod
    def tearDownClass(cls):
        import psycopg2
        with psycopg2.connect(cls.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("DROP SCHEMA " + cls.schema + " CASCADE")

    def setUp(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE axonos_funding_events, axonos_funding_transactions, axgt_ledger, axgt_deposits, axgt_verified_deposits")
        self.init_patch = patch.object(dl, "init_once", return_value=True)
        self.conn_patch = patch.object(dl, "_get_connection", side_effect=self.connect)
        self.init_patch.start()
        self.conn_patch.start()
        self.addCleanup(self.init_patch.stop)
        self.addCleanup(self.conn_patch.stop)

    def purchase(self, suffix="1", **changes):
        payment = fl.create_pending_card(WALLET, 5000, Decimal("3000"), Decimal("60"), False)
        session = session_for(payment, suffix, **changes)
        fl.bind_checkout(payment["id"], session)
        return payment, session

    def scalar(self, query, args=()):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, args)
                return cur.fetchone()[0]

    def test_success_credits_only_persisted_wallet_and_status_is_scoped(self):
        payment, session = self.purchase()
        self.assertEqual(fl.lookup_card_binding(session["id"]), payment["id"])
        self.assertIsNone(fl.lookup_card_binding("cs_not_ours"))
        result = fl.process_checkout_event("evt_1", "checkout.session.completed", session)
        self.assertTrue(result["credited"])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)
        self.assertEqual(dl.get_remaining_minutes(OTHER_WALLET), 0)
        self.assertIsNone(fl.get_card_payment(payment["id"], OTHER_WALLET))
        self.assertTrue(fl.get_card_payment(payment["id"], WALLET)["credited"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axgt_ledger"), 1)

    def test_session_metadata_never_replaces_database_binding_or_wallet(self):
        payment, session = self.purchase()
        session["metadata"] = {"axonos_funding_id": "some-other-order", "wallet_address": OTHER_WALLET}
        self.assertEqual(fl.lookup_card_binding(session["id"]), payment["id"])
        self.assertTrue(fl.process_checkout_event("evt_metadata", "checkout.session.completed", session)["credited"])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)
        self.assertEqual(dl.get_remaining_minutes(OTHER_WALLET), 0)

    def test_duplicate_and_concurrent_distinct_events_credit_once(self):
        _, session = self.purchase()
        barrier = threading.Barrier(8)
        def process(i):
            barrier.wait(timeout=10)
            # Same event replay and different events for the same Session.
            event_id = "evt_same" if i < 4 else "evt_other_" + str(i)
            return fl.process_checkout_event(event_id, "checkout.session.completed", session)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(process, range(8)))
        self.assertEqual(sum(result["credited"] for result in results), 1)
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axgt_ledger"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_events"), 5)

    def run_processes(self, schema, tasks):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(len(tasks))
        with ProcessPoolExecutor(max_workers=len(tasks), mp_context=context,
                                 initializer=_worker_init, initargs=(self.dsn, schema, barrier)) as pool:
            return list(pool.map(_database_worker, tasks))

    def test_separate_processes_same_and_different_success_events_credit_once(self):
        _, session = self.purchase()
        results = self.run_processes(self.schema, [
            ("settle", "evt_process_same", "checkout.session.completed", session),
            ("settle", "evt_process_same", "checkout.session.completed", session),
            ("settle", "evt_process_async", "checkout.session.async_payment_succeeded", session),
            ("settle", "evt_process_other", "checkout.session.completed", session),
        ])
        self.assertEqual(sum(result["credited"] for result in results), 1)
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axgt_ledger"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_events"), 3)

    def test_concurrent_process_schema_initialization_is_serialized(self):
        schema = "funding_bootstrap_" + uuid.uuid4().hex
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA " + schema)
        try:
            self.assertEqual(self.run_processes(schema, [("initialize",)] * 4), [True] * 4)
        finally:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DROP SCHEMA " + schema + " CASCADE")

    def test_initialization_failure_rolls_back_schema_and_retry_succeeds(self):
        import psycopg2
        schema = "funding_migration_" + uuid.uuid4().hex
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA " + schema)
        ensure_funding = fl.ensure_tables
        def fail_after_funding(cur):
            ensure_funding(cur)
            raise RuntimeError("injected failure after schema and backfill")
        try:
            conn = psycopg2.connect(self.dsn, options="-c search_path=" + schema)
            try:
                with patch.object(fl, "ensure_tables", side_effect=fail_after_funding):
                    with self.assertRaises(RuntimeError):
                        dl._ensure_tables(conn)
            finally:
                conn.close()  # The initializer's failure path closes/rolls back.
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM pg_tables WHERE schemaname=%s", (schema,))
                    self.assertEqual(cur.fetchone()[0], 0)
            self.assertEqual(self.run_processes(schema, [("initialize",)] * 2), [True] * 2)
        finally:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DROP SCHEMA " + schema + " CASCADE")

    def test_process_termination_before_commit_rolls_back_and_after_commit_replays_safely(self):
        context = multiprocessing.get_context("spawn")
        payment, session = self.purchase()
        for after_commit in (False, True):
            with self.subTest(after_commit=after_commit):
                process = context.Process(target=_terminate_settlement,
                                          args=(self.dsn, self.schema, session, after_commit))
                process.start()
                process.join(timeout=30)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("Settlement subprocess did not terminate")
                self.assertEqual(process.exitcode, 29)
                self.assertEqual(dl.get_remaining_minutes(WALLET), 3000 if after_commit else 0)
                self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_events"), int(after_commit))
                self.assertEqual(fl.get_card_payment(payment["id"], WALLET)["credited"], after_commit)
        replay = fl.process_checkout_event("evt_process_exit", "checkout.session.completed", session)
        self.assertFalse(replay["credited"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axgt_ledger"), 1)

    def test_commit_failure_rolls_back_all_completed_writes_then_replay_succeeds(self):
        payment, session = self.purchase()
        real_connection = self.connect()
        connection = MagicMock(wraps=real_connection)
        connection.commit.side_effect = RuntimeError("injected commit failure")
        with patch.object(dl, "_get_connection", return_value=connection):
            with self.assertRaises(fl.FundingUnavailable):
                fl.process_checkout_event("evt_commit_retry", "checkout.session.completed", session)
        connection.rollback.assert_called_once()
        self.assertEqual(dl.get_remaining_minutes(WALLET), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_events"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axgt_ledger"), 0)
        self.assertFalse(fl.get_card_payment(payment["id"], WALLET)["credited"])
        self.assertTrue(fl.process_checkout_event("evt_commit_retry", "checkout.session.completed", session)["credited"])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)

    def test_purchase_rate_is_frozen_and_float_conversion_never_inflates_quote(self):
        from axonos_gate import stripe_payments as sp
        for suffix, amount_cents, rate in (
            ("round_small", 100, Decimal("0.3")),
            ("round_extreme", 99999999, Decimal("999999999.9999999999")),
        ):
            expected = (Decimal(amount_cents) / 100 * rate).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            payment = fl.create_pending_card(WALLET, amount_cents, expected, rate, False)
            session = session_for(payment, suffix)
            fl.bind_checkout(payment["id"], session)
            with patch.object(sp, "_rate", side_effect=AssertionError("Settlement must not re-price purchase")), \
                    patch.dict(os.environ, {"AXGT_USD_PER_HOUR": "99999", "AXGT_BONUS_PCT": "100"}):
                result = fl.process_checkout_event("evt_" + suffix, "checkout.session.completed", session)
            actual = Decimal.from_float(result["credits_added"])
            self.assertLessEqual(actual, expected)
            self.assertLessEqual(expected - actual, Decimal.from_float(math.ulp(float(expected))))
            self.assertEqual(self.scalar("SELECT expected_credits FROM axonos_funding_transactions WHERE id=%s", (payment["id"],)), expected)
            self.assertEqual(self.scalar("SELECT credits_added FROM axonos_funding_transactions WHERE id=%s", (payment["id"],)), actual)
            self.assertEqual(self.scalar("SELECT credits_per_usd FROM axonos_funding_transactions WHERE id=%s", (payment["id"],)), rate)

    def test_invalid_provider_data_never_mutates_balance(self):
        _, session = self.purchase()
        for changes in ({"amount_total": 4999}, {"amount_subtotal": 4999}, {"currency": "eur"},
                        {"client_reference_id": "arbitrary"}, {"livemode": True},
                        {"mode": "subscription"}, {"payment_intent": "pi_other"}, {"payment_intent": None}):
            with self.subTest(changes=changes), self.assertRaises(fl.PaymentMismatch):
                fl.process_checkout_event("evt_invalid", "checkout.session.completed", dict(session, **changes))
        self.assertEqual(dl.get_remaining_minutes(WALLET), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_events"), 0)

    def test_unpaid_failed_expired_never_credit_and_late_failure_cannot_erase_success(self):
        _, session = self.purchase(payment_status="unpaid", status="open")
        for i, event_type in enumerate(("checkout.session.completed", "checkout.session.async_payment_failed", "checkout.session.expired")):
            self.assertFalse(fl.process_checkout_event("evt_unpaid_" + str(i), event_type, session)["credited"])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 0)
        session.update(payment_status="paid", status="complete")
        self.assertTrue(fl.process_checkout_event("evt_paid", "checkout.session.completed", session)["credited"])
        result = fl.process_checkout_event("evt_late_failure", "checkout.session.async_payment_failed", session)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)

    def test_credit_write_failure_rolls_back_event_balance_and_status_then_retry_succeeds(self):
        payment, session = self.purchase()
        with patch.object(dl, "_ledger_write", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(fl.FundingUnavailable):
                fl.process_checkout_event("evt_retry", "checkout.session.completed", session)
        self.assertEqual(dl.get_remaining_minutes(WALLET), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_events"), 0)
        self.assertFalse(fl.get_card_payment(payment["id"], WALLET)["credited"])
        self.assertTrue(fl.process_checkout_event("evt_retry", "checkout.session.completed", session)["credited"])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)

    def test_session_and_intent_unique_even_across_orders(self):
        payment, session = self.purchase()
        payment2 = fl.create_pending_card(OTHER_WALLET, 5000, 3000, 60, False)
        for changes in ({"client_reference_id": payment2["id"]},
                        {"client_reference_id": payment2["id"], "id": "cs_different"}):
            with self.assertRaises(fl.FundingUnavailable):
                fl.bind_checkout(payment2["id"], dict(session, **changes))
        self.assertEqual(dl.get_remaining_minutes(WALLET), 0)
        with self.assertRaises(fl.PaymentMismatch):
            fl.bind_checkout(payment["id"], dict(session, id="cs_replacement", payment_intent="pi_replacement"))
        fl.process_checkout_event("evt_still_bound", "checkout.session.completed", session)
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)
        self.assertEqual(dl.get_remaining_minutes(OTHER_WALLET), 0)
        self.assertFalse(fl.get_card_payment(payment2["id"], OTHER_WALLET)["credited"])

    def test_event_identifier_cannot_be_rebound_to_another_payment(self):
        _, first = self.purchase("first")
        second_payment = fl.create_pending_card(OTHER_WALLET, 5000, 3000, 60, False)
        second = session_for(second_payment, "second")
        fl.bind_checkout(second_payment["id"], second)
        self.assertTrue(fl.process_checkout_event("evt_unique", "checkout.session.completed", first)["credited"])
        with self.assertRaises(fl.PaymentMismatch):
            fl.process_checkout_event("evt_unique", "checkout.session.completed", second)
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)
        self.assertEqual(dl.get_remaining_minutes(OTHER_WALLET), 0)
        self.assertFalse(fl.get_card_payment(second_payment["id"], OTHER_WALLET)["credited"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_events"), 1)

    def test_refunds_are_monotonic_audits_and_never_remove_spent_credits(self):
        payment, session = self.purchase()
        fl.process_checkout_event("evt_paid", "checkout.session.completed", session)
        dl.deduct_usage(WALLET, 2900)
        refund = {"id": "ch_1", "payment_intent": "pi_1", "currency": "usd", "amount": 5000, "amount_refunded": 5000}
        fl.record_reversal("evt_refund", "charge.refunded", refund, False)
        fl.record_reversal("evt_older_partial", "charge.refunded", dict(refund, amount_refunded=1000), False)
        fl.record_reversal("evt_refund", "charge.refunded", refund, False)
        self.assertEqual(dl.get_remaining_minutes(WALLET), 100)
        status = fl.get_card_payment(payment["id"], WALLET)
        self.assertTrue(status["reconciliation_required"])
        self.assertEqual(status["status"], "refunded")
        self.assertEqual(self.scalar("SELECT refunded_amount FROM axonos_funding_transactions"), Decimal("50"))

    def test_reversal_before_completion_holds_credits_for_manual_review(self):
        payment, session = self.purchase()
        dispute = {"id": "dp_1", "payment_intent": "pi_1", "currency": "usd", "amount": 5000, "status": "needs_response"}
        fl.record_reversal("evt_dispute", "charge.dispute.created", dispute, False)
        result = fl.process_checkout_event("evt_paid", "checkout.session.completed", session)
        self.assertFalse(result["credited"])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 0)
        self.assertTrue(fl.get_card_payment(payment["id"], WALLET)["reconciliation_required"])

    def test_crypto_all_rails_share_balance_and_record_actual_amount_and_snapshot(self):
        for i, (rail, function, amount) in enumerate((
            ("axgt", dl.credit_deposit, Decimal("100")),
            ("eth", dl.credit_eth_deposit, Decimal("0.002")),
            ("usdc", dl.credit_usdc_deposit, Decimal("5")),
        )):
            with self.subTest(rail=rail):
                snapshot = {"source": "test_verifier", "axgt_bonus_percent": "25" if rail == "axgt" else "0"}
                ok, _, error = function(WALLET, amount, 300, "0x" + str(i) * 64, 123, 1, pricing_snapshot=snapshot)
                self.assertTrue(ok, error)
                self.assertEqual(self.scalar("SELECT crypto_amount FROM axonos_funding_transactions WHERE payment_method=%s", (rail,)), amount)
        self.assertEqual(dl.get_remaining_minutes(WALLET), 900)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_transactions"), 3)
        # A second schema/bootstrap pass neither duplicates nor rewrites snapshots.
        conn = self.connect()
        try:
            dl._ensure_tables(conn)
        finally:
            conn.close()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_transactions"), 3)
        self.assertEqual(self.scalar("SELECT pricing_snapshot->>'source' FROM axonos_funding_transactions LIMIT 1"), "test_verifier")

    def test_unknown_session_is_retryable_and_never_uses_metadata_for_recipient(self):
        payment = fl.create_pending_card(WALLET, 5000, 3000, 60, False)
        session = session_for(payment)
        with self.assertRaises(fl.FundingUnavailable):
            fl.process_checkout_event("evt_unbound", "checkout.session.completed", session)
        self.assertEqual(dl.get_remaining_minutes(WALLET), 0)
        # Recovery requires the provider's separately retrieved matching Session.
        fl.bind_checkout(payment["id"], session)
        self.assertTrue(fl.process_checkout_event("evt_unbound", "checkout.session.completed", session)["credited"])

    def test_crypto_replay_and_funding_insert_failure_cannot_partially_credit(self):
        args = (WALLET, Decimal("5"), 300, "0x" + "a" * 64, 123, 1)
        with patch.object(fl, "record_crypto_on_cursor", side_effect=RuntimeError("injected audit failure")):
            self.assertFalse(dl.credit_usdc_deposit(*args)[0])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axgt_verified_deposits"), 0)
        self.assertTrue(dl.credit_usdc_deposit(*args)[0])
        self.assertFalse(dl.credit_usdc_deposit(*args)[0])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 300)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_transactions"), 1)

    def test_legacy_backfill_preserves_known_amount_and_does_not_invent_rates(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                for i, (rail, source, amount) in enumerate((
                    ("axgt", "onchain", 100), ("usdc", "onchain", 0), ("eth", "test_credit", 0)
                )):
                    cur.execute("""INSERT INTO axgt_verified_deposits
                        (tx_hash, wallet_address, sender_wallet, recipient_wallet, axgt_amount,
                         credited_minutes, block_number, credit_source, payment_rail, created_at)
                        VALUES (%s, %s, %s, %s, %s, 300, 123, %s, %s, 100)""",
                        ("0x" + str(i) * 64, WALLET, WALLET, OTHER_WALLET, amount, source, rail))
        conn = self.connect()
        try:
            dl._ensure_tables(conn)
        finally:
            conn.close()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_transactions"), 2)
        self.assertEqual(self.scalar("SELECT crypto_amount FROM axonos_funding_transactions WHERE payment_method='axgt'"), 100)
        self.assertIsNone(self.scalar("SELECT crypto_amount FROM axonos_funding_transactions WHERE payment_method='usdc'"))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_transactions WHERE credits_per_usd IS NULL"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_transactions WHERE pricing_snapshot->>'historical_pricing_unavailable'='true'"), 2)

    def test_old_crypto_writer_coexists_but_requires_later_backfill_for_funding_history(self):
        args = (WALLET, Decimal("5"), 300, "0x" + "b" * 64, 123, 1)
        # Prior writers execute these same accounting statements without funding.
        with patch.object(fl, "record_crypto_on_cursor", return_value=None):
            self.assertTrue(dl.credit_usdc_deposit(*args)[0])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 300)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_transactions"), 0)
        conn = self.connect()
        try:
            dl._ensure_tables(conn)
        finally:
            conn.close()
        self.assertEqual(dl.get_remaining_minutes(WALLET), 300)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM axonos_funding_transactions"), 1)
        self.assertEqual(self.scalar("SELECT pricing_snapshot->>'source' FROM axonos_funding_transactions"), "legacy_backfill")
        self.assertIsNone(self.scalar("SELECT crypto_amount FROM axonos_funding_transactions"))
        self.assertFalse(dl.credit_usdc_deposit(*args)[0])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 300)

    def test_provider_checkout_then_webhook_uses_real_atomic_ledger(self):
        from axonos_gate import stripe_payments as sp
        client = MagicMock()
        created = {}
        def create(params, options):
            self.assertEqual(options["idempotency_key"], "axonos-card-" + params["client_reference_id"])
            created.update(session_for({"id": params["client_reference_id"], "amount_cents": 5000},
                                       payment_status="unpaid", status="open", payment_intent=None,
                                       url="https://checkout.stripe.com/c/pay/test"))
            return created
        client.v1.checkout.sessions.create.side_effect = create
        with patch.object(sp, "_credentials", return_value=("unused", "unused", False)), \
                patch.object(sp, "_public_base_url", return_value="https://example.test"), \
                patch.object(sp, "_client", return_value=client), \
                patch.object(sp, "_rate", return_value=Decimal("60")), \
                patch.dict(os.environ, {"AXGT_USER_CONTAINER_ENABLED": "true", "AXGT_SESSION_ID": "",
                                       "AXGT_CARD_GATE_ONLY": "true", "AXGT_DESKTOP_ENABLED": "false", "AXGT_SSH_ENABLED": "false"}):
            checkout = sp.create_checkout(WALLET, "50.00")
        self.assertEqual(dl.get_remaining_minutes(WALLET), 0)
        self.assertEqual(checkout["payment_id"], created["client_reference_id"])
        paid = self._expanded_paid_session(dict(created, payment_status="paid", status="complete"))
        event = {"id": "evt_provider", "type": "checkout.session.completed", "livemode": False,
                 "data": {"object": {"object": "checkout.session", "id": paid["id"]}}}
        with patch.object(sp, "verify_webhook", return_value=event), patch.object(sp, "_retrieve_session", return_value=paid):
            self.assertTrue(sp.handle_webhook(b"signed body verified in provider unit tests", "signature")["credited"])
            self.assertFalse(sp.handle_webhook(b"same", "signature")["credited"])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)

    @staticmethod
    def _expanded_paid_session(session, refunded=0):
        charge = {"id": "ch_1", "object": "charge", "livemode": False, "paid": True,
                  "captured": True, "amount_captured": 5000, "payment_intent": "pi_1",
                  "currency": "usd", "amount": 5000, "amount_refunded": refunded,
                  "payment_method_details": {"type": "card"}}
        intent = {"id": "pi_1", "object": "payment_intent", "status": "succeeded", "currency": "usd", "amount": 5000,
                  "amount_received": 5000, "livemode": False, "latest_charge": charge}
        return dict(session, payment_intent=intent)

    def test_webhook_after_auth_expiry_revocation_or_wallet_switch_credits_saved_recipient(self):
        import gate_server as gate
        from axonos_gate import stripe_payments as sp

        payment = fl.create_pending_card(WALLET, 5000, 3000, 60, False)
        session = self._expanded_paid_session(session_for(payment))
        fl.bind_checkout(payment["id"], session)
        event = {"id": "evt_changed_auth", "type": "checkout.session.completed", "livemode": False,
                 "data": {"object": {"object": "checkout.session", "id": session["id"]}}}
        # A webhook is independent of the browser's later login state. Even a
        # different current wallet cannot replace the saved funding recipient.
        for state, wallet in (("expired", None), ("revoked", None), ("switched", OTHER_WALLET)):
            with self.subTest(state=state), patch.object(gate, "stripe_payments", sp), \
                    patch.object(gate, "_card_authenticated_wallet", return_value=(wallet, None)) as auth, \
                    patch.object(sp, "verify_webhook", return_value=event), \
                    patch.object(sp, "_retrieve_session", return_value=session):
                response = gate.app.test_client().post("/api/payments/stripe/webhook", data=b"verified event")
                self.assertEqual(response.status_code, 200)
                auth.assert_not_called()
            self.assertEqual(dl.get_remaining_minutes(WALLET), 3000)
            self.assertEqual(dl.get_remaining_minutes(OTHER_WALLET), 0)
            self.assertIsNone(fl.get_card_payment(payment["id"], OTHER_WALLET))

    def test_provider_recovers_unbound_session_but_holds_already_refunded_payment(self):
        from axonos_gate import stripe_payments as sp
        payment = fl.create_pending_card(WALLET, 5000, 3000, 60, False)
        session = self._expanded_paid_session(session_for(payment), refunded=5000)
        event = {"id": "evt_recovery", "type": "checkout.session.completed", "livemode": False,
                 "data": {"object": {"object": "checkout.session", "id": session["id"]}}}
        with patch.object(sp, "verify_webhook", return_value=event), patch.object(sp, "_retrieve_session", return_value=session):
            result = sp.handle_webhook(b"verified body", "signature")
        self.assertFalse(result["credited"])
        self.assertEqual(dl.get_remaining_minutes(WALLET), 0)
        self.assertTrue(fl.get_card_payment(payment["id"], WALLET)["reconciliation_required"])
