#!/bin/bash
set -e

cd "$(dirname "$0")"

# 버전 단일 소스: resource/pyproject.toml. 이미지 태그·앱 런타임 버전이 여기서 파생된다.
VERSION=$(grep -E '^version' resource/pyproject.toml | sed -E 's/.*"(.+)".*/\1/')
if [ -z "$VERSION" ]; then
    echo "버전 추출 실패: resource/pyproject.toml의 version 줄을 확인하세요." >&2
    exit 1
fi
IMAGE_TAG="redgw-lite:${VERSION}"

# log 디렉토리 사전 생성 (없으면 Docker가 root로 생성하므로 미리 만듦)
mkdir -p log

docker buildx \
       build --platform=linux/amd64 \
       --tag ${IMAGE_TAG} \
       --build-arg REDGW_VERSION=${VERSION} \
       -f Dockerfile \
       --progress=plain \
       --no-cache \
       .

echo "Built $IMAGE_TAG"

# compose가 참조하는 .env의 REDGW_VERSION과 불일치하면 `docker compose up`이 옛 태그를 찾는다.
if [ -f .env ]; then
    ENV_VERSION=$(grep -E '^REDGW_VERSION=' .env | cut -d= -f2-)
    if [ -n "$ENV_VERSION" ] && [ "$ENV_VERSION" != "$VERSION" ]; then
        echo "경고: .env의 REDGW_VERSION(${ENV_VERSION})이 pyproject(${VERSION})과 다릅니다. .env를 맞추세요." >&2
    fi
fi


