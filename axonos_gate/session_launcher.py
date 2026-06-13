"""
Session container launcher adapters.

Mode B architecture: session_manager delegates launch/cleanup to this module so
runtime-specific orchestration (Docker socket, host-side launcher service, etc.)
is configurable without changing scheduler logic.
"""

import json
import logging
import os
import shlex
import subprocess
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

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

logger = logging.getLogger(__name__)


def _truthy(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _container_mode_enabled() -> bool:
    return _truthy("AXGT_USER_CONTAINER_ENABLED", False)


def _container_name_for_session(session_id: int) -> str:
    return f"axgt-session-{session_id}"


def _launcher_mode() -> str:
    return (os.getenv("AXGT_SESSION_LAUNCHER_MODE") or "docker_cli").strip().lower()


def _persistent_storage_enabled() -> bool:
    return _truthy("AXGT_PERSISTENT_STORAGE_ENABLED", True)


def _persistent_storage_volume_prefix() -> str:
    raw = (os.getenv("AXGT_PERSISTENT_STORAGE_VOLUME_PREFIX") or "axgt-user-storage-").strip()
    return "".join(c for c in raw if c.isalnum() or c in ("-", "_"))


def _persistent_storage_mount_path() -> str:
    raw = (os.getenv("AXGT_PERSISTENT_STORAGE_MOUNT_PATH") or "/home/aXonian").strip()
    if not raw.startswith("/") or any(c in raw for c in (" ", "\t", ";", "&", "|", "$", "`")):
        return "/home/aXonian"
    return raw


def launch_session(session_id: int, wallet: str, profile: str, gpu_ids: List[int], template: Optional[str] = None, files_key: Optional[str] = None, ssh_enabled: bool = False, ssh_pubkey: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    """Launch user session runtime; returns (ok, container_id, error)."""
    if not _container_mode_enabled():
        return True, "shared-desktop", None
    mode = _launcher_mode()
    if mode == "http":
        return _launch_via_http(session_id, wallet, profile, gpu_ids, template, files_key, ssh_enabled, ssh_pubkey)
    if mode == "noop":
        # Useful when validating scheduler/queue logic without runtime orchestration.
        return True, _container_name_for_session(session_id), None
    return _launch_via_docker_cli(session_id, wallet, profile, gpu_ids, template, files_key, ssh_enabled, ssh_pubkey)


def stop_session(session_id: int, container_id: Optional[str]) -> None:
    """Cleanup user session runtime resources."""
    if not _container_mode_enabled():
        return
    mode = _launcher_mode()
    if mode == "http":
        _stop_via_http(session_id, container_id)
        return
    if mode == "noop":
        return
    _stop_via_docker_cli(session_id, container_id)


# Per-session port scheme. WebRTC sessions get a UDP block for direct ICE; SSH
# sessions instead get a single published TCP port -> container :22. Both are
# deterministic from session_id so the gate can derive the connect details
# without a round-trip (see session_manager._ssh_port_for_session — keep in sync).
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


def _mode_env_args(session_id: int, ssh_enabled: bool, ssh_pubkey: Optional[str]) -> List[str]:
    """Env that selects the session runtime: headless SSH vs. WebRTC desktop.

    SSH sessions disable the X desktop and WebRTC capture entirely — the
    container becomes a headless GPU shell reachable only over sshd.
    """
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


def _launch_via_docker_cli(session_id: int, wallet: str, profile: str, gpu_ids: List[int], template: Optional[str] = None, files_key: Optional[str] = None, ssh_enabled: bool = False, ssh_pubkey: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    image = (os.getenv("AXGT_SESSION_CONTAINER_IMAGE") or "").strip()
    if not image:
        return False, None, "AXGT_SESSION_CONTAINER_IMAGE is required in docker_cli mode"
    gpu_spec = ",".join(str(i) for i in gpu_ids)
    name = _container_name_for_session(session_id)

    cmd: List[str] = [
        "docker", "run", "-d", "--rm",
        "--name", name,
    ]
    cmd.extend(_publish_args_for_session(session_id, ssh_enabled))
    if _persistent_storage_enabled():
        safe_wallet = "".join(c for c in wallet if c.isalnum() or c in ("-", "_")).lower()
        volume_name = f"{_persistent_storage_volume_prefix()}{safe_wallet}"
        mount_path = _persistent_storage_mount_path()
        cmd.extend(["-v", f"{volume_name}:{mount_path}"])

    cmd.extend([
        "--gpus", docker_run_gpus_device_value(gpu_ids),
        "-e", f"AXGT_SESSION_ID={session_id}",
        "-e", f"AXGT_WALLET_ADDRESS={wallet}",
        "-e", f"AXGT_REQUESTED_PROFILE={profile}",
        "-e", f"AXGT_ASSIGNED_GPU_IDS={gpu_spec}",
    ])
    cmd.extend(_mode_env_args(session_id, ssh_enabled, ssh_pubkey))
    if template:
        cmd.extend(["-e", f"AXONOS_SELECTED_TEMPLATE={template}"])
    if files_key:
        cmd.extend(["-e", f"AXGT_SESSION_FILES_KEY={files_key}"])
    cmd.extend(session_container_ompi_mca_env_flags())
    extra_raw = (os.getenv("AXGT_SESSION_CONTAINER_EXTRA_ARGS") or "").strip()
    if extra_raw:
        cmd.extend(strip_conflicting_gpu_run_flags(shlex.split(extra_raw)))
    cmd.append(image)
    run_cmd = (os.getenv("AXGT_SESSION_CONTAINER_COMMAND") or "").strip()
    if run_cmd:
        cmd.extend(shlex.split(run_cmd))
    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            text=True,
            env=subprocess_env_for_nested_docker(),
        ).strip()
        container_id = out.splitlines()[-1][:64] if out else name
        return True, container_id, None
    except subprocess.CalledProcessError as exc:
        msg = (exc.output or "").strip() or str(exc)
        logger.warning("session_launcher: docker run failed: %s", msg)
        return False, None, msg
    except Exception as exc:
        logger.warning("session_launcher: docker run failed: %s", exc)
        return False, None, str(exc)


def _stop_via_docker_cli(session_id: int, container_id: Optional[str]) -> None:
    target = (container_id or "").strip() or _container_name_for_session(session_id)
    try:
        subprocess.run(
            ["docker", "rm", "-f", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception as exc:
        logger.warning("session_launcher: docker cleanup failed for %s: %s", target, exc)


def _launch_via_http(session_id: int, wallet: str, profile: str, gpu_ids: List[int], template: Optional[str] = None, files_key: Optional[str] = None, ssh_enabled: bool = False, ssh_pubkey: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    base_url = (os.getenv("AXGT_SESSION_LAUNCHER_URL") or "").strip().rstrip("/")
    if not base_url:
        return False, None, "AXGT_SESSION_LAUNCHER_URL is required in http mode"
    payload = {
        "session_id": session_id,
        "wallet_address": wallet,
        "requested_profile": profile,
        "assigned_gpu_ids": gpu_ids,
        "requested_template": template,
        "files_key": files_key,
        "ssh_enabled": ssh_enabled,
        "ssh_pubkey": ssh_pubkey,
    }
    status, data, err = _http_json("POST", f"{base_url}/launch", payload)
    if err:
        return False, None, err
    if status >= 400:
        return False, None, (data.get("error") if isinstance(data, dict) else f"http {status}")
    if not isinstance(data, dict) or not data.get("ok"):
        return False, None, (data.get("error") if isinstance(data, dict) else "launcher rejected request")
    container_id = (data.get("container_id") or _container_name_for_session(session_id))
    return True, str(container_id), None


def _stop_via_http(session_id: int, container_id: Optional[str]) -> None:
    base_url = (os.getenv("AXGT_SESSION_LAUNCHER_URL") or "").strip().rstrip("/")
    if not base_url:
        return
    payload = {"session_id": session_id, "container_id": container_id}
    _http_json("POST", f"{base_url}/stop", payload)


def enumerate_host_gpus_via_http() -> Optional[List[int]]:
    """Ask the session launcher service to probe host GPUs via `docker run --gpus all`.

    Used when the gate container has no GPU passthrough so `nvidia-smi` is unavailable
    locally. Requires AXGT_SESSION_LAUNCHER_MODE=http and a launcher that exposes
    GET /enumerate-gpus (session_launcher_service).
    """
    if (os.getenv("AXGT_SESSION_LAUNCHER_MODE") or "").strip().lower() != "http":
        return None
    if not _truthy("AXGT_GPU_ENUMERATE_VIA_LAUNCHER", True):
        return None
    base_url = (os.getenv("AXGT_SESSION_LAUNCHER_URL") or "").strip().rstrip("/")
    if not base_url:
        return None
    token = (os.getenv("AXGT_SESSION_LAUNCHER_TOKEN") or "").strip()
    timeout_raw = (os.getenv("AXGT_SESSION_LAUNCHER_ENUMERATE_TIMEOUT_SECONDS") or "").strip()
    try:
        timeout_s = float(timeout_raw) if timeout_raw else 90.0
    except ValueError:
        timeout_s = 90.0
    url = f"{base_url}/enumerate-gpus"
    req = urllib.request.Request(url=url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8").strip()
            data = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8").strip()
            logger.warning(
                "session_launcher: enumerate-gpus HTTP %s: %s",
                exc.code,
                raw[:500],
            )
        except Exception:
            logger.warning("session_launcher: enumerate-gpus HTTP %s", exc.code)
        return None
    except Exception as exc:
        logger.warning("session_launcher: enumerate-gpus request failed %s", exc)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    raw_ids = data.get("indices") or data.get("gpu_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return None
    try:
        out = sorted(set(int(float(x)) for x in raw_ids))
    except (TypeError, ValueError):
        return None
    return out if out else None


def _http_json(method: str, url: str, payload: dict) -> Tuple[int, object, Optional[str]]:
    token = (os.getenv("AXGT_SESSION_LAUNCHER_TOKEN") or "").strip()
    timeout_raw = (os.getenv("AXGT_SESSION_LAUNCHER_TIMEOUT_SECONDS") or "").strip()
    try:
        timeout_s = float(timeout_raw) if timeout_raw else 10.0
    except ValueError:
        timeout_s = 10.0
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url=url, method=method, data=body)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8").strip()
            data = json.loads(raw) if raw else {}
            return int(resp.status), data, None
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8").strip()
            data = json.loads(raw) if raw else {"error": f"http {exc.code}"}
        except Exception:
            data = {"error": f"http {exc.code}"}
        return int(exc.code), data, None
    except Exception as exc:
        logger.warning("session_launcher: http call failed %s %s: %s", method, url, exc)
        return 0, {}, str(exc)
