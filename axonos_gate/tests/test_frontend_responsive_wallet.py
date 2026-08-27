import unittest
from pathlib import Path


class ResponsiveWalletDialogContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo = Path(__file__).resolve().parents[2]
        cls.page = (repo / "novnc-theme" / "vnc.html").read_text(encoding="utf-8")
        cls.css = (repo / "novnc-theme" / "axonos-theme.css").read_text(
            encoding="utf-8"
        )

    def test_compact_view_notice_explains_mobile_window_and_zoom_triggers(self) -> None:
        # The compact notice was intentionally shortened during the responsive
        # polish. Pin the current user-facing contract instead of obsolete copy.
        self.assertIn("Built for desktops", self.page)
        self.assertIn("You're in a compact view", self.page)
        self.assertIn("browse the environments here", self.page)
        self.assertIn("open AxonOS on a desktop", self.page)
        self.assertIn("widen the window / reduce zoom", self.page)

    def test_browse_environments_uses_a_lighter_dedicated_purple_style(self) -> None:
        browse = self.css.split("#axonos_hero_browse_btn {", 1)[1].split("}", 1)[0]
        topbar = self.css.split(".axonos-topbar-cta {", 1)[1].split("}", 1)[0]

        self.assertIn("#c2baff", browse)
        self.assertIn("#9b8eff", browse)
        self.assertIn("#8b7cff", topbar)
        self.assertIn("#6a57f2", topbar)
        self.assertIn("#axonos_hero_browse_btn:hover", self.css)
        self.assertIn("#axonos_hero_browse_btn:focus-visible", self.css)

    def test_wallet_dialog_remains_viewport_fixed_at_tablet_breakpoint(self) -> None:
        responsive = self.css.split("@media (max-width: 992px) {", 1)[1].split(
            "@media (max-width: 768px) {", 1
        )[0]
        dialog = responsive.split("#noVNC_credentials_dlg {", 1)[1].split("}", 1)[0]

        self.assertIn("position: fixed !important", dialog)
        self.assertIn("top: 50% !important", dialog)
        self.assertIn("left: 50% !important", dialog)
        self.assertIn("transform: translate(-50%, -50%) !important", dialog)
        self.assertIn("max-height: calc(100vh - 24px) !important", dialog)
        self.assertIn("max-height: calc(100dvh - 24px) !important", dialog)
        self.assertIn("overflow-y: auto !important", dialog)
        self.assertNotIn("position: relative", dialog)
        self.assertNotIn("top: auto", dialog)

    def test_wallet_session_warning_is_persistent_nonmodal_and_responsive(self) -> None:
        banner = self.css.split(".axonos-wallet-unavailable-banner {", 1)[1].split(
            "}", 1
        )[0]
        hidden = self.css.split(
            ".axonos-wallet-unavailable-banner--hidden {", 1
        )[1].split("}", 1)[0]
        actions = self.css.split(
            ".axonos-wallet-unavailable-banner__actions {", 1
        )[1].split("}", 1)[0]
        focus = self.css.rsplit(
            ".axonos-wallet-unavailable-banner__btn:focus-visible {", 1
        )[1].split("}", 1)[0]
        mobile = self.css.split("@media (max-width: 680px) {", 1)[1].split(
            "}", 1
        )[0]

        self.assertIn("position: fixed", banner)
        self.assertIn("width: min(760px, calc(100vw - 32px))", banner)
        self.assertNotIn("inset: 0", banner)
        self.assertIn("display: none !important", hidden)
        self.assertIn("pointer-events: auto", actions)
        self.assertIn("outline: 2px solid", focus)
        self.assertIn("flex-wrap: wrap", mobile)

    def test_extension_failures_do_not_open_the_novnc_fatal_overlay(self) -> None:
        guard = self.page.split(
            "// Suppress browser-extension errors", 1
        )[1].split('<script src="app/error-handler.js"></script>', 1)[0]

        self.assertIn("unhandledrejection", guard)
        self.assertIn("isExtensionFailure(e.reason)", guard)
        self.assertIn("isExtensionFailure(e.error)", guard)
        self.assertIn("chrome-extension", guard)
        self.assertIn("moz-extension", guard)
        self.assertIn("safari-web-extension", guard)
        self.assertIn("e.stopImmediatePropagation()", guard)
        self.assertIn("e.preventDefault()", guard)

    def test_fatal_fallback_uses_axonos_theme_and_can_be_dismissed(self) -> None:
        self.assertIn('class="axonos-fallback-card"', self.page)
        self.assertIn('class="axonos-fallback-dismiss"', self.page)
        self.assertIn("fallback.classList.remove('noVNC_open')", self.page)
        self.assertIn("message.replaceChildren()", self.page)
        self.assertIn("#noVNC_fallback_error > .axonos-fallback-card", self.css)
        self.assertIn("background: linear-gradient", self.css)


if __name__ == "__main__":
    unittest.main()
