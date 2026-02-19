#!/usr/bin/env bash
# Apply WhiteSur-Dark theme and os.svg wallpaper for AxonOS (run after XFCE is up).
su - aXonian -c "
DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority \
xfconf-query -c xsettings -p /Net/ThemeName -n -t string -s WhiteSur-Dark 2>/dev/null || \
DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority xfconf-query -c xsettings -p /Net/ThemeName -s WhiteSur-Dark;

DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority \
xfconf-query -c xfwm4 -p /general/theme -n -t string -s WhiteSur-Dark 2>/dev/null || \
DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority xfconf-query -c xfwm4 -p /general/theme -s WhiteSur-Dark;

DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority \
xfconf-query -c xsettings -p /Net/IconThemeName -n -t string -s Adwaita 2>/dev/null || \
DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority xfconf-query -c xsettings -p /Net/IconThemeName -s Adwaita;

W=/usr/share/desktop-base/active-theme/wallpaper/contents/images/1920x1080.svg
for p in \$(DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority xfconf-query -c xfce4-desktop -l | sed -n \"s|^\\(/backdrop/screen0/monitor[^/]*/workspace[0-9]*/\\)last-image$|\\1|p\"); do
  DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority xfconf-query -c xfce4-desktop -p \"\${p}last-image\" -s \"\$W\"
  DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority xfconf-query -c xfce4-desktop -p \"\${p}image-style\" -n -t int -s 5 2>/dev/null || true
done

DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority xfce4-panel -r 2>/dev/null || true
"