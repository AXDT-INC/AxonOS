#!/bin/bash
# Resolve an apt version string that exists for all four NVIDIA Xorg/GL packages.
# Used at image build time (CUDA base + Ubuntu + CUDA apt repos).
# Prints the version on stdout; logs resolution notes to stderr.
set -euo pipefail

ver_major="${NVIDIA_DRIVER_VERSION:-535}"
requested="${NVIDIA_DRIVER_PKG_VERSION:-}"

pkgs=(
  "xserver-xorg-video-nvidia-${ver_major}"
  "libnvidia-gl-${ver_major}"
  "libnvidia-cfg1-${ver_major}"
  "libnvidia-common-${ver_major}"
)

match_count() {
  local v="$1"
  local n=0
  for p in "${pkgs[@]}"; do
    if apt-cache madison "$p" 2>/dev/null | awk '{print $3}' | grep -Fxq "$v"; then
      n=$((n + 1))
    fi
  done
  echo "$n"
}

# Lower score = preferred (Ubuntu archive suffix beats CUDA repo suffix).
pref_score() {
  case "$1" in
    *-0ubuntu0.22.04.1) echo 1 ;;
    *-0ubuntu1) echo 2 ;;
    *-1ubuntu1) echo 3 ;;
    *) echo 9 ;;
  esac
}

collect_candidates() {
  for p in "${pkgs[@]}"; do
    apt-cache madison "$p" 2>/dev/null | awk '{print $3}'
  done | sort -u
}

pick_best() {
  local prefix="${1:-}"
  local best="" best_pref=99
  local cand pref cnt
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    if [ -n "$prefix" ] && [[ "$cand" != "${prefix}"* ]]; then
      continue
    fi
    cnt="$(match_count "$cand")"
    [ "$cnt" = 4 ] || continue
    pref="$(pref_score "$cand")"
    if [ -z "$best" ] || [ "$pref" -lt "$best_pref" ] || { [ "$pref" -eq "$best_pref" ] && [[ "$cand" > "$best" ]]; }; then
      best="$cand"
      best_pref="$pref"
    fi
  done < <(collect_candidates)
  [ -n "$best" ] && echo "$best"
}

if [ -n "$requested" ]; then
  if [ "$(match_count "$requested")" = 4 ]; then
    echo "$requested"
    exit 0
  fi
  base="$(echo "$requested" | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+' || true)"
  if [ -n "$base" ]; then
    resolved="$(pick_best "$base")"
    if [ -n "$resolved" ]; then
      echo "axonos: resolved NVIDIA_DRIVER_PKG_VERSION ${requested} -> ${resolved}" >&2
      echo "$resolved"
      exit 0
    fi
  fi
  echo "axonos: no installable 4-package NVIDIA set for ${requested} (branch ${ver_major})" >&2
  echo "axonos: host driver 535.288.01 often needs 535.288.01-0ubuntu1 in this image (not …-0ubuntu0.22.04.1)." >&2
  exit 1
fi

resolved="$(pick_best "${ver_major}.")"
if [ -n "$resolved" ]; then
  echo "axonos: unpinned NVIDIA userspace -> ${resolved}" >&2
  echo "$resolved"
  exit 0
fi

echo "axonos: could not resolve NVIDIA userspace for branch ${ver_major}" >&2
exit 1
