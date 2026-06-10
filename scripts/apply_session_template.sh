#!/bin/bash

# MIT License
#
# Copyright (c) 2025 Avimanyu Bandyopadhyay
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# Per-session template launcher.
#
# The AxonOS desktop image is a single "fat" build that ships every scientific
# tool. This script aligns the running session's UX with the environment template
# the user picked on the landing page by auto-opening that template's hero app.
#
# The template id is written to ~/.config/axonos/selected_template by startup.sh,
# because XFCE (started by supervisord with a fixed environment= subset) does not
# inherit the AXONOS_SELECTED_TEMPLATE Docker ENV.

set -u

CONF_DIR="$HOME/.config/axonos"
TEMPLATE_FILE="${AXONOS_TEMPLATE_FILE:-$CONF_DIR/selected_template}"
LOG="$CONF_DIR/template-launch.log"
mkdir -p "$CONF_DIR"

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/aXonian/.Xauthority}"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

template=""
# Prefer the runtime env (if ever propagated); otherwise the file from startup.sh.
if [ -n "${AXONOS_SELECTED_TEMPLATE:-}" ]; then
    template="${AXONOS_SELECTED_TEMPLATE}"
elif [ -f "$TEMPLATE_FILE" ]; then
    template="$(tr -d '[:space:]' < "$TEMPLATE_FILE" 2>/dev/null)"
fi

log "selected template='${template}'"
[ -z "$template" ] && exit 0

# Only auto-launch once per session so a desktop restart doesn't stack windows.
# Stamp lives in /tmp (ephemeral per container), NOT under the persistent home
# volume — otherwise a returning wallet's stale stamp would suppress every future
# session's launch.
STAMP="/tmp/.axonos-template-launched"
if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$template" ]; then
    log "already launched for '${template}', skipping"
    exit 0
fi

# Wait for the desktop to actually be ready rather than a fixed delay:
# first X answering, then xfce4-panel up (desktop session fully started).
for _ in $(seq 1 90); do
    xset q >/dev/null 2>&1 && break
    sleep 1
done
for _ in $(seq 1 60); do
    pgrep -x xfce4-panel >/dev/null 2>&1 && break
    sleep 1
done
# Brief settle so xfwm4 is managing windows before apps open.
sleep 3

# Open the JupyterLab service started by supervisord (loopback + token).
open_jupyter() {
    log "opening jupyterlab"
    /usr/local/bin/open-jupyterlab.sh >/dev/null 2>&1 &
}

# Open a Terminator window running BODY, then drop into an interactive shell.
launch_terminal() {
    local title="$1"; shift
    local body="$1"
    local f
    f="$(mktemp /tmp/axonos-tmpl-XXXXXX.sh)"
    {
        echo '#!/bin/bash'
        echo "$body"
        echo 'exec bash'
    } > "$f"
    chmod +x "$f"
    terminator --title "$title" -x bash "$f" >/dev/null 2>&1 &
}

case "$template" in
    pytorch|beakerx)
        open_jupyter
        ;;
    rstudio)
        log "launching rstudio"
        rstudio --no-sandbox >/dev/null 2>&1 &
        ;;
    ugene)
        log "launching ugene + pymol"
        ugene -ui >/dev/null 2>&1 &
        # PyMOL for visualizing structures from UGENE analyses (GPU GL via vglrun).
        ( vglrun pymol >/dev/null 2>&1 || pymol >/dev/null 2>&1 ) &
        ;;
    gromacs)
        log "launching gromacs terminal + pymol"
        launch_terminal "GROMACS — Molecular Dynamics" \
            "source /opt/gromacs/bin/GMXRC 2>/dev/null; echo 'GROMACS ready. Try: gmx -version'; echo; gmx -version 2>/dev/null | head -n 20"
        # PyMOL for visualizing GROMACS structures/trajectories (GPU GL via vglrun).
        ( vglrun pymol >/dev/null 2>&1 || pymol >/dev/null 2>&1 ) &
        ;;
    quantum-espresso)
        log "launching quantum espresso terminal + xcrysden"
        launch_terminal "Quantum ESPRESSO — DFT" \
            "echo 'Quantum ESPRESSO ready. Run pw.x (reads a namelist on stdin) to start.'; echo; echo | pw.x 2>&1 | head -n 3"
        # XCrySDen for visualizing crystal structures / QE output (GPU GL via vglrun).
        ( vglrun xcrysden >/dev/null 2>&1 || xcrysden >/dev/null 2>&1 ) &
        ;;
    *)
        log "no launcher mapping for '${template}'"
        ;;
esac

printf '%s\n' "$template" > "$STAMP"
exit 0
