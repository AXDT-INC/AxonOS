import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]


class CellModellerImageContractTests(unittest.TestCase):
    def test_primary_image_uses_one_python_runtime_and_checks_imports(self) -> None:
        source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("python3-pyopencl", source)
        self.assertIn("/usr/bin/python3 -m pip install -e .", source)
        self.assertIn(
            'PYTHONPATH=/opt/CellModeller /usr/bin/python3 -c "import CellModeller; import pyopencl"',
            source,
        )
        self.assertIn(
            "PYTHONPATH=/opt/CellModeller /usr/bin/python3 /opt/CellModeller/Scripts/CellModellerGUI.py",
            source,
        )

    def test_template_launch_sets_explicit_module_path(self) -> None:
        source = (ROOT / "scripts" / "apply_session_template.sh").read_text(
            encoding="utf-8"
        )
        cellmodeller = source.split("    cellmodeller)", 1)[1].split("        ;;", 1)[0]
        self.assertIn("PYTHONPATH=/opt/CellModeller", cellmodeller)
        self.assertIn("/usr/bin/python3", cellmodeller)

    def test_generated_image_variants_keep_cellmodeller_contract(self) -> None:
        for relative in ("axonos_launcher/launcher_core.py", "axonos_launcher/main.py"):
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertGreaterEqual(source.count("python3-pyopencl"), 2)
                self.assertGreaterEqual(
                    source.count("/usr/bin/python3 -m pip install -e ."), 2
                )
                self.assertGreaterEqual(
                    source.count("import CellModeller; import pyopencl"), 2
                )
                self.assertGreaterEqual(
                    source.count("PYTHONPATH=/opt/CellModeller /usr/bin/python3"), 4
                )


if __name__ == "__main__":
    unittest.main()
