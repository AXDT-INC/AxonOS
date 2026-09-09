#!/bin/bash

# Opens the running JupyterLab service (started by supervisord on loopback with a
# token) in Firefox. Used by the JupyterLab desktop launcher and by the PyTorch /
# BeakerX session templates. Falls back to the default lab URL if the token isn't
# available yet.
#
# The Jupyter runtime dir lives on the persistent per-wallet home volume, so
# `jupyter server list` also reports servers from earlier containers whose PIDs
# may be reused by unrelated processes. Never trust the first entry: probe each
# candidate token against the live server and open the one that authenticates.

set -u

port="${JUPYTER_PORT:-8888}"
base="http://127.0.0.1:${port}"

live_token() {
    local tok
    for tok in $(jupyter server list 2>/dev/null \
            | grep -oE "http://(127\.0\.0\.1|localhost):${port}/[^[:space:]]*token=[a-f0-9]+" \
            | grep -oE 'token=[a-f0-9]+' | cut -d= -f2 | awk '!seen[$0]++'); do
        if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
                "${base}/api/status?token=${tok}")" = "200" ]; then
            printf '%s' "$tok"
            return 0
        fi
    done
    return 1
}

token=""
for _ in $(seq 1 30); do
    token="$(live_token)" && [ -n "$token" ] && break
    sleep 2
done

if [ -n "$token" ]; then
    url="${base}/lab?token=${token}"
else
    url="${base}/lab"
fi

exec firefox --new-window "$url"
