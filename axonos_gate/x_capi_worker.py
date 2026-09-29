#!/usr/bin/env python3
"""Independent, bounded X Ads Conversion API outbox worker."""

from __future__ import annotations

import email.utils
import fcntl
import hashlib
import hmac
import http.client
import json
import logging
import math
import os
import random
import re
import select
import signal
import socket
import ssl
import stat
import struct
import sys
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Dict, Iterable, Mapping, Optional

try:
    from . import x_capi
except ImportError:
    import x_capi

logger = logging.getLogger("x_capi_worker")
TOTAL_REQUEST_SECONDS = 20.0
HTTP_SOCKET_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 16 * 1024
X_API_HOST = "ads-api.x.com"
X_API_PATH = "/12/measurement/conversions/"
LEASE_SECONDS = 45
WORKER_STATEMENT_TIMEOUT_MS = 5_000
WORKER_LOCK_TIMEOUT_MS = 500
CLEANUP_STATEMENT_TIMEOUT_MS = 750
EXPECTED_SCHEMA_VERSION = 4
SCHEMA_ATTESTATION_TTL_SECONDS = 30.0
MIN_ATTEMPT_INTERVAL_SECONDS = 0.25
LIVE_DELIVERY_BLOCK_REASON = x_capi.LIVE_MODE_UNAVAILABLE_REASON
INGEST_MAX_BYTES = 8 * 1024
INGEST_BATCH_SIZE = 100
INGEST_DISPATCH_DRAIN_LIMIT = 1000
DEFAULT_INGEST_SOCKET = "/run/axonos-x-capi/events.sock"
INGEST_ACCEPTANCE_SUFFIX = ".accept"
INGEST_ACCEPTANCE_MAGIC = b"AXCIA001"
CONSENT_MAX_BYTES = 4 * 1024
CONSENT_BATCH_SIZE = 32
DEFAULT_CONSENT_SOCKET = "/run/axonos-x-capi/consent.sock"
CONFIG_GUARD_ATTESTATION = "/run/axonos-x-capi/config-guard.json"
CONFIG_GUARD_ATTESTATION_MAX_BYTES = 1024
CONFIG_GUARD_LOCK_NAME = "config-guard.lock"
CONFIG_GUARD_LOCK_MAGIC = b"AXCGL001"
DEFAULT_PRIVACY_FENCE_DIR = "/run/axonos-x-capi-privacy"
PRIVACY_FENCE_LOCK_NAME = "dispatch.lock"
PRIVACY_FENCE_LOCK_MAGIC = b"AXCPF001"
PRIVACY_GLOBAL_NAME = "global-quarantine"
PRIVACY_GLOBAL_INACTIVE = b"AXCPQ000"
PRIVACY_GLOBAL_ACTIVE = b"AXCPQ001"
PRIVACY_PENDING_SLOT_COUNT = 64
PRIVACY_PENDING_SLOT_PREFIX = "pending-"
PRIVACY_PENDING_SLOT_RECORD_BYTES = 138
PRIVACY_PENDING_SLOT_INACTIVE = b"I:" + (b"0" * 136)
PRIVACY_PENDING_SLOT_PENDING_PREFIX = b"P:"
PRIVACY_MARKER_MAX_BYTES = 512
PRIVACY_MARKER_BATCH_SIZE = 256
PRIVACY_MARKER_CAP = 1_024
_QUEUE_CAPACITY_LOCK = 8_684_972_042
_CONFIG_GUARD_LOCK = 8_684_972_043
_stop = False
_schema_attestation_lock = threading.Lock()
_schema_attestation_key: Optional[tuple[str, str, str]] = None
_schema_attestation_until = 0.0

# Canonical, schema-name-independent catalog representation of the reviewed v4
# objects.  The digest is intentionally compiled into the worker: trusting a
# mutable "schema version" row would let an existing trigger, rewrite rule,
# RLS policy, or weakened constraint survive a grants-only migration.
_EXPECTED_SCHEMA_CATALOG_SHA256 = (
    "7bd320a856a20d3c73d904a8b78f4f14f9c194de2e938bbfb563ed686c439195"
)
_SCHEMA_CATALOG_SQL = r"""
WITH target AS (
    SELECT c.relnamespace AS oid
      FROM pg_class c
     WHERE c.oid='x_capi_schema_meta'::regclass
), catalog_items(item) AS (
    SELECT jsonb_build_array(
               'relation',c.relname,c.relkind,c.relpersistence,
               c.relrowsecurity,c.relforcerowsecurity,c.relispartition,
               c.relhasrules,c.relhastriggers,c.relreplident,c.reloftype,
               COALESCE(am.amname,''),
               COALESCE((SELECT array_agg(option ORDER BY option)
                           FROM unnest(c.reloptions) option),ARRAY[]::text[])
           )::text
      FROM pg_class c
      LEFT JOIN pg_am am ON am.oid=c.relam
     WHERE c.relnamespace=(SELECT oid FROM target)
       AND left(c.relname,7)='x_capi_'
    UNION ALL
    SELECT jsonb_build_array(
               'column',c.relname,a.attnum,a.attname,
               format_type(a.atttypid,a.atttypmod),a.attnotnull,
               COALESCE(pg_get_expr(d.adbin,d.adrelid),''),
               a.attidentity,a.attgenerated,a.attstorage,
               COALESCE(a.attcompression,''),
               CASE WHEN a.attcollation=0 THEN '' ELSE
                    (SELECT n.nspname||'.'||coll.collname
                       FROM pg_collation coll
                       JOIN pg_namespace n ON n.oid=coll.collnamespace
                      WHERE coll.oid=a.attcollation) END
           )::text
      FROM pg_class c
      JOIN pg_attribute a ON a.attrelid=c.oid
      LEFT JOIN pg_attrdef d
        ON d.adrelid=a.attrelid AND d.adnum=a.attnum
     WHERE c.relnamespace=(SELECT oid FROM target)
       AND c.relkind IN ('r','p') AND left(c.relname,7)='x_capi_'
       AND a.attnum>0 AND NOT a.attisdropped
    UNION ALL
    SELECT jsonb_build_array(
               'dropped-column',c.relname,a.attnum
           )::text
      FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
     WHERE c.relnamespace=(SELECT oid FROM target)
       AND c.relkind IN ('r','p') AND left(c.relname,7)='x_capi_'
       AND a.attnum>0 AND a.attisdropped
    UNION ALL
    SELECT jsonb_build_array(
               'constraint',c.relname,con.conname,con.contype,
               COALESCE(f.relname,''),pg_get_constraintdef(con.oid,false),
               con.condeferrable,con.condeferred,con.convalidated,
               con.connoinherit,con.confupdtype,con.confdeltype,
               con.confmatchtype
           )::text
      FROM pg_constraint con
      JOIN pg_class c ON c.oid=con.conrelid
      LEFT JOIN pg_class f ON f.oid=con.confrelid
     WHERE c.relnamespace=(SELECT oid FROM target)
       AND left(c.relname,7)='x_capi_'
    UNION ALL
    SELECT jsonb_build_array(
               'index',t.relname,i.relname,am.amname,ix.indisunique,
               ix.indisprimary,ix.indisexclusion,ix.indimmediate,
               ix.indisvalid,ix.indisready,ix.indislive,ix.indisreplident,
               ix.indnatts,ix.indnkeyatts,
               ARRAY(SELECT pg_get_indexdef(ix.indexrelid,n,false)
                       FROM generate_series(1,ix.indnatts) n),
               COALESCE(pg_get_expr(ix.indpred,ix.indrelid,false),''),
               COALESCE((SELECT array_agg(option ORDER BY option)
                           FROM unnest(i.reloptions) option),ARRAY[]::text[])
           )::text
      FROM pg_index ix
      JOIN pg_class i ON i.oid=ix.indexrelid
      JOIN pg_class t ON t.oid=ix.indrelid
      JOIN pg_am am ON am.oid=i.relam
     WHERE t.relnamespace=(SELECT oid FROM target)
       AND left(t.relname,7)='x_capi_'
    UNION ALL
    SELECT jsonb_build_array(
               'trigger',c.relname,t.tgname,t.tgenabled,
               pg_get_triggerdef(t.oid,false)
           )::text
      FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
     WHERE c.relnamespace=(SELECT oid FROM target)
       AND left(c.relname,7)='x_capi_' AND NOT t.tgisinternal
    UNION ALL
    SELECT jsonb_build_array(
               'internal-trigger',c.relname,COALESCE(con.conname,''),
               p.proname,t.tgtype,t.tgenabled,t.tgdeferrable,
               t.tginitdeferred
           )::text
      FROM pg_trigger t
      JOIN pg_class c ON c.oid=t.tgrelid
      JOIN pg_proc p ON p.oid=t.tgfoid
      LEFT JOIN pg_constraint con ON con.oid=t.tgconstraint
     WHERE c.relnamespace=(SELECT oid FROM target)
       AND left(c.relname,7)='x_capi_' AND t.tgisinternal
    UNION ALL
    SELECT jsonb_build_array(
               'policy',c.relname,p.polname,p.polcmd,p.polpermissive,
               p.polroles::text,COALESCE(pg_get_expr(p.polqual,p.polrelid),''),
               COALESCE(pg_get_expr(p.polwithcheck,p.polrelid),'')
           )::text
      FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
     WHERE c.relnamespace=(SELECT oid FROM target)
       AND left(c.relname,7)='x_capi_'
    UNION ALL
    SELECT jsonb_build_array(
               'rule',c.relname,r.rulename,r.ev_type,r.ev_enabled,
               r.is_instead,pg_get_ruledef(r.oid,false)
           )::text
      FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
     WHERE c.relnamespace=(SELECT oid FROM target)
       AND left(c.relname,7)='x_capi_'
    UNION ALL
    SELECT jsonb_build_array(
               'inheritance',child_ns.nspname,child.relname,
               parent_ns.nspname,parent.relname,i.inhseqno,i.inhdetachpending
           )::text
      FROM pg_inherits i
      JOIN pg_class child ON child.oid=i.inhrelid
      JOIN pg_class parent ON parent.oid=i.inhparent
      JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace
      JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace
     WHERE (
           child.relnamespace=(SELECT oid FROM target)
           AND left(child.relname,7)='x_capi_'
       ) OR (
           parent.relnamespace=(SELECT oid FROM target)
           AND left(parent.relname,7)='x_capi_'
       )
    UNION ALL
    SELECT jsonb_build_array(
               'publication',c.relname,p.pubname,
               COALESCE(pg_get_expr(pr.prqual,pr.prrelid),''),
               pr.prattrs::text
           )::text
      FROM pg_publication_rel pr
      JOIN pg_class c ON c.oid=pr.prrelid
      JOIN pg_publication p ON p.oid=pr.prpubid
     WHERE c.relnamespace=(SELECT oid FROM target)
       AND left(c.relname,7)='x_capi_'
    UNION ALL
    SELECT jsonb_build_array('publication-all-tables',p.pubname)::text
      FROM pg_publication p WHERE p.puballtables
)
SELECT COALESCE(
    string_agg(item,E'\n' ORDER BY item COLLATE "C"),''
) FROM catalog_items
"""


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class RequestsTransport:
    """Dedicated-token HTTPS adapter; the existing activation block still applies.

    The historical class name is retained for the test-injection boundary. The
    standard-library client has no ambient proxy, netrc, cookie, redirect, or
    default User-Agent behavior. No network client or token is cached.
    """

    def __init__(self):
        # Do not construct a network client or ambient cookie/proxy state.
        self._session = None

    def send(self, pixel_id: str, token: str, payload: Dict[str, Any]) -> TransportResponse:
        cfg = x_capi.load_config()
        if cfg.mode != "live" or not cfg.producer_ready or cfg.errors:
            raise RuntimeError(LIVE_DELIVERY_BLOCK_REASON)
        body = _serialize_request(pixel_id, payload, cfg)
        # Read only the protected worker file, including for direct adapter
        # callers. A supplied/environment token cannot replace that source.
        runtime_token, error = read_token()
        if (
            error or runtime_token is None or type(token) is not str
            or not hmac.compare_digest(token.encode("utf-8"), runtime_token.encode("ascii"))
        ):
            raise ValueError("invalid_transport_token")
        return _post_conversion(pixel_id, runtime_token, body)


@contextmanager
def _http_deadline():
    """Bound even DNS and slow-drip I/O without leaving a sender behind.

    Socket timeouts handle ordinary failures. The kernel's default SIGALRM
    action terminates a wedged worker at the absolute deadline, including when
    Python cannot run a handler. Its durable lease is recovered after restart;
    there is no thread that can send after the privacy lock is released.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("transport_requires_main_thread")
    if signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, set()):
        raise RuntimeError("transport_deadline_signal_blocked")
    if any(signal.getitimer(signal.ITIMER_REAL)):
        raise RuntimeError("transport_deadline_conflict")
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    try:
        signal.setitimer(signal.ITIMER_REAL, TOTAL_REQUEST_SECONDS)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _serialize_request(pixel_id: str, payload: Any, cfg: x_capi.Config) -> bytes:
    """Independently enforce the four-field allowlist at the HTTP boundary."""
    if (
        type(pixel_id) is not str or not x_capi._valid_vendor_id(pixel_id)
        or pixel_id != cfg.pixel_id
        or type(payload) is not dict or set(payload) != {"conversions"}
    ):
        raise ValueError("invalid_transport_payload")
    conversions = payload["conversions"]
    if type(conversions) is not list or len(conversions) != 1:
        raise ValueError("invalid_transport_payload")
    conversion = conversions[0]
    if type(conversion) is not dict or set(conversion) != {
        "conversion_timestamp", "event_id", "identifiers", "conversion_id"
    }:
        raise ValueError("invalid_transport_payload")
    identifiers = conversion["identifiers"]
    if (
        type(identifiers) is not list or len(identifiers) != 1
        or type(identifiers[0]) is not dict or set(identifiers[0]) != {"twclid"}
    ):
        raise ValueError("invalid_transport_payload")
    event_id, click_id = conversion["event_id"], identifiers[0]["twclid"]
    if (
        type(event_id) is not str or not x_capi._valid_vendor_id(event_id)
        or event_id not in cfg.event_ids.values()
        or type(click_id) is not str or x_capi.validate_twclid(click_id, cfg) != click_id
    ):
        raise ValueError("invalid_transport_payload")
    # Reconstruct primitive values instead of serializing caller-owned extras
    # or invoking arbitrary __str__/JSON hooks.
    canonical = build_payload({
        "conversion_timestamp_ms": conversion["conversion_timestamp"],
        "event_id": event_id, "twclid": click_id,
        "conversion_id": conversion["conversion_id"],
    })
    return json.dumps(canonical, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


def _tls_context() -> ssl.SSLContext:
    # Neither session-key logging nor environment-supplied trust stores are
    # deployment inputs for this secret-bearing, fixed-destination transport.
    if any(os.environ.get(name) for name in (
        "SSLKEYLOGFILE", "SSL_CERT_FILE", "SSL_CERT_DIR"
    )):
        raise ValueError("unsupported_transport_tls_environment")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    return context


def _post_conversion(pixel_id: str, token: str, body: bytes) -> TransportResponse:
    # This private I/O primitive is reached only after send's configuration,
    # payload and protected secret checks. TLS verification is never optional.
    with _http_deadline():
        connection = http.client.HTTPSConnection(
            X_API_HOST, port=443, timeout=HTTP_SOCKET_TIMEOUT_SECONDS,
            context=_tls_context(),
        )
        try:
            # Never inherit stdlib wire debugging: it includes secret headers.
            connection.set_debuglevel(0)
            connection.request(
                "POST", X_API_PATH + pixel_id, body=body,
                headers={"Content-Type": "application/json", "X-Pixel-Token": token},
            )
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
            # Retain only the one response header used by the retry policy.
            retry_after = response.getheader("Retry-After")
            headers = {"Retry-After": retry_after} if retry_after is not None else {}
            return TransportResponse(response.status, headers, response_body)
        finally:
            connection.close()


def _injected_test_transport_allowed(transport: Any) -> bool:
    """Allow delivery logic only behind two explicit, test-only boundaries."""
    return bool(
        transport is not None
        and not isinstance(transport, RequestsTransport)
        and os.getenv("X_CAPI_ALLOW_TEST_SECRETS") == "1"
    )


def _conversion_timestamp(timestamp_ms: Any) -> int:
    """Keep the immutable Unix milliseconds required by the token contract."""
    if type(timestamp_ms) is not int:
        raise ValueError("invalid_conversion_timestamp_ms")
    # PostgreSQL already enforces >0; repeat it at the serialization boundary.
    # Retain the reviewed year-9999 bound at the serialization boundary.
    if not 0 < timestamp_ms <= 253_402_300_799_999:
        raise ValueError("invalid_conversion_timestamp_ms")
    return timestamp_ms


def build_payload(job: Mapping[str, Any]) -> Dict[str, Any]:
    """Strict documented allowlist: exactly four conversion fields."""
    event_id, click_id = job["event_id"], job["twclid"]
    conversion_id = job["conversion_id"]
    if type(conversion_id) is uuid.UUID:
        conversion_id = str(conversion_id)
    if type(event_id) is not str or type(click_id) is not str or type(conversion_id) is not str:
        raise ValueError("invalid_conversion_fields")
    try:
        parsed_id = uuid.UUID(conversion_id)
        if parsed_id.version != 4 or str(parsed_id) != conversion_id:
            raise ValueError
    except ValueError:
        raise ValueError("invalid_conversion_id") from None
    conversion = {
        "conversion_timestamp": _conversion_timestamp(
            job["conversion_timestamp_ms"]
        ),
        "event_id": event_id,
        "identifiers": [{"twclid": click_id}],
        "conversion_id": conversion_id,
    }
    if set(conversion) != {
        "conversion_timestamp", "event_id", "identifiers", "conversion_id"
    } or set(conversion["identifiers"][0]) != {"twclid"}:
        raise AssertionError("outbound allowlist violated")
    return {"conversions": [conversion]}


def _token_path() -> str:
    return (os.getenv("X_CAPI_ACCESS_TOKEN_FILE") or "/run/secrets/x_capi_access_token").strip()


def _read_private_file(
    path: str,
    *,
    label: str,
    min_bytes: int,
    max_bytes: int,
) -> tuple[Optional[str], Optional[str]]:
    """Open a single-owner regular secret without symlink or stat/open races."""
    try:
        secrets_root = os.path.realpath("/run/secrets")
        candidate = os.path.abspath(path)
        canonical = os.path.realpath(candidate)
        if (
            canonical != candidate
            or os.path.commonpath((secrets_root, canonical)) != secrets_root
        ):
            return None, f"{label} file must be mounted below /run/secrets"
    except (OSError, ValueError):
        return None, f"{label} file path is invalid"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(candidate, flags)
    except OSError:
        return None, f"dedicated {label} file is unreadable"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, f"dedicated {label} file must be regular"
        if info.st_uid not in (0, os.geteuid()):
            return None, f"dedicated {label} file has an unexpected owner"
        if info.st_mode & 0o077:
            return None, f"{label} file permissions must be 0600 or stricter"
        if info.st_nlink != 1:
            return None, f"dedicated {label} file must not be hard-linked"
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
    except OSError:
        return None, f"dedicated {label} file is unreadable"
    finally:
        os.close(fd)
    if len(value) > max_bytes:
        return None, f"dedicated {label} file is invalid"
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError:
        return None, f"dedicated {label} file is invalid"
    if not min_bytes <= len(decoded) <= max_bytes:
        return None, f"dedicated {label} file is invalid"
    if any(not 0x21 <= ord(character) <= 0x7E for character in decoded):
        return None, f"dedicated {label} file is invalid"
    return decoded, None


def read_token() -> tuple[Optional[str], Optional[str]]:
    if os.getenv("X_CAPI_ACCESS_TOKEN"):
        return None, "environment token source is prohibited outside isolated tests"
    return _read_private_file(
        _token_path(), label="token", min_bytes=16, max_bytes=8192
    )


def _configure_connection(conn) -> None:
    with conn.cursor() as cur:
        if os.getenv("X_CAPI_ALLOW_TEST_DB_URL") != "1":
            # Do not list pg_catalog explicitly after a writable schema: when
            # it is omitted PostgreSQL searches it implicitly first, while
            # unqualified X CAPI relations still resolve in public.
            cur.execute("SET search_path=public")
        cur.execute("SET statement_timeout=%s", (WORKER_STATEMENT_TIMEOUT_MS,))
        cur.execute("SET lock_timeout=%s", (WORKER_LOCK_TIMEOUT_MS,))
        cur.execute("SET idle_in_transaction_session_timeout=%s", (30_000,))
        cur.execute("SET application_name='axonos_x_capi_worker'")
    conn.commit()


def _worker_db_target_is_isolated() -> bool:
    """Accept only the dedicated database, host, and least-privilege login."""
    url = x_capi._db_url(worker=True)
    if not url:
        return False
    if os.getenv("X_CAPI_ALLOW_TEST_DB_URL") == "1":
        return True
    try:
        from psycopg2.extensions import parse_dsn

        if not url.startswith(("postgresql://", "postgres://")):
            return False
        parsed = parse_dsn(url)
        if set(parsed) - {
            "user", "password", "dbname", "host", "port", "sslmode"
        }:
            return False
        expected_role = (
            os.getenv("X_CAPI_WORKER_DB_ROLE") or "axonos_x_capi_worker"
        ).strip()
        expected_database = (
            os.getenv("X_CAPI_POSTGRES_DB") or "axonos_x_capi"
        ).strip()
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", expected_role)
            or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]{0,62}", expected_database
            )
        ):
            return False
        port = int(parsed.get("port") or 5432)
    except (ImportError, TypeError, ValueError):
        return False
    return (
        parsed.get("host") == "x-capi-postgres"
        and port == 5432
        and parsed.get("user") == expected_role
        and parsed.get("dbname") == expected_database
        and bool(parsed.get("password"))
        and parsed.get("sslmode", "prefer")
        in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
    )


def _schema_catalog_is_exact(cur) -> bool:
    """Reject every catalog shape other than the reviewed fresh-v4 schema."""
    try:
        cur.execute(_SCHEMA_CATALOG_SQL)
        row = cur.fetchone()
        if row is None or not isinstance(row[0], str):
            return False
        observed = hashlib.sha256(row[0].encode("utf-8")).hexdigest()
        return hmac.compare_digest(observed, _EXPECTED_SCHEMA_CATALOG_SHA256)
    except Exception:
        return False


def _database_role_is_isolated(cur) -> bool:
    """Attest the effective login and both positive and negative DB rights."""
    expected_role = (
        os.getenv("X_CAPI_WORKER_DB_ROLE") or "axonos_x_capi_worker"
    ).strip()
    expected_owner = (
        os.getenv("X_CAPI_OWNER_DB_ROLE") or "axonos_x_capi_owner"
    ).strip()
    if (
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", expected_role)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", expected_owner)
        or hmac.compare_digest(expected_role, expected_owner)
    ):
        return False
    cur.execute(
        """SELECT r.rolname,r.rolsuper,r.rolbypassrls,r.rolcreaterole,
                  r.rolcreatedb,r.rolreplication
             FROM pg_roles r WHERE r.rolname=current_user"""
    )
    identity = cur.fetchone()
    if (
        identity is None
        or not hmac.compare_digest(str(identity[0]), expected_role)
        or any(bool(value) for value in identity[1:])
    ):
        return False
    cur.execute(
        """SELECT EXISTS (
               SELECT 1 FROM pg_roles inherited
                WHERE inherited.rolname<>current_user
                  AND pg_has_role(current_user,inherited.oid,'MEMBER')
           )"""
    )
    if bool(cur.fetchone()[0]):
        return False
    cur.execute(
        """SELECT r.rolcanlogin,r.rolsuper,r.rolbypassrls,r.rolcreaterole,
                  r.rolcreatedb,r.rolreplication,
                  EXISTS (
                    SELECT 1 FROM pg_roles inherited
                     WHERE inherited.rolname<>r.rolname
                       AND pg_has_role(r.rolname,inherited.oid,'MEMBER')
                  ),
                  EXISTS (
                    SELECT 1 FROM pg_auth_members membership
                     WHERE membership.roleid=r.oid
                  )
             FROM pg_roles r WHERE r.rolname=%s""",
        (expected_owner,),
    )
    owner_identity = cur.fetchone()
    if owner_identity is None or any(bool(value) for value in owner_identity):
        return False
    cur.execute(
        """SELECT EXISTS (
               SELECT 1 FROM pg_auth_members membership
                 JOIN pg_roles target ON target.oid=membership.roleid
                WHERE target.rolname=current_user
           )"""
    )
    if bool(cur.fetchone()[0]):
        return False
    cur.execute(
        "SELECT has_database_privilege(current_user,current_database(),'CREATE')"
    )
    if bool(cur.fetchone()[0]):
        return False
    cur.execute(
        "SELECT has_database_privilege(current_user,current_database(),'TEMPORARY')"
    )
    if bool(cur.fetchone()[0]):
        return False
    cur.execute(
        """SELECT EXISTS (
               SELECT 1 FROM pg_database d
                WHERE d.datallowconn
                  AND d.datname<>current_database()
                  AND has_database_privilege(current_user,d.oid,'CONNECT')
           )"""
    )
    if bool(cur.fetchone()[0]):
        return False
    cur.execute(
        """SELECT n.oid
             FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.oid='x_capi_schema_meta'::regclass"""
    )
    schema_row = cur.fetchone()
    if schema_row is None:
        return False
    capi_schema_oid = int(schema_row[0])
    if not _schema_catalog_is_exact(cur):
        return False
    cur.execute(
        r"""SELECT count(*)=9 AND bool_and(owner.rolname=%s)
             FROM pg_class c
             JOIN pg_roles owner ON owner.oid=c.relowner
            WHERE c.relnamespace=%s AND c.relkind IN ('r','p')
              AND c.relname LIKE 'x_capi\_%%' ESCAPE chr(92)""",
        (expected_owner, capi_schema_oid),
    )
    if not bool(cur.fetchone()[0]):
        return False
    cur.execute(
        r"""SELECT EXISTS (
               SELECT 1
                 FROM pg_class c
                 JOIN pg_namespace n ON n.oid=c.relnamespace
                 CROSS JOIN LATERAL aclexplode(c.relacl) acl
                 JOIN pg_roles owner ON owner.oid=c.relowner
                WHERE n.oid=%s AND c.relkind IN ('r','p')
                  AND c.relname LIKE 'x_capi\_%%' ESCAPE chr(92)
                  AND (
                    (acl.grantee=(SELECT oid FROM pg_roles
                                   WHERE rolname=current_user)
                     AND acl.is_grantable)
                    OR acl.grantee NOT IN (
                         owner.oid,
                         (SELECT oid FROM pg_roles WHERE rolname=current_user)
                       )
                  )
           ) OR EXISTS (
               SELECT 1
                 FROM pg_class c
                 JOIN pg_namespace n ON n.oid=c.relnamespace
                 JOIN pg_attribute a ON a.attrelid=c.oid
                 CROSS JOIN LATERAL aclexplode(a.attacl) acl
                 JOIN pg_roles owner ON owner.oid=c.relowner
                WHERE n.oid=%s AND c.relkind IN ('r','p')
                  AND c.relname LIKE 'x_capi\_%%' ESCAPE chr(92)
                  AND a.attnum>0 AND NOT a.attisdropped
                  AND (
                    (acl.grantee=(SELECT oid FROM pg_roles
                                   WHERE rolname=current_user)
                     AND acl.is_grantable)
                    OR acl.grantee NOT IN (
                         owner.oid,
                         (SELECT oid FROM pg_roles WHERE rolname=current_user)
                       )
                  )
           )""",
        (capi_schema_oid, capi_schema_oid),
    )
    if bool(cur.fetchone()[0]):
        return False
    # A worker that can create persistent schema objects or touch any ordinary
    # non-CAPI relation is not least-privileged, even if its positive CAPI
    # grants happen to be sufficient.
    cur.execute(
        """SELECT EXISTS (
               SELECT 1 FROM pg_namespace n
                WHERE n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                  AND n.nspname<>'information_schema'
                  AND has_schema_privilege(current_user,n.oid,'CREATE')
           )"""
    )
    if bool(cur.fetchone()[0]):
        return False
    cur.execute(
        """SELECT EXISTS (
               SELECT 1
                 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE c.relkind IN ('r','p','v','m','f')
                  AND n.nspname NOT LIKE 'pg\\_%%' ESCAPE '\\'
                  AND n.nspname<>'information_schema'
                  AND NOT (
                      n.oid=%s AND c.relname IN (
                        'x_capi_schema_meta','x_capi_config_guard',
                        'x_capi_attribution_contexts',
                        'x_capi_revocation_tombstones','x_capi_capacity',
                        'x_capi_dedup','x_capi_outbox','x_capi_counters',
                        'x_capi_worker_state'
                      )
                  )
                  AND (
                    has_table_privilege(
                        current_user,c.oid,
                        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                    )
                    OR has_any_column_privilege(
                        current_user,c.oid,'SELECT,INSERT,UPDATE,REFERENCES'
                    )
                  )
           )""",
        (capi_schema_oid,),
    )
    if bool(cur.fetchone()[0]):
        return False
    cur.execute(
        """SELECT EXISTS (
               SELECT 1
                 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE c.relkind='S'
                  AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                  AND n.nspname<>'information_schema'
                  AND has_sequence_privilege(
                      current_user,c.oid,'USAGE,SELECT,UPDATE'
                  )
           )"""
    )
    if bool(cur.fetchone()[0]):
        return False
    cur.execute(
        """SELECT EXISTS (
               SELECT 1 FROM pg_proc p
                 JOIN pg_namespace n ON n.oid=p.pronamespace
                WHERE p.prosecdef
                  AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                  AND n.nspname<>'information_schema'
                  AND has_function_privilege(current_user,p.oid,'EXECUTE')
           )"""
    )
    if bool(cur.fetchone()[0]):
        return False
    cur.execute(
        """SELECT EXISTS (
               SELECT 1 FROM pg_largeobject_metadata object
                WHERE object.lomowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
                   OR EXISTS (
                      SELECT 1 FROM aclexplode(COALESCE(object.lomacl,'{}')) acl
                       WHERE acl.grantee IN (
                           0,(SELECT oid FROM pg_roles WHERE rolname=current_user)
                       )
                   )
           )"""
    )
    if bool(cur.fetchone()[0]):
        return False

    # Check the complete effective privilege matrix, not just the positive
    # grants the worker needs.  This makes ACL drift (for example a table-wide
    # SELECT or TRUNCATE added after migration) a readiness failure before any
    # ingest or dispatch is accepted.
    allowed_table_privileges = {
        "x_capi_schema_meta": frozenset(),
        "x_capi_config_guard": frozenset(("SELECT", "INSERT", "UPDATE")),
        "x_capi_attribution_contexts": frozenset(("DELETE",)),
        "x_capi_revocation_tombstones": frozenset(
            ("SELECT", "INSERT", "UPDATE", "DELETE")
        ),
        "x_capi_capacity": frozenset(("SELECT", "UPDATE")),
        "x_capi_dedup": frozenset(("SELECT", "INSERT", "DELETE")),
        "x_capi_outbox": frozenset(("DELETE",)),
        "x_capi_counters": frozenset(("SELECT", "INSERT", "UPDATE")),
        "x_capi_worker_state": frozenset(("SELECT", "INSERT", "UPDATE")),
    }
    table_privileges = (
        "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE",
        "REFERENCES", "TRIGGER",
    )
    for relation_name, allowed in allowed_table_privileges.items():
        for privilege in table_privileges:
            cur.execute(
                "SELECT has_table_privilege(current_user,%s,%s)",
                (relation_name, privilege),
            )
            if bool(cur.fetchone()[0]) != (privilege in allowed):
                return False

    allowed_column_privileges = {
        ("x_capi_schema_meta", "SELECT"): ("singleton", "schema_version"),
        ("x_capi_attribution_contexts", "SELECT"): (
            "id", "handle_hash", "csrf_hash", "consent_state", "mode_scope",
            "policy_version", "policy_epoch", "lifecycle_expires_at", "twclid",
            "consented_at", "expires_at", "audience_scope", "wallet_hash",
            "wallet_bound_at", "first_seen_at", "updated_at",
        ),
        ("x_capi_attribution_contexts", "INSERT"): (
            "id", "handle_hash", "csrf_hash", "consent_state", "mode_scope",
            "policy_version", "policy_epoch", "lifecycle_expires_at", "twclid",
            "consented_at", "audience_scope", "expires_at", "wallet_hash",
            "wallet_bound_at", "first_seen_at", "updated_at",
        ),
        ("x_capi_attribution_contexts", "UPDATE"): (
            "consent_state", "twclid", "declined_at", "revoked_at", "expires_at",
            "wallet_hash", "wallet_bound_at", "updated_at",
        ),
        ("x_capi_dedup", "UPDATE"): ("expires_at",),
        ("x_capi_outbox", "SELECT"): (
            "conversion_id", "milestone", "pixel_id", "event_id",
            "conversion_timestamp_ms", "twclid", "context_id",
            "consent_policy_version", "consent_policy_epoch",
            "consent_audience_scope", "attribution_expires_at", "mode_scope",
            "status", "attempt_count", "next_attempt_at", "lease_owner",
            "lease_token", "lease_expires_at", "created_at", "updated_at",
            "last_error_code",
        ),
        ("x_capi_outbox", "INSERT"): (
            "conversion_id", "milestone", "source_key_hash", "mode_scope",
            "pixel_id", "event_id", "conversion_timestamp_ms", "twclid",
            "context_id", "consent_policy_version", "consent_policy_epoch",
            "consent_audience_scope", "attribution_expires_at", "status",
            "attempt_count", "next_attempt_at", "created_at", "updated_at",
        ),
        ("x_capi_outbox", "UPDATE"): (
            "status", "attempt_count", "next_attempt_at", "lease_owner",
            "lease_token", "lease_expires_at", "accepted_at",
            "last_error_code", "safe_debug_id", "twclid", "updated_at",
        ),
    }
    for relation_name, table_allowed in allowed_table_privileges.items():
        cur.execute(
            """SELECT attname
                 FROM pg_attribute
                WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped
                ORDER BY attnum""",
            (relation_name,),
        )
        column_names = tuple(str(row[0]) for row in cur.fetchall())
        if not column_names:
            return False
        for column_name in column_names:
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                allowed_columns = allowed_column_privileges.get(
                    (relation_name, privilege), ()
                )
                expected = (
                    privilege in table_allowed or column_name in allowed_columns
                )
                cur.execute(
                    "SELECT has_column_privilege(current_user,%s,%s,%s)",
                    (relation_name, column_name, privilege),
                )
                if bool(cur.fetchone()[0]) != expected:
                    return False

    # Every explicitly required column must exist; otherwise the loop above
    # could mistake a misspelled/removed grant declaration for least privilege.
    for (relation_name, privilege), required_names in (
        allowed_column_privileges.items()
    ):
        for column_name in required_names:
            cur.execute(
                """SELECT EXISTS (
                       SELECT 1 FROM pg_attribute
                        WHERE attrelid=%s::regclass AND attname=%s
                          AND attnum>0 AND NOT attisdropped
                   )""",
                (relation_name, column_name),
            )
            if not bool(cur.fetchone()[0]):
                return False
    return True


def _schema_ready(conn) -> bool:
    global _schema_attestation_key, _schema_attestation_until
    expected_role = (
        os.getenv("X_CAPI_WORKER_DB_ROLE") or "axonos_x_capi_worker"
    ).strip()
    expected_owner = (
        os.getenv("X_CAPI_OWNER_DB_ROLE") or "axonos_x_capi_owner"
    ).strip()
    db_source_fingerprint = x_capi._hash(x_capi._db_url(worker=True) or "")
    cache_key = (expected_role, expected_owner, db_source_fingerprint)
    with _schema_attestation_lock:
        if (
            cache_key == _schema_attestation_key
            and time.monotonic() < _schema_attestation_until
        ):
            return True
        try:
            with conn.cursor() as cur:
                if not _database_role_is_isolated(cur):
                    conn.rollback()
                    return False
                cur.execute(
                    "SELECT schema_version FROM x_capi_schema_meta WHERE singleton=TRUE"
                )
                row = cur.fetchone()
                if not row or int(row[0]) != EXPECTED_SCHEMA_VERSION:
                    conn.rollback()
                    return False
                # These zero-row queries are also a direct privilege/readiness test.
                cur.execute(
                    """SELECT max_policy_epoch,deployment_id_hash,mode_scope,
                              policy_version,audience_scope,hash_key_fingerprint,
                              context_key_fingerprint
                         FROM x_capi_config_guard WHERE FALSE"""
                )
                cur.execute(
                    """SELECT conversion_id, lease_owner, lease_token, status
                         FROM x_capi_outbox WHERE FALSE"""
                )
                cur.execute(
                    """SELECT id, handle_hash, csrf_hash, consent_state, mode_scope, twclid,
                              policy_epoch, audience_scope, lifecycle_expires_at,
                              wallet_hash FROM x_capi_attribution_contexts WHERE FALSE"""
                )
                cur.execute(
                    """SELECT handle_hash,csrf_hash,lifecycle_expires_at
                         FROM x_capi_revocation_tombstones WHERE FALSE"""
                )
                cur.execute(
                    """SELECT context_count,tombstone_count,outbox_active_count,
                              revocation_saturated,revocation_saturated_until
                         FROM x_capi_capacity WHERE singleton=TRUE"""
                )
                if cur.fetchone() is None:
                    conn.rollback()
                    return False
                cur.execute(
                    """SELECT lifecycle_state,lifecycle_token,ticket_not_before
                         FROM x_capi_worker_state WHERE FALSE"""
                )
            conn.commit()
            _schema_attestation_key = cache_key
            _schema_attestation_until = (
                time.monotonic() + SCHEMA_ATTESTATION_TTL_SECONDS
            )
            return True
        except Exception:
            conn.rollback()
            _schema_attestation_key = None
            _schema_attestation_until = 0.0
            return False


def _deployment_id_hash(cfg: x_capi.Config) -> str:
    return x_capi._hash(str(cfg.deployment_id))


def _hash_key_fingerprint() -> Optional[str]:
    """Return a one-way generation ID without exposing worker HMAC material."""
    return x_capi.keyed_internal_hash(
        "config-guard-hash-key", "AxonOS X CAPI immutable HMAC key v1"
    )


def _context_key_fingerprint() -> Optional[str]:
    helper = getattr(x_capi, "primary_context_key_fingerprint", None)
    if not callable(helper):
        return None
    fingerprint = helper()
    return (
        str(fingerprint)
        if isinstance(fingerprint, str)
        and re.fullmatch(r"[0-9a-f]{64}", fingerprint)
        else None
    )


def _config_guard_attestation_document(row) -> Dict[str, Any]:
    """Return the exact, identifier-free public view of the durable guard."""
    if row is None:
        return {"configured": False, "v": 1}
    document = {
        "audience_scope": str(row[4]),
        "configured": True,
        "context_key_fingerprint": str(row[6]),
        "deployment_id_hash": str(row[1]),
        "max_policy_epoch": int(row[0]),
        "mode_scope": str(row[2]),
        "policy_version": str(row[3]),
        "v": 1,
    }
    if (
        isinstance(row[0], bool)
        or not 1 <= document["max_policy_epoch"] <= 2_147_483_647
        or not re.fullmatch(r"[0-9a-f]{64}", document["deployment_id_hash"])
        or document["mode_scope"] not in ("dry_run", "live")
        or not x_capi._POLICY_RE.fullmatch(document["policy_version"])
        or not re.fullmatch(r"[0-9a-f]{64}", document["audience_scope"])
        or not re.fullmatch(
            r"[0-9a-f]{64}", document["context_key_fingerprint"]
        )
    ):
        raise ValueError("invalid durable config guard")
    return document


def _parse_config_guard_attestation(raw: bytes) -> Optional[Dict[str, Any]]:
    """Strictly validate a previously published monotonic high-water file."""
    if not raw or len(raw) > CONFIG_GUARD_ATTESTATION_MAX_BYTES:
        return None
    try:
        document = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("v") != 1:
        return None
    if document.get("configured") is False:
        if set(document) == {"v", "configured"}:
            expected = _config_guard_attestation_document(None)
        elif set(document) == {"v", "configured", "high_water"}:
            high_water = document.get("high_water")
            if not isinstance(high_water, dict) or high_water.get("configured") is not True:
                return None
            try:
                validated = _config_guard_attestation_document((
                    high_water["max_policy_epoch"],
                    high_water["deployment_id_hash"], high_water["mode_scope"],
                    high_water["policy_version"], high_water["audience_scope"],
                    "0" * 64, high_water["context_key_fingerprint"],
                ))
            except (KeyError, TypeError, ValueError):
                return None
            if high_water != validated:
                return None
            expected = {"configured": False, "high_water": validated, "v": 1}
        else:
            return None
    elif document.get("configured") is True:
        if set(document) != {
            "v", "configured", "max_policy_epoch", "deployment_id_hash",
            "mode_scope", "policy_version", "audience_scope",
            "context_key_fingerprint",
        }:
            return None
        try:
            expected = _config_guard_attestation_document((
                document["max_policy_epoch"], document["deployment_id_hash"],
                document["mode_scope"], document["policy_version"],
                document["audience_scope"], "0" * 64,
                document["context_key_fingerprint"],
            ))
        except (KeyError, TypeError, ValueError):
            return None
    else:
        return None
    canonical = (
        json.dumps(expected, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=True).encode("ascii") + b"\n"
    )
    return expected if raw == canonical else None


def _write_config_guard_attestation(
    row, *, allow_transition_match: bool = False
) -> bool:
    """Atomically publish without ever rolling back the filesystem high-water."""
    try:
        document = _config_guard_attestation_document(row)
        path = os.path.abspath(CONFIG_GUARD_ATTESTATION)
        parent, target_name = os.path.split(path)
        if (
            not target_name
            or target_name in (".", "..")
            or os.path.realpath(parent) != parent
        ):
            return False
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_fd = os.open(parent, directory_flags)
    except (OSError, TypeError, ValueError):
        return False
    lock_fd: Optional[int] = None
    temporary_name = ".%s.%d.%s.tmp" % (
        target_name, os.getpid(), uuid.uuid4().hex,
    )
    temporary_created = False
    try:
        directory_info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid()
            or directory_info.st_mode & 0o077
        ):
            return False
        lock_flags = (
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        lock_fd = os.open(
            CONFIG_GUARD_LOCK_NAME, lock_flags, 0o600, dir_fd=directory_fd
        )
        lock_info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != os.geteuid()
            or stat.S_IMODE(lock_info.st_mode) != 0o600
            or lock_info.st_nlink != 1
        ):
            return False
        if lock_info.st_size == 0:
            if os.write(lock_fd, CONFIG_GUARD_LOCK_MAGIC) != len(
                CONFIG_GUARD_LOCK_MAGIC
            ):
                return False
            os.fsync(lock_fd)
        elif (
            lock_info.st_size != len(CONFIG_GUARD_LOCK_MAGIC)
            or os.pread(lock_fd, len(CONFIG_GUARD_LOCK_MAGIC), 0)
            != CONFIG_GUARD_LOCK_MAGIC
        ):
            return False
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return False
        try:
            target_info = os.stat(
                target_name, dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            target_info = None
        if target_info is not None and (
            not stat.S_ISREG(target_info.st_mode)
            or target_info.st_uid != os.geteuid()
            or target_info.st_mode & 0o077
            or target_info.st_nlink != 1
        ):
            return False
        existing = None
        if target_info is not None:
            read_flags = (
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            existing_fd = os.open(target_name, read_flags, dir_fd=directory_fd)
            try:
                raw = os.read(existing_fd, CONFIG_GUARD_ATTESTATION_MAX_BYTES + 1)
            finally:
                os.close(existing_fd)
            existing = _parse_config_guard_attestation(raw)
            if existing is None:
                return False
        elif document.get("configured") is True:
            # Once PostgreSQL has a guard row, loss of the independent file is
            # indistinguishable from rollback/tampering.  First activation is
            # safe because before_advance publishes an unconfigured sentinel.
            return False
        # During an advance publish an intentionally gate-invalid transition
        # document that retains the prior high-water.  A crash can therefore
        # neither leave the old generation active nor erase rollback evidence.
        existing_high_water = None
        if existing is not None:
            existing_high_water = (
                existing if existing.get("configured") is True
                else existing.get("high_water")
            )
        if document.get("configured") is not True and existing_high_water is not None:
            if existing.get("configured") is False:
                return True
            document = {
                "configured": False,
                "high_water": existing_high_water,
                "v": 1,
            }
        elif existing_high_water is not None and document.get("configured") is True:
            old_epoch = int(existing_high_water["max_policy_epoch"])
            new_epoch = int(document["max_policy_epoch"])
            if new_epoch < old_epoch:
                return False
            if new_epoch == old_epoch:
                # A configured exact match is an idempotent no-op.  A
                # transition marker at this epoch is ambiguous (the DB may
                # have been restored), so it can only be retired by a newer
                # policy generation.
                if document != existing_high_water:
                    return False
                return bool(
                    existing.get("configured") is True
                    or allow_transition_match
                )
        payload = (
            json.dumps(
                document, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            + b"\n"
        )
        if len(payload) > CONFIG_GUARD_ATTESTATION_MAX_BYTES:
            return False
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        file_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        temporary_created = True
        try:
            os.fchmod(file_fd, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(file_fd, remaining)
                if written <= 0:
                    raise OSError("short config attestation write")
                remaining = remaining[written:]
            os.fsync(file_fd)
            file_info = os.fstat(file_fd)
            if (
                not stat.S_ISREG(file_info.st_mode)
                or file_info.st_uid != os.geteuid()
                or stat.S_IMODE(file_info.st_mode) != 0o600
                or file_info.st_nlink != 1
            ):
                return False
        finally:
            os.close(file_fd)
        os.replace(
            temporary_name, target_name,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
        temporary_created = False
        os.fsync(directory_fd)
        final_info = os.stat(
            target_name, dir_fd=directory_fd, follow_symlinks=False
        )
        return bool(
            stat.S_ISREG(final_info.st_mode)
            and final_info.st_uid == os.geteuid()
            and stat.S_IMODE(final_info.st_mode) == 0o600
            and final_info.st_nlink == 1
        )
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
        os.close(directory_fd)


def _publish_durable_config_guard(conn) -> bool:
    """Publish only a row read while holding the DB generation fence."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_CONFIG_GUARD_LOCK,))
            cur.execute(
                """SELECT max_policy_epoch,deployment_id_hash,mode_scope,
                          policy_version,audience_scope,hash_key_fingerprint,
                          context_key_fingerprint
                     FROM x_capi_config_guard
                    WHERE singleton=TRUE FOR UPDATE"""
            )
            row = cur.fetchone()
            if not _write_config_guard_attestation(row):
                conn.rollback()
                return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


def _config_guard_on_cursor(
    cur,
    *,
    policy_epoch: int,
    deployment_id_hash: str,
    mode_scope: str,
    policy_version: str,
    audience_scope: str,
    hash_key_fingerprint: str,
    context_key_fingerprint: str,
    now: float,
    allow_advance: bool,
    require_lock: bool = True,
    on_observed=None,
    before_advance=None,
) -> bool:
    """Atomically enforce the irreversible active-configuration high-water."""
    if (
        isinstance(policy_epoch, bool)
        or not 1 <= int(policy_epoch) <= 2_147_483_647
        or not re.fullmatch(r"[0-9a-f]{64}", deployment_id_hash)
        or mode_scope not in ("dry_run", "live")
        or not x_capi._POLICY_RE.fullmatch(str(policy_version))
        or not re.fullmatch(r"[0-9a-f]{64}", audience_scope)
        or not re.fullmatch(r"[0-9a-f]{64}", hash_key_fingerprint)
        or not re.fullmatch(r"[0-9a-f]{64}", context_key_fingerprint)
    ):
        return False
    requested = (
        int(policy_epoch), deployment_id_hash, mode_scope,
        str(policy_version), audience_scope,
    )
    if not require_lock:
        cur.execute(
            """SELECT max_policy_epoch,deployment_id_hash,mode_scope,
                      policy_version,audience_scope,hash_key_fingerprint,
                      context_key_fingerprint
                 FROM x_capi_config_guard WHERE singleton=TRUE"""
        )
        observed = cur.fetchone()
        if observed is not None:
            durable = (
                int(observed[0]), str(observed[1]), str(observed[2]),
                str(observed[3]), str(observed[4]),
            )
            if requested[0] <= durable[0]:
                return (
                    requested == durable
                    and hmac.compare_digest(
                        hash_key_fingerprint, str(observed[5])
                    )
                    and hmac.compare_digest(
                        context_key_fingerprint, str(observed[6])
                    )
                )

    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_CONFIG_GUARD_LOCK,))
    cur.execute(
        """SELECT max_policy_epoch,deployment_id_hash,mode_scope,
                  policy_version,audience_scope,hash_key_fingerprint,
                  context_key_fingerprint
             FROM x_capi_config_guard WHERE singleton=TRUE FOR UPDATE"""
    )
    row = cur.fetchone()
    if on_observed is not None and not on_observed(row):
        return False
    if row is None:
        if not allow_advance:
            return False
        if before_advance is not None and not before_advance():
            return False
        cur.execute(
            """INSERT INTO x_capi_config_guard
               (singleton,max_policy_epoch,deployment_id_hash,mode_scope,
                policy_version,audience_scope,hash_key_fingerprint,
                context_key_fingerprint,updated_at)
               VALUES(TRUE,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (*requested, hash_key_fingerprint, context_key_fingerprint, now),
        )
        return True
    durable = (int(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]))
    durable_hash_fingerprint = str(row[5])
    durable_context_fingerprint = str(row[6])
    if requested[0] < durable[0]:
        return False
    if requested[0] == durable[0]:
        return hmac.compare_digest(
            durable_hash_fingerprint, hash_key_fingerprint
        ) and hmac.compare_digest(
            durable_context_fingerprint, context_key_fingerprint
        ) and all(
            hmac.compare_digest(left, right)
            for left, right in zip(
                (requested[1], requested[2], requested[3], requested[4]),
                (durable[1], durable[2], durable[3], durable[4]),
            )
        )
    if not allow_advance:
        return False
    # The all-zero marker exists only on a pre-fingerprint migration. It can
    # initialize once during the already-required strict epoch advance; after
    # that, even a later epoch cannot rotate dedup/wallet identity underneath
    # retained rows.
    if not (
        hmac.compare_digest(durable_hash_fingerprint, hash_key_fingerprint)
        or hmac.compare_digest(durable_hash_fingerprint, "0" * 64)
    ):
        return False
    if before_advance is not None and not before_advance():
        return False
    cur.execute(
        """UPDATE x_capi_config_guard
              SET max_policy_epoch=%s,deployment_id_hash=%s,mode_scope=%s,
                  policy_version=%s,audience_scope=%s,
                  hash_key_fingerprint=%s,context_key_fingerprint=%s,
                  updated_at=%s
            WHERE singleton=TRUE AND max_policy_epoch<%s""",
        (
            *requested, hash_key_fingerprint, context_key_fingerprint,
            now, requested[0],
        ),
    )
    return cur.rowcount == 1


def enforce_config_guard(
    conn, cfg: x_capi.Config, now: Optional[float] = None
) -> bool:
    """Register/verify an active generation, committing no identifiers."""
    if cfg.mode == "off":
        return _publish_durable_config_guard(conn)
    if not cfg.producer_ready:
        return False
    hash_key_fingerprint = _hash_key_fingerprint()
    context_key_fingerprint = _context_key_fingerprint()
    if hash_key_fingerprint is None or context_key_fingerprint is None:
        return False
    try:
        with conn.cursor() as cur:
            accepted = _config_guard_on_cursor(
                cur,
                policy_epoch=cfg.policy_epoch,
                deployment_id_hash=_deployment_id_hash(cfg),
                mode_scope=cfg.mode,
                policy_version=cfg.policy_version,
                audience_scope=cfg.audience_scope,
                hash_key_fingerprint=hash_key_fingerprint,
                context_key_fingerprint=context_key_fingerprint,
                now=float(time.time() if now is None else now),
                allow_advance=True,
                require_lock=True,
                on_observed=lambda row: _write_config_guard_attestation(
                    row, allow_transition_match=True
                ),
                before_advance=lambda: _write_config_guard_attestation(None),
            )
        if not accepted:
            conn.rollback()
            return False
        conn.commit()
        # Reacquire the DB fence after a generation advance. If the DB commit
        # succeeded but this publication fails, the preceding unconfigured
        # marker remains and the gate fails closed rather than accepting a
        # stale generation.
        return _publish_durable_config_guard(conn)
    except Exception:
        conn.rollback()
        return False


def verify_config_guard(
    conn, cfg: x_capi.Config, now: Optional[float] = None
) -> bool:
    """Read-only exact-match check used after the singleton worker activates.

    A healthcheck or second process must never advance the irreversible DB
    high-water. On mismatch we publish the *durable* row so the gate also fails
    closed, but we do not mutate that row.
    """
    if cfg.mode == "off":
        return _publish_durable_config_guard(conn)
    if not cfg.producer_ready:
        return False
    hash_key_fingerprint = _hash_key_fingerprint()
    context_key_fingerprint = _context_key_fingerprint()
    if hash_key_fingerprint is None or context_key_fingerprint is None:
        return False
    try:
        with conn.cursor() as cur:
            accepted = _config_guard_on_cursor(
                cur,
                policy_epoch=cfg.policy_epoch,
                deployment_id_hash=_deployment_id_hash(cfg),
                mode_scope=cfg.mode,
                policy_version=cfg.policy_version,
                audience_scope=cfg.audience_scope,
                hash_key_fingerprint=hash_key_fingerprint,
                context_key_fingerprint=context_key_fingerprint,
                now=float(time.time() if now is None else now),
                allow_advance=False,
                require_lock=False,
            )
        if accepted:
            conn.commit()
            # The independent persistent attestation is part of the
            # high-water, not merely an output. A restored DB that matches the
            # restored environment but is older than the file must fail here
            # before ingest/readiness/dispatch.
            return _publish_durable_config_guard(conn)
        conn.rollback()
        _publish_durable_config_guard(conn)
        return False
    except Exception:
        conn.rollback()
        return False


def _recover_privacy_quarantine_generation(
    conn, cfg: x_capi.Config, now: Optional[float] = None
) -> bool:
    """Atomically advance the policy generation and invalidate old DB state.

    Filesystem controls are reset only *after* this transaction commits.  If
    the process dies between those two operations the privacy fence remains
    closed and the operator must choose another strictly newer epoch.  That is
    an intentional availability loss instead of an ambiguous rollback.
    """
    requested = (os.getenv("X_CAPI_PRIVACY_RECOVERY_EPOCH") or "").strip()
    if (
        cfg.mode not in ("dry_run", "live")
        or not cfg.producer_ready
        or requested != str(cfg.policy_epoch)
    ):
        return False
    hash_key_fingerprint = _hash_key_fingerprint()
    context_key_fingerprint = _context_key_fingerprint()
    if hash_key_fingerprint is None or context_key_fingerprint is None:
        return False
    timestamp = float(time.time() if now is None else now)

    def observe_prior(row) -> bool:
        return bool(
            row is not None
            and cfg.policy_epoch > int(row[0])
            and _write_config_guard_attestation(
                row, allow_transition_match=True
            )
        )

    try:
        with conn.cursor() as cur:
            accepted = _config_guard_on_cursor(
                cur,
                policy_epoch=cfg.policy_epoch,
                deployment_id_hash=_deployment_id_hash(cfg),
                mode_scope=cfg.mode,
                policy_version=cfg.policy_version,
                audience_scope=cfg.audience_scope,
                hash_key_fingerprint=hash_key_fingerprint,
                context_key_fingerprint=context_key_fingerprint,
                now=timestamp,
                allow_advance=True,
                require_lock=True,
                on_observed=observe_prior,
                before_advance=lambda: _write_config_guard_attestation(None),
            )
            if not accepted:
                conn.rollback()
                return False
            _quarantine_all_on_cursor(
                cur, timestamp, "privacy_epoch_recovery"
            )
        conn.commit()
        # Keep the filesystem attestation in its configured:false transition
        # state.  The caller holds the dispatch boundary, resets old privacy
        # controls, then publishes the new configured row.  Publishing here
        # would let the gate issue a new-generation capability into slots that
        # the subsequent reset is about to erase.
        return True
    except Exception:
        conn.rollback()
        return False


def _socket_listener_present(path: str) -> bool:
    try:
        socket_info = os.lstat(path)
        if (
            not stat.S_ISSOCK(socket_info.st_mode)
            or socket_info.st_uid != os.geteuid()
            or socket_info.st_mode & 0o077
        ):
            return False
    except OSError:
        return False
    return True


def _ingest_runtime_state() -> tuple[bool, bool, bool]:
    path = str(os.getenv("X_CAPI_INGEST_SOCKET") or DEFAULT_INGEST_SOCKET)
    consent_path = str(
        os.getenv("X_CAPI_CONSENT_SOCKET") or DEFAULT_CONSENT_SOCKET
    )
    parent = os.path.dirname(os.path.abspath(path))
    if os.path.dirname(os.path.abspath(consent_path)) != parent:
        return False, False, False
    try:
        parent_info = os.lstat(parent)
        runtime_secure = bool(
            os.path.realpath(parent) == parent
            and stat.S_ISDIR(parent_info.st_mode)
            and parent_info.st_uid == os.geteuid()
            and not (parent_info.st_mode & 0o077)
        )
    except OSError:
        return False, False, False
    if not runtime_secure:
        return False, False, False
    try:
        lock_info = os.lstat(path + ".lock")
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != os.geteuid()
            or lock_info.st_mode & 0o077
        ):
            return True, False, False
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path + ".lock", flags)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                acceptance_fd = None
                try:
                    acceptance_path = path + INGEST_ACCEPTANCE_SUFFIX
                    acceptance_info = os.lstat(acceptance_path)
                    if (
                        not stat.S_ISREG(acceptance_info.st_mode)
                        or acceptance_info.st_uid != os.geteuid()
                        or stat.S_IMODE(acceptance_info.st_mode) != 0o600
                        or acceptance_info.st_nlink != 1
                        or acceptance_info.st_size
                        != len(INGEST_ACCEPTANCE_MAGIC)
                    ):
                        return True, False, False
                    acceptance_fd = os.open(acceptance_path, flags)
                    descriptor_info = os.fstat(acceptance_fd)
                    if (
                        (int(descriptor_info.st_dev), int(descriptor_info.st_ino))
                        != (
                            int(acceptance_info.st_dev),
                            int(acceptance_info.st_ino),
                        )
                        or os.pread(
                            acceptance_fd, len(INGEST_ACCEPTANCE_MAGIC), 0
                        ) != INGEST_ACCEPTANCE_MAGIC
                    ):
                        return True, False, False
                    try:
                        fcntl.flock(
                            acceptance_fd, fcntl.LOCK_SH | fcntl.LOCK_NB
                        )
                    except (BlockingIOError, OSError):
                        return True, False, False
                    finally:
                        try:
                            fcntl.flock(acceptance_fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                except OSError:
                    return True, False, False
                finally:
                    if acceptance_fd is not None:
                        os.close(acceptance_fd)
                return (
                    True,
                    _socket_listener_present(path),
                    _socket_listener_present(consent_path),
                )
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                return True, False, False
        finally:
            os.close(fd)
    except OSError:
        return True, False, False


def _privacy_fence_runtime_secure(now: Optional[float] = None) -> bool:
    fence = PrivacyFence()
    try:
        # Readiness must not provision state; only the singleton main process
        # may do so before startup activation.
        if not os.path.isdir(fence.path):
            return False
        fence.open(create_controls=False)
        if (
            fence._control_bytes(fence.lock_fd, len(PRIVACY_FENCE_LOCK_MAGIC))
            != PRIVACY_FENCE_LOCK_MAGIC
            or fence._control_bytes(
                fence.global_fd, len(PRIVACY_GLOBAL_INACTIVE)
            ) != PRIVACY_GLOBAL_INACTIVE
            or fence._pending_slots_state() != "clear"
        ):
            return False
        names, overflow = fence._bounded_names()
        if overflow:
            return False
        for name in names:
            if name in fence._fixed_control_names():
                continue
            if fence._read_marker(name, float(time.time() if now is None else now)) is None:
                return False
        return True
    except (OSError, RuntimeError):
        return False
    finally:
        fence.close()


def worker_readiness() -> Dict[str, Any]:
    cfg = x_capi.load_config()
    status = x_capi.config_status()
    direct_db = bool((os.getenv("X_CAPI_DB_URL") or "").strip())
    db_file = (os.getenv("X_CAPI_DB_URL_FILE") or "").strip()
    db_sources_conflict = direct_db and bool(db_file)
    db_configured = (direct_db or bool(db_file)) and not db_sources_conflict
    db_credential_readable = bool(x_capi._db_url(worker=True)) if db_configured else False
    db_target_isolated = bool(
        db_credential_readable and _worker_db_target_is_isolated()
    )
    db_available = False
    db_schema_ready = False
    db_config_guard_ready = False
    if db_target_isolated:
        conn = x_capi.get_connection(worker=True)
        if conn:
            try:
                _configure_connection(conn)
                db_available = True
                db_schema_ready = _schema_ready(conn)
                if db_schema_ready:
                    db_config_guard_ready = verify_config_guard(conn, cfg)
            except Exception:
                conn.rollback()
            finally:
                conn.close()
    token_ok = False
    token_error = None
    context_key_ok = False
    hash_key_ok = False
    # Even with transmission switched off, a retained ticket must remain
    # revocable and retention maintenance must remain observable as unhealthy
    # when its DB/key boundary is unavailable.
    if status["mode"] in ("off", "dry_run", "live"):
        _cipher, context_error = x_capi._context_cipher()
        context_key_ok = context_error is None and _cipher is not None
    if status["mode"] in ("dry_run", "live"):
        hash_key_ok = x_capi.keyed_internal_hash("readiness", "probe") is not None
    live_delivery_blocked_reason = (
        LIVE_DELIVERY_BLOCK_REASON if status["mode"] == "live" else None
    )
    if status["mode"] == "live":
        # No normative X contract currently proves that a supplied twclid is
        # authentic campaign provenance. Do not even open the delivery token
        # while the production path is structurally disabled.
        token_error = live_delivery_blocked_reason
    runtime_secure, listener_present, consent_listener_present = _ingest_runtime_state()
    privacy_fence_secure = _privacy_fence_runtime_secure()
    try:
        allowed_uid_ok = int(os.getenv("X_CAPI_INGEST_ALLOWED_UID", "0")) == 0
    except ValueError:
        allowed_uid_ok = False
    live_ready = bool(
        status["mode"] == "live"
        and status["producer_ready"]
        and db_schema_ready
        and db_config_guard_ready
        and token_ok
        and context_key_ok
        and hash_key_ok
        and runtime_secure
        and privacy_fence_secure
        and allowed_uid_ok
        and live_delivery_blocked_reason is None
    )
    dry_run_ready = bool(
        status["mode"] == "dry_run"
        and status["producer_ready"]
        and db_schema_ready
        and db_config_guard_ready
        and context_key_ok
        and hash_key_ok
        and runtime_secure
        and privacy_fence_secure
        and allowed_uid_ok
    )
    off_ready = bool(
        status["mode"] == "off"
        and db_schema_ready
        and db_config_guard_ready
        and context_key_ok
        and runtime_secure
        and privacy_fence_secure
        and allowed_uid_ok
    )
    worker_ready = bool(off_ready or live_ready or dry_run_ready)
    return {
        **status,
        "worker_db_configured": db_configured,
        "worker_db_credential_readable": db_credential_readable,
        "worker_db_target_isolated": db_target_isolated,
        "worker_db_available": db_available,
        "worker_db_schema_ready": db_schema_ready,
        "worker_db_config_guard_ready": db_config_guard_ready,
        "worker_db_sources_conflict": db_sources_conflict,
        "token_file_readable": token_ok,
        "token_error": token_error,
        "live_delivery_blocked_reason": live_delivery_blocked_reason,
        "context_key_file_readable": context_key_ok,
        "hash_key_file_readable": hash_key_ok,
        "ingest_runtime_secure": runtime_secure,
        "ingest_listener_present": listener_present,
        "consent_listener_present": consent_listener_present,
        "privacy_fence_secure": privacy_fence_secure,
        "ingest_allowed_uid_valid": allowed_uid_ok,
        "off_ready": off_ready,
        "dry_run_ready": dry_run_ready,
        "live_ready": live_ready,
        "worker_ready": worker_ready,
    }


_EVENT_KEYS = frozenset({
    "v", "action", "milestone", "context_token", "wallet_address",
    "source_key", "event_timestamp_ms", "metadata",
})
_REVOKE_KEYS = frozenset({"v", "action", "context_token", "event_timestamp_ms"})
_BIND_KEYS = frozenset({
    "v", "action", "context_token", "wallet_address", "event_timestamp_ms",
})
_METADATA_KEYS = frozenset({
    "allow_context_binding", "credit_source", "payment_rail", "chain_id",
})
_TX_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")


def _validate_ingest_envelope(document: Any) -> Optional[Dict[str, Any]]:
    """Return a normalized strict envelope or None; never echo bad content."""
    if not isinstance(document, dict) or document.get("v") != 1:
        return None
    action = document.get("action")
    if action == "revoke":
        if set(document) != _REVOKE_KEYS:
            return None
        if not isinstance(document.get("context_token"), str):
            return None
        token = document["context_token"]
        timestamp = document.get("event_timestamp_ms")
        if (
            not x_capi._OPAQUE_RE.fullmatch(token)
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or not 0 < timestamp < 10**16
        ):
            return None
        return dict(document)
    if action == "bind":
        if set(document) != _BIND_KEYS or not isinstance(
            document.get("context_token"), str
        ) or not isinstance(document.get("wallet_address"), str):
            return None
        token = document["context_token"]
        wallet = document["wallet_address"].strip().lower()
        timestamp = document.get("event_timestamp_ms")
        if (
            not x_capi._OPAQUE_RE.fullmatch(token)
            or not x_capi.wallet_is_campaign_eligible(wallet)
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or not 0 < timestamp < 10**16
        ):
            return None
        normalized = dict(document)
        normalized["wallet_address"] = wallet
        return normalized
    if action != "event" or set(document) != _EVENT_KEYS:
        return None
    milestone = document.get("milestone")
    if not all(
        isinstance(document.get(name), str)
        for name in ("context_token", "wallet_address", "source_key")
    ):
        return None
    token = document["context_token"]
    wallet = document["wallet_address"].strip().lower()
    source_key = document["source_key"].strip().lower()
    timestamp = document.get("event_timestamp_ms")
    metadata = document.get("metadata")
    if (
        milestone not in x_capi.MILESTONES
        or not x_capi._OPAQUE_RE.fullmatch(token)
        or not x_capi.wallet_is_campaign_eligible(wallet)
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or not 0 < timestamp < 10**16
        or not isinstance(metadata, dict)
        or set(metadata) != _METADATA_KEYS
        or not isinstance(metadata.get("allow_context_binding"), bool)
    ):
        return None
    binding = metadata["allow_context_binding"]
    credit_source = metadata["credit_source"]
    payment_rail = metadata["payment_rail"]
    chain_id = metadata["chain_id"]
    if milestone == x_capi.MILESTONE_WALLET_VERIFIED:
        if (
            not binding
            or source_key != wallet
            or credit_source is not None
            or payment_rail is not None
            or chain_id is not None
        ):
            return None
    elif milestone == x_capi.MILESTONE_DEPOSIT_COMPLETED:
        if (
            source_key != source_key.lower()
            or not _TX_HASH_RE.fullmatch(source_key)
            or credit_source != "onchain"
            or payment_rail not in ("axgt", "eth", "usdc")
            or isinstance(chain_id, bool)
            or not isinstance(chain_id, int)
            or not x_capi.production_chain_eligible(payment_rail, chain_id)
        ):
            return None
    else:
        try:
            session_id = int(source_key)
        except (TypeError, ValueError):
            return None
        if (
            binding
            or session_id <= 0
            or session_id > 2**63 - 1
            or source_key != str(session_id)
            or credit_source is not None
            or payment_rail is not None
            or chain_id is not None
        ):
            return None
    normalized = dict(document)
    normalized["wallet_address"] = wallet
    normalized["source_key"] = source_key
    normalized["metadata"] = dict(metadata)
    return normalized


def _ticket_for_ingest(
    token: str, *, require_primary: bool = False
) -> Optional[Dict[str, Any]]:
    ticket = x_capi.decode_context_ticket(
        token, require_primary=bool(require_primary)
    )
    return ticket if isinstance(ticket, dict) else None


def _count_on_cursor(cur, reason: str, now: float) -> None:
    cur.execute(
        """INSERT INTO x_capi_counters(reason,count,updated_at) VALUES(%s,1,%s)
           ON CONFLICT(reason) DO UPDATE SET count=x_capi_counters.count+1,
           updated_at=EXCLUDED.updated_at""",
        (str(reason or "ingest_unknown")[:64], now),
    )


def _decrement_outbox_capacity_on_cursor(cur, amount: int, now: float) -> None:
    """Apply an exact active-row decrement or abort the whole transaction."""
    changed = int(amount)
    if changed <= 0:
        return
    cur.execute(
        """UPDATE x_capi_capacity
              SET outbox_active_count=outbox_active_count-%s,updated_at=%s
            WHERE singleton=TRUE AND outbox_active_count>=%s
          RETURNING outbox_active_count""",
        (changed, now, changed),
    )
    if cur.fetchone() is None:
        raise RuntimeError("x_capi_outbox_capacity_invariant")


def _extend_revocation_saturation_on_cursor(
    cur, lifecycle_expires_at: float, now: float
) -> bool:
    """Durably cover every unstored privacy deny through its ticket expiry."""
    deadline = float(lifecycle_expires_at)
    cur.execute(
        """UPDATE x_capi_capacity
              SET revocation_saturated=TRUE,
                  revocation_saturated_until=GREATEST(
                      revocation_saturated_until,%s
                  ),
                  updated_at=%s
            WHERE singleton=TRUE
          RETURNING revocation_saturated_until""",
        (deadline, now),
    )
    row = cur.fetchone()
    return bool(row and float(row[0]) >= deadline)


def _wallet_advisory_key(wallet_hash: str) -> int:
    value = int(str(wallet_hash)[:16], 16)
    return value - 2**64 if value >= 2**63 else value


def _handle_advisory_key(handle_hash: str) -> int:
    value = int(str(handle_hash)[16:32], 16)
    return value - 2**64 if value >= 2**63 else value


def _upsert_revocation(conn, envelope: Mapping[str, Any], now: float) -> str:
    ticket = _ticket_for_ingest(str(envelope["context_token"]))
    if not ticket:
        return "invalid_ticket"
    response = process_consent_request(
        conn,
        {
            "v": 1,
            "action": "consent",
            "operation": "revoke",
            "context_token": str(envelope["context_token"]),
            "csrf_token": str(ticket["csrf"]),
            "request_timestamp_ms": int(now * 1000),
        },
        now,
    )
    if response.get("ok"):
        return "revoked"
    return str(response.get("error") or "db_error")


def _event_ticket_is_current(
    ticket: Mapping[str, Any], cfg: x_capi.Config, event_timestamp_ms: int, now: float
) -> bool:
    click_id = x_capi.validate_twclid(ticket.get("twclid"), cfg)
    if (
        ticket.get("state") != "granted"
        or ticket.get("mode_scope") != cfg.mode
        or ticket.get("policy_version") != cfg.policy_version
        or ticket.get("policy_epoch") != cfg.policy_epoch
        or ticket.get("audience_scope") != cfg.audience_scope
        or click_id is None
    ):
        return False
    try:
        issued_at = float(ticket["issued_at"])
        consented_at = float(ticket["consented_at"])
        expires_at = float(ticket["expires_at"])
        lifecycle_expires_at = float(ticket["lifecycle_expires_at"])
    except (KeyError, TypeError, ValueError):
        return False
    event_seconds = int(event_timestamp_ms) / 1000.0
    return bool(
        0 < issued_at <= consented_at <= event_seconds
        and event_seconds >= now - cfg.max_event_age_hours * 3600
        and event_seconds <= now + 300
        and expires_at > now
        and expires_at == lifecycle_expires_at
        and lifecycle_expires_at <= issued_at + 90 * 86400 + 1
        and event_seconds <= expires_at
    )


def _ingest_event(
    conn, envelope: Mapping[str, Any], now: float, *, bind_only: bool = False
) -> str:
    cfg = x_capi.load_config()
    if not cfg.producer_ready:
        return "disabled"
    ticket = _ticket_for_ingest(
        str(envelope["context_token"]), require_primary=True
    )
    if not ticket or not _event_ticket_is_current(
        ticket, cfg, int(envelope["event_timestamp_ms"]), now
    ):
        return "invalid_or_stale_ticket"
    wallet = str(envelope["wallet_address"])
    click_id = str(ticket["twclid"])
    if x_capi.twclid_conflicts_with_wallet(click_id, wallet):
        return "identifier_conflicts_with_wallet"
    wallet_hash = x_capi.keyed_internal_hash("wallet", wallet)
    logical_hash = x_capi.keyed_internal_hash(
        "event:" + str(envelope["milestone"]), str(envelope["source_key"])
    )
    hash_key_fingerprint = _hash_key_fingerprint()
    context_key_fingerprint = _context_key_fingerprint()
    if (
        wallet_hash is None
        or logical_hash is None
        or hash_key_fingerprint is None
        or context_key_fingerprint is None
    ):
        return "hash_key_unavailable"
    handle_hash = x_capi._hash(str(ticket["handle"]))
    csrf_hash = x_capi._hash(str(ticket["csrf"]))
    milestone = str(envelope["milestone"])
    event_id = cfg.event_ids.get(milestone, "")
    try:
        with conn.cursor() as cur:
            if not _config_guard_on_cursor(
                cur,
                policy_epoch=cfg.policy_epoch,
                deployment_id_hash=_deployment_id_hash(cfg),
                mode_scope=cfg.mode,
                policy_version=cfg.policy_version,
                audience_scope=cfg.audience_scope,
                hash_key_fingerprint=hash_key_fingerprint,
                context_key_fingerprint=context_key_fingerprint,
                now=now,
                # Only the startup/readiness guard is allowed to advance the
                # durable generation after publishing its fail-closed marker.
                # Ingest must match the already-attested generation exactly.
                allow_advance=False,
            ):
                conn.rollback()
                return "config_guard_rejected"
            # A handle lock closes the absent-row revoke/event race. The wallet
            # lock then serializes fresh tabs racing to bind the same wallet.
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_handle_advisory_key(handle_hash),),
            )
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_wallet_advisory_key(wallet_hash),),
            )
            cur.execute(
                """SELECT 1 FROM x_capi_revocation_tombstones
                    WHERE handle_hash=%s FOR UPDATE""",
                (handle_hash,),
            )
            if cur.fetchone():
                _count_on_cursor(cur, "revoked_lifecycle", now)
                conn.commit()
                return "revoked_lifecycle"
            cur.execute(
                """SELECT id,csrf_hash,consent_state,mode_scope,policy_version,
                          policy_epoch,audience_scope,lifecycle_expires_at,twclid,
                          consented_at,expires_at,first_seen_at,wallet_hash
                     FROM x_capi_attribution_contexts
                    WHERE handle_hash=%s FOR UPDATE""",
                (handle_hash,),
            )
            row = cur.fetchone()
            if not row:
                if not (
                    milestone in (
                        x_capi.MILESTONE_WALLET_VERIFIED,
                        x_capi.MILESTONE_DEPOSIT_COMPLETED,
                    )
                    and envelope["metadata"]["allow_context_binding"]
                ):
                    _count_on_cursor(cur, "missing_bound_context", now)
                    conn.commit()
                    return "missing_bound_context"
                # The wallet advisory lock makes this a stable first-touch
                # decision. Check for a conflicting active owner *before*
                # reserving capacity so rejected sibling tickets cannot leak
                # one context slot per attempt.
                cur.execute(
                    """SELECT id,consent_state,expires_at,mode_scope,
                              policy_version,policy_epoch,audience_scope
                         FROM x_capi_attribution_contexts
                        WHERE wallet_hash=%s FOR UPDATE""",
                    (wallet_hash,),
                )
                other = cur.fetchone()
                if other:
                    (
                        other_id, other_state, other_expiry, other_mode,
                        other_policy, other_epoch, other_audience,
                    ) = other
                    if (
                        other_state == "granted"
                        and float(other_expiry or 0) > now
                        and other_mode == cfg.mode
                        and other_policy == cfg.policy_version
                        and int(other_epoch) == cfg.policy_epoch
                        and other_audience == cfg.audience_scope
                    ):
                        _count_on_cursor(cur, "wallet_already_attributed", now)
                        conn.commit()
                        return "wallet_already_attributed"
                    cur.execute(
                        """UPDATE x_capi_attribution_contexts
                              SET consent_state=CASE WHEN consent_state='granted'
                                                   THEN 'stale' ELSE consent_state END,
                                  twclid=NULL,expires_at=NULL,wallet_hash=NULL,
                                  wallet_bound_at=NULL,updated_at=%s WHERE id=%s""",
                        (now, other_id),
                    )
                    cur.execute(
                        """UPDATE x_capi_outbox SET status='cancelled',twclid=NULL,
                                  lease_owner=NULL,lease_token=NULL,
                                  lease_expires_at=NULL,updated_at=%s,
                                  last_error_code='attribution_scope_stale'
                            WHERE context_id=%s AND status IN
                              ('dry_run','pending','retrying','leased')""",
                        (now, other_id),
                    )
                    _decrement_outbox_capacity_on_cursor(
                        cur, cur.rowcount, now
                    )
                cur.execute(
                    """UPDATE x_capi_capacity
                          SET context_count=context_count+1,updated_at=%s
                        WHERE singleton=TRUE AND context_count<%s
                          AND revocation_saturated=FALSE
                          AND revocation_saturated_until=0
                      RETURNING context_count""",
                    (now, cfg.context_limit),
                )
                if not cur.fetchone():
                    _count_on_cursor(cur, "context_capacity_unavailable", now)
                    conn.commit()
                    return "context_capacity_unavailable"
                durable_id = str(uuid.uuid4())
                cur.execute(
                    """INSERT INTO x_capi_attribution_contexts
                       (id,handle_hash,csrf_hash,consent_state,mode_scope,
                        policy_version,policy_epoch,audience_scope,
                        lifecycle_expires_at,twclid,consented_at,expires_at,
                        wallet_hash,wallet_bound_at,first_seen_at,updated_at)
                       VALUES(%s,%s,%s,'granted',%s,%s,%s,%s,%s,%s,%s,%s,
                              %s,%s,%s,%s)""",
                    (
                        durable_id, handle_hash, csrf_hash, cfg.mode,
                        cfg.policy_version, cfg.policy_epoch, cfg.audience_scope,
                        float(ticket["lifecycle_expires_at"]), click_id,
                        float(ticket["consented_at"]), float(ticket["expires_at"]),
                        wallet_hash, now, float(ticket["issued_at"]), now,
                    ),
                )
                bound_wallet = wallet_hash
            else:
                (
                    durable_id, durable_csrf, state, mode_scope, policy,
                    policy_epoch, audience_scope, lifecycle_expires_at, twclid,
                    consented_at, expires_at, first_seen_at, bound_wallet,
                ) = row
                immutable_match = (
                    state == "granted"
                    and hmac.compare_digest(str(durable_csrf), csrf_hash)
                    and mode_scope == cfg.mode == ticket["mode_scope"]
                    and policy == cfg.policy_version == ticket["policy_version"]
                    and int(policy_epoch) == cfg.policy_epoch == ticket["policy_epoch"]
                    and audience_scope == cfg.audience_scope == ticket["audience_scope"]
                    and twclid == click_id
                    and float(consented_at or 0) == float(ticket["consented_at"])
                    and float(expires_at or 0) == float(ticket["expires_at"])
                    and float(lifecycle_expires_at) == float(ticket["lifecycle_expires_at"])
                    and float(first_seen_at) == float(ticket["issued_at"])
                )
                if not immutable_match:
                    _count_on_cursor(cur, "wallet_or_lifecycle_mismatch", now)
                    conn.commit()
                    return "wallet_or_lifecycle_mismatch"

            if bound_wallet is None and envelope["metadata"]["allow_context_binding"]:
                cur.execute(
                    """SELECT id,consent_state,expires_at,mode_scope,
                              policy_version,policy_epoch,audience_scope
                         FROM x_capi_attribution_contexts
                        WHERE wallet_hash=%s AND id<>%s FOR UPDATE""",
                    (wallet_hash, durable_id),
                )
                for other in cur.fetchall():
                    (
                        other_id, other_state, other_expiry, other_mode,
                        other_policy, other_epoch, other_audience,
                    ) = other
                    if (
                        other_state == "granted"
                        and float(other_expiry or 0) > now
                        and other_mode == cfg.mode
                        and other_policy == cfg.policy_version
                        and int(other_epoch) == cfg.policy_epoch
                        and other_audience == cfg.audience_scope
                    ):
                        _count_on_cursor(cur, "wallet_already_attributed", now)
                        conn.commit()
                        return "wallet_already_attributed"
                    cur.execute(
                        """UPDATE x_capi_attribution_contexts
                              SET consent_state=CASE WHEN consent_state='granted'
                                                   THEN 'stale' ELSE consent_state END,
                                  twclid=NULL,expires_at=NULL,wallet_hash=NULL,
                                  wallet_bound_at=NULL,
                                  updated_at=%s WHERE id=%s""",
                        (now, other_id),
                    )
                    cur.execute(
                        """UPDATE x_capi_outbox SET status='cancelled',twclid=NULL,
                                  lease_owner=NULL,lease_token=NULL,
                                  lease_expires_at=NULL,updated_at=%s,
                                  last_error_code='attribution_scope_stale'
                            WHERE context_id=%s AND status IN
                              ('dry_run','pending','retrying','leased')""",
                        (now, other_id),
                    )
                    _decrement_outbox_capacity_on_cursor(
                        cur, cur.rowcount, now
                    )
                cur.execute(
                    """UPDATE x_capi_attribution_contexts
                          SET wallet_hash=%s,wallet_bound_at=%s,updated_at=%s
                        WHERE id=%s AND wallet_hash IS NULL""",
                    (wallet_hash, now, now, durable_id),
                )
                bound_wallet = wallet_hash if cur.rowcount == 1 else None
                if bound_wallet is None:
                    cur.execute(
                        "SELECT wallet_hash FROM x_capi_attribution_contexts WHERE id=%s",
                        (durable_id,),
                    )
                    bound_row = cur.fetchone()
                    bound_wallet = bound_row[0] if bound_row else None
            if not bound_wallet or not hmac.compare_digest(
                str(bound_wallet), wallet_hash
            ):
                _count_on_cursor(cur, "wallet_or_lifecycle_mismatch", now)
                conn.commit()
                return "wallet_or_lifecycle_mismatch"

            if bind_only:
                _count_on_cursor(cur, "context_bound", now)
                conn.commit()
                return "bound"

            # Binding is deliberately complete before mapping lookup.
            if not event_id:
                _count_on_cursor(cur, "mapping_disabled_" + milestone, now)
                conn.commit()
                return "mapping_disabled"
            # The singleton capacity row is the O(1), transactional admission
            # fence. Its row lock serializes multiple workers without scanning
            # a potentially million-row outbox while authenticated paths wait.
            cur.execute(
                """UPDATE x_capi_capacity
                      SET outbox_active_count=outbox_active_count+1,
                          updated_at=%s
                    WHERE singleton=TRUE AND outbox_active_count<%s
                      AND revocation_saturated=FALSE
                      AND revocation_saturated_until=0
                  RETURNING outbox_active_count""",
                (now, cfg.queue_limit),
            )
            if cur.fetchone() is None:
                _count_on_cursor(cur, "queue_full", now)
                conn.commit()
                return "queue_full"
            cur.execute(
                """INSERT INTO x_capi_dedup
                   (milestone,source_key_hash,first_seen_at,expires_at)
                   VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (milestone, logical_hash, now, now + 90 * 86400),
            )
            if cur.rowcount != 1:
                _decrement_outbox_capacity_on_cursor(cur, 1, now)
                _count_on_cursor(cur, "duplicate_" + milestone, now)
                conn.commit()
                return "duplicate"
            status = "dry_run" if cfg.mode == "dry_run" else "pending"
            cur.execute(
                """INSERT INTO x_capi_outbox
                   (conversion_id,milestone,source_key_hash,mode_scope,pixel_id,
                    event_id,conversion_timestamp_ms,twclid,context_id,
                    consent_policy_version,consent_policy_epoch,
                    consent_audience_scope,attribution_expires_at,status,
                    attempt_count,next_attempt_at,created_at,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                          0,%s,%s,%s)""",
                (
                    str(uuid.uuid4()), milestone, logical_hash, cfg.mode,
                    cfg.pixel_id, event_id, int(envelope["event_timestamp_ms"]),
                    click_id, durable_id, cfg.policy_version, cfg.policy_epoch,
                    cfg.audience_scope, float(ticket["lifecycle_expires_at"]),
                    status, now, now, now,
                ),
            )
            _count_on_cursor(cur, "enqueued_" + status, now)
        conn.commit()
        return status
    except Exception:
        conn.rollback()
        return "db_error"


def process_ingest_envelope(conn, document: Any, now: Optional[float] = None) -> str:
    envelope = _validate_ingest_envelope(document)
    if envelope is None:
        return "invalid_envelope"
    timestamp = float(time.time() if now is None else now)
    if envelope["action"] == "revoke":
        return _upsert_revocation(conn, envelope, timestamp)
    if envelope["action"] == "bind":
        wallet = str(envelope["wallet_address"])
        return _ingest_event(
            conn,
            {
                "v": 1,
                "action": "event",
                "milestone": x_capi.MILESTONE_WALLET_VERIFIED,
                "context_token": envelope["context_token"],
                "wallet_address": wallet,
                "source_key": wallet,
                "event_timestamp_ms": envelope["event_timestamp_ms"],
                "metadata": {
                    "allow_context_binding": True,
                    "credit_source": None,
                    "payment_rail": None,
                    "chain_id": None,
                },
            },
            timestamp,
            bind_only=True,
        )
    return _ingest_event(conn, envelope, timestamp)


_CONSENT_KEYS = frozenset({
    "v", "action", "operation", "context_token", "csrf_token",
    "request_timestamp_ms",
})
_CONSENT_OPERATIONS = frozenset({"decline", "revoke", "new_lifecycle"})


def _validate_consent_request(document: Any, now: float) -> Optional[Dict[str, Any]]:
    if (
        not isinstance(document, dict)
        or set(document) != _CONSENT_KEYS
        or document.get("v") != 1
        or document.get("action") != "consent"
        or document.get("operation") not in _CONSENT_OPERATIONS
    ):
        return None
    if not isinstance(document.get("context_token"), str) or not isinstance(
        document.get("csrf_token"), str
    ):
        return None
    token = document["context_token"]
    csrf = document["csrf_token"]
    request_ms = document.get("request_timestamp_ms")
    if (
        not x_capi._OPAQUE_RE.fullmatch(token)
        or not x_capi._NONCE_RE.fullmatch(csrf)
        or isinstance(request_ms, bool)
        or not isinstance(request_ms, int)
        or abs(request_ms / 1000.0 - now) > 300
    ):
        return None
    return dict(document)


def _ticket_lifecycle_fields(ticket: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        issued_at = float(ticket["issued_at"])
        lifecycle_expires_at = float(ticket["lifecycle_expires_at"])
        policy_epoch = int(ticket["policy_epoch"])
        audience_scope = str(ticket["audience_scope"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not 0 < issued_at < 10_000_000_000
        or not issued_at < lifecycle_expires_at <= issued_at + 90 * 86400 + 1
        or policy_epoch <= 0
        or not re.fullmatch(r"[0-9a-f]{64}", audience_scope)
    ):
        return None
    return {
        "issued_at": issued_at,
        "lifecycle_expires_at": lifecycle_expires_at,
        "policy_epoch": policy_epoch,
        "audience_scope": audience_scope,
    }


def _consent_error(exc: Exception) -> str:
    pgcode = str(getattr(exc, "pgcode", "") or "")
    if pgcode == "55P03":
        return "lock_unavailable"
    if pgcode in ("57014", "57P01", "57P02", "57P03"):
        return "store_unavailable"
    return "store_unavailable"


def process_consent_request(
    conn, document: Any, now: Optional[float] = None
) -> Dict[str, Any]:
    """Durably close a lifecycle without exposing any DB identifier to the gate."""
    timestamp = float(time.time() if now is None else now)
    request = _validate_consent_request(document, timestamp)
    if request is None:
        return {"v": 1, "ok": False, "error": "invalid_request"}
    operation = str(request["operation"])
    ticket = _ticket_for_ingest(
        str(request["context_token"]),
        require_primary=operation == "new_lifecycle",
    )
    lifecycle = _ticket_lifecycle_fields(ticket or {})
    csrf = str(request["csrf_token"])
    if (
        ticket is None
        or lifecycle is None
        or not hmac.compare_digest(str(ticket.get("csrf") or ""), csrf)
    ):
        return {"v": 1, "ok": False, "error": "invalid_context"}

    handle_hash = x_capi._hash(str(ticket["handle"]))
    csrf_hash = x_capi._hash(csrf)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_handle_advisory_key(handle_hash),),
            )
            cur.execute(
                """SELECT id,csrf_hash,consent_state,mode_scope,policy_version,
                          policy_epoch,audience_scope,lifecycle_expires_at,
                          first_seen_at
                     FROM x_capi_attribution_contexts
                    WHERE handle_hash=%s FOR UPDATE""",
                (handle_hash,),
            )
            row = cur.fetchone()
            if row:
                (
                    context_id, durable_csrf, durable_state, durable_mode,
                    durable_policy, durable_epoch, durable_audience,
                    durable_lifecycle, durable_issued,
                ) = row
                immutable_match = (
                    hmac.compare_digest(str(durable_csrf), csrf_hash)
                    and durable_mode == ticket.get("mode_scope")
                    and durable_policy == ticket.get("policy_version")
                    and int(durable_epoch) == lifecycle["policy_epoch"]
                    and durable_audience == lifecycle["audience_scope"]
                    and float(durable_lifecycle) == lifecycle["lifecycle_expires_at"]
                    and float(durable_issued) == lifecycle["issued_at"]
                )
                if not immutable_match:
                    conn.rollback()
                    return {"v": 1, "ok": False, "error": "invalid_context"}
                if operation == "new_lifecycle":
                    desired = "stale"
                elif operation == "revoke" or durable_state in ("granted", "revoked"):
                    desired = "revoked"
                else:
                    desired = "denied"
                cur.execute(
                    """UPDATE x_capi_attribution_contexts SET
                         consent_state=%s,twclid=NULL,expires_at=NULL,
                         declined_at=%s,revoked_at=%s,wallet_hash=NULL,
                         wallet_bound_at=NULL,updated_at=%s
                       WHERE id=%s""",
                    (
                        desired,
                        timestamp if desired == "denied" else None,
                        timestamp if desired == "revoked" else None,
                        timestamp,
                        context_id,
                    ),
                )
                cur.execute(
                    """UPDATE x_capi_outbox SET status='cancelled',twclid=NULL,
                         lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                         updated_at=%s,last_error_code=%s
                       WHERE context_id=%s AND status IN
                         ('dry_run','pending','retrying','leased')""",
                    (
                        timestamp,
                        "new_lifecycle" if desired == "stale" else "consent_" + desired,
                        context_id,
                    ),
                )
                _decrement_outbox_capacity_on_cursor(
                    cur, cur.rowcount, timestamp
                )
                _count_on_cursor(cur, "consent_" + desired, timestamp)
            else:
                desired = (
                    "stale" if operation == "new_lifecycle"
                    else "revoked" if operation == "revoke"
                    else "denied"
                )
                if lifecycle["lifecycle_expires_at"] > timestamp:
                    # A close can beat the first authoritative wallet event.
                    # Its bounded, identifier-free tombstone prevents that old
                    # ticket from creating a context after this commit.
                    cur.execute(
                        """SELECT handle_hash,csrf_hash,lifecycle_expires_at
                             FROM x_capi_revocation_tombstones
                            WHERE handle_hash=%s FOR UPDATE""",
                        (handle_hash,),
                    )
                    tombstone = cur.fetchone()
                    if tombstone:
                        if (
                            not hmac.compare_digest(str(tombstone[1]), csrf_hash)
                            or float(tombstone[2]) != lifecycle["lifecycle_expires_at"]
                        ):
                            conn.rollback()
                            return {"v": 1, "ok": False, "error": "invalid_context"}
                    else:
                        limit = max(100, int(x_capi.load_config().context_limit)) * 4
                        cur.execute(
                            """UPDATE x_capi_capacity
                                  SET tombstone_count=tombstone_count+1,updated_at=%s
                                WHERE singleton=TRUE AND tombstone_count<%s
                              RETURNING tombstone_count""",
                            (timestamp, limit),
                        )
                        if not cur.fetchone():
                            if not _extend_revocation_saturation_on_cursor(
                                cur, lifecycle["lifecycle_expires_at"], timestamp
                            ):
                                conn.rollback()
                                return {
                                    "v": 1, "ok": False,
                                    "error": "store_unavailable",
                                }
                            conn.commit()
                            return {
                                "v": 1, "ok": False,
                                "error": "revocation_capacity_unavailable",
                            }
                        cur.execute(
                            """INSERT INTO x_capi_revocation_tombstones
                               (handle_hash,csrf_hash,lifecycle_expires_at,
                                created_at,updated_at)
                               VALUES(%s,%s,%s,%s,%s)""",
                            (
                                handle_hash, csrf_hash,
                                lifecycle["lifecycle_expires_at"], timestamp,
                                timestamp,
                            ),
                        )
        conn.commit()
        return {"v": 1, "ok": True, "state": desired}
    except Exception as exc:
        conn.rollback()
        return {"v": 1, "ok": False, "error": _consent_error(exc)}


def _quarantine_all_on_cursor(cur, timestamp: float, reason: str) -> None:
    """Invalidate all dispatchable state inside the caller's transaction."""
    cur.execute("SET LOCAL statement_timeout=%s", (WORKER_STATEMENT_TIMEOUT_MS,))
    cur.execute("SET LOCAL lock_timeout=%s", (WORKER_LOCK_TIMEOUT_MS,))
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_QUEUE_CAPACITY_LOCK,))
    cur.execute(
        """UPDATE x_capi_outbox
              SET status='cancelled',twclid=NULL,lease_owner=NULL,
                  lease_token=NULL,lease_expires_at=NULL,updated_at=%s,
                  last_error_code=%s
            WHERE status IN ('dry_run','pending','retrying','leased')""",
        (timestamp, reason),
    )
    _decrement_outbox_capacity_on_cursor(cur, cur.rowcount, timestamp)
    cur.execute(
        """UPDATE x_capi_attribution_contexts
              SET consent_state='stale',twclid=NULL,expires_at=NULL,
                  wallet_hash=NULL,wallet_bound_at=NULL,updated_at=%s
            WHERE consent_state='granted' OR twclid IS NOT NULL
               OR wallet_hash IS NOT NULL""",
        (timestamp,),
    )
    _count_on_cursor(cur, reason, timestamp)


def begin_worker_lifecycle(
    conn,
    lifecycle_token: str,
    now: Optional[float] = None,
) -> bool:
    """Persist DIRTY before work and return whether the prior stop was clean."""
    timestamp = float(time.time() if now is None else now)
    try:
        token = str(uuid.UUID(str(lifecycle_token)))
    except (TypeError, ValueError, AttributeError):
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT lifecycle_state,ticket_not_before
                     FROM x_capi_worker_state WHERE singleton=TRUE FOR UPDATE"""
            )
            prior = cur.fetchone()
            prior_clean = bool(prior and prior[0] == "clean")
            cutoff = float(prior[1] or 0) if prior else 0.0
            cur.execute(
                """INSERT INTO x_capi_worker_state
                   (singleton,lifecycle_state,lifecycle_token,ticket_not_before,
                    updated_at) VALUES(TRUE,'dirty',%s,%s,%s)
                   ON CONFLICT(singleton) DO UPDATE SET
                     lifecycle_state='dirty',lifecycle_token=EXCLUDED.lifecycle_token,
                     ticket_not_before=EXCLUDED.ticket_not_before,
                     updated_at=EXCLUDED.updated_at""",
                (token, cutoff, timestamp),
            )
        conn.commit()
        return prior_clean
    except Exception:
        conn.rollback()
        raise


def mark_worker_lifecycle_clean(
    conn, lifecycle_token: str, now: Optional[float] = None
) -> bool:
    """Commit CLEAN only for the process generation that marked DIRTY."""
    timestamp = float(time.time() if now is None else now)
    try:
        token = str(uuid.UUID(str(lifecycle_token)))
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE x_capi_worker_state
                      SET lifecycle_state='clean',lifecycle_token=NULL,updated_at=%s
                    WHERE singleton=TRUE AND lifecycle_state='dirty'
                      AND lifecycle_token=%s RETURNING 1""",
                (timestamp, token),
            )
            updated = cur.fetchone() is not None
        if not updated:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


def _apply_privacy_marker(conn, marker: Mapping[str, Any], now: float) -> bool:
    """Persist one already capability-validated local deny marker."""
    handle_hash = str(marker["handle_hash"])
    csrf_hash = str(marker["csrf_hash"])
    lifecycle_expires_at = float(marker["lifecycle_expires_at"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_handle_advisory_key(handle_hash),),
            )
            cur.execute(
                """SELECT id,csrf_hash,lifecycle_expires_at
                     FROM x_capi_attribution_contexts
                    WHERE handle_hash=%s FOR UPDATE""",
                (handle_hash,),
            )
            context = cur.fetchone()
            if context:
                if (
                    not hmac.compare_digest(str(context[1]), csrf_hash)
                    or float(context[2]) != lifecycle_expires_at
                ):
                    conn.rollback()
                    return False
                context_id = context[0]
                cur.execute(
                    """UPDATE x_capi_attribution_contexts
                          SET consent_state='revoked',twclid=NULL,expires_at=NULL,
                              revoked_at=%s,wallet_hash=NULL,wallet_bound_at=NULL,
                              updated_at=%s WHERE id=%s""",
                    (now, now, context_id),
                )
                cur.execute(
                    """UPDATE x_capi_outbox
                          SET status='cancelled',twclid=NULL,lease_owner=NULL,
                              lease_token=NULL,lease_expires_at=NULL,updated_at=%s,
                              last_error_code='local_privacy_fence'
                        WHERE context_id=%s AND status IN
                          ('dry_run','pending','retrying','leased')""",
                    (now, context_id),
                )
                _decrement_outbox_capacity_on_cursor(
                    cur, cur.rowcount, now
                )
            elif lifecycle_expires_at > now:
                cur.execute(
                    """SELECT csrf_hash,lifecycle_expires_at
                         FROM x_capi_revocation_tombstones
                        WHERE handle_hash=%s FOR UPDATE""",
                    (handle_hash,),
                )
                tombstone = cur.fetchone()
                if tombstone:
                    if (
                        not hmac.compare_digest(str(tombstone[0]), csrf_hash)
                        or float(tombstone[1]) != lifecycle_expires_at
                    ):
                        conn.rollback()
                        return False
                else:
                    limit = max(100, int(x_capi.load_config().context_limit)) * 4
                    cur.execute(
                        """UPDATE x_capi_capacity
                              SET tombstone_count=tombstone_count+1,updated_at=%s
                            WHERE singleton=TRUE AND tombstone_count<%s
                          RETURNING tombstone_count""",
                        (now, limit),
                    )
                    if not cur.fetchone():
                        if not _extend_revocation_saturation_on_cursor(
                            cur, lifecycle_expires_at, now
                        ):
                            conn.rollback()
                            return False
                        _count_on_cursor(
                            cur, "revocation_capacity_saturated", now
                        )
                        conn.commit()
                        return True
                    cur.execute(
                        """INSERT INTO x_capi_revocation_tombstones
                           (handle_hash,csrf_hash,lifecycle_expires_at,
                            created_at,updated_at) VALUES(%s,%s,%s,%s,%s)""",
                        (handle_hash, csrf_hash, lifecycle_expires_at, now, now),
                    )
            _count_on_cursor(cur, "local_privacy_fence", now)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


def _privacy_marker_is_applied(conn, marker: Mapping[str, Any]) -> bool:
    """Confirm the durable deny represented by a committed publisher slot."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT (
                         EXISTS (
                           SELECT 1 FROM x_capi_revocation_tombstones
                            WHERE handle_hash=%s AND csrf_hash=%s
                              AND lifecycle_expires_at=%s
                         )
                         OR EXISTS (
                           SELECT 1 FROM x_capi_attribution_contexts c
                            WHERE c.handle_hash=%s AND c.csrf_hash=%s
                              AND c.lifecycle_expires_at=%s
                              AND c.consent_state='revoked' AND c.twclid IS NULL
                              AND NOT EXISTS (
                                SELECT 1 FROM x_capi_outbox o
                                 WHERE o.context_id=c.id AND o.twclid IS NOT NULL
                                   AND o.status IN
                                     ('dry_run','pending','retrying','leased')
                              )
                         )
                         OR EXISTS (
                           SELECT 1 FROM x_capi_capacity
                            WHERE singleton=TRUE
                              AND revocation_saturated=TRUE
                              AND revocation_saturated_until>=%s
                         )
                       )""",
                (
                    str(marker["handle_hash"]), str(marker["csrf_hash"]),
                    float(marker["lifecycle_expires_at"]),
                    str(marker["handle_hash"]), str(marker["csrf_hash"]),
                    float(marker["lifecycle_expires_at"]),
                    float(marker["lifecycle_expires_at"]),
                ),
            )
            applied = bool(cur.fetchone()[0])
        conn.commit()
        return applied
    except Exception:
        conn.rollback()
        return False


class PrivacyFence:
    """Worker-owned local deny spool and cross-process dispatch boundary."""

    def __init__(self, path: Optional[str] = None):
        self.path = os.path.abspath(
            str(path or os.getenv("X_CAPI_PRIVACY_FENCE_DIR")
                or DEFAULT_PRIVACY_FENCE_DIR)
        )
        self.directory_fd: Optional[int] = None
        self.lock_fd: Optional[int] = None
        self.global_fd: Optional[int] = None
        self.pending_slot_fds: list[int] = []
        self.controls_created = False

    @staticmethod
    def _pending_slot_name(index: int) -> str:
        return f"{PRIVACY_PENDING_SLOT_PREFIX}{index:02d}"

    @classmethod
    def _fixed_control_names(cls) -> frozenset[str]:
        return frozenset(
            (PRIVACY_FENCE_LOCK_NAME, PRIVACY_GLOBAL_NAME)
            + tuple(
                cls._pending_slot_name(index)
                for index in range(PRIVACY_PENDING_SLOT_COUNT)
            )
        )

    @staticmethod
    def _open_control_file(
        directory_fd: int, name: str, initial: bytes, *, create: bool
    ) -> tuple[int, bool]:
        flags = (
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        created = False
        try:
            if not create:
                descriptor = os.open(name, flags, dir_fd=directory_fd)
            else:
                descriptor = os.open(
                    name, flags | os.O_CREAT | os.O_EXCL, 0o600,
                    dir_fd=directory_fd,
                )
                created = True
        except FileExistsError:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            if created:
                if os.write(descriptor, initial) != len(initial):
                    raise RuntimeError("unsafe_privacy_fence_control")
                os.fsync(descriptor)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size != len(initial)
            ):
                raise RuntimeError("unsafe_privacy_fence_control")
            return descriptor, created
        except Exception:
            os.close(descriptor)
            raise

    def open(self, *, create_controls: bool = True) -> None:
        if os.path.realpath(self.path) != self.path:
            raise RuntimeError("unsafe_privacy_fence_directory")
        try:
            os.mkdir(self.path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(self.path)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise RuntimeError("unsafe_privacy_fence_directory")
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_fd = os.open(self.path, directory_flags)
        opened: list[int] = []
        created_any = False
        try:
            lock_fd, created = self._open_control_file(
                directory_fd, PRIVACY_FENCE_LOCK_NAME,
                PRIVACY_FENCE_LOCK_MAGIC, create=create_controls,
            )
            opened.append(lock_fd)
            created_any = created_any or created
            global_fd, created = self._open_control_file(
                directory_fd, PRIVACY_GLOBAL_NAME,
                PRIVACY_GLOBAL_INACTIVE, create=create_controls,
            )
            opened.append(global_fd)
            created_any = created_any or created
            slot_fds = []
            for index in range(PRIVACY_PENDING_SLOT_COUNT):
                slot_fd, created = self._open_control_file(
                    directory_fd, self._pending_slot_name(index),
                    PRIVACY_PENDING_SLOT_INACTIVE, create=create_controls,
                )
                slot_fds.append(slot_fd)
                opened.append(slot_fd)
                created_any = created_any or created
        except Exception:
            for descriptor in reversed(opened):
                os.close(descriptor)
            os.close(directory_fd)
            raise
        self.directory_fd = directory_fd
        self.lock_fd = lock_fd
        self.global_fd = global_fd
        self.pending_slot_fds = slot_fds
        self.controls_created = bool(created_any)

    def close(self) -> None:
        for descriptor in self.pending_slot_fds:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
        self.pending_slot_fds = []
        if self.global_fd is not None:
            os.close(self.global_fd)
            self.global_fd = None
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None
        self.controls_created = False

    def _control_path_matches(self, name: str, descriptor: Optional[int]) -> bool:
        if self.directory_fd is None or descriptor is None:
            return False
        try:
            opened = os.fstat(descriptor)
            current = os.stat(
                name, dir_fd=self.directory_fd, follow_symlinks=False
            )
            return bool(
                stat.S_ISREG(current.st_mode)
                and (opened.st_dev, opened.st_ino)
                == (current.st_dev, current.st_ino)
            )
        except OSError:
            return False

    def _pending_slot_record(
        self, index: int, descriptor: int
    ) -> tuple[str, Optional[Dict[str, Any]]]:
        if not self._control_path_matches(
            self._pending_slot_name(index), descriptor
        ):
            return "invalid", None
        raw = self._control_bytes(
            descriptor, len(PRIVACY_PENDING_SLOT_INACTIVE)
        )
        if raw == PRIVACY_PENDING_SLOT_INACTIVE:
            return "inactive", None
        if len(raw) != len(PRIVACY_PENDING_SLOT_INACTIVE):
            return "invalid", None
        prefix = raw[:2]
        encoded_handle = raw[2:66]
        encoded_csrf = raw[66:130]
        encoded_expiry = raw[130:138]
        try:
            handle_hash = encoded_handle.decode("ascii")
            csrf_hash = encoded_csrf.decode("ascii")
            lifecycle_expires_at = struct.unpack("!d", encoded_expiry)[0]
        except (UnicodeDecodeError, struct.error):
            return "invalid", None
        if (
            prefix != PRIVACY_PENDING_SLOT_PENDING_PREFIX
            or not re.fullmatch(r"[0-9a-f]{64}", handle_hash)
            or not re.fullmatch(r"[0-9a-f]{64}", csrf_hash)
            or not math.isfinite(lifecycle_expires_at)
            or lifecycle_expires_at <= 0
        ):
            return "invalid", None
        return "ready", {
            "v": 1,
            "handle_hash": handle_hash,
            "csrf_hash": csrf_hash,
            "lifecycle_expires_at": lifecycle_expires_at,
        }

    def _pending_slots_state(self) -> str:
        if len(self.pending_slot_fds) != PRIVACY_PENDING_SLOT_COUNT:
            return "invalid"
        saw_committed = False
        saw_pending = False
        for index, descriptor in enumerate(self.pending_slot_fds):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return "pending"
            try:
                state, _marker = self._pending_slot_record(
                    index, descriptor
                )
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            if state == "invalid":
                return "invalid"
            if state == "ready":
                saw_committed = True
        if saw_pending:
            return "pending"
        return "committed" if saw_committed else "clear"

    def _pending_slots_prepare_state(self) -> str:
        """Read non-idle slots coherently and identify orphaned P records."""
        if len(self.pending_slot_fds) != PRIVACY_PENDING_SLOT_COUNT:
            return "invalid"
        saw_committed = False
        for index, descriptor in enumerate(self.pending_slot_fds):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                # P->C is written while the publisher owns this slot. Seeing a
                # partial regular-file pwrite is live pending, not corruption.
                return "pending"
            try:
                stable_state, _stable_marker = self._pending_slot_record(
                    index, descriptor
                )
                if stable_state == "invalid":
                    return "invalid"
                if stable_state == "ready":
                    saw_committed = True
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
        return "committed" if saw_committed else "clear"

    @contextmanager
    def dispatch_boundary(self):
        if self.lock_fd is None:
            yield False
            return
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            yield False
            return
        try:
            try:
                lock_info = os.fstat(self.lock_fd)
                valid = bool(
                    stat.S_ISREG(lock_info.st_mode)
                    and lock_info.st_uid == os.geteuid()
                    and stat.S_IMODE(lock_info.st_mode) == 0o600
                    and lock_info.st_nlink == 1
                    and lock_info.st_size == len(PRIVACY_FENCE_LOCK_MAGIC)
                    and os.pread(
                        self.lock_fd, len(PRIVACY_FENCE_LOCK_MAGIC), 0
                    ) == PRIVACY_FENCE_LOCK_MAGIC
                    and self._control_path_matches(
                        PRIVACY_FENCE_LOCK_NAME, self.lock_fd
                    )
                    and self._control_path_matches(
                        PRIVACY_GLOBAL_NAME, self.global_fd
                    )
                    and self._control_bytes(
                        self.global_fd, len(PRIVACY_GLOBAL_INACTIVE)
                    ) == PRIVACY_GLOBAL_INACTIVE
                    and self._pending_slots_state() in (
                        "clear", "committed"
                    )
                )
            except OSError:
                valid = False
            yield valid
        finally:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass

    @contextmanager
    def privacy_close_boundary(self):
        """Serialize a revoke/decline DB commit even while quarantine is active.

        This boundary never authorizes dispatch.  It validates and takes the
        same exclusive OS lock as senders, but deliberately accepts either
        well-formed global-control state and ignores pending publisher slots.
        Thus an already-active global quarantine cannot prevent a durable
        privacy close, while malformed requests/new-lifecycle operations never
        reach this boundary.
        """
        if self.lock_fd is None or self.global_fd is None:
            yield False
            return
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            yield False
            return
        try:
            valid = bool(
                self._control_path_matches(
                    PRIVACY_FENCE_LOCK_NAME, self.lock_fd
                )
                and self._control_path_matches(
                    PRIVACY_GLOBAL_NAME, self.global_fd
                )
                and self._control_bytes(
                    self.lock_fd, len(PRIVACY_FENCE_LOCK_MAGIC)
                ) == PRIVACY_FENCE_LOCK_MAGIC
                and self._control_bytes(
                    self.global_fd, len(PRIVACY_GLOBAL_INACTIVE)
                ) in (PRIVACY_GLOBAL_INACTIVE, PRIVACY_GLOBAL_ACTIVE)
            )
            yield valid
        finally:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass

    @contextmanager
    def exclusive_boundary(self):
        if self.lock_fd is None:
            yield False
            return
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def _read_marker(self, filename: str, now: float) -> Optional[Dict[str, Any]]:
        if self.directory_fd is None or not re.fullmatch(r"[0-9a-f]{64}\.json", filename):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(filename, flags, dir_fd=self.directory_fd)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size <= 0
                or info.st_size > PRIVACY_MARKER_MAX_BYTES
            ):
                return None
            raw = os.read(fd, PRIVACY_MARKER_MAX_BYTES + 1)
        except OSError:
            return None
        finally:
            os.close(fd)
        try:
            document = json.loads(raw.decode("ascii"))
            expiry = float(document["lifecycle_expires_at"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        if (
            not isinstance(document, dict)
            or set(document) != {"v", "handle_hash", "csrf_hash", "lifecycle_expires_at"}
            or document.get("v") != 1
            or isinstance(document.get("lifecycle_expires_at"), bool)
            or not math.isfinite(expiry)
            or not 0 < expiry <= now + 90 * 86400 + 300
            or not re.fullmatch(r"[0-9a-f]{64}", str(document.get("handle_hash") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(document.get("csrf_hash") or ""))
            or filename != str(document["handle_hash"]) + ".json"
        ):
            return None
        return {
            "v": 1,
            "handle_hash": str(document["handle_hash"]),
            "csrf_hash": str(document["csrf_hash"]),
            "lifecycle_expires_at": expiry,
        }

    def _bounded_names(self) -> tuple[list[str], bool]:
        names: list[str] = []
        with os.scandir(self.path) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > (
                    PRIVACY_MARKER_CAP + len(self._fixed_control_names())
                ):
                    return names, True
        return names, False

    def _control_bytes(self, descriptor: Optional[int], size: int) -> bytes:
        if descriptor is None:
            return b""
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size != size
            ):
                return b""
            return os.pread(descriptor, size, 0)
        except OSError:
            return b""

    def _clear_pending_slot(self, descriptor: int) -> bool:
        try:
            if os.pwrite(
                descriptor, PRIVACY_PENDING_SLOT_INACTIVE, 0
            ) != len(PRIVACY_PENDING_SLOT_INACTIVE):
                return False
            os.fsync(descriptor)
            return self._control_bytes(
                descriptor, len(PRIVACY_PENDING_SLOT_INACTIVE)
            ) == PRIVACY_PENDING_SLOT_INACTIVE
        except OSError:
            return False

    def _consume_committed_slots(self, conn, now: float) -> str:
        """Durably apply complete targeted P records before any send."""
        if len(self.pending_slot_fds) != PRIVACY_PENDING_SLOT_COUNT:
            return "privacy_global_quarantine"
        performed_maintenance = False
        for index, descriptor in enumerate(self.pending_slot_fds):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return "privacy_fence_pending"
            try:
                state, marker = self._pending_slot_record(index, descriptor)
                if state == "invalid":
                    return "privacy_global_quarantine"
                if state == "inactive":
                    continue
                if state != "ready" or marker is None:
                    return "privacy_global_quarantine"
                if (
                    float(marker["lifecycle_expires_at"])
                    > now + 90 * 86400 + 300
                    or not _apply_privacy_marker(conn, marker, now)
                    or not _privacy_marker_is_applied(conn, marker)
                ):
                    return "privacy_global_quarantine"
                if not self._clear_pending_slot(descriptor):
                    return "privacy_global_quarantine"
                performed_maintenance = True
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
        return "privacy_fence_pending" if performed_maintenance else "clear"

    def consume(self, conn, now: float) -> str:
        """Drain a bounded batch; any ambiguity globally blocks dispatch."""
        if self.directory_fd is None:
            return "privacy_fence_unavailable"
        if (
            not self._control_path_matches(
                PRIVACY_FENCE_LOCK_NAME, self.lock_fd
            )
            or not self._control_path_matches(
                PRIVACY_GLOBAL_NAME, self.global_fd
            )
            or
            self._control_bytes(self.lock_fd, len(PRIVACY_FENCE_LOCK_MAGIC))
            != PRIVACY_FENCE_LOCK_MAGIC
            or self._control_bytes(self.global_fd, len(PRIVACY_GLOBAL_INACTIVE))
            != PRIVACY_GLOBAL_INACTIVE
        ):
            return "privacy_global_quarantine"
        slot_state = self._consume_committed_slots(conn, now)
        if slot_state != "clear":
            return slot_state
        try:
            names, overflow = self._bounded_names()
        except OSError:
            return "privacy_fence_unavailable"
        marker_names = [
            name for name in names
            if name not in self._fixed_control_names()
        ]
        if overflow or len(marker_names) > PRIVACY_MARKER_CAP:
            return "privacy_global_quarantine"
        markers = []
        for filename in marker_names:
            marker = self._read_marker(filename, now)
            if marker is None:
                return "privacy_global_quarantine"
            markers.append((filename, marker))
        for filename, marker in markers[:PRIVACY_MARKER_BATCH_SIZE]:
            if not _apply_privacy_marker(conn, marker, now):
                return "privacy_global_quarantine"
            try:
                os.unlink(filename, dir_fd=self.directory_fd)
            except OSError:
                return "privacy_fence_unavailable"
        return (
            "privacy_fence_pending"
            if len(markers) > PRIVACY_MARKER_BATCH_SIZE else "clear"
        )

    def prepare(self, conn, now: float, *, boundary_held: bool = False) -> str:
        """Validate the fixed spool controls without contending when healthy.

        The healthy path is lock-free: the final shared-lock check establishes
        ordering with a producer. A poison, overflow, invalid marker, or active
        global sentinel is deliberately not self-healed: it lacks enough
        information to tombstone a possibly unbound stateless ticket. Recovery
        requires an operator policy-epoch advance that invalidates every old
        ticket before resetting the volume controls.
        """
        if self.lock_fd is None or self.global_fd is None or self.directory_fd is None:
            return "privacy_fence_unavailable"
        try:
            names, overflow = self._bounded_names()
        except OSError:
            return "privacy_fence_unavailable"
        marker_names = [
            name for name in names
            if name not in self._fixed_control_names()
        ]
        pending_slot_state = self._pending_slots_prepare_state()
        if pending_slot_state == "pending":
            return "privacy_fence_pending"
        lock_valid = (
            self._control_path_matches(
                PRIVACY_FENCE_LOCK_NAME, self.lock_fd
            )
            and self._control_path_matches(
                PRIVACY_GLOBAL_NAME, self.global_fd
            )
            and
            self._control_bytes(self.lock_fd, len(PRIVACY_FENCE_LOCK_MAGIC))
            == PRIVACY_FENCE_LOCK_MAGIC
        )
        global_state = self._control_bytes(
            self.global_fd, len(PRIVACY_GLOBAL_INACTIVE)
        )
        invalid_marker = any(
            self._read_marker(name, now) is None for name in marker_names
        )
        if (
            lock_valid
            and global_state == PRIVACY_GLOBAL_INACTIVE
            and pending_slot_state in ("clear", "committed")
            and not overflow
            and len(marker_names) <= PRIVACY_MARKER_CAP
            and not invalid_marker
        ):
            return "clear"
        # A producer creates the final O_EXCL marker before filling it.  Seeing
        # a partial file without synchronization is therefore not proof of
        # corruption.  Only the unhealthy path tries EX and re-evaluates; a
        # healthy iteration never contends with a producer.
        # flock locks belong to the open file description. Re-entering the
        # context manager on the same descriptor and then unlocking it would
        # silently release the caller's outer recovery boundary. Callers that
        # already own EX explicitly select a no-op context here.
        boundary = (
            nullcontext(True) if boundary_held else self.exclusive_boundary()
        )
        with boundary as acquired:
            if not acquired:
                return "privacy_fence_pending"
            try:
                names, overflow = self._bounded_names()
                marker_names = [
                    name for name in names
                    if name not in self._fixed_control_names()
                ]
                pending_slot_state = self._pending_slots_prepare_state()
                if pending_slot_state == "pending":
                    return "privacy_fence_pending"
                recovered_race = bool(
                    self._control_path_matches(
                        PRIVACY_FENCE_LOCK_NAME, self.lock_fd
                    )
                    and self._control_path_matches(
                        PRIVACY_GLOBAL_NAME, self.global_fd
                    )
                    and self._control_bytes(
                        self.lock_fd, len(PRIVACY_FENCE_LOCK_MAGIC)
                    ) == PRIVACY_FENCE_LOCK_MAGIC
                    and self._control_bytes(
                        self.global_fd, len(PRIVACY_GLOBAL_INACTIVE)
                    ) == PRIVACY_GLOBAL_INACTIVE
                    and pending_slot_state in ("clear", "committed")
                    and not overflow
                    and len(marker_names) <= PRIVACY_MARKER_CAP
                    and all(
                        self._read_marker(name, now) is not None
                        for name in marker_names
                    )
                )
            except OSError:
                return "privacy_fence_unavailable"
            # Never dispatch in the same iteration after an EX recheck. This
            # lets the main loop drain any paired revoke datagram first.
            return (
                "privacy_fence_pending"
                if recovered_race else "privacy_global_quarantine"
            )

    def reset_after_epoch_recovery(self) -> bool:
        """Reset bounded controls after the DB has committed a newer epoch.

        The caller must hold ``exclusive_boundary``.  No user request invokes
        this path; fsync is appropriate because readiness must not be announced
        until the operator-directed recovery is durable.
        """
        if self.directory_fd is None or self.lock_fd is None or self.global_fd is None:
            return False
        try:
            names, overflow = self._bounded_names()
            if overflow:
                return False
            for name in names:
                if name in self._fixed_control_names():
                    continue
                info = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                ):
                    return False
                os.unlink(name, dir_fd=self.directory_fd)
            for index, descriptor in enumerate(self.pending_slot_fds):
                try:
                    fcntl.flock(
                        descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                except (BlockingIOError, OSError):
                    return False
                try:
                    if not self._control_path_matches(
                        self._pending_slot_name(index), descriptor
                    ) or not self._clear_pending_slot(descriptor):
                        return False
                finally:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
            if os.pwrite(self.lock_fd, PRIVACY_FENCE_LOCK_MAGIC, 0) != len(
                PRIVACY_FENCE_LOCK_MAGIC
            ):
                return False
            if os.pwrite(self.global_fd, PRIVACY_GLOBAL_INACTIVE, 0) != len(
                PRIVACY_GLOBAL_INACTIVE
            ):
                return False
            os.fsync(self.lock_fd)
            os.fsync(self.global_fd)
            os.fsync(self.directory_fd)
            return bool(
                self._control_bytes(
                    self.lock_fd, len(PRIVACY_FENCE_LOCK_MAGIC)
                ) == PRIVACY_FENCE_LOCK_MAGIC
                and self._control_bytes(
                    self.global_fd, len(PRIVACY_GLOBAL_INACTIVE)
                ) == PRIVACY_GLOBAL_INACTIVE
            )
        except OSError:
            return False

    def activate_global_quarantine(self) -> bool:
        """Persistently close a newly/replaced control volume."""
        if self.global_fd is None:
            return False
        try:
            if os.pwrite(self.global_fd, PRIVACY_GLOBAL_ACTIVE, 0) != len(
                PRIVACY_GLOBAL_ACTIVE
            ):
                return False
            os.fsync(self.global_fd)
            if self.directory_fd is not None:
                os.fsync(self.directory_fd)
            return self._control_bytes(
                self.global_fd, len(PRIVACY_GLOBAL_ACTIVE)
            ) == PRIVACY_GLOBAL_ACTIVE
        except OSError:
            return False


class ConsentListener:
    """Bounded synchronous lifecycle-close RPC restricted by Linux peer UID."""

    def __init__(self, path: Optional[str] = None, allowed_uid: Optional[int] = None):
        self.path = str(
            path or os.getenv("X_CAPI_CONSENT_SOCKET") or DEFAULT_CONSENT_SOCKET
        )
        self.allowed_uid = int(
            os.getenv("X_CAPI_INGEST_ALLOWED_UID", "0")
            if allowed_uid is None else allowed_uid
        )
        self.sock: Optional[socket.socket] = None

    def open(self) -> None:
        if not hasattr(socket, "SO_PEERCRED") or not hasattr(socket, "SOCK_SEQPACKET"):
            raise RuntimeError("unix_peer_credentials_unavailable")
        parent = os.path.dirname(os.path.abspath(self.path))
        info = os.lstat(parent)
        if (
            os.path.realpath(parent) != parent
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise RuntimeError("unsafe_consent_socket_directory")
        if os.path.lexists(self.path):
            existing = os.lstat(self.path)
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
                raise RuntimeError("unsafe_existing_consent_socket")
            os.unlink(self.path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            listener.setblocking(False)
            listener.bind(self.path)
            os.chmod(self.path, 0o600)
            listener.listen(16)
            self.sock = listener
        except Exception:
            listener.close()
            self.close()
            raise

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        try:
            info = os.lstat(self.path)
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid():
                os.unlink(self.path)
        except FileNotFoundError:
            pass

    def receive_batch(self, limit: int = CONSENT_BATCH_SIZE):
        if self.sock is None:
            return []
        pending = []
        credential_size = struct.calcsize("3i")
        for _unused in range(max(1, min(CONSENT_BATCH_SIZE, int(limit)))):
            try:
                connection, _address = self.sock.accept()
            except BlockingIOError:
                break
            try:
                credentials = connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, credential_size
                )
                _pid, peer_uid, _gid = struct.unpack("3i", credentials)
                if peer_uid != self.allowed_uid:
                    connection.close()
                    continue
                connection.settimeout(0.05)
                data = connection.recv(CONSENT_MAX_BYTES + 1)
                if not data or len(data) > CONSENT_MAX_BYTES:
                    connection.close()
                    continue
                document = json.loads(data.decode("utf-8"))
                pending.append((document, connection))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                connection.close()
        pending.sort(
            key=lambda item: 0
            if isinstance(item[0], dict)
            and item[0].get("operation") in ("revoke", "decline")
            else 1
        )
        return pending


def drain_consent(listener: Optional[ConsentListener], conn, now: float) -> int:
    if listener is None:
        return 0
    processed = 0
    for document, connection in listener.receive_batch():
        try:
            response = process_consent_request(conn, document, now)
            _send_consent_response(connection, response)
        except OSError:
            pass
        finally:
            connection.close()
        processed += 1
    return processed


def _send_consent_response(
    connection: socket.socket, response: Mapping[str, Any]
) -> None:
    encoded = json.dumps(
        dict(response), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    if not encoded or len(encoded) > CONSENT_MAX_BYTES:
        raise OSError("invalid_consent_response")
    connection.settimeout(0.05)
    # SOCK_SEQPACKET responses must remain one record. `sendall()` may split a
    # short write into multiple records; fail closed instead.
    if connection.send(encoded) != len(encoded):
        raise OSError("short_consent_response")


class ConsentService:
    """Concurrent privacy-close service with a thread-owned DB connection.

    The sender thread and delivery loop never share a psycopg connection. Each
    accepted RPC gets a short-lived worker-role connection.  A separately
    opened privacy-fence descriptor (flock locks are per open-file description)
    serializes durable consent closure against the complete outbound request.
    """

    def __init__(
        self, listener: ConsentListener, privacy_path: Optional[str] = None
    ):
        self.listener = listener
        self.privacy_path = privacy_path
        self._stop_event = threading.Event()
        self._drain_on_stop = False
        self._started = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.failure_type: Optional[str] = None

    def start(self) -> None:
        if self.listener.sock is None:
            raise RuntimeError("consent_listener_not_open")
        if self._thread is not None:
            raise RuntimeError("consent_service_already_started")
        thread = threading.Thread(
            target=self._run, name="x-capi-consent", daemon=True
        )
        self._thread = thread
        thread.start()
        if not self._started.wait(timeout=1.0) or not thread.is_alive():
            raise RuntimeError("consent_service_start_failed")

    def is_healthy(self) -> bool:
        return bool(
            self._thread is not None
            and self._thread.is_alive()
            and self.failure_type is None
        )

    def stop(self, *, drain: bool = False) -> bool:
        self._drain_on_stop = bool(drain)
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=6.0)
        return not thread.is_alive()

    def _process(
        self, document: Any, privacy_fence: PrivacyFence
    ) -> Dict[str, Any]:
        response: Dict[str, Any] = {
            "v": 1, "ok": False, "error": "store_unavailable"
        }
        validated = _validate_consent_request(document, time.time())
        if validated is None:
            return {"v": 1, "ok": False, "error": "invalid_request"}
        operation = str(validated["operation"])
        # This PrivacyFence instance owns an independent lock FD from the main
        # worker.  A send already holding EX therefore cannot be mistaken for
        # a re-entrant acquisition in this thread.
        boundary = (
            privacy_fence.privacy_close_boundary()
            if operation in ("revoke", "decline")
            else privacy_fence.dispatch_boundary()
        )
        with boundary as acquired:
            if not acquired:
                return {"v": 1, "ok": False, "error": "lock_unavailable"}
            conn = None
            try:
                if not _worker_db_target_is_isolated():
                    return response
                conn = x_capi.get_connection(worker=True)
                if conn is None:
                    return response
                _configure_connection(conn)
                if not _schema_ready(conn):
                    return response
                return process_consent_request(conn, document, time.time())
            except Exception:
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                return response
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def _run(self) -> None:
        privacy_fence = PrivacyFence(self.privacy_path)
        try:
            privacy_fence.open(create_controls=False)
            self._started.set()
            while True:
                stopping = self._stop_event.is_set()
                if stopping and not self._drain_on_stop:
                    break
                listener_socket = self.listener.sock
                if listener_socket is None:
                    raise RuntimeError("consent_listener_closed")
                try:
                    readable, _writable, _exceptional = select.select(
                        [listener_socket], [], [], 0.0 if stopping else 0.25
                    )
                except (OSError, ValueError):
                    if self._stop_event.is_set():
                        break
                    raise
                if not readable:
                    if stopping:
                        break
                    continue
                # One peer per iteration bounds head-of-line work and gives the
                # stop condition a chance between every DB connection.
                for document, connection in self.listener.receive_batch(limit=1):
                    try:
                        _send_consent_response(
                            connection, self._process(document, privacy_fence)
                        )
                    except OSError:
                        pass
                    finally:
                        connection.close()
        except Exception as exc:
            self.failure_type = type(exc).__name__
        finally:
            privacy_fence.close()


class IngestListener:
    """Credential-authenticated, bounded local datagram receiver."""

    def __init__(self, path: Optional[str] = None, allowed_uid: Optional[int] = None):
        self.path = str(path or os.getenv("X_CAPI_INGEST_SOCKET") or DEFAULT_INGEST_SOCKET)
        self.allowed_uid = int(
            os.getenv("X_CAPI_INGEST_ALLOWED_UID", "0")
            if allowed_uid is None else allowed_uid
        )
        self.sock: Optional[socket.socket] = None
        self.lock_fd: Optional[int] = None
        self.acceptance_fd: Optional[int] = None
        self.passcred = False
        self.accepting = False
        self._path_identity: Optional[tuple[int, int]] = None
        self._acceptance_identity: Optional[tuple[int, int]] = None

    def open(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
        parent_info = os.lstat(parent)
        if (
            os.path.realpath(parent) != parent
            or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid()
            or parent_info.st_mode & 0o077
        ):
            raise RuntimeError("unsafe_ingest_socket_directory")
        lock_path = self.path + ".lock"
        lock_flags = (
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        lock_fd = os.open(lock_path, lock_flags, 0o600)
        lock_info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != os.geteuid()
            or lock_info.st_mode & 0o077
            or lock_info.st_nlink != 1
        ):
            os.close(lock_fd)
            raise RuntimeError("unsafe_ingest_lock_file")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock_fd)
            raise RuntimeError("ingest_listener_already_running")
        self.lock_fd = lock_fd
        try:
            acceptance_path = self.path + INGEST_ACCEPTANCE_SUFFIX
            acceptance_fd = os.open(acceptance_path, lock_flags, 0o600)
            acceptance_info = os.fstat(acceptance_fd)
            if (
                not stat.S_ISREG(acceptance_info.st_mode)
                or acceptance_info.st_uid != os.geteuid()
                or stat.S_IMODE(acceptance_info.st_mode) != 0o600
                or acceptance_info.st_nlink != 1
            ):
                os.close(acceptance_fd)
                raise RuntimeError("unsafe_ingest_acceptance_file")
            if acceptance_info.st_size == 0:
                if os.write(acceptance_fd, INGEST_ACCEPTANCE_MAGIC) != len(
                    INGEST_ACCEPTANCE_MAGIC
                ):
                    os.close(acceptance_fd)
                    raise RuntimeError("unsafe_ingest_acceptance_file")
                os.fsync(acceptance_fd)
            elif (
                acceptance_info.st_size != len(INGEST_ACCEPTANCE_MAGIC)
                or os.pread(
                    acceptance_fd, len(INGEST_ACCEPTANCE_MAGIC), 0
                ) != INGEST_ACCEPTANCE_MAGIC
            ):
                os.close(acceptance_fd)
                raise RuntimeError("unsafe_ingest_acceptance_file")
            acceptance_path_info = os.lstat(acceptance_path)
            if (
                not stat.S_ISREG(acceptance_path_info.st_mode)
                or acceptance_path_info.st_uid != os.geteuid()
                or stat.S_IMODE(acceptance_path_info.st_mode) != 0o600
                or acceptance_path_info.st_nlink != 1
                or (
                    int(acceptance_path_info.st_dev),
                    int(acceptance_path_info.st_ino),
                ) != (
                    int(acceptance_info.st_dev), int(acceptance_info.st_ino)
                )
            ):
                os.close(acceptance_fd)
                raise RuntimeError("unsafe_ingest_acceptance_file")
            self.acceptance_fd = acceptance_fd
            self._acceptance_identity = (
                int(acceptance_info.st_dev), int(acceptance_info.st_ino)
            )
            if os.path.lexists(self.path):
                info = os.lstat(self.path)
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                    raise RuntimeError("unsafe_existing_ingest_socket")
                os.unlink(self.path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
            except OSError:
                pass  # the kernel default is still bounded
            if not hasattr(socket, "SO_PASSCRED"):
                listener.close()
                raise RuntimeError("unix_peer_credentials_unavailable")
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                self.passcred = True
            except OSError:
                listener.close()
                raise RuntimeError("unix_peer_credentials_unavailable")
            listener.setblocking(False)
            listener.bind(self.path)
            os.chmod(self.path, 0o600)
            path_info = os.lstat(self.path)
            if (
                not stat.S_ISSOCK(path_info.st_mode)
                or path_info.st_uid != os.geteuid()
            ):
                listener.close()
                raise RuntimeError("unsafe_bound_ingest_socket")
            self.sock = listener
            self.accepting = True
            self._path_identity = (int(path_info.st_dev), int(path_info.st_ino))
        except Exception:
            self.close()
            raise

    def stop_accepting(self) -> bool:
        """Remove the public pathname while retaining queued datagrams.

        Producers create a fresh datagram socket for each nonblocking emission,
        so unlinking the bound name makes every later connect/send fail.  The
        receiver fd remains open so records already accepted by the kernel can
        still be drained before a diagnostic CLEAN lifecycle transition is
        committed. Producers intentionally do not participate in this lock:
        one sender already connected at unlink may race the final drain and be
        dropped rather than delay a core response.
        """
        if self.sock is None:
            return False
        if not self.accepting:
            return True
        if self.acceptance_fd is None or self._acceptance_identity is None:
            return False
        try:
            acceptance_info = os.lstat(
                self.path + INGEST_ACCEPTANCE_SUFFIX
            )
            if (
                not stat.S_ISREG(acceptance_info.st_mode)
                or acceptance_info.st_uid != os.geteuid()
                or stat.S_IMODE(acceptance_info.st_mode) != 0o600
                or acceptance_info.st_nlink != 1
                or (
                    int(acceptance_info.st_dev), int(acceptance_info.st_ino)
                ) != self._acceptance_identity
                or os.pread(
                    self.acceptance_fd, len(INGEST_ACCEPTANCE_MAGIC), 0
                ) != INGEST_ACCEPTANCE_MAGIC
            ):
                return False
            # Serialize worker-owned pathname/lifecycle transitions. Core
            # producers intentionally never take this file lock.
            fcntl.flock(
                self.acceptance_fd, fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except (BlockingIOError, OSError):
            return False
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            # The pathname is already closed to new producers. Treat this as
            # safely stopped, while retaining the receiver for its final drain.
            self.accepting = False
            self._path_identity = None
            return True
        identity = (int(info.st_dev), int(info.st_ino))
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.geteuid()
            or self._path_identity is None
            or identity != self._path_identity
        ):
            return False
        try:
            os.unlink(self.path)
        except OSError:
            return False
        self.accepting = False
        self._path_identity = None
        return True

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.accepting:
            try:
                info = os.lstat(self.path)
                identity = (int(info.st_dev), int(info.st_ino))
                if (
                    stat.S_ISSOCK(info.st_mode)
                    and info.st_uid == os.geteuid()
                    and self._path_identity is not None
                    and identity == self._path_identity
                ):
                    os.unlink(self.path)
            except FileNotFoundError:
                pass
        self.accepting = False
        self._path_identity = None
        if self.acceptance_fd is not None:
            try:
                fcntl.flock(self.acceptance_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.acceptance_fd)
            self.acceptance_fd = None
        self._acceptance_identity = None
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self.lock_fd)
                self.lock_fd = None

    def _receive_batch_state(
        self, limit: int = INGEST_BATCH_SIZE
    ) -> tuple[list[Dict[str, Any]], bool]:
        """Return validated records and whether EAGAIN proved the queue empty."""
        if self.sock is None:
            return [], True
        received = []
        exhausted = False
        credential_size = socket.CMSG_SPACE(struct.calcsize("3i"))
        for _unused in range(max(1, min(1000, int(limit)))):
            try:
                data, ancillary, flags, _address = self.sock.recvmsg(
                    INGEST_MAX_BYTES + 1, credential_size
                )
            except BlockingIOError:
                exhausted = True
                break
            if flags & getattr(socket, "MSG_TRUNC", 0) or len(data) > INGEST_MAX_BYTES:
                continue
            peer_uid = None
            for level, kind, payload in ancillary:
                if (
                    level == socket.SOL_SOCKET
                    and kind == getattr(socket, "SCM_CREDENTIALS", -1)
                    and len(payload) >= struct.calcsize("3i")
                ):
                    _pid, peer_uid, _gid = struct.unpack("3i", payload[:struct.calcsize("3i")])
                    break
            if not self.passcred or peer_uid != self.allowed_uid:
                continue
            try:
                document = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            envelope = _validate_ingest_envelope(document)
            if envelope is not None:
                received.append(envelope)
        return received, exhausted

    def receive_batch(self, limit: int = INGEST_BATCH_SIZE) -> list[Dict[str, Any]]:
        received, _exhausted = self._receive_batch_state(limit)
        return received


def drain_ingest(listener: Optional[IngestListener], conn, now: float) -> Dict[str, int]:
    """Drain a bounded batch, always applying revocations before events."""
    if listener is None:
        return {}
    envelopes = listener.receive_batch()
    envelopes.sort(key=lambda item: 0 if item["action"] == "revoke" else 1)
    outcomes: Dict[str, int] = {}
    for envelope in envelopes:
        outcome = process_ingest_envelope(conn, envelope, now)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


def drain_ingest_until_empty(
    listener: Optional[IngestListener],
    conn,
    now: float,
    *,
    limit: int = INGEST_DISPATCH_DRAIN_LIMIT,
) -> tuple[Dict[str, int], bool]:
    """Drain and globally prioritize a bounded queue prefix.

    Dispatch may proceed only when the nonblocking receiver returned EAGAIN.
    Merely consuming one batch is not a privacy fence: a revoke can otherwise
    sit behind event hints in the kernel queue.  If the bounded prefix is
    exhausted first, all collected records are applied but the caller must
    release the dispatch boundary without sending and try again.
    """
    if listener is None:
        return {}, True
    remaining = max(1, min(INGEST_DISPATCH_DRAIN_LIMIT, int(limit)))
    envelopes: list[Dict[str, Any]] = []
    exhausted = False
    while remaining > 0:
        batch_limit = min(INGEST_BATCH_SIZE, remaining)
        batch, exhausted = listener._receive_batch_state(batch_limit)
        envelopes.extend(batch)
        remaining -= batch_limit
        if exhausted:
            break
    envelopes.sort(key=lambda item: 0 if item["action"] == "revoke" else 1)
    outcomes: Dict[str, int] = {}
    for envelope in envelopes:
        outcome = process_ingest_envelope(conn, envelope, now)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes, exhausted


def _job_from_row(row) -> Dict[str, Any]:
    keys = (
        "conversion_id", "milestone", "pixel_id", "event_id",
        "conversion_timestamp_ms", "twclid", "context_id", "attempt_count",
        "attribution_expires_at", "consent_policy_epoch",
        "consent_audience_scope", "lease_owner", "lease_token",
        "lease_expires_at",
    )
    return dict(zip(keys, row))


def claim_job(
    conn,
    worker_id: str,
    now: float,
    lease_seconds: int = LEASE_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Lease one job and commit before any HTTP call."""
    lease_token = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """WITH candidate AS (
                   SELECT o.conversion_id
                     FROM x_capi_outbox o
                     JOIN x_capi_attribution_contexts c ON c.id=o.context_id
                     JOIN x_capi_capacity cap ON cap.singleton=TRUE
                      AND cap.revocation_saturated=FALSE
                      AND cap.revocation_saturated_until=0
                    WHERE o.mode_scope='live'
                      AND o.status IN ('pending','retrying','leased')
                      AND o.next_attempt_at <= %s
                      AND (o.lease_expires_at IS NULL OR o.lease_expires_at <= %s)
                      AND c.consent_state='granted'
                      AND c.mode_scope='live'
                      AND c.expires_at > %s
                      AND c.twclid IS NOT NULL
                    ORDER BY o.next_attempt_at, o.created_at
                    FOR UPDATE OF o SKIP LOCKED LIMIT 1
               )
               UPDATE x_capi_outbox o
                  SET status='leased', lease_owner=%s, lease_token=%s,
                      lease_expires_at=%s, updated_at=%s
                 FROM candidate
                WHERE o.conversion_id=candidate.conversion_id
                RETURNING o.conversion_id, o.milestone, o.pixel_id, o.event_id,
                          o.conversion_timestamp_ms, o.twclid, o.context_id,
                          o.attempt_count, o.attribution_expires_at,
                          o.consent_policy_epoch, o.consent_audience_scope,
                          o.lease_owner, o.lease_token, o.lease_expires_at""",
            (
                now, now, now, worker_id, lease_token,
                now + lease_seconds, now,
            ),
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return None
        job = _job_from_row(row)
    conn.commit()
    return job


def begin_dispatch(
    conn,
    job: Mapping[str, Any],
    now: float,
    policy_version: str,
    max_event_age_hours: int,
    policy_epoch: Optional[int] = None,
    audience_scope: Optional[str] = None,
    deployment_id_hash: Optional[str] = None,
) -> str:
    """Fence and validate an owned lease, committing before any network I/O.

    The caller holds the local PrivacyFence exclusive dispatch lock across this
    check and the HTTP request. That lock is the cross-process revocation and
    lease-reclaim boundary; no PostgreSQL transaction or row lock is held while
    waiting on X.
    """
    event_seconds = int(job["conversion_timestamp_ms"]) / 1000.0
    expected_epoch = int(
        job["consent_policy_epoch"] if policy_epoch is None else policy_epoch
    )
    expected_audience = str(
        job["consent_audience_scope"] if audience_scope is None else audience_scope
    )
    expected_deployment = str(
        _deployment_id_hash(x_capi.load_config())
        if deployment_id_hash is None else deployment_id_hash
    )
    hash_key_fingerprint = _hash_key_fingerprint()
    context_key_fingerprint = _context_key_fingerprint()
    if hash_key_fingerprint is None or context_key_fingerprint is None:
        conn.rollback()
        return "config_guard_rejected"
    with conn.cursor() as cur:
        if not _config_guard_on_cursor(
            cur,
            policy_epoch=expected_epoch,
            deployment_id_hash=expected_deployment,
            mode_scope="live",
            policy_version=policy_version,
            audience_scope=expected_audience,
            hash_key_fingerprint=hash_key_fingerprint,
            context_key_fingerprint=context_key_fingerprint,
            now=now,
            allow_advance=False,
        ):
            conn.rollback()
            return "config_guard_rejected"
        # Reassert ownership and renew the per-claim lease for the entire
        # bounded request window immediately before the committed send fence.
        cur.execute(
            """UPDATE x_capi_outbox
                  SET lease_expires_at=%s,updated_at=%s
                WHERE conversion_id=%s AND status='leased'
                  AND lease_owner=%s AND lease_token=%s
                  AND lease_expires_at>%s
              RETURNING 1""",
            (
                now + max(LEASE_SECONDS, TOTAL_REQUEST_SECONDS + 10.0), now,
                job["conversion_id"], job["lease_owner"], job["lease_token"], now,
            ),
        )
        if cur.fetchone() is None:
            conn.rollback()
            return "lease_lost"
        cur.execute(
            """SELECT o.conversion_timestamp_ms, o.attribution_expires_at,
                      c.consent_state, c.mode_scope, c.policy_version,
                      c.policy_epoch, c.audience_scope,
                      c.lifecycle_expires_at, c.twclid, c.expires_at
                 FROM x_capi_outbox o
               JOIN x_capi_attribution_contexts c ON c.id=o.context_id
               JOIN x_capi_capacity cap ON cap.singleton=TRUE
                AND cap.revocation_saturated=FALSE
                AND cap.revocation_saturated_until=0
               WHERE o.conversion_id=%s AND o.status='leased'
                 AND o.lease_owner=%s AND o.lease_token=%s
                 AND o.lease_expires_at>%s
                 AND o.mode_scope='live' AND o.twclid=%s
                 AND o.pixel_id=%s AND o.event_id=%s
                 AND o.conversion_timestamp_ms=%s AND o.context_id=%s
                 AND o.consent_policy_version=%s
                 AND o.consent_policy_epoch=%s
                 AND o.consent_audience_scope=%s
               FOR UPDATE OF o, c""",
            (
                job["conversion_id"], job["lease_owner"], job["lease_token"],
                now, job["twclid"], job["pixel_id"], job["event_id"],
                job["conversion_timestamp_ms"], job["context_id"],
                policy_version, expected_epoch, expected_audience,
            ),
        )
        row = cur.fetchone()
    if not row:
        conn.rollback()
        return "lease_lost"
    if event_seconds < now - int(max_event_age_hours) * 3600:
        return "event_too_old"
    if event_seconds > now + 300:
        return "event_in_future"
    (
        _event_ms, attribution_expires_at, consent_state, context_mode,
        context_policy, context_epoch, context_audience, lifecycle_expires_at,
        context_twclid, context_expires_at,
    ) = row
    if (
        consent_state != "granted"
        or context_mode != "live"
        or context_twclid != job["twclid"]
        or context_policy != policy_version
        or int(context_epoch) != expected_epoch
        or context_audience != expected_audience
        or float(context_expires_at or 0) <= now
        or float(lifecycle_expires_at or 0) <= now
        or float(attribution_expires_at or 0) != float(lifecycle_expires_at or 0)
        or float(attribution_expires_at or 0) <= now
    ):
        return "consent_not_dispatchable"
    conn.commit()
    return "allowed"


def dispatch_still_allowed(
    conn,
    job: Mapping[str, Any],
    now: float,
    policy_version: str,
    max_event_age_hours: int,
) -> bool:
    """Compatibility wrapper for the committed pre-network validation."""
    return begin_dispatch(conn, job, now, policy_version, max_event_age_hours) == "allowed"


def _retry_after(headers: Mapping[str, str], now: float) -> Optional[float]:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if not value or len(str(value)) > 128:
        return None
    raw = str(value).strip()
    try:
        seconds = int(raw)
        return now + max(1, min(3600, seconds))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return now + max(1, min(3600, parsed.timestamp() - now))
        except (TypeError, ValueError, OverflowError):
            return None


def _accepted(body: bytes) -> tuple[bool, Optional[str]]:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, None
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict):
        return False, None
    processed = data.get("conversions_processed")
    # Every request contains exactly one conversion.  Treat any other count as
    # an ambiguous response rather than accepting a job X may not have stored.
    if isinstance(processed, bool) or not isinstance(processed, int) or processed != 1:
        return False, None
    # A syntactically plausible vendor debug ID can still echo the credential
    # or click. No response string is necessary for local acknowledgement.
    return True, None


def classify_response(response: TransportResponse, now: float) -> Dict[str, Any]:
    status = int(response.status_code)
    if status == 200:
        accepted, debug_id = _accepted(response.body)
        return {"action": "accepted" if accepted else "permanent", "code": "accepted" if accepted else "unknown_success_body", "debug_id": debug_id}
    if status in (401, 403):
        return {"action": "pause", "code": "authentication_failed"}
    if status == 429:
        return {"action": "retry", "code": "rate_limited", "retry_at": _retry_after(response.headers, now)}
    if status in (408, 425) or 500 <= status <= 599:
        return {"action": "retry", "code": "transient_http"}
    if 300 <= status <= 399:
        return {"action": "permanent", "code": "redirect_rejected"}
    return {"action": "permanent", "code": "request_rejected"}


def _backoff(attempt: int, rng=random.random) -> float:
    base = min(3600.0, 5.0 * (2 ** min(10, max(0, attempt))))
    return base * (0.75 + 0.5 * float(rng()))


def finish_job(
    conn,
    job: Mapping[str, Any],
    result: Mapping[str, Any],
    now: float,
    rng=random.random,
) -> bool:
    """Finish only the exact claim token; stale workers have no side effects."""
    action = result["action"]
    code = str(result.get("code") or "unknown")[:64]
    with conn.cursor() as cur:
        if action == "accepted":
            cur.execute(
                """UPDATE x_capi_outbox SET status='accepted', accepted_at=%s,
                   twclid=NULL, lease_owner=NULL, lease_expires_at=NULL,
                   lease_token=NULL, updated_at=%s, last_error_code=NULL,
                   safe_debug_id=NULL
                   WHERE conversion_id=%s AND status='leased'
                     AND lease_owner=%s AND lease_token=%s
                   RETURNING 1""",
                (
                    now, now, job["conversion_id"],
                    job["lease_owner"], job["lease_token"],
                ),
            )
        elif action in ("retry", "pause"):
            attempt = int(job["attempt_count"]) + 1
            retry_at = result.get("retry_at") or now + (
                300.0 if action == "pause" else _backoff(attempt, rng)
            )
            cur.execute(
                """UPDATE x_capi_outbox SET status='retrying', attempt_count=%s,
                   next_attempt_at=%s, lease_owner=NULL, lease_expires_at=NULL,
                   lease_token=NULL, updated_at=%s, last_error_code=%s
                   WHERE conversion_id=%s AND status='leased'
                     AND lease_owner=%s AND lease_token=%s
                   RETURNING 1""",
                (
                    attempt, retry_at, now, code, job["conversion_id"],
                    job["lease_owner"], job["lease_token"],
                ),
            )
        elif action == "cancel":
            cur.execute(
                """UPDATE x_capi_outbox SET status='cancelled', twclid=NULL,
                   lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                   updated_at=%s, last_error_code=%s
                   WHERE conversion_id=%s AND status='leased'
                     AND lease_owner=%s AND lease_token=%s
                   RETURNING 1""",
                (
                    now, code, job["conversion_id"], job["lease_owner"],
                    job["lease_token"],
                ),
            )
        else:
            cur.execute(
                """UPDATE x_capi_outbox SET status='failed', attempt_count=attempt_count+1,
                   twclid=NULL, lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                   updated_at=%s, last_error_code=%s
                   WHERE conversion_id=%s AND status='leased'
                     AND lease_owner=%s AND lease_token=%s
                   RETURNING 1""",
                (
                    now, code, job["conversion_id"], job["lease_owner"],
                    job["lease_token"],
                ),
            )
        mutated = cur.fetchone() is not None
        if not mutated:
            conn.rollback()
            return False
        if action not in ("retry", "pause"):
            _decrement_outbox_capacity_on_cursor(cur, 1, now)
        if action == "pause":
            cur.execute(
                """INSERT INTO x_capi_worker_state (singleton, paused_reason, paused_at, updated_at)
                   VALUES (TRUE,%s,%s,%s) ON CONFLICT (singleton) DO UPDATE SET
                   paused_reason=EXCLUDED.paused_reason, paused_at=EXCLUDED.paused_at,
                   updated_at=EXCLUDED.updated_at""",
                (code, now, now),
            )
        cur.execute(
            """INSERT INTO x_capi_counters(reason,count,updated_at) VALUES(%s,1,%s)
               ON CONFLICT(reason) DO UPDATE SET count=x_capi_counters.count+1,
               updated_at=EXCLUDED.updated_at""",
            ("worker_" + code, now),
        )
    conn.commit()
    return True


def expire_and_cleanup(
    conn,
    now: float,
    max_age_hours: int,
    policy_version: str,
    retention_days: int = 30,
    policy_epoch: int = 1,
    audience_scope: str = "0" * 64,
    context_limit: int = 10_000,
) -> None:
    cutoff_ms = int((now - max_age_hours * 3600) * 1000)
    purge_before = now - max(1, retention_days) * 86400
    # A maximum-TTL stateless ticket must never become reusable merely because
    # its denied/revoked/stale durable anti-rollback row was purged early.
    context_purge_before = now - max(91, retention_days) * 86400
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout=%s", (CLEANUP_STATEMENT_TIMEOUT_MS,))
        cur.execute("SET LOCAL lock_timeout=%s", (100,))
        cur.execute(
            """WITH targets AS (
                   SELECT id FROM x_capi_attribution_contexts
                    WHERE twclid IS NOT NULL
                      AND (expires_at<=%s OR policy_version<>%s
                           OR policy_epoch<>%s OR audience_scope<>%s)
                    ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 500
               )
               UPDATE x_capi_attribution_contexts c SET consent_state='stale',
                  twclid=NULL, expires_at=NULL, wallet_hash=NULL,
                  wallet_bound_at=NULL,
                  updated_at=%s
                 FROM targets WHERE c.id=targets.id""",
            (now, policy_version, int(policy_epoch), str(audience_scope), now),
        )
        cur.execute(
            """WITH targets AS (
                   SELECT conversion_id FROM x_capi_outbox
                    WHERE status IN ('dry_run','pending','retrying','leased')
                      AND (consent_policy_version<>%s
                           OR consent_policy_epoch<>%s
                           OR consent_audience_scope<>%s)
                    ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 500
               )
               UPDATE x_capi_outbox o SET status='cancelled', twclid=NULL,
                  lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                  updated_at=%s, last_error_code='consent_policy_stale'
                 FROM targets WHERE o.conversion_id=targets.conversion_id""",
            (policy_version, int(policy_epoch), str(audience_scope), now),
        )
        _decrement_outbox_capacity_on_cursor(cur, cur.rowcount, now)
        cur.execute(
            """WITH targets AS (
                   SELECT conversion_id FROM x_capi_outbox
                    WHERE status IN ('dry_run','pending','retrying','leased')
                      AND (conversion_timestamp_ms<%s OR attribution_expires_at<=%s)
                    ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 500
               )
               UPDATE x_capi_outbox o SET status='expired', twclid=NULL,
                  lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                  updated_at=%s, last_error_code='event_too_old'
                 FROM targets WHERE o.conversion_id=targets.conversion_id""",
            (cutoff_ms, now, now),
        )
        _decrement_outbox_capacity_on_cursor(cur, cur.rowcount, now)
        cur.execute(
            """WITH targets AS (
                   SELECT conversion_id FROM x_capi_outbox
                    WHERE status IN ('accepted','failed','expired','cancelled','dry_run')
                      AND updated_at<%s
                    ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 500
               ), deleted AS (
                   DELETE FROM x_capi_outbox o USING targets
                    WHERE o.conversion_id=targets.conversion_id
                  RETURNING o.status
               ) SELECT count(*) FILTER (WHERE status='dry_run') FROM deleted""",
            (purge_before,),
        )
        _decrement_outbox_capacity_on_cursor(
            cur, int((cur.fetchone() or (0,))[0]), now
        )
        cur.execute(
            """WITH targets AS (
                   SELECT c.id FROM x_capi_attribution_contexts c
                    WHERE c.updated_at<%s AND NOT EXISTS
                      (SELECT 1 FROM x_capi_outbox o WHERE o.context_id=c.id)
                    ORDER BY c.updated_at FOR UPDATE OF c SKIP LOCKED LIMIT 500
               ), deleted AS (
                   DELETE FROM x_capi_attribution_contexts c USING targets
                    WHERE c.id=targets.id RETURNING 1
               )
               UPDATE x_capi_capacity
                  SET context_count=context_count-(SELECT count(*) FROM deleted),
                      updated_at=%s
                WHERE singleton=TRUE""",
            (context_purge_before, now),
        )
        tombstone_limit = max(100, int(context_limit)) * 4
        cur.execute(
            """WITH targets AS (
                   SELECT handle_hash FROM x_capi_revocation_tombstones
                    WHERE lifecycle_expires_at<=%s
                    ORDER BY lifecycle_expires_at
                    FOR UPDATE SKIP LOCKED LIMIT 500
               ), deleted AS (
                   DELETE FROM x_capi_revocation_tombstones t USING targets
                    WHERE t.handle_hash=targets.handle_hash RETURNING 1
               )
               UPDATE x_capi_capacity
                  SET tombstone_count=tombstone_count-(SELECT count(*) FROM deleted),
                      revocation_saturated=CASE
                        WHEN revocation_saturated=TRUE
                         AND revocation_saturated_until<=%s
                         AND tombstone_count-(SELECT count(*) FROM deleted)<%s
                        THEN FALSE ELSE revocation_saturated END,
                      revocation_saturated_until=CASE
                        WHEN revocation_saturated=TRUE
                         AND revocation_saturated_until<=%s
                         AND tombstone_count-(SELECT count(*) FROM deleted)<%s
                        THEN 0 ELSE revocation_saturated_until END,
                      updated_at=%s
                WHERE singleton=TRUE""",
            (now, now, tombstone_limit, now, tombstone_limit, now),
        )
        cur.execute(
            """WITH targets AS (
                   SELECT milestone, source_key_hash FROM x_capi_dedup
                    WHERE expires_at<=%s
                    ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT 500
               )
               DELETE FROM x_capi_dedup d USING targets
                WHERE d.milestone=targets.milestone
                  AND d.source_key_hash=targets.source_key_hash""",
            (now,),
        )
    conn.commit()


def _is_paused(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT paused_reason FROM x_capi_worker_state WHERE singleton=TRUE")
        row = cur.fetchone()
    conn.commit()
    return bool(row and row[0])


def clear_pause() -> bool:
    """Operator action after fixing/rotating credentials; never contacts X."""
    if not _worker_db_target_is_isolated():
        return False
    conn = x_capi.get_connection(worker=True)
    if not conn:
        return False
    try:
        _configure_connection(conn)
        if not _schema_ready(conn):
            return False
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE x_capi_worker_state SET paused_reason=NULL,
                   paused_at=NULL, updated_at=%s WHERE singleton=TRUE""",
                (time.time(),),
            )
            cur.execute(
                """UPDATE x_capi_outbox SET next_attempt_at=%s, updated_at=%s
                   WHERE status='retrying'
                     AND last_error_code='authentication_failed'""",
                (time.time(), time.time()),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def local_status() -> Dict[str, Any]:
    """Return aggregate-only operator status; never identifiers or payloads."""
    result = worker_readiness()
    result.update({
        "worker_db_available": False,
        "queue_counts": {
            name: 0 for name in (
                "dry_run", "pending", "leased", "retrying", "accepted",
                "failed", "expired", "cancelled",
            )
        },
        "reason_counts": {},
        "paused_reason": None,
    })
    if not _worker_db_target_is_isolated():
        return result
    conn = x_capi.get_connection(worker=True)
    if not conn:
        return result
    try:
        _configure_connection(conn)
        if not _schema_ready(conn):
            return result
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*) FROM x_capi_outbox GROUP BY status ORDER BY status"
            )
            for status, count in cur.fetchall():
                if str(status) in result["queue_counts"]:
                    result["queue_counts"][str(status)] = int(count)
            cur.execute(
                """SELECT reason, count FROM x_capi_counters
                   ORDER BY reason LIMIT 100"""
            )
            result["reason_counts"] = {
                str(reason)[:64]: int(count) for reason, count in cur.fetchall()
            }
            cur.execute(
                "SELECT paused_reason FROM x_capi_worker_state WHERE singleton=TRUE"
            )
            paused = cur.fetchone()
            result["paused_reason"] = (
                str(paused[0])[:64] if paused and paused[0] else None
            )
        conn.commit()
        result["worker_db_available"] = True
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return result


def run_once(
    transport=None,
    *,
    now_fn=time.time,
    rng=random.random,
    listener: Optional[IngestListener] = None,
    consent_listener: Optional[ConsentListener] = None,
    privacy_fence: Optional[PrivacyFence] = None,
    worker_id: Optional[str] = None,
    perform_cleanup: bool = True,
) -> str:
    cfg = x_capi.load_config()
    injected_test_transport = _injected_test_transport_allowed(transport)
    if not x_capi._db_url(worker=True) or not _worker_db_target_is_isolated():
        return "disabled"
    conn = x_capi.get_connection(worker=True)
    if not conn:
        return "db_unavailable"
    try:
        _configure_connection(conn)
        if not _schema_ready(conn):
            return "schema_unavailable"
        if not verify_config_guard(conn, cfg):
            return "config_guard_rejected"
        now = float(now_fn())
        # Inspect the privacy controls before accepting any more datagrams into
        # durable state.  In particular, a global/poison marker must make both
        # ingest and dispatch fail closed until an explicit epoch recovery.
        if privacy_fence is not None:
            privacy_prepare = privacy_fence.prepare(conn, now)
            if privacy_prepare != "clear":
                return privacy_prepare
        # The dedicated ConsentService is the sole consumer of the RPC socket;
        # it uses a separate connection so revocation and dispatch serialize at
        # PostgreSQL rather than in this polling loop. Datagram revocations are
        # still prioritized over events in their bounded batch.
        drain_ingest(listener, conn, now)
        if perform_cleanup:
            expire_and_cleanup(
                conn, now, cfg.max_event_age_hours, cfg.policy_version,
                policy_epoch=cfg.policy_epoch,
                audience_scope=cfg.audience_scope,
                context_limit=cfg.context_limit,
            )
        token = None
        if cfg.mode == "live":
            if not cfg.producer_ready:
                return "disabled"
            cipher, context_error = x_capi._context_cipher()
            if (
                context_error is not None
                or cipher is None
                or x_capi.keyed_internal_hash("readiness", "probe") is None
            ):
                return "disabled"
        if privacy_fence is None:
            # Direct unit callers may omit the OS fence only in the existing,
            # explicit test-secret mode. Production dispatch never does.
            boundary = (
                nullcontext(True)
                if os.getenv("X_CAPI_ALLOW_TEST_SECRETS") == "1"
                else nullcontext(False)
            )
        else:
            boundary = privacy_fence.dispatch_boundary()
        # The exclusive lock is the final local ordering boundary and remains
        # held across claim, the committed pre-send fence, HTTP, and completion.
        # A producer that publishes a marker first always wins; a stopped/dead
        # worker cannot be reclaimed by a peer until the OS releases this lock.
        with boundary as acquired:
            if not acquired:
                return "privacy_fence_unavailable"
            # Re-check the credential-authenticated local queue beneath the
            # same boundary that covers HTTP. Revocations in this batch are
            # sorted ahead of events and commit before a claim is selected.
            _ingest_outcomes, ingest_empty = drain_ingest_until_empty(
                listener, conn, float(now_fn())
            )
            if not ingest_empty:
                return "ingest_backlog"
            if privacy_fence is not None:
                privacy_state = privacy_fence.consume(conn, float(now_fn()))
                if privacy_state != "clear":
                    return privacy_state
            if cfg.mode != "live":
                return "maintenance"
            # Official X documentation currently supplies no normative
            # authenticity/provenance mechanism for twclid. The normal worker
            # invocation never crosses this boundary, regardless of live env
            # settings or token presence. Tests must inject a non-production
            # transport *and* enable the existing isolated-secret test guard.
            if not injected_test_transport:
                return LIVE_DELIVERY_BLOCK_REASON
            token, token_error = read_token()
            if token_error or token is None:
                return "disabled"
            if _is_paused(conn):
                return "paused"
            identity = worker_id or ("worker-" + os.urandom(8).hex())
            claim_now = float(now_fn())
            job = claim_job(conn, identity, claim_now)
            if not job:
                return "idle"
            decision = begin_dispatch(
                conn,
                job,
                float(now_fn()),
                cfg.policy_version,
                cfg.max_event_age_hours,
                cfg.policy_epoch,
                cfg.audience_scope,
                _deployment_id_hash(cfg),
            )
            if decision != "allowed":
                if decision not in ("lease_lost", "config_guard_rejected"):
                    finish_job(
                        conn, job, {"action": "cancel", "code": decision},
                        float(now_fn()), rng,
                    )
                return decision
            payload = build_payload(job)
            try:
                response = transport.send(
                    job["pixel_id"], token, payload
                )
                result = classify_response(response, float(now_fn()))
            except Exception as exc:
                code = (
                    "response_too_large"
                    if str(exc) == "response_too_large" else "transport_failure"
                )
                result = {
                    "action": "permanent" if code == "response_too_large" else "retry",
                    "code": code,
                }
            completed = finish_job(conn, job, result, float(now_fn()), rng)
            return str(result["action"]) if completed else "lease_lost"
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _handle_stop(_signum, _frame):
    global _stop
    _stop = True


def _wait_between_iterations(timeout: float) -> None:
    """Enforce pacing even with queued ingest or an unavailable database.

    No DB or dispatch lock is held here. The separate consent service remains
    responsive; ordinary ingest cannot bypass dispatch/error-retry pacing.
    """
    deadline = time.monotonic() + min(30.0, max(0.0, float(timeout)))
    while not _stop:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def activate_worker_startup(
    privacy_fence: PrivacyFence, lifecycle_token: str
) -> bool:
    """Advance the guard and consume durable denies after socket ownership.

    Every process restart preserves leases and queued work. The OS-level
    exclusive boundary prevents another sender or producer from crossing the
    targeted-marker drain performed here.
    """
    cfg = x_capi.load_config()
    if not x_capi._db_url(worker=True) or not _worker_db_target_is_isolated():
        return False
    conn = x_capi.get_connection(worker=True)
    if conn is None:
        return False
    try:
        _configure_connection(conn)
        if not _schema_ready(conn):
            return False
        if privacy_fence.controls_created:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM x_capi_config_guard)"
                )
                prior_generation = bool(cur.fetchone()[0])
            conn.commit()
            if prior_generation:
                with privacy_fence.exclusive_boundary() as acquired:
                    if not acquired or not privacy_fence.activate_global_quarantine():
                        return False
        deadline = time.monotonic() + 5.0
        while True:
            privacy_state = privacy_fence.prepare(conn, time.time())
            if privacy_state != "privacy_fence_pending":
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        if privacy_state == "privacy_global_quarantine":
            # Recovery is deliberately operator-directed and one shot.  The
            # DB generation/state transition commits before filesystem poison
            # can be cleared, all while the dispatch boundary is exclusive.
            with privacy_fence.exclusive_boundary() as acquired:
                if (
                    not acquired
                    or privacy_fence.prepare(
                        conn, time.time(), boundary_held=True
                    )
                    != "privacy_global_quarantine"
                    or not _recover_privacy_quarantine_generation(conn, cfg)
                    or not privacy_fence.reset_after_epoch_recovery()
                    or privacy_fence.prepare(conn, time.time()) != "clear"
                    or not _publish_durable_config_guard(conn)
                ):
                    return False
                begin_worker_lifecycle(conn, lifecycle_token)
                return privacy_fence.consume(conn, time.time()) == "clear"
        if privacy_state != "clear":
            return False
        if not enforce_config_guard(conn, cfg):
            return False
        deadline = time.monotonic() + 5.0
        with privacy_fence.exclusive_boundary() as acquired:
            if not acquired:
                return False
            # Persist this generation as DIRTY before it can dispatch. A prior
            # DIRTY lifecycle is diagnostic, not authority to discard stable
            # conversion IDs: complete targeted P records are consumed below,
            # and expired leases are reclaimed with a fresh per-claim fence.
            begin_worker_lifecycle(conn, lifecycle_token)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                privacy_state = privacy_fence.consume(conn, time.time())
                if privacy_state == "clear":
                    return True
                if privacy_state != "privacy_fence_pending":
                    return False
            return False
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def complete_worker_shutdown(
    listener: IngestListener,
    privacy_fence: PrivacyFence,
    lifecycle_token: str,
) -> bool:
    """Drain visible local work and durably mark a graceful stop CLEAN."""
    cfg = x_capi.load_config()
    conn = x_capi.get_connection(worker=True)
    if conn is None:
        return False
    try:
        _configure_connection(conn)
        if (
            not _schema_ready(conn)
            or not verify_config_guard(conn, cfg)
            or privacy_fence.prepare(conn, time.time()) != "clear"
        ):
            return False
        deadline = time.monotonic() + 5.0
        with privacy_fence.exclusive_boundary() as acquired:
            if not acquired:
                return False
            # Close the pathname before the last drain and keep the receiver fd
            # open while applying records already visible in the kernel queue.
            # A producer that connected just before unlink may still race this
            # diagnostic clean transition; optional loss is accepted because
            # core producers never wait on the CAPI acceptance control.
            if not listener.stop_accepting():
                return False
            # Drain bounded datagram batches until the socket is momentarily
            # empty. Invalid records are discarded by the listener itself.
            while time.monotonic() < deadline:
                _outcomes, ingest_empty = drain_ingest_until_empty(
                    listener, conn, time.time()
                )
                if ingest_empty:
                    break
            else:
                return False
            while time.monotonic() < deadline:
                privacy_state = privacy_fence.consume(conn, time.time())
                if privacy_state == "clear":
                    break
                if privacy_state != "privacy_fence_pending":
                    return False
            else:
                return False
            return mark_worker_lifecycle_clean(conn, lifecycle_token)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--validate-config" in args:
        readiness = worker_readiness()
        print(json.dumps(readiness, sort_keys=True))
        return 0 if readiness["worker_ready"] else 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s x-capi %(message)s")
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    listener = IngestListener()
    consent_listener = ConsentListener()
    privacy_fence = PrivacyFence()
    consent_service: Optional[ConsentService] = None
    lifecycle_token = str(uuid.uuid4())
    try:
        privacy_fence.open()
        listener.open()
        consent_listener.open()
        if not activate_worker_startup(privacy_fence, lifecycle_token):
            raise RuntimeError("worker_startup_privacy_activation_failed")
        consent_service = ConsentService(
            consent_listener, privacy_path=privacy_fence.path
        )
        consent_service.start()
    except Exception as exc:
        if consent_service is not None:
            consent_service.stop()
        consent_listener.close()
        listener.close()
        privacy_fence.close()
        logger.error("x_capi local socket unavailable (%s)", type(exc).__name__)
        return 2
    worker_id = "worker-" + os.urandom(16).hex()
    next_cleanup = 0.0
    failure_delay = 1.0
    try:
        while not _stop:
            if consent_service is None or not consent_service.is_healthy():
                logger.error("x_capi consent service unavailable")
                return 2
            perform_cleanup = time.monotonic() >= next_cleanup
            try:
                outcome = run_once(
                    listener=listener,
                    privacy_fence=privacy_fence,
                    worker_id=worker_id,
                    perform_cleanup=perform_cleanup,
                )
                failure_delay = 1.0
            except Exception as exc:
                logger.error("x_capi worker iteration failed (%s)", type(exc).__name__)
                outcome = "iteration_error"
                failure_delay = min(30.0, failure_delay * 2.0)
            if perform_cleanup:
                next_cleanup = time.monotonic() + (
                    failure_delay if outcome == "iteration_error" else 60.0
                )
            if outcome in ("disabled", "paused", "maintenance"):
                delay = 1.0  # keep local revoke/event ingestion responsive
            elif outcome == LIVE_DELIVERY_BLOCK_REASON:
                delay = 1.0  # keep local revocation/maintenance responsive
            elif outcome == "config_guard_rejected":
                delay = 5.0
            elif outcome in ("idle", "db_unavailable", "schema_unavailable"):
                delay = 1.0
            elif outcome == "iteration_error":
                delay = failure_delay
            else:
                # Four attempts/sec remains below each 500-row/minute terminal
                # and dedupe cleanup budget, so steady-state retention cannot
                # grow faster than the bounded cleanup can retire it.
                delay = MIN_ATTEMPT_INTERVAL_SECONDS
            _wait_between_iterations(delay)
        return 0
    finally:
        consent_stopped = bool(
            consent_service is not None
            and consent_service.stop(drain=True)
            and consent_service.failure_type is None
        )
        if consent_service is not None and not consent_stopped:
            logger.error("x_capi consent service did not stop cleanly")
        if _stop and consent_stopped and not complete_worker_shutdown(
            listener, privacy_fence, lifecycle_token
        ):
            logger.error("x_capi worker remained dirty during shutdown")
        consent_listener.close()
        listener.close()
        privacy_fence.close()


if __name__ == "__main__":
    raise SystemExit(main())
