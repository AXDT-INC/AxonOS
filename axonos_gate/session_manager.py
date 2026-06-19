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
    return _truthy("AXGT_MULTI_SESSION_ENABLED", True)


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
                f"Insufficient prepaid credit for profile \"{profile_name}\" ({requested_gpus} GPU(s)). "
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
    """Keep container/desktop alive when prepaid credit hits zero (resume after top-up)."""
    return _truthy("AXGT_SESSION_PRESERVE_ON_CREDIT_EXHAUST", True)


def _session_paused_max_seconds() -> int:
    """How long a credit-paused session (and its container) may be resumed."""
    raw = (os.getenv("AXGT_SESSION_PAUSED_MAX_MINUTES") or "").strip()
    try:
        minutes = int(raw)
        if minutes > 0:
            return minutes * 60
    except (ValueError, TypeError):
        pass
    return 2 * 60 * 60  # default 2 hours


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
                files_key   TEXT
            )
        """)
        # Add last_billed_at if table existed from before migration
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s AND column_name = 'last_billed_at'
        """, (_SESSION_TABLE,))
        if cur.fetchone() is None:
            cur.execute(f"ALTER TABLE {_SESSION_TABLE} ADD COLUMN last_billed_at DOUBLE PRECISION")
        for col_name, col_sql in (
            ("requested_profile", "TEXT NOT NULL DEFAULT 'small'"),
            ("gpu_ids", "TEXT"),
            ("container_id", "TEXT"),
            ("allocation_status", "TEXT NOT NULL DEFAULT 'allocated'"),
            ("files_key", "TEXT"),
            # Non-sliding hard cap (unlike expires_at, which slides on heartbeat).
            # Set for headless/SSH sessions so an abandoned session can't drain the
            # whole prepaid balance. NULL = no cap (e.g. desktop, legacy rows).
            ("hard_expires_at", "DOUBLE PRECISION"),
        ):
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
                """,
                (_SESSION_TABLE, col_name),
            )
            if cur.fetchone() is None:
                cur.execute(f"ALTER TABLE {_SESSION_TABLE} ADD COLUMN {col_name} {col_sql}")
        # Ensure no NULL last_billed_at: bill from session start (fixes pre-migration or old migrations)
        cur.execute(
            f"UPDATE {_SESSION_TABLE} SET last_billed_at = started_at WHERE last_billed_at IS NULL"
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

def _pause_stale_zero_credit_sessions(cur, now: float, hb_cutoff: float) -> List[tuple]:
    """Pause (not kill) active sessions that timed out with no prepaid credit left."""
    if not _preserve_session_on_credit_exhaust():
        return []
    deposit_ledger = _import_deposit_ledger()
    if not deposit_ledger.init_once():
        return []
    cur.execute(
        f"""SELECT id, wallet_address FROM {_SESSION_TABLE}
            WHERE status = 'active' AND last_heartbeat < %s AND expires_at > %s""",
        (hb_cutoff, now),
    )
    rows = cur.fetchall() or []
    paused: List[tuple] = []
    for session_id, wallet in rows:
        if deposit_ledger.get_remaining_minutes(wallet) > 0:
            continue
        cur.execute(
            f"""UPDATE {_SESSION_TABLE}
                SET status = 'paused', last_heartbeat = %s
                WHERE id = %s AND status = 'active'
                RETURNING wallet_address""",
            (now, session_id),
        )
        if cur.fetchone():
            paused.append((wallet, session_id))
    return paused


def _expire_stale_session(cur, now: float) -> Tuple[Optional[tuple], List[tuple]]:
    """End or pause stale active sessions.

    Zero-credit heartbeat timeouts become ``paused`` when preserve is enabled (container kept).
    Stale ``expires_at`` (no heartbeat within ``AXGT_SESSION_MAX_MINUTES``) or heartbeat timeout end the session.

    Returns (ended_session_or_none, paused_zero_credit_sessions).
    """
    hb_cutoff = now - _heartbeat_timeout_seconds()
    paused = _pause_stale_zero_credit_sessions(cur, now, hb_cutoff)
    cur.execute(
        f"""UPDATE {_SESSION_TABLE}
            SET status = 'ended'
            WHERE status = 'active'
              AND (last_heartbeat < %s
                   OR expires_at <= %s
                   OR (hard_expires_at IS NOT NULL AND hard_expires_at <= %s))
            RETURNING wallet_address, id""",
        (hb_cutoff, now, now),
    )
    row = cur.fetchone()
    ended = (row[0], row[1]) if row else None
    return ended, paused


def _apply_stale_session_maintenance(ended: Optional[tuple], paused_credit: List[tuple]) -> None:
    for wallet, session_id in paused_credit:
        _on_session_credit_paused(wallet, session_id)
    if ended:
        _on_session_ended(ended[0], ended[1])


def _cleanup_after_stale_maintenance(
    ended: Optional[tuple],
    paused_credit: List[tuple],
    paused_ttl_ended: Optional[List[tuple]] = None,
) -> None:
    """Post-commit hooks for stale session DB updates."""
    _apply_stale_session_maintenance(ended, paused_credit)
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
    }


def _get_active_rows(cur) -> List[Dict[str, Any]]:
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at, files_key, hard_expires_at
            FROM {_SESSION_TABLE}
            WHERE status = 'active'
            ORDER BY started_at ASC""",
    )
    rows = cur.fetchall() or []
    return [_session_row_to_dict(r) for r in rows]


def _get_paused_rows(cur, now: float) -> List[Dict[str, Any]]:
    """Credit-paused sessions still holding GPUs/container until paused TTL expires."""
    cutoff = now - _session_paused_max_seconds()
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at, files_key, hard_expires_at
            FROM {_SESSION_TABLE}
            WHERE status = 'paused' AND last_heartbeat >= %s
            ORDER BY started_at ASC""",
        (cutoff,),
    )
    rows = cur.fetchall() or []
    return [_session_row_to_dict(r) for r in rows]


def _get_gpu_reserved_rows(cur, now: float) -> List[Dict[str, Any]]:
    """Active plus non-expired paused sessions (both reserve GPU IDs)."""
    return _get_active_rows(cur) + _get_paused_rows(cur, now)


def _paused_session_for_wallet(cur, wallet: str, now: float) -> Optional[Dict[str, Any]]:
    cur.execute(
        f"""SELECT id, wallet_address, requested_profile, gpu_ids, container_id, allocation_status,
                   started_at, last_heartbeat, last_billed_at, expires_at, files_key, hard_expires_at
            FROM {_SESSION_TABLE}
            WHERE status = 'paused' AND wallet_address = %s AND last_heartbeat >= %s
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
                   started_at, last_heartbeat, last_billed_at, expires_at, files_key, hard_expires_at
            FROM {_SESSION_TABLE}
            WHERE status = 'active' AND wallet_address = %s
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
            f"This host exposes {total} GPU(s), but the \"{profile_name}\" profile needs {requested_gpus}. "
            "Pick a smaller profile (Small = 1, Medium = 2, Large = 4, Max = 8 GPUs), then connect again."
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


def _on_session_credit_paused(wallet_address: str, session_id: int) -> None:
    """Credit exhausted: disconnect billing but keep container/desktop for resume."""
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
        "session_manager: session %s paused for %s (container preserved)",
        session_id,
        _mask(wallet_address),
    )


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
    """End paused sessions past resume TTL (container teardown)."""
    cutoff = now - _session_paused_max_seconds()
    cur.execute(
        f"""UPDATE {_SESSION_TABLE}
            SET status = 'ended'
            WHERE status = 'paused' AND last_heartbeat < %s
            RETURNING wallet_address, id""",
        (cutoff,),
    )
    rows = cur.fetchall() or []
    return [(r[0], r[1]) for r in rows]


def _resume_paused_session(
    cur,
    wallet: str,
    paused: Dict[str, Any],
    now: float,
) -> Dict[str, Any]:
    """Reactivate a credit-paused session without spawning a new container.

    Restores the same profile, GPU assignment, and container as before credit exhaustion.
    Client-supplied ``requested_profile`` is ignored for resume.
    """
    paused_profile = (paused.get("requested_profile") or "small").strip().lower()
    assigned = list(paused.get("gpu_ids") or [])
    max_secs = _session_max_seconds()
    cur.execute(
        f"""UPDATE {_SESSION_TABLE}
            SET status = 'active',
                last_heartbeat = %s,
                expires_at = %s
            WHERE id = %s AND status = 'paused' AND wallet_address = %s
            RETURNING id, gpu_ids, container_id, expires_at, requested_profile""",
        (now, now + max_secs, paused["id"], wallet),
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
    return {
        "granted": True,
        "resumed": True,
        "session_id": session_id,
        "requested_profile": profile_name,
        "assigned_gpu_ids": assigned,
        "container_id": container_id,
        "allocation_status": "allocated",
        "remaining_seconds": int(remaining),
    }


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


def _spawn_session_container(session_id: int, wallet: str, profile: str, gpu_ids: List[int], template: Optional[str] = None, files_key: Optional[str] = None, ssh_enabled: bool = False, ssh_pubkey: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str]]:
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
    )


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
    """Active or credit-paused session row for *wallet_address*, or None.

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
            reserved_rows = active_rows + paused_rows
            active = active_rows[-1] if active_rows else None
            blocking = active if active else (paused_rows[-1] if paused_rows else None)

            is_owner = _active_session_for_wallet(cur, wallet) is not None
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
                resume = _resume_paused_session(cur, wallet, paused, now)
                conn.commit()
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
                # The SSH port is deterministic from the session id, so a reload that
                # re-asserts ssh intent can recover the connect-string without a DB column.
                if requested_ssh:
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
                            f"No GPUs available for profile \"{profile_name}\" "
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
                         started_at, last_heartbeat, last_billed_at, expires_at, status, files_key, hard_expires_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)
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
                    ),
                )
                session_id = cur.fetchone()[0]
                conn.commit()

                spawned, container_id, spawn_error = _spawn_session_container(
                    session_id=session_id,
                    wallet=wallet,
                    profile=profile_name,
                    gpu_ids=allocated_gpu_ids,
                    template=requested_template,
                    files_key=files_key,
                    ssh_enabled=requested_ssh,
                    ssh_pubkey=ssh_pubkey,
                )
                conn2 = _get_connection()
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
                            else:
                                cur2.execute(
                                    f"""UPDATE {_SESSION_TABLE}
                                        SET status = 'ended', allocation_status = 'failed'
                                        WHERE id = %s AND status = 'active'""",
                                    (session_id,),
                                )
                        conn2.commit()
                    finally:
                        conn2.close()
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


def heartbeat(wallet_address: str) -> Dict[str, Any]:
    """Update heartbeat for the active session owner; bill elapsed time from last_billed_at."""
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
            ended, paused_credit = _expire_stale_session(cur, now)
            paused_ended = _expire_stale_paused_sessions(cur, now)
            cur.execute(
                f"""SELECT id, last_billed_at, expires_at, started_at, requested_profile, gpu_ids, container_id
                    FROM {_SESSION_TABLE}
                    WHERE status = 'active' AND wallet_address = %s
                    FOR UPDATE""",
                (wallet,),
            )
            row = cur.fetchone()
            if not row:
                paused = _paused_session_for_wallet(cur, wallet, now)
                conn.commit()
                _cleanup_after_stale_maintenance(ended, paused_credit, paused_ended)
                if paused and _preserve_session_on_credit_exhaust():
                    return {
                        "ok": False,
                        "reason": "Credit exhausted",
                        "paused_for_resume": True,
                        "session_id": paused["id"],
                        "container_id": paused.get("container_id"),
                    }
                return {"ok": False, "reason": "No active session for this wallet"}

            session_id, last_billed_at, expires_at, started_at = row[0], row[1], row[2], row[3]
            req_profile = row[4] or "small"
            assigned_gpu_ids = _parse_gpu_ids(row[5])
            container_id = row[6]
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
                    # Pause or end session (same cursor: lock still held). Commit before cleanup hooks.
                    if _preserve_session_on_credit_exhaust():
                        cur.execute(
                            f"""UPDATE {_SESSION_TABLE}
                                SET status = 'paused', last_heartbeat = %s
                                WHERE id = %s AND status = 'active'
                                RETURNING wallet_address""",
                            (now, session_id),
                        )
                        paused_row = cur.fetchone()
                        conn.commit()
                        if paused_row:
                            _on_session_credit_paused(paused_row[0], session_id)
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
                        "paused_for_resume": _preserve_session_on_credit_exhaust(),
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
            cur.execute(
                f"""UPDATE {_SESSION_TABLE}
                    SET last_heartbeat = %s, last_billed_at = %s, expires_at = %s
                    WHERE status = 'active' AND wallet_address = %s AND id = %s
                    RETURNING expires_at""",
                (now, last_billed_at_value, new_expires_at, wallet, session_id),
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
                    WHERE status IN ('active', 'paused') AND wallet_address = %s
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
                if wallet and active["wallet_address"] == wallet:
                    result["is_owner"] = True

            if _multi_session_enabled():
                result["active_sessions"] = [
                    {
                        "session_id": row["id"],
                        "wallet_address": row["wallet_address"] if wallet and wallet == row["wallet_address"] else _mask(row["wallet_address"]),
                        "requested_profile": row.get("requested_profile") or "small",
                        "assigned_gpu_ids": row.get("gpu_ids", []),
                        "container_id": row.get("container_id"),
                        "allocation_status": row.get("allocation_status") or "allocated",
                        "started_at": row.get("started_at"),
                        "expires_at": row.get("expires_at"),
                        "last_heartbeat": row.get("last_heartbeat"),
                    }
                    for row in active_rows
                ]

            if wallet:
                owned = _active_session_for_wallet(cur, wallet)
                if owned:
                    result["is_owner"] = True
                    owner_profile = (owned.get("requested_profile") or "small").strip().lower()
                    owner_gpu_ids = owned.get("gpu_ids", [])
                    result["owner_requested_profile"] = owner_profile
                    result["owner_assigned_gpu_ids"] = owner_gpu_ids
                    result["owner_gpu_count"] = (
                        len(owner_gpu_ids)
                        if owner_gpu_ids
                        else _billing_gpu_count(owner_gpu_ids, owner_profile)
                    )
                paused_owned = _paused_session_for_wallet(cur, wallet, now)
                if paused_owned and _preserve_session_on_credit_exhaust():
                    paused_profile = paused_owned.get("requested_profile") or "small"
                    paused_gpus = paused_owned.get("gpu_ids", [])
                    billing_count = _billing_gpu_count(paused_gpus, paused_profile)
                    pause_remaining = max(
                        0,
                        paused_owned["last_heartbeat"] + _session_paused_max_seconds() - now,
                    )
                    result["paused"] = True
                    result["can_resume"] = pause_remaining > 0
                    result["paused_session_id"] = paused_owned["id"]
                    result["paused_container_id"] = paused_owned.get("container_id")
                    result["paused_requested_profile"] = paused_profile
                    result["paused_assigned_gpu_ids"] = paused_gpus
                    result["paused_gpu_count"] = len(paused_gpus) if paused_gpus else billing_count
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


def validate_session_files_key(wallet_address: str, files_key: str) -> bool:
    """True if *files_key* matches the active session's per-session secret for *wallet*.

    Lets a headless/SSH session container authenticate its own heartbeats (no
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
