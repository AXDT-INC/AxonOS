"""Structured logging for WebRTC lifecycle (connection, negotiation, health)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("axonos.webrtc")

_lock = threading.Lock()
_counters: dict[str, int] = {
    "negotiation_started": 0,
    "negotiation_ok": 0,
    "negotiation_failed": 0,
    "ice_client_posts": 0,
    "agent_answers": 0,
    "client_disconnect_events": 0,
    "fallback_to_novnc": 0,
}


def bump(key: str, n: int = 1) -> None:
    with _lock:
        _counters[key] = _counters.get(key, 0) + n


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def log_event(event: str, **fields: Any) -> None:
    """Structured single-line log for operators (no secrets)."""
    parts = [f"event={event}"]
    for k, v in sorted(fields.items()):
        if v is None:
            continue
        if k in ("credential", "password", "token", "sdp"):
            continue
        parts.append(f"{k}={v}")
    msg = " ".join(parts)
    if event.endswith("_failed") or event == "negotiation_error":
        logger.warning(msg)
    else:
        logger.info(msg)


def log_negotiation_start(session_id: str, wallet_masked: str) -> None:
    bump("negotiation_started")
    log_event("webrtc_negotiation_start", session_id=session_id[:16], wallet=wallet_masked)


def log_negotiation_ok(session_id: str, ms: Optional[float] = None) -> None:
    bump("negotiation_ok")
    log_event("webrtc_negotiation_ok", session_id=session_id[:16], ms=round(ms, 2) if ms else None)


def log_negotiation_failed(session_id: str, reason: str) -> None:
    bump("negotiation_failed")
    log_event("webrtc_negotiation_failed", session_id=session_id[:16], reason=reason[:500])


def log_agent_answer(session_id: str, wallet_masked: str) -> None:
    bump("agent_answers")
    log_event("webrtc_agent_answer", session_id=session_id[:16], wallet=wallet_masked)


def log_fallback(reason: str) -> None:
    bump("fallback_to_novnc")
    log_event("webrtc_fallback_novnc", reason=reason[:500])


def log_client_metrics(session_id: str, stats: dict[str, Any]) -> None:
    """Client-reported RTT / frames — summary only."""
    rtt = stats.get("rtt_ms")
    fps = stats.get("fps")
    lost = stats.get("packets_lost")
    state = stats.get("connection_state")
    log_event("webrtc_client_metrics", session_id=session_id[:16], rtt=rtt, fps=fps, lost=lost, state=state)


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0
