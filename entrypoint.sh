#!/bin/sh
set -eu
cd /app
.venv/bin/alembic upgrade head
/usr/sbin/nginx -t
/usr/sbin/nginx -g 'daemon off;' &
nginx_pid=$!
.venv/bin/python /app/main.py &
app_pid=$!
cleanup() {
  trap - EXIT INT TERM
  kill -TERM "$app_pid" 2>/dev/null || true
  kill -QUIT "$nginx_pid" 2>/dev/null || true
  wait "$app_pid" 2>/dev/null || true
  wait "$nginx_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM
# Exit when either service dies so the container restart policy can recover it.
while kill -0 "$nginx_pid" 2>/dev/null && kill -0 "$app_pid" 2>/dev/null; do
  sleep 1
done
exit 1
