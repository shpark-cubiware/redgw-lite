# ── Stage 1: Rust 빌드 (redgw_core PyO3 모듈) ──
FROM python:3.14.5-slim AS rust-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
       curl build-essential pkg-config \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.cargo/bin:${PATH}"

RUN pip install --no-cache-dir maturin

WORKDIR /build
COPY redgw_core/ .
# cp314 ABI 고정 wheel — pyo3 features에 abi3 미포함이라 산출물은 인터프리터 고정이며,
# Stage 2도 동일 python:3.14.5-slim 핀이라 ABI 일치 보장. (ABI3 forward-compat env var는
# abi3 빌드에서만 의미가 있어 제거 — 현 설계에선 no-op이었음.)
RUN maturin build --release --strip --interpreter python3.14 \
    && ls -la target/wheels/

# ── Stage 2: 기존 앱 이미지 ──
# 패치 버전 고정: 3.14.0~3.14.4의 incremental GC는 일부 워크로드서 RSS 최대 5배 폭증
# (3.14.5에서 generational GC로 롤백). 부동 태그(3.14-slim)는 빌드 시점에 따라 거동이
# 비결정적이라 patch를 핀한다. 업그레이드는 보안패치 확인 후 의도적으로.
FROM python:3.14.5-slim AS base

WORKDIR /app

# Rust 모듈 설치 (빌드 실패 시에도 앱은 Python fallback으로 동작)
COPY --from=rust-builder /build/target/wheels/ /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels/

# 의존성 먼저 설치 (캐시 레이어 활용)
COPY resource/requirements.txt resource/pyproject.toml ./

# 빌드 도구로 의존성 설치 → pip install 후 컴파일러를 purge하여 이미지 경량화
# jemalloc 사용을 위해 libjemalloc2도 설치
# gosu: entrypoint에서 root→app 사용자 전환 (볼륨 권한 보정 후)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       gcc g++ \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc g++ \
    && apt-get autoremove -y \
    && apt-get install -y --no-install-recommends \
       libjemalloc2 gosu \
    && rm -rf /var/lib/apt/lists/*

# 메모리 할당 최적화 위해 jemalloc 사용
# jemalloc: glibc malloc 대체 — RSS 단편화 방지 (Redis도 jemalloc 사용)
ENV LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2

# 앱 코드만 포함 (tests/scripts는 호스트에서 볼륨 마운트)
COPY app/ app/

# entrypoint 스크립트 복사
COPY resource/entrypoint.sh /entrypoint.sh

# non-root 사용자 생성 (UID/GID 1000 고정)
# /app/log 디렉토리를 미리 생성하여 볼륨 마운트 시 소유권 문제 방지
RUN addgroup --system --gid 1000 app && adduser --system --uid 1000 --ingroup app app \
    && mkdir -p /app/log \
    && chown -R app:app /app \
    && chmod +x /entrypoint.sh

# 앱 버전 주입 — 단일 소스 resource/pyproject.toml에서 build.sh가 추출해 전달.
# 늦게 선언해 위 무거운 pip 레이어 캐시가 버전 변경에 무효화되지 않게 한다.
ARG REDGW_VERSION=dev
ENV REDGW_VERSION=${REDGW_VERSION}

EXPOSE 8080

# entrypoint: root로 시작 → /app/log 권한 보정 → gosu로 app 사용자 전환
ENTRYPOINT ["/entrypoint.sh"]
# --no-control-socket: gunicorn 25.1+ 런타임 제어 소켓 비활성화.
#   미사용 기능(gunicornc)이며, non-root app 유저의 HOME이 /nonexistent라
#   기본 소켓 경로($HOME/.gunicorn) 생성이 권한 거부로 매 기동마다 ERROR 발생.
#   운영 제어는 admin API + Redis 시그널 리로드로 충분 → 소켓 표면 제거.
CMD ["gunicorn", "app.main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "4", \
     "-b", "0.0.0.0:8080", \
     "--worker-tmp-dir", "/dev/shm", \
     "--no-control-socket", \
     "--max-requests", "2000", \
     "--max-requests-jitter", "500", \
     "--access-logfile", "-"]
