"""
Helpers for nested `docker run` from Python (launcher / gate docker_cli mode).

If the parent process inherits NVIDIA_VISIBLE_DEVICES (common on GPU hosts /
compose), Docker's CLI + NVIDIA hooks can synthesize *both* a GPU-count request
and an explicit `--gpus device=...` request →
"cannot set both Count and DeviceIDs on duplicate device request".

See: session launcher calling `docker run --gpus device=…` while env still sets
VISIBILE_DEVICES=all or similar.
"""

from __future__ import annotations

import os
from typing import Dict, List

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
