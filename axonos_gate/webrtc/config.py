"""WEBRTC_* environment configuration (ICE/STUN/TURN, timeouts, feature flags)."""

from __future__ import annotations

import json
import os
import re
from typing import Any

# RFC 4566 SDP session descriptions are typically well under this; cap defensively.
_MAX_SDP_CHARS = 256_000


def webrtc_enabled() -> bool:
    return (os.getenv("WEBRTC_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def fallback_enabled() -> bool:
    raw = (os.getenv("WEBRTC_FALLBACK_ENABLED") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def session_timeout_seconds() -> int:
    raw = (os.getenv("WEBRTC_SESSION_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 600
    try:
        n = int(raw)
        return max(60, min(86400, n))
    except ValueError:
        return 600


def max_reconnect_attempts() -> int:
    raw = (os.getenv("WEBRTC_MAX_RECONNECT_ATTEMPTS") or "").strip()
    if not raw:
        return 5
    try:
        n = int(raw)
        return max(0, min(50, n))
    except ValueError:
        return 5


def rate_limit_per_minute() -> int:
    raw = (os.getenv("WEBRTC_SIGNAL_RATE_LIMIT_PER_MIN") or "60").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 60
    if n <= 0:
        return 0
    return min(600, max(5, n))


def agent_internal_key() -> str:
    return (os.getenv("WEBRTC_AGENT_INTERNAL_KEY") or "").strip()


def gate_internal_base_url() -> str:
    return (os.getenv("WEBRTC_GATE_INTERNAL_URL") or "http://127.0.0.1:8889").rstrip("/")


def parse_ice_url_list(env_name: str) -> list[str]:
    """Comma-separated stun:/turn: URLs (no credentials in URL for turn here — use TURN user env)."""
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p and p.startswith(("stun:", "turn:", "turns:"))]


def ice_servers_for_client() -> list[dict[str, Any]]:
    """Return RTCPeerConnectionConfiguration.iceServers (no raw secrets in logs from callers)."""
    stuns = parse_ice_url_list("WEBRTC_STUN_URLS")
    turns = parse_ice_url_list("WEBRTC_TURN_URLS")
    user = (os.getenv("WEBRTC_TURN_USERNAME") or "").strip()
    credential = (os.getenv("WEBRTC_TURN_CREDENTIAL") or "").strip()

    servers: list[dict[str, Any]] = []
    for url in stuns:
        servers.append({"urls": url})
    for url in turns:
        entry: dict[str, Any] = {"urls": url}
        if user:
            entry["username"] = user
        if credential:
            entry["credential"] = credential
        servers.append(entry)
    if not servers:
        servers.append({"urls": "stun:stun.l.google.com:19302"})
    return servers


def public_config() -> dict[str, Any]:
    """Subset exposed via GET /api/config and GET /api/webrtc/config."""
    return {
        "webrtc_enabled": webrtc_enabled(),
        "webrtc_fallback_enabled": fallback_enabled(),
        "webrtc_session_timeout_seconds": session_timeout_seconds(),
        "webrtc_max_reconnect_attempts": max_reconnect_attempts(),
    }


def validate_sdp(sdp: str) -> bool:
    if not sdp or not isinstance(sdp, str):
        return False
    if len(sdp) > _MAX_SDP_CHARS:
        return False
    if "\x00" in sdp:
        return False
    # Minimal sanity: SDP starts with v= or offer/answer marker
    head = sdp.lstrip()[:32]
    return head.startswith("v=") or "SDP" in sdp[:120]


def validate_ice_candidate_obj(obj: Any) -> bool:
    if obj is None:
        return True
    if not isinstance(obj, dict):
        return False
    cand = obj.get("candidate")
    if cand is not None:
        if not isinstance(cand, str) or len(cand) > 20_000:
            return False
        if "\x00" in cand:
            return False
    mid = obj.get("sdpMid")
    if mid is not None and (not isinstance(mid, str) or len(mid) > 256):
        return False
    mline = obj.get("sdpMLineIndex")
    if mline is not None:
        if not isinstance(mline, int) or mline < 0 or mline > 255:
            return False
    return True


def ice_candidate_list_from_body(body: Any, max_items: int = 500) -> list[dict[str, Any]]:
    if body is None:
        return []
    if isinstance(body, list):
        items = body[:max_items]
        out = []
        for x in items:
            if isinstance(x, dict):
                out.append(x)
        return out if all(validate_ice_candidate_obj(x) for x in out) else []
    if isinstance(body, dict):
        if "candidates" in body:
            return ice_candidate_list_from_body(body.get("candidates"), max_items)
        if validate_ice_candidate_obj(body):
            return [body]
    return []


def dumps_config_summary_for_log() -> str:
    """Safe for logs — never includes TURN credentials."""
    return json.dumps(
        {
            "enabled": webrtc_enabled(),
            "stun_count": len(parse_ice_url_list("WEBRTC_STUN_URLS")),
            "turn_count": len(parse_ice_url_list("WEBRTC_TURN_URLS")),
            "turn_user_configured": bool((os.getenv("WEBRTC_TURN_USERNAME") or "").strip()),
        }
    )
