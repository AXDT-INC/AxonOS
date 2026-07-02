import os
import time
import logging
import base64
import json
import hashlib
import re
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional, Dict, Any

logger = logging.getLogger(__name__)

def _get_connection():
    url = os.getenv("AXGT_CHALLENGE_DB_URL")
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as e:
        logger.warning("agentlink_verifier: DB connection failed: %s", e)
        return None

def init_nonce_table() -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS axgt_agentlink_nonces (
                    nonce TEXT PRIMARY KEY,
                    agent_address TEXT NOT NULL,
                    chain_id TEXT NOT NULL,
                    resource_uri_hash TEXT NOT NULL,
                    issued_at DOUBLE PRECISION NOT NULL,
                    expires_at DOUBLE PRECISION NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_agentlink_nonces_expiry 
                ON axgt_agentlink_nonces(expires_at)
            """)
        conn.commit()
        return True
    except Exception as e:
        logger.warning("agentlink_verifier: Failed to init nonce table: %s", e)
        return False
    finally:
        conn.close()

def is_nonce_used(nonce: str) -> bool:
    init_nonce_table()
    conn = _get_connection()
    if not conn:
        # Fail closed for security to prevent replays if DB is down
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM axgt_agentlink_nonces WHERE nonce = %s AND expires_at > %s",
                (nonce, time.time())
            )
            return cur.fetchone() is not None
    except Exception as e:
        logger.warning("agentlink_verifier: Failed to check nonce: %s", e)
        return True
    finally:
        conn.close()

def record_nonce(nonce: str, agent_address: str, chain_id: str, resource_uri_hash: str, issued_at: float, expires_at: float) -> bool:
    init_nonce_table()
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            # Clean up expired nonces
            cur.execute("DELETE FROM axgt_agentlink_nonces WHERE expires_at <= %s", (time.time(),))
            # Insert new nonce
            cur.execute(
                """
                INSERT INTO axgt_agentlink_nonces (nonce, agent_address, chain_id, resource_uri_hash, issued_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (nonce) DO NOTHING
                """,
                (nonce, agent_address, chain_id, resource_uri_hash, issued_at, expires_at)
            )
            inserted = cur.rowcount > 0
        conn.commit()
        return inserted
    except Exception as e:
        logger.warning("agentlink_verifier: Failed to record nonce: %s", e)
        return False
    finally:
        conn.close()

def parse_iso_datetime(dt_str: str) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        s = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None

def format_siwe_message(payload: dict) -> str:
    domain = payload.get("domain") or ""
    address = payload.get("address") or ""
    statement = payload.get("statement")
    uri = payload.get("uri") or ""
    version = payload.get("version") or ""
    chain_id = payload.get("chainId") or ""
    nonce = payload.get("nonce") or ""
    issued_at = payload.get("issuedAt") or ""
    
    match = re.match(r"^eip155:(\d+)$", chain_id)
    if not match:
        raise ValueError(f"Invalid chainId format: {chain_id}")
    numeric_chain = int(match.group(1))

    msg = f"{domain} wants you to sign in with your Ethereum account:\n{address}\n"
    if statement:
        msg += f"\n{statement}\n"
        
    msg += "\n"
    
    body_parts = [
        f"URI: {uri}",
        f"Version: {version}",
        f"Chain ID: {numeric_chain}",
        f"Nonce: {nonce}",
        f"Issued At: {issued_at}"
    ]
    
    if payload.get("expirationTime"):
        body_parts.append(f"Expiration Time: {payload['expirationTime']}")
    if payload.get("notBefore"):
        body_parts.append(f"Not Before: {payload['notBefore']}")
    if payload.get("requestId"):
        body_parts.append(f"Request ID: {payload['requestId']}")
    if payload.get("resources"):
        body_parts.append("Resources:")
        for r in payload["resources"]:
            body_parts.append(f"- {r}")
            
    msg += "\n".join(body_parts)
    return msg

def verify_agent_on_chain_registry(agent_address: str, chain_id: str) -> Tuple[bool, Optional[str], Optional[str]]:
    rpc_url = os.getenv("AXGT_AGENTLINK_RPC_URL") or os.getenv("USDC_RPC_URL")
    if not rpc_url:
        if chain_id == "eip155:8453":
            rpc_url = "https://mainnet.base.org"
        elif chain_id == "eip155:84532":
            rpc_url = "https://sepolia.base.org"
        else:
            return False, None, "unsupported_chain"

    contract_address = os.getenv("AXGT_AGENTLINK_REGISTRY_ADDRESS")
    if not contract_address:
        if chain_id == "eip155:8453":
            contract_address = "0x7Ef35Bf180dcDAA5AB6cdEC7e9DED6230aD12263"
        elif chain_id == "eip155:84532":
            contract_address = "0xc0fb26BaACe7E1BCb3aFFD547AD5f2cAc4A4F51b"
        else:
            return False, None, "unsupported_chain"

    addr_hex = agent_address.lower().replace("0x", "").zfill(64)
    call_data = "0xdc10a652" + addr_hex

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": contract_address, "data": call_data},
            "latest"
        ]
    }

    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            res = json.loads(response.read().decode("utf-8"))
            if "error" in res:
                logger.warning("agentlink_verifier: RPC error response: %s", res["error"])
                return False, None, "registry_error"
            
            result = res.get("result")
            if not result or result == "0x":
                return False, None, "registry_error"

            h = result[2:] if result.startswith("0x") else result
            if len(h) < 192:
                return False, None, "registry_error"

            owner = "0x" + h[24:64].lower()
            active = int(h[128:192], 16) != 0

            zero_address = "0x0000000000000000000000000000000000000000"
            if owner == zero_address or not active:
                reason = "not_linked" if owner == zero_address else "inactive"
                return False, owner if owner != zero_address else None, reason

            return True, owner, "active"
    except Exception as e:
        logger.warning("agentlink_verifier: RPC request failed: %s", e)
        return False, None, "registry_error"

def verify_agentlink_header(header_val: str, expected_resource_uri: str) -> dict:
    def fail(reason_code: str, error_msg: str) -> dict:
        return {"verified": False, "reason": reason_code, "error": error_msg}

    if not header_val or len(header_val) > 8192:
        return fail("invalid_header", "Header is missing or exceeds maximum size limit of 8192 bytes")

    try:
        decoded_bytes = base64.b64decode(header_val)
    except Exception:
        return fail("bad_base64", "Header is not valid base64")

    if len(decoded_bytes) > 4096:
        return fail("bad_json", "Decoded JSON exceeds maximum size limit of 4096 bytes")

    try:
        payload = json.loads(decoded_bytes.decode("utf-8"))
    except Exception:
        return fail("bad_json", "Decoded payload is not valid JSON")

    required_fields = ["domain", "address", "uri", "version", "chainId", "type", "nonce", "issuedAt", "signature"]
    for f in required_fields:
        if f not in payload or not payload[f]:
            return fail("missing_fields", f"Required field '{f}' is missing or empty")

    sig_scheme = payload.get("signatureScheme") or payload.get("type")
    if sig_scheme == "eip1271":
        return fail("eip1271_unsupported", "ERC-1271 signatures are not supported in this implementation")

    if payload.get("type") != "eip191":
        return fail("unsupported_type", f"Unsupported signature type: {payload.get('type')}")

    from urllib.parse import urlparse
    parsed_expected = urlparse(expected_resource_uri)
    expected_domain = parsed_expected.hostname or ""

    if payload["domain"] != expected_domain:
        return fail("domain_mismatch", f"Payload domain '{payload['domain']}' does not match expected '{expected_domain}'")

    if payload["uri"] != expected_resource_uri:
        return fail("uri_mismatch", f"Payload URI '{payload['uri']}' does not match expected '{expected_resource_uri}'")

    if "resources" in payload:
        if expected_resource_uri not in payload["resources"]:
            return fail("resource_mismatch", f"Expected URI '{expected_resource_uri}' not found in resources list")

    now = datetime.now(timezone.utc)
    
    try:
        issued_at_dt = parse_iso_datetime(payload["issuedAt"])
        if not issued_at_dt:
            return fail("invalid_time_format", "Failed to parse issuedAt")
        
        if issued_at_dt > now + timedelta(seconds=60):
            return fail("future_issued_at", "issuedAt is in the future")

        max_age = int(os.getenv("AXGT_AGENTLINK_MAX_AGE_SECONDS") or "300")
        if (now - issued_at_dt).total_seconds() > max_age:
            return fail("expired_issued_at", "issuedAt exceeds maximum age limit")

        if payload.get("notBefore"):
            nb_dt = parse_iso_datetime(payload["notBefore"])
            if nb_dt and now < nb_dt:
                return fail("not_yet_valid", "Current time is before notBefore constraint")

        if payload.get("expirationTime"):
            exp_dt = parse_iso_datetime(payload["expirationTime"])
            if exp_dt and now > exp_dt:
                return fail("expired", "Payload has expired")
    except Exception as e:
        return fail("invalid_time_format", f"Time parsing/validation failed: {e}")

    try:
        siwe_message = format_siwe_message(payload)
    except Exception as e:
        return fail("siwe_reconstruction_error", f"SIWE message reconstruction failed: {e}")

    from eth_account.messages import encode_defunct
    from eth_account import Account

    try:
        signable = encode_defunct(text=siwe_message)
        recovered = Account.recover_message(signable, signature=payload["signature"])
        if recovered.lower() != payload["address"].lower():
            return fail("signature_mismatch", "Recovered address does not match payload address")
    except Exception as e:
        return fail("signature_invalid", f"Signature verification failed: {e}")

    nonce = payload["nonce"]
    if payload.get("expirationTime"):
        expires_at_dt = parse_iso_datetime(payload["expirationTime"])
        expires_at = expires_at_dt.timestamp() if expires_at_dt else (issued_at_dt.timestamp() + max_age)
    else:
        expires_at = issued_at_dt.timestamp() + max_age

    uri_hash = hashlib.sha256(expected_resource_uri.encode()).hexdigest()
    inserted = record_nonce(
        nonce=nonce,
        agent_address=payload["address"],
        chain_id=payload["chainId"],
        resource_uri_hash=uri_hash,
        issued_at=issued_at_dt.timestamp(),
        expires_at=expires_at
    )
    if not inserted:
        return fail("replayed_nonce", "Nonce has already been used")

    verified, owner, reg_reason = verify_agent_on_chain_registry(payload["address"], payload["chainId"])
    if not verified:
        def mask_addr(addr):
            return f"{addr[:6]}...{addr[-4:]}" if addr else "None"
        
        logger.warning(
            "agentlink_verifier: Registry verification failed. Agent: %s, Owner: %s, Reason: %s",
            mask_addr(payload["address"]),
            mask_addr(owner),
            reg_reason
        )
        return fail(reg_reason, f"Registry lookup failed or not active: {reg_reason}")

    return {
        "verified": True,
        "agent": payload["address"],
        "owner": owner,
        "chainId": payload["chainId"],
        "reason": "active"
    }
