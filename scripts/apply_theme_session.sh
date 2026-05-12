#!/usr/bin/env bash
# Apply WhiteSur-Dark theme and os.svg wallpaper. Intended to run from XFCE autostart
# (same session as xfce4), so DISPLAY and DBUS_SESSION_BUS_ADDRESS are already set.
set -e
xfconf-query -c xsettings -p /Net/ThemeName -n -t string -s WhiteSur-Dark 2>/dev/null || xfconf-query -c xsettings -p /Net/ThemeName -s WhiteSur-Dark
xfconf-query -c xfwm4 -p /general/theme -n -t string -s WhiteSur-Dark 2>/dev/null || xfconf-query -c xfwm4 -p /general/theme -s WhiteSur-Dark
xfconf-query -c xsettings -p /Net/IconThemeName -n -t string -s Adwaita 2>/dev/null || xfconf-query -c xsettings -p /Net/IconThemeName -s Adwaita
W=/usr/share/desktop-base/active-theme/wallpaper/contents/images/1920x1080.svg
for p in $(xfconf-query -c xfce4-desktop -l 2>/dev/null | sed -n 's|^\(/backdrop/screen0/monitor[^/]*/workspace[0-9]*/\)last-image$|\1|p'); do
  xfconf-query -c xfce4-desktop -p "${p}last-image" -s "$W" 2>/dev/null || true
  xfconf-query -c xfce4-desktop -p "${p}image-style" -n -t int -s 5 2>/dev/null || true
done

# Disable screen lock / blanking. On this remote desktop the session is
# already wallet-gated at the gate; an idle lockscreen only adds UX friction
# and (crucially) its keyboard grab blocks xdotool/XTEST input from the
# WebRTC agent, leaving users unable to type the unlock password.
xset s off -dpms 2>/dev/null || true
xset s noblank 2>/dev/null || true

for key in \
  "/saver/enabled" \
  "/lock/enabled" \
  "/saver/idle-activation/enabled"; do
  xfconf-query -c xfce4-screensaver -p "$key" -n -t bool -s false 2>/dev/null \
    || xfconf-query -c xfce4-screensaver -p "$key" -s false 2>/dev/null || true
done

# Stop any screensaver/locker that may already be running for this session.
pkill -f xfce4-screensaver 2>/dev/null || true
pkill -f xss-lock          2>/dev/null || true
pkill -f light-locker      2>/dev/null || true
pkill -f xscreensaver      2>/dev/null || true
