"""Worker-only handoff integration; never imported by public HTTP adapters.

No network service, wallet authority, or database credentials are added to the
public gate. Each RPC owns its connection and independent privacy-fence FD.
"""
from __future__ import annotations

import os
import logging
import time
import uuid
from contextlib import contextmanager

try:
    from . import x_capi
    from .x_capi_handoff import (HandoffError, ServiceKeyring, FreshnessLease,
                                LinuxClockAssurance, DEFAULT_SOCKET, PROTOCOL)
    from .x_capi_handoff_rpc import WorkerHandoffService, HandoffRpcServer
    from .x_capi_handoff_store import HandoffStore
except ImportError:
    import x_capi
    from x_capi_handoff import (HandoffError, ServiceKeyring, FreshnessLease,
                               LinuxClockAssurance, DEFAULT_SOCKET, PROTOCOL)
    from x_capi_handoff_rpc import WorkerHandoffService, HandoffRpcServer
    from x_capi_handoff_store import HandoffStore

POLICY = "x-capi-handoff-v2"
logger = logging.getLogger(__name__)


class DeadlineCursor:
    """One RPC SQL budget, including row-lock waits, not one budget per query."""
    def __init__(self, cursor, deadline):
        self.cursor, self.deadline = cursor, deadline

    def execute(self, query, parameters=None):
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise HandoffError("DEPENDENCY_UNAVAILABLE")
        self.cursor.execute("SET LOCAL statement_timeout=%s", (min(1000, remaining),))
        self.cursor.execute("SET LOCAL lock_timeout=%s", (min(250, remaining),))
        self.cursor.execute(query, parameters)
        if time.monotonic() >= self.deadline:
            raise HandoffError("DEPENDENCY_UNAVAILABLE")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.cursor.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class DeadlineConnection:
    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline

    def cursor(self, *args, **kwargs):
        return DeadlineCursor(self.connection.cursor(*args, **kwargs), self.deadline)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class WorkerHandoffRuntime:
    def __init__(self, worker, *, store, keyring, lease, privacy_path,
                 clock_assured=None, connection_factory=None):
        self.worker, self.store, self.keyring, self.lease = worker, store, keyring, lease
        self.privacy_path = privacy_path
        self.clock_assured = clock_assured or LinuxClockAssurance()
        self.connection_factory = connection_factory or self._connect
        self.server = None
        self._restart_required = False
        self._restart_drain_failed = False
        self._closed = False
        self._cleanup_generation = ""
        self.service = WorkerHandoffService(
            keyring=keyring, lease=lease, source_handler=self.source,
            app_handler=self.app, positive_allowed=self.positive_allowed,
            clock_assured=self.clock_assured, generation_guard=self.generation_guard,
        )

    @classmethod
    def from_environment(cls, worker, privacy_path):
        cfg = x_capi.load_config()
        if cfg.policy_version != POLICY:
            return None
        # No auto-created keys or generation. The policy is opt-in through the
        # existing attested configuration, and missing infrastructure fails closed.
        expected = {
            "X_HANDOFF_PROTOCOL": PROTOCOL, "X_HANDOFF_WITHDRAWAL_POLICY": "B",
            "X_HANDOFF_SOCKET": DEFAULT_SOCKET, "X_HANDOFF_LEASE_MS": "30000",
            "X_HANDOFF_RENEW_INTERVAL_MS": "10000", "X_HANDOFF_TRANSACTION_TTL_MS": "300000",
            "X_HANDOFF_OBSERVATION_TTL_MS": "120000", "X_HANDOFF_MAX_CLOCK_ERROR_MS": "5000",
        }
        for name, value in expected.items():
            if os.getenv(name, value if name != "X_HANDOFF_WITHDRAWAL_POLICY" else "") != value:
                raise ValueError("invalid_handoff_configuration")
        generation = os.environ["X_HANDOFF_SOURCE_GENERATION"]
        if str(uuid.UUID(generation, version=4)) != generation:
            raise ValueError("invalid_handoff_generation")
        service = ServiceKeyring.from_file(os.environ["X_HANDOFF_SERVICE_KEYRING_FILE"])
        data = ServiceKeyring.from_file(os.environ["X_HANDOFF_SOURCE_DATA_KEYRING_FILE"])
        if any(a == b for a in service.keys.values() for b in data.keys.values()):
            raise ValueError("handoff_key_purpose_reuse")
        store = HandoffStore(generation, cfg.policy_epoch, cfg.audience_scope,
                             data.keys, data.primary_kid)
        return cls(worker, store=store, keyring=service,
                   lease=FreshnessLease(cfg.policy_epoch, generation), privacy_path=privacy_path)

    def _connect(self):
        # Dedicated worker-only secret path; the gate has no path to this code.
        if not self.worker._worker_db_target_is_isolated():
            raise HandoffError("DEPENDENCY_UNAVAILABLE")
        import psycopg2
        return psycopg2.connect(x_capi._db_url(worker=True), connect_timeout=1)

    def positive_allowed(self):
        cfg = x_capi.load_config()
        return (not self._restart_required and not self._closed
                and cfg.producer_ready and cfg.policy_version == POLICY
                and cfg.policy_epoch == self.store.epoch
                and cfg.audience_scope == self.store.audience.hex())

    @contextmanager
    def transaction(self, *, closure=False):
        deadline = time.monotonic() + 1.8
        fence = self.worker.PrivacyFence(self.privacy_path)
        conn = None
        try:
            fence.open(create_controls=False)
            boundary = fence.privacy_close_boundary() if closure else fence.dispatch_boundary()
            with boundary as acquired:
                if not acquired:
                    raise HandoffError("DEPENDENCY_UNAVAILABLE")
                try:
                    conn = self.connection_factory()
                    bounded = DeadlineConnection(conn, deadline)
                    self.worker._configure_connection(bounded)
                    if not self.worker._schema_ready(bounded):
                        raise HandoffError("DEPENDENCY_UNAVAILABLE")
                    if not closure and not self.worker.verify_config_guard(bounded, x_capi.load_config()):
                        raise HandoffError("QUARANTINED")
                    with bounded.cursor() as cursor:
                        yield cursor
                        if time.monotonic() >= deadline:
                            raise HandoffError("DEPENDENCY_UNAVAILABLE")
                    conn.commit()
                except HandoffError as exc:
                    if conn is not None:
                        if exc.durable_error:
                            # Quarantine is itself privacy closure. Commit it
                            # before releasing the dispatch serialization lock.
                            self.lease.invalidate()
                            conn.commit()
                        else:
                            conn.rollback()
                    raise
                except Exception:
                    if conn is not None:
                        conn.rollback()
                    raise HandoffError("DEPENDENCY_UNAVAILABLE") from None
        except HandoffError:
            raise
        except Exception:
            raise HandoffError("DEPENDENCY_UNAVAILABLE") from None
        finally:
            if conn is not None:
                conn.close()
            fence.close()

    def source(self, request):
        operation = request["op"]
        with self.transaction(closure=operation in ("revoke", "status")) as cur:
            if operation == "revoke":
                result = self.store.revoke(cur, request, self.close_context)
            else:
                result = getattr(self.store, operation)(cur, request)
        return result

    def generation_guard(self, request):
        with self.transaction() as cur:
            if request["op"] == "lease_attest":
                self.store.control_version(cur, request["control_version"])
            row = self.store.generation_guard(cur)
            value = row["landing_control_version"]
        return value

    def app(self, operation, payload, owner_hash, new_owner=False):
        with self.transaction(closure=operation == "withdraw") as cur:
            if operation == "prepare":
                result = self.store.prepare(cur, owner_hash, payload, new_owner=new_owner)
            elif operation == "begin":
                result = self.store.begin(cur, owner_hash, payload)
            elif operation == "redeem":
                result = self.store.redeem(cur, owner_hash, payload["handoff"], self.create_context, self.recover_context)
            elif operation == "recover":
                result = self.store.recover(cur, owner_hash, payload["handoff"], self.recover_context)
            else:
                result = self.store.withdraw(cur, owner_hash, payload, self.close_context)
        return result

    def create_context(self, cur, source, transaction, cutoff_ms, expires_ms):
        cfg = x_capi.load_config()
        if not self.positive_allowed() or not self.clock_assured():
            raise HandoffError("ATTRIBUTION_DISABLED")
        observed = source["observed_at_ms"] / 1000.0
        cutoff, expires = cutoff_ms / 1000.0, expires_ms / 1000.0
        ticket = x_capi._new_context_payload(cfg, observed, source["twclid"])
        ticket.update(state="granted", consented_at=cutoff, expires_at=expires,
                      lifecycle_expires_at=expires, twclid=source["twclid"])
        cipher = x_capi._seal_context_ticket(ticket)
        if not cipher or not self.worker._ticket_for_ingest(cipher, require_primary=True):
            raise HandoffError("DEPENDENCY_UNAVAILABLE")
        cur.execute("""UPDATE x_capi_capacity SET context_count=context_count+1,updated_at=%s
            WHERE singleton=TRUE AND context_count<%s AND revocation_saturated=FALSE
              AND revocation_saturated_until=0 RETURNING context_count""", (cutoff, cfg.context_limit))
        if cur.fetchone() is None:
            raise HandoffError("RATE_LIMITED")
        context_id, ownership_id = str(uuid.uuid4()), str(uuid.uuid4())
        handle_hash = x_capi._hash(ticket["handle"])
        cur.execute("""INSERT INTO x_capi_attribution_contexts
            (id,handle_hash,csrf_hash,consent_state,mode_scope,policy_version,policy_epoch,
             audience_scope,lifecycle_expires_at,twclid,consented_at,expires_at,
             wallet_hash,wallet_bound_at,first_seen_at,updated_at)
            VALUES (%s,%s,%s,'granted',%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s,%s)""",
            (context_id,handle_hash,x_capi._hash(ticket["csrf"]),cfg.mode,cfg.policy_version,
             cfg.policy_epoch,cfg.audience_scope,expires,source["twclid"],cutoff,expires,observed,cutoff))
        return dict(context_id=context_id, context_handle_hash=handle_hash,
                    context_ciphertext=cipher, ownership_id=ownership_id, csrf=ticket["csrf"])

    def recover_context(self, ciphertext):
        ticket = self.worker._ticket_for_ingest(ciphertext, require_primary=True)
        if not ticket:
            raise HandoffError("RESULT_UNAVAILABLE")
        return ticket

    def close_context(self, cur, context_id):
        now = self.store._now(cur) / 1000.0
        cur.execute("""UPDATE x_capi_attribution_contexts SET consent_state='revoked',
            twclid=NULL,expires_at=NULL,wallet_hash=NULL,wallet_bound_at=NULL,
            revoked_at=%s,updated_at=%s WHERE id=%s""", (now,now,context_id))
        cur.execute("""UPDATE x_capi_outbox SET status='cancelled',twclid=NULL,
            lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=%s,
            last_error_code='source_withdrawn' WHERE context_id=%s
            AND status IN ('dry_run','pending','retrying','leased')""", (now,context_id))
        self.worker._decrement_outbox_capacity_on_cursor(cur, cur.rowcount, now)

    def cleanup(self, cur):
        """Current expiry every cycle, plus one retired-generation retention batch.

        Old metadata counts toward global admission until safely erased. Rotation
        avoids starving later retired generations behind an unchanged tombstone.
        This cursor is scheduling only, never consent or replay authority.
        """
        self.store.cleanup(cur, self.close_context)
        cur.execute("""SELECT generation,policy_epoch,audience_scope
            FROM x_capi_handoff_generations
            WHERE generation<>%s AND (source_count>0 OR app_owner_count>0 OR transaction_count>0)
            ORDER BY (generation::text>%s) DESC,generation LIMIT 1""",
            (self.store.generation,self._cleanup_generation))
        old=cur.fetchone()
        if old is not None:
            scope=HandoffStore(str(old[0]),int(old[1]),bytes(old[2]).hex(),
                self.store.source_keys,self.store.active_key_id,clock_ms=self.store.clock_ms)
            scope.cleanup(cur,self.close_context)
            self._cleanup_generation=str(old[0])

    def dispatch_fresh(self):
        return self.positive_allowed() and self.clock_assured() and self.lease.allows_dispatch()

    def readiness(self):
        """Observe this process's real lease; never create or renew authority."""
        positive = bool(self.positive_allowed())
        clock = bool(self.clock_assured())
        fresh = bool(self.lease.allows_dispatch())
        return {"ready": positive and clock and fresh, "positive_allowed": positive,
                "clock_trusted": clock, "lease_fresh": fresh}

    def start(self):
        self.lease.invalidate()
        self.server = HandoffRpcServer(DEFAULT_SOCKET, self.service.dispatch,
                                      allowed_uid=int(os.getenv("X_CAPI_INGEST_ALLOWED_UID", "0")),
                                      readiness=self.readiness)
        self.server.start()

    def prepare_restart_if_needed(self):
        """Retire this process's authority and drain for supervised replacement.

        No lease, service, ledger or boot identity is replaced in this process.
        The main loop must exit and an external supervisor starts a new process
        through its ordinary schema/configuration/privacy activation checks.
        """
        if self._restart_drain_failed:
            return "drain_failed"
        if self._closed:
            return "closed"
        status = self.lease.ledger_status()
        if not self._restart_required and not status["recycle_required"]:
            return "idle"
        if not self._restart_required:
            self._restart_required = True
            self.lease.retire_boot()
            logger.warning("x_capi handoff process replacement required: challenges=%d limit=%d",
                           status["count"], status["limit"])
        if self.server is not None:
            try:
                drained = self.server.close(timeout=3.0)
            except Exception:
                drained = False
            if not drained:
                # A later successful close cannot erase this failed transition
                # or permit a CLEAN marker for the process replacement attempt.
                self._restart_drain_failed = True
                return "drain_failed"
            self.server = None
        return "restart_ready"

    def close(self):
        self._closed = True
        self.lease.retire_boot()
        if self.server is not None:
            return self.server.close()
        return True
