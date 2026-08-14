#!/bin/sh
# Runs from /docker-entrypoint.d/ after template rendering (20-envsubst) and
# before nginx starts. Blocks until the Let's Encrypt cert exists so the
# container can be part of `docker compose up -d` before first issuance.
# NOTE: the compose command MUST stay the image default (`nginx -g "daemon
# off;"`) — the entrypoint skips all /docker-entrypoint.d scripts (including
# the template render this config depends on) unless $1 is nginx.
until [ -f "/etc/letsencrypt/live/${FILES_TLS_SERVER_NAME}/fullchain.pem" ]; do
    echo "files-tls: waiting for certificate for ${FILES_TLS_SERVER_NAME}"
    sleep 15
done
echo "files-tls: certificate present, starting nginx"
