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
from security_utils import cors_origin_for_request, get_rate_limiter_from_env, parse_cors_allowlist

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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_allow_any, _allowlist = parse_cors_allowlist(os.getenv("AXGT_CORS_ORIGINS"))
_rate_limiter = get_rate_limiter_from_env()


_auth_tokens: dict[str, dict] = {}
_auth_lock = Lock()


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


def _prune_expired_auth_tokens(now_ts: float) -> None:
    expired_tokens = []
    for token, info in _auth_tokens.items():
        expires_at = float(info.get("expires_at", 0))
        grace_until = float(info.get("grace_until", 0))
        if now_ts >= max(expires_at, grace_until):
            expired_tokens.append(token)
    for token in expired_tokens:
        _auth_tokens.pop(token, None)


def _retire_current_wallet_tokens(wallet_address: str, now_ts: float) -> None:
    grace_until = now_ts + _auth_grace_seconds()
    for token, info in list(_auth_tokens.items()):
        if info.get("wallet_address") == wallet_address and info.get("status") == "current":
            info["status"] = "grace"
            info["grace_until"] = max(float(info.get("expires_at", now_ts)), grace_until)


def _issue_auth_token(wallet_address: str) -> tuple[str, int]:
    now_ts = time.time()
    ttl = _auth_ttl_seconds()
    token = secrets.token_urlsafe(32)
    with _auth_lock:
        _prune_expired_auth_tokens(now_ts)
        _retire_current_wallet_tokens(wallet_address, now_ts)
        _auth_tokens[token] = {
            "wallet_address": wallet_address,
            "issued_at": now_ts,
            "expires_at": now_ts + ttl,
            "status": "current",
            "grace_until": now_ts + ttl,
        }
    return token, ttl


def _current_wallet_token_and_remaining(wallet_address: str) -> tuple[str | None, int | None]:
    now_ts = time.time()
    current_token = None
    best_remaining = None
    with _auth_lock:
        _prune_expired_auth_tokens(now_ts)
        for token, info in _auth_tokens.items():
            if info.get("wallet_address") != wallet_address:
                continue
            if info.get("status") != "current":
                continue
            remaining = int(float(info.get("expires_at", now_ts)) - now_ts)
            if remaining < 0:
                continue
            if best_remaining is None or remaining > best_remaining:
                current_token = token
                best_remaining = remaining
    return current_token, best_remaining


def _auth_token_remaining_seconds(token: str, wallet_address: str) -> int | None:
    now_ts = time.time()
    with _auth_lock:
        _prune_expired_auth_tokens(now_ts)
        info = _auth_tokens.get(token)
        if not info or info.get("wallet_address") != wallet_address:
            return None
        if info.get("status") != "current":
            return None
        remaining = int(float(info.get("expires_at", 0)) - now_ts)
        if remaining < 0:
            return None
        return remaining


def _is_auth_token_valid(token: str, wallet_address: str) -> bool:
    if not token:
        return False
    now_ts = time.time()
    with _auth_lock:
        _prune_expired_auth_tokens(now_ts)
        info = _auth_tokens.get(token)
        if not info:
            return False
        if info.get("wallet_address") != wallet_address:
            return False
        status = info.get("status")
        expires_at = float(info.get("expires_at", 0))
        grace_until = float(info.get("grace_until", 0))
        if status == "current":
            return now_ts < expires_at
        if status == "grace":
            return now_ts < grace_until
        return False


def _rotate_auth_token(existing_token: str, wallet_address: str) -> tuple[str | None, int]:
    now_ts = time.time()
    with _auth_lock:
        _prune_expired_auth_tokens(now_ts)
        info = _auth_tokens.get(existing_token)
        if not info or info.get("wallet_address") != wallet_address:
            return None, 0
        current_token, remaining = _current_wallet_token_and_remaining(wallet_address)
        if current_token and current_token != existing_token and remaining is not None:
            return current_token, remaining
        ttl = _auth_ttl_seconds()
        new_token = secrets.token_urlsafe(32)
        _retire_current_wallet_tokens(wallet_address, now_ts)
        _auth_tokens[new_token] = {
            "wallet_address": wallet_address,
            "issued_at": now_ts,
            "expires_at": now_ts + ttl,
            "status": "current",
            "grace_until": now_ts + ttl,
        }
        return new_token, ttl


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

    def _send_json(self, status_code: int, payload: dict, set_cookie: str | None = None):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
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
            self.path.startswith('/api/auth/verify-wallet')
            or self.path.startswith('/api/auth/wallet-status')
            or self.path.startswith('/api/auth/challenge')
            or self.path.startswith('/api/config')
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
        # Serve vnc.html at / without redirect (keeps clean URL in address bar)
        if self.path == '/' or self.path == '/?':
            self.path = '/vnc.html'
            return super().do_GET()

        if self.path.startswith('/api/config'):
            policy = get_credit_policy()
            payload = {
                "axgt_contract_address": (os.getenv("AXGT_CONTRACT_ADDRESS") or "").strip() or None,
                "axgt_chain_id": (os.getenv("AXGT_CHAIN_ID") or "").strip() or None,
                "axgt_min_hold_amount": policy.get("min_hold_amount"),
                "axgt_credit_per_100_axgt_minutes": policy.get("credit_per_100_axgt_minutes"),
                "axgt_warning_threshold_minutes": policy.get("warning_threshold_minutes"),
            }
            return self._send_json(200, payload)

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

        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith('/api/auth/verify-wallet'):
            # websockify doesn't implement POST for static; return 404 for safety
            return self.send_error(404, "Not Found")

        # Best-effort rate limiting (per client IP)
        if _rate_limiter is not None:
            client_ip = (self.headers.get("X-Forwarded-For") or self.client_address[0] or "unknown").split(",")[0].strip()
            if not _rate_limiter.allow(client_ip):
                return self._send_json(429, {"verified": False, "error": "Rate limit exceeded"})

        try:
            content_length = int(self.headers.get('Content-Length') or '0')
        except ValueError:
            content_length = 0

        raw = self.rfile.read(content_length) if content_length > 0 else b''
        try:
            data = json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            return self._send_json(400, {'verified': False, 'error': 'Invalid JSON'})

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
        status['wallet_address'] = wallet_address
        if not status.get("verified"):
            logger.info("Wallet verification failed after signature check: %s", mask_wallet_address(wallet_address))
            return self._send_json(200, {
                'verified': False,
                'error': status.get("reason") or 'No access available for this wallet'
            }, set_cookie=_clear_auth_cookie())

        token, ttl = _issue_auth_token(wallet_address)
        status['auth_token'] = token
        status['auth_token_expires_in_seconds'] = ttl
        logger.info("Wallet verified with signature: %s", mask_wallet_address(wallet_address))
        return self._send_json(200, status, set_cookie=_build_auth_cookie(token, ttl))

    def handle_upgrade(self):
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
            if status.get("verified"):
                logger.info(
                    "WebSocket upgrade approved (internal proxy): %s",
                    mask_wallet_address(wallet_address),
                )
                return super().handle_upgrade()
            reason = status.get("reason") or "Access denied for this wallet"
            self.send_error(403, reason)
            return

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
