"""Offline contract tests; the bounded CI job exercises the actual resolver."""

import importlib.util
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docker/scientific-python.txt"
SPEC = importlib.util.spec_from_file_location(
    "check_scientific_python", ROOT / "scripts/check_scientific_python.py"
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
EXPECTED = {
    "numpy": "1.26.4",
    "matplotlib": "3.10.9",
    "spyder": "6.1.7",
    "pyparsing": "3.3.2",
    "pylint": "4.0.10",
}


class ScientificPythonImageTests(unittest.TestCase):
    def test_manifest_pins_only_reviewed_roots_and_one_transitive(self):
        requirements = [
            line.strip() for line in MANIFEST.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertCountEqual(
            requirements, [f"{name}=={version}" for name, version in EXPECTED.items()]
        )

    def test_both_scientific_layers_use_the_manifest_and_bounded_resolver(self):
        source = (ROOT / "Dockerfile").read_text()
        self.assertIn("ARG AXONOS_SKIP_HEAVY=0", source)
        self.assertIn(
            "COPY docker/scientific-python.txt /opt/axonos-build/scientific-python.txt",
            source,
        )
        self.assertIn("-r /opt/axonos-build/scientific-python.txt", source)
        self.assertIn(
            "-c /opt/axonos-build/scientific-python.txt numpy matplotlib pyparsing",
            source,
        )
        self.assertEqual(source.count("timeout --kill-after=30s 15m"), 2)
        self.assertNotIn("'numpy>=1.24.0,<2' matplotlib", source)
        self.assertNotIn("--no-deps", source)
        self.assertNotIn("--upgrade pip", source)
        self.assertNotRegex(source, r"(?m)^ENV\s+PIP_CONSTRAINT")

    def test_full_build_checks_science_after_all_package_installations(self):
        source = (ROOT / "Dockerfile").read_text()
        check = source.index(
            "/usr/bin/python3 /opt/axonos-build/check_scientific_python.py"
        )
        installs = list(re.finditer(r"pip install|apt(?:-get)? install", source))
        self.assertTrue(installs)
        self.assertGreater(check, max(match.start() for match in installs))
        self.assertIn('if [ "$AXONOS_SKIP_HEAVY" != "1" ]; then', source[:check])

    def test_ci_resolves_with_production_pip_and_a_deadline(self):
        source = (ROOT / ".github/workflows/validate.yaml").read_text()
        job = source.split("  scientific-python:\n", 1)[1].split("  dockerfile:", 1)[0]
        self.assertIn('python-version: "3.10"', job)
        self.assertIn("timeout-minutes: 25", job)
        self.assertIn("pip==22.0.2", job)
        self.assertLess(job.index("jupyterlab"), job.index("-r docker/scientific-python.txt"))
        self.assertIn("timeout --kill-after=30s 20m", job)
        self.assertIn("python scripts/check_scientific_python.py", job)

    @mock.patch.object(CHECK.importlib, "import_module")
    @mock.patch.object(CHECK.metadata, "version", side_effect=EXPECTED.__getitem__)
    def test_version_check_imports_every_pinned_module(self, version, load):
        load.side_effect = lambda name: SimpleNamespace(__version__=EXPECTED[name])
        CHECK.check_versions(MANIFEST)
        self.assertEqual(load.call_count, len(EXPECTED))

    @mock.patch.object(CHECK.metadata, "version", return_value="999.0")
    def test_version_drift_is_rejected(self, version):
        with self.assertRaisesRegex(RuntimeError, "Expected numpy==1.26.4"):
            CHECK.check_versions(MANIFEST)

    @mock.patch.object(CHECK.importlib, "import_module")
    @mock.patch.object(CHECK.metadata, "version", side_effect=EXPECTED.__getitem__)
    def test_shadowed_module_is_rejected(self, version, load):
        load.return_value = SimpleNamespace(__version__="0.0")
        with self.assertRaisesRegex(RuntimeError, "module/metadata version mismatch"):
            CHECK.check_versions(MANIFEST)

    @mock.patch.object(CHECK.metadata, "requires", return_value=[
        "pylint<4.1,>=3.1; extra == 'all'",
        "not-installed-windows-only; sys_platform == 'win32'",
    ])
    @mock.patch.object(CHECK.metadata, "version", return_value="4.1.1")
    def test_language_server_extra_conflict_is_rejected(self, version, requires):
        with self.assertRaisesRegex(RuntimeError, "python-lsp-server requires pylint"):
            CHECK.check_requirements("python-lsp-server", extras=("", "all"))

    @mock.patch.object(CHECK.metadata, "requires", return_value=[
        "pylint<4.1,>=3.1; extra == 'all'",
        "not-installed-test-extra; extra == 'test'",
    ])
    @mock.patch.object(CHECK.metadata, "version", return_value="4.0.10")
    def test_inactive_extras_are_not_required(self, version, requires):
        CHECK.check_requirements("python-lsp-server", extras=("", "all"))
        version.assert_called_once_with("pylint")


if __name__ == "__main__":
    unittest.main()
