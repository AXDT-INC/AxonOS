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
        self.assertIn("More screen space recommended", self.page)
        self.assertIn(
            "AxonOS is designed for desktop use. You're using a compact", self.page
        )
        self.assertIn("phone or", self.page)
        self.assertIn("in a narrow window, or at higher browser zoom", self.page)
        self.assertIn("widen the window or reduce", self.page)
        self.assertIn("to a laptop or desktop to continue", self.page)

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


if __name__ == "__main__":
    unittest.main()
