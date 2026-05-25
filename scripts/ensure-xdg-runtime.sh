#!/usr/bin/env bash
# Ensure /run/user/<uid> exists for session D-Bus (no systemd-logind in containers).
set -euo pipefail

user="${1:-aXonian}"
uid_num="$(id -u "${user}")"
runtime="/run/user/${uid_num}"

mkdir -p "${runtime}"
chown "${user}:${user}" "${runtime}"
chmod 700 "${runtime}"
