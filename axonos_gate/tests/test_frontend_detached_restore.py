import unittest
from pathlib import Path


class FrontendDetachedRestoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo = Path(__file__).resolve().parents[2]
        cls.page_source = (repo / "novnc-theme" / "vnc.html").read_text(
            encoding="utf-8"
        )
        cls.ui_source = (repo / "novnc-theme" / "ui.js").read_text(
            encoding="utf-8"
        )

    def _between(self, start: str, end: str) -> str:
        self.assertIn(start, self.page_source)
        self.assertIn(end, self.page_source)
        return self.page_source.split(start, 1)[1].split(end, 1)[0]

    def test_owned_active_desktop_restores_detached_heartbeat_state(self) -> None:
        restore = self._between(
            "function axonosRestoreActiveDesktopAsDetached(st)",
            "function axonosApplySessionStatusToResumeUi(st, walletHint)",
        )

        # Paused and SSH sessions have distinct restore paths and must never be
        # reclassified as a detached desktop.
        self.assertIn("st.paused", restore)
        self.assertIn("st.owner_ssh_enabled", restore)
        self.assertIn("window.axonosSessionDetached = true", restore)
        self.assertIn(
            "window.axonosDetachedSession = window.axonosOwnedSession", restore
        )
        self.assertIn("axonosApplyDetachedSessionUi(true)", restore)
        self.assertIn("window.axonosPausedResume = null", restore)

        # Heartbeats resume only after the page has a verified wallet identity.
        self.assertIn("window.verifiedWalletAddress", restore)
        self.assertIn("window.verifiedWalletAuthToken", restore)
        self.assertIn("!UI._axgtStatusPollId", restore)
        self.assertIn("UI._axgtStartSessionBillingPoll()", restore)

    def test_lifecycle_transition_preserves_owner_without_active_restore(self) -> None:
        lifecycle = self._between(
            "function axonosSessionLifecycleInProgress(st)",
            "/** Clear a previous wallet's cached active-session state",
        )
        clear_non_owner = self._between(
            "function axonosClearDetachedStateForNonOwner(st)",
            "function axonosEffectiveOwnedSession()",
        )
        restore = self._between(
            "function axonosRestoreActiveDesktopAsDetached(st)",
            "function axonosApplySessionStatusToResumeUi(st, walletHint)",
        )
        status_apply = self._between(
            "function axonosApplySessionStatusToResumeUi(st, walletHint)",
            "function axonosRefreshPausedResumeStatus(expectedWallet)",
        )
        dashboard_sync = self._between(
            "function axonosSyncDashboardPausedResume(status, wallet)",
            "function axonosScheduleDashboardRefresh(delayMs)",
        )

        self.assertIn("st.lifecycle_in_progress === true", lifecycle)
        self.assertIn("st.owner_lifecycle_state", lifecycle)
        self.assertIn("lifecycleState === 'pausing'", lifecycle)
        self.assertIn("lifecycleState === 'resuming'", lifecycle)

        # A transitional owner is not a non-owner, an active detached desktop, or
        # a confirmed paused-resume target. Both status entry points leave the
        # existing owner state untouched until the backend reports a stable state.
        self.assertIn("axonosSessionLifecycleInProgress(st)", clear_non_owner)
        self.assertIn("axonosSessionLifecycleInProgress(st)", restore)
        self.assertIn("if (axonosSessionLifecycleInProgress(st))", status_apply)
        self.assertLess(
            status_apply.index("if (axonosSessionLifecycleInProgress(st))"),
            status_apply.index("axonosClearDetachedStateForNonOwner(st)"),
        )
        self.assertIn(
            "if (axonosSessionLifecycleInProgress(status))", dashboard_sync
        )
        self.assertLess(
            dashboard_sync.index("if (axonosSessionLifecycleInProgress(status))"),
            dashboard_sync.index("axonosRestoreActiveDesktopAsDetached(status)"),
        )

        claim_denied = self._between(
            "function handleSessionClaimDenied(claim, fallbackReason)",
            "/** Called from ui.js when Launch runs connect",
        )
        self.assertIn("claim.lifecycle_in_progress === true", claim_denied)
        self.assertIn("axonosRefreshPausedResumeStatus()", claim_denied)
        self.assertLess(
            claim_denied.index("claim.lifecycle_in_progress === true"),
            claim_denied.index("axonosClaimReasonNeedsWalletOrResume(claim)"),
        )
        self.assertIn("lifecycleInProgress ? 'normal'", self.ui_source)

    def test_status_and_dashboard_paths_restore_active_without_overriding_pause(self) -> None:
        status_apply = self._between(
            "function axonosApplySessionStatusToResumeUi(st, walletHint)",
            "function axonosRefreshPausedResumeStatus(expectedWallet)",
        )
        dashboard_sync = self._between(
            "function axonosSyncDashboardPausedResume(status, wallet)",
            "function axonosScheduleDashboardRefresh(delayMs)",
        )
        self.assertIn("axonosRestoreActiveDesktopAsDetached(st)", status_apply)
        self.assertIn("status.paused === true", dashboard_sync)
        self.assertIn(
            "axonosApplySessionStatusToResumeUi(status, wallet)", dashboard_sync
        )
        self.assertIn(
            "axonosRestoreActiveDesktopAsDetached(status)", dashboard_sync
        )
        self.assertLess(
            dashboard_sync.index("status.paused === true"),
            dashboard_sync.index("axonosRestoreActiveDesktopAsDetached(status)"),
        )

    def test_dashboard_distinguishes_pause_reason_and_describes_host_process_pause(self) -> None:
        paused_row = self._between(
            "// Paused fields are already scoped to the requested wallet",
            "function axonosSyncDashboardPausedResume(status, wallet)",
        )
        renderer = self._between(
            "dashboardSessions.forEach(function (session)",
            "// Bind events",
        )

        self.assertIn(
            "status.paused_reason || status.pause_reason", paused_row
        )
        self.assertIn("Paused — credits exhausted", renderer)
        self.assertIn("Paused after disconnect", renderer)
        self.assertIn("Container host processes paused · billing stopped", renderer)

    def test_detach_copy_says_jobs_and_billing_survive_tab_close(self) -> None:
        detach = self.ui_source.split("async detach()", 1)[1].split(
            "async restartDesktopSession()", 1
        )[0]

        self.assertIn("desktop and jobs keep running", detach)
        self.assertIn("even if you close this tab", detach)
        self.assertIn("prepaid minutes keep counting", detach)
        self.assertIn("credit exhaustion", detach.lower())
        self.assertIn("container's host processes", detach)
        self.assertIn("persistent or long-running kernels", detach)
        self.assertNotIn("desktop pauses shortly", detach)
        self.assertNotIn("closing the tab then pauses", self.page_source)
        self.assertIn("jobs and billing continue", self.page_source)

    def test_successful_non_owner_status_clears_cross_wallet_session_state(self) -> None:
        clear_non_owner = self._between(
            "function axonosClearDetachedStateForNonOwner(st)",
            "function axonosEffectiveOwnedSession()",
        )
        complete_status = self._between(
            "function axonosSessionStatusSnapshotComplete(st)",
            "function axonosClearDetachedStateForNonOwner(st)",
        )
        status_apply = self._between(
            "function axonosApplySessionStatusToResumeUi(st, walletHint)",
            "function axonosRefreshPausedResumeStatus(expectedWallet)",
        )
        dashboard_sync = self._between(
            "function axonosSyncDashboardPausedResume(status, wallet)",
            "function axonosScheduleDashboardRefresh(delayMs)",
        )
        dashboard_load = self._between(
            "function axonosLoadDashboard()",
            "window.axonosLoadDashboard = axonosLoadDashboard",
        )
        detached_refresh = self._between(
            "window.axonosOnDetachedToHome = function ()",
            "function getRequestedProfile()",
        )

        # Only a well-formed successful status may clear cached ownership; paused
        # ownership and active ownership remain on their dedicated paths.
        self.assertIn("typeof st.active === 'boolean'", complete_status)
        self.assertIn(
            "typeof st.active_sessions_count === 'number'", complete_status
        )
        self.assertIn("axonosSessionStatusSnapshotComplete(st)", clear_non_owner)
        self.assertIn("st.is_owner === true", clear_non_owner)
        self.assertIn("st.paused === true", clear_non_owner)
        self.assertIn("var hadOwnedSessionState", clear_non_owner)
        self.assertIn("window.axonosSessionDetached = false", clear_non_owner)
        self.assertIn("window.axonosPausedResume = null", clear_non_owner)
        self.assertIn("window.axonosClearDetachedSession()", clear_non_owner)
        self.assertIn("UI.hideAxonosSshCard()", clear_non_owner)
        self.assertIn("UI.resetAxonosSshLaunchIntent()", clear_non_owner)
        self.assertIn("if (hadOwnedSessionState", clear_non_owner)
        self.assertIn("UI._axgtStatusPollId = null", clear_non_owner)
        self.assertIn("axonosClearDetachedStateForNonOwner(st)", status_apply)
        self.assertIn("!axonosSessionStatusSnapshotComplete(st)", status_apply)
        self.assertIn("var currentWallet", status_apply)
        self.assertIn("wallet !== currentWallet", status_apply)
        self.assertLess(
            status_apply.index("wallet !== currentWallet"),
            status_apply.index("axonosClearDetachedStateForNonOwner(st)"),
        )
        self.assertIn("axonosClearDetachedStateForNonOwner(status)", dashboard_sync)
        self.assertIn(
            "!axonosSessionStatusSnapshotComplete(status)", dashboard_sync
        )
        self.assertLess(
            dashboard_sync.index("status.paused === true"),
            dashboard_sync.index("axonosClearDetachedStateForNonOwner(status)"),
        )
        self.assertIn(
            "!axonosSessionStatusSnapshotComplete(status)", dashboard_load
        )
        self.assertLess(
            dashboard_load.index("!axonosSessionStatusSnapshotComplete(status)"),
            dashboard_load.index("axonosSyncDashboardPausedResume(status || {}, wallet)"),
        )
        self.assertIn("walletStillCurrent", detached_refresh)
        self.assertIn(
            "!axonosSessionStatusSnapshotComplete(st)", detached_refresh
        )
        self.assertIn(
            "axonosApplySessionStatusToResumeUi(st, wallet)", detached_refresh
        )


if __name__ == "__main__":
    unittest.main()
