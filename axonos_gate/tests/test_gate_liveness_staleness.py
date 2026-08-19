"""Session liveness must be measured in gate-observed time, not wall-clock.

A control-plane redeploy makes every live session look stale at once: the
in-container heartbeat daemons keep sending on their 30s timer, but the gate
they post to is gone. Charging that gap to the sessions ended them mid-run and
destroyed the user's container. These tests pin the corrected accounting.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

_tests_dir = os.path.dirname(os.path.abspath(__file__))
_pkg_dir = os.path.dirname(_tests_dir)
_repo_root = os.path.dirname(_pkg_dir)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import session_manager  # noqa: E402


def _old_gate(now):
    """Treat the gate process as long-running so the uptime clamp is inert."""
    return patch.object(session_manager, "_GATE_PROCESS_START", now - 100_000.0)


class TestGateAbsentSeconds(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AXGT_HEARTBEAT_TIMEOUT_SECONDS": "120",
            "AXGT_GATE_LIVENESS_INTERVAL_SECONDS": "15",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def _cur(self, last_seen):
        """Cursor stub; callers pin _GATE_PROCESS_START via _old_gate."""
        cur = MagicMock()
        cur.fetchone.return_value = None if last_seen is None else (last_seen,)
        return cur

    def test_gate_present_reports_no_absence(self):
        now = 1_000_000.0
        # Stamped 5s ago: well inside one interval of jitter slack.
        with _old_gate(now):
            self.assertEqual(
                session_manager._gate_absent_seconds(self._cur(now - 5.0), now), 0.0
            )

    def test_gate_absent_excludes_downtime_beyond_slack(self):
        now = 1_000_000.0
        # Gate last stamped 300s ago; 15s interval absorbed as jitter slack.
        with _old_gate(now):
            self.assertAlmostEqual(
                session_manager._gate_absent_seconds(self._cur(now - 300.0), now), 285.0
            )

    def test_missing_row_fails_toward_keeping_sessions_alive(self):
        now = 1_000_000.0
        # A fresh gate with no record credits only its own (here: long) uptime.
        with _old_gate(now):
            self.assertEqual(
                session_manager._gate_absent_seconds(self._cur(None), now), 100_000.0
            )

    def test_unreadable_row_fails_toward_keeping_sessions_alive(self):
        cur = MagicMock()
        cur.execute.side_effect = RuntimeError("relation does not exist")
        now = 1_000_000.0
        with _old_gate(now):
            self.assertEqual(
                session_manager._gate_absent_seconds(cur, now), 100_000.0
            )


class TestUptimeClamp(unittest.TestCase):
    """Credited downtime can never exceed the gate's own uptime."""

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AXGT_HEARTBEAT_TIMEOUT_SECONDS": "120",
            "AXGT_GATE_LIVENESS_INTERVAL_SECONDS": "15",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_fresh_process_cannot_credit_more_than_it_has_been_up(self):
        now = 1_000_000.0
        with patch.object(session_manager, "_GATE_PROCESS_START", now - 30.0):
            self.assertEqual(session_manager._bound_absence(float("inf"), now), 30.0)

    def test_long_running_process_passes_absence_through(self):
        now = 1_000_000.0
        with patch.object(session_manager, "_GATE_PROCESS_START", now - 100_000.0):
            self.assertEqual(session_manager._bound_absence(285.0, now), 285.0)


class TestPrimingAcrossGateProcesses(unittest.TestCase):
    """Both API servers import session_manager as separate processes.

    The in-container heartbeat daemon posts to websockify_gate (:6080), so
    THAT process runs the stale sweep. It never calls prime_gate_liveness(),
    so absence resolution must self-prime — otherwise it reaps on raw
    wall-clock and ends every live session after a redeploy (session 369).
    """

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AXGT_HEARTBEAT_TIMEOUT_SECONDS": "120",
            "AXGT_GATE_LIVENESS_INTERVAL_SECONDS": "15",
        })
        self.env.start()
        self._saved = (
            session_manager._gate_startup_absence,
            session_manager._gate_last_stamp,
        )
        session_manager._gate_startup_absence = None
        session_manager._gate_last_stamp = None

    def tearDown(self):
        (session_manager._gate_startup_absence,
         session_manager._gate_last_stamp) = self._saved
        self.env.stop()

    def test_unprimed_process_credits_nothing_and_opens_no_connection(self):
        """The sweep must never open its own connection (claim paths mock it)."""
        now = 1_000_000.0
        with patch.object(session_manager, "_GATE_PROCESS_START", now - 10.0), \
             patch.object(session_manager, "_get_connection") as conn:
            self.assertEqual(session_manager._resolve_gate_absent(now), 0.0)
        conn.assert_not_called()

    def test_primed_process_credits_the_measured_downtime_unclamped(self):
        now = 1_000_000.0
        # Young process (inside the heartbeat window) that HAS been primed:
        # the MEASURED gap applies in full. Clamping it by uptime (seconds,
        # right after restart) is what reduced a 145s credit to ~0 and reaped
        # session 370 despite the outage being correctly measured.
        with patch.object(session_manager, "_GATE_PROCESS_START", now - 20.0), \
             patch.object(session_manager, "_gate_startup_absence", 145.0):
            self.assertEqual(session_manager._resolve_gate_absent(now), 145.0)

    def test_recent_stamp_does_not_zero_the_startup_credit(self):
        """The heartbeat handler stamps presence immediately before sweeping.

        "This process stamped seconds ago" is therefore ALWAYS true at resolve
        time; treating it as "no downtime to credit" silently discarded the
        measured gap (session 370). Presence now != presence during the gap.
        """
        now = 1_000_000.0
        with patch.object(session_manager, "_GATE_PROCESS_START", now - 20.0), \
             patch.object(session_manager, "_gate_startup_absence", 145.0), \
             patch.object(session_manager, "_gate_last_stamp", now - 1.0):
            self.assertEqual(session_manager._resolve_gate_absent(now), 145.0)

    def test_credit_expires_once_every_daemon_had_time_to_resume(self):
        """One heartbeat window after restart, dead sessions must be reapable."""
        now = 1_000_000.0
        with patch.object(session_manager, "_GATE_PROCESS_START", now - 121.0), \
             patch.object(session_manager, "_gate_startup_absence", 145.0):
            self.assertEqual(session_manager._resolve_gate_absent(now), 0.0)

    def test_both_api_servers_prime_at_startup(self):
        """Regression: only gate_server primed, so websockify reaped session 369.

        The heartbeat daemons post to websockify_gate, so THAT process runs the
        sweep. Both servers must call prime_gate_liveness() at startup.
        """
        import pathlib
        pkg = pathlib.Path(session_manager.__file__).parent
        for server in ("gate_server.py", "websockify_gate.py"):
            src = (pkg / server).read_text()
            with self.subTest(server=server):
                self.assertIn("prime_gate_liveness", src)


class TestPrimingIsAtomicAcrossProcesses(unittest.TestCase):
    """Both API servers start together and both stamp presence.

    A read-then-stamp primer loses that race: whichever process stamps first
    erases the predecessor's gap before the other measures it, so both credit
    0 and every live session is reaped (session 370). Priming must claim the
    row and return the PRE-UPDATE value in one statement.
    """

    def test_primer_reads_old_value_in_the_same_statement_it_stamps(self):
        import pathlib
        src = pathlib.Path(session_manager.__file__).read_text()
        start = src.index("def prime_gate_liveness")
        body = src[start:start + 2500]
        # One statement: upsert + RETURNING a pre-update snapshot.
        self.assertIn("ON CONFLICT (id) DO UPDATE", body)
        self.assertIn("RETURNING", body)
        self.assertIn("SELECT last_seen FROM", body)
        # The measurement must be stored unclamped: priming runs seconds after
        # process start, so an uptime clamp floors every real gap to ~0.
        self.assertNotIn("_bound_absence", body)
        # A separate read before stamping is what loses the race.
        self.assertNotIn("_read_gate_absent_seconds", body)


class TestStartupAbsenceIsShared(unittest.TestCase):
    """Only one process wins the atomic claim; both must credit the gap.

    The sweep runs in websockify_gate (the heartbeat daemons post there). If
    gate_server wins the claim and websockify inherits nothing, the sweeping
    process credits 0 and reaps every live session anyway.
    """

    def test_loser_of_the_claim_inherits_the_measurement(self):
        import pathlib
        src = pathlib.Path(session_manager.__file__).read_text()
        self.assertIn("_publish_or_inherit_startup_absence", src)
        start = src.index("def _publish_or_inherit_startup_absence")
        body = src[start:start + 2200]
        # Winner writes, loser reads back.
        self.assertIn("ON CONFLICT (id) DO UPDATE", body)
        self.assertIn("SELECT absence_seconds", body)
        # A stale measurement from an older restart must not be inherited.
        self.assertIn("_heartbeat_timeout_seconds()", body)


class TestExpireStaleSessionAccounting(unittest.TestCase):
    """The SQL the sweep issues, under each control-plane condition."""

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AXGT_HEARTBEAT_TIMEOUT_SECONDS": "120",
            "AXGT_SESSION_GRACE_SECONDS": "60",
            "AXGT_GATE_LIVENESS_INTERVAL_SECONDS": "15",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def _run(self, absent):
        cur = MagicMock()
        cur.fetchall.return_value = []
        with patch.object(session_manager, "_gate_absent_seconds", return_value=absent):
            session_manager._expire_stale_session(cur, 1_000_000.0)
        sql, params = cur.execute.call_args[0]
        return " ".join(sql.split()), params

    def test_healthy_gate_still_reaps_on_heartbeat_silence(self):
        """Normal operation keeps the full 120s detector sensitivity."""
        sql, params = self._run(0.0)
        self.assertIn("last_heartbeat < %s", sql)
        # cutoff == now - timeout, with no downtime credit
        self.assertEqual(params[0], 1_000_000.0 - 120)

    def test_downtime_is_credited_to_the_cutoff(self):
        sql, params = self._run(285.0)
        self.assertIn("last_heartbeat < %s", sql)
        self.assertEqual(params[0], 1_000_000.0 - 120 - 285.0)

    def test_unobserved_span_suppresses_the_liveness_branch(self):
        """With no presence record, heartbeat silence proves nothing."""
        sql, _ = self._run(float("inf"))
        self.assertNotIn("last_heartbeat", sql)

    def test_expiry_branches_survive_gate_downtime(self):
        """A session that genuinely runs out mid-redeploy must still end."""
        sql, _ = self._run(float("inf"))
        self.assertIn("expires_at <= %s", sql)
        self.assertIn("hard_expires_at", sql)


if __name__ == "__main__":
    unittest.main()
