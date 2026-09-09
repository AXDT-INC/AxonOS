"""Map a session container's host PIDs to its own PID namespace.

Why this exists
---------------
NVML reports GPU compute processes by *host* PID. A session container has its
own PID namespace, so `nvidia-smi` inside it cannot resolve those PIDs in its
`/proc` and prints "No running processes found" even while its own training
job holds 90% of the GPU. Giving sessions `--pid=host` would fix the display
but expose every tenant's processes, so the translation happens host-side:

* `docker top <container>` lists the container's processes with their host
  PIDs (the daemon resolves them from the container's cgroup).
* `/proc/<host_pid>/status` carries an `NSpid:` line with the PID in every
  namespace from outermost to innermost. Entry ``depth`` (the number of
  namespaces the container's init sits under) is the PID as the container
  sees it.

The launcher container is not in the host PID namespace either, so it reads
the host procfs from a read-only bind mount (``AXGT_HOST_PROC_DIR``, default
``/host/proc``). Without that mount the map still carries names and command
lines, just no container-side PIDs.
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional

DEFAULT_HOST_PROC_DIR = "/host/proc"
ARGS_MAX_CHARS = 200


def host_proc_dir() -> str:
    raw = (os.getenv("AXGT_HOST_PROC_DIR") or "").strip().rstrip("/")
    return raw or DEFAULT_HOST_PROC_DIR


def read_nspid(proc_dir: str, host_pid: int) -> Optional[List[int]]:
    """Return the NSpid chain for *host_pid* from *proc_dir*, else None."""
    try:
        with open(os.path.join(proc_dir, str(host_pid), "status"), "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("NSpid:"):
                    return [int(tok) for tok in line.split()[1:]]
    except (OSError, ValueError):
        return None
    return None


def parse_docker_top(output: str) -> Dict[int, Dict[str, str]]:
    """Parse `docker top … -eo pid,comm,args` into {host_pid: {comm, args}}."""
    rows: Dict[int, Dict[str, str]] = {}
    lines = [ln for ln in (output or "").splitlines() if ln.strip()]
    for line in lines[1:]:  # first line is the ps header
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        rows[pid] = {
            "comm": parts[1],
            "args": (parts[2] if len(parts) > 2 else parts[1])[:ARGS_MAX_CHARS],
        }
    return rows


def container_process_map(
    container_id: str,
    *,
    proc_dir: Optional[str] = None,
    docker_env: Optional[dict] = None,
) -> Dict[str, Dict[str, object]]:
    """{host_pid(str): {"pid": container_pid|None, "comm": …, "args": …}}.

    The container-side PID is taken from the NSpid entry at the depth of the
    container's init process, so a job that nests further namespaces (e.g. an
    inner `unshare`) still maps to the PID the session's `ps` shows.
    """
    proc_dir = proc_dir or host_proc_dir()
    try:
        out = subprocess.check_output(
            ["docker", "top", container_id, "-eo", "pid,comm,args"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            env=docker_env,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    rows = parse_docker_top(out)
    if not rows:
        return {}

    depth: Optional[int] = None
    init_pid = min(rows)
    init_chain = read_nspid(proc_dir, init_pid)
    if init_chain and len(init_chain) >= 2:
        depth = len(init_chain) - 1

    result: Dict[str, Dict[str, object]] = {}
    for host_pid, info in rows.items():
        container_pid: Optional[int] = None
        if depth is not None:
            chain = read_nspid(proc_dir, host_pid)
            if chain and len(chain) > depth:
                container_pid = chain[depth]
        result[str(host_pid)] = {
            "pid": container_pid,
            "comm": info["comm"],
            "args": info["args"],
        }
    return result
