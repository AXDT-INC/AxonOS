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
        # The GUI command itself lives in the shared launcher installed by the image.
        self.assertIn(
            "COPY scripts/cellmodeller-gui.sh /usr/local/bin/cellmodeller-gui", source
        )
        self.assertIn("RUN chmod +x /usr/local/bin/cellmodeller-gui", source)

    def test_shared_launcher_sets_explicit_module_path_and_runtime(self) -> None:
        source = (ROOT / "scripts" / "cellmodeller-gui.sh").read_text(encoding="utf-8")
        self.assertIn("PYTHONPATH=", source)
        self.assertIn("/opt/CellModeller", source)
        self.assertIn(
            "/usr/bin/python3 /opt/CellModeller/Scripts/CellModellerGUI.py",
            source,
        )
        self.assertIn("vglrun /usr/bin/python3", source)
        # Simulator.init_data_output does os.mkdir($CMPATH/data/<run>) and needs
        # the data directory to exist; the launcher must create it.
        self.assertIn("CMPATH", source)
        self.assertIn('mkdir -p "$CMPATH/data"', source)

    def test_shared_launcher_keeps_cellmodeller_output_visible(self) -> None:
        source = (ROOT / "scripts" / "cellmodeller-gui.sh").read_text(encoding="utf-8")
        self.assertIn("CellModeller simulation output will remain visible", source)
        self.assertIn("The terminal will remain open", source)
        self.assertIn("exec bash", source)
        self.assertNotIn("CellModellerGUI.py >/dev/null", source)
        self.assertNotIn("CellModellerGUI.py 2>&1", source)

    def test_template_launches_shared_launcher_in_titled_terminal(self) -> None:
        source = (ROOT / "scripts" / "apply_session_template.sh").read_text(
            encoding="utf-8"
        )
        cellmodeller = source.split("    cellmodeller)", 1)[1].split("        ;;", 1)[0]
        self.assertIn(
            'launch_terminal "CellModeller — Simulation Output"', cellmodeller
        )
        self.assertIn("/usr/local/bin/cellmodeller-gui", cellmodeller)
        self.assertNotIn("CellModellerGUI.py", cellmodeller)

    def test_menu_entry_uses_shared_launcher_without_terminal_helper(self) -> None:
        # The XFCE menu entry must not depend on Terminal=true / the exo
        # terminal-helper lookup (helpers.rc is masked by wallet home volumes);
        # it opens the same titled terminator as the template and holds it open.
        source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "COPY scripts/cellmodeller-gui.sh /usr/local/bin/cellmodeller-gui", source
        )
        entry = source.split("Name=CellModeller", 1)[1].split("Categories=Science;", 1)[0]
        self.assertIn(
            'Exec=terminator --title "CellModeller — Simulation Output" '
            "-x /usr/local/bin/cellmodeller-gui --hold",
            entry,
        )
        self.assertIn("Terminal=false", entry)
        self.assertNotIn("Terminal=true", entry)

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
                self.assertGreaterEqual(
                    source.count(
                        "COPY scripts/cellmodeller-gui.sh /usr/local/bin/cellmodeller-gui"
                    ),
                    2,
                )
                self.assertGreaterEqual(
                    source.count("-x /usr/local/bin/cellmodeller-gui --hold"), 2
                )
                self.assertNotIn(
                    'Exec=bash -c "/usr/bin/python3 /opt/CellModeller', source
                )


if __name__ == "__main__":
    unittest.main()
