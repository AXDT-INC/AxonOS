"""Funding provenance, separate from the existing spendable credit balance.

The authenticated wallet and immutable quote are saved before talking to Stripe.
Only a verified provider object matching that saved quote can settle it. Session
row locks, unique provider IDs and the balance mutation share one transaction.
Refunds/disputes are audit records requiring operator reconciliation: they never
silently subtract compute that may already have been consumed.
"""

import json
import math
import re
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from . import deposit_ledger as _ledger
    from .guest_mode import is_guest_identity
except ImportError:
    import deposit_ledger as _ledger
    from guest_mode import is_guest_identity

_TABLE = "axonos_funding_transactions"
_EVENTS = "axonos_funding_events"
_WALLET = re.compile(r"^0x[0-9a-f]{40}$")
_CHECKOUT_EVENTS = frozenset({
    "checkout.session.completed", "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed", "checkout.session.expired",
})
_REVERSAL_EVENTS = frozenset({
    "charge.refunded", "charge.dispute.created", "charge.dispute.updated", "charge.dispute.closed",
})
_COLUMNS = (
    "id", "wallet_address", "fiat_amount", "fiat_currency", "expected_credits",
    "credits_added", "credits_per_usd", "livemode", "status",
    "stripe_checkout_session_id", "stripe_payment_intent_id", "credited_at",
    "reconciliation_required", "refunded_amount",
)


class FundingUnavailable(RuntimeError):
    """Retryable storage/reconciliation failure; do not acknowledge a webhook."""


class PaymentMismatch(ValueError):
    """A provider payment does not match the immutable server-side purchase."""


def ensure_tables(cur):
    """Run inside the existing ledger's schema transaction (no separate commit)."""
    cur.execute((Path(__file__).parent / "migrations" / "005_hybrid_funding.sql").read_text())


@contextmanager
def _transaction():
    if not _ledger.init_once():
        raise FundingUnavailable("Funding database unavailable")
    conn = _ledger._get_connection()
    if conn is None:
        raise FundingUnavailable("Funding database unavailable")
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except (PaymentMismatch, FundingUnavailable):
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        # Never return DB exceptions (which may include payment or connection data).
        raise FundingUnavailable("Funding transaction could not be committed") from None
    finally:
        conn.close()


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PaymentMismatch("Invalid payment number") from None
    if not result.is_finite():
        raise PaymentMismatch("Invalid payment number")
    return result


def _provider_id(value, prefix):
    if isinstance(value, dict):
        value = value.get("id")
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 255:
        raise PaymentMismatch("Missing or invalid provider identifier")
    return value


def _optional_id(value, prefix):
    return _provider_id(value, prefix) if value is not None else None


def _read(cur, column, value, *, wallet=None, lock=True):
    # column is always an internal constant, never caller input.
    sql = f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE} WHERE {column} = %s AND provider = 'stripe'"
    args = [value]
    if wallet is not None:
        sql += " AND wallet_address = %s"
        args.append(wallet)
    if lock:
        sql += " FOR UPDATE"
    cur.execute(sql, args)
    row = cur.fetchone()
    return dict(zip(_COLUMNS, row)) if row else None


def create_pending_card(wallet_address, amount_cents, credits, credits_per_usd, livemode):
    wallet = str(wallet_address or "").strip().lower()
    if not _WALLET.fullmatch(wallet) or is_guest_identity(wallet):
        raise PaymentMismatch("Invalid verified wallet")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or not 0 < amount_cents <= 99999999:
        raise PaymentMismatch("Invalid payment amount")
    credit_amount, rate = _decimal(credits), _decimal(credits_per_usd)
    amount = Decimal(amount_cents) / 100
    if credit_amount <= 0 or rate <= 0 or not math.isfinite(float(credit_amount)):
        raise PaymentMismatch("Invalid credit quote")
    # Provider may round down fractional minutes to a precision of 1e-8.
    if abs(credit_amount - amount * rate) > Decimal("0.00000001"):
        raise PaymentMismatch("Credit quote does not match purchase rate")
    if not isinstance(livemode, bool):
        raise PaymentMismatch("Invalid payment mode")
    payment_id = str(uuid.uuid4())
    now = time.time()
    snapshot = {"source": "AXGT_USD_PER_HOUR", "credits_per_usd": str(rate),
                "axgt_bonus_percent": "0", "holder_discount_percent": "0"}
    with _transaction() as cur:
        cur.execute(
            f"""INSERT INTO {_TABLE}
                (id, wallet_address, funding_type, payment_method, provider,
                 fiat_currency, fiat_amount, usd_valuation, expected_credits,
                 credits_per_usd, pricing_snapshot, status, livemode, created_at, updated_at)
                VALUES (%s, %s, 'fiat', 'stripe_card', 'stripe', 'usd', %s, %s, %s,
                        %s, %s::jsonb, 'pending', %s, %s, %s)""",
            (payment_id, wallet, amount, amount, credit_amount, rate,
             json.dumps(snapshot), livemode, now, now),
        )
    return {"id": payment_id, "wallet_address": wallet, "amount_cents": amount_cents,
            "credits": credit_amount, "credits_per_usd": rate, "livemode": livemode}


def _validate_session(payment, session):
    session_id = _provider_id(session.get("id"), "cs_")
    if payment["stripe_checkout_session_id"] not in (None, session_id):
        raise PaymentMismatch("Checkout Session does not match purchase")
    amount_cents = int(payment["fiat_amount"] * 100)
    for field in ("amount_total", "amount_subtotal"):
        if isinstance(session.get(field), bool) or not isinstance(session.get(field), int) or session[field] != amount_cents:
            raise PaymentMismatch("Checkout amount does not match purchase")
    if session.get("currency") != payment["fiat_currency"] or session.get("mode") != "payment":
        raise PaymentMismatch("Checkout currency or mode does not match purchase")
    if not isinstance(session.get("livemode"), bool) or session["livemode"] != payment["livemode"]:
        raise PaymentMismatch("Checkout environment does not match purchase")
    if session.get("client_reference_id") != payment["id"]:
        raise PaymentMismatch("Checkout identity binding does not match purchase")
    # client_reference_id is a cross-check. The recipient always comes from the
    # previously authenticated database record, never session metadata.
    intent = _optional_id(session.get("payment_intent"), "pi_")
    if payment["stripe_payment_intent_id"] not in (None, intent):
        raise PaymentMismatch("PaymentIntent does not match purchase")
    return session_id, intent


def bind_checkout(payment_id, checkout_session):
    with _transaction() as cur:
        payment = _read(cur, "id", payment_id)
        if not payment:
            raise FundingUnavailable("Pending purchase not found")
        session_id, intent = _validate_session(payment, checkout_session)
        cur.execute(
            f"""UPDATE {_TABLE} SET stripe_checkout_session_id = %s,
                stripe_payment_intent_id = COALESCE(stripe_payment_intent_id, %s),
                stripe_customer_id = COALESCE(stripe_customer_id, %s),
                status = CASE WHEN status = 'pending' THEN 'checkout_created' ELSE status END,
                updated_at = %s WHERE id = %s""",
            (session_id, intent, _optional_id(checkout_session.get("customer"), "cus_"),
             time.time(), payment_id),
        )


def lookup_card_binding(session_id):
    """Find a trusted persisted association independently of mutable metadata."""
    session_id = _provider_id(session_id, "cs_")
    with _transaction() as cur:
        payment = _read(cur, "stripe_checkout_session_id", session_id, lock=False)
        return payment["id"] if payment else None


def _event(cur, payment_id, event_id, event_type, details):
    _provider_id(event_id, "evt_")
    cur.execute(
        f"""INSERT INTO {_EVENTS} (provider_event_id, funding_id, event_type, details, created_at)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (provider_event_id) DO NOTHING RETURNING provider_event_id""",
        (event_id, payment_id, event_type, json.dumps(details), time.time()),
    )
    if cur.fetchone() is not None:
        return True
    cur.execute(f"SELECT funding_id, event_type FROM {_EVENTS} WHERE provider_event_id = %s", (event_id,))
    if cur.fetchone() != (payment_id, event_type):
        raise PaymentMismatch("Provider event is bound to a different payment")
    return False


def process_checkout_event(event_id, event_type, session):
    if event_type not in _CHECKOUT_EVENTS:
        raise PaymentMismatch("Unsupported Checkout event")
    session_id = _provider_id(session.get("id"), "cs_")
    with _transaction() as cur:
        payment = _read(cur, "stripe_checkout_session_id", session_id)
        if not payment:
            # Creation can succeed at Stripe just before a local bind fails.
            # Do not acknowledge and lose the payment; retry/reconcile it.
            raise FundingUnavailable("Checkout Session has not been bound")
        _, intent = _validate_session(payment, session)
        paid = (event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded")
                and session.get("payment_status") == "paid" and session.get("status") == "complete")
        if paid and not intent:
            raise PaymentMismatch("Paid Checkout has no PaymentIntent")
        fresh = _event(cur, payment["id"], event_id, event_type,
                       {"session_id": session_id, "payment_status": session.get("payment_status"),
                        "session_status": session.get("status"), "payment_intent": intent})
        result = {"status": payment["status"], "credited": False, "duplicate": not fresh,
                  "wallet_address": payment["wallet_address"], "credits_added": float(payment["credits_added"])}
        if not fresh:
            return result
        now = time.time()
        if paid and payment["credited_at"] is None and not payment["reconciliation_required"]:
            credits = float(payment["expected_credits"])
            # Existing compute balances are DOUBLE PRECISION. Conversion of the
            # immutable Decimal quote must never round its nominal credit delta
            # upward; retain both the quote and the actual issued delta for audit.
            if Decimal.from_float(credits) > payment["expected_credits"]:
                credits = math.nextafter(credits, -math.inf)
            wallet = payment["wallet_address"]
            cur.execute(
                """INSERT INTO axgt_deposits
                    (wallet_address, deposited_amount_axgt, credited_minutes_total,
                     consumed_minutes_total, remaining_minutes, created_at, updated_at)
                    VALUES (%s, 0, %s, 0, %s, %s, %s)
                    ON CONFLICT (wallet_address) DO UPDATE SET
                    credited_minutes_total = axgt_deposits.credited_minutes_total + EXCLUDED.credited_minutes_total,
                    remaining_minutes = axgt_deposits.remaining_minutes + EXCLUDED.remaining_minutes,
                    updated_at = EXCLUDED.updated_at RETURNING remaining_minutes""",
                (wallet, credits, credits, now, now),
            )
            remaining = float(cur.fetchone()[0])
            _ledger._ledger_write(cur, wallet, "deposit_credit", credits, Decimal("0"), remaining,
                                  reference_session_id=session_id, notes=f"CARD funding {payment['id']}",
                                  created_by="stripe_webhook")
            cur.execute(
                f"""UPDATE {_TABLE} SET status = 'succeeded', credits_added = %s,
                    credited_at = %s, paid_at = %s WHERE id = %s""",
                (Decimal.from_float(credits), now, now, payment["id"]),
            )
            result.update(status="succeeded", credited=True, credits_added=credits)
        elif not paid and payment["credited_at"] is None and not payment["reconciliation_required"]:
            status = {"checkout.session.async_payment_failed": "failed",
                      "checkout.session.expired": "expired"}.get(event_type, "awaiting_payment")
            cur.execute(f"UPDATE {_TABLE} SET status = %s WHERE id = %s", (status, payment["id"]))
            result["status"] = status
        cur.execute(
            f"""UPDATE {_TABLE} SET stripe_payment_intent_id = COALESCE(stripe_payment_intent_id, %s),
                provider_transaction_id = COALESCE(provider_transaction_id, %s),
                stripe_customer_id = COALESCE(stripe_customer_id, %s),
                stripe_invoice_id = COALESCE(stripe_invoice_id, %s),
                paid_at = COALESCE(paid_at, %s), updated_at = %s WHERE id = %s""",
            (intent, intent, _optional_id(session.get("customer"), "cus_"),
             _optional_id(session.get("invoice"), "in_"), now if paid else None, now, payment["id"]),
        )
        return result


def record_reversal(event_id, event_type, obj, livemode):
    if event_type not in _REVERSAL_EVENTS:
        raise PaymentMismatch("Unsupported reversal event")
    intent = _provider_id(obj.get("payment_intent"), "pi_")
    with _transaction() as cur:
        payment = _read(cur, "stripe_payment_intent_id", intent)
        if not payment:
            raise FundingUnavailable("PaymentIntent has not been reconciled")
        if not isinstance(livemode, bool) or livemode != payment["livemode"]:
            raise PaymentMismatch("Reversal environment does not match purchase")
        if obj.get("currency") != payment["fiat_currency"]:
            raise PaymentMismatch("Reversal currency does not match purchase")
        cents = obj.get("amount_refunded") if event_type == "charge.refunded" else obj.get("amount")
        if isinstance(cents, bool) or not isinstance(cents, int) or not 0 <= cents <= int(payment["fiat_amount"] * 100):
            raise PaymentMismatch("Reversal amount does not match purchase")
        if event_type == "charge.refunded" and obj.get("amount") != int(payment["fiat_amount"] * 100):
            raise PaymentMismatch("Refund charge does not match purchase")
        provider_id = _provider_id(obj.get("id"), "ch_" if event_type == "charge.refunded" else "dp_")
        details = {"object_id": provider_id, "payment_intent": intent, "amount_cents": cents,
                   "currency": obj.get("currency"), "provider_status": obj.get("status"),
                   "reason": obj.get("reason"), "accounting_action": "manual_reconciliation_required"}
        fresh = _event(cur, payment["id"], event_id, event_type, details)
        if not fresh:
            return {"duplicate": True, "reconciliation_required": True}
        if event_type == "charge.refunded":
            # charge.refunded is cumulative. GREATEST prevents an older webhook
            # delivered later from lowering the auditable refunded total.
            refunded = max(payment["refunded_amount"], Decimal(cents) / 100)
            status = "refunded" if refunded == payment["fiat_amount"] else "partially_refunded"
        else:
            refunded = payment["refunded_amount"]
            status = "disputed"
        cur.execute(
            f"""UPDATE {_TABLE} SET refunded_amount = %s, reconciliation_required = TRUE,
                status = %s, updated_at = %s WHERE id = %s""",
            (refunded, status, time.time(), payment["id"]),
        )
        return {"duplicate": False, "reconciliation_required": True, "status": status}


def get_card_payment(payment_id, wallet_address):
    wallet = str(wallet_address or "").strip().lower()
    with _transaction() as cur:
        payment = _read(cur, "id", payment_id, wallet=wallet, lock=False)
        if payment is None:
            return None
        return {"id": payment["id"], "status": payment["status"],
                "credited": payment["credited_at"] is not None,
                "credits_added": float(payment["credits_added"]),
                "expected_credits": float(payment["expected_credits"]),
                "amount_cents": int(payment["fiat_amount"] * 100), "currency": payment["fiat_currency"],
                "reconciliation_required": payment["reconciliation_required"]}


def record_crypto_on_cursor(cur, wallet, rail, amount, credits, tx_hash, block_number, chain_id, pricing_snapshot, *, observed_at=None):
    """Called by all existing crypto issuers in their original credit transaction."""
    amount, credits = _decimal(amount), _decimal(credits)
    snapshot = dict(pricing_snapshot or {})
    snapshot.setdefault("effective_credits_per_token", str(credits / amount))
    snapshot.setdefault("source", "verifier_credit_rate")
    now = time.time() if observed_at is None else observed_at
    cur.execute(
        f"""INSERT INTO {_TABLE}
            (id, wallet_address, funding_type, payment_method, provider, provider_transaction_id,
             crypto_currency, crypto_amount, usd_valuation, expected_credits, credits_added,
             credits_per_usd, pricing_snapshot, status, chain_id, block_number,
             created_at, updated_at, paid_at, credited_at)
            VALUES (%s, %s, 'crypto', %s, 'onchain', %s, %s, %s, %s, %s, %s,
                    %s, %s::jsonb, 'succeeded', %s, %s, %s, %s, %s, %s)""",
        (uuid.uuid4().hex, wallet, rail, tx_hash, rail.upper(), amount, snapshot.get("usd_value"),
         credits, credits, snapshot.get("credits_per_usd"), json.dumps(snapshot, default=str),
         chain_id, block_number, now, now, now, now),
    )
