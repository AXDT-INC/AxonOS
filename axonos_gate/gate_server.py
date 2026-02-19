#!/usr/bin/env python3
"""AXGT Gate Server - API helper implementation."""

import os
import sys
import logging
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

def main():
    """Run the gate server."""
    # Defaults chosen to avoid collisions with IPFS gateway (8080) and reduce surface area.
    # Override via env for non-default deployments.
    host = os.getenv('GATE_HOST', '127.0.0.1')
    port = int(os.getenv('GATE_PORT', '8889'))
    
    logger.info(f"Starting AxonOS AXGT Gate Server on {host}:{port}")
    logger.info(f"AXGT Contract: {(os.getenv('AXGT_CONTRACT_ADDRESS') or '<unset>').strip()}")
    logger.info(f"RPC URL: {(os.getenv('AXGT_RPC_URL') or '<unset>').strip()}")
    
    app.run(host=host, port=port, debug=False, use_reloader=False)

if __name__ == '__main__':
    main()
