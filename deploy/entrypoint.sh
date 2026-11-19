#!/bin/sh
# Fix ownership of the bind-mounted data dir (created by root on the host), then drop to the 'bot' user.
set -e
mkdir -p /app/data
if [ "$(id -u)" = "0" ]; then
  chown -R bot:bot /app/data 2>/dev/null || true
  exec setpriv --reuid=bot --regid=bot --init-groups --no-new-privs "$@"
fi
exec "$@"
