"""Worker-only transactional storage for the frozen handoff v2 contract.

Every method uses a caller-owned transaction and NEVER commits or performs I/O
outside PostgreSQL. The worker owns mode/config checks and the exclusive privacy
boundary before closure. Only HandoffError(durable_error=True) carries a quarantine that must commit.
Other errors roll back, including positive admission and expiry side effects.
No raw owner or handoff capability is persisted. This module has no connection
factory and must never be imported as a source of gate database credentials.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    from .x_capi_handoff import HandoffError, canonical_json, new_token, token_hash
    from .x_capi import _validate_twclid_unscoped
except ImportError:
    from x_capi_handoff import HandoffError, canonical_json, new_token, token_hash
    from x_capi import _validate_twclid_unscoped

SOURCE_WINDOW_MS = 604790000
OWNER_WINDOW_MS = 604800000
OBSERVATION_WINDOW_MS = 110000
TRANSACTION_WINDOW_MS = 300000
PROVISIONAL_WINDOW_MS = 120000
RETENTION_MS = 7862400000
MAX_INTEGER = 9007199254740991
TABLE_PRIVILEGES = {
    "x_capi_handoff_generations": frozenset(("SELECT", "INSERT", "UPDATE")),
    **{f"x_capi_handoff_{name}": frozenset(("SELECT", "INSERT", "UPDATE", "DELETE"))
       for name in ("sources", "app_owners", "transactions", "imports")},
}


def _row(cur):
    value = cur.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        result = dict(value)
    else:
        result = dict(zip((item[0] for item in cur.description), value))
    return {key: bytes(val) if isinstance(val, memoryview) else val
            for key, val in result.items()}


def _hash(value: str) -> bytes:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise HandoffError("INVALID_REQUEST")
    return bytes.fromhex(value)


def _digest(req):
    return hashlib.sha256(canonical_json(req)).digest()


def _same(existing, request_id, digest, id_field, digest_field):
    if str(existing.get(id_field)) == request_id:
        if not hmac.compare_digest(existing.get(digest_field) or b"", digest):
            raise HandoffError("IDEMPOTENCY_CONFLICT")
        return True
    return False


class HandoffStore:
    """Bounded worker storage. Explicit generation provisioning is not HTTP RPC."""

    def __init__(self, generation, epoch, audience_scope, source_keys,
                 active_key_id, clock_ms=None, limits=None):
        self.generation = str(uuid.UUID(generation))
        self.epoch = epoch
        self.audience = _hash(audience_scope)
        self.source_keys = dict(source_keys)
        self.active_key_id = active_key_id
        if type(epoch) is not int or not 1 <= epoch <= 2147483647:
            raise ValueError("invalid handoff epoch")
        if active_key_id not in self.source_keys or any(len(k) != 32 for k in self.source_keys.values()):
            raise ValueError("invalid source data keyring")
        # An injected clock is exclusively an in-process synthetic-test seam.
        # Production lifecycle decisions use PostgreSQL wall time, sampled after
        # the relevant locks; transaction_timestamp()/now() freeze before waits.
        self.clock_ms = clock_ms
        self.limits = {"sources": 10000, "app_owners": 10000, "transactions": 40000}
        for key, value in (limits or {}).items():
            if key not in self.limits or type(value) is not int or not 1 <= value <= self.limits[key]:
                raise ValueError("limits can only reduce the frozen synthetic caps")
            self.limits[key] = value

    def _now(self, cur=None):
        if self.clock_ms is None:
            if cur is None:
                raise HandoffError("CLOCK_UNTRUSTED")
            cur.execute("SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint")
            now = cur.fetchone()[0]
        else:
            now = self.clock_ms()
        if type(now) is not int or not 1 <= now <= MAX_INTEGER - RETENTION_MS:
            raise HandoffError("CLOCK_UNTRUSTED")
        return now

    def generation_guard(self, cur, *, exclusive=False, positive=True):
        cur.execute("SELECT * FROM x_capi_handoff_generations WHERE generation=%s "
                    + ("FOR UPDATE" if exclusive else "FOR KEY SHARE"), (self.generation,))
        row = _row(cur)
        if row is None or row["policy_epoch"] != self.epoch or row["audience_scope"] != self.audience:
            raise HandoffError("QUARANTINED")
        if positive and row["state"] != "active":
            raise HandoffError("GENERATION_RETIRED" if row["state"] == "retired" else "QUARANTINED")
        return row

    def provision_generation(self, cur, *, activate=False):
        """Operator/test-only provisioning after external restore/config attestation.

        Missing rows are never automatically treated as an empty healthy queue.
        Quarantined/retired generations cannot be reopened by this helper.
        """
        now = self._now(cur)
        cur.execute("SELECT * FROM x_capi_handoff_generations WHERE generation=%s FOR UPDATE", (self.generation,))
        if cur.fetchone() is not None:
            raise HandoffError("QUARANTINED")
        cur.execute("SELECT max_policy_epoch,audience_scope FROM x_capi_config_guard WHERE singleton=TRUE FOR UPDATE")
        guard = cur.fetchone()
        if not guard or guard[0] != self.epoch or str(guard[1]) != self.audience.hex():
            raise HandoffError("QUARANTINED")
        cur.execute("""INSERT INTO x_capi_handoff_generations
            (generation,policy_epoch,audience_scope,state,landing_control_version,
             source_count,app_owner_count,transaction_count,created_at_ms,updated_at_ms)
            VALUES (%s,%s,%s,%s,0,0,0,0,%s,%s)""",
                    (self.generation,self.epoch,self.audience,"active" if activate else "quarantined",now,now))

    def quarantine(self, cur):
        self.generation_guard(cur, exclusive=True, positive=False)
        cur.execute("UPDATE x_capi_handoff_generations SET state='quarantined',updated_at_ms=%s "
                    "WHERE generation=%s AND state='active'", (self._now(cur),self.generation))

    def control_version(self, cur, version):
        self.generation_guard(cur,exclusive=True)
        if type(version) is not int or not 0 <= version <= MAX_INTEGER:
            raise HandoffError("INVALID_REQUEST")
        cur.execute("""UPDATE x_capi_handoff_generations SET landing_control_version=%s,
            updated_at_ms=%s WHERE generation=%s AND landing_control_version<=%s
            RETURNING landing_control_version""", (version,self._now(cur),self.generation,version))
        if cur.fetchone() is None:
            self.quarantine(cur)
            raise HandoffError("QUARANTINED",durable_error=True)

    def _admit(self, cur, kind):
        column = {"sources":"source_count", "app_owners":"app_owner_count", "transactions":"transaction_count"}[kind]
        # Caps cover retained rows in every generation, not a fresh allowance
        # after rotation. Serialize all admissions, including close-before-capture
        # tombstones in a quarantined/retired scope. No admission path takes a
        # new source/owner lock after this lock; cleanup only decreases counters.
        capacity_lock = int.from_bytes(hashlib.sha256(b"handoff-total-capacity\x00").digest()[:8],"big",signed=True)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (capacity_lock,))
        cur.execute(f"UPDATE x_capi_handoff_generations SET {column}={column}+1 WHERE generation=%s "
                    f"AND {column}<%s-COALESCE((SELECT sum({column}) FROM x_capi_handoff_generations "
                    f"WHERE generation<>%s),0) RETURNING {column}",
                    (self.generation,self.limits[kind],self.generation))
        if cur.fetchone() is None:
            raise HandoffError("RATE_LIMITED")

    def _source(self, cur, owner):
        # Missing-row lock serializes capture against a close-before-capture insert.
        lock = int.from_bytes(hashlib.sha256(b"handoff-source\x00" + self.generation.encode() + owner).digest()[:8], "big", signed=True)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
        cur.execute("SELECT * FROM x_capi_handoff_sources WHERE generation=%s AND landing_owner_hash=%s FOR UPDATE", (self.generation,owner))
        return _row(cur)

    def _request_identity(self, cur, operation, req, identity):
        # Source identity is already locked. Same operation IDs on different
        # sources serialize before a unique-index conflict aborts a transaction.
        # This coordination lock stores neither a request body nor a capability.
        key = int.from_bytes(hashlib.sha256(b"handoff-request\x00" + operation.encode()
              + self.generation.encode() + req["request_id"].encode()).digest()[:8],"big",signed=True)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        table, prefix, column = {
            "capture":("sources","capture","landing_owner_hash"),
            "authorize":("transactions","authorize","transaction_hash"),
            "revoke":("sources","close","landing_owner_hash"),
        }[operation]
        cur.execute(f"SELECT {column},{prefix}_request_digest FROM x_capi_handoff_{table} "
                    f"WHERE generation=%s AND {prefix}_request_id=%s", (self.generation,req["request_id"]))
        row = cur.fetchone()
        if row and (bytes(row[0]) != identity or not hmac.compare_digest(bytes(row[1]),_digest(req))):
            raise HandoffError("IDEMPOTENCY_CONFLICT")

    def _owner(self, cur, owner):
        cur.execute("SELECT * FROM x_capi_handoff_app_owners WHERE generation=%s AND app_owner_hash=%s FOR UPDATE", (self.generation,owner))
        row = _row(cur)
        if row is None:
            raise HandoffError("OWNER_REQUIRED")
        return row

    def _transaction(self, cur, hashed):
        cur.execute("SELECT * FROM x_capi_handoff_transactions WHERE generation=%s AND transaction_hash=%s FOR UPDATE", (self.generation,hashed))
        row = _row(cur)
        if row is None:
            raise HandoffError("RESULT_UNAVAILABLE")
        return row

    def _source_live(self, row, now):
        if row is None:
            raise HandoffError("RESULT_UNAVAILABLE")
        if row["state"] == "closed":
            raise HandoffError("WITHDRAWN")
        if row["state"] == "expired" or row["original_expires_at_ms"] <= now:
            raise HandoffError("EXPIRED")

    def _owner_live(self, row, now, *, confirm=False):
        if row["state"] == "closed":
            raise HandoffError("WITHDRAWN")
        if row["state"] == "expired" or row["owner_expires_at_ms"] <= now:
            raise HandoffError("EXPIRED")
        if row["cookie_confirmed_at_ms"] is None and row["provisional_expires_at_ms"] <= now:
            raise HandoffError("EXPIRED")

    def _tx_live(self, row, now):
        if row["state"] == "cancelled":
            raise HandoffError("WITHDRAWN")
        if row["state"] == "expired" or row["expires_at_ms"] <= now:
            raise HandoffError("EXPIRED")

    def _source_response(self, req, **result):
        return dict(v=2,ok=True,epoch=self.epoch,generation=self.generation,request_id=req["request_id"],**result)

    def _scope(self, req):
        if req.get("generation") != self.generation or req.get("epoch") != self.epoch:
            raise HandoffError("GENERATION_RETIRED")

    def _aad(self, owner, observation):
        return canonical_json(dict(v=2,epoch=self.epoch,generation=self.generation,
                                   owner_hash=owner.hex(),observation_id=str(observation),purpose="pending-source"))

    def capture(self, cur, req):
        self._scope(req)
        if _validate_twclid_unscoped(req.get("twclid")) is None:
            raise HandoffError("INVALID_CLICK")
        self.generation_guard(cur)
        owner, digest = _hash(req["owner_hash"]), _digest(req)
        row = self._source(cur,owner)
        self._request_identity(cur,"capture",req,owner)
        now = self._now(cur)
        if row:
            self._source_live(row,now)
            if _same(row,req["request_id"],digest,"capture_request_id","capture_request_digest"):
                return self._source_response(req,state="captured",expires_at_ms=row["original_expires_at_ms"])
            raise HandoffError("FIRST_TOUCH_LOCKED")
        observed, consented, expires = req["observed_at_ms"],req["landing_consented_at_ms"],req["original_expires_at_ms"]
        if (expires != observed + SOURCE_WINDOW_MS or not observed <= consented <= now
                or not observed <= now < observed + OBSERVATION_WINDOW_MS):
            raise HandoffError("EXPIRED")
        cur.execute("SELECT capture_request_digest FROM x_capi_handoff_sources WHERE generation=%s AND capture_request_id=%s", (self.generation,req["request_id"]))
        if cur.fetchone():
            raise HandoffError("IDEMPOTENCY_CONFLICT")
        self._admit(cur,"sources")
        now = self._now(cur)
        if not observed <= consented <= now < observed + OBSERVATION_WINDOW_MS:
            raise HandoffError("EXPIRED")
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.source_keys[self.active_key_id]).encrypt(nonce,req["twclid"].encode("ascii"),self._aad(owner,req["observation_id"]))
        cur.execute("""INSERT INTO x_capi_handoff_sources
            (generation,policy_epoch,landing_owner_hash,state,capture_request_id,capture_request_digest,
             observation_id,observed_at_ms,landing_consented_at_ms,original_expires_at_ms,
             landing_policy_version,twclid_ciphertext,encryption_nonce,encryption_key_id,
             revocation_version,created_at_ms,updated_at_ms)
            VALUES (%s,%s,%s,'captured',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s)""",
            (self.generation,self.epoch,owner,req["request_id"],digest,req["observation_id"],observed,consented,expires,
             req["policy"],ciphertext,nonce,self.active_key_id,now,now))
        return self._source_response(req,state="captured",expires_at_ms=expires)

    def status(self, cur, req):
        self._scope(req)
        self.generation_guard(cur,positive=False)
        row = self._source(cur,_hash(req["owner_hash"]))
        state = row["state"] if row else "none"
        if row and state not in ("closed","expired") and row["original_expires_at_ms"] <= self._now(cur):
            state = "expired"
        return self._source_response(req,state=state,expires_at_ms=row["original_expires_at_ms"] if row else None)

    def prepare(self, cur, owner_hash, req, new_owner=False):
        self.generation_guard(cur)
        owner, digest = _hash(owner_hash), _digest(req)
        # Same-ID preparations serialize even when the first owner row is absent.
        # Do not take a generation counter lock before an owner: begin takes the
        # owner first and would otherwise form an owner/counter lock cycle.
        prepare_lock = int.from_bytes(hashlib.sha256(b"handoff-prepare\x00" + self.generation.encode() + req["request_id"].encode()).digest()[:8], "big", signed=True)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (prepare_lock,))
        cur.execute("SELECT * FROM x_capi_handoff_app_owners WHERE generation=%s AND prepare_request_id=%s", (self.generation,req["request_id"]))
        prior = _row(cur)
        if prior:
            _same(prior,req["request_id"],digest,"prepare_request_id","prepare_request_digest")
            if new_owner or prior["app_owner_hash"] != owner:
                raise HandoffError("RESULT_UNAVAILABLE")
        cur.execute("SELECT * FROM x_capi_handoff_app_owners WHERE generation=%s AND app_owner_hash=%s FOR UPDATE", (self.generation,owner))
        row = _row(cur)
        now = self._now(cur)
        if row:
            if new_owner:
                raise HandoffError("RESULT_UNAVAILABLE")
            self._owner_live(row,now)
            return dict(v=2,ok=True,state="prepared",expires_at_ms=row["owner_expires_at_ms"])
        if not new_owner:
            raise HandoffError("OWNER_REQUIRED")
        self._admit(cur,"app_owners")
        now = self._now(cur)
        expires = now + OWNER_WINDOW_MS
        cur.execute("""INSERT INTO x_capi_handoff_app_owners
            (generation,policy_epoch,app_owner_hash,state,prepare_request_id,prepare_request_digest,
             created_at_ms,provisional_expires_at_ms,owner_expires_at_ms,open_transaction_count,updated_at_ms)
            VALUES (%s,%s,%s,'prepared',%s,%s,%s,%s,%s,0,%s)""",
            (self.generation,self.epoch,owner,req["request_id"],digest,now,now+PROVISIONAL_WINDOW_MS,expires,now))
        return dict(v=2,ok=True,state="prepared",expires_at_ms=expires)

    def _expire_owner_transactions(self, cur, owner, now):
        cur.execute("""UPDATE x_capi_handoff_transactions SET state='expired',updated_at_ms=%s
            WHERE generation=%s AND app_owner_hash=%s AND state IN ('app_granted','source_approved')
              AND expires_at_ms<=%s RETURNING transaction_hash""", (now,self.generation,owner,now))
        expired = len(cur.fetchall())
        if expired:
            cur.execute("UPDATE x_capi_handoff_app_owners SET open_transaction_count=open_transaction_count-%s WHERE generation=%s AND app_owner_hash=%s AND open_transaction_count>=%s", (expired,self.generation,owner,expired))
            if cur.rowcount != 1:
                raise HandoffError("QUARANTINED")

    def begin(self, cur, owner_hash, req):
        self.generation_guard(cur)
        owner, digest = _hash(owner_hash),_digest(req)
        row = self._owner(cur,owner)
        now = self._now(cur)
        self._owner_live(row,now)
        cur.execute("SELECT * FROM x_capi_handoff_transactions WHERE generation=%s AND app_owner_hash=%s AND begin_request_id=%s", (self.generation,owner,req["request_id"]))
        prior = _row(cur)
        if prior:
            _same(prior,req["request_id"],digest,"begin_request_id","begin_request_digest")
            raise HandoffError("RESULT_UNAVAILABLE")
        if row["state"] != "prepared":
            raise HandoffError("FIRST_TOUCH_LOCKED")
        self._expire_owner_transactions(cur,owner,now)
        self._admit(cur,"transactions")
        now = self._now(cur)
        self._owner_live(row,now)
        cur.execute("""UPDATE x_capi_handoff_app_owners SET open_transaction_count=open_transaction_count+1,
            cookie_confirmed_at_ms=COALESCE(cookie_confirmed_at_ms,%s),updated_at_ms=%s
            WHERE generation=%s AND app_owner_hash=%s AND open_transaction_count<4 RETURNING owner_expires_at_ms""", (now,now,self.generation,owner))
        admitted = cur.fetchone()
        if not admitted:
            # Reverse only this failed positive admission; no accepted closure is lost.
            cur.execute("UPDATE x_capi_handoff_generations SET transaction_count=transaction_count-1 WHERE generation=%s", (self.generation,))
            raise HandoffError("RATE_LIMITED")
        handoff = new_token()
        expires = min(now + TRANSACTION_WINDOW_MS,admitted[0])
        cur.execute("""INSERT INTO x_capi_handoff_transactions
            (transaction_hash,generation,policy_epoch,app_owner_hash,state,begin_request_id,begin_request_digest,
             app_policy_version,app_consented_at_ms,created_at_ms,expires_at_ms,updated_at_ms)
            VALUES (%s,%s,%s,%s,'app_granted',%s,%s,%s,%s,%s,%s,%s)""",
            (_hash(token_hash(handoff,"handoff")),self.generation,self.epoch,owner,req["request_id"],digest,req["policy"],now,now,expires,now))
        return dict(v=2,ok=True,state="app_granted",handoff=handoff,expires_at_ms=expires)

    def authorize(self, cur, req):
        self._scope(req)
        self.generation_guard(cur)
        owner,hashed,digest = _hash(req["owner_hash"]),_hash(token_hash(req["handoff"],"handoff")),_digest(req)
        source = self._source(cur,owner)
        self._request_identity(cur,"authorize",req,hashed)
        # Preserve the source error precedence, then recheck all lifetimes after
        # the app/transaction locks below; this early check is not authority.
        self._source_live(source,self._now(cur))
        cur.execute("SELECT app_owner_hash FROM x_capi_handoff_transactions WHERE generation=%s AND transaction_hash=%s", (self.generation,hashed))
        located = cur.fetchone()
        if not located:
            raise HandoffError("RESULT_UNAVAILABLE")
        app = self._owner(cur,bytes(located[0]))
        tx = self._transaction(cur,hashed)
        now = self._now(cur)
        self._source_live(source,now)
        self._owner_live(app,now)
        self._tx_live(tx,now)
        if tx["app_owner_hash"] != app["app_owner_hash"]:
            raise HandoffError("OWNER_MISMATCH")
        if tx["authorize_request_id"] is not None:
            _same(tx,req["request_id"],digest,"authorize_request_id","authorize_request_digest")
            if tx["landing_owner_hash"] != owner:
                raise HandoffError("SOURCE_ALREADY_ASSIGNED")
            if source["assigned_transaction_hash"] != hashed:
                raise HandoffError("SOURCE_ALREADY_ASSIGNED")
            return self._source_response(req,state="source_approved",expires_at_ms=tx["expires_at_ms"])
        if source["state"] == "assigned":
            cur.execute("SELECT expires_at_ms,state FROM x_capi_handoff_transactions WHERE transaction_hash=%s", (source["assigned_transaction_hash"],))
            old = cur.fetchone()
            if old and old[0] <= now and old[1] != "redeemed":
                # Assignment can release after actual original H expiry; never on navigation loss.
                cur.execute("UPDATE x_capi_handoff_sources SET state='captured',assigned_transaction_hash=NULL,updated_at_ms=%s WHERE generation=%s AND landing_owner_hash=%s", (now,self.generation,owner))
                source["state"] = "captured"
        if source["state"] != "captured" or app["state"] != "prepared":
            raise HandoffError("SOURCE_ALREADY_ASSIGNED")
        expires = min(tx["expires_at_ms"],source["original_expires_at_ms"],app["owner_expires_at_ms"])
        now = self._now(cur)
        if expires <= now:
            raise HandoffError("EXPIRED")
        cur.execute("""UPDATE x_capi_handoff_transactions SET state='source_approved',landing_owner_hash=%s,
            authorize_request_id=%s,authorize_request_digest=%s,expires_at_ms=%s,updated_at_ms=%s
            WHERE transaction_hash=%s""", (owner,req["request_id"],digest,expires,now,hashed))
        cur.execute("UPDATE x_capi_handoff_sources SET state='assigned',assigned_transaction_hash=%s,updated_at_ms=%s WHERE generation=%s AND landing_owner_hash=%s", (hashed,now,self.generation,owner))
        cur.execute("UPDATE x_capi_handoff_app_owners SET owner_expires_at_ms=LEAST(owner_expires_at_ms,%s),updated_at_ms=%s WHERE generation=%s AND app_owner_hash=%s", (source["original_expires_at_ms"],now,self.generation,app["app_owner_hash"]))
        return self._source_response(req,state="source_approved",expires_at_ms=expires)

    def _located_transaction(self, cur, owner_hash, handoff):
        hashed = _hash(token_hash(handoff,"handoff"))
        owner = _hash(owner_hash)
        cur.execute("SELECT landing_owner_hash,app_owner_hash FROM x_capi_handoff_transactions WHERE generation=%s AND transaction_hash=%s", (self.generation,hashed))
        hint = cur.fetchone()
        if not hint:
            raise HandoffError("RESULT_UNAVAILABLE")
        if bytes(hint[1]) != owner:
            raise HandoffError("OWNER_MISMATCH")
        if hint[0] is None:
            raise HandoffError("RESULT_UNAVAILABLE")
        source = self._source(cur,bytes(hint[0]))
        app = self._owner(cur,owner)
        tx = self._transaction(cur,hashed)
        if tx["landing_owner_hash"] != source["landing_owner_hash"] or tx["app_owner_hash"] != owner:
            raise HandoffError("OWNER_MISMATCH")
        return source,app,tx

    def _recovery(self, cur, source, app, tx, recover_context):
        cur.execute("SELECT * FROM x_capi_handoff_imports WHERE transaction_hash=%s FOR UPDATE", (tx["transaction_hash"],))
        imported = _row(cur)
        now = self._now(cur)
        self._source_live(source,now)
        self._owner_live(app,now)
        self._tx_live(tx,now)
        if not imported or imported["state"] != "active":
            raise HandoffError("WITHDRAWN")
        if imported["app_owner_hash"] != app["app_owner_hash"]:
            raise HandoffError("OWNER_MISMATCH")
        if not imported["context_ciphertext"] or imported["result_recover_until_ms"] <= now:
            raise HandoffError("RESULT_UNAVAILABLE")
        recovered = recover_context(imported["context_ciphertext"])
        return dict(v=2,ok=True,state="redeemed",context=imported["context_ciphertext"],csrf=recovered["csrf"],
                    ownership_id=str(imported["ownership_id"]),expires_at_ms=imported["effective_expires_at_ms"])

    def redeem(self, cur, owner_hash, handoff, create_context, recover_context):
        self.generation_guard(cur)
        source,app,tx = self._located_transaction(cur,owner_hash,handoff)
        now = self._now(cur)
        self._source_live(source,now)
        self._owner_live(app,now)
        self._tx_live(tx,now)
        if tx["state"] == "redeemed":
            return self._recovery(cur,source,app,tx,recover_context)
        if tx["state"] != "source_approved" or source["state"] != "assigned" or source["assigned_transaction_hash"] != tx["transaction_hash"]:
            raise HandoffError("RESULT_UNAVAILABLE")
        if app["state"] != "prepared":
            raise HandoffError("FIRST_TOUCH_LOCKED")
        if now < max(source["observed_at_ms"],source["landing_consented_at_ms"],tx["app_consented_at_ms"]):
            raise HandoffError("CLOCK_UNTRUSTED")
        effective = min(source["original_expires_at_ms"],app["owner_expires_at_ms"])
        key = self.source_keys.get(source["encryption_key_id"])
        if key is None:
            raise HandoffError("QUARANTINED")
        try:
            click = AESGCM(key).decrypt(source["encryption_nonce"],source["twclid_ciphertext"],
                        self._aad(source["landing_owner_hash"],source["observation_id"])).decode("ascii")
        except Exception:
            raise HandoffError("QUARANTINED") from None
        # Context creation reserves the existing CAPI capacity row. Acquire it
        # before choosing the immutable import decision time, so a capacity wait
        # cannot admit an expired handoff or retroactively date the new import.
        cur.execute("SELECT singleton FROM x_capi_capacity WHERE singleton=TRUE FOR UPDATE")
        if cur.fetchone() is None:
            raise HandoffError("QUARANTINED")
        now = self._now(cur)
        self._source_live(source,now)
        self._owner_live(app,now)
        self._tx_live(tx,now)
        if now < max(source["observed_at_ms"],source["landing_consented_at_ms"],tx["app_consented_at_ms"]):
            raise HandoffError("CLOCK_UNTRUSTED")
        context_source = dict(source,twclid=click)
        result = create_context(cur,context_source,tx,now,effective)
        # A callback must never make a late mutation durable. On expiry the
        # caller rolls back the context allocation and the entire import.
        checked_at = self._now(cur)
        self._source_live(source,checked_at)
        self._owner_live(app,checked_at)
        self._tx_live(tx,checked_at)
        cur.execute("""INSERT INTO x_capi_handoff_imports
            (context_id,ownership_id,context_handle_hash,transaction_hash,generation,policy_epoch,
             landing_owner_hash,app_owner_hash,observed_at_ms,landing_consented_at_ms,app_consented_at_ms,
             import_cutoff_ms,original_expires_at_ms,effective_expires_at_ms,state,context_ciphertext,
             result_recover_until_ms,created_at_ms,updated_at_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s)""",
            (result["context_id"],result["ownership_id"],result["context_handle_hash"],tx["transaction_hash"],
             self.generation,self.epoch,source["landing_owner_hash"],app["app_owner_hash"],source["observed_at_ms"],
             source["landing_consented_at_ms"],tx["app_consented_at_ms"],now,source["original_expires_at_ms"],effective,
             result["context_ciphertext"],min(tx["expires_at_ms"],effective),now,now))
        cur.execute("UPDATE x_capi_handoff_sources SET state='consumed',twclid_ciphertext=NULL,encryption_nonce=NULL,encryption_key_id=NULL,updated_at_ms=%s WHERE generation=%s AND landing_owner_hash=%s", (now,self.generation,source["landing_owner_hash"]))
        cur.execute("UPDATE x_capi_handoff_transactions SET state='redeemed',redeemed_at_ms=%s,updated_at_ms=%s WHERE transaction_hash=%s", (now,now,tx["transaction_hash"]))
        cur.execute("UPDATE x_capi_handoff_app_owners SET state='imported',open_transaction_count=open_transaction_count-1,updated_at_ms=%s WHERE generation=%s AND app_owner_hash=%s AND open_transaction_count>0", (now,self.generation,app["app_owner_hash"]))
        if cur.rowcount != 1:
            raise HandoffError("QUARANTINED")
        # Last transactional check catches expiry during insertion/constraint
        # work as well; any error rolls back every allocation and state change.
        checked_at = self._now(cur)
        self._source_live(source,checked_at)
        self._owner_live(app,checked_at)
        self._tx_live(tx,checked_at)
        return dict(v=2,ok=True,state="redeemed",context=result["context_ciphertext"],csrf=result["csrf"],
                    ownership_id=str(result["ownership_id"]),expires_at_ms=effective)

    def recover(self, cur, owner_hash, handoff, recover_context):
        self.generation_guard(cur)
        source,app,tx = self._located_transaction(cur,owner_hash,handoff)
        if tx["state"] != "redeemed":
            raise HandoffError("RESULT_UNAVAILABLE")
        return self._recovery(cur,source,app,tx,recover_context)

    def _close_source(self, cur, row, reason, now, close_context):
        owner = row["landing_owner_hash"]
        cur.execute("SELECT * FROM x_capi_handoff_imports WHERE generation=%s AND landing_owner_hash=%s", (self.generation,owner))
        imported = _row(cur)
        if imported:
            self._owner(cur,imported["app_owner_hash"])
            self._transaction(cur,imported["transaction_hash"])
            close_context(cur,str(imported["context_id"]))
            cur.execute("UPDATE x_capi_handoff_imports SET state='closed',context_ciphertext=NULL,updated_at_ms=%s WHERE context_id=%s", (now,imported["context_id"]))
        elif row["assigned_transaction_hash"]:
            cur.execute("SELECT app_owner_hash FROM x_capi_handoff_transactions WHERE transaction_hash=%s", (row["assigned_transaction_hash"],))
            located = cur.fetchone()
            if located:
                self._owner(cur,bytes(located[0]))
                tx = self._transaction(cur,row["assigned_transaction_hash"])
                if tx["state"] in ("app_granted","source_approved"):
                    cur.execute("UPDATE x_capi_handoff_app_owners SET open_transaction_count=open_transaction_count-1 WHERE generation=%s AND app_owner_hash=%s AND open_transaction_count>0", (self.generation,tx["app_owner_hash"]))
                    cur.execute("UPDATE x_capi_handoff_transactions SET state='cancelled',cancelled_reason=%s,updated_at_ms=%s WHERE transaction_hash=%s", (reason,now,tx["transaction_hash"]))
        cur.execute("""UPDATE x_capi_handoff_sources SET state='closed',twclid_ciphertext=NULL,
            encryption_nonce=NULL,encryption_key_id=NULL,revocation_version=revocation_version+1,
            closed_reason=%s,closed_at_ms=%s,tombstone_until_ms=GREATEST(%s,COALESCE(original_expires_at_ms,0)),updated_at_ms=%s
            WHERE generation=%s AND landing_owner_hash=%s AND state<>'closed'""",
            (reason,now,now+RETENTION_MS,now,self.generation,owner))

    def revoke(self, cur, req, close_context):
        self._scope(req)
        # Exclusive generation guard permits bounded closure/quarantine without
        # lock upgrades against concurrent admission.
        self.generation_guard(cur,exclusive=True,positive=False)
        now,owner,digest = self._now(cur),_hash(req["owner_hash"]),_digest(req)
        row = self._source(cur,owner)
        self._request_identity(cur,"revoke",req,owner)
        if row:
            _same(row,req["request_id"],digest,"close_request_id","close_request_digest")
            if row["state"] == "closed":
                return self._source_response(req,state="withdrawn")
            self._close_source(cur,row,req["reason"],now,close_context)
            cur.execute("UPDATE x_capi_handoff_sources SET close_request_id=%s,close_request_digest=%s WHERE generation=%s AND landing_owner_hash=%s", (req["request_id"],digest,self.generation,owner))
        else:
            try:
                self._admit(cur,"sources")
            except HandoffError:
                self.quarantine(cur)
                raise HandoffError("QUARANTINED", durable_error=True) from None
            cur.execute("""INSERT INTO x_capi_handoff_sources
                (generation,policy_epoch,landing_owner_hash,state,revocation_version,close_request_id,
                 close_request_digest,closed_reason,closed_at_ms,tombstone_until_ms,created_at_ms,updated_at_ms)
                VALUES (%s,%s,%s,'closed',1,%s,%s,%s,%s,%s,%s,%s)""",
                (self.generation,self.epoch,owner,req["request_id"],digest,req["reason"],now,now+RETENTION_MS,now,now))
        return self._source_response(req,state="withdrawn")

    def withdraw(self, cur, owner_hash, req, close_context):
        self.generation_guard(cur,exclusive=True,positive=False)
        now,owner = self._now(cur),_hash(owner_hash)
        # Exclusive generation guard fixes the complete source set before owner lock.
        cur.execute("SELECT DISTINCT landing_owner_hash FROM x_capi_handoff_transactions WHERE generation=%s AND app_owner_hash=%s AND landing_owner_hash IS NOT NULL ORDER BY landing_owner_hash", (self.generation,owner))
        sources = [self._source(cur,bytes(row[0])) for row in cur.fetchall()]
        app = self._owner(cur,owner)
        if app["state"] == "closed":
            return dict(v=2,ok=True,state="withdrawn",request_id=req["request_id"])
        for source in sources:
            if source and source["state"] != "closed":
                self._close_source(cur,source,req["reason"],now,close_context)
        cur.execute("UPDATE x_capi_handoff_transactions SET state='cancelled',cancelled_reason=%s,updated_at_ms=%s WHERE generation=%s AND app_owner_hash=%s AND state IN ('app_granted','source_approved')", (req["reason"],now,self.generation,owner))
        cur.execute("UPDATE x_capi_handoff_app_owners SET state='closed',closed_at_ms=%s,open_transaction_count=0,updated_at_ms=%s WHERE generation=%s AND app_owner_hash=%s", (now,now,self.generation,owner))
        return dict(v=2,ok=True,state="withdrawn",request_id=req["request_id"])

    def lock_import(self, cur, handle_hash, owner_hash=None, positive=True):
        """Source-first locks before existing handle/wallet locks; no legacy fallback."""
        self.generation_guard(cur,positive=positive)
        cur.execute("SELECT landing_owner_hash,app_owner_hash,transaction_hash FROM x_capi_handoff_imports WHERE generation=%s AND context_handle_hash=%s", (self.generation,handle_hash))
        located = cur.fetchone()
        if not located:
            raise HandoffError("RESULT_UNAVAILABLE")
        source = self._source(cur,bytes(located[0]))
        app = self._owner(cur,bytes(located[1]))
        tx = self._transaction(cur,bytes(located[2]))
        cur.execute("SELECT * FROM x_capi_handoff_imports WHERE generation=%s AND context_handle_hash=%s", (self.generation,handle_hash))
        imported = _row(cur)
        if not imported or imported["landing_owner_hash"] != source["landing_owner_hash"] or imported["app_owner_hash"] != app["app_owner_hash"]:
            raise HandoffError("OWNER_MISMATCH")
        if positive:
            # Caller ingress/dispatch timestamps cannot authorize after lock waits.
            now = self._now(cur)
            self._source_live(source,now)
            self._owner_live(app,now)
            if imported["state"] != "active" or imported["effective_expires_at_ms"] <= now or source["state"] != "consumed":
                raise HandoffError("WITHDRAWN")
            if owner_hash is None or not hmac.compare_digest(app["app_owner_hash"],_hash(owner_hash)):
                raise HandoffError("OWNER_MISMATCH")
        return dict(imported,source=source,owner=app,transaction=tx)

    def close_by_handle(self, cur, handle_hash, close_context, reason="withdrawn"):
        """Close an imported context by an already verified suppression capability.

        This deliberately needs neither A nor a current source encryption key.
        Historical generation metadata is read only to locate its lock scope;
        the root caller has already verified the retained context/CSRF secret.
        """
        cur.execute("SELECT generation,policy_epoch FROM x_capi_handoff_imports WHERE context_handle_hash=%s", (handle_hash,))
        hint = cur.fetchone()
        if not hint:
            return False
        scope = object.__new__(HandoffStore)
        scope.__dict__ = dict(self.__dict__, generation=str(hint[0]), epoch=int(hint[1]))
        cur.execute("SELECT audience_scope FROM x_capi_handoff_generations WHERE generation=%s", (scope.generation,))
        audience = cur.fetchone()
        if not audience:
            raise HandoffError("QUARANTINED")
        scope.audience = bytes(audience[0])
        scope.generation_guard(cur,exclusive=True,positive=False)
        imported = scope.lock_import(cur,handle_hash,positive=False)
        if imported["source"]["state"] != "closed":
            scope._close_source(cur,imported["source"],reason,scope._now(cur),close_context)
        return True

    def retire(self, cur, close_context):
        self.generation_guard(cur,exclusive=True,positive=False)
        now = self._now(cur)
        cur.execute("SELECT landing_owner_hash FROM x_capi_handoff_sources WHERE generation=%s ORDER BY landing_owner_hash", (self.generation,))
        owners = [bytes(row[0]) for row in cur.fetchall()]
        for owner in owners:
            row = self._source(cur,owner)
            if row["state"] != "closed":
                self._close_source(cur,row,"generation_retired",now,close_context)
        cur.execute("UPDATE x_capi_handoff_transactions SET state='cancelled',cancelled_reason='generation_retired',updated_at_ms=%s WHERE generation=%s AND state IN ('app_granted','source_approved')", (now,self.generation))
        cur.execute("UPDATE x_capi_handoff_app_owners SET state='closed',closed_at_ms=COALESCE(closed_at_ms,%s),open_transaction_count=0,updated_at_ms=%s WHERE generation=%s", (now,now,self.generation))
        cur.execute("UPDATE x_capi_handoff_generations SET state='retired',retired_at_ms=COALESCE(retired_at_ms,%s),updated_at_ms=%s WHERE generation=%s", (now,now,self.generation))

    def cleanup(self, cur, close_context, batch=64):
        """Bounded erasure; replay metadata retained at least 91 days.

        Caller holds privacy boundary. The exclusive generation guard prevents
        discovery/lock-order races against captures and multi-source app closure.
        """
        if type(batch) is not int or not 1 <= batch <= 256:
            raise ValueError("invalid cleanup bound")
        generation = self.generation_guard(cur,exclusive=True,positive=False)
        now = self._now(cur)
        cur.execute("SELECT landing_owner_hash FROM x_capi_handoff_sources WHERE generation=%s AND state IN ('captured','assigned','consumed') AND original_expires_at_ms<=%s ORDER BY landing_owner_hash LIMIT %s", (self.generation,now,batch))
        for located in cur.fetchall():
            source = self._source(cur,bytes(located[0]))
            self._close_source(cur,source,"withdrawn",now,close_context)
            cur.execute("UPDATE x_capi_handoff_sources SET state='expired' WHERE generation=%s AND landing_owner_hash=%s", (self.generation,source["landing_owner_hash"]))
            cur.execute("UPDATE x_capi_handoff_imports SET state='expired' WHERE generation=%s AND landing_owner_hash=%s", (self.generation,source["landing_owner_hash"]))
        cur.execute("SELECT app_owner_hash FROM x_capi_handoff_app_owners WHERE generation=%s AND (owner_expires_at_ms<=%s OR (cookie_confirmed_at_ms IS NULL AND provisional_expires_at_ms<=%s)) AND state IN ('prepared','imported') ORDER BY app_owner_hash LIMIT %s", (self.generation,now,now,batch))
        for located in cur.fetchall():
            self.withdraw(cur,bytes(located[0]).hex(),{"reason":"withdrawn","request_id":str(uuid.uuid4())},close_context)
            cur.execute("UPDATE x_capi_handoff_app_owners SET state='expired' WHERE generation=%s AND app_owner_hash=%s", (self.generation,bytes(located[0])))
        cur.execute("SELECT app_owner_hash FROM x_capi_handoff_app_owners WHERE generation=%s AND open_transaction_count>0 ORDER BY app_owner_hash LIMIT %s", (self.generation,batch))
        for located in cur.fetchall():
            self._owner(cur,bytes(located[0]))
            self._expire_owner_transactions(cur,bytes(located[0]),now)
        cur.execute("""UPDATE x_capi_handoff_imports SET context_ciphertext=NULL
            WHERE context_id IN (SELECT context_id FROM x_capi_handoff_imports WHERE generation=%s
             AND context_ciphertext IS NOT NULL AND result_recover_until_ms<=%s LIMIT %s)""", (self.generation,now,batch))
        # Closure/replay metadata erasure only after all original lifetimes and
        # retention bounds. Clear circular assignment FK before terminal tx cleanup.
        cur.execute("""UPDATE x_capi_handoff_sources SET assigned_transaction_hash=NULL WHERE
            (generation,landing_owner_hash) IN (SELECT generation,landing_owner_hash FROM x_capi_handoff_sources
            WHERE generation=%s AND state IN ('closed','expired') AND tombstone_until_ms<=%s LIMIT %s)""", (self.generation,now,batch))
        cur.execute("""DELETE FROM x_capi_handoff_imports WHERE context_id IN (SELECT context_id
            FROM x_capi_handoff_imports WHERE generation=%s AND state IN ('closed','expired')
            AND updated_at_ms<=%s AND original_expires_at_ms<=%s LIMIT %s)""", (self.generation,now-RETENTION_MS,now,batch))
        cur.execute("""DELETE FROM x_capi_handoff_transactions WHERE transaction_hash IN
            (SELECT t.transaction_hash FROM x_capi_handoff_transactions t WHERE t.generation=%s
             AND t.state IN ('cancelled','expired','redeemed') AND t.updated_at_ms<=%s AND t.expires_at_ms<=%s
             AND NOT EXISTS(SELECT 1 FROM x_capi_handoff_imports i WHERE i.transaction_hash=t.transaction_hash)
             AND NOT EXISTS(SELECT 1 FROM x_capi_handoff_sources s WHERE s.assigned_transaction_hash=t.transaction_hash)
             LIMIT %s)""", (self.generation,now-RETENTION_MS,now,batch))
        removed_tx = cur.rowcount
        cur.execute("""DELETE FROM x_capi_handoff_sources WHERE (generation,landing_owner_hash) IN
            (SELECT s.generation,s.landing_owner_hash FROM x_capi_handoff_sources s WHERE s.generation=%s
             AND s.state IN ('closed','expired') AND s.tombstone_until_ms<=%s
             AND (s.original_expires_at_ms IS NOT NULL OR %s)
             AND NOT EXISTS(SELECT 1 FROM x_capi_handoff_imports i WHERE i.generation=s.generation AND i.landing_owner_hash=s.landing_owner_hash)
             AND NOT EXISTS(SELECT 1 FROM x_capi_handoff_transactions t WHERE t.generation=s.generation AND t.landing_owner_hash=s.landing_owner_hash)
             LIMIT %s)""", (self.generation,now,generation["state"]=="retired",batch))
        removed_sources = cur.rowcount
        cur.execute("""DELETE FROM x_capi_handoff_app_owners WHERE (generation,app_owner_hash) IN
            (SELECT a.generation,a.app_owner_hash FROM x_capi_handoff_app_owners a WHERE a.generation=%s
             AND a.state IN ('closed','expired') AND a.updated_at_ms<=%s AND a.owner_expires_at_ms<=%s
             AND NOT EXISTS(SELECT 1 FROM x_capi_handoff_imports i WHERE i.generation=a.generation AND i.app_owner_hash=a.app_owner_hash)
             AND NOT EXISTS(SELECT 1 FROM x_capi_handoff_transactions t WHERE t.generation=a.generation AND t.app_owner_hash=a.app_owner_hash)
             LIMIT %s)""", (self.generation,now-RETENTION_MS,now,batch))
        removed_owners = cur.rowcount
        cur.execute("UPDATE x_capi_handoff_generations SET source_count=source_count-%s,app_owner_count=app_owner_count-%s,transaction_count=transaction_count-%s WHERE generation=%s", (removed_sources,removed_owners,removed_tx,self.generation))


def close_import_by_handle(cur, handle_hash, close_context, now_ms, reason="withdrawn"):
    """Retained suppression capability path, independent of positive configuration.

    Call only after authenticating the context/CSRF capability under the existing
    exclusive privacy boundary. No source data/service key, A cookie, generation
    activation, or new wallet authority is needed to erase previously imported data.
    """
    scope = object.__new__(HandoffStore)
    scope.clock_ms = lambda: now_ms
    scope.source_keys = {}
    scope.limits = {"sources":10000,"app_owners":10000,"transactions":40000}
    return scope.close_by_handle(cur,handle_hash,close_context,reason=reason)
