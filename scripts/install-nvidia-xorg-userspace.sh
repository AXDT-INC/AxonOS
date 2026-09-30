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

# Retain the four resolver roots plus NVML/compute and kernel-common. Ask APT
# to derive their complete version-coherent dependency closure; pinning only
# the roots or the repository origin leaves split driver packages free to drift.
roots=(
  "xserver-xorg-video-nvidia-${ver_major}"
  "libnvidia-gl-${ver_major}"
  "libnvidia-cfg1-${ver_major}"
  "libnvidia-common-${ver_major}"
  "libnvidia-compute-${ver_major}"
  "nvidia-kernel-common-${ver_major}"
)
if apt-cache madison "libnvidia-egl-${ver_major}" 2>/dev/null | awk '{print $3}' | grep -Fxq "${NVIDIA_PKG_RESOLVED}"; then
  roots+=("libnvidia-egl-${ver_major}")
elif apt-cache madison "libnvidia-egl-${ver_major}-server" 2>/dev/null | awk '{print $3}' | grep -Fxq "${NVIDIA_PKG_RESOLVED}"; then
  roots+=("libnvidia-egl-${ver_major}-server")
fi

manifest=/usr/local/share/axonos/nvidia-userspace.txt
install -d /usr/local/share/axonos
/usr/bin/python3 /usr/local/bin/plan-nvidia-userspace.py plan \
  --version "${NVIDIA_PKG_RESOLVED}" \
  --preferences /etc/apt/preferences.d/axonos-nvidia.pref \
  --extra libglvnd0 --extra libglx0 --extra libegl1 \
  --manifest "${manifest}" "${roots[@]}"
mapfile -t pins < "${manifest}"
[ "${#pins[@]}" -ge "${#roots[@]}" ]

# Simulate the exact transaction before downloading/unpacking anything. Keep
# the exact-version preferences for later apt layers; reject fallback versions.
apt-get --simulate --no-remove install --no-install-recommends --allow-downgrades \
  "${pins[@]}" libglvnd0 libglx0 libegl1
apt-get -o Dpkg::Options::=--force-unsafe-io install -y --no-remove --no-install-recommends --allow-downgrades \
  "${pins[@]}" libglvnd0 libglx0 libegl1

if [ -d /usr/lib/x86_64-linux-gnu/nvidia ] && [ ! -d /usr/lib/x86_64-linux-gnu/nvidia/current ]; then
  ver="$(ls /usr/lib/x86_64-linux-gnu/nvidia | sort -V | tail -1)"
  ln -s "/usr/lib/x86_64-linux-gnu/nvidia/${ver}" /usr/lib/x86_64-linux-gnu/nvidia/current
fi

apt-get -o Dpkg::Options::=--force-unsafe-io install -y --no-remove --reinstall --no-install-recommends --allow-downgrades \
  "xserver-xorg-video-nvidia-${ver_major}=${NVIDIA_PKG_RESOLVED}"

for pkg in \
  "xserver-xorg-video-nvidia-${ver_major}" \
  "libnvidia-gl-${ver_major}" \
  "libnvidia-cfg1-${ver_major}" \
  "libnvidia-common-${ver_major}" \
  "libnvidia-compute-${ver_major}"; do
  inst="$(dpkg-query -W -f='${Version}' "${pkg}" 2>/dev/null || true)"
  echo "axonos: ${pkg}=${inst}"
  [ "${inst}" = "${NVIDIA_PKG_RESOLVED}" ] || {
    echo "axonos: ${pkg} version mismatch (want ${NVIDIA_PKG_RESOLVED})"
    exit 1
  }
done

# Attest every transitive driver component, not just the original five checks.
/usr/bin/python3 /usr/local/bin/plan-nvidia-userspace.py verify --manifest "${manifest}"
apt-get clean && rm -rf /var/lib/apt/lists/*
