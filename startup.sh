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

# Set hostname at runtime
hostname AxonOS
if ! grep -q "AxonOS" /etc/hosts; then
    echo "127.0.0.1 AxonOS" >> /etc/hosts
fi

# Refresh NVSHMEM library paths at runtime so GUI shells match SSH
NVSHMEM_DIRS="$(ls -d \
  /opt/nvidia/hpc_sdk/Linux_x86_64/*/comm_libs/nvshmem*/lib \
  /opt/nvidia/hpc_sdk/*/comm_libs/nvshmem*/lib \
  /opt/nvidia/hpc_sdk/*/comm_libs/*/nvshmem*/lib 2>/dev/null | sort -u)"
if [ -n "$NVSHMEM_DIRS" ]; then
    echo "$NVSHMEM_DIRS" > /etc/ld.so.conf.d/nvshmem.conf
    ldconfig
fi

# Initialize IPFS for aXonian user
echo "Initializing IPFS..."
su - aXonian -c 'ipfs init --profile=server' || echo "IPFS already initialized or failed to initialize"

# Configure IPFS bind addresses (runtime-configurable via env).
# Defaults preserve prior behavior unless overridden.
IPFS_API_BIND="${IPFS_API_BIND:-0.0.0.0}"
IPFS_API_PORT="${IPFS_API_PORT:-5001}"
IPFS_GATEWAY_BIND="${IPFS_GATEWAY_BIND:-0.0.0.0}"
IPFS_GATEWAY_PORT="${IPFS_GATEWAY_PORT:-8080}"

echo "Configuring IPFS bind addresses..."
su - aXonian -c "ipfs config Addresses.API \"/ip4/${IPFS_API_BIND}/tcp/${IPFS_API_PORT}\""
su - aXonian -c "ipfs config Addresses.Gateway \"/ip4/${IPFS_GATEWAY_BIND}/tcp/${IPFS_GATEWAY_PORT}\""

# Start IPFS daemon in background
echo "Starting IPFS daemon..."
su - aXonian -c 'ipfs daemon --enable-gc --routing=dht' &

# Wait a moment for IPFS to start and check status
sleep 3
echo "Checking IPFS status..."
su - aXonian -c 'ipfs id' || echo "IPFS still starting up..."

# Per-session desktops (axgt-session-*): base axonos is gate-only — no Xorg on GPU 0.
# Session runtimes set AXGT_SESSION_ID; respect explicit overrides if already set.
if [ -n "${AXGT_SESSION_ID:-}" ]; then
    if [ -z "${AXGT_DESKTOP_ENABLED:-}" ]; then
        export AXGT_DESKTOP_ENABLED=true
    fi
    if [ -z "${WEBRTC_AGENT_ENABLED:-}" ]; then
        export WEBRTC_AGENT_ENABLED=true
    fi
elif [ -z "${AXGT_SESSION_ID:-}" ]; then
    _uc=$(echo "${AXGT_USER_CONTAINER_ENABLED:-}" | tr '[:upper:]' '[:lower:]')
    if [ "${_uc}" = "true" ] || [ "${_uc}" = "1" ] || [ "${_uc}" = "yes" ]; then
        if [ -z "${AXGT_DESKTOP_ENABLED:-}" ]; then
            export AXGT_DESKTOP_ENABLED=false
        fi
        if [ -z "${WEBRTC_AGENT_ENABLED:-}" ]; then
            export WEBRTC_AGENT_ENABLED=false
        fi
    else
        if [ -z "${AXGT_DESKTOP_ENABLED:-}" ]; then
            export AXGT_DESKTOP_ENABLED=true
        fi
        if [ -z "${WEBRTC_AGENT_ENABLED:-}" ]; then
            export WEBRTC_AGENT_ENABLED=true
        fi
    fi
fi

# Persist the selected environment template so the desktop session can align its
# hero app with the user's choice. XFCE is started by supervisord with a fixed
# environment= subset and does NOT inherit Docker ENV (see supervisord.conf), so
# the autostart launcher reads this file instead of AXONOS_SELECTED_TEMPLATE.
mkdir -p /home/aXonian/.config/axonos
if [ -n "${AXONOS_SELECTED_TEMPLATE:-}" ]; then
    printf '%s\n' "${AXONOS_SELECTED_TEMPLATE}" > /home/aXonian/.config/axonos/selected_template
else
    rm -f /home/aXonian/.config/axonos/selected_template 2>/dev/null || true
fi
chown -R aXonian:aXonian /home/aXonian/.config/axonos

# Start supervisord
/usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf &

# Wait for VNC server to start
sleep 5

# Create a script to run as aXonian
cat > /tmp/setup_x.sh << 'EOF'
#!/bin/bash

# Set DISPLAY variable
export DISPLAY=:0
export XAUTHORITY=/home/aXonian/.Xauthority

# Create .Xauthority if it doesn't exist
touch "$XAUTHORITY"

# Add local authorization
XAUTHORITY="$XAUTHORITY" xauth generate :0 . trusted

# Wait for X server to be fully ready
for i in {1..30}; do
    if XAUTHORITY="$XAUTHORITY" xset q &>/dev/null; then
        echo "X server is ready"
        break
    fi
    echo "Waiting for X server... ($i/30)"
    sleep 1
done

# Allow local connections
xhost +local:

# Wait a bit more for XFCE to initialize
sleep 10

# Apply WhiteSur theme (using the working script)
if [ -d "/usr/share/themes/WhiteSur-Dark" ]; then
    # Wait for xfconfd to be ready, then apply theme
    for i in {1..20}; do
        if DISPLAY=:0 xfconf-query -c xsettings -p /Net/ThemeName 2>/dev/null > /dev/null; then
            echo "xfconfd is ready, applying WhiteSur theme..."
            DISPLAY=:0 xfconf-query -c xsettings -p /Net/ThemeName -s "WhiteSur-Dark" 2>/dev/null
            DISPLAY=:0 xfconf-query -c xfwm4 -p /general/theme -s "WhiteSur-Dark" 2>/dev/null
            DISPLAY=:0 xfconf-query -c xsettings -p /Net/IconThemeName -s "Adwaita" 2>/dev/null
            echo "WhiteSur theme applied"

            # Enforce AxonOS wallpaper at runtime (Ubuntu XFCE can reset)
            WALLPAPER_PATH="/usr/share/desktop-base/active-theme/wallpaper/contents/images/1920x1080.svg"
            if [ -f "$WALLPAPER_PATH" ]; then
                MONS=$(DISPLAY=:0 xfconf-query -c xfce4-desktop -l 2>/dev/null | \
                    sed -n 's|^/backdrop/screen0/\(monitor[^/]*\)/workspace[0-9]\+/last-image$|\1|p' | sort -u)
                if [ -z "$MONS" ]; then
                    MONS="monitor0 monitor0-0"
                fi
                for MON in $MONS; do
                    for WS in 0 1 2 3; do
                        DISPLAY=:0 xfconf-query -c xfce4-desktop -p "/backdrop/screen0/${MON}/workspace${WS}/last-image" -n -t string -s "$WALLPAPER_PATH" 2>/dev/null || true
                        DISPLAY=:0 xfconf-query -c xfce4-desktop -p "/backdrop/screen0/${MON}/workspace${WS}/image-style" -n -t int -s 5 2>/dev/null || true
                    done
                done
            fi

            # Panel: transparent by default + ~2x height + use AxonOS icon for menu
            # NOTE: do this after xfconfd is ready so xfce4-panel channel is writable.
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /panels/panel-1/size -n -t uint -s 56 2>/dev/null || true
            # Panel length: with length-adjust=true this is stored as a percentage.
            # 50% of a 1920px-wide screen = 960px.
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /panels/panel-1/length -n -t double -s 50 2>/dev/null || true
            # Keep auto-length enabled so items never disappear
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /panels/panel-1/length-adjust -n -t bool -s true 2>/dev/null || true
            # Move panel further up (y=940) to give tooltips vertical space above the panel
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /panels/panel-1/position -n -t string -s "p=10;x=480;y=940" 2>/dev/null || true
            # Disable tooltips globally on the panel
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /panels/panel-1/show-tooltips -n -t bool -s false 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /panels/panel-1/background-style -n -t int -s 0 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /panels/panel-1/background-alpha -n -t uint -s 0 2>/dev/null || true
            # Don't reserve space on borders - allows tooltips to appear above panel
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /panels/panel-1/don-t-reserve-space-on-borders -n -t bool -s true 2>/dev/null || true

            # Separator plugins: force "Transparent" style (0) instead of visible line
            # plugin-3,6,8,10 are separators per /etc/xdg/xfce4/panel/default.xml
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-3/style -n -t int -s 0 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-6/style -n -t int -s 0 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-8/style -n -t int -s 0 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-10/style -n -t int -s 0 2>/dev/null || true

            if [ -f "/usr/share/novnc/icon.png" ]; then
                # applicationsmenu plugin is plugin-1 per /etc/xdg/xfce4/panel/default.xml
                DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-1/button-icon -n -t string -s "/usr/share/novnc/icon.png" 2>/dev/null || true
            fi
            # Disable tooltips for applications menu
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-1/show-tooltips -n -t bool -s false 2>/dev/null || true

            # Clock defaults (plugin-5) as per your screenshot
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/timezone -n -t string -s "UTC" 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/mode -n -t int -s 2 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/digital-layout -n -t int -s 0 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/digital-date-font -n -t string -s "Sans 23" 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/digital-time-font -n -t string -s "Sans 23" 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/digital-time-format -n -t string -s "%T" 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/show-seconds -n -t bool -s true 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/show-meridiem -n -t bool -s false 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/flash-separators -n -t bool -s true 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/tooltip-format -n -t string -s "%A %d %B %Y" 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-5/command -n -t string -s "" 2>/dev/null || true

            # Launcher plugins: hide labels to show only icons (images)
            # plugin-7: AxonOS Assistant, plugin-9: Talk to K
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-7/show-label -n -t bool -s false 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-7/names-visible -n -t bool -s false 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-7/show-tooltips -n -t bool -s false 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-7/disable-tooltips -n -t bool -s true 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-9/show-label -n -t bool -s false 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-9/names-visible -n -t bool -s false 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-9/show-tooltips -n -t bool -s false 2>/dev/null || true
            DISPLAY=:0 xfconf-query -c xfce4-panel -p /plugins/plugin-9/disable-tooltips -n -t bool -s true 2>/dev/null || true

            # Disable GTK tooltips globally (GTK3) + restore dark theme
            mkdir -p /home/$USER/.config/gtk-3.0
            cat > /home/$USER/.config/gtk-3.0/settings.ini << 'GTK3'
gtk-enable-tooltips=0
gtk-theme-name=WhiteSur-Dark
gtk-icon-theme-name=Adwaita
gtk-application-prefer-dark-theme=1
GTK3
            chown -R $USER:$USER /home/$USER/.config/gtk-3.0

            break
        fi
        sleep 1
    done
fi

# Try to get root window geometry using xwininfo
if DISPLAY=:0 xwininfo -root > ~/.vnc/geometry.log 2>&1; then
    # Extract dimensions from xwininfo output
    WIDTH=$(grep 'Width:' ~/.vnc/geometry.log | awk '{print $2}')
    HEIGHT=$(grep 'Height:' ~/.vnc/geometry.log | awk '{print $2}')
    
    if [ ! -z "$WIDTH" ] && [ ! -z "$HEIGHT" ]; then
        # Calculate cursor position
        X=$((WIDTH * 95 / 100))
        Y=1060
        
        # Try to move cursor using xte
        echo "Attempting to move cursor to $X,$Y" >> ~/.vnc/cursor.log
        for i in {1..5}; do
            if DISPLAY=:0 xte "mousemove $X $Y" 2>/dev/null; then
                echo "Successfully moved cursor using xte (attempt $i)" >> ~/.vnc/cursor.log
                break
            else
                echo "Failed to move cursor using xte (attempt $i)" >> ~/.vnc/cursor.log
                sleep 1
            fi
        done
        
        echo "Screen dimensions from xwininfo: ${WIDTH}x${HEIGHT}" >> ~/.vnc/geometry.log
    else
        echo "Failed to parse dimensions from xwininfo output" >> ~/.vnc/geometry.log
    fi
else
    echo "Failed to get root window info" >> ~/.vnc/geometry.log
fi
EOF

# Make the script executable
chmod +x /tmp/setup_x.sh

# Gate-only base (AXGT_DESKTOP_ENABLED=false): skip XFCE/VNC setup — desktops run in axgt-session-*.
if [ "${AXGT_DESKTOP_ENABLED:-true}" != "false" ]; then
    su - aXonian -c '/tmp/setup_x.sh'
    ( sleep 35; /usr/local/bin/post_deploy_theme.sh ) &
    # Auto-launch the selected template's hero app once the desktop is up. Driven
    # from here (not XDG autostart) because ~/.config/autostart is on the persistent
    # home volume and would shadow any baked-in entry. The launcher reads the
    # template id from the file written above, self-gates if none is set, and
    # waits for X + xfce4-panel itself (no fixed delay needed here).
    ( su - aXonian -c 'DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority /usr/local/bin/apply_session_template.sh' ) &
    echo "== Xorg log =="; ls -l /var/log/Xorg.0.log 2>/dev/null || true
    test -f /var/log/Xorg.0.log && tail -n 80 /var/log/Xorg.0.log || true
    ls -l /tmp/.X11-unix/X0 /tmp/.X0-lock 2>/dev/null || true
    ps -ef | grep -E "[X]org" || true
    su - aXonian -c "DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority xset q" || true
else
    echo "AXGT_DESKTOP_ENABLED=false: gate-only container (no local X desktop)."
fi

# Keep the container running
tail -f /dev/null