"""Synthetic F2 producer authority, worker proof, and bounded failure isolation."""

import base64
import copy
import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, patch

import pytest

from axonos_gate import x_capi, x_capi_worker

WALLET = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
CONTEXT = "C" * 64
NOW = 1700000000.25


@pytest.fixture(autouse=True)
def synthetic_capi():
    values = {
        "X_CAPI_MODE": "dry_run",
        "X_CAPI_PIXEL_ID": "synthetic-source",
        "X_CAPI_EVENT_WALLET_VERIFIED": "synthetic-wallet",
        "X_CAPI_EVENT_DEPOSIT_COMPLETED": "synthetic-deposit",
        "X_CAPI_EVENT_SESSION_STARTED": "synthetic-session",
        "X_CAPI_ALLOWED_ORIGIN": "https://app.example",
        "X_CAPI_DEPLOYMENT_ID": "synthetic-deployment",
        "X_CAPI_CONSENT_POLICY_VERSION": "synthetic-policy",
        "X_CAPI_CONSENT_POLICY_EPOCH": "1",
        "X_CAPI_CONTEXT_KEY": base64.urlsafe_b64encode(b"k" * 32).decode(),
        "X_CAPI_ALLOW_TEST_SECRETS": "1",
    }
    # Preserve only the explicit disposable fixture and unsafe libpq names so
    # its independent guard can reject ambient overrides before connecting.
    values.update({key: value for key, value in os.environ.items()
                   if key.startswith("PG") or key in (
                       "PAYMENT_AUTH_TEST_DB_URL", "PAYMENT_AUTH_TEST_DB_CONFIRM")})
    with patch.dict(os.environ, values, clear=True):
        yield


def envelope(action="event", milestone="wallet_verified"):
    result = {
        "v": 1, "action": action, "context_token": CONTEXT,
        "wallet_address": WALLET, "event_timestamp_ms": int(NOW * 1000),
    }
    if action == "event":
        result.update({
            "milestone": milestone,
            "source_key": WALLET if milestone == "wallet_verified" else "0x" + "a" * 64,
            "metadata": {
                "allow_context_binding": milestone == "wallet_verified",
                "credit_source": "onchain" if milestone == "deposit_completed" else None,
                "payment_rail": "usdc" if milestone == "deposit_completed" else None,
                "chain_id": 8453 if milestone == "deposit_completed" else None,
            },
        })
    return result


def signed(document=None, now=NOW):
    result = envelope() if document is None else copy.deepcopy(document)
    result["wallet_authority"] = x_capi._seal_wallet_authority(result, x_capi.load_config(), now)
    return result


def context(authorize=None):
    return x_capi.authenticated_context(CONTEXT, WALLET, authorize or (lambda: True))


def test_context_creation_is_io_free_and_immutable():
    authorize = Mock(return_value=True)
    with patch.object(x_capi, "_context_cipher", side_effect=AssertionError("key I/O")), \
         patch.object(x_capi, "_send_local_envelope", side_effect=AssertionError("socket")):
        authority = context(authorize)
    authorize.assert_not_called()
    assert CONTEXT not in repr(authority) and WALLET not in repr(authority)
    with pytest.raises(FrozenInstanceError):
        authority._wallet = OTHER
    with pytest.raises(TypeError):
        json.dumps(authority)


@pytest.mark.parametrize("mode", ["off", "live"])
def test_disabled_modes_create_no_authority_or_task(mode):
    with patch.dict(os.environ, {"X_CAPI_MODE": mode}), \
         patch.object(x_capi, "_queue_authenticated_envelope") as queue:
        assert context() is None
        assert not x_capi.emit_binding_nonblocking(context_token=context(), wallet_address=WALLET)
    queue.assert_not_called()


@pytest.mark.parametrize("action", ["event", "bind"])
def test_raw_context_cannot_emit_even_for_previously_bound_wallet(action):
    with patch.object(x_capi, "_queue_authenticated_envelope") as queue:
        if action == "bind":
            assert not x_capi.emit_binding_nonblocking(context_token=CONTEXT, wallet_address=WALLET)
        else:
            assert not x_capi.emit_event_nonblocking(
                context_token=CONTEXT, wallet_address=WALLET, milestone="deposit_completed",
                source_key="0x" + "a" * 64, credit_source="onchain", payment_rail="usdc", chain_id=8453,
            )
    queue.assert_not_called()


@pytest.mark.parametrize("substitution", ["wallet", "process", "scope"])
def test_captured_authority_cannot_change_wallet_process_or_generation(substitution):
    authority = context()
    wallet = OTHER if substitution == "wallet" else WALLET
    if substitution == "process":
        guard = patch.object(x_capi.os, "getpid", return_value=os.getpid() + 1)
    else:
        guard = patch.dict(os.environ, {"X_CAPI_DEPLOYMENT_ID": "changed"} if substitution == "scope" else {})
    with guard, patch.object(x_capi, "_queue_authenticated_envelope") as queue:
        assert not x_capi.emit_binding_nonblocking(context_token=authority, wallet_address=wallet)
    queue.assert_not_called()


@pytest.mark.parametrize("outcome", [False, None, "synthetic-bearer", RuntimeError("synthetic-bearer")])
def test_invalid_ambiguous_or_failed_auth_never_sends_or_logs(outcome, caplog):
    authorize = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    with patch.object(x_capi, "_send_local_envelope") as send:
        assert not x_capi._authorize_and_send(context(authorize), envelope(), x_capi.load_config(), time.monotonic())
    send.assert_not_called()
    assert "synthetic-bearer" not in caplog.text


@pytest.mark.parametrize("action", ["event", "bind"])
def test_successful_authorization_produces_strict_worker_verifiable_proof(action):
    document = envelope(action=action)
    authority = context()
    with patch.object(x_capi.time, "time", return_value=NOW), \
         patch.object(x_capi, "_send_local_envelope", return_value=True) as send:
        assert x_capi._authorize_and_send(authority, document, x_capi.load_config(), time.monotonic())
    wire = send.call_args.args[0]
    assert x_capi.wallet_authority_valid(wire, NOW)
    assert x_capi_worker._validate_ingest_envelope(wire) == wire
    cipher, _ = x_capi._context_cipher()
    token = wire["wallet_authority"]
    proof = json.loads(cipher.decrypt((token + "=" * ((4 - len(token) % 4) % 4)).encode()))
    assert set(proof) == {"purpose", "protocol", "audience_scope", "issued_at_ms", "expires_at_ms", "envelope_digest"}
    assert CONTEXT not in json.dumps(proof) and WALLET not in json.dumps(proof)


@pytest.mark.parametrize("field,value", [
    ("wallet_address", OTHER), ("context_token", "D" * 64),
    ("source_key", OTHER), ("event_timestamp_ms", int(NOW * 1000) + 1),
    ("action", "bind"), ("milestone", "session_started"),
])
def test_proof_cannot_be_transplanted_to_another_fact(field, value):
    wire = signed()
    wire[field] = value
    assert not x_capi.wallet_authority_valid(wire, NOW)


def test_proof_covers_payment_provenance_and_binding_flag():
    wire = signed(envelope(milestone="deposit_completed"))
    for field, value in (("payment_rail", "eth"), ("chain_id", 1), ("allow_context_binding", True)):
        changed = copy.deepcopy(wire)
        changed["metadata"][field] = value
        assert not x_capi.wallet_authority_valid(changed, NOW)


@pytest.mark.parametrize("now", [NOW - 0.001, NOW + 30, NOW + 300])
def test_expired_or_future_proofs_fail_closed(now):
    assert not x_capi.wallet_authority_valid(signed(), now)


def test_primary_key_only_and_generation_binding():
    wire = signed()
    with patch.dict(os.environ, {"X_CAPI_CONTEXT_KEY": base64.urlsafe_b64encode(b"j" * 32).decode() + "\n" + os.environ["X_CAPI_CONTEXT_KEY"]}):
        assert not x_capi.wallet_authority_valid(wire, NOW)
    with patch.dict(os.environ, {"X_CAPI_DEPLOYMENT_ID": "different"}):
        assert not x_capi.wallet_authority_valid(wire, NOW)


def test_delayed_auth_does_not_mint_a_fresh_proof_window():
    authority = context()
    with patch.object(x_capi.time, "time", side_effect=[NOW, NOW + 31]), \
         patch.object(x_capi, "_send_local_envelope") as send:
        assert not x_capi._authorize_and_send(authority, envelope(), x_capi.load_config(), time.monotonic())
    send.assert_not_called()


def test_worker_rejects_missing_or_forged_proof_before_database_access():
    conn = Mock()
    missing = envelope()
    assert x_capi_worker.process_ingest_envelope(conn, missing, NOW) == "invalid_envelope"
    forged = dict(missing, wallet_authority="A" * 128)
    assert x_capi_worker.process_ingest_envelope(conn, forged, NOW) == "invalid_wallet_authority"
    assert x_capi_worker._ingest_event(conn, forged, NOW) == "invalid_wallet_authority"
    conn.cursor.assert_not_called()


def test_worker_rejects_deposit_binding_even_with_valid_authentication_proof():
    deposit = envelope(milestone="deposit_completed")
    deposit["metadata"]["allow_context_binding"] = True
    conn = Mock()
    assert x_capi_worker.process_ingest_envelope(conn, signed(deposit), NOW) == "invalid_envelope"
    conn.cursor.assert_not_called()


def test_producer_rejects_deposit_binding():
    with patch.object(x_capi, "_queue_authenticated_envelope") as queue:
        assert not x_capi.emit_event_nonblocking(
            context_token=context(), wallet_address=WALLET, milestone="deposit_completed",
            source_key="0x" + "a" * 64, credit_source="onchain", payment_rail="usdc",
            chain_id=8453, allow_context_binding=True,
        )
    queue.assert_not_called()


def test_pool_is_bounded_and_payment_caller_never_waits_for_authentication():
    ready = threading.Event()
    release = threading.Event()
    calls = []

    def held_authorization():
        calls.append(threading.get_ident())
        ready.set()
        release.wait(timeout=5)
        return False

    # Isolate the actual production executor and restore any suite-owned pool.
    with patch.object(x_capi, "_authority_lock", threading.Lock()), \
         patch.object(x_capi, "_authority_pid", 0), \
         patch.object(x_capi, "_authority_executor", None), \
         patch.object(x_capi, "_authority_slots", None), \
         patch.object(x_capi, "_send_local_envelope") as send:
        try:
            for _ in range(x_capi._AUTHORITY_MAX_PENDING):
                assert x_capi.emit_binding_nonblocking(context_token=context(held_authorization), wallet_address=WALLET)
            assert ready.wait(timeout=2)
            assert not x_capi.emit_binding_nonblocking(context_token=context(held_authorization), wallet_address=WALLET)
            assert all(tid != threading.get_ident() for tid in calls)
            assert len(calls) <= x_capi._AUTHORITY_MAX_WORKERS
        finally:
            release.set()
            if x_capi._authority_executor:
                x_capi._authority_executor.shutdown(wait=True)
        send.assert_not_called()


def test_queue_mutex_contention_drops_immediately():
    mutex = threading.Lock()
    mutex.acquire()
    with patch.object(x_capi, "_authority_lock", mutex):
        assert not x_capi.emit_binding_nonblocking(context_token=context(), wallet_address=WALLET)
    mutex.release()


def test_fork_reset_discards_inherited_executor_locks_and_pending_callbacks():
    inherited = threading.Lock()
    inherited.acquire()
    with patch.object(x_capi, "_authority_lock", inherited), \
         patch.object(x_capi, "_authority_pid", os.getpid()), \
         patch.object(x_capi, "_authority_executor", Mock()), \
         patch.object(x_capi, "_authority_slots", Mock()):
        x_capi._reset_consent_rpc_after_fork()
        assert x_capi._authority_executor is None
        assert x_capi._authority_slots is None
        assert x_capi._authority_pid == 0
        assert x_capi._authority_lock.acquire(blocking=False)
        x_capi._authority_lock.release()
    inherited.release()


def test_protocol_change_alters_audience_scope_without_changing_operator_configuration():
    current = x_capi.load_config()
    with patch.object(x_capi, "_WALLET_AUTHORITY_PROTOCOL", "prior-protocol"):
        old = x_capi.load_config()
    assert current.policy_epoch == old.policy_epoch
    assert current.audience_scope != old.audience_scope


@pytest.mark.parametrize("complete", [False, True])
def test_slow_prior_ingest_cannot_extend_wallet_proof_lifetime(complete):
    listener = Mock()
    records = [signed(), signed()]
    listener.receive_batch.return_value = records
    listener._receive_batch_state.return_value = (records, True)
    times = []

    def consume(_conn, document, now):
        times.append(now)
        return "accepted" if x_capi.wallet_authority_valid(document, now) else "expired"

    with patch.object(x_capi_worker.time, "monotonic", side_effect=[100.0, 100.0, 131.0]), \
         patch.object(x_capi_worker, "process_ingest_envelope", side_effect=consume):
        result = (x_capi_worker.drain_ingest_until_empty(listener, Mock(), NOW)[0]
                  if complete else x_capi_worker.drain_ingest(listener, Mock(), NOW))
    assert times == [NOW, NOW + 31]
    assert result == {"accepted": 1, "expired": 1}


# Real token-row ordering is covered by test_x_capi_human_authority_postgres.
