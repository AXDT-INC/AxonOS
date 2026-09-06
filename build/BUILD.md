# AxonOS Launcher Build Guide

This guide explains how to build platform-specific binaries and packages for the AxonOS Launcher.

## ⚠️ Read First: Working Directory

Every build script (`build_launcher.py`, `build_deb.py`, `build_all.py`, `build_macos.py`,
`build_windows.py`, `build_cross_platform.py`) checks for `axonos_launcher/main.py` relative to the
current directory and exits if it is not found. The PyInstaller spec files they generate also hardcode
`axonos_launcher/main.py` and `axonos_launcher/README.md`. **Run the scripts from the repository root**:

```bash
cd /path/to/AxonOS          # repo root, NOT build/
python3 build/build_launcher.py
```

The `build/Makefile` invokes the scripts as `python3 build_launcher.py` (no `build/` prefix), so it
only finds them when `make` is run from inside `build/` — but from `build/` the scripts themselves fail
the `axonos_launcher/main.py` check. As a result **the Makefile targets do not currently work as
written** (except `make cross`, because `build_simple_cross.py` detects `../axonos_launcher/main.py`
and changes to the parent directory itself). Until the Makefile is fixed, use the direct
`python3 build/<script>.py` commands shown below. The Makefile targets are documented for reference.

## 🎯 Quick Start

### Manual Process (recommended)
```bash
# From the repository root
pip3 install pyinstaller pyyaml

# Build binary
python3 build/build_launcher.py

# Build .deb package (Linux only; requires dist/axonos from the previous step)
python3 build/build_deb.py

# Or build everything (binary + .deb + RELEASE_INFO.txt)
python3 build/build_all.py
```

### Using Make (see working-directory caveat above)
```bash
cd build
make deps      # pip3 install pyinstaller (+ dpkg-dev gzip on Linux)
make release   # python3 build_all.py  -- fails from build/ (see above)
make install
```

## 📋 Prerequisites

### All Platforms
- Python 3.8 or later (the scripts use `subprocess.run(capture_output=...)`, which needs 3.7+, and
  current PyInstaller releases require 3.8+; the Docker cross-build images use `python:3.12-slim`)
- tkinter (usually included with Python; `python3-tk` on Debian/Ubuntu)
- PyInstaller (installed automatically by `build_launcher.py`, `build_macos.py`, `build_windows.py`)
- PyYAML (`axonos_launcher/requirements.txt`; imported by `main.py`, `cli.py`, `launcher_core.py`).
  It is only auto-installed by `build_windows.py`; on Linux/macOS install it yourself.

### Linux (Ubuntu 22.04)
```bash
# Essential packages
sudo apt update
sudo apt install -y python3 python3-tk python3-pip

# For .deb package building
sudo apt install -y dpkg-dev gzip

# Python build dependencies
pip3 install pyinstaller pyyaml   # or: pip3 install pyinstaller -r axonos_launcher/requirements.txt
```

### macOS
```bash
pip3 install pyinstaller pyyaml

# tkinter via Homebrew if missing
brew install python-tk
```

### Windows
```powershell
# Install Python from python.org (includes tkinter)
pip install pyinstaller pyyaml
```

## 🔨 Build Process

### 1. Binary Creation (`build/build_launcher.py`)

Creates a single-file binary using PyInstaller (spec generated on the fly as `axonos_launcher.spec`
and deleted afterwards). Output names for this script:

**Linux**: `dist/axonos`  
**macOS**: `dist/axonos.app` (plus the raw executable `dist/axonos`)  
**Windows**: `dist/axonos.exe`

Note: `build/build_macos.py` and `build/build_windows.py` (used by `make dmg` / `make exe`) use a
different naming scheme: `dist/AxonOS Launcher.app` and `dist/AxonOS Launcher.exe`. The helper
scripts `build/create_dmg.sh` (expects `dist/axonos.app`) and `build/create_windows_installer.bat`
(expects `dist\axonos.exe`) only pair with `build_launcher.py` output.

Features:
- Single executable with all dependencies (`hiddenimports` for `yaml`)
- Windowed build (`console=False`). Note the launcher defaults to **CLI mode** (see below), so on
  Windows/macOS the windowed build suppresses CLI stdout; run `axonos --gui` for the GUI.
- Bundles `axonos_launcher/README.md`
- UPX compression only if a `upx` binary is on PATH (the scripts do not install or check for it)
- macOS builds add a PyInstaller `BUNDLE` step
- Also writes `dist/BUILD_INFO.txt`

### 2. Package Creation (`build/build_deb.py`)

Creates a `.deb` package for Ubuntu 22.04 systems. Requires `dist/axonos` to exist already.

**Output**: `AxonOS-Launcher-0.1.0-Linux-amd64.deb` (written to the current directory)

Package includes:
- Binary installation to `/usr/local/bin/axonos`
- Desktop entry for applications menu
- Generated placeholder SVG icon and documentation
- Dependency management (python3, python3-tk, docker.io | docker-ce)
- Post-installation script (desktop database refresh, Docker availability hint)

### 3. Complete Build (`build/build_all.py`)

Orchestrates the entire build process:
- Checks prerequisites (`axonos_launcher/main.py` present, Python version)
- Runs `build_launcher.py`
- Runs `build_deb.py` (Linux only, skipped if `dpkg-deb` is missing)
- Writes `RELEASE_INFO.txt`

### Launcher runtime behaviour

The launcher is **CLI-by-default**. `axonos` with no arguments prints the CLI help; the GUI is
started with `axonos --gui` (requires a display). CLI subcommands: `list`, `generate`, `build`,
`deploy`, `config save|load` (see `axonos_launcher/cli.py`). The GitHub auto-clone feature only
runs in GUI mode.

## 📦 Package Details

### .deb Package Structure
```
AxonOS-Launcher-0.1.0-Linux-amd64.deb
├── DEBIAN/
│   ├── control           # Package metadata
│   ├── postinst         # Post-installation script
│   └── prerm            # Pre-removal script
├── usr/local/bin/
│   └── axonos          # Main binary
├── usr/share/applications/
│   └── axonos-launcher.desktop  # GUI menu entry
├── usr/share/pixmaps/
│   └── axonos.svg      # Application icon (generated placeholder)
└── usr/share/doc/axonos-launcher/
    ├── copyright        # License information
    └── changelog.Ubuntu2204.gz  # Package changelog
```

### Dependencies
- **Required**: python3, python3-tk, docker.io | docker-ce
- **Recommended**: firefox | chromium-browser
- **Suggested**: nvidia-container-toolkit

## 🚀 Installation Methods

### Ubuntu 22.04 (.deb package)
```bash
# Install package
sudo dpkg -i AxonOS-Launcher-0.1.0-Linux-amd64.deb
sudo apt-get install -f  # Fix dependencies if needed

# Verify installation
axonos --version
```

### Linux (Manual)
```bash
# Copy binary
sudo cp dist/axonos /usr/local/bin/
sudo chmod +x /usr/local/bin/axonos

# Verify
axonos --version
```

### macOS
```bash
# GUI application (build_launcher.py naming)
cp -r dist/axonos.app /Applications/

# Command line (optional)
sudo cp dist/axonos.app/Contents/MacOS/axonos /usr/local/bin/axonos
```

### Windows
```cmd
# Copy to Program Files
copy dist\axonos.exe "C:\Program Files\AxonOS\"

# Add to PATH (optional)
# Add C:\Program Files\AxonOS to your PATH environment variable
```

## 🧹 Maintenance

### Clean Build Artifacts
```bash
make clean   # from build/: rm -rf dist/ build/ *.spec *.deb RELEASE_INFO.txt
```

**Warning**: `make clean` runs `rm -rf build/`. From inside `build/` this is harmless (there is no
`build/build/`), but if you copy the same command to the repository root it deletes the build
scripts directory. From the repo root clean manually:

```bash
rm -rf dist/ *.spec RELEASE_INFO.txt
rm -f *.deb AxonOS-Launcher-*.zip AxonOS-Launcher-*.dmg
rm -f Dockerfile.macos Dockerfile.windows build/Dockerfile.macos build/Dockerfile.windows
```

`make clean` does not remove the `.zip` / `.dmg` packages, generated `Dockerfile.*` files, or
`dist/BUILD_INFO.txt` / `dist/MACOS_INSTALL.txt` / `dist/WINDOWS_INSTALL.txt`.

### Update Version
The version string `0.1.0` is hardcoded in all of these places; update them together:
- `build/build_deb.py` → `PACKAGE_VERSION`
- `axonos_launcher/main.py` → `version='AxonOS Launcher 0.1.0'`
- `axonos_launcher/cli.py` → `version='AxonOS Launcher CLI 0.1.0'`
- `build/build_macos.py` → `CFBundleVersion`, `CFBundleShortVersionString`, `dmg_name`, final summary
- `build/build_windows.py` → `installer_name`, `zip_name`, final summary
- `build/build_simple_cross.py` → `CFBundleVersion`, `CFBundleShortVersionString`, both `zip_name`s
- `build/build_cross_platform.py` → file names in the final summary
- `build/create_dmg.sh` → `DMG_NAME`
- `build/create_windows_installer.bat` → `INSTALLER_NAME`
- `build/Makefile` → `help` target text

### Rebuild After Changes
```bash
make clean      # from build/
python3 build/build_all.py   # from repo root
```

## 🔧 Make Targets

All targets assume `cd build` first; see the working-directory caveat at the top.

| Target | Description |
|--------|-------------|
| `all` | Build binary and package (default) |
| `binary` | Build binary only (`build_launcher.py`) |
| `package` | Build binary and package |
| `release` | Complete build with release info (`build_all.py`) |
| `deb` | Build .deb package only (requires existing `dist/axonos`; run `binary` first) |
| `dmg` | Build macOS DMG (`build_macos.py`; macOS only) |
| `exe` | Build Windows EXE + ZIP (`build_windows.py`; Windows only) |
| `cross` | Linux-hosted Docker build of ZIP packages (`build_simple_cross.py`; see CROSS_BUILD_FROM_LINUX.md) |
| `install` | Install launcher system-wide (the `.deb` branch looks for `axonos-launcher_*.deb`, which no longer matches the produced name, so it falls through to the binary copy) |
| `uninstall` | Remove launcher from system |
| `clean` | Clean build artifacts |
| `deps` | Install build dependencies (PyInstaller; dpkg-dev/gzip on Linux). Does not install PyYAML. |
| `run` | Run the launcher (binary, or `python3 axonos_launcher/main.py` — path only resolves from the repo root) |
| `help` | Show available targets |

## 🐛 Troubleshooting

### PyInstaller Issues
```bash
# Clear PyInstaller cache
rm -rf ~/.cache/pyinstaller/

# Reinstall PyInstaller
pip3 uninstall pyinstaller
pip3 install pyinstaller
```

### tkinter Not Found
```bash
# Ubuntu 22.04
sudo apt install python3-tk

# CentOS/RHEL
sudo yum install tkinter

# macOS (Homebrew)
brew install python-tk
```

### `ModuleNotFoundError: yaml`
```bash
pip3 install pyyaml
```

### Docker Permission Issues
```bash
# Add user to docker group
sudo usermod -aG docker $USER
# Log out and back in
```

### .deb Build Failures
```bash
# Install packaging tools
sudo apt install dpkg-dev gzip

# Make sure dist/axonos exists first
python3 build/build_launcher.py
```

## 📋 Build Requirements Summary

Sizes and times below are rough estimates, not measured values.

| Platform | Binary Size (est.) | Build Time (est.) | Dependencies |
|----------|-------------|------------|--------------|
| Linux | ~15-20 MB | 30-60s | python3, python3-tk, pyinstaller, pyyaml |
| macOS | ~20-25 MB | 45-75s | python3, pyinstaller, pyyaml |
| Windows | ~20-30 MB | 60-90s | python3, pyinstaller, pyyaml |

## 🎉 Success Indicators

After successful build, you should see:
- ✅ Binary in `dist/` directory, plus `dist/BUILD_INFO.txt`
- ✅ `AxonOS-Launcher-0.1.0-Linux-amd64.deb` in the current directory (Linux)
- ✅ `RELEASE_INFO.txt` with instructions (when using `build_all.py`)
- ✅ No error messages in build output

Test the binary:
```bash
./dist/axonos --version
# Should show: AxonOS Launcher 0.1.0
./dist/axonos          # prints CLI help (CLI mode is the default)
./dist/axonos --gui    # starts the GUI (needs a display)
```

## 📞 Support

If you encounter issues:
1. Check this documentation
2. Clean build artifacts (see Maintenance)
3. Verify prerequisites are installed
4. Check GitHub issues: https://github.com/AxonDAO-AXGT/AxonOS/issues
   (note: `build_all.py` and `build_deb.py` still embed the older `GizmoQuest/AxonOS` URLs)
5. Create new issue with build logs
