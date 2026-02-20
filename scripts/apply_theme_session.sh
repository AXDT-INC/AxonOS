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
