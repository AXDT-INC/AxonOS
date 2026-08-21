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

# Initialize IPFS for aXonian user.  Supervisord owns the daemon process;
# keeping startup limited to repository/config preparation lets desktop services
# begin without an unrelated fixed IPFS readiness delay.
echo "Initializing IPFS..."
su - aXonian -c 'ipfs init --profile=server' || echo "IPFS already initialized or failed to initialize"

# Configure IPFS bind addresses (runtime-configurable via env). Tenant sessions
# and the multi-user central container default to loopback so their unauthenticated
# IPFS control APIs are never exposed laterally. Standalone deployments preserve
# the historical all-interface default unless explicitly overridden.
_axonos_truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}
_multi_user=$(echo "${AXGT_USER_CONTAINER_ENABLED:-}" | tr '[:upper:]' '[:lower:]')
if [ -n "${AXGT_SESSION_ID:-}" ] || _axonos_truthy "${_multi_user}"; then
    IPFS_API_BIND="${IPFS_API_BIND:-127.0.0.1}"
    IPFS_GATEWAY_BIND="${IPFS_GATEWAY_BIND:-127.0.0.1}"
else
    IPFS_API_BIND="${IPFS_API_BIND:-0.0.0.0}"
    IPFS_GATEWAY_BIND="${IPFS_GATEWAY_BIND:-0.0.0.0}"
fi
IPFS_API_PORT="${IPFS_API_PORT:-5001}"
IPFS_GATEWAY_PORT="${IPFS_GATEWAY_PORT:-8080}"

echo "Configuring IPFS bind addresses..."
su - aXonian -c "ipfs config Addresses.API \"/ip4/${IPFS_API_BIND}/tcp/${IPFS_API_PORT}\""
su - aXonian -c "ipfs config Addresses.Gateway \"/ip4/${IPFS_GATEWAY_BIND}/tcp/${IPFS_GATEWAY_PORT}\""

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
    if _axonos_truthy "${_uc}"; then
        # The central multi-user container is control-plane only. Tenant
        # launchers set both values explicitly for each desktop, so a stale
        # operator env must not resurrect a shared base desktop/agent.
        export AXGT_DESKTOP_ENABLED=false
        export WEBRTC_AGENT_ENABLED=false
    else
        if [ -z "${AXGT_DESKTOP_ENABLED:-}" ]; then
            export AXGT_DESKTOP_ENABLED=true
        fi
        if [ -z "${WEBRTC_AGENT_ENABLED:-}" ]; then
            export WEBRTC_AGENT_ENABLED=true
        fi
    fi
fi

# Self-heal home volumes whose filesystem root is not owned by the session user.
# A loop-ext4 volume formatted without root_owner comes up root:root, and Docker
# skips its image-skeleton copy-up because the fresh fs already holds lost+found;
# the session user then cannot write $HOME, so xfce4-session and jupyterlab
# crash-loop and the desktop streams a black screen. Ownership of the home root
# is fixed non-recursively (user files inside are already correct on healthy
# volumes), .config is fixed recursively only when it was created root-owned by
# an earlier launch, and missing shell dotfiles are restored from /etc/skel.
if [ -d /home/aXonian ] && [ "$(stat -c %u /home/aXonian)" != "1000" ]; then
    echo "startup: home volume root owned by $(stat -c %u:%g /home/aXonian); repairing ownership"
    chown 1000:1000 /home/aXonian
    if [ -d /home/aXonian/.config ] && [ "$(stat -c %u /home/aXonian/.config)" = "0" ]; then
        chown -R 1000:1000 /home/aXonian/.config
    fi
fi
for skel_file in /etc/skel/.bashrc /etc/skel/.profile /etc/skel/.bash_logout; do
    dest="/home/aXonian/$(basename "$skel_file")"
    if [ -f "$skel_file" ] && [ ! -e "$dest" ]; then
        install -m 644 -o aXonian -g aXonian "$skel_file" "$dest"
    fi
done

# The wallet-persistent home volume mounts OVER the image's /home/aXonian, so its
# dotfiles mask whatever the image ships and no rebuild can reach them. Volumes
# provisioned before the interpreter-prefix fix still carry a PATH line that shadows
# the system python3 (torch/CUDA) with the 3D viewer's bundled interpreter; the same
# applies to any such volume restored from a backup. Strip it here, after the mount
# and before any user shell starts, so those homes self-heal at launch.
# Anchored to the exact line the image used to write, so a conda prefix a user added
# themselves is left alone. Idempotent. Removable once no such volume remains.
for rc in /home/aXonian/.bashrc /home/aXonian/.profile; do
    if [ -f "$rc" ] && grep -q '^export PATH="/opt/conda/bin:\$PATH"$' "$rc" 2>/dev/null; then
        sed -i '\#^export PATH="/opt/conda/bin:\$PATH"$#d' "$rc" \
            && echo "startup: removed stale interpreter-prefix PATH line from $rc"
    fi
done

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

# Direct-SSH session setup (AXGT_SSH_ENABLED=true): the landing-page SSH toggle
# launches a headless session (no X desktop / WebRTC) reachable only via sshd.
# Configure pubkey auth + host keys here, before supervisord starts sshd. The
# public key arriving in AXGT_SSH_PUBKEY was already validated gate-side
# (single line, known key type) so it is safe to write verbatim.
_ssh_on=$(echo "${AXGT_SSH_ENABLED:-}" | tr '[:upper:]' '[:lower:]')
if _axonos_truthy "${_ssh_on}"; then
    echo "AXGT_SSH_ENABLED: configuring direct SSH access for aXonian..."

    # Researcher's authorized key (single key; latest claim wins).
    install -d -m 700 -o aXonian -g aXonian /home/aXonian/.ssh
    if [ -n "${AXGT_SSH_PUBKEY:-}" ]; then
        printf '%s\n' "${AXGT_SSH_PUBKEY}" > /home/aXonian/.ssh/authorized_keys
        chmod 600 /home/aXonian/.ssh/authorized_keys
        chown aXonian:aXonian /home/aXonian/.ssh/authorized_keys
    else
        echo "WARNING: AXGT_SSH_ENABLED set but AXGT_SSH_PUBKEY empty; no key installed."
    fi

    # Persist host keys under the (per-wallet) home volume so a returning wallet
    # keeps a stable fingerprint — no known_hosts churn between sessions. Kept
    # root:600 so the unprivileged login user cannot read the host private keys.
    HOSTKEY_DIR=/home/aXonian/.config/axonos/ssh
    install -d -m 700 -o root -g root "$HOSTKEY_DIR"
    chown root:root "$HOSTKEY_DIR" || exit 1
    chmod 700 "$HOSTKEY_DIR" || exit 1
    for key_path in \
        "$HOSTKEY_DIR/ssh_host_ed25519_key" \
        "$HOSTKEY_DIR/ssh_host_ed25519_key.pub" \
        "$HOSTKEY_DIR/ssh_host_rsa_key" \
        "$HOSTKEY_DIR/ssh_host_rsa_key.pub"; do
        if [ -L "$key_path" ]; then
            echo "ERROR: refusing symlinked SSH host-key path: $key_path" >&2
            exit 1
        fi
    done
    [ -f "$HOSTKEY_DIR/ssh_host_ed25519_key" ] || ssh-keygen -q -t ed25519 -N '' -f "$HOSTKEY_DIR/ssh_host_ed25519_key"
    [ -f "$HOSTKEY_DIR/ssh_host_rsa_key" ] || ssh-keygen -q -t rsa -b 4096 -N '' -f "$HOSTKEY_DIR/ssh_host_rsa_key"
    chown root:root "$HOSTKEY_DIR"/ssh_host_ed25519_key "$HOSTKEY_DIR"/ssh_host_rsa_key || exit 1
    chmod 600 "$HOSTKEY_DIR"/ssh_host_ed25519_key "$HOSTKEY_DIR"/ssh_host_rsa_key || exit 1
    # Public keys are derived from the protected private keys on every launch;
    # never trust a persisted .pub file that may be stale or mismatched.
    ssh-keygen -y -f "$HOSTKEY_DIR/ssh_host_ed25519_key" > "$HOSTKEY_DIR/ssh_host_ed25519_key.pub.tmp" || exit 1
    ssh-keygen -y -f "$HOSTKEY_DIR/ssh_host_rsa_key" > "$HOSTKEY_DIR/ssh_host_rsa_key.pub.tmp" || exit 1
    mv -f "$HOSTKEY_DIR/ssh_host_ed25519_key.pub.tmp" "$HOSTKEY_DIR/ssh_host_ed25519_key.pub" || exit 1
    mv -f "$HOSTKEY_DIR/ssh_host_rsa_key.pub.tmp" "$HOSTKEY_DIR/ssh_host_rsa_key.pub" || exit 1
    chown root:root "$HOSTKEY_DIR"/ssh_host_*_key "$HOSTKEY_DIR"/ssh_host_*_key.pub || exit 1
    chmod 600 "$HOSTKEY_DIR"/ssh_host_*_key || exit 1
    chmod 644 "$HOSTKEY_DIR"/ssh_host_*_key.pub || exit 1
    cp -f "$HOSTKEY_DIR"/ssh_host_*_key /etc/ssh/ || exit 1
    cp -f "$HOSTKEY_DIR"/ssh_host_*_key.pub /etc/ssh/ || exit 1
    chown root:root /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub || exit 1
    chmod 600 /etc/ssh/ssh_host_*_key || exit 1
    chmod 644 /etc/ssh/ssh_host_*_key.pub || exit 1
    install -d -m 755 -o root -g root /run/axonos
    FINGERPRINT_TMP=$(mktemp /run/axonos/ssh-host-ed25519.sha256.XXXXXX) || exit 1
    if ! FINGERPRINT_LINE=$(ssh-keygen -lf "$HOSTKEY_DIR/ssh_host_ed25519_key.pub" -E sha256 2>/dev/null); then
        rm -f "$FINGERPRINT_TMP"
        echo "ERROR: could not derive SSH host-key fingerprint" >&2
        exit 1
    fi
    FINGERPRINT_VALUE=$(printf '%s\n' "$FINGERPRINT_LINE" | awk '{print $2}') || {
        rm -f "$FINGERPRINT_TMP"
        echo "ERROR: could not parse SSH host-key fingerprint" >&2
        exit 1
    }
    printf '%s\n' "$FINGERPRINT_VALUE" > "$FINGERPRINT_TMP" || exit 1
    if ! grep -Eq '^SHA256:[A-Za-z0-9+/]{43}$' "$FINGERPRINT_TMP"; then
        rm -f "$FINGERPRINT_TMP"
        echo "ERROR: derived SSH host-key fingerprint has an invalid format" >&2
        exit 1
    fi
    chown root:root "$FINGERPRINT_TMP" || exit 1
    chmod 644 "$FINGERPRINT_TMP" || exit 1
    mv -f "$FINGERPRINT_TMP" /run/axonos/ssh-host-ed25519.sha256 || exit 1

    # Hardened drop-in. The Ubuntu sshd_config reads sshd_config.d/*.conf via an
    # Include at the TOP of the file, so these directives win over later defaults
    # (sshd honours the first occurrence of each keyword). Ensure that Include is
    # present in case the base image lacks it.
    mkdir -p /etc/ssh/sshd_config.d
    if ! grep -q 'sshd_config.d/\*.conf' /etc/ssh/sshd_config 2>/dev/null; then
        printf 'Include /etc/ssh/sshd_config.d/*.conf\n%s' "$(cat /etc/ssh/sshd_config 2>/dev/null)" > /etc/ssh/sshd_config.axtmp \
            && mv /etc/ssh/sshd_config.axtmp /etc/ssh/sshd_config
    fi
    cat > /etc/ssh/sshd_config.d/axonos.conf <<'SSHD'
PasswordAuthentication no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
AllowUsers aXonian
X11Forwarding no
PrintMotd no
MaxAuthTries 3
MaxStartups 10:30:60
ClientAliveInterval 120
ClientAliveCountMax 5
SSHD

    # aXonian needs a login shell and a non-locked (but unusable) password so PAM
    # account checks permit pubkey login; '*' blocks password auth without the
    # locked-account ('!') state that pam_unix can reject.
    usermod -s /bin/bash aXonian 2>/dev/null || true
    usermod -p '*' aXonian 2>/dev/null || true
fi

# Start supervisord
/usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf &
SUPERVISOR_PID=$!

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

# A Docker healthcheck makes listener failure visible, but Compose does not
# restart merely-unhealthy containers. If a critical Supervisor child exhausts
# its retries, terminate Supervisor so the container exits and restart policy
# can recover the complete process set.
critical_supervisor_child_is_terminal() {
    local statuses
    # supervisorctl intentionally exits nonzero for stopped states. Preserve its
    # output instead of treating FATAL/EXITED as a query failure and skipping it.
    statuses=$(
        "${SUPERVISORCTL_BIN:-/usr/bin/supervisorctl}" \
            -c "${SUPERVISOR_CONFIG_PATH:-/etc/supervisor/conf.d/supervisord.conf}" \
            status axgt-api novnc webrtc-agent-gate webrtc-agent \
                x11vnc xorg-nvidia xfce4 \
            2>/dev/null || true
    )
    case "$statuses" in
        *" FATAL "*|*" EXITED "*) return 0 ;;
        *) return 1 ;;
    esac
}

critical_supervisor_listeners_ready() {
    local -a ports=(6080 8889)
    if _axonos_truthy "${WEBRTC_ENABLED:-false}"; then
        ports+=(8890)
    fi
    if _axonos_truthy "${AXGT_DESKTOP_ENABLED:-true}"; then
        ports+=(5901)
    fi
    "${AXONOS_LISTENER_PROBE_BIN:-/usr/bin/python3}" -c '
import socket
import sys

for raw_port in sys.argv[1:]:
    connection = socket.create_connection(("127.0.0.1", int(raw_port)), timeout=2)
    connection.close()
' "${ports[@]}" 2>/dev/null
}

monitor_critical_supervisor_children() {
    local listener_failures=0
    local max_listener_failures=6
    while kill -0 "$SUPERVISOR_PID" 2>/dev/null; do
        sleep 10
        if critical_supervisor_child_is_terminal; then
            echo "Critical Supervisor child entered FATAL/EXITED; restarting container." >&2
            kill -TERM "$SUPERVISOR_PID" 2>/dev/null || true
            return
        fi
        if critical_supervisor_listeners_ready; then
            listener_failures=0
            continue
        fi
        listener_failures=$((listener_failures + 1))
        if [ "$listener_failures" -ge "$max_listener_failures" ]; then
            echo "Critical Supervisor listeners failed ${listener_failures} consecutive readiness checks; restarting container." >&2
            kill -TERM "$SUPERVISOR_PID" 2>/dev/null || true
            return
        fi
    done
}
# Launcher-managed tenants use an external lifecycle/reconciliation path and
# are intentionally not self-terminated here. The central/legacy container has
# Docker restart policy and owns the listeners covered by its healthcheck.
if [ -z "${AXGT_SESSION_ID:-}" ]; then
    monitor_critical_supervisor_children &
fi

# Supervisord is the container's service owner. Propagate its death to Docker so
# restart policy can recover instead of leaving a false "running" shell.
wait "$SUPERVISOR_PID"
