#!/usr/bin/env bash
# In-container XFCE theme/wallpaper fixer for AxonOS.
# Run inside container:
#   /usr/local/bin/post_deploy_theme_fix.sh
# Or from host:
#   docker exec axonos /usr/local/bin/post_deploy_theme_fix.sh

set -euo pipefail

TARGET_WALL="/usr/share/desktop-base/active-theme/wallpaper/contents/images/1920x1080.svg"
TARGET_THEME="WhiteSur-Dark"
TARGET_ICON="Adwaita"

run_user() {
  su - aXonian -c "DISPLAY=:0 XAUTHORITY=/home/aXonian/.Xauthority $*"
}

echo "== assets =="
ls -l "${TARGET_WALL}" || true
ls -ld /usr/share/themes/WhiteSur-Dark || true
echo

theme_now="$(run_user "xfconf-query -c xsettings -p /Net/ThemeName" 2>/dev/null || true)"
wm_now="$(run_user "xfconf-query -c xfwm4 -p /general/theme" 2>/dev/null || true)"
icon_now="$(run_user "xfconf-query -c xsettings -p /Net/IconThemeName" 2>/dev/null || true)"

mapfile -t wall_paths < <(
  run_user "xfconf-query -c xfce4-desktop -l" 2>/dev/null | \
    sed -n 's|^\(/backdrop/screen0/monitor[^/]*/workspace[0-9]*/last-image\)$|\1|p'
)

need_apply=0
if [[ "${theme_now}" != "${TARGET_THEME}" || "${wm_now}" != "${TARGET_THEME}" || "${icon_now}" != "${TARGET_ICON}" ]]; then
  need_apply=1
fi

if (( ${#wall_paths[@]} == 0 )); then
  need_apply=1
else
  for p in "${wall_paths[@]}"; do
    current_wall="$(run_user "xfconf-query -c xfce4-desktop -p ${p}" 2>/dev/null || true)"
    if [[ "${current_wall}" != "${TARGET_WALL}" ]]; then
      need_apply=1
      break
    fi
  done
fi

echo "== current =="
echo "ThemeName: ${theme_now:-<unset>}"
echo "WM Theme:  ${wm_now:-<unset>}"
echo "IconTheme: ${icon_now:-<unset>}"
if (( ${#wall_paths[@]} > 0 )); then
  for p in "${wall_paths[@]}"; do
    v="$(run_user "xfconf-query -c xfce4-desktop -p ${p}" 2>/dev/null || true)"
    echo "${p} => ${v}"
  done
else
  echo "No xfce4-desktop wallpaper keys detected."
fi
echo

if (( need_apply == 0 )); then
  echo "Theme/wallpaper already correct. No changes needed."
  exit 0
fi

echo "Applying theme and wallpaper..."

run_user "xfconf-query -c xsettings -p /Net/ThemeName -n -t string -s ${TARGET_THEME}" 2>/dev/null || \
run_user "xfconf-query -c xsettings -p /Net/ThemeName -s ${TARGET_THEME}" || true

run_user "xfconf-query -c xfwm4 -p /general/theme -n -t string -s ${TARGET_THEME}" 2>/dev/null || \
run_user "xfconf-query -c xfwm4 -p /general/theme -s ${TARGET_THEME}" || true

run_user "xfconf-query -c xsettings -p /Net/IconThemeName -n -t string -s ${TARGET_ICON}" 2>/dev/null || \
run_user "xfconf-query -c xsettings -p /Net/IconThemeName -s ${TARGET_ICON}" || true

mapfile -t monitors < <(
  run_user "xfconf-query -c xfce4-desktop -l" 2>/dev/null | \
    sed -n 's|^/backdrop/screen0/\(monitor[^/]*\)/workspace[0-9]*/last-image$|\1|p' | sort -u
)

if (( ${#monitors[@]} == 0 )); then
  monitors=(monitor0 monitor0-0)
fi

for mon in "${monitors[@]}"; do
  for ws in 0 1 2 3; do
    run_user "xfconf-query -c xfce4-desktop -p /backdrop/screen0/${mon}/workspace${ws}/last-image -n -t string -s ${TARGET_WALL}" 2>/dev/null || \
    run_user "xfconf-query -c xfce4-desktop -p /backdrop/screen0/${mon}/workspace${ws}/last-image -s ${TARGET_WALL}" || true
    run_user "xfconf-query -c xfce4-desktop -p /backdrop/screen0/${mon}/workspace${ws}/image-style -n -t int -s 5" 2>/dev/null || true
  done
done

run_user "xfce4-panel -r" 2>/dev/null || true
sleep 1

echo
echo "== final =="
run_user "xfconf-query -c xsettings -p /Net/ThemeName" || true
run_user "xfconf-query -c xfwm4 -p /general/theme" || true
run_user "xfconf-query -c xsettings -p /Net/IconThemeName" || true
run_user "xfconf-query -c xfce4-desktop -l" 2>/dev/null | \
  sed -n 's|^\(/backdrop/screen0/monitor[^/]*/workspace[0-9]*/last-image\)$|\1|p' | \
  while read -r p; do
    val="$(run_user "xfconf-query -c xfce4-desktop -p ${p}" 2>/dev/null || true)"
    echo "${p} => ${val}"
  done
