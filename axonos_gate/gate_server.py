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

from security_utils import cors_origin_for_request, get_rate_limiter_from_env, parse_cors_allowlist

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

# In-memory auth tokens for WebSocket (same-origin verify_wallet flow when tunnel points at GATE_PORT)
_AUTH_TOKEN_TTL = int(os.getenv("AXGT_AUTH_TOKEN_TTL_SECONDS", "300").strip() or "300") or 300
_auth_tokens = {}
_auth_lock = threading.Lock()


def _issue_gate_auth_token(wallet_address: str) -> tuple[str, int]:
    now_ts = time.time()
    token = secrets.token_urlsafe(32)
    with _auth_lock:
        # Prune expired
        expired = [t for t, info in _auth_tokens.items() if (info.get("expires_at") or 0) < now_ts]
        for t in expired:
            _auth_tokens.pop(t, None)
        _auth_tokens[token] = {
            "wallet_address": wallet_address,
            "expires_at": now_ts + _AUTH_TOKEN_TTL,
        }
    return token, _AUTH_TOKEN_TTL


def _is_gate_auth_token_valid(token: str, wallet_address: str) -> bool:
    if not token:
        return False
    now_ts = time.time()
    with _auth_lock:
        info = _auth_tokens.get(token)
        if not info or info.get("wallet_address") != wallet_address:
            return False
        if (info.get("expires_at") or 0) < now_ts:
            return False
        return True

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
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Wallet-Address"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
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
        status['wallet_address'] = wallet_address
        if status.get('verified'):
            token, ttl = _issue_gate_auth_token(wallet_address)
            status['auth_token'] = token
            status['auth_token_expires_in_seconds'] = ttl
            logger.info("Wallet verified: %s", mask_wallet_address(wallet_address))
            return jsonify(status)
        logger.info("Wallet verification failed after signature check: %s", mask_wallet_address(wallet_address))
        return jsonify({
            'verified': False,
            'error': status.get('reason') or 'No access available for this wallet'
        })
            
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


@app.route('/api/config', methods=['GET'])
def api_config():
    policy = get_credit_policy()
    return jsonify({
        'axgt_contract_address': (os.getenv("AXGT_CONTRACT_ADDRESS") or "").strip() or None,
        'axgt_chain_id': (os.getenv("AXGT_CHAIN_ID") or "").strip() or None,
        'axgt_min_hold_amount': policy.get("min_hold_amount"),
        'axgt_credit_per_100_axgt_minutes': policy.get("credit_per_100_axgt_minutes"),
        'axgt_warning_threshold_minutes': policy.get("warning_threshold_minutes"),
    })


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


def _handle_websockify_proxy(environ, start_response):
    """Handle /websockify WebSocket: validate wallet+token, proxy to websockify_gate on 6080."""
    ws = environ.get('wsgi.websocket')
    if not ws:
        start_response('400 Bad Request', [('Content-Type', 'text/plain')])
        return [b'WebSocket expected']

    wallet, auth_token = _extract_wallet_and_token_from_environ(environ)
    if not wallet or not validate_wallet_address(wallet):
        try:
            ws.close(code=403, reason='Invalid or missing wallet')
        except Exception:
            pass
        return []

    if not auth_token or not _is_gate_auth_token_valid(auth_token, wallet):
        try:
            ws.close(code=403, reason='Invalid or expired auth token')
        except Exception:
            pass
        return []

    status = get_wallet_access_status(wallet, consume_usage=False)
    if not status.get('verified'):
        try:
            ws.close(code=403, reason=status.get('reason') or 'Access denied')
        except Exception:
            pass
        return []

    websockify_port = int(os.getenv('WEBSOCKIFY_PORT', '6080'))
    backend_url = f'ws://127.0.0.1:{websockify_port}/websockify?wallet={wallet}'

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


def main():
    """Run the gate server (HTTP + WebSocket on same port)."""
    host = os.getenv('GATE_HOST', '127.0.0.1')
    port = int(os.getenv('GATE_PORT', '8889'))

    logger.info(f"Starting AxonOS AXGT Gate Server on {host}:{port}")
    logger.info(f"AXGT Contract: {(os.getenv('AXGT_CONTRACT_ADDRESS') or '<unset>').strip()}")
    logger.info(f"RPC URL: {(os.getenv('AXGT_RPC_URL') or '<unset>').strip()}")

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
