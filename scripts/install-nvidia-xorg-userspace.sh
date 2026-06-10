#!/bin/bash
# Install pinned NVIDIA Xorg/GL userspace (image build). Avoids Dockerfile $$ / $( ) escaping bugs.
set -euxo pipefail

ver_major="${NVIDIA_DRIVER_VERSION:-535}"
export NVIDIA_DRIVER_VERSION="${ver_major}"
export NVIDIA_DRIVER_PKG_VERSION="${NVIDIA_DRIVER_PKG_VERSION:-}"

apt-get update
NVIDIA_PKG_RESOLVED="$(
  /usr/local/bin/resolve-nvidia-driver-pkg-version.sh
)"
echo "axonos: NVIDIA_PKG_RESOLVED=${NVIDIA_PKG_RESOLVED}"

if echo "${NVIDIA_PKG_RESOLVED}" | grep -q '0ubuntu0.22.04'; then
  printf '%s\n' \
    'Package: libnvidia-* xserver-xorg-video-nvidia-* nvidia-kernel-common-* nvidia-firmware-*' \
    'Pin: release o=Ubuntu' \
    'Pin-Priority: 1001' \
    '' \
    'Package: libnvidia-* xserver-xorg-video-nvidia-* nvidia-kernel-common-* nvidia-firmware-*' \
    'Pin: origin developer.download.nvidia.com' \
    'Pin-Priority: 50' \
    > /etc/apt/preferences.d/axonos-nvidia.pref
else
  printf '%s\n' \
    'Package: libnvidia-* xserver-xorg-video-nvidia-* nvidia-kernel-common-* nvidia-firmware-*' \
    'Pin: origin developer.download.nvidia.com' \
    'Pin-Priority: 1001' \
    '' \
    'Package: libnvidia-* xserver-xorg-video-nvidia-* nvidia-kernel-common-* nvidia-firmware-*' \
    'Pin: release o=Ubuntu' \
    'Pin-Priority: 50' \
    > /etc/apt/preferences.d/axonos-nvidia.pref
fi

apt-get update
apt-get -o Dpkg::Options::=--force-unsafe-io install -y --no-install-recommends --allow-downgrades \
  "xserver-xorg-video-nvidia-${ver_major}=${NVIDIA_PKG_RESOLVED}" \
  "libnvidia-gl-${ver_major}=${NVIDIA_PKG_RESOLVED}" \
  "libnvidia-cfg1-${ver_major}=${NVIDIA_PKG_RESOLVED}" \
  "libnvidia-common-${ver_major}=${NVIDIA_PKG_RESOLVED}" \
  libglvnd0 libglx0 libegl1

if apt-cache madison "libnvidia-egl-${ver_major}" 2>/dev/null | awk '{print $3}' | grep -Fxq "${NVIDIA_PKG_RESOLVED}"; then
  apt-get -o Dpkg::Options::=--force-unsafe-io install -y --no-install-recommends --allow-downgrades \
    "libnvidia-egl-${ver_major}=${NVIDIA_PKG_RESOLVED}"
elif apt-cache madison "libnvidia-egl-${ver_major}-server" 2>/dev/null | awk '{print $3}' | grep -Fxq "${NVIDIA_PKG_RESOLVED}"; then
  apt-get -o Dpkg::Options::=--force-unsafe-io install -y --no-install-recommends --allow-downgrades \
    "libnvidia-egl-${ver_major}-server=${NVIDIA_PKG_RESOLVED}"
fi

if [ -d /usr/lib/x86_64-linux-gnu/nvidia ] && [ ! -d /usr/lib/x86_64-linux-gnu/nvidia/current ]; then
  ver="$(ls /usr/lib/x86_64-linux-gnu/nvidia | sort -V | tail -1)"
  ln -s "/usr/lib/x86_64-linux-gnu/nvidia/${ver}" /usr/lib/x86_64-linux-gnu/nvidia/current
fi

apt-get -o Dpkg::Options::=--force-unsafe-io install -y --reinstall --no-install-recommends --allow-downgrades \
  "xserver-xorg-video-nvidia-${ver_major}=${NVIDIA_PKG_RESOLVED}"

for pkg in \
  "xserver-xorg-video-nvidia-${ver_major}" \
  "libnvidia-gl-${ver_major}" \
  "libnvidia-cfg1-${ver_major}" \
  "libnvidia-common-${ver_major}"; do
  inst="$(dpkg-query -W -f='${Version}' "${pkg}" 2>/dev/null || true)"
  echo "axonos: ${pkg}=${inst}"
  [ "${inst}" = "${NVIDIA_PKG_RESOLVED}" ] || {
    echo "axonos: ${pkg} version mismatch (want ${NVIDIA_PKG_RESOLVED})"
    exit 1
  }
done

apt-get clean && rm -rf /var/lib/apt/lists/*
