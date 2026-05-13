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
    return shlex.split(raw)


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
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
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

    cmd: List[str] = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
    ]
    shm = _shm_size_for_run()
    if shm:
        cmd.extend(["--shm-size", shm])
    cmd.extend(
        [
            "--gpus",
            f"device={gpu_spec}",
            "-e",
            f"AXGT_SESSION_ID={session_id}",
            "-e",
            f"AXGT_WALLET_ADDRESS={wallet}",
            "-e",
            f"AXGT_REQUESTED_PROFILE={profile}",
            "-e",
            f"AXGT_ASSIGNED_GPU_IDS={gpu_spec}",
        ]
    )

    for env_name in _env_passthrough_names():
        env_value = os.getenv(env_name)
        if env_value is not None:
            cmd.extend(["-e", f"{env_name}={env_value}"])

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
