#!/usr/bin/env bash
# Start XFCE with a session D-Bus and XDG runtime dir (containers lack systemd --user).
set -euo pipefail

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"
export XDG_CONFIG_DIRS="/etc/xdg:${XDG_CONFIG_HOME:-${HOME}/.config}"

uid_num="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${uid_num}}"
mkdir -p "${XDG_RUNTIME_DIR}"
chmod 700 "${XDG_RUNTIME_DIR}" 2>/dev/null || true

touch "${XAUTHORITY}"

exec dbus-run-session -- startxfce4
