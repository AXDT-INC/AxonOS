"""Frozen handoff-v2 bytes, validation and worker-only freshness authority.

The embedded schemas are an exact data copy of the frozen reviewed contract.
They avoid a deployment dependency on an unmounted repository directory or an
unbounded/network-capable schema resolver. The deliberately small interpreter
rejects unsupported keywords; tests compare every embedded schema to its source.
No default keys, positive clock assurance or production-enable decision exists.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

PROTOCOL = "axonos-first-party-handoff-v2"
DOMAIN = "AXONOS-X-HANDOFF-V2"
SOURCE_PATH = "/api/x-attribution/handoff/source"
APP_ORIGIN = "https://app.axonos.io"
LANDING_ORIGIN = "https://axonos.io"
APP_COOKIE = "__Host-axon_x_a"
BODY_MAX = 8192
RPC_MAX = 12288
LEASE_MS = 30000
RENEW_INTERVAL_MS = 10000
TIMEOUT_SECONDS = 2.0
DEFAULT_SOCKET = "/run/axonos-x-capi/handoff.sock"
ERROR_STATUS = {
    **dict.fromkeys(("INVALID_REQUEST", "INVALID_CLICK", "INVALID_RECEIPT"), 400),
    "SERVICE_AUTH_FAILED": 401,
    **dict.fromkeys(("ORIGIN_REJECTED", "OWNER_REQUIRED", "OWNER_MISMATCH", "GPC_ACTIVE"), 403),
    **dict.fromkeys(("FIRST_TOUCH_LOCKED", "IDEMPOTENCY_CONFLICT", "SOURCE_ALREADY_ASSIGNED", "RESULT_UNAVAILABLE", "VERSION_MISMATCH"), 409),
    **dict.fromkeys(("EXPIRED", "WITHDRAWN", "GENERATION_RETIRED"), 410),
    "BODY_TOO_LARGE": 413, "UNSUPPORTED_MEDIA_TYPE": 415, "RATE_LIMITED": 429,
    **dict.fromkeys(("ATTRIBUTION_DISABLED", "DEPENDENCY_UNAVAILABLE", "CLOCK_UNTRUSTED", "QUARANTINED"), 503),
}


class HandoffError(Exception):
    """Public bounded error; never carry raw identifiers or internal exceptions."""
    def __init__(self, code: str, request_id: Optional[str] = None, *, durable_error: bool = False):
        if code not in ERROR_STATUS:
            raise ValueError("unknown_handoff_error")
        self.code = code
        self.status = ERROR_STATUS[code]
        self.retryable = self.status in (429, 503)
        self.request_id = request_id
        self.durable_error = durable_error
        super().__init__(code)

    def body(self, request_id: Optional[str] = None) -> dict:
        candidate = request_id if request_id is not None else self.request_id
        if candidate is not None and not _matches(_UUID, candidate):
            candidate = None
        return {"v": 2, "ok": False, "error": self.code, "retryable": self.retryable, "request_id": candidate}


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_KID = r"[A-Za-z0-9_-]{1,32}"
_TOKEN = r"[A-Za-z0-9_-]{43}"
_HASH = r"[0-9a-f]{64}"


def _matches(pattern: str, value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value, flags=re.ASCII) is not None


def _reject_number(value):
    raise ValueError("non_integer_number")


def _parse_integer(value):
    if value == "-0" or len(value) > 17:
        raise ValueError("non_minimal_or_unbounded_integer")
    result = int(value)
    if abs(result) > 9007199254740991:
        raise ValueError("integer_out_of_range")
    return result


def _pairs(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate_key")
        output[key] = value
    return output


def _ascii_tree(value, depth=0):
    if depth > 16:
        raise ValueError("json_too_deep")
    if isinstance(value, str):
        value.encode("ascii")
    elif value is None or type(value) is bool:
        pass
    elif type(value) is int:
        if abs(value) > 9007199254740991:
            raise ValueError("integer_out_of_range")
    elif isinstance(value, list):
        for child in value:
            _ascii_tree(child, depth + 1)
    elif isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("nonstring_key")
            key.encode("ascii")
            _ascii_tree(child, depth + 1)
    else:
        raise ValueError("invalid_json_type")


def canonical_json(value: Any) -> bytes:
    try:
        _ascii_tree(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise HandoffError("INVALID_REQUEST") from exc


def strict_json_loads(raw: bytes, *, canonical: bool = False, max_bytes: int = BODY_MAX) -> Any:
    if not isinstance(raw, bytes):
        raise HandoffError("INVALID_REQUEST")
    if len(raw) > max_bytes:
        raise HandoffError("BODY_TOO_LARGE")
    try:
        if not raw:
            raise ValueError("empty")
        result = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs, parse_float=_reject_number,
                            parse_constant=_reject_number, parse_int=_parse_integer)
        _ascii_tree(result)
        if canonical and canonical_json(result) != raw:
            raise ValueError("noncanonical")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise HandoffError("INVALID_REQUEST") from exc


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str, *, length: Optional[int] = None, max_chars: int = BODY_MAX) -> bytes:
    if not isinstance(text, str) or not 1 <= len(text) <= max_chars or re.fullmatch(r"[A-Za-z0-9_-]+", text, re.ASCII) is None:
        raise HandoffError("INVALID_REQUEST")
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise HandoffError("INVALID_REQUEST") from exc
    if b64url_encode(raw) != text or (length is not None and len(raw) != length):
        raise HandoffError("INVALID_REQUEST")
    return raw


def new_token() -> str:
    return b64url_encode(secrets.token_bytes(32))


def token_hash(token: str, purpose: str) -> str:
    if purpose not in ("landing-owner", "app-owner", "handoff"):
        raise ValueError("invalid_hash_purpose")
    b64url_decode(token, length=32, max_chars=43)
    return hashlib.sha256((DOMAIN + "\n" + purpose + "\n" + token).encode("ascii")).hexdigest()


def parse_owner_cookie(header: Optional[str]) -> Optional[str]:
    if header is None or header == "":
        return None
    if not isinstance(header, str) or len(header) > BODY_MAX or "," in header or any(ord(ch) < 32 or ord(ch) > 126 for ch in header):
        raise HandoffError("OWNER_REQUIRED")
    found = []
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if name == APP_COOKIE:
            if not sep:
                raise HandoffError("OWNER_REQUIRED")
            found.append(value)
    if not found:
        return None
    if len(found) != 1:
        raise HandoffError("OWNER_REQUIRED")
    try:
        b64url_decode(found[0], length=32, max_chars=43)
    except HandoffError as exc:
        raise HandoffError("OWNER_REQUIRED") from exc
    return found[0]


_SCHEMAS = {'app-begin.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                           'additionalProperties': False,
                           'properties': {'choice': {'const': 'grant'},
                                          'destination': {'const': 'desktop'},
                                          'policy': {'const': 'app-x-v2'},
                                          'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                          'v': {'const': 2}},
                           'required': ['v', 'request_id', 'choice', 'policy', 'destination'],
                           'title': 'app-begin',
                           'type': 'object'},
 'app-granted.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                             'additionalProperties': False,
                             'properties': {'expires_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                            'handoff': {'$ref': 'common.schema.json#/definitions/Token'},
                                            'ok': {'const': True},
                                            'state': {'const': 'app_granted'},
                                            'v': {'const': 2}},
                             'required': ['v', 'ok', 'state', 'handoff', 'expires_at_ms'],
                             'title': 'app-granted',
                             'type': 'object'},
 'app-prepare.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                             'additionalProperties': False,
                             'properties': {'choice': {'const': 'grant'},
                                            'policy': {'const': 'app-x-v2'},
                                            'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                            'v': {'const': 2}},
                             'required': ['v', 'request_id', 'choice', 'policy'],
                             'title': 'app-prepare',
                             'type': 'object'},
 'app-recover.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                             'additionalProperties': False,
                             'properties': {'handoff': {'$ref': 'common.schema.json#/definitions/Token'},
                                            'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                            'v': {'const': 2}},
                             'required': ['v', 'request_id', 'handoff'],
                             'title': 'app-recover',
                             'type': 'object'},
 'app-withdraw.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                              'additionalProperties': False,
                              'properties': {'reason': {'enum': ['withdrawn', 'gpc', 'denied'],
                                                        'type': 'string'},
                                             'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                             'v': {'const': 2}},
                              'required': ['v', 'request_id', 'reason'],
                              'title': 'app-withdraw',
                              'type': 'object'},
 'bridge-form.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                             'additionalProperties': False,
                             'properties': {'handoff': {'$ref': 'common.schema.json#/definitions/Token'},
                                            'v': {'const': '2'}},
                             'required': ['v', 'handoff'],
                             'title': 'bridge-form',
                             'type': 'object'},
 'browser-success.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                 'anyOf': [{'$ref': 'prepared.schema.json'},
                                           {'$ref': 'captured.schema.json'},
                                           {'$ref': 'landing-status-success.schema.json'},
                                           {'$ref': 'app-granted.schema.json'},
                                           {'$ref': 'redeemed.schema.json'},
                                           {'$ref': 'withdrawal-pending.schema.json'},
                                           {'$ref': 'withdrawn.schema.json'}],
                                 'title': 'browser-success'},
 'captured.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                          'additionalProperties': False,
                          'properties': {'expires_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                         'ok': {'const': True},
                                         'state': {'const': 'captured'},
                                         'v': {'const': 2}},
                          'required': ['v', 'ok', 'state', 'expires_at_ms'],
                          'title': 'captured',
                          'type': 'object'},
 'common.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                        'definitions': {'Authority': {'maxLength': 1536,
                                                      'minLength': 64,
                                                      'pattern': '^[A-Za-z0-9_-]+$(?![\\s\\S])',
                                                      'type': 'string'},
                                        'CSRF': {'maxLength': 128,
                                                 'minLength': 32,
                                                 'pattern': '^[A-Za-z0-9_-]+$(?![\\s\\S])',
                                                 'type': 'string'},
                                        'Click': {'description': 'Syntax only. Existing validate_twclid '
                                                                 'pinned grammar and identifier-shape '
                                                                 'exclusions are mandatory semantic checks.',
                                                  'maxLength': 256,
                                                  'minLength': 8,
                                                  'pattern': '^[A-Za-z0-9_-]+$(?![\\s\\S])',
                                                  'type': 'string'},
                                        'Context': {'maxLength': 2048,
                                                    'minLength': 32,
                                                    'pattern': '^[A-Za-z0-9_-]+$(?![\\s\\S])',
                                                    'type': 'string'},
                                        'Counter': {'maximum': 9007199254740991,
                                                    'minimum': 0,
                                                    'type': 'integer'},
                                        'Epoch': {'maximum': 2147483647, 'minimum': 1, 'type': 'integer'},
                                        'Hash': {'pattern': '^[0-9a-f]{64}$(?![\\s\\S])', 'type': 'string'},
                                        'KeyId': {'pattern': '^[A-Za-z0-9_-]{1,32}$(?![\\s\\S])',
                                                  'type': 'string'},
                                        'Ms': {'maximum': 9007199254740991, 'minimum': 1, 'type': 'integer'},
                                        'Policy': {'pattern': '^[A-Za-z0-9._-]{1,64}$(?![\\s\\S])',
                                                   'type': 'string'},
                                        'Token': {'pattern': '^[A-Za-z0-9_-]{43}$(?![\\s\\S])',
                                                  'type': 'string'},
                                        'UUID': {'pattern': '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$(?![\\s\\S])',
                                                 'type': 'string'},
                                        'Wallet': {'pattern': '^0x[0-9a-f]{40}$(?![\\s\\S])',
                                                   'type': 'string'}},
                        'title': 'common'},
 'error.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                       'additionalProperties': False,
                       'properties': {'error': {'enum': ['INVALID_REQUEST',
                                                         'INVALID_CLICK',
                                                         'INVALID_RECEIPT',
                                                         'SERVICE_AUTH_FAILED',
                                                         'ORIGIN_REJECTED',
                                                         'OWNER_REQUIRED',
                                                         'OWNER_MISMATCH',
                                                         'GPC_ACTIVE',
                                                         'FIRST_TOUCH_LOCKED',
                                                         'IDEMPOTENCY_CONFLICT',
                                                         'SOURCE_ALREADY_ASSIGNED',
                                                         'RESULT_UNAVAILABLE',
                                                         'VERSION_MISMATCH',
                                                         'EXPIRED',
                                                         'WITHDRAWN',
                                                         'GENERATION_RETIRED',
                                                         'BODY_TOO_LARGE',
                                                         'UNSUPPORTED_MEDIA_TYPE',
                                                         'RATE_LIMITED',
                                                         'ATTRIBUTION_DISABLED',
                                                         'DEPENDENCY_UNAVAILABLE',
                                                         'CLOCK_UNTRUSTED',
                                                         'QUARANTINED'],
                                                'type': 'string'},
                                      'ok': {'const': False},
                                      'request_id': {'anyOf': [{'$ref': 'common.schema.json#/definitions/UUID'},
                                                               {'type': 'null'}]},
                                      'retryable': {'type': 'boolean'},
                                      'v': {'const': 2}},
                       'required': ['v', 'ok', 'error', 'retryable', 'request_id'],
                       'title': 'error',
                       'type': 'object'},
 'f2-positive-ingestion.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                       'description': 'Envelope schema only; existing independent '
                                                      'exact-bearer F2 proof and all imported-path semantic '
                                                      'checks are mandatory. No wallet/hash fields are sent '
                                                      'to X.',
                                       'oneOf': [{'additionalProperties': False,
                                                  'properties': {'action': {'const': 'bind'},
                                                                 'browser_owner_hash': {'$ref': 'common.schema.json#/definitions/Hash'},
                                                                 'context_token': {'$ref': 'common.schema.json#/definitions/Context'},
                                                                 'event_timestamp_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                                                 'v': {'const': 2},
                                                                 'wallet_address': {'$ref': 'common.schema.json#/definitions/Wallet'},
                                                                 'wallet_authority': {'$ref': 'common.schema.json#/definitions/Authority'}},
                                                  'required': ['v',
                                                               'context_token',
                                                               'wallet_address',
                                                               'event_timestamp_ms',
                                                               'wallet_authority',
                                                               'browser_owner_hash',
                                                               'action'],
                                                  'type': 'object'},
                                                 {'additionalProperties': False,
                                                  'properties': {'action': {'const': 'event'},
                                                                 'browser_owner_hash': {'$ref': 'common.schema.json#/definitions/Hash'},
                                                                 'context_token': {'$ref': 'common.schema.json#/definitions/Context'},
                                                                 'event_timestamp_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                                                 'metadata': {'additionalProperties': False,
                                                                              'properties': {'allow_context_binding': {'const': True},
                                                                                             'chain_id': {'type': 'null'},
                                                                                             'credit_source': {'type': 'null'},
                                                                                             'payment_rail': {'type': 'null'}},
                                                                              'required': ['allow_context_binding',
                                                                                           'credit_source',
                                                                                           'payment_rail',
                                                                                           'chain_id'],
                                                                              'type': 'object'},
                                                                 'milestone': {'const': 'wallet_verified'},
                                                                 'source_key': {'$ref': 'common.schema.json#/definitions/Wallet'},
                                                                 'v': {'const': 2},
                                                                 'wallet_address': {'$ref': 'common.schema.json#/definitions/Wallet'},
                                                                 'wallet_authority': {'$ref': 'common.schema.json#/definitions/Authority'}},
                                                  'required': ['v',
                                                               'context_token',
                                                               'wallet_address',
                                                               'event_timestamp_ms',
                                                               'wallet_authority',
                                                               'browser_owner_hash',
                                                               'action',
                                                               'milestone',
                                                               'source_key',
                                                               'metadata'],
                                                  'type': 'object'},
                                                 {'additionalProperties': False,
                                                  'properties': {'action': {'const': 'event'},
                                                                 'browser_owner_hash': {'$ref': 'common.schema.json#/definitions/Hash'},
                                                                 'context_token': {'$ref': 'common.schema.json#/definitions/Context'},
                                                                 'event_timestamp_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                                                 'metadata': {'additionalProperties': False,
                                                                              'properties': {'allow_context_binding': {'const': False},
                                                                                             'chain_id': {'enum': [1,
                                                                                                                   8453],
                                                                                                          'type': 'integer'},
                                                                                             'credit_source': {'const': 'onchain'},
                                                                                             'payment_rail': {'enum': ['axgt',
                                                                                                                       'eth',
                                                                                                                       'usdc'],
                                                                                                              'type': 'string'}},
                                                                              'required': ['allow_context_binding',
                                                                                           'credit_source',
                                                                                           'payment_rail',
                                                                                           'chain_id'],
                                                                              'type': 'object'},
                                                                 'milestone': {'const': 'deposit_completed'},
                                                                 'source_key': {'pattern': '^0x[0-9a-f]{64}$(?![\\s\\S])',
                                                                                'type': 'string'},
                                                                 'v': {'const': 2},
                                                                 'wallet_address': {'$ref': 'common.schema.json#/definitions/Wallet'},
                                                                 'wallet_authority': {'$ref': 'common.schema.json#/definitions/Authority'}},
                                                  'required': ['v',
                                                               'context_token',
                                                               'wallet_address',
                                                               'event_timestamp_ms',
                                                               'wallet_authority',
                                                               'browser_owner_hash',
                                                               'action',
                                                               'milestone',
                                                               'source_key',
                                                               'metadata'],
                                                  'type': 'object'},
                                                 {'additionalProperties': False,
                                                  'properties': {'action': {'const': 'event'},
                                                                 'browser_owner_hash': {'$ref': 'common.schema.json#/definitions/Hash'},
                                                                 'context_token': {'$ref': 'common.schema.json#/definitions/Context'},
                                                                 'event_timestamp_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                                                 'metadata': {'additionalProperties': False,
                                                                              'properties': {'allow_context_binding': {'const': False},
                                                                                             'chain_id': {'type': 'null'},
                                                                                             'credit_source': {'type': 'null'},
                                                                                             'payment_rail': {'type': 'null'}},
                                                                              'required': ['allow_context_binding',
                                                                                           'credit_source',
                                                                                           'payment_rail',
                                                                                           'chain_id'],
                                                                              'type': 'object'},
                                                                 'milestone': {'const': 'session_started'},
                                                                 'source_key': {'pattern': '^[1-9][0-9]{0,18}$(?![\\s\\S])',
                                                                                'type': 'string'},
                                                                 'v': {'const': 2},
                                                                 'wallet_address': {'$ref': 'common.schema.json#/definitions/Wallet'},
                                                                 'wallet_authority': {'$ref': 'common.schema.json#/definitions/Authority'}},
                                                  'required': ['v',
                                                               'context_token',
                                                               'wallet_address',
                                                               'event_timestamp_ms',
                                                               'wallet_authority',
                                                               'browser_owner_hash',
                                                               'action',
                                                               'milestone',
                                                               'source_key',
                                                               'metadata'],
                                                  'type': 'object'}],
                                       'title': 'f2-positive-ingestion'},
 'landing-capture.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                 'additionalProperties': False,
                                 'properties': {'observation': {'$ref': 'receipt.schema.json'},
                                                'policy': {'const': 'landing-x-v2'},
                                                'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                'twclid': {'$ref': 'common.schema.json#/definitions/Click'},
                                                'v': {'const': 2}},
                                 'required': ['v', 'request_id', 'policy', 'observation', 'twclid'],
                                 'title': 'landing-capture',
                                 'type': 'object'},
 'landing-prepare.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                 'additionalProperties': False,
                                 'properties': {'choice': {'const': 'grant'},
                                                'observation': {'$ref': 'receipt.schema.json'},
                                                'policy': {'const': 'landing-x-v2'},
                                                'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                'v': {'const': 2}},
                                 'required': ['v', 'request_id', 'choice', 'policy', 'observation'],
                                 'title': 'landing-prepare',
                                 'type': 'object'},
 'landing-status-success.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                        'additionalProperties': False,
                                        'properties': {'expires_at_ms': {'anyOf': [{'$ref': 'common.schema.json#/definitions/Ms'},
                                                                                   {'type': 'null'}]},
                                                       'ok': {'const': True},
                                                       'state': {'enum': ['none',
                                                                          'prepared',
                                                                          'capture_pending',
                                                                          'captured',
                                                                          'withdrawal_pending',
                                                                          'withdrawn',
                                                                          'expired'],
                                                                 'type': 'string'},
                                                       'v': {'const': 2}},
                                        'required': ['v', 'ok', 'state', 'expires_at_ms'],
                                        'title': 'landing-status-success',
                                        'type': 'object'},
 'landing-status.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                'additionalProperties': False,
                                'properties': {'v': {'const': 2}},
                                'required': ['v'],
                                'title': 'landing-status',
                                'type': 'object'},
 'landing-withdraw.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                  'additionalProperties': False,
                                  'properties': {'reason': {'enum': ['withdrawn', 'gpc', 'denied'],
                                                            'type': 'string'},
                                                 'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                 'v': {'const': 2}},
                                  'required': ['v', 'request_id', 'reason'],
                                  'title': 'landing-withdraw',
                                  'type': 'object'},
 'observation.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                             'additionalProperties': False,
                             'properties': {'click_commitment': {'$ref': 'common.schema.json#/definitions/Hash'},
                                            'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                            'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                            'observation_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                            'observed_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                            'origin': {'const': 'https://axonos.io'},
                                            'original_expires_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                            'policy': {'const': 'landing-x-v2'},
                                            'receipt_expires_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                            'v': {'const': 2}},
                             'required': ['v',
                                          'epoch',
                                          'generation',
                                          'observation_id',
                                          'origin',
                                          'policy',
                                          'observed_at_ms',
                                          'receipt_expires_at_ms',
                                          'original_expires_at_ms',
                                          'click_commitment'],
                             'title': 'observation',
                             'type': 'object'},
 'prepared.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                          'additionalProperties': False,
                          'properties': {'expires_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                         'ok': {'const': True},
                                         'state': {'const': 'prepared'},
                                         'v': {'const': 2}},
                          'required': ['v', 'ok', 'state', 'expires_at_ms'],
                          'title': 'prepared',
                          'type': 'object'},
 'receipt.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                         'additionalProperties': False,
                         'properties': {'kid': {'$ref': 'common.schema.json#/definitions/KeyId'},
                                        'mac': {'$ref': 'common.schema.json#/definitions/Token'},
                                        'payload': {'maxLength': 1536,
                                                    'minLength': 1,
                                                    'pattern': '^[A-Za-z0-9_-]+$(?![\\s\\S])',
                                                    'type': 'string'}},
                         'required': ['kid', 'payload', 'mac'],
                         'title': 'receipt',
                         'type': 'object'},
 'redeemed.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                          'additionalProperties': False,
                          'properties': {'context': {'$ref': 'common.schema.json#/definitions/Context'},
                                         'csrf': {'$ref': 'common.schema.json#/definitions/CSRF'},
                                         'expires_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                         'ok': {'const': True},
                                         'ownership_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                         'state': {'const': 'redeemed'},
                                         'v': {'const': 2}},
                          'required': ['v',
                                       'ok',
                                       'state',
                                       'context',
                                       'csrf',
                                       'ownership_id',
                                       'expires_at_ms'],
                          'title': 'redeemed',
                          'type': 'object'},
 'source-authorize-success.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                          'additionalProperties': False,
                                          'properties': {'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                                         'expires_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                                         'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                         'ok': {'const': True},
                                                         'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                         'state': {'const': 'source_approved'},
                                                         'v': {'const': 2}},
                                          'required': ['v',
                                                       'ok',
                                                       'epoch',
                                                       'generation',
                                                       'request_id',
                                                       'state',
                                                       'expires_at_ms'],
                                          'title': 'source-authorize-success',
                                          'type': 'object'},
 'source-authorize.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                  'additionalProperties': False,
                                  'properties': {'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                                 'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                 'handoff': {'$ref': 'common.schema.json#/definitions/Token'},
                                                 'op': {'const': 'authorize'},
                                                 'owner_hash': {'$ref': 'common.schema.json#/definitions/Hash'},
                                                 'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                 'v': {'const': 2}},
                                  'required': ['v',
                                               'epoch',
                                               'generation',
                                               'request_id',
                                               'op',
                                               'owner_hash',
                                               'handoff'],
                                  'title': 'source-authorize',
                                  'type': 'object'},
 'source-capture-success.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                        'additionalProperties': False,
                                        'properties': {'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                                       'expires_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                                       'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                       'ok': {'const': True},
                                                       'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                       'state': {'const': 'captured'},
                                                       'v': {'const': 2}},
                                        'required': ['v',
                                                     'ok',
                                                     'epoch',
                                                     'generation',
                                                     'request_id',
                                                     'state',
                                                     'expires_at_ms'],
                                        'title': 'source-capture-success',
                                        'type': 'object'},
 'source-capture.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                'additionalProperties': False,
                                'properties': {'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                               'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                               'gpc': {'const': False},
                                               'landing_consented_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                               'observation_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                               'observed_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                               'op': {'const': 'capture'},
                                               'original_expires_at_ms': {'$ref': 'common.schema.json#/definitions/Ms'},
                                               'owner_hash': {'$ref': 'common.schema.json#/definitions/Hash'},
                                               'policy': {'const': 'landing-x-v2'},
                                               'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                               'twclid': {'$ref': 'common.schema.json#/definitions/Click'},
                                               'v': {'const': 2}},
                                'required': ['v',
                                             'epoch',
                                             'generation',
                                             'request_id',
                                             'op',
                                             'owner_hash',
                                             'observation_id',
                                             'observed_at_ms',
                                             'original_expires_at_ms',
                                             'landing_consented_at_ms',
                                             'policy',
                                             'gpc',
                                             'twclid'],
                                'title': 'source-capture',
                                'type': 'object'},
 'source-lease-attest-success.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                             'additionalProperties': False,
                                             'properties': {'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                                            'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                            'ok': {'const': True},
                                                            'remaining_ms': {'maximum': 30000,
                                                                             'minimum': 1,
                                                                             'type': 'integer'},
                                                            'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                            'state': {'const': 'lease_active'},
                                                            'v': {'const': 2}},
                                             'required': ['v',
                                                          'ok',
                                                          'epoch',
                                                          'generation',
                                                          'request_id',
                                                          'state',
                                                          'remaining_ms'],
                                             'title': 'source-lease-attest-success',
                                             'type': 'object'},
 'source-lease-attest.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                     'additionalProperties': False,
                                     'properties': {'challenge': {'$ref': 'common.schema.json#/definitions/Token'},
                                                    'control_version': {'$ref': 'common.schema.json#/definitions/Counter'},
                                                    'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                                    'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                    'op': {'const': 'lease_attest'},
                                                    'ready': {'const': True},
                                                    'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                    'v': {'const': 2},
                                                    'worker_boot': {'$ref': 'common.schema.json#/definitions/UUID'}},
                                     'required': ['v',
                                                  'epoch',
                                                  'generation',
                                                  'request_id',
                                                  'op',
                                                  'worker_boot',
                                                  'challenge',
                                                  'control_version',
                                                  'ready'],
                                     'title': 'source-lease-attest',
                                     'type': 'object'},
 'source-lease-challenge-success.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                                'additionalProperties': False,
                                                'properties': {'challenge': {'$ref': 'common.schema.json#/definitions/Token'},
                                                               'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                                               'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                               'lease_ms': {'const': 30000},
                                                               'ok': {'const': True},
                                                               'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                               'state': {'const': 'challenge'},
                                                               'v': {'const': 2},
                                                               'worker_boot': {'$ref': 'common.schema.json#/definitions/UUID'}},
                                                'required': ['v',
                                                             'ok',
                                                             'epoch',
                                                             'generation',
                                                             'request_id',
                                                             'state',
                                                             'worker_boot',
                                                             'challenge',
                                                             'lease_ms'],
                                                'title': 'source-lease-challenge-success',
                                                'type': 'object'},
 'source-lease-challenge.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                        'additionalProperties': False,
                                        'properties': {'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                                       'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                       'op': {'const': 'lease_challenge'},
                                                       'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                       'v': {'const': 2}},
                                        'required': ['v', 'epoch', 'generation', 'request_id', 'op'],
                                        'title': 'source-lease-challenge',
                                        'type': 'object'},
 'source-request.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                'oneOf': [{'$ref': 'source-capture.schema.json'},
                                          {'$ref': 'source-status.schema.json'},
                                          {'$ref': 'source-authorize.schema.json'},
                                          {'$ref': 'source-revoke.schema.json'},
                                          {'$ref': 'source-lease-challenge.schema.json'},
                                          {'$ref': 'source-lease-attest.schema.json'}],
                                'title': 'source-request'},
 'source-revoke-success.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                       'additionalProperties': False,
                                       'properties': {'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                                      'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                      'ok': {'const': True},
                                                      'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                      'state': {'const': 'withdrawn'},
                                                      'v': {'const': 2}},
                                       'required': ['v', 'ok', 'epoch', 'generation', 'request_id', 'state'],
                                       'title': 'source-revoke-success',
                                       'type': 'object'},
 'source-revoke.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                               'additionalProperties': False,
                               'properties': {'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                              'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                              'op': {'const': 'revoke'},
                                              'owner_hash': {'$ref': 'common.schema.json#/definitions/Hash'},
                                              'reason': {'enum': ['withdrawn', 'gpc', 'denied'],
                                                         'type': 'string'},
                                              'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                              'v': {'const': 2}},
                               'required': ['v',
                                            'epoch',
                                            'generation',
                                            'request_id',
                                            'op',
                                            'owner_hash',
                                            'reason'],
                               'title': 'source-revoke',
                               'type': 'object'},
 'source-status-success.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                       'additionalProperties': False,
                                       'properties': {'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                                      'expires_at_ms': {'anyOf': [{'$ref': 'common.schema.json#/definitions/Ms'},
                                                                                  {'type': 'null'}]},
                                                      'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                      'ok': {'const': True},
                                                      'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                      'state': {'enum': ['none',
                                                                         'captured',
                                                                         'assigned',
                                                                         'consumed',
                                                                         'closed',
                                                                         'expired'],
                                                                'type': 'string'},
                                                      'v': {'const': 2}},
                                       'required': ['v',
                                                    'ok',
                                                    'epoch',
                                                    'generation',
                                                    'request_id',
                                                    'state',
                                                    'expires_at_ms'],
                                       'title': 'source-status-success',
                                       'type': 'object'},
 'source-status.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                               'additionalProperties': False,
                               'properties': {'epoch': {'$ref': 'common.schema.json#/definitions/Epoch'},
                                              'generation': {'$ref': 'common.schema.json#/definitions/UUID'},
                                              'op': {'const': 'status'},
                                              'owner_hash': {'$ref': 'common.schema.json#/definitions/Hash'},
                                              'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                              'v': {'const': 2}},
                               'required': ['v', 'epoch', 'generation', 'request_id', 'op', 'owner_hash'],
                               'title': 'source-status',
                               'type': 'object'},
 'source-success.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                'anyOf': [{'$ref': 'source-capture-success.schema.json'},
                                          {'$ref': 'source-status-success.schema.json'},
                                          {'$ref': 'source-authorize-success.schema.json'},
                                          {'$ref': 'source-revoke-success.schema.json'},
                                          {'$ref': 'source-lease-challenge-success.schema.json'},
                                          {'$ref': 'source-lease-attest-success.schema.json'}],
                                'title': 'source-success'},
 'withdrawal-pending.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                    'additionalProperties': False,
                                    'properties': {'ok': {'const': True},
                                                   'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                                   'state': {'const': 'withdrawal_pending'},
                                                   'v': {'const': 2}},
                                    'required': ['v', 'ok', 'state', 'request_id'],
                                    'title': 'withdrawal-pending',
                                    'type': 'object'},
 'withdrawn.schema.json': {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                           'additionalProperties': False,
                           'properties': {'ok': {'const': True},
                                          'request_id': {'$ref': 'common.schema.json#/definitions/UUID'},
                                          'state': {'const': 'withdrawn'},
                                          'v': {'const': 2}},
                           'required': ['v', 'ok', 'state', 'request_id'],
                           'title': 'withdrawn',
                           'type': 'object'}}

_ALLOWED_KEYWORDS = frozenset(("$schema", "title", "description", "definitions", "$ref", "type", "const", "enum", "properties", "required", "additionalProperties", "pattern", "minLength", "maxLength", "minimum", "maximum", "oneOf", "anyOf"))


def _schema_supported(schema):
    if not isinstance(schema, dict) or set(schema) - _ALLOWED_KEYWORDS:
        raise RuntimeError("unsupported_handoff_schema_keyword")
    for name in ("definitions", "properties"):
        for child in schema.get(name, {}).values():
            _schema_supported(child)
    for name in ("oneOf", "anyOf"):
        for child in schema.get(name, []):
            _schema_supported(child)


for _schema in _SCHEMAS.values():
    _schema_supported(_schema)


def _schema_equal(left, right):
    # Python considers True == 1; JSON Schema does not.
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    return left == right


def _schema_matches(schema, value):
    if "$ref" in schema:
        file_name, _, pointer = schema["$ref"].partition("#")
        resolved = _SCHEMAS[file_name]
        for part in pointer.split("/")[1:]:
            resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
        if not _schema_matches(resolved, value):
            return False
    if "oneOf" in schema and sum(_schema_matches(item, value) for item in schema["oneOf"]) != 1:
        return False
    if "anyOf" in schema and not any(_schema_matches(item, value) for item in schema["anyOf"]):
        return False
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if kind and not any({"object": isinstance(value, dict), "string": isinstance(value, str),
                         "integer": type(value) is int, "boolean": type(value) is bool,
                         "null": value is None}.get(candidate, False) for candidate in kinds):
        return False
    if "const" in schema and not _schema_equal(value, schema["const"]):
        return False
    if "enum" in schema and not any(_schema_equal(value, item) for item in schema["enum"]):
        return False
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if set(schema.get("required", ())) - set(value):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(props):
            return False
        if not all(_schema_matches(props[key], item) for key, item in value.items() if key in props):
            return False
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 9007199254740991):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value, flags=re.ASCII) is None:
            return False
    if type(value) is int and not schema.get("minimum", -9007199254740991) <= value <= schema.get("maximum", 9007199254740991):
        return False
    return True


def validate_schema(name: str, value: Any) -> None:
    name = name if name.endswith(".schema.json") else name + ".schema.json"
    if name not in _SCHEMAS:
        raise ValueError("unknown_handoff_schema")
    try:
        _ascii_tree(value)
        if not _schema_matches(_SCHEMAS[name], value):
            raise ValueError("schema_mismatch")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise HandoffError("INVALID_REQUEST") from exc


@dataclass(frozen=True)
class ServiceKeyring:
    primary_kid: str
    keys: Mapping[str, bytes]

    def __post_init__(self):
        if not _matches(_KID, self.primary_kid) or not 1 <= len(self.keys) <= 8 or self.primary_kid not in self.keys:
            raise ValueError("invalid_service_keyring")
        clean = {}
        for kid, key in self.keys.items():
            if not _matches(_KID, kid) or not isinstance(key, bytes) or len(key) != 32:
                raise ValueError("invalid_service_keyring")
            clean[kid] = bytes(key)
        from types import MappingProxyType
        object.__setattr__(self, "keys", MappingProxyType(clean))

    @classmethod
    def from_file(cls, path: str):
        # Only workers/source clients call this. The gate never loads keys.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o037 or not 1 <= info.st_size <= BODY_MAX:
                raise ValueError("unsafe_keyring_file")
            raw = os.read(fd, BODY_MAX + 1)
        finally:
            os.close(fd)
        doc = strict_json_loads(raw)
        if not isinstance(doc, dict) or set(doc) != {"primary_kid", "keys"} or not isinstance(doc["keys"], list) or not 1 <= len(doc["keys"]) <= 8:
            raise ValueError("invalid_service_keyring")
        keys = {}
        for item in doc["keys"]:
            if not isinstance(item, dict) or set(item) != {"kid", "key_base64url"} or not _matches(_KID, item["kid"]) or item["kid"] in keys:
                raise ValueError("invalid_service_keyring")
            keys[item["kid"]] = b64url_decode(item["key_base64url"], length=32, max_chars=43)
        return cls(doc["primary_kid"], keys)

    def _key(self, kid):
        if not _matches(_KID, kid) or kid not in self.keys:
            raise HandoffError("SERVICE_AUTH_FAILED")
        return self.keys[kid]

    def request_mac(self, raw: bytes, kid: str, request: Optional[dict] = None, *, method: str = "POST", path: str = SOURCE_PATH) -> str:
        document = request if request is not None else strict_json_loads(raw, canonical=True)
        message = "\n".join((DOMAIN, "request", method, path, kid, str(document["epoch"]), document["generation"], document["request_id"], hashlib.sha256(raw).hexdigest(), ""))
        return b64url_encode(hmac.new(self._key(kid), message.encode("ascii"), hashlib.sha256).digest())

    def authenticate_request(self, raw: bytes, kid: str, mac: str) -> dict:
        document = strict_json_loads(raw, canonical=True)
        # Parse identity before MAC without granting schema/version authority.
        if not isinstance(document, dict) or type(document.get("epoch")) is not int or not 1 <= document["epoch"] <= 2147483647 or not _matches(_UUID, document.get("generation")) or not _matches(_UUID, document.get("request_id")):
            raise HandoffError("SERVICE_AUTH_FAILED")
        try:
            received = b64url_decode(mac, length=32, max_chars=43)
            expected = b64url_decode(self.request_mac(raw, kid, document), length=32)
            if not hmac.compare_digest(received, expected):
                raise ValueError("mac")
        except (ValueError, HandoffError) as exc:
            raise HandoffError("SERVICE_AUTH_FAILED") from exc
        return document

    def verify_request(self, raw: bytes, kid: str, mac: str) -> dict:
        document = self.authenticate_request(raw, kid, mac)
        validate_schema("source-request", document)
        return document

    def response_mac(self, request_raw: bytes, status: int, response_raw: bytes, kid: str, request: Optional[dict] = None) -> str:
        document = request if request is not None else strict_json_loads(request_raw, canonical=True)
        message = "\n".join((DOMAIN, "response", hashlib.sha256(request_raw).hexdigest(), str(status), kid, str(document["epoch"]), document["generation"], document["request_id"], hashlib.sha256(response_raw).hexdigest(), ""))
        return b64url_encode(hmac.new(self._key(kid), message.encode("ascii"), hashlib.sha256).digest())

    def sign_response(self, request_raw: bytes, status: int, payload: dict, kid: str) -> dict:
        raw = canonical_json(payload)
        if len(raw) > BODY_MAX:
            raise HandoffError("DEPENDENCY_UNAVAILABLE")
        return {"X-AxonOS-Handoff-Key-Id": kid, "X-AxonOS-Handoff-MAC": self.response_mac(request_raw, status, raw, kid)}

    def verify_response(self, request_raw: bytes, status: int, response_raw: bytes, kid: str, mac: str) -> dict:
        document = strict_json_loads(response_raw, canonical=True)
        try:
            received = b64url_decode(mac, length=32, max_chars=43)
            expected = b64url_decode(self.response_mac(request_raw, status, response_raw, kid), length=32)
            if not hmac.compare_digest(received, expected):
                raise ValueError("mac")
        except (ValueError, HandoffError) as exc:
            raise HandoffError("SERVICE_AUTH_FAILED") from exc
        validate_schema("source-success" if status == 200 else "error", document)
        request = strict_json_loads(request_raw, canonical=True)
        if document.get("request_id") != request["request_id"] or (status == 200 and (document.get("epoch"), document.get("generation")) != (request["epoch"], request["generation"])):
            raise HandoffError("SERVICE_AUTH_FAILED")
        return document


def boottime_ms() -> int:
    if not hasattr(time, "CLOCK_BOOTTIME"):
        raise HandoffError("CLOCK_UNTRUSTED")
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1000000


LEASE_LEDGER_LIMIT = 65536
LEASE_LEDGER_HIGH_WATER = 61440


class FreshnessLease:
    """One worker-local lease; no mutex spans DB, RPC or external transport.

    Call allows_dispatch inside the existing exclusive privacy fence immediately
    before dispatch. Durable generation/control-version guards are checked by the
    worker before these methods; this object never substitutes for PostgreSQL.
    """
    def __init__(self, epoch: int, generation: str, *, elapsed_ms: Callable[[], int] = boottime_ms):
        if type(epoch) is not int or not 1 <= epoch <= 2147483647 or not _matches(_UUID, generation):
            raise ValueError("invalid_lease_scope")
        self.epoch, self.generation = epoch, generation
        self.worker_boot = str(uuid.uuid4())
        self._clock = elapsed_ms
        self._lock = threading.Lock()
        self._pending = None
        self._active = None
        self._version = 0
        self._last_elapsed = None
        self._seen_challenges = {}  # retained until this boot is drained and replaced
        self._retired = False

    def _now(self):
        try:
            now = self._clock()
            if type(now) is not int or now < 0 or (self._last_elapsed is not None and now < self._last_elapsed):
                raise ValueError("clock_regression")
            self._last_elapsed = now
            return now
        except Exception as exc:
            self._pending = self._active = None
            raise HandoffError("CLOCK_UNTRUSTED") from exc

    def _scope(self, request):
        if request["generation"] != self.generation:
            raise HandoffError("GENERATION_RETIRED")
        if request["epoch"] != self.epoch:
            raise HandoffError("VERSION_MISMATCH")

    def _reply(self, request, state, **fields):
        return dict(v=2, ok=True, epoch=self.epoch, generation=self.generation, request_id=request["request_id"], state=state, **fields)

    def invalidate(self):
        with self._lock:
            self._pending = self._active = None

    def ledger_status(self):
        """Bounded operational metadata; never expose identifiers or authority."""
        with self._lock:
            count = len(self._seen_challenges)
            return {"count": count, "limit": LEASE_LEDGER_LIMIT,
                    "high_water": LEASE_LEDGER_HIGH_WATER,
                    "recycle_required": self._retired or count >= LEASE_LEDGER_HIGH_WATER}

    def retire_boot(self):
        """Irreversible in this object, including for already admitted handlers."""
        with self._lock:
            self._retired = True
            self._pending = self._active = None

    def challenge(self, request: dict) -> dict:
        validate_schema("source-lease-challenge", request)
        self._scope(request)
        with self._lock:
            if self._retired:
                raise HandoffError("RATE_LIMITED")
            now = self._now()
            digest = hashlib.sha256(canonical_json(request)).hexdigest()
            for state in (self._pending, self._active):
                if state is not None and state["challenge_request_id"] == request["request_id"]:
                    if state["request_digest"] != digest:
                        raise HandoffError("IDEMPOTENCY_CONFLICT")
                    if now >= state["deadline"]:
                        raise HandoffError("EXPIRED")
                    return self._reply(request, "challenge", worker_boot=self.worker_boot, challenge=state["token"], lease_ms=LEASE_MS)
            previous = self._seen_challenges.get(request["request_id"])
            if previous is not None:
                raise HandoffError("EXPIRED" if previous == digest else "IDEMPOTENCY_CONFLICT")
            if len(self._seen_challenges) >= LEASE_LEDGER_LIMIT:
                self._active = self._pending = None
                raise HandoffError("RATE_LIMITED")
            self._seen_challenges[request["request_id"]] = digest
            self._pending = {"token": new_token(), "deadline": now + LEASE_MS, "challenge_request_id": request["request_id"], "request_digest": digest}
            return self._reply(request, "challenge", worker_boot=self.worker_boot, challenge=self._pending["token"], lease_ms=LEASE_MS)

    def attest(self, request: dict, *, durable_control_version: int = 0) -> dict:
        validate_schema("source-lease-attest", request)
        self._scope(request)
        with self._lock:
            if self._retired:
                raise HandoffError("EXPIRED")
            now = self._now()
            if type(durable_control_version) is not int or durable_control_version < 0 or request["control_version"] < max(self._version, durable_control_version):
                self._active = self._pending = None
                raise HandoffError("QUARANTINED")
            if request["worker_boot"] != self.worker_boot:
                raise HandoffError("EXPIRED")
            digest = hashlib.sha256(canonical_json(request)).hexdigest()
            if self._active and hmac.compare_digest(self._active["token"], request["challenge"]):
                if self._active["attest_digest"] != digest:
                    raise HandoffError("IDEMPOTENCY_CONFLICT")
                state = self._active
            elif self._pending and hmac.compare_digest(self._pending["token"], request["challenge"]):
                state = dict(self._pending, attest_digest=digest)
            else:
                raise HandoffError("EXPIRED")
            remaining = state["deadline"] - now
            if remaining <= 0:
                raise HandoffError("EXPIRED")
            self._version = max(self._version, request["control_version"], durable_control_version)
            self._active = state
            if self._pending and hmac.compare_digest(self._pending["token"], state["token"]):
                self._pending = None
            return self._reply(request, "lease_active", remaining_ms=remaining)

    def allows_dispatch(self) -> bool:
        with self._lock:
            try:
                return self._active is not None and self._now() < self._active["deadline"]
            except HandoffError:
                return False

    def remaining_ms(self) -> int:
        with self._lock:
            try:
                return max(0, self._active["deadline"] - self._now()) if self._active else 0
            except HandoffError:
                return 0


class LinuxClockAssurance:
    """Read-only kernel UTC-error assurance, with sticky local step detection.

    This trusts the host's separately reviewed time discipline. It cannot detect
    a malicious host or prove external UTC correctness. No syscall changes time.
    Only the reviewed Linux 64-bit timex layout is supported; other hosts close.
    """
    def __init__(self, *, max_error_ms: int = 5000, sample: Optional[Callable] = None,
                 wall_ms: Optional[Callable[[], int]] = None, elapsed_ms: Callable[[], int] = boottime_ms):
        if max_error_ms != 5000:
            raise ValueError("clock_error_profile_mismatch")
        self._bound = max_error_ms
        self._sample = sample or self._kernel_sample
        self._wall = wall_ms or (lambda: time.time_ns() // 1000000)
        self._elapsed = elapsed_ms
        self._previous = None
        self._failed = False
        self._lock = threading.Lock()

    @staticmethod
    def _kernel_sample():
        import ctypes
        import platform
        import sys
        if sys.platform != "linux" or ctypes.sizeof(ctypes.c_long) != 8 or platform.machine() not in ("x86_64", "aarch64"):
            raise RuntimeError("unsupported_timex_layout")

        class Timeval(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]

        class Timex(ctypes.Structure):
            _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
                        ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                        ("constant", ctypes.c_long), ("precision", ctypes.c_long), ("tolerance", ctypes.c_long),
                        ("time", Timeval), ("tick", ctypes.c_long), ("ppsfreq", ctypes.c_long),
                        ("jitter", ctypes.c_long), ("shift", ctypes.c_int), ("stabil", ctypes.c_long),
                        ("jitcnt", ctypes.c_long), ("calcnt", ctypes.c_long), ("errcnt", ctypes.c_long),
                        ("stbcnt", ctypes.c_long), ("tai", ctypes.c_int), ("padding", ctypes.c_int * 11)]

        if ctypes.sizeof(Timex) != 208:
            raise RuntimeError("unsupported_timex_layout")
        libc = ctypes.CDLL(None, use_errno=True)
        call = libc.adjtimex
        call.argtypes, call.restype = [ctypes.POINTER(Timex)], ctypes.c_int
        values = Timex()  # modes=0 is a query; never set adjustment flags.
        result = call(ctypes.byref(values))
        return result, values.status, values.maxerror, values.esterror

    def __call__(self) -> bool:
        with self._lock:
            if self._failed:
                return False
            try:
                result, status, maximum, estimate = self._sample()
                wall, elapsed = self._wall(), self._elapsed()
                if (result not in (0, 1, 2, 3, 4) or status & (0x0040 | 0x1000)
                        or not 0 <= maximum <= self._bound * 1000 or not 0 <= estimate <= self._bound * 1000
                        or type(wall) is not int or type(elapsed) is not int):
                    return False
                if self._previous is not None:
                    old_wall, old_elapsed = self._previous
                    if elapsed < old_elapsed or abs((wall - old_wall) - (elapsed - old_elapsed)) > self._bound:
                        self._failed = True
                        return False
                self._previous = wall, elapsed
                return True
            except Exception:
                return False
