#!/bin/sh
set -e

# 볼륨 마운트된 /app/log 디렉토리 소유권을 app 사용자로 보정
# (호스트에서 root로 생성된 경우 권한 문제 방지)
if [ "$(id -u)" = "0" ]; then
    chown app:app /app/log 2>/dev/null || true
    exec gosu app "$@"
fi

exec "$@"
