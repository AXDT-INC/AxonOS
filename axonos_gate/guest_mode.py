"""Invite-gated, wallet-free guest/demo sessions.

A guest identity occupies the ordinary EVM address shape:

    0x 6775657374 <30 random hex nibbles>
       └ ASCII "guest"

That is deliberate. ``validate_wallet_address()`` is a hard
``^0x[a-fA-F0-9]{40}$`` and runs *before* the auth-token check at every compute
endpoint in both gate servers; ``axgt_auth_tokens.wallet_address`` is NOT NULL and
``axgt_sessions`` is keyed by wallet. Reusing the address shape means the token,
session, heartbeat and expiry machinery all work unchanged, with no relaxation of the
one validator that also guards the wallet-gated endpoints.

The reserved 10-nibble prefix makes guest-ness decidable *offline*, with no DB round
trip -- required because the deny-list checks run in hot paths and inside
``websockify_gate.py``, which has no Flask context. The namespace is closed at the
door: the SIWE routes reject guest-shaped addresses outright, so no signature can ever
enter it.

Guest sessions are mintable only from an admin-issued invite token. There is no
anonymous self-serve path: free GPU minutes are an abuse magnet.
"""

import hashlib
import logging
import math
import os
import re
import secrets
import time
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

try:
    from .docker_gpu_cli import (
        SUPPORTED_SESSION_TEMPLATE_IDS,
        normalize_session_template,
    )
except ImportError:
    try:
        from axonos_gate.docker_gpu_cli import (
            SUPPORTED_SESSION_TEMPLATE_IDS,
            normalize_session_template,
        )
    except ImportError:
        from docker_gpu_cli import (
            SUPPORTED_SESSION_TEMPLATE_IDS,
            normalize_session_template,
        )

logger = logging.getLogger(__name__)

_INVITES_TABLE = "axgt_guest_invites"
_GUEST_SESSIONS_TABLE = "axgt_guest_sessions"
_SESSION_TABLE = "axgt_sessions"  # concurrency checks + revocation deadlines

# ``deposit_ledger.credit_test_grant`` deliberately caps one atomic grant at a
# day of minutes. A valid long/high-GPU demo can need more than that in total,
# so guest credit is issued in idempotent chunks below instead of failing after
# the invite has already been redeemed.
MAX_GUEST_CREDIT_CHUNK_MINUTES = 1440.0

# ASCII "guest". Reserved prefix for synthetic guest identities.
GUEST_ADDRESS_TAG = "6775657374"
_GUEST_ADDRESS_RE = re.compile(r"^0x" + GUEST_ADDRESS_TAG + r"[0-9a-f]{30}$")
_EVM_ADDRESS_RE = re.compile(r"^0x[a-f0-9]{40}$")

# Invite tokens are URL-safe base64 from secrets.token_urlsafe().
_INVITE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_INVITE_TOKEN_BYTES = 32
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")

_LABEL_RE = re.compile(r"^[\w .@:/+-]{0,120}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

DEFAULT_SESSION_MINUTES = 30
MAX_SESSION_MINUTES = 240
DEFAULT_WARN_MINUTES = 5
DEFAULT_INVITE_TTL_HOURS = 168  # 7 days
MAX_INVITE_TTL_HOURS = 8760  # 1 year
DEFAULT_CREDIT_BUFFER_MINUTES = 5.0
MAX_CREDIT_BUFFER_MINUTES = 120.0
DEFAULT_ALLOWED_PROFILES = ("small",)
MAX_USES_CEILING = 1000
DEFAULT_DATA_RETENTION_DAYS = 30
MAX_DATA_RETENTION_DAYS = 36500
DEFAULT_REAPER_BATCH_SIZE = 250
MAX_REAPER_BATCH_SIZE = 1000

# Sponsor quotas. Invites create NEW identities, each with its own fresh ledger
# row, so the test-credit balance cap -- which bounds a single identity -- does
# not bound a sponsor who mints many links. The quota therefore lives on the
# minting wallet, not on the identity it funds.
DEFAULT_MAX_LIVE_PER_SPONSOR = 2
MAX_LIVE_PER_SPONSOR_CEILING = 100
DEFAULT_MAX_INVITES_PER_DAY = 20
MAX_INVITES_PER_DAY_CEILING = 1000

# Ledger headroom over the wall-clock cap. The hard cap must be the binding
# limit: if credit ran out first the session would enter credit-grace and hold a
# GPU for the grace TTL (default 2h) instead of being torn down.
_CREDIT_HEADROOM_FACTOR = 1.25

# A redemption with no container yet still counts as occupying the invite, so a
# double-click cannot open two demos. Covers the claim/spawn window.
_RESERVATION_WINDOW_SECONDS = 120.0

_PROFILE_GPU_COUNTS_FALLBACK = {"small": 1, "medium": 2, "large": 4, "max": 8}

_pg_init_done = False
_pg_init_lock = Lock()


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _truthy(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def guest_mode_enabled() -> bool:
    """Explicit fail-closed switch for the whole guest/demo feature.

    Off unless set. Existing invite rows never enable it by themselves -- the same
    contract as the test-credit rail, where a populated wallet list is inert until
    the feature flag is on.
    """
    return _truthy("AXONOS_GUEST_MODE_ENABLED", False)


def _bounded_int(env_name: str, default: int, low: int, high: int) -> int:
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %s", env_name, raw, default)
        return default
    if not (low <= value <= high):
        logger.warning(
            "Out-of-range %s=%r; expected %s..%s, using %s",
            env_name, raw, low, high, default,
        )
        return default
    return value


def default_session_minutes() -> int:
    return _bounded_int(
        "AXONOS_GUEST_SESSION_MINUTES", DEFAULT_SESSION_MINUTES, 1, MAX_SESSION_MINUTES
    )


def default_invite_ttl_hours() -> int:
    return _bounded_int(
        "AXONOS_GUEST_INVITE_TTL_HOURS", DEFAULT_INVITE_TTL_HOURS, 1, MAX_INVITE_TTL_HOURS
    )


def warn_seconds_for(session_minutes: int) -> int:
    """Lead time for the "demo ending" upsell warning, in seconds.

    The warning has to arrive while the prospect is still inside the desktop, so
    it fires before the hard cutoff rather than at it. Clamped below the session
    length: a short demo (a 2-minute operator test, say) warns at its halfway
    point instead of immediately, which would make the banner meaningless.
    """
    configured = _bounded_int(
        "AXONOS_GUEST_WARN_MINUTES", DEFAULT_WARN_MINUTES, 0, MAX_SESSION_MINUTES
    )
    try:
        total = int(session_minutes)
    except (TypeError, ValueError):
        total = DEFAULT_SESSION_MINUTES
    if configured <= 0:
        return 0
    if configured >= total:
        configured = max(1, total // 2)
    return configured * 60


def credit_buffer_minutes() -> float:
    raw = (os.getenv("AXONOS_GUEST_CREDIT_BUFFER_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_CREDIT_BUFFER_MINUTES
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CREDIT_BUFFER_MINUTES
    if not math.isfinite(value) or value < 0 or value > MAX_CREDIT_BUFFER_MINUTES:
        logger.warning(
            "Invalid AXONOS_GUEST_CREDIT_BUFFER_MINUTES=%r; using %s",
            raw, DEFAULT_CREDIT_BUFFER_MINUTES,
        )
        return DEFAULT_CREDIT_BUFFER_MINUTES
    return value


def max_live_demos_per_sponsor() -> int:
    return _bounded_int(
        "AXONOS_GUEST_MAX_LIVE_PER_SPONSOR",
        DEFAULT_MAX_LIVE_PER_SPONSOR, 1, MAX_LIVE_PER_SPONSOR_CEILING,
    )


def max_invites_per_day_per_sponsor() -> int:
    return _bounded_int(
        "AXONOS_GUEST_MAX_INVITES_PER_DAY",
        DEFAULT_MAX_INVITES_PER_DAY, 1, MAX_INVITES_PER_DAY_CEILING,
    )


def data_retention_days() -> int:
    """Days to retain expired per-demo identity and credit rows.

    Zero explicitly disables pruning for operators that require indefinite raw
    audit history. Invite rows are retained independently for sponsor/use audit.
    """
    return _bounded_int(
        "AXONOS_GUEST_DATA_RETENTION_DAYS",
        DEFAULT_DATA_RETENTION_DAYS,
        0,
        MAX_DATA_RETENTION_DAYS,
    )


def _parse_wallet_csv(raw: Optional[str]) -> frozenset:
    out = set()
    for chunk in str(raw or "").split(","):
        wallet = chunk.strip().lower()
        if _EVM_ADDRESS_RE.match(wallet):
            out.add(wallet)
    return frozenset(out)


def invite_minter_wallets() -> frozenset:
    """Wallets permitted to mint demo invites.

    Defaults to the existing test-credit wallet list: an operator who already
    trusts a wallet with free compute for itself is the same operator who wants
    that person handing prospects a demo. ``AXONOS_GUEST_INVITE_MINTERS``
    overrides it when the two groups should differ.

    Deliberately does NOT call ``is_wallet_whitelisted()``: that returns False
    whenever ``AXONOS_TEST_CREDITS_ENABLED`` is off, which would silently make
    demo mode depend on an unrelated feature being enabled.
    """
    explicit = (os.getenv("AXONOS_GUEST_INVITE_MINTERS") or "").strip()
    if explicit:
        return _parse_wallet_csv(explicit)
    inherited = (os.getenv("AXONOS_TEST_CREDIT_WALLETS") or "").strip()
    if inherited:
        return _parse_wallet_csv(inherited)
    return _parse_wallet_csv(os.getenv("AXONOS_WHITELISTED_WALLETS"))


def can_mint_invites(wallet_address: Optional[str]) -> bool:
    """True when *wallet_address* may mint demo invites. Fail-closed."""
    if not guest_mode_enabled():
        return False
    wallet = (wallet_address or "").strip().lower()
    if not _EVM_ADDRESS_RE.match(wallet):
        return False
    # A demo identity must never mint further demos, or a single invite could
    # propagate. Guest addresses are not in the operator's list anyway; this is
    # the explicit guarantee rather than an accident of configuration.
    if is_guest_identity(wallet):
        return False
    return wallet in invite_minter_wallets()


def _parse_name_csv(raw: Optional[str]) -> List[str]:
    """Parse a CSV allowlist, dropping anything that is not a safe identifier."""
    if not raw:
        return []
    out: List[str] = []
    for chunk in str(raw).split(","):
        name = chunk.strip().lower()
        if name and _NAME_RE.match(name) and name not in out:
            out.append(name)
    return out


def default_allowed_profiles() -> List[str]:
    """Hardware tiers a guest may launch.

    Defaults to the single-GPU tier only. This is the cost lever, so it is
    restrictive by default rather than open.
    """
    configured = _parse_name_csv(os.getenv("AXONOS_GUEST_ALLOWED_PROFILES"))
    return configured or list(DEFAULT_ALLOWED_PROFILES)


def default_allowed_templates() -> List[str]:
    """Environment templates a guest may launch; empty list means "any".

    Unlike the hardware tier, template choice does not change what a demo costs,
    so an unset allowlist permits all of them. Operators curating which
    environments a prospect sees set the list explicitly.
    """
    return _parse_name_csv(os.getenv("AXONOS_GUEST_ALLOWED_TEMPLATES"))


def _profile_gpu_counts() -> Dict[str, int]:
    """GPU-per-profile map, preferring the session manager's own definition."""
    try:
        sm = _import_session_manager()
        counts = sm._configured_profiles()
        if isinstance(counts, dict) and counts:
            return {str(k).lower(): int(v) for k, v in counts.items()}
    except Exception:
        pass
    return dict(_PROFILE_GPU_COUNTS_FALLBACK)


def _import_session_manager():
    # Imported lazily: session_manager imports this module, so a top-level
    # import here would be circular.
    try:
        from . import session_manager  # type: ignore
        return session_manager
    except ImportError:
        try:
            from axonos_gate import session_manager  # type: ignore
            return session_manager
        except ImportError:
            import session_manager  # type: ignore
            return session_manager


def _import_deposit_ledger():
    try:
        from . import deposit_ledger  # type: ignore
        return deposit_ledger
    except ImportError:
        try:
            from axonos_gate import deposit_ledger  # type: ignore
            return deposit_ledger
        except ImportError:
            import deposit_ledger  # type: ignore
            return deposit_ledger


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def new_guest_identity() -> str:
    """Mint a fresh synthetic guest address (lowercase, EVM-shaped)."""
    return "0x" + GUEST_ADDRESS_TAG + secrets.token_hex(15)


def is_guest_identity(address: Optional[str]) -> bool:
    """True when *address* is in the reserved guest namespace.

    Offline prefix test: no DB access, safe to call from any hot path or from
    websockify_gate.py.
    """
    if not address:
        return False
    return bool(_GUEST_ADDRESS_RE.match(str(address).strip().lower()))


def mask_guest_identity(address: Optional[str]) -> str:
    """Log-safe rendering of a guest address."""
    addr = (address or "").strip().lower()
    if not is_guest_identity(addr):
        return "***"
    return f"guest:{addr[-6:]}"


def mask_wallet(address: Optional[str]) -> str:
    """Log-safe rendering of any address, guest or real."""
    addr = (address or "").strip().lower()
    if is_guest_identity(addr):
        return mask_guest_identity(addr)
    if len(addr) < 10:
        return "***"
    return f"{addr[:6]}...{addr[-4:]}"


def reject_if_guest(address: Optional[str], what: str = "This endpoint") -> Optional[
    Tuple[Dict[str, Any], int]
]:
    """Deny a guest identity access to a wallet-only endpoint.

    Returns ``(payload, status)`` to refuse, or None to continue -- the same
    shape ``_require_auth_token`` uses, so each gate server can hand it to
    ``jsonify`` or to ``_send_json`` unchanged. The payload carries ``verified``,
    ``granted`` and ``ok`` all false because the refused endpoints disagree about
    which of the three means failure; a client checking any one of them sees it.
    """
    if not is_guest_identity(address):
        return None
    return (
        {
            "verified": False,
            "granted": False,
            "ok": False,
            "error_code": "guest_not_permitted",
            "guest_not_permitted": True,
            "error": (
                f"{what} is not available in a demo session. "
                "Connect a wallet to continue."
            ),
        },
        403,
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _db_url() -> Optional[str]:
    return os.getenv("AXGT_CHALLENGE_DB_URL") or None


def _get_connection():
    url = _db_url()
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as exc:
        logger.warning("guest_mode: Postgres connect failed: %s", exc)
        return None


def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        # Only the sha256 of an invite token is stored. The token itself is a
        # bearer credential that travels in a URL; it is shown once at mint time
        # and never persisted, so a database read cannot yield a working link.
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_INVITES_TABLE} (
                token_hash TEXT PRIMARY KEY,
                label TEXT,
                max_uses INTEGER NOT NULL DEFAULT 1,
                uses INTEGER NOT NULL DEFAULT 0,
                session_minutes INTEGER NOT NULL,
                allowed_profiles TEXT,
                allowed_templates TEXT,
                created_at DOUBLE PRECISION NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL,
                revoked BOOLEAN NOT NULL DEFAULT FALSE,
                created_by TEXT NOT NULL DEFAULT 'admin'
            )
        """)
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_INVITES_TABLE}_expires "
            f"ON {_INVITES_TABLE}(expires_at)"
        )
        # Sponsor quotas count rows by minting wallet on every mint and redeem.
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_INVITES_TABLE}_created_by "
            f"ON {_INVITES_TABLE}(created_by)"
        )
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_GUEST_SESSIONS_TABLE} (
                guest_address TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL,
                attempt_id TEXT,
                issued_at DOUBLE PRECISION NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL,
                session_minutes INTEGER NOT NULL,
                allowed_profiles TEXT,
                allowed_templates TEXT
            )
        """)
        cur.execute(
            f"ALTER TABLE {_GUEST_SESSIONS_TABLE} "
            "ADD COLUMN IF NOT EXISTS attempt_id TEXT"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_GUEST_SESSIONS_TABLE}_token "
            f"ON {_GUEST_SESSIONS_TABLE}(token_hash)"
        )
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS "
            f"idx_{_GUEST_SESSIONS_TABLE}_attempt "
            f"ON {_GUEST_SESSIONS_TABLE}(token_hash, attempt_id) "
            "WHERE attempt_id IS NOT NULL"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_GUEST_SESSIONS_TABLE}_expires "
            f"ON {_GUEST_SESSIONS_TABLE}(expires_at)"
        )
    conn.commit()


def init_once() -> bool:
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
            logger.warning("guest_mode: table init failed: %s", exc)
            return False
        finally:
            conn.close()


def _reset_init_state_for_tests() -> None:
    global _pg_init_done
    with _pg_init_lock:
        _pg_init_done = False


# --------------------------------------------------------------------------
# Invite lifecycle
# --------------------------------------------------------------------------

def mint_invite(
    label: Optional[str] = None,
    max_uses: int = 1,
    session_minutes: Optional[int] = None,
    allowed_profiles: Optional[List[str]] = None,
    allowed_templates: Optional[List[str]] = None,
    ttl_hours: Optional[int] = None,
    created_by: str = "admin",
) -> Dict[str, Any]:
    """Create a demo invite. Returns the raw token exactly once."""
    if not guest_mode_enabled():
        return {"ok": False, "error_code": "guest_mode_disabled", "error": "Guest mode is disabled"}

    label_norm = (label or "").strip()
    if not _LABEL_RE.match(label_norm):
        return {"ok": False, "error_code": "invalid_label", "error": "label has unsupported characters"}

    try:
        uses_allowed = int(max_uses)
    except (TypeError, ValueError):
        return {"ok": False, "error_code": "invalid_max_uses", "error": "max_uses must be an integer"}
    if not (1 <= uses_allowed <= MAX_USES_CEILING):
        return {
            "ok": False,
            "error_code": "invalid_max_uses",
            "error": f"max_uses must be between 1 and {MAX_USES_CEILING}",
        }

    minutes = default_session_minutes() if session_minutes is None else session_minutes
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return {"ok": False, "error_code": "invalid_session_minutes", "error": "session_minutes must be an integer"}
    if not (1 <= minutes <= MAX_SESSION_MINUTES):
        return {
            "ok": False,
            "error_code": "invalid_session_minutes",
            "error": f"session_minutes must be between 1 and {MAX_SESSION_MINUTES}",
        }

    profiles = (
        _parse_name_csv(",".join(allowed_profiles))
        if allowed_profiles
        else default_allowed_profiles()
    )
    if not profiles:
        return {
            "ok": False,
            "error_code": "invalid_allowed_profiles",
            "error": "allowed_profiles must name at least one hardware tier",
        }
    known = _profile_gpu_counts()
    unknown = [p for p in profiles if p not in known]
    if unknown:
        return {
            "ok": False,
            "error_code": "unknown_profile",
            "error": f"unknown hardware tier(s): {', '.join(unknown)}",
        }

    if allowed_templates is not None and not isinstance(allowed_templates, (list, tuple)):
        return {
            "ok": False,
            "error_code": "invalid_allowed_templates",
            "error": "allowed_templates must be a list of environment IDs",
        }
    try:
        template_values = (
            list(allowed_templates)
            if allowed_templates
            else default_allowed_templates()
        )
        templates: List[str] = []
        for value in template_values:
            normalized = normalize_session_template(value)
            if normalized and normalized not in templates:
                templates.append(normalized)
    except TypeError:
        return {
            "ok": False,
            "error_code": "invalid_allowed_templates",
            "error": "allowed_templates must contain only strings",
        }
    except ValueError:
        unknown = []
        for value in template_values:
            try:
                normalize_session_template(value)
            except (TypeError, ValueError):
                unknown.append(str(value))
        return {
            "ok": False,
            "error_code": "unknown_template",
            "allowed_templates": list(SUPPORTED_SESSION_TEMPLATE_IDS),
            "error": f"unknown environment(s): {', '.join(unknown)}",
        }

    hours = default_invite_ttl_hours() if ttl_hours is None else ttl_hours
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return {"ok": False, "error_code": "invalid_ttl", "error": "ttl_hours must be an integer"}
    if not (1 <= hours <= MAX_INVITE_TTL_HOURS):
        return {
            "ok": False,
            "error_code": "invalid_ttl",
            "error": f"ttl_hours must be between 1 and {MAX_INVITE_TTL_HOURS}",
        }

    if not init_once():
        return {
            "ok": False,
            "error_code": "guest_db_unavailable",
            "retryable": True,
            "error": "Guest invite database unavailable",
        }
    conn = _get_connection()
    if not conn:
        return {
            "ok": False,
            "error_code": "guest_db_unavailable",
            "retryable": True,
            "error": "Guest invite database unavailable",
        }

    token = secrets.token_urlsafe(_INVITE_TOKEN_BYTES)
    now = time.time()
    expires_at = now + hours * 3600.0
    sponsor = (created_by or "admin").strip().lower()[:64] or "admin"
    try:
        with conn.cursor() as cur:
            # Rate-limit a sponsor's minting. Without this one wallet can mint
            # unbounded links; the live-session quota below then caps how many
            # of them can burn a GPU at once.
            if _EVM_ADDRESS_RE.match(sponsor):
                per_day = max_invites_per_day_per_sponsor()
                cur.execute(
                    f"""SELECT COUNT(*) FROM {_INVITES_TABLE}
                        WHERE created_by = %s AND created_at > %s""",
                    (sponsor, now - 86400.0),
                )
                row = cur.fetchone()
                minted_today = int(row[0]) if row else 0
                if minted_today >= per_day:
                    conn.rollback()
                    logger.warning(
                        "guest_mode: mint refused, sponsor %s at daily cap %s",
                        mask_wallet(sponsor), per_day,
                    )
                    return {
                        "ok": False,
                        "error_code": "sponsor_daily_limit",
                        "minted_today": minted_today,
                        "limit": per_day,
                        "error": (
                            f"You have minted {minted_today} demo links today "
                            f"(limit {per_day}). Try again tomorrow."
                        ),
                    }
            cur.execute(
                f"""INSERT INTO {_INVITES_TABLE}
                    (token_hash, label, max_uses, uses, session_minutes,
                     allowed_profiles, allowed_templates, created_at, expires_at,
                     revoked, created_by)
                    VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s, FALSE, %s)""",
                (
                    _token_hash(token),
                    label_norm or None,
                    uses_allowed,
                    minutes,
                    ",".join(profiles),
                    ",".join(templates) or None,
                    now,
                    expires_at,
                    sponsor,
                ),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning("guest_mode: invite mint failed: %s", exc)
        return {"ok": False, "error_code": "mint_failed", "error": "Could not create invite"}
    finally:
        conn.close()

    logger.info(
        "guest_mode: minted invite label=%r max_uses=%s minutes=%s profiles=%s by=%s",
        label_norm or "-", uses_allowed, minutes, ",".join(profiles),
        mask_wallet(sponsor),
    )
    return {
        "ok": True,
        # Returned once and never stored. Only sha256(token) is persisted.
        "token": token,
        "token_hash": _token_hash(token),
        "label": label_norm or None,
        "max_uses": uses_allowed,
        "session_minutes": minutes,
        "allowed_profiles": profiles,
        "allowed_templates": templates,
        "expires_at": expires_at,
        "created_by": sponsor,
    }


def _redemption_payload(
    *,
    guest_address: str,
    token_hash: str,
    attempt_id: str,
    minutes: int,
    session_expires_at: float,
    profiles_raw: Optional[str],
    templates_raw: Optional[str],
    label: Optional[str],
    sponsor: Optional[str],
    reused_attempt: bool,
) -> Dict[str, Any]:
    now = time.time()
    return {
        "ok": True,
        "guest_address": guest_address,
        # The hash, never the token: callers derive the idempotency key for the
        # credit grant from this so the raw bearer value goes no further.
        "token_hash": token_hash,
        "attempt_id": attempt_id,
        "reused_attempt": reused_attempt,
        "session_minutes": int(minutes),
        "expires_at": float(session_expires_at),
        "remaining_seconds": int(max(0, float(session_expires_at) - now)),
        "warn_seconds": warn_seconds_for(int(minutes)),
        "allowed_profiles": _parse_name_csv(profiles_raw) or default_allowed_profiles(),
        "allowed_templates": _parse_name_csv(templates_raw),
        "label": label,
        "sponsor": sponsor,
    }


def redeem_invite(token: str, attempt_id: Optional[str] = None) -> Dict[str, Any]:
    """Consume one use of an invite and mint a guest identity for it.

    One transaction, serialized per invite by an advisory lock, so concurrent
    redemptions of the same link cannot both observe a stale use count.
    """
    if not guest_mode_enabled():
        return {"ok": False, "error_code": "guest_mode_disabled", "error": "Guest mode is disabled"}

    token_norm = (token or "").strip()
    if not _INVITE_TOKEN_RE.match(token_norm):
        # Same generic answer as an unknown token: never distinguish malformed
        # from unrecognized, so the endpoint is not a token-shape oracle.
        return {"ok": False, "error_code": "invalid_invite", "error": "Invite is not valid"}

    supplied_attempt = attempt_id is not None
    attempt_norm = (
        str(attempt_id or "").strip()
        if supplied_attempt
        else secrets.token_urlsafe(18)
    )
    if not _ATTEMPT_ID_RE.fullmatch(attempt_norm):
        return {
            "ok": False,
            "error_code": "invalid_attempt_id",
            "error": "Demo attempt is not valid",
        }

    if not init_once():
        return {
            "ok": False,
            "error_code": "guest_db_unavailable",
            "retryable": True,
            "error": "Guest invite database unavailable",
        }
    conn = _get_connection()
    if not conn:
        return {
            "ok": False,
            "error_code": "guest_db_unavailable",
            "retryable": True,
            "error": "Guest invite database unavailable",
        }

    token_hash = _token_hash(token_norm)
    invalid = {"ok": False, "error_code": "invalid_invite", "error": "Invite is not valid"}
    now = time.time()
    try:
        with conn.cursor() as cur:
            # Serialize every redemption of this one invite.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (token_hash,))
            cur.execute(
                f"""SELECT max_uses, uses, session_minutes, allowed_profiles,
                           allowed_templates, expires_at, revoked, label, created_by
                    FROM {_INVITES_TABLE} WHERE token_hash = %s""",
                (token_hash,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return invalid
            (max_uses, uses, minutes, profiles_raw, templates_raw,
             expires_at, revoked, label, sponsor) = row
            if revoked:
                conn.rollback()
                logger.warning("guest_mode: redemption refused, invite revoked (label=%r)", label)
                return {"ok": False, "error_code": "invite_revoked", "error": "This demo link was revoked"}

            # A browser keeps one non-secret attempt ID in sessionStorage. If
            # ledger/auth setup transiently fails after this transaction commits,
            # retrying the same invite+attempt recovers the exact identity instead
            # of consuming another use (or burning a single-use sales link).
            if supplied_attempt:
                cur.execute(
                    f"""SELECT guest_address, expires_at, session_minutes,
                               allowed_profiles, allowed_templates
                        FROM {_GUEST_SESSIONS_TABLE}
                        WHERE token_hash = %s AND attempt_id = %s""",
                    (token_hash, attempt_norm),
                )
                existing = cur.fetchone()
                if existing:
                    (guest_address, session_expires_at, existing_minutes,
                     existing_profiles, existing_templates) = existing
                    if float(session_expires_at) <= now:
                        conn.rollback()
                        return {
                            "ok": False,
                            "error_code": "invite_exhausted",
                            "error": "This demo link has already been used",
                        }
                    conn.commit()
                    logger.info(
                        "guest_mode: resumed redemption attempt label=%r identity=%s",
                        label, mask_guest_identity(guest_address),
                    )
                    return _redemption_payload(
                        guest_address=str(guest_address).lower(),
                        token_hash=token_hash,
                        attempt_id=attempt_norm,
                        minutes=int(existing_minutes),
                        session_expires_at=float(session_expires_at),
                        profiles_raw=existing_profiles,
                        templates_raw=existing_templates,
                        label=label,
                        sponsor=sponsor,
                        reused_attempt=True,
                    )
            if float(expires_at) <= now:
                conn.rollback()
                logger.warning("guest_mode: redemption refused, invite expired (label=%r)", label)
                return {"ok": False, "error_code": "invite_expired", "error": "This demo link has expired"}
            if int(uses) >= int(max_uses):
                conn.rollback()
                logger.warning(
                    "guest_mode: redemption refused, invite exhausted %s/%s (label=%r)",
                    uses, max_uses, label,
                )
                return {
                    "ok": False,
                    "error_code": "invite_exhausted",
                    "error": "This demo link has already been used",
                }

            if _invite_has_live_session(cur, token_hash, now):
                conn.rollback()
                logger.warning(
                    "guest_mode: redemption refused, invite already has a live demo (label=%r)",
                    label,
                )
                return {
                    "ok": False,
                    "error_code": "invite_session_active",
                    "error": "A demo session from this link is already running",
                }

            # The quota that actually protects the fleet. Each redemption mints a
            # NEW identity with its own ledger row, so no per-identity balance cap
            # can bound a sponsor who minted many links -- only a count of that
            # sponsor's currently-live demos can.
            sponsor_norm = str(sponsor or "").strip().lower()
            if _EVM_ADDRESS_RE.match(sponsor_norm):
                # Different invite hashes otherwise take different locks and
                # can all observe the same stale sponsor count. Serialize the
                # quota decision across every link minted by this sponsor.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"guest-sponsor:{sponsor_norm}",),
                )
            live_cap = max_live_demos_per_sponsor()
            live_now = _sponsor_live_session_count(cur, sponsor, now)
            if live_now >= live_cap:
                conn.rollback()
                logger.warning(
                    "guest_mode: redemption refused, sponsor %s at live cap %s/%s",
                    mask_wallet(sponsor), live_now, live_cap,
                )
                return {
                    "ok": False,
                    "error_code": "sponsor_live_limit",
                    "live_sessions": live_now,
                    "limit": live_cap,
                    "error": (
                        "Too many demo sessions are running from this team member's "
                        "links right now. Try again shortly."
                    ),
                }

            minutes = int(minutes)
            guest_address = new_guest_identity()
            session_expires_at = now + minutes * 60.0
            cur.execute(
                f"""INSERT INTO {_GUEST_SESSIONS_TABLE}
                    (guest_address, token_hash, attempt_id, issued_at, expires_at,
                     session_minutes, allowed_profiles, allowed_templates)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    guest_address,
                    token_hash,
                    attempt_norm,
                    now,
                    session_expires_at,
                    minutes,
                    profiles_raw,
                    templates_raw,
                ),
            )
            cur.execute(
                f"UPDATE {_INVITES_TABLE} SET uses = uses + 1 WHERE token_hash = %s",
                (token_hash,),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning("guest_mode: invite redemption failed: %s", exc)
        return {"ok": False, "error_code": "redeem_failed", "error": "Could not start a demo session"}
    finally:
        conn.close()

    logger.info(
        "guest_mode: redeemed invite label=%r use=%s/%s identity=%s minutes=%s",
        label, int(uses) + 1, int(max_uses), mask_guest_identity(guest_address), minutes,
    )
    return _redemption_payload(
        guest_address=guest_address,
        token_hash=token_hash,
        attempt_id=attempt_norm,
        minutes=minutes,
        session_expires_at=session_expires_at,
        profiles_raw=profiles_raw,
        templates_raw=templates_raw,
        label=label,
        sponsor=sponsor,
        reused_attempt=False,
    )


def _invite_has_live_session(cur, token_hash: str, now: float) -> bool:
    """True when this invite already occupies a demo session.

    Counts both a running container and a just-issued identity whose claim is
    still in flight, so a double-click cannot open two demos on one invite.
    """
    cur.execute(
        f"""SELECT 1
              FROM {_GUEST_SESSIONS_TABLE} gs
             WHERE gs.token_hash = %s
               AND gs.expires_at > %s
               AND (
                     gs.issued_at > %s
                     OR EXISTS (
                          SELECT 1 FROM {_SESSION_TABLE} s
                           WHERE s.wallet_address = gs.guest_address
                             AND s.status = 'active'
                     )
                   )
             LIMIT 1""",
        (token_hash, now, now - _RESERVATION_WINDOW_SECONDS),
    )
    return cur.fetchone() is not None


def _sponsor_live_session_count(cur, sponsor: Optional[str], now: float) -> int:
    """How many demo sessions minted by *sponsor* are live right now.

    Counts the same two states as the per-invite check: a running container, and
    a just-issued identity whose claim is still in flight.
    """
    if not sponsor or not _EVM_ADDRESS_RE.match(str(sponsor).strip().lower()):
        return 0  # admin/CLI-minted invites are not quota-limited
    cur.execute(
        f"""SELECT COUNT(*)
              FROM {_GUEST_SESSIONS_TABLE} gs
              JOIN {_INVITES_TABLE} inv ON inv.token_hash = gs.token_hash
             WHERE inv.created_by = %s
               AND gs.expires_at > %s
               AND (
                     gs.issued_at > %s
                     OR EXISTS (
                          SELECT 1 FROM {_SESSION_TABLE} s
                           WHERE s.wallet_address = gs.guest_address
                             AND s.status = 'active'
                     )
                   )""",
        (str(sponsor).strip().lower(), now, now - _RESERVATION_WINDOW_SECONDS),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def claim_capacity_rejection(
    cur,
    guest_address: str,
    token_hash: str,
    sponsor: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Enforce invite/sponsor concurrency at the instant of GPU allocation.

    Redemption-time reservations stop double-clicks, but an issued identity is
    claimable until its own deadline. A user could otherwise redeem identities
    sequentially, wait out the short reservation window, then claim/re-claim
    several at once. The session manager calls this while holding the global
    scheduler lock, the wallet lock, and the invite lock.
    """
    address = (guest_address or "").strip().lower()
    cur.execute(
        f"""SELECT 1
            FROM {_GUEST_SESSIONS_TABLE} AS gs
            JOIN {_SESSION_TABLE} AS s
              ON s.wallet_address = gs.guest_address
            WHERE gs.token_hash = %s
              AND gs.guest_address <> %s
              AND s.status IN ('active', 'credit_grace')
            LIMIT 1""",
        (token_hash, address),
    )
    if cur.fetchone() is not None:
        return {
            "granted": False,
            "error_code": "invite_session_active",
            "reason": "Another demo from this link is already running.",
        }

    sponsor_norm = str(sponsor or "").strip().lower()
    if not _EVM_ADDRESS_RE.match(sponsor_norm):
        return None
    # Same ordering as redemption: invite lock first, sponsor lock second.
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"guest-sponsor:{sponsor_norm}",),
    )
    cur.execute(
        f"""SELECT COUNT(*)
            FROM {_GUEST_SESSIONS_TABLE} AS gs
            JOIN {_INVITES_TABLE} AS inv ON inv.token_hash = gs.token_hash
            JOIN {_SESSION_TABLE} AS s ON s.wallet_address = gs.guest_address
            WHERE inv.created_by = %s
              AND gs.guest_address <> %s
              AND s.status IN ('active', 'credit_grace')""",
        (sponsor_norm, address),
    )
    row = cur.fetchone()
    live_others = int(row[0]) if row else 0
    live_cap = max_live_demos_per_sponsor()
    if live_others >= live_cap:
        return {
            "granted": False,
            "error_code": "sponsor_live_limit",
            "live_sessions": live_others,
            "limit": live_cap,
            "reason": (
                "Too many demo sessions are running from this team member's "
                "links right now. Try again shortly."
            ),
        }
    return None


def revoke_invite(token_or_hash: str) -> Dict[str, Any]:
    """Revoke an invite and end every demo identity issued from it.

    Revocation is serialized with redemption by the same advisory lock.  Guest
    and live-session deadlines are shortened in the revocation transaction,
    giving the periodic session sweep a durable fallback, then the normal
    exact-session release path performs immediate container teardown after the
    commit.  External teardown is deliberately never attempted while holding
    the invite lock: release takes the scheduler lock, while claims take the
    scheduler lock before this invite lock.
    """
    raw = (token_or_hash or "").strip()
    if not raw:
        return {"ok": False, "error_code": "invalid_invite", "error": "Invite is not valid"}
    candidates = [raw.lower()] if re.fullmatch(r"[0-9a-fA-F]{64}", raw) else []
    if _INVITE_TOKEN_RE.match(raw):
        candidates.insert(0, _token_hash(raw))
    if not candidates:
        return {"ok": False, "error_code": "invalid_invite", "error": "Invite is not valid"}

    if not init_once():
        return {
            "ok": False,
            "error_code": "guest_db_unavailable",
            "retryable": True,
            "error": "Guest invite database unavailable",
        }
    conn = _get_connection()
    if not conn:
        return {
            "ok": False,
            "error_code": "guest_db_unavailable",
            "retryable": True,
            "error": "Guest invite database unavailable",
        }
    matched_hash: Optional[str] = None
    already_revoked = False
    session_targets: List[Tuple[str, int]] = []
    now = time.time()
    try:
        with conn.cursor() as cur:
            for token_hash in candidates:
                # The redeem path uses this exact lock. A revoke can therefore
                # neither miss a just-committed identity nor be undone by a
                # redemption that observed revoked=FALSE concurrently.
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (token_hash,))
                cur.execute(
                    f"SELECT revoked FROM {_INVITES_TABLE} WHERE token_hash = %s",
                    (token_hash,),
                )
                invite_row = cur.fetchone()
                if not invite_row:
                    continue
                matched_hash = token_hash
                already_revoked = bool(invite_row[0])
                cur.execute(
                    f"UPDATE {_INVITES_TABLE} SET revoked = TRUE WHERE token_hash = %s",
                    (token_hash,),
                )
                # Issued-but-unclaimed identities must be permanently inert too.
                cur.execute(
                    f"""UPDATE {_GUEST_SESSIONS_TABLE}
                        SET expires_at = LEAST(expires_at, %s)
                        WHERE token_hash = %s""",
                    (now, token_hash),
                )
                # Do not mark these rows ended here: doing so would expose their
                # GPUs before Docker teardown. The deadline is the durable
                # fallback; release_session below owns the state+runtime order.
                cur.execute(
                    f"""UPDATE {_SESSION_TABLE} AS s
                        SET expires_at = LEAST(s.expires_at, %s)
                        WHERE s.status IN ('active', 'credit_grace')
                          AND s.wallet_address IN (
                              SELECT gs.guest_address
                              FROM {_GUEST_SESSIONS_TABLE} AS gs
                              WHERE gs.token_hash = %s
                          )
                        RETURNING s.wallet_address, s.id""",
                    (now, token_hash),
                )
                session_targets = [
                    (str(row[0]).lower(), int(row[1]))
                    for row in (cur.fetchall() or [])
                ]
                break
        if matched_hash is None:
            conn.commit()
            return {
                "ok": False,
                "error_code": "invite_not_found",
                "error": "No invite matched",
            }
        # Make revocation authoritative before any external/container work.
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning("guest_mode: invite revoke failed: %s", exc)
        return {"ok": False, "error_code": "revoke_failed", "error": "Could not revoke invite"}
    finally:
        conn.close()

    stopped = 0
    pending: List[int] = []
    if session_targets:
        try:
            session_manager = _import_session_manager()
        except Exception as exc:
            logger.warning("guest_mode: session manager unavailable during revoke: %s", exc)
            session_manager = None
        for guest_address, session_id in session_targets:
            try:
                result = (
                    session_manager.release_session(
                        guest_address,
                        expected_session_id=session_id,
                    )
                    if session_manager is not None
                    else {"released": False}
                )
            except Exception as exc:
                logger.warning(
                    "guest_mode: teardown raised for revoked demo session %s: %s",
                    session_id, exc,
                )
                result = {"released": False}
            if result.get("released"):
                stopped += 1
            else:
                pending.append(session_id)

    logger.warning(
        "guest_mode: invite revoked (hash=%s... targets=%s stopped=%s pending=%s)",
        matched_hash[:12], len(session_targets), stopped, len(pending),
    )
    return {
        "ok": True,
        "revoked": True,
        "already_revoked": already_revoked,
        "token_hash": matched_hash,
        "sessions_targeted": len(session_targets),
        "sessions_stopped": stopped,
        "cleanup_pending": bool(pending),
        "cleanup_pending_session_ids": pending,
    }


def list_invites(limit: int = 100) -> Dict[str, Any]:
    """List invites (hashes and usage only -- raw tokens are unrecoverable)."""
    try:
        cap = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        cap = 100
    if not init_once():
        return {
            "ok": False,
            "error_code": "guest_db_unavailable",
            "retryable": True,
            "error": "Guest invite database unavailable",
        }
    conn = _get_connection()
    if not conn:
        return {
            "ok": False,
            "error_code": "guest_db_unavailable",
            "retryable": True,
            "error": "Guest invite database unavailable",
        }
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT token_hash, label, max_uses, uses, session_minutes,
                           allowed_profiles, allowed_templates, created_at,
                           expires_at, revoked, created_by
                    FROM {_INVITES_TABLE}
                    ORDER BY created_at DESC LIMIT %s""",
                (cap,),
            )
            rows = cur.fetchall() or []
    except Exception as exc:
        logger.warning("guest_mode: invite list failed: %s", exc)
        return {"ok": False, "error_code": "list_failed", "error": "Could not list invites"}
    finally:
        conn.close()

    now = time.time()
    invites = []
    for r in rows:
        invites.append({
            "token_hash": r[0],
            "label": r[1],
            "max_uses": int(r[2]),
            "uses": int(r[3]),
            "session_minutes": int(r[4]),
            "allowed_profiles": _parse_name_csv(r[5]),
            "allowed_templates": _parse_name_csv(r[6]),
            "created_at": float(r[7]),
            "expires_at": float(r[8]),
            "revoked": bool(r[9]),
            "created_by": r[10],
            "usable": (
                not bool(r[9]) and float(r[8]) > now and int(r[3]) < int(r[2])
            ),
        })
    return {"ok": True, "invites": invites, "count": len(invites)}


# --------------------------------------------------------------------------
# Expired demo data retention
# --------------------------------------------------------------------------

def reap_expired_guest_data(
    *,
    now: Optional[float] = None,
    batch_size: int = DEFAULT_REAPER_BATCH_SIZE,
) -> Dict[str, Any]:
    """Delete one bounded batch of expired per-demo data.

    ``axgt_guest_sessions`` is the authoritative anchor. Never infer ownership
    from the reserved address prefix: a real vanity address can occupy that
    40-bit namespace. The caller runs this after session teardown hooks while
    holding the allocation scheduler lock, preventing a late ``session_expiry``
    event from recreating an orphan ledger row.

    Invite and generic session-history rows are intentionally retained. Missing
    tables are treated as a quiet no-op so never-enabled deployments do not
    create guest schema or emit a warning on every cleanup sweep.
    """
    retention_days = data_retention_days()
    if retention_days == 0:
        return {"ok": True, "skipped": True, "reason": "retention_disabled", "deleted": 0}
    try:
        batch = max(1, min(MAX_REAPER_BATCH_SIZE, int(batch_size)))
    except (TypeError, ValueError):
        batch = DEFAULT_REAPER_BATCH_SIZE
    reference = time.time() if now is None else float(now)
    cutoff = reference - retention_days * 86400.0

    conn = _get_connection()
    if not conn:
        return {
            "ok": False,
            "error_code": "guest_db_unavailable",
            "error": "Guest data database unavailable",
        }
    try:
        with conn.cursor() as cur:
            # The guest table may never have been created. Child tables can be
            # sparse after a failed credit transaction, so discover each one
            # and delete only what exists.
            cur.execute(
                "SELECT to_regclass(%s), to_regclass(%s), "
                "to_regclass(%s), to_regclass(%s)",
                (
                    _GUEST_SESSIONS_TABLE,
                    "axgt_ledger",
                    "axgt_verified_deposits",
                    "axgt_deposits",
                ),
            )
            table_row = cur.fetchone()
            if not table_row or table_row[0] is None:
                conn.commit()
                return {"ok": True, "skipped": True, "reason": "guest_table_absent", "deleted": 0}

            cur.execute(
                f"""SELECT gs.guest_address
                    FROM {_GUEST_SESSIONS_TABLE} AS gs
                    WHERE gs.expires_at <= %s
                      AND NOT EXISTS (
                          SELECT 1 FROM {_SESSION_TABLE} AS s
                          WHERE s.wallet_address = gs.guest_address
                            AND s.status NOT IN ('ended', 'expired', 'released')
                      )
                    ORDER BY gs.expires_at
                    LIMIT %s
                    FOR UPDATE OF gs SKIP LOCKED""",
                (cutoff, batch),
            )
            addresses = [str(row[0]).lower() for row in (cur.fetchall() or [])]
            if not addresses:
                conn.commit()
                return {"ok": True, "deleted": 0}

            counts = {
                "ledger_rows": 0,
                "verified_deposit_rows": 0,
                "deposit_rows": 0,
                "guest_session_rows": 0,
            }
            # Child/audit rows first; the authoritative anchor is deleted last.
            for exists, table_name, count_name in (
                (table_row[1], "axgt_ledger", "ledger_rows"),
                (table_row[2], "axgt_verified_deposits", "verified_deposit_rows"),
                (table_row[3], "axgt_deposits", "deposit_rows"),
            ):
                if exists is None:
                    continue
                cur.execute(
                    f"DELETE FROM {table_name} WHERE wallet_address = ANY(%s)",
                    (addresses,),
                )
                counts[count_name] = max(0, int(cur.rowcount or 0))
            cur.execute(
                f"DELETE FROM {_GUEST_SESSIONS_TABLE} WHERE guest_address = ANY(%s)",
                (addresses,),
            )
            counts["guest_session_rows"] = max(0, int(cur.rowcount or 0))
        conn.commit()
        logger.info(
            "guest_mode: reaped %s expired demo identity row(s) older than %s day(s)",
            len(addresses), retention_days,
        )
        return {
            "ok": True,
            "deleted": len(addresses),
            "retention_days": retention_days,
            **counts,
        }
    except Exception as exc:
        conn.rollback()
        logger.warning("guest_mode: expired data reaper failed: %s", exc)
        return {
            "ok": False,
            "error_code": "guest_reaper_failed",
            "error": "Could not prune expired guest data",
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Guest session lookups
# --------------------------------------------------------------------------

def guest_session_for_cursor(cur, address: str) -> Optional[Dict[str, Any]]:
    """Return a guest record using the caller's current transaction.

    Claims use this after taking the invite advisory lock. Opening a second
    connection there would not be protected by the transaction snapshot/lock
    whose ordering makes revocation and claim mutually exclusive.
    """
    addr = (address or "").strip().lower()
    if not is_guest_identity(addr):
        return None
    cur.execute(
        f"""SELECT gs.guest_address, gs.token_hash, gs.issued_at,
                   gs.expires_at, gs.session_minutes, gs.allowed_profiles,
                   gs.allowed_templates, inv.created_by, inv.revoked
            FROM {_GUEST_SESSIONS_TABLE} AS gs
            JOIN {_INVITES_TABLE} AS inv ON inv.token_hash = gs.token_hash
            WHERE gs.guest_address = %s""",
        (addr,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "guest_address": row[0],
        "token_hash": row[1],
        "issued_at": float(row[2]),
        "expires_at": float(row[3]),
        "session_minutes": int(row[4]),
        "allowed_profiles": _parse_name_csv(row[5]) or default_allowed_profiles(),
        "allowed_templates": _parse_name_csv(row[6]),
        "sponsor": row[7],
        "invite_revoked": bool(row[8]),
    }


def guest_session_for(address: str) -> Optional[Dict[str, Any]]:
    """Return the guest-session record for *address*, or None."""
    addr = (address or "").strip().lower()
    if not is_guest_identity(addr):
        return None
    if not init_once():
        return None
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            return guest_session_for_cursor(cur, addr)
    except Exception as exc:
        logger.warning("guest_mode: guest session lookup failed: %s", exc)
        return None
    finally:
        conn.close()


def session_cap_seconds_for(address: str, now: Optional[float] = None) -> Optional[float]:
    """Seconds of demo time left for *address*, or None when not a guest.

    This is the authoritative deadline: it is set at redemption and never
    extended, so re-claiming or reloading cannot buy more time.
    """
    record = guest_session_for(address)
    if not record:
        return None
    reference = time.time() if now is None else now
    return max(0.0, float(record["expires_at"]) - reference)


def allowed_profiles_for(address: str) -> List[str]:
    record = guest_session_for(address)
    if not record:
        return default_allowed_profiles()
    return record["allowed_profiles"]


def allowed_templates_for(address: str) -> List[str]:
    record = guest_session_for(address)
    if not record:
        return default_allowed_templates()
    return record["allowed_templates"]


def profile_allowed(address: str, profile: Optional[str]) -> bool:
    allowed = allowed_profiles_for(address)
    if not allowed:
        return True
    return (profile or "").strip().lower() in allowed


def template_allowed(address: str, template: Optional[str]) -> bool:
    """Empty allowlist means any template is permitted."""
    allowed = allowed_templates_for(address)
    if not allowed:
        return True
    name = (template or "").strip().lower()
    if not name:
        return True  # no template selected == the plain desktop
    return name in allowed


# --------------------------------------------------------------------------
# Credit
# --------------------------------------------------------------------------

def credit_minutes_for(session_minutes: int, allowed_profiles: List[str]) -> float:
    """Ledger minutes to grant for a demo of *session_minutes* wall-clock.

    Deliberately above the wall-clock cap. Heartbeat billing charges GPU-weighted
    minutes, so the grant scales with the largest tier the invite permits; the
    headroom keeps ``hard_expires_at`` the binding limit rather than credit
    exhaustion, which would park the container in credit-grace holding a GPU.
    """
    counts = _profile_gpu_counts()
    gpus = max([counts.get(p, 1) for p in (allowed_profiles or [])] or [1])
    return math.ceil(session_minutes * gpus * _CREDIT_HEADROOM_FACTOR) + credit_buffer_minutes()


def grant_guest_credit(
    guest_address: str,
    session_minutes: int,
    allowed_profiles: List[str],
    token_hash: str,
) -> Dict[str, Any]:
    """Credit a redeemed guest identity its demo minutes."""
    if not is_guest_identity(guest_address):
        return {"ok": False, "error_code": "not_guest_identity", "error": "Not a guest identity"}
    minutes = float(credit_minutes_for(session_minutes, allowed_profiles))
    ledger = _import_deposit_ledger()
    try:
        # Reuses the test-credit rail's advisory-lock + replay logic, but writes
        # its own provenance so demo minutes stay distinguishable from a team
        # member's test credit in axgt_ledger. A guest identity is freshly minted
        # per redemption, so its balance is 0 and cap == grant.
        base_request_id = f"{(token_hash or '')[:32]}-{guest_address[-12:]}"
        remaining = minutes
        chunk_index = 0
        last_result: Dict[str, Any] = {"ok": True}
        while remaining > 0:
            chunk_index += 1
            chunk = min(remaining, MAX_GUEST_CREDIT_CHUNK_MINUTES)
            last_result = ledger.credit_test_grant(
                wallet_address=guest_address,
                grant_minutes=chunk,
                # Every chunk converges on the same total balance ceiling.
                # Replaying after a partial/transient failure is therefore safe.
                max_balance_minutes=minutes,
                request_id=f"{base_request_id}-{chunk_index}",
                payment_rail="guest",
                credit_source="guest_credit",
                event_type="guest_credit",
                created_by="guest_mode",
                reference_prefix="guest-credit",
                notes_label="Guest demo credit",
            )
            if not last_result.get("ok"):
                return last_result
            remaining -= chunk
        return {
            **last_result,
            "ok": True,
            "credited_minutes": minutes,
            "credit_chunks": chunk_index,
        }
    except Exception as exc:
        logger.error(
            "guest_mode: credit grant failed for %s: %s",
            mask_guest_identity(guest_address), exc, exc_info=True,
        )
        return {"ok": False, "error_code": "credit_failed", "error": "Could not fund the demo session"}
