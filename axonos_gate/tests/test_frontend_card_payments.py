"""Execute CARD checkout and return behavior from the actual page JavaScript."""

import os
from pathlib import Path
import shutil
import subprocess
import unittest


class CardPaymentFrontendTests(unittest.TestCase):
    def test_card_payment_runtime(self) -> None:
        node = shutil.which("node")
        if not node:
            try:
                import playwright

                candidate = Path(playwright.__file__).resolve().parent / "driver" / "node"
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    node = str(candidate)
            except (ImportError, OSError, TypeError):
                pass
        if not node:
            self.skipTest("Node runtime is unavailable")
        repo = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [node, str(repo / "axonos_gate/tests/card_payments_runtime.js"),
             str(repo / "novnc-theme/vnc.html")],
            cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("card payment runtime checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
