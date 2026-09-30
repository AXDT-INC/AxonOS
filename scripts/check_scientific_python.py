"""Fail the full image build on scientific version or dependency drift.

Also usable in a disposable Python 3.10 environment after installing
docker/scientific-python.txt. Does not start Spyder or contact any service.
"""

import importlib
from importlib import metadata
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from packaging.requirements import Requirement


def check_versions(manifest):
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.partition("#")[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        actual = metadata.version(requirement.name)
        if actual not in requirement.specifier:
            raise RuntimeError(f"Expected {requirement}; installed {actual}")
        print(f"{requirement.name}=={actual}", flush=True)
        # Check the module actually loaded, not only distribution metadata.
        module = importlib.import_module(requirement.name)
        if module.__version__ != actual:
            raise RuntimeError(f"{requirement.name}: module/metadata version mismatch")


def check_requirements(project, extras=("",)):
    """pip check alone does not verify the language server's 'all' extra."""
    checked = 0
    for line in metadata.requires(project) or ():
        requirement = Requirement(line)
        if requirement.marker and not any(
            requirement.marker.evaluate({"extra": extra}) for extra in extras
        ):
            continue
        actual = metadata.version(requirement.name)
        if actual not in requirement.specifier:
            raise RuntimeError(f"{project} requires {requirement}; installed {actual}")
        checked += 1
    print(f"{project}: {checked} active requirements satisfied", flush=True)


def main():
    with tempfile.TemporaryDirectory(prefix="axonos-science-check-") as config:
        os.environ["MPLCONFIGDIR"] = config
        check_versions(Path(sys.argv[1]))
        check_requirements("spyder")
        check_requirements("python-lsp-server", extras=("", "all"))

        import numpy
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        figure = Figure()
        canvas = FigureCanvasAgg(figure)
        figure.subplots().plot(numpy.arange(3))
        canvas.draw()
        print("NumPy/Matplotlib headless render passed", flush=True)

    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)


if __name__ == "__main__":
    main()
