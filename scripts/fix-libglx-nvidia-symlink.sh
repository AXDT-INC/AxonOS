#!/bin/sh
# After apt layers, libglx.so may still point at Mesa; Xorg then loads two GLX vendors and
# SIGSEGVs ("Another vendor is already registered for screen 0"). Run this only after
# xserver-xorg-video-nvidia is installed. Invoked from Dockerfile (avoid RUN "$$VAR" — sh treats $$ as PID).
set -e
GLX_EXT=/usr/lib/xorg/modules/extensions

# Prefer the Xorg extensions dir (usual on Ubuntu); some releases use
# /usr/lib/xorg/modules/updates/extensions; fall back to a broad /usr/lib search.
NVGLX=""
for search_root in \
  "$GLX_EXT" \
  /usr/lib/xorg/modules/updates/extensions \
  /usr/lib/x86_64-linux-gnu/nvidia/xorg \
  /usr/lib/x86_64-linux-gnu/nvidia/current/xorg; do
  [ -d "$search_root" ] || continue
  NVGLX=$(find "$search_root" -maxdepth 1 \( -name 'libglxserver_nvidia.so.*' -o -name 'libglxserver_nvidia.so' \) \( -type f -o -type l \) 2>/dev/null | sort -V | tail -1)
  [ -n "$NVGLX" ] && [ -e "$NVGLX" ] && break
  NVGLX=""
done
if [ -z "$NVGLX" ] || [ ! -e "$NVGLX" ]; then
  NVGLX=$(find /usr/lib /usr/lib64 -maxdepth 28 \( -name 'libglxserver_nvidia.so.*' -o -name 'libglxserver_nvidia.so' \) \( -type f -o -type l \) ! -name '*.debug' 2>/dev/null | sort -V | tail -1)
fi

if [ -z "$NVGLX" ] || [ ! -e "$NVGLX" ]; then
  echo "axonos: no libglxserver_nvidia found — is xserver-xorg-video-nvidia installed?"
  echo "axonos: listing $GLX_EXT:"
  ls -la "$GLX_EXT" 2>/dev/null || true
  echo "axonos: sample *glx* / *nvidia* under /usr/lib:"
  find /usr/lib /usr/lib64 -maxdepth 12 \( -iname '*glx*nvidia*' -o -iname '*nvidia*glx*' -o -name 'libglxserver_nvidia.so*' \) 2>/dev/null | head -50 || true
  exit 1
fi

NVGLX=$(readlink -f "$NVGLX")
mkdir -p "$GLX_EXT"
rm -f "$GLX_EXT/libglx.so"
ln -sf "$NVGLX" "$GLX_EXT/libglx.so"
ls -la "$GLX_EXT/libglx.so"
