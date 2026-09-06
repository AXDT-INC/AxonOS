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
4. **Model Download**: qwen3.8:latest (multimodal model)
5. **OpenCode Installation**: Version 1.18.26, pinned to the server API used by AxonAI
6. **Scientific Applications**: JupyterLab, RStudio, Spyder, UGENE, etc.
7. **AxonAI**: GTK research agent with persistent sessions, live events, approvals, and screenshot attachments
8. **Theme Installation**: AxonOS noVNC theme

## Build Output

After successful build, you'll have:
- **Image**: `axonos:latest` (or your specified tag)
- **Size**: ~8-12GB (depending on included applications)
- **Layers**: ~130 build steps

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

These commands run the image standalone (single-container legacy mode: desktop + gate in one container). For the multi-tenant stack use `docker compose up -d --build` instead — see [`HOST_LAUNCHER.md`](HOST_LAUNCHER.md).

### With GPU Support (Recommended, secure by default)

```bash
docker run -d --gpus all --env-file .env -p 6080:6080 -p 8889:8889 \
  --name axonos axonos:latest
```

Only the web UI (`6080`) and gate API (`8889`) are published. `.env` supplies the runtime configuration ([`ENVIRONMENT_VARIABLES.md`](ENVIRONMENT_VARIABLES.md)); set `AXGT_USER_CONTAINER_ENABLED=false` for standalone mode so the desktop starts in this container.

### Advanced: also publish VNC and IPFS ports

Only when host-side access to raw VNC or IPFS is explicitly required:

```bash
docker run -d --gpus all --env-file .env -p 6080:6080 -p 8889:8889 \
  -p 5901:5901 \
  -p 4001:4001 -p 4001:4001/udp -p 5001:5001 -p 8080:8080 -p 9090:9090 \
  --name axonos axonos:latest
```

### Without GPU

Drop `--gpus all` from either command above. WebRTC NVENC capture and GPU workloads are unavailable; the noVNC path still works in standalone mode.

## AxonAI Runtime

Supervisor starts Ollama and the pinned OpenCode 1.18.26 server with the container. OpenCode listens only on `127.0.0.1:4096` inside the container and uses the local `qwen3.8:latest` model through Ollama.

AxonAI keeps one OpenCode session per conversation. It can use tools and approved subagents, streams live activity into the chat, asks before protected actions, and can attach a freshly captured desktop screenshot to visual turns. **Stop** aborts and reconciles active work, and a successor cannot dispatch until terminal runner state is proven. An atomic `/run/axonos-assistant/opencode-active` marker extends that fence across GTK crashes and multiple windows; `startup.sh` clears it only at the full container/session boundary. If cleanup proof fails, AxonAI therefore requires an AxonOS session/container restart instead of risking overlap with a detached tool process. **Reset** deletes the current OpenCode session only after safe cleanup and begins a clean conversation. The supervised service uses a root-owned executable and policy at `/etc/axonos-opencode/opencode.json`, isolated config/home paths, disabled project discovery, and only a root-owned shell-environment plugin. Local files therefore cannot weaken its approval rules, while approved shell tools still see the desktop user's normal HOME/XDG/display. This isolation applies only to AxonAI's backend; OpenCode launched from a terminal remains independently configurable.

Agentic mode is enabled by default; disabling it in **Settings** changes the default to direct chat. The following prompt prefixes override automatic routing:

- `/agent <request>` — use the OpenCode agent even when direct chat is the default
- `/chat <request>` — use direct local chat without agent tools
- `/vision <request>` — capture and attach the current desktop screenshot

After the container starts, verify both local AI services:

```bash
docker exec axonos curl -fsS http://127.0.0.1:4096/global/health
docker exec axonos curl -fsS http://127.0.0.1:11434/api/tags
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
- The `ollama pull` runs once inside its layer; a failed pull fails that layer. Re-run the build — every earlier layer is cached, so only the model download repeats.

### AxonAI Reports OpenCode Is Unavailable

```bash
# Check the supervised local services
docker exec axonos supervisorctl status opencode ollama

# Verify the loopback-only OpenCode API
docker exec axonos curl -fsS http://127.0.0.1:4096/global/health

# Review startup or runtime errors
docker logs axonos
```

OpenCode is intentionally not exposed as a Docker host port. If Ollama is healthy but OpenCode is not, confirm the image was rebuilt after the OpenCode 1.18.26 integration was added.

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

For custom builds with selected applications, use the AxonOS Launcher. The GUI (`main.py`) needs a display (tkinter); the CLI works headless:

```bash
# GUI
python3 axonos_launcher/main.py

# Headless: generate, then build
python3 axonos_launcher/cli.py generate --output Dockerfile.custom
python3 axonos_launcher/cli.py build
```

Both generate a `Dockerfile.custom` with your selected applications. See [`axonos_launcher/README_CLI.md`](../axonos_launcher/README_CLI.md).

## Next Steps

After building:

1. **Test the container**: Run and verify it starts
2. **Access the web interface**: http://localhost:6080/vnc.html
3. **Verify branding**: Check that AxonOS branding appears correctly
4. **Test AxonAI**: Launch AxonAI, try `/agent` and `/vision`, approve a harmless tool request, then verify **Stop** and **Reset**
5. **Test other applications**: Launch the scientific tools needed by your workflow

## Build Logs

The build script prints to the terminal and does not write a log file. Capture one yourself:

```bash
./scripts/build_axonos.sh "$AXONOS_VNC_PASSWORD" 2>&1 | tee build.log
# or, for a manual build
docker build --build-arg PASSWORD="$AXONOS_VNC_PASSWORD" -t axonos:latest . 2>&1 | tee build.log
```
