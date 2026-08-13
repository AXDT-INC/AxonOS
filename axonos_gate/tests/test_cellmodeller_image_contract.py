import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]


class CellModellerImageContractTests(unittest.TestCase):
    def test_primary_image_preserves_known_working_opencl_install(self) -> None:
        source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("python3-pyopencl", source)
        self.assertNotIn("pocl-opencl-icd", source)
        self.assertNotIn('"pyopencl==2024.3"', source)
        self.assertIn("cd /opt/CellModeller && pip install -e .", source)
        self.assertNotIn("import CellModeller; import pyopencl", source)
        self.assertIn(
            "/usr/bin/python3 /opt/CellModeller/Scripts/CellModellerGUI.py",
            source,
        )

    def test_template_launch_sets_explicit_module_path(self) -> None:
        source = (ROOT / "scripts" / "apply_session_template.sh").read_text(
            encoding="utf-8"
        )
        cellmodeller = source.split("    cellmodeller)", 1)[1].split("        ;;", 1)[0]
        self.assertIn("PYTHONPATH=/opt/CellModeller", cellmodeller)
        self.assertIn("/usr/bin/python3", cellmodeller)

    def test_template_keeps_cellmodeller_output_visible(self) -> None:
        source = (ROOT / "scripts" / "apply_session_template.sh").read_text(
            encoding="utf-8"
        )
        cellmodeller = source.split("    cellmodeller)", 1)[1].split("        ;;", 1)[0]
        self.assertIn(
            'launch_terminal "CellModeller — Simulation Output"', cellmodeller
        )
        self.assertIn("CellModeller simulation output will remain visible", cellmodeller)
        self.assertIn("The terminal will remain open", cellmodeller)
        self.assertNotIn("CellModellerGUI.py >/dev/null", cellmodeller)
        self.assertNotIn("CellModellerGUI.py 2>&1", cellmodeller)

    def test_generated_image_variants_keep_cellmodeller_contract(self) -> None:
        for relative in ("axonos_launcher/launcher_core.py", "axonos_launcher/main.py"):
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("python3-pyopencl", source)
                self.assertNotIn("pocl-opencl-icd", source)
                self.assertNotIn('"pyopencl==2024.3"', source)
                self.assertGreaterEqual(
                    source.count("cd /opt/CellModeller && pip install -e ."), 2
                )
                self.assertNotIn("import CellModeller; import pyopencl", source)


if __name__ == "__main__":
    unittest.main()
