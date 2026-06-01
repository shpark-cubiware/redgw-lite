#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "ERROR: .env 파일이 없습니다. .env.example을 복사하여 생성하세요." >&2
  exit 1
fi

# docker-compose.yml이 .env에서 REDIS_PASSWORD를 직접 읽어
# REDGW_REDIS_URL에 비밀번호를 포함시킴.
# conftest.py가 DB 번호를 0 → 15로 변경하여 테스트 격리 수행.
exec docker compose run --rm \
  redgw python -m pytest tests/ "$@"
