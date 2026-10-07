#!/bin/sh
# Railway (и любой другой оркестратор) монтирует Volume ПОВЕРХ созданной в
# образе папки — со своими правами (root:root). chown из Dockerfile до этого
# момента уже не действует, поэтому непривилегированный aml получает
# "[Errno 13] Permission denied" при записи posted.json, дедупликация молча
# ломается и в группу каждый день уходит один и тот же матч.
#
# Поэтому контейнер стартует root'ом, чинит права на примонтированные папки
# и только потом роняет привилегии до aml (uid 10001).
set -e

STATE_FILE="${POSTED_STATE_FILE:-/app/state/posted.json}"
STATE_DIR=$(dirname "$STATE_FILE")

for d in "$STATE_DIR" /app/out /app/.cache; do
    mkdir -p "$d" 2>/dev/null || true
    chown -R 10001:10001 "$d" 2>/dev/null || true
done

# setpriv есть в util-linux (в debian-slim присутствует). Если вдруг нет —
# лучше отработать от root, чем не отработать вообще.
if command -v setpriv >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
    exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"
fi

exec "$@"
