#!/bin/sh
# Run as PUID:PGID (Unraid default 99:100) so renamed/split files keep
# ownership your other containers expect.
set -e
PUID=${PUID:-99}
PGID=${PGID:-100}
umask "${UMASK:-002}"
mkdir -p "$CONFIG_DIR" "$CACHE_DIR"

if [ "$(id -u)" = "0" ]; then
  chown "$PUID:$PGID" "$CONFIG_DIR" "$CACHE_DIR" 2>/dev/null || true
  chown -R "$PUID:$PGID" "$CONFIG_DIR" "$CACHE_DIR" 2>/dev/null || true
  export HOME="$CONFIG_DIR"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups \
    python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8686}" --proxy-headers
fi
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8686}" --proxy-headers
