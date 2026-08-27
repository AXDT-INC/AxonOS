"""Tests for invite-gated, wallet-free guest/demo sessions.

Covers the new auth path, the hard time cap, and the promise that nothing
wallet-gated was weakened to make room for it.
"""

import ast
import hashlib
import math
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_TESTS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _TESTS_DIR.parent
_REPO_ROOT = _PKG_DIR.parent
for _path in (str(_PKG_DIR), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import guest_mode
import session_manager

try:
    import flask  # noqa: F401
    import gate_server
    _HAVE_GATE = True
except BaseException:
    _HAVE_GATE = False


WALLET = "0x1111111111111111111111111111111111111111"
INVITE = "aaaaBBBBccccDDDDeeeeFFFFgggg1111"
TOKEN_HASH = hashlib.sha256(INVITE.encode()).hexdigest()

GUEST_ENV = {
    "AXONOS_GUEST_MODE_ENABLED": "true",
    "AXONOS_GUEST_SESSION_MINUTES": "30",
    "AXONOS_GUEST_WARN_MINUTES": "5",
    "AXONOS_GUEST_ALLOWED_PROFILES": "small",
    "AXONOS_GUEST_ALLOWED_TEMPLATES": "",
    "AXONOS_GUEST_CREDIT_BUFFER_MINUTES": "5",
    "AXGT_CHALLENGE_DB_URL": "postgresql://test/test",
}


def _strip_js_comments(src):
    """Drop // and /* */ comments so assertions test code, not prose."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(
        re.sub(r"//.*$", "", line) for line in src.splitlines()
    )


def _mock_conn():
    """A psycopg2-shaped connection used as `with conn.cursor() as cur`."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cur


SPONSOR = "0x90f8bf6553474585a147e0689b8e40798c0b29fd"


def _invite_row(
    max_uses=1, uses=0, minutes=30, profiles="small",
    templates=None, expires_in=3600, revoked=False, label="acme",
    created_by=SPONSOR,
):
    """The column order redeem_invite() selects."""
    return (
        max_uses, uses, minutes, profiles, templates,
        time.time() + expires_in, revoked, label, created_by,
    )


class TestGuestIdentity(unittest.TestCase):
    """The synthetic identity must be indistinguishable in shape from a wallet."""

    def test_identity_is_a_valid_evm_address(self):
        from axgt_verifier import validate_wallet_address
        address = guest_mode.new_guest_identity()
        self.assertRegex(address, r"^0x[a-f0-9]{40}$")
        self.assertEqual(len(address), 42)
        # The whole design rests on this: no validation site had to be relaxed.
        self.assertTrue(validate_wallet_address(address))

    def test_identity_is_detected_offline_and_case_insensitively(self):
        address = guest_mode.new_guest_identity()
        self.assertTrue(guest_mode.is_guest_identity(address))
        self.assertTrue(guest_mode.is_guest_identity(address.upper().replace("0X", "0x")))
        self.assertTrue(guest_mode.is_guest_identity("  " + address + "  "))

    def test_real_wallets_are_never_treated_as_guests(self):
        for addr in (WALLET, "0x" + "ab" * 20, "", None, "guest:abc", "0xdeadbeef"):
            self.assertFalse(guest_mode.is_guest_identity(addr), addr)

    def test_identities_are_unique_and_high_entropy(self):
        seen = {guest_mode.new_guest_identity() for _ in range(200)}
        self.assertEqual(len(seen), 200)

    def test_masking_never_leaks_a_full_identity(self):
        address = guest_mode.new_guest_identity()
        masked = guest_mode.mask_guest_identity(address)
        self.assertNotIn(address, masked)
        self.assertEqual(guest_mode.mask_guest_identity(WALLET), "***")


class TestGuestFeatureFlag(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {"AXONOS_GUEST_MODE_ENABLED": ""}, clear=False):
            self.assertFalse(guest_mode.guest_mode_enabled())

    def test_redeem_and_mint_refuse_while_disabled(self):
        # An invite row in the database must never be enough on its own.
        with patch.dict(os.environ, {"AXONOS_GUEST_MODE_ENABLED": "false"}, clear=False):
            redeem = guest_mode.redeem_invite(INVITE)
            mint = guest_mode.mint_invite(label="x")
        self.assertFalse(redeem["ok"])
        self.assertEqual(redeem["error_code"], "guest_mode_disabled")
        self.assertFalse(mint["ok"])
        self.assertEqual(mint["error_code"], "guest_mode_disabled")

    def test_allowed_profiles_default_to_the_single_gpu_tier(self):
        with patch.dict(os.environ, {"AXONOS_GUEST_ALLOWED_PROFILES": ""}, clear=False):
            self.assertEqual(guest_mode.default_allowed_profiles(), ["small"])

    def test_invite_cannot_allow_an_unknown_environment(self):
        with patch.dict(os.environ, GUEST_ENV, clear=False), \
             patch.object(guest_mode, "init_once") as init:
            result = guest_mode.mint_invite(
                label="bad-template",
                allowed_templates=["not-deployed"],
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "unknown_template")
        init.assert_not_called()


class TestGuestWarningLeadTime(unittest.TestCase):
    def test_warning_fires_before_the_cutoff(self):
        with patch.dict(os.environ, GUEST_ENV, clear=False):
            self.assertEqual(guest_mode.warn_seconds_for(30), 300)

    def test_warning_is_clamped_below_a_short_demo(self):
        # A 2-minute operator test must not warn at second zero.
        with patch.dict(os.environ, GUEST_ENV, clear=False):
            self.assertEqual(guest_mode.warn_seconds_for(2), 60)
            self.assertEqual(guest_mode.warn_seconds_for(5), 120)

    def test_warning_can_be_disabled(self):
        with patch.dict(os.environ, {**GUEST_ENV, "AXONOS_GUEST_WARN_MINUTES": "0"}, clear=False):
            self.assertEqual(guest_mode.warn_seconds_for(30), 0)


class TestInviteRedemption(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, GUEST_ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _redeem(self, row, live_session=False, sponsor_live=0):
        conn, cur = _mock_conn()
        # invite row -> per-invite live check -> sponsor live COUNT
        cur.fetchone.side_effect = [
            row,
            (1,) if live_session else None,
            (sponsor_live,),
        ]
        with patch.object(guest_mode, "init_once", return_value=True), \
             patch.object(guest_mode, "_get_connection", return_value=conn):
            return guest_mode.redeem_invite(INVITE), conn, cur

    def test_valid_invite_mints_a_guest_identity(self):
        result, conn, cur = self._redeem(_invite_row())
        self.assertTrue(result["ok"], result)
        self.assertTrue(guest_mode.is_guest_identity(result["guest_address"]))
        self.assertEqual(result["session_minutes"], 30)
        self.assertEqual(result["allowed_profiles"], ["small"])
        self.assertEqual(result["warn_seconds"], 300)
        self.assertAlmostEqual(result["remaining_seconds"], 30 * 60, delta=2)
        conn.commit.assert_called()

    def test_redemption_looks_up_by_hash_and_never_stores_the_token(self):
        result, conn, cur = self._redeem(_invite_row())
        self.assertTrue(result["ok"])
        flat = " ".join(
            str(call.args) + str(call.kwargs) for call in cur.execute.call_args_list
        )
        # The raw bearer token must not reach the database in any statement.
        self.assertNotIn(INVITE, flat)
        self.assertIn(TOKEN_HASH, flat)
        self.assertEqual(result["token_hash"], TOKEN_HASH)

    def test_redemption_increments_the_use_count(self):
        result, conn, cur = self._redeem(_invite_row())
        self.assertTrue(result["ok"])
        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertTrue(
            any("uses = uses + 1" in sql for sql in statements),
            "redemption must consume a use",
        )

    def test_redemption_is_serialized_per_invite(self):
        result, conn, cur = self._redeem(_invite_row())
        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertTrue(
            any("pg_advisory_xact_lock" in sql for sql in statements),
            "concurrent redemptions of one link must not both see a stale count",
        )

    def test_exhausted_invite_is_refused(self):
        result, conn, _ = self._redeem(_invite_row(max_uses=1, uses=1))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invite_exhausted")
        conn.rollback.assert_called()

    def test_capped_use_invite_allows_its_remaining_uses(self):
        result, _, _ = self._redeem(_invite_row(max_uses=5, uses=4))
        self.assertTrue(result["ok"], result)
        spent, _, _ = self._redeem(_invite_row(max_uses=5, uses=5))
        self.assertFalse(spent["ok"])
        self.assertEqual(spent["error_code"], "invite_exhausted")

    def test_revoked_invite_is_refused(self):
        result, _, _ = self._redeem(_invite_row(revoked=True))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invite_revoked")

    def test_expired_invite_is_refused(self):
        result, _, _ = self._redeem(_invite_row(expires_in=-1))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invite_expired")

    def test_unknown_invite_is_refused(self):
        result, _, _ = self._redeem(None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_invite")

    def test_one_concurrent_session_per_invite(self):
        result, _, _ = self._redeem(_invite_row(max_uses=10), live_session=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invite_session_active")

    def test_malformed_token_is_indistinguishable_from_an_unknown_one(self):
        # The endpoint must not become an oracle for token shape.
        for bad in ("", "short", "!!!!" * 8, "x" * 400):
            result = guest_mode.redeem_invite(bad)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "invalid_invite", bad)

    def test_same_browser_attempt_recovers_without_consuming_another_use(self):
        address = guest_mode.new_guest_identity()
        conn, cur = _mock_conn()
        # Invite is already at its one-use cap, but this exact attempt owns that
        # use and must be returned before the ordinary exhaustion check.
        cur.fetchone.side_effect = [
            _invite_row(max_uses=1, uses=1),
            (address, time.time() + 1700, 30, "small", "pytorch"),
        ]
        with patch.object(guest_mode, "init_once", return_value=True), \
             patch.object(guest_mode, "_get_connection", return_value=conn):
            result = guest_mode.redeem_invite(
                INVITE,
                attempt_id="guest-attempt-1234567890",
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["reused_attempt"])
        self.assertEqual(result["guest_address"], address)
        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertFalse(any("uses = uses + 1" in sql for sql in statements))
        self.assertFalse(any("INSERT INTO axgt_guest_sessions" in sql for sql in statements))

    def test_a_different_attempt_cannot_bypass_the_use_cap(self):
        conn, cur = _mock_conn()
        cur.fetchone.side_effect = [_invite_row(max_uses=1, uses=1), None]
        with patch.object(guest_mode, "init_once", return_value=True), \
             patch.object(guest_mode, "_get_connection", return_value=conn):
            result = guest_mode.redeem_invite(
                INVITE,
                attempt_id="guest-attempt-different-123",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invite_exhausted")


class TestInviteRevocation(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, GUEST_ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.address = guest_mode.new_guest_identity()

    def _revoke(self, *, revoked=False, targets=None, release_result=None):
        conn, cur = _mock_conn()
        cur.fetchone.return_value = (revoked,)
        cur.fetchall.return_value = targets or []
        session_mgr = MagicMock()
        session_mgr.release_session.return_value = (
            release_result if release_result is not None else {"released": True}
        )
        with patch.object(guest_mode, "init_once", return_value=True), \
             patch.object(guest_mode, "_get_connection", return_value=conn), \
             patch.object(guest_mode, "_import_session_manager", return_value=session_mgr):
            result = guest_mode.revoke_invite(INVITE)
        return result, conn, cur, session_mgr

    def test_revoke_serializes_then_expires_issued_and_running_demos(self):
        result, conn, cur, session_mgr = self._revoke(
            targets=[(self.address, 42)]
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["sessions_targeted"], 1)
        self.assertEqual(result["sessions_stopped"], 1)
        self.assertFalse(result["cleanup_pending"])
        session_mgr.release_session.assert_called_once_with(
            self.address, expected_session_id=42
        )

        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        lock_index = next(i for i, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql)
        invite_update = next(i for i, sql in enumerate(statements) if "SET revoked = TRUE" in sql)
        guest_expiry = next(
            i for i, sql in enumerate(statements)
            if "UPDATE axgt_guest_sessions" in sql and "LEAST(expires_at" in sql
        )
        runtime_expiry = next(
            i for i, sql in enumerate(statements)
            if "UPDATE axgt_sessions AS s" in sql and "RETURNING" in sql
        )
        self.assertLess(lock_index, invite_update)
        self.assertLess(invite_update, guest_expiry)
        self.assertLess(guest_expiry, runtime_expiry)
        conn.commit.assert_called_once()

    def test_revoke_commits_before_external_teardown(self):
        events = []
        conn, cur = _mock_conn()
        cur.fetchone.return_value = (False,)
        cur.fetchall.return_value = [(self.address, 7)]
        conn.commit.side_effect = lambda: events.append("commit")
        session_mgr = MagicMock()
        session_mgr.release_session.side_effect = lambda *_a, **_kw: (
            events.append("release") or {"released": True}
        )
        with patch.object(guest_mode, "init_once", return_value=True), \
             patch.object(guest_mode, "_get_connection", return_value=conn), \
             patch.object(guest_mode, "_import_session_manager", return_value=session_mgr):
            result = guest_mode.revoke_invite(INVITE)
        self.assertTrue(result["ok"])
        self.assertEqual(events, ["commit", "release"])

    def test_already_revoked_invite_can_retry_teardown(self):
        result, _, _, session_mgr = self._revoke(
            revoked=True,
            targets=[(self.address, 9)],
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_revoked"])
        session_mgr.release_session.assert_called_once()

    def test_teardown_failure_is_reported_but_revocation_stays_committed(self):
        result, conn, _, _ = self._revoke(
            targets=[(self.address, 12)],
            release_result={"released": False, "reason": "cleanup failed"},
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["cleanup_pending"])
        self.assertEqual(result["cleanup_pending_session_ids"], [12])
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()

    def test_issued_but_unclaimed_identity_is_expired(self):
        result, _, cur, session_mgr = self._revoke(targets=[])
        self.assertTrue(result["ok"])
        session_mgr.release_session.assert_not_called()
        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertTrue(any("UPDATE axgt_guest_sessions" in sql for sql in statements))

    def test_claim_rechecks_guest_after_taking_the_invite_lock(self):
        conn, cur = _mock_conn()
        valid = {
            "cap_seconds": 1800.0,
            "expires_at": time.time() + 1800,
            "token_hash": TOKEN_HASH,
            "sponsor": SPONSOR,
            "session_minutes": 30,
            "warn_seconds": 300,
        }
        expired = {
            "granted": False,
            "error_code": "guest_session_expired",
            "guest_expired": True,
            "reason": "This demo session has ended. Connect a wallet to continue.",
        }
        guest_module = MagicMock()
        guest_module.claim_capacity_rejection.return_value = None
        with patch.object(
            session_manager,
            "_guest_claim_context",
            side_effect=[(valid, None), (None, expired)],
        ) as context, patch.object(
            session_manager, "_init_once", return_value=True
        ), patch.object(
            session_manager, "_get_connection", return_value=conn
        ), patch.object(
            session_manager, "_acquire_allocation_scheduler_lock"
        ), patch.object(
            session_manager, "_run_stale_session_maintenance_locked"
        ), patch.object(
            session_manager, "_import_guest_mode", return_value=guest_module
        ):
            result = session_manager.try_claim_session(
                self.address,
                requested_profile="small",
                requested_template="pytorch",
            )

        self.assertEqual(result["error_code"], "guest_session_expired")
        self.assertEqual(context.call_count, 2)
        guest_module.guest_session_for_cursor.assert_called_once_with(cur, self.address)
        self.assertTrue(context.call_args.kwargs["record_is_authoritative"])
        self.assertIs(
            context.call_args.kwargs["guest_record"],
            guest_module.guest_session_for_cursor.return_value,
        )
        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertTrue(any("pg_advisory_xact_lock(hashtext(%s))" in sql for sql in statements))
        self.assertFalse(any("INSERT INTO axgt_sessions" in sql for sql in statements))

    def test_cli_contract_says_revoke_ends_running_demos(self):
        source = (_REPO_ROOT / "scripts" / "guest_invite.py").read_text(encoding="utf-8")
        self.assertNotIn("keeps its remaining time", source)
        self.assertIn('require_enabled=args.command == "mint"', source)


class TestGuestDataReaper(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            **GUEST_ENV,
            "AXONOS_GUEST_DATA_RETENTION_DAYS": "30",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.address = guest_mode.new_guest_identity()

    def _run(self, *, tables=None, addresses=None):
        conn, cur = _mock_conn()
        cur.fetchone.return_value = tables or (
            "axgt_guest_sessions",
            "axgt_ledger",
            "axgt_verified_deposits",
            "axgt_deposits",
        )
        cur.fetchall.return_value = [
            (address,) for address in (addresses if addresses is not None else [self.address])
        ]
        cur.rowcount = 1
        with patch.object(guest_mode, "_get_connection", return_value=conn):
            result = guest_mode.reap_expired_guest_data(now=10_000_000.0)
        return result, conn, cur

    def test_reaper_anchors_on_expired_guest_rows_and_excludes_nonterminal_sessions(self):
        result, conn, cur = self._run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], 1)
        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        candidate_sql = next(sql for sql in statements if "SELECT gs.guest_address" in sql)
        self.assertIn("gs.expires_at <= %s", candidate_sql)
        self.assertIn("s.status NOT IN ('ended', 'expired', 'released')", candidate_sql)
        self.assertIn("LIMIT %s", candidate_sql)
        self.assertIn("FOR UPDATE OF gs SKIP LOCKED", candidate_sql)
        # Selection is anchored on the mapping table, never on the 40-bit
        # address prefix that a vanity-mined real wallet could share.
        self.assertNotIn(guest_mode.GUEST_ADDRESS_TAG, candidate_sql)
        conn.commit.assert_called_once()

    def test_reaper_deletes_children_then_anchor(self):
        result, _, cur = self._run()
        self.assertTrue(result["ok"])
        deletes = [
            " ".join(str(call.args[0]).split())
            for call in cur.execute.call_args_list
            if str(call.args[0]).lstrip().startswith("DELETE FROM")
        ]
        self.assertEqual(
            [sql.split()[2] for sql in deletes],
            [
                "axgt_ledger",
                "axgt_verified_deposits",
                "axgt_deposits",
                "axgt_guest_sessions",
            ],
        )
        self.assertTrue(all("wallet_address = ANY(%s)" in sql for sql in deletes[:3]))

    def test_sparse_or_never_enabled_schema_is_safe(self):
        result, conn, cur = self._run(
            tables=("axgt_guest_sessions", None, None, None)
        )
        self.assertTrue(result["ok"])
        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertFalse(any("DELETE FROM axgt_ledger" in sql for sql in statements))
        self.assertTrue(any("DELETE FROM axgt_guest_sessions" in sql for sql in statements))

        missing, missing_conn, missing_cur = self._run(
            tables=(None, None, None, None)
        )
        self.assertTrue(missing["ok"])
        self.assertEqual(missing["reason"], "guest_table_absent")
        self.assertFalse(any(
            str(call.args[0]).lstrip().startswith("DELETE FROM")
            for call in missing_cur.execute.call_args_list
        ))
        missing_conn.commit.assert_called_once()

    def test_empty_batch_is_idempotent(self):
        result, conn, cur = self._run(addresses=[])
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], 0)
        self.assertFalse(any(
            str(call.args[0]).lstrip().startswith("DELETE FROM")
            for call in cur.execute.call_args_list
        ))
        conn.commit.assert_called_once()

    def test_delete_failure_rolls_back_the_whole_batch(self):
        conn, cur = _mock_conn()
        cur.fetchone.return_value = (
            "axgt_guest_sessions",
            "axgt_ledger",
            "axgt_verified_deposits",
            "axgt_deposits",
        )
        cur.fetchall.return_value = [(self.address,)]

        def execute(sql, params=None):
            if "DELETE FROM axgt_verified_deposits" in str(sql):
                raise RuntimeError("database failure")

        cur.execute.side_effect = execute
        with patch.object(guest_mode, "_get_connection", return_value=conn):
            result = guest_mode.reap_expired_guest_data(now=10_000_000.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "guest_reaper_failed")
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_zero_retention_disables_pruning_even_when_guest_mode_is_off(self):
        with patch.dict(os.environ, {
            "AXONOS_GUEST_MODE_ENABLED": "false",
            "AXONOS_GUEST_DATA_RETENTION_DAYS": "0",
        }, clear=False), patch.object(guest_mode, "_get_connection") as connect:
            result = guest_mode.reap_expired_guest_data()
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "retention_disabled")
        connect.assert_not_called()

    def test_historical_cleanup_still_runs_when_guest_mode_is_off(self):
        with patch.dict(os.environ, {
            **GUEST_ENV,
            "AXONOS_GUEST_MODE_ENABLED": "false",
            "AXONOS_GUEST_DATA_RETENTION_DAYS": "30",
        }, clear=False):
            result, _, _ = self._run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], 1)

    def test_schema_has_an_expiry_index(self):
        conn, cur = _mock_conn()
        guest_mode._ensure_tables(conn)
        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertTrue(any(
            "idx_axgt_guest_sessions_expires" in sql and "(expires_at)" in sql
            for sql in statements
        ))

    def test_schema_has_an_idempotent_redemption_attempt_key(self):
        conn, cur = _mock_conn()
        guest_mode._ensure_tables(conn)
        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertTrue(any(
            "ADD COLUMN IF NOT EXISTS attempt_id" in sql for sql in statements
        ))
        self.assertTrue(any(
            "UNIQUE INDEX" in sql and "(token_hash, attempt_id)" in sql
            for sql in statements
        ))

    def test_session_cleanup_reaps_after_hooks_and_before_unlock(self):
        conn, _cur = _mock_conn()
        events = []
        guest_module = MagicMock()
        guest_module.reap_expired_guest_data.side_effect = lambda: (
            events.append("reap") or {"ok": True, "deleted": 0}
        )
        with patch.object(session_manager, "_init_once", return_value=True), \
             patch.object(session_manager, "_get_connection", return_value=conn), \
             patch.object(session_manager, "_try_acquire_allocation_scheduler_lock", return_value=True), \
             patch.object(session_manager, "_run_stale_session_maintenance_locked"), \
             patch.object(session_manager, "_reconcile_containers", return_value=([], [(self.address, 8)])), \
             patch.object(session_manager, "_on_session_ended", side_effect=lambda *_: events.append("hook")), \
             patch.object(session_manager, "_import_guest_mode", return_value=guest_module), \
             patch.object(
                 session_manager,
                 "_release_allocation_scheduler_lock",
                 side_effect=lambda *_: events.append("unlock"),
             ):
            session_manager.perform_session_cleanup()

        self.assertEqual(events, ["hook", "reap", "unlock"])


class TestInviteMinterAuthorization(unittest.TestCase):
    """Minting is authorized by the operator's minter list, not the admin secret."""

    def test_defaults_to_the_test_credit_wallet_list(self):
        # A wallet already trusted with free compute for itself is the same
        # person who hands prospects a demo.
        with patch.dict(os.environ, {
            **GUEST_ENV,
            "AXONOS_GUEST_INVITE_MINTERS": "",
            "AXONOS_TEST_CREDIT_WALLETS": SPONSOR,
        }, clear=False):
            self.assertTrue(guest_mode.can_mint_invites(SPONSOR))
            self.assertFalse(guest_mode.can_mint_invites(WALLET))

    def test_explicit_minter_list_overrides_the_inherited_one(self):
        with patch.dict(os.environ, {
            **GUEST_ENV,
            "AXONOS_GUEST_INVITE_MINTERS": WALLET,
            "AXONOS_TEST_CREDIT_WALLETS": SPONSOR,
        }, clear=False):
            self.assertTrue(guest_mode.can_mint_invites(WALLET))
            self.assertFalse(guest_mode.can_mint_invites(SPONSOR))

    def test_minting_does_not_require_the_test_credit_feature(self):
        # Reusing is_wallet_whitelisted() would have made demo mode silently
        # depend on AXONOS_TEST_CREDITS_ENABLED. It must not.
        with patch.dict(os.environ, {
            **GUEST_ENV,
            "AXONOS_TEST_CREDITS_ENABLED": "false",
            "AXONOS_GUEST_INVITE_MINTERS": "",
            "AXONOS_TEST_CREDIT_WALLETS": SPONSOR,
        }, clear=False):
            from axgt_verifier import is_wallet_whitelisted
            self.assertFalse(is_wallet_whitelisted(SPONSOR))   # test credit off
            self.assertTrue(guest_mode.can_mint_invites(SPONSOR))  # demo minting on

    def test_minting_is_refused_while_guest_mode_is_off(self):
        with patch.dict(os.environ, {
            **GUEST_ENV,
            "AXONOS_GUEST_MODE_ENABLED": "",
            "AXONOS_GUEST_INVITE_MINTERS": SPONSOR,
        }, clear=False):
            self.assertFalse(guest_mode.can_mint_invites(SPONSOR))

    def test_a_demo_identity_can_never_mint_further_demos(self):
        address = guest_mode.new_guest_identity()
        with patch.dict(os.environ, {
            **GUEST_ENV, "AXONOS_GUEST_INVITE_MINTERS": f"{SPONSOR},{address}",
        }, clear=False):
            self.assertFalse(guest_mode.can_mint_invites(address))

    def test_malformed_entries_in_the_minter_list_are_ignored(self):
        with patch.dict(os.environ, {
            **GUEST_ENV, "AXONOS_GUEST_INVITE_MINTERS": f"not-an-address, ,{SPONSOR}",
        }, clear=False):
            self.assertEqual(guest_mode.invite_minter_wallets(), frozenset({SPONSOR}))


class TestSponsorQuotas(unittest.TestCase):
    """Invites mint NEW identities, so the quota must live on the sponsor."""

    def setUp(self):
        self.env = patch.dict(os.environ, {
            **GUEST_ENV,
            "AXONOS_GUEST_INVITE_MINTERS": SPONSOR,
            "AXONOS_GUEST_MAX_LIVE_PER_SPONSOR": "2",
            "AXONOS_GUEST_MAX_INVITES_PER_DAY": "3",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _mint(self, minted_today):
        conn, cur = _mock_conn()
        cur.fetchone.side_effect = [(minted_today,)]
        with patch.object(guest_mode, "init_once", return_value=True), \
             patch.object(guest_mode, "_get_connection", return_value=conn):
            return guest_mode.mint_invite(label="demo", created_by=SPONSOR)

    def _redeem(self, sponsor_live):
        conn, cur = _mock_conn()
        cur.fetchone.side_effect = [_invite_row(max_uses=10), None, (sponsor_live,)]
        with patch.object(guest_mode, "init_once", return_value=True), \
             patch.object(guest_mode, "_get_connection", return_value=conn):
            return guest_mode.redeem_invite(INVITE)

    def test_daily_mint_quota_is_enforced(self):
        self.assertTrue(self._mint(minted_today=2)["ok"])
        blocked = self._mint(minted_today=3)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error_code"], "sponsor_daily_limit")
        self.assertEqual(blocked["limit"], 3)

    def test_concurrent_live_quota_is_enforced_at_redemption(self):
        # This is the quota that actually protects the fleet: without it one
        # sponsor's many links light up one GPU each.
        self.assertTrue(self._redeem(sponsor_live=1)["ok"])
        blocked = self._redeem(sponsor_live=2)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error_code"], "sponsor_live_limit")
        self.assertEqual(blocked["limit"], 2)

    def test_redemptions_from_different_invites_serialize_on_the_sponsor(self):
        conn, cur = _mock_conn()
        cur.fetchone.side_effect = [_invite_row(max_uses=10), None, (0,)]
        with patch.object(guest_mode, "init_once", return_value=True), \
             patch.object(guest_mode, "_get_connection", return_value=conn):
            result = guest_mode.redeem_invite(INVITE)
        self.assertTrue(result["ok"])
        lock_params = [
            call.args[1]
            for call in cur.execute.call_args_list
            if "pg_advisory_xact_lock(hashtext(%s))" in str(call.args[0])
        ]
        self.assertIn((f"guest-sponsor:{SPONSOR}",), lock_params)

    def test_live_count_query_is_scoped_to_the_sponsor(self):
        conn, cur = _mock_conn()
        cur.fetchone.return_value = (0,)
        guest_mode._sponsor_live_session_count(cur, SPONSOR, time.time())
        sql, params = cur.execute.call_args.args
        self.assertIn("inv.created_by = %s", sql)
        self.assertIn("JOIN", sql)
        self.assertEqual(params[0], SPONSOR)

    def test_operator_minted_invites_are_not_quota_limited(self):
        # A CLI/admin mint has no sponsor wallet to attribute or bound.
        conn, cur = _mock_conn()
        self.assertEqual(
            guest_mode._sponsor_live_session_count(cur, "guest_invite_cli", time.time()), 0
        )
        cur.execute.assert_not_called()

    def test_invite_records_its_sponsor(self):
        result = self._mint(minted_today=0)
        self.assertEqual(result["created_by"], SPONSOR)

    def test_claim_time_refuses_another_active_identity_from_the_invite(self):
        address = guest_mode.new_guest_identity()
        conn, cur = _mock_conn()
        cur.fetchone.return_value = (1,)
        rejection = guest_mode.claim_capacity_rejection(
            cur, address, TOKEN_HASH, SPONSOR
        )
        self.assertEqual(rejection["error_code"], "invite_session_active")
        sql, params = cur.execute.call_args_list[0].args
        self.assertIn("gs.guest_address <> %s", sql)
        self.assertIn("s.status IN ('active', 'credit_grace')", sql)
        self.assertEqual(params, (TOKEN_HASH, address))

    def test_claim_time_enforces_sponsor_cap_across_old_invites(self):
        address = guest_mode.new_guest_identity()
        conn, cur = _mock_conn()
        # No other active identity on this invite; two on the sponsor's links.
        cur.fetchone.side_effect = [None, (2,)]
        rejection = guest_mode.claim_capacity_rejection(
            cur, address, TOKEN_HASH, SPONSOR
        )
        self.assertEqual(rejection["error_code"], "sponsor_live_limit")
        statements = [str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertTrue(any("COUNT(*)" in sql for sql in statements))
        sponsor_locks = [
            call.args[1]
            for call in cur.execute.call_args_list
            if "pg_advisory_xact_lock(hashtext(%s))" in str(call.args[0])
        ]
        self.assertEqual(sponsor_locks, [(f"guest-sponsor:{SPONSOR}",)])

    def test_claim_time_allows_reclaim_of_current_identity_below_cap(self):
        address = guest_mode.new_guest_identity()
        conn, cur = _mock_conn()
        cur.fetchone.side_effect = [None, (1,)]
        self.assertIsNone(
            guest_mode.claim_capacity_rejection(cur, address, TOKEN_HASH, SPONSOR)
        )


class TestGuestCreditSizing(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, GUEST_ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_credit_exceeds_the_wall_clock_cap(self):
        # The hard cap must be the binding limit. If credit ran out first the
        # session would sit in credit-grace holding a GPU for hours.
        self.assertGreater(guest_mode.credit_minutes_for(30, ["small"]), 30)

    def test_credit_scales_with_the_widest_permitted_tier(self):
        single = guest_mode.credit_minutes_for(30, ["small"])
        quad = guest_mode.credit_minutes_for(30, ["small", "large"])
        self.assertGreater(quad, single)
        # Four GPUs bill at 4x wall-clock, so 30 wall-minutes needs >120 credit.
        self.assertGreater(quad, 120)

    def test_grant_is_refused_for_a_real_wallet(self):
        result = guest_mode.grant_guest_credit(WALLET, 30, ["small"], TOKEN_HASH)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "not_guest_identity")

    def test_grant_request_id_is_idempotent_per_identity(self):
        address = guest_mode.new_guest_identity()
        ledger = MagicMock()
        ledger.credit_test_grant.return_value = {"ok": True, "credited_minutes": 43.0}
        with patch.object(guest_mode, "_import_deposit_ledger", return_value=ledger):
            guest_mode.grant_guest_credit(address, 30, ["small"], TOKEN_HASH)
            guest_mode.grant_guest_credit(address, 30, ["small"], TOKEN_HASH)
        first, second = ledger.credit_test_grant.call_args_list
        self.assertEqual(first.kwargs["request_id"], second.kwargs["request_id"])
        # A different identity from the same invite gets a different key.
        other = guest_mode.new_guest_identity()
        with patch.object(guest_mode, "_import_deposit_ledger", return_value=ledger):
            guest_mode.grant_guest_credit(other, 30, ["small"], TOKEN_HASH)
        self.assertNotEqual(
            ledger.credit_test_grant.call_args_list[-1].kwargs["request_id"],
            first.kwargs["request_id"],
        )

    def test_guest_credit_carries_its_own_provenance(self):
        # Demo minutes must never be indistinguishable from a team member's
        # own test credit in the ledger.
        address = guest_mode.new_guest_identity()
        ledger = MagicMock()
        ledger.credit_test_grant.return_value = {"ok": True, "credited_minutes": 43.0}
        with patch.object(guest_mode, "_import_deposit_ledger", return_value=ledger):
            guest_mode.grant_guest_credit(address, 30, ["small"], TOKEN_HASH)
        kwargs = ledger.credit_test_grant.call_args.kwargs
        self.assertEqual(kwargs["credit_source"], "guest_credit")
        self.assertEqual(kwargs["event_type"], "guest_credit")
        self.assertEqual(kwargs["payment_rail"], "guest")
        self.assertEqual(kwargs["reference_prefix"], "guest-credit")

    def test_a_large_valid_demo_is_split_below_the_ledger_grant_ceiling(self):
        address = guest_mode.new_guest_identity()
        ledger = MagicMock()
        ledger.credit_test_grant.return_value = {"ok": True}
        with patch.object(guest_mode, "_import_deposit_ledger", return_value=ledger):
            result = guest_mode.grant_guest_credit(
                address, 240, ["max"], TOKEN_HASH
            )
        self.assertTrue(result["ok"])
        self.assertGreater(result["credit_chunks"], 1)
        grants = [call.kwargs["grant_minutes"] for call in ledger.credit_test_grant.call_args_list]
        self.assertAlmostEqual(sum(grants), guest_mode.credit_minutes_for(240, ["max"]))
        self.assertTrue(all(
            grant <= guest_mode.MAX_GUEST_CREDIT_CHUNK_MINUTES for grant in grants
        ))
        request_ids = [
            call.kwargs["request_id"] for call in ledger.credit_test_grant.call_args_list
        ]
        self.assertEqual(len(request_ids), len(set(request_ids)))


class TestLedgerGuestGrant(unittest.TestCase):
    """Guest credit reuses credit_test_grant with its own provenance."""

    def setUp(self):
        self.env = patch.dict(os.environ, GUEST_ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        import deposit_ledger
        self.ledger = deposit_ledger

    def test_guest_credit_is_an_allowed_ledger_event(self):
        self.assertIn("guest_credit", self.ledger._ALLOWED_EVENT_TYPES)

    def test_guest_is_an_accepted_rail(self):
        self.assertIn("guest", self.ledger._TEST_CREDIT_RAILS)

    def test_there_is_no_forked_guest_grant_function(self):
        # The fork was consolidated; a reappearing copy would drift from the
        # locking and replay logic it was duplicating.
        self.assertFalse(hasattr(self.ledger, "credit_guest_grant"))

    def test_grant_rejects_a_non_address_identity(self):
        result = self.ledger.credit_test_grant(
            "guest:abc", 30.0, 30.0, "req-00000001", "guest")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_wallet")

    def test_grant_rejects_out_of_bounds_minutes(self):
        address = guest_mode.new_guest_identity()
        for minutes in (0, -5, 99999):
            result = self.ledger.credit_test_grant(
                address, minutes, minutes, "req-00000001", "guest")
            self.assertFalse(result["ok"], minutes)
            self.assertEqual(result["error_code"], "invalid_credit_config")

    def test_grant_rejects_a_malformed_request_id(self):
        address = guest_mode.new_guest_identity()
        result = self.ledger.credit_test_grant(
            address, 30.0, 30.0, "short", "guest")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_request_id")

    def test_test_credit_defaults_are_unchanged(self):
        # The parameterisation must not have altered the existing rail.
        import inspect
        sig = inspect.signature(self.ledger.credit_test_grant)
        self.assertEqual(sig.parameters["credit_source"].default, "test_credit")
        self.assertEqual(sig.parameters["event_type"].default, "test_credit")
        self.assertEqual(sig.parameters["created_by"].default, "test_credit_api")
        self.assertEqual(sig.parameters["reference_prefix"].default, "test-credit")


class TestGuestClaimLimits(unittest.TestCase):
    """try_claim_session's demo preflight: caps, tiers, environments, SSH."""

    def setUp(self):
        self.env = patch.dict(os.environ, GUEST_ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.address = guest_mode.new_guest_identity()

    def _record(self, minutes=30, profiles=None, templates=None, remaining=30 * 60):
        return {
            "guest_address": self.address,
            "token_hash": TOKEN_HASH,
            "issued_at": time.time(),
            "expires_at": time.time() + remaining,
            "session_minutes": minutes,
            "allowed_profiles": profiles if profiles is not None else ["small"],
            "allowed_templates": templates if templates is not None else [],
            "sponsor": SPONSOR,
        }

    def _context(self, profile="small", template=None, ssh=False, record=None):
        stub = MagicMock()
        stub.is_guest_identity.side_effect = guest_mode.is_guest_identity
        stub.guest_mode_enabled.return_value = True
        stub.guest_session_for.return_value = (
            self._record() if record is None else record
        )
        stub.warn_seconds_for.side_effect = guest_mode.warn_seconds_for
        with patch.object(session_manager, "_import_guest_mode", return_value=stub):
            return session_manager._guest_claim_context(
                self.address, profile, template, ssh
            )

    def test_a_real_wallet_gets_no_guest_context(self):
        ctx, rejection = session_manager._guest_claim_context(WALLET, "small", None, False)
        self.assertIsNone(ctx)
        self.assertIsNone(rejection)

    def test_valid_demo_claim_resolves_a_cap(self):
        ctx, rejection = self._context()
        self.assertIsNone(rejection)
        self.assertAlmostEqual(ctx["cap_seconds"], 30 * 60, delta=2)
        self.assertEqual(ctx["warn_seconds"], 300)
        self.assertEqual(ctx["token_hash"], TOKEN_HASH)

    def test_guest_is_refused_when_the_feature_is_switched_off(self):
        # Fail closed: a token minted before the operator disabled guest mode
        # must not keep launching compute.
        stub = MagicMock()
        stub.is_guest_identity.side_effect = guest_mode.is_guest_identity
        stub.guest_mode_enabled.return_value = False
        with patch.object(session_manager, "_import_guest_mode", return_value=stub):
            ctx, rejection = session_manager._guest_claim_context(
                self.address, "small", None, False
            )
        self.assertIsNone(ctx)
        self.assertFalse(rejection["granted"])
        self.assertEqual(rejection["error_code"], "guest_mode_disabled")

    def test_unknown_guest_record_is_refused(self):
        ctx, rejection = self._context(record=False)
        self.assertIsNone(ctx)
        self.assertEqual(rejection["error_code"], "guest_session_unknown")

    def test_used_up_demo_is_refused(self):
        ctx, rejection = self._context(record=self._record(remaining=0))
        self.assertIsNone(ctx)
        self.assertEqual(rejection["error_code"], "guest_session_expired")
        self.assertTrue(rejection["guest_expired"])

    def test_revoked_record_is_refused_even_before_its_deadline(self):
        record = self._record()
        record["invite_revoked"] = True
        ctx, rejection = self._context(record=record)
        self.assertIsNone(ctx)
        self.assertEqual(rejection["error_code"], "guest_session_expired")

    def test_locked_cursor_lookup_carries_revocation_and_absolute_deadline(self):
        conn, cur = _mock_conn()
        expires_at = time.time() + 900
        cur.fetchone.return_value = (
            self.address, TOKEN_HASH, time.time(), expires_at, 30,
            "small", "pytorch", SPONSOR, False,
        )
        record = guest_mode.guest_session_for_cursor(cur, self.address)
        self.assertEqual(record["expires_at"], expires_at)
        self.assertFalse(record["invite_revoked"])
        sql = str(cur.execute.call_args.args[0])
        self.assertIn("inv.revoked", sql)

    def test_ssh_is_refused_for_a_demo(self):
        ctx, rejection = self._context(ssh=True)
        self.assertIsNone(ctx)
        self.assertEqual(rejection["error_code"], "guest_ssh_not_permitted")

    def test_profile_outside_the_invite_allowlist_is_refused(self):
        ctx, rejection = self._context(profile="max")
        self.assertIsNone(ctx)
        self.assertEqual(rejection["error_code"], "guest_profile_not_permitted")
        self.assertEqual(rejection["allowed_profiles"], ["small"])

    def test_template_outside_the_invite_allowlist_is_refused(self):
        record = self._record(templates=["pytorch"])
        ctx, rejection = self._context(template="gromacs", record=record)
        self.assertIsNone(ctx)
        self.assertEqual(rejection["error_code"], "guest_template_not_permitted")

    def test_permitted_template_is_allowed(self):
        record = self._record(templates=["pytorch"])
        ctx, rejection = self._context(template="pytorch", record=record)
        self.assertIsNone(rejection)
        self.assertIsNotNone(ctx)

    def test_empty_template_allowlist_permits_any_environment(self):
        ctx, rejection = self._context(template="gromacs")
        self.assertIsNone(rejection)
        self.assertIsNotNone(ctx)

    def test_global_template_allowlist_rejects_before_database_work(self):
        with patch.object(session_manager, "_init_once") as init:
            result = session_manager.try_claim_session(
                WALLET,
                requested_profile="small",
                requested_template="not-deployed",
            )
        self.assertFalse(result["granted"])
        self.assertEqual(result["error_code"], "template_not_supported")
        init.assert_not_called()

    def test_template_is_normalized_before_guest_policy_and_launch(self):
        with patch.object(
            session_manager,
            "_guest_claim_context",
            return_value=(None, None),
        ) as guest_context, patch.object(
            session_manager, "_init_once", return_value=False
        ):
            result = session_manager.try_claim_session(
                WALLET,
                requested_profile="small",
                requested_template="  PyTorch  ",
            )
        self.assertFalse(result["granted"])
        self.assertEqual(guest_context.call_args.args[2], "pytorch")


class TestGuestHardCap(unittest.TestCase):
    def test_stored_deadline_accounts_for_the_expiry_grace(self):
        # _expire_stale_session ends a capped session at hard_expires_at + grace,
        # so the stored value is pulled back for teardown to land on the
        # deadline the prospect was actually shown.
        with patch.dict(os.environ, {"AXGT_SESSION_GRACE_SECONDS": "60"}, clear=False):
            now = 1_000_000.0
            self.assertEqual(
                session_manager._guest_hard_expires_at(1800.0, now), now + 1740.0
            )

    def test_deadline_never_goes_backwards_for_a_tiny_cap(self):
        with patch.dict(os.environ, {"AXGT_SESSION_GRACE_SECONDS": "60"}, clear=False):
            now = 1_000_000.0
            self.assertEqual(session_manager._guest_hard_expires_at(10.0, now), now)

    def test_expiry_sweep_ends_capped_sessions_generically(self):
        # The demo cap reuses the SSH column precisely because this sweep is not
        # SSH-specific. If that changed, demos would never be torn down.
        source = (_PKG_DIR / "session_manager.py").read_text(encoding="utf-8")
        sweep = source.split("def _expire_stale_session", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("hard_expires_at IS NOT NULL", sweep)
        self.assertNotIn("ssh_enabled", sweep)

    def test_cap_renewal_paths_stay_ssh_gated(self):
        # A demo cap must be non-renewable: every extension of hard_expires_at
        # has to remain behind an SSH condition.
        source = (_PKG_DIR / "session_manager.py").read_text(encoding="utf-8")
        for renewal in (
            'if ssh_enabled and ssh_active and hard_expires_at is not None:',
            'if owned.get("ssh_enabled") and hard_expires_at is not None:',
            'if credit_grace.get("ssh_enabled"):',
        ):
            self.assertIn(renewal, source, f"missing SSH gate: {renewal}")

    def test_guest_claim_sets_the_cap_without_ssh(self):
        source = (_PKG_DIR / "session_manager.py").read_text(encoding="utf-8")
        self.assertIn("elif is_guest:", source)
        self.assertIn("_guest_hard_expires_at(", source)

    def test_guest_expiry_clamp_never_extends_when_lookup_fails(self):
        address = guest_mode.new_guest_identity()
        with patch.object(guest_mode, "guest_session_for", return_value=None):
            self.assertEqual(
                session_manager._guest_clamped_expires_at(
                    address,
                    proposed=5000.0,
                    now=1000.0,
                    current_expires_at=1800.0,
                ),
                1800.0,
            )


class TestGuestNeverHoldsAGpu(unittest.TestCase):
    def test_credit_grace_is_disabled_for_a_guest_identity(self):
        address = guest_mode.new_guest_identity()
        with patch.object(
            session_manager, "_preserve_session_on_credit_exhaust", return_value=True
        ):
            self.assertFalse(session_manager._preserve_for_wallet(address))
            self.assertTrue(session_manager._preserve_for_wallet(WALLET))

    def test_operator_switch_still_wins_for_real_wallets(self):
        with patch.object(
            session_manager, "_preserve_session_on_credit_exhaust", return_value=False
        ):
            self.assertFalse(session_manager._preserve_for_wallet(WALLET))

    def test_every_preserve_decision_is_identity_aware(self):
        source = (_PKG_DIR / "session_manager.py").read_text(encoding="utf-8")
        body = source.split("def _preserve_for_wallet", 1)[1]
        # Only the wrapper itself may consult the raw operator switch.
        self.assertEqual(body.count("_preserve_session_on_credit_exhaust()"), 1)


class TestEphemeralStorage(unittest.TestCase):
    """A demo must never provision a persistent volume."""

    def test_guest_claim_requests_ephemeral_storage(self):
        source = (_PKG_DIR / "session_manager.py").read_text(encoding="utf-8")
        self.assertIn("ephemeral_storage = is_guest", source)
        self.assertIn("ephemeral_storage=ephemeral_storage", source)

    def test_launcher_skips_the_volume_mount(self):
        source = (_PKG_DIR / "session_launcher.py").read_text(encoding="utf-8")
        self.assertIn(
            "if _persistent_storage_enabled() and not ephemeral_storage:", source
        )

    def test_launcher_service_skips_provisioning(self):
        source = (_PKG_DIR / "session_launcher_service.py").read_text(encoding="utf-8")
        self.assertIn(
            "if _persistent_storage_enabled() and not _ephemeral_storage_for_payload(payload):",
            source,
        )

    def test_launcher_service_forces_ephemeral_for_a_guest_identity(self):
        import session_launcher_service as svc
        address = guest_mode.new_guest_identity()
        # Derived from the wallet, which has already been matched against the
        # live allocation row -- so a dropped flag cannot create a volume.
        self.assertTrue(svc._ephemeral_storage_for_payload({"wallet_address": address}))
        self.assertFalse(svc._ephemeral_storage_for_payload({"wallet_address": WALLET}))
        self.assertTrue(
            svc._ephemeral_storage_for_payload(
                {"wallet_address": WALLET, "ephemeral_storage": True}
            )
        )

    def test_runtime_digest_distinguishes_ephemeral_topology(self):
        from docker_gpu_cli import session_runtime_config_digest
        common = dict(
            session_id=7, wallet=WALLET, profile="small", gpu_ids=[0],
            files_key="k", ssh_enabled=False, network_name="net", image_name="img",
        )
        persistent = session_runtime_config_digest(**common)
        ephemeral = session_runtime_config_digest(**common, ephemeral_storage=True)
        self.assertNotEqual(persistent, ephemeral)

    def test_persistent_digest_is_unchanged_by_the_new_field(self):
        # A digest mismatch means `docker rm -f`. Adding the field must not
        # change any existing persistent launch, or the first claim after a
        # deploy would tear down live sessions.
        from docker_gpu_cli import session_runtime_config_digest
        common = dict(
            session_id=7, wallet=WALLET, profile="small", gpu_ids=[0],
            files_key="k", ssh_enabled=False, network_name="net", image_name="img",
        )
        self.assertEqual(
            session_runtime_config_digest(**common),
            session_runtime_config_digest(**common, ephemeral_storage=False),
        )
        # Pinned: this is the digest releases before guest mode produced.
        self.assertEqual(
            session_runtime_config_digest(**common),
            session_runtime_config_digest(
                session_id=7, wallet=WALLET, profile="small", gpu_ids=[0],
                files_key="k", ssh_enabled=False, network_name="net",
                image_name="img", requested_template="", ssh_pubkey="",
            ),
        )

    def test_guest_media_quality_inherits_the_wallet_session_profile(self):
        helper = (_PKG_DIR / "docker_gpu_cli.py").read_text(encoding="utf-8")
        direct = (_PKG_DIR / "session_launcher.py").read_text(encoding="utf-8")
        service = (_PKG_DIR / "session_launcher_service.py").read_text(encoding="utf-8")
        example = (_REPO_ROOT / "env.example").read_text(encoding="utf-8")

        for source in (helper, direct, service, example):
            self.assertNotIn("AXONOS_GUEST_WEBRTC_CAPTURE", source)
            self.assertNotIn("guest_session_media", source)
        self.assertIn("for env_name in SESSION_MEDIA_ENV_NAMES", direct)
        self.assertIn("for env_name in _env_passthrough_names()", service)
        self.assertIn(
            "_ALLOWED_SESSION_PASSTHROUGH_NAMES = set(SESSION_MEDIA_ENV_NAMES)",
            service,
        )


@unittest.skipUnless(_HAVE_GATE, "Flask / gate_server not importable")
class TestGuestAuthEndpoint(unittest.TestCase):
    def setUp(self):
        gate_server.app.testing = True
        self.client = gate_server.app.test_client()

    def test_endpoint_is_absent_while_the_feature_is_disabled(self):
        with patch.dict(os.environ, {"AXONOS_GUEST_MODE_ENABLED": ""}, clear=False):
            res = self.client.post("/api/auth/guest", json={"invite": INVITE})
        # 404, not 403: a disabled feature must not advertise itself.
        self.assertEqual(res.status_code, 404)

    def test_invite_is_required(self):
        with patch.dict(os.environ, GUEST_ENV, clear=False):
            res = self.client.post("/api/auth/guest", json={})
        self.assertEqual(res.status_code, 400)
        self.assertIn("invite", res.get_json()["error"])

    def test_attempt_id_is_required_before_redemption(self):
        with patch.dict(os.environ, GUEST_ENV, clear=False), patch.object(
            gate_server.guest_mode, "redeem_invite"
        ) as redeem:
            res = self.client.post("/api/auth/guest", json={"invite": INVITE})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "invalid_attempt_id")
        redeem.assert_not_called()

    def test_successful_redemption_issues_a_token_for_a_guest_identity(self):
        address = guest_mode.new_guest_identity()
        self.client.set_cookie("axgt_auth_token", "paid-wallet-token")
        with patch.dict(os.environ, GUEST_ENV, clear=False), \
             patch.object(gate_server.guest_mode, "redeem_invite", return_value={
                 "ok": True, "guest_address": address, "token_hash": TOKEN_HASH,
                 "session_minutes": 30, "expires_at": time.time() + 1800,
                 "remaining_seconds": 1800, "warn_seconds": 300,
                 "allowed_profiles": ["small"], "allowed_templates": [],
             }) as redeem, \
             patch.object(gate_server.guest_mode, "grant_guest_credit",
                          return_value={"ok": True, "credited_minutes": 43.0}), \
             patch.object(gate_server, "_issue_gate_auth_token", return_value=("tok", 1860)) as issue:
            res = self.client.post(
                "/api/auth/guest",
                json={"invite": INVITE, "attempt_id": "guest-attempt-1234567890"},
            )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["guest"])
        self.assertEqual(body["wallet_address"], address)
        self.assertEqual(body["auth_token"], "tok")
        self.assertEqual(body["guest_remaining_seconds"], 1800)
        self.assertEqual(body["guest_warn_seconds"], 300)
        self.assertEqual(
            redeem.call_args.kwargs["attempt_id"],
            "guest-attempt-1234567890",
        )
        self.assertEqual(
            issue.call_args.args,
            (address,),
        )
        self.assertEqual(issue.call_args.kwargs, {})
        cookie = res.headers.get("Set-Cookie", "")
        self.assertEqual(cookie, "")
        self.assertEqual(
            self.client.get_cookie("axgt_auth_token").value,
            "paid-wallet-token",
        )

    def test_a_failed_credit_grant_does_not_issue_a_token(self):
        address = guest_mode.new_guest_identity()
        with patch.dict(os.environ, GUEST_ENV, clear=False), \
             patch.object(gate_server.guest_mode, "redeem_invite", return_value={
                 "ok": True, "guest_address": address, "token_hash": TOKEN_HASH,
                 "session_minutes": 30, "expires_at": time.time() + 1800,
                 "remaining_seconds": 1800, "warn_seconds": 300,
                 "allowed_profiles": ["small"], "allowed_templates": [],
             }), \
             patch.object(gate_server.guest_mode, "grant_guest_credit",
                          return_value={"ok": False, "error_code": "ledger_unavailable"}), \
             patch.object(gate_server, "_issue_gate_auth_token") as issue:
            res = self.client.post(
                "/api/auth/guest",
                json={"invite": INVITE, "attempt_id": "guest-attempt-1234567890"},
            )
        self.assertEqual(res.status_code, 503)
        issue.assert_not_called()
        self.assertTrue(res.get_json()["retryable"])

    def test_guest_auth_failure_does_not_clear_a_paid_wallet_cookie(self):
        address = guest_mode.new_guest_identity()
        self.client.set_cookie("axgt_auth_token", "paid-wallet-token")
        with patch.dict(os.environ, GUEST_ENV, clear=False), \
             patch.object(gate_server.guest_mode, "redeem_invite", return_value={
                 "ok": True, "guest_address": address, "token_hash": TOKEN_HASH,
                 "session_minutes": 30, "expires_at": time.time() + 1800,
                 "remaining_seconds": 1800, "warn_seconds": 300,
                 "allowed_profiles": ["small"], "allowed_templates": [],
             }), \
             patch.object(gate_server.guest_mode, "grant_guest_credit",
                          return_value={"ok": True, "credited_minutes": 35.0}), \
             patch.object(gate_server, "_issue_gate_auth_token",
                          side_effect=RuntimeError("auth unavailable")):
            res = self.client.post(
                "/api/auth/guest",
                json={"invite": INVITE, "attempt_id": "guest-attempt-1234567890"},
            )
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.headers.get("Set-Cookie", ""), "")
        self.assertEqual(
            self.client.get_cookie("axgt_auth_token").value,
            "paid-wallet-token",
        )

    def test_explicit_guest_header_is_not_shadowed_by_an_old_wallet_cookie(self):
        with gate_server.app.test_request_context(
            "/api/session/status?wallet_address=" + WALLET,
            headers={
                "Cookie": "axgt_auth_token=old-wallet-cookie",
                "X-AXGT-Auth-Token": "explicit-guest-token",
            },
        ), patch.object(
            gate_server,
            "_is_gate_auth_token_valid",
            side_effect=lambda token, _wallet: token == "explicit-guest-token",
        ) as validate:
            rejection = gate_server._require_auth_token(WALLET)
        self.assertIsNone(rejection)
        self.assertEqual(validate.call_args.args[0], "explicit-guest-token")

    def test_stale_header_falls_back_to_a_valid_same_wallet_cookie(self):
        with gate_server.app.test_request_context(
            "/api/session/status?wallet_address=" + WALLET,
            headers={
                "Cookie": "axgt_auth_token=current-cookie-token",
                "X-AXGT-Auth-Token": "stale-header-token",
            },
        ), patch.object(
            gate_server,
            "_is_gate_auth_token_valid",
            side_effect=lambda token, _wallet: token == "current-cookie-token",
        ) as validate:
            rejection = gate_server._require_auth_token(WALLET)
        self.assertIsNone(rejection)
        self.assertEqual(
            [call.args[0] for call in validate.call_args_list],
            ["stale-header-token", "current-cookie-token"],
        )

    def test_flask_wallet_status_requires_and_echoes_auth(self):
        with patch.object(gate_server, "_resolve_gate_auth_token", return_value=None), \
             patch.object(gate_server, "get_wallet_access_status") as access:
            denied = self.client.get(
                "/api/auth/wallet-status?wallet_address=" + WALLET
            )
        self.assertEqual(denied.status_code, 401)
        self.assertNotIn("Max-Age=0", denied.headers.get("Set-Cookie", ""))
        access.assert_not_called()

        with patch.object(
            gate_server, "_resolve_gate_auth_token", return_value="grace-token"
        ), patch.object(
            gate_server,
            "_gate_current_wallet_token_and_remaining",
            return_value=("current-token", 120),
        ), patch.object(
            gate_server,
            "get_wallet_access_status",
            return_value={"verified": True, "remaining_minutes": 10.0},
        ), patch.object(
            gate_server, "_can_wallet_mint_guest_invites", return_value=True,
        ):
            restored = self.client.get(
                "/api/auth/wallet-status?wallet_address=" + WALLET
            )
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.get_json()["auth_token"], "current-token")
        self.assertIs(restored.get_json()["guest_invite_minter"], True)
        self.assertIn("axgt_auth_token=current-token", restored.headers.get("Set-Cookie", ""))
        self.assertIn("no-store", restored.headers.get("Cache-Control", ""))

    def test_guest_status_echoes_token_without_overwriting_wallet_cookie(self):
        address = guest_mode.new_guest_identity()
        self.client.set_cookie("axgt_auth_token", "paid-wallet-token")
        with patch.object(
            gate_server, "_resolve_gate_auth_token", return_value="guest-grace-token"
        ), patch.object(
            gate_server,
            "_gate_current_wallet_token_and_remaining",
            return_value=("guest-current-token", 120),
        ), patch.object(
            gate_server,
            "get_wallet_access_status",
            return_value={"verified": True, "remaining_minutes": 10.0},
        ):
            restored = self.client.get(
                "/api/auth/wallet-status?wallet_address=" + address
            )
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.get_json()["auth_token"], "guest-current-token")
        self.assertEqual(restored.headers.get("Set-Cookie", ""), "")
        self.assertEqual(
            self.client.get_cookie("axgt_auth_token").value,
            "paid-wallet-token",
        )

    def test_guest_release_accepts_an_identity_bound_body_bearer_for_beacon(self):
        address = guest_mode.new_guest_identity()
        with patch.object(gate_server, "_session_mgr_available", True), \
             patch.object(
                 gate_server,
                 "_is_gate_auth_token_valid",
                 side_effect=lambda token, wallet: (
                     token == "guest-tab-token" and wallet == address
                 ),
             ) as validate, \
             patch.object(gate_server, "_require_auth_token") as standard_auth, \
             patch.object(
                 gate_server,
                 "release_session",
                 return_value={"released": True},
             ) as release:
            response = self.client.post("/api/session/release", json={
                "wallet_address": address,
                "auth_token": "guest-tab-token",
                "expected_session_id": 42,
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["released"])
        validate.assert_called_once_with("guest-tab-token", address)
        standard_auth.assert_not_called()
        release.assert_called_once_with(address, expected_session_id=42)

    def test_release_body_bearer_is_rejected_for_guests_when_invalid(self):
        address = guest_mode.new_guest_identity()
        with patch.object(gate_server, "_session_mgr_available", True), \
             patch.object(
                 gate_server, "_is_gate_auth_token_valid", return_value=False
             ) as validate, \
             patch.object(
                 gate_server,
                 "_require_auth_token",
                 return_value=({"released": False, "error": "auth"}, 401),
             ) as standard_auth, \
             patch.object(gate_server, "release_session") as release:
            response = self.client.post("/api/session/release", json={
                "wallet_address": address,
                "auth_token": "wrong-guest-token",
            })
        self.assertEqual(response.status_code, 401)
        validate.assert_called_once_with("wrong-guest-token", address)
        standard_auth.assert_called_once_with(address)
        release.assert_not_called()

    def test_release_body_bearer_cannot_authenticate_a_paid_wallet(self):
        with patch.object(gate_server, "_session_mgr_available", True), \
             patch.object(gate_server, "_is_gate_auth_token_valid") as validate, \
             patch.object(
                 gate_server,
                 "_require_auth_token",
                 return_value=({"released": False, "error": "auth"}, 401),
             ) as standard_auth, \
             patch.object(gate_server, "release_session") as release:
            response = self.client.post("/api/session/release", json={
                "wallet_address": WALLET,
                "auth_token": "paid-wallet-body-token",
            })
        self.assertEqual(response.status_code, 401)
        validate.assert_not_called()
        standard_auth.assert_called_once_with(WALLET)
        release.assert_not_called()

    def test_guest_auth_ttl_has_an_absolute_deadline(self):
        address = guest_mode.new_guest_identity()
        with patch.object(
            gate_server.guest_mode,
            "guest_session_for",
            return_value={"expires_at": 1000.0},
        ), patch.object(
            gate_server, "_guest_token_grace_seconds", return_value=60
        ):
            for now_ts, expected in (
                (999.0, 61),
                (1059.0, 1),
                (1060.0, None),
                (1061.0, None),
            ):
                with self.subTest(now_ts=now_ts):
                    self.assertEqual(
                        gate_server._gate_guest_auth_ttl_seconds(
                            address, now_ts=now_ts
                        ),
                        expected,
                    )

    def test_guest_issuance_and_validation_fail_closed_without_the_guest_row(self):
        address = guest_mode.new_guest_identity()
        with patch.object(
            gate_server.guest_mode, "guest_session_for", return_value=None
        ), patch.object(
            gate_server.time, "time", return_value=1000.0
        ), patch.object(gate_server, "_gate_pg_init_once") as auth_init:
            self.assertIsNone(gate_server._gate_guest_auth_deadline(address))
            with self.assertRaisesRegex(RuntimeError, "Guest auth deadline"):
                gate_server._issue_gate_auth_token(address)
            self.assertFalse(gate_server._is_gate_auth_token_valid("legacy", address))
        auth_init.assert_not_called()

        # The reserved namespace remains fail-closed even if guest_mode cannot
        # be imported at all; it must never fall through to the wallet TTL.
        with patch.object(gate_server, "guest_mode", None), patch.object(
            gate_server.time, "time", return_value=1000.0
        ), patch.object(gate_server, "_gate_pg_init_once") as auth_init:
            with self.assertRaisesRegex(RuntimeError, "Guest auth deadline"):
                gate_server._issue_gate_auth_token(address)
            self.assertFalse(gate_server._is_gate_auth_token_valid("legacy", address))
        auth_init.assert_not_called()

    def test_guest_issuance_and_current_token_ttl_are_clamped_to_deadline(self):
        address = guest_mode.new_guest_identity()
        issue_conn, issue_cur = _mock_conn()
        current_conn, current_cur = _mock_conn()
        current_cur.fetchone.return_value = ("legacy-current", 9999.0)
        connections = iter((issue_conn, current_conn))
        with patch.object(
            gate_server.guest_mode,
            "guest_session_for",
            return_value={"expires_at": 1000.0},
        ), patch.object(
            gate_server, "_guest_token_grace_seconds", return_value=60
        ), patch.object(
            gate_server.time, "time", return_value=1050.0
        ), patch.object(
            gate_server, "_gate_pg_init_once", return_value=True
        ), patch.object(
            gate_server, "_gate_pg_get_connection", side_effect=connections
        ):
            _token, ttl = gate_server._issue_gate_auth_token(
                address, custom_ttl=9999
            )
            current = gate_server._gate_current_wallet_token_and_remaining(address)

        self.assertEqual(ttl, 10)
        insert_params = next(
            call.args[1]
            for call in issue_cur.execute.call_args_list
            if "INSERT INTO" in call.args[0]
        )
        self.assertEqual(insert_params[3], 1060.0)
        self.assertEqual(current, ("legacy-current", 10))

    def test_legacy_guest_token_is_rejected_at_the_absolute_deadline(self):
        address = guest_mode.new_guest_identity()
        conn, cur = _mock_conn()
        # Simulate a token minted by the old rotating implementation far past
        # the demo deadline. The guest row, not that legacy expiry, is binding.
        cur.fetchone.return_value = ("current", 9999.0, 9999.0)
        with patch.object(
            gate_server.guest_mode,
            "guest_session_for",
            return_value={"expires_at": 1000.0},
        ), patch.object(
            gate_server, "_guest_token_grace_seconds", return_value=60
        ), patch.object(
            gate_server, "_gate_pg_init_once", return_value=True
        ), patch.object(
            gate_server, "_gate_pg_get_connection", return_value=conn
        ), patch.object(gate_server.time, "time", return_value=1059.0):
            self.assertTrue(gate_server._is_gate_auth_token_valid("legacy", address))

        with patch.object(
            gate_server.guest_mode,
            "guest_session_for",
            return_value={"expires_at": 1000.0},
        ), patch.object(
            gate_server, "_guest_token_grace_seconds", return_value=60
        ), patch.object(
            gate_server, "_gate_pg_init_once"
        ) as auth_init, patch.object(
            gate_server.time, "time", return_value=1060.0
        ):
            self.assertFalse(gate_server._is_gate_auth_token_valid("legacy", address))
        auth_init.assert_not_called()

    def test_flask_wallet_sign_in_sets_the_reload_cookie(self):
        with patch.object(
            gate_server, "verify_signed_challenge", return_value=True
        ), patch.object(
            gate_server,
            "get_wallet_access_status",
            return_value={"verified": True, "remaining_minutes": 10.0},
        ), patch.object(
            gate_server,
            "_issue_gate_auth_token",
            return_value=("wallet-token", 300),
        ):
            response = self.client.post("/api/auth/verify-wallet", json={
                "wallet_address": WALLET,
                "message": "signed challenge",
                "signature": "0x1234",
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "axgt_auth_token=wallet-token",
            response.headers.get("Set-Cookie", ""),
        )

    def test_redemption_failures_map_to_stable_statuses(self):
        cases = {
            "invite_exhausted": 403,
            "invite_expired": 403,
            "invite_revoked": 403,
            "invite_session_active": 409,
            "guest_db_unavailable": 503,
            "invalid_invite": 400,
        }
        for code, expected in cases.items():
            with patch.dict(os.environ, GUEST_ENV, clear=False), \
                 patch.object(gate_server.guest_mode, "redeem_invite",
                              return_value={"ok": False, "error_code": code, "error": "no"}):
                res = self.client.post(
                    "/api/auth/guest",
                    json={"invite": INVITE, "attempt_id": "guest-attempt-1234567890"},
                )
            self.assertEqual(res.status_code, expected, code)

    def test_config_advertises_the_feature_state(self):
        with patch.dict(os.environ, {"AXONOS_GUEST_MODE_ENABLED": ""}, clear=False):
            self.assertFalse(self.client.get("/api/config").get_json()["guest_mode_enabled"])
        with patch.dict(os.environ, GUEST_ENV, clear=False):
            self.assertTrue(self.client.get("/api/config").get_json()["guest_mode_enabled"])


@unittest.skipUnless(_HAVE_GATE, "Flask / gate_server not importable")
class TestGuestNamespaceIsClosed(unittest.TestCase):
    """No signature may ever mint a token for a guest-shaped address."""

    def setUp(self):
        gate_server.app.testing = True
        self.client = gate_server.app.test_client()
        self.address = guest_mode.new_guest_identity()

    def test_challenge_refuses_a_guest_address(self):
        res = self.client.get(f"/api/auth/challenge?wallet_address={self.address}")
        self.assertEqual(res.status_code, 400)

    def test_challenge_still_serves_a_real_wallet(self):
        res = self.client.get(f"/api/auth/challenge?wallet_address={WALLET}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("challenge", res.get_json())

    def test_verify_wallet_refuses_a_guest_address(self):
        res = self.client.post("/api/auth/verify-wallet", json={
            "wallet_address": self.address, "message": "m", "signature": "0x00",
        })
        self.assertEqual(res.status_code, 400)


@unittest.skipUnless(_HAVE_GATE, "Flask / gate_server not importable")
class TestGuestDeniedOnPaymentRails(unittest.TestCase):
    def setUp(self):
        gate_server.app.testing = True
        self.client = gate_server.app.test_client()
        self.address = guest_mode.new_guest_identity()

    def _assert_refused(self, path, payload):
        res = self.client.post(path, json=payload)
        self.assertEqual(res.status_code, 403, f"{path} -> {res.status_code}")
        body = res.get_json()
        self.assertEqual(body["error_code"], "guest_not_permitted", path)
        return body

    def test_test_credit_is_refused(self):
        self._assert_refused("/api/auth/test-credit", {
            "wallet_address": self.address, "rail": "usdc", "request_id": "req-00000001",
        })

    def test_axgt_deposit_is_refused(self):
        self._assert_refused("/api/auth/verify-deposit", {
            "wallet_address": self.address, "tx_hash": "0x" + "ab" * 32,
        })

    def test_usdc_deposit_is_refused(self):
        self._assert_refused("/api/auth/verify-usdc-deposit", {
            "wallet_address": self.address, "tx_hash": "0x" + "ab" * 32,
        })

    def test_auto_deposit_is_refused(self):
        self._assert_refused("/api/auth/verify-deposit-auto", {
            "wallet_address": self.address, "tx_hash": "0x" + "ab" * 32,
        })

    def test_x402_session_is_refused(self):
        res = self.client.post("/api/x402/session", json={"wallet_address": self.address})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error_code"], "guest_not_permitted")

    def test_refusal_precedes_the_auth_token_check(self):
        # A demo identity holding a currently valid token must still be refused.
        with patch.object(gate_server, "_require_auth_token", return_value=None):
            self._assert_refused("/api/auth/verify-deposit", {
                "wallet_address": self.address, "tx_hash": "0x" + "ab" * 32,
            })


@unittest.skipUnless(_HAVE_GATE, "Flask / gate_server not importable")
class TestWalletPathsNotWeakened(unittest.TestCase):
    """The wallet-gated surface must behave exactly as before."""

    def setUp(self):
        gate_server.app.testing = True
        self.client = gate_server.app.test_client()

    def test_real_wallet_is_not_refused_as_a_guest(self):
        self.assertIsNone(gate_server._guest_denied(WALLET))

    def test_claim_rejects_a_non_string_template(self):
        res = self.client.post("/api/session/claim", json={
            "wallet_address": WALLET,
            "requested_template": ["pytorch"],
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("must be a string", res.get_json()["error"])

    def test_compute_endpoints_still_require_a_token(self):
        for path, payload in (
            ("/api/session/claim", {"wallet_address": WALLET}),
            ("/api/session/heartbeat", {"wallet_address": WALLET}),
            ("/api/session/restart", {"wallet_address": WALLET}),
        ):
            with patch.object(
                gate_server, "_require_auth_token",
                return_value=({"error": "Valid auth token required"}, 401),
            ):
                res = self.client.post(path, json=payload)
            self.assertIn(res.status_code, (401, 503), f"{path} -> {res.status_code}")

    def test_deposit_endpoints_still_reject_a_malformed_wallet(self):
        res = self.client.post(
            "/api/auth/verify-deposit",
            json={"wallet_address": "nope", "tx_hash": "0x" + "ab" * 32},
        )
        self.assertEqual(res.status_code, 400)

    def test_admin_invite_routes_require_the_admin_secret(self):
        # Same contract as the existing admin routes: 401 without the secret.
        with patch.dict(os.environ, {**GUEST_ENV, "AXGT_ADMIN_SECRET": "s3cret"}, clear=False):
            self.assertEqual(
                self.client.post("/api/admin/guest-invite", json={}).status_code, 401
            )
            self.assertEqual(
                self.client.get("/api/admin/guest-invites").status_code, 401
            )
            self.assertEqual(
                self.client.post("/api/admin/guest-invite/revoke", json={}).status_code,
                401,
            )

    def test_admin_invite_routes_are_off_without_an_admin_secret(self):
        with patch.dict(os.environ, {**GUEST_ENV, "AXGT_ADMIN_SECRET": ""}, clear=False):
            self.assertEqual(
                self.client.post("/api/admin/guest-invite", json={}).status_code, 503
            )


class TestWebsockifyGuestAuthDeadline(unittest.TestCase):
    """Execute the auth helpers without requiring websockify to be installed."""

    @staticmethod
    def _load_proxy_functions(*names):
        source = (_PKG_DIR / "websockify_gate.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        wanted = set(names)
        nodes = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in wanted
        ]
        found = {node.name for node in nodes}
        if found != wanted:
            raise AssertionError(f"missing proxy helpers: {sorted(wanted - found)}")
        namespace = {"math": math}
        module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        exec(compile(module, "websockify_gate.py", "exec"), namespace)
        return namespace

    def _namespace(self, *names, now_ts=1000.0, record=None):
        namespace = self._load_proxy_functions(*names)
        clock = MagicMock()
        clock.time.return_value = now_ts
        guest = MagicMock()
        guest.guest_session_for.return_value = record
        namespace.update({
            "time": clock,
            "guest_mode": guest,
            "_is_guest_shaped": lambda _address: True,
            "_guest_token_grace_seconds": lambda: 60,
        })
        return namespace, clock, guest

    def test_proxy_guest_ttl_has_the_same_absolute_boundaries(self):
        namespace, _clock, _guest = self._namespace(
            "_guest_auth_deadline",
            "_guest_auth_ttl_seconds",
            record={"expires_at": 1000.0},
        )
        ttl_for = namespace["_guest_auth_ttl_seconds"]
        for now_ts, expected in (
            (999.0, 61),
            (1059.0, 1),
            (1060.0, None),
            (1061.0, None),
        ):
            with self.subTest(now_ts=now_ts):
                self.assertEqual(ttl_for(WALLET, now_ts=now_ts), expected)

    def test_proxy_central_issuance_clamps_and_fails_closed(self):
        names = (
            "_guest_auth_deadline",
            "_guest_auth_ttl_seconds",
            "_issue_auth_token",
        )
        namespace, _clock, _guest = self._namespace(
            *names, now_ts=1050.0, record=None
        )
        auth_init = MagicMock()
        namespace.update({
            "_auth_ttl_seconds": lambda: 300,
            "_auth_pg_init_once": auth_init,
            "secrets": MagicMock(),
        })
        with self.assertRaisesRegex(RuntimeError, "Guest auth deadline"):
            namespace["_issue_auth_token"](WALLET)
        auth_init.assert_not_called()

        # A missing guest module is also terminal for the reserved namespace.
        namespace["guest_mode"] = None
        with self.assertRaisesRegex(RuntimeError, "Guest auth deadline"):
            namespace["_issue_auth_token"](WALLET)
        auth_init.assert_not_called()

        namespace, _clock, _guest = self._namespace(
            *names, now_ts=1050.0, record={"expires_at": 1000.0}
        )
        conn, cur = _mock_conn()
        secrets_stub = MagicMock()
        secrets_stub.token_urlsafe.return_value = "bounded-token"
        namespace.update({
            "_auth_ttl_seconds": lambda: 300,
            "_auth_pg_init_once": lambda: True,
            "_auth_pg_get_connection": lambda: conn,
            "_auth_grace_seconds": lambda: 15,
            "_AUTH_TABLE": "axgt_auth_tokens",
            "logger": MagicMock(),
            "secrets": secrets_stub,
        })
        token, ttl = namespace["_issue_auth_token"](WALLET, custom_ttl=9999)
        self.assertEqual((token, ttl), ("bounded-token", 10))
        insert_params = next(
            call.args[1]
            for call in cur.execute.call_args_list
            if "INSERT INTO" in call.args[0]
        )
        self.assertEqual(insert_params[3], 1060.0)

    def test_proxy_legacy_validation_and_echoed_ttl_stop_at_deadline(self):
        validation_names = ("_guest_auth_deadline", "_is_auth_token_valid")
        namespace, clock, _guest = self._namespace(
            *validation_names,
            now_ts=1059.0,
            record={"expires_at": 1000.0},
        )
        conn, cur = _mock_conn()
        cur.fetchone.return_value = ("current", 9999.0, 9999.0)
        auth_init = MagicMock(return_value=True)
        namespace.update({
            "_auth_pg_init_once": auth_init,
            "_auth_pg_get_connection": lambda: conn,
            "_AUTH_TABLE": "axgt_auth_tokens",
            "logger": MagicMock(),
        })
        self.assertTrue(namespace["_is_auth_token_valid"]("legacy", WALLET))

        clock.time.return_value = 1060.0
        auth_init.reset_mock()
        self.assertFalse(namespace["_is_auth_token_valid"]("legacy", WALLET))
        auth_init.assert_not_called()

        current_names = (
            "_guest_auth_deadline",
            "_guest_auth_ttl_seconds",
            "_current_wallet_token_and_remaining",
        )
        namespace, _clock, _guest = self._namespace(
            *current_names,
            now_ts=1059.0,
            record={"expires_at": 1000.0},
        )
        conn, cur = _mock_conn()
        cur.fetchone.return_value = ("legacy-current", 9999.0)
        namespace.update({
            "_auth_pg_init_once": lambda: True,
            "_auth_pg_get_connection": lambda: conn,
            "_AUTH_TABLE": "axgt_auth_tokens",
            "logger": MagicMock(),
        })
        self.assertEqual(
            namespace["_current_wallet_token_and_remaining"](WALLET),
            ("legacy-current", 1),
        )

    def test_proxy_rotation_does_not_fall_back_when_guest_row_is_missing(self):
        namespace, _clock, _guest = self._namespace(
            "_guest_auth_deadline",
            "_guest_auth_ttl_seconds",
            "_rotate_auth_token",
            record=None,
        )
        auth_init = MagicMock()
        namespace["_auth_pg_init_once"] = auth_init
        self.assertEqual(
            namespace["_rotate_auth_token"]("legacy", WALLET),
            (None, 0),
        )
        auth_init.assert_not_called()


class TestDualGateParity(unittest.TestCase):
    """Both gate servers re-implement /api routes; :6080 is the browser path."""

    def setUp(self):
        self.gate = (_PKG_DIR / "gate_server.py").read_text(encoding="utf-8")
        self.proxy = (_PKG_DIR / "websockify_gate.py").read_text(encoding="utf-8")
        self.ui = (_REPO_ROOT / "novnc-theme" / "ui.js").read_text(encoding="utf-8")

    def test_both_servers_serve_the_guest_auth_route(self):
        self.assertIn("/api/auth/guest", self.gate)
        self.assertIn("/api/auth/guest", self.proxy)

    def test_both_servers_gate_the_route_behind_the_feature_flag(self):
        for name, source in (("gate_server", self.gate), ("websockify_gate", self.proxy)):
            self.assertIn("_guest_mode_ready()", source, name)

    def test_both_servers_bind_retries_to_the_same_browser_attempt(self):
        for name, source in (("gate_server", self.gate), ("websockify_gate", self.proxy)):
            guest_route = source.split("redeem_invite(invite, attempt_id=attempt_value)", 1)
            self.assertEqual(len(guest_route), 2, name)

    def test_wallet_status_canonicalizes_stale_tokens_in_both_gates(self):
        self.assertIn("_valid_auth_token_from_path_and_headers", self.proxy)
        self.assertIn("_current_wallet_token_and_remaining(wallet_address)", self.proxy)
        self.assertIn("_resolve_gate_auth_token(wallet_address)", self.gate)
        self.assertIn("_gate_current_wallet_token_and_remaining(wallet_address)", self.gate)
        for name, source in (("gate_server", self.gate), ("websockify_gate", self.proxy)):
            status = source.split("/api/auth/wallet-status", 1)[1]
            self.assertIn("auth_token_expires_in_seconds", status, name)
        self.assertIn("window.verifiedWalletAuthToken = data.auth_token", self.ui)

    def test_both_servers_keep_guest_auth_tab_scoped_not_cookie_scoped(self):
        guest_gate = self.gate.split("def api_auth_guest():", 1)[1]
        guest_gate = guest_gate.split("def _guest_token_grace_seconds", 1)[0]
        self.assertNotIn("_set_gate_auth_cookie", guest_gate)
        guest_proxy = self.proxy.split("if ponly == '/api/auth/guest':", 1)[1]
        guest_proxy = guest_proxy.split("if ponly == '/api/auth/test-credit':", 1)[0]
        self.assertNotIn("_build_auth_cookie", guest_proxy)
        self.assertNotIn("_clear_auth_cookie", guest_proxy)

        gate_status = self.gate.split("def wallet_status():", 1)[1]
        gate_status = gate_status.split("def api_test_credit", 1)[0]
        self.assertIn("if not _is_guest_shaped(wallet_address)", gate_status)
        proxy_status = self.proxy.split(
            "if self.path.startswith('/api/auth/wallet-status'):", 1
        )[1].split("if webrtc_service", 1)[0]
        self.assertIn("None if _is_guest_shaped(wallet_address)", proxy_status)

    def test_both_release_handlers_scope_body_bearers_to_guests(self):
        gate_release = self.gate.split("def api_session_release():", 1)[1]
        gate_release = gate_release.split("def api_session_restart", 1)[0]
        proxy_release = self.proxy.split(
            "self.path.startswith('/api/session/release')", 1
        )[1].split("self.path.startswith('/api/session/restart')", 1)[0]
        for name, source, validator in (
            ("gate_server", gate_release, "_is_gate_auth_token_valid"),
            ("websockify_gate", proxy_release, "_is_auth_token_valid"),
        ):
            self.assertIn("data.get('auth_token')", source, name)
            self.assertIn("_is_guest_shaped(wallet_address)", source, name)
            self.assertIn(validator, source, name)
            self.assertIn("if not guest_body_authenticated", source, name)

    def test_proxy_guest_token_uses_the_central_absolute_deadline(self):
        issuer = self.proxy.split("def _issue_auth_token", 1)[1].split("\n\ndef ", 1)[0]
        self.assertIn("custom_ttl", issuer)
        self.assertIn("_guest_auth_ttl_seconds(wallet_address, now_ts=now_ts)", issuer)
        self.assertIn("Guest auth deadline unavailable or expired", issuer)
        guest_proxy = self.proxy.split("if ponly == '/api/auth/guest':", 1)[1]
        guest_proxy = guest_proxy.split("if ponly == '/api/auth/test-credit':", 1)[0]
        self.assertIn("_issue_auth_token(guest_address)", guest_proxy)
        rotation = self.proxy.split("def _rotate_auth_token", 1)[1].split("\n\ndef ", 1)[0]
        self.assertIn("_guest_auth_ttl_seconds(wallet_address, now_ts=now_ts)", rotation)
        self.assertNotIn("_guest_auth_ttl_seconds(wallet_address) or", rotation)

    def test_both_servers_close_the_guest_namespace_to_signatures(self):
        for name, source in (("gate_server", self.gate), ("websockify_gate", self.proxy)):
            self.assertGreaterEqual(
                source.count("_is_guest_shaped(wallet_address)"), 2,
                f"{name} must refuse guest addresses at both SIWE routes",
            )

    def test_namespace_guard_fails_closed_without_guest_mode(self):
        # 40 bits of prefix is within vanity-mining reach, so this refusal is the
        # only thing stopping a mined address from signing in. It must not depend
        # on guest_mode being importable.
        for name, source in (("gate_server", self.gate), ("websockify_gate", self.proxy)):
            self.assertIn("_GUEST_ADDRESS_RE_FALLBACK", source, name)
            helper = source.split("def _is_guest_shaped", 1)[1].split("\n\n", 1)[0]
            self.assertIn("_GUEST_ADDRESS_RE_FALLBACK.match", helper, name)

    def test_both_servers_serve_the_sponsor_mint_route(self):
        self.assertIn("/api/auth/guest-invite", self.gate)
        self.assertIn("/api/auth/guest-invite", self.proxy)
        for name, source in (("gate_server", self.gate), ("websockify_gate", self.proxy)):
            self.assertIn("can_mint_invites(wallet_address)", source, name)

    def test_both_servers_refuse_guests_on_every_payment_rail(self):
        expected = (
            "/api/auth/test-credit",
            "/api/auth/verify-deposit",
            "/api/auth/verify-usdc-deposit",
            "/api/auth/verify-deposit-auto",
            "/api/x402/session",
        )
        for route in expected:
            self.assertIn(route, self.gate)
            self.assertIn(route, self.proxy)
        # gate_server uses _guest_denied; websockify uses _guest_rejection.
        self.assertGreaterEqual(self.gate.count("_guest_denied("), 5)
        self.assertGreaterEqual(self.proxy.count("_guest_rejection("), 5)


class TestDemoDeadlineIsExact(unittest.TestCase):
    """The cap must hold whatever AXGT_SESSION_GRACE_SECONDS is set to."""

    def test_sliding_ttl_pins_the_exact_deadline(self):
        now = 1_000_000.0
        self.assertEqual(session_manager._guest_expires_at(1800.0, now), now + 1800.0)

    def test_a_grace_longer_than_the_demo_cannot_extend_it(self):
        # hard_expires_at + grace would fire an hour out for a 30-minute demo;
        # expires_at is compared with no grace allowance, so it still ends on time.
        with patch.dict(os.environ, {"AXGT_SESSION_GRACE_SECONDS": "3600"}, clear=False):
            now = 1_000_000.0
            hard = session_manager._guest_hard_expires_at(1800.0, now)
            hard_fires_at = hard + session_manager.session_grace_seconds()
            exact = session_manager._guest_expires_at(1800.0, now)
            self.assertGreater(hard_fires_at - now, 1800.0)   # the old behaviour
            self.assertEqual(exact - now, 1800.0)             # the binding one
            self.assertLess(exact, hard_fires_at)

    def test_claim_writes_the_demo_deadline_to_both_columns(self):
        source = (_PKG_DIR / "session_manager.py").read_text(encoding="utf-8")
        self.assertIn("_guest_hard_expires_at(", source)
        self.assertIn("_guest_expires_at(", source)

    def test_heartbeat_slide_is_clamped_for_a_demo(self):
        # expires_at slides forward on every heartbeat; unclamped it would walk
        # a demo past its deadline indefinitely.
        address = guest_mode.new_guest_identity()
        deadline = time.time() + 600
        stub = MagicMock()
        stub.is_guest_identity.side_effect = guest_mode.is_guest_identity
        stub.guest_session_for.return_value = {"expires_at": deadline}
        with patch.object(session_manager, "_import_guest_mode", return_value=stub):
            proposed = time.time() + 3600
            clamped = session_manager._guest_clamped_expires_at(address, proposed, time.time())
            self.assertEqual(clamped, deadline)
            # A real wallet's slide is untouched.
            self.assertEqual(
                session_manager._guest_clamped_expires_at(WALLET, proposed, time.time()),
                proposed,
            )


@unittest.skipUnless(_HAVE_GATE, "Flask / gate_server not importable")
class TestSponsorMintEndpoint(unittest.TestCase):
    def setUp(self):
        gate_server.app.testing = True
        self.client = gate_server.app.test_client()
        self.env = patch.dict(os.environ, {
            **GUEST_ENV, "AXONOS_GUEST_INVITE_MINTERS": SPONSOR,
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_absent_while_the_feature_is_disabled(self):
        with patch.dict(os.environ, {"AXONOS_GUEST_MODE_ENABLED": ""}, clear=False):
            res = self.client.post("/api/auth/guest-invite", json={"wallet_address": SPONSOR})
        self.assertEqual(res.status_code, 404)

    def test_requires_an_auth_token(self):
        with patch.object(
            gate_server, "_require_auth_token",
            return_value=({"error": "Valid auth token required"}, 401),
        ):
            res = self.client.post("/api/auth/guest-invite", json={"wallet_address": SPONSOR})
        self.assertEqual(res.status_code, 401)

    def test_non_minter_wallet_is_refused(self):
        with patch.object(gate_server, "_require_auth_token", return_value=None):
            res = self.client.post("/api/auth/guest-invite", json={"wallet_address": WALLET})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error_code"], "not_invite_minter")

    def test_a_demo_identity_cannot_mint(self):
        address = guest_mode.new_guest_identity()
        with patch.object(gate_server, "_require_auth_token", return_value=None):
            res = self.client.post("/api/auth/guest-invite", json={"wallet_address": address})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error_code"], "guest_not_permitted")

    def test_approved_minter_gets_a_link_attributed_to_them(self):
        with patch.object(gate_server, "_require_auth_token", return_value=None), \
             patch.object(gate_server.guest_mode, "mint_invite", return_value={
                 "ok": True, "token": "tok123", "token_hash": TOKEN_HASH,
                 "label": "acme", "max_uses": 1, "session_minutes": 30,
                 "allowed_profiles": ["small"], "allowed_templates": [],
                 "expires_at": time.time() + 3600, "created_by": SPONSOR,
             }) as mint:
            res = self.client.post(
                "/api/auth/guest-invite",
                json={"wallet_address": SPONSOR, "label": "acme"},
            )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertIn("invite=tok123", body["invite_url"])
        self.assertEqual(mint.call_args.kwargs["created_by"], SPONSOR.lower())

    def test_quota_rejections_map_to_429(self):
        for code in ("sponsor_daily_limit", "sponsor_live_limit"):
            with patch.object(gate_server, "_require_auth_token", return_value=None), \
                 patch.object(gate_server.guest_mode, "mint_invite",
                              return_value={"ok": False, "error_code": code, "error": "x"}):
                res = self.client.post(
                    "/api/auth/guest-invite", json={"wallet_address": SPONSOR})
            self.assertEqual(res.status_code, 429, code)


class TestFrontendGuestContract(unittest.TestCase):
    """Source-level guarantees for the demo path in the browser bundle."""

    @classmethod
    def setUpClass(cls):
        cls.vnc = (_REPO_ROOT / "novnc-theme" / "vnc.html").read_text(encoding="utf-8")
        cls.ui = (_REPO_ROOT / "novnc-theme" / "ui.js").read_text(encoding="utf-8")
        cls.css = (_REPO_ROOT / "novnc-theme" / "axonos-theme.css").read_text(
            encoding="utf-8"
        )
        cls.gate = (_PKG_DIR / "gate_server.py").read_text(encoding="utf-8")
        cls.proxy = (_PKG_DIR / "websockify_gate.py").read_text(encoding="utf-8")
        cls.webrtc = (
            _REPO_ROOT / "novnc-theme" / "app" / "webrtc" / "axonos-webrtc.js"
        ).read_text(encoding="utf-8")

    def test_guest_offer_embeds_candidates_for_mixed_version_rollouts(self):
        self.assertIn("function _waitIceGatheringComplete", self.webrtc)
        offer = self.webrtc.split("const offer = await pc.createOffer();", 1)[1]
        offer = offer.split("const offerRes =", 1)[0]
        self.assertIn("/^0x6775657374", offer)
        self.assertIn("await _waitIceGatheringComplete(pc, 5000)", offer)

    def test_connected_ui_requires_a_decoded_desktop_frame(self):
        self.assertIn("function _waitForDecodedVideoFrame", self.webrtc)
        self.assertLess(
            self.webrtc.index("await _waitForDecodedVideoFrame"),
            self.webrtc.index("UI.connected = true"),
        )

    def test_guest_sidebar_reports_demo_time_not_paid_resources(self):
        self.assertIn('id="axonos_sidebar_balance_label"', self.vnc)
        self.assertIn("label.textContent = active ? 'Demo Time'", self.vnc)
        self.assertIn("topup.style.display = active ? 'none'", self.vnc)
        self.assertIn("relaunch.style.display = active ? 'none'", self.vnc)
        self.assertIn("storage.style.display = active ? 'none'", self.vnc)
        self.assertIn("#axonos_sidebar_topup_btn", self.css)
        self.assertIn("#axonos_sidebar_swap_btn", self.css)
        self.assertIn('id="axonos_wizard_storage_section"', self.vnc)
        self.assertIn('id="axonos_wizard_rail_storage_section"', self.vnc)
        self.assertIn("#axonos_wizard_storage_section", self.css)
        self.assertIn("#axonos_wizard_rail_storage_section", self.css)
        self.assertIn("function axonosSyncGuestWizardStorage", self.vnc)
        self.assertIn("axonosSyncGuestWizardStorage(true)", self.vnc)
        self.assertIn("axonosSyncGuestWizardStorage(false)", self.vnc)
        self.assertIn("active ? 'important' : ''", self.vnc)

    def test_expired_demo_cancel_returns_to_public_landing(self):
        upsell = self.vnc.split("function axonosShowGuestUpsell", 1)[1]
        upsell = upsell.split("window.axonosShowGuestUpsell", 1)[0]
        teardown = upsell.index("axonosTeardownGuestSession")
        route = upsell.index("UI._axonosReturnToWorkspace")
        open_panel = upsell.index("UI.openConnectPanel")
        self.assertLess(teardown, route)
        self.assertLess(route, open_panel)
        self.assertIn("reason: 'guest-expired-exit'", upsell)
        self.assertIn("axonosUpdateActiveScreen('landing')", upsell)

    def test_wallet_preflight_short_circuits_for_a_demo(self):
        # Without this, axonosEnsureWalletSessionCurrent asks a non-existent
        # provider for eth_accounts and fails closed, blocking every demo claim.
        body = self.vnc.split("window.axonosEnsureWalletSessionCurrent = function", 1)[1]
        head = body.split("resolveEthereumProvider", 1)[0]
        self.assertIn("window.axonosGuestSession", head)

    def test_public_demo_code_entry_accepts_code_or_complete_link(self):
        self.assertIn('id="axonos_topbar_demo_code_btn"', self.vnc)
        self.assertIn('id="axonos_guest_redeem_modal"', self.vnc)
        normalizer = self.vnc.split("function axonosNormalizeGuestInviteInput", 1)[1]
        normalizer = normalizer.split("\n        function ", 1)[0]
        self.assertIn("new URL(raw, window.location.origin)", normalizer)
        self.assertIn("searchParams.get('invite')", normalizer)
        self.assertIn("^[A-Za-z0-9_-]{16,128}$", normalizer)
        submit = self.vnc.split("function axonosSubmitGuestRedeemDialog", 1)[1]
        submit = submit.split("\n        function ", 1)[0]
        self.assertIn("axonosRedeemGuestInvite(token)", submit)
        self.assertIn("window.axonosGuestEntryPending = true", submit)

    def test_invite_creator_exposes_separate_one_time_link_and_code_copy(self):
        self.assertIn('id="axonos_guest_invite_url"', self.vnc)
        self.assertIn('id="axonos_guest_invite_token"', self.vnc)
        self.assertIn('id="axonos_guest_invite_copy_token"', self.vnc)
        create = self.vnc.split("function axonosCreateGuestInvite", 1)[1]
        create = create.split("\n        function ", 1)[0]
        self.assertIn("el.url.value = data.invite_url", create)
        self.assertIn("el.token.value = data.token", create)
        close = self.vnc.split("function axonosCloseGuestInviteDialog", 1)[1]
        close = close.split("\n        function ", 1)[0]
        self.assertIn("url.value = ''", close)
        self.assertIn("token.value = ''", close)
        self.assertIn("bearer secret", self.vnc)

    def test_workspace_invite_button_is_server_eligibility_gated(self):
        self.assertIn('id="axonos_dashboard_guest_invite_btn"', self.vnc)
        self.assertIn('class="axonos-dashboard-guest-btn" hidden', self.vnc)
        setter = self.vnc.split("function axonosSetGuestInviteEligibility", 1)[1]
        setter = setter.split("window.axonosSetGuestInviteEligibility", 1)[0]
        self.assertIn("dataOrEligible.guest_invite_minter === true", setter)
        self.assertIn("button.hidden = !eligible", setter)

    def test_workspace_mints_with_the_current_wallet_bearer(self):
        block = self.vnc.split("function axonosCreateGuestInvite", 1)[1]
        block = block.split("function axonosCopyGuestInvite", 1)[0]
        self.assertIn("'/api/auth/guest-invite'", block)
        self.assertIn("'X-Wallet-Address': wallet", block)
        self.assertIn("'X-AXGT-Auth-Token': token", block)
        self.assertIn("axonosPaymentIdentityIsCurrent(wallet)", block)
        self.assertIn("el.url.value = data.invite_url", block)

    def test_both_wallet_status_routes_expose_invite_eligibility(self):
        for name, source in (("gate_server", self.gate), ("websockify_gate", self.proxy)):
            self.assertIn(
                "status['guest_invite_minter'] = _can_wallet_mint_guest_invites(wallet_address)",
                source,
                name,
            )

    def test_public_config_exposes_guest_mode_on_the_browser_serving_route(self):
        self.assertIn('"guest_mode_enabled": _guest_mode_ready()', self.proxy)
        self.assertIn("axonosConfig.guest_mode_enabled", self.vnc)
        self.assertIn("demoCodeButton.hidden = !axonosConfig.guest_mode_enabled", self.vnc)

    def test_invite_token_is_stripped_from_the_url(self):
        block = self.vnc.split("function axonosTakeGuestInviteFromUrl", 1)[1]
        block = block.split("\n        function ", 1)[0]
        self.assertIn("params.delete('invite')", block)
        self.assertIn("replaceState", block)

    def test_guest_entry_is_marked_before_remembered_wallet_paint(self):
        marker = self.vnc.index("(function captureAxonosGuestEntry()")
        remembered_paint = self.vnc.index("Optimistic connected paint")
        self.assertLess(marker, remembered_paint)
        paint = self.vnc[remembered_paint:self.vnc.index("</script>", remembered_paint)]
        self.assertIn("axonosGuestEntryPendingOrActive", paint)

    def test_automatic_wallet_restores_defer_to_guest_entry(self):
        probe = self.vnc.split("function axonosProbePausedResumeOnLoad", 1)[1]
        probe = probe.split("\n        function ", 1)[0]
        self.assertIn("axonosGuestEntryPendingOrActive", probe)
        restore = self.vnc.split("Silent reload restore", 1)[1]
        restore = restore.split("// If the user reloads", 1)[0]
        self.assertIn("axonosGuestEntryPendingOrActive", restore)
        self.assertLess(
            restore.index("axonosGuestEntryPendingOrActive"),
            restore.index("fetch(url.toString()"),
        )

    def test_transient_setup_retry_reuses_a_non_secret_attempt_id(self):
        block = self.vnc.split("function axonosRedeemGuestInvite", 1)[1]
        block = block.split("\n        function axonosShowGuestInviteError", 1)[0]
        self.assertIn("attempt_id: attemptId", block)
        self.assertIn("retriesRemaining", block)
        attempt = self.vnc.split("function axonosGuestAttemptId", 1)[1]
        attempt = attempt.split("\n        function ", 1)[0]
        self.assertIn("sessionStorage", attempt)
        self.assertNotIn("invite", _strip_js_comments(attempt).lower())

    def test_transient_setup_offers_an_in_page_retry_with_the_same_attempt(self):
        offer = self.vnc.split("function axonosOfferGuestRetry", 1)[1]
        offer = offer.split("\n        function axonosShowGuestInviteError", 1)[0]
        self.assertIn("Retry Demo", offer)
        self.assertIn("axonosRedeemGuestInvite(token, attemptId)", offer)
        self.assertIn("axonosGuestEntryPending = true", offer)

    def test_demo_identity_is_never_persisted_as_a_returning_wallet(self):
        block = _strip_js_comments(
            self.vnc.split("function axonosActivateGuestSession", 1)[1]
            .split("\n        function ", 1)[0]
        )
        # 'axonos_last_wallet' drives the returning-wallet path; a synthetic
        # identity must never be offered there.
        self.assertNotIn("localStorage", block)
        self.assertNotIn("axonos_last_wallet", block)
        self.assertIn("axonosStoreGuestSession", block)

    def test_stored_demo_state_excludes_the_invite_token(self):
        block = _strip_js_comments(
            self.vnc.split("function axonosStoreGuestSession", 1)[1]
            .split("\n        function ", 1)[0]
        )
        # The one-use invitation is never retained. The distinct, short-lived
        # guest auth bearer is deliberately tab-scoped so another tab cannot
        # overwrite the compatibility cookie and strand this demo.
        self.assertIn("sessionStorage", block)
        self.assertNotIn("localStorage", block)
        self.assertNotIn("invite", block.lower())
        self.assertIn("authToken", block)
        self.assertIn("allowedProfiles", block)
        self.assertIn("allowedTemplates", block)

    def test_stored_demo_revalidates_its_tab_bearer_with_cookie_fallback(self):
        restore = self.vnc.split("function axonosRestoreStoredGuestSession", 1)[1]
        restore = restore.split("\n        /** Entry point", 1)[0]
        self.assertIn("/api/auth/wallet-status", restore)
        self.assertIn("credentials: 'include'", restore)
        self.assertIn("stored.authToken", restore)
        self.assertIn("headers['X-AXGT-Auth-Token'] = storedAuthToken", restore)
        self.assertIn("authToken: data.auth_token", restore)
        self.assertIn("return axonosActivateGuestSession({", restore)
        # Transient control-plane failures retain recoverable state. Only an
        # authoritative authentication denial drops the tab credential.
        self.assertIn("response.status === 401 || response.status === 403", restore)
        init = self.vnc.split("function axonosInitGuestMode", 1)[1]
        init = init.split("\n        window.axonosInitGuestMode", 1)[0]
        self.assertIn("axonosRestoreStoredGuestSession(stored)", init)

    def test_guest_rfb_always_uses_the_tab_scoped_explicit_bearer(self):
        connect = self.ui.split("const wsAuthMode = String", 1)[1]
        connect = connect.split("// Check if wallet is verified", 1)[0]
        self.assertIn("guestExplicitAuth", connect)
        self.assertIn("!!window.axonosGuestSession", connect)
        self.assertIn("const includeQueryAuthToken = guestExplicitAuth", connect)

    def test_guest_pagehide_beacon_carries_only_the_tab_bearer_in_json(self):
        beacon = self.ui.split("_axonosReleaseSessionBeacon() {", 1)[1]
        beacon = beacon.split("_axonosReleaseSessionBestEffort(context)", 1)[0]
        self.assertIn("guestSession.address", beacon)
        self.assertIn("payload.auth_token = window.verifiedWalletAuthToken", beacon)
        self.assertIn("JSON.stringify(payload)", beacon)
        self.assertIn("navigator.sendBeacon(url", beacon)
        self.assertNotIn("if (guestUsesExplicitBearer) return", beacon)
        self.assertLess(
            beacon.index("payload.auth_token = window.verifiedWalletAuthToken"),
            beacon.index("JSON.stringify(payload)"),
        )

    def test_rotated_guest_bearer_updates_the_tab_restore_state(self):
        helper = self.vnc.split("function axonosGuestUpdateAuthToken", 1)[1]
        helper = helper.split("\n        window.axonosGuestUpdateAuthToken", 1)[0]
        self.assertIn("window.axonosGuestSession.authToken = canonical", helper)
        self.assertIn("axonosStoreGuestSession", helper)
        poll = self.ui.split("wallet-status canonicalizes", 1)[1]
        poll = poll.split("const remaining", 1)[0]
        self.assertIn("window.axonosGuestUpdateAuthToken(data.auth_token)", poll)

    def test_countdown_is_fed_from_server_payloads(self):
        self.assertIn("axonosGuestSyncFromPayload", self.vnc)
        # Claim, status and heartbeat must all refresh the deadline.
        self.assertGreaterEqual(self.vnc.count("axonosGuestSyncFromPayload(") , 3)
        self.assertIn("window.axonosGuestSyncFromPayload(hb)", self.ui)

    def test_early_warning_precedes_the_cutoff(self):
        block = self.vnc.split("function axonosTickGuestCountdown", 1)[1]
        block = block.split("\n        function ", 1)[0]
        self.assertIn("axonosShowGuestWarning", block)
        self.assertIn("axonosShowGuestUpsell", block)
        # The warning must be driven by the server's warn window, not a constant.
        self.assertIn("warnSeconds", block)

    def test_upsell_clears_the_demo_identity_before_wallet_connect(self):
        block = self.vnc.split("function axonosShowGuestUpsell", 1)[1]
        block = block.split("\n        window.axonosShowGuestUpsell", 1)[0]
        self.assertIn("if (axonosGuestTeardownPromise) return", block)
        self.assertIn("axonosTeardownGuestSession", block)
        self.assertIn("if (!released)", block)
        self.assertIn("onConnectWalletClick", block)
        self.assertLess(
            block.index("if (!released)"),
            block.index("onConnectWalletClick"),
        )

    def test_banner_markup_and_styles_exist(self):
        self.assertIn('id="axonos_guest_banner"', self.vnc)
        self.assertIn('id="axonos_guest_banner_time"', self.vnc)
        self.assertIn('id="axonos_guest_banner_cta"', self.vnc)
        self.assertIn(".axonos-guest-banner", self.css)
        self.assertIn(".axonos-guest-banner--warning", self.css)

    def test_wizard_offers_a_demo_panel_instead_of_payment(self):
        self.assertIn('id="axonos_wizard_guest_panel"', self.vnc)
        self.assertIn("axonosGuestActive()", self.vnc)

    def test_demo_launch_skips_the_payment_step(self):
        actions = self.vnc.split("newBtn.addEventListener('click'", 1)[1]
        actions = actions.split("// Back button wiring", 1)[0]
        step_two = actions.split("axonosCurrentWizardStep === 2", 1)[1]
        step_two = step_two.split("axonosCurrentWizardStep === 3", 1)[0]
        self.assertIn("axonosTriggerGuestLaunchFromWizard", step_two)
        self.assertIn("axonosGoToWizardStep(3)", step_two)
        self.assertLess(
            step_two.index("axonosGuestActive()"),
            step_two.index("axonosGoToWizardStep(3)"),
        )

    def test_demo_choices_are_constrained_to_the_invite_allowlists(self):
        gpu = self.vnc.split("function axonosApplyWizardGpuPinState", 1)[1]
        gpu = gpu.split("\n        window.axonosApplyWizardGpuPinState", 1)[0]
        self.assertIn("allowedProfiles", gpu)
        self.assertIn("card.disabled = !permitted", gpu)
        render = self.ui.split("renderAxonosTemplates", 1)[1]
        render = render.split("updateAxonosSelectedTemplateBanner", 1)[0]
        self.assertIn("allowedTemplates", render)
        self.assertIn("permittedForGuest", render)

    def test_landing_hardware_picker_obeys_the_guest_allowlist(self):
        restrictions = self.vnc.split(
            "function axonosApplyGuestProfileRestrictions", 1
        )[1].split("\n        window.axonosApplyGuestProfileRestrictions", 1)[0]
        self.assertIn("select.options", restrictions)
        self.assertIn("option.disabled = !permitted", restrictions)
        self.assertIn("option.hidden = !permitted", restrictions)
        requested = self.vnc.split("function getRequestedProfile", 1)[1]
        requested = requested.split("\n        /**", 1)[0]
        self.assertIn("guestAllowed", requested)
        self.assertIn("return guestAllowed[0]", requested)

    def test_a_wallet_sign_in_ends_the_demo_first(self):
        # Otherwise the stale demo flag makes the launch/claim preflight
        # short-circuit for a PAID session.
        verified = self.vnc.split(
            "if (data.verified && data.auth_token) {", 1
        )[1].split("axonosSetTestCreditEligibility", 1)[0]
        self.assertIn("await axonosEndGuestSessionForWalletHandover", verified)
        self.assertIn("if (!releasedGuest)", verified)
        self.assertLess(
            verified.index("await axonosEndGuestSessionForWalletHandover"),
            verified.index("window.verifiedWalletAddress = walletAddress"),
        )

        insufficient = self.vnc.split(
            "A signed-in wallet with no credit is still a successful", 1
        )[1].split("if (verifyOptions.releaseOnly", 1)[0]
        self.assertIn("await axonosEndGuestSessionForWalletHandover", insufficient)
        self.assertIn("if (!releasedGuestWithoutCredit)", insufficient)
        self.assertLess(
            insufficient.index("await axonosEndGuestSessionForWalletHandover"),
            insufficient.index("window.verifiedWalletAddress = walletAddress"),
        )

    def test_guest_handoff_requires_release_and_resets_guest_owned_ui_state(self):
        teardown = self.vnc.split(
            "function axonosTeardownGuestSession", 1
        )[1].split("\n        window.axonosTeardownGuestSession", 1)[0]
        self.assertIn("teardownSessionForWalletChange", teardown)
        self.assertIn("forceRelease: true", teardown)
        self.assertIn("confirmIdleBeforeRelease: true", teardown)
        self.assertIn("released !== true", teardown)
        self.assertIn("axonosClearGuestSession()", teardown)
        self.assertLess(
            teardown.index("released !== true"),
            teardown.index("axonosClearGuestSession()"),
        )

        clear = self.vnc.split("function axonosClearGuestSession", 1)[1]
        clear = clear.split("\n        window.axonosClearGuestSession", 1)[0]
        self.assertIn("window.axonosOwnedSession = null", clear)
        self.assertIn("window.axonosDetachedSession = null", clear)
        self.assertIn("window.axonosPausedResume = null", clear)
        self.assertIn("axonosClearSessionReleaseFailure", clear)
        self.assertIn("sshToggle.disabled = false", clear)
        self.assertIn("wizardSshCard.disabled = false", clear)
        self.assertIn("axonosApplyWizardGpuPinState()", clear)

    def test_guest_exit_with_no_local_owned_flags_confirms_server_idle(self):
        guest_teardown = self.vnc.split(
            "function axonosTeardownGuestSession", 1
        )[1].split("\n        window.axonosTeardownGuestSession", 1)[0]
        self.assertIn("confirmIdleBeforeRelease: true", guest_teardown)

        shared_teardown = self.vnc.split(
            "function teardownSessionForWalletChange(options)", 1
        )[1].split("\n        function axonosFinishWalletSwitchInProgress", 1)[0]
        no_local_state = shared_teardown.split(
            "options && options.confirmIdleBeforeRelease", 1
        )[1].split("// Headless SSH view", 1)[0]
        self.assertIn("!axonosWalletHasProtectedSessionState()", no_local_state)
        self.assertIn("axonosConfirmWalletIdentityIdle(expectedWallet)", no_local_state)
        self.assertIn("if (result.idle) return true", no_local_state)
        self.assertIn("confirmIdleBeforeRelease: false", no_local_state)
        self.assertIn("return teardownSessionForWalletChange(releaseOptions)", no_local_state)

    def test_guest_topbar_exit_uses_the_release_confirmed_teardown(self):
        topbar = self.vnc.split("// Bind topbar Wallet click to disconnect", 1)[1]
        topbar = topbar.split("// Silent reload restore", 1)[0]
        self.assertIn("axonosGuestActive()", topbar)
        self.assertIn("axonosTeardownGuestSession", topbar)
        self.assertLess(
            topbar.index("axonosGuestActive()"),
            topbar.index("axonosDisconnectWalletSession"),
        )

    def test_failed_wallet_sign_in_preserves_the_live_demo_identity(self):
        helper = self.vnc.split(
            "function axonosResetIdentityAfterFailedWalletAttempt", 1
        )[1].split("\n        function ", 1)[0]
        self.assertIn("if (window.axonosGuestSession)", helper)
        self.assertIn("window.axonosGuestSession.address", helper)
        self.assertIn("window.axonosGuestSession.authToken", helper)
        self.assertIn("window.axonosAllowVncConnect = true", helper)

    def test_wallet_connect_and_provider_events_defer_to_pending_guest_entry(self):
        connect = self.vnc.split("function onConnectWalletClick", 1)[1]
        connect = connect.split("\n        window.onConnectWalletClick", 1)[0]
        self.assertIn("axonosGuestEntryPending === true", connect)
        for function_name in (
            "onWalletAccountsChanged",
            "onWalletProviderDisconnected",
            "onWalletProviderConnected",
        ):
            block = self.vnc.split("function " + function_name, 1)[1]
            block = block.split("\n        function ", 1)[0]
            self.assertIn("axonosGuestEntryPendingOrActive", block, function_name)

    def test_clearing_a_demo_never_signs_out_a_connected_wallet(self):
        block = _strip_js_comments(
            self.vnc.split("function axonosClearGuestSession", 1)[1]
            .split("\n        window.axonosClearGuestSession", 1)[0]
        )
        # The wallet globals may only be torn down when the demo is still live.
        self.assertIn("live === demoAddress", block)
        idx_guard = block.index("live === demoAddress")
        idx_clear = block.index("window.verifiedWalletAuthToken = null")
        self.assertLess(idx_guard, idx_clear)

    def test_a_restored_demo_cannot_displace_a_connected_wallet(self):
        block = _strip_js_comments(
            self.vnc.split("function axonosActivateGuestSession", 1)[1]
            .split("\n        /** Client-side mirror", 1)[0]
        )
        self.assertIn("return false", block)
        self.assertIn("axonosIsGuestAddress", block)

    def test_expired_demo_denial_routes_to_the_upsell(self):
        block = self.vnc.split("function handleSessionClaimDenied", 1)[1]
        block = block.split("\n        function ", 1)[0]
        self.assertIn("guest_session_expired", block)
        self.assertIn("axonosShowGuestUpsell", block)


if __name__ == "__main__":
    unittest.main()
