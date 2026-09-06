# Cross-Platform ZIP Packaging from Linux (Linux binaries)

This guide explains what `build/build_simple_cross.py` (`make cross`) actually does: it builds the
launcher with PyInstaller inside a `python:3.12-slim` Docker container and packages the result into
two ZIP files named for macOS and Windows.

**Important**: PyInstaller cannot cross-compile. Both ZIPs contain a **Linux x86_64 ELF binary**.
No macOS `.app`/DMG and no Windows PE `.exe` is produced. The macOS ZIP is not runnable on macOS
(macOS has no ELF loader); the Windows ZIP is only runnable under WSL. For real macOS/Windows
packages, build natively with `python3 build/build_macos.py` on macOS and
`python3 build/build_windows.py` on Windows (see CROSS_PLATFORM_BUILD.md).

## 🎯 Quick Start

```bash
# From the repository root
python3 build/build_simple_cross.py

# Or via make (this is the one Makefile target that works from build/,
# because the script detects ../axonos_launcher/main.py and changes directory itself)
cd build
make cross
```

There are no command-line arguments; the script always builds both targets in order
(`macos`, then `windows`).

## 📋 Requirements

- **Linux host** (Ubuntu 22.04 recommended)
- **Docker** installed and running, and accessible by the current user
- **Python 3.8+** on the host (the script itself uses `subprocess.run(capture_output=...)`)

Nothing else is needed on the host: PyInstaller and PyYAML are installed inside the Docker image.

## 🔧 Setup

### Install Docker
```bash
# Ubuntu 22.04
sudo apt update
sudo apt install -y docker.io
sudo usermod -aG docker $USER

# Start Docker
sudo systemctl start docker
sudo systemctl enable docker

# Log out and back in for group changes
```

### Verify Setup
```bash
docker --version
docker run hello-world
python3 --version
```

## 📦 Output Files

After a successful run (all files in the repository root):

### "macOS" package
- `AxonOS-Launcher-0.1.0-macOS-x86_64.zip`
- Contains: `AxonOS Launcher` (Linux ELF, no `.app` bundle) + `INSTALL.txt`

### "Windows" package
- `AxonOS-Launcher-0.1.0-Windows-x86_64.zip`
- Contains: `AxonOS Launcher.exe` (Linux ELF renamed with `.exe`, run under WSL) + `INSTALL.txt`

### Build artifacts in `dist/`
- `dist/axonos` — output of the macOS-spec build (`EXE name='axonos'`)
- `dist/AxonOS Launcher.app/` — PyInstaller `BUNDLE` directory produced on Linux (not a usable app)
- `dist/AxonOS Launcher` — output of the Windows-spec build (`EXE name='AxonOS Launcher'`)

Known issue: `create_package()` reads `dist/axonos` for **both** ZIPs, so the Windows ZIP actually
contains the macOS-spec binary and `dist/AxonOS Launcher` is never packaged. The two binaries are
functionally identical (same source, same host), so the ZIP contents still work under WSL.

## 🔍 How It Works

1. Writes `axonos_launcher_<target>.spec` and `Dockerfile.<target>` in the repo root
2. Builds image `axonos-<target>-builder` from `python:3.12-slim` (same image for both targets),
   installing `binutils`, `pyinstaller`, `pyyaml`, and `COPY . .` of the source tree
3. Runs `pyinstaller --clean --noconfirm <spec>` in the container with only `dist/` bind-mounted
   back to the host (source is copied into the image, not mounted)
4. Deletes the spec, Dockerfile and image, then zips `dist/axonos` with an `INSTALL.txt`

## ⚠️ Limitations

- Output is a Linux binary in every case; nothing is cross-compiled
- Not signed, not notarized, no installer
- UPX compression is requested in the spec but `upx` is not installed in the image, so it is skipped

### Recommended Approach
For releases, build natively on each platform (run from the repo root):
- **macOS**: `python3 build/build_macos.py` (or `make dmg`, subject to the Makefile cwd caveat)
- **Windows**: `python3 build/build_windows.py` (or `make exe`)
- **Linux**: `python3 build/build_launcher.py && python3 build/build_deb.py` (or `make binary deb`)

### Deprecated: `build/build_cross_platform.py`
A second Docker-based script exists that tries to run `build_macos.py` and `build_windows.py` inside
Linux containers. Both of those scripts exit immediately when not running on macOS/Windows, so
`build_cross_platform.py` cannot succeed and should be considered non-functional/deprecated. Its
final summary also uses stale artifact names.

## 🧪 Testing

```bash
# Clean previous outputs (make clean does NOT remove the .zip files)
rm -f AxonOS-Launcher-*.zip
rm -rf dist/

python3 build/build_simple_cross.py

ls -la AxonOS-Launcher-*.zip
ls -la dist/
file dist/axonos     # ELF 64-bit LSB executable, x86-64
```

Inspect a package:
```bash
unzip -l AxonOS-Launcher-0.1.0-Windows-x86_64.zip
```

## 🐛 Troubleshooting

### Docker Issues
```bash
sudo systemctl status docker
sudo systemctl restart docker
groups $USER | grep docker
```

### Build Failures
```bash
# Reclaim space from dangling builder images/layers
docker system prune -a
df -h
```
Containers are started with `--rm`, so there are no container logs to inspect after a failure;
the PyInstaller output is printed directly to your terminal.

### Memory Issues
If PyInstaller is killed inside the container, free host memory or run the container manually with
a higher `--memory` limit; there is no Docker-daemon-wide memory setting on Linux.

## 📋 Build Checklist

- [ ] Docker is running and accessible
- [ ] Both targets report `✓ Success`
- [ ] Both `-x86_64.zip` files exist in the repo root
- [ ] `INSTALL.txt` is present in each ZIP
- [ ] Testers understand these are Linux binaries (WSL for Windows; not runnable on macOS)

## 🎉 Success Indicators

```
🌍 AxonOS Launcher Cross-Platform Build (Linux)
==================================================

Building for macos...
✓ macos build completed
✓ Created AxonOS-Launcher-0.1.0-macOS-x86_64.zip

Building for windows...
✓ windows build completed
✓ Created AxonOS-Launcher-0.1.0-Windows-x86_64.zip

==================================================
Build Summary:
  macos: ✓ Success
  windows: ✓ Success

🎉 Cross-platform build completed!
Note: Cross-compiled builds may have limitations.
For best results, build on the target platform.
```

### Generated Files
```
AxonOS-Launcher-0.1.0-macOS-x86_64.zip     # Linux ELF + INSTALL.txt
AxonOS-Launcher-0.1.0-Windows-x86_64.zip   # Linux ELF (.exe name) + INSTALL.txt
dist/                                       # Build artifacts (see above)
```
