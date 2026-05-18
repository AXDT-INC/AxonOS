#!/bin/sh
# After apt layers, libglx.so may still point at Mesa; Xorg then loads two GLX vendors and
# SIGSEGVs ("Another vendor is already registered for screen 0"). Run this only after
# xserver-xorg-video-nvidia is installed. Invoked from Dockerfile (avoid RUN "$$VAR" — sh treats $$ as PID).
set -e
GLX_EXT=/usr/lib/xorg/modules/extensions
NVGLX="$(ls -1 "$GLX_EXT"/libglxserver_nvidia.so.* 2>/dev/null | sort -V | tail -1)"
if [ -z "$NVGLX" ]; then
  echo "axonos: no libglxserver_nvidia.* under $GLX_EXT"
  ls -la "$GLX_EXT" || true
  exit 1
fi
rm -f "$GLX_EXT/libglx.so"
ln -sf "$NVGLX" "$GLX_EXT/libglx.so"
ls -la "$GLX_EXT/libglx.so"
