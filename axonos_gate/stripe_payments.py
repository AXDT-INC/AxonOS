"""Stripe-hosted CARD payments. Wallet identity and credit accounting stay in AxonOS.

Only signed webhooks fulfill purchases. Stripe objects locate a persisted order;
the order's wallet and frozen credit quote are the issuance authority.
"""

import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urlsplit

try:
    import stripe
except ImportError:  # Crypto-only installations continue to work.
    stripe = None

try:
    from . import funding_ledger, price_oracle
except ImportError:
    import funding_ledger
    import price_oracle

API_VERSION = "2025-08-27.basil"
MAX_WEBHOOK_BYTES = 256 * 1024
CHECKOUT_EVENTS = frozenset({
    "checkout.session.completed", "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed", "checkout.session.expired",
})
REVERSAL_EVENTS = frozenset({
    "charge.refunded", "charge.dispute.created", "charge.dispute.updated",
    "charge.dispute.closed",
})


class CardUnavailable(Exception):
    pass


class InvalidAmount(ValueError):
    pass


class InvalidWebhook(ValueError):
    pass


def _credentials():
    _require_isolated_gate()
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    signing = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    key_match = re.fullmatch(r"(sk|rk)_(test|live)_\S+", key)
    if (stripe is None or not key_match
            or not signing.startswith("whsec_") or not os.getenv("AXGT_CHALLENGE_DB_URL")):
        raise CardUnavailable("Card payments are unavailable")
    return key, signing, key_match.group(2) == "live"


def _public_base_url():
    base = os.getenv("AXGT_PUBLIC_BASE_URL", "").strip().rstrip("/")
    url = urlsplit(base)
    try:
        url.port
    except ValueError:
        raise CardUnavailable("Card payments are unavailable") from None
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path or any(c.isspace() for c in base)):
        raise CardUnavailable("Card payments are unavailable")
    return base


def _require_isolated_gate():
    # Shared legacy desktops intentionally have sudo. Only the central gate
    # with per-wallet containers may possess a payment processor credential.
    if (os.getenv("AXGT_USER_CONTAINER_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on")
            or os.getenv("AXGT_SESSION_ID", "").strip()
            or os.getenv("AXGT_CARD_GATE_ONLY", "").strip().lower() != "true"
            or os.getenv("AXGT_DESKTOP_ENABLED", "").strip().lower() != "false"
            or os.getenv("AXGT_SSH_ENABLED", "false").strip().lower() not in ("", "0", "false", "no", "off")):
        raise CardUnavailable("Card payments require an isolated central gate")


def _limits():
    try:
        low = Decimal(os.getenv("STRIPE_MIN_AMOUNT_USD", "1") or "1")
        high = Decimal(os.getenv("STRIPE_MAX_AMOUNT_USD", "1000") or "1000")
        if (not low.is_finite() or not high.is_finite() or low < Decimal("0.50")
                or high < low or high > Decimal("999999.99")
                or low * 100 != (low * 100).to_integral_value()
                or high * 100 != (high * 100).to_integral_value()):
            raise ValueError()
        return low, high
    except (InvalidOperation, ValueError):
        raise CardUnavailable("Card payments are unavailable") from None


def _rate():
    # Same USD compute pricing as the oracle, without holder discounts/AXGT bonus.
    try:
        hourly = price_oracle.usd_per_hour()
        if not hourly.is_finite() or hourly <= 0:
            raise ValueError()
        rate = Decimal(60) / hourly
        if not rate.is_finite() or rate <= 0 or rate > Decimal("1000000000"):
            raise ValueError()
        return rate
    except (InvalidOperation, ValueError, OverflowError):
        raise CardUnavailable("Card payments are unavailable") from None


def public_config():
    """Explicit public allowlist: never serialize configuration or credentials."""
    try:
        _credentials()
        _require_isolated_gate()
        _public_base_url()
        low, high = _limits()
        rate = _rate()
        return {"card_payments_enabled": True, "card_min_amount_usd": str(low),
                "card_max_amount_usd": str(high), "card_credits_per_usd": str(rate)}
    except (CardUnavailable, ValueError):
        return {"card_payments_enabled": False}


def quote(amount):
    # Accept USD decimal strings or JSON numbers, never booleans/non-finite values,
    # scientific notation, sub-cent amounts or silently rounded payments.
    if isinstance(amount, bool) or not isinstance(amount, (str, int, Decimal)):
        raise InvalidAmount("Enter a USD amount with at most two decimal places")
    value = str(amount)
    if not re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,2})?", value):
        raise InvalidAmount("Enter a USD amount with at most two decimal places")
    usd = Decimal(value)
    low, high = _limits()
    if not low <= usd <= high:
        raise InvalidAmount(f"Amount must be between ${low} and ${high}")
    rate = _rate()
    credits = (usd * rate).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    if credits <= 0:
        raise InvalidAmount("Amount is too small for the configured compute price")
    return int(usd * 100), credits, rate


def _client():
    key, _, _ = _credentials()
    # SDK exceptions and debug request/response bodies must never enter our logs.
    logging.getLogger("stripe").setLevel(logging.WARNING)
    # Pinned SDK also has a direct stderr path independent of Python logging.
    stripe.log = None
    stripe._util.STRIPE_LOG = None
    return stripe.StripeClient(key, stripe_version=API_VERSION, max_network_retries=1,
                               http_client=stripe.http_client.RequestsClient(timeout=15))


def create_checkout(wallet_address, amount):
    _, _, livemode = _credentials()
    _require_isolated_gate()
    base = _public_base_url()
    cents, credits, rate = quote(amount)
    payment = funding_ledger.create_pending_card(wallet_address, cents, credits, rate, livemode)
    payment_id = str(payment["id"])
    try:
        session = _client().v1.checkout.sessions.create({
            "mode": "payment", "payment_method_types": ["card"],
            "client_reference_id": payment_id,
            "metadata": {"axonos_funding_id": payment_id},
            "payment_intent_data": {"metadata": {"axonos_funding_id": payment_id}},
            "customer_creation": "always", "billing_address_collection": "required",
            "line_items": [{"quantity": 1, "price_data": {
                "currency": "usd", "unit_amount": cents,
                "product_data": {"name": "AxonOS compute credits"},
            }}],
            "success_url": base + "/vnc.html?card_payment=success&payment_id=" + payment_id,
            "cancel_url": base + "/vnc.html?card_payment=cancel&payment_id=" + payment_id,
        }, {"idempotency_key": "axonos-card-" + payment_id})
    except Exception:
        # Pending row remains available for reconciliation after an ambiguous
        # network failure. Never delete an order that may exist at the provider.
        raise CardUnavailable("Secure checkout is temporarily unavailable; retry shortly") from None
    funding_ledger.bind_checkout(payment_id, session)
    url = session.get("url") or ""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "checkout.stripe.com":
        raise CardUnavailable("Secure checkout is temporarily unavailable")
    return {"checkout_url": url, "payment_id": payment_id}


def verify_webhook(payload, signature):
    _, signing, livemode = _credentials()
    if not payload or len(payload) > MAX_WEBHOOK_BYTES or not signature:
        raise InvalidWebhook("Invalid webhook")
    try:
        event = stripe.Webhook.construct_event(payload, signature, signing, tolerance=300)
        # Also bound future timestamps (SDK bounds stale timestamps).
        stamps = [int(v[2:]) for v in signature.split(",") if v.startswith("t=")]
        if len(stamps) != 1 or abs(time.time() - stamps[0]) > 300:
            raise ValueError()
        if (not isinstance(event.get("livemode"), bool) or event["livemode"] != livemode
                or not isinstance(event.get("id"), str) or not event["id"].startswith("evt_")
                or not isinstance(event.get("data", {}).get("object"), dict)):
            raise ValueError()
        return event
    except Exception:
        raise InvalidWebhook("Invalid webhook signature or event") from None


def _retrieve_session(session_id):
    try:
        session = _client().v1.checkout.sessions.retrieve(
            session_id, {"expand": ["payment_intent.latest_charge"]})
    except Exception:
        raise CardUnavailable("Payment verification temporarily unavailable") from None
    if session.get("id") != session_id or session.get("object") != "checkout.session":
        raise funding_ledger.PaymentMismatch("Checkout retrieval does not match event")
    return session


def _is_axonos_session(session):
    # Metadata is mutable at Stripe. Once a Session is bound, clearing/editing
    # metadata must never cause a paid purchase or a reversal to be discarded.
    if funding_ledger.lookup_card_binding(session.get("id")) is not None:
        return True
    reference = session.get("client_reference_id")
    metadata = session.get("metadata")
    return bool(reference and isinstance(metadata, dict)
                and metadata.get("axonos_funding_id") == reference)


def _bind_retrieved(session):
    # Recovery after a crash between Stripe creation and the local bind uses a
    # server-created random order id from an independently retrieved Session.
    # The persisted order still supplies the wallet, amount, and credit snapshot.
    payment_id = session.get("client_reference_id")
    if not isinstance(payment_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", payment_id):
        raise funding_ledger.PaymentMismatch("Unrecognized Checkout Session")
    funding_ledger.bind_checkout(payment_id, session)


def _validate_paid_intent(session):
    intent = session.get("payment_intent")
    if (not isinstance(intent, dict) or intent.get("object") != "payment_intent"
            or not isinstance(intent.get("id"), str) or not intent["id"].startswith("pi_")
            or intent.get("status") != "succeeded"
            or intent.get("currency") != session.get("currency")
            or intent.get("amount_received") != session.get("amount_total")
            or intent.get("amount") != session.get("amount_total")
            or intent.get("livemode") != session.get("livemode")):
        raise funding_ledger.PaymentMismatch("PaymentIntent does not match purchase")
    charge = intent.get("latest_charge")
    if (not isinstance(charge, dict) or charge.get("object") != "charge"
            or not isinstance(charge.get("id"), str) or not charge["id"].startswith("ch_")
            or not isinstance(charge.get("livemode"), bool) or charge["livemode"] != session.get("livemode")
            or charge.get("paid") is not True
            or charge.get("captured") is not True
            or charge.get("payment_intent") != intent.get("id")
            or charge.get("currency") != session.get("currency")
            or charge.get("amount") != session.get("amount_total")
            or charge.get("amount_captured") != session.get("amount_total")
            or charge.get("payment_method_details", {}).get("type") != "card"):
        raise funding_ledger.PaymentMismatch("Charge does not match purchase")
    return charge


def handle_webhook(payload, signature):
    event = verify_webhook(payload, signature)
    event_type, event_id = event.get("type"), event["id"]
    obj = event["data"]["object"]
    if event_type in CHECKOUT_EVENTS:
        if obj.get("object") != "checkout.session" or not str(obj.get("id", "")).startswith("cs_"):
            raise InvalidWebhook("Invalid Checkout Session")
        session = _retrieve_session(obj["id"])
        if not _is_axonos_session(session):
            return {"status": "ignored"}  # An unrelated product on the Stripe account.
        _bind_retrieved(session)
        if session.get("payment_status") == "paid" and session.get("status") == "complete":
            charge = _validate_paid_intent(session)
            if charge.get("amount_refunded", 0) > 0:
                funding_ledger.record_reversal(event_id + ":refund", "charge.refunded", charge, event["livemode"])
            if charge.get("disputed"):
                # Locate the authoritative dispute; no invented negative credit.
                try:
                    disputes = _client().v1.disputes.list({"charge": charge["id"], "limit": 1})
                except Exception:
                    raise CardUnavailable("Payment verification temporarily unavailable") from None
                if not disputes.get("data"):
                    raise CardUnavailable("Payment reconciliation pending")
                funding_ledger.record_reversal(event_id + ":dispute", "charge.dispute.updated",
                                               disputes["data"][0], event["livemode"])
        # The signed event supplies the event type; payment state comes from the
        # current API object. Ledger transitions preserve already credited orders.
        return funding_ledger.process_checkout_event(event_id, event_type, session)
    if event_type in REVERSAL_EVENTS:
        intent_id = obj.get("payment_intent")
        if not isinstance(intent_id, str) or not intent_id.startswith("pi_"):
            raise InvalidWebhook("Invalid reversal")
        try:
            sessions = _client().v1.checkout.sessions.list({"payment_intent": intent_id, "limit": 1})
        except Exception:
            raise CardUnavailable("Payment verification temporarily unavailable") from None
        if not sessions.get("data"):
            return {"status": "ignored"}  # Another product on this Stripe account.
        session = _retrieve_session(sessions["data"][0]["id"])
        if not _is_axonos_session(session):
            return {"status": "ignored"}
        _bind_retrieved(session)
        return funding_ledger.record_reversal(event_id, event_type, obj, event["livemode"])
    return {"status": "ignored"}
