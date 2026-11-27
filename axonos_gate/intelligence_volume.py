"""Run the conversation worker against one existing Docker-managed wallet home."""
from functools import lru_cache
import json
from pathlib import Path
import re
import socket
import subprocess
import threading

WORKER = Path(__file__).with_name("intelligence_store.py")
WALLET = re.compile(r"0x[0-9a-f]{40}")
OPERATIONS = frozenset(("list", "create", "get", "rename", "delete", "prepare", "append", "share", "revoke", "public"))
SLOTS = threading.BoundedSemaphore(8)
UNAVAILABLE = {"error": "Wallet storage is unavailable. Try again shortly."}, 503


def _no_volume(operation):
    if operation == "list":
        return {"conversations": [], "storage_available": False}, 200
    if operation == "public":
        return {"error": "Shared conversation unavailable."}, 404
    return {"error": "This wallet has no persistent storage volume.", "code": "no_wallet_volume"}, 409


@lru_cache(maxsize=1)
def _image(fallback):
    # Reuse the small local launcher image. Host-native installations can use
    # the already configured session image, which also provides Python.
    result = subprocess.run(
        ["docker", "inspect", "--type=container", "--format={{.Image}}", socket.gethostname()],
        capture_output=True, text=True, timeout=5,
    )
    digest = result.stdout.strip()
    return digest if result.returncode == 0 and re.fullmatch(r"sha256:[a-f0-9]{64}", digest) else fallback


def run(wallet, operation, data, volume_prefix, enabled, fallback_image):
    if not isinstance(wallet, str) or not WALLET.fullmatch(wallet) or not isinstance(data, dict):
        return {"error": "Invalid wallet storage request."}, 400
    if not isinstance(operation, str) or operation not in OPERATIONS:
        return {"error": "Invalid storage operation."}, 400
    if not enabled:
        return _no_volume(operation)
    if not volume_prefix or not SLOTS.acquire(blocking=False):
        return UNAVAILABLE
    try:
        name = volume_prefix + wallet
        volume = subprocess.run(
            ["docker", "volume", "inspect", "--format={{.Driver}}", name],
            capture_output=True, text=True, timeout=5,
        )
        if volume.returncode:
            return _no_volume(operation) if "no such volume" in volume.stderr.lower() else UNAVAILABLE
        if volume.stdout.strip() != "local":
            return UNAVAILABLE
        image = _image(fallback_image)
        if not image:
            return UNAVAILABLE
        mount = "type=volume,source=" + name + ",target=/wallet,volume-nocopy"
        if operation == "public":
            mount += ",readonly"
        cmd = [
            "docker", "run", "--rm", "--interactive", "--pull=never", "--runtime=runc",
            "--network=none", "--read-only", "--user=1000:1000", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=16", "--memory=96m",
            "--cpus=1", "--no-healthcheck", "--log-driver=none", "--workdir=/",
            "--mount", mount, "--entrypoint=python3", image, "-I", "-B", "-c", WORKER.read_text(),
        ]
        # stdin carries private content. Neither command lines nor container
        # logs should ever contain a query, message, reply, or capability.
        completed = subprocess.run(cmd, input=json.dumps({"operation": operation, "data": data}, ensure_ascii=False),
                                   capture_output=True, text=True, timeout=20)
        if completed.returncode or len(completed.stdout) > 8 * 1024 * 1024:
            return UNAVAILABLE
        payload, status = json.loads(completed.stdout)
        if not isinstance(payload, dict) or type(status) is not int or not 200 <= status < 600:
            return UNAVAILABLE
        return payload, status
    except (OSError, ValueError, subprocess.SubprocessError):
        return UNAVAILABLE
    finally:
        SLOTS.release()
