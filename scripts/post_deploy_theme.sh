#!/usr/bin/env bash
# Apply WhiteSur-Dark theme and os.svg wallpaper for AxonOS (run after XFCE is up).
# xfconf-query needs DBUS_SESSION_BUS_ADDRESS of the running XFCE session.
# Discover it as aXonian (same user as xfce4-session) to avoid /proc read permission denied.

su - aXonian -c "
export DISPLAY=:0
export XAUTHORITY=/home/aXonian/.Xauthority
pid=\$(pgrep -f xfce4-session 2>/dev/null | head -1)
[ -z \"\$pid\" ] && pid=\$(pgrep -f xfconfd 2>/dev/null | head -1)
if [ -n \"\$pid\" ] && [ -r /proc/\$pid/environ ]; then
  export DBUS_SESSION_BUS_ADDRESS=\$(tr '\\0' '\\n' < /proc/\$pid/environ 2>/dev/null | grep '^DBUS_SESSION_BUS_ADDRESS=' | cut -d= -f2-)
fi
[ -z \"\$DBUS_SESSION_BUS_ADDRESS\" ] && [ -S /run/user/\$(id -u)/bus ] && export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/\$(id -u)/bus

xfconf-query -c xsettings -p /Net/ThemeName -n -t string -s WhiteSur-Dark 2>/dev/null || xfconf-query -c xsettings -p /Net/ThemeName -s WhiteSur-Dark
xfconf-query -c xfwm4 -p /general/theme -n -t string -s WhiteSur-Dark 2>/dev/null || xfconf-query -c xfwm4 -p /general/theme -s WhiteSur-Dark
xfconf-query -c xsettings -p /Net/IconThemeName -n -t string -s Adwaita 2>/dev/null || xfconf-query -c xsettings -p /Net/IconThemeName -s Adwaita

W=/usr/share/desktop-base/active-theme/wallpaper/contents/images/1920x1080.svg
for p in \$(xfconf-query -c xfce4-desktop -l 2>/dev/null | sed -n \"s|^\\(/backdrop/screen0/monitor[^/]*/workspace[0-9]*/\\)last-image$|\\1|p\"); do
  xfconf-query -c xfce4-desktop -p \"\${p}last-image\" -s \"\$W\" 2>/dev/null || true
  xfconf-query -c xfce4-desktop -p \"\${p}image-style\" -n -t int -s 5 2>/dev/null || true
done
"