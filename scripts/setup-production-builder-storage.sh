#!/usr/bin/env bash
# Explicit operator setup; never called by deploy-production.sh.
# Creates storage only. The bounded builder is provisioned separately.
set +x
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
usage() {
  echo 'Usage: setup-production-builder-storage.sh --host NAME --account USER --storage-mount PATH --node NAME'
}
expected_host= account= storage_mount= node=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help) usage; exit 0 ;;
    --host|--account|--storage-mount|--node)
      [[ $# -ge 2 && -n $2 ]] || { usage >&2; exit 1; }
      case "$1" in
        --host) [[ -z $expected_host ]] || exit 1; expected_host=$2 ;;
        --account) [[ -z $account ]] || exit 1; account=$2 ;;
        --storage-mount) [[ -z $storage_mount ]] || exit 1; storage_mount=$2 ;;
        --node) [[ -z $node ]] || exit 1; node=$2 ;;
      esac
      shift 2 ;;
    *) usage >&2; exit 1 ;;
  esac
done
[[ $expected_host =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ &&
   $account =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ &&
   $node =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$ &&
   $storage_mount == /* && $storage_mount != / ]] || { usage >&2; exit 1; }
[[ $EUID -eq 0 ]] || { echo 'Run this script with sudo.' >&2; exit 1; }
for tool in docker mount mountpoint realpath setfacl getfacl python3 hostname id stat mktemp mkdir find runuser; do
  command -v "$tool" >/dev/null || { echo "Required utility missing: $tool" >&2; exit 1; }
done
[[ $(hostname -s) == "$expected_host" ]] || { echo 'Expected host differs; refusing setup.' >&2; exit 1; }
id "$account" >/dev/null
[[ $(id -u "$account") -ne 0 ]] || { echo 'Specify a non-root deployment account.' >&2; exit 1; }
docker_local=(docker --host unix:///var/run/docker.sock)
docker_root=$("${docker_local[@]}" info --format '{{.DockerRootDir}}')
[[ $docker_root == /* && $docker_root != / && $(realpath "$docker_root") == "$docker_root" ]] || exit 1
[[ -d $docker_root/volumes && ! -L $docker_root/volumes ]] || exit 1
mountpoint -q "$storage_mount" || { echo 'Storage path must already be mounted.' >&2; exit 1; }
[[ $(stat -c %d "$storage_mount") != $(stat -c %d "$docker_root/volumes") ]] || exit 1
[[ $(realpath "$storage_mount") == "$storage_mount" ]] || exit 1
volume=buildx_buildkit_${node}_state
source_dir=$storage_mount/axonos-buildkit-$node
volume_dir=$docker_root/volumes/$volume
state_dir=$volume_dir/_data
[[ ! -e $source_dir && ! -L $source_dir && ! -e $volume_dir && ! -L $volume_dir ]] || {
  echo 'Dedicated storage path already exists; stop for inspection, do not overwrite.' >&2; exit 1;
}
if "${docker_local[@]}" volume inspect "$volume" >/dev/null 2>&1; then
  echo 'Dedicated volume already exists; stop for inspection.' >&2; exit 1
fi
backup=$(mktemp /var/tmp/axonos-builder-storage-acl.XXXXXX)
getfacl -p "$docker_root" "$docker_root/volumes" > "$backup"
printf 'Parent-directory ACL backup: %s\n' "$backup"
mkdir -m 0700 "$source_dir"
"${docker_local[@]}" volume create --driver local --label com.axonos.purpose=isolated-capi-builder "$volume" >/dev/null
[[ $("${docker_local[@]}" volume inspect --format '{{.Mountpoint}}' "$volume") == "$state_dir" ]] || exit 1
[[ $(realpath "$state_dir") == "$state_dir" ]] || exit 1
[[ -z $("${docker_local[@]}" ps -a -q --filter "volume=$volume") ]] || exit 1
[[ -z $(find "$state_dir" -mindepth 1 -maxdepth 1 -print -quit) ]] || exit 1
mount --bind "$source_dir" "$state_dir"
# Add traversal only, not directory-listing or file-read permissions.
# Existing descendant permissions continue to control access to their contents.
setfacl -m "u:$account:--x" "$docker_root" "$docker_root/volumes" "$volume_dir" "$state_dir"
runuser -u "$account" -- python3 - "$state_dir" <<'PY'
import os, pathlib, sys
p = pathlib.Path(sys.argv[1])
assert str(p.resolve()) == str(p)
assert os.path.ismount(p)
assert p.stat().st_dev != p.parent.stat().st_dev
print('PASS: builder state is separately mounted and its metadata is accessible.')
PY
printf 'Storage ready: %s\n' "$state_dir"
echo 'No builder started; no production service or secret was accessed.'
echo 'Mount is temporary until reboot; keep the future builder restart policy disabled.'
