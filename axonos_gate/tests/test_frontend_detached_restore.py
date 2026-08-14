import unittest
from pathlib import Path


class FrontendSessionSemanticsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo = Path(__file__).resolve().parents[2]
        cls.page_source = (repo / "novnc-theme" / "vnc.html").read_text(
            encoding="utf-8"
        )
        cls.ui_source = (repo / "novnc-theme" / "ui.js").read_text(
            encoding="utf-8"
        )
        cls.proxy_source = (repo / "axonos_gate" / "websockify_gate.py").read_text(
            encoding="utf-8"
        )
        cls.public_telemetry_source = (repo / "novnc-theme" / "telemetry.html").read_text(
            encoding="utf-8"
        )

    def _page_between(self, start: str, end: str) -> str:
        self.assertIn(start, self.page_source)
        self.assertIn(end, self.page_source)
        return self.page_source.split(start, 1)[1].split(end, 1)[0]

    def _ui_between(self, start: str, end: str) -> str:
        self.assertIn(start, self.ui_source)
        self.assertIn(end, self.ui_source)
        return self.ui_source.split(start, 1)[1].split(end, 1)[0]

    def test_detach_keeps_jobs_and_billing_alive_across_tab_close(self) -> None:
        detach = self.ui_source.split("async detach()", 1)[1].split(
            "async restartDesktopSession()", 1
        )[0]

        self.assertIn("desktop and jobs keep running", detach)
        self.assertIn("prepaid minutes keep counting", detach)
        self.assertIn("even if you close this tab", detach)
        self.assertNotIn("desktop pauses shortly", detach)
        self.assertNotIn("closing the tab then pauses", self.page_source)
        self.assertIn("jobs and compute billing continue, even if this tab closes", self.page_source)
        self.assertIn("Closing an attached tab requests session end", self.page_source)

    def test_credit_exhaustion_copy_describes_logical_top_up_grace(self) -> None:
        self.assertIn("Credit exhausted · 2h top-up grace", self.ui_source)
        self.assertIn("Jobs are still running; compute billing and viewer access have stopped", self.ui_source)
        self.assertIn("Credit exhausted · 2h top-up grace", self.page_source)
        self.assertIn("Jobs still running · compute billing and viewer access stopped", self.page_source)
        self.assertNotIn("Session paused — usage credit exhausted", self.ui_source)
        self.assertNotIn("Compute billing paused", self.page_source)
        self.assertNotIn("Docker-paused", self.page_source + self.ui_source)
        self.assertNotIn("freezes the container", self.page_source + self.ui_source)

    def test_credit_grace_api_is_preferred_with_paused_alias_fallback(self) -> None:
        helper = self._page_between(
            "function axonosCreditGraceActive(data)",
            "function axonosSessionProfileContextFromPayload(payload)",
        )
        apply_payload = self._page_between(
            "window.axonosApplyCreditGraceResumeFromPayload = function (payload)",
            "function axonosSetLaunchConnectButtonCopy(mode)",
        )
        status_apply = self._page_between(
            "function axonosApplySessionStatusToResumeUi(st, walletHint)",
            "function axonosRefreshPausedResumeStatus(expectedWallet)",
        )
        dashboard = self._page_between(
            "function axonosOwnedDashboardSessions(status, wallet)",
            "function axonosScheduleDashboardRefresh(delayMs)",
        )

        self.assertIn("data.credit_grace === true", helper)
        self.assertIn("data.paused === true", helper)
        for preferred, compatibility in (
            ("credit_grace_requested_profile", "paused_requested_profile"),
            ("credit_grace_assigned_gpu_ids", "paused_assigned_gpu_ids"),
            ("credit_grace_gpu_count", "paused_gpu_count"),
            ("credit_grace_remaining_seconds", "paused_resume_seconds"),
            ("credit_grace_session_id", "paused_session_id"),
        ):
            combined = apply_payload + status_apply + dashboard
            self.assertIn(preferred, combined)
            self.assertIn(compatibility, combined)
            self.assertLess(combined.index(preferred), combined.index(compatibility))

        self.assertIn("credit_grace_ssh_enabled", dashboard)
        self.assertIn("dashboard_state: 'credit_grace'", dashboard)
        self.assertIn(
            "window.axonosApplyPausedResumeFromPayload = "
            "window.axonosApplyCreditGraceResumeFromPayload",
            apply_payload,
        )

    def test_eligible_balance_card_refill_is_one_click_and_never_starts_a_new_session(self) -> None:
        eligibility = self._page_between(
            "function axonosSyncTestCreditControls()",
            "function axonosPaymentIdentityIsCurrent(wallet)",
        )
        refill = self._page_between(
            "function axonosRequestBalanceCardTestCredit()",
            "const dashTopupBtn = document.getElementById('axonos_dashboard_topup_btn')",
        )
        balance_actions = self._page_between(
            "const dashTopupBtn = document.getElementById('axonos_dashboard_topup_btn')",
            "// Live Telemetry Loop when Connected",
        )
        success = self._page_between(
            "function axonosDepositVerifiedSuccess(d, wallet, operation, options)",
            "var AXONOS_VERIFY_DEPOSIT_ENDPOINT",
        )

        self.assertIn("Add up to ", eligibility)
        self.assertIn("axonos_dashboard_topup_btn", eligibility)
        self.assertIn("axonos_sidebar_topup_btn", eligibility)
        self.assertIn("test_credit_grant_minutes", self.page_source)
        self.assertIn("test_credit_max_balance_minutes", self.page_source)
        self.assertIn("axonosRequestTestCredit('eth', null", refill)
        self.assertIn("resumeCreditGraceOnly: true", refill)
        self.assertGreaterEqual(balance_actions.count("if (window.axonosTestCreditEligible)"), 2)
        self.assertGreaterEqual(balance_actions.count("axonosRequestBalanceCardTestCredit()"), 2)
        self.assertIn("axonosStartWizard()", balance_actions)
        self.assertIn("window.axonosOpenWalletTopUpDialog(true)", balance_actions)
        self.assertIn("if (successOptions.resumeCreditGraceOnly)", success)
        self.assertIn("passiveWalletPreflight: successOptions.passiveWalletPreflight", success)
        self.assertIn("passiveWalletPreflight: true", self.page_source)
        self.assertIn("resumeDiscoveredAfterCredit", success)
        self.assertIn("Number(resumeHintSource.sessionId)", success)
        self.assertIn("shouldResumeRetainedSession", success)
        self.assertLess(
            success.index("if (successOptions.resumeCreditGraceOnly)"),
            success.index("claimSession().then"),
        )

    def test_desktop_top_up_never_reuses_or_waits_forever_on_verifying_state(self) -> None:
        top_up = self._page_between(
            "window.axonosOpenWalletTopUpDialog = function (forcePayment)",
            "function setWalletUIState(state, data)",
        )

        self.assertIn("showPaymentState('Refreshing credit balance…')", top_up)
        self.assertIn("axonosFetchWalletAccessStatus(wallet)", top_up)
        self.assertLess(
            top_up.index("showPaymentState('Refreshing credit balance…')"),
            top_up.index("axonosFetchWalletAccessStatus(wallet)"),
        )
        self.assertIn("res.unavailable || res.ok === false", top_up)
        self.assertGreaterEqual(top_up.count("Credit status unavailable"), 2)
        self.assertNotIn("fetch(url.toString()", top_up)

    def test_resume_claim_is_bound_to_the_retained_session(self) -> None:
        page_claim = self._page_between(
            "function claimSession(options)",
            "function sessionStatus()",
        )
        ui_claim = self.ui_source.split("_axonosFetchSessionClaim(options)", 1)[1].split(
            "_axonosReleaseSessionHeaders()", 1
        )[0]
        for source in (page_claim, ui_claim):
            self.assertIn("payload.resume_only = true", source)
            self.assertIn("payload.expected_session_id", source)
            self.assertIn("invalid_resume_request: true", source)
        resume_flow = self._page_between(
            "function axonosTryResumeDesktopAfterCredit(paymentOperation, expectedSessionIdOverride, options)",
            "window.axonosResumeDesktopConnectIfPaused = function (options)",
        )
        self.assertIn("claim.resume_expired === true", resume_flow)
        self.assertIn("Your new credits remain available; launch a new session", resume_flow)
        self.assertIn("window.axonosPendingResumeClaim", resume_flow)
        self.assertIn("expectedSessionId: expectedResumeSessionId", resume_flow)
        self.assertIn("preclaimedResumeAtConnectStart", self.ui_source)
        self.assertIn("Promise.resolve(preclaimedResumeAtConnectStart.claim)", self.ui_source)

    def test_resume_marker_survives_non_authoritative_status_and_wallet_failures(self) -> None:
        status = self._page_between(
            "function sessionStatusForWallet(wallet)",
            "function axonosSyncTestCreditControls()",
        )
        refresh = self._page_between(
            "function axonosRefreshPausedResumeStatus(expectedWallet)",
            "function axonosProbePausedResumeOnLoad()",
        )
        preflight = self._page_between(
            "window.axonosEnsureWalletSessionCurrent = function (opts)",
            "// HUD wallet controls",
        )

        self.assertIn("_axonosHttpOk", status)
        self.assertIn("axonosSessionStatusIsAuthoritative", refresh)
        self.assertLess(
            refresh.index("axonosSessionStatusIsAuthoritative"),
            refresh.index("axonosApplySessionStatusToResumeUi"),
        )
        self.assertIn("axonosWalletProviderRequest", preflight)
        self.assertIn("account_mismatch", preflight)
        self.assertIn("onWalletAccountsChanged(accounts, eth)", preflight)
        self.assertIn("axonosMarkWalletProviderOutOfSync", preflight)
        self.assertNotIn("teardownSessionForWalletChange", preflight)
        self.assertNotIn("clearWalletIdentityAndUi", preflight)

    def test_wallet_identity_cleanup_clears_all_browser_owned_state(self) -> None:
        cleanup = self._page_between(
            "function clearWalletIdentityAndUi()",
            "function axonosDesktopSessionLive()",
        )

        for state_reset in (
            "window.verifiedWalletAddress = null",
            "window.verifiedWalletAuthToken = null",
            "window.axonosOwnedSession = null",
            "window.axonosDetachedSession = null",
            "window.axonosSessionDetached = false",
            "window.axonosPendingResumeClaim = null",
            "window.axonosPausedResume = null",
            "window.axonosSshEnabled = false",
            "window.axonosCurrentSessionReleaseOperation = null",
            "window.axonosSessionReleaseFailure = null",
        ):
            self.assertIn(state_reset, cleanup)
        self.assertIn("localStorage.removeItem('axonos_last_wallet')", cleanup)
        self.assertIn("clearInterval(UI._axgtStatusPollId)", cleanup)
        self.assertIn("UI._axgtStatusPollId = null", cleanup)
        self.assertIn("UI._axgtStopSessionTimer()", cleanup)
        self.assertIn("UI.hideAxonosSshCard()", cleanup)

    def test_environment_catalog_is_browsable_without_wallet_authentication(self) -> None:
        init = self._ui_between(
            "initAxonosTemplates()",
            "renderLandingFeaturedTemplates()",
        )
        catalog = self._ui_between(
            "openAxonosCatalogModal()",
            "renderLandingFeaturedTemplates()",
        )

        self.assertIn("viewAllBtn.addEventListener('click', UI.openAxonosCatalogModal)", init)
        self.assertNotIn("onConnectWalletClick", init)
        self.assertNotIn("verifiedWalletAddress", init)
        self.assertIn("AXONOS_TEMPLATES.forEach", catalog)
        self.assertIn("window.axonosOpenCatalogModal", self.ui_source)
        self.assertIn('id="axonos_hero_browse_btn"', self.page_source)
        self.assertIn('>Read the docs</a>', self.page_source)

    def test_storage_context_is_generation_and_wallet_scoped(self) -> None:
        storage_helpers = self._page_between(
            "var axonosWizardStorageUserEdited = false;",
            "function axonosStartWizard()",
        )
        apply_context = self._page_between(
            "function axonosApplyWizardStorageContext(wallet, data, generation)",
            "function axonosShowStorageGrowthOnlyWarning()",
        )
        wizard_start = self._page_between(
            "function axonosStartWizard()",
            "window.axonosStartWizard = axonosStartWizard;",
        )
        step_three = self._page_between(
            "// Wallet view state checks in Step 3",
            "window.axonosGoToWizardStep = axonosGoToWizardStep;",
        )
        launch = self._page_between(
            "function axonosTriggerLaunchFromWizard()",
            "// Stepper click binding for unconnected wallet connect",
        )

        self.assertIn("var axonosWizardStorageGeneration = 0", storage_helpers)
        self.assertIn("var axonosWizardStorageFloorWallet = ''", storage_helpers)
        self.assertIn("generation !== axonosWizardStorageGeneration", apply_context)
        self.assertIn("walletKey !== axonosWizardStorageFloorWallet", apply_context)
        self.assertIn("!axonosPaymentIdentityIsCurrent(wallet)", apply_context)

        self.assertIn(
            "const storageGeneration = ++axonosWizardStorageGeneration",
            wizard_start,
        )
        for flow in (wizard_start, step_three, launch):
            self.assertIn("res.stale", flow)
            self.assertIn("!axonosPaymentIdentityIsCurrent(", flow)
            self.assertIn("axonosApplyWizardStorageContext(", flow)
            self.assertIn("storageGeneration", flow)
            self.assertLess(
                flow.index("res.stale"),
                flow.index("axonosApplyWizardStorageContext("),
            )

    def test_storage_floor_is_authoritative_and_preserves_larger_choice(self) -> None:
        storage_helpers = self._page_between(
            "var axonosWizardStorageUserEdited = false;",
            "function axonosStartWizard()",
        )
        apply_context = self._page_between(
            "function axonosApplyWizardStorageContext(wallet, data, generation)",
            "function axonosShowStorageGrowthOnlyWarning()",
        )
        wizard_start = self._page_between(
            "function axonosStartWizard()",
            "window.axonosStartWizard = axonosStartWizard;",
        )
        step_three = self._page_between(
            "// Wallet view state checks in Step 3",
            "window.axonosGoToWizardStep = axonosGoToWizardStep;",
        )

        self.assertIn("function axonosReadStoragePreference(wallet)", storage_helpers)
        self.assertIn("function axonosWriteStoragePreference(wallet, value)", storage_helpers)
        self.assertIn(
            "storageGb = Math.max(storageGb, axonosWizardStorageFloorGb)",
            storage_helpers,
        )
        self.assertIn("data.minimum_storage_gb != null", apply_context)
        self.assertIn("data.provisioned_storage_gb", apply_context)
        self.assertLess(
            apply_context.index("data.minimum_storage_gb"),
            apply_context.index("data.provisioned_storage_gb"),
        )
        self.assertIn(
            "axonosWizardStorageFloorGb = Math.max(",
            apply_context,
        )
        self.assertLess(
            apply_context.index("var selectedGb = slider"),
            apply_context.index("axonosUpdateStorageFloorUi()"),
        )
        self.assertIn("axonosWizardStorageFloorResolved = true", apply_context)
        self.assertIn("selectedGb < axonosWizardStorageFloorGb", apply_context)
        self.assertIn(
            "axonosApplyStorageSelection(effectiveSelectionGb)",
            apply_context,
        )
        self.assertIn(
            "provisionedStorageGb: axonosWizardStorageFloorGb",
            apply_context,
        )

        self.assertIn("axonosWizardStorageUserEdited = false", wizard_start)
        self.assertIn("axonosRestoreStoragePreference(", wizard_start)
        self.assertIn("axonosWizardStorageUserEdited = true", wizard_start)
        self.assertIn("requestedGb < axonosWizardStorageFloorGb", wizard_start)
        self.assertIn("axonosShowStorageGrowthOnlyWarning()", wizard_start)
        self.assertIn("axonosWriteStoragePreference(w, slider.value)", wizard_start)
        self.assertIn("serverStorageGb = axonosNormalizeStorageGb", apply_context)
        self.assertIn("!axonosWizardStorageUserEdited", apply_context)
        self.assertIn("savedStorageGb === null", apply_context)
        self.assertIn("axonosApplyWizardStorageContext(", step_three)
        self.assertNotIn("slider.value = data.requested_storage_gb", step_three)

    def test_growth_only_floor_updates_slider_label_warning_and_copy(self) -> None:
        floor_ui = self._page_between(
            "function axonosUpdateStorageFloorUi()",
            "function axonosResetStorageFloor(wallet)",
        )
        warning = self._page_between(
            "function axonosShowStorageGrowthOnlyWarning()",
            "function axonosRequestedStorageGbForClaim(wallet)",
        )

        self.assertIn('id="axonos_wizard_storage_slider"', self.page_source)
        self.assertIn('id="axonos_wizard_storage_min"', self.page_source)
        self.assertIn('id="axonos_wizard_storage_desc"', self.page_source)
        self.assertIn('role="status" aria-live="polite"', self.page_source)
        normalized_page = " ".join(self.page_source.split())
        self.assertIn("Volumes can grow but cannot be reduced", normalized_page)
        self.assertIn("permanently raises the minimum capacity", normalized_page)
        self.assertIn("unused selected capacity is not billed", normalized_page)

        self.assertIn("slider.min = String(axonosWizardStorageFloorGb)", floor_ui)
        self.assertIn(
            "minLabel.textContent = axonosWizardStorageFloorGb + ' GB'",
            floor_ui,
        )
        self.assertIn("axonosUpdateStorageSelectionCopy", floor_ui)
        selection_copy = self._page_between(
            "function axonosUpdateStorageSelectionCopy(storageGb)",
            "function axonosUpdateStorageFloorUi()",
        )
        self.assertIn("storageGb > axonosWizardStorageFloorGb", selection_copy)
        self.assertIn("!axonosWizardStorageFloorResolved", selection_copy)
        self.assertIn("Checking this wallet\\'s existing persistent-volume capacity", selection_copy)
        self.assertIn("permanently raises this wallet\\'s minimum capacity", selection_copy)
        self.assertIn("This increase cannot be undone", selection_copy)
        self.assertIn("data actually stored", selection_copy)
        self.assertIn("unused selected capacity is not billed", selection_copy)
        self.assertIn("Your existing volume is ", warning)
        self.assertIn("GB and cannot be reduced", warning)
        self.assertIn("Review the updated storage setting, then launch again", warning)
        self.assertIn("'warn'", warning)

    def test_both_claim_paths_use_floor_checked_storage_selection(self) -> None:
        page_claim = self._page_between(
            "function claimSession(options)",
            "function sessionStatus()",
        )
        ui_claim = self.ui_source.split("_axonosFetchSessionClaim(options)", 1)[1].split(
            "_axonosReleaseSessionHeaders()", 1
        )[0]

        expected = "window.axonosRequestedStorageGbForClaim(wallet)"
        for claim in (page_claim, ui_claim):
            self.assertIn("payload.requested_storage_gb", claim)
            self.assertIn(expected, claim)
            self.assertNotIn("axonos_wizard_storage_slider", claim)

    def test_launch_preflight_aborts_when_authoritative_floor_adjusts(self) -> None:
        launch = self._page_between(
            "function axonosTriggerLaunchFromWizard()",
            "// Stepper click binding for unconnected wallet connect",
        )

        self.assertIn("const storageGeneration = axonosWizardStorageGeneration", launch)
        self.assertIn("axonosFetchWalletAccessStatus(wallet)", launch)
        self.assertIn("axonosApplyWizardStorageContext(", launch)
        self.assertLess(
            launch.index("axonosApplyWizardStorageContext("),
            launch.index("claimSession().then"),
        )

        not_applied = launch.split("if (!storageContext.applied)", 1)[1].split(
            "if (storageContext.adjusted)", 1
        )[0]
        self.assertIn("hideConnectionLoader();", not_applied)
        self.assertIn("return;", not_applied)

        adjusted = launch.split("if (storageContext.adjusted)", 1)[1].split(
            "const remaining", 1
        )[0]
        self.assertIn("hideConnectionLoader();", adjusted)
        self.assertIn("axonosGoToWizardStep(2);", adjusted)
        self.assertIn("axonosShowStorageGrowthOnlyWarning();", adjusted)
        self.assertIn("return;", adjusted)

    def test_structured_storage_rejection_returns_to_hardware_step(self) -> None:
        denied = self._page_between(
            "function handleSessionClaimDenied(claim, fallbackReason)",
            "/** Called from ui.js when Launch runs connect while session is not claimed. */",
        )
        storage_rejection = denied.split(
            "if (claim && claim.error_code === 'storage_below_provisioned')", 1
        )[1].split("if (axonosClaimReasonNeedsWalletOrResume(claim))", 1)[0]

        self.assertIn("axonosApplyWizardStorageContext(", storage_rejection)
        self.assertIn("claim,", storage_rejection)
        self.assertIn("axonosWizardStorageGeneration", storage_rejection)
        self.assertIn("if (storageContext.applied)", storage_rejection)
        self.assertIn("axonosUpdateActiveScreen('wizard');", storage_rejection)
        self.assertIn("axonosGoToWizardStep(2);", storage_rejection)
        self.assertIn("axonosShowStorageGrowthOnlyWarning();", storage_rejection)
        self.assertIn("return;", storage_rejection)

    def test_storage_telemetry_uses_allocated_blocks_not_total_minus_free(self) -> None:
        renderer = self._page_between(
            "function axonosRenderStorageUsage(usedBytes, totalBytes)",
            "function axonosTickTelemetry(generation)",
        )
        telemetry = self._page_between(
            "function axonosTickTelemetry(generation)",
            "// Listen for VNC connection events to manage the loop.",
        )

        self.assertIn(
            "axonosRenderStorageUsage(st.disk_used_bytes, st.disk_total_bytes)",
            telemetry,
        )
        self.assertNotIn("disk_free_bytes", telemetry)
        self.assertIn("Math.min(totalBytes, usedBytes)", renderer)
        self.assertNotIn("totalBytes -", renderer)
        self.assertNotIn("freeBytes", renderer)

    def test_live_telemetry_rejects_stale_and_overlapping_samples(self) -> None:
        telemetry = self._page_between(
            "function axonosStartTelemetryLoop()",
            "// Listen for VNC connection events to manage the loop.",
        )

        self.assertIn("axonosTelemetryGeneration += 1", telemetry)
        self.assertIn("if (!wallet || axonosGpuTelemetryPending) return", telemetry)
        self.assertIn("if (!axonosContainerTelemetryPending)", telemetry)
        self.assertIn("axonosTickGpuTelemetry(axonosTelemetryGeneration)", telemetry)
        self.assertIn("AXONOS_GPU_TELEMETRY_INTERVAL_MS", telemetry)
        self.assertIn("AXONOS_CONTAINER_TELEMETRY_INTERVAL_MS", telemetry)
        self.assertIn("timeoutMs: 4000", telemetry)
        self.assertIn("_telemetry_ts", telemetry)
        self.assertIn("gpuUrl.searchParams.set('viewer', '1')", telemetry)
        self.assertIn("'Cache-Control': 'no-cache'", telemetry)
        self.assertIn("cacheAge > AXONOS_GPU_TELEMETRY_MAX_AGE_SECONDS", telemetry)
        self.assertIn("if (!tickIsCurrent()) return", telemetry)
        self.assertIn("AXONOS_TELEMETRY_FAILURE_GRACE_MS", telemetry)
        self.assertIn("AXONOS_GPU_TELEMETRY_FAILURE_GRACE_MS", telemetry)
        self.assertIn("AXONOS_TELEMETRY_FAILURE_LIMIT", telemetry)
        self.assertIn("axonosGpuTelemetryFailures += 1", telemetry)
        self.assertIn("axonosContainerTelemetryFailures += 1", telemetry)
        self.assertIn("sourceUnavailable", telemetry)
        self.assertIn("axonosGpuTelemetryLastSuccessAt = 0", telemetry)
        self.assertIn("axonosContainerTelemetryLastSuccessAt = 0", telemetry)

        self.assertIn("cacheAge > 25", self.public_telemetry_source)
        self.assertIn("GPU telemetry cache is stale", self.public_telemetry_source)
        self.assertIn(
            'viewer_only = bool(parse_qs(pu.query).get("viewer"))',
            self.proxy_source,
        )
        self.assertIn("if not viewer_only:", self.proxy_source)
        self.assertIn("}, no_cache=True)", self.proxy_source)
        self.assertNotIn("urllib.parse.parse_qs(pu.query)", self.proxy_source)
        self.assertNotIn("_poll_gpus", self.proxy_source)
        self.assertNotIn("_gpu_cache_lock", self.proxy_source)
        self.assertNotIn("Thread(target=", self.proxy_source)

    def test_sidebar_telemetry_aggregates_exact_assigned_gpu_set(self) -> None:
        aggregation = self._page_between(
            "function axonosAggregateAssignedGpuTelemetry(",
            "function axonosTickGpuTelemetry(generation)",
        )
        gpu_tick = self._page_between(
            "function axonosTickGpuTelemetry(generation)",
            "function axonosTickTelemetry(generation)",
        )

        # The same helper handles 1x, 2x, 4x, and 8x profiles because the
        # expected assignment count is enforced before aggregation.
        self.assertIn("ids.length !== expected", aggregation)
        self.assertIn("normalizedIds.some(index => index == null)", aggregation)
        self.assertIn("ids.length !== normalizedIds.length", aggregation)
        self.assertIn("typeof value === 'number'", aggregation)
        self.assertIn("typeof index !== 'number'", aggregation)
        self.assertIn("assigned.some(gpu => !gpu)", aggregation)
        self.assertIn("/ assigned.length", aggregation)
        self.assertIn("vramUsed / vramTotal", aggregation)
        self.assertIn("gpu.memory_used_mb <= gpu.memory_total_mb", aggregation)
        self.assertNotIn("if (mine.length)", gpu_tick)
        self.assertIn("const owned = axonosEffectiveOwnedSession()", gpu_tick)
        self.assertIn("owned && owned.gpuIds", gpu_tick)
        self.assertIn("owned && owned.gpuCount", gpu_tick)
        self.assertIn("aggregate.matchedGpuCount !== expected", gpu_tick)
        self.assertIn("aggregate.gpuPct", gpu_tick)
        self.assertIn("aggregate.vramPct", gpu_tick)

    def test_sidebar_telemetry_restarts_only_on_connection_state_edges(self) -> None:
        self.assertIn(
            "let axonosTelemetryConnectionState =",
            self.page_source,
        )
        self.assertIn(
            "if (connected === axonosTelemetryConnectionState) return;",
            self.page_source,
        )
        self.assertIn(
            "axonosTelemetryConnectionState = connected;",
            self.page_source,
        )

    def test_same_origin_claim_route_forwards_requested_storage(self) -> None:
        claim_route = self.proxy_source.split(
            "if _session_mgr_available and self.path.startswith('/api/session/claim'):",
            1,
        )[1].split(
            "if _session_mgr_available and self.path.startswith('/api/session/heartbeat'):",
            1,
        )[0]

        self.assertIn("raw_storage_gb = data.get('requested_storage_gb')", claim_route)
        self.assertIn(
            "requested_storage_gb = min(500, max(10, int(raw_storage_gb)))",
            claim_route,
        )
        self.assertIn("requested_storage_gb=requested_storage_gb", claim_route)

    def test_explicit_wallet_sign_out_clears_identity_only_after_confirmed_release(self) -> None:
        sign_out = self._page_between(
            "window.axonosDisconnectWalletSession = function ()",
            "window.axonosBeginWalletSwitch = function (opts)",
        )

        self.assertIn("var walletDisconnectPromise = teardownSessionForWalletChange({", sign_out)
        self.assertIn("forceRelease: true", sign_out)
        self.assertIn(".then(function (released)", sign_out)
        self.assertIn("if (released !== true)", sign_out)
        self.assertIn("return false", sign_out)
        clear = "clearWalletIdentityAndUi();"
        confirmed = sign_out.split("if (released !== true)", 1)[1]
        self.assertIn(clear, confirmed)
        self.assertLess(confirmed.index("return false"), confirmed.index(clear))
        self.assertIn("axonosHandleSessionReleaseFailure", sign_out)
        self.assertIn("return window.axonosWalletDisconnectPromise", sign_out)
        self.assertIn("return walletDisconnectPromise", sign_out)

    def test_wallet_account_events_are_provider_aware_and_fail_closed(self) -> None:
        account_change = self._page_between(
            "function onWalletAccountsChanged(accounts, sourceProvider)",
            "// AUTHORITATIVE preflight:",
        )
        idle_check = self._page_between(
            "function axonosConfirmWalletIdentityIdle(wallet)",
            "function teardownSessionForWalletChange(options)",
        )
        binding = self._page_between(
            "function bindWalletAccountEvents(provider)",
            "bindWalletAccountEvents(getSafeWindowEthereum());",
        )

        self.assertIn("sourceProvider !== activeProvider", account_change)
        self.assertIn("window.axonosWalletAccountEventGeneration", account_change)
        self.assertIn("window.axonosPendingWalletAccountEvent", account_change)
        self.assertIn("axonosConfirmWalletIdentityIdle(oldAccount)", account_change)
        self.assertIn("clearWalletIdentityAndUi();", account_change)
        self.assertIn("window.axonosBeginWalletSwitch({", account_change)
        self.assertIn("useExposedAccount: true", account_change)
        self.assertNotIn("teardownSessionForWalletChange", account_change)
        self.assertIn("sessionStatusForWallet(wallet)", idle_check)
        self.assertIn("axonosSessionStatusIsAuthoritative(status)", idle_check)
        self.assertIn("onWalletAccountsChanged(accounts, provider)", binding)

    def test_page_claim_runs_passive_wallet_preflight_before_posting(self) -> None:
        claim = self._page_between(
            "function claimSession(options)",
            "function sessionStatus()",
        )

        preflight = "window.axonosEnsureWalletSessionCurrent({ requestPermission: false })"
        payload = "const payload = { wallet_address: wallet };"
        self.assertIn(preflight, claim)
        self.assertIn("walletPreflightDone: true", claim)
        self.assertIn("wallet_preflight_failed: true", claim)
        self.assertIn(payload, claim)
        self.assertLess(claim.index(preflight), claim.index(payload))

    def test_authoritative_ssh_claim_never_falls_through_to_desktop_connect(self) -> None:
        route = self._page_between(
            "function tryConnectAfterClaim(claim)",
            "const connectButton = document.getElementById('noVNC_connect_button');",
        )

        remember = "window.axonosRememberOwnedSession(claim);"
        ssh_branch = "if (granted && claim.ssh_enabled === true)"
        desktop_loader = "showConnectionLoader();"
        desktop_click = "launchBtn.click()"
        self.assertIn(remember, route)
        self.assertIn(ssh_branch, route)
        self.assertIn("if (claim.ssh_port)", route)
        self.assertIn("axonosRestoreSshSessionUi(Object.assign({}, claim", route)
        self.assertIn("owner_hard_cap_remaining_seconds", route)
        self.assertIn("SSH session is running, but its connection endpoint is unavailable", route)
        self.assertGreaterEqual(route.count("return;"), 2)
        self.assertIn(desktop_loader, route)
        self.assertIn(desktop_click, route)
        self.assertLess(route.index(remember), route.index(ssh_branch))
        self.assertLess(route.index(ssh_branch), route.index(desktop_loader))
        self.assertNotIn("UI.axonosSshEnabled()", route)

    def test_ssh_restore_and_card_render_do_not_restart_poll_or_screen(self) -> None:
        restore = self._page_between(
            "function axonosRestoreSshSessionUi(st)",
            "window.axonosRestoreSshSessionUi = axonosRestoreSshSessionUi;",
        )
        card = self._ui_between(
            "showAxonosSshCard(claim, options = {})",
            "_axonosUpdateSshCardCap(payload)",
        )
        ui_connect = self._ui_between(
            "connect(event, password)",
            "disconnect(options)",
        )

        self.assertEqual(restore.count("UI._axgtStartSessionBillingPoll();"), 1)
        self.assertIn("!UI._axgtStatusPollId", restore)
        self.assertNotIn("axonosUpdateActiveScreen('landing')", restore)
        self.assertIn("landingAlreadyActive", card)
        self.assertIn("!landingAlreadyActive", card)
        self.assertIn("!preserveScreen", card)
        ssh_connect = ui_connect.split(
            "if (claim && claim.ssh_enabled === true)", 1
        )[1].split("if (claim && claim.resumed === true", 1)[0]
        self.assertIn("UI.showAxonosSshCard(claim)", ssh_connect)
        self.assertNotIn("UI.openAxonosSshTerminal(claim)", ssh_connect)
        terminal_open = self._ui_between(
            "async openAxonosSshTerminal(claim, options = {})",
            "showAxonosTemplateDetails(t)",
        )
        self.assertIn("if (!UI._axgtStatusPollId)", terminal_open)

    def test_periodic_wallet_status_ignores_stale_identity_responses(self) -> None:
        poll = self._ui_between(
            "    _axgtPollWalletStatus() {",
            "    _axgtSetupUsageOverlayButton() {",
        )

        self.assertIn("const pollIdentityIsCurrent", poll)
        self.assertGreaterEqual(poll.count("if (!pollIdentityIsCurrent()) return;"), 2)

    def test_claim_and_post_claim_config_reads_are_bounded(self) -> None:
        page_claim = self._page_between(
            "function claimSession(options)",
            "function sessionStatus()",
        )
        claim_deadline = self._page_between(
            "function axonosSessionClaimTimeoutMs(resumeRequested)",
            "window.axonosSessionClaimTimeoutMs = axonosSessionClaimTimeoutMs",
        )
        ui_fetch = self.ui_source.split("_axonosFetchJsonWithTimeout(url", 1)[1].split(
            "/** POST /api/session/claim", 1
        )[0]
        ui_claim = self.ui_source.split("_axonosFetchSessionClaim(options)", 1)[1].split(
            "_axonosReleaseSessionHeaders()", 1
        )[0]
        ui_connect = self.ui_source.split("connect(event, password)", 1)[1].split(
            "disconnect(options)", 1
        )[0]

        self.assertIn("if (resumeRequested) return 20000", claim_deadline)
        self.assertIn("150000", claim_deadline)
        self.assertIn("session_claim_timeout_seconds", claim_deadline)
        self.assertIn("session_launcher_timeout_seconds", claim_deadline)
        self.assertIn("timeoutMs: axonosSessionClaimTimeoutMs(resumeRequested)", page_claim)
        self.assertIn("resumeRequested ? 20000 : 150000", ui_claim)
        self.assertIn("AbortController", ui_fetch)
        self.assertIn("response.text()", ui_fetch)
        self.assertIn("UI._axonosFetchJsonWithTimeout(url", ui_claim)
        self.assertIn("'./api/config'", ui_connect)
        self.assertIn("UI._axonosFetchJsonWithTimeout(", ui_connect)

    def test_uncertain_claims_are_reconciled_without_releasing_sessions(self) -> None:
        recovery = self._page_between(
            "function axonosReconcileUncertainSessionClaim(options)",
            "window.axonosReconcileUncertainSessionClaim = axonosReconcileUncertainSessionClaim",
        )
        ui_connect = self.ui_source.split("connect(event, password)", 1)[1].split(
            "disconnect(options)", 1
        )[0]
        run_verify = self._page_between(
            "function runVerify(walletAddress, provider, options)",
            "var axonosPaymentOperationGeneration = 0",
        )
        wizard = self._page_between(
            "function axonosTriggerLaunchFromWizard()",
            "// Stepper click binding for unconnected wallet connect",
        )

        self.assertIn("sessionStatusForWallet(wallet)", recovery)
        self.assertIn("axonosSessionStatusIsAuthoritative(status)", recovery)
        self.assertIn("axonosCreditGraceActive(status)", recovery)
        self.assertIn("axonosOwnedActiveSessionFromStatus(status, wallet)", recovery)
        self.assertIn("retryDelays = [1000, 3000, 6000]", recovery)
        self.assertGreaterEqual(recovery.count("sessionStatusForWallet(wallet)"), 1)
        self.assertIn("readRecoverableStatus()", recovery)
        self.assertIn("window.axonosSessionDetached = true", recovery)
        self.assertNotIn("/api/session/release", recovery)
        self.assertIn("if (!connectAttemptIsCurrent())", ui_connect)
        self.assertIn("if (granted)", ui_connect)
        self.assertGreaterEqual(
            ui_connect.count("UI._axonosReconcileUncertainSessionClaim({"),
            2,
        )
        self.assertIn(".catch(function (error)", run_verify)
        self.assertIn("axonosReconcileUncertainSessionClaim({", run_verify)
        self.assertIn(".catch(function (error)", wizard)
        self.assertIn("axonosReconcileUncertainSessionClaim({", wizard)
        self.assertLess(
            wizard.index("showConnectionLoader('preparing')"),
            wizard.index("axonosFetchWalletAccessStatus(wallet)"),
        )
        preflight = wizard.split("axonosFetchWalletAccessStatus(wallet)", 1)[0]
        self.assertNotIn("axonosTestCreditEligible", preflight)
        self.assertNotIn("is_whitelisted", preflight)
        self.assertIn("res.unavailable || res.ok === false", wizard)
        self.assertIn("hideConnectionLoader();", wizard)
        self.assertIn("Unable to check wallet credits", wizard)
        self.assertIn("retained session was not released", self.page_source)
        self.assertIn("running session exists and may be using credits", self.page_source)

    def test_wallet_verification_has_timeouts_cancellation_and_stale_attempt_guards(self) -> None:
        helpers = self._page_between(
            "var axonosWalletConnectGeneration = 0",
            "async function requestConnectedWallet(provider)",
        )
        verify = self._page_between(
            "function runVerify(walletAddress, provider, options)",
            "var axonosPaymentOperationGeneration = 0",
        )
        observer = self._page_between(
            "var hasActiveVerifiedSession =",
            "// Hide loader and branding when connection is established",
        )

        self.assertIn("axonosPromiseWithTimeout", helpers)
        self.assertIn("AbortController", helpers)
        self.assertIn("axonosInvalidateWalletVerification", verify)
        self.assertGreaterEqual(verify.count("axonosWalletVerifyFetch"), 2)
        self.assertGreaterEqual(verify.count("axonosWalletProviderRequest"), 2)
        self.assertIn("_axonosStaleWalletAttempt", verify)
        self.assertIn("axonosPromiseWithTimeout(", observer)
        self.assertIn("session status unavailable", observer)

    def test_credit_exhaustion_warning_remains_readable(self) -> None:
        warning = "Credit exhausted · 2h top-up grace. Jobs are still running"
        self.assertIn(warning, self.ui_source)
        self.assertGreaterEqual(self.ui_source.count("'warn',\n                12000"), 2)

    def test_persistent_wallet_warning_has_recovery_actions(self) -> None:
        self.assertIn('id="axonos_wallet_unavailable_banner"', self.page_source)
        self.assertIn('role="status"', self.page_source)
        self.assertIn('aria-live="polite"', self.page_source)
        self.assertIn('aria-hidden="true"', self.page_source)
        self.assertIn("still running and billing", self.page_source)
        self.assertIn('id="axonos_wallet_reconnect_btn"', self.page_source)
        self.assertIn('id="axonos_wallet_end_session_btn"', self.page_source)

        sync = self._page_between(
            "function axonosSyncWalletUnavailableIndicator()",
            "window.axonosSyncWalletUnavailableIndicator = axonosSyncWalletUnavailableIndicator",
        )
        self.assertIn("!!releaseFailure || (providerProblem && protectedState)", sync)
        self.assertIn("axonosWalletHasProtectedSessionState()", sync)
        self.assertIn("session may still be running and billing", sync)
        self.assertIn("Re-authenticate wallet", sync)
        self.assertIn("aria-hidden", sync)

    def test_provider_disconnect_and_connect_are_guarded_and_never_release(self) -> None:
        disconnected = self._page_between(
            "function onWalletProviderDisconnected(error, sourceProvider)",
            "function onWalletProviderConnected(connectInfo, sourceProvider)",
        )
        connected = self._page_between(
            "function onWalletProviderConnected(connectInfo, sourceProvider)",
            "function bindWalletAccountEvents(provider)",
        )
        binding = self._page_between(
            "function bindWalletAccountEvents(provider)",
            "bindWalletAccountEvents(getSafeWindowEthereum());",
        )

        self.assertIn("sourceProvider !== activeProvider", disconnected)
        self.assertIn("axonosWalletProviderEventGeneration", disconnected)
        self.assertIn("providerEventIsCurrent()", disconnected)
        self.assertIn("axonosMarkWalletProviderOutOfSync(", disconnected)
        self.assertNotIn("release", disconnected.lower())
        self.assertNotIn("teardownSessionForWalletChange", disconnected)
        self.assertIn("{ method: 'eth_accounts' }", connected)
        self.assertIn("onWalletAccountsChanged(accounts, sourceProvider)", connected)
        self.assertNotIn("axonosClearWalletProviderOutOfSync", connected)
        for event in ("accountsChanged", "disconnect", "connect"):
            self.assertEqual(binding.count(f"provider.on('{event}'"), 1)
        self.assertIn("_walletEventsBound.has(provider)", binding)

    def test_release_context_is_nonsecret_bounded_retryable_and_exact(self) -> None:
        context = self._ui_between(
            "_axonosSessionReleaseContext(options)",
            "_axonosNotifySessionReleaseResult(context)",
        )
        request = self._ui_between(
            "_axonosReleaseSessionBestEffort(context)",
            "/** Retry a failed explicit release",
        )
        retry = self._ui_between(
            "retryAxonosSessionRelease(snapshot)",
            "/** Cancel in-flight WebRTC negotiation",
        )
        disconnect = self._ui_between(
            "disconnect(options)",
            "/** Final billing heartbeat enters credit grace",
        )

        self.assertIn("attemptId", context)
        self.assertIn("sessionId", context)
        self.assertIn("hadServerSession", context)
        self.assertNotIn("verifiedWalletAuthToken", context)
        self.assertIn("AbortController", request)
        self.assertIn("_axonosSessionReleaseTimeoutMs()", request)
        self.assertIn("requestBody.expected_session_id", request)
        self.assertIn("data.released === true", request)
        self.assertIn("data.session_mismatch === true", request)
        self.assertIn("axonosConfirmSessionReleaseState", request)
        self.assertIn("confirmedByStatus: true", request)
        self.assertIn("reconciliationDelays = [750, 1500, 3000]", request)
        self.assertIn("_axonosExplicitReleasePromise", retry)
        self.assertIn("return retryPromise", retry)
        self.assertIn("_axonosExplicitReleasePromise", disconnect)
        self.assertIn("return releasePromise", disconnect)
        self.assertIn("if (released)", disconnect)
        self.assertIn("window.axonosSessionDetached = true", disconnect)
        confirmed = self._ui_between(
            "_axonosApplyConfirmedSessionRelease()",
            "_axonosSessionOwnsServerSlot()",
        )
        self.assertIn("window.axonosPendingResumeClaim = null", confirmed)
        self.assertIn("window.axonosPausedResume = null", confirmed)
        self.assertIn("window.axonosApplyResumeConnectUi(false)", confirmed)

    def test_release_results_are_correlated_to_the_latest_wallet_session_operation(self) -> None:
        operation = self._page_between(
            "window.axonosBeginSessionReleaseOperation = function (context)",
            "function axonosReleaseFailureSnapshot(context)",
        )
        failure = self._page_between(
            "window.axonosHandleSessionReleaseFailure = function (context)",
            "window.axonosClearSessionReleaseFailure = function ()",
        )
        success = self._page_between(
            "window.axonosHandleSessionReleaseSuccess = function ()",
            "function axonosStatusHasProtectedWalletSession(status, wallet)",
        )
        notify = self._ui_between(
            "_axonosNotifySessionReleaseResult(context)",
            "_axonosSessionReleaseTimeoutMs()",
        )

        self.assertIn("axonosSessionReleaseOperationGeneration || 0) + 1", operation)
        self.assertIn("operationId: operationId", operation)
        self.assertIn("source.operationId !== current.operationId", operation)
        self.assertIn("current.wallet !== sourceWallet", operation)
        self.assertIn("current.sessionId !== sourceSessionId", operation)
        self.assertLess(
            failure.index("axonosSessionReleaseResultIsCurrent(context)"),
            failure.index("window.axonosSessionReleaseFailure ="),
        )
        self.assertLess(
            success.index("axonosSessionReleaseResultIsCurrent(context)"),
            success.index("window.axonosClearSessionReleaseFailure()"),
        )
        self.assertLess(
            notify.index("axonosSessionReleaseResultIsCurrent(context)"),
            notify.index("UI._axonosSessionReleaseFailureContext ="),
        )

    def test_release_reauth_and_dashboard_end_are_recoverable(self) -> None:
        reauth = self._page_between(
            "function axonosReauthenticateWalletForSessionRelease(snapshot, provider)",
            "function axonosReleaseIntentClearsWallet(snapshot)",
        )
        actions = self._page_between(
            "var walletReconnectBtn = document.getElementById('axonos_wallet_reconnect_btn')",
            "// EIP-1193 wallet events fire",
        )
        dashboard_end = self._page_between(
            "function axonosEndSession(sessionId)",
            "window.axonosEndSession = axonosEndSession",
        )

        self.assertIn("runVerify(expectedWallet, provider, {", reauth)
        self.assertIn("releaseOnly: true", reauth)
        self.assertIn("currentToken === previousToken", reauth)
        self.assertIn("releaseFailureIsCurrent()", reauth)
        self.assertIn("identityGeneration === axonosWalletConnectGeneration", reauth)
        self.assertIn("walletEndSessionBtn.disabled = true", actions)
        self.assertIn("walletReconnectBtn.disabled = true", actions)
        self.assertIn("axonosRetrySessionRelease(retrySnapshot)", actions)
        self.assertIn("releaseBody.expected_session_id", dashboard_end)
        self.assertGreaterEqual(
            dashboard_end.count("axonosHandleSessionReleaseFailure"), 2
        )
        self.assertIn("axonosHandleSessionReleaseSuccess", dashboard_end)

        verify = self._page_between(
            "function runVerify(walletAddress, provider, options)",
            "var axonosPaymentOperationGeneration = 0",
        )
        release_only = verify.split(
            "if (verifyOptions.releaseOnly === true)", 1
        )[1].split("if (typeof axonosOnWalletVerified", 1)[0]
        self.assertIn("return true", release_only)
        self.assertNotIn("axonosOnWalletVerified", release_only)
        self.assertNotIn("claimSession", release_only)

    def test_release_status_reconciliation_is_authoritative_and_new_session_safe(self) -> None:
        reconcile = self._page_between(
            "window.axonosConfirmSessionReleaseState = function (context)",
            "// Local flags can lag an already-created server allocation",
        )

        self.assertIn("sessionStatusForWallet(wallet)", reconcile)
        self.assertIn("axonosSessionStatusIsAuthoritative(status)", reconcile)
        self.assertIn("axonosOwnedActiveSessionFromStatus(status, wallet)", reconcile)
        self.assertIn("axonosCreditGraceActive(status)", reconcile)
        self.assertIn("activeId !== expectedId", reconcile)
        self.assertIn("confirmed: true", reconcile)
        self.assertIn("billingEnded: true", reconcile)


if __name__ == "__main__":
    unittest.main()
