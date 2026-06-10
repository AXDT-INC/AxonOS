#!/usr/bin/env python3
"""AXGT Gate Server - API helper implementation."""

import os
import sys
import logging
import secrets
import time
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from security_utils import (
    SimpleRateLimiter,
    cors_origin_for_request,
    get_rate_limiter_from_env,
    parse_cors_allowlist,
)

# Add /axonos_gate to path for imports
_script_dir = os.path.dirname(os.path.abspath(__file__))
if '/axonos_gate' not in sys.path:
    sys.path.insert(0, '/axonos_gate')

# Import our modules
try:
    from axgt_verifier import (
        get_challenge_message,
        get_challenge_ttl_seconds,
        get_credit_policy,
        get_wallet_access_status,
        mask_wallet_address,
        validate_wallet_address,
        verify_signed_challenge,
    )
except ImportError:
    # Fallback to package import
    try:
        from axonos_gate.axgt_verifier import (
            get_challenge_message,
            get_challenge_ttl_seconds,
            get_credit_policy,
            get_wallet_access_status,
            mask_wallet_address,
            validate_wallet_address,
            verify_signed_challenge,
        )
    except ImportError as e:
        print(f"ERROR: Cannot import axgt_verifier: {e}", file=sys.stderr)
        sys.exit(1)

try:
    from deposit_verifier import verify_deposit, verify_deposit_is_pending
except ImportError:
    try:
        from axonos_gate.deposit_verifier import verify_deposit, verify_deposit_is_pending
    except ImportError:
        verify_deposit = None
        verify_deposit_is_pending = None

try:
    from session_manager import (
        get_active_session,
        heartbeat as session_heartbeat,
        is_session_owner,
        release_session,
        restart_desktop_session,
        session_status,
        try_claim_session,
    )
    _session_mgr_available = True
except ImportError:
    try:
        from axonos_gate.session_manager import (
            get_active_session,
            heartbeat as session_heartbeat,
            is_session_owner,
            release_session,
            restart_desktop_session,
            session_status,
            try_claim_session,
        )
        _session_mgr_available = True
    except ImportError:
        _session_mgr_available = False

try:
    from webrtc import config as webrtc_config
    from webrtc import service as webrtc_service
except ImportError:
    try:
        from axonos_gate.webrtc import config as webrtc_config
        from axonos_gate.webrtc import service as webrtc_service
    except ImportError:
        webrtc_config = None
        webrtc_service = None

_webrtc_sig_limiter = None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# CORS: default is same-origin (no wildcard). For unusual deployments, set AXGT_CORS_ORIGINS
# to "*" or a comma-separated list of allowed origins.
_allow_any, _allowlist = parse_cors_allowlist(os.getenv("AXGT_CORS_ORIGINS"))
# Keep flask-cors installed but don't let it default to "*".
CORS(app, resources={r"/api/*": {"origins": []}})

_rate_limiter = get_rate_limiter_from_env()

NOVNC_WEB_DIR = Path('/usr/share/novnc')

# Postgres-backed auth tokens (shared across backends via AXGT_CHALLENGE_DB_URL)
_AUTH_TOKEN_TTL = int(os.getenv("AXGT_AUTH_TOKEN_TTL_SECONDS", "300").strip() or "300") or 300
_AUTH_TABLE = "axgt_auth_tokens"
_gate_pg_init_done = False
_gate_pg_init_lock = threading.Lock()


def _gate_db_url():
    return os.getenv("AXGT_CHALLENGE_DB_URL") or None


def _gate_pg_get_connection():
    url = _gate_db_url()
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as e:
        logger.warning("Postgres auth token DB connect failed: %s", e)
        return None


def _gate_pg_init_once() -> bool:
    global _gate_pg_init_done
    if not _gate_db_url():
        return False
    with _gate_pg_init_lock:
        if _gate_pg_init_done:
            return True
        conn = _gate_pg_get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_AUTH_TABLE} (
                        token TEXT PRIMARY KEY,
                        wallet_address TEXT NOT NULL,
                        issued_at DOUBLE PRECISION NOT NULL,
                        expires_at DOUBLE PRECISION NOT NULL,
                        status TEXT NOT NULL DEFAULT 'current',
                        grace_until DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{_AUTH_TABLE}_wallet ON {_AUTH_TABLE} (wallet_address)"
                )
            conn.commit()
            _gate_pg_init_done = True
            return True
        except Exception as e:
            logger.warning("Postgres auth token table init failed: %s", e)
            return False
        finally:
            conn.close()


def _issue_gate_auth_token(wallet_address: str) -> tuple[str, int]:
    now_ts = time.time()
    token = secrets.token_urlsafe(32)
    wallet_norm = (wallet_address or "").strip().lower()
    if not wallet_norm:
        raise RuntimeError("Wallet address required")
    if not _gate_pg_init_once():
        raise RuntimeError("Auth token DB unavailable")
    conn = _gate_pg_get_connection()
    if not conn:
        raise RuntimeError("Auth token DB connect failed")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {_AUTH_TABLE} WHERE GREATEST(expires_at, grace_until) <= %s",
                (now_ts,),
            )
            cur.execute(
                f"""INSERT INTO {_AUTH_TABLE}
                    (token, wallet_address, issued_at, expires_at, status, grace_until)
                    VALUES (%s, %s, %s, %s, 'current', %s)""",
                (token, wallet_norm, now_ts, now_ts + _AUTH_TOKEN_TTL, now_ts + _AUTH_TOKEN_TTL),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning("Postgres auth token insert failed: %s", e)
        raise RuntimeError("Auth token DB write failed") from e
    finally:
        conn.close()
    return token, _AUTH_TOKEN_TTL


def _is_gate_auth_token_valid(token: str, wallet_address: str) -> bool:
    if not token:
        return False
    now_ts = time.time()
    wallet_norm = (wallet_address or "").strip().lower()
    if not wallet_norm:
        return False
    if not _gate_pg_init_once():
        return False
    conn = _gate_pg_get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT status, expires_at, grace_until FROM {_AUTH_TABLE}
                    WHERE token = %s AND wallet_address = %s""",
                (token, wallet_norm),
            )
            row = cur.fetchone()
            if not row:
                return False
            status, expires_at, grace_until = row
            if status == "current":
                return now_ts < expires_at
            if status == "grace":
                return now_ts < grace_until
            return False
    except Exception as e:
        logger.warning("Postgres auth token validation failed: %s", e)
        return False
    finally:
        conn.close()

@app.after_request
def after_request(response):
    """Add CORS headers to all responses."""
    origin = cors_origin_for_request(
        request.headers.get("Origin"),
        request.headers.get("Host"),
        _allow_any,
        _allowlist,
    )
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Wallet-Address, X-AXGT-Auth-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    # WebRTC status must never be cached (CDN/browser); stale JSON caused endless polling on wrong state.
    if request.path.startswith("/api/webrtc/status"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
    return response

@app.route('/api/auth/verify-wallet', methods=['POST', 'OPTIONS'])
def verify_wallet():
    """Verify wallet ownership (signed challenge) and AXGT access policy."""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        # Best-effort rate limiting (per client IP)
        if _rate_limiter is not None:
            client_ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown").split(",")[0].strip()
            if not _rate_limiter.allow(client_ip):
                return jsonify({"verified": False, "error": "Rate limit exceeded"}), 429

        data = request.get_json()
        if not data:
            return jsonify({'verified': False, 'error': 'No JSON data provided'}), 400
        
        wallet_address = data.get('wallet_address', '').strip()
        message = (data.get('message') or '').strip()
        signature_hex = (data.get('signature') or data.get('signature_hex') or '').strip()
        
        if not wallet_address:
            return jsonify({'verified': False, 'error': 'wallet_address is required'}), 400
        
        if not validate_wallet_address(wallet_address):
            return jsonify({
                'verified': False,
                'error': 'Invalid wallet address format. Must be 0x followed by 40 hex characters.'
            }), 400
        
        if not message:
            return jsonify({'verified': False, 'error': 'message is required'}), 400
        if not signature_hex:
            return jsonify({'verified': False, 'error': 'signature is required'}), 400

        if not verify_signed_challenge(wallet_address, message, signature_hex):
            logger.warning("Sign-to-verify failed for %s", mask_wallet_address(wallet_address))
            return jsonify({'verified': False, 'error': 'Wallet signature verification failed.'}), 401

        status = get_wallet_access_status(wallet_address, consume_usage=False)
        status["wallet_address"] = wallet_address
        # Auth token = wallet ownership (for verify-deposit, session APIs). Desktop/VNC still requires prepaid minutes.
        try:
            token, ttl = _issue_gate_auth_token(wallet_address)
            status["auth_token"] = token
            status["auth_token_expires_in_seconds"] = ttl
        except Exception as ex:
            logger.warning("Auth token issue failed for %s: %s", mask_wallet_address(wallet_address), ex)
            return jsonify(
                {
                    "verified": False,
                    "error": "Auth database unavailable; cannot complete sign-in.",
                    "wallet_address": wallet_address,
                }
            ), 503
        if status.get("verified"):
            logger.info("Wallet verified (prepaid): %s", mask_wallet_address(wallet_address))
            return jsonify(status)
        logger.info("Wallet signed in, no prepaid credit: %s", mask_wallet_address(wallet_address))
        denied = dict(status)
        denied["verified"] = False
        denied["error"] = status.get("reason") or "No access available for this wallet"
        return jsonify(denied)
    except Exception as e:
        logger.error(f"Error in verify_wallet: {e}", exc_info=True)
        return jsonify({'verified': False, 'error': 'Internal server error'}), 500

@app.route('/api/auth/challenge', methods=['GET', 'OPTIONS'])
def auth_challenge():
    if request.method == 'OPTIONS':
        return '', 200
    wallet_address = (request.args.get('wallet_address') or request.headers.get('X-Wallet-Address') or '').strip()
    if not wallet_address:
        return jsonify({'error': 'wallet_address is required'}), 400
    if not validate_wallet_address(wallet_address):
        return jsonify({'error': 'Invalid wallet address format.'}), 400
    try:
        return jsonify({
            'challenge': get_challenge_message(wallet_address),
            'challenge_expires_in_seconds': get_challenge_ttl_seconds(),
        })
    except ValueError:
        return jsonify({'error': 'wallet_address is invalid'}), 400


@app.route('/api/auth/wallet-status', methods=['GET', 'OPTIONS'])
def wallet_status():
    if request.method == 'OPTIONS':
        return '', 200
    wallet_address = (request.args.get('wallet_address') or request.headers.get('X-Wallet-Address') or '').strip()
    if not wallet_address:
        return jsonify({'verified': False, 'error': 'wallet_address is required'}), 400
    if not validate_wallet_address(wallet_address):
        return jsonify({'verified': False, 'error': 'Invalid wallet address format.'}), 400
    status = get_wallet_access_status(wallet_address, consume_usage=False)
    status['wallet_address'] = wallet_address
    if not status.get('verified'):
        status['error'] = status.get('reason') or 'Access denied for this wallet.'
    return jsonify(status)


@app.route('/api/auth/verify-deposit', methods=['POST', 'OPTIONS'])
def api_verify_deposit():
    """Verify AXGT deposit by tx hash. Requires authenticated wallet (auth token)."""
    if request.method == 'OPTIONS':
        return '', 200
    if verify_deposit is None:
        return jsonify({"verified": False, "error": "Deposit verification unavailable"}), 503
    data = request.get_json()
    if not data:
        return jsonify({"verified": False, "error": "JSON body required"}), 400
    wallet_address = (data.get("wallet_address") or "").strip()
    tx_hash = (data.get("tx_hash") or "").strip()
    if not wallet_address or not validate_wallet_address(wallet_address):
        return jsonify({"verified": False, "error": "Valid wallet_address required"}), 400
    if not tx_hash:
        return jsonify({"verified": False, "error": "tx_hash required"}), 400
    auth_err = _require_auth_token(wallet_address)
    if auth_err:
        return auth_err
    result = verify_deposit(authenticated_wallet=wallet_address, tx_hash=tx_hash)
    if result.get("verified") or verify_deposit_is_pending(result):
        try:
            token, ttl = _issue_gate_auth_token(wallet_address)
            result = dict(result)
            result["auth_token"] = token
            result["auth_token_expires_in_seconds"] = ttl
        except Exception as ex:
            logger.warning("Auth token refresh failed for %s: %s", mask_wallet_address(wallet_address), ex)
        return jsonify(result)
    return jsonify(result), 400


@app.route('/api/config', methods=['GET'])
def api_config():
    policy = get_credit_policy()
    _dec_raw = (os.getenv("AXGT_TOKEN_DECIMALS") or "").strip()
    try:
        _td = int(_dec_raw) if _dec_raw else 18
        axgt_token_decimals = max(0, min(255, _td))
    except ValueError:
        axgt_token_decimals = 18

    _cost_raw = (os.getenv("AXGT_PERSISTENT_STORAGE_GB_HOUR_COST_MINUTES") or "").strip()
    try:
        storage_cost = float(_cost_raw) if _cost_raw else 0.05
    except ValueError:
        storage_cost = 0.05

    _limit_raw = (os.getenv("AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES") or "").strip()
    try:
        min_balance_limit = float(_limit_raw) if _limit_raw else -1440.0
    except ValueError:
        min_balance_limit = -1440.0

    return jsonify({
        'axgt_contract_address': (os.getenv("AXGT_CONTRACT_ADDRESS") or "").strip() or None,
        'axgt_chain_id': (os.getenv("AXGT_CHAIN_ID") or "").strip() or None,
        'axgt_revenue_wallet': (os.getenv("AXGT_REVENUE_WALLET") or "").strip() or None,
        'axgt_token_decimals': axgt_token_decimals,
        'axgt_min_deposit': policy.get("min_deposit"),
        'axgt_credit_per_100_axgt_minutes': policy.get("credit_per_100_axgt_minutes"),
        'eth_deposits_enabled': policy.get("eth_deposits_enabled"),
        'eth_min_deposit': policy.get("eth_min_deposit"),
        'eth_credit_per_eth_minutes': policy.get("eth_credit_per_eth_minutes"),
        'axgt_warning_threshold_minutes': policy.get("warning_threshold_minutes"),
        'min_axgt_deposit_minutes': policy.get("min_axgt_deposit_minutes"),
        'min_eth_deposit_minutes': policy.get("min_eth_deposit_minutes"),
        'axgt_direct_deposits_enabled': policy.get("axgt_direct_deposits_enabled"),
        'axgt_discount_tiers': policy.get("axgt_discount_tiers", []),
        'multi_session_enabled': (os.getenv("AXGT_MULTI_SESSION_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")),
        'gpu_profiles_enabled': (os.getenv("AXGT_GPU_PROFILES_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")),
        'gpu_profiles': {'small': 1, 'medium': 2, 'large': 4, 'max': 8},
        'gpu_weighted_billing_enabled': policy.get("gpu_weighted_billing_enabled", False),
        'persistent_storage_enabled': (os.getenv("AXGT_PERSISTENT_STORAGE_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")),
        'persistent_storage_gb_hour_cost_minutes': storage_cost,
        'persistent_storage_min_balance_limit_minutes': min_balance_limit,
        **(
            webrtc_config.public_config()
            if webrtc_config is not None
            else {
                "webrtc_enabled": False,
                "webrtc_fallback_enabled": True,
            }
        ),
    })


@app.route('/api/discount/quote', methods=['GET', 'OPTIONS'])
def api_discount_quote():
    """Return the discount tier + final ETH price for a wallet's on-chain AXGT balance.

    Query params:
        wallet_address (required): 0x... wallet to quote.
        base_eth (optional): override the ETH price to discount; defaults to ETH_MIN_DEPOSIT.

    The backend always re-fetches the AXGT balance on-chain (no client trust)
    and resolves the tier from the server-side configuration. On RPC failure
    the response sets balance_check_ok=False and falls back to no discount —
    callers should display the result and let the user retry.
    """
    if request.method == 'OPTIONS':
        return '', 200
    wallet_address = (request.args.get('wallet_address') or request.headers.get('X-Wallet-Address') or '').strip()
    if not wallet_address:
        return jsonify({'ok': False, 'error': 'wallet_address is required'}), 400
    if not validate_wallet_address(wallet_address):
        return jsonify({'ok': False, 'error': 'Invalid wallet address format.'}), 400
    try:
        try:
            from . import discount as _disc
        except ImportError:
            try:
                from axonos_gate import discount as _disc
            except ImportError:
                import discount as _disc  # type: ignore[no-redef]
    except ImportError:
        return jsonify({'ok': False, 'error': 'Discount module unavailable'}), 503

    from decimal import Decimal as _D, InvalidOperation as _IO
    base_eth_raw = (request.args.get('base_eth') or '').strip()
    policy = get_credit_policy()
    if base_eth_raw:
        try:
            base_eth = _D(base_eth_raw)
            if base_eth <= 0:
                raise _IO("base_eth must be positive")
        except (_IO, ValueError):
            return jsonify({'ok': False, 'error': 'Invalid base_eth value'}), 400
    else:
        try:
            base_eth = _D(str(policy.get('eth_min_deposit') or '0.0005'))
        except (_IO, ValueError):
            base_eth = _D('0.0005')

    bal = _disc.fetch_axgt_balance(wallet_address)
    floor_axgt = bal.floor_axgt() if bal.ok else 0
    tier = _disc.resolve_tier(floor_axgt) if bal.ok else _disc.resolve_tier(0)
    discount_pct_for_quote = tier.discount_percent if bal.ok else 0.0
    final_eth = _disc.apply_discount(base_eth, discount_pct_for_quote)
    eth_rate = _D(str(policy.get('eth_credit_per_eth_minutes') or 120000))
    if discount_pct_for_quote >= 100:
        effective_rate = eth_rate
    else:
        effective_rate = eth_rate / (_D('1') - _D(str(discount_pct_for_quote)) / _D('100'))
    estimated_minutes = float(base_eth * effective_rate)
    return jsonify({
        'ok': True,
        'wallet_address': wallet_address,
        'base_eth': format(base_eth.normalize(), 'f') if base_eth > 0 else '0',
        'final_eth': format(final_eth.normalize(), 'f') if final_eth > 0 else '0',
        'discount_percent': discount_pct_for_quote,
        'tier_index': tier.index,
        'tier_label': tier.label,
        'tier_min_axgt': tier.min_axgt,
        'axgt_balance': format(bal.balance_axgt.normalize(), 'f') if bal.ok and bal.balance_axgt > 0 else '0',
        'axgt_balance_floor': floor_axgt,
        'balance_check_ok': bal.ok,
        'balance_check_error': bal.error if not bal.ok else None,
        'tiers': _disc.public_tiers(),
        'estimated_minutes': round(estimated_minutes, 2),
        'eth_credit_per_eth_minutes': float(policy.get('eth_credit_per_eth_minutes') or 120000),
    })


def _require_admin():
    """Require AXGT_ADMIN_SECRET. Returns None on success, or (response, status)."""
    secret = (os.getenv("AXGT_ADMIN_SECRET") or "").strip()
    if not secret:
        return jsonify({"error": "Admin API disabled"}), 503
    provided = (request.headers.get("X-AXGT-Admin-Secret") or request.args.get("admin_secret") or "").strip()
    if provided != secret:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route('/api/admin/credit-minutes', methods=['POST', 'OPTIONS'])
def api_admin_credit_minutes():
    if request.method == 'OPTIONS':
        return '', 200
    err = _require_admin()
    if err:
        return err
    try:
        from axonos_gate import deposit_ledger
    except ImportError:
        import deposit_ledger
    data = request.get_json() or {}
    wallet = (data.get("wallet_address") or "").strip()
    minutes = data.get("minutes")
    notes = (data.get("notes") or "").strip() or None
    if not wallet or not validate_wallet_address(wallet):
        return jsonify({"ok": False, "error": "Valid wallet_address required"}), 400
    try:
        minutes = float(minutes)
        if minutes <= 0:
            raise ValueError("minutes must be positive")
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Positive minutes required"}), 400
    ok, remaining, error = deposit_ledger.credit_wallet_minutes(wallet, minutes, notes=notes, created_by="admin_api")
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "remaining_minutes": remaining})


@app.route('/api/admin/refund-minutes', methods=['POST', 'OPTIONS'])
def api_admin_refund_minutes():
    if request.method == 'OPTIONS':
        return '', 200
    err = _require_admin()
    if err:
        return err
    try:
        from axonos_gate import deposit_ledger
    except ImportError:
        import deposit_ledger
    data = request.get_json() or {}
    wallet = (data.get("wallet_address") or "").strip()
    minutes = data.get("minutes")
    notes = (data.get("notes") or "").strip() or None
    if not wallet or not validate_wallet_address(wallet):
        return jsonify({"ok": False, "error": "Valid wallet_address required"}), 400
    try:
        minutes = float(minutes)
        if minutes <= 0:
            raise ValueError("minutes must be positive")
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Positive minutes required"}), 400
    ok, remaining, error = deposit_ledger.refund_wallet_minutes(wallet, minutes, notes=notes, created_by="admin_api")
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "remaining_minutes": remaining})


@app.route('/api/admin/adjust-balance', methods=['POST', 'OPTIONS'])
def api_admin_adjust_balance():
    if request.method == 'OPTIONS':
        return '', 200
    err = _require_admin()
    if err:
        return err
    try:
        from axonos_gate import deposit_ledger
    except ImportError:
        import deposit_ledger
    data = request.get_json() or {}
    wallet = (data.get("wallet_address") or "").strip()
    minutes_delta = data.get("minutes_delta")
    notes = (data.get("notes") or "").strip() or None
    if not wallet or not validate_wallet_address(wallet):
        return jsonify({"ok": False, "error": "Valid wallet_address required"}), 400
    try:
        minutes_delta = float(minutes_delta)
        if minutes_delta == 0:
            raise ValueError("minutes_delta must be non-zero")
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Non-zero minutes_delta required"}), 400
    ok, remaining, error = deposit_ledger.adjust_wallet_balance(wallet, minutes_delta, notes=notes, created_by="admin_api")
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "remaining_minutes": remaining})


@app.route('/api/admin/ledger', methods=['GET', 'OPTIONS'])
def api_admin_ledger():
    if request.method == 'OPTIONS':
        return '', 200
    err = _require_admin()
    if err:
        return err
    try:
        from axonos_gate import deposit_ledger
    except ImportError:
        import deposit_ledger
    wallet = (request.args.get("wallet_address") or "").strip()
    if not wallet or not validate_wallet_address(wallet):
        return jsonify({"error": "Valid wallet_address required"}), 400
    limit = request.args.get("limit", "100")
    try:
        limit = min(500, max(1, int(limit)))
    except ValueError:
        limit = 100
    entries = deposit_ledger.get_wallet_ledger(wallet, limit=limit)
    return jsonify({"wallet_address": wallet, "ledger": entries})


# ---------------------------------------------------------------------------
# Telemetry admin endpoints
# ---------------------------------------------------------------------------

def _telemetry_pg_query(query, params=None):
    conn = _gate_pg_get_connection()
    if not conn:
        return None, "Database unavailable"
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return rows, None
    except Exception as exc:
        logger.warning("Telemetry query failed: %s", exc)
        return None, str(exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.route('/api/admin/telemetry/summary', methods=['GET', 'OPTIONS'])
def api_admin_telemetry_summary():
    if request.method == 'OPTIONS':
        return '', 200
    err = _require_admin()
    if err:
        return err
    rows, e = _telemetry_pg_query("""
        SELECT
            COUNT(*) AS total_sessions,
            COUNT(DISTINCT wallet_address) AS unique_wallets,
            COUNT(CASE WHEN allocation_status='failed' THEN 1 END) AS failed_allocations,
            COUNT(CASE WHEN status='active' THEN 1 END) AS active_sessions,
            COALESCE(ROUND(SUM(last_heartbeat - started_at)::numeric / 60, 2), 0) AS total_wall_minutes,
            COALESCE(MIN(started_at), 0) AS first_session_ts,
            COALESCE(MAX(started_at), 0) AS last_session_ts
        FROM axgt_sessions
    """)
    if e:
        return jsonify({"error": e}), 500
    s = rows[0]
    dep, _ = _telemetry_pg_query("""
        SELECT
            COALESCE(SUM(credited_minutes_total), 0) AS total_credited,
            COALESCE(SUM(consumed_minutes_total), 0) AS total_consumed,
            COALESCE(SUM(remaining_minutes), 0) AS total_remaining
        FROM axgt_deposits
    """)
    d = (dep or [{}])[0]
    wr, _ = _telemetry_pg_query("""
        SELECT
            COUNT(*) AS total,
            COUNT(CASE WHEN state='closed' THEN 1 END) AS closed,
            COUNT(CASE WHEN state='failed' THEN 1 END) AS failed,
            COUNT(CASE WHEN answer_sdp IS NOT NULL THEN 1 END) AS answered
        FROM axgt_webrtc_signaling
    """)
    w = (wr or [{}])[0]
    return jsonify({
        "sessions": {
            "total": int(s.get("total_sessions") or 0),
            "unique_wallets": int(s.get("unique_wallets") or 0),
            "failed_allocations": int(s.get("failed_allocations") or 0),
            "active": int(s.get("active_sessions") or 0),
            "total_wall_minutes": float(s.get("total_wall_minutes") or 0),
            "first_session_ts": float(s.get("first_session_ts") or 0),
            "last_session_ts": float(s.get("last_session_ts") or 0),
        },
        "deposits": {
            "total_credited_minutes": float(d.get("total_credited") or 0),
            "total_consumed_minutes": float(d.get("total_consumed") or 0),
            "total_remaining_minutes": float(d.get("total_remaining") or 0),
        },
        "webrtc": {
            "total": int(w.get("total") or 0),
            "closed": int(w.get("closed") or 0),
            "failed": int(w.get("failed") or 0),
            "answered": int(w.get("answered") or 0),
        },
    })


@app.route('/api/admin/telemetry/sessions', methods=['GET', 'OPTIONS'])
def api_admin_telemetry_sessions():
    if request.method == 'OPTIONS':
        return '', 200
    err = _require_admin()
    if err:
        return err
    limit = min(1000, max(1, int(request.args.get("limit", "300") or "300")))
    rows, e = _telemetry_pg_query("""
        SELECT
            id, wallet_address, requested_profile, gpu_ids,
            container_id, allocation_status, status,
            started_at, last_heartbeat, expires_at,
            ROUND(((last_heartbeat - started_at) / 60)::numeric, 2) AS duration_minutes
        FROM axgt_sessions
        ORDER BY started_at DESC
        LIMIT %s
    """, (limit,))
    if e:
        return jsonify({"error": e}), 500
    for r in rows:
        r["id"] = int(r["id"])
        r["duration_minutes"] = float(r.get("duration_minutes") or 0)
        for k in ("started_at", "last_heartbeat", "expires_at"):
            r[k] = float(r.get(k) or 0)
    return jsonify({"sessions": rows, "count": len(rows)})


@app.route('/api/admin/telemetry/wallets', methods=['GET', 'OPTIONS'])
def api_admin_telemetry_wallets():
    if request.method == 'OPTIONS':
        return '', 200
    err = _require_admin()
    if err:
        return err
    rows, e = _telemetry_pg_query("""
        SELECT
            s.wallet_address,
            COUNT(*) AS total_sessions,
            COUNT(CASE WHEN s.allocation_status='failed' THEN 1 END) AS failed_sessions,
            COUNT(CASE WHEN s.requested_profile='max' THEN 1 END) AS max_sessions,
            COUNT(CASE WHEN s.requested_profile='small' THEN 1 END) AS small_sessions,
            ROUND(SUM((s.last_heartbeat - s.started_at) / 60)::numeric, 2) AS total_wall_minutes,
            COALESCE(MIN(s.started_at), 0) AS first_session_ts,
            COALESCE(MAX(s.started_at), 0) AS last_session_ts,
            COALESCE(d.credited_minutes_total, 0) AS credited_minutes,
            COALESCE(d.consumed_minutes_total, 0) AS consumed_minutes,
            COALESCE(d.remaining_minutes, 0) AS remaining_minutes
        FROM axgt_sessions s
        LEFT JOIN axgt_deposits d ON d.wallet_address = s.wallet_address
        GROUP BY s.wallet_address, d.credited_minutes_total, d.consumed_minutes_total, d.remaining_minutes
        ORDER BY total_sessions DESC
    """)
    if e:
        return jsonify({"error": e}), 500
    for r in rows:
        for k in ("first_session_ts", "last_session_ts"):
            r[k] = float(r.get(k) or 0)
        for k in ("total_wall_minutes", "credited_minutes", "consumed_minutes", "remaining_minutes"):
            r[k] = float(r.get(k) or 0)
        for k in ("total_sessions", "failed_sessions", "max_sessions", "small_sessions"):
            r[k] = int(r.get(k) or 0)
    return jsonify({"wallets": rows})


@app.route('/api/admin/telemetry/events', methods=['GET', 'OPTIONS'])
def api_admin_telemetry_events():
    if request.method == 'OPTIONS':
        return '', 200
    err = _require_admin()
    if err:
        return err
    limit = min(500, max(1, int(request.args.get("limit", "100") or "100")))
    rows, e = _telemetry_pg_query("""
        SELECT
            id, wallet_address, event_type,
            ROUND(minutes_delta::numeric, 4) AS minutes_delta,
            ROUND(balance_after_minutes::numeric, 2) AS balance_after_minutes,
            reference_tx_hash, reference_session_id, notes, created_at, created_by
        FROM axgt_ledger
        ORDER BY id DESC
        LIMIT %s
    """, (limit,))
    if e:
        return jsonify({"error": e}), 500
    for r in rows:
        r["id"] = int(r["id"])
        r["created_at"] = float(r.get("created_at") or 0)
        for k in ("minutes_delta", "balance_after_minutes"):
            r[k] = float(r.get(k) or 0)
    return jsonify({"events": rows, "count": len(rows)})


@app.route('/api/admin/telemetry/webrtc', methods=['GET', 'OPTIONS'])
def api_admin_telemetry_webrtc():
    if request.method == 'OPTIONS':
        return '', 200
    err = _require_admin()
    if err:
        return err
    rows, e = _telemetry_pg_query("""
        SELECT
            wallet_address, state,
            offer_sdp IS NOT NULL AS has_offer,
            answer_sdp IS NOT NULL AS has_answer,
            last_error, created_at, updated_at,
            ROUND((updated_at - created_at)::numeric, 1) AS duration_seconds
        FROM axgt_webrtc_signaling
        ORDER BY created_at DESC
        LIMIT 500
    """)
    if e:
        return jsonify({"error": e}), 500
    for r in rows:
        for k in ("created_at", "updated_at"):
            r[k] = float(r.get(k) or 0)
        r["duration_seconds"] = float(r.get("duration_seconds") or 0)
    brows, _ = _telemetry_pg_query("""
        SELECT state, COUNT(*) AS count FROM axgt_webrtc_signaling GROUP BY state
    """)
    breakdown = {r["state"]: int(r["count"]) for r in (brows or [])}
    return jsonify({"sessions": rows, "count": len(rows), "breakdown": breakdown})


def _require_auth_token(wallet_address: str):
    """Validate auth token from cookie / header / query. Returns None on success, or (response, status)."""
    from flask import request as _req
    wallet_norm = (wallet_address or "").strip().lower()
    if not wallet_norm:
        return jsonify({"error": "Valid wallet_address required"}), 400
    token = None
    cookie_val = _req.cookies.get(os.getenv("AXGT_AUTH_COOKIE_NAME", "axgt_auth_token").strip())
    if cookie_val:
        token = cookie_val.strip()
    if not token:
        token = (_req.headers.get("X-AXGT-Auth-Token") or "").strip() or None
    if not token:
        token = (_req.args.get("auth_token") or "").strip() or None
    if not token or not _is_gate_auth_token_valid(token, wallet_norm):
        return jsonify({"error": "Valid auth token required"}), 401
    return None


@app.route('/api/session/status', methods=['GET', 'OPTIONS'])
def api_session_status():
    if request.method == 'OPTIONS':
        return '', 200
    if not _session_mgr_available:
        return jsonify({"error": "Session manager unavailable"}), 503
    wallet_address = (request.args.get('wallet_address') or request.headers.get('X-Wallet-Address') or '').strip() or None
    return jsonify(session_status(wallet_address))


@app.route('/api/session/claim', methods=['POST', 'OPTIONS'])
def api_session_claim():
    if request.method == 'OPTIONS':
        return '', 200
    if not _session_mgr_available:
        return jsonify({"granted": False, "error": "Session manager unavailable"}), 503
    data = request.get_json() or {}
    wallet_address = (data.get('wallet_address') or '').strip()
    requested_profile = (data.get('requested_profile') or '').strip() or None
    requested_template = (data.get('requested_template') or '').strip() or None
    if not wallet_address or not validate_wallet_address(wallet_address):
        return jsonify({"granted": False, "error": "Valid wallet_address required"}), 400
    auth_err = _require_auth_token(wallet_address)
    if auth_err:
        return auth_err
    return jsonify(try_claim_session(wallet_address, requested_profile=requested_profile, requested_template=requested_template))


@app.route('/api/session/heartbeat', methods=['POST', 'OPTIONS'])
def api_session_heartbeat():
    if request.method == 'OPTIONS':
        return '', 200
    if not _session_mgr_available:
        return jsonify({"ok": False, "error": "Session manager unavailable"}), 503
    data = request.get_json() or {}
    wallet_address = (data.get('wallet_address') or '').strip()
    if not wallet_address or not validate_wallet_address(wallet_address):
        return jsonify({"ok": False, "error": "Valid wallet_address required"}), 400
    auth_err = _require_auth_token(wallet_address)
    if auth_err:
        return auth_err
    return jsonify(session_heartbeat(wallet_address))


@app.route('/api/session/release', methods=['POST', 'OPTIONS'])
def api_session_release():
    if request.method == 'OPTIONS':
        return '', 200
    if not _session_mgr_available:
        return jsonify({"released": False, "error": "Session manager unavailable"}), 503
    data = request.get_json() or {}
    wallet_address = (data.get('wallet_address') or '').strip()
    if not wallet_address or not validate_wallet_address(wallet_address):
        return jsonify({"released": False, "error": "Valid wallet_address required"}), 400
    auth_err = _require_auth_token(wallet_address)
    if auth_err:
        return auth_err
    return jsonify(release_session(wallet_address))


@app.route('/api/session/restart', methods=['POST', 'OPTIONS'])
def api_session_restart():
    if request.method == 'OPTIONS':
        return '', 200
    if not _session_mgr_available:
        return jsonify({"restarted": False, "error": "Session manager unavailable"}), 503
    data = request.get_json() or {}
    wallet_address = (data.get('wallet_address') or '').strip()
    if not wallet_address or not validate_wallet_address(wallet_address):
        return jsonify({"restarted": False, "error": "Valid wallet_address required"}), 400
    auth_err = _require_auth_token(wallet_address)
    if auth_err:
        return auth_err
    return jsonify(restart_desktop_session(wallet_address))


def _webrtc_sig_allow(wallet_key: str) -> bool:
    """Rate-limit WebRTC signaling per client IP + wallet bucket."""
    global _webrtc_sig_limiter
    if webrtc_config is None:
        return True
    n = webrtc_config.rate_limit_per_minute()
    if n <= 0:
        return True
    if _webrtc_sig_limiter is None:
        _webrtc_sig_limiter = SimpleRateLimiter(limit=n, window_seconds=60)
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown").split(",")[0].strip()
    return _webrtc_sig_limiter.allow(f"{ip}|{wallet_key or '_'}")


@app.route('/api/webrtc/config', methods=['GET', 'OPTIONS'])
def api_webrtc_config():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None:
        return jsonify({"ok": False, "error": "WebRTC module unavailable"}), 503
    st, payload = webrtc_service.handle_config_public()
    return jsonify(payload), st


@app.route('/api/webrtc/session', methods=['POST', 'OPTIONS'])
def api_webrtc_session():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None or webrtc_config is None:
        return jsonify({"ok": False, "error": "WebRTC module unavailable"}), 503
    data = request.get_json() or {}
    wallet = (data.get("wallet_address") or "").strip()
    if not wallet or not validate_wallet_address(wallet):
        return jsonify({"ok": False, "error": "Valid wallet_address required"}), 400
    auth_err = _require_auth_token(wallet)
    if auth_err:
        return auth_err
    wn = wallet.lower()
    if not _webrtc_sig_allow(wn):
        return jsonify({"ok": False, "error": "Rate limit exceeded"}), 429
    owner = _session_mgr_available and is_session_owner(wn)
    st, payload = webrtc_service.handle_create_session(wn, True, owner)
    return jsonify(payload), st


@app.route('/api/webrtc/offer', methods=['POST', 'OPTIONS'])
def api_webrtc_offer():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None:
        return jsonify({"ok": False, "error": "WebRTC module unavailable"}), 503
    data = request.get_json() or {}
    wallet = (data.get("wallet_address") or "").strip()
    sid = (data.get("session_id") or "").strip()
    if not wallet or not validate_wallet_address(wallet):
        return jsonify({"ok": False, "error": "Valid wallet_address required"}), 400
    auth_err = _require_auth_token(wallet)
    if auth_err:
        return auth_err
    wn = wallet.lower()
    if not _webrtc_sig_allow(wn):
        return jsonify({"ok": False, "error": "Rate limit exceeded"}), 429
    owner = _session_mgr_available and is_session_owner(wn)
    tok_ok = True
    st, payload = webrtc_service.handle_post_offer(sid, wn, tok_ok, owner, data)
    return jsonify(payload), st


@app.route('/api/webrtc/status', methods=['GET', 'OPTIONS'])
def api_webrtc_status():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None:
        return jsonify({"ok": False, "error": "WebRTC module unavailable"}), 503
    sid = (request.args.get("session_id") or "").strip()
    wallet = (request.args.get("wallet_address") or request.headers.get("X-Wallet-Address") or "").strip()
    if not sid or not wallet or not validate_wallet_address(wallet):
        return jsonify({"ok": False, "error": "session_id and wallet_address required"}), 400
    auth_err = _require_auth_token(wallet)
    if auth_err:
        return auth_err
    wn = wallet.lower()
    tok_ok = True
    st, payload = webrtc_service.handle_get_status(sid, wn, tok_ok)
    return jsonify(payload), st


@app.route('/api/webrtc/ice', methods=['POST', 'OPTIONS'])
def api_webrtc_ice():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None:
        return jsonify({"ok": False, "error": "WebRTC module unavailable"}), 503
    data = request.get_json()
    wallet = ""
    if isinstance(data, dict):
        wallet = (data.get("wallet_address") or "").strip()
    if not wallet or not validate_wallet_address(wallet):
        return jsonify({"ok": False, "error": "Valid wallet_address required"}), 400
    auth_err = _require_auth_token(wallet)
    if auth_err:
        return auth_err
    sid = ""
    if isinstance(data, dict):
        sid = (data.get("session_id") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "session_id required"}), 400
    wn = wallet.lower()
    if not _webrtc_sig_allow(wn):
        return jsonify({"ok": False, "error": "Rate limit exceeded"}), 429
    st, payload = webrtc_service.handle_post_client_ice(sid, wn, True, data)
    return jsonify(payload), st


@app.route('/api/webrtc/metrics', methods=['POST', 'OPTIONS'])
def api_webrtc_metrics():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None:
        return jsonify({"ok": False, "error": "WebRTC module unavailable"}), 503
    data = request.get_json() or {}
    wallet = (data.get("wallet_address") or "").strip()
    sid = (data.get("session_id") or "").strip()
    if not wallet or not validate_wallet_address(wallet) or not sid:
        return jsonify({"ok": False, "error": "wallet_address and session_id required"}), 400
    auth_err = _require_auth_token(wallet)
    if auth_err:
        return auth_err
    st, payload = webrtc_service.handle_post_client_metrics(sid, wallet.lower(), True, data)
    return jsonify(payload), st


@app.route('/api/webrtc/close', methods=['POST', 'OPTIONS'])
def api_webrtc_close():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None:
        return jsonify({"ok": False, "error": "WebRTC module unavailable"}), 503
    data = request.get_json() or {}
    wallet = (data.get("wallet_address") or "").strip()
    sid = (data.get("session_id") or "").strip()
    if not wallet or not validate_wallet_address(wallet) or not sid:
        return jsonify({"ok": False, "error": "wallet_address and session_id required"}), 400
    auth_err = _require_auth_token(wallet)
    if auth_err:
        return auth_err
    st, payload = webrtc_service.handle_close(sid, wallet.lower(), True)
    return jsonify(payload), st


@app.route('/api/webrtc/agent/next', methods=['GET', 'OPTIONS'])
def api_webrtc_agent_next():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None:
        return '', 503
    key = (request.headers.get("X-AxonOS-WebRTC-Agent-Key") or "").strip()
    st, payload = webrtc_service.handle_agent_next(key)
    if st == 204:
        return '', 204
    return jsonify(payload), st


@app.route('/api/webrtc/agent/row', methods=['GET', 'OPTIONS'])
def api_webrtc_agent_row():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None:
        return jsonify({"ok": False}), 503
    key = (request.headers.get("X-AxonOS-WebRTC-Agent-Key") or "").strip()
    sid = (request.args.get("session_id") or "").strip()
    st, payload = webrtc_service.handle_agent_row(key, sid)
    return jsonify(payload), st


@app.route('/api/webrtc/agent/answer', methods=['POST', 'OPTIONS'])
def api_webrtc_agent_answer():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None:
        return jsonify({"ok": False, "error": "WebRTC module unavailable"}), 503
    key = (request.headers.get("X-AxonOS-WebRTC-Agent-Key") or "").strip()
    data = request.get_json() or {}
    st, payload = webrtc_service.handle_agent_answer(key, data)
    return jsonify(payload), st


@app.route('/api/webrtc/agent/fail', methods=['POST', 'OPTIONS'])
def api_webrtc_agent_fail():
    if request.method == 'OPTIONS':
        return '', 200
    if webrtc_service is None:
        return jsonify({"ok": False}), 503
    key = (request.headers.get("X-AxonOS-WebRTC-Agent-Key") or "").strip()
    data = request.get_json() or {}
    st, payload = webrtc_service.handle_agent_fail(key, data)
    return jsonify(payload), st


@app.route('/')
def index():
    """Serve the main noVNC HTML page."""
    return send_from_directory(str(NOVNC_WEB_DIR), 'vnc.html')

@app.route('/<path:path>')
def serve_static(path):
    """Serve static files from noVNC directory."""
    return send_from_directory(str(NOVNC_WEB_DIR), path)


def _extract_wallet_and_token_from_environ(environ):
    query = environ.get('QUERY_STRING', '')
    qs = parse_qs(query)
    wallet = (qs.get('wallet') or [None])[0] if qs else None
    token = (qs.get('auth_token') or [None])[0] if qs else None
    if wallet:
        wallet = wallet.strip()
    if token:
        token = token.strip()
    return wallet, token


def _extract_auth_cookie_from_environ(environ) -> str | None:
    """Best-effort parse auth token from Cookie header for WebSocket upgrades."""
    cookie_header = environ.get("HTTP_COOKIE") or ""
    if not cookie_header:
        return None
    cookie_name = (os.getenv("AXGT_AUTH_COOKIE_NAME", "axgt_auth_token") or "").strip()
    if not cookie_name:
        cookie_name = "axgt_auth_token"
    # Minimal cookie parser: "a=b; c=d" → tokens
    parts = [p.strip() for p in cookie_header.split(";") if p.strip()]
    for p in parts:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        if k.strip() == cookie_name:
            val = v.strip()
            return val or None
    return None


def _handle_websockify_proxy(environ, start_response):
    """Handle /websockify WebSocket: validate wallet+token, proxy to websockify_gate on 6080."""
    ws = environ.get('wsgi.websocket')
    if not ws:
        start_response('400 Bad Request', [('Content-Type', 'text/plain')])
        return [b'WebSocket expected']

    wallet, auth_token = _extract_wallet_and_token_from_environ(environ)
    if not auth_token:
        # Prefer HttpOnly cookie when present (default production mode).
        auth_token = _extract_auth_cookie_from_environ(environ)
    if not wallet or not validate_wallet_address(wallet):
        try:
            ws.close(code=403, reason='Invalid or missing wallet')
        except Exception:
            pass
        return []

    wallet_norm = wallet.strip().lower()
    if not auth_token or not _is_gate_auth_token_valid(auth_token, wallet_norm):
        try:
            ws.close(code=403, reason='Invalid or expired auth token')
        except Exception:
            pass
        return []

    status = get_wallet_access_status(wallet_norm, consume_usage=False)
    if not status.get('verified'):
        try:
            ws.close(code=403, reason=status.get('reason') or 'Access denied')
        except Exception:
            pass
        return []

    if _session_mgr_available and not is_session_owner(wallet_norm):
        try:
            ws.close(code=403, reason='Session not owned by this wallet')
        except Exception:
            pass
        return []

    websockify_port = int(os.getenv('WEBSOCKIFY_PORT', '6080'))
    backend_url = f'ws://127.0.0.1:{websockify_port}/websockify?wallet={wallet_norm}'

    try:
        import gevent
        from websocket import create_connection
        backend = create_connection(backend_url)
    except Exception as e:
        logger.error("WebSocket proxy backend connect failed: %s", e, exc_info=True)
        try:
            ws.close(code=503, reason='Backend unavailable')
        except Exception:
            pass
        return []

    def client_to_backend():
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                if isinstance(msg, bytes):
                    backend.send(msg, opcode=2)
                else:
                    backend.send(msg, opcode=1)
        except Exception:
            pass
        try:
            backend.close()
        except Exception:
            pass

    def backend_to_client():
        try:
            while True:
                msg = backend.recv()
                if msg is None:
                    break
                ws.send(msg)
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass

    logger.info("WebSocket proxy started: %s", mask_wallet_address(wallet))
    gevent.spawn(client_to_backend)
    gevent.spawn(backend_to_client).get()
    return []


def _application(environ, start_response):
    """WSGI app: /websockify with Upgrade: websocket -> proxy; else Flask."""
    path = (environ.get('PATH_INFO') or '').strip()
    is_ws = (environ.get('HTTP_UPGRADE') or '').lower() == 'websocket'
    if path == '/websockify' and is_ws and environ.get('wsgi.websocket'):
        return _handle_websockify_proxy(environ, start_response)
    return app.wsgi_app(environ, start_response)


def _init_all_tables():
    """Eagerly create all DB tables at startup so the DB is ready before the first request."""
    try:
        from axonos_gate import deposit_ledger as _dl
    except ImportError:
        import deposit_ledger as _dl
    try:
        from axonos_gate import session_manager as _sm
    except ImportError:
        import session_manager as _sm
    try:
        from axonos_gate.webrtc import store as _ws
    except ImportError:
        try:
            from webrtc import store as _ws
        except ImportError:
            _ws = None

    # deposit_ledger tables (axgt_deposits, axgt_ledger, axgt_verified_deposits)
    try:
        conn = _dl._get_connection()
        if conn:
            _dl._ensure_tables(conn)
            conn.commit()
            conn.close()
            logger.info("DB init: deposit_ledger tables ready")
    except Exception as exc:
        logger.warning("DB init: deposit_ledger tables failed: %s", exc)

    # session_manager tables (axgt_sessions)
    try:
        conn = _sm._get_connection()
        if conn:
            _sm._ensure_tables(conn)
            conn.commit()
            conn.close()
            logger.info("DB init: session_manager tables ready")
    except Exception as exc:
        logger.warning("DB init: session_manager tables failed: %s", exc)

    # webrtc signaling table (axgt_webrtc_signaling)
    if _ws:
        try:
            _ws.ensure_table()
            logger.info("DB init: webrtc store table ready")
        except Exception as exc:
            logger.warning("DB init: webrtc store table failed: %s", exc)

    # auth tokens table (handled by _gate_pg_init_once, call it here too)
    _gate_pg_init_once()


def main():
    """Run the gate server (HTTP + WebSocket on same port)."""
    host = os.getenv('GATE_HOST', '127.0.0.1')
    port = int(os.getenv('GATE_PORT', '8889'))

    logger.info(f"Starting AxonOS AXGT Gate Server on {host}:{port}")
    logger.info(f"AXGT Contract: {(os.getenv('AXGT_CONTRACT_ADDRESS') or '<unset>').strip()}")
    logger.info(f"RPC URL: {(os.getenv('AXGT_RPC_URL') or '<unset>').strip()}")
    _init_all_tables()

    use_gevent = (os.getenv('GATE_USE_GEVENT', '1').strip().lower() in ('1', 'true', 'yes'))
    if use_gevent:
        try:
            from gevent import pywsgi
            from geventwebsocket.handler import WebSocketHandler
            logger.info("WebSocket /websockify enabled (proxy to websockify_gate on 127.0.0.1)")
            server = pywsgi.WSGIServer((host, port), _application, handler_class=WebSocketHandler)
            server.serve_forever()
        except ImportError as e:
            logger.warning("gevent/gevent-websocket not available (%s); running Flask only (no WebSocket)", e)
            app.run(host=host, port=port, debug=False, use_reloader=False)
    else:
        app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
