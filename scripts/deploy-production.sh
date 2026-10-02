#!/usr/bin/env bash
# Never trace configuration or secret-bearing command output.
set +x
set +v
set -Eeuo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1 GIT_OPTIONAL_LOCKS=0
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
command -v python3 >/dev/null 2>&1 || { printf 'ERROR: python3 is required\n' >&2; exit 1; }
exec python3 "$root/scripts/deploy_production.py" "$@"
