"""Regression coverage for holder discounts in the workspace payment wizard."""

from pathlib import Path
import unittest


class WizardDiscountFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo = Path(__file__).resolve().parents[2]
        cls.page = (repo / "novnc-theme" / "vnc.html").read_text(encoding="utf-8")

    def test_holder_rails_always_request_authoritative_quote(self) -> None:
        self.assertIn(
            "if (amount > 0 && (token !== 'axgt' || dyn))",
            self.page,
        )
        self.assertIn("axonosWizardScheduleQuote(token, amount);", self.page)

    def test_quote_updates_credits_and_visible_holder_tier(self) -> None:
        self.assertIn(
            "axonosWizardApplyCalculatorCredits(Number(d.estimated_minutes));",
            self.page,
        )
        self.assertIn(
            "tierVal.textContent = tierLabel + ' (' + discount + '% discount)'",
            self.page,
        )
        self.assertIn("Checking on-chain balance…", self.page)

    def test_direct_axgt_is_not_presented_as_holder_discount(self) -> None:
        self.assertIn("Not applicable (flat bonus)", self.page)


if __name__ == "__main__":
    unittest.main()
