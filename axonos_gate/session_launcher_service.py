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
import time
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


def _persistent_storage_enabled() -> bool:
    raw = (os.getenv("AXGT_PERSISTENT_STORAGE_ENABLED") or "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def _persistent_storage_volume_prefix() -> str:
    raw = (os.getenv("AXGT_PERSISTENT_STORAGE_VOLUME_PREFIX") or "axgt-user-storage-").strip()
    return "".join(c for c in raw if c.isalnum() or c in ("-", "_"))


def _persistent_storage_mount_path() -> str:
    raw = (os.getenv("AXGT_PERSISTENT_STORAGE_MOUNT_PATH") or "/home/aXonian").strip()
    if not raw.startswith("/") or any(c in raw for c in (" ", "\t", ";", "&", "|", "$", "`")):
        return "/home/aXonian"
    return raw


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


# Per-session port scheme — must match axonos_gate/session_launcher.py and
# session_manager._ssh_port_for_session so the gate-advertised connect-string
# matches the port actually published here.
_WEBRTC_BASE_PORT = 40000
_WEBRTC_BLOCK_SIZE = 10
_SSH_BASE_PORT = 42000
_MAX_SESSIONS = 50


def _webrtc_port_range(session_id: int) -> str:
    start_port = _WEBRTC_BASE_PORT + (session_id % _MAX_SESSIONS) * _WEBRTC_BLOCK_SIZE
    end_port = start_port + _WEBRTC_BLOCK_SIZE - 1
    return f"{start_port}-{end_port}"


def _ssh_port(session_id: int) -> int:
    return _SSH_BASE_PORT + (session_id % _MAX_SESSIONS)


def _publish_args_for_session(session_id: int, ssh_enabled: bool) -> List[str]:
    if ssh_enabled:
        return ["-p", f"{_ssh_port(session_id)}:22/tcp"]
    port_range = _webrtc_port_range(session_id)
    return ["-p", f"{port_range}:{port_range}/udp"]


def _mode_env_args(session_id: int, ssh_enabled: bool, ssh_pubkey: str) -> List[str]:
    """Runtime-selecting env: headless SSH shell vs. WebRTC desktop."""
    if ssh_enabled:
        args = [
            "-e", "AXGT_DESKTOP_ENABLED=false",
            "-e", "WEBRTC_AGENT_ENABLED=false",
            "-e", "AXGT_SSH_ENABLED=true",
        ]
        if ssh_pubkey:
            args.extend(["-e", f"AXGT_SSH_PUBKEY={ssh_pubkey}"])
        return args
    return [
        "-e", "AXGT_DESKTOP_ENABLED=true",
        "-e", "WEBRTC_AGENT_ENABLED=true",
        "-e", f"WEBRTC_PORT_RANGE={_webrtc_port_range(session_id)}",
    ]


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

    ssh_enabled = bool(payload.get("ssh_enabled"))
    ssh_pubkey = str(payload.get("ssh_pubkey") or "").strip()

    cmd: List[str] = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
    ]
    cmd.extend(_publish_args_for_session(session_id, ssh_enabled))
    if _persistent_storage_enabled():
        safe_wallet = "".join(c for c in wallet if c.isalnum() or c in ("-", "_")).lower()
        volume_name = f"{_persistent_storage_volume_prefix()}{safe_wallet}"
        mount_path = _persistent_storage_mount_path()
        cmd.extend(["-v", f"{volume_name}:{mount_path}"])

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
        ]
    )
    cmd.extend(_mode_env_args(session_id, ssh_enabled, ssh_pubkey))

    requested_template = str(payload.get("requested_template") or "").strip()
    if requested_template:
        cmd.extend(["-e", f"AXONOS_SELECTED_TEMPLATE={requested_template}"])

    files_key = str(payload.get("files_key") or "").strip()
    if files_key:
        cmd.extend(["-e", f"AXGT_SESSION_FILES_KEY={files_key}"])

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


def _get_volume_size_kb(volume_name: str) -> float:
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{volume_name}:/volume-data",
        "alpine", "du", "-s", "/volume-data"
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=15).strip()
        parts = out.split()
        if parts:
            return float(parts[0])
    except Exception as exc:
        logger.warning("Failed to get volume size for %s: %s", volume_name, exc)
    return 0.0


def _run_volume_cleanup() -> None:
    db_url = os.getenv("AXGT_CHALLENGE_DB_URL")
    if not db_url:
        logger.warning("Auto volume prune: AXGT_CHALLENGE_DB_URL is not set. Skipping.")
        return

    try:
        import psycopg2
    except ImportError:
        logger.warning("Auto volume prune: psycopg2 is not installed. Skipping.")
        return

    prefix = _persistent_storage_volume_prefix()
    try:
        out = subprocess.check_output(
            ["docker", "volume", "ls", "--filter", f"name={prefix}", "--format", "{{.Name}}"],
            stderr=subprocess.STDOUT,
            text=True
        ).strip()
        volume_names = [line.strip() for line in out.splitlines() if line.strip()]
    except Exception as exc:
        logger.warning("Auto volume prune: Failed to list local docker volumes: %s", exc)
        return

    if not volume_names:
        return

    conn = None
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SELECT wallet_address, remaining_minutes, updated_at FROM axgt_deposits")
            rows = cur.fetchall()
            db_wallets = {
                "".join(c for c in r[0] if c.isalnum() or c in ("-", "_")).lower(): {
                    "original": r[0],
                    "remaining": float(r[1]),
                    "updated_at": float(r[2])
                } for r in rows
            }
    except Exception as exc:
        logger.warning("Auto volume prune: Database query failed: %s", exc)
        return
    finally:
        if conn and conn.closed == 0:
            conn.close()

    cost_per_gb_hour_raw = os.getenv("AXGT_PERSISTENT_STORAGE_GB_HOUR_COST_MINUTES")
    try:
        cost_per_gb_hour = float(cost_per_gb_hour_raw) if cost_per_gb_hour_raw else 0.05
    except ValueError:
        cost_per_gb_hour = 0.05

    interval_raw = os.getenv("AXGT_PERSISTENT_STORAGE_CLEANUP_INTERVAL_SECONDS")
    try:
        interval = float(interval_raw) if interval_raw else 3600.0
    except ValueError:
        interval = 3600.0

    min_balance_limit_raw = os.getenv("AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES")
    try:
        min_balance_limit = float(min_balance_limit_raw) if min_balance_limit_raw else -1440.0
    except ValueError:
        min_balance_limit = -1440.0

    now = time.time()

    conn = None
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        with conn.cursor() as cur:
            for volume_name in volume_names:
                safe_wallet = volume_name[len(prefix):]
                if safe_wallet not in db_wallets:
                    continue

                wallet_info = db_wallets[safe_wallet]
                original_wallet = wallet_info["original"]
                remaining = wallet_info["remaining"]

                if remaining < min_balance_limit:
                    logger.info("Auto volume prune: Pruning volume %s due to balance (%s) exceeding debt limit (%s)", volume_name, remaining, min_balance_limit)
                    rm_res = subprocess.run(["docker", "volume", "rm", volume_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    if rm_res.returncode != 0:
                        logger.warning("Auto volume prune: Failed to remove volume %s: %s", volume_name, rm_res.stderr.strip())
                    continue

                size_kb = _get_volume_size_kb(volume_name)
                size_gb = size_kb / (1024.0 * 1024.0)
                charge = size_gb * cost_per_gb_hour * (interval / 3600.0)

                if charge > 0:
                    new_remaining = remaining - charge
                    cur.execute(
                        "UPDATE axgt_deposits SET remaining_minutes = %s, updated_at = %s WHERE wallet_address = %s",
                        (new_remaining, now, original_wallet)
                    )
                    cur.execute(
                        """
                        INSERT INTO axgt_ledger (wallet_address, event_type, minutes_delta, axgt_delta, balance_after_minutes, notes, created_at, created_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (original_wallet, "usage_deduction", -charge, 0, new_remaining, f"Offline storage charge for volume size: {size_gb:.4f} GB", now, "volume_billing_daemon")
                    )
                    logger.info("Auto volume prune: Charged %s for %s offline storage: %s remaining minutes", original_wallet, f"{size_gb:.4f} GB", new_remaining)

                    if new_remaining < min_balance_limit:
                        logger.info("Auto volume prune: Pruning volume %s after charge pushed balance (%s) below debt limit (%s)", volume_name, new_remaining, min_balance_limit)
                        rm_res = subprocess.run(["docker", "volume", "rm", volume_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        if rm_res.returncode != 0:
                            logger.warning("Auto volume prune: Failed to remove volume %s: %s", volume_name, rm_res.stderr.strip())

        conn.commit()
    except Exception as exc:
        if conn:
            conn.rollback()
        logger.warning("Auto volume prune/billing sweep failed: %s", exc)
    finally:
        if conn:
            conn.close()


def _prune_inactive_volumes_loop() -> None:
    # Wait for the service to warm up
    time.sleep(30)
    import time as time_mod
    while True:
        try:
            if _persistent_storage_enabled():
                _run_volume_cleanup()
        except Exception as exc:
            logger.warning("Auto volume prune loop encountered error: %s", exc)
        
        interval_raw = os.getenv("AXGT_PERSISTENT_STORAGE_CLEANUP_INTERVAL_SECONDS")
        try:
            interval = int(interval_raw) if interval_raw else 3600
        except ValueError:
            interval = 3600
        time_mod.sleep(max(60, interval))


def main():
    host = (os.getenv("AXGT_SESSION_LAUNCHER_BIND_HOST") or "127.0.0.1").strip()
    port_raw = (os.getenv("AXGT_SESSION_LAUNCHER_BIND_PORT") or "8090").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 8090

    if _persistent_storage_enabled():
        import threading
        t = threading.Thread(target=_prune_inactive_volumes_loop, daemon=True)
        t.start()
        logger.info("Started automatic volume pruning background thread")

    logger.info("starting host launcher on %s:%s", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
