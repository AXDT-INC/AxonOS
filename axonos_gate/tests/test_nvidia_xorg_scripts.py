"""Regression tests for the NVIDIA Xorg runtime-link repair."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class NvidiaXorgScriptTests(unittest.TestCase):
    def test_glx_link_keeps_runtime_managed_indirection(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        script = repo / "scripts" / "fix-libglx-nvidia-symlink.sh"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extensions = root / "xorg" / "extensions"
            nvidia_xorg = root / "nvidia" / "xorg"
            extensions.mkdir(parents=True)
            nvidia_xorg.mkdir(parents=True)

            old_module = nvidia_xorg / "libglxserver_nvidia.so.580.159.03"
            new_module = nvidia_xorg / "libglxserver_nvidia.so.580.173.02"
            old_module.touch()
            new_module.touch()

            runtime_link = nvidia_xorg / "libglxserver_nvidia.so"
            runtime_link.symlink_to(new_module.name)
            (extensions / "libglx.so").symlink_to(old_module)

            env = {
                **os.environ,
                "AXONOS_GLX_EXT_DIR": str(extensions),
                "AXONOS_NVIDIA_XORG_DIR": str(nvidia_xorg),
            }
            subprocess.run(["sh", str(script)], check=True, env=env, capture_output=True, text=True)

            repaired = extensions / "libglx.so"
            self.assertEqual(os.readlink(repaired), str(runtime_link))
            self.assertEqual(repaired.resolve(), new_module)

    def test_xorg_start_repairs_link_before_launch(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        source = (repo / "scripts" / "start-xorg-nvidia.sh").read_text(encoding="utf-8")
        self.assertLess(source.index('"$GLX_FIX"'), source.index("/usr/bin/Xorg :0"))
        self.assertIn("does not match runtime driver", source)


if __name__ == "__main__":
    unittest.main()
