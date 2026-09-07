#!/bin/bash
# Firefox launcher for AxonOS sessions.
#
# A wallet may run several sessions at once, and they all share one home
# volume. Firefox holds an fcntl lock on the default profile's .parentlock,
# so a second desktop opening Firefox against the same profile gets
# "Firefox is already running, but is not responding". When that lock is held
# by another session, this wrapper starts Firefox with a per-session profile
# on the container's own disk instead (bookmarks/history of the shared profile
# are not visible there; they remain in the first session).
set -u

FIREFOX_BIN="${AXONOS_FIREFOX_BIN:-/usr/bin/firefox-esr}"
if [ ! -x "$FIREFOX_BIN" ]; then
    FIREFOX_BIN="$(command -v firefox-esr || command -v firefox-real || true)"
fi
if [ -z "$FIREFOX_BIN" ] || [ ! -x "$FIREFOX_BIN" ]; then
    echo "axonos-firefox: no Firefox binary found" >&2
    exit 127
fi

# An explicit profile on the command line means the caller knows what it wants.
for arg in "$@"; do
    case "$arg" in
        -P|--profile|-profile|--ProfileManager|-ProfileManager|--no-remote|-no-remote)
            exec "$FIREFOX_BIN" "$@"
            ;;
    esac
done

profile_dir="$(/usr/bin/python3 - <<'PY'
import configparser, os, sys
home = os.path.expanduser("~")
base = os.path.join(home, ".mozilla", "firefox")
ini = os.path.join(base, "profiles.ini")
if not os.path.isfile(ini):
    sys.exit(0)
cfg = configparser.ConfigParser()
cfg.read(ini)
chosen = None
for section in cfg.sections():
    if not section.startswith("Profile"):
        continue
    path = cfg.get(section, "Path", fallback="")
    if not path:
        continue
    is_rel = cfg.get(section, "IsRelative", fallback="1") == "1"
    full = os.path.join(base, path) if is_rel else path
    if cfg.get(section, "Default", fallback="0") == "1":
        chosen = full
        break
    if chosen is None:
        chosen = full
print(chosen or "")
PY
)"

locked_elsewhere=0
if [ -n "$profile_dir" ] && [ -f "$profile_dir/.parentlock" ]; then
    if ! /usr/bin/python3 - "$profile_dir/.parentlock" <<'PY'
import fcntl, sys
try:
    fd = open(sys.argv[1], "a+")
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.lockf(fd, fcntl.LOCK_UN)
except OSError:
    sys.exit(1)
PY
    then
        locked_elsewhere=1
    fi
fi

if [ "$locked_elsewhere" = "1" ]; then
    session_profile="${AXONOS_SESSION_STATE_DIR:-/var/lib/axonos-desktop}/firefox-profile"
    mkdir -p "$session_profile" 2>/dev/null || session_profile="/tmp/axonos-firefox-profile"
    mkdir -p "$session_profile"
    echo "axonos-firefox: shared profile is open in another of your sessions; using a per-session profile at $session_profile" >&2
    if command -v notify-send >/dev/null 2>&1; then
        notify-send "Firefox" "Your shared Firefox profile is open in another AxonOS session. This session uses a separate, temporary profile." >/dev/null 2>&1 || true
    fi
    exec "$FIREFOX_BIN" --no-remote --profile "$session_profile" "$@"
fi

exec "$FIREFOX_BIN" "$@"
