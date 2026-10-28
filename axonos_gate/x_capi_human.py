"""Human-only attribution boundaries; never authorize or modify payments.

The exact bearer is captured before business work. Optional bounded CAPI tasks
revalidate it after commit, without token issuance, refresh, or schema setup.
"""
import math
import re


def authorize_exact_bearer(token, wallet, connect):
    """Validation-only transaction, serialized against exact-row revocation.

    No financial operation runs under this row lock. Unknown/lost commit outcome
    drops attribution. No database diagnostics or credentials escape this helper.
    """
    wallet = str(wallet or '').strip().lower()
    if not isinstance(token, str) or not token or not re.fullmatch(r'0x[0-9a-f]{40}', wallet):
        return False
    conn = None
    try:
        conn = connect()
        if conn is None:
            return False
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '1000ms'")
            cur.execute("SET LOCAL statement_timeout = '2000ms'")
            cur.execute("""SELECT status, expires_at, grace_until
                FROM axgt_auth_tokens WHERE token = %s AND wallet_address = %s
                FOR UPDATE""", (token, wallet))
            row = cur.fetchone()
            if not row or row[0] not in ('current', 'grace'):
                conn.rollback()
                return False
            deadline = float(row[1] if row[0] == 'current' else row[2])
            if not math.isfinite(deadline):
                conn.rollback()
                return False
            cur.execute('SELECT EXTRACT(EPOCH FROM clock_timestamp()) < %s', (deadline,))
            if cur.fetchone()[0] is not True:
                conn.rollback()
                return False
        conn.commit()
        return True
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def manual_usdc_context(context, chain_id, token_address, logs):
    """Exclude agent ERC-3009 receipts even when claimed through manual APIs.

    Reuse already-fetched receipt evidence; no new RPC, ledger or settlement.
    Unclassifiable metadata suppresses marketing only. Smart-account transfers
    remain eligible; transaction sender need not be the USDC token sender.
    """
    if context is None or chain_id != 8453:
        return None
    try:
        from eth_utils import keccak
        authorization_used = '0x' + keccak(text='AuthorizationUsed(address,bytes32)').hex()
        if not isinstance(logs, list):
            return None
        for event in logs:
            if not isinstance(event, dict):
                return None
            if str(event.get('address', '')).lower() != token_address.lower():
                continue
            topics = event.get('topics')
            if not isinstance(topics, list) or not topics or not isinstance(topics[0], str):
                return None
            if topics[0].lower() == authorization_used:
                return None
        return context
    except Exception:
        return None
