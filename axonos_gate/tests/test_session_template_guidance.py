import subprocess
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_SCRIPT = _REPO_ROOT / "scripts" / "apply_session_template.sh"


class SessionTemplateGuidanceTests(unittest.TestCase):
    def test_template_launcher_has_valid_shell_syntax(self) -> None:
        subprocess.run(["bash", "-n", str(_TEMPLATE_SCRIPT)], check=True)

    def test_gromacs_template_shows_verified_compatibility_command(self) -> None:
        source = _TEMPLATE_SCRIPT.read_text(encoding="utf-8")
        gromacs_case = source.split("    gromacs)", 1)[1].split("        ;;", 1)[0]

        self.assertNotIn("export OMP_NUM_THREADS=8", gromacs_case)
        self.assertIn(
            "OMP_NUM_THREADS=8 gmx_mpi mdrun -deffnm md -ntomp 8 -nb gpu -pme cpu "
            "-update cpu -pin on",
            gromacs_case,
        )
        self.assertIn("GPU PME is currently experimental", gromacs_case)
        self.assertIn("compatibility fallback, not a performance default", gromacs_case)


if __name__ == "__main__":
    unittest.main()
