# AxonOS Docker Build Guide

## Quick Start

### Using the Build Script (Recommended)

```bash
# Build with custom password
./scripts/build_axonos.sh "$AXONOS_VNC_PASSWORD"

# Build with custom password and tag
./scripts/build_axonos.sh "$AXONOS_VNC_PASSWORD" axonos:latest
```

### Manual Build

```bash
# Build with custom password (recommended)
docker build --build-arg PASSWORD="$AXONOS_VNC_PASSWORD" -t axonos:latest .

# Build with default password (testing only)
docker build -t axonos:latest .
```

## Build Requirements

- **Docker**: 20.10 or later
- **Disk Space**: ~10GB free space for the image
- **RAM**: 4GB+ recommended
- **Network**: Stable internet connection (downloads ~5-8GB of packages and models)
- **Time**: 15-30 minutes depending on system and network speed

## Build Process

The build process includes:

1. **Base Image**: NVIDIA CUDA 12.2 on Ubuntu 22.04 (jammy)
2. **System Packages**: XFCE4, VNC, noVNC, scientific tools
3. **Ollama Installation**: AI model server
4. **Model Downloads**: 
   - granite3-guardian (safety model)
   - gemma4:31b (text model)
   - granite3.2-vision (vision model)
5. **Scientific Applications**: JupyterLab, RStudio, Spyder, UGENE, etc.
6. **AxonOS Assistant**: Custom AI assistant application
7. **Theme Installation**: AxonOS noVNC theme

## Build Output

After successful build, you'll have:
- **Image**: `axonos:latest` (or your specified tag)
- **Size**: ~8-12GB (depending on included applications)
- **Layers**: ~51 build steps

## Verifying the Build

```bash
# Check image was created
docker images axonos

# Inspect image details
docker inspect axonos:latest

# Check image size
docker images axonos --format "{{.Repository}}:{{.Tag}} - {{.Size}}"
```

## Running the Built Image

### With GPU Support (Recommended)

```bash
docker run -d --gpus all -p 6080:6080 -p 5901:5901 \
  -p 4001:4001 -p 4001:4001/udp -p 5001:5001 -p 8080:8080 -p 9090:9090 \
  --name axonos axonos:latest
```

### Without GPU

```bash
docker run -d -p 6080:6080 -p 5901:5901 \
  -p 4001:4001 -p 4001:4001/udp -p 5001:5001 -p 8080:8080 -p 9090:9090 \
  --name axonos axonos:latest
```

## Troubleshooting

### Build Fails with "No space left on device"

```bash
# Clean up Docker
docker system prune -a

# Check disk space
df -h
```

### Build Fails During Package Installation

- Check internet connection
- Retry the build (Docker caches layers)
- Check if Ubuntu 22.04 repositories are accessible

### Build Fails During Model Download

- Ollama model downloads can be slow
- Check network connection
- Models will be retried automatically

### Password Warning

The warning about `ARG PASSWORD` is informational. The password is used during build to set VNC password and is not stored in the final image layers.

## Build Optimization

### Using BuildKit Cache

```bash
DOCKER_BUILDKIT=1 docker build --build-arg PASSWORD="$AXONOS_VNC_PASSWORD" -t axonos:latest .
```

### Building Specific Stages

The Dockerfile doesn't use multi-stage builds, but you can optimize by:
- Reusing cached layers
- Building on a system with good network connection
- Using a local Docker registry for base images

## Custom Builds

For custom builds with selected applications, use the AxonOS Launcher:

```bash
python3 axonos_launcher/main.py
```

The launcher will generate a `Dockerfile.custom` with your selected applications.

## Next Steps

After building:

1. **Test the container**: Run and verify it starts
2. **Access the web interface**: http://localhost:6080/vnc.html
3. **Verify branding**: Check that AxonOS branding appears correctly
4. **Test applications**: Launch AxonOS Assistant and other tools

## Build Logs

Build logs are saved to `/tmp/axonos_build.log` when using the build script, or you can redirect manually:

```bash
docker build --build-arg PASSWORD="$AXONOS_VNC_PASSWORD" -t axonos:latest . 2>&1 | tee build.log
```
