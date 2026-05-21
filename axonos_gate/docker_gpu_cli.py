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

import os
from typing import Dict, List


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
            if i < len(tokens) and not tokens[i].startswith("-") and "=" not in tokens[i]:
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
