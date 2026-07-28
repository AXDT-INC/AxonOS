"""
Session container launcher adapters.

Mode B architecture: session_manager delegates launch/cleanup to this module so
runtime-specific orchestration (Docker socket, host-side launcher service, etc.)
is configurable without changing scheduler logic.
"""

import json
import logging
import math
import os
import secrets
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

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

logger = logging.getLogger(__name__)
_session_operation_locks: dict[int, threading.RLock] = {}
_session_operation_locks_guard = threading.Lock()


def _session_operation_lock(session_id: int) -> threading.RLock:
    sid = int(session_id)
    with _session_operation_locks_guard:
        return _session_operation_locks.setdefault(sid, threading.RLock())


def _db_connect_timeout_seconds() -> int:
    raw = (os.getenv("AXGT_SESSION_DB_CONNECT_TIMEOUT_SECONDS") or "5").strip()
    try:
        return max(1, min(30, int(raw)))
    except ValueError:
        return 5


def _credit_grace_max_seconds() -> int:
    raw = (
        os.getenv("AXGT_SESSION_CREDIT_GRACE_MINUTES")
        or os.getenv("AXGT_SESSION_PAUSED_MAX_MINUTES")
        or ""
    ).strip()
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


def _unmanaged_session_container_names_direct() -> Optional[List[str]]:
    """Find legacy direct-mode containers that this launcher must not adopt."""
    try:
        output = subprocess.check_output(
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
            ],
            stderr=subprocess.STDOUT,
            text=True,
            env=subprocess_env_for_nested_docker(),
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.error("session_launcher: Docker session preflight failed: %s", exc)
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


def runtime_configuration_error() -> Optional[str]:
    """Return a fail-closed direct-launcher error, if one is present."""
    if not _container_mode_enabled() or _launcher_mode() != "docker_cli":
        return None
    if not _isolated_networks_enabled() and not (
        os.getenv("AXGT_SESSION_CONTAINER_NETWORK") or ""
    ).strip():
        return (
            "AXGT_SESSION_CONTAINER_NETWORK is required when direct session "
            "network isolation is disabled"
        )
    unmanaged = _unmanaged_session_container_names_direct()
    if unmanaged is None:
        return "Docker session-container preflight failed"
    if unmanaged:
        return (
            "Drain and remove legacy unlabeled session containers before upgrade: "
            + ", ".join(unmanaged)
        )
    return None


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


def _launch_timeout_seconds() -> float:
    """HTTP timeout for launch/stop calls to the host launcher.

    Default 90s (was 10s): `docker run -d` of a GPU desktop image routinely
    exceeds 10s on a cold image cache, during GPU/nvidia-runtime init, with
    ``--shm-size 32g``, or under docker-daemon contention. A premature client
    timeout was being treated as a hard spawn failure even though the container
    went on to start moments later — surfacing "Failed to start user
    container / timed out" as a false positive over a healthy session.
    """
    raw = (os.getenv("AXGT_SESSION_LAUNCHER_TIMEOUT_SECONDS") or "").strip()
    try:
        return float(raw) if raw else 90.0
    except ValueError:
        return 90.0


def _launch_verify_attempts() -> int:
    raw = (os.getenv("AXGT_SESSION_LAUNCH_VERIFY_ATTEMPTS") or "").strip()
    try:
        return max(0, int(raw)) if raw else 5
    except ValueError:
        return 5


def _launch_verify_interval_seconds() -> float:
    raw = (os.getenv("AXGT_SESSION_LAUNCH_VERIFY_INTERVAL_SECONDS") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else 2.0
    except ValueError:
        return 2.0


def session_claim_timeout_seconds() -> int:
    """Browser deadline for a synchronous fresh session claim.

    A claim can spend the launcher HTTP timeout on its first request and then
    retry the identical launch contract several times after an inconclusive
    response.  Publish the complete server-side envelope (plus transport/DB
    headroom) so the browser never times out first and mistakes a successful,
    billable cold launch for a failed request.  Strict retained-session resumes
    do not spawn and intentionally use a separate short client deadline.
    """
    launch_timeout = _launch_timeout_seconds()
    if not math.isfinite(launch_timeout) or launch_timeout <= 0:
        launch_timeout = 90.0
    verify_attempts = max(1, _launch_verify_attempts())
    verify_interval = _launch_verify_interval_seconds()
    if not math.isfinite(verify_interval) or verify_interval < 0:
        verify_interval = 2.0
    verify_envelope = (verify_attempts * 5.0) + (
        max(0, verify_attempts - 1) * verify_interval
    )
    return int(math.ceil(max(150.0, launch_timeout + verify_envelope + 15.0)))


def _verify_container_started_via_http(
    base_url: str,
    payload: dict,
) -> Optional[str]:
    """Resolve an inconclusive launch through the host's exact contract check.

    A timeout is not proof that the first request failed, but a name-only
    container listing is not proof that the requested identity, configuration,
    and isolated network were established. Retry the same idempotent, authenticated
    launch request instead; the host-side per-session lock and runtime-contract
    validation are the authority for reuse.
    """
    attempts = max(1, _launch_verify_attempts())
    interval = _launch_verify_interval_seconds()
    for i in range(attempts):
        status, data, err = _http_json(
            "POST",
            f"{base_url}/launch",
            payload,
            timeout_s=5.0,
        )
        if (
            not err
            and status < 400
            and isinstance(data, dict)
            and data.get("ok")
        ):
            session_id = int(payload.get("session_id"))
            return str(
                data.get("container_id")
                or _container_name_for_session(session_id)
            )
        # An authenticated 4xx is a definitive host-side rejection. Retrying it
        # cannot prove a contract and would only delay the caller's failure.
        if not err and 400 <= status < 500:
            return None
        if i < attempts - 1 and interval > 0:
            time.sleep(interval)
    return None


def launch_session(
    session_id: int,
    wallet: str,
    profile: str,
    gpu_ids: List[int],
    template: Optional[str] = None,
    files_key: Optional[str] = None,
    ssh_enabled: bool = False,
    ssh_pubkey: Optional[str] = None,
    webrtc_agent_token: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Launch user session runtime; returns (ok, container_id, error)."""
    if not _container_mode_enabled():
        return True, "shared-desktop", None
    mode = _launcher_mode()
    if mode == "http":
        return _launch_via_http(
            session_id,
            wallet,
            profile,
            gpu_ids,
            template,
            files_key,
            ssh_enabled,
            ssh_pubkey,
            webrtc_agent_token,
        )
    if mode == "noop":
        # Useful when validating scheduler/queue logic without runtime orchestration.
        return True, _container_name_for_session(session_id), None
    with _session_operation_lock(session_id):
        configuration_error = runtime_configuration_error()
        if configuration_error:
            return False, None, configuration_error
        return _launch_via_docker_cli(
            session_id,
            wallet,
            profile,
            gpu_ids,
            template,
            files_key,
            ssh_enabled,
            ssh_pubkey,
            webrtc_agent_token,
        )


def stop_session(session_id: int, container_id: Optional[str]) -> bool:
    """Cleanup user session runtime resources."""
    if not _container_mode_enabled():
        return True
    mode = _launcher_mode()
    if mode == "http":
        return _stop_via_http(session_id, container_id)
    if mode == "noop":
        return True
    with _session_operation_lock(session_id):
        return _stop_via_docker_cli(session_id, container_id)


def list_running_sessions() -> List[int]:
    """Retrieve all running session container IDs by querying the launcher or running docker ps."""
    if not _container_mode_enabled():
        return []
    mode = _launcher_mode()
    if mode == "noop":
        return []
    import re
    pattern = re.compile(r"^/?axgt-session-([0-9]+)$")

    if mode == "http":
        base_url = (os.getenv("AXGT_SESSION_LAUNCHER_URL") or "").strip().rstrip("/")
        if not base_url:
            return []
        status, data, err = _http_json("GET", f"{base_url}/list-containers", None, timeout_s=10.0)
        if not err and status < 400 and isinstance(data, dict) and data.get("ok"):
            containers = data.get("containers") or []
            session_ids = []
            for c in containers:
                if not isinstance(c, dict):
                    continue
                name = c.get("name") or ""
                m = pattern.match(name)
                if m:
                    try:
                        session_ids.append(int(m.group(1)))
                    except ValueError:
                        pass
            return session_ids
        return []

    # docker_cli mode
    try:
        out = subprocess.check_output(
            [
                "docker",
                "ps",
                "--filter",
                "name=axgt-session-",
                "--filter",
                "label=com.axonos.session-container=true",
                "--format",
                "{{.Names}}",
            ],
            text=True,
            env=subprocess_env_for_nested_docker()
        )
        session_ids = []
        for name in out.splitlines():
            name = name.strip()
            m = pattern.match(name)
            if m:
                try:
                    session_ids.append(int(m.group(1)))
                except ValueError:
                    pass
        return session_ids
    except Exception as exc:
        logger.warning("session_launcher: failed to list docker cli containers: %s", exc)
        return []


def reconcile_session_networks() -> None:
    """Repair direct-mode central endpoints after a container redeploy.

    HTTP mode is reconciled continuously by the trusted launcher service.
    """
    if (
        not _container_mode_enabled()
        or _launcher_mode() != "docker_cli"
        or not _isolated_networks_enabled()
    ):
        return
    configuration_error = runtime_configuration_error()
    if configuration_error:
        logger.error("session_launcher: network reconciliation blocked: %s", configuration_error)
        return
    env = subprocess_env_for_nested_docker()
    try:
        output = subprocess.check_output(
            [
                "docker",
                "network",
                "ls",
                "--filter",
                "label=com.axonos.session-network=true",
                "--format",
                '{{.Name}}\t{{.Label "com.axonos.session-id"}}',
            ],
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
    except Exception as exc:
        logger.warning("session_launcher: network reconciliation list failed: %s", exc)
        return
    managed: List[Tuple[str, int]] = []
    for line in output.splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        session_id = int(parts[1])
        if session_id <= 0 or parts[0] != _session_network_name(session_id):
            continue
        managed.append((parts[0], session_id))
    if not managed:
        return
    db_url = (os.getenv("AXGT_CHALLENGE_DB_URL") or "").strip()
    if not db_url:
        logger.warning("session_launcher: cannot reconcile networks without database")
        return
    conn = None
    try:
        import psycopg2

        conn = psycopg2.connect(
            db_url,
            connect_timeout=_db_connect_timeout_seconds(),
        )
        now = time.time()
        credit_grace_cutoff = now - _credit_grace_max_seconds()
        grace_seconds = _session_grace_seconds()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM axgt_sessions
                   WHERE allocation_status IN ('allocating', 'allocated')
                     AND (
                         (
                             status = 'active'
                             AND expires_at > %s
                             AND (
                                 hard_expires_at IS NULL
                                 OR hard_expires_at + %s > %s
                             )
                         )
                         OR (
                             status IN ('credit_grace', 'paused')
                             AND last_heartbeat >= %s
                         )
                     )""",
                (now, grace_seconds, now, credit_grace_cutoff),
            )
            authorized_ids = {int(row[0]) for row in (cur.fetchall() or [])}
    except Exception as exc:
        logger.warning("session_launcher: network authorization failed: %s", exc)
        return
    finally:
        if conn is not None:
            conn.close()
    for _network, session_id in managed:
        with _session_operation_lock(session_id):
            if session_id not in authorized_ids:
                cleanup_ok, cleanup_error = _cleanup_session_network_direct(session_id)
                if not cleanup_ok:
                    logger.warning(
                        "session_launcher: unauthorized network cleanup failed for session %s: %s",
                        session_id,
                        cleanup_error,
                    )
                continue
            ownership_state, tenant_id, ownership_error = (
                _inspect_managed_container_ownership_direct(session_id)
            )
            if ownership_state == "error" or (
                ownership_state == "owned_running" and not tenant_id
            ):
                # Docker inspection uncertainty is not authority to disrupt a
                # possibly valid live tenant. Retry on the next reconciliation
                # pass without changing any endpoint.
                logger.warning(
                    "session_launcher: tenant endpoint inspection uncertain for session %s: %s",
                    session_id,
                    ownership_error or "missing container identity",
                )
                continue
            if ownership_state != "owned_running" or not tenant_id:
                logger.warning(
                    "session_launcher: cannot authorize tenant endpoint for session %s: %s",
                    session_id,
                    ownership_error or ownership_state,
                )
                # A partial/legacy upgrade can leave an untrusted same-named
                # endpoint on a previously managed bridge.  Cleanup first
                # revokes the exactly verified central endpoint, then refuses
                # to mutate the unknown tenant endpoint or remove its network.
                cleanup_ok, cleanup_error = _cleanup_session_network_direct(session_id)
                if not cleanup_ok:
                    logger.warning(
                        "session_launcher: central endpoint revocation failed for session %s: %s",
                        session_id,
                        cleanup_error,
                    )
                continue
            ok, error = _ensure_session_network_direct(
                session_id,
                allow_tenant=True,
                expected_tenant_id=tenant_id,
            )
            if not ok:
                logger.warning(
                    "session_launcher: network reconciliation failed for session %s: %s",
                    session_id,
                    error,
                )


# Per-session port scheme. WebRTC sessions get a UDP block for direct ICE; SSH
# sessions instead get a single published TCP port -> container :22. Both are
# deterministic from session_id so the gate can derive the connect details
# without a round-trip (see session_manager._ssh_port_for_session — keep in sync).
_WEBRTC_BASE_PORT = 40000
_WEBRTC_BLOCK_SIZE = 10
_SSH_BASE_PORT = 42000
_MAX_SESSIONS = 50


def _isolated_networks_enabled() -> bool:
    raw = (os.getenv("AXGT_SESSION_NETWORK_ISOLATION") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _isolated_session_network_name(session_id: int) -> str:
    """Deterministic private network name, independent of current mode."""
    return f"axgt-session-net-{int(session_id)}"


def _session_network_name(session_id: int) -> str:
    if _isolated_networks_enabled():
        return _isolated_session_network_name(session_id)
    return (os.getenv("AXGT_SESSION_CONTAINER_NETWORK") or "").strip()


def _central_gate_container() -> str:
    raw = (os.getenv("AXGT_CENTRAL_GATE_CONTAINER") or "axonos").strip()
    return raw if raw and all(c.isalnum() or c in "-_." for c in raw) else "axonos"


def _central_gate_network_alias() -> str:
    """Collision-proof DNS name used only inside isolated tenant networks."""
    return "axonos-gate"


def _inspect_central_gate_container_id_direct() -> Tuple[str, Optional[str], str]:
    ok, output = _run_docker_direct(
        ["docker", "inspect", "--format", "{{.Id}}", _central_gate_container()]
    )
    if not ok:
        if _docker_object_is_absent_direct(output):
            return "absent", None, ""
        return "error", None, output or "could not inspect central gate container"
    container_id = output.strip()
    if not container_id:
        return "error", None, "central gate inspection omitted its id"
    return "present", container_id[:64], ""


def _run_docker_direct(cmd: List[str]) -> Tuple[bool, str]:
    env = subprocess_env_for_nested_docker()
    try:
        output = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        ).strip()
        return True, output
    except subprocess.CalledProcessError as exc:
        return False, (exc.output or "").strip() or str(exc)
    except OSError as exc:
        return False, str(exc)


def _docker_object_is_absent_direct(output: str) -> bool:
    message = str(output or "").strip().lower()
    return any(
        marker in message
        for marker in (
            "no such object",
            "no such container",
            "no such network",
        )
    )


def _docker_network_is_absent_direct(output: str, network: str) -> bool:
    message = str(output or "").strip().lower()
    expected = str(network or "").strip().lower()
    return _docker_object_is_absent_direct(output) or (
        bool(expected) and f"network {expected} not found" in message
    )


def _inspect_session_network_contract_direct(
    session_id: int,
) -> Tuple[str, dict[str, str], str]:
    network = _isolated_session_network_name(session_id)
    ok, output = _run_docker_direct(
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
        if _docker_network_is_absent_direct(output, network):
            return "absent", {}, ""
        return "error", {}, output or "could not inspect session network"
    parts = output.strip().split("|", 2)
    if len(parts) != 3:
        return "error", {}, "malformed session network inspection"
    if f"{parts[0].strip().lower()}|{parts[1].strip()}" != f"true|{int(session_id)}":
        return "unmanaged", {}, "refusing unmanaged or mismatched session network"
    try:
        containers = json.loads(parts[2])
    except (TypeError, json.JSONDecodeError):
        return "error", {}, "malformed session network endpoint inspection"
    if not isinstance(containers, dict):
        return "error", {}, "malformed session network endpoint inspection"
    endpoints: dict[str, str] = {}
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


def _validate_session_network_endpoints_direct(
    session_id: int,
    endpoints: dict[str, str],
    *,
    allow_tenant: bool,
    expected_tenant_id: Optional[str],
) -> Tuple[bool, Optional[str]]:
    allowed = {_central_gate_container()}
    if allow_tenant:
        allowed.add(_container_name_for_session(session_id))
    unexpected = set(endpoints) - allowed
    if unexpected:
        return (
            False,
            "refusing session network with unexpected endpoint(s): "
            + ", ".join(sorted(unexpected)),
        )
    if allow_tenant:
        tenant_name = _container_name_for_session(session_id)
        actual_tenant_id = endpoints.get(tenant_name)
        expected_id = str(expected_tenant_id or "").strip()
        if actual_tenant_id and (
            not expected_id
            or not secrets.compare_digest(actual_tenant_id, expected_id)
        ):
            return False, "session network tenant endpoint identity mismatch"
    return True, None


def _ensure_session_network_direct(
    session_id: int,
    *,
    allow_tenant: bool = False,
    expected_tenant_id: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    if not _isolated_networks_enabled():
        return True, None
    network = _isolated_session_network_name(session_id)
    state, endpoints, error = _inspect_session_network_contract_direct(session_id)
    if state in ("error", "unmanaged"):
        return False, error
    if state == "owned":
        valid, endpoint_error = _validate_session_network_endpoints_direct(
            session_id,
            endpoints,
            allow_tenant=allow_tenant,
            expected_tenant_id=expected_tenant_id,
        )
        if not valid:
            return False, endpoint_error
    else:
        ok, output = _run_docker_direct(
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
            return False, output or "network create failed"
    ok, output = _run_docker_direct(
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
        marker in output.lower() for marker in ("already exists", "already connected")
    ):
        return False, output or "central gate network attach failed"
    state, endpoints, error = _inspect_session_network_contract_direct(session_id)
    if state != "owned":
        return False, error or "session network disappeared during setup"
    valid, endpoint_error = _validate_session_network_endpoints_direct(
        session_id,
        endpoints,
        allow_tenant=allow_tenant,
        expected_tenant_id=expected_tenant_id,
    )
    if not valid:
        return False, endpoint_error
    expected_endpoints = {_central_gate_container()}
    if allow_tenant:
        expected_endpoints.add(_container_name_for_session(session_id))
    if set(endpoints) != expected_endpoints:
        return False, "session network is missing an expected endpoint"
    return True, None


def _cleanup_session_network_direct(session_id: int) -> Tuple[bool, Optional[str]]:
    state, endpoints, error = _inspect_session_network_contract_direct(session_id)
    if state == "absent":
        return True, None
    if state != "owned":
        return False, error or "refusing to clean an unowned session network"
    allowed = {_central_gate_container()}
    unexpected = set(endpoints) - allowed
    central_name = _central_gate_container()
    network = _isolated_session_network_name(session_id)
    central_endpoint_id = endpoints.get(central_name)
    if central_endpoint_id:
        central_state, central_id, central_error = (
            _inspect_central_gate_container_id_direct()
        )
        if (
            central_state != "present"
            or not central_id
            or not secrets.compare_digest(central_endpoint_id, central_id)
        ):
            return False, central_error or "session network central endpoint identity mismatch"
        ok, output = _run_docker_direct(
            ["docker", "network", "disconnect", "-f", network, central_name]
        )
        if not ok and not _docker_object_is_absent_direct(output) and "not connected" not in output.lower():
            return False, output or "could not disconnect central gate from session network"
    if unexpected:
        return (
            False,
            "refusing cleanup of session network with unexpected endpoint(s): "
            + ", ".join(sorted(unexpected)),
        )
    ok, output = _run_docker_direct(["docker", "network", "rm", network])
    if ok or _docker_network_is_absent_direct(output, network):
        return True, None
    return False, output or "could not remove session network"


def _managed_container_id_direct(
    session_id: int,
    *,
    require_running: bool,
) -> Optional[str]:
    """Resolve only this launcher's exactly labeled deterministic container."""
    state, container_id, _error = _inspect_managed_container_ownership_direct(
        session_id
    )
    if state not in ("owned_running", "owned_stopped"):
        return None
    if require_running and state != "owned_running":
        return None
    return container_id


def _inspect_managed_container_ownership_direct(
    session_id: int,
) -> Tuple[str, Optional[str], str]:
    ok, output = _run_docker_direct(
        [
            "docker",
            "inspect",
            "--format",
            '{{.State.Running}}|{{index .Config.Labels "com.axonos.session-container"}}|'
            '{{index .Config.Labels "com.axonos.session-id"}}|'
            '{{index .Config.Labels "com.axonos.session-config-sha256"}}|{{.Id}}',
            _container_name_for_session(session_id),
        ]
    )
    if not ok:
        if _docker_object_is_absent_direct(output):
            return "absent", None, ""
        return "error", None, output or "could not inspect session container"
    parts = output.split("|", 4)
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


def _managed_container_runtime_matches_direct(
    session_id: int,
    expected_digest: str,
    expected_network: str,
) -> bool:
    """Compatibility wrapper around the atomic launch-contract inspection."""
    state, _container_id, _error = _inspect_managed_container_contract_direct(
        session_id,
        expected_digest,
        expected_network,
    )
    return state == "match_running"


def _inspect_managed_container_contract_direct(
    session_id: int,
    expected_digest: str,
    expected_network: str,
) -> Tuple[str, Optional[str], str]:
    """Atomically classify a direct-mode deterministic tenant container."""
    ok, output = _run_docker_direct(
        [
            "docker",
            "inspect",
            "--format",
            '{{.State.Running}}|{{index .Config.Labels "com.axonos.session-container"}}|'
            '{{index .Config.Labels "com.axonos.session-id"}}|'
            '{{index .Config.Labels "com.axonos.session-config-sha256"}}|'
            '{{json .NetworkSettings.Networks}}|{{.Id}}',
            _container_name_for_session(session_id),
        ]
    )
    if not ok:
        if _docker_object_is_absent_direct(output):
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
        "-e", "WEBRTC_ENABLED=true",
        "-e", "WEBRTC_AGENT_ENABLED=true",
        "-e", f"WEBRTC_PORT_RANGE={_webrtc_port_range(session_id)}",
    ]


def _launch_via_docker_cli(
    session_id: int,
    wallet: str,
    profile: str,
    gpu_ids: List[int],
    template: Optional[str] = None,
    files_key: Optional[str] = None,
    ssh_enabled: bool = False,
    ssh_pubkey: Optional[str] = None,
    webrtc_agent_token: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    image = (os.getenv("AXGT_SESSION_CONTAINER_IMAGE") or "").strip()
    if not image:
        return False, None, "AXGT_SESSION_CONTAINER_IMAGE is required in docker_cli mode"
    extra_raw = (os.getenv("AXGT_SESSION_CONTAINER_EXTRA_ARGS") or "").strip()
    try:
        extra_tokens = (
            strip_unsafe_session_run_flags(
                strip_conflicting_gpu_run_flags(shlex.split(extra_raw))
            )
            if extra_raw
            else []
        )
    except ValueError as exc:
        return False, None, f"invalid AXGT_SESSION_CONTAINER_EXTRA_ARGS: {exc}"
    gpu_spec = ",".join(str(i) for i in gpu_ids)
    name = _container_name_for_session(session_id)
    network = _session_network_name(session_id)
    runtime_digest = session_runtime_config_digest(
        session_id=session_id,
        wallet=wallet,
        profile=profile,
        gpu_ids=gpu_ids,
        files_key=files_key or "",
        ssh_enabled=ssh_enabled,
        network_name=network,
        image_name=image,
        requested_template=template or "",
        ssh_pubkey=ssh_pubkey or "",
    )
    if not ssh_enabled and not (webrtc_agent_token or "").strip():
        return False, None, "webrtc_agent_token is required for a desktop session"
    contract_state, existing_id, contract_error = _inspect_managed_container_contract_direct(
        session_id,
        runtime_digest,
        network,
    )
    if contract_state == "error":
        logger.warning(
            "session_launcher: container inspection failed for %s: %s",
            name,
            contract_error,
        )
        return False, None, "could not inspect session container"
    if contract_state == "unmanaged":
        return False, None, contract_error
    if contract_state == "match_running" and existing_id:
        if not _isolated_networks_enabled():
            cleanup_ok, cleanup_error = _cleanup_session_network_direct(session_id)
            if not cleanup_ok:
                return False, None, cleanup_error or "session network cleanup failed"
        network_ok, network_error = _ensure_session_network_direct(
            session_id,
            allow_tenant=True,
            expected_tenant_id=existing_id,
        )
        if not network_ok:
            return False, None, network_error or "isolated session network setup failed"
        return True, existing_id, None
    if contract_state in ("match_stopped", "mismatch") and existing_id:
        remove_result = subprocess.run(
            ["docker", "rm", "-f", existing_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            env=subprocess_env_for_nested_docker(),
        )
        if remove_result.returncode != 0:
            return False, None, "could not remove stale managed session container"
    cleanup_ok, cleanup_error = _cleanup_session_network_direct(session_id)
    if not cleanup_ok:
        return False, None, cleanup_error or "session network cleanup failed"
    network_ok, network_error = _ensure_session_network_direct(
        session_id,
        allow_tenant=False,
    )
    if not network_ok:
        return False, None, network_error or "isolated session network setup failed"

    cmd: List[str] = [
        "docker", "run", "-d", "--rm",
        "--name", name,
        "--label", "com.axonos.session-container=true",
        "--label", f"com.axonos.session-id={session_id}",
        "--label", f"com.axonos.session-config-sha256={runtime_digest}",
        "--cap-drop", "NET_RAW",
    ]
    cmd.extend(_publish_args_for_session(session_id, ssh_enabled))
    if network:
        cmd.extend(["--network", network])
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
        "-e", "WEBRTC_GATE_INTERNAL_URL=http://axonos-gate:8890",
        "-e", "AXGT_GATE_HEARTBEAT_URL=http://axonos-gate:8889",
    ])
    cmd.extend(_mode_env_args(session_id, ssh_enabled, ssh_pubkey))
    heartbeat_interval = (
        os.getenv("AXGT_HEARTBEAT_INTERVAL_SECONDS") or ""
    ).strip()
    if heartbeat_interval:
        cmd.extend([
            "-e",
            f"AXGT_HEARTBEAT_INTERVAL_SECONDS={heartbeat_interval}",
        ])
    if template:
        cmd.extend(["-e", f"AXONOS_SELECTED_TEMPLATE={template}"])
    if files_key:
        cmd.extend(["-e", f"AXGT_SESSION_FILES_KEY={files_key}"])
    if webrtc_agent_token:
        cmd.extend(["-e", f"AXGT_WEBRTC_AGENT_TOKEN={webrtc_agent_token}"])
    for env_name in SESSION_MEDIA_ENV_NAMES:
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
    cmd.extend(extra_tokens)
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
    except Exception as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            msg = (exc.output or "").strip() or str(exc)
        else:
            msg = str(exc)
        logger.warning("session_launcher: docker run failed: %s", msg)
        retry_state, retry_id, retry_error = _inspect_managed_container_contract_direct(
            session_id,
            runtime_digest,
            network,
        )
        if retry_state == "match_running" and retry_id:
            network_ok, network_error = _ensure_session_network_direct(
                session_id,
                allow_tenant=True,
                expected_tenant_id=retry_id,
            )
            if not network_ok:
                return False, None, network_error or "isolated session network setup failed"
            return True, retry_id, None
        if retry_state in ("error", "unmanaged"):
            logger.warning(
                "session_launcher: post-failure inspection for %s was inconclusive: %s",
                name,
                retry_error,
            )
        return False, None, msg


def _stop_via_docker_cli(session_id: int, container_id: Optional[str]) -> bool:
    try:
        ownership_state, target, ownership_error = (
            _inspect_managed_container_ownership_direct(session_id)
        )
        if ownership_state in ("error", "unmanaged"):
            logger.warning(
                "session_launcher: refusing/inconclusive cleanup for session %s: %s",
                session_id,
                ownership_error,
            )
            return False
        if target and ownership_state in ("owned_running", "owned_stopped"):
            result = subprocess.run(
                ["docker", "rm", "-f", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                env=subprocess_env_for_nested_docker(),
            )
            if result.returncode != 0:
                logger.warning(
                    "session_launcher: could not remove managed session container %s",
                    session_id,
                )
                return False
        cleanup_ok, cleanup_error = _cleanup_session_network_direct(session_id)
        if not cleanup_ok:
            logger.warning(
                "session_launcher: network cleanup failed for session %s: %s",
                session_id,
                cleanup_error,
            )
            return False
        return True
    except Exception as exc:
        logger.warning(
            "session_launcher: docker cleanup failed for session %s: %s",
            session_id,
            exc,
        )
        return False


def _launch_via_http(
    session_id: int,
    wallet: str,
    profile: str,
    gpu_ids: List[int],
    template: Optional[str] = None,
    files_key: Optional[str] = None,
    ssh_enabled: bool = False,
    ssh_pubkey: Optional[str] = None,
    webrtc_agent_token: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
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
        "webrtc_agent_token": webrtc_agent_token,
    }
    status, data, err = _http_json("POST", f"{base_url}/launch", payload)
    launch_ok = (not err) and status < 400 and isinstance(data, dict) and bool(data.get("ok"))
    if launch_ok:
        container_id = (data.get("container_id") or _container_name_for_session(session_id))
        return True, str(container_id), None

    reason = err or (data.get("error") if isinstance(data, dict) else f"http {status}") or "launcher rejected request"

    # Only the *inconclusive* outcomes can be a false failure over a live
    # container: a transport error/timeout (err set, status 0) or a 5xx where
    # the launch may have proceeded behind a proxy. A 4xx is a definitive
    # rejection (bad request / auth) — nothing was launched, so fail fast and
    # skip the verify poll.
    if not (bool(err) or status >= 500):
        return False, None, reason

    # The container may have started anyway (slow `docker run` outliving our
    # HTTP timeout is the common case). Retry the identical idempotent request so
    # only the host's exact identity/digest/network contract can confirm success.
    verified = _verify_container_started_via_http(base_url, payload)
    if verified:
        logger.warning(
            "session_launcher: /launch for %s was inconclusive (%s) but an idempotent contract retry succeeded",
            _container_name_for_session(session_id), reason,
        )
        return True, verified, None
    return False, None, reason


def _stop_via_http(session_id: int, container_id: Optional[str]) -> bool:
    base_url = (os.getenv("AXGT_SESSION_LAUNCHER_URL") or "").strip().rstrip("/")
    if not base_url:
        return False
    payload = {"session_id": session_id, "container_id": container_id}
    status, data, error = _http_json("POST", f"{base_url}/stop", payload)
    ok = not error and status < 400 and isinstance(data, dict) and bool(data.get("ok"))
    if not ok:
        logger.warning(
            "session_launcher: host stop failed for session %s: %s",
            session_id,
            error or (data.get("error") if isinstance(data, dict) else f"http {status}"),
        )
    return ok


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


def _http_json(method: str, url: str, payload: Optional[dict], timeout_s: Optional[float] = None) -> Tuple[int, object, Optional[str]]:
    token = (os.getenv("AXGT_SESSION_LAUNCHER_TOKEN") or "").strip()
    if timeout_s is None:
        timeout_s = _launch_timeout_seconds()
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url=url, method=method, data=body)
    if body is not None:
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
