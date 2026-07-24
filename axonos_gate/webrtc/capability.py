"""Purpose-scoped signed capabilities for launcher-managed WebRTC agents.

The long-lived fleet secret remains in the central gate.  A session container
receives only an Ed25519-signed JWT bound to its immutable compute-session row,
wallet, and high-entropy per-session file-key fingerprint.  Invalid tokens are
rejected cryptographically before any database lookup.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from typing import Any, Optional

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


_ISSUER = "axonos-gate"
_AUDIENCE = "axonos-webrtc-agent"
_TOKEN_TYPE = "AXONOS-WEBRTC-CAP+JWT"
_MAX_TOKEN_CHARS = 4096
_KEY_CONTEXT = b"AxonOS WebRTC capability signing key v1\x00"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _signing_secret() -> str:
    # This variable is now a central signing secret. It must never be forwarded
    # to launcher-managed tenant containers.
    return (os.getenv("WEBRTC_AGENT_INTERNAL_KEY") or "").strip()


def _keypair() -> Optional[tuple[Ed25519PrivateKey, Ed25519PublicKey, str]]:
    secret = _signing_secret()
    if not secret or any(c in secret for c in "\r\n"):
        return None
    seed = hashlib.sha256(_KEY_CONTEXT + secret.encode("utf-8")).digest()
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public_key = private_key.public_key()
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    kid = "webrtc-v1-" + _b64url(hashlib.sha256(public_raw).digest()[:12])
    return private_key, public_key, kid


def _positive_session_id(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or not raw.isascii() or not raw.isdigit():
            return None
        parsed = int(raw)
    else:
        return None
    return parsed if 0 < parsed <= 2_147_483_647 else None


def _wallet(value: Any) -> Optional[str]:
    wallet = str(value or "").strip().lower()
    if (
        len(wallet) != 42
        or not wallet.startswith("0x")
        or any(c not in "0123456789abcdef" for c in wallet[2:])
    ):
        return None
    return wallet


def _ttl_seconds() -> int:
    raw = (os.getenv("WEBRTC_AGENT_CAPABILITY_TTL_SECONDS") or "").strip()
    try:
        value = int(raw) if raw else 86_400
    except ValueError:
        value = 86_400
    return max(600, min(604_800, value))


def files_key_fingerprint(files_key: str) -> Optional[str]:
    key = (files_key or "").strip()
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _encode_claims(
    private_key: Ed25519PrivateKey,
    kid: str,
    *,
    sid: int,
    wallet: str,
    fingerprint: str,
    jti: str,
    now: int,
    expires_at: int,
) -> str:
    claims = {
        "v": 1,
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": f"session:{sid}",
        "sid": sid,
        "wallet": wallet,
        "fkh": fingerprint,
        "jti": jti,
        "iat": now,
        "nbf": now - 5,
        "exp": expires_at,
    }
    return jwt.encode(
        claims,
        private_key,
        algorithm="EdDSA",
        headers={"kid": kid, "typ": _TOKEN_TYPE},
    )


def issue(
    compute_session_id: Any,
    wallet_address: Any,
    files_key: str,
) -> Optional[dict[str, Any]]:
    """Mint one bearer capability and return token plus revocation metadata."""
    keypair = _keypair()
    sid = _positive_session_id(compute_session_id)
    wallet = _wallet(wallet_address)
    fingerprint = files_key_fingerprint(files_key)
    if keypair is None or sid is None or wallet is None or fingerprint is None:
        return None
    private_key, _public_key, kid = keypair
    now = int(time.time())
    expires_at = now + _ttl_seconds()
    jti = secrets.token_urlsafe(24)
    token = _encode_claims(
        private_key,
        kid,
        sid=sid,
        wallet=wallet,
        fingerprint=fingerprint,
        jti=jti,
        now=now,
        expires_at=expires_at,
    )
    return {
        "token": token,
        "jti_hash": hashlib.sha256(jti.encode("utf-8")).hexdigest(),
        "files_key_fingerprint": fingerprint,
        "expires_at": float(expires_at),
    }


def _verified_claims(
    token: str,
    expected_compute_session_id: Any,
    expected_wallet_address: Any,
) -> Optional[dict[str, Any]]:
    """Return verified claims for central capability operations."""
    raw_token = (token or "").strip()
    sid = _positive_session_id(expected_compute_session_id)
    wallet = _wallet(expected_wallet_address)
    keypair = _keypair()
    if (
        not raw_token
        or len(raw_token) > _MAX_TOKEN_CHARS
        or sid is None
        or wallet is None
        or keypair is None
    ):
        return None
    _private_key, public_key, kid = keypair
    try:
        header = jwt.get_unverified_header(raw_token)
        if (
            header.get("alg") != "EdDSA"
            or header.get("kid") != kid
            or header.get("typ") != _TOKEN_TYPE
        ):
            return None
        claims = jwt.decode(
            raw_token,
            public_key,
            algorithms=["EdDSA"],
            audience=_AUDIENCE,
            issuer=_ISSUER,
            leeway=10,
            options={
                "require": [
                    "exp",
                    "iat",
                    "nbf",
                    "iss",
                    "aud",
                    "sub",
                    "sid",
                    "wallet",
                    "fkh",
                    "jti",
                ]
            },
        )
    except (jwt.PyJWTError, TypeError, ValueError):
        return None

    claim_sid = _positive_session_id(claims.get("sid"))
    claim_wallet = _wallet(claims.get("wallet"))
    fingerprint = str(claims.get("fkh") or "")
    jti = str(claims.get("jti") or "")
    if (
        claims.get("v") != 1
        or claim_sid != sid
        or claim_wallet != wallet
        or claims.get("sub") != f"session:{sid}"
        or len(fingerprint) != 64
        or any(c not in "0123456789abcdef" for c in fingerprint)
        or not jti
        or len(jti) > 256
    ):
        return None
    return {
        "id": sid,
        "wallet_address": wallet,
        "files_key_fingerprint": fingerprint,
        "jti": jti,
        "issued_at": float(claims["iat"]),
        "expires_at": float(claims["exp"]),
    }


def verify(
    token: str,
    expected_compute_session_id: Any,
    expected_wallet_address: Any,
) -> Optional[dict[str, Any]]:
    """Verify bounded JWT input without database access.

    Raw JTI material stays inside this module; callers receive only its hash.
    """
    claims = _verified_claims(
        token,
        expected_compute_session_id,
        expected_wallet_address,
    )
    if not claims:
        return None
    return {
        "id": claims["id"],
        "wallet_address": claims["wallet_address"],
        "files_key_fingerprint": claims["files_key_fingerprint"],
        "jti_hash": hashlib.sha256(claims["jti"].encode("utf-8")).hexdigest(),
        "expires_at": claims["expires_at"],
    }


def renew(
    token: str,
    expected_compute_session_id: Any,
    expected_wallet_address: Any,
) -> Optional[dict[str, Any]]:
    """Extend a still-valid capability while preserving its revocable JTI.

    Database authorization belongs to ``session_manager`` and must happen
    before this result is persisted or returned. Preserving the JTI lets an old
    token remain usable until its original expiry if a refresh response is lost.
    """
    claims = _verified_claims(
        token,
        expected_compute_session_id,
        expected_wallet_address,
    )
    keypair = _keypair()
    if not claims or keypair is None:
        return None
    private_key, _public_key, kid = keypair
    now = int(time.time())
    expires_at = now + _ttl_seconds()
    renewed_token = _encode_claims(
        private_key,
        kid,
        sid=int(claims["id"]),
        wallet=str(claims["wallet_address"]),
        fingerprint=str(claims["files_key_fingerprint"]),
        jti=str(claims["jti"]),
        now=now,
        expires_at=expires_at,
    )
    return {
        "token": renewed_token,
        "jti_hash": hashlib.sha256(claims["jti"].encode("utf-8")).hexdigest(),
        "files_key_fingerprint": claims["files_key_fingerprint"],
        "expires_at": float(expires_at),
    }
