#!/bin/bash

# Opens the running JupyterLab service (started by supervisord on loopback with a
# token) in Firefox. Used by the JupyterLab desktop launcher and by the PyTorch /
# BeakerX session templates. Falls back to the default lab URL if the token isn't
# available yet.

set -u

url=""
for _ in $(seq 1 30); do
    url="$(jupyter server list 2>/dev/null | grep -oE 'http://127\.0\.0\.1:[0-9]+/(lab)?[^[:space:]]*token=[a-f0-9]+' | head -n1)"
    [ -n "$url" ] && break
    sleep 2
done
[ -z "$url" ] && url="http://127.0.0.1:8888/lab"

exec firefox --new-window "$url"
