"""CARD boundaries with real SDK signature verification, no external API calls."""

import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gate_server as gate
import stripe_payments as card

WALLET = "0x" + "a" * 40
PAYMENT = "f0f80cc5-659a-4a9d-988b-bd7c49f621cc"
ENV = {
    "STRIPE_SECRET_KEY": "sk_test_unit_test_placeholder",
    "STRIPE_WEBHOOK_SECRET": "whsec_unit_test_placeholder",
    "AXGT_CHALLENGE_DB_URL": "postgresql://unused-test-database",
    "AXGT_PUBLIC_BASE_URL": "https://app.example.test",
    "AXGT_USD_PER_HOUR": "1", "AXGT_USD_BONUS_PERCENT": "25",
    "STRIPE_MIN_AMOUNT_USD": "1", "STRIPE_MAX_AMOUNT_USD": "1000",
    "AXGT_USER_CONTAINER_ENABLED": "true", "AXGT_SESSION_ID": "",
    "AXGT_CARD_GATE_ONLY": "true", "AXGT_DESKTOP_ENABLED": "false", "AXGT_SSH_ENABLED": "false",
}


def checkout(paid=False):
    return {"id": "cs_test_example", "object": "checkout.session", "mode": "payment",
            "amount_total": 5000, "amount_subtotal": 5000, "currency": "usd",
            "client_reference_id": PAYMENT, "livemode": False,
            "metadata": {"axonos_funding_id": PAYMENT},
            "status": "complete" if paid else "open",
            "payment_status": "paid" if paid else "unpaid",
            "customer": "cus_example", "invoice": None,
            "url": "https://checkout.stripe.com/c/pay/cs_test_example",
            "payment_intent": {"id": "pi_example", "object": "payment_intent", "status": "succeeded", "currency": "usd",
                "amount": 5000, "amount_received": 5000, "livemode": False,
                "latest_charge": {"id": "ch_example", "object": "charge", "paid": True,
                    "captured": True, "currency": "usd", "amount": 5000, "amount_captured": 5000,
                    "amount_refunded": 0, "livemode": False,
                    "payment_intent": "pi_example", "disputed": False,
                    "payment_method_details": {"type": "card"}}} if paid else None}


def signed_event(kind="checkout.session.completed", obj=None, stamp=None, **extra):
    event = {"id": "evt_example", "object": "event", "type": kind, "livemode": False,
             "data": {"object": obj or checkout(True)}, **extra}
    payload = json.dumps(event).encode()
    stamp = int(time.time()) if stamp is None else stamp
    signature = hmac.new(ENV["STRIPE_WEBHOOK_SECRET"].encode(),
                         str(stamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
    return payload, f"t={stamp},v1={signature}"


@unittest.skipIf(card.stripe is None, "Stripe SDK not installed")
class StripePaymentTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        binding = patch.object(card.funding_ledger, "lookup_card_binding", return_value=None)
        binding.start()
        self.addCleanup(binding.stop)

    def test_config_disabled_without_credentials_and_does_not_expose_secrets(self):
        cfg = card.public_config()
        self.assertTrue(cfg["card_payments_enabled"])
        self.assertEqual(Decimal(cfg["card_credits_per_usd"]), 60)
        self.assertNotIn(ENV["STRIPE_SECRET_KEY"], json.dumps(cfg))
        for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "AXGT_PUBLIC_BASE_URL", "AXGT_CHALLENGE_DB_URL"):
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                self.assertEqual(card.public_config(), {"card_payments_enabled": False})
        with patch.dict(os.environ, {"AXGT_PUBLIC_BASE_URL": "https://user:secret@example.com"}):
            self.assertFalse(card.public_config()["card_payments_enabled"])
        for values in ({"AXGT_USER_CONTAINER_ENABLED": "false"}, {"AXGT_SESSION_ID": "tenant-session"},
                       {"AXGT_CARD_GATE_ONLY": "false"}, {"AXGT_DESKTOP_ENABLED": "true"},
                       {"AXGT_SSH_ENABLED": "true"}, {"AXGT_CARD_GATE_ONLY": ""}):
            with patch.dict(os.environ, values):
                self.assertFalse(card.public_config()["card_payments_enabled"])

    def test_sdk_debug_cannot_log_payment_request_or_response_details(self):
        with patch.object(card.stripe, "log", "debug"), patch.object(card.stripe._util, "STRIPE_LOG", "debug"), patch("sys.stderr") as stderr:
            card._client()
            card.stripe._util.log_debug("provider-detail", secret=ENV["STRIPE_SECRET_KEY"])
            stderr.write.assert_not_called()

    def test_normal_usd_quote_uses_compute_rate_without_axgt_bonus(self):
        self.assertEqual(card.quote("50.00"), (5000, Decimal(3000), Decimal(60)))
        with patch.dict(os.environ, {"AXGT_USD_PER_HOUR": "2", "AXGT_USD_BONUS_PERCENT": "90"}):
            self.assertEqual(card.quote("50.00"), (5000, Decimal(1500), Decimal(30)))
        with patch.dict(os.environ, {"AXGT_USD_PER_HOUR": "7"}):
            self.assertLessEqual(card.quote("1.01")[1], Decimal("1.01") * 60 / 7)

    def test_invalid_amounts(self):
        for value in (None, True, {}, [], "", "NaN", "Infinity", "1e2", "-1", "0", "0.99", "1000.01", "50.001", " 50", "50 ", float("nan")):
            with self.subTest(value=value), self.assertRaises(card.InvalidAmount):
                card.quote(value)

    def test_invalid_configuration_disables_card(self):
        for values in ({"STRIPE_MIN_AMOUNT_USD": "NaN"}, {"STRIPE_MAX_AMOUNT_USD": "0"},
                       {"STRIPE_MIN_AMOUNT_USD": "0.501"}, {"AXGT_USD_PER_HOUR": "Infinity"}):
            with self.subTest(values=values), patch.dict(os.environ, values):
                self.assertFalse(card.public_config()["card_payments_enabled"])

    def test_checkout_persists_authenticated_recipient_and_frozen_quote_before_provider(self):
        client = MagicMock()
        client.v1.checkout.sessions.create.return_value = checkout()
        with patch.object(card, "_client", return_value=client), patch.object(card.funding_ledger, "create_pending_card", return_value={"id": PAYMENT}) as create, patch.object(card.funding_ledger, "bind_checkout") as bind:
            result = card.create_checkout(WALLET, "50.00")
        create.assert_called_once_with(WALLET, 5000, Decimal(3000), Decimal(60), False)
        params, options = client.v1.checkout.sessions.create.call_args.args
        self.assertEqual(params["client_reference_id"], PAYMENT)
        self.assertEqual(params["payment_method_types"], ["card"])
        self.assertEqual(params["line_items"][0]["price_data"]["unit_amount"], 5000)
        self.assertNotIn("allow_promotion_codes", params)
        self.assertIn("idempotency_key", options)
        self.assertTrue(params["success_url"].startswith(ENV["AXGT_PUBLIC_BASE_URL"]))
        bind.assert_called_once_with(PAYMENT, checkout())
        self.assertEqual(set(result), {"checkout_url", "payment_id"})

    def test_provider_failure_does_not_disclose_secret(self):
        client = MagicMock()
        client.v1.checkout.sessions.create.side_effect = RuntimeError(ENV["STRIPE_SECRET_KEY"])
        with patch.object(card, "_client", return_value=client), patch.object(card.funding_ledger, "create_pending_card", return_value={"id": PAYMENT}), self.assertRaises(card.CardUnavailable) as caught:
            card.create_checkout(WALLET, "50.00")
        self.assertNotIn(ENV["STRIPE_SECRET_KEY"], str(caught.exception))

    def test_real_signature_verification_rejects_tampering_wrong_secret_stale_future_wrong_mode(self):
        payload, signature = signed_event()
        self.assertEqual(card.verify_webhook(payload, signature)["id"], "evt_example")
        for body, sig in ((payload + b" ", signature), (payload, ""), (payload, signature + "bad"),
                          signed_event(stamp=int(time.time()) - 301), signed_event(stamp=int(time.time()) + 301),
                          signed_event(livemode=True)):
            with self.subTest(sig=sig), self.assertRaises(card.InvalidWebhook):
                card.verify_webhook(body, sig)
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_wrong"}), self.assertRaises(card.InvalidWebhook):
            card.verify_webhook(payload, signature)

    def test_verified_paid_webhook_retrieves_provider_session_and_delegates_one_settlement(self):
        session = checkout(True)
        payload, signature = signed_event()
        with patch.object(card, "_retrieve_session", return_value=session) as retrieve, patch.object(card.funding_ledger, "bind_checkout") as bind, patch.object(card.funding_ledger, "process_checkout_event", return_value={"credited": True}) as settle:
            self.assertTrue(card.handle_webhook(payload, signature)["credited"])
        retrieve.assert_called_once_with("cs_test_example")
        bind.assert_called_once_with(PAYMENT, session)
        settle.assert_called_once_with("evt_example", "checkout.session.completed", session)

    def test_invalid_paid_intent_or_charge_never_reaches_credit_issuance(self):
        for change in (lambda s: s["payment_intent"].update(amount_received=100),
                       lambda s: s["payment_intent"].update(object="customer"),
                       lambda s: s["payment_intent"].update(status="processing"),
                       lambda s: s["payment_intent"]["latest_charge"].update(captured=False),
                       lambda s: s["payment_intent"]["latest_charge"].update(amount_captured=100),
                       lambda s: s["payment_intent"]["latest_charge"].update(livemode=True),
                       lambda s: s["payment_intent"]["latest_charge"].update(payment_intent="pi_someone_else"),
                       lambda s: s["payment_intent"]["latest_charge"]["payment_method_details"].update(type="us_bank_account")):
            session = checkout(True)
            change(session)
            with patch.object(card, "_retrieve_session", return_value=session), patch.object(card.funding_ledger, "bind_checkout"), patch.object(card.funding_ledger, "process_checkout_event") as settle, self.assertRaises(card.funding_ledger.PaymentMismatch):
                card.handle_webhook(*signed_event())
            settle.assert_not_called()

    def test_refund_seen_before_completed_event_recorded_before_settlement(self):
        session = checkout(True)
        session["payment_intent"]["latest_charge"]["amount_refunded"] = 2500
        order = MagicMock()
        with patch.object(card, "_retrieve_session", return_value=session), patch.object(card.funding_ledger, "bind_checkout"), patch.object(card.funding_ledger, "record_reversal", order.refund), patch.object(card.funding_ledger, "process_checkout_event", order.settle):
            card.handle_webhook(*signed_event())
        self.assertEqual([c[0] for c in order.mock_calls], ["refund", "settle"])

    def test_unknown_event_is_ignored_without_provider_or_accounting_calls(self):
        with patch.object(card, "_client") as client, patch.object(card.funding_ledger, "process_checkout_event") as settle:
            self.assertEqual(card.handle_webhook(*signed_event(kind="customer.created")), {"status": "ignored"})
        client.assert_not_called()
        settle.assert_not_called()

    def test_mutable_metadata_cannot_suppress_known_purchase_or_change_wallet(self):
        for metadata in ({}, {"axonos_funding_id": "attacker", "wallet_address": "0x" + "b" * 40}):
            session = checkout(True)
            session["metadata"] = metadata
            with self.subTest(metadata=metadata), patch.object(card, "_retrieve_session", return_value=session), patch.object(card.funding_ledger, "lookup_card_binding", return_value=PAYMENT), patch.object(card.funding_ledger, "bind_checkout") as bind, patch.object(card.funding_ledger, "process_checkout_event") as settle:
                card.handle_webhook(*signed_event())
                bind.assert_called_once_with(PAYMENT, session)
                settle.assert_called_once_with("evt_example", "checkout.session.completed", session)

    def test_mutable_metadata_cannot_hide_refund_on_a_bound_payment(self):
        client = MagicMock()
        session = checkout(True)
        session["metadata"] = {}
        client.v1.checkout.sessions.list.return_value = {"data": [session]}
        charge = dict(session["payment_intent"]["latest_charge"], amount_refunded=5000)
        with patch.object(card, "_client", return_value=client), patch.object(card, "_retrieve_session", return_value=session), patch.object(card.funding_ledger, "lookup_card_binding", return_value=PAYMENT), patch.object(card.funding_ledger, "bind_checkout"), patch.object(card.funding_ledger, "record_reversal") as reverse, patch.object(card.funding_ledger, "process_checkout_event") as settle:
            card.handle_webhook(*signed_event(kind="charge.refunded", obj=charge))
        reverse.assert_called_once_with("evt_example", "charge.refunded", charge, False)
        settle.assert_not_called()

    def test_retrieved_session_must_match_signed_event_and_object_type(self):
        client = MagicMock()
        for changes in ({"id": "cs_other"}, {"object": "payment_intent"}):
            client.v1.checkout.sessions.retrieve.return_value = dict(checkout(True), **changes)
            with patch.object(card, "_client", return_value=client), self.assertRaises(card.funding_ledger.PaymentMismatch):
                card._retrieve_session("cs_test_example")

    def test_sdk_version_matches_explicit_api_pin(self):
        self.assertEqual(card.stripe.VERSION, "12.5.1")
        self.assertEqual(card.stripe.api_version, card.API_VERSION)


class CardHttpTests(unittest.TestCase):
    def setUp(self):
        self.client = gate.app.test_client()
        gate.app.testing = True
        self.enabled = patch.object(card, "public_config", return_value={"card_payments_enabled": True})
        self.enabled.start()
        self.addCleanup(self.enabled.stop)
        limiter = patch.object(gate._card_checkout_limiter, "allow", return_value=True)
        limiter.start()
        self.addCleanup(limiter.stop)

    def test_missing_cookie_only_or_query_only_identity_rejected(self):
        with patch.object(card, "create_checkout") as create:
            for path in ("/api/payments/stripe/checkout", "/api/payments/stripe/checkout?auth_token=attacker"):
                response = self.client.post(path, json={"amount_usd": "50.00"})
                self.assertEqual(response.status_code, 401)
            self.client.set_cookie(gate._auth_cookie_name(), "cookie-only")
            self.assertEqual(self.client.post("/api/payments/stripe/checkout", json={"amount_usd": "50.00"}).status_code, 401)
        create.assert_not_called()

    def test_identity_derived_from_valid_bearer_row_rejects_expired_guest_and_invalid_tokens(self):
        now = time.time()
        for row, status in ((None, 401), ((WALLET, "current", now - 1, now - 1), 401),
                            (("0x6775657374" + "a" * 30, "current", now + 100, now + 100), 401),
                            ((WALLET, "current", now + 100, now + 100), 200),
                            ((WALLET, "grace", now - 10, now + 30), 200)):
            conn = MagicMock()
            conn.cursor.return_value.__enter__.return_value.fetchone.return_value = row
            with self.subTest(row=row), patch.object(gate, "_gate_pg_init_once", return_value=True), patch.object(gate, "_gate_pg_get_connection", return_value=conn), patch.object(card, "create_checkout", return_value={"checkout_url": "https://checkout.stripe.com/test", "payment_id": PAYMENT}) as create:
                response = self.client.post("/api/payments/stripe/checkout", json={"amount_usd": "50.00"}, headers={"X-AXGT-Auth-Token": "verified-token"})
                self.assertEqual(response.status_code, status)
                if status == 200:
                    create.assert_called_once_with(WALLET, "50.00")
                else:
                    create.assert_not_called()

    def test_browser_cannot_supply_wallet_or_credits_or_success_url(self):
        with patch.object(gate, "_card_authenticated_wallet", return_value=(WALLET, None)), patch.object(card, "create_checkout") as create:
            for extra in ({"wallet_address": "0x" + "b" * 40}, {"credits": 99999}, {"success_url": "https://evil.test"}):
                self.assertEqual(self.client.post("/api/payments/stripe/checkout", json={"amount_usd": "50.00", **extra}).status_code, 400)
        create.assert_not_called()

    def test_raw_json_numeric_precision_is_not_rounded_before_validation(self):
        def validate_only(wallet, amount):
            cents, credits, _ = card.quote(amount)
            return {"cents": cents, "credits": str(credits)}
        with patch.dict(os.environ, ENV), patch.object(gate, "_card_authenticated_wallet", return_value=(WALLET, None)), patch.object(card, "create_checkout", side_effect=validate_only):
            for amount in ("1.00000000000000001", "9.99999999999999999", "1e0", "1e2", "NaN", "Infinity", "-Infinity", "-1", "0", "100000000000000000000", "1.001"):
                response = self.client.post("/api/payments/stripe/checkout", data='{"amount_usd":' + amount + '}', content_type="application/json")
                self.assertEqual(response.status_code, 400, amount)
            for amount in ("1", "1.00", "1.01", "999.99", "1000.00"):
                response = self.client.post("/api/payments/stripe/checkout", data='{"amount_usd":' + amount + '}', content_type="application/json")
                self.assertEqual(response.status_code, 200, amount)
                self.assertEqual(response.json["cents"], int(Decimal(amount) * 100))

    def test_revoked_token_and_expired_grace_cannot_create_a_checkout(self):
        conn = MagicMock()
        for status, expiry, grace in (("revoked", time.time() + 100, time.time() + 100),
                                      ("grace", time.time() + 100, time.time() - 1)):
            conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (WALLET, status, expiry, grace)
            with patch.object(gate, "_gate_pg_init_once", return_value=True), patch.object(gate, "_gate_pg_get_connection", return_value=conn), patch.object(card, "create_checkout") as create:
                response = self.client.post("/api/payments/stripe/checkout", json={"amount_usd": "50.00"}, headers={"X-AXGT-Auth-Token": "token"})
                self.assertEqual(response.status_code, 401)
                create.assert_not_called()

    def test_disabled_endpoint_does_not_create_checkout(self):
        with patch.object(card, "public_config", return_value={"card_payments_enabled": False}), patch.object(card, "create_checkout") as create:
            self.assertEqual(self.client.post("/api/payments/stripe/checkout", json={"amount_usd": "50.00"}).status_code, 503)
        create.assert_not_called()

    def test_status_is_wallet_scoped_and_cannot_fulfill_on_success_page_visit(self):
        with patch.object(gate, "_card_authenticated_wallet", return_value=(WALLET, None)), patch.object(card.funding_ledger, "get_card_payment", return_value=None) as status, patch.object(card.funding_ledger, "process_checkout_event") as settle:
            response = self.client.get("/api/payments/stripe/status?payment_id=" + PAYMENT)
        self.assertEqual(response.status_code, 404)
        status.assert_called_once_with(PAYMENT, WALLET)
        settle.assert_not_called()
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_webhook_receives_unchanged_raw_bytes_and_returns_only_ack(self):
        payload = b'{ "id" : "evt_example" }\n'
        with patch.object(card, "handle_webhook", return_value={"wallet_address": WALLET}) as handle:
            response = self.client.post("/api/payments/stripe/webhook", data=payload, content_type="application/json", headers={"Stripe-Signature": "sig"})
        handle.assert_called_once_with(payload, "sig")
        self.assertEqual(response.json, {"received": True})

    def test_webhook_invalid_signature_mismatch_and_database_retry_codes(self):
        for exc, status in ((card.InvalidWebhook(), 400), (card.funding_ledger.PaymentMismatch(), 400),
                            (card.funding_ledger.FundingUnavailable(), 503), (card.CardUnavailable(), 503)):
            with self.subTest(exc=exc), patch.object(card, "handle_webhook", side_effect=exc):
                self.assertEqual(self.client.post("/api/payments/stripe/webhook", data=b"{}").status_code, status)

    def test_body_limits(self):
        with patch.object(gate, "_card_authenticated_wallet", return_value=(WALLET, None)), patch.object(card, "create_checkout") as create, patch.object(card, "handle_webhook") as hook:
            self.assertEqual(self.client.post("/api/payments/stripe/checkout", data=b"x" * 4097, content_type="application/json").status_code, 413)
            self.assertEqual(self.client.post("/api/payments/stripe/webhook", data=b"x" * (card.MAX_WEBHOOK_BYTES + 1)).status_code, 413)
        create.assert_not_called()
        hook.assert_not_called()

    def test_slow_body_times_out_before_provider_processing(self):
        with patch.object(gate, "_card_request_body", side_effect=TimeoutError()), patch.object(card, "handle_webhook") as handle:
            self.assertEqual(self.client.post("/api/payments/stripe/webhook", data=b"{}").status_code, 408)
            handle.assert_not_called()

    def test_production_body_deadline_interrupts_a_trickled_stream(self):
        try:
            import gevent
        except ImportError:
            self.skipTest("Production gevent transport is not installed")
        fake = MagicMock()
        fake.stream.read.side_effect = lambda _: gevent.sleep(1)
        with patch.object(gate, "request", fake), patch.object(gate, "_CARD_BODY_DEADLINE_SECONDS", 0.01), self.assertRaises(TimeoutError):
            gate._card_request_body(4096)
