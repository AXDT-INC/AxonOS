"""
Session manager for AxonOS.

Profile-aware multi-session scheduling with exclusive full-GPU allocation and
per-session container lifecycle hooks. When GPUs are unavailable, claim fails
immediately (no waitlist).
"""

import logging
import os
import secrets
import subprocess
import time
from threading import Lock
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_SESSION_TABLE = "axgt_sessions"

# Namespace key for the per-wallet claim advisory lock (pg_advisory_xact_lock's
# two-int form). Serializes concurrent claims for one wallet so the UI's racing
# claims (vnc.html + ui.js) can't both pass the "no active session" check and
# spawn duplicate containers — a leaked container + a spurious failure response.
_CLAIM_ADVISORY_LOCK_NAMESPACE = 0x4158  # "AX"

_pg_init_done = False
_pg_init_lock = Lock()

_gpu_device_cache_last: Optional[List[int]] = None
_gpu_device_cache_until: float = 0.0


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _db_url() -> Optional[str]:
    return os.getenv("AXGT_CHALLENGE_DB_URL") or None


def _truthy(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _multi_session_enabled() -> bool:
    # Multiple wallet owners are safe only when each claim receives its own
    # runtime. Treat a shared-desktop deployment as single-session even if the
    # multi-session flag was accidentally left at its historical default.
    return _truthy("AXGT_USER_CONTAINER_ENABLED", False) and _truthy(
        "AXGT_MULTI_SESSION_ENABLED",
        True,
    )


def _gpu_profiles_enabled() -> bool:
    return _truthy("AXGT_GPU_PROFILES_ENABLED", True)


def _default_profile() -> str:
    return (os.getenv("AXGT_DEFAULT_GPU_PROFILE") or "small").strip().lower()


def _configured_profiles() -> Dict[str, int]:
    # Fixed public-beta profiles
    return {
        "small": 1,
        "medium": 2,
        "large": 4,
        "max": 8,
    }


# Human-facing profile names for user-visible messages. The lowercase keys above
# are the canonical wire/config identifiers and never change; these labels only
# affect display so the UI reads consistently (the frontend shows the same names).
_PROFILE_DISPLAY_LABELS = {
    "small": "Single",
    "medium": "Dual",
    "large": "Quad",
    "max": "Octa",
}


def _profile_display_label(profile: Optional[str]) -> str:
    if not profile:
        return str(profile)
    return _PROFILE_DISPLAY_LABELS.get(str(profile).strip().lower(), str(profile))


def _gpu_billing_enabled() -> bool:
    """When true, heartbeat billing multiplies wall-clock minutes by assigned GPU count."""
    if not _gpu_profiles_enabled():
        return False
    raw = (os.getenv("AXGT_GPU_WEIGHTED_BILLING") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def _billing_gpu_count(gpu_ids: Optional[List[int]], profile: Optional[str] = None) -> int:
    """GPU multiplier for usage billing (exclusive devices in this session)."""
    if gpu_ids:
        return max(1, len(gpu_ids))
    _, requested = _resolve_profile(profile)
    return max(1, requested)


def _usage_minutes_for_interval(
    wall_clock_minutes: float,
    gpu_ids: Optional[List[int]],
    profile: Optional[str] = None,
) -> float:
    if wall_clock_minutes <= 0:
        return 0.0
    if not _gpu_billing_enabled():
        return wall_clock_minutes
    return wall_clock_minutes * _billing_gpu_count(gpu_ids, profile)


def _prepaid_credit_allows_profile(
    wallet: str,
    requested_gpus: int,
    profile_name: str,
) -> Tuple[bool, Optional[str]]:
    """Require prepaid minutes > 0 and enough balance for at least one billed heartbeat."""
    deposit_ledger = _import_deposit_ledger()
    if not deposit_ledger.init_once():
        return False, "Billing unavailable. Cannot claim without deposit ledger."
    remaining = deposit_ledger.get_remaining_minutes(wallet)
    if remaining <= 0:
        return False, "No prepaid credit. Deposit AXGT and verify tx hash."
    if _gpu_billing_enabled() and remaining < requested_gpus:
        return (
            False,
            (
                f"Insufficient prepaid credit for profile \"{_profile_display_label(profile_name)}\" ({requested_gpus} GPU(s)). "
                f"You have {remaining:.1f} minute(s) but need at least {requested_gpus} "
                f"(usage bills at {requested_gpus}× wall-clock minutes per GPU)."
            ),
        )
    return True, None


def billing_context_for_wallet(wallet_address: str) -> Dict[str, Any]:
    """Active-session GPU billing context for wallet-status / UI warnings."""
    enabled = _gpu_billing_enabled()
    ctx: Dict[str, Any] = {
        "gpu_billing_enabled": enabled,
        "billing_gpu_count": 1,
        "gpu_profiles": _configured_profiles() if enabled else None,
    }
    if not enabled or not _init_once():
        return ctx
    wallet = (wallet_address or "").strip().lower()
    if not wallet:
        return ctx
    conn = _get_connection()
    if not conn:
        return ctx
    try:
        with conn.cursor() as cur:
            owned = _active_session_for_wallet(cur, wallet)
        if owned:
            gpu_ids = owned.get("gpu_ids") or []
            profile = owned.get("requested_profile")
            count = _billing_gpu_count(gpu_ids, profile)
            ctx["billing_gpu_count"] = count
            ctx["requested_profile"] = profile or "small"
    except Exception as exc:
        logger.debug("billing_context_for_wallet failed: %s", exc)
    finally:
        conn.close()
    return ctx


def _explicit_gpu_ids_from_env() -> Optional[List[int]]:
    """Return GPU indices from env if configured; None means use auto-detect or fallback."""
    raw = (os.getenv("AXGT_GPU_DEVICE_IDS") or "").strip()
    if raw:
        out: List[int] = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                out.append(int(token))
            except ValueError:
                logger.warning("session_manager: invalid AXGT_GPU_DEVICE_IDS token: %s", token)
        if out:
            return sorted(set(out))
    raw_count = (os.getenv("AXGT_GPU_TOTAL_COUNT") or "").strip()
    try:
        count = int(raw_count) if raw_count else 0
    except ValueError:
        count = 0
    if count > 0:
        return list(range(count))
    return None


def _gpu_device_cache_ttl_seconds() -> float:
    raw = (os.getenv("AXGT_GPU_DEVICE_CACHE_SECONDS") or "").strip()
    try:
        val = float(raw)
        if val >= 0:
            return val
    except (ValueError, TypeError):
        pass
    return 120.0


def _detect_nvidia_smi_gpu_indices(timeout: float = 5.0) -> Optional[List[int]]:
    """Enumerate GPU indices visible to this process (respects NVIDIA_VISIBLE_DEVICES in containers)."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("session_manager: nvidia-smi GPU discovery failed: %s", exc)
        return None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        logger.debug(
            "session_manager: nvidia-smi returned %s: %s",
            proc.returncode,
            err[:200] if err else "(no stderr)",
        )
        return None
    ids: List[int] = []
    for line in (proc.stdout or "").splitlines():
        part = line.strip().split(",")[0].strip()
        if not part:
            continue
        try:
            ids.append(int(float(part)))
        except ValueError:
            logger.debug("session_manager: skip unparseable nvidia-smi line: %r", line)
    ids = sorted(set(ids))
    return ids if ids else None


def reset_gpu_device_cache() -> None:
    """Clear cached auto-detected GPU list (e.g. after host reconfiguration)."""
    global _gpu_device_cache_last, _gpu_device_cache_until
    _gpu_device_cache_last = None
    _gpu_device_cache_until = 0.0


def _gpu_device_ids() -> List[int]:
    global _gpu_device_cache_last, _gpu_device_cache_until

    explicit = _explicit_gpu_ids_from_env()
    if explicit is not None:
        return explicit

    now = time.monotonic()
    if _gpu_device_cache_last is not None and now < _gpu_device_cache_until:
        return _gpu_device_cache_last

    if not _truthy("AXGT_GPU_AUTO_DETECT", True):
        _gpu_device_cache_last = [0]
        _gpu_device_cache_until = now + _gpu_device_cache_ttl_seconds()
        return _gpu_device_cache_last

    discovered = _detect_nvidia_smi_gpu_indices()
    discovery_source = "nvidia-smi"
    if not discovered:
        ln = _import_session_launcher()
        launcher_fn = getattr(ln, "enumerate_host_gpus_via_http", None)
        if callable(launcher_fn):
            discovered = launcher_fn()
            if discovered:
                discovery_source = "launcher"
    if discovered:
        logger.info(
            "session_manager: auto-detected %d GPU(s) via %s: %s",
            len(discovered),
            discovery_source,
            discovered,
        )
        _gpu_device_cache_last = discovered
    else:
        logger.info(
            "session_manager: GPU auto-detect found no devices; "
            "falling back to [0]. Set AXGT_GPU_DEVICE_IDS, AXGT_GPU_TOTAL_COUNT, or ensure "
            "the session launcher exposes GET /enumerate-gpus when the gate container has no GPUs."
        )
        _gpu_device_cache_last = [0]
    _gpu_device_cache_until = now + _gpu_device_cache_ttl_seconds()
    return _gpu_device_cache_last


def _resolve_profile(profile: Optional[str]) -> Tuple[str, int]:
    requested = (profile or "").strip().lower() or _default_profile()
    profiles = _configured_profiles()
    if not _gpu_profiles_enabled():
        return "small", 1
    if requested not in profiles:
        requested = _default_profile()
    if requested not in profiles:
        requested = "small"
    return requested, profiles[requested]


def _session_max_seconds() -> int:
    raw = (os.getenv("AXGT_SESSION_MAX_MINUTES") or "").strip()
    try:
        val = int(raw)
        if val > 0:
            return val * 60
    except (ValueError, TypeError):
        pass
    return 60 * 60  # default 60 min


def _remaining_minutes_for(wallet: str) -> Optional[float]:
    """Current prepaid remaining minutes for a wallet (for the SSH hard cap)."""
    try:
        try:
            from . import deposit_ledger
        except ImportError:
            try:
                from axonos_gate import deposit_ledger
            except ImportError:
                import deposit_ledger
        if not deposit_ledger.init_once():
            return None
        st = deposit_ledger.get_deposit_status(wallet)
        return float(st.get("remaining_minutes") or 0)
    except Exception as exc:
        logger.warning("_remaining_minutes_for failed: %s", exc)
        return None


def _ssh_hard_cap_seconds(remaining_minutes: Optional[float]) -> Optional[float]:
    """Hard billing cap for a headless/SSH session, in seconds from now.

    An SSH session bills for at most the time it can afford,
    optionally clamped to an operator ceiling (AXGT_SSH_MAX_SESSION_MINUTES). The
    in-container heartbeat daemon keeps a headless session alive with no natural
    "user left" signal, so without this an abandoned session would slide
    expires_at forward until the entire prepaid balance is drained.

    Returns seconds-from-now for the cap, or None to disable (no SSH cap).
    """
    # Operator ceiling (0/unset = no ceiling).
    ceiling_min = None
    raw = (os.getenv("AXGT_SSH_MAX_SESSION_MINUTES") or "").strip()
    if raw:
        try:
            n = float(raw)
            if n > 0:
                ceiling_min = n
        except (ValueError, TypeError):
            pass

    # Affordability: cap to the minutes the wallet can pay for (× GPU billing
    # already reflected in remaining_minutes deduction rate is per heartbeat, so
    # use remaining minutes directly as a wall-clock-ish bound).
    afford_min = remaining_minutes if (remaining_minutes is not None and remaining_minutes > 0) else None

    candidates = [m for m in (ceiling_min, afford_min) if m is not None]
    if not candidates:
        return None  # nothing to cap on (e.g. no ceiling + unknown balance)
    return min(candidates) * 60.0


def _heartbeat_timeout_seconds() -> int:
    raw = (os.getenv("AXGT_HEARTBEAT_TIMEOUT_SECONDS") or "").strip()
    try:
        val = int(raw)
        if val > 0:
            return val
    except (ValueError, TypeError):
        pass
    return 120  # default 2 min


def _session_cooldown_seconds() -> int:
    """Grace period after session release before the same wallet can reclaim."""
    raw = (os.getenv("AXGT_SESSION_COOLDOWN_SECONDS") or "").strip()
    try:
        val = int(raw)
        if val >= 0:
            return val
    except (ValueError, TypeError):
        pass
    return 0


def _preserve_session_on_credit_exhaust() -> bool:
    """Freeze an isolated tenant container when prepaid credit hits zero.

    The legacy shared-desktop runtime has no tenant boundary that can be frozen
    or destroyed without also stopping the central gate.  Never advertise a
    resumable pause there: arbitrary user processes could otherwise continue
    after billing stopped.
    """
    return _truthy("AXGT_USER_CONTAINER_ENABLED", False) and _truthy(
        "AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST",
        True,
    )


def _session_paused_max_seconds() -> int:
    """How long a frozen session (and its container) may be resumed."""
    raw = (os.getenv("AXGT_SESSION_PAUSED_MAX_MINUTES") or "").strip()
    try:
        minutes = int(raw)
        if minutes > 0:
            return minutes * 60
    except (ValueError, TypeError):
        pass
    return 2 * 60 * 60  # default 2 hours


def _lifecycle_transition_timeout_seconds() -> int:
    """Age after which cleanup may recover an interrupted pause/resume."""
    raw = (os.getenv("AXGT_SESSION_LIFECYCLE_TRANSITION_SECONDS") or "").strip()
    try:
        value = int(raw) if raw else 180
        return max(30, min(600, value))
    except (ValueError, TypeError):
        return 180


def _reset_script_path() -> Optional[str]:
    raw = (os.getenv("AXGT_SESSION_RESET_SCRIPT") or "").strip()
    if raw:
        return raw
    # Default path in container (Feature 2 Option A desktop reset)
    default = "/usr/local/bin/reset_session.sh"
    return default if os.path.isfile(default) else None


# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------

def _get_connection():
    url = _db_url()
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as exc:
        logger.warning("session_manager: Postgres connect failed: %s", exc)
        return None


def _import_deposit_ledger():
    """Works when loaded as package, as axonos_gate.*, or flat on sys.path (websockify_gate)."""
    try:
        from . import deposit_ledger
    except ImportError:
        try:
            from axonos_gate import deposit_ledger
        except ImportError:
            import deposit_ledger
    return deposit_ledger


def _import_session_launcher():
    """Works when loaded as package, as axonos_gate.*, or flat on sys.path."""
    try:
        from . import session_launcher
    except ImportError:
        try:
            from axonos_gate import session_launcher
        except ImportError:
            import session_launcher
    return session_launcher


def _import_webrtc_capability():
    """Import the capability codec in package and flat-script layouts."""
    try:
        from .webrtc import capability
    except ImportError:
        try:
            from axonos_gate.webrtc import capability
        except ImportError:
            from webrtc import capability
    return capability


def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_SESSION_TABLE} (
                id          SERIAL PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                requested_profile TEXT NOT NULL DEFAULT 'small',
                gpu_ids     TEXT,
                container_id TEXT,
                allocation_status TEXT NOT NULL DEFAULT 'allocated',
                started_at  DOUBLE PRECISION NOT NULL,
                last_heartbeat DOUBLE PRECISION NOT NULL,
                last_billed_at DOUBLE PRECISION,
                expires_at  DOUBLE PRECISION NOT NULL,
                status      TEXT NOT NULL DEFAULT 'active',
                files_key   TEXT,
                ssh_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                pause_reason TEXT,
                paused_at   DOUBLE PRECISION,
                runtime_paused BOOLEAN NOT NULL DEFAULT FALSE,
                transition_started_at DOUBLE PRECISION,
                transition_token TEXT
            )
        """)
        # Add last_billed_at if table existed from before migration
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s AND column_name = 'last_billed_at'
        """, (_SESSION_TABLE,))
        if cur.fetchone() is None:
            cur.execute(
                f"ALTER TABLE {_SESSION_TABLE} "
                "ADD COLUMN IF NOT EXISTS last_billed_at DOUBLE PRECISION"
            )
        for col_name, col_sql in (
            ("requested_profile", "TEXT NOT NULL DEFAULT 'small'"),
            ("gpu_ids", "TEXT"),
            ("container_id", "TEXT"),
            ("allocation_status", "TEXT NOT NULL DEFAULT 'allocated'"),
            ("files_key", "TEXT"),
            # Revocation metadata for the signed, purpose-specific WebRTC
            # capability. The bearer token itself is never stored in Postgres.
            ("webrtc_cap_jti_hash", "TEXT"),
            ("webrtc_cap_expires_at", "DOUBLE PRECISION"),
            # Non-sliding hard cap (unlike expires_at, which slides on heartbeat).
            # Set for headless/SSH sessions so an abandoned session can't drain the
            # whole prepaid balance. NULL = no cap (e.g. desktop, legacy rows).
            ("hard_expires_at", "DOUBLE PRECISION"),
            # Persisted SSH mode so a page reload / status query can tell a headless
            # SSH session from a desktop one without the client re-asserting intent.
            ("ssh_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
            # A paused row must say why it stopped and when its resumable TTL
            # began.  ``last_heartbeat`` remains presence evidence instead of
            # being overloaded as the pause timestamp.
            ("pause_reason", "TEXT"),
            ("paused_at", "DOUBLE PRECISION"),
            # Cross-process pause/resume is two-phase. These fields let cleanup
            # distinguish a confirmed frozen runtime from an interrupted
            # lifecycle transition without trusting browser presence.
            ("runtime_paused", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("transition_started_at", "DOUBLE PRECISION"),
            # Generation fence: a delayed lifecycle worker may mutate Docker
            # only while it still owns this exact token.
            ("transition_token", "TEXT"),
        ):
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
                """,
                (_SESSION_TABLE, col_name),
            )
            if cur.fetchone() is None:
                # Multiple central listeners initialize concurrently on startup;
                # IF NOT EXISTS makes their additive migrations race-safe.
                cur.execute(
                    f"ALTER TABLE {_SESSION_TABLE} "
                    f"ADD COLUMN IF NOT EXISTS {col_name} {col_sql}"
                )
                if col_name == "ssh_enabled":
                    # One-time backfill for rows that predate the column (they get
                    # the FALSE default): hard_expires_at was only ever set for
                    # requested_ssh claims, so it reliably marks old SSH sessions.
                    # Without this, an SSH session active across the upgrade would
                    # lose its connect-string recovery and cap renewal.
                    cur.execute(
                        f"UPDATE {_SESSION_TABLE} SET ssh_enabled = TRUE "
                        f"WHERE hard_expires_at IS NOT NULL"
                    )
        # Ensure no NULL last_billed_at: bill from session start (fixes pre-migration or old migrations)
        cur.execute(
            f"UPDATE {_SESSION_TABLE} SET last_billed_at = started_at WHERE last_billed_at IS NULL"
        )
        # Older rows used one generic paused state and did not record a cause.
        # Keep that uncertainty explicit; runtime reconciliation freezes these
        # containers before they can be resumed under the new semantics.
        cur.execute(
            f"""UPDATE {_SESSION_TABLE}
                SET pause_reason = COALESCE(pause_reason, 'legacy'),
                    paused_at = COALESCE(paused_at, last_heartbeat),
                    runtime_paused = CASE
                        WHEN pause_reason IS NULL THEN FALSE
                        ELSE runtime_paused
                    END
                WHERE status = 'paused'"""
        )
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{_SESSION_TABLE}_status
            ON {_SESSION_TABLE} (status)
        """)
    conn.commit()


def _init_once() -> bool:
    global _pg_init_done
    if not _db_url():
        return False
    with _pg_init_lock:
        if _pg_init_done:
            return True
        conn = _get_connection()
        if not conn:
            return False
        try:
            _ensure_tables(conn)
            _pg_init_done = True
            return True
        except Exception as exc:
            logger.warning("session_manager: table init failed: %s", exc)
            return False
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def session_grace_seconds() -> int:
    """Return the session grace period in seconds from AXGT_SESSION_GRACE_SECONDS (default 60)."""
    raw = (os.getenv("AXGT_SESSION_GRACE_SECONDS") or "").strip()
    try:
        val = int(raw)
        if val >= 0:
            return val
    except (ValueError, TypeError):
        pass
    return 60


def _expire_stale_session(cur, now: float) -> Tuple[List[tuple], List[tuple]]:
    """End stale active sessions.

    Every compute container now owns its heartbeat, including desktop runtimes,
    so browser Detach/reload is not a liveness signal. A missed runtime heartbeat
    means the container/control path is unhealthy and is ended; only verified
    credit exhaustion enters the resumable frozen state.

    Final usage is settled through ``now`` on the same transaction/cursor as
    the active-to-ended compare-and-swap. Returns (ended_sessions,
    paused_sessions).
    """
    hb_cutoff = now - _heartbeat_timeout_seconds()
    grace = session_grace_seconds()
    cur.execute(
        f"""WITH stale AS (
                SELECT id,
                       wallet_address,
                       COALESCE(last_billed_at, started_at) AS bill_from,
                       started_at,
                       requested_profile,
                       gpu_ids
                FROM {_SESSION_TABLE}
                WHERE status = 'active'
                  AND (last_heartbeat < %s
                       OR expires_at <= %s
                       OR (hard_expires_at IS NOT NULL AND hard_expires_at + %s <= %s))
                FOR UPDATE
            )
            UPDATE {_SESSION_TABLE} AS target
            SET status = 'ended', last_billed_at = %s
            FROM stale
            WHERE target.id = stale.id AND target.status = 'active'
            RETURNING stale.wallet_address,
                      target.id,
                      stale.bill_from,
                      stale.started_at,
                      stale.requested_profile,
                      stale.gpu_ids""",
        (hb_cutoff, now, grace, now, now),
    )
    rows = list(cur.fetchall() or [])
    rows.sort(key=lambda row: (str(row[0]).lower(), int(row[1])))
    if rows:
        deposit_ledger = _import_deposit_ledger()
        ledger_ready = bool(deposit_ledger.init_once())
        if not ledger_ready:
            # Stop fail-closed even if accounting is unavailable. Leaving a
            # stale compute runtime active would turn a ledger outage into
            # unlimited free execution.
            logger.error(
                "session_manager: ending %d stale session(s) without final usage settlement; ledger unavailable",
                len(rows),
            )
        for row in rows:
            wallet, session_id = str(row[0]).lower(), int(row[1])
            bill_from = row[2] if row[2] is not None else row[3]
            wall_minutes = max(0.0, now - float(bill_from)) / 60.0
            gpu_ids = _parse_gpu_ids(row[5])
            profile = row[4] or "small"
            minutes_delta = _usage_minutes_for_interval(
                wall_minutes, gpu_ids, profile
            )
            if not ledger_ready or minutes_delta <= 0:
                continue
            ok, _remaining, error = deposit_ledger._deduct_usage_on_cursor(
                cur,
                wallet,
                minutes_delta,
                session_id=str(session_id),
            )
            if not ok:
                logger.error(
                    "session_manager: final stale usage settlement failed for session %s: %s",
                    session_id,
                    error or "unknown ledger error",
                )
    ended = [(str(row[0]).lower(), int(row[1])) for row in rows]
    return ended, []


def _normalize_ended_sessions(ended: Any) -> List[Tuple[str, int]]:
    """Accept the new ended-row list plus legacy single tuples from callers/tests."""
    if not ended:
        return []
    if (
        isinstance(ended, tuple)
        and len(ended) >= 2
        and not isinstance(ended[0], (tuple, list))
    ):
        return [(str(ended[0]).lower(), int(ended[1]))]
    normalized: List[Tuple[str, int]] = []
    for row in ended:
        if row and len(row) >= 2:
            normalized.append((str(row[0]).lower(), int(row[1])))
    return normalized


def _apply_stale_session_maintenance(ended: Any, paused_sessions: List[tuple]) -> None:
    for paused in paused_sessions:
        # Two-element tuples are accepted for compatibility with callers/tests
        # created before pause cause and container identity were persisted.
        wallet, session_id = paused[0], paused[1]
        container_id = paused[2] if len(paused) > 2 else None
        pause_reason = paused[3] if len(paused) > 3 else "heartbeat_timeout"
        _on_session_credit_paused(wallet, session_id, container_id, pause_reason)
    for wallet_ended, session_id_ended in _normalize_ended_sessions(ended):
        _on_session_ended(wallet_ended, session_id_ended)


def _cleanup_after_stale_maintenance(
    ended: Any,
    paused_sessions: List[tuple],
    paused_ttl_ended: Optional[List[tuple]] = None,
) -> None:
    """Post-commit hooks for stale session DB updates."""
    _apply_stale_session_maintenance(ended, paused_sessions)
    for wallet_ended, session_id_ended in paused_ttl_ended or []:
        logger.info(
            "session_manager: auto-ended stale session for %s",
            _mask(wallet_ended),
        )
        _on_session_ended(wallet_ended, session_id_ended)


def _parse_gpu_ids(text: Optional[str]) -> List[int]:
    if not text:
        return []
    out: List[int] = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return sorted(set(out))


def _serialize_gpu_ids(gpu_ids: List[int]) -> str:
    return ",".join(str(i) for i in sorted(set(gpu_ids)))


def _session_row_to_dict(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "wallet_address": row[1],
        "requested_profile": row[2] or "small",
        "gpu_ids": _parse_gpu_ids(row[3]),
        "container_id": row[4],
        "allocation_status": row[5] or "allocated",
        "started_at": row[6],
        "last_heartbeat": row[7],
        "last_billed_at": row[8],
        "expires_at": row[9],
        "files_key": row[10] if len(row) > 10 else None,
        "hard_expires_at": row[11] if len(row) > 11 else None,
        "ssh_enabled": bool(row[12]) if len(row) > 12 else False,
        "pause_reason": row[13] if len(row) > 13 else None,
        "paused_at": row[14] if len(row) > 14 else None,
        "runtime_paused": bool(row[15]) if len(row) > 15 else False,
        "transition_started_at": row[16] if len(row) > 16 else None,
        "status": row[17] if len(row) > 17 else None,
        "transition_token": row[18] if len(row) > 18 else None,
    }


def _get_active_rows(cur) -> List[Dict[str, Any]]:
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at, files_key, hard_expires_at,
                   ssh_enabled, pause_reason, paused_at, runtime_paused, transition_started_at,
                   status, transition_token
            FROM {_SESSION_TABLE}
            WHERE status = 'active'
            ORDER BY started_at ASC""",
    )
    rows = cur.fetchall() or []
    return [_session_row_to_dict(r) for r in rows]


def _get_paused_rows(cur, now: float) -> List[Dict[str, Any]]:
    """Frozen sessions still holding GPUs/container until their paused TTL expires."""
    cutoff = now - _session_paused_max_seconds()
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at, files_key, hard_expires_at,
                   ssh_enabled, pause_reason, paused_at, runtime_paused, transition_started_at,
                   status, transition_token
            FROM {_SESSION_TABLE}
            WHERE status = 'paused' AND COALESCE(paused_at, last_heartbeat) >= %s
            ORDER BY started_at ASC""",
        (cutoff,),
    )
    rows = cur.fetchall() or []
    return [_session_row_to_dict(r) for r in rows]


def _get_transition_rows(cur, now: float) -> List[Dict[str, Any]]:
    """Lifecycle transitions that still reserve their container and GPUs."""
    # A wedged transition is still a real container/GPU reservation. Cleanup
    # recovers it by generation token; capacity calculation must never make it
    # disappear merely because its lease is old.
    del now
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at, files_key, hard_expires_at,
                   ssh_enabled, pause_reason, paused_at, runtime_paused, transition_started_at,
                   status, transition_token
            FROM {_SESSION_TABLE}
            WHERE status IN ('pausing', 'resuming')
            ORDER BY started_at ASC""",
    )
    rows = cur.fetchall() or []
    return [_session_row_to_dict(r) for r in rows]


def _get_gpu_reserved_rows(cur, now: float) -> List[Dict[str, Any]]:
    """Active, frozen, and transitioning sessions all reserve GPU IDs."""
    return _get_active_rows(cur) + _get_paused_rows(cur, now) + _get_transition_rows(cur, now)


def _paused_session_for_wallet(cur, wallet: str, now: float) -> Optional[Dict[str, Any]]:
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at, files_key, hard_expires_at,
                   ssh_enabled, pause_reason, paused_at, runtime_paused, transition_started_at,
                   status, transition_token
            FROM {_SESSION_TABLE}
            WHERE status = 'paused' AND wallet_address = %s
              AND COALESCE(paused_at, last_heartbeat) >= %s
            ORDER BY started_at DESC
            LIMIT 1""",
        (wallet, now - _session_paused_max_seconds()),
    )
    row = cur.fetchone()
    return _session_row_to_dict(row) if row else None


def _get_active_row(cur) -> Optional[Dict[str, Any]]:
    rows = _get_active_rows(cur)
    if not rows:
        return None
    return rows[-1]


def _active_session_for_wallet(cur, wallet: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at, files_key, hard_expires_at,
                   ssh_enabled, pause_reason, paused_at, runtime_paused, transition_started_at,
                   status, transition_token
            FROM {_SESSION_TABLE}
            WHERE status = 'active' AND wallet_address = %s
            ORDER BY started_at DESC
            LIMIT 1""",
        (wallet,),
    )
    row = cur.fetchone()
    return _session_row_to_dict(row) if row else None


def _transition_session_for_wallet(cur, wallet: str) -> Optional[Dict[str, Any]]:
    """Return an owned pause/resume transition regardless of its age."""
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at, files_key, hard_expires_at,
                   ssh_enabled, pause_reason, paused_at, runtime_paused, transition_started_at,
                   status, transition_token
            FROM {_SESSION_TABLE}
            WHERE status IN ('pausing', 'resuming') AND wallet_address = %s
            ORDER BY started_at DESC
            LIMIT 1""",
        (wallet,),
    )
    row = cur.fetchone()
    return _session_row_to_dict(row) if row else None


def _allocated_gpu_ids(rows: List[Dict[str, Any]]) -> Set[int]:
    out: Set[int] = set()
    for row in rows:
        for gid in row.get("gpu_ids", []):
            out.add(int(gid))
    return out


def _free_gpu_ids(rows: List[Dict[str, Any]]) -> List[int]:
    all_gpu_ids = _gpu_device_ids()
    allocated = _allocated_gpu_ids(rows)
    return [gid for gid in all_gpu_ids if gid not in allocated]


def _choose_allocation(rows: List[Dict[str, Any]], requested_gpus: int) -> Optional[List[int]]:
    free_ids = _free_gpu_ids(rows)
    if len(free_ids) < requested_gpus:
        return None
    return free_ids[:requested_gpus]


def _gpu_capacity_fields(
    requested_gpus: int,
    active_rows: List[Dict[str, Any]],
    profile_name: str,
) -> Dict[str, Any]:
    """Human-oriented GPU capacity context for API + UI (multi-GPU allocation)."""
    total = len(_gpu_device_ids())
    free_n = len(_free_gpu_ids(active_rows))
    impossible = requested_gpus > total
    out: Dict[str, Any] = {
        "machine_total_gpus": total,
        "machine_free_gpus": free_n,
        "requested_gpus": requested_gpus,
        "profile_impossible_on_host": impossible,
    }
    if impossible:
        out["capacity_note"] = (
            f"This host exposes {total} GPU(s), but the \"{_profile_display_label(profile_name)}\" profile needs {requested_gpus}. "
            "Pick a smaller profile (Single = 1, Dual = 2, Quad = 4, Octa = 8 GPUs), then connect again."
        )
    elif free_n < requested_gpus:
        out["capacity_note"] = (
            f"Right now {free_n} GPU(s) are free; your profile needs {requested_gpus}. "
            "Try again later or choose a smaller profile."
        )
    return out


def _run_reset_script() -> None:
    script = _reset_script_path()
    if not script:
        return
    try:
        logger.info("session_manager: running reset script %s", script)
        subprocess.Popen(
            ["/bin/bash", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.warning("session_manager: reset script failed: %s", exc)


def _mask(addr: str) -> str:
    if not addr or len(addr) < 10:
        return "***"
    return f"{addr[:6]}...{addr[-4:]}"


def _new_transition_token() -> str:
    """Return an unguessable generation fence for one lifecycle operation."""
    return secrets.token_urlsafe(24)


def _end_after_runtime_pause_failure(
    wallet_address: str,
    session_id: int,
    transition_token: Optional[str] = None,
) -> bool:
    """Fence, stop, and end a transition only after removal is confirmed."""
    # Claim this exact transition generation before touching Docker. A delayed
    # pause worker must not stop a runtime that a newer Resume already owns.
    stop_token = _new_transition_token()
    claimed_at = time.time()
    conn = _get_connection()
    if not conn:
        logger.error(
            "session_manager: cannot fence failed pause for session %s; DB unavailable",
            session_id,
        )
        return False
    try:
        with conn.cursor() as cur:
            token_clause = (
                "transition_token = %s"
                if transition_token is not None
                else "transition_token IS NULL"
            )
            params: List[Any] = [stop_token, claimed_at, session_id]
            if transition_token is not None:
                params.append(transition_token)
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET status = 'pausing',
                        transition_token = %s,
                        transition_started_at = %s
                    WHERE id = %s
                      AND status IN ('pausing', 'paused', 'resuming')
                      AND {token_clause}
                    RETURNING id""",
                tuple(params),
            )
            claimed = cur.fetchone() is not None
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.error(
            "session_manager: could not fence failed pause for session %s: %s",
            session_id,
            exc,
        )
        claimed = False
    finally:
        conn.close()
    if not claimed:
        logger.info(
            "session_manager: stale failed-pause callback ignored for session %s",
            session_id,
        )
        return False

    launcher = _import_session_launcher()
    try:
        stopped = bool(
            launcher.stop_session(
                session_id=session_id,
                container_id=None,
                transition_token=stop_token,
            )
        )
    except Exception as exc:
        logger.error(
            "session_manager: runtime stop raised for session %s: %s",
            session_id,
            exc,
        )
        stopped = False
    if not stopped:
        # Leave the transition row reserved. Periodic cleanup retries it; it
        # must never be published as safely paused/ended on an ambiguous stop.
        logger.critical(
            "session_manager: session %s could neither pause nor confirm stop; retry required",
            session_id,
        )
        return False

    conn = _get_connection()
    marked_ended = False
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE {_SESSION_TABLE}
                        SET status = 'ended',
                            pause_reason = 'runtime_pause_failed',
                            transition_started_at = NULL,
                            transition_token = NULL
                        WHERE id = %s AND status = 'pausing'
                          AND transition_token = %s
                        RETURNING wallet_address""",
                    (session_id, stop_token),
                )
                marked_ended = cur.fetchone() is not None
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.error(
                "session_manager: could not mark failed pause for session %s ended: %s",
                session_id,
                exc,
            )
        finally:
            conn.close()

    # Runtime removal was already confirmed above. The normal hook records the
    # ledger event and remains idempotent if stop is called a second time.
    if marked_ended:
        _on_session_ended(wallet_address, session_id)
    return marked_ended


def _on_session_credit_paused(
    wallet_address: str,
    session_id: int,
    container_id: Optional[str] = None,
    pause_reason: str = "credit_exhausted",
    transition_token: Optional[str] = None,
) -> bool:
    """Complete ``pausing`` only after the runtime is verifiably frozen.

    The historical function name is retained for import/test compatibility.
    """
    # Lease this exact generation before the external pause. Cleanup may replace
    # an abandoned token, in which case this delayed callback becomes a no-op.
    operation_token = _new_transition_token()
    claimed_at = time.time()
    conn = _get_connection()
    if not conn:
        logger.error(
            "session_manager: cannot fence pause for session %s; DB unavailable",
            session_id,
        )
        return False
    try:
        with conn.cursor() as cur:
            token_clause = (
                "transition_token = %s"
                if transition_token is not None
                else "transition_token IS NULL"
            )
            params: List[Any] = [
                operation_token,
                claimed_at,
                pause_reason,
                session_id,
                wallet_address.lower(),
            ]
            if transition_token is not None:
                params.append(transition_token)
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET status = 'pausing',
                        transition_token = %s,
                        transition_started_at = %s,
                        pause_reason = COALESCE(pause_reason, %s)
                    WHERE id = %s
                      AND wallet_address = %s
                      AND status IN ('pausing', 'paused')
                      AND {token_clause}
                    RETURNING id""",
                tuple(params),
            )
            claimed = cur.fetchone() is not None
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.error(
            "session_manager: could not fence pause for session %s: %s",
            session_id,
            exc,
        )
        claimed = False
    finally:
        conn.close()
    if not claimed:
        logger.info(
            "session_manager: stale pause callback ignored for session %s",
            session_id,
        )
        return False

    if not _ensure_session_runtime_paused(
        session_id, container_id, operation_token
    ):
        logger.error(
            "session_manager: could not freeze session %s (%s); ending it fail-closed",
            session_id,
            pause_reason,
        )
        _end_after_runtime_pause_failure(
            wallet_address, session_id, operation_token
        )
        return False

    confirmed_at = time.time()
    conn = _get_connection()
    if not conn:
        logger.error(
            "session_manager: runtime frozen but pause finalization DB unavailable for session %s",
            session_id,
        )
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET status = 'paused',
                        runtime_paused = TRUE,
                        paused_at = COALESCE(paused_at, %s),
                        last_billed_at = %s,
                        transition_started_at = NULL,
                        transition_token = NULL
                    WHERE id = %s
                      AND wallet_address = %s
                      AND status = 'pausing'
                      AND transition_token = %s
                    RETURNING id""",
                (
                    confirmed_at,
                    confirmed_at,
                    session_id,
                    wallet_address.lower(),
                    operation_token,
                ),
            )
            finalized = cur.fetchone() is not None
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.error(
            "session_manager: could not finalize frozen session %s: %s",
            session_id,
            exc,
        )
        finalized = False
    finally:
        conn.close()
    if not finalized:
        # The container is safely frozen. Keep/recover the transition on the
        # next cleanup cycle rather than claiming resumability prematurely.
        return False

    deposit_ledger = _import_deposit_ledger()
    if deposit_ledger.init_once():
        remaining = deposit_ledger.get_remaining_minutes(wallet_address)
        deposit_ledger.record_session_expiry(
            wallet_address,
            minutes_deducted=0.0,
            balance_after_minutes=remaining,
            session_id=str(session_id),
        )
    logger.info(
        "session_manager: session %s frozen for %s (reason=%s)",
        session_id,
        _mask(wallet_address),
        pause_reason,
    )
    return True


def _ensure_session_runtime_paused(
    session_id: int,
    container_id: Optional[str] = None,
    transition_token: Optional[str] = None,
) -> bool:
    """Idempotently verify that a preserved session runtime is frozen."""
    launcher = _import_session_launcher()
    try:
        return bool(launcher.pause_session(
            session_id=session_id,
            container_id=container_id,
            transition_token=transition_token,
        ))
    except Exception as exc:
        logger.error(
            "session_manager: runtime pause raised for session %s: %s",
            session_id,
            exc,
        )
        return False


def _on_session_ended(wallet_address: str, session_id: int) -> None:
    """Record session expiry in ledger, stop container, and run reset script."""
    deposit_ledger = _import_deposit_ledger()
    if deposit_ledger.init_once():
        remaining = deposit_ledger.get_remaining_minutes(wallet_address)
        deposit_ledger.record_session_expiry(
            wallet_address,
            minutes_deducted=0.0,
            balance_after_minutes=remaining,
            session_id=str(session_id),
        )
    _cleanup_session_container(session_id)
    _run_reset_script()


def _expire_stale_paused_sessions(cur, now: float) -> List[tuple]:
    """End frozen sessions past resume TTL (container teardown)."""
    cutoff = now - _session_paused_max_seconds()
    grace = session_grace_seconds()
    cur.execute(
        f"""UPDATE {_SESSION_TABLE}
            SET status = 'ended'
            WHERE status = 'paused'
              AND (COALESCE(paused_at, last_heartbeat) < %s
                   OR (hard_expires_at IS NOT NULL AND hard_expires_at + %s <= %s))
            RETURNING wallet_address, id""",
        (cutoff, grace, now),
    )
    rows = cur.fetchall() or []
    return [(r[0], r[1]) for r in rows]


def _restore_paused_transition(
    wallet: str,
    session_id: int,
    container_id: Optional[str],
    transition_token: Optional[str] = None,
    pause_reason: str = "credit_exhausted",
) -> bool:
    """Fence and re-freeze a failed ``resuming`` generation."""
    compensation_token = _new_transition_token()
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET status = 'pausing',
                        transition_token = %s,
                        transition_started_at = %s
                    WHERE id = %s AND wallet_address = %s
                      AND status = 'resuming'
                      AND {('transition_token = %s' if transition_token is not None else 'transition_token IS NULL')}
                    RETURNING id""",
                (
                    (
                        compensation_token,
                        time.time(),
                        session_id,
                        wallet.lower(),
                        transition_token,
                    )
                    if transition_token is not None
                    else (
                        compensation_token,
                        time.time(),
                        session_id,
                        wallet.lower(),
                    )
                ),
            )
            claimed = cur.fetchone() is not None
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.error(
            "session_manager: could not restore paused transition for session %s: %s",
            session_id,
            exc,
        )
        claimed = False
    finally:
        conn.close()
    if not claimed:
        logger.info(
            "session_manager: stale resume compensation ignored for session %s",
            session_id,
        )
        return False
    return _on_session_credit_paused(
        wallet,
        session_id,
        container_id,
        pause_reason,
        compensation_token,
    )


def _resume_paused_session(
    cur,
    wallet: str,
    paused: Dict[str, Any],
    now: float,
    transition_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Finalize a verified runtime unfreeze without spawning a new container.

    The paused container's host processes are Docker-frozen, so they stop
    scheduling new work while billing is stopped. Resume unfreezes those same
    processes in the same container, profile, and GPU assignment. Docker pause is
    not a CUDA checkpoint or proof of GPU idleness: already-enqueued device work
    can continue, and a persistent or long-running kernel may run throughout the
    paused interval.
    The billing checkpoint advances atomically with reactivation so frozen wall
    time is never charged.
    Client-supplied ``requested_profile`` is ignored for resume.
    """
    paused_profile = (paused.get("requested_profile") or "small").strip().lower()
    assigned = list(paused.get("gpu_ids") or [])
    max_secs = _session_max_seconds()
    cur.execute(
        f"""UPDATE {_SESSION_TABLE}
            SET status = 'active',
                last_heartbeat = %s,
                last_billed_at = %s,
                expires_at = %s,
                pause_reason = NULL,
                paused_at = NULL,
                runtime_paused = FALSE,
                transition_started_at = NULL,
                transition_token = NULL
            WHERE id = %s AND status = 'resuming' AND runtime_paused = TRUE
              AND wallet_address = %s
              AND {('transition_token = %s' if transition_token is not None else 'transition_token IS NULL')}
            RETURNING id, gpu_ids, container_id, expires_at, requested_profile""",
        (
            (now, now, now + max_secs, paused["id"], wallet, transition_token)
            if transition_token is not None
            else (now, now, now + max_secs, paused["id"], wallet)
        ),
    )
    row = cur.fetchone()
    if not row:
        return {"granted": False, "reason": "Paused session no longer available"}
    session_id, gpu_ids_text, container_id, expires_at, stored_profile = row[0], row[1], row[2], row[3], row[4]
    assigned = _parse_gpu_ids(gpu_ids_text) or assigned
    profile_name = (stored_profile or paused_profile or "small").strip().lower()
    remaining = max(0, expires_at - now)
    logger.info(
        "session_manager: resumed paused session %s for %s (profile=%s, gpus=%s)",
        session_id,
        _mask(wallet),
        profile_name,
        assigned,
    )
    resp = {
        "granted": True,
        "resumed": True,
        "session_id": session_id,
        "requested_profile": profile_name,
        "assigned_gpu_ids": assigned,
        "container_id": container_id,
        "allocation_status": "allocated",
        "remaining_seconds": int(remaining),
    }
    if paused.get("ssh_enabled"):
        # Resume is an explicit owner action: renew the SSH hard cap (extend-only;
        # an uncapped session stays uncapped) and return the connect fields so an
        # agent or browser that lost state gets its endpoint back in the same
        # shape as a fresh claim — the client must not attempt a desktop connect.
        hard_expires_at = paused.get("hard_expires_at")
        if hard_expires_at is not None:
            cap_secs = _ssh_hard_cap_seconds(_remaining_minutes_for(wallet))
            if cap_secs is not None and now + cap_secs > hard_expires_at:
                cur.execute(
                    f"""UPDATE {_SESSION_TABLE} SET hard_expires_at = %s
                        WHERE id = %s AND status = 'active'""",
                    (now + cap_secs, session_id),
                )
                hard_expires_at = now + cap_secs
            resp["hard_cap_remaining_seconds"] = int(max(0, hard_expires_at - now))
        resp.update(_ssh_connection_fields(session_id))
    return resp


def _cleanup_session_container(session_id: int) -> None:
    launcher = _import_session_launcher()
    launcher.stop_session(session_id=session_id, container_id=None)


# Direct-SSH session template: each session gets one published TCP port -> container :22.
# Same deterministic per-session scheme as the WebRTC UDP blocks (port = base + id % N),
# and the launcher MUST publish the identical port for the connect-string to be valid.
_SSH_BASE_PORT = 42000
_SSH_MAX_SESSIONS = 50


def _ssh_port_for_session(session_id: int) -> int:
    return _SSH_BASE_PORT + (session_id % _SSH_MAX_SESSIONS)


def _ssh_public_host() -> str:
    return (os.getenv("AXGT_SSH_PUBLIC_HOST") or "").strip()


def _ssh_user() -> str:
    return (os.getenv("AXGT_SSH_USER") or "aXonian").strip() or "aXonian"


def _ssh_connection_fields(session_id: int) -> Dict[str, Any]:
    """Connection details the landing page renders into the SSH connect-string."""
    return {
        "ssh_enabled": True,
        "ssh_host": _ssh_public_host(),
        "ssh_port": _ssh_port_for_session(session_id),
        "ssh_user": _ssh_user(),
    }


def _spawn_session_container(
    session_id: int,
    wallet: str,
    profile: str,
    gpu_ids: List[int],
    template: Optional[str] = None,
    files_key: Optional[str] = None,
    ssh_enabled: bool = False,
    ssh_pubkey: Optional[str] = None,
    webrtc_agent_token: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    launcher = _import_session_launcher()
    return launcher.launch_session(
        session_id=session_id,
        wallet=wallet,
        profile=profile,
        gpu_ids=gpu_ids,
        template=template,
        files_key=files_key,
        ssh_enabled=ssh_enabled,
        ssh_pubkey=ssh_pubkey,
        webrtc_agent_token=webrtc_agent_token,
    )


def _issue_webrtc_agent_capability(
    cur,
    session_id: int,
    wallet: str,
    files_key: str,
) -> Optional[str]:
    """Mint and persist revocation metadata, returning only the bearer token."""
    try:
        issued = _import_webrtc_capability().issue(session_id, wallet, files_key)
    except Exception as exc:
        logger.warning(
            "WebRTC capability issuance failed for session %s: %s",
            session_id,
            exc,
        )
        return None
    if not isinstance(issued, dict):
        return None
    token = str(issued.get("token") or "").strip()
    jti_hash = str(issued.get("jti_hash") or "").strip()
    try:
        expires_at = float(issued.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if not token or len(jti_hash) != 64 or expires_at <= time.time():
        return None
    cur.execute(
        f"""UPDATE {_SESSION_TABLE}
            SET webrtc_cap_jti_hash = %s,
                webrtc_cap_expires_at = %s
            WHERE id = %s AND status = 'active'""",
        (jti_hash, expires_at, session_id),
    )
    return token


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_active_session() -> Optional[Dict[str, Any]]:
    """Return info about the current active session, or None."""
    if not _init_once():
        return None
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            now = time.time()
            ended, paused_credit = _expire_stale_session(cur, now)
            paused_ended = _expire_stale_paused_sessions(cur, now)
            if ended or paused_credit or paused_ended:
                conn.commit()
                _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)
            rows = _get_active_rows(cur)
            if not rows:
                return None
            if _multi_session_enabled():
                return rows[-1]
            return rows[-1]
    except Exception as exc:
        logger.warning("get_active_session failed: %s", exc)
        return None
    finally:
        conn.close()


def get_session_for_wallet(wallet_address: str) -> Optional[Dict[str, Any]]:
    """Active or frozen session row for *wallet_address*, or None.

    Paused sessions are included so wallets can still retrieve files while
    their container survives the paused TTL.
    """
    wallet = (wallet_address or "").strip().lower()
    if not wallet or not _init_once():
        return None
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            session = _active_session_for_wallet(cur, wallet)
            if session is None:
                session = _paused_session_for_wallet(cur, wallet, time.time())
            return session
    except Exception as exc:
        logger.warning("get_session_for_wallet failed: %s", exc)
        return None
    finally:
        conn.close()


def try_claim_session(
    wallet_address: str,
    requested_profile: Optional[str] = None,
    requested_template: Optional[str] = None,
    requested_ssh: bool = False,
    ssh_pubkey: Optional[str] = None,
) -> Dict[str, Any]:
    """Attempt to claim the desktop session for *wallet_address*.

    Returns a dict with at least ``granted`` (bool).  On failure, includes
    ``active_wallet`` (masked) when another session blocks access.
    """
    wallet = wallet_address.lower()
    if not _truthy("AXGT_USER_CONTAINER_ENABLED", False):
        # A shared desktop cannot provide a trustworthy per-tenant stop/freeze
        # boundary.  In particular, its historical reset script does not stop
        # arbitrary CPU/CUDA jobs, so accepting a metered claim could leave work
        # running after the database says billing ended.  Fail before touching
        # credit, allocation, or session state.
        return {
            "granted": False,
            "configuration_error": True,
            "reason": (
                "Paid sessions require isolated user containers "
                "(AXGT_USER_CONTAINER_ENABLED=true)."
            ),
        }
    profile_name, requested_gpus = _resolve_profile(requested_profile)
    if not _init_once():
        return {"granted": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"granted": False, "reason": "Session DB unavailable"}
    try:
        with conn.cursor() as cur:
            now = time.time()

            ended, paused_credit = _expire_stale_session(cur, now)
            paused_ended = _expire_stale_paused_sessions(cur, now)
            if ended or paused_credit or paused_ended:
                conn.commit()
                _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)

            # Serialize concurrent claims for the same wallet. The UI fires two
            # claims that race (vnc.html + ui.js); without this both can read
            # "no active session", INSERT, and spawn duplicate containers — one
            # leaks and one branch can surface a spurious failure. Taken AFTER
            # the stale-expiry commit above (which would otherwise release it)
            # and held through the INSERT+commit below; auto-releases on commit.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), %s)",
                (wallet, _CLAIM_ADVISORY_LOCK_NAMESPACE),
            )

            active_rows = _get_active_rows(cur)
            paused_rows = _get_paused_rows(cur, now)
            transition_rows = _get_transition_rows(cur, now)
            reserved_rows = active_rows + paused_rows + transition_rows
            active = active_rows[-1] if active_rows else None
            blocking = active if active else (
                paused_rows[-1] if paused_rows else (
                    transition_rows[-1] if transition_rows else None
                )
            )

            is_owner = _active_session_for_wallet(cur, wallet) is not None
            transition_owned = next(
                (
                    row
                    for row in transition_rows
                    if (row.get("wallet_address") or "").lower() == wallet
                ),
                None,
            )
            if transition_owned:
                # A second Resume/claim must observe ownership of the existing
                # lifecycle operation. Falling through here could allocate a
                # second container for the same wallet while its first one is
                # still being frozen or restored.
                lifecycle_state = transition_owned.get("status") or "pausing"
                conn.commit()
                return {
                    "granted": False,
                    "reason": (
                        "Session resume is in progress"
                        if lifecycle_state == "resuming"
                        else "Session pause is in progress"
                    ),
                    "lifecycle_in_progress": True,
                    "lifecycle_state": lifecycle_state,
                    "session_id": transition_owned["id"],
                    "container_id": transition_owned.get("container_id"),
                }
            paused = _paused_session_for_wallet(cur, wallet, now)
            if not is_owner and paused and _preserve_session_on_credit_exhaust():
                paused_profile = (paused.get("requested_profile") or "small").strip().lower()
                _, paused_gpus = _resolve_profile(paused_profile)
                ok_credit, credit_reason = _prepaid_credit_allows_profile(
                    wallet, paused_gpus, paused_profile
                )
                if not ok_credit:
                    conn.commit()
                    return {"granted": False, "reason": credit_reason}

                # Legacy rows predate verified runtime freezing. Secure them
                # before beginning Resume; the wallet advisory lock prevents a
                # second claim from racing this migration path.
                if not paused.get("runtime_paused"):
                    legacy_token = _new_transition_token()
                    cur.execute(
                        f"""UPDATE {_SESSION_TABLE}
                            SET status = 'pausing',
                                transition_started_at = %s,
                                transition_token = %s
                            WHERE id = %s AND wallet_address = %s
                              AND status = 'paused' AND runtime_paused = FALSE
                            RETURNING id""",
                        (time.time(), legacy_token, paused["id"], wallet),
                    )
                    legacy_transition = cur.fetchone() is not None
                    conn.commit()
                    if not legacy_transition or not _on_session_credit_paused(
                        wallet,
                        paused["id"],
                        paused.get("container_id"),
                        paused.get("pause_reason") or "legacy",
                        legacy_token,
                    ):
                        return {
                            "granted": False,
                            "reason": "Saved session is still being secured; retry Resume",
                            "paused_for_resume": True,
                        }
                    # Start a fresh transaction/lock after the external pause.
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s), %s)",
                        (wallet, _CLAIM_ADVISORY_LOCK_NAMESPACE),
                    )
                    paused = _paused_session_for_wallet(cur, wallet, time.time())
                    if not paused or not paused.get("runtime_paused"):
                        conn.commit()
                        return {
                            "granted": False,
                            "reason": "Saved session is not ready to resume",
                            "paused_for_resume": True,
                        }

                transition_at = time.time()
                resume_token = _new_transition_token()
                cur.execute(
                    f"""UPDATE {_SESSION_TABLE}
                        SET status = 'resuming',
                            transition_started_at = %s,
                            last_heartbeat = %s,
                            transition_token = %s
                        WHERE id = %s AND wallet_address = %s
                          AND status = 'paused' AND runtime_paused = TRUE
                        RETURNING id""",
                    (
                        transition_at,
                        transition_at,
                        resume_token,
                        paused["id"],
                        wallet,
                    ),
                )
                if cur.fetchone() is None:
                    conn.commit()
                    return {
                        "granted": False,
                        "reason": "Saved session lifecycle changed; retry Resume",
                        "paused_for_resume": True,
                    }
                conn.commit()

                launcher = _import_session_launcher()
                runtime_resumed = False
                try:
                    runtime_resumed = bool(
                        launcher.resume_session(
                            session_id=paused["id"],
                            container_id=paused.get("container_id"),
                            transition_token=resume_token,
                        )
                    )
                except Exception as exc:
                    logger.error(
                        "session_manager: runtime resume raised for session %s: %s",
                        paused["id"],
                        exc,
                    )
                if not runtime_resumed:
                    _restore_paused_transition(
                        wallet,
                        paused["id"],
                        paused.get("container_id"),
                        resume_token,
                        paused.get("pause_reason") or "credit_exhausted",
                    )
                    return {
                        "granted": False,
                        "reason": "Saved session could not be resumed",
                        "paused_for_resume": True,
                    }

                try:
                    resume = _resume_paused_session(
                        cur, wallet, paused, time.time(), resume_token
                    )
                    if not resume.get("granted"):
                        conn.rollback()
                        _restore_paused_transition(
                            wallet,
                            paused["id"],
                            paused.get("container_id"),
                            resume_token,
                            paused.get("pause_reason") or "credit_exhausted",
                        )
                    else:
                        conn.commit()
                except Exception:
                    conn.rollback()
                    _restore_paused_transition(
                        wallet,
                        paused["id"],
                        paused.get("container_id"),
                        resume_token,
                        paused.get("pause_reason") or "credit_exhausted",
                    )
                    raise
                return resume

            if not is_owner:
                ok_credit, credit_reason = _prepaid_credit_allows_profile(
                    wallet, requested_gpus, profile_name
                )
                if not ok_credit:
                    conn.commit()
                    return {"granted": False, "reason": credit_reason}

            # Already owner in multi-session mode
            owned = _active_session_for_wallet(cur, wallet)
            if owned:
                remaining = max(0, owned["expires_at"] - now)
                # An explicit owner re-claim of an SSH session RENEWS its hard
                # billing cap: extend-only to max(current, now + min(affordable,
                # ceiling)). This is the deliberate "extend session" signal for
                # browsers (Extend button) and agents (re-POST claim / pay more
                # via x402) — a forgotten session has nobody to renew it, so the
                # anti-drain property of the cap is preserved. Sessions with no
                # cap (legacy rows / no ceiling configured) are left uncapped.
                hard_expires_at = owned.get("hard_expires_at")
                if owned.get("ssh_enabled") and hard_expires_at is not None:
                    cap_secs = _ssh_hard_cap_seconds(_remaining_minutes_for(wallet))
                    if cap_secs is not None and now + cap_secs > hard_expires_at:
                        cur.execute(
                            f"""UPDATE {_SESSION_TABLE}
                                SET hard_expires_at = %s
                                WHERE id = %s AND status = 'active'""",
                            (now + cap_secs, owned["id"]),
                        )
                        hard_expires_at = now + cap_secs
                        logger.info(
                            "session_manager: SSH hard cap renewed for session %s (%s): +%ds",
                            owned["id"], _mask(wallet), int(cap_secs),
                        )
                conn.commit()
                owned_resp = {
                    "granted": True,
                    "session_id": owned["id"],
                    "requested_profile": owned.get("requested_profile") or profile_name,
                    "assigned_gpu_ids": owned.get("gpu_ids", []),
                    "container_id": owned.get("container_id"),
                    "allocation_status": "allocated",
                    "remaining_seconds": int(remaining),
                }
                if hard_expires_at is not None:
                    owned_resp["hard_cap_remaining_seconds"] = int(max(0, hard_expires_at - now))
                # The stored ssh_enabled flag (not the client's requested_ssh) decides
                # whether SSH connect fields are returned: a reload with a stale SSH
                # toggle must not present an ssh connect-string for a desktop container,
                # and a reload that lost the toggle must still recover the SSH card.
                if owned.get("ssh_enabled"):
                    owned_resp.update(_ssh_connection_fields(owned["id"]))
                return owned_resp

            if (not _multi_session_enabled()) and blocking and blocking["wallet_address"] != wallet:
                conn.commit()
                return {
                    "granted": False,
                    "reason": "Desktop is in use by another researcher.",
                    "active_wallet": _mask(blocking["wallet_address"]),
                }

            if _multi_session_enabled():
                allocated_gpu_ids = _choose_allocation(reserved_rows, requested_gpus)
                if not allocated_gpu_ids:
                    cap_meta = _gpu_capacity_fields(requested_gpus, reserved_rows, profile_name)
                    conn.commit()
                    free_gpus = len(_free_gpu_ids(reserved_rows))
                    total_gpus = len(_gpu_device_ids())
                    if total_gpus > 0 and free_gpus == 0:
                        reason = "Desktop is in use by another researcher."
                    else:
                        reason = (
                            f"No GPUs available for profile \"{_profile_display_label(profile_name)}\" "
                            f"({requested_gpus} GPU(s) required)"
                        )
                    return {
                        "granted": False,
                        "allocation_status": "unavailable",
                        "requested_profile": profile_name,
                        "requested_gpus": requested_gpus,
                        "reason": reason,
                        "free_gpu_count": free_gpus,
                        **cap_meta,
                    }

                max_secs = _session_max_seconds()
                # Per-session secret for the in-container file agent; injected into
                # the container env at launch and used by the gate file proxy.
                files_key = secrets.token_urlsafe(32)
                # Hard billing cap for headless/SSH sessions (no browser "user left"
                # signal). expires_at slides on heartbeat (idle timeout); hard_expires_at
                # does NOT, bounding an abandoned session to min(affordable, ceiling).
                hard_expires_at = None
                if requested_ssh:
                    cap_secs = _ssh_hard_cap_seconds(_remaining_minutes_for(wallet))
                    if cap_secs is not None:
                        hard_expires_at = now + cap_secs
                cur.execute(
                    f"""INSERT INTO {_SESSION_TABLE}
                        (wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                         started_at, last_heartbeat, last_billed_at, expires_at, status, files_key, hard_expires_at,
                         ssh_enabled)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
                        RETURNING id""",
                    (
                        wallet,
                        profile_name,
                        _serialize_gpu_ids(allocated_gpu_ids),
                        None,
                        "allocating",
                        now,
                        now,
                        now,
                        now + max_secs,
                        files_key,
                        hard_expires_at,
                        bool(requested_ssh),
                    ),
                )
                session_id = cur.fetchone()[0]
                webrtc_agent_token = None
                if not requested_ssh:
                    failure_reason = None
                    if not _truthy("WEBRTC_ENABLED"):
                        failure_reason = "WebRTC desktop streaming is not configured"
                    else:
                        webrtc_agent_token = _issue_webrtc_agent_capability(
                            cur,
                            session_id,
                            wallet,
                            files_key,
                        )
                        if not webrtc_agent_token:
                            failure_reason = "Could not establish isolated desktop agent identity"
                    if failure_reason:
                        cur.execute(
                            f"""UPDATE {_SESSION_TABLE}
                                SET status = 'ended', allocation_status = 'failed'
                                WHERE id = %s AND status = 'active'""",
                            (session_id,),
                        )
                        conn.commit()
                        logger.warning(
                            "Desktop claim rejected before spawn for session %s: %s",
                            session_id,
                            failure_reason,
                        )
                        return {
                            "granted": False,
                            "allocation_status": "failed",
                            "requested_profile": profile_name,
                            "requested_gpus": requested_gpus,
                            "reason": failure_reason,
                        }
                conn.commit()

                spawned = False
                container_id = None
                spawn_error = None
                try:
                    spawned, container_id, spawn_error = _spawn_session_container(
                        session_id=session_id,
                        wallet=wallet,
                        profile=profile_name,
                        gpu_ids=allocated_gpu_ids,
                        template=requested_template,
                        files_key=files_key,
                        ssh_enabled=requested_ssh,
                        ssh_pubkey=ssh_pubkey,
                        webrtc_agent_token=webrtc_agent_token,
                    )
                except Exception as exc:
                    spawn_error = str(exc)
                    logger.warning(
                        "Session container spawn raised for session %s: %s",
                        session_id,
                        exc,
                    )

                finalized = False
                try:
                    conn2 = _get_connection()
                except Exception as exc:
                    conn2 = None
                    logger.warning(
                        "Could not open spawn-finalization DB connection for session %s: %s",
                        session_id,
                        exc,
                    )
                if conn2:
                    try:
                        with conn2.cursor() as cur2:
                            if spawned:
                                cur2.execute(
                                    f"""UPDATE {_SESSION_TABLE}
                                        SET container_id = %s, allocation_status = 'allocated'
                                        WHERE id = %s AND status = 'active'""",
                                    (container_id, session_id),
                                )
                                if cur2.rowcount != 1:
                                    raise RuntimeError(
                                        "allocation ended before spawn finalization"
                                    )
                            else:
                                cur2.execute(
                                    f"""UPDATE {_SESSION_TABLE}
                                        SET status = 'ended', allocation_status = 'failed'
                                        WHERE id = %s AND status = 'active'""",
                                    (session_id,),
                                )
                        conn2.commit()
                        finalized = True
                    except Exception as exc:
                        try:
                            conn2.rollback()
                        except Exception:
                            pass
                        logger.warning(
                            "Session spawn finalization failed for session %s: %s",
                            session_id,
                            exc,
                        )
                    finally:
                        conn2.close()
                if not finalized:
                    # The allocation row was committed before the external
                    # Docker call. Reuse the still-open primary connection as a
                    # best-effort fail transition if the finalizer is unavailable.
                    try:
                        cur.execute(
                            f"""UPDATE {_SESSION_TABLE}
                                SET status = 'ended', allocation_status = 'failed'
                                WHERE id = %s AND status = 'active'""",
                            (session_id,),
                        )
                        conn.commit()
                    except Exception as exc:
                        logger.warning(
                            "Could not mark unfinalized session %s failed: %s",
                            session_id,
                            exc,
                        )
                    if spawned:
                        spawn_error = spawn_error or "Could not finalize session allocation"
                    spawned = False
                if not spawned:
                    # Confirmed failure (the launcher's verify poll also found no
                    # running container). Reap any half-created container by its
                    # deterministic name so a partial spawn can't leak a GPU/ports
                    # and later starve real claims ("No GPUs available").
                    try:
                        _import_session_launcher().stop_session(
                            session_id=session_id, container_id=None
                        )
                    except Exception as exc:
                        logger.warning(
                            "try_claim_session: cleanup after failed spawn of session %s failed: %s",
                            session_id, exc,
                        )
                    return {
                        "granted": False,
                        "allocation_status": "failed",
                        "requested_profile": profile_name,
                        "requested_gpus": requested_gpus,
                        "reason": "Failed to start user container",
                        "container_error": spawn_error,
                    }
                granted = {
                    "granted": True,
                    "session_id": session_id,
                    "requested_profile": profile_name,
                    "assigned_gpu_ids": allocated_gpu_ids,
                    "container_id": container_id,
                    "allocation_status": "allocated",
                    "remaining_seconds": max_secs,
                }
                if hard_expires_at is not None:
                    granted["hard_cap_remaining_seconds"] = int(max(0, hard_expires_at - now))
                if requested_ssh:
                    granted.update(_ssh_connection_fields(session_id))
                return granted

            # Legacy single-session mode (explicitly disabled multi-session)
            max_secs = _session_max_seconds()
            cur.execute(
                f"""INSERT INTO {_SESSION_TABLE}
                    (wallet_address, started_at, last_heartbeat, last_billed_at, expires_at, status)
                    VALUES (%s, %s, %s, %s, %s, 'active')
                    RETURNING id""",
                    (wallet, now, now, now, now + max_secs),
            )
            session_id = cur.fetchone()[0]
        conn.commit()
        logger.info("session_manager: session granted to %s", _mask(wallet))
        return {
            "granted": True,
            "session_id": session_id,
            "remaining_seconds": max_secs,
        }
    except Exception as exc:
        conn.rollback()
        logger.warning("try_claim_session failed: %s", exc)
        return {"granted": False, "reason": "Internal error"}
    finally:
        conn.close()


def heartbeat(wallet_address: str, ssh_active: bool = False) -> Dict[str, Any]:
    """Update heartbeat for the active session owner; bill elapsed time from last_billed_at.

    ``ssh_active`` is reported by the in-container heartbeat daemon when at
    least one ESTABLISHED connection to the container's sshd exists. A present
    user renews the SSH hard billing cap exactly like an explicit re-claim
    (extend-only, still bounded by min(affordable, ceiling)), so interactive
    sessions never die under the operator ceiling while someone is connected,
    and abandoned ones still do.
    """
    wallet = wallet_address.lower()
    if not _init_once():
        return {"ok": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"ok": False, "reason": "Session DB unavailable"}
    try:
        from . import deposit_ledger
    except ImportError:
        try:
            from axonos_gate import deposit_ledger
        except ImportError:
            import deposit_ledger

    try:
        now = time.time()
        cur = conn.cursor()
        try:
            # Serialize active->pausing with same-wallet claim snapshots. Without
            # this shared lock a claim could read ACTIVE before this transaction,
            # miss PAUSING after it, and allocate a second container in between.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), %s)",
                (wallet, _CLAIM_ADVISORY_LOCK_NAMESPACE),
            )
            ended, paused_credit = _expire_stale_session(cur, now)
            paused_ended = _expire_stale_paused_sessions(cur, now)
            cur.execute(
                f"""SELECT id, last_billed_at, expires_at, started_at, requested_profile, gpu_ids, container_id,
                           hard_expires_at
                    FROM {_SESSION_TABLE}
                    WHERE status = 'active' AND wallet_address = %s
                    FOR UPDATE""",
                (wallet,),
            )
            row = cur.fetchone()
            if not row:
                transition = _transition_session_for_wallet(cur, wallet)
                paused = _paused_session_for_wallet(cur, wallet, now)
                conn.commit()
                _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)
                if transition:
                    lifecycle_state = transition.get("status") or "pausing"
                    return {
                        "ok": False,
                        "reason": (
                            "Session resume is in progress"
                            if lifecycle_state == "resuming"
                            else "Session pause is in progress"
                        ),
                        "paused_for_resume": False,
                        "lifecycle_in_progress": True,
                        "lifecycle_state": lifecycle_state,
                        "session_id": transition["id"],
                        "container_id": transition.get("container_id"),
                    }
                if paused and _preserve_session_on_credit_exhaust():
                    if not paused.get("runtime_paused"):
                        return {
                            "ok": False,
                            "reason": "Session pause is still being secured",
                            "paused_for_resume": False,
                            "pause_transition": True,
                            "session_id": paused["id"],
                        }
                    pause_reason = paused.get("pause_reason") or "unknown"
                    if pause_reason == "credit_exhausted":
                        reason = "Credit exhausted"
                    elif pause_reason == "heartbeat_timeout":
                        reason = "Session paused after disconnect"
                    else:
                        reason = "Saved session is paused"
                    return {
                        "ok": False,
                        "reason": reason,
                        "paused_for_resume": True,
                        "pause_reason": pause_reason,
                        "session_id": paused["id"],
                        "container_id": paused.get("container_id"),
                    }
                return {"ok": False, "reason": "No active session for this wallet"}

            session_id, last_billed_at, expires_at, started_at = row[0], row[1], row[2], row[3]
            req_profile = row[4] or "small"
            assigned_gpu_ids = _parse_gpu_ids(row[5])
            container_id = row[6]
            hard_expires_at = row[7]
            # Bill from last checkpoint, or from session start if never billed (e.g. pre-migration row)
            bill_from = last_billed_at if last_billed_at is not None else started_at
            elapsed_seconds = max(0.0, now - bill_from)
            wall_minutes = elapsed_seconds / 60.0
            billing_gpu_count = _billing_gpu_count(assigned_gpu_ids, req_profile)
            minutes_delta = _usage_minutes_for_interval(
                wall_minutes, assigned_gpu_ids, req_profile
            )

            # If there is billable time but ledger is unavailable, fail the heartbeat (no silent unbilled use).
            if minutes_delta > 0 and not deposit_ledger.init_once():
                conn.commit()
                _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)
                return {"ok": False, "reason": "Billing unavailable. Cannot record usage."}

            if minutes_delta > 0 and deposit_ledger.init_once():
                # Use same connection/cursor so one transaction; no separate connection or mid-transaction commit
                ok, remaining, err = deposit_ledger._deduct_usage_on_cursor(
                    cur, wallet, minutes_delta, session_id=str(session_id)
                )
                if not ok:
                    conn.commit()
                    _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)
                    return {"ok": False, "reason": err or "Billing failed"}
                if remaining <= 0:
                    # Begin a two-phase runtime freeze (same cursor: billing
                    # deduction and checkpoint stay atomic). ``paused`` is not
                    # published until Docker confirms the frozen state.
                    runtime_preserved = False
                    if _preserve_session_on_credit_exhaust():
                        pause_token = _new_transition_token()
                        cur.execute(
                            f"""UPDATE {_SESSION_TABLE}
                                SET status = 'pausing',
                                    last_heartbeat = %s,
                                    last_billed_at = %s,
                                    pause_reason = 'credit_exhausted',
                                    paused_at = NULL,
                                    runtime_paused = FALSE,
                                    transition_started_at = %s,
                                    transition_token = %s
                                WHERE id = %s AND status = 'active'
                                RETURNING wallet_address, container_id""",
                            (now, now, now, pause_token, session_id),
                        )
                        paused_row = cur.fetchone()
                        conn.commit()
                        if paused_row:
                            runtime_preserved = bool(
                                _on_session_credit_paused(
                                    paused_row[0],
                                    session_id,
                                    paused_row[1] if len(paused_row) > 1 else container_id,
                                    "credit_exhausted",
                                    pause_token,
                                )
                            )
                    else:
                        cur.execute(
                            f"""UPDATE {_SESSION_TABLE} SET status = 'ended'
                                WHERE id = %s AND status = 'active' RETURNING wallet_address""",
                            (session_id,),
                        )
                        ended_row = cur.fetchone()
                        conn.commit()
                        if ended_row:
                            _on_session_ended(ended_row[0], session_id)
                    _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)
                    return {
                        "ok": False,
                        "reason": "Credit exhausted",
                        "remaining_minutes": 0.0,
                        "requested_profile": req_profile,
                        "assigned_gpu_ids": assigned_gpu_ids,
                        "container_id": container_id,
                        "paused_for_resume": runtime_preserved,
                        "pause_reason": "credit_exhausted",
                        "gpu_billing_enabled": _gpu_billing_enabled(),
                        "billing_gpu_count": billing_gpu_count,
                    }
                billed_this_heartbeat = True
            else:
                billed_this_heartbeat = False

            # Only advance last_billed_at when we actually deducted usage; otherwise keep baseline for next run
            last_billed_at_value = now if billed_this_heartbeat else bill_from
            # Slide expires_at so AXGT_SESSION_MAX_MINUTES is idle timeout, not a fixed wall-clock cap.
            new_expires_at = now + _session_max_seconds()
            # Presence-based SSH hard-cap renewal: a live sshd connection reported
            # by the in-container daemon slides the cap forward (extend-only,
            # min(affordable, ceiling) from now). Sessions without a cap stay
            # uncapped; absence of the flag (old daemons, browser heartbeats)
            # changes nothing.
            if ssh_active and hard_expires_at is not None:
                cap_secs = _ssh_hard_cap_seconds(_remaining_minutes_for(wallet))
                if cap_secs is not None and now + cap_secs > hard_expires_at:
                    hard_expires_at = now + cap_secs
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET last_heartbeat = %s, last_billed_at = %s, expires_at = %s, hard_expires_at = %s
                    WHERE status = 'active' AND wallet_address = %s AND id = %s
                    RETURNING expires_at""",
                (now, last_billed_at_value, new_expires_at, hard_expires_at, wallet, session_id),
            )
            row2 = cur.fetchone()
            # Single commit: persists both deposit_ledger updates (from _deduct_usage_on_cursor) and session row.
            # No commit happens between deduct and this; if anything fails above, rollback in except unwinds all.
            conn.commit()
            if not row2:
                _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)
                return {"ok": False, "reason": "Session ended"}
            remaining_secs = max(0, row2[0] - now)
            result = {
                "ok": True,
                "remaining_seconds": int(remaining_secs),
                "requested_profile": req_profile,
                "assigned_gpu_ids": assigned_gpu_ids,
                "container_id": container_id,
                "allocation_status": "allocated",
                "gpu_billing_enabled": _gpu_billing_enabled(),
                "billing_gpu_count": billing_gpu_count,
            }
            if hard_expires_at is not None:
                result["hard_cap_remaining_seconds"] = int(max(0, hard_expires_at - now))
            if wall_minutes > 0 and _gpu_billing_enabled():
                result["wall_minutes_billed"] = round(wall_minutes, 4)
                result["minutes_billed"] = round(minutes_delta, 4)
            if minutes_delta > 0 and deposit_ledger.init_once():
                remaining_after = deposit_ledger.get_remaining_minutes(wallet)
                result["remaining_minutes"] = round(remaining_after, 2)
                if _gpu_billing_enabled() and billing_gpu_count > 1:
                    result["estimated_wall_minutes_remaining"] = round(
                        remaining_after / billing_gpu_count, 2
                    )
            _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)
            return result
        finally:
            cur.close()
    except Exception as exc:
        conn.rollback()
        logger.warning("heartbeat failed: %s", exc)
        return {"ok": False, "reason": "Internal error"}
    finally:
        conn.close()


def release_session(wallet_address: str) -> Dict[str, Any]:
    """Explicitly end the active session for *wallet_address*."""
    wallet = wallet_address.lower()
    if not _init_once():
        return {"released": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"released": False, "reason": "Session DB unavailable"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET status = 'ended'
                    WHERE status IN ('active', 'pausing', 'paused', 'resuming')
                      AND wallet_address = %s
                    RETURNING id, requested_profile, gpu_ids, container_id""",
                (wallet,),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return {"released": False, "reason": "No active session for this wallet"}
        session_id = row[0]
        _on_session_ended(wallet, session_id)
        logger.info("session_manager: session released by %s", _mask(wallet))
        return {
            "released": True,
            "requested_profile": row[1] or "small",
            "released_gpu_ids": _parse_gpu_ids(row[2]),
            "container_id": row[3],
        }
    except Exception as exc:
        conn.rollback()
        logger.warning("release_session failed: %s", exc)
        return {"released": False, "reason": "Internal error"}
    finally:
        conn.close()


def restart_desktop_session(wallet_address: str) -> Dict[str, Any]:
    """Restart desktop services for the active session owner without releasing ownership."""
    wallet = wallet_address.lower()
    if not _init_once():
        return {"restarted": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"restarted": False, "reason": "Session DB unavailable"}
    try:
        with conn.cursor() as cur:
            now = time.time()
            ended, paused_credit = _expire_stale_session(cur, now)
            active = _get_active_row(cur)
        conn.commit()
        _cleanup_after_stale_maintenance(ended, paused_credit, [])
        if not active:
            return {"restarted": False, "reason": "No active session"}
        if active["wallet_address"] != wallet:
            return {"restarted": False, "reason": "Only the active session owner can restart"}
        _run_reset_script()
        logger.info("session_manager: desktop restart requested by %s", _mask(wallet))
        return {"restarted": True, "session_id": active["id"]}
    except Exception as exc:
        conn.rollback()
        logger.warning("restart_desktop_session failed: %s", exc)
        return {"restarted": False, "reason": "Internal error"}
    finally:
        conn.close()


def session_status(wallet_address: Optional[str] = None) -> Dict[str, Any]:
    """Return current session state visible to *wallet_address*."""
    wallet = wallet_address.lower() if wallet_address else None
    if not _init_once():
        return {"active": False, "reason": "Session DB unavailable"}
    conn = _get_connection()
    if not conn:
        return {"active": False, "reason": "Session DB unavailable"}
    try:
        now = time.time()
        with conn.cursor() as cur:
            ended, paused_credit = _expire_stale_session(cur, now)
            paused_ended = _expire_stale_paused_sessions(cur, now)
            if ended or paused_credit or paused_ended:
                conn.commit()
                _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)

            active_rows = _get_active_rows(cur)
            reserved_rows = _get_gpu_reserved_rows(cur, now)
            active = active_rows[-1] if active_rows else None
            free_gpu_ids = _free_gpu_ids(reserved_rows)

            result: Dict[str, Any] = {
                "active": active is not None,
                "multi_session_enabled": _multi_session_enabled(),
                "gpu_profiles_enabled": _gpu_profiles_enabled(),
                "total_gpus": len(_gpu_device_ids()),
                "free_gpus": free_gpu_ids,
                "active_sessions_count": len(active_rows),
            }

            if active:
                remaining = max(0, active["expires_at"] - now)
                result["active_wallet"] = _mask(active["wallet_address"])
                result["session_remaining_seconds"] = int(remaining)
                result["latest_requested_profile"] = active.get("requested_profile") or "small"
                result["latest_assigned_gpu_ids"] = active.get("gpu_ids", [])
                if wallet and active["wallet_address"].lower() == wallet.lower():
                    result["is_owner"] = True

            if _multi_session_enabled():
                result["active_sessions"] = [
                    {
                        "session_id": row["id"],
                        "wallet_address": row["wallet_address"] if wallet and wallet.lower() == row["wallet_address"].lower() else _mask(row["wallet_address"]),
                        "requested_profile": row.get("requested_profile") or "small",
                        "assigned_gpu_ids": row.get("gpu_ids", []),
                        "container_id": row.get("container_id"),
                        "allocation_status": row.get("allocation_status") or "allocated",
                        "started_at": row.get("started_at"),
                        "expires_at": row.get("expires_at"),
                        "last_heartbeat": row.get("last_heartbeat"),
                        "ssh_enabled": bool(row.get("ssh_enabled")),
                    }
                    for row in active_rows
                ]

            if wallet:
                owned = _active_session_for_wallet(cur, wallet)
                if owned:
                    result["is_owner"] = True
                    owner_profile = (owned.get("requested_profile") or "small").strip().lower()
                    owner_gpu_ids = owned.get("gpu_ids", [])
                    result["owner_session_id"] = owned["id"]
                    result["owner_requested_profile"] = owner_profile
                    result["owner_assigned_gpu_ids"] = owner_gpu_ids
                    result["owner_allocation_status"] = owned.get("allocation_status") or "allocated"
                    result["owner_started_at"] = owned.get("started_at")
                    result["owner_gpu_count"] = (
                        len(owner_gpu_ids)
                        if owner_gpu_ids
                        else _billing_gpu_count(owner_gpu_ids, owner_profile)
                    )
                    result["owner_remaining_seconds"] = int(max(0, owned["expires_at"] - now))
                    if owned.get("hard_expires_at") is not None:
                        result["owner_hard_cap_remaining_seconds"] = int(
                            max(0, owned["hard_expires_at"] - now)
                        )
                    # Headless SSH session: tell the client so a reload restores the
                    # SSH connect card instead of offering a desktop viewer that the
                    # container cannot serve.
                    result["owner_ssh_enabled"] = bool(owned.get("ssh_enabled"))
                    if owned.get("ssh_enabled"):
                        result.update(_ssh_connection_fields(owned["id"]))
                transition_owned = _transition_session_for_wallet(cur, wallet)
                if transition_owned:
                    transition_profile = (
                        transition_owned.get("requested_profile") or "small"
                    ).strip().lower()
                    transition_gpus = transition_owned.get("gpu_ids", [])
                    result["is_owner"] = True
                    result["lifecycle_in_progress"] = True
                    result["owner_lifecycle_state"] = (
                        transition_owned.get("status") or "pausing"
                    )
                    result["owner_session_id"] = transition_owned["id"]
                    result["owner_requested_profile"] = transition_profile
                    result["owner_assigned_gpu_ids"] = transition_gpus
                    result["owner_container_id"] = transition_owned.get("container_id")
                    result["owner_allocation_status"] = (
                        transition_owned.get("allocation_status") or "allocated"
                    )
                    result["owner_started_at"] = transition_owned.get("started_at")
                    result["owner_gpu_count"] = (
                        len(transition_gpus)
                        if transition_gpus
                        else _billing_gpu_count(
                            transition_gpus, transition_profile
                        )
                    )
                    result["owner_ssh_enabled"] = bool(
                        transition_owned.get("ssh_enabled")
                    )
                paused_owned = _paused_session_for_wallet(cur, wallet, now)
                if (
                    paused_owned
                    and paused_owned.get("runtime_paused")
                    and _preserve_session_on_credit_exhaust()
                ):
                    paused_profile = paused_owned.get("requested_profile") or "small"
                    paused_gpus = paused_owned.get("gpu_ids", [])
                    billing_count = _billing_gpu_count(paused_gpus, paused_profile)
                    paused_at = paused_owned.get("paused_at") or paused_owned["last_heartbeat"]
                    pause_remaining = max(
                        0,
                        paused_at + _session_paused_max_seconds() - now,
                    )
                    result["paused"] = True
                    result["can_resume"] = pause_remaining > 0
                    result["paused_reason"] = paused_owned.get("pause_reason") or "unknown"
                    result["paused_session_id"] = paused_owned["id"]
                    result["paused_container_id"] = paused_owned.get("container_id")
                    result["paused_requested_profile"] = paused_profile
                    result["paused_assigned_gpu_ids"] = paused_gpus
                    result["paused_gpu_count"] = len(paused_gpus) if paused_gpus else billing_count
                    result["paused_ssh_enabled"] = bool(paused_owned.get("ssh_enabled"))
                    result["paused_resume_seconds"] = int(pause_remaining)
                    result["resume_minutes_required"] = (
                        billing_count if _gpu_billing_enabled() else 1
                    )
                    try:
                        deposit_ledger = _import_deposit_ledger()
                        if deposit_ledger.init_once():
                            remaining = deposit_ledger.get_remaining_minutes(wallet)
                            result["remaining_minutes"] = round(remaining, 2)
                            required = float(result["resume_minutes_required"])
                            result["can_resume_with_credit"] = remaining > 0 and (
                                not _gpu_billing_enabled() or remaining >= required
                            )
                    except Exception:
                        pass
                    if _gpu_billing_enabled():
                        result["gpu_billing_enabled"] = True
                        result["billing_gpu_count"] = billing_count

        return result
    except Exception as exc:
        logger.warning("session_status failed: %s", exc)
        return {"active": False, "reason": "Internal error"}
    finally:
        conn.close()


def is_session_owner(wallet_address: str) -> bool:
    """Fast check: does *wallet_address* own the active session?"""
    wallet = wallet_address.lower()
    if not _init_once():
        return False
    conn = _get_connection()
    if not conn:
        return False
    try:
        now = time.time()
        with conn.cursor() as cur:
            ended, paused_credit = _expire_stale_session(cur, now)
            active = _active_session_for_wallet(cur, wallet)
        conn.commit()
        _cleanup_after_stale_maintenance(ended, paused_credit, [])
        return active is not None
    except Exception as exc:
        logger.warning("is_session_owner failed: %s", exc)
        return False
    finally:
        conn.close()


def get_active_desktop_session_for_wallet(wallet_address: str) -> Optional[Dict[str, Any]]:
    """Resolve the wallet's exact active WebRTC-capable compute session.

    The returned identity is intentionally minimal and never includes the
    per-session ``files_key``.  Only fully allocated, unexpired desktop rows are
    eligible; SSH, paused, allocating, failed, ended, and stale sessions must
    not receive browser WebRTC signaling.
    """
    wallet = (wallet_address or "").strip().lower()
    if not wallet or not _init_once():
        return None
    conn = _get_connection()
    if not conn:
        return None
    try:
        now = time.time()
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, wallet_address
                    FROM {_SESSION_TABLE}
                    WHERE wallet_address = %s
                      AND status = 'active'
                      AND allocation_status = 'allocated'
                      AND ssh_enabled = FALSE
                      AND expires_at > %s
                    ORDER BY started_at DESC
                    LIMIT 1""",
                (wallet, now),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "wallet_address": (row[1] or "").strip().lower(),
        }
    except Exception as exc:
        logger.warning("get_active_desktop_session_for_wallet failed: %s", exc)
        return None
    finally:
        conn.close()


def get_single_active_desktop_session() -> Optional[Dict[str, Any]]:
    """Resolve the sole legacy shared desktop, never a multi-session tenant.

    The documented single-container deployment has no injected per-container
    identity. Compatibility is safe only while multi-session scheduling is
    explicitly disabled and exactly one eligible desktop row exists.
    """
    if _truthy("AXGT_USER_CONTAINER_ENABLED", False) or not _init_once():
        return None
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, wallet_address
                    FROM {_SESSION_TABLE}
                    WHERE status = 'active'
                      AND allocation_status = 'allocated'
                      AND ssh_enabled = FALSE
                      AND expires_at > %s
                    ORDER BY started_at DESC
                    LIMIT 2""",
                (time.time(),),
            )
            rows = cur.fetchall()
        if len(rows) != 1:
            return None
        return {
            "id": int(rows[0][0]),
            "wallet_address": (rows[0][1] or "").strip().lower(),
        }
    except Exception as exc:
        logger.warning("get_single_active_desktop_session failed: %s", exc)
        return None
    finally:
        conn.close()


def _numeric_session_id(session_id: Any) -> Optional[int]:
    """Return a positive integer session ID accepted from an HTTP header."""
    if isinstance(session_id, bool):
        return None
    if isinstance(session_id, int):
        parsed = session_id
    elif isinstance(session_id, str):
        raw = session_id.strip()
        if not raw or not raw.isascii() or not raw.isdigit():
            return None
        parsed = int(raw)
    else:
        return None
    return parsed if parsed > 0 else None


def _webrtc_capability_matches_row(
    capability,
    verified: Dict[str, Any],
    row,
    sid: int,
    wallet: str,
    now: float,
) -> bool:
    try:
        trusted_id = int(row[0])
        trusted_wallet = (row[1] or "").strip().lower()
        stored_key = row[2] if isinstance(row[2], str) else ""
        stored_jti_hash = row[3] if isinstance(row[3], str) else ""
        stored_cap_expiry = float(row[4] or 0)
        token_expiry = float(verified.get("expires_at") or 0)
    except (TypeError, ValueError, IndexError):
        return False
    stored_fingerprint = capability.files_key_fingerprint(stored_key) or ""
    return bool(
        trusted_id == sid
        and trusted_wallet == wallet
        and stored_fingerprint
        and stored_jti_hash
        and stored_cap_expiry > now
        # A successfully renewed row deliberately accepts the previous token
        # until that token's own signed expiry. This makes refresh retry-safe if
        # the first response is lost, while never accepting a token that claims
        # a later expiry than the database authorized.
        and stored_cap_expiry + 1.0 >= token_expiry
        and secrets.compare_digest(
            stored_fingerprint,
            str(verified.get("files_key_fingerprint") or ""),
        )
        and secrets.compare_digest(
            stored_jti_hash,
            str(verified.get("jti_hash") or ""),
        )
    )


def validate_webrtc_agent_identity(
    session_id: Any,
    wallet_address: str,
    agent_token: str,
) -> Optional[Dict[str, Any]]:
    """Validate one session container as the exact active desktop compute row.

    Signature, issuer, audience, expiry, compute ID, and wallet are checked
    before any database call. The signed JTI and file-key fingerprint must then
    match the same active row, providing immediate central revocation without
    exposing either the fleet signing key or the database credential.
    """
    sid = _numeric_session_id(session_id)
    wallet = (wallet_address or "").strip().lower()
    token = (agent_token or "").strip()
    if sid is None or not wallet or not token:
        return None
    try:
        capability = _import_webrtc_capability()
        verified = capability.verify(token, sid, wallet)
    except Exception as exc:
        logger.warning("WebRTC capability verification failed: %s", exc)
        return None
    if not verified or not _init_once():
        return None
    conn = _get_connection()
    if not conn:
        return None
    try:
        now = time.time()
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, wallet_address, files_key,
                           webrtc_cap_jti_hash, webrtc_cap_expires_at
                    FROM {_SESSION_TABLE}
                    WHERE id = %s
                      AND wallet_address = %s
                      AND status = 'active'
                      AND allocation_status = 'allocated'
                      AND ssh_enabled = FALSE
                      AND expires_at > %s
                    LIMIT 1""",
                (sid, wallet, now),
            )
            row = cur.fetchone()
        if not row:
            return None
        if not _webrtc_capability_matches_row(
            capability,
            verified,
            row,
            sid,
            wallet,
            now,
        ):
            return None
        return {"id": sid, "wallet_address": wallet}
    except Exception as exc:
        logger.warning("validate_webrtc_agent_identity failed: %s", exc)
        return None
    finally:
        conn.close()


def refresh_webrtc_agent_capability(
    session_id: Any,
    wallet_address: str,
    agent_token: str,
) -> Optional[Dict[str, Any]]:
    """Renew a live/paused desktop capability without delegating the signer."""
    sid = _numeric_session_id(session_id)
    wallet = (wallet_address or "").strip().lower()
    token = (agent_token or "").strip()
    if sid is None or not wallet or not token:
        return None
    try:
        capability = _import_webrtc_capability()
        verified = capability.verify(token, sid, wallet)
    except Exception as exc:
        logger.warning("WebRTC capability refresh verification failed: %s", exc)
        return None
    if not verified or not _init_once():
        return None
    conn = _get_connection()
    if not conn:
        return None
    try:
        now = time.time()
        paused_cutoff = now - _session_paused_max_seconds()
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, wallet_address, files_key,
                           webrtc_cap_jti_hash, webrtc_cap_expires_at
                    FROM {_SESSION_TABLE}
                    WHERE id = %s
                      AND wallet_address = %s
                      AND allocation_status = 'allocated'
                      AND ssh_enabled = FALSE
                      AND (
                          (status = 'active' AND expires_at > %s)
                          OR (status = 'paused' AND COALESCE(paused_at, last_heartbeat) >= %s)
                      )
                    LIMIT 1
                    FOR UPDATE""",
                (sid, wallet, now, paused_cutoff),
            )
            row = cur.fetchone()
            if not row or not _webrtc_capability_matches_row(
                capability,
                verified,
                row,
                sid,
                wallet,
                now,
            ):
                conn.rollback()
                return None
            renewed = capability.renew(token, sid, wallet)
            if not isinstance(renewed, dict):
                conn.rollback()
                return None
            renewed_token = str(renewed.get("token") or "").strip()
            renewed_jti = str(renewed.get("jti_hash") or "").strip()
            renewed_fingerprint = str(
                renewed.get("files_key_fingerprint") or ""
            ).strip()
            renewed_expiry = float(renewed.get("expires_at") or 0)
            if (
                not renewed_token
                or renewed_jti != str(verified.get("jti_hash") or "")
                or renewed_fingerprint
                != str(verified.get("files_key_fingerprint") or "")
                or renewed_expiry <= now
            ):
                conn.rollback()
                return None
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET webrtc_cap_expires_at = %s
                    WHERE id = %s
                      AND wallet_address = %s
                      AND webrtc_cap_jti_hash = %s
                      AND status IN ('active', 'paused')""",
                (renewed_expiry, sid, wallet, renewed_jti),
            )
        conn.commit()
        return {"token": renewed_token, "expires_at": renewed_expiry}
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("refresh_webrtc_agent_capability failed: %s", exc)
        return None
    finally:
        conn.close()


def validate_session_files_key(wallet_address: str, files_key: str) -> bool:
    """True if *files_key* matches the active session's per-session secret for *wallet*.

    Lets every session container authenticate its durable runtime heartbeat (no
    browser wallet token). The files_key is minted at claim, stored on the session
    row, and injected into the container env as AXGT_SESSION_FILES_KEY.
    """
    wallet = (wallet_address or "").lower()
    key = (files_key or "").strip()
    if not wallet or not key:
        return False
    if not _init_once():
        return False
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT files_key FROM {_SESSION_TABLE}
                    WHERE wallet_address = %s AND status = 'active'
                    ORDER BY started_at DESC LIMIT 1""",
                (wallet,),
            )
            row = cur.fetchone()
        stored = (row[0] if row else None) or ""
        return bool(stored) and secrets.compare_digest(stored, key)
    except Exception as exc:
        logger.warning("validate_session_files_key failed: %s", exc)
        return False
    finally:
        conn.close()


def _reconcile_containers(cur, now: float) -> Tuple[List[int], List[Tuple[str, int]]]:
    """Query running session containers and reconcile them against their DB status.

    Returns (to_stop_ids, to_expire_list).
    """
    launcher = _import_session_launcher()
    running_session_ids = launcher.list_running_sessions()
    if not running_session_ids:
        return [], []

    to_stop_ids = []
    to_expire = []
    grace = session_grace_seconds()
    for s_id in running_session_ids:
        cur.execute(
            f"SELECT status, hard_expires_at, wallet_address FROM {_SESSION_TABLE} WHERE id = %s",
            (s_id,),
        )
        row = cur.fetchone()
        if not row:
            logger.info("reconcile: session %s does not exist in DB, scheduling container stop", s_id)
            to_stop_ids.append(s_id)
            continue

        status, hard_expires_at, wallet = row[0], row[1], row[2]
        if status in ("ended", "expired", "released"):
            logger.info("reconcile: session %s is already %s, scheduling container stop retry", s_id, status)
            to_stop_ids.append(s_id)
            continue

        if hard_expires_at is not None and hard_expires_at + grace <= now:
            logger.info("reconcile: session %s reached hard expiry, scheduling DB update to ended", s_id)
            cur.execute(
                f"UPDATE {_SESSION_TABLE} SET status = 'ended' WHERE id = %s",
                (s_id,),
            )
            to_expire.append((wallet, s_id))

    return to_stop_ids, to_expire


def perform_session_cleanup() -> None:
    """Enforce session expiry and reconcile container state with DB under advisory lock."""
    try:
        _import_session_launcher().reconcile_session_networks()
    except Exception as exc:
        logger.warning("Session-network reconciliation failed: %s", exc)
    if not _init_once():
        return
    conn = _get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(8889197)")
            locked = cur.fetchone()[0]
            if not locked:
                return

            now = time.time()
            ended, paused_credit = _expire_stale_session(cur, now)
            paused_ended = _expire_stale_paused_sessions(cur, now)

            to_stop_ids, to_expire = _reconcile_containers(cur, now)

            # Heal legacy rows immediately and lifecycle transitions only after
            # their owner request has had time to finish. The transient status
            # prevents cleanup from racing a live Resume/Pause operation. This
            # CAS claims recovery *before* any Docker action, so a stale
            # resuming snapshot can never freeze a row that already became active.
            recovery_token = _new_transition_token()
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET status = 'pausing',
                        transition_started_at = %s,
                        last_heartbeat = GREATEST(last_heartbeat, %s),
                        transition_token = %s
                    WHERE (status = 'paused' AND runtime_paused = FALSE)
                       OR (status IN ('pausing', 'resuming')
                           AND COALESCE(transition_started_at, last_heartbeat, 0) < %s)
                    RETURNING wallet_address, id, container_id,
                              COALESCE(pause_reason, 'legacy')""",
                (
                    now,
                    now,
                    recovery_token,
                    now - _lifecycle_transition_timeout_seconds(),
                ),
            )
            lifecycle_rows = cur.fetchall() or []

            conn.commit()

            # 1. Trigger post-commit stale session maintenance hooks
            if ended or paused_credit or paused_ended:
                _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)

            for wallet, s_id, container_id, pause_reason in lifecycle_rows:
                _on_session_credit_paused(
                    wallet,
                    s_id,
                    container_id,
                    pause_reason,
                    recovery_token,
                )

            # 2. Trigger post-commit reconciliation expired hooks
            for wallet, s_id in to_expire:
                try:
                    _on_session_ended(wallet, s_id)
                except Exception as exc:
                    logger.warning("Post-commit reconcile expire failed for session %s: %s", s_id, exc)

            # 3. Trigger post-commit container cleanups
            for s_id in to_stop_ids:
                try:
                    _cleanup_session_container(s_id)
                except Exception as exc:
                    logger.warning("Post-commit container stop failed for session %s: %s", s_id, exc)

    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("perform_session_cleanup failed: %s", exc, exc_info=True)
    finally:
        conn.close()
