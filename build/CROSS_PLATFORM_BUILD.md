# Cross-Platform Build Guide

This guide explains how to build AxonOS Launcher packages natively for Linux, macOS, and Windows.

## ⚠️ Working Directory

All scripts check for `axonos_launcher/main.py` in the current directory and must be run from the
**repository root** as `python3 build/<script>.py`. The `build/Makefile` calls the scripts without a
`build/` prefix, so `cd build && make <target>` finds the script but the script then fails its
repo-root check. The `make` commands below are shown for reference; the direct commands are what
currently work. See BUILD.md for details.

## 🎯 Quick Start

### Linux (Ubuntu 22.04)
```bash
# From the repo root: build the binary, then the .deb (build_deb.py needs dist/axonos)
python3 build/build_launcher.py
python3 build/build_deb.py
# Makefile equivalent (cwd caveat applies): cd build && make binary && make deb
```

### macOS
```bash
python3 build/build_macos.py      # Makefile equivalent: make dmg
```

### Windows
```bash
python build\build_windows.py     # Makefile equivalent: make exe
```

## 📋 Prerequisites

### All Platforms
- Python 3.8 or later
- PyInstaller (auto-installed by `build_launcher.py`, `build_macos.py`, `build_windows.py`)
- PyYAML (`pip install pyyaml`; only `build_windows.py` auto-installs it)
- tkinter (`python3-tk` on Debian/Ubuntu; bundled with python.org installers)

### Linux (Ubuntu 22.04)
- python3-tk
- dpkg-dev
- gzip

### macOS
- Homebrew (`build_macos.py` runs `brew install create-dmg` if `create-dmg` is missing)
- Xcode Command Line Tools

### Windows
- No additional tools required (the ZIP is created with Python's `zipfile`)

## 🔨 Build Process

### 1. Linux (.deb Package)

**Requirements:**
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-tk dpkg-dev gzip
pip3 install pyinstaller pyyaml
```

**Build (repo root):**
```bash
python3 build/build_launcher.py   # -> dist/axonos
python3 build/build_deb.py        # -> AxonOS-Launcher-0.1.0-Linux-amd64.deb
```

**Output:**
- `AxonOS-Launcher-0.1.0-Linux-amd64.deb` - Ubuntu 22.04 package (in the current directory)
- `dist/axonos` - Binary executable
- `dist/BUILD_INFO.txt`

### 2. macOS (.app + DMG)

**Requirements:**
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install create-dmg
pip3 install pyinstaller pyyaml
```

**Build (repo root):**
```bash
python3 build/build_macos.py
```

**Output:**
- `AxonOS-Launcher-0.1.0-macOS-x86_64.dmg` - DMG package (in the current directory; the script's
  final summary line still prints the older name `AxonOS-Launcher-0.1.0-macOS.dmg`)
- `dist/AxonOS Launcher.app` - App bundle
- `dist/MACOS_INSTALL.txt` - Installation instructions

Note: `build/create_dmg.sh` is a separate, older helper that expects `dist/axonos.app` (the
`build_launcher.py` naming) and writes `dist/axonos-launcher-0.1.0-macos.dmg` with `hdiutil`. It is
not used by `build_macos.py` or the Makefile.

### 3. Windows (.exe + ZIP)

**Requirements:**
```bash
pip install pyinstaller pyyaml
```

**Build (repo root):**
```bash
python build\build_windows.py
```

**Output:**
- `AxonOS-Launcher-0.1.0-Windows-x86_64.zip` - ZIP package (the script's final summary still prints
  `AxonOS-Launcher-0.1.0-Windows.zip`)
- `dist/AxonOS Launcher.exe` - Executable
- `dist/WINDOWS_INSTALL.txt` - Installation instructions

**No Windows installer is produced.** `build_windows.py` defines an `installer_name`
(`AxonOS-Launcher-0.1.0-Windows-x86_64.exe`) but never creates it; the `Makefile` help text that
advertises a "native installer" `.exe` is aspirational. `build/create_windows_installer.bat` only
writes a `dist\install_axonos.bat` copy script and expects `dist\axonos.exe` (the `build_launcher.py`
name), not `dist\AxonOS Launcher.exe`.

## 📦 Package Contents

### Linux (.deb)
```
/usr/local/bin/axonos                    # Main executable
/usr/share/applications/axonos-launcher.desktop  # Menu entry
/usr/share/pixmaps/axonos.svg            # Generated placeholder icon
/usr/share/doc/axonos-launcher/          # copyright, changelog.Ubuntu2204.gz
```

### macOS (.app)
```
AxonOS Launcher.app/
├── Contents/
│   ├── MacOS/axonos                     # Main executable
│   ├── Info.plist                        # App metadata (references MyDocument.icns, which does not exist)
│   └── Resources/                        # App resources
```

### Windows (ZIP)
```
AxonOS Launcher.exe                      # Self-contained executable
INSTALL.txt                               # Installation instructions
```

## 🚀 Installation

### Linux
```bash
sudo dpkg -i AxonOS-Launcher-0.1.0-Linux-amd64.deb
sudo apt-get install -f
axonos --version
```

### macOS
1. Double-click the DMG to mount
2. Drag `AxonOS Launcher.app` to the Applications folder
3. Run `/Applications/AxonOS\ Launcher.app/Contents/MacOS/axonos --gui` (the app is CLI-by-default;
   double-clicking runs the windowed binary with no visible CLI output)

### Windows
1. Extract the ZIP file
2. Run `"AxonOS Launcher.exe" --gui` from a command prompt (double-clicking runs CLI mode in a
   windowless build, which shows nothing)

## 🔧 Advanced Build Options

### Custom Icons
Not wired up. Every generated spec sets `icon=None` (`build_launcher.py`, `build_macos.py`,
`build_windows.py`, `build_simple_cross.py`), no script reads icon files from `build/`, and the
`.deb` icon is an inline-generated SVG in `build_deb.py`. Adding real icons requires code changes.

### Code Signing (macOS)
```bash
# Sign the app bundle
codesign --force --deep --sign "Developer ID Application: Your Name" "dist/AxonOS Launcher.app"

# Notarize (requires Apple Developer account; altool was retired in 2023)
xcrun notarytool submit "AxonOS-Launcher-0.1.0-macOS-x86_64.dmg" \
  --apple-id "your-email@example.com" --team-id "TEAMID" --password "@env:APPLE_ID_PASSWORD" --wait
xcrun stapler staple "AxonOS-Launcher-0.1.0-macOS-x86_64.dmg"
```

### Windows Installer (Advanced)
No installer is generated by the build scripts. For a proper installer use NSIS or Inno Setup, e.g.:

**NSIS Example:**
```nsi
!include "MUI2.nsh"

Name "AxonOS Launcher"
OutFile "AxonOS-Launcher-0.1.0-Windows-x86_64-Setup.exe"
InstallDir "$PROGRAMFILES\AxonOS"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

Section "Install"
    SetOutPath "$INSTDIR"
    File "dist\AxonOS Launcher.exe"
    CreateDirectory "$SMPROGRAMS\AxonOS"
    CreateShortCut "$SMPROGRAMS\AxonOS\AxonOS Launcher.lnk" "$INSTDIR\AxonOS Launcher.exe" "--gui"
    CreateShortCut "$DESKTOP\AxonOS Launcher.lnk" "$INSTDIR\AxonOS Launcher.exe" "--gui"
SectionEnd
```

## 🧪 Testing

### Test Installation
```bash
# Linux
axonos --version            # AxonOS Launcher 0.1.0

# macOS
/Applications/AxonOS\ Launcher.app/Contents/MacOS/axonos --version

# Windows
"AxonOS Launcher.exe" --version
```

### Test CLI / GUI modes
```bash
axonos            # CLI help (default mode)
axonos list       # CLI subcommand
axonos --gui      # GUI (requires a display)
```

### Test Auto-clone Feature (GUI mode only)
```bash
# Move AxonOS directory temporarily
mv ~/AxonOS ~/AxonOS_backup

# Run launcher in GUI mode from a directory without a Dockerfile - should clone to ~/axonos
axonos --gui

# Restore directory
mv ~/AxonOS_backup ~/AxonOS
```

## 🐛 Troubleshooting

**PyInstaller Build Failures:**
```bash
rm -rf ~/.cache/pyinstaller/
pip uninstall pyinstaller
pip install pyinstaller
```

**Missing Dependencies:**
```bash
pip install pyinstaller pyyaml
sudo apt install python3-tk      # Debian/Ubuntu
```

**macOS DMG Creation:**
```bash
brew install create-dmg
echo $PATH | grep -q /opt/homebrew/bin
```

**Windows EXE Issues:**
```bash
python --version
# Install Visual C++ Redistributable if needed
```

## 📋 Build Checklist

Before releasing:

- [ ] All platforms build successfully (natively; see CROSS_BUILD_FROM_LINUX.md for what `make cross` really produces)
- [ ] `--version` prints `AxonOS Launcher 0.1.0` on each platform
- [ ] `--gui` launches without errors; CLI `list` works
- [ ] Auto-clone works in GUI mode
- [ ] Installation instructions are clear
- [ ] Package sizes are reasonable
- [ ] Version numbers are consistent across every location listed in BUILD.md "Update Version"

## 🎉 Release Process

1. **Build all platforms (repo root):**
   ```bash
   # Linux
   python3 build/build_launcher.py && python3 build/build_deb.py

   # macOS
   python3 build/build_macos.py

   # Windows
   python build\build_windows.py
   ```

2. **Test packages:**
   - Install on clean systems
   - Test CLI and `--gui`
   - Test auto-clone functionality (GUI)

3. **Create release:**
   - Upload `AxonOS-Launcher-0.1.0-Linux-amd64.deb`, `AxonOS-Launcher-0.1.0-macOS-x86_64.dmg`,
     `AxonOS-Launcher-0.1.0-Windows-x86_64.zip` to GitHub Releases
   - Write release notes
   - Tag the release

---

**AxonOS Launcher** - Cross-platform scientific computing deployment made easy!
