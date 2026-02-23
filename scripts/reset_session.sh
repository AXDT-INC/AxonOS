#!/usr/bin/env bash
# Desktop reset between AxonOS sessions (Feature 2 Option A).
# Called by session_manager when the active session ends so the next user
# gets a clean desktop. Runs in container as root; targets aXonian's session.
set -e

USER="${AXONOS_DESKTOP_USER:-aXonian}"
HOME_USER="/home/${USER}"

# Kill common desktop and scientific apps so they don't carry over to next user.
# Avoids killing shell/daemons; only processes whose command line matches these.
for pattern in xfce4-session startxfce4 xfce4-panel thunar firefox spyder rstudio octave qgis.*grass remix; do
  pkill -u "$USER" -f "$pattern" 2>/dev/null || true
done

# Clean session and recency caches so the next user doesn't see previous user's state
rm -rf "${HOME_USER}/.cache/sessions/"* 2>/dev/null || true
rm -f "${HOME_USER}/.local/share/recently-used.xbel" 2>/dev/null || true
rm -rf /tmp/user-session-* 2>/dev/null || true
rm -rf /tmp/.X*-lock 2>/dev/null || true

# Restart XFCE so the desktop comes up clean (supervisord is PID 1 in container)
if command -v supervisorctl >/dev/null 2>&1; then
  supervisorctl restart xfce4 2>/dev/null || true
  # Give XFCE time to start, then re-apply AxonOS theme and wallpaper
  sleep 5
  if [ -x /usr/local/bin/post_deploy_theme.sh ]; then
    /usr/local/bin/post_deploy_theme.sh 2>/dev/null || true
  fi
fi
