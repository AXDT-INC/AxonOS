"""
CDP x402 facilitator client — the Bazaar-listable settlement rail.

This is an OPT-IN alternative to the self-settle path in x402_verifier.py. When
`AXGT_X402_FACILITATOR_ENABLED=true`, the gate routes x402 `verify`/`settle`
through Coinbase's CDP facilitator (https://api.cdp.coinbase.com/platform/v2/x402)
instead of broadcasting the EIP-3009 transferWithAuthorization itself. CDP catalogs
a resource in the x402 Bazaar the first time it *settles* a payment for that
resource, so this rail is what makes AxonOS discoverable at
https://docs.cdp.coinbase.com/x402/bazaar.

Design constraints:
  - Self-settle stays the DEFAULT and is left completely untouched. This module is
    only consulted when the flag is on; with it off, behaviour is byte-identical to
    before.
  - No new hard dependency at import time. CDP JWT auth uses the official
    `cdp-sdk` helper when installed, else a PyJWT + cryptography fallback. Either
    path is only exercised when the facilitator is enabled.

Config (env):
  AXGT_X402_FACILITATOR_ENABLED   master switch (default false → self-settle)
  CDP_API_KEY_ID                  CDP API key id
  CDP_API_KEY_SECRET              CDP API key secret (Ed25519 base64, or EC PEM)
  CDP_FACILITATOR_URL             base URL (default the CDP platform v2 endpoint)
  AXGT_X402_BAZAAR_DISCOVERABLE   advertise the resource for Bazaar indexing (default true when facilitator on)
  AXGT_X402_BAZAAR_CATEGORY       Bazaar category tag (default "compute")
  AXGT_X402_BAZAAR_TAGS           comma-separated Bazaar tags (default "gpu,compute,ssh,linux")
"""

import logging
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
_JWT_TTL_SECONDS = 120


def facilitator_enabled() -> bool:
    """True only when explicitly switched on. Default = self-settle (off)."""
    raw = (os.getenv("AXGT_X402_FACILITATOR_ENABLED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _facilitator_base() -> str:
    return (os.getenv("CDP_FACILITATOR_URL") or DEFAULT_FACILITATOR_URL).strip().rstrip("/")


def _cdp_key_id() -> str:
    return (os.getenv("CDP_API_KEY_ID") or "").strip()


def _cdp_key_secret() -> str:
    return (os.getenv("CDP_API_KEY_SECRET") or "").strip()


def bazaar_discoverable() -> bool:
    """Whether to advertise this resource for Bazaar indexing. Default true when the
    facilitator is enabled (the whole point of the rail), off otherwise."""
    raw = (os.getenv("AXGT_X402_BAZAAR_DISCOVERABLE") or "").strip().lower()
    if not raw:
        return facilitator_enabled()
    return raw in ("1", "true", "yes", "on")


def bazaar_discovery_extension() -> Optional[Dict[str, Any]]:
    """
    Build the Bazaar discovery extension attached to PaymentRequirements so CDP
    indexes the resource. Shape mirrors the CDP `declareDiscoveryExtension()`
    v2 schema.

    Returns None when discovery is off, so callers can leave requirements untouched.
    """
    if not bazaar_discoverable():
        return None
    category = (os.getenv("AXGT_X402_BAZAAR_CATEGORY") or "compute").strip()
    tags_raw = os.getenv("AXGT_X402_BAZAAR_TAGS")
    if tags_raw is None:
        tags = ["gpu", "compute", "ssh", "linux"]
    else:
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    return {
        "bazaar": {
            "discoverable": True,
            "category": category,
            "tags": tags,
            "info": {
                "input": {
                    "type": "http",
                    "method": "POST",
                    "bodyType": "json",
                    "body": {
                        "wallet_address": {
                            "type": "string",
                            "description": "EVM wallet address paying for or claiming the AxonOS compute session.",
                            "required": True
                        },
                        "ssh_pubkey": {
                            "type": "string",
                            "description": "SSH public key to authorize access to the rented compute session.",
                            "required": True
                        },
                        "requested_profile": {
                            "type": "string",
                            "description": "Optional compute profile, for example small.",
                            "required": False
                        }
                    }
                },
                "output": {
                    "type": "object",
                    "example": {
                        "granted": True,
                        "session_id": 192,
                        "requested_profile": "small",
                        "assigned_gpu_ids": [0],
                        "remaining_seconds": 3600,
                        "ssh_enabled": True,
                        "ssh_host": "axonconsole.io",
                        "ssh_port": 42042,
                        "ssh_user": "aXonian",
                        "payment": {
                            "verified": True,
                            "credited_minutes": 60,
                            "settlement_tx_hash": "0x..."
                        }
                    }
                }
            },
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["input"],
                "properties": {
                    "input": {
                        "type": "object",
                        "required": ["type", "method", "bodyType", "body"],
                        "properties": {
                            "type": { "const": "http", "type": "string" },
                            "method": { "enum": ["POST"], "type": "string" },
                            "bodyType": { "enum": ["json"], "type": "string" },
                            "body": { "type": "object" }
                        }
                    },
                    "output": {
                        "type": "object",
                        "required": ["type"],
                        "properties": {
                            "type": { "type": "string" },
                            "example": { "type": "object" }
                        }
                    }
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# CDP authentication: short-lived Bearer JWT (EdDSA/Ed25519, or ES256 for legacy
# EC keys). Verified against CDP JWT auth docs: alg+typ+kid+nonce header; claims
# sub=keyId, iss="cdp", aud=["cdp_service"], nbf, exp (=nbf+120), uri="METHOD HOST PATH".
# ---------------------------------------------------------------------------

def _generate_cdp_jwt(method: str, request_path: str) -> Optional[str]:
    key_id = _cdp_key_id()
    key_secret = _cdp_key_secret()
    if not key_id or not key_secret:
        logger.warning("CDP facilitator enabled but CDP_API_KEY_ID/CDP_API_KEY_SECRET not set")
        return None

    host = urlsplit(_facilitator_base()).netloc or "api.cdp.coinbase.com"

    # Preferred: official CDP helper (exact, maintained by Coinbase).
    try:
        from cdp.auth.utils.jwt import generate_jwt, JwtOptions  # type: ignore
        return generate_jwt(JwtOptions(
            api_key_id=key_id,
            api_key_secret=key_secret,
            request_method=method.upper(),
            request_host=host,
            request_path=request_path,
            expires_in=_JWT_TTL_SECONDS,
        ))
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — fall through to the manual path
        logger.warning("cdp-sdk generate_jwt failed (%s); trying PyJWT fallback", exc)

    # Fallback: hand-rolled with PyJWT + cryptography.
    try:
        import secrets as _secrets
        import jwt as pyjwt  # PyJWT
    except ImportError as exc:
        logger.error("CDP JWT needs `cdp-sdk` or `PyJWT` installed: %s", exc)
        return None

    now = int(time.time())
    claims = {
        "sub": key_id,
        "iss": "cdp",
        "aud": ["cdp_service"],
        "nbf": now,
        "exp": now + _JWT_TTL_SECONDS,
        "uri": f"{method.upper()} {host}{request_path}",
    }
    headers = {"kid": key_id, "nonce": _secrets.token_hex(16), "typ": "JWT"}

    secret = key_secret
    try:
        if secret.startswith("-----BEGIN"):
            # Legacy EC private key (PEM) → ES256.
            return pyjwt.encode(claims, secret, algorithm="ES256", headers=headers)
        # Ed25519 secret: base64 of (32-byte seed [+ 32-byte pubkey]) → EdDSA.
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        raw = base64.b64decode(secret)
        seed = raw[:32]
        priv = Ed25519PrivateKey.from_private_bytes(seed)
        return pyjwt.encode(claims, priv, algorithm="EdDSA", headers=headers)
    except Exception as exc:  # noqa: BLE001
        logger.error("CDP JWT generation failed: %s", exc)
        return None


def _post(endpoint: str, body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, str]]]:
    """POST `body` to `{base}/{endpoint}` with a fresh CDP Bearer JWT. Returns (json, error, headers)."""
    import requests

    base = _facilitator_base()
    url = f"{base}/{endpoint.lstrip('/')}"
    path = urlsplit(url).path
    jwt_token = _generate_cdp_jwt("POST", path)
    if not jwt_token:
        return None, "CDP facilitator auth unavailable (check CDP_API_KEY_ID/SECRET and cdp-sdk/PyJWT)", None
    try:
        resp = requests.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"Facilitator request error: {exc}", None
    try:
        data = resp.json()
    except Exception:
        data = None

    captured_headers = {}
    for h in ("EXTENSION-RESPONSES", "X-PAYMENT-RESPONSE", "PAYMENT-RESPONSE"):
        val = resp.headers.get(h)
        if val is not None:
            captured_headers[h] = val

    # Log Bazaar extension response status clearly
    ext_resp_raw = captured_headers.get("EXTENSION-RESPONSES")
    if ext_resp_raw:
        import base64
        import json
        try:
            decoded_bytes = base64.b64decode(ext_resp_raw)
            decoded_json = json.loads(decoded_bytes.decode('utf-8'))
            bazaar_resp = decoded_json.get("bazaar")
            if bazaar_resp:
                status = bazaar_resp.get("status")
                rejected_reason = bazaar_resp.get("rejectedReason")
                if status:
                    logger.info("Bazaar extension status: %s", status)
                if rejected_reason:
                    logger.info("Bazaar extension rejectedReason: %s", rejected_reason)
        except Exception as e:
            logger.debug("Failed to decode EXTENSION-RESPONSES: %s", e)

    if resp.status_code >= 400:
        detail = (data or {}).get("error") or (data or {}).get("message") or resp.text[:200]
        return data, f"Facilitator HTTP {resp.status_code}: {detail}", captured_headers
    return data, None, captured_headers


def facilitator_verify(
    payment_payload: Dict[str, Any],
    payment_requirements: Dict[str, Any],
    x402_version: int = 1,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    """
    POST /verify. Returns (is_valid, invalid_reason, extension_responses, captured_headers). The facilitator checks the
    EIP-3009 authorization against `payment_requirements` (scheme/network/asset/
    payTo/amount). We still run our own checks first; this is defense in depth and
    the path that lets CDP associate the resource.
    """
    data, err, headers = _post("verify", {
        "x402Version": x402_version,
        "paymentPayload": payment_payload,
        "paymentRequirements": payment_requirements,
    })
    if err:
        return False, err, None, headers
    if not isinstance(data, dict):
        return False, "Malformed facilitator verify response", None, headers
    ext_resp = data.get("extensionResponses") or data.get("extension_responses")
    if ext_resp:
        logger.info("CDP facilitator verify extension responses: %s", ext_resp)
    if data.get("isValid") is True:
        return True, None, ext_resp, headers
    return False, data.get("invalidReason") or "Facilitator reported payment invalid", ext_resp, headers


def facilitator_settle(
    payment_payload: Dict[str, Any],
    payment_requirements: Dict[str, Any],
    x402_version: int = 1,
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    """
    POST /settle. CDP broadcasts the transferWithAuthorization (and indexes the
    resource for the Bazaar). Returns (settlement_tx_hash, error, extension_responses, captured_headers).
    """
    data, err, headers = _post("settle", {
        "x402Version": x402_version,
        "paymentPayload": payment_payload,
        "paymentRequirements": payment_requirements,
    })
    if err:
        return None, err, None, headers
    if not isinstance(data, dict):
        return None, "Malformed facilitator settle response", None, headers
    ext_resp = data.get("extensionResponses") or data.get("extension_responses")
    if ext_resp:
        logger.info("CDP facilitator settle extension responses: %s", ext_resp)
    if data.get("success") is not True:
        return None, data.get("errorReason") or "Facilitator settlement failed", ext_resp, headers
    tx_hash = data.get("transaction") or data.get("txHash")
    if not tx_hash:
        return None, "Facilitator settled but returned no transaction hash", ext_resp, headers
    return tx_hash, None, ext_resp, headers
