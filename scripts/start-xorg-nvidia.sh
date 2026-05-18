#!/bin/bash
# Start Xorg :0 with NVIDIA driver for VirtualGL when GPU is present.
# If no NVIDIA GPU, sleep forever so supervisord does not keep retrying.
set -e
if ! nvidia-smi &>/dev/null; then
  echo "No NVIDIA GPU detected; skipping X :0 (VirtualGL will not be used)."
  exec sleep infinity
fi
BUS_ID_RAW="$(nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader 2>/dev/null | head -n 1 | tr -d '[:space:]')"
CONFIG_SRC="/etc/X11/xorg.conf.nvidia"
CONFIG_TMP="/tmp/xorg.conf.nvidia"
if [ -n "$BUS_ID_RAW" ]; then
  # Convert 00000000:01:00.0 -> PCI:1:0:0 (avoid awk strtonum dependency)
  IFS=':.' read -r _domain bus_hex dev_hex func_hex <<<"$BUS_ID_RAW"
  bus_dec="$(printf '%d' "0x${bus_hex}")"
  dev_dec="$(printf '%d' "0x${dev_hex}")"
  func_dec="$(printf '%d' "0x${func_hex}")"
  BUS_ID_FMT="PCI:${bus_dec}:${dev_dec}:${func_dec}"
  echo "Using NVIDIA BusID: $BUS_ID_FMT"
  awk -v busid="$BUS_ID_FMT" '
    $0 ~ /Section "Device"/ {print; in_dev=1; next}
    in_dev && $0 ~ /Driver "nvidia"/ {print; print "  BusID \"" busid "\""; next}
    $0 ~ /EndSection/ && in_dev {in_dev=0}
    {print}
  ' "$CONFIG_SRC" > "$CONFIG_TMP"
else
  echo "No BusID detected; using default Xorg config."
  cp "$CONFIG_SRC" "$CONFIG_TMP"
fi
# Do not use -keeptty here: supervisord/docker often have no controlling TTY, and Xorg
# exits immediately (clients then see "unable to open display :0"). See VirtualGL headless NV.
echo "start-xorg-nvidia: exec Xorg :0 config=${CONFIG_TMP}"
exec /usr/bin/Xorg :0 -config "$CONFIG_TMP" -noreset -nolisten tcp -novtswitch -sharevts \
  -logfile /var/log/Xorg.0.log
