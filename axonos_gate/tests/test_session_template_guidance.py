import re
import subprocess
import sys
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_SCRIPT = _REPO_ROOT / "scripts" / "apply_session_template.sh"
_UI_SCRIPT = _REPO_ROOT / "novnc-theme" / "ui.js"
_GATE_ROOT = _REPO_ROOT / "axonos_gate"
if str(_GATE_ROOT) not in sys.path:
    sys.path.insert(0, str(_GATE_ROOT))


class SessionTemplateGuidanceTests(unittest.TestCase):
    def test_browser_shell_and_backend_template_catalogs_match(self) -> None:
        from docker_gpu_cli import SUPPORTED_SESSION_TEMPLATE_IDS

        ui_source = _UI_SCRIPT.read_text(encoding="utf-8")
        ui_catalog = tuple(re.findall(r"^\s*id:\s*'([^']+)'", ui_source, re.M))

        shell_source = _TEMPLATE_SCRIPT.read_text(encoding="utf-8")
        shell_case = shell_source.split('case "$template" in', 1)[1].split("    *)", 1)[0]
        shell_catalog = []
        for labels in re.findall(r"^    ([a-z0-9|-]+)\)$", shell_case, re.M):
            shell_catalog.extend(labels.split("|"))

        self.assertEqual(len(ui_catalog), len(set(ui_catalog)))
        self.assertEqual(set(ui_catalog), set(SUPPORTED_SESSION_TEMPLATE_IDS))
        self.assertEqual(set(shell_catalog), set(SUPPORTED_SESSION_TEMPLATE_IDS))

    def test_template_launcher_has_valid_shell_syntax(self) -> None:
        subprocess.run(["bash", "-n", str(_TEMPLATE_SCRIPT)], check=True)

    def test_gromacs_template_shows_verified_compatibility_command(self) -> None:
        source = _TEMPLATE_SCRIPT.read_text(encoding="utf-8")
        gromacs_case = source.split("    gromacs)", 1)[1].split("        ;;", 1)[0]

        self.assertIn("export OMP_NUM_THREADS=8", gromacs_case)
        self.assertIn("GROMACS ready (OMP_NUM_THREADS=8)", gromacs_case)
        self.assertIn(
            "gmx_mpi mdrun -deffnm md -ntomp 8 -nb gpu -pme cpu "
            "-update cpu -pin on",
            gromacs_case,
        )
        self.assertIn("GPU PME is currently experimental", gromacs_case)
        self.assertIn("compatibility fallback, not a performance default", gromacs_case)

    def test_gromacs_template_avoids_unstable_force_field_numbers(self) -> None:
        source = _TEMPLATE_SCRIPT.read_text(encoding="utf-8")
        gromacs_case = source.split("    gromacs)", 1)[1].split("        ;;", 1)[0]

        self.assertIn("Select force fields by name, not menu number", gromacs_case)
        self.assertIn("-ff amber99sb-ildn", gromacs_case)


if __name__ == "__main__":
    unittest.main()
