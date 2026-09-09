#!/bin/bash
# Shared CellModeller GUI launcher.
#
# Both launch paths use this script so they cannot drift apart:
#   - the session template (apply_session_template.sh) for CellModeller sessions
#   - the XFCE application menu entry (/usr/share/applications/cellmodeller.desktop)
#
# Behaviour:
#   - runs the GUI with the system Python that carries the OpenCL stack
#   - exports the CellModeller module path explicitly
#   - prefers VirtualGL so OpenGL renders on the GPU-backed X :0
#   - keeps simulation output under the session home: CellModeller's Simulator
#     writes to $CMPATH/data/<model>-<timestamp>/ and expects the data
#     directory to already exist (os.mkdir), so a missing directory made
#     "save pickles" fail from $HOME before this script existed
#   - streams stdout/stderr to the terminal and prints the exit status
#   - with --hold, stays in an interactive shell after the GUI exits so
#     tracebacks remain readable (used by the menu entry; the template's
#     terminal helper appends its own shell)

hold=false
if [ "${1:-}" = "--hold" ]; then
    hold=true
    shift
fi

export PYTHONPATH="/opt/CellModeller${PYTHONPATH:+:$PYTHONPATH}"

if [ -z "${CMPATH:-}" ]; then
    CMPATH="${HOME:-/tmp}/CellModeller"
fi
if ! mkdir -p "$CMPATH/data" 2>/dev/null; then
    CMPATH="/opt"
    mkdir -p "$CMPATH/data" 2>/dev/null || true
fi
export CMPATH
cd "${HOME:-/}" 2>/dev/null || cd /

echo 'CellModeller simulation output will remain visible in this terminal.'
echo 'Closing the GUI returns here without closing the terminal.'
echo "Saved simulations are written under: $CMPATH/data"
echo

# VirtualGL only when its target GPU display is actually reachable; otherwise
# fall back to the display's own GL rather than failing on "[VGL] ERROR".
use_vgl=false
if command -v vglrun >/dev/null 2>&1 && command -v xdpyinfo >/dev/null 2>&1 \
   && xdpyinfo -display "${VGL_DISPLAY:-:0}" >/dev/null 2>&1; then
    use_vgl=true
fi
if [ "$use_vgl" = true ]; then
    vglrun /usr/bin/python3 /opt/CellModeller/Scripts/CellModellerGUI.py "$@"
else
    /usr/bin/python3 /opt/CellModeller/Scripts/CellModellerGUI.py "$@"
fi
rc=$?
echo
echo "CellModeller exited with status $rc. The terminal will remain open."

if [ "$hold" = true ]; then
    exec bash
fi
exit "$rc"
