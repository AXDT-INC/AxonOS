# AxonOS Launcher CLI

Command-line interface for building and deploying AxonOS on headless GPU servers.

## Installation

Run the CLI from the AxonOS repo root, either from source or as the built binary:

```bash
# From source (no install step)
python3 axonos_launcher/cli.py list

# Or build the standalone binary and put it on PATH
python3 build/build_launcher.py && sudo cp dist/axonos /usr/local/bin/
```

The examples below assume `axonos` is on PATH (or `alias axonos='python3 axonos_launcher/cli.py'`).
Requires Python 3.8+ and PyYAML (`pip install -r axonos_launcher/requirements.txt`).

## Quick Start

```bash
# List available applications
axonos list

# Generate Dockerfile (with the default configuration no file is written; the stock Dockerfile is used)
axonos generate

# Build Docker image
axonos build --password "$AXONOS_VNC_PASSWORD"

# Build and deploy with GPU support
axonos build --password "$AXONOS_VNC_PASSWORD" && axonos deploy --gpu
```

## Commands

### List Applications

```bash
axonos list
```

Lists all available applications with their status (enabled/disabled).

### Generate Dockerfile

```bash
# Generate with default configuration
axonos generate

# Generate with custom config file
axonos generate --config my-config.json

# Specify output file (default Dockerfile.custom)
axonos generate --output Dockerfile.custom

# GROMACS build knobs
axonos generate --cuda-archs "70;86;89" --no-gmx-cufftmp   # or --gmx-cufftmp
```

Short flags: `-c/--config`, `-o/--output`.

### Build Image

```bash
# Build with default settings
axonos build

# Build with custom password
axonos build --password mySecurePassword

# Build with custom image tag
axonos build --image axonos:custom --password myPassword

# Build using specific Dockerfile
axonos build --dockerfile Dockerfile.custom

# Build with config file (note: the image tag still comes from --image, default axonos:latest)
axonos build --config my-config.json --password myPassword

# GROMACS build knobs
axonos build --cuda-archs "70;86;89" --gmx-cufftmp
```

Short flags: `-i/--image` (default `axonos:latest`), `-f/--dockerfile`, `-c/--config`, `-p/--password`.

### Deploy Container

```bash
# Deploy with GPU support (recommended)
axonos deploy --gpu

# Deploy without GPU
axonos deploy

# Deploy with custom image and container name
axonos deploy --image axonos:custom --name my-axonos --gpu

# Also publish direct VNC (5901) and/or IPFS ports
axonos deploy --gpu --expose-vnc --expose-ipfs

# Use a specific env file (default .env)
axonos deploy --gpu --env-file .env.prod
```

By default only port 6080 is published. `--ports-only` is kept for backward compatibility and is a no-op (it just forces `--expose-vnc`/`--expose-ipfs` off). Short flags: `-i/--image` (default `axonos:latest`), `-n/--name` (default `axonos`).

### Configuration Management

```bash
# Save current configuration (default file: axonos-config.json)
axonos config save --file my-config.json

# Validate a configuration file (loads it into a transient process; nothing is persisted)
axonos config load --file my-config.json
```

Saved configurations are applied with `generate --config` / `build --config`.

## Configuration File Format

```json
{
  "applications": {
    "jupyterlab": true,
    "r_rstudio": true,
    "spyder": false,
    "ugene": true
  },
  "ollama_models": [
    "qwen3.8:latest"
  ],
  "username": "aXonian",
  "password": "REPLACE_WITH_STRONG_PASSWORD",
  "image_tag": "axonos:latest",
  "gpu_enabled": true,
  "gmx_cuda_archs": "70;75;86;89",
  "gmx_use_cufftmp": true
}
```

## Examples

### Full Workflow

```bash
# 1. List applications to see what's available
axonos list

# 2. Generate Dockerfile (optional, uses defaults if skipped)
axonos generate

# 3. Build image
axonos build --password mySecurePassword123

# 4. Deploy with GPU
axonos deploy --gpu
```

### Using Configuration Files

```bash
# Create config file (edit manually or use config save)
axonos config save --file production-config.json

# Edit production-config.json to customize

# Generate Dockerfile from config
axonos generate --config production-config.json

# Build from config
axonos build --config production-config.json --password myPassword

# Deploy
axonos deploy --gpu
```

### Headless Server Deployment

```bash
# On GPU server without display
ssh user@gpu-server

# Clone and setup
git clone https://github.com/AxonDAO-AXGT/AxonOS.git
cd AxonOS

# Build image
axonos build --password secure_password

# Deploy with GPU
axonos deploy --gpu

# Access via web browser from your machine
# http://gpu-server-ip:6080/vnc.html
```

## Ports

Default deployment publishes only:
- **6080**: noVNC web interface

`--expose-vnc` adds:
- **5901**: Direct VNC access

`--expose-ipfs` adds:
- **4001**: IPFS swarm (TCP and UDP)
- **5001**: IPFS API
- **8080**: IPFS Gateway
- **9090**: IPFS Web UI

## GPU Support

GPU support requires:
- NVIDIA GPU with CUDA support
- NVIDIA Container Toolkit installed
- Docker configured for GPU access

Enable with `--gpu` flag on deploy command.

## Troubleshooting

### "Dockerfile not found"
Run from the AxonOS root directory where Dockerfile is located.

### "Docker image not found"
Build the image first with `axonos build`.

### "Permission denied" on Docker
Add your user to docker group: `sudo usermod -aG docker $USER`

### GPU not working
- Verify NVIDIA drivers: `nvidia-smi`
- Check Docker GPU support: `docker run --rm --gpus all nvidia/cuda:12.2.2-base-ubuntu22.04 nvidia-smi`
- Ensure `--gpu` flag is used on deploy
