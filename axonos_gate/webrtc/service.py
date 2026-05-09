"""Core WebRTC signaling logic (used by Flask gate and websockify handler)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from . import config, metrics, store

logger = logging.getLogger(__name__)

OwnerCheck = Callable[[str], bool]


def _mask_wallet(w: str) -> str:
    w = (w or "").strip()
    if len(w) <= 10:
        return "***"
    return w[:6] + "…" + w[-4:]


def handle_config_public() -> dict[str, Any]:
    out = dict(config.public_config())
    out["ice_servers"] = config.ice_servers_for_client()
    return {"ok": True, **out}


def handle_create_session(
    wallet_norm: str,
    is_auth_valid: bool,
    is_session_owner: bool,
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not is_auth_valid:
        return 401, {"ok": False, "error": "Authentication required"}
    if not is_session_owner:
        return 403, {"ok": False, "error": "Active session required (claim desktop first)"}
    if not store.ensure_table():
        return 503, {"ok": False, "error": "WebRTC store unavailable"}
    sid = store.create_session(wallet_norm)
    if not sid:
        return 503, {"ok": False, "error": "Could not create WebRTC session"}
    metrics.log_event("webrtc_session_created", session_id=sid[:16], wallet=_mask_wallet(wallet_norm))
    return 200, {
        "ok": True,
        "session_id": sid,
        "ice_servers": config.ice_servers_for_client(),
        "session_timeout_seconds": config.session_timeout_seconds(),
        "max_reconnect_attempts": config.max_reconnect_attempts(),
    }


def handle_post_offer(
    session_id: str,
    wallet_norm: str,
    is_auth_valid: bool,
    is_session_owner: bool,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not is_auth_valid:
        return 401, {"ok": False, "error": "Authentication required"}
    if not is_session_owner:
        return 403, {"ok": False, "error": "Not session owner"}
    sdp = (body.get("sdp") or "").strip()
    typ = (body.get("type") or "offer").strip().lower()
    if typ != "offer":
        return 400, {"ok": False, "error": "type must be offer"}
    if not config.validate_sdp(sdp):
        metrics.log_negotiation_failed(session_id, "invalid_sdp")
        return 400, {"ok": False, "error": "Invalid SDP"}
    row = store.get_row(session_id)
    if not row or row["wallet_address"] != wallet_norm.strip().lower():
        return 403, {"ok": False, "error": "Invalid session"}
    now = time.time()
    if row["expires_at"] < now:
        return 410, {"ok": False, "error": "Session expired"}
    if not store.set_offer(session_id, wallet_norm, sdp, typ):
        return 409, {"ok": False, "error": "Could not store offer"}
    metrics.log_negotiation_start(session_id, _mask_wallet(wallet_norm))
    return 200, {"ok": True, "session_id": session_id}


def handle_get_status(
    session_id: str,
    wallet_norm: str,
    is_auth_valid: bool,
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not is_auth_valid:
        return 401, {"ok": False, "error": "Authentication required"}
    row = store.get_row(session_id)
    if not row or row["wallet_address"] != wallet_norm.strip().lower():
        return 403, {"ok": False, "error": "Invalid session"}
    now = time.time()
    if row["expires_at"] < now:
        return 410, {"ok": False, "error": "Session expired", "state": "expired"}
    ice_out: list[Any] = []
    if row.get("server_ice"):
        try:
            ice_out = json.loads(row["server_ice"])
            if not isinstance(ice_out, list):
                ice_out = []
        except json.JSONDecodeError:
            ice_out = []
    ans = row.get("answer_sdp")
    st = row.get("state") or ""
    payload: dict[str, Any] = {
        "ok": True,
        "state": st,
        "has_answer": bool(ans),
    }
    if ans:
        payload["answer"] = {"type": "answer", "sdp": ans}
        payload["server_ice"] = ice_out
    if row.get("last_error"):
        payload["last_error"] = row["last_error"]
    return 200, payload


def handle_post_client_ice(
    session_id: str,
    wallet_norm: str,
    is_auth_valid: bool,
    body: Any,
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not is_auth_valid:
        return 401, {"ok": False, "error": "Authentication required"}
    row = store.get_row(session_id)
    if not row or row["wallet_address"] != wallet_norm.strip().lower():
        return 403, {"ok": False, "error": "Invalid session"}
    cands = config.ice_candidate_list_from_body(body)
    if body is not None and not cands and body not in ([], {}):
        return 400, {"ok": False, "error": "Invalid ICE payload"}
    if store.append_client_ice(session_id, wallet_norm, cands):
        metrics.bump("ice_client_posts")
        return 200, {"ok": True}
    return 400, {"ok": False, "error": "ICE update failed"}


def handle_post_client_metrics(
    session_id: str,
    wallet_norm: str,
    is_auth_valid: bool,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    if not is_auth_valid:
        return 401, {"ok": False, "error": "Authentication required"}
    row = store.get_row(session_id)
    if not row or row["wallet_address"] != wallet_norm.strip().lower():
        return 403, {"ok": False, "error": "Invalid session"}
    metrics.log_client_metrics(session_id, body or {})
    return 200, {"ok": True}


def handle_close(
    session_id: str,
    wallet_norm: str,
    is_auth_valid: bool,
) -> tuple[int, dict[str, Any]]:
    if not is_auth_valid:
        return 401, {"ok": False, "error": "Authentication required"}
    row = store.get_row(session_id)
    if not row or row["wallet_address"] != wallet_norm.strip().lower():
        return 403, {"ok": False, "error": "Invalid session"}
    store.close_session(session_id, wallet_norm)
    metrics.log_event("webrtc_session_closed", session_id=session_id[:16], wallet=_mask_wallet(wallet_norm))
    return 200, {"ok": True}


def handle_agent_next(agent_key: str) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    expected = config.agent_internal_key()
    if not expected or agent_key != expected:
        return 403, {"ok": False, "error": "Forbidden"}
    job = store.fetch_next_pending_offer_for_agent()
    if not job:
        return 204, {}
    metrics.log_event(
        "webrtc_agent_claimed_offer",
        session_id=job["session_id"][:16],
        wallet=_mask_wallet(job.get("wallet_address", "")),
    )
    return 200, job


def handle_agent_answer(
    agent_key: str,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    expected = config.agent_internal_key()
    if not expected or agent_key != expected:
        return 403, {"ok": False, "error": "Forbidden"}
    sid = (body.get("session_id") or "").strip()
    sdp = (body.get("sdp") or "").strip()
    typ = (body.get("type") or "answer").strip().lower()
    if typ != "answer":
        return 400, {"ok": False, "error": "type must be answer"}
    if not sid or not config.validate_sdp(sdp):
        store.mark_failed(sid, "invalid_answer_sdp")
        return 400, {"ok": False, "error": "Invalid answer"}
    if not store.set_answer(sid, sdp, typ):
        return 409, {"ok": False, "error": "Could not store answer"}
    cands = config.ice_candidate_list_from_body(body.get("server_ice"))
    if cands:
        store.append_server_ice(sid, cands)
    row = store.get_row(sid)
    w = row["wallet_address"] if row else ""
    metrics.log_agent_answer(sid, _mask_wallet(w))
    metrics.log_negotiation_ok(sid)
    return 200, {"ok": True, "session_id": sid}


def handle_agent_row(agent_key: str, session_id: str) -> tuple[int, dict[str, Any]]:
    """Agent polls signaling row (client ICE) without going through the browser."""
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    expected = config.agent_internal_key()
    if not expected or agent_key != expected:
        return 403, {"ok": False, "error": "Forbidden"}
    if not session_id:
        return 400, {"ok": False, "error": "session_id required"}
    row = store.get_row(session_id)
    if not row:
        return 404, {"ok": False, "error": "Unknown session"}
    ice_raw = row.get("client_ice") or "[]"
    try:
        ice = json.loads(ice_raw)
        if not isinstance(ice, list):
            ice = []
    except json.JSONDecodeError:
        ice = []
    return 200, {
        "ok": True,
        "state": row.get("state"),
        "client_ice": ice,
        "expires_at": row.get("expires_at"),
    }


def handle_agent_fail(agent_key: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    expected = config.agent_internal_key()
    if not expected or agent_key != expected:
        return 403, {"ok": False, "error": "Forbidden"}
    sid = (body.get("session_id") or "").strip()
    reason = (body.get("error") or "agent_failed").strip()
    if sid:
        store.mark_failed(sid, reason)
        store.reset_agent_stale(sid)
        metrics.log_negotiation_failed(sid, reason)
    return 200, {"ok": True}
