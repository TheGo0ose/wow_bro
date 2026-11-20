#!/usr/bin/env sh
set -eu

mkdir -p /app/outbox
chmod -R a+rwX /app/outbox 2>/dev/null || true

if [ "$(id -u)" = "0" ]; then
    chown -R appuser:appgroup /app/outbox 2>/dev/null || true
    exec gosu appuser "$@"
fi

exec "$@"
