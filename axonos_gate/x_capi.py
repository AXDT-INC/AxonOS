"""Privacy-minimized first-party attribution and local X CAPI producer.

Ordinary core event paths can only emit one bounded, non-blocking Unix datagram
after their authoritative transaction commits. An ordinary request carrying
GPC may additionally publish one complete record in the preallocated privacy
controls under nonblocking locks. If the targeted pool cannot accept a valid
ticket, it trips the preallocated global fail-closed control without fsync or
allocation. Core success never depends on either operation or on CAPI tables.
Public consent is stateless except for bounded local privacy RPCs to a dedicated
worker credential. Only ``x_capi_worker.py`` may access CAPI tables or X.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import socket
import stat
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MILESTONE_WALLET_VERIFIED = "wallet_verified"
MILESTONE_DEPOSIT_COMPLETED = "deposit_completed"
MILESTONE_SESSION_STARTED = "session_started"
MILESTONES = (
    MILESTONE_WALLET_VERIFIED,
    MILESTONE_DEPOSIT_COMPLETED,
    MILESTONE_SESSION_STARTED,
)

_EVENT_ENV = {
    MILESTONE_WALLET_VERIFIED: "X_CAPI_EVENT_WALLET_VERIFIED",
    MILESTONE_DEPOSIT_COMPLETED: "X_CAPI_EVENT_DEPOSIT_COMPLETED",
    MILESTONE_SESSION_STARTED: "X_CAPI_EVENT_SESSION_STARTED",
}
_MODE_VALUES = frozenset({"off", "dry_run", "live"})
LIVE_MODE_UNAVAILABLE_REASON = "live_delivery_blocked_untrusted_twclid_provenance"
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TWCLID_ALPHABETS = {
    "lower_alnum": re.compile(r"^[a-z0-9]+$"),
    "alnum": re.compile(r"^[A-Za-z0-9]+$"),
    "url_safe": re.compile(r"^[A-Za-z0-9._~-]+$"),
}
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_-]{32,2048}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_POLICY_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_WALLET_RE = re.compile(r"^0x[a-f0-9]{40}$")
_WALLET_SHAPE_RE = re.compile(r"^(?:0x)?[a-f0-9]{40}$", re.IGNORECASE)
_DIGEST_SHAPE_RE = re.compile(r"^(?:0x)?[a-f0-9]{64}$", re.IGNORECASE)
_UUID_SHAPE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_WALLET_HANDLE_RE = re.compile(
    r"^[A-Za-z0-9_-]{1,64}\.(?:eth|sol|btc|crypto|wallet)$", re.IGNORECASE
)
_CONTEXT_TICKET_VERSION = 4
_CONTEXT_STATES = frozenset({"unset", "granted", "denied", "revoked"})
_MAX_INGEST_DATAGRAM = 4096
_DEFAULT_INGEST_SOCKET = "/run/axonos-x-capi/events.sock"
_DEFAULT_CONSENT_SOCKET = "/run/axonos-x-capi/consent.sock"
_DEFAULT_CONFIG_GUARD_FILE = "/run/axonos-x-capi/config-guard.json"
_DEFAULT_PRIVACY_FENCE_DIR = "/run/axonos-x-capi-privacy"
_PRIVACY_FENCE_LOCK_NAME = "dispatch.lock"
_PRIVACY_FENCE_LOCK_MAGIC = b"AXCPF001"
_PRIVACY_FENCE_QUARANTINE_NAME = "global-quarantine"
_PRIVACY_FENCE_QUARANTINE_INACTIVE = b"AXCPQ000"
_PRIVACY_FENCE_QUARANTINE_ACTIVE = b"AXCPQ001"
_PRIVACY_FENCE_MARKER_CAP = 1024
_PRIVACY_PENDING_SLOT_COUNT = 64
_PRIVACY_PENDING_SLOT_PREFIX = "pending-"
_PRIVACY_PENDING_SLOT_NAMES = frozenset(
    f"{_PRIVACY_PENDING_SLOT_PREFIX}{index:02d}"
    for index in range(_PRIVACY_PENDING_SLOT_COUNT)
)
_PRIVACY_PENDING_RECORD_BODY_SIZE = 64 + 64 + 8
_PRIVACY_PENDING_EMPTY = b"I:" + (b"0" * _PRIVACY_PENDING_RECORD_BODY_SIZE)
_PRIVACY_PENDING_PREFIX = b"P:"
_PRIVACY_SLOT_ALREADY_FENCED = -1
_PRIVACY_SLOT_DISPATCH_BUSY = -2
_PRIVACY_SLOT_GLOBAL_FENCED = -3
_CONFIG_GUARD_MAX_BYTES = 1024
_CONSENT_RPC_MAX_WORKERS = 2
_CONSENT_RPC_MAX_PENDING = 4
_CONSENT_RPC_WAIT_SECONDS = 0.85
_SECRET_ROOT = "/run/secrets"
_KNOWN_SECRET_PREFIX_RE = re.compile(
    r"^(?:sk-[A-Za-z0-9_-]+|xox[baprs]-|gh[pousr]_|AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.|bearer[._~-]|postgres(?:ql)?[._~:-])",
    re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")
_PHONEISH_RE = re.compile(r"^[0-9][0-9._~-]{6,20}$")
_SOURCE_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_GUEST_WALLET_RE = re.compile(r"^0x6775657374[0-9a-f]{30}$")

_consent_rpc_lock = threading.Lock()
_consent_rpc_pid = 0
_consent_rpc_executor: Optional[ThreadPoolExecutor] = None
_consent_rpc_slots: Optional[threading.BoundedSemaphore] = None
_context_cipher_cache_lock = threading.Lock()
_context_cipher_cache_pid = 0
_context_cipher_cache_initialized = False
_context_cipher_cache = None
_context_cipher_cache_error: Optional[str] = None


def _reset_consent_rpc_after_fork() -> None:
    """Discard inherited thread-pool state in a forked Websockify child."""
    global _consent_rpc_lock, _consent_rpc_pid, _consent_rpc_executor, _consent_rpc_slots
    global _context_cipher_cache_lock, _context_cipher_cache_pid
    _consent_rpc_lock = threading.Lock()
    _consent_rpc_pid = 0
    _consent_rpc_executor = None
    _consent_rpc_slots = None
    # A mutex may be inherited while held by a vanished thread, so always
    # replace it. The validated Fernet tuple itself contains immutable key
    # bytes and no live cipher context; preserve a preloaded tuple so
    # Websockify's fork-per-request model cannot turn crafted bearers into one
    # key-file read plus eight Fernet constructions per child. Rotation already
    # requires an orderly parent restart.
    _context_cipher_cache_lock = threading.Lock()
    _context_cipher_cache_pid = os.getpid() if _context_cipher_cache_initialized else 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_consent_rpc_after_fork)


def _valid_origin(value: str) -> bool:
    try:
        value.encode("ascii")
        if not value or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
            return False
        parsed = urlsplit(value)
        # Accessing ``port`` performs urllib's otherwise-lazy validation.
        parsed.port
    except (UnicodeEncodeError, ValueError):
        return False
    return (
        parsed.scheme in ("https", "http")
        and bool(parsed.hostname)
        and bool(re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", parsed.netloc))
        and not parsed.username
        and not parsed.password
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )


def _valid_vendor_id(value: str) -> bool:
    """Accept path-safe vendor IDs without obvious wallet/credential material."""
    lowered = value.lower()
    try:
        is_ip_address = ipaddress.ip_address(value) is not None
    except ValueError:
        is_ip_address = False
    return bool(
        _ID_RE.fullmatch(value)
        and re.search(r"[A-Za-z0-9]", value)
        and not _WALLET_RE.fullmatch(lowered)
        and not _WALLET_SHAPE_RE.fullmatch(value)
        and not _DIGEST_SHAPE_RE.fullmatch(value)
        and not _UUID_SHAPE_RE.fullmatch(value)
        and not _WALLET_HANDLE_RE.fullmatch(value)
        and not is_ip_address
        and not _KNOWN_SECRET_PREFIX_RE.match(value)
    )


@dataclass(frozen=True)
class Config:
    mode: str
    pixel_id: str
    event_ids: Mapping[str, str]
    attribution_ttl_days: int
    max_event_age_hours: int
    send_values: bool
    allowed_origin: str
    policy_version: str
    policy_epoch: int
    deployment_id: str
    twclid_contract_version: str
    twclid_charset: str
    twclid_min_length: int
    twclid_max_length: int
    audience_scope: str
    queue_limit: int
    context_limit: int
    production_chain_ids: Tuple[int, ...]
    errors: Tuple[str, ...]

    @property
    def producer_ready(self) -> bool:
        # This build has no callable live transport and no normative way to
        # establish that a caller-supplied twclid has authentic X campaign
        # provenance.  Treating live as producer-ready would still mint grant
        # tickets and persist raw click IDs even though delivery can never run.
        return self.mode == "dry_run" and not self.errors


def load_config(environ: Optional[Mapping[str, str]] = None) -> Config:
    """Load and strictly validate non-secret producer configuration."""
    env = os.environ if environ is None else environ

    def get(name: str, default: str = "") -> str:
        return str(env.get(name, default) or "").strip()

    mode = get("X_CAPI_MODE", "off").lower()
    errors = []
    if mode not in _MODE_VALUES:
        errors.append("X_CAPI_MODE must be off, dry_run, or live")
        mode = "off"
    pixel_id = get("X_CAPI_PIXEL_ID")
    if pixel_id and not _valid_vendor_id(pixel_id):
        errors.append("X_CAPI_PIXEL_ID has an invalid format")
    event_ids: Dict[str, str] = {}
    for milestone, name in _EVENT_ENV.items():
        event_id = get(name)
        if event_id and not _valid_vendor_id(event_id):
            errors.append(f"{name} has an invalid format")
        event_ids[milestone] = event_id
    ttl_raw = get("X_CAPI_ATTRIBUTION_TTL_DAYS", "7")
    age_raw = get("X_CAPI_MAX_EVENT_AGE_HOURS", "24")
    limit_raw = get("X_CAPI_QUEUE_LIMIT", "10000")
    context_limit_raw = get("X_CAPI_CONTEXT_LIMIT", "10000")
    try:
        ttl = int(ttl_raw)
        if not 1 <= ttl <= 90:
            raise ValueError
    except ValueError:
        ttl = 7
        errors.append("X_CAPI_ATTRIBUTION_TTL_DAYS must be between 1 and 90")
    try:
        max_age = int(age_raw)
        if not 1 <= max_age <= 24 * 30:
            raise ValueError
    except ValueError:
        max_age = 24
        errors.append("X_CAPI_MAX_EVENT_AGE_HOURS must be between 1 and 720")
    try:
        queue_limit = int(limit_raw)
        if not 100 <= queue_limit <= 1_000_000:
            raise ValueError
    except ValueError:
        queue_limit = 10_000
        errors.append("X_CAPI_QUEUE_LIMIT must be between 100 and 1000000")
    try:
        context_limit = int(context_limit_raw)
        if not 100 <= context_limit <= 1_000_000:
            raise ValueError
    except ValueError:
        context_limit = 10_000
        errors.append("X_CAPI_CONTEXT_LIMIT must be between 100 and 1000000")
    raw_values = get("X_CAPI_SEND_VALUES", "false").lower()
    if raw_values in ("1", "true", "yes", "on"):
        send_values = True
    elif raw_values in ("0", "false", "no", "off"):
        send_values = False
    else:
        send_values = False
        errors.append("X_CAPI_SEND_VALUES must be true or false")
    if send_values:
        # V1 has no typed, frozen revenue amount in the authoritative deposit row.
        errors.append("X_CAPI_SEND_VALUES=true is unsupported until typed value provenance exists")
    allowed_origin_raw = get("X_CAPI_ALLOWED_ORIGIN")
    allowed_origin_valid = bool(allowed_origin_raw and _valid_origin(allowed_origin_raw))
    if allowed_origin_raw and not allowed_origin_valid:
        errors.append("X_CAPI_ALLOWED_ORIGIN must be one exact http(s) origin")
    allowed_origin = allowed_origin_raw.rstrip("/") if allowed_origin_valid else ""
    policy_version = get("X_CAPI_CONSENT_POLICY_VERSION", "x-capi-v1")
    if not _POLICY_RE.fullmatch(policy_version):
        errors.append("X_CAPI_CONSENT_POLICY_VERSION has an invalid format")
    policy_epoch_raw = get("X_CAPI_CONSENT_POLICY_EPOCH")
    try:
        policy_epoch = int(policy_epoch_raw)
        if isinstance(policy_epoch, bool) or not 1 <= policy_epoch <= 2_147_483_647:
            raise ValueError
    except ValueError:
        policy_epoch = 0
        if mode in ("dry_run", "live") or policy_epoch_raw:
            errors.append("X_CAPI_CONSENT_POLICY_EPOCH must be an explicit positive integer")
    deployment_id = get("X_CAPI_DEPLOYMENT_ID")
    if deployment_id and not _POLICY_RE.fullmatch(deployment_id):
        errors.append("X_CAPI_DEPLOYMENT_ID has an invalid format")
    if mode in ("dry_run", "live") and not deployment_id:
        errors.append("X_CAPI_DEPLOYMENT_ID is required")

    contract_version = get("X_CAPI_TWCLID_CONTRACT_VERSION")
    if contract_version and not _POLICY_RE.fullmatch(contract_version):
        errors.append("X_CAPI_TWCLID_CONTRACT_VERSION has an invalid format")
    charset_raw = get("X_CAPI_TWCLID_CHARSET")
    charset = (charset_raw or "url_safe").lower()
    if charset not in _TWCLID_ALPHABETS:
        errors.append("X_CAPI_TWCLID_CHARSET must be lower_alnum, alnum, or url_safe")
        charset = "url_safe"
    min_length_raw = get("X_CAPI_TWCLID_MIN_LENGTH")
    max_length_raw = get("X_CAPI_TWCLID_MAX_LENGTH")
    try:
        twclid_min_length = int(min_length_raw or "8")
        twclid_max_length = int(max_length_raw or "256")
        if not 8 <= twclid_min_length <= twclid_max_length <= 256:
            raise ValueError
    except ValueError:
        twclid_min_length, twclid_max_length = 8, 256
        errors.append("X_CAPI_TWCLID length bounds must satisfy 8 <= min <= max <= 256")
    # X documents this value only as "X generated" and publishes no normative
    # grammar.  Live operation therefore requires an operator-pinned contract;
    # the software must not silently promote its permissive dry-run grammar.
    if mode == "live" and not contract_version:
        errors.append("X_CAPI_TWCLID_CONTRACT_VERSION is required in live mode")
    if mode == "live" and not charset_raw:
        errors.append("X_CAPI_TWCLID_CHARSET must be explicitly pinned in live mode")
    if mode == "live" and (not min_length_raw or not max_length_raw):
        errors.append("X_CAPI_TWCLID length bounds must be explicitly pinned in live mode")
    if mode == "live":
        # Live collection is disabled together with live delivery.  This is an
        # implementation invariant, not an operator-configurable kill switch.
        errors.append(LIVE_MODE_UNAVAILABLE_REASON)
    if mode in ("dry_run", "live"):
        if not pixel_id:
            errors.append("X_CAPI_PIXEL_ID is required")
        if not any(event_ids.values()):
            errors.append("At least one X_CAPI_EVENT_* mapping is required")
        if not allowed_origin:
            errors.append("X_CAPI_ALLOWED_ORIGIN is required")
        elif mode == "live" and not allowed_origin.startswith("https://"):
            errors.append("X_CAPI_ALLOWED_ORIGIN must use https in live mode")
    production_chain_ids = []
    for item in get("X_CAPI_PRODUCTION_CHAIN_IDS", "1,8453").split(","):
        try:
            chain_id = int(item.strip())
        except ValueError:
            chain_id = 0
        if chain_id <= 0 or chain_id in production_chain_ids:
            errors.append("X_CAPI_PRODUCTION_CHAIN_IDS must contain unique positive integers")
            production_chain_ids = []
            break
        production_chain_ids.append(chain_id)

    exclusion_wallets = set()
    exclusion_config_valid = True
    for exclusion_env in (
        "AXGT_REVENUE_WALLET",
        "AXONOS_TEST_CREDIT_WALLETS",
        "AXONOS_WHITELISTED_WALLETS",
        "AXONOS_GUEST_INVITE_MINTERS",
        "X_CAPI_EXCLUDED_WALLETS",
    ):
        exclusion_raw = get(exclusion_env)
        exclusion_items = exclusion_raw.split(",") if exclusion_raw else []
        if len(exclusion_raw) > 4096 or len(exclusion_items) > 256:
            exclusion_config_valid = False
            continue
        for item in exclusion_items:
            normalized = item.strip().lower()
            if normalized:
                if not _WALLET_RE.fullmatch(normalized):
                    exclusion_config_valid = False
                else:
                    exclusion_wallets.add(normalized)
    if not exclusion_config_valid:
        errors.append("X CAPI wallet exclusion sources must contain valid wallet addresses")
    exclusion_scope = hashlib.sha256(
        json.dumps(
            sorted(exclusion_wallets), separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()

    audience_scope = ""
    if deployment_id and allowed_origin and pixel_id and policy_epoch > 0:
        audience_document = json.dumps(
            {
                "v": 1,
                "deployment_id": deployment_id,
                "mode": mode,
                "origin": allowed_origin.rstrip("/"),
                "pixel_id": pixel_id,
                "policy_version": policy_version,
                "attribution_ttl_days": ttl,
                "max_event_age_hours": max_age,
                "send_values": send_values,
                "production_chain_ids": sorted(production_chain_ids),
                "wallet_exclusion_scope": exclusion_scope,
                "event_ids": {
                    milestone: event_ids[milestone]
                    for milestone in sorted(event_ids)
                },
                "twclid_contract_version": contract_version,
                "twclid_charset": charset,
                "twclid_min_length": twclid_min_length,
                "twclid_max_length": twclid_max_length,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        audience_scope = hashlib.sha256(audience_document.encode("ascii")).hexdigest()
    return Config(
        mode=mode,
        pixel_id=pixel_id,
        event_ids=event_ids,
        attribution_ttl_days=ttl,
        max_event_age_hours=max_age,
        send_values=send_values,
        allowed_origin=allowed_origin,
        policy_version=policy_version,
        policy_epoch=policy_epoch,
        deployment_id=deployment_id,
        twclid_contract_version=contract_version,
        twclid_charset=charset,
        twclid_min_length=twclid_min_length,
        twclid_max_length=twclid_max_length,
        audience_scope=audience_scope,
        queue_limit=queue_limit,
        context_limit=context_limit,
        production_chain_ids=tuple(production_chain_ids),
        errors=tuple(errors),
    )


def config_status(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    cfg = load_config(environ)
    disabled = [name for name in MILESTONES if not cfg.event_ids.get(name)]
    return {
        "mode": cfg.mode,
        "producer_ready": cfg.producer_ready,
        "pixel_id_configured": bool(cfg.pixel_id),
        "allowed_origin_configured": bool(cfg.allowed_origin),
        "policy_version": cfg.policy_version,
        "policy_epoch": cfg.policy_epoch,
        "event_mappings": {
            name: "configured" if cfg.event_ids.get(name) else "disabled"
            for name in MILESTONES
        },
        "disabled_milestones": disabled,
        "errors": list(cfg.errors),
        "send_values": cfg.send_values,
        "attribution_ttl_days": cfg.attribution_ttl_days,
        "max_event_age_hours": cfg.max_event_age_hours,
        "production_chain_ids": list(cfg.production_chain_ids),
        "context_limit": cfg.context_limit,
        "deployment_scope_configured": bool(cfg.audience_scope),
        "twclid_contract_configured": bool(cfg.twclid_contract_version),
    }


def _reject_twclid_identifier_shapes(candidate: str) -> bool:
    lowered = candidate.lower()
    if (
        _WALLET_RE.fullmatch(lowered)
        or _WALLET_SHAPE_RE.fullmatch(candidate)
        or _DIGEST_SHAPE_RE.fullmatch(candidate)
    ):
        return True
    if _UUID_RE.fullmatch(candidate) or _IPV4_RE.fullmatch(candidate):
        return True
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        pass
    if _PHONEISH_RE.fullmatch(candidate):
        digits = sum(ch.isdigit() for ch in candidate)
        if 7 <= digits <= 15:
            return True
    if re.fullmatch(r"[0-9a-fA-F]{32,128}", candidate):
        return True
    if _KNOWN_SECRET_PREFIX_RE.match(candidate):
        return True
    if re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_-]{0,62}\.(?:eth|crypto|wallet|sol|btc)",
        candidate,
        re.IGNORECASE,
    ):
        return True
    return False


def _validate_twclid_unscoped(value: Any) -> Optional[str]:
    """Parse an old ticket without applying today's campaign contract."""
    raw_candidate = str(value or "")
    candidate = raw_candidate.strip()
    # Never canonicalize control/whitespace around an identifier into a value
    # that this service would accept.  Besides making the validation contract
    # ambiguous across HTTP stacks, doing so could turn an encoded-confusion
    # input into a durable advertising identifier.
    if candidate != raw_candidate:
        return None
    if not 8 <= len(candidate) <= 256 or not _TWCLID_ALPHABETS["url_safe"].fullmatch(candidate):
        return None
    if _reject_twclid_identifier_shapes(candidate):
        return None
    return candidate


def validate_twclid(value: Any, cfg: Optional[Config] = None) -> Optional[str]:
    """Validate an X click ID against the operator-pinned current contract.

    X documents ``twclid`` as X-generated but publishes no normative grammar.
    Dry-run uses a conservative URL-safe envelope; live readiness additionally
    requires the operator to pin the vendor/CMO contract and its length/alphabet.
    Independent shape checks keep obvious wallet, phone, IP, UUID, digest,
    account-handle, and common credential values out of the identifier slot.
    """
    raw_candidate = str(value or "")
    candidate = raw_candidate.strip()
    if candidate != raw_candidate:
        return None
    config = cfg or load_config()
    if not config.twclid_min_length <= len(candidate) <= config.twclid_max_length:
        return None
    alphabet = _TWCLID_ALPHABETS.get(config.twclid_charset)
    if alphabet is None or not alphabet.fullmatch(candidate):
        return None
    if _reject_twclid_identifier_shapes(candidate):
        return None
    return candidate


def twclid_conflicts_with_wallet(click_id: Any, wallet_address: Any) -> bool:
    """Defense in depth against common encodings of the proven wallet bytes.

    Syntax cannot prove that an opaque public query value was generated by X.
    The server therefore binds the landing candidate into its encrypted ticket,
    requires a pinned live contract, and rejects common encodings of the wallet
    once authoritative wallet proof is available to the isolated worker.
    """
    click = str(click_id or "").strip()
    wallet = str(wallet_address or "").strip().lower()
    if not click or not _WALLET_RE.fullmatch(wallet):
        return True
    raw = bytes.fromhex(wallet[2:])

    def encode_integer(material: bytes, alphabet: str) -> str:
        number = int.from_bytes(material, "big")
        zero_prefix = len(material) - len(material.lstrip(b"\x00"))
        if number == 0:
            return alphabet[0] * max(1, zero_prefix)
        encoded = ""
        while number:
            number, remainder = divmod(number, len(alphabet))
            encoded = alphabet[remainder] + encoded
        return alphabet[0] * zero_prefix + encoded

    wallet_materials = (raw, wallet.encode("ascii"), wallet[2:].encode("ascii"))
    encoded_materials = list(wallet_materials)
    for material in wallet_materials:
        digests = [
            hashlib.sha256(material).digest(),
            hashlib.sha3_256(material).digest(),
            hashlib.sha512(material).digest(),
            hashlib.blake2s(material).digest(),
            hashlib.blake2b(material, digest_size=32).digest(),
        ]
        try:
            from eth_hash.auto import keccak

            digests.append(keccak(material))
        except (ImportError, TypeError, ValueError):
            # SHA-256/SHA3 remain deterministic mandatory protections when the
            # optional Ethereum hashing package is absent.
            pass
        encoded_materials.append(material[::-1])
        for digest in digests:
            encoded_materials.extend((digest, digest[::-1]))
            if len(digest) > 32:
                encoded_materials.extend((digest[:32], digest[-32:]))

    encodings = {wallet, wallet[2:]}
    for material in encoded_materials:
        encodings.update(
            {
                base64.urlsafe_b64encode(material).decode("ascii").rstrip("="),
                base64.b64encode(material).decode("ascii").rstrip("="),
                base64.b32encode(material).decode("ascii").rstrip("=").lower(),
                str(int.from_bytes(material, "big")),
                encode_integer(material, "0123456789abcdefghijklmnopqrstuvwxyz"),
                encode_integer(
                    material,
                    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz",
                ),
                encode_integer(
                    material,
                    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
                ),
            }
        )
    # These encodings are generally case-sensitive, but click IDs are
    # compared case-insensitively as defense in depth: an operator-selected
    # alphabet may normalize case before this point.  Normalize both sides so
    # those common wallet encodings cannot slip through solely due to casing.
    return click.lower() in {encoded.lower() for encoded in encodings}


def production_chain_eligible(payment_rail: Any, chain_id: Any) -> bool:
    """Return true only for an explicitly configured production EVM chain."""
    if str(payment_rail or "").strip().lower() not in ("axgt", "eth", "usdc"):
        return False
    if isinstance(chain_id, bool):
        return False
    try:
        parsed = int(str(chain_id).strip())
    except (TypeError, ValueError):
        return False
    cfg = load_config()
    return not cfg.errors and parsed in cfg.production_chain_ids


def wallet_is_campaign_eligible(wallet_address: Any) -> bool:
    """Reject synthetic, operator, revenue, and configured internal wallets.

    This classifier is deliberately environment-only so importing or calling it
    from authentication/session code cannot initialize billing or touch a DB.
    Unknown or malformed configuration fails closed for the candidate address.
    """
    wallet = str(wallet_address or "").strip().lower()
    if not _WALLET_RE.fullmatch(wallet):
        return False
    if wallet == "0x" + ("0" * 40) or _GUEST_WALLET_RE.fullmatch(wallet):
        return False
    excluded = set()
    for env_name in (
        "AXGT_REVENUE_WALLET",
        "AXONOS_TEST_CREDIT_WALLETS",
        "AXONOS_WHITELISTED_WALLETS",
        "AXONOS_GUEST_INVITE_MINTERS",
        "X_CAPI_EXCLUDED_WALLETS",
    ):
        raw = os.getenv(env_name) or ""
        if len(raw) > 4096:
            return False
        candidates = raw.split(",")
        if len(candidates) > 256:
            return False
        for candidate in candidates:
            normalized = candidate.strip().lower()
            if normalized:
                if not _WALLET_RE.fullmatch(normalized):
                    return False
                excluded.add(normalized)
    return wallet not in excluded


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def config_guard_current(cfg: Optional[Config] = None) -> bool:
    """Verify the worker's durable, irreversible configuration attestation."""
    config = cfg or load_config()
    if config.mode == "off":
        return True
    if (
        os.getenv("X_CAPI_ALLOW_TEST_SECRETS") == "1"
        and os.getenv("X_CAPI_ALLOW_TEST_CONFIG_GUARD_BYPASS") == "1"
    ):
        return True
    configured_path = (
        os.getenv("X_CAPI_CONFIG_GUARD_FILE") or _DEFAULT_CONFIG_GUARD_FILE
    ).strip()
    path = os.path.abspath(configured_path)
    if path != _DEFAULT_CONFIG_GUARD_FILE:
        if (
            os.getenv("X_CAPI_ALLOW_TEST_CONFIG_GUARD_FILE") != "1"
            or not path.startswith("/tmp/")
        ):
            return False
    try:
        expected_uid = int(os.getenv("X_CAPI_WORKER_UID", "10001"))
        parent = os.path.dirname(path)
        parent_info = os.lstat(parent)
        before = os.lstat(path)
        if (
            expected_uid < 0
            or os.path.realpath(parent) != parent
            or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != expected_uid
            or stat.S_IMODE(parent_info.st_mode) != 0o700
            or not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != expected_uid
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or not 1 <= before.st_size <= _CONFIG_GUARD_MAX_BYTES
        ):
            return False
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_uid != before.st_uid
                or opened.st_mode != before.st_mode
                or opened.st_nlink != 1
                or opened.st_size != before.st_size
            ):
                return False
            raw = os.read(descriptor, _CONFIG_GUARD_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) != before.st_size or not raw.endswith(b"\n"):
            return False
        document = json.loads(raw.decode("ascii"))
        expected_fields = {
            "v", "configured", "max_policy_epoch", "deployment_id_hash",
            "mode_scope", "policy_version", "audience_scope",
            "context_key_fingerprint",
        }
        if not isinstance(document, dict) or set(document) != expected_fields:
            return False
        canonical = (
            json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
            + b"\n"
        )
        if raw != canonical or document.get("v") != 1 or document.get("configured") is not True:
            return False
        if (
            isinstance(document.get("max_policy_epoch"), bool)
            or not isinstance(document.get("max_policy_epoch"), int)
        ):
            return False
        return bool(
            document["max_policy_epoch"] == config.policy_epoch
            and secrets.compare_digest(
                str(document["deployment_id_hash"]), _hash(config.deployment_id)
            )
            and document["mode_scope"] == config.mode
            and secrets.compare_digest(
                str(document["policy_version"]), config.policy_version
            )
            and secrets.compare_digest(
                str(document["audience_scope"]), config.audience_scope
            )
            and secrets.compare_digest(
                str(document["context_key_fingerprint"]),
                str(primary_context_key_fingerprint() or ""),
            )
        )
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return False


def _read_restricted_secret(
    *,
    file_env: str,
    default_path: str,
    direct_env: str,
    min_bytes: int = 32,
    max_bytes: int = 4096,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Read a regular, non-symlinked secret below /run/secrets.

    Direct values are accepted only behind the explicit isolated-test boundary.
    Root, the current process UID, and the dedicated worker UID may own a mounted
    file; group/other permission bits are never accepted.
    """
    direct = (os.getenv(direct_env) or "").encode("utf-8")
    if direct:
        if os.getenv("X_CAPI_ALLOW_TEST_SECRETS") != "1":
            return None, f"{direct_env} is allowed only in isolated tests"
        if not min_bytes <= len(direct) <= max_bytes:
            return None, f"{direct_env} has an invalid length"
        return direct, None

    path = (os.getenv(file_env) or default_path).strip()
    try:
        absolute = os.path.abspath(path)
        canonical_root = os.path.realpath(_SECRET_ROOT)
        canonical = os.path.realpath(absolute)
        if (
            not absolute.startswith(_SECRET_ROOT + os.sep)
            or os.path.commonpath((canonical_root, canonical)) != canonical_root
            or canonical != absolute
        ):
            return None, f"{file_env} must be a canonical file below /run/secrets"
        metadata = os.lstat(absolute)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            return None, f"{file_env} must name a regular non-symlink file"
        if metadata.st_mode & 0o077:
            return None, f"{file_env} permissions must be 0600 or stricter"
        if metadata.st_uid not in {0, os.geteuid(), 10001}:
            return None, f"{file_env} has an unexpected owner"
        if metadata.st_nlink != 1:
            return None, f"{file_env} must not be hard-linked"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(absolute, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_uid != metadata.st_uid
                or opened.st_mode != metadata.st_mode
                or opened.st_nlink != 1
                or opened.st_size > max_bytes
            ):
                return None, f"{file_env} changed or is not a bounded regular file"
            value = os.read(descriptor, max_bytes + 1).strip()
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return None, f"{file_env} is unreadable"
    if not min_bytes <= len(value) <= max_bytes:
        return None, f"{file_env} has an invalid length"
    return value, None


def _load_context_ciphers():
    """Validate and load the active key followed by decrypt-only keys."""
    key, error = _read_restricted_secret(
        file_env="X_CAPI_CONTEXT_KEY_FILE",
        default_path="/run/secrets/x_capi_context_key",
        direct_env="X_CAPI_CONTEXT_KEY",
        min_bytes=44,
        max_bytes=4096,
    )
    if error or key is None:
        return None, error
    try:
        from cryptography.fernet import Fernet
        lines = [line.strip() for line in key.splitlines() if line.strip()]
        if not 1 <= len(lines) <= 8 or any(len(line) != 44 for line in lines):
            raise ValueError
        if len(set(lines)) != len(lines):
            raise ValueError
        return tuple(Fernet(line) for line in lines), None
    except (ImportError, ValueError):
        return None, "X_CAPI_CONTEXT_KEY_FILE does not contain a valid Fernet keyring"


def _context_ciphers():
    """Return a per-process validated production keyring.

    Invalid bearer traffic must not turn every GPC-bearing business request
    into repeated secret-file metadata checks and reads. Direct test secrets
    remain uncached because unit tests intentionally replace them in-process;
    mounted production keyrings are immutable until the gate is restarted.
    """
    if os.getenv("X_CAPI_CONTEXT_KEY"):
        return _load_context_ciphers()
    global _context_cipher_cache_pid, _context_cipher_cache_initialized
    global _context_cipher_cache, _context_cipher_cache_error
    pid = os.getpid()
    with _context_cipher_cache_lock:
        if (
            _context_cipher_cache_initialized
            and _context_cipher_cache_pid == pid
        ):
            return _context_cipher_cache, _context_cipher_cache_error
        ciphers, error = _load_context_ciphers()
        _context_cipher_cache_pid = pid
        _context_cipher_cache_initialized = True
        _context_cipher_cache = ciphers
        _context_cipher_cache_error = error
        return ciphers, error


def preload_context_keyring() -> None:
    """Populate the immutable keyring before a fork-per-request server forks."""
    _context_ciphers()


def _context_cipher():
    ciphers, error = _context_ciphers()
    if not ciphers:
        return None, error
    return ciphers[0], None


def primary_context_key_fingerprint() -> Optional[str]:
    """Return a domain-separated one-way identity for the active Fernet key."""
    key, error = _read_restricted_secret(
        file_env="X_CAPI_CONTEXT_KEY_FILE",
        default_path="/run/secrets/x_capi_context_key",
        direct_env="X_CAPI_CONTEXT_KEY",
        min_bytes=44,
        max_bytes=4096,
    )
    if error or key is None:
        return None
    lines = [line.strip() for line in key.splitlines() if line.strip()]
    if not 1 <= len(lines) <= 8 or any(len(line) != 44 for line in lines):
        return None
    if len(set(lines)) != len(lines):
        return None
    try:
        primary = base64.b64decode(lines[0], altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None
    if len(primary) != 32:
        return None
    return hashlib.sha256(
        b"AxonOS X CAPI primary context key v1\x00" + primary
    ).hexdigest()


def rate_limit_digest_key() -> Optional[bytes]:
    """Derive a local HMAC key without exposing the context-key material."""
    key, error = _read_restricted_secret(
        file_env="X_CAPI_CONTEXT_KEY_FILE",
        default_path="/run/secrets/x_capi_context_key",
        direct_env="X_CAPI_CONTEXT_KEY",
        min_bytes=44,
        max_bytes=4096,
    )
    if error or key is None:
        return None
    return hmac.new(key, b"AxonOS X CAPI rate limiter v1", hashlib.sha256).digest()


def keyed_internal_hash(label: str, value: str) -> Optional[str]:
    """HMAC an internal logical identifier with the worker-only hash key."""
    key, error = _read_restricted_secret(
        file_env="X_CAPI_HASH_KEY_FILE",
        default_path="/run/secrets/x_capi_hash_key",
        direct_env="X_CAPI_HASH_KEY",
        min_bytes=32,
        max_bytes=256,
    )
    if error or key is None:
        return None
    message = (str(label) + "\x00" + str(value)).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _seal_context_ticket(payload: Mapping[str, Any]) -> Optional[str]:
    cipher, _error = _context_cipher()
    if cipher is None:
        return None
    encoded = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    if len(encoded) > 1024:
        return None
    return cipher.encrypt(encoded).decode("ascii").rstrip("=")


def _fernet_wire_envelope_valid(candidate: str) -> bool:
    """Cheaply reject inputs that cannot be a ticket minted by this service."""
    try:
        padded = candidate + "=" * ((4 - len(candidate) % 4) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, TypeError):
        return False
    # Fernet: version(1), timestamp(8), IV(16), non-empty AES-CBC ciphertext
    # in 16-byte blocks, and HMAC(32). Our plaintext is capped at 1024 bytes.
    ciphertext_bytes = len(raw) - 57
    return bool(
        raw[:1] == b"\x80"
        and 16 <= ciphertext_bytes <= 1040
        and ciphertext_bytes % 16 == 0
    )


def decode_context_ticket(
    token: Any, *, require_primary: bool = False
) -> Optional[Dict[str, Any]]:
    """Authenticate/decrypt a ticket, optionally excluding decrypt-only keys."""
    candidate = str(token or "").strip()
    if (
        not _OPAQUE_RE.fullmatch(candidate)
        or not _fernet_wire_envelope_valid(candidate)
    ):
        return None
    ciphers, _error = _context_ciphers()
    if not ciphers:
        return None
    candidate += "=" * ((4 - len(candidate) % 4) % 4)
    plaintext = None
    try:
        from cryptography.fernet import InvalidToken
        cipher_index = -1
        for index, cipher in enumerate(ciphers):
            try:
                plaintext = cipher.decrypt(candidate.encode("ascii"))
                cipher_index = index
                break
            except InvalidToken:
                continue
        if plaintext is None or (require_primary and cipher_index != 0):
            return None
        if len(plaintext) > 1024:
            return None
        document = json.loads(plaintext.decode("ascii"))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    expected_fields = {
        "v", "handle", "csrf", "state", "policy_version", "policy_epoch",
        "mode_scope", "audience_scope", "issued_at", "lifecycle_expires_at",
        "landing_commitment", "consented_at", "expires_at", "twclid",
    }
    if not isinstance(document, dict) or set(document) != expected_fields:
        return None
    if document.get("v") != _CONTEXT_TICKET_VERSION:
        return None
    if not _NONCE_RE.fullmatch(str(document.get("handle") or "")):
        return None
    if not _NONCE_RE.fullmatch(str(document.get("csrf") or "")):
        return None
    if document.get("state") not in _CONTEXT_STATES:
        return None
    if not _POLICY_RE.fullmatch(str(document.get("policy_version") or "")):
        return None
    if (
        isinstance(document.get("policy_epoch"), bool)
        or not isinstance(document.get("policy_epoch"), int)
        or not 1 <= document["policy_epoch"] <= 2_147_483_647
    ):
        return None
    if document.get("mode_scope") not in ("dry_run", "live"):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", str(document.get("audience_scope") or "")):
        return None
    try:
        issued_at = float(document["issued_at"])
        lifecycle_expires_at = float(document["lifecycle_expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not math.isfinite(issued_at)
        or not math.isfinite(lifecycle_expires_at)
        or not 0 < issued_at < lifecycle_expires_at <= issued_at + 90 * 86400 + 1
    ):
        return None
    commitment = str(document.get("landing_commitment") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", commitment):
        return None
    if document["state"] == "granted":
        if _validate_twclid_unscoped(document.get("twclid")) is None:
            return None
        try:
            consented_at = float(document["consented_at"])
            expires_at = float(document["expires_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            not math.isfinite(consented_at)
            or not math.isfinite(expires_at)
            or not issued_at <= consented_at < expires_at
            or expires_at != lifecycle_expires_at
            or not _click_matches_commitment(document, document.get("twclid"))
        ):
            return None
    else:
        if any(document.get(name) is not None for name in ("twclid", "consented_at", "expires_at")):
            return None
    return document


def _landing_click_commitment(handle: str, twclid: str) -> str:
    """Bind a click to an opaque ticket without persisting the raw value."""
    return hmac.new(
        handle.encode("ascii"),
        b"AxonOS X landing commitment v1\x00" + twclid.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _click_matches_commitment(ticket: Mapping[str, Any], twclid: Any) -> bool:
    candidate = _validate_twclid_unscoped(twclid)
    if candidate is None:
        return False
    expected = _landing_click_commitment(str(ticket["handle"]), candidate)
    return secrets.compare_digest(expected, str(ticket["landing_commitment"]))


def _new_context_payload(
    cfg: Config, now: float, landing_twclid: str
) -> Dict[str, Any]:
    handle = secrets.token_urlsafe(32)
    return {
        "v": _CONTEXT_TICKET_VERSION,
        "handle": handle,
        "csrf": secrets.token_urlsafe(32),
        "state": "unset",
        "policy_version": cfg.policy_version,
        "policy_epoch": cfg.policy_epoch,
        "mode_scope": cfg.mode,
        "audience_scope": cfg.audience_scope,
        "issued_at": float(now),
        "lifecycle_expires_at": float(now + cfg.attribution_ttl_days * 86400),
        "landing_commitment": _landing_click_commitment(handle, landing_twclid),
        "consented_at": None,
        "expires_at": None,
        "twclid": None,
    }


def _effective_ticket_state(ticket: Mapping[str, Any], cfg: Config, now: float) -> str:
    state = str(ticket.get("state") or "unset")
    issued_at = float(ticket.get("issued_at") or 0)
    if issued_at > now + 300 or float(ticket.get("lifecycle_expires_at") or 0) <= now:
        return "stale"
    if ticket.get("policy_version") != cfg.policy_version:
        return "stale"
    if ticket.get("policy_epoch") != cfg.policy_epoch:
        return "stale"
    if ticket.get("mode_scope") != cfg.mode:
        return "stale"
    if not cfg.audience_scope or ticket.get("audience_scope") != cfg.audience_scope:
        return "stale"
    if state == "granted" and float(ticket.get("expires_at") or 0) <= now:
        return "stale"
    return state


def _db_url(worker: bool = False) -> Optional[str]:
    if worker:
        direct = (os.getenv("X_CAPI_DB_URL") or "").strip()
        path = (os.getenv("X_CAPI_DB_URL_FILE") or "").strip()
        if direct and path:
            logger.warning("x_capi worker DB credential sources conflict")
            return None
        if direct:
            if os.getenv("X_CAPI_ALLOW_TEST_DB_URL") != "1":
                logger.warning("x_capi direct worker DB URL is allowed only in isolated tests")
                return None
            return direct
        if path:
            value, error = _read_restricted_secret(
                file_env="X_CAPI_DB_URL_FILE",
                default_path="/run/secrets/x_capi_db_url",
                direct_env="X_CAPI_UNUSED_DB_SECRET_VALUE",
                min_bytes=1,
                max_bytes=8192,
            )
            if error or value is None:
                return None
            try:
                decoded = value.decode("utf-8")
            except UnicodeDecodeError:
                return None
            if any(character in decoded for character in ("\n", "\r", "\x00")):
                return None
            if not decoded.startswith(("postgresql://", "postgres://")):
                return None
            return decoded
        return None
    # Gate/auth/payment processes must never use their broad core credential for
    # optional attribution state. Only the isolated worker has a CAPI DB URL.
    return None


def get_connection(worker: bool = False):
    if not worker:
        return None
    url = _db_url(worker=worker)
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url, connect_timeout=5)
    except Exception as exc:
        logger.warning("x_capi database unavailable (%s)", type(exc).__name__)
        return None


def exact_origin_allowed(origin: Any, cfg: Optional[Config] = None) -> bool:
    config = cfg or load_config()
    supplied = str(origin or "").strip()
    return bool(
        config.allowed_origin
        and _valid_origin(supplied)
        and supplied == config.allowed_origin
    )


def attribution_cors_origin(
    origin: Any,
    cfg: Optional[Config] = None,
    *,
    allow_revocation_origin: bool = False,
) -> Optional[str]:
    """Return the narrow CORS echo used by attribution endpoints.

    The configured HTTP(S) origin may perform ordinary actions. Any syntactically
    valid HTTPS origin may reach the endpoint so a frontend holding its unguessable
    ticket+CSRF can still revoke after the deployment origin changes. Mutation
    authorization remains enforced independently by ``update_consent``/bind.
    """
    config = cfg or load_config()
    supplied = str(origin or "").strip()
    if exact_origin_allowed(supplied, config):
        return supplied
    if (
        allow_revocation_origin
        and _valid_origin(supplied)
        and urlsplit(supplied).scheme == "https"
    ):
        return supplied
    return None


def attribution_status(
    context_token: Any = None, *, landing_twclid: Any = None, gpc: bool = False
) -> Dict[str, Any]:
    """Return or lazily issue a signed context without touching PostgreSQL.

    A valid ticket and CSRF value are stable: polling never rotates either one.
    A visitor with no click and no prior capability receives no unique handle.
    Attribution contexts are admitted only after authoritative wallet proof by
    the isolated worker. Privacy actions may create bounded, expiring
    tombstones before binding; exhaustion fails the optional feature closed
    without granting public traffic access to the CAPI database.
    """
    cfg = load_config()
    now = time.time()
    supplied = str(context_token or "").strip()
    ticket = decode_context_ticket(supplied)
    base: Dict[str, Any] = {
        "mode": cfg.mode,
        "enabled": False,
        "policy_version": cfg.policy_version,
        "state": "unavailable",
        "attribution_ttl_days": cfg.attribution_ttl_days,
        "revocation_available": bool(ticket),
    }
    if ticket:
        base.update({"context": supplied, "csrf": ticket["csrf"]})

    if cfg.mode == "off":
        if gpc and ticket:
            base["state"] = (
                ticket["state"]
                if ticket["state"] in ("denied", "revoked")
                else "revocation_required"
            )
        else:
            base["state"] = "off"
        base["gpc_applied"] = bool(gpc)
        if ticket:
            base["prior_state"] = ticket["state"]
        return base
    if not cfg.producer_ready:
        base["configuration_error"] = True
        base["gpc_applied"] = bool(gpc)
        if gpc and ticket:
            base["state"] = (
                ticket["state"]
                if ticket["state"] in ("denied", "revoked")
                else "revocation_required"
            )
        return base
    if not config_guard_current(cfg):
        base["configuration_error"] = True
        base["gpc_applied"] = bool(gpc)
        if gpc and ticket:
            base["state"] = (
                ticket["state"]
                if ticket["state"] in ("denied", "revoked")
                else "revocation_required"
            )
        else:
            base["state"] = "unavailable"
        return base
    cipher, _key_error = _context_cipher()
    if cipher is None:
        base["configuration_error"] = True
        return base

    # Trailing keyring entries are decrypt-only so an old or compromised key
    # cannot mint a capability that remains active after rotation. Such tickets
    # stay readable solely so the holder can close/revoke their lifecycle.
    if ticket is not None and decode_context_ticket(supplied, require_primary=True) is None:
        base["enabled"] = False
        base["gpc_applied"] = bool(gpc)
        base["prior_state"] = ticket["state"]
        base["state"] = (
            ticket["state"]
            if ticket["state"] in ("denied", "revoked")
            else "revocation_required" if gpc else "stale"
        )
        return base

    base["enabled"] = not gpc
    if ticket is None:
        candidate_raw = str(landing_twclid or "")
        if gpc:
            base.update(
                {
                    "state": "denied",
                    "gpc_applied": True,
                    "landing_click_accepted": False,
                }
            )
            return base
        if not candidate_raw:
            base.update(
                {
                    "state": "denied" if gpc else "idle",
                    "gpc_applied": bool(gpc),
                    "landing_click_accepted": False,
                }
            )
            return base
        candidate = validate_twclid(candidate_raw, cfg)
        if candidate is None:
            base.update(
                {
                    "enabled": False,
                    "state": "invalid_click",
                    "landing_click_accepted": False,
                    "gpc_applied": bool(gpc),
                }
            )
            return base
        payload = _new_context_payload(cfg, now, candidate)
        sealed = _seal_context_ticket(payload)
        if not sealed:
            base["configuration_error"] = True
            base["enabled"] = False
            return base
        base.update(
            {
                "context": sealed,
                "csrf": payload["csrf"],
                "state": "denied" if gpc else "unset",
                "revocation_available": False,
                "gpc_applied": bool(gpc),
                "landing_click_accepted": True,
                "expires_at": payload["lifecycle_expires_at"],
            }
        )
        return base

    effective = _effective_ticket_state(ticket, cfg, now)
    base["state"] = (
        effective
        if gpc and effective in ("denied", "revoked")
        else ("revocation_required" if gpc else effective)
    )
    base["gpc_applied"] = bool(gpc)
    base["new_lifecycle_available"] = effective in ("denied", "revoked", "stale")
    if gpc:
        base["new_lifecycle_available"] = False
        base["landing_click_accepted"] = False
        base["expires_at"] = float(ticket["lifecycle_expires_at"])
        return base
    supplied_click = str(landing_twclid or "")
    if supplied_click:
        candidate = validate_twclid(supplied_click, cfg)
        if candidate is None or not _click_matches_commitment(ticket, candidate):
            base.update(
                {
                    "enabled": False,
                    "state": "invalid_click",
                    "landing_click_accepted": False,
                }
            )
            return base
    base["landing_click_accepted"] = bool(
        effective == "granted" or (supplied_click and effective == "unset")
    )
    base["expires_at"] = float(ticket["lifecycle_expires_at"])
    return base


def _revocation_origin_allowed(origin: Any, cfg: Config) -> bool:
    # Revocation is privacy-improving and authenticated by the unguessable
    # capability plus CSRF. Keep it available from the prior hosted frontend
    # after an origin/policy/config change; non-configured HTTP origins stay out.
    return attribution_cors_origin(
        origin, cfg, allow_revocation_origin=True
    ) is not None


def _context_payload_for_state(
    ticket: Mapping[str, Any],
    state: str,
    *,
    consented_at: Optional[float] = None,
    expires_at: Optional[float] = None,
    twclid: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "v": _CONTEXT_TICKET_VERSION,
        "handle": ticket["handle"],
        "csrf": ticket["csrf"],
        "state": state,
        "policy_version": ticket["policy_version"],
        "policy_epoch": ticket["policy_epoch"],
        "mode_scope": ticket["mode_scope"],
        "audience_scope": ticket["audience_scope"],
        "issued_at": float(ticket["issued_at"]),
        "lifecycle_expires_at": float(ticket["lifecycle_expires_at"]),
        "landing_commitment": ticket["landing_commitment"],
        "consented_at": None,
        "expires_at": None,
        "twclid": None,
    }
    if state == "granted":
        payload.update(
            {
                "consented_at": float(consented_at),
                "expires_at": float(expires_at),
                "twclid": str(twclid),
            }
        )
    return payload


def _consent_socket_path() -> Optional[str]:
    configured = (os.getenv("X_CAPI_CONSENT_SOCKET") or _DEFAULT_CONSENT_SOCKET).strip()
    if configured == _DEFAULT_CONSENT_SOCKET:
        return configured
    if os.getenv("X_CAPI_ALLOW_TEST_SOCKET") == "1" and configured.startswith("/tmp/"):
        return configured
    return None


def _request_worker_consent_blocking(
    operation: str, context_token: str, csrf_token: str, now: float
) -> Optional[Dict[str, Any]]:
    """Perform one bounded local RPC in a dedicated native worker thread."""
    path = _consent_socket_path()
    if not path:
        return None
    request_document = {
        "v": 1,
        "action": "consent",
        "operation": operation,
        "context_token": context_token,
        "csrf_token": csrf_token,
        "request_timestamp_ms": int(now * 1000),
    }
    try:
        payload = json.dumps(
            request_document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if not payload or len(payload) > 4096:
            return None
        parent = os.path.dirname(path)
        parent_info = os.lstat(parent)
        socket_info = os.lstat(path)
        expected_uid = int(os.getenv("X_CAPI_WORKER_UID", "10001"))
        if (
            expected_uid < 0
            or os.path.realpath(parent) != parent
            or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid not in (0, expected_uid)
            or parent_info.st_mode & 0o022
            or not stat.S_ISSOCK(socket_info.st_mode)
            or socket_info.st_uid != expected_uid
            or socket_info.st_mode & 0o077
        ):
            return None
        deadline = time.monotonic() + 0.75
        client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            client.settimeout(max(0.001, deadline - time.monotonic()))
            client.connect(path)
            if not hasattr(socket, "SO_PEERCRED"):
                return None
            credentials = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
            if peer_uid != expected_uid:
                return None
            client.settimeout(max(0.001, deadline - time.monotonic()))
            if client.send(payload) != len(payload):
                return None
            client.settimeout(max(0.001, deadline - time.monotonic()))
            encoded = client.recv(4097)
        finally:
            client.close()
        if not encoded or len(encoded) > 4096:
            return None
        response = json.loads(encoded.decode("ascii"))
        if not isinstance(response, dict) or response.get("v") != 1:
            return None
        if not isinstance(response.get("ok"), bool):
            return None
        if response["ok"]:
            if set(response) != {"v", "ok", "state"}:
                return None
            if response.get("state") not in ("denied", "revoked", "stale"):
                return None
        else:
            if set(response) - {"v", "ok", "error", "state"}:
                return None
            if not re.fullmatch(r"[a-z0-9_]{1,64}", str(response.get("error") or "")):
                return None
        return response
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def _consent_rpc_components() -> Tuple[ThreadPoolExecutor, threading.BoundedSemaphore]:
    """Return a process-local bounded pool, safely replacing forked state."""
    global _consent_rpc_pid, _consent_rpc_executor, _consent_rpc_slots
    current_pid = os.getpid()
    with _consent_rpc_lock:
        if (
            _consent_rpc_pid != current_pid
            or _consent_rpc_executor is None
            or _consent_rpc_slots is None
        ):
            _consent_rpc_pid = current_pid
            _consent_rpc_executor = ThreadPoolExecutor(
                max_workers=_CONSENT_RPC_MAX_WORKERS,
                thread_name_prefix="x-capi-consent-rpc",
            )
            _consent_rpc_slots = threading.BoundedSemaphore(_CONSENT_RPC_MAX_PENDING)
        return _consent_rpc_executor, _consent_rpc_slots


def _request_worker_consent(
    operation: str, context_token: str, csrf_token: str, now: float
) -> Optional[Dict[str, Any]]:
    """Offload privacy RPCs so a slow worker cannot stall a core event loop."""
    executor, slots = _consent_rpc_components()
    if not slots.acquire(blocking=False):
        return None
    try:
        future = executor.submit(
            _request_worker_consent_blocking,
            operation,
            context_token,
            csrf_token,
            now,
        )
    except (RuntimeError, MemoryError):
        slots.release()
        return None
    future.add_done_callback(lambda _future: slots.release())

    deadline = time.monotonic() + _CONSENT_RPC_WAIT_SECONDS
    try:
        # Flask is served by gevent without relying on global monkey-patching.
        # Yield its hub while native pool threads perform the blocking socket
        # work. In non-gevent processes, Future.result is a bounded wait.
        try:
            from gevent import sleep as cooperative_sleep
        except ImportError:
            return future.result(timeout=_CONSENT_RPC_WAIT_SECONDS)
        while not future.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            cooperative_sleep(min(0.01, remaining))
        return future.result(timeout=0)
    except FutureTimeout:
        return None
    except Exception:
        return None


def privacy_action_rate_key(
    *, context_token: Any, csrf_token: Any, action: Any, origin: Any, gpc: bool = False
) -> Optional[str]:
    """Identify a valid privacy-improving request before shared rate limiting."""
    requested = str(action or "").strip().lower()
    if gpc:
        requested = "revoke"
    if requested not in ("grant", "decline", "revoke", "new_lifecycle"):
        return None
    if requested not in ("decline", "revoke"):
        return None
    cfg = load_config()
    if not _revocation_origin_allowed(origin, cfg):
        return None
    ticket = decode_context_ticket(context_token)
    csrf = str(csrf_token or "").strip()
    if ticket is None or not _NONCE_RE.fullmatch(csrf):
        return None
    if not secrets.compare_digest(ticket["csrf"], csrf):
        return None
    return _hash(ticket["handle"])


def privacy_bearer_rate_key(*, context_token: Any, origin: Any) -> Optional[str]:
    """Rate-key a GPC withdrawal authenticated by its opaque bearer ticket."""
    cfg = load_config()
    if not _revocation_origin_allowed(origin, cfg):
        return None
    ticket = decode_context_ticket(context_token)
    if ticket is None:
        return None
    return _hash(ticket["handle"])


def privacy_signal_rate_key(context_token: Any) -> Optional[str]:
    """Rate-key a valid GPC bearer before any durable local mutation.

    This origin-independent form is used only for the privacy signal carried on
    an existing same-service request. It grants no consent or data access and
    can only suppress the exact signed lifecycle represented by the ticket.
    """
    ticket = decode_context_ticket(context_token)
    if ticket is None:
        return None
    return _hash(ticket["handle"])


def update_consent(
    *,
    context_token: Any,
    csrf_token: Any,
    action: Any,
    twclid: Any = None,
    landing_twclid: Any = None,
    origin: Any,
    gpc: bool = False,
    offload_worker_rpc: bool = True,
) -> Tuple[Dict[str, Any], int]:
    """Apply a stateless grant or a worker-isolated durable privacy transition."""
    cfg = load_config()
    requested = str(action or "").strip().lower()
    # GPC is authoritative for every action. In particular, a caller cannot
    # use ``new_lifecycle`` to leave a prior durable grant/queued conversion
    # alive while simultaneously asserting the privacy signal.
    if gpc:
        requested = "revoke"
    if requested not in ("grant", "decline", "revoke", "new_lifecycle"):
        return {"ok": False, "error": "Invalid consent action"}, 400
    revocation_action = requested in ("decline", "revoke")
    if cfg.mode == "off" and requested not in ("decline", "revoke"):
        return {"ok": False, "state": "off", "error": "Conversion sharing is off"}, 409
    if not cfg.producer_ready and requested not in ("decline", "revoke"):
        return {
            "ok": False,
            "state": "unavailable",
            "error": "Conversion sharing is unavailable",
        }, 503
    if requested not in ("decline", "revoke") and not config_guard_current(cfg):
        return {
            "ok": False,
            "state": "unavailable",
            "error": "Conversion sharing is unavailable",
        }, 503
    origin_ok = (
        _revocation_origin_allowed(origin, cfg)
        if revocation_action
        else exact_origin_allowed(origin, cfg)
    )
    if not origin_ok:
        return {"ok": False, "error": "Origin not allowed"}, 403

    supplied = str(context_token or "").strip()
    ticket = decode_context_ticket(supplied)
    csrf = str(csrf_token or "").strip()
    if ticket is None or not _NONCE_RE.fullmatch(csrf) or not secrets.compare_digest(
        ticket["csrf"], csrf
    ):
        return {"ok": False, "error": "Invalid consent context"}, 403
    if not revocation_action and decode_context_ticket(
        supplied, require_primary=True
    ) is None:
        return {
            "ok": False,
            "state": "stale",
            "error": "Attribution key generation is no longer active",
        }, 409

    now = time.time()
    effective = _effective_ticket_state(ticket, cfg, now)
    if requested == "new_lifecycle":
        if gpc:
            return {"ok": False, "state": "denied", "error": "GPC is active"}, 409
        if effective not in ("denied", "revoked", "stale"):
            return {"ok": False, "state": effective, "error": "Current lifecycle is still active"}, 409
    elif requested == "grant":
        if effective != "unset":
            return {"ok": False, "state": effective, "error": "Attribution lifecycle is closed"}, 409
        # The raw click remains transient page memory before consent. The
        # encrypted unset ticket contains only its commitment; grant must
        # present the same click in a dedicated header.
        if str(twclid or "").strip():
            return {"ok": False, "state": "unset", "error": "Click identifier is not accepted here"}, 400
        click_id = validate_twclid(landing_twclid, cfg)
        if click_id is None or not _click_matches_commitment(ticket, click_id):
            return {"ok": False, "state": "unset", "error": "Invalid X click identifier"}, 400
    else:
        click_id = None

    if requested == "grant":
        expires_at = float(ticket["lifecycle_expires_at"])
        response_payload = _context_payload_for_state(
            ticket,
            "granted",
            consented_at=max(now, float(ticket["issued_at"])),
            expires_at=expires_at,
            twclid=click_id,
        )
        state = "granted"
    else:
        if requested == "new_lifecycle":
            candidate = validate_twclid(landing_twclid, cfg)
            if candidate is None:
                return {"ok": False, "state": effective, "error": "Invalid X click identifier"}, 400
            if _click_matches_commitment(ticket, candidate):
                return {
                    "ok": False,
                    "state": effective,
                    "error": "A fresh X click identifier is required",
                }, 409
        # A datagram and fixed local control give the worker early
        # cancellation/fencing signals; only its credential-authenticated
        # durable RPC acknowledgement is ever presented to the browser as
        # success.
        privacy_fenced = True
        if requested in ("decline", "revoke"):
            # The preallocated control survives a worker process outage and is
            # independently checked immediately before dispatch. If a targeted
            # slot cannot be claimed, a validated ticket trips the bounded
            # global control. This request path never creates marker files or
            # fsyncs; the worker RPC below remains the only successful durable
            # acknowledgement.
            privacy_fence_status = _persist_privacy_fence_status(
                supplied, request_safe=True
            )
            privacy_fenced = privacy_fence_status in ("published", "quarantined")
            _emit_revocation_hint_nonblocking(supplied, int(now * 1000))
            if not privacy_fenced:
                # The worker may have crossed its final pre-send boundary, or
                # the local fence may be ambiguous/corrupt. Do not let a later
                # DB acknowledgement make this request look as though it
                # prevented an outbound conversion. A verified global
                # quarantine is safe to proceed because it independently closes
                # every future send.
                return {
                    "ok": False,
                    "state": effective,
                    "error": "Consent store unavailable",
                }, 503
        request_worker = (
            _request_worker_consent
            if offload_worker_rpc
            else _request_worker_consent_blocking
        )
        worker_response = request_worker(requested, supplied, csrf, now)
        # A local marker is a pre-dispatch safety fence, not a durable consent
        # acknowledgement. Only the worker's committed DB response may be
        # presented as a successful decline/revoke; otherwise the browser keeps
        # sharing locally suppressed and offers an explicit retry.
        if not worker_response:
            return {"ok": False, "error": "Consent store unavailable"}, 503
        if not worker_response.get("ok"):
            error_code = str(worker_response.get("error") or "consent_store_unavailable")
            if error_code in ("invalid_context", "csrf_mismatch", "invalid_request"):
                return {"ok": False, "error": "Invalid consent context"}, 403
            if error_code in ("lifecycle_closed", "first_touch_locked"):
                return {"ok": False, "state": worker_response.get("state", effective), "error": "Attribution lifecycle is closed"}, 409
            return {"ok": False, "error": "Consent store unavailable"}, 503
        if requested == "new_lifecycle":
            response_payload = _new_context_payload(cfg, now, candidate)
            state = "unset"
        else:
            state = str(worker_response.get("state") or ("revoked" if requested == "revoke" else "denied"))
            if state not in ("denied", "revoked"):
                return {"ok": False, "error": "Consent store unavailable"}, 503
            response_payload = _context_payload_for_state(ticket, state)

    sealed = _seal_context_ticket(response_payload)
    if not sealed:
        return {"ok": False, "error": "Consent context unavailable"}, 503
    result: Dict[str, Any] = {
        "ok": True,
        "state": state,
        "context": sealed,
        "csrf": response_payload["csrf"],
        "gpc_applied": bool(gpc),
        "attribution_ttl_days": cfg.attribution_ttl_days,
        "expires_at": float(response_payload["lifecycle_expires_at"]),
        "revocation_available": state == "granted",
    }
    return result, 200


def revoke_for_gpc(
    context_token: Any,
    *,
    origin: Any,
    offload_worker_rpc: bool = True,
) -> Tuple[Dict[str, Any], int]:
    """Turn a bearer attribution capability into a durable GPC withdrawal.

    Revocation is privacy-improving, so the encrypted ticket itself is enough
    authority for a non-consent endpoint such as ``bind``. The CSRF nonce never
    has to be exposed to that endpoint's request body or logs.
    """
    supplied = str(context_token or "").strip()
    ticket = decode_context_ticket(supplied)
    if ticket is None:
        return {"ok": False, "error": "Invalid consent context"}, 403
    return update_consent(
        context_token=supplied,
        csrf_token=ticket["csrf"],
        action="revoke",
        origin=origin,
        gpc=True,
        offload_worker_rpc=offload_worker_rpc,
    )


def _ingest_socket_path() -> Optional[str]:
    configured = (os.getenv("X_CAPI_INGEST_SOCKET") or _DEFAULT_INGEST_SOCKET).strip()
    if configured == _DEFAULT_INGEST_SOCKET:
        return configured
    if os.getenv("X_CAPI_ALLOW_TEST_SOCKET") == "1" and configured.startswith("/tmp/"):
        return configured
    return None


def _send_local_envelope(envelope: Mapping[str, Any]) -> bool:
    """Attempt exactly one non-blocking local datagram; never retry or log data.

    Core producers perform no CAPI filesystem metadata reads and acquire no
    CAPI lock. The fixed production socket lives in a worker-owned mode-0700
    directory mounted read-only into the gate; the kernel performs one
    non-blocking local connect/send and failures simply drop optional
    attribution. A shutdown race may therefore lose an uncommitted datagram,
    which is preferable to making auth/payment/session responses wait on an
    optional subsystem.
    """
    path = _ingest_socket_path()
    if not path:
        return False
    try:
        payload = json.dumps(
            dict(envelope), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        return False
    if not payload or len(payload) > _MAX_INGEST_DATAGRAM:
        return False
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.connect(path)
        return sock.send(payload) == len(payload)
    except (BlockingIOError, OSError):
        return False
    finally:
        if sock is not None:
            sock.close()


def emit_event_nonblocking(
    *,
    context_token: Any,
    wallet_address: Any,
    milestone: Any,
    source_key: Any,
    event_timestamp_ms: Optional[int] = None,
    allow_context_binding: bool = False,
    credit_source: Optional[str] = None,
    payment_rail: Optional[str] = None,
    chain_id: Optional[int] = None,
) -> bool:
    """Emit one bounded post-commit fact without reading CAPI durable state.

    This function is the only CAPI operation used by core auth/payment/session
    paths.  It deliberately does not decrypt the ticket, read the configuration
    guard, touch PostgreSQL, acquire a filesystem lock, or perform network I/O.
    The isolated worker repeats every semantic/configuration check before it can
    materialize an outbox row.  Keeping the producer to a non-blocking local
    datagram means a slow, corrupt, locked, or absent CAPI subsystem cannot hold
    a core transaction or response hostage.
    """
    cfg = load_config()
    name = str(milestone or "")
    token = str(context_token or "").strip()
    wallet = str(wallet_address or "").strip().lower()
    source = str(source_key or "").strip().lower()
    if (
        not cfg.producer_ready
        or name not in MILESTONES
        or not _OPAQUE_RE.fullmatch(token)
        or not wallet_is_campaign_eligible(wallet)
        or not _SOURCE_KEY_RE.fullmatch(source)
        or not isinstance(allow_context_binding, bool)
    ):
        return False
    if name != MILESTONE_WALLET_VERIFIED and not cfg.event_ids.get(name):
        return False
    if name == MILESTONE_WALLET_VERIFIED and not allow_context_binding:
        return False
    if name == MILESTONE_SESSION_STARTED and allow_context_binding:
        return False
    metadata: Dict[str, Any] = {
        "allow_context_binding": allow_context_binding,
        "credit_source": None,
        "payment_rail": None,
        "chain_id": None,
    }
    if name == MILESTONE_WALLET_VERIFIED:
        if source != wallet or any(v is not None for v in (credit_source, payment_rail, chain_id)):
            return False
    elif name == MILESTONE_DEPOSIT_COMPLETED:
        rail = str(payment_rail or "").strip().lower()
        provenance = str(credit_source or "").strip().lower()
        if (
            not re.fullmatch(r"0x[0-9a-f]{64}", source)
            or provenance != "onchain"
            or rail not in ("axgt", "eth", "usdc")
            or isinstance(chain_id, bool)
        ):
            return False
        try:
            parsed_chain = int(chain_id)
        except (TypeError, ValueError):
            return False
        if not production_chain_eligible(rail, parsed_chain):
            return False
        metadata.update(
            {
                "credit_source": provenance,
                "payment_rail": rail,
                "chain_id": parsed_chain,
            }
        )
    else:
        if not re.fullmatch(r"[1-9][0-9]{0,18}", source) or any(
            v is not None for v in (credit_source, payment_rail, chain_id)
        ):
            return False
    if isinstance(event_timestamp_ms, bool):
        return False
    try:
        timestamp = int(
            event_timestamp_ms if event_timestamp_ms is not None else time.time() * 1000
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if timestamp <= 0:
        return False
    return _send_local_envelope(
        {
            "v": 1,
            "action": "event",
            "milestone": name,
            "context_token": token,
            "wallet_address": wallet,
            "source_key": source,
            "event_timestamp_ms": timestamp,
            "metadata": metadata,
        }
    )


def emit_binding_nonblocking(
    *,
    context_token: Any,
    wallet_address: Any,
    event_timestamp_ms: Optional[int] = None,
) -> bool:
    """Request a wallet binding without manufacturing a conversion milestone.

    The caller must have independently authenticated the wallet. The isolated
    worker validates the granted primary ticket, first-touch rules, revocation,
    and configuration before creating any durable context. This action never
    inserts a dedup key or outbox row.
    """
    cfg = load_config()
    token = str(context_token or "").strip()
    wallet = str(wallet_address or "").strip().lower()
    if (
        not cfg.producer_ready
        or not _OPAQUE_RE.fullmatch(token)
        or not wallet_is_campaign_eligible(wallet)
    ):
        return False
    if isinstance(event_timestamp_ms, bool):
        return False
    try:
        timestamp = int(
            event_timestamp_ms if event_timestamp_ms is not None else time.time() * 1000
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if timestamp <= 0:
        return False
    return _send_local_envelope(
        {
            "v": 1,
            "action": "bind",
            "context_token": token,
            "wallet_address": wallet,
            "event_timestamp_ms": timestamp,
        }
    )


def emit_revocation_nonblocking(
    context_token: Any, *, event_timestamp_ms: Optional[int] = None
) -> bool:
    """Publish both a local deny marker and the worker's fast cancel hint.

    The preallocated record contains only hashes of random ticket capabilities
    and survives a worker process restart. Capacity/contention trips the
    preallocated global fail-closed control; the datagram remains the
    low-latency path for a running worker. Neither path uses a database, fsync,
    allocation, or a network request.
    """
    token = str(context_token or "").strip()
    if not _OPAQUE_RE.fullmatch(token):
        return False
    try:
        timestamp = int(
            event_timestamp_ms if event_timestamp_ms is not None else time.time() * 1000
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if timestamp <= 0:
        return False
    fenced = publish_privacy_fence_nonblocking(token, fail_closed=True)
    hinted = _emit_revocation_hint_nonblocking(token, timestamp)
    return bool(fenced or hinted)


def _emit_revocation_hint_nonblocking(token: str, timestamp: int) -> bool:
    return _send_local_envelope(
        {
            "v": 1,
            "action": "revoke",
            "context_token": token,
            "event_timestamp_ms": timestamp,
        }
    )


def observe_business_gpc_nonblocking(
    context_token: Any, *, event_timestamp_ms: Optional[int] = None
) -> bool:
    """Publish an immediate bounded fail-closed fence plus a wake-up hint.

    The signed ticket is decrypted before any shared control is mutated. A
    targeted preallocated slot is preferred; contention, collision, or capacity
    exhaustion trips the single preallocated global quarantine control. Neither
    path allocates a file/row, fsyncs, waits for a lock/worker, or affects the
    business response. The datagram is only a redundant low-latency hint.
    """
    token = str(context_token or "").strip()
    if not _OPAQUE_RE.fullmatch(token) or not _fernet_wire_envelope_valid(token):
        return False
    if isinstance(event_timestamp_ms, bool):
        return False
    try:
        timestamp = int(
            event_timestamp_ms
            if event_timestamp_ms is not None
            else time.time() * 1000
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if timestamp <= 0:
        return False
    fenced = publish_privacy_fence_nonblocking(token, fail_closed=True)
    _emit_revocation_hint_nonblocking(token, timestamp)
    return bool(fenced)


def _privacy_expected_uid() -> Optional[int]:
    try:
        expected_uid = int(os.getenv("X_CAPI_WORKER_UID", "10001"))
    except (TypeError, ValueError):
        return None
    return expected_uid if expected_uid >= 0 else None


def _open_validated_privacy_directory() -> tuple[Optional[int], Optional[int]]:
    directory = _privacy_fence_directory()
    expected_uid = _privacy_expected_uid()
    if not directory or expected_uid is None:
        return None, None
    descriptor = None
    try:
        path_info = os.lstat(directory)
        if (
            os.path.realpath(directory) != directory
            or not stat.S_ISDIR(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or path_info.st_uid != expected_uid
            or stat.S_IMODE(path_info.st_mode) != 0o700
        ):
            return None, None
        descriptor = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (path_info.st_dev, path_info.st_ino):
            os.close(descriptor)
            return None, None
        return descriptor, expected_uid
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None, None


def _validated_privacy_control(
    directory_fd: int, name: str, expected_uid: int, expected_size: int
) -> Optional[int]:
    descriptor = None
    try:
        descriptor = os.open(
            name,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or opened.st_size != expected_size
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            os.close(descriptor)
            return None
        return descriptor
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None


def _acquire_privacy_dispatch_control() -> tuple[Optional[int], Optional[int], str]:
    """Acquire the fixed EX boundary used for every slot state transition."""
    directory_fd, expected_uid = _open_validated_privacy_directory()
    if directory_fd is None or expected_uid is None:
        return None, None, "unavailable"
    descriptor = _validated_privacy_control(
        directory_fd,
        _PRIVACY_FENCE_LOCK_NAME,
        expected_uid,
        len(_PRIVACY_FENCE_LOCK_MAGIC),
    )
    if descriptor is None:
        os.close(directory_fd)
        return None, None, "unavailable"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            os.close(directory_fd)
            return None, None, "busy"
        if os.pread(
            descriptor, len(_PRIVACY_FENCE_LOCK_MAGIC), 0
        ) != _PRIVACY_FENCE_LOCK_MAGIC:
            raise OSError("invalid privacy dispatch control")
        return directory_fd, descriptor, "acquired"
    except OSError:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)
        os.close(directory_fd)
        return None, None, "unavailable"


def _release_privacy_dispatch_control(
    directory_fd: Optional[int], descriptor: Optional[int]
) -> None:
    if descriptor is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass
    if directory_fd is not None:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _activate_global_privacy_quarantine() -> bool:
    """Durably flip only the fixed quarantine control outside core paths."""
    directory_fd, expected_uid = _open_validated_privacy_directory()
    if directory_fd is None or expected_uid is None:
        return False
    descriptor = _validated_privacy_control(
        directory_fd,
        _PRIVACY_FENCE_QUARANTINE_NAME,
        expected_uid,
        len(_PRIVACY_FENCE_QUARANTINE_INACTIVE),
    )
    try:
        if descriptor is None:
            return False
        if (
            os.pwrite(descriptor, _PRIVACY_FENCE_QUARANTINE_ACTIVE, 0)
            != len(_PRIVACY_FENCE_QUARANTINE_ACTIVE)
        ):
            return False
        os.fsync(descriptor)
        os.fsync(directory_fd)
        return bool(
            os.pread(descriptor, len(_PRIVACY_FENCE_QUARANTINE_ACTIVE), 0)
            == _PRIVACY_FENCE_QUARANTINE_ACTIVE
        )
    except OSError:
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _activate_global_privacy_quarantine_nonblocking() -> bool:
    """Trip a preallocated fail-closed control without waiting or allocating.

    Callers must authenticate/decrypt a lifecycle ticket first. We try the
    dispatch lock only to obtain the cleanest ordering, but never wait for it:
    a busy lock means a dispatch already crossed its final boundary. Writing
    the global control still prevents every later dispatch. A torn control or
    poisoned lock also fails closed because the worker validates exact bytes
    before every send. No fsync is performed on a business request path.
    """
    directory_fd, expected_uid = _open_validated_privacy_directory()
    if directory_fd is None or expected_uid is None:
        return False
    dispatch_descriptor = _validated_privacy_control(
        directory_fd,
        _PRIVACY_FENCE_LOCK_NAME,
        expected_uid,
        len(_PRIVACY_FENCE_LOCK_MAGIC),
    )
    quarantine_descriptor = _validated_privacy_control(
        directory_fd,
        _PRIVACY_FENCE_QUARANTINE_NAME,
        expected_uid,
        len(_PRIVACY_FENCE_QUARANTINE_INACTIVE),
    )
    lock_acquired = False
    try:
        if dispatch_descriptor is None or quarantine_descriptor is None:
            return False
        try:
            fcntl.flock(
                dispatch_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
            )
            lock_acquired = True
        except (BlockingIOError, OSError):
            # A sender may already own the boundary. It cannot be retroactively
            # stopped, but the preallocated sentinel still closes later sends.
            pass

        lock_state = os.pread(
            dispatch_descriptor, len(_PRIVACY_FENCE_LOCK_MAGIC), 0
        )
        quarantine_state = os.pread(
            quarantine_descriptor,
            len(_PRIVACY_FENCE_QUARANTINE_INACTIVE),
            0,
        )
        if lock_state != _PRIVACY_FENCE_LOCK_MAGIC:
            return True
        if quarantine_state != _PRIVACY_FENCE_QUARANTINE_INACTIVE:
            # Exact ACTIVE and every malformed/torn value both block dispatch.
            return True

        try:
            os.pwrite(
                quarantine_descriptor, _PRIVACY_FENCE_QUARANTINE_ACTIVE, 0
            )
        except OSError:
            pass
        quarantine_state = os.pread(
            quarantine_descriptor,
            len(_PRIVACY_FENCE_QUARANTINE_INACTIVE),
            0,
        )
        if quarantine_state != _PRIVACY_FENCE_QUARANTINE_INACTIVE:
            return True

        # If the sentinel write made no observable progress, poison one byte of
        # the already allocated dispatch control. The worker's exact-magic check
        # treats this as global quarantine; a failed poison simply reports False.
        try:
            os.pwrite(dispatch_descriptor, b"\x00", 0)
        except OSError:
            pass
        return bool(
            os.pread(
                dispatch_descriptor, len(_PRIVACY_FENCE_LOCK_MAGIC), 0
            )
            != _PRIVACY_FENCE_LOCK_MAGIC
        )
    except OSError:
        return False
    finally:
        if lock_acquired and dispatch_descriptor is not None:
            try:
                fcntl.flock(dispatch_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        for descriptor in (quarantine_descriptor, dispatch_descriptor):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _privacy_pending_record(ticket: Mapping[str, Any]) -> Optional[bytes]:
    """Encode one complete targeted withdrawal in a fixed-size control record."""
    try:
        handle_hash = _hash(str(ticket["handle"])).encode("ascii")
        csrf_hash = _hash(str(ticket["csrf"])).encode("ascii")
        lifecycle_expires_at = float(ticket["lifecycle_expires_at"])
        if (
            not re.fullmatch(rb"[0-9a-f]{64}", handle_hash)
            or not re.fullmatch(rb"[0-9a-f]{64}", csrf_hash)
            or not math.isfinite(lifecycle_expires_at)
            or lifecycle_expires_at <= 0
        ):
            return None
        record = (
            _PRIVACY_PENDING_PREFIX
            + handle_hash
            + csrf_hash
            + struct.pack("!d", lifecycle_expires_at)
        )
        return record if len(record) == len(_PRIVACY_PENDING_EMPTY) else None
    except (KeyError, TypeError, ValueError, OverflowError, struct.error):
        return None


def _privacy_pending_record_identity(record: bytes) -> Optional[bytes]:
    """Return a validated handle hash, or ``None`` for malformed state."""
    if (
        len(record) != len(_PRIVACY_PENDING_EMPTY)
        or record[:2] != _PRIVACY_PENDING_PREFIX
        or not re.fullmatch(rb"[0-9a-f]{64}", record[2:66])
        or not re.fullmatch(rb"[0-9a-f]{64}", record[66:130])
    ):
        return None
    try:
        expiry = struct.unpack("!d", record[130:138])[0]
    except struct.error:
        return None
    if not math.isfinite(expiry) or expiry <= 0:
        return None
    return record[2:66]


def _privacy_pending_slot_indices(identity: bytes) -> Tuple[int, ...]:
    """Choose four stable candidate slots without a directory-wide scan.

    The start and odd stride form a permutation over the 64-slot power-of-two
    table, so the first four candidates are distinct. A collision can delay
    optional attribution privacy acknowledgement until the worker consumes a
    slot, but it cannot amplify an arbitrary core request into 64 file opens.
    """
    if not re.fullmatch(rb"[0-9a-f]{64}", identity):
        return ()
    start = int(identity[:16], 16) % _PRIVACY_PENDING_SLOT_COUNT
    stride = (int(identity[16:32], 16) % (_PRIVACY_PENDING_SLOT_COUNT // 2)) * 2 + 1
    return tuple(
        (start + offset * stride) % _PRIVACY_PENDING_SLOT_COUNT
        for offset in range(4)
    )


def _claim_privacy_pending_slot(
    record: bytes, *, fail_closed: bool = False
) -> Optional[int]:
    """Publish one complete P record using only preallocated nonblocking I/O.

    A successful ``pwrite`` is immediately visible after a producer-process
    crash without waiting for disk. It is deliberately not fsynced on a core
    request path, so sudden host power loss before the page reaches storage is
    a documented residual; this build cannot deliver live conversions. The
    worker can apply an unlocked P record directly, so there is no executor,
    post-response publication gap, or per-fork thread.
    """
    identity = _privacy_pending_record_identity(record)
    if identity is None or not isinstance(fail_closed, bool):
        return None

    def failed_claim() -> Optional[int]:
        # When called inside the dispatch boundary, the helper's own LOCK_NB
        # attempt observes our lock as busy but can still flip the preallocated
        # global sentinel before this function releases the boundary. That
        # closes the otherwise exploitable release/reacquire dispatch gap.
        if fail_closed and _activate_global_privacy_quarantine_nonblocking():
            return _PRIVACY_SLOT_GLOBAL_FENCED
        return None

    directory_fd, dispatch_descriptor, dispatch_status = (
        _acquire_privacy_dispatch_control()
    )
    if dispatch_status == "busy":
        fenced = failed_claim()
        return fenced if fenced is not None else _PRIVACY_SLOT_DISPATCH_BUSY
    expected_uid = _privacy_expected_uid()
    if (
        directory_fd is None
        or dispatch_descriptor is None
        or expected_uid is None
    ):
        return failed_claim()
    candidate_descriptor = None
    try:
        for index in _privacy_pending_slot_indices(identity):
            name = f"{_PRIVACY_PENDING_SLOT_PREFIX}{index:02d}"
            descriptor = _validated_privacy_control(
                directory_fd, name, expected_uid, len(_PRIVACY_PENDING_EMPTY)
            )
            if descriptor is None:
                return failed_claim()
            try:
                current = os.pread(
                    descriptor, len(_PRIVACY_PENDING_EMPTY), 0
                )
                current_identity = (
                    None
                    if current == _PRIVACY_PENDING_EMPTY
                    else _privacy_pending_record_identity(current)
                )
                if current_identity == identity:
                    if candidate_descriptor is not None:
                        try:
                            fcntl.flock(candidate_descriptor, fcntl.LOCK_UN)
                        finally:
                            os.close(candidate_descriptor)
                        candidate_descriptor = None
                    if hmac.compare_digest(current, record):
                        return _PRIVACY_SLOT_ALREADY_FENCED
                    return failed_claim()
                if current == _PRIVACY_PENDING_EMPTY and candidate_descriptor is None:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        fenced = failed_claim()
                        return (
                            fenced
                            if fenced is not None
                            else _PRIVACY_SLOT_DISPATCH_BUSY
                        )
                    except OSError:
                        return failed_claim()
                    if os.pread(
                        descriptor, len(_PRIVACY_PENDING_EMPTY), 0
                    ) != _PRIVACY_PENDING_EMPTY:
                        return failed_claim()
                    # All P/I transitions take the dispatch control first.
                    # Keep the first empty slot locked while scanning the rest,
                    # making same-handle publication atomic across processes.
                    candidate_descriptor = descriptor
                    descriptor = None
                    continue
                if (
                    current != _PRIVACY_PENDING_EMPTY
                    and current_identity is None
                ):
                    return failed_claim()
            except OSError:
                return failed_claim()
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        if candidate_descriptor is None:
            return failed_claim()
        if os.pwrite(candidate_descriptor, record, 0) != len(record):
            return failed_claim()
        if os.pread(candidate_descriptor, len(record), 0) != record:
            return failed_claim()
        # Deliberately do not fsync in a business request. The complete fixed P
        # record is immediately visible to running workers and survives a
        # producer-process crash. Sudden host power loss before writeback is a
        # documented residual; live delivery is structurally unavailable.
        return 1
    except (OSError, UnicodeError):
        return failed_claim()
    finally:
        if candidate_descriptor is not None:
            try:
                fcntl.flock(candidate_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(candidate_descriptor)
            except OSError:
                pass
        try:
            _release_privacy_dispatch_control(
                directory_fd, dispatch_descriptor
            )
        except Exception:
            pass


def publish_privacy_fence_nonblocking(
    context_token: Any, *, fail_closed: bool = False
) -> bool:
    """Synchronously publish a complete targeted P record without waiting.

    This privacy-only path performs bounded validation and preallocated local
    I/O under ``LOCK_NB``. It never touches PostgreSQL, allocates spool files,
    waits for a worker, fsyncs, or starts a thread. Capacity/contention failure
    remains targeted by default. A caller handling authoritative GPC on an
    ordinary business request may select ``fail_closed=True``; only after
    successful ticket authentication does that mode trip the preallocated
    global quarantine control as a bounded fallback.
    """
    token = str(context_token or "").strip()
    if not isinstance(fail_closed, bool) or not _OPAQUE_RE.fullmatch(token):
        return False
    ticket = decode_context_ticket(token)
    if ticket is None:
        return False
    record = _privacy_pending_record(ticket)
    if record is None:
        return False
    claimed = _claim_privacy_pending_slot(record, fail_closed=fail_closed)
    if claimed in (
        1, _PRIVACY_SLOT_ALREADY_FENCED, _PRIVACY_SLOT_GLOBAL_FENCED
    ):
        return True
    return bool(
        fail_closed and _activate_global_privacy_quarantine_nonblocking()
    )


def observe_privacy_signal_nonblocking(
    context_token: Any,
    *,
    event_timestamp_ms: Optional[int] = None,
    emit_hint: bool = True,
) -> bool:
    """Publish a crash-visible targeted fence plus a fast worker hint."""
    token = str(context_token or "").strip()
    if not _OPAQUE_RE.fullmatch(token):
        return False
    if isinstance(event_timestamp_ms, bool):
        return False
    try:
        timestamp = int(
            event_timestamp_ms if event_timestamp_ms is not None else time.time() * 1000
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if timestamp <= 0:
        return False
    fenced = publish_privacy_fence_nonblocking(token)
    if not isinstance(emit_hint, bool):
        return False
    if emit_hint:
        _emit_revocation_hint_nonblocking(token, timestamp)
    # A datagram is only a latency hint. Success means the complete targeted P
    # record is present, so a worker crash between recv and DB commit cannot
    # turn an optimistic response into a lost withdrawal.
    return bool(fenced)


def _privacy_fence_directory() -> Optional[str]:
    configured = (
        os.getenv("X_CAPI_PRIVACY_FENCE_DIR") or _DEFAULT_PRIVACY_FENCE_DIR
    ).strip()
    path = os.path.abspath(configured)
    if path == _DEFAULT_PRIVACY_FENCE_DIR:
        return path
    if (
        os.getenv("X_CAPI_ALLOW_TEST_PRIVACY_FENCE") == "1"
        and path.startswith("/tmp/")
    ):
        return path
    return None


def persist_privacy_fence(context_token: Any) -> bool:
    return _persist_privacy_fence_status(context_token) == "published"


def _persist_privacy_fence_status(
    context_token: Any, *, request_safe: bool = False
) -> str:
    """Publish a privacy deny fence without DB/network access.

    Normal request handling selects ``request_safe=True`` and therefore uses
    only fixed preallocated controls, nonblocking locks, and no fsync. The
    legacy durable JSON writer remains readable/callable solely for backward
    compatibility with already-provisioned marker volumes and recovery tests;
    no production request invokes that allocating path.

    The worker owns and provisions the fixed local directory and dispatch lock.
    A non-blocking exclusive lock orders a completed marker before a future X
    request. If the worker already holds its shared lock, dispatch has begun and
    the caller must retry; this function never waits behind outbound X I/O.
    """
    if not isinstance(request_safe, bool):
        return "unavailable"
    if request_safe:
        return (
            "published"
            if publish_privacy_fence_nonblocking(
                context_token, fail_closed=True
            )
            else "unavailable"
        )
    token = str(context_token or "").strip()
    if not _OPAQUE_RE.fullmatch(token):
        return "unavailable"
    ticket = decode_context_ticket(token)
    if ticket is None:
        return "unavailable"
    directory = _privacy_fence_directory()
    if not directory:
        return "unavailable"
    try:
        expected_uid = int(os.getenv("X_CAPI_WORKER_UID", "10001"))
    except (TypeError, ValueError):
        return "unavailable"
    if expected_uid < 0:
        return "unavailable"

    directory_descriptor = None
    lock_descriptor = None
    quarantine_descriptor = None
    marker_descriptor = None
    lock_acquired = False
    publication_complete = False

    def activate_global_quarantine() -> bool:
        nonlocal publication_complete
        if quarantine_descriptor is None:
            return False
        try:
            written = os.pwrite(
                quarantine_descriptor, _PRIVACY_FENCE_QUARANTINE_ACTIVE, 0
            )
            if written == len(_PRIVACY_FENCE_QUARANTINE_ACTIVE):
                os.fsync(quarantine_descriptor)
            active = bool(
                written == len(_PRIVACY_FENCE_QUARANTINE_ACTIVE)
                and os.pread(
                    quarantine_descriptor,
                    len(_PRIVACY_FENCE_QUARANTINE_ACTIVE),
                    0,
                ) == _PRIVACY_FENCE_QUARANTINE_ACTIVE
            )
            publication_complete = active
            return active
        except OSError:
            return False

    def quarantine_or_unavailable() -> str:
        quarantined = activate_global_quarantine()
        # A durable global sentinel is sufficient for a success path only when
        # this request proved exclusive ownership of the dispatch boundary. If
        # pread/flock itself failed, a stale worker may already hold SH and be
        # past its final check; conservatively require the browser to retry.
        return "quarantined" if quarantined and lock_acquired else "unavailable"

    try:
        directory_info = os.lstat(directory)
        if (
            os.path.realpath(directory) != directory
            or not stat.S_ISDIR(directory_info.st_mode)
            or stat.S_ISLNK(directory_info.st_mode)
            or directory_info.st_uid != expected_uid
            or stat.S_IMODE(directory_info.st_mode) != 0o700
        ):
            return "unavailable"
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_directory_info = os.fstat(directory_descriptor)
        if (
            opened_directory_info.st_dev != directory_info.st_dev
            or opened_directory_info.st_ino != directory_info.st_ino
        ):
            return "unavailable"
        lock_path = os.path.join(directory, _PRIVACY_FENCE_LOCK_NAME)
        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        lock_info = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != expected_uid
            or stat.S_IMODE(lock_info.st_mode) != 0o600
            or lock_info.st_nlink != 1
            or lock_info.st_size != len(_PRIVACY_FENCE_LOCK_MAGIC)
        ):
            return "unavailable"
        quarantine_path = os.path.join(
            directory, _PRIVACY_FENCE_QUARANTINE_NAME
        )
        quarantine_descriptor = os.open(
            quarantine_path,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        quarantine_info = os.fstat(quarantine_descriptor)
        if (
            not stat.S_ISREG(quarantine_info.st_mode)
            or quarantine_info.st_uid != expected_uid
            or stat.S_IMODE(quarantine_info.st_mode) != 0o600
            or quarantine_info.st_nlink != 1
            or quarantine_info.st_size
            != len(_PRIVACY_FENCE_QUARANTINE_INACTIVE)
        ):
            return "unavailable"
        quarantine_state = os.pread(
            quarantine_descriptor,
            len(_PRIVACY_FENCE_QUARANTINE_INACTIVE),
            0,
        )
        quarantine_active = (
            quarantine_state == _PRIVACY_FENCE_QUARANTINE_ACTIVE
        )
        if not quarantine_active and quarantine_state != _PRIVACY_FENCE_QUARANTINE_INACTIVE:
            return "unavailable"

        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Do not create marker files without exclusive publication
            # ownership. The same lock also covers dispatch, so contention can
            # mean either a send already crossed its final boundary or another
            # privacy publisher is counting/creating a marker. In both cases
            # the endpoint reports a retry; its preallocated P-slot attempt is
            # independent and no unbounded directory race is introduced.
            return "dispatch_in_progress"
        else:
            lock_acquired = True
            if os.pread(
                lock_descriptor, len(_PRIVACY_FENCE_LOCK_MAGIC), 0
            ) != _PRIVACY_FENCE_LOCK_MAGIC:
                return "unavailable"
        if quarantine_active:
            publication_complete = True
            return "quarantined"

        handle_hash = _hash(ticket["handle"])
        csrf_hash = _hash(ticket["csrf"])
        document = {
            "v": 1,
            "handle_hash": handle_hash,
            "csrf_hash": csrf_hash,
            "lifecycle_expires_at": float(ticket["lifecycle_expires_at"]),
        }
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if not 1 <= len(encoded) <= 512:
            return "unavailable"

        destination = os.path.join(directory, handle_hash + ".json")
        try:
            existing = os.lstat(destination)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            # Never equate a pathname with a durable marker. It may be a stale
            # zero-length O_EXCL file, symlink, replacement inode, or partial
            # write left by a killed publisher. Validate exact bytes and force
            # both file and directory metadata durable before acknowledging it.
            try:
                marker_descriptor = os.open(
                    destination,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                marker_info = os.fstat(marker_descriptor)
                current_info = os.stat(destination, follow_symlinks=False)
                marker_bytes = os.read(marker_descriptor, 513)
                if (
                    not stat.S_ISREG(marker_info.st_mode)
                    or marker_info.st_uid != expected_uid
                    or stat.S_IMODE(marker_info.st_mode) != 0o600
                    or marker_info.st_nlink != 1
                    or marker_info.st_size != len(encoded)
                    or (marker_info.st_dev, marker_info.st_ino)
                    != (current_info.st_dev, current_info.st_ino)
                    or marker_bytes != encoded
                ):
                    return quarantine_or_unavailable()
                os.fsync(marker_descriptor)
                os.fsync(directory_descriptor)
                os.close(marker_descriptor)
                marker_descriptor = None
            except OSError:
                return quarantine_or_unavailable()
            publication_complete = True
            return "published"

        marker_count = 0
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name in (
                        _PRIVACY_FENCE_LOCK_NAME,
                        _PRIVACY_FENCE_QUARANTINE_NAME,
                    ) or entry.name in _PRIVACY_PENDING_SLOT_NAMES:
                        continue
                    if not re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                        return quarantine_or_unavailable()
                    marker_count += 1
                    if marker_count >= _PRIVACY_FENCE_MARKER_CAP:
                        return quarantine_or_unavailable()
        except OSError:
            return "unavailable"

        try:
            marker_descriptor = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            # Another publisher may have won between lstat and O_EXCL, and it
            # may still be filling the inode without owning the dispatch lock.
            # Revalidate the exact durable record; pathname existence alone is
            # never a publication acknowledgement.
            try:
                marker_descriptor = os.open(
                    destination,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                marker_info = os.fstat(marker_descriptor)
                current_info = os.stat(destination, follow_symlinks=False)
                marker_bytes = os.read(marker_descriptor, 513)
                if (
                    not stat.S_ISREG(marker_info.st_mode)
                    or marker_info.st_uid != expected_uid
                    or stat.S_IMODE(marker_info.st_mode) != 0o600
                    or marker_info.st_nlink != 1
                    or marker_info.st_size != len(encoded)
                    or (marker_info.st_dev, marker_info.st_ino)
                    != (current_info.st_dev, current_info.st_ino)
                    or marker_bytes != encoded
                ):
                    return quarantine_or_unavailable()
                os.fsync(marker_descriptor)
                os.fsync(directory_descriptor)
                os.close(marker_descriptor)
                marker_descriptor = None
            except OSError:
                return quarantine_or_unavailable()
            publication_complete = True
            return "published"
        if os.geteuid() == 0:
            os.fchown(marker_descriptor, expected_uid, expected_uid)
        os.fchmod(marker_descriptor, 0o600)
        written = 0
        while written < len(encoded):
            count = os.write(marker_descriptor, encoded[written:])
            if count <= 0:
                raise OSError("short privacy-fence write")
            written += count
        marker_info = os.fstat(marker_descriptor)
        if (
            not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_uid != expected_uid
            or stat.S_IMODE(marker_info.st_mode) != 0o600
            or marker_info.st_nlink != 1
            or marker_info.st_size != len(encoded)
        ):
            return "unavailable"
        os.fsync(marker_descriptor)
        os.close(marker_descriptor)
        marker_descriptor = None
        os.fsync(directory_descriptor)
        publication_complete = True
        return "published"
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return quarantine_or_unavailable()
    finally:
        if marker_descriptor is not None:
            try:
                os.close(marker_descriptor)
            except OSError:
                pass
        if quarantine_descriptor is not None:
            try:
                os.close(quarantine_descriptor)
            except OSError:
                pass
        if lock_descriptor is not None:
            if lock_acquired and not publication_complete:
                # The fixed lock inode is preallocated by the worker. Poisoning
                # one existing byte needs no new directory entry or data block;
                # the worker validates this magic beneath its shared lock before
                # every dispatch and therefore fails closed after an ambiguous
                # publication error (including ENOSPC).
                try:
                    if os.pwrite(lock_descriptor, b"\x00", 0) == 1:
                        os.fsync(lock_descriptor)
                except OSError:
                    pass
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_descriptor)
            except OSError:
                pass
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass


def business_context_or_none(context_token: Any, *, gpc: bool = False) -> Optional[str]:
    """Sanitize a browser context and make GPC dominant on business routes.

    Browsers send ``Sec-GPC`` on ordinary auth/payment/session requests too.
    Those routes must never turn a stale frontend header into a conversion while
    GPC is active. The early server hook publishes a complete preallocated
    privacy fence; this fallback does the same for direct callers. A valid
    ticket may require bounded local control-file I/O and nonblocking locks, but
    it never creates a thread/file/row, fsyncs, waits for a worker, uses
    PostgreSQL, or performs external network I/O.
    Errors suppress attribution but do not fail the business operation. The
    worker validates and decrypts ordinary tickets, so returning a merely
    well-formed opaque token here cannot authorize an outbound event.
    """
    token = str(context_token or "").strip()
    if not _OPAQUE_RE.fullmatch(token):
        return None
    if gpc:
        try:
            observe_business_gpc_nonblocking(token)
        except Exception:
            pass
        return None
    return token


def paid_deposit_eligible(
    credit_source: str,
    payment_rail: str,
    block_number: Any,
    chain_id: Any = None,
) -> bool:
    """Strict paid provenance allowlist; test/demo/admin/sentinel rows fail closed."""
    source = str(credit_source or "").strip().lower()
    rail = str(payment_rail or "").strip().lower()
    try:
        block = int(block_number)
    except (TypeError, ValueError):
        return False
    return (
        source == "onchain"
        and rail in ("axgt", "eth", "usdc")
        and block > 0
        and (chain_id is None or production_chain_eligible(rail, chain_id))
    )
