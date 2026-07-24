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
import secrets
import shlex
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

from flask import Flask, jsonify, request

try:
    from .docker_gpu_cli import (
        SESSION_MEDIA_ENV_NAMES,
        docker_run_gpus_device_value,
        session_runtime_config_digest,
        session_container_ompi_mca_env_flags,
        subprocess_env_for_nested_docker,
        strip_conflicting_gpu_run_flags,
        strip_unsafe_session_run_flags,
    )
except ImportError:
    try:
        from axonos_gate.docker_gpu_cli import (
            SESSION_MEDIA_ENV_NAMES,
            docker_run_gpus_device_value,
            session_runtime_config_digest,
            session_container_ompi_mca_env_flags,
            subprocess_env_for_nested_docker,
            strip_conflicting_gpu_run_flags,
            strip_unsafe_session_run_flags,
        )
    except ImportError:
        from docker_gpu_cli import (
            SESSION_MEDIA_ENV_NAMES,
            docker_run_gpus_device_value,
            session_runtime_config_digest,
            session_container_ompi_mca_env_flags,
            subprocess_env_for_nested_docker,
            strip_conflicting_gpu_run_flags,
            strip_unsafe_session_run_flags,
        )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
app = Flask(__name__)

_FORBIDDEN_SESSION_ENV_NAMES = {
    "AXGT_ADMIN_SECRET",
    "AXGT_CHALLENGE_DB_URL",
    "AXGT_CONTRACT_ADDRESS",
    "AXGT_GATE_HEARTBEAT_URL",
    "AXGT_REVENUE_WALLET",
    "AXGT_RPC_URL",
    "AXGT_SESSION_FILES_KEY",
    "AXGT_SESSION_ID",
    "AXGT_SESSION_LAUNCHER_TOKEN",
    "AXGT_WALLET_ADDRESS",
    "AXGT_WEBRTC_AGENT_TOKEN",
    "CDP_API_KEY_ID",
    "CDP_API_KEY_SECRET",
    "DATABASE_URL",
    "PGDATABASE",
    "PGHOST",
    "PGPASSWORD",
    "PGPORT",
    "PGUSER",
    "WEBRTC_AGENT_INTERNAL_KEY",
    "WEBRTC_GATE_INTERNAL_URL",
    "X402_SETTLEMENT_PRIVATE_KEY",
    "USDC_CONTRACT_ADDRESS",
    "USDC_RPC_URL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
}

# Keep inherited tenant configuration deliberately narrow. Every value here is
# required by the in-container media agent and is safe to share across sessions;
# identity and control-plane values are injected explicitly per session instead.
_ALLOWED_SESSION_PASSTHROUGH_NAMES = set(SESSION_MEDIA_ENV_NAMES)
_session_operation_locks: Dict[int, threading.RLock] = {}
_session_operation_locks_guard = threading.Lock()


def _session_operation_lock(session_id: int) -> threading.RLock:
    """Serialize lifecycle mutations for one deterministic tenant runtime."""
    sid = int(session_id)
    with _session_operation_locks_guard:
        return _session_operation_locks.setdefault(sid, threading.RLock())


def _db_connect_timeout_seconds() -> int:
    raw = (os.getenv("AXGT_SESSION_DB_CONNECT_TIMEOUT_SECONDS") or "5").strip()
    try:
        return max(1, min(30, int(raw)))
    except ValueError:
        return 5


def _paused_session_max_seconds() -> int:
    raw = (os.getenv("AXGT_SESSION_PAUSED_MAX_MINUTES") or "").strip()
    try:
        minutes = int(raw) if raw else 120
        if minutes > 0:
            return minutes * 60
    except ValueError:
        pass
    return 120 * 60


def _session_grace_seconds() -> int:
    raw = (os.getenv("AXGT_SESSION_GRACE_SECONDS") or "").strip()
    try:
        value = int(raw) if raw else 60
        if value >= 0:
            return value
    except ValueError:
        pass
    return 60


def _require_token() -> Optional[Tuple[object, int]]:
    expected = (os.getenv("AXGT_SESSION_LAUNCHER_TOKEN") or "").strip()
    if not expected:
        # Explicitly allow no-token mode for local development only.
        return None
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.startswith("Bearer "):
        return jsonify({"ok": False, "error": "missing bearer token"}), 401
    token = auth[len("Bearer ") :].strip()
    if not secrets.compare_digest(token, expected):
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
    tokens = strip_conflicting_gpu_run_flags(shlex.split(raw))
    safe = strip_unsafe_session_run_flags(tokens)
    if len(safe) != len(tokens):
        logger.warning(
            "Ignored session-container extra args that could bypass launcher isolation"
        )
    return safe


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


def _isolated_networks_enabled() -> bool:
    raw = (os.getenv("AXGT_HOST_SESSION_NETWORK_ISOLATION") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _isolated_session_network_name(session_id: int) -> str:
    """Deterministic private network name, independent of current mode."""
    return f"axgt-session-net-{int(session_id)}"


def _session_network_name(session_id: int) -> str:
    if _isolated_networks_enabled():
        return _isolated_session_network_name(session_id)
    return (os.getenv("AXGT_HOST_SESSION_CONTAINER_NETWORK") or "").strip()


def _central_gate_container() -> str:
    raw = (os.getenv("AXGT_HOST_CENTRAL_GATE_CONTAINER") or "axonos").strip()
    return raw if raw and all(c.isalnum() or c in "-_." for c in raw) else "axonos"


def _central_gate_network_alias() -> str:
    """Collision-proof DNS name used only inside isolated tenant networks."""
    return "axonos-gate"


def _inspect_central_gate_container_id() -> Tuple[str, Optional[str], str]:
    ok, output = _run_cmd(
        ["docker", "inspect", "--format", "{{.Id}}", _central_gate_container()]
    )
    if not ok:
        if _docker_object_is_absent(output):
            return "absent", None, ""
        return "error", None, output or "could not inspect central gate container"
    container_id = output.strip()
    if not container_id:
        return "error", None, "central gate inspection omitted its id"
    return "present", container_id[:64], ""


def _env_passthrough_names() -> List[str]:
    raw = (os.getenv("AXGT_HOST_SESSION_ENV_PASSTHROUGH") or "").strip()
    if not raw:
        return []
    requested = [tok.strip() for tok in raw.split(",") if tok.strip()]
    rejected = sorted(
        {
            name
            for name in requested
            if name in _FORBIDDEN_SESSION_ENV_NAMES
            or name not in _ALLOWED_SESSION_PASSTHROUGH_NAMES
        }
    )
    if rejected:
        logger.warning(
            "Refusing non-media/control-plane env passthrough to tenant sessions: %s",
            ", ".join(rejected),
        )
    return [
        name
        for name in requested
        if name in _ALLOWED_SESSION_PASSTHROUGH_NAMES
        and name not in _FORBIDDEN_SESSION_ENV_NAMES
    ]


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
        "-e", "WEBRTC_ENABLED=true",
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


def _session_network_label_value(session_id: int) -> str:
    return f"true|{int(session_id)}"


def _docker_object_is_absent(output: str) -> bool:
    message = str(output or "").strip().lower()
    return any(
        marker in message
        for marker in (
            "no such object",
            "no such container",
            "no such network",
        )
    )


def _docker_network_is_absent(output: str, network: str) -> bool:
    message = str(output or "").strip().lower()
    expected = str(network or "").strip().lower()
    return _docker_object_is_absent(output) or (
        bool(expected) and f"network {expected} not found" in message
    )


def _inspect_session_network_contract(
    session_id: int,
) -> Tuple[str, Dict[str, str], str]:
    """Return ``owned``/``absent``/``unmanaged``/``error`` plus endpoints."""
    network = _isolated_session_network_name(session_id)
    ok, output = _run_cmd(
        [
            "docker",
            "network",
            "inspect",
            "--format",
            '{{ index .Labels "com.axonos.session-network" }}|'
            '{{ index .Labels "com.axonos.session-id" }}|{{json .Containers}}',
            network,
        ]
    )
    if not ok:
        if _docker_network_is_absent(output, network):
            return "absent", {}, ""
        return "error", {}, output or "could not inspect session network"
    parts = output.strip().split("|", 2)
    if len(parts) != 3:
        return "error", {}, "malformed session network inspection"
    if f"{parts[0].strip().lower()}|{parts[1].strip()}" != _session_network_label_value(
        session_id
    ):
        return "unmanaged", {}, "refusing unmanaged or mismatched session network"
    try:
        containers = json.loads(parts[2])
    except (TypeError, json.JSONDecodeError):
        return "error", {}, "malformed session network endpoint inspection"
    if not isinstance(containers, dict):
        return "error", {}, "malformed session network endpoint inspection"
    endpoints: Dict[str, str] = {}
    for raw_id, value in containers.items():
        container_id = str(raw_id or "").strip()
        name = (
            str(value.get("Name") or "").strip()
            if isinstance(value, dict)
            else ""
        )
        if not container_id or not name or name in endpoints:
            return "error", {}, "malformed session network endpoint inspection"
        endpoints[name] = container_id
    return "owned", endpoints, ""


def _validate_session_network_endpoints(
    session_id: int,
    endpoints: Dict[str, str],
    *,
    allow_tenant: bool,
    expected_tenant_id: Optional[str],
) -> Tuple[bool, str]:
    allowed = {_central_gate_container()}
    if allow_tenant:
        allowed.add(_container_name(session_id))
    unexpected = set(endpoints) - allowed
    if unexpected:
        return (
            False,
            "refusing session network with unexpected endpoint(s): "
            + ", ".join(sorted(unexpected)),
        )
    if allow_tenant:
        tenant_name = _container_name(session_id)
        actual_tenant_id = endpoints.get(tenant_name)
        expected_id = str(expected_tenant_id or "").strip()
        if actual_tenant_id and (
            not expected_id
            or not secrets.compare_digest(actual_tenant_id, expected_id)
        ):
            return False, "session network tenant endpoint identity mismatch"
    return True, ""


def _ensure_session_network(
    session_id: int,
    *,
    allow_tenant: bool = False,
    expected_tenant_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """Create a tenant-isolated bridge and enforce its endpoint allowlist."""
    if not _isolated_networks_enabled():
        return True, ""
    network = _isolated_session_network_name(session_id)
    state, endpoints, error = _inspect_session_network_contract(session_id)
    if state == "error":
        return False, error
    if state == "unmanaged":
        return False, error
    if state == "owned":
        valid, endpoint_error = _validate_session_network_endpoints(
            session_id,
            endpoints,
            allow_tenant=allow_tenant,
            expected_tenant_id=expected_tenant_id,
        )
        if not valid:
            return False, endpoint_error
    else:
        ok, out = _run_cmd(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--label",
                "com.axonos.session-network=true",
                "--label",
                f"com.axonos.session-id={int(session_id)}",
                network,
            ]
        )
        if not ok:
            return False, out or "could not create isolated session network"

    ok, out = _run_cmd(
        [
            "docker",
            "network",
            "connect",
            "--alias",
            _central_gate_network_alias(),
            network,
            _central_gate_container(),
        ]
    )
    if not ok and not any(
        marker in out.lower() for marker in ("already exists", "already connected")
    ):
        return False, out or "could not attach central gate to session network"

    state, endpoints, error = _inspect_session_network_contract(session_id)
    if state != "owned":
        return False, error or "session network disappeared during setup"
    valid, endpoint_error = _validate_session_network_endpoints(
        session_id,
        endpoints,
        allow_tenant=allow_tenant,
        expected_tenant_id=expected_tenant_id,
    )
    if not valid:
        return False, endpoint_error
    expected_endpoints = {_central_gate_container()}
    if allow_tenant:
        expected_endpoints.add(_container_name(session_id))
    if set(endpoints) != expected_endpoints:
        return False, "session network is missing an expected endpoint"
    return True, ""


def _cleanup_session_network(session_id: int) -> Tuple[bool, str]:
    """Remove the exact owned private network, even after switching modes."""
    state, endpoints, error = _inspect_session_network_contract(session_id)
    if state == "absent":
        return True, ""
    if state != "owned":
        return False, error or "refusing to clean an unowned session network"
    allowed = {_central_gate_container()}
    unexpected = set(endpoints) - allowed
    central_name = _central_gate_container()
    network = _isolated_session_network_name(session_id)
    central_endpoint_id = endpoints.get(central_name)
    if central_endpoint_id:
        central_state, central_id, central_error = _inspect_central_gate_container_id()
        if (
            central_state != "present"
            or not central_id
            or not secrets.compare_digest(central_endpoint_id, central_id)
        ):
            return False, central_error or "session network central endpoint identity mismatch"
        ok, output = _run_cmd(
            ["docker", "network", "disconnect", "-f", network, central_name]
        )
        if not ok and not _docker_object_is_absent(output) and "not connected" not in output.lower():
            return False, output or "could not disconnect central gate from session network"
    if unexpected:
        return (
            False,
            "refusing cleanup of session network with unexpected endpoint(s): "
            + ", ".join(sorted(unexpected)),
        )
    ok, output = _run_cmd(["docker", "network", "rm", network])
    if ok or _docker_network_is_absent(output, network):
        return True, ""
    return False, output or "could not remove session network"


def _managed_container_id(
    session_id: int,
    *,
    require_running: bool,
) -> Optional[str]:
    """Return the ID only for our exactly labeled deterministic container."""
    state, container_id, _error = _inspect_managed_container_ownership(session_id)
    if state not in ("owned_running", "owned_stopped"):
        return None
    if require_running and state != "owned_running":
        return None
    return container_id


def _inspect_managed_container_ownership(
    session_id: int,
) -> Tuple[str, Optional[str], str]:
    """Classify ownership without conflating Docker errors with absence."""
    name = _container_name(session_id)
    ok, output = _run_cmd(
        [
            "docker",
            "inspect",
            "--format",
            '{{.State.Running}}|{{index .Config.Labels "com.axonos.session-container"}}|'
            '{{index .Config.Labels "com.axonos.session-id"}}|'
            '{{index .Config.Labels "com.axonos.session-config-sha256"}}|{{.Id}}',
            name,
        ]
    )
    if not ok:
        if _docker_object_is_absent(output):
            return "absent", None, ""
        return "error", None, output or "could not inspect session container"
    parts = output.strip().split("|", 4)
    if len(parts) != 5:
        return "error", None, "malformed session container inspection"
    running = parts[0].strip().lower()
    config_digest = parts[3].strip()
    container_id = parts[4].strip()
    if not container_id:
        return "error", None, "session container inspection omitted its id"
    container_id = container_id[:64]
    valid_digest = len(config_digest) == 64 and all(
        character in "0123456789abcdef" for character in config_digest
    )
    if (
        parts[1].strip().lower() != "true"
        or parts[2].strip() != str(int(session_id))
        or not valid_digest
    ):
        return "unmanaged", container_id, "refusing unowned same-name session container"
    if running == "true":
        return "owned_running", container_id, ""
    if running == "false":
        return "owned_stopped", container_id, ""
    return "error", container_id, "invalid session container running state"


def _managed_running_container_id(session_id: int) -> Optional[str]:
    return _managed_container_id(session_id, require_running=True)


def _runtime_contract_for_payload(payload: Dict[str, object]) -> Tuple[str, str]:
    network = _session_network_name(int(payload.get("session_id")))
    digest = session_runtime_config_digest(
        session_id=int(payload.get("session_id")),
        wallet=str(payload.get("wallet_address") or ""),
        profile=str(payload.get("requested_profile") or "small"),
        gpu_ids=[int(value) for value in (payload.get("assigned_gpu_ids") or [])],
        files_key=str(payload.get("files_key") or ""),
        ssh_enabled=bool(payload.get("ssh_enabled")),
        network_name=network,
        image_name=_image_name(),
        requested_template=str(payload.get("requested_template") or ""),
        ssh_pubkey=str(payload.get("ssh_pubkey") or ""),
    )
    return digest, network


def _managed_container_runtime_matches(
    session_id: int,
    expected_digest: str,
    expected_network: str,
) -> bool:
    """Compatibility wrapper around the atomic launch-contract inspection."""
    state, _container_id, _error = _inspect_managed_container_contract(
        session_id,
        expected_digest,
        expected_network,
    )
    return state == "match_running"


def _inspect_managed_container_contract(
    session_id: int,
    expected_digest: str,
    expected_network: str,
) -> Tuple[str, Optional[str], str]:
    """Atomically classify a deterministic tenant container.

    Docker/JSON uncertainty is deliberately distinct from absence and a
    definite owned-contract mismatch. Only the latter two states authorize a
    replacement; an inspection error or unowned same-name container is never
    mutated.
    """
    ok, output = _run_cmd(
        [
            "docker",
            "inspect",
            "--format",
            '{{.State.Running}}|{{index .Config.Labels "com.axonos.session-container"}}|'
            '{{index .Config.Labels "com.axonos.session-id"}}|'
            '{{index .Config.Labels "com.axonos.session-config-sha256"}}|'
            '{{json .NetworkSettings.Networks}}|{{.Id}}',
            _container_name(session_id),
        ]
    )
    if not ok:
        if _docker_object_is_absent(output):
            return "absent", None, ""
        return "error", None, output or "could not inspect session container"
    parts = output.split("|", 5)
    if len(parts) != 6:
        return "error", None, "malformed session container inspection"
    running, managed, labeled_id, digest, networks_json, container_id = (
        part.strip() for part in parts
    )
    container_id = container_id[:64]
    if not container_id:
        return "error", None, "session container inspection omitted its id"
    if managed.lower() != "true" or labeled_id != str(int(session_id)):
        return "unmanaged", container_id, "refusing unowned same-name session container"
    if running.lower() not in ("true", "false"):
        return "error", container_id, "invalid session container running state"
    try:
        networks = json.loads(networks_json)
    except (TypeError, json.JSONDecodeError):
        return "error", container_id, "malformed session container network inspection"
    if not isinstance(networks, dict):
        return "error", container_id, "malformed session container network inspection"
    runtime_matches = secrets.compare_digest(digest, expected_digest) and set(
        networks
    ) == {expected_network}
    if not runtime_matches:
        return "mismatch", container_id, "session container runtime contract mismatch"
    if running.lower() == "true":
        return "match_running", container_id, ""
    return "match_stopped", container_id, ""


def _launch_row_authorized(payload: Dict[str, object]) -> Tuple[bool, str]:
    """Authorize launch input against the exact live scheduler allocation."""
    db_url = (os.getenv("AXGT_CHALLENGE_DB_URL") or "").strip()
    if not db_url:
        return False, "launcher control database is not configured"
    try:
        session_id = int(payload.get("session_id"))
        wallet = str(payload.get("wallet_address") or "").strip().lower()
        requested_gpus = sorted(int(value) for value in (payload.get("assigned_gpu_ids") or []))
        files_key = str(payload.get("files_key") or "").strip()
        ssh_enabled = bool(payload.get("ssh_enabled"))
        requested_profile = str(payload.get("requested_profile") or "small").strip().lower()
    except (TypeError, ValueError):
        return False, "invalid launch allocation identity"
    conn = None
    try:
        import psycopg2

        conn = psycopg2.connect(
            db_url,
            connect_timeout=_db_connect_timeout_seconds(),
        )
        with conn.cursor() as cur:
            cur.execute(
                """SELECT gpu_ids, files_key, ssh_enabled, requested_profile
                   FROM axgt_sessions
                   WHERE id = %s
                     AND wallet_address = %s
                     AND status = 'active'
                     AND allocation_status IN ('allocating', 'allocated')
                     AND expires_at > %s
                   LIMIT 1""",
                (session_id, wallet, time.time()),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.warning("Could not authorize session %s launch: %s", session_id, exc)
        return False, "launcher could not authorize allocation"
    finally:
        if conn is not None:
            conn.close()
    if not row:
        return False, "allocation is no longer active"
    try:
        stored_gpus = sorted(
            int(value.strip())
            for value in str(row[0] or "").split(",")
            if value.strip()
        )
    except ValueError:
        return False, "allocation GPU identity is invalid"
    stored_key = row[1] if isinstance(row[1], str) else ""
    if (
        stored_gpus != requested_gpus
        or not stored_key
        or not files_key
        or not secrets.compare_digest(stored_key, files_key)
        or bool(row[2]) != ssh_enabled
        or str(row[3] or "small").strip().lower() != requested_profile
    ):
        return False, "launch allocation identity mismatch"
    return True, ""


def _reconcile_session_networks() -> None:
    """Reattach a recreated central gate only to live DB-backed tenants."""
    if not _isolated_networks_enabled():
        return
    unmanaged = _unmanaged_session_container_names()
    if unmanaged is None:
        logger.warning("Skipping network reconciliation: container preflight failed")
        return
    if unmanaged:
        logger.error(
            "Skipping network reconciliation until legacy/unmanaged containers are drained: %s",
            ", ".join(unmanaged),
        )
        return
    ok, output = _run_cmd(
        [
            "docker",
            "network",
            "ls",
            "--filter",
            "label=com.axonos.session-network=true",
            "--format",
            '{{.Name}}\t{{.Label "com.axonos.session-id"}}',
        ]
    )
    if not ok:
        logger.warning("Could not enumerate managed session networks: %s", output)
        return
    managed: List[Tuple[str, int]] = []
    for line in output.splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        session_id = int(parts[1])
        network = parts[0]
        if session_id <= 0 or network != _session_network_name(session_id):
            continue
        managed.append((network, session_id))
    if not managed:
        return

    db_url = (os.getenv("AXGT_CHALLENGE_DB_URL") or "").strip()
    if not db_url:
        logger.warning("Cannot reconcile session networks without the control database")
        return
    conn = None
    try:
        import psycopg2

        conn = psycopg2.connect(
            db_url,
            connect_timeout=_db_connect_timeout_seconds(),
        )
        now = time.time()
        paused_cutoff = now - _paused_session_max_seconds()
        grace_seconds = _session_grace_seconds()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM axgt_sessions
                   WHERE allocation_status IN ('allocating', 'allocated')
                     AND (hard_expires_at IS NULL OR hard_expires_at + %s > %s)
                     AND (
                         (status = 'active' AND expires_at > %s)
                         OR (status = 'paused' AND last_heartbeat >= %s)
                     )""",
                (grace_seconds, now, now, paused_cutoff),
            )
            authorized_ids = {int(row[0]) for row in (cur.fetchall() or [])}
    except Exception as exc:
        logger.warning("Could not authorize managed session networks: %s", exc)
        return
    finally:
        if conn is not None:
            conn.close()

    for network, session_id in managed:
        with _session_operation_lock(session_id):
            if session_id not in authorized_ids:
                # Do not re-expose a tenant whose row has ended. Cleanup is label-
                # guarded and leaves unrelated same-named networks untouched.
                cleanup_ok, cleanup_error = _cleanup_session_network(session_id)
                if not cleanup_ok:
                    logger.warning(
                        "Could not clean unauthorized session network %s: %s",
                        network,
                        cleanup_error,
                    )
                continue
            ownership_state, tenant_id, ownership_error = (
                _inspect_managed_container_ownership(session_id)
            )
            if ownership_state == "error" or (
                ownership_state == "owned_running" and not tenant_id
            ):
                # Inspection uncertainty is not authority to detach a
                # potentially valid live session. Leave it unchanged and retry.
                logger.warning(
                    "Tenant endpoint inspection uncertain on %s: %s",
                    network,
                    ownership_error or "missing container identity",
                )
                continue
            if ownership_state != "owned_running" or not tenant_id:
                logger.warning(
                    "Could not authorize tenant endpoint on %s: %s",
                    network,
                    ownership_error or ownership_state,
                )
                # Revoke the exactly verified central endpoint even when a
                # partial/legacy upgrade left an untrusted same-named tenant on
                # this labeled bridge. Cleanup deliberately leaves that unknown
                # endpoint and its network untouched after central detachment.
                cleanup_ok, cleanup_error = _cleanup_session_network(session_id)
                if not cleanup_ok:
                    logger.warning(
                        "Could not revoke central endpoint from %s: %s",
                        network,
                        cleanup_error,
                    )
                continue
            attached, attach_output = _ensure_session_network(
                session_id,
                allow_tenant=True,
                expected_tenant_id=tenant_id,
            )
            if not attached:
                logger.warning(
                    "Could not validate/reattach central gate to %s: %s",
                    network,
                    attach_output,
                )


def _reconcile_session_networks_loop() -> None:
    # The launcher becomes healthy before Compose creates the central gate, so
    # retry throughout the process lifetime rather than relying on one startup
    # attempt. This also repairs endpoints after a central-container redeploy.
    time.sleep(3)
    while True:
        try:
            _reconcile_session_networks()
        except Exception as exc:
            logger.warning("Session-network reconciliation failed: %s", exc)
        raw = (os.getenv("AXGT_SESSION_NETWORK_RECONCILE_SECONDS") or "10").strip()
        try:
            interval = max(5, int(raw))
        except ValueError:
            interval = 10
        time.sleep(interval)


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
    runtime_digest, _runtime_network = _runtime_contract_for_payload(payload)

    ssh_enabled = bool(payload.get("ssh_enabled"))
    ssh_pubkey = str(payload.get("ssh_pubkey") or "").strip()
    webrtc_agent_token = str(payload.get("webrtc_agent_token") or "").strip()
    if not ssh_enabled and not webrtc_agent_token:
        return None, "webrtc_agent_token is required for a desktop session"

    cmd: List[str] = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--label",
        "com.axonos.session-container=true",
        "--label",
        f"com.axonos.session-id={session_id}",
        "--label",
        f"com.axonos.session-config-sha256={runtime_digest}",
        "--cap-drop",
        "NET_RAW",
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
            "-e",
            "WEBRTC_GATE_INTERNAL_URL=http://axonos-gate:8890",
            "-e",
            "AXGT_GATE_HEARTBEAT_URL=http://axonos-gate:8889",
        ]
    )
    cmd.extend(_mode_env_args(session_id, ssh_enabled, ssh_pubkey))

    requested_template = str(payload.get("requested_template") or "").strip()
    if requested_template:
        cmd.extend(["-e", f"AXONOS_SELECTED_TEMPLATE={requested_template}"])

    files_key = str(payload.get("files_key") or "").strip()
    if files_key:
        cmd.extend(["-e", f"AXGT_SESSION_FILES_KEY={files_key}"])

    if webrtc_agent_token:
        cmd.extend(["-e", f"AXGT_WEBRTC_AGENT_TOKEN={webrtc_agent_token}"])

    for env_name in _env_passthrough_names():
        if env_name in (
            "AXGT_DESKTOP_ENABLED",
            "WEBRTC_AGENT_ENABLED",
            "WEBRTC_ENABLED",
        ):
            continue
        env_value = os.getenv(env_name)
        if env_value is not None:
            cmd.extend(["-e", f"{env_name}={env_value}"])

    cmd.extend(session_container_ompi_mca_env_flags())

    network = _session_network_name(session_id)
    if network:
        cmd.extend(["--network", network])

    cmd.extend(_extra_args_tokens())
    cmd.append(image)
    cmd.extend(_default_command_tokens())
    return cmd, None


def _unmanaged_session_container_names() -> Optional[List[str]]:
    """Find pre-boundary session containers that must be drained, never adopted."""
    ok, output = _run_cmd(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=axgt-session-",
            "--format",
            '{{.Names}}|{{.Label "com.axonos.session-container"}}|'
            '{{.Label "com.axonos.session-id"}}|'
            '{{.Label "com.axonos.session-config-sha256"}}',
        ]
    )
    if not ok:
        return None
    unmanaged: List[str] = []
    for line in output.splitlines():
        parts = line.strip().split("|", 3)
        if len(parts) != 4:
            continue
        name, managed, labeled_id, config_digest = (
            part.strip() for part in parts
        )
        prefix = "axgt-session-"
        suffix = name[len(prefix) :] if name.startswith(prefix) else ""
        if not suffix.isdigit():
            continue
        valid_digest = len(config_digest) == 64 and all(
            char in "0123456789abcdef" for char in config_digest
        )
        if (
            managed.lower() != "true"
            or labeled_id != suffix
            or not valid_digest
        ):
            unmanaged.append(name)
    return sorted(set(unmanaged))


def _configuration_errors() -> List[str]:
    errors: List[str] = []
    if not _image_name():
        errors.append("AXGT_HOST_SESSION_CONTAINER_IMAGE is required")
    if not _isolated_networks_enabled() and not (
        os.getenv("AXGT_HOST_SESSION_CONTAINER_NETWORK") or ""
    ).strip():
        errors.append(
            "AXGT_HOST_SESSION_CONTAINER_NETWORK is required when session "
            "network isolation is disabled"
        )
    if not (os.getenv("AXGT_CHALLENGE_DB_URL") or "").strip():
        errors.append(
            "AXGT_CHALLENGE_DB_URL is required for launch authorization"
        )
    unmanaged = _unmanaged_session_container_names()
    if unmanaged is None:
        errors.append("Docker session-container preflight failed")
    elif unmanaged:
        errors.append(
            "Drain and remove legacy unlabeled session containers before upgrade: "
            + ", ".join(unmanaged)
        )
    return errors


@app.route("/healthz", methods=["GET"])
def healthz():
    errors = _configuration_errors()
    if errors:
        return jsonify({"ok": False, "errors": errors}), 503
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
    configuration_errors = _configuration_errors()
    if configuration_errors:
        return jsonify({"ok": False, "errors": configuration_errors}), 503
    payload = request.get_json(silent=True) or {}
    required = ("session_id", "wallet_address", "assigned_gpu_ids")
    missing = [k for k in required if k not in payload]
    if missing:
        return jsonify({"ok": False, "error": f"missing required fields: {', '.join(missing)}"}), 400

    try:
        session_id = int(payload.get("session_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "session_id must be an integer"}), 400
    if session_id <= 0:
        return jsonify({"ok": False, "error": "session_id must be positive"}), 400
    name = _container_name(session_id)
    with _session_operation_lock(session_id):
        authorized, authorization_error = _launch_row_authorized(payload)
        if not authorized:
            return jsonify({"ok": False, "error": authorization_error}), 409
        try:
            cmd, build_err = _build_launch_cmd(payload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": f"invalid launcher arguments: {exc}"}), 400
        if build_err:
            return jsonify({"ok": False, "error": build_err}), 400
        runtime_digest, runtime_network = _runtime_contract_for_payload(payload)

        contract_state, existing_id, contract_error = _inspect_managed_container_contract(
            session_id,
            runtime_digest,
            runtime_network,
        )
        if contract_state == "error":
            logger.warning("launcher: container inspection failed for %s: %s", name, contract_error)
            return jsonify({"ok": False, "error": "could not inspect session container"}), 503
        if contract_state == "unmanaged":
            return jsonify({"ok": False, "error": contract_error}), 409
        if contract_state == "match_running" and existing_id:
            if not _isolated_networks_enabled():
                cleanup_ok, cleanup_error = _cleanup_session_network(session_id)
                if not cleanup_ok:
                    logger.warning(
                        "launcher: stale private network cleanup failed for %s: %s",
                        name,
                        cleanup_error,
                    )
                    return jsonify({"ok": False, "error": "session network cleanup failed"}), 500
            network_ok, network_error = _ensure_session_network(
                session_id,
                allow_tenant=True,
                expected_tenant_id=existing_id,
            )
            if not network_ok:
                logger.warning(
                    "launcher: existing %s network repair failed: %s",
                    name,
                    network_error,
                )
                return jsonify({"ok": False, "error": "isolated session network setup failed"}), 500
            logger.info("launcher: reusing active %s -> %s", name, existing_id[:12])
            return jsonify(
                {
                    "ok": True,
                    "container_id": existing_id,
                    "container_name": name,
                    "reused": True,
                }
            )

        if contract_state in ("match_stopped", "mismatch") and existing_id:
            remove_result = subprocess.run(
                ["docker", "rm", "-f", existing_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if remove_result.returncode != 0:
                return jsonify(
                    {
                        "ok": False,
                        "error": "could not remove stale managed session container",
                    }
                ), 500
        cleanup_ok, cleanup_error = _cleanup_session_network(session_id)
        if not cleanup_ok:
            logger.warning(
                "launcher: isolated network cleanup failed for %s: %s",
                name,
                cleanup_error,
            )
            return jsonify({"ok": False, "error": "session network cleanup failed"}), 500
        network_ok, network_error = _ensure_session_network(
            session_id,
            allow_tenant=False,
        )
        if not network_ok:
            logger.warning("launcher: isolated network setup failed for %s: %s", name, network_error)
            return jsonify({"ok": False, "error": "isolated session network setup failed"}), 500

        ok, out = _run_cmd(cmd)
        if not ok:
            logger.warning("launcher: launch failed for %s: %s", name, out)
            retry_state, retry_id, retry_error = _inspect_managed_container_contract(
                session_id,
                runtime_digest,
                runtime_network,
            )
            if retry_state == "match_running" and retry_id:
                network_ok, network_error = _ensure_session_network(
                    session_id,
                    allow_tenant=True,
                    expected_tenant_id=retry_id,
                )
                if not network_ok:
                    logger.warning(
                        "launcher: raced %s network validation failed: %s",
                        name,
                        network_error,
                    )
                    return jsonify({"ok": False, "error": "isolated session network setup failed"}), 500
                return jsonify(
                    {
                        "ok": True,
                        "container_id": retry_id,
                        "container_name": name,
                        "reused": True,
                    }
                )
            if retry_state in ("error", "unmanaged"):
                logger.warning(
                    "launcher: post-failure inspection for %s was inconclusive: %s",
                    name,
                    retry_error,
                )
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

    if session_id <= 0:
        return jsonify({"ok": False, "error": "session_id must be positive"}), 400
    with _session_operation_lock(session_id):
        ownership_state, target, ownership_error = _inspect_managed_container_ownership(
            session_id
        )
        if ownership_state == "error":
            return jsonify({"ok": False, "error": ownership_error}), 503
        if ownership_state == "unmanaged":
            return jsonify({"ok": False, "error": ownership_error}), 409
        removed = ownership_state == "absent"
        if target and ownership_state in ("owned_running", "owned_stopped"):
            result = subprocess.run(["docker", "rm", "-f", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            removed = result.returncode == 0
            if not removed:
                return jsonify({"ok": False, "error": "could not remove managed session container"}), 500
        cleanup_ok, cleanup_error = _cleanup_session_network(session_id)
        if not cleanup_ok:
            return jsonify({"ok": False, "error": cleanup_error or "session network cleanup failed"}), 500
    stopped = target if removed else None
    logger.info("launcher: stopped managed session=%s target=%s", session_id, stopped or "absent")
    return jsonify({"ok": True, "stopped": stopped})


@app.route("/list-containers", methods=["GET"])
def list_containers():
    auth_err = _require_token()
    if auth_err:
        return auth_err
    ok, out = _run_cmd([
        "docker", "ps",
        "--filter", "name=axgt-session",
        "--filter", "label=com.axonos.session-container=true",
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


def _volume_created_at_epoch(volume_name: str):
    """Volume creation time (epoch seconds), or None if it can't be determined."""
    try:
        out = subprocess.check_output(
            ["docker", "volume", "inspect", "-f", "{{.CreatedAt}}", volume_name],
            stderr=subprocess.STDOUT, text=True, timeout=10
        ).strip()
        if out:
            from datetime import datetime
            return datetime.fromisoformat(out.replace("Z", "+00:00")).timestamp()
    except Exception as exc:
        logger.warning("volume inspect CreatedAt failed for %s: %s", volume_name, exc)
    return None


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
        conn = psycopg2.connect(
            db_url,
            connect_timeout=_db_connect_timeout_seconds(),
        )
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
        conn = psycopg2.connect(
            db_url,
            connect_timeout=_db_connect_timeout_seconds(),
        )
        conn.autocommit = False
        with conn.cursor() as cur:
            # Anchor each wallet's storage charge to its LAST ledger charge (or the
            # volume's creation) and bill actual elapsed hours. The old code billed a
            # fixed `interval/3600` hours per sweep regardless of elapsed time, so the
            # sweep that runs 30s after every launcher restart charged a full hour —
            # frequent deploys silently over-billed every wallet with a volume.
            cur.execute(
                """SELECT wallet_address, MAX(created_at) FROM axgt_ledger
                   WHERE created_by = 'volume_billing_daemon'
                   GROUP BY wallet_address"""
            )
            last_charges = {row[0]: float(row[1]) for row in cur.fetchall() or []}

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

                # Later of last ledger charge / volume creation, so a volume recreated
                # after its wallet's last charge doesn't inherit the old anchor.
                anchor = last_charges.get(original_wallet)
                created_at = _volume_created_at_epoch(volume_name)
                if created_at is not None:
                    anchor = max(anchor, created_at) if anchor is not None else created_at
                if anchor is None:
                    # No ledger history and no readable creation time: bill one
                    # interval (legacy behavior) so storage is never free forever.
                    anchor = now - interval
                elapsed_hours = max(0.0, (now - anchor) / 3600.0)
                # Guard against clock skew / bad CreatedAt producing a monster charge.
                elapsed_hours = min(elapsed_hours, 24.0 * 7)
                if elapsed_hours * 3600.0 < 60.0:
                    # Sweep re-ran right after a charge (e.g. daemon restart) — the
                    # elapsed time rides over to the next sweep instead of rounding up.
                    continue

                size_kb = _get_volume_size_kb(volume_name)
                size_gb = size_kb / (1024.0 * 1024.0)
                charge = size_gb * cost_per_gb_hour * elapsed_hours

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
                        (original_wallet, "usage_deduction", -charge, 0, new_remaining, f"Offline storage charge: {size_gb:.4f} GB x {elapsed_hours:.3f} h", now, "volume_billing_daemon")
                    )
                    logger.info("Auto volume prune: Charged %s for %s offline storage over %.3f h: %s remaining minutes", original_wallet, f"{size_gb:.4f} GB", elapsed_hours, new_remaining)

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

    if _isolated_networks_enabled():
        import threading
        network_thread = threading.Thread(
            target=_reconcile_session_networks_loop,
            daemon=True,
        )
        network_thread.start()
        logger.info("Started isolated session-network reconciliation thread")

    logger.info("starting host launcher on %s:%s", host, port)
    # threaded=True so a slow /launch (cold `docker run`) or the volume-prune /
    # enumerate `docker run`s don't head-of-line block concurrent launch
    # requests — serialized handling was a source of gate-side launch timeouts.
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
