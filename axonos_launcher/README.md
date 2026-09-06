# AxonOS Launcher

A launcher for customizing AxonOS Docker builds. It runs as a CLI by default (see `README_CLI.md`) and offers a Tk GUI with `--gui`. Run it from the repo root so it finds the `Dockerfile` and `axonos_plugins/`.

## Features

- **Application Selection**: Choose which scientific applications to include in your AxonOS build
- **🧩 Custom Applications**: Create and manage your own applications using templates or plugins
- **Template System**: Built-in templates for Python packages, APT packages, GitHub releases, web apps, and custom commands
- **Plugin Support**: Load applications from external YAML/JSON files in the `axonos_plugins/` directory
- **Ollama Model Configuration**: Customize which AI models to install
- **User Settings**: Configure username and VNC password
- **GPU Support**: Enable/disable GPU acceleration with automatic Docker command generation
- **Docker Build Integration**: Generate custom Dockerfiles and build images directly
- **One-Click Deployment**: Deploy and automatically launch AxonOS with the Deploy button
- **Configuration Management**: Save build configurations from the GUI; load them via the CLI (`--config`)

## Usage

### Running the Launcher

```bash
python3 axonos_launcher/main.py --gui
```

Without `--gui` the same entrypoint runs the CLI (`python3 axonos_launcher/main.py list`, etc.).

### Tabs Overview

1. **Applications Tab**
   - Select which applications to install
   - Mandatory: AxonAI, Talk to K, the AxonAI UI font, Python3-pip, and IPFS CLI & Desktop (always included)
   - Optional: All other scientific applications (JupyterLab is now truly optional!)
   - Custom applications marked with 🧩 emoji
   - Use "Select All", "Select None", or "Reset to Defaults" buttons

2. **🧩 Custom Apps Tab**
   - **Your Custom Applications**: View and select custom apps loaded from plugins
   - **Add New Application**: Create new applications using templates:
     - `python_package`: Install via pip
     - `apt_package`: Install system packages
     - `github_release`: Download and install from releases
     - `web_app`: Create browser shortcuts
     - `custom`: Write your own Dockerfile commands
   - **Preview**: Click "🔄 Update Preview" to see the generated Dockerfile section
   - **Save & Load**: "💾 Save Application" writes `axonos_plugins/<app_id>.yaml`; "📁 Load Plugin File" copies a YAML/JSON file in. Both need a launcher restart to take effect
   - **Template Builder**: Form-based creation; checks that App ID/Name/Description are filled and the ID is unique

3. **Settings Tab**
   - Configure Ollama AI models (one per line)
   - Set username and VNC password
   - Enable/disable GPU support for deployment
   - GROMACS CUDA architectures and cuFFTMp toggle
   - Optionally expose direct VNC (5901) and IPFS ports (both off by default)
   - Env file path passed to `docker run --env-file` (default `.env`)
   - Default model: qwen3.8:latest

4. **Build & Deploy Tab**
   - Set custom Docker image tag (default `axonos:custom`; the CLI defaults to `axonos:latest`)
   - Generate customized Dockerfile with essential Qt dependencies
   - Build Docker image with real-time logging
   - **Deploy!** - One-click deployment with automatic web interface launch
   - View GPU-aware deployment commands
   - Save configuration as JSON (loading is CLI-only: `axonos generate --config` / `axonos build --config`)
   - Comprehensive build and deployment logs

### Available Applications

- **JupyterLab** - Interactive development environment for notebooks
- **R & RStudio** - Statistical computing language and IDE
- **Spyder** - Scientific Python IDE
- **UGENE** - Bioinformatics suite
- **GNU Octave** - MATLAB-compatible scientific computing
- **Fiji (ImageJ)** - Image processing and analysis
- **Nextflow** - Workflow management system
- **GROMACS (MPI)** - Molecular dynamics package (release-2026)
- **QGIS & GRASS GIS** - Geographic Information Systems
- **Syncthing** - Continuous file synchronization
- **EtherCalc** - Collaborative spreadsheet (browser-based)
- **BeakerX** - Multi-language kernel extension for JupyterLab
- **NGL Viewer** - Molecular visualization (browser-based)
- **Remix IDE** - Ethereum development environment (browser-based)
- **Nault** - Nano cryptocurrency wallet (browser-based)
- **CellModeller** - Bacterial cell growth simulation

## Building and Deploying Custom AxonOS

### Quick Start (Recommended)
1. Open the launcher: `python3 axonos_launcher/main.py --gui`
2. Select desired applications in the **Applications** tab
3. Configure settings in the **Settings** tab (enable GPU if available)
4. Go to **Build & Deploy** tab
5. Click **"Generate Dockerfile"** to create `Dockerfile.custom` (skip if using defaults)
6. Click **"Build Docker Image"** to build your custom image
7. Click **"Deploy!"** to automatically launch AxonOS and open web interface

### Default Configuration Fast Track
If you want all applications with default settings:
1. Open the launcher (all applications are selected by default)
2. Go directly to **Build & Deploy** tab
3. Click **"Build Docker Image"** - it will automatically use the original `Dockerfile` for maximum speed
4. Click **"Deploy!"** when build completes

The launcher automatically detects when you're using the default configuration and builds directly from the original `Dockerfile`, skipping the custom generation step for faster builds.

### Manual Deployment
After building, you can also run manually:
```bash
# With GPU support
docker run -d --gpus all --env-file .env -p 6080:6080 --name axonos your-custom-tag

# Without GPU
docker run -d --env-file .env -p 6080:6080 --name axonos your-custom-tag
```

## Deploy Button Features

The **Deploy!** button provides one-click deployment with:

- **GPU flag**: Adds `--gpus all` when the GPU checkbox in Settings is enabled
- **Container Management**: Stops and removes existing containers to prevent conflicts
- **Image Validation**: Checks if the Docker image exists before deployment
- **Automatic Launch**: Opens `http://localhost:6080/vnc.html` in your default browser
- **Status Logging**: Real-time deployment status and container information

## Intelligent Build System

The launcher features smart build detection:

- **Default Configuration**: Automatically uses the original `Dockerfile` for maximum speed when all default settings are detected
- **Custom Configuration**: Generates and uses `Dockerfile.custom` when any settings are modified
- **Real-time Detection**: Shows build status and which Dockerfile will be used
- **No Manual Steps**: Skip Dockerfile generation when using defaults - just click "Build Docker Image"

## GPU Support

AxonOS supports GPU acceleration for scientific computing workloads:

### Requirements
- NVIDIA GPU with CUDA support
- NVIDIA Docker runtime installed
- Compatible GPU drivers

### Configuration
1. In the Settings tab, check "Enable GPU support"
2. The launcher will automatically generate Docker commands with `--gpus all` flag
3. GPU-enabled applications (like ML frameworks) will have access to GPU acceleration
4. The Deploy button respects GPU settings automatically

## Access AxonOS

After deployment, access AxonOS through:
- **Web Interface**: http://localhost:6080/vnc.html (automatically opened by Deploy button)
- **Direct VNC**: localhost:5901 (with configured password) — only if "Expose direct VNC port (5901)" was enabled in Settings (off by default)

## Container Management

```bash
# Stop the container
docker stop axonos

# Restart the container
docker start axonos

# Remove the container
docker rm axonos

# View logs
docker logs axonos
```

## Requirements

- Python 3.8+
- PyYAML (`pip install -r axonos_launcher/requirements.txt`)
- tkinter (usually included with Python; GUI mode only)
- Docker (for building images)
- xdg-open, or firefox/chromium-browser/google-chrome on PATH (for automatic web interface launch)

## Troubleshooting

### Qt Platform Plugin Issues
The launcher now automatically includes essential Qt dependencies to fix GUI application errors like:
```
qt.qpa.plugin: Could not load the Qt platform plugin "xcb"
```

### JupyterLab Not Installing
JupyterLab is now truly optional - it will only be included if explicitly selected in the Applications tab.

### GPU Support Not Working
Ensure you have:
- NVIDIA Container Toolkit installed
- Compatible GPU drivers
- GPU support enabled in Settings tab
