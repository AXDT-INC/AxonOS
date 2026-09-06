# AxonOS Launcher - Installation Guide

## 📦 Package Information

**Package Name**: `AxonOS-Launcher-0.1.0-Linux-amd64.deb`  
**Version**: 0.1.0  
**Architecture**: amd64  
**Size**: varies per build (see `dist/BUILD_INFO.txt`)  
**Build Date**: see `RELEASE_INFO.txt` produced by `make release`

## 🎯 What's Included

This Ubuntu 22.04 package (.deb) contains:

- **AxonOS Launcher Binary**: Self-contained executable (`/usr/local/bin/axonos`)
- **Desktop Integration**: Application menu entry and icon
- **Documentation**: Copyright and changelog files
- **Installation Scripts**: Post-installation setup and dependency checks

## 🚀 Quick Install

```bash
# Install the package
sudo dpkg -i AxonOS-Launcher-0.1.0-Linux-amd64.deb

# Fix any dependency issues (if needed)
sudo apt-get install -f

# Launch the GUI launcher (bare `axonos` runs the CLI and prints help)
axonos --gui
```

### Verify Installation
```bash
# Check if binary is available
which axonos

# Check version
axonos --version
```

## 📋 System Requirements

### Required Dependencies
- **Python 3**: For runtime support
- **Python3-tk**: GUI toolkit
- **Docker**: Container runtime (docker.io or docker-ce)

### Recommended Dependencies
- **Web Browser**: Firefox or Chromium for accessing AxonOS web interface
- **NVIDIA Container Toolkit**: For GPU acceleration (optional)

### Installation Commands
```bash
# Install required dependencies
sudo apt update
sudo apt install -y python3 python3-tk docker.io

# For GPU support (optional)
sudo apt install -y nvidia-container-toolkit
```

## 🔧 Usage

### Launching AxonOS Launcher
```bash
# Command line: GUI
axonos --gui

# Command line: CLI
axonos list

# Or find it in your applications menu
# Applications → Science → AxonOS Launcher
```

### What You Can Do
1. **Select Applications**: Choose which scientific tools to include
2. **Configure AI Models**: Set up Ollama models for AI assistance
3. **Customize Settings**: Set username, password, and GPU options
4. **Build & Deploy**: One-click Docker build and deployment
5. **Access AxonOS**: Automatic browser launch to the scientific desktop

## 🧹 Uninstallation

```bash
# Remove the package
sudo apt remove axonos-launcher

# Or completely purge (removes configuration files)
sudo apt purge axonos-launcher
```

## 📁 Package Contents

```
/usr/local/bin/axonos                    # Main executable
/usr/share/applications/axonos-launcher.desktop  # Menu entry
/usr/share/pixmaps/axonos.svg            # Application icon
/usr/share/doc/axonos-launcher/          # Documentation
├── copyright                             # License information
└── changelog.Ubuntu2204.gz               # Package changelog
```

## 🔍 Troubleshooting

### Common Issues

**1. Docker Permission Issues**
```bash
# Add user to docker group
sudo usermod -aG docker $USER
# Log out and back in for changes to take effect
```

**2. GUI Not Starting**
```bash
# Check if tkinter is available
python3 -c "import tkinter; print('tkinter available')"

# Install if missing
sudo apt install python3-tk
```

**3. Binary Not Found**
```bash
# Check if binary exists
ls -la /usr/local/bin/axonos

# Reinstall if needed
sudo dpkg -i AxonOS-Launcher-0.1.0-Linux-amd64.deb
```

### Logs and Debugging
```bash
# Run with verbose output
axonos --version

# Check system logs
journalctl -u docker.service
```

## 🏗️ Building from Source

For developers who want to build their own package:

```bash
# Clone the repository
git clone https://github.com/AxonDAO-AXGT/AxonOS.git
cd AxonOS

# Set up build environment
python3 -m venv build_env
source build_env/bin/activate
pip install pyinstaller pyyaml

# Install packaging tools
sudo apt install dpkg-dev gzip

# Build everything
cd build
make release
```

## 📞 Support

- **GitHub Issues**: https://github.com/AxonDAO-AXGT/AxonOS/issues
- **Documentation**: https://github.com/AxonDAO-AXGT/AxonOS
- **Community**: Global DeSci movement

## 📄 License

This package is licensed under the MIT License. See the copyright file in `/usr/share/doc/axonos-launcher/copyright` for details.

---

**AxonOS Launcher** - Empowering decentralized scientific computing through intuitive GUI deployment. 