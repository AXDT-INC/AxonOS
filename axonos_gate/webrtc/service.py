"""Core WebRTC signaling logic (used by Flask gate and websockify handler)."""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import config, metrics, store

logger = logging.getLogger(__name__)

OwnerCheck = Callable[[str], bool]
AgentIdentityValidator = Callable[[int, str, str], Optional[dict[str, Any]]]


@dataclass(frozen=True)
class AgentScope:
    """Trusted compute identity produced by the central session database."""

    compute_session_id: int
    wallet_address: str
    fleet_key_required: bool


def scope_from_trusted_identity(
    identity: Any,
    *,
    fleet_key_required: bool = True,
) -> Optional[AgentScope]:
    """Build a scope from a server-side session-manager result."""
    if not isinstance(identity, dict):
        return None
    trusted_id = _positive_session_id(identity.get("id"))
    trusted_wallet = str(identity.get("wallet_address") or "").strip().lower()
    if trusted_id is None or not trusted_wallet:
        return None
    return AgentScope(trusted_id, trusted_wallet, fleet_key_required)


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


def resolve_agent_scope(
    compute_session_id: Any,
    wallet_address: str,
    agent_token: str,
    validator: Optional[AgentIdentityValidator],
) -> Optional[AgentScope]:
    """Validate untrusted agent headers and retain only trusted DB identity."""
    owner_id = _positive_session_id(compute_session_id)
    wallet = (wallet_address or "").strip().lower()
    token = (agent_token or "").strip()
    if owner_id is None or not wallet or not token or validator is None:
        return None
    try:
        trusted = validator(owner_id, wallet, token)
    except Exception:
        logger.exception("WebRTC agent identity validation failed")
        return None
    if not isinstance(trusted, dict):
        return None
    trusted_id = _positive_session_id(trusted.get("id"))
    trusted_wallet = str(trusted.get("wallet_address") or "").strip().lower()
    if trusted_id != owner_id or trusted_wallet != wallet:
        return None
    return AgentScope(
        compute_session_id=trusted_id,
        wallet_address=trusted_wallet,
        fleet_key_required=False,
    )


def _agent_authorized(agent_key: str, scope: Optional[AgentScope]) -> bool:
    if scope is None:
        return False
    if not scope.fleet_key_required:
        # Multi-session scopes already passed signed capability verification and
        # exact active-row validation. The fleet key stays central and is never
        # delegated to tenant containers.
        return True
    expected = config.agent_internal_key()
    supplied = (agent_key or "").strip()
    return bool(
        expected
        and supplied
        and secrets.compare_digest(expected, supplied)
    )


def _mask_wallet(w: str) -> str:
    w = (w or "").strip()
    if len(w) <= 10:
        return "***"
    return w[:6] + "…" + w[-4:]


def handle_config_public() -> tuple[int, dict[str, Any]]:
    out = dict(config.public_config())
    out["ice_servers"] = config.ice_servers_for_client()
    return 200, {"ok": True, **out}


def handle_create_session(
    wallet_norm: str,
    is_auth_valid: bool,
    active_compute_session_id: Optional[int],
    requested_compute_session_id: Any,
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not is_auth_valid:
        return 401, {"ok": False, "error": "Authentication required"}
    active_id = _positive_session_id(active_compute_session_id)
    requested_id = _positive_session_id(requested_compute_session_id)
    if requested_id is None:
        return 400, {"ok": False, "error": "Valid compute_session_id required"}
    if active_id is None:
        return 403, {"ok": False, "error": "Active session required (claim desktop first)"}
    if requested_id != active_id:
        return 409, {"ok": False, "error": "Desktop session changed; claim it again"}
    if not store.ensure_table():
        return 503, {"ok": False, "error": "WebRTC store unavailable"}
    sid = store.create_session(wallet_norm, active_id)
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
    active_compute_session_id: Optional[int],
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not is_auth_valid:
        return 401, {"ok": False, "error": "Authentication required"}
    active_id = _positive_session_id(active_compute_session_id)
    requested_id = _positive_session_id(body.get("compute_session_id"))
    if requested_id is None:
        return 400, {"ok": False, "error": "Valid compute_session_id required"}
    if active_id is None:
        return 403, {"ok": False, "error": "Not session owner"}
    if requested_id != active_id:
        return 409, {"ok": False, "error": "Desktop session changed; create a new offer"}
    sdp = (body.get("sdp") or "").strip()
    typ = (body.get("type") or "offer").strip().lower()
    if typ != "offer":
        return 400, {"ok": False, "error": "type must be offer"}
    if not config.validate_sdp(sdp):
        metrics.log_negotiation_failed(session_id, "invalid_sdp")
        return 400, {"ok": False, "error": "Invalid SDP"}
    row = store.get_row(session_id)
    if (
        not row
        or row["wallet_address"] != wallet_norm.strip().lower()
        or row.get("compute_session_id") != active_id
    ):
        return 403, {"ok": False, "error": "Invalid session"}
    now = time.time()
    if row["expires_at"] < now:
        return 410, {"ok": False, "error": "Session expired"}
    if not store.set_offer(session_id, wallet_norm, active_id, sdp, typ):
        return 409, {"ok": False, "error": "Could not store offer"}
    metrics.log_negotiation_start(session_id, _mask_wallet(wallet_norm))
    return 200, {"ok": True, "session_id": session_id}


def handle_get_status(
    session_id: str,
    wallet_norm: str,
    is_auth_valid: bool,
    active_compute_session_id: Optional[int],
    requested_compute_session_id: Any,
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not is_auth_valid:
        return 401, {"ok": False, "error": "Authentication required"}
    active_id = _positive_session_id(active_compute_session_id)
    requested_id = _positive_session_id(requested_compute_session_id)
    if requested_id is None:
        return 400, {"ok": False, "error": "Valid compute_session_id required"}
    if active_id is None:
        return 410, {
            "ok": False,
            "error": "Desktop session ended during WebRTC negotiation",
            "state": "closed",
        }
    if requested_id != active_id:
        return 409, {
            "ok": False,
            "error": "Desktop session changed; discard stale WebRTC status",
        }
    row = store.get_row(session_id)
    if (
        not row
        or row["wallet_address"] != wallet_norm.strip().lower()
        or row.get("compute_session_id") != active_id
    ):
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
    active_compute_session_id: Optional[int],
    body: Any,
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not is_auth_valid:
        return 401, {"ok": False, "error": "Authentication required"}
    active_id = _positive_session_id(active_compute_session_id)
    requested_id = (
        _positive_session_id(body.get("compute_session_id"))
        if isinstance(body, dict)
        else None
    )
    if requested_id is None:
        return 400, {"ok": False, "error": "Valid compute_session_id required"}
    if active_id is None:
        return 403, {"ok": False, "error": "Not session owner"}
    if requested_id != active_id:
        return 409, {"ok": False, "error": "Desktop session changed; discard stale ICE"}
    row = store.get_row(session_id)
    if (
        not row
        or row["wallet_address"] != wallet_norm.strip().lower()
        or row.get("compute_session_id") != active_id
    ):
        return 403, {"ok": False, "error": "Invalid session"}
    cands = config.ice_candidate_list_from_body(body)
    if body is not None and not cands and body not in ([], {}):
        return 400, {"ok": False, "error": "Invalid ICE payload"}
    if store.append_client_ice(session_id, wallet_norm, active_id, cands):
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


def handle_agent_next(
    agent_key: str,
    scope: Optional[AgentScope],
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not _agent_authorized(agent_key, scope):
        return 403, {"ok": False, "error": "Forbidden"}
    assert scope is not None
    job = store.fetch_next_pending_offer_for_agent(
        scope.compute_session_id,
        scope.wallet_address,
    )
    if not job:
        return 204, {}
    if (
        job.get("compute_session_id") != scope.compute_session_id
        or str(job.get("wallet_address") or "").strip().lower() != scope.wallet_address
    ):
        logger.error("WebRTC store returned an offer outside the authenticated compute scope")
        return 409, {"ok": False, "error": "Offer scope mismatch"}
    metrics.log_event(
        "webrtc_agent_claimed_offer",
        session_id=job["session_id"][:16],
        wallet=_mask_wallet(job.get("wallet_address", "")),
    )
    return 200, job


def handle_agent_answer(
    agent_key: str,
    scope: Optional[AgentScope],
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not _agent_authorized(agent_key, scope):
        return 403, {"ok": False, "error": "Forbidden"}
    assert scope is not None
    sid = (body.get("session_id") or "").strip()
    sdp = (body.get("sdp") or "").strip()
    typ = (body.get("type") or "answer").strip().lower()
    if typ != "answer":
        return 400, {"ok": False, "error": "type must be answer"}
    if not sid:
        return 400, {"ok": False, "error": "session_id required"}
    if not config.validate_sdp(sdp):
        store.mark_failed(
            sid,
            scope.compute_session_id,
            scope.wallet_address,
            "invalid_answer_sdp",
        )
        return 400, {"ok": False, "error": "Invalid answer"}
    if not store.set_answer(
        sid,
        scope.compute_session_id,
        scope.wallet_address,
        sdp,
        typ,
    ):
        return 409, {"ok": False, "error": "Could not store answer"}
    cands = config.ice_candidate_list_from_body(body.get("server_ice"))
    if cands:
        if not store.append_server_ice(
            sid,
            scope.compute_session_id,
            scope.wallet_address,
            cands,
        ):
            logger.warning("Could not store scoped server ICE for session=%s", sid[:16])
    metrics.log_agent_answer(sid, _mask_wallet(scope.wallet_address))
    metrics.log_negotiation_ok(sid)
    return 200, {"ok": True, "session_id": sid}


def handle_agent_row(
    agent_key: str,
    scope: Optional[AgentScope],
    session_id: str,
) -> tuple[int, dict[str, Any]]:
    """Agent polls signaling row (client ICE) without going through the browser."""
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not _agent_authorized(agent_key, scope):
        return 403, {"ok": False, "error": "Forbidden"}
    assert scope is not None
    if not session_id:
        return 400, {"ok": False, "error": "session_id required"}
    row = store.get_row_for_agent(
        session_id,
        scope.compute_session_id,
        scope.wallet_address,
    )
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


def handle_agent_fail(
    agent_key: str,
    scope: Optional[AgentScope],
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    if not config.webrtc_enabled():
        return 503, {"ok": False, "error": "WebRTC disabled"}
    if not _agent_authorized(agent_key, scope):
        return 403, {"ok": False, "error": "Forbidden"}
    assert scope is not None
    sid = (body.get("session_id") or "").strip()
    reason = (body.get("error") or "agent_failed").strip()
    if not sid:
        return 400, {"ok": False, "error": "session_id required"}
    if not store.mark_failed(
        sid,
        scope.compute_session_id,
        scope.wallet_address,
        reason,
    ):
        return 404, {"ok": False, "error": "Unknown session"}
    metrics.log_negotiation_failed(sid, reason)
    return 200, {"ok": True}
