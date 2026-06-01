#!/usr/bin/env python3
"""
Websockify Gate Wrapper

Wraps websockify to add AXGT wallet verification and signed auth flow.
This script is called by supervisord instead of websockify directly.
"""

import os
import sys
import logging
import json
import time
import secrets
from http.cookies import SimpleCookie
from threading import Lock
from urllib.parse import parse_qs, urlparse

# Local security helpers (same directory)
from security_utils import (
    SimpleRateLimiter,
    cors_origin_for_request,
    get_rate_limiter_from_env,
    parse_cors_allowlist,
)

# Add system Python path for Ubuntu 22.04 packages (websockify) FIRST
if '/usr/lib/python3/dist-packages' not in sys.path:
    sys.path.insert(0, '/usr/lib/python3/dist-packages')

# Import websockify early
try:
    import websockify
    # Check if WebSocketProxy is available
    if not hasattr(websockify, 'WebSocketProxy'):
        # Try importing from websockifyserver
        from websockify import websockifyserver
        websockify.WebSocketProxy = websockifyserver.WebSocketProxy
except (ImportError, AttributeError) as e:
    # Can't use logger yet, print to stderr
    print(f"ERROR: websockify not available: {e}. Install with: apt install python3-websockify", file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Add parent directory to path for imports
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_allow_any, _allowlist = parse_cors_allowlist(os.getenv("AXGT_CORS_ORIGINS"))
_rate_limiter = get_rate_limiter_from_env()

_webrtc_sig_ws = None


def _webrtc_ws_rate_allow(wallet_key: str, headers, client_ip: str) -> bool:
    global _webrtc_sig_ws
    if webrtc_config is None:
        return True
    n = webrtc_config.rate_limit_per_minute()
    if n <= 0:
        return True
    if _webrtc_sig_ws is None:
        _webrtc_sig_ws = SimpleRateLimiter(limit=n, window_seconds=60)
    ip = (headers.get("X-Forwarded-For") or client_ip or "unknown").split(",")[0].strip()
    return _webrtc_sig_ws.allow(f"{ip}|{wallet_key or '_'}")


_auth_lock = Lock()
_AUTH_TABLE = "axgt_auth_tokens"
_auth_pg_init_done = False
_auth_pg_init_lock = Lock()


def _auth_db_url():
    """Reuse the same Postgres URL as the challenge registry."""
    return os.getenv("AXGT_CHALLENGE_DB_URL") or None


def _auth_pg_get_connection():
    url = _auth_db_url()
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as e:
        logger.warning("Postgres auth token DB connect failed: %s", e)
        return None


_gpu_cache = {"gpus": [], "ts": 0}
_gpu_cache_lock = Lock()

def _poll_gpus():
    import subprocess as _sp, time as _t
    while True:
        try:
            res = _sp.run(
                ["nvidia-smi",
                 "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=30
            )
            gpus = []
            for line in res.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 7:
                    continue
                def _f(v):
                    try: return float(v)
                    except: return None
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "utilization_pct": _f(parts[2]),
                    "memory_used_mb": _f(parts[3]),
                    "memory_total_mb": _f(parts[4]),
                    "temperature_c": _f(parts[5]),
                    "power_draw_w": _f(parts[6]),
                })
            with _gpu_cache_lock:
                _gpu_cache["gpus"] = gpus
                _gpu_cache["ts"] = _t.time()
        except Exception as exc:
            logger.warning("GPU poller: %s", exc)
        _t.sleep(10)

import threading as _threading
_threading.Thread(target=_poll_gpus, daemon=True).start()


def _telemetry_query(query, params=None):
    conn = _auth_pg_get_connection()
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


def _telemetry_summary():
    rows, e = _telemetry_query("""
        SELECT COUNT(*) AS total_sessions,
               COUNT(DISTINCT wallet_address) AS unique_wallets,
               COUNT(CASE WHEN allocation_status='failed' THEN 1 END) AS failed_allocations,
               COUNT(CASE WHEN status='active' THEN 1 END) AS active_sessions,
               COALESCE(ROUND(SUM(last_heartbeat - started_at)::numeric / 60, 2), 0) AS total_wall_minutes,
               COALESCE(MIN(started_at), 0) AS first_session_ts,
               COALESCE(MAX(started_at), 0) AS last_session_ts
        FROM axgt_sessions
    """)
    if e:
        return None, e
    s = rows[0]
    dep, _ = _telemetry_query("""
        SELECT COALESCE(SUM(credited_minutes_total), 0) AS total_credited,
               COALESCE(SUM(consumed_minutes_total), 0) AS total_consumed,
               COALESCE(SUM(remaining_minutes), 0) AS total_remaining
        FROM axgt_deposits
    """)
    d = (dep or [{}])[0]
    wr, _ = _telemetry_query("""
        SELECT COUNT(*) AS total,
               COUNT(CASE WHEN state='closed' THEN 1 END) AS closed,
               COUNT(CASE WHEN state='failed' THEN 1 END) AS failed,
               COUNT(CASE WHEN answer_sdp IS NOT NULL THEN 1 END) AS answered
        FROM axgt_webrtc_signaling
    """)
    w = (wr or [{}])[0]
    return {
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
    }, None


def _auth_pg_ensure_table(conn) -> None:
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


def _auth_pg_init_once() -> bool:
    global _auth_pg_init_done
    if not _auth_db_url():
        return False
    with _auth_pg_init_lock:
        if _auth_pg_init_done:
            return True
        conn = _auth_pg_get_connection()
        if not conn:
            return False
        try:
            _auth_pg_ensure_table(conn)
            _auth_pg_init_done = True
            return True
        except Exception as e:
            logger.warning("Postgres auth token table init failed: %s", e)
            return False
        finally:
            conn.close()


def _auth_ttl_seconds() -> int:
    raw = (os.getenv("AXGT_AUTH_TOKEN_TTL_SECONDS") or "").strip()
    if not raw:
        return 300
    try:
        ttl = int(raw)
        if ttl <= 0:
            raise ValueError("must be positive")
        return ttl
    except ValueError:
        logger.warning("Invalid AXGT_AUTH_TOKEN_TTL_SECONDS '%s', using default 300", raw)
        return 300


def _auth_rotate_before_expiry_seconds() -> int:
    raw = (os.getenv("AXGT_AUTH_ROTATE_BEFORE_EXPIRY_SECONDS") or "").strip()
    if not raw:
        return 60
    try:
        value = int(raw)
        if value < 0:
            raise ValueError("must be >= 0")
        return value
    except ValueError:
        logger.warning("Invalid AXGT_AUTH_ROTATE_BEFORE_EXPIRY_SECONDS '%s', using default 60", raw)
        return 60


def _auth_grace_seconds() -> int:
    raw = (os.getenv("AXGT_AUTH_GRACE_SECONDS") or "").strip()
    if not raw:
        return 15
    try:
        value = int(raw)
        if value < 0:
            raise ValueError("must be >= 0")
        return value
    except ValueError:
        logger.warning("Invalid AXGT_AUTH_GRACE_SECONDS '%s', using default 15", raw)
        return 15


def _auth_cookie_name() -> str:
    return (os.getenv("AXGT_AUTH_COOKIE_NAME") or "axgt_auth_token").strip()


def _auth_cookie_secure() -> bool:
    raw = (os.getenv("AXGT_AUTH_COOKIE_SECURE") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _build_auth_cookie(token: str, max_age_seconds: int) -> str:
    name = _auth_cookie_name()
    secure_flag = "Secure; " if _auth_cookie_secure() else ""
    return (
        f"{name}={token}; Path=/; HttpOnly; SameSite=Lax; "
        f"{secure_flag}Max-Age={max_age_seconds}"
    )


def _clear_auth_cookie() -> str:
    name = _auth_cookie_name()
    secure_flag = "Secure; " if _auth_cookie_secure() else ""
    return (
        f"{name}=; Path=/; HttpOnly; SameSite=Lax; "
        f"{secure_flag}Max-Age=0"
    )


def _issue_auth_token(wallet_address: str) -> tuple[str, int]:
    now_ts = time.time()
    ttl = _auth_ttl_seconds()
    token = secrets.token_urlsafe(32)

    if not _auth_pg_init_once():
        raise RuntimeError("Auth token DB unavailable (Postgres init failed)")

    conn = _auth_pg_get_connection()
    if not conn:
        raise RuntimeError("Auth token DB unavailable (could not connect)")
    try:
        with conn.cursor() as cur:
            # Prune expired
            cur.execute(
                f"DELETE FROM {_AUTH_TABLE} WHERE GREATEST(expires_at, grace_until) <= %s",
                (now_ts,),
            )
            # Retire current tokens for this wallet → grace
            grace_until = now_ts + _auth_grace_seconds()
            cur.execute(
                f"""UPDATE {_AUTH_TABLE}
                    SET status = 'grace',
                        grace_until = GREATEST(expires_at, %s)
                    WHERE wallet_address = %s AND status = 'current'""",
                (grace_until, wallet_address),
            )
            # Insert new token
            cur.execute(
                f"""INSERT INTO {_AUTH_TABLE}
                    (token, wallet_address, issued_at, expires_at, status, grace_until)
                    VALUES (%s, %s, %s, %s, 'current', %s)""",
                (token, wallet_address, now_ts, now_ts + ttl, now_ts + ttl),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning("Postgres auth token insert failed: %s", e)
        raise RuntimeError("Auth token DB write failed") from e
    finally:
        conn.close()
    return token, ttl


def _current_wallet_token_and_remaining(wallet_address: str) -> tuple[str | None, int | None]:
    now_ts = time.time()
    if not _auth_pg_init_once():
        return None, None
    conn = _auth_pg_get_connection()
    if not conn:
        return None, None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT token, expires_at FROM {_AUTH_TABLE}
                    WHERE wallet_address = %s AND status = 'current' AND expires_at > %s
                    ORDER BY expires_at DESC LIMIT 1""",
                (wallet_address, now_ts),
            )
            row = cur.fetchone()
            if row:
                return row[0], int(row[1] - now_ts)
    except Exception as e:
        logger.warning("Postgres auth token lookup failed: %s", e)
    finally:
        conn.close()
    return None, None


def _auth_token_remaining_seconds(token: str, wallet_address: str) -> int | None:
    now_ts = time.time()
    if not _auth_pg_init_once():
        return None
    conn = _auth_pg_get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT expires_at FROM {_AUTH_TABLE}
                    WHERE token = %s AND wallet_address = %s
                    AND status = 'current' AND expires_at > %s""",
                (token, wallet_address, now_ts),
            )
            row = cur.fetchone()
            if row:
                return int(row[0] - now_ts)
    except Exception as e:
        logger.warning("Postgres auth token remaining check failed: %s", e)
    finally:
        conn.close()
    return None


def _is_auth_token_valid(token: str, wallet_address: str) -> bool:
    if not token:
        return False
    now_ts = time.time()
    if not _auth_pg_init_once():
        return False
    conn = _auth_pg_get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT status, expires_at, grace_until FROM {_AUTH_TABLE}
                    WHERE token = %s AND wallet_address = %s""",
                (token, wallet_address),
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


def _rotate_auth_token(existing_token: str, wallet_address: str) -> tuple[str | None, int]:
    now_ts = time.time()
    if not _auth_pg_init_once():
        return None, 0
    conn = _auth_pg_get_connection()
    if not conn:
        return None, 0
    try:
        with conn.cursor() as cur:
            # Prune expired
            cur.execute(
                f"DELETE FROM {_AUTH_TABLE} WHERE GREATEST(expires_at, grace_until) <= %s",
                (now_ts,),
            )
            # Verify existing token belongs to wallet
            cur.execute(
                f"SELECT 1 FROM {_AUTH_TABLE} WHERE token = %s AND wallet_address = %s",
                (existing_token, wallet_address),
            )
            if not cur.fetchone():
                conn.rollback()
                return None, 0
            # Check if a newer current token already exists
            cur.execute(
                f"""SELECT token, expires_at FROM {_AUTH_TABLE}
                    WHERE wallet_address = %s AND status = 'current'
                    AND expires_at > %s AND token != %s
                    ORDER BY expires_at DESC LIMIT 1""",
                (wallet_address, now_ts, existing_token),
            )
            row = cur.fetchone()
            if row:
                conn.rollback()
                return row[0], int(row[1] - now_ts)
            # Issue new token
            ttl = _auth_ttl_seconds()
            new_token = secrets.token_urlsafe(32)
            grace_until = now_ts + _auth_grace_seconds()
            cur.execute(
                f"""UPDATE {_AUTH_TABLE}
                    SET status = 'grace',
                        grace_until = GREATEST(expires_at, %s)
                    WHERE wallet_address = %s AND status = 'current'""",
                (grace_until, wallet_address),
            )
            cur.execute(
                f"""INSERT INTO {_AUTH_TABLE}
                    (token, wallet_address, issued_at, expires_at, status, grace_until)
                    VALUES (%s, %s, %s, %s, 'current', %s)""",
                (new_token, wallet_address, now_ts, now_ts + ttl, now_ts + ttl),
            )
        conn.commit()
        return new_token, ttl
    except Exception as e:
        conn.rollback()
        logger.warning("Postgres auth token rotate failed: %s", e)
        return None, 0
    finally:
        conn.close()


def _extract_wallet_from_path_and_headers(path: str, headers) -> str | None:
    try:
        parsed = urlparse(path if path else '/')
        query_params = parse_qs(parsed.query)
        wallet_address = query_params.get('wallet', [None])[0]
    except Exception:
        wallet_address = None

    if not wallet_address:
        wallet_address = headers.get('X-Wallet-Address') if headers else None
    if not wallet_address:
        return None
    return wallet_address.strip()


def _extract_auth_token_from_path_and_headers(path: str, headers) -> str | None:
    cookie_header = headers.get('Cookie') if headers else None
    if cookie_header:
        try:
            parsed_cookie = SimpleCookie()
            parsed_cookie.load(cookie_header)
            cookie_name = _auth_cookie_name()
            if cookie_name in parsed_cookie:
                token = parsed_cookie[cookie_name].value.strip()
                if token:
                    return token
        except Exception:
            pass
    header_token = headers.get('X-AXGT-Auth-Token') if headers else None
    if header_token:
        token = header_token.strip()
        if token:
            return token
    try:
        parsed = urlparse(path if path else '/')
        query_params = parse_qs(parsed.query)
        token = query_params.get('auth_token', [None])[0]
        if token:
            return token.strip()
    except Exception:
        pass
    return None


class AxonOSProxyRequestHandler(websockify.websocketproxy.ProxyRequestHandler):
    """
    Extends websockify's HTTP handler to:
    - Serve /api/auth/* on the SAME origin/port as noVNC (6080)
    - Gate WebSocket upgrades using wallet + short-lived auth token
    """

    def _send_json(
        self,
        status_code: int,
        payload: dict,
        set_cookie: str | None = None,
        no_cache: bool = False,
    ):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        if no_cache:
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, private')
            self.send_header('Pragma', 'no-cache')
        # CORS: default is same-origin (no wildcard). For unusual deployments set AXGT_CORS_ORIGINS.
        origin = cors_origin_for_request(
            self.headers.get("Origin"),
            self.headers.get("Host"),
            _allow_any,
            _allowlist,
        )
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
            self.send_header(
                'Access-Control-Allow-Headers',
                'Content-Type, X-Wallet-Address, X-AXGT-Auth-Token'
            )
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Credentials', 'true')
        if set_cookie:
            self.send_header('Set-Cookie', set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if (
            self.path.startswith('/api/auth/')
            or self.path.startswith('/api/config')
            or self.path.startswith('/api/session/')
            or self.path.startswith('/api/webrtc/')
            or self.path.startswith('/api/public/')
        ):
            self.send_response(200)
            origin = cors_origin_for_request(
                self.headers.get("Origin"),
                self.headers.get("Host"),
                _allow_any,
                _allowlist,
            )
            if origin:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
                self.send_header(
                    'Access-Control-Allow-Headers',
                    'Content-Type, X-Wallet-Address, X-AXGT-Auth-Token'
                )
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Access-Control-Allow-Credentials', 'true')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        return super().do_OPTIONS()

    def do_GET(self):
        from urllib.parse import urlparse as _up_cfg

        if _up_cfg(self.path).path in ('/telemetry', '/telemetry/'):
            try:
                with open('/usr/share/novnc/telemetry.html', 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if _up_cfg(self.path).path == '/api/config':
            policy = get_credit_policy()
            _dec_raw = (os.getenv("AXGT_TOKEN_DECIMALS") or "").strip()
            try:
                _td = int(_dec_raw) if _dec_raw else 18
                axgt_token_decimals = max(0, min(255, _td))
            except ValueError:
                axgt_token_decimals = 18
            payload = {
                "axgt_contract_address": (os.getenv("AXGT_CONTRACT_ADDRESS") or "").strip() or None,
                "axgt_chain_id": (os.getenv("AXGT_CHAIN_ID") or "").strip() or None,
                "axgt_revenue_wallet": (os.getenv("AXGT_REVENUE_WALLET") or "").strip() or None,
                "axgt_token_decimals": axgt_token_decimals,
                "axgt_min_deposit": policy.get("min_deposit"),
                "axgt_credit_per_100_axgt_minutes": policy.get("credit_per_100_axgt_minutes"),
                "eth_deposits_enabled": policy.get("eth_deposits_enabled"),
                "eth_min_deposit": policy.get("eth_min_deposit"),
                "eth_credit_per_eth_minutes": policy.get("eth_credit_per_eth_minutes"),
                "axgt_warning_threshold_minutes": policy.get("warning_threshold_minutes"),
                "min_axgt_deposit_minutes": policy.get("min_axgt_deposit_minutes"),
                "min_eth_deposit_minutes": policy.get("min_eth_deposit_minutes"),
                "axgt_direct_deposits_enabled": policy.get("axgt_direct_deposits_enabled"),
                "axgt_discount_tiers": policy.get("axgt_discount_tiers", []),
                "multi_session_enabled": (os.getenv("AXGT_MULTI_SESSION_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")),
                "gpu_profiles_enabled": (os.getenv("AXGT_GPU_PROFILES_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")),
                "gpu_profiles": {"small": 1, "medium": 2, "large": 4, "max": 8},
                "gpu_weighted_billing_enabled": policy.get("gpu_weighted_billing_enabled", False),
            }
            if webrtc_config is not None:
                payload.update(webrtc_config.public_config())
            else:
                payload.update(
                    {
                        "webrtc_enabled": False,
                        "webrtc_fallback_enabled": True,
                    }
                )
            return self._send_json(200, payload)

        if self.path.startswith('/api/discount/quote'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            wallet_address = ''
            if 'wallet_address' in qs and qs['wallet_address']:
                wallet_address = (qs['wallet_address'][0] or '').strip()
            if not wallet_address:
                wallet_address = (self.headers.get('X-Wallet-Address') or '').strip()
            if not wallet_address:
                return self._send_json(400, {'ok': False, 'error': 'wallet_address is required'})
            if not validate_wallet_address(wallet_address):
                return self._send_json(400, {'ok': False, 'error': 'Invalid wallet address format.'})
            try:
                try:
                    from . import discount as _disc
                except ImportError:
                    try:
                        from axonos_gate import discount as _disc
                    except ImportError:
                        import discount as _disc  # type: ignore[no-redef]
            except ImportError:
                return self._send_json(503, {'ok': False, 'error': 'Discount module unavailable'})
            policy = get_credit_policy()
            from decimal import Decimal as _D, InvalidOperation as _IO
            base_raw = (qs.get('base_eth', [''])[0] or '').strip()
            if base_raw:
                try:
                    base_eth = _D(base_raw)
                    if base_eth <= 0:
                        raise _IO("base_eth must be positive")
                except (_IO, ValueError):
                    return self._send_json(400, {'ok': False, 'error': 'Invalid base_eth value'})
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
            return self._send_json(200, {
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

        if self.path.startswith('/api/auth/challenge'):
            wallet_address = _extract_wallet_from_path_and_headers(self.path, self.headers)
            if not wallet_address:
                return self._send_json(400, {'error': 'wallet_address is required'})
            if not validate_wallet_address(wallet_address):
                return self._send_json(400, {'error': 'Invalid wallet address format.'})
            try:
                challenge = get_challenge_message(wallet_address)
                return self._send_json(200, {
                    'challenge': challenge,
                    'challenge_expires_in_seconds': get_challenge_ttl_seconds(),
                })
            except ValueError:
                return self._send_json(400, {'error': 'wallet_address is invalid'})
            except Exception as e:
                logger.error("Failed generating auth challenge: %s", e, exc_info=True)
                return self._send_json(500, {'error': 'Failed to generate challenge'})

        if self.path.startswith('/api/auth/wallet-status'):
            wallet_address = _extract_wallet_from_path_and_headers(self.path, self.headers)
            if not wallet_address:
                return self._send_json(400, {'verified': False, 'error': 'wallet_address is required'})
            if not validate_wallet_address(wallet_address):
                return self._send_json(400, {'verified': False, 'error': 'Invalid wallet address format.'})
            auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
            if not auth_token:
                return self._send_json(
                    401,
                    {'verified': False, 'error': 'AXGT auth token required. Please verify wallet again.'},
                    set_cookie=_clear_auth_cookie()
                )
            if not _is_auth_token_valid(auth_token, wallet_address):
                return self._send_json(
                    401,
                    {'verified': False, 'error': 'Invalid or expired AXGT auth token. Please verify wallet again.'},
                    set_cookie=_clear_auth_cookie()
                )

            status = get_wallet_access_status(wallet_address, consume_usage=True)
            status['wallet_address'] = wallet_address
            if not status.get("verified"):
                status['error'] = status.get("reason") or 'Access denied for this wallet.'
                return self._send_json(200, status)

            remaining = _auth_token_remaining_seconds(auth_token, wallet_address)
            if remaining is None:
                return self._send_json(
                    401,
                    {'verified': False, 'error': 'Invalid or expired AXGT auth token. Please verify wallet again.'},
                    set_cookie=_clear_auth_cookie()
                )

            if remaining <= _auth_rotate_before_expiry_seconds():
                new_token, ttl = _rotate_auth_token(auth_token, wallet_address)
                if not new_token:
                    return self._send_json(
                        401,
                        {'verified': False, 'error': 'AXGT auth token refresh failed. Please verify wallet again.'},
                        set_cookie=_clear_auth_cookie()
                    )
                status['auth_token'] = new_token
                status['auth_token_expires_in_seconds'] = ttl
                return self._send_json(200, status, set_cookie=_build_auth_cookie(new_token, ttl))

            status['auth_token_expires_in_seconds'] = remaining
            return self._send_json(200, status)

        from urllib.parse import parse_qs, urlparse

        pu = urlparse(self.path)
        ponly = pu.path

        if webrtc_service and ponly.startswith('/api/webrtc/config'):
            st, pl = webrtc_service.handle_config_public()
            return self._send_json(st, pl)

        if webrtc_service and ponly.startswith('/api/webrtc/status'):
            qs = parse_qs(pu.query)
            sid = (qs.get('session_id') or [''])[0].strip()
            wallet = (qs.get('wallet_address') or [''])[0].strip() or (self.headers.get('X-Wallet-Address') or '').strip()
            if not sid or not wallet or not validate_wallet_address(wallet):
                return self._send_json(400, {'ok': False, 'error': 'session_id and wallet_address required'})
            auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
            if not auth_token or not _is_auth_token_valid(auth_token, wallet):
                return self._send_json(401, {'ok': False, 'error': 'Valid auth token required'})
            wn = wallet.lower()
            st, pl = webrtc_service.handle_get_status(sid, wn, True)
            return self._send_json(st, pl, no_cache=True)

        if webrtc_service and ponly.startswith('/api/webrtc/agent/next'):
            key = (self.headers.get('X-AxonOS-WebRTC-Agent-Key') or '').strip()
            st, pl = webrtc_service.handle_agent_next(key)
            if st == 204:
                self.send_response(204)
                origin = cors_origin_for_request(
                    self.headers.get("Origin"),
                    self.headers.get("Host"),
                    _allow_any,
                    _allowlist,
                )
                if origin:
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Vary', 'Origin')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            return self._send_json(st, pl)

        if webrtc_service and ponly.startswith('/api/webrtc/agent/row'):
            key = (self.headers.get('X-AxonOS-WebRTC-Agent-Key') or '').strip()
            qs = parse_qs(pu.query)
            sid = (qs.get('session_id') or [''])[0].strip()
            st, pl = webrtc_service.handle_agent_row(key, sid)
            return self._send_json(st, pl)

        # ---- Session / Queue read endpoints ----
        if _session_mgr_available and self.path.startswith('/api/session/status'):
            wallet_address = _extract_wallet_from_path_and_headers(self.path, self.headers)
            result = session_status(wallet_address)
            return self._send_json(200, result)

        # ---- Public telemetry (no auth required) ----
        if pu.path == '/api/public/telemetry/summary':
            data, err = _telemetry_summary()
            if err:
                return self._send_json(500, {"error": err})
            return self._send_json(200, data)

        if pu.path == '/api/public/telemetry/sessions':
            qs = parse_qs(pu.query)
            limit = min(1000, max(1, int((qs.get('limit') or ['300'])[0])))
            rows, err = _telemetry_query("""
                SELECT id, wallet_address, requested_profile, gpu_ids,
                       allocation_status, status, started_at, last_heartbeat,
                       ROUND(((last_heartbeat - started_at) / 60)::numeric, 2) AS duration_minutes
                FROM axgt_sessions ORDER BY started_at DESC LIMIT %s
            """, (limit,))
            if err:
                return self._send_json(500, {"error": err})
            for r in rows:
                r["id"] = int(r["id"])
                r["duration_minutes"] = float(r.get("duration_minutes") or 0)
                for k in ("started_at", "last_heartbeat"):
                    r[k] = float(r.get(k) or 0)
            return self._send_json(200, {"sessions": rows, "count": len(rows)})

        if pu.path == '/api/public/telemetry/wallets':
            rows, err = _telemetry_query("""
                SELECT s.wallet_address,
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
            if err:
                return self._send_json(500, {"error": err})
            for r in rows:
                for k in ("first_session_ts", "last_session_ts"):
                    r[k] = float(r.get(k) or 0)
                for k in ("total_wall_minutes", "credited_minutes", "consumed_minutes", "remaining_minutes"):
                    r[k] = float(r.get(k) or 0)
                for k in ("total_sessions", "failed_sessions", "max_sessions", "small_sessions"):
                    r[k] = int(r.get(k) or 0)
            return self._send_json(200, {"wallets": rows})

        if pu.path == '/api/public/telemetry/events':
            qs = parse_qs(pu.query)
            limit = min(500, max(1, int((qs.get('limit') or ['100'])[0])))
            rows, err = _telemetry_query("""
                SELECT id, wallet_address, event_type,
                       ROUND(minutes_delta::numeric, 4) AS minutes_delta,
                       ROUND(balance_after_minutes::numeric, 2) AS balance_after_minutes,
                       reference_session_id, notes, created_at
                FROM axgt_ledger ORDER BY id DESC LIMIT %s
            """, (limit,))
            if err:
                return self._send_json(500, {"error": err})
            for r in rows:
                r["id"] = int(r["id"])
                r["created_at"] = float(r.get("created_at") or 0)
                for k in ("minutes_delta", "balance_after_minutes"):
                    r[k] = float(r.get(k) or 0)
            return self._send_json(200, {"events": rows, "count": len(rows)})

        if pu.path == '/api/public/telemetry/live':
            import time as _t, urllib.request as _ur
            now = _t.time()

            # Active sessions with heartbeat staleness
            sess_rows, _ = _telemetry_query("""
                SELECT id, wallet_address, requested_profile, gpu_ids, container_id,
                       started_at, last_heartbeat, expires_at,
                       ROUND(((last_heartbeat - started_at) / 60)::numeric, 2) AS duration_minutes
                FROM axgt_sessions WHERE status = 'active'
                ORDER BY started_at DESC
            """)
            live_sessions = []
            for r in (sess_rows or []):
                r["id"] = int(r["id"])
                r["duration_minutes"] = float(r.get("duration_minutes") or 0)
                for k in ("started_at", "last_heartbeat", "expires_at"):
                    r[k] = float(r.get(k) or 0)
                r["heartbeat_age_seconds"] = round(now - r["last_heartbeat"], 1) if r["last_heartbeat"] else None
                r["expires_in_seconds"] = round(r["expires_at"] - now, 1) if r["expires_at"] else None
                live_sessions.append(r)

            # GPU stats from background cache (updated every 10s)
            with _gpu_cache_lock:
                gpus = list(_gpu_cache["gpus"])
                gpu_cache_age = round(now - _gpu_cache["ts"], 1) if _gpu_cache["ts"] else None

            # Running session containers from launcher
            containers = []
            try:
                launcher_url = (os.getenv("AXGT_SESSION_LAUNCHER_URL") or "").rstrip("/")
                launcher_token = os.getenv("AXGT_SESSION_LAUNCHER_TOKEN") or ""
                if launcher_url and launcher_token:
                    req = _ur.Request(
                        launcher_url + "/list-containers",
                        headers={"Authorization": "Bearer " + launcher_token}
                    )
                    with _ur.urlopen(req, timeout=5) as resp:
                        containers = json.loads(resp.read()).get("containers", [])
            except Exception as exc:
                logger.warning("launcher list-containers failed: %s", exc)

            return self._send_json(200, {
                "timestamp": now,
                "active_sessions": live_sessions,
                "gpus": gpus,
                "gpu_cache_age_seconds": gpu_cache_age,
                "containers": containers,
            })

        if pu.path == '/api/public/telemetry/webrtc':
            rows, err = _telemetry_query("""
                SELECT wallet_address, state,
                       offer_sdp IS NOT NULL AS has_offer,
                       answer_sdp IS NOT NULL AS has_answer,
                       last_error, created_at, updated_at,
                       ROUND((updated_at - created_at)::numeric, 1) AS duration_seconds
                FROM axgt_webrtc_signaling ORDER BY created_at DESC LIMIT 200
            """)
            if err:
                return self._send_json(500, {"error": err})
            for r in rows:
                for k in ("created_at", "updated_at"):
                    r[k] = float(r.get(k) or 0)
                r["duration_seconds"] = float(r.get("duration_seconds") or 0)
            brows, _ = _telemetry_query("""
                SELECT state, COUNT(*) AS count FROM axgt_webrtc_signaling GROUP BY state
            """)
            breakdown = {r["state"]: int(r["count"]) for r in (brows or [])}
            return self._send_json(200, {"sessions": rows, "count": len(rows), "breakdown": breakdown})

        return super().do_GET()

    def _read_json_body(self) -> dict:
        try:
            content_length = int(self.headers.get('Content-Length') or '0')
        except ValueError:
            content_length = 0
        raw = self.rfile.read(content_length) if content_length > 0 else b''
        try:
            return json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            return {}

    def do_POST(self):
        from urllib.parse import urlparse

        pu = urlparse(self.path)
        ponly = pu.path
        client_ip = self.client_address[0] if getattr(self, "client_address", None) else "unknown"

        if webrtc_service and ponly.startswith("/api/webrtc/"):
            data = self._read_json_body()

            if ponly == "/api/webrtc/session":
                wallet = (data.get("wallet_address") or "").strip()
                if not wallet or not validate_wallet_address(wallet):
                    return self._send_json(400, {"ok": False, "error": "Valid wallet_address required"})
                auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
                if not auth_token or not _is_auth_token_valid(auth_token, wallet):
                    return self._send_json(401, {"ok": False, "error": "Valid auth token required"})
                wn = wallet.lower()
                if not _webrtc_ws_rate_allow(wn, self.headers, client_ip):
                    return self._send_json(429, {"ok": False, "error": "Rate limit exceeded"})
                owner = _session_mgr_available and is_session_owner(wn)
                st, pl = webrtc_service.handle_create_session(wn, True, owner)
                return self._send_json(st, pl)

            if ponly == "/api/webrtc/offer":
                wallet = (data.get("wallet_address") or "").strip()
                sid = (data.get("session_id") or "").strip()
                if not wallet or not validate_wallet_address(wallet):
                    return self._send_json(400, {"ok": False, "error": "Valid wallet_address required"})
                auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
                if not auth_token or not _is_auth_token_valid(auth_token, wallet):
                    return self._send_json(401, {"ok": False, "error": "Valid auth token required"})
                wn = wallet.lower()
                if not _webrtc_ws_rate_allow(wn, self.headers, client_ip):
                    return self._send_json(429, {"ok": False, "error": "Rate limit exceeded"})
                owner = _session_mgr_available and is_session_owner(wn)
                st, pl = webrtc_service.handle_post_offer(sid, wn, True, owner, data)
                return self._send_json(st, pl)

            if ponly == "/api/webrtc/ice":
                wallet = (data.get("wallet_address") or "").strip()
                sid = (data.get("session_id") or "").strip()
                if not wallet or not validate_wallet_address(wallet) or not sid:
                    return self._send_json(400, {"ok": False, "error": "wallet_address and session_id required"})
                auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
                if not auth_token or not _is_auth_token_valid(auth_token, wallet):
                    return self._send_json(401, {"ok": False, "error": "Valid auth token required"})
                wn = wallet.lower()
                if not _webrtc_ws_rate_allow(wn, self.headers, client_ip):
                    return self._send_json(429, {"ok": False, "error": "Rate limit exceeded"})
                st, pl = webrtc_service.handle_post_client_ice(sid, wn, True, data)
                return self._send_json(st, pl)

            if ponly == "/api/webrtc/metrics":
                wallet = (data.get("wallet_address") or "").strip()
                sid = (data.get("session_id") or "").strip()
                if not wallet or not validate_wallet_address(wallet) or not sid:
                    return self._send_json(400, {"ok": False, "error": "wallet_address and session_id required"})
                auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
                if not auth_token or not _is_auth_token_valid(auth_token, wallet):
                    return self._send_json(401, {"ok": False, "error": "Valid auth token required"})
                st, pl = webrtc_service.handle_post_client_metrics(sid, wallet.lower(), True, data)
                return self._send_json(st, pl)

            if ponly == "/api/webrtc/close":
                wallet = (data.get("wallet_address") or "").strip()
                sid = (data.get("session_id") or "").strip()
                if not wallet or not validate_wallet_address(wallet) or not sid:
                    return self._send_json(400, {"ok": False, "error": "wallet_address and session_id required"})
                auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
                if not auth_token or not _is_auth_token_valid(auth_token, wallet):
                    return self._send_json(401, {"ok": False, "error": "Valid auth token required"})
                st, pl = webrtc_service.handle_close(sid, wallet.lower(), True)
                return self._send_json(st, pl)

            if ponly == "/api/webrtc/agent/answer":
                key = (self.headers.get("X-AxonOS-WebRTC-Agent-Key") or "").strip()
                st, pl = webrtc_service.handle_agent_answer(key, data)
                return self._send_json(st, pl)

            if ponly == "/api/webrtc/agent/fail":
                key = (self.headers.get("X-AxonOS-WebRTC-Agent-Key") or "").strip()
                st, pl = webrtc_service.handle_agent_fail(key, data)
                return self._send_json(st, pl)

            return self._send_json(404, {"ok": False, "error": "Unknown WebRTC path"})

        # ---- Session / Queue write endpoints ----
        if _session_mgr_available and self.path.startswith('/api/session/claim'):
            data = self._read_json_body()
            wallet_address = (data.get('wallet_address') or '').strip()
            requested_profile = (data.get('requested_profile') or '').strip() or None
            if not wallet_address or not validate_wallet_address(wallet_address):
                return self._send_json(400, {'granted': False, 'error': 'Valid wallet_address required'})
            auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
            if not auth_token or not _is_auth_token_valid(auth_token, wallet_address):
                return self._send_json(401, {'granted': False, 'error': 'Valid auth token required'})
            result = try_claim_session(wallet_address, requested_profile=requested_profile)
            return self._send_json(200, result)

        if _session_mgr_available and self.path.startswith('/api/session/heartbeat'):
            data = self._read_json_body()
            wallet_address = (data.get('wallet_address') or '').strip()
            if not wallet_address or not validate_wallet_address(wallet_address):
                return self._send_json(400, {'ok': False, 'error': 'Valid wallet_address required'})
            auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
            if not auth_token or not _is_auth_token_valid(auth_token, wallet_address):
                return self._send_json(401, {'ok': False, 'error': 'Valid auth token required'})
            result = session_heartbeat(wallet_address)
            return self._send_json(200, result)

        if _session_mgr_available and self.path.startswith('/api/session/release'):
            data = self._read_json_body()
            wallet_address = (data.get('wallet_address') or '').strip()
            if not wallet_address or not validate_wallet_address(wallet_address):
                return self._send_json(400, {'released': False, 'error': 'Valid wallet_address required'})
            auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
            if not auth_token or not _is_auth_token_valid(auth_token, wallet_address):
                return self._send_json(401, {'released': False, 'error': 'Valid auth token required'})
            result = release_session(wallet_address)
            return self._send_json(200, result)

        if _session_mgr_available and self.path.startswith('/api/session/restart'):
            data = self._read_json_body()
            wallet_address = (data.get('wallet_address') or '').strip()
            if not wallet_address or not validate_wallet_address(wallet_address):
                return self._send_json(400, {'restarted': False, 'error': 'Valid wallet_address required'})
            auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
            if not auth_token or not _is_auth_token_valid(auth_token, wallet_address):
                return self._send_json(401, {'restarted': False, 'error': 'Valid auth token required'})
            result = restart_desktop_session(wallet_address)
            return self._send_json(200, result)

        if self.path.startswith('/api/auth/verify-deposit'):
            if verify_deposit is None:
                return self._send_json(
                    503, {"verified": False, "error": "Deposit verification unavailable"}
                )
            data = self._read_json_body()
            wallet_address = (data.get("wallet_address") or "").strip()
            tx_hash = (data.get("tx_hash") or "").strip()
            if not wallet_address or not validate_wallet_address(wallet_address):
                return self._send_json(
                    400, {"verified": False, "error": "Valid wallet_address required"}
                )
            if not tx_hash:
                return self._send_json(400, {"verified": False, "error": "tx_hash required"})
            auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
            if not auth_token or not _is_auth_token_valid(auth_token, wallet_address):
                return self._send_json(
                    401, {"verified": False, "error": "Valid auth token required"}
                )
            result = verify_deposit(authenticated_wallet=wallet_address, tx_hash=tx_hash)
            if result.get("verified") or verify_deposit_is_pending(result):
                return self._send_json(200, result)
            return self._send_json(400, result)

        if not self.path.startswith('/api/auth/verify-wallet'):
            return self.send_error(404, "Not Found")

        # Best-effort rate limiting (per client IP)
        if _rate_limiter is not None:
            client_ip = (self.headers.get("X-Forwarded-For") or self.client_address[0] or "unknown").split(",")[0].strip()
            if not _rate_limiter.allow(client_ip):
                return self._send_json(429, {"verified": False, "error": "Rate limit exceeded"})

        data = self._read_json_body()

        wallet_address = (data.get('wallet_address') or '').strip()
        message = (data.get('message') or '').strip()
        signature_hex = (data.get('signature') or data.get('signature_hex') or '').strip()
        if not wallet_address:
            return self._send_json(400, {'verified': False, 'error': 'wallet_address is required'})
        if not validate_wallet_address(wallet_address):
            return self._send_json(400, {
                'verified': False,
                'error': 'Invalid wallet address format. Must be 0x followed by 40 hex characters.'
            })
        if not message:
            return self._send_json(400, {'verified': False, 'error': 'message is required'})
        if not signature_hex:
            return self._send_json(400, {'verified': False, 'error': 'signature is required'})

        if not verify_signed_challenge(wallet_address, message, signature_hex):
            logger.warning("Sign-to-verify failed for %s", mask_wallet_address(wallet_address))
            return self._send_json(401, {
                'verified': False,
                'error': 'Wallet signature verification failed.'
            })

        status = get_wallet_access_status(wallet_address, consume_usage=False)
        status["wallet_address"] = wallet_address
        try:
            token, ttl = _issue_auth_token(wallet_address)
        except Exception as ex:
            logger.warning(
                "Auth token issue failed for %s: %s",
                mask_wallet_address(wallet_address),
                ex,
            )
            return self._send_json(
                503,
                {
                    "verified": False,
                    "error": "Auth database unavailable; cannot complete sign-in.",
                    "wallet_address": wallet_address,
                },
                set_cookie=_clear_auth_cookie(),
            )
        if status.get("verified"):
            status["auth_token"] = token
            status["auth_token_expires_in_seconds"] = ttl
            logger.info("Wallet verified (prepaid): %s", mask_wallet_address(wallet_address))
            return self._send_json(200, status, set_cookie=_build_auth_cookie(token, ttl))
        denied = dict(status)
        denied["verified"] = False
        denied["error"] = status.get("reason") or "No access available for this wallet"
        denied["auth_token"] = token
        denied["auth_token_expires_in_seconds"] = ttl
        logger.info("Wallet signed in, no prepaid credit: %s", mask_wallet_address(wallet_address))
        return self._send_json(200, denied, set_cookie=_build_auth_cookie(token, ttl))

    def handle_upgrade(self):
        # Diagnostic: confirms the WebSocket upgrade request reached this process (helps debug 1006 in proxy chains)
        logger.info("WebSocket upgrade request received for /websockify (chain reached AxonOS)")
        wallet_address = _extract_wallet_from_path_and_headers(self.path, self.headers)
        if not wallet_address:
            self.send_error(403, "Wallet address required (?wallet=0x... or X-Wallet-Address)")
            return
        if not validate_wallet_address(wallet_address):
            self.send_error(403, "Invalid wallet address format")
            return

        # Allow internal proxy from gate_server (127.0.0.1 only; tunnel points at GATE_PORT)
        client_address = self.client_address[0] if getattr(self, "client_address", None) else None
        if client_address == "127.0.0.1":
            status = get_wallet_access_status(wallet_address, consume_usage=False)
            if not status.get("verified"):
                reason = status.get("reason") or "Access denied for this wallet"
                self.send_error(403, reason)
                return
            if _session_mgr_available and not is_session_owner(wallet_address):
                self.send_error(403, "Session not owned by this wallet")
                return
            logger.info(
                "WebSocket upgrade approved (internal proxy): %s",
                mask_wallet_address(wallet_address),
            )
            return super().handle_upgrade()

        auth_token = _extract_auth_token_from_path_and_headers(self.path, self.headers)
        if not auth_token:
            self.send_error(403, "AXGT auth token required")
            return
        if not _is_auth_token_valid(auth_token, wallet_address):
            self.send_error(403, "Invalid or expired AXGT auth token")
            return

        status = get_wallet_access_status(wallet_address, consume_usage=False)
        if not status.get("verified"):
            reason = status.get("reason") or "Access denied for this wallet"
            self.send_error(403, reason)
            return

        if _session_mgr_available and not is_session_owner(wallet_address):
            self.send_error(403, "Session not owned by this wallet. Claim a session first.")
            return

        logger.info("WebSocket upgrade approved: %s", mask_wallet_address(wallet_address))
        return super().handle_upgrade()

def main():
    """Run websockify server with wallet gating + same-origin /api/auth/verify-wallet."""
    # Get configuration
    listen_host = (os.getenv('WEBSOCKIFY_HOST') or '0.0.0.0').strip()
    listen_port = int(os.getenv('WEBSOCKIFY_PORT', '6080'))
    target_host = os.getenv('VNC_HOST', 'localhost')
    target_port = int(os.getenv('VNC_PORT', '5901'))
    web_dir = os.getenv('NOVNC_WEB_DIR', '/usr/share/novnc')
    
    logger.info(f"Starting Websockify on {listen_host}:{listen_port}")
    logger.info(f"Target: {target_host}:{target_port}")
    logger.info(
        "AXGT gate enabled: /api/auth/{challenge,verify-wallet,wallet-status} "
        "served on same origin; WebSocket upgrades require wallet + auth token"
    )
    
    # Create and run the proxy (listen_host so tunnel/cloudflared can reach localhost:6080)
    proxy_kw = dict(
        RequestHandlerClass=AxonOSProxyRequestHandler,
        listen_port=listen_port,
        target_host=target_host,
        target_port=target_port,
        web=web_dir,
    )
    try:
        proxy_kw["listen_host"] = listen_host
        server = websockify.WebSocketProxy(**proxy_kw)
    except TypeError:
        # Older websockify package may not support listen_host
        server = websockify.WebSocketProxy(**{k: v for k, v in proxy_kw.items() if k != "listen_host"})
    
    server.start_server()

if __name__ == '__main__':
    main()
