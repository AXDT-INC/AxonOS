#!/usr/bin/env python3
"""
Host-side launcher service for AxonOS public-beta sessions.

Run this service on the Docker host (not inside the AxonOS gate container) and
configure gate/session_manager to use:

  AXGT_SESSION_LAUNCHER_MODE=http
  AXGT_SESSION_LAUNCHER_URL=http://<host>:8090
  AXGT_SESSION_LAUNCHER_TOKEN=<shared-secret>
"""

import json
import logging
import os
import shlex
import subprocess
from typing import Dict, List, Optional, Tuple

from flask import Flask, jsonify, request

try:
    from .docker_gpu_cli import (
        docker_run_gpus_device_value,
        session_container_ompi_mca_env_flags,
        subprocess_env_for_nested_docker,
        strip_conflicting_gpu_run_flags,
    )
except ImportError:
    try:
        from axonos_gate.docker_gpu_cli import (
            docker_run_gpus_device_value,
            session_container_ompi_mca_env_flags,
            subprocess_env_for_nested_docker,
            strip_conflicting_gpu_run_flags,
        )
    except ImportError:
        from docker_gpu_cli import (
            docker_run_gpus_device_value,
            session_container_ompi_mca_env_flags,
            subprocess_env_for_nested_docker,
            strip_conflicting_gpu_run_flags,
        )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
app = Flask(__name__)


def _require_token() -> Optional[Tuple[object, int]]:
    expected = (os.getenv("AXGT_SESSION_LAUNCHER_TOKEN") or "").strip()
    if not expected:
        # Explicitly allow no-token mode for local development only.
        return None
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.startswith("Bearer "):
        return jsonify({"ok": False, "error": "missing bearer token"}), 401
    token = auth[len("Bearer ") :].strip()
    if token != expected:
        return jsonify({"ok": False, "error": "invalid bearer token"}), 401
    return None


def _container_name(session_id: int) -> str:
    return f"axgt-session-{session_id}"


def _image_name() -> str:
    return (os.getenv("AXGT_HOST_SESSION_CONTAINER_IMAGE") or "").strip()


def _default_command_tokens() -> List[str]:
    raw = (os.getenv("AXGT_HOST_SESSION_CONTAINER_COMMAND") or "").strip()
    if not raw:
        return []
    return shlex.split(raw)


def _extra_args_tokens() -> List[str]:
    raw = (os.getenv("AXGT_HOST_SESSION_CONTAINER_EXTRA_ARGS") or "").strip()
    if not raw:
        return []
    return strip_conflicting_gpu_run_flags(shlex.split(raw))


def _enumerate_image_name() -> str:
    """Image that includes `nvidia-smi` on PATH; reuse session desktop image by default."""
    for key in ("AXGT_LAUNCHER_GPU_ENUMERATE_IMAGE", "AXGT_HOST_SESSION_CONTAINER_IMAGE"):
        img = (os.getenv(key) or "").strip()
        if img:
            return img
    return ""


def _parse_nvidia_index_csv(text: str) -> List[int]:
    ids: List[int] = []
    for line in (text or "").splitlines():
        part = line.strip().split(",")[0].strip()
        if not part:
            continue
        try:
            ids.append(int(float(part)))
        except ValueError:
            continue
    return sorted(set(ids))


def _shm_size_for_run() -> Optional[str]:
    """
    Docker default /dev/shm is tiny; GLX and many GPU apps need more (matches main axonos shm_size).
    Unset env -> 32g. Explicit empty string -> omit --shm-size (not recommended).
    """
    key = "AXGT_HOST_SESSION_CONTAINER_SHM_SIZE"
    if key not in os.environ:
        return "32g"
    raw = (os.getenv(key) or "").strip()
    return raw or None


def _network_name() -> str:
    return (os.getenv("AXGT_HOST_SESSION_CONTAINER_NETWORK") or "").strip()


def _env_passthrough_names() -> List[str]:
    raw = (os.getenv("AXGT_HOST_SESSION_ENV_PASSTHROUGH") or "").strip()
    if not raw:
        return []
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def _run_cmd(cmd: List[str]) -> Tuple[bool, str]:
    env = subprocess_env_for_nested_docker()
    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        ).strip()
        return True, out
    except subprocess.CalledProcessError as exc:
        return False, (exc.output or "").strip() or str(exc)
    except Exception as exc:
        return False, str(exc)


def _stop_container_by_name(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _build_launch_cmd(payload: Dict[str, object]) -> Tuple[Optional[List[str]], Optional[str]]:
    image = _image_name()
    if not image:
        return None, "AXGT_HOST_SESSION_CONTAINER_IMAGE is required"

    session_id = int(payload.get("session_id"))
    wallet = str(payload.get("wallet_address") or "").strip().lower()
    profile = str(payload.get("requested_profile") or "small").strip().lower()
    assigned_gpu_ids = payload.get("assigned_gpu_ids") or []

    if not wallet:
        return None, "wallet_address is required"
    if not isinstance(assigned_gpu_ids, list) or not assigned_gpu_ids:
        return None, "assigned_gpu_ids must be a non-empty list"
    try:
        gpu_ids = [int(v) for v in assigned_gpu_ids]
    except (TypeError, ValueError):
        return None, "assigned_gpu_ids must contain integers"

    gpu_spec = ",".join(str(i) for i in gpu_ids)
    name = _container_name(session_id)

    # Calculate unique UDP port range for WebRTC ICE direct connection (Path B)
    base_port = 40000
    block_size = 10
    max_sessions = 50
    start_port = base_port + (session_id % max_sessions) * block_size
    end_port = start_port + block_size - 1
    port_range = f"{start_port}-{end_port}"

    cmd: List[str] = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-p",
        f"{port_range}:{port_range}/udp",
    ]
    shm = _shm_size_for_run()
    if shm:
        cmd.extend(["--shm-size", shm])
    cmd.extend(
        [
            "--gpus",
            docker_run_gpus_device_value(gpu_ids),
            "-e",
            f"AXGT_SESSION_ID={session_id}",
            "-e",
            f"AXGT_WALLET_ADDRESS={wallet}",
            "-e",
            f"AXGT_REQUESTED_PROFILE={profile}",
            "-e",
            f"AXGT_ASSIGNED_GPU_IDS={gpu_spec}",
            "-e",
            "AXGT_DESKTOP_ENABLED=true",
            "-e",
            "WEBRTC_AGENT_ENABLED=true",
            "-e",
            f"WEBRTC_PORT_RANGE={port_range}",
        ]
    )

    for env_name in _env_passthrough_names():
        if env_name in ("AXGT_DESKTOP_ENABLED", "WEBRTC_AGENT_ENABLED"):
            continue
        env_value = os.getenv(env_name)
        if env_value is not None:
            cmd.extend(["-e", f"{env_name}={env_value}"])

    cmd.extend(session_container_ompi_mca_env_flags())

    network = _network_name()
    if network:
        cmd.extend(["--network", network])

    cmd.extend(_extra_args_tokens())
    cmd.append(image)
    cmd.extend(_default_command_tokens())
    return cmd, None


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"ok": True})


@app.route("/enumerate-gpus", methods=["GET"])
def enumerate_gpus():
    """Run a one-shot privileged container so the gate (GPU-less) can size the host pool."""
    auth_err = _require_token()
    if auth_err:
        return auth_err
    image = _enumerate_image_name()
    if not image:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Set AXGT_HOST_SESSION_CONTAINER_IMAGE or AXGT_LAUNCHER_GPU_ENUMERATE_IMAGE",
                }
            ),
            503,
        )
    cmd = [
        "docker",
        "run",
        "--rm",
        "--gpus",
        "all",
        "--entrypoint",
        "nvidia-smi",
        image,
        "--query-gpu=index",
        "--format=csv,noheader,nounits",
    ]
    ok, out = _run_cmd(cmd)
    if not ok:
        logger.warning("launcher: enumerate-gpus failed: %s", out[:800] if out else "")
        return jsonify({"ok": False, "error": out or "docker run enumerate failed"}), 500
    indices = _parse_nvidia_index_csv(out)
    if not indices:
        return jsonify({"ok": False, "error": "nvidia-smi returned no GPUs", "raw": out}), 500
    logger.info("launcher: enumerated %d GPU(s): %s", len(indices), indices)
    return jsonify({"ok": True, "indices": indices})


@app.route("/launch", methods=["POST"])
def launch():
    auth_err = _require_token()
    if auth_err:
        return auth_err
    payload = request.get_json(silent=True) or {}
    required = ("session_id", "wallet_address", "assigned_gpu_ids")
    missing = [k for k in required if k not in payload]
    if missing:
        return jsonify({"ok": False, "error": f"missing required fields: {', '.join(missing)}"}), 400

    try:
        session_id = int(payload.get("session_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "session_id must be an integer"}), 400
    name = _container_name(session_id)
    _stop_container_by_name(name)

    cmd, build_err = _build_launch_cmd(payload)
    if build_err:
        return jsonify({"ok": False, "error": build_err}), 400

    ok, out = _run_cmd(cmd)
    if not ok:
        logger.warning("launcher: launch failed for %s: %s", name, out)
        return jsonify({"ok": False, "error": out}), 500

    container_id = (out.splitlines()[-1] if out else "").strip()[:64] or name
    logger.info("launcher: started %s -> %s", name, container_id[:12])
    return jsonify({"ok": True, "container_id": container_id, "container_name": name})


@app.route("/stop", methods=["POST"])
def stop():
    auth_err = _require_token()
    if auth_err:
        return auth_err
    payload = request.get_json(silent=True) or {}
    if "session_id" not in payload:
        return jsonify({"ok": False, "error": "session_id is required"}), 400
    try:
        session_id = int(payload.get("session_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "session_id must be an integer"}), 400

    container_id = str(payload.get("container_id") or "").strip()
    target = container_id or _container_name(session_id)
    subprocess.run(["docker", "rm", "-f", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    logger.info("launcher: stopped session=%s target=%s", session_id, target)
    return jsonify({"ok": True, "stopped": target})


@app.route("/list-containers", methods=["GET"])
def list_containers():
    auth_err = _require_token()
    if auth_err:
        return auth_err
    ok, out = _run_cmd([
        "docker", "ps",
        "--filter", "name=axgt-session",
        "--format", "{{.Names}}\t{{.ID}}\t{{.Status}}\t{{.CreatedAt}}"
    ])
    if not ok:
        return jsonify({"ok": False, "error": out}), 500
    containers = []
    for line in (out or "").strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 4:
            containers.append({
                "name": parts[0],
                "short_id": parts[1][:12],
                "status": parts[2],
                "created_at": parts[3],
            })
    return jsonify({"ok": True, "containers": containers})


def main():
    host = (os.getenv("AXGT_SESSION_LAUNCHER_BIND_HOST") or "127.0.0.1").strip()
    port_raw = (os.getenv("AXGT_SESSION_LAUNCHER_BIND_PORT") or "8090").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 8090
    logger.info("starting host launcher on %s:%s", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
