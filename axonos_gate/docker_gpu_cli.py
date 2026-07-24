"""
Helpers for nested `docker run` from Python (launcher / gate docker_cli mode).

If the parent process inherits NVIDIA_VISIBLE_DEVICES (common on GPU hosts /
compose), Docker's CLI + NVIDIA hooks can synthesize *both* a GPU-count request
and an explicit `--gpus device=...` request →
"cannot set both Count and DeviceIDs on duplicate device request".

Multi-GPU `device=i,j` must be passed with Docker-documented quoting or the CLI
mis-parses the comma into count + IDs (same daemon error).

See: session launcher calling `docker run --gpus device=…` while env still sets
VISIBILE_DEVICES=all or similar.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, List, Sequence


def docker_run_gpus_device_value(gpu_ids: List[int]) -> str:
    """Argument value for ``docker run --gpus …`` when pinning specific GPU indices.

    Docker's CLI mishandles comma-separated device lists unless the ``device=…``
    token is quoted, which surfaces as::

        cannot set both Count and DeviceIDs on device request

    Official form for multiple GPUs: ``--gpus '\"device=0,2\"'`` (single argv
    ending up as ``\"device=0,2\"``). Single-GPU ``device=N`` works unquoted.

    https://docs.docker.com/engine/containers/gpu/#access-specific-gpus
    """
    spec = ",".join(str(i) for i in gpu_ids)
    if not spec.strip():
        raise ValueError("gpu_ids must be non-empty")
    device = f"device={spec}"
    if "," in spec:
        return f'"{device}"'
    return device


# Drop from subprocess env before invoking `docker`; add keys if tooling sets more aliases.
_DROP_ENV_KEYS = (
    "NVIDIA_VISIBLE_DEVICES",
    "NVDOCKER_VISIBLE_DEVICES",
    "CUDA_VISIBLE_DEVICES",
)


def subprocess_env_for_nested_docker() -> Dict[str, str]:
    env = dict(os.environ)
    for key in _DROP_ENV_KEYS:
        env.pop(key, None)
    return env


# OpenMPI: avoid legacy sm BTL in session containers (see docs/GROMACS.md).
_SESSION_OMPI_MCA_ENV: Dict[str, str] = {
    "OMPI_MCA_btl": "vader,self,tcp",
    "OMPI_MCA_btl_base_warn_component_unused": "0",
}


# Values the capture agent legitimately inherits from the trusted launcher.
# Keep this list media-only: identity and control-plane credentials are injected
# explicitly per session and must never be added here.
SESSION_MEDIA_ENV_NAMES = (
    "WEBRTC_AUDIO_ENABLED",
    "WEBRTC_AUDIO_SOURCE",
    "WEBRTC_CAPTURE_BACKEND",
    "WEBRTC_CAPTURE_BITRATE",
    "WEBRTC_CAPTURE_DISPLAY",
    "WEBRTC_CAPTURE_FPS",
    "WEBRTC_CAPTURE_LOW_LATENCY",
    "WEBRTC_CAPTURE_MAX_STALE_FRAMES",
    "WEBRTC_CAPTURE_MAX_WIDTH",
    "WEBRTC_CAPTURE_NVENC_PRESET",
    "WEBRTC_CAPTURE_NVENC_TUNE",
    "WEBRTC_CAPTURE_NVFBC_BIN",
    "WEBRTC_CAPTURE_NVFBC_PRESET",
    "WEBRTC_CLIPBOARD_MAX_BYTES",
    "WEBRTC_CLIPBOARD_POLL_PRIMARY",
    "WEBRTC_DISPLAY_WAIT_SECONDS",
    "WEBRTC_LOCAL_CURSOR",
    "WEBRTC_MIC_ENABLED",
    "WEBRTC_MIC_SINK",
    "WEBRTC_PUBLIC_IP",
    "WEBRTC_STUN_URLS",
    "WEBRTC_TURN_CREDENTIAL",
    "WEBRTC_TURN_URLS",
    "WEBRTC_TURN_USERNAME",
)


def session_runtime_config_digest(
    *,
    session_id: int,
    wallet: str,
    profile: str,
    gpu_ids: Sequence[int],
    files_key: str,
    ssh_enabled: bool,
    network_name: str,
    image_name: str,
    requested_template: str = "",
    ssh_pubkey: str = "",
) -> str:
    """Fingerprint the immutable identity/topology expected for safe reuse.

    Only the digest is placed in a Docker label. The per-session file key is
    hashed before it enters the canonical payload and is never exposed there.
    """
    key_fingerprint = hashlib.sha256(
        str(files_key or "").encode("utf-8")
    ).hexdigest()
    ssh_pubkey_fingerprint = hashlib.sha256(
        str(ssh_pubkey or "").strip().encode("utf-8")
    ).hexdigest()
    payload = {
        "v": 2,
        "session_id": int(session_id),
        "wallet": str(wallet or "").strip().lower(),
        "profile": str(profile or "").strip().lower(),
        "gpu_ids": sorted(int(value) for value in gpu_ids),
        "files_key_sha256": key_fingerprint,
        "ssh_enabled": bool(ssh_enabled),
        "ssh_pubkey_sha256": ssh_pubkey_fingerprint,
        "requested_template": str(requested_template or "").strip(),
        "network_name": str(network_name or "").strip(),
        "image_name": str(image_name or "").strip(),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def session_container_ompi_mca_env_flags() -> List[str]:
    """Return ``docker run -e …`` flags for OpenMPI MCA defaults in session containers."""
    flags: List[str] = []
    for key, value in _SESSION_OMPI_MCA_ENV.items():
        flags.extend(["-e", f"{key}={value}"])
    return flags


def strip_conflicting_gpu_run_flags(tokens: List[str]) -> List[str]:
    """Remove redundant `--gpus`/`-g` clauses from AXGT_*_EXTRA_ARGS; we inject our own."""
    out: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--gpus="):
            i += 1
            continue
        if tok == "--gpus":
            i += 1
            if i < len(tokens) and not tokens[i].startswith("-"):
                i += 1
            continue
        if tok.startswith("--gpu="):
            i += 1
            continue
        if tok == "--gpu":
            i += 1
            if i < len(tokens) and not tokens[i].startswith("-"):
                i += 1
            continue
        out.append(tok)
        i += 1
    return out


# Options that would let a launcher configuration escape the session boundary
# established by the scheduler.  Environment forwarding has a dedicated,
# allowlisted channel in session_launcher_service; accepting another -e or
# --env-file here would make that boundary cosmetic.
_UNSAFE_SESSION_RUN_FLAGS = {
    "--add-host",
    "--annotation",
    "--cap-add",
    "--cap-drop",
    "--cgroup-parent",
    "--cgroupns",
    "--cidfile",
    "--device",
    "--device-cgroup-rule",
    "--dns",
    "--dns-option",
    "--dns-search",
    "--domainname",
    "--entrypoint",
    "--env",
    "--env-file",
    "--expose",
    "--group-add",
    "--hostname",
    "--ipc",
    "--ip",
    "--ip6",
    "--isolation",
    "--label",
    "--label-file",
    "--link",
    "--link-local-ip",
    "--mac-address",
    "--mount",
    "--name",
    "--network",
    "--network-alias",
    "--net",
    "--pid",
    "--publish",
    "--runtime",
    "--security-opt",
    "--userns",
    "--uts",
    "--volume",
    "--volumes-from",
    "-e",
    "-h",
    "-l",
    "-p",
    "-v",
}
_UNSAFE_SESSION_RUN_SWITCHES = {
    "--oom-kill-disable",
    "--privileged",
    "--publish-all",
    "--rm",
    "--use-api-socket",
    "-P",
}


def strip_unsafe_session_run_flags(tokens: List[str]) -> List[str]:
    """Remove docker-run options that can bypass tenant isolation.

    The remaining extension point is suitable for resource and logging knobs
    such as ``--cpus`` or ``--memory``. Networking, host mounts/devices,
    namespace sharing, privilege changes, arbitrary ports, and alternate env
    injection remain owned by the launcher.
    """
    out: List[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _UNSAFE_SESSION_RUN_SWITCHES or any(
            token.startswith(f"{flag}=")
            for flag in _UNSAFE_SESSION_RUN_SWITCHES
            if flag.startswith("--")
        ):
            i += 1
            continue
        if token in _UNSAFE_SESSION_RUN_FLAGS:
            # All exact options in this set consume one following value.
            i += 2
            continue
        if any(
            token.startswith(f"{flag}=")
            for flag in _UNSAFE_SESSION_RUN_FLAGS
            if flag.startswith("--")
        ):
            i += 1
            continue
        # Docker accepts attached forms for its short value options (-eFOO,
        # -p8080:80, -vsrc:dst, -hname). Treat them exactly like split forms.
        if len(token) > 2 and token[:2] in {"-e", "-h", "-l", "-p", "-v"}:
            i += 1
            continue
        # Docker also accepts clustered short options. A token such as -itP,
        # -itv/tmp:/host, or -iteNAME=value must not smuggle a publish, mount,
        # hostname, or environment option past the split/attached checks.
        if (
            token.startswith("-")
            and not token.startswith("--")
            and len(token) > 2
            and any(flag in token[1:] for flag in "Pehlpv")
        ):
            i += 1
            continue
        out.append(token)
        i += 1
    return out
