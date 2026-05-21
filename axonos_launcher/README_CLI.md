# AxonOS Launcher CLI

Command-line interface for building and deploying AxonOS on headless GPU servers.

## Quick Start

```bash
# List available applications
axonos list

# Generate Dockerfile (default configuration)
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

# Specify output file
axonos generate --output Dockerfile.custom
```

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

# Build with config file
axonos build --config my-config.json --password myPassword
```

### Deploy Container

```bash
# Deploy with GPU support (recommended)
axonos deploy --gpu

# Deploy without GPU
axonos deploy

# Deploy with custom image and container name
axonos deploy --image axonos:custom --name my-axonos --gpu

# Deploy with VNC ports only (no IPFS)
axonos deploy --ports-only
```

### Configuration Management

```bash
# Save current configuration
axonos config save --file my-config.json

# Load configuration
axonos config load --file my-config.json
```

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
    "gemma4:31b",
    "granite3.2-vision"
  ],
  "username": "aXonian",
  "password": "REPLACE_WITH_STRONG_PASSWORD",
  "image_tag": "axonos:latest",
  "gpu_enabled": true
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
cd axonos

# Build with GPU support
axonos build --password secure_password

# Deploy with GPU
axonos deploy --gpu

# Access via web browser from your machine
# http://gpu-server-ip:6080/vnc.html
```

## Ports

Default deployment exposes:
- **6080**: noVNC web interface
- **5901**: Direct VNC access
- **4001**: IPFS swarm (TCP and UDP)
- **5001**: IPFS API
- **8080**: IPFS Gateway
- **9090**: IPFS Web UI

Use `--ports-only` to only expose VNC ports (6080, 5901).

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
- Check Docker GPU support: `docker run --rm --gpus all nvidia/cuda:11.0-base nvidia-smi`
- Ensure `--gpu` flag is used on deploy
