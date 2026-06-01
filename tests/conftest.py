"""
=============================================================================
RedGW 테스트 공통 픽스처 (conftest.py)
=============================================================================

pytest가 자동 인식하는 전역 픽스처 정의 파일입니다.
모든 테스트 모듈에서 별도 import 없이 여기 정의된 픽스처를 사용할 수 있습니다.

핵심 설계:
  - Redis DB 15 사용 → 운영 DB(0)와 완전 격리
  - Session 스코프 이벤트 루프 → Redis 연결이 session 스코프이므로 필수
  - 각 테스트 후 FLUSHDB → 테스트 간 데이터 오염 방지

실행:
  docker compose run --rm redgw python -m pytest tests/ -v
=============================================================================
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ─── 환경변수 오버라이드 ────────────────────────────────────
# app import 전에 설정하여 테스트용 Redis DB 15를 사용합니다.
# docker-compose.yml이 REDGW_REDIS_URL에 비밀번호 포함 URL을 설정하므로,
# 기존 URL에서 DB 번호만 15로 변경하여 테스트 격리를 수행합니다.
import re as _re

_test_redis_url = os.environ.get("REDGW_TEST_REDIS_URL")
_current_redis_url = os.environ.get("REDGW_REDIS_URL", "")

if _test_redis_url and ":" in _test_redis_url.split("@")[0].split("://")[-1]:
    # REDGW_TEST_REDIS_URL에 비밀번호가 포함되어 있으면 그대로 사용
    os.environ["REDGW_REDIS_URL"] = _test_redis_url
elif _current_redis_url:
    # docker-compose.yml이 설정한 REDGW_REDIS_URL의 DB 번호만 15로 변경
    os.environ["REDGW_REDIS_URL"] = _re.sub(r"/\d+$", "/15", _current_redis_url)
else:
    os.environ["REDGW_REDIS_URL"] = "redis://localhost:6379/15"

# Audit 로그 디렉토리 격리 — 계획서 §2 비변경 사항: "conftest.py에서 REDGW_AUDIT_DIRECTORY를
# tmp_path로 격리". 운영용 /app/audit를 건드리지 않고, tests/test_audit.py가 동일 경로를
# 읽어 audit 라인을 검증할 수 있도록 동일 디폴트(/tmp/audit-test)를 미리 박는다.
os.environ.setdefault("REDGW_AUDIT_DIRECTORY", "/tmp/audit-test")
import shutil as _shutil
from pathlib import Path as _Path
_audit_test_dir = _Path(os.environ["REDGW_AUDIT_DIRECTORY"])
if _audit_test_dir.exists():
    _shutil.rmtree(_audit_test_dir, ignore_errors=True)

from app.config import get_settings, reset_settings
from app.main import create_app
from app.redis_client import get_redis_manager, init_redis_manager


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def app():
    """
    FastAPI 앱 인스턴스 (세션 스코프).

    scope="session"  : 전체 테스트 세션 동안 단 하나의 앱 인스턴스 유지
    loop_scope="session" : pytest-asyncio 이벤트 루프를 session 스코프로 맞춤
      → Redis 연결이 session 스코프이므로 루프도 동일해야 함
      → 불일치 시 "Event loop is closed" 에러 발생
    """
    reset_settings()
    application = create_app()

    # lifespan 수동 처리: 테스트에서는 ASGI lifespan 이벤트가
    # 자동 호출되지 않으므로 Redis 연결을 직접 관리
    settings = get_settings()
    manager = init_redis_manager(settings)
    await manager.connect()

    # Audit 로거도 lifespan에서 init되므로 세션 픽스처에서 직접 호출
    from app.audit import init_audit_logger, shutdown_audit_logger, reset_audit_logger
    reset_audit_logger()
    init_audit_logger(settings.audit)

    yield application

    shutdown_audit_logger()
    await manager.disconnect()


@pytest_asyncio.fixture(loop_scope="session")
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    """
    httpx AsyncClient — 인프로세스 HTTP 테스트 클라이언트.

    ASGITransport를 사용하면 실제 서버 기동 없이
    인프로세스에서 요청/응답을 테스트할 수 있습니다.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def cleanup_redis(app):
    """
    각 테스트 후 Redis DB 15 전체 정리.

    autouse=True: 모든 테스트에 자동 적용
    yield 이후 코드가 teardown으로 실행됩니다.
    """
    yield
    manager = get_redis_manager()
    r = manager.get_client()
    await r.flushdb()


# ─── 테스트용 API 키 상수 ───────────────────────────────────
# 환경변수(REDGW_CLIENT_{ID}_API_KEY)가 있으면 사용, 없으면 config.yaml 플레이스홀더 사용.
# docker compose run 시 .env → 환경변수로 전달되므로 실제 키가 자동 적용됩니다.
HRM_KEY = os.environ.get("REDGW_CLIENT_HRM_API_KEY", "redgw_ak_hrm_xxxxxxxxxxxxxxxx")
ERP_KEY = os.environ.get("REDGW_CLIENT_ERP_API_KEY", "redgw_ak_erp_xxxxxxxxxxxxxxxx")
CRM_KEY = os.environ.get("REDGW_CLIENT_CRM_API_KEY", "redgw_ak_crm_xxxxxxxxxxxxxxxx")
MONITOR_KEY = os.environ.get("REDGW_CLIENT_MONITOR_API_KEY", "redgw_ak_monitor_xxxxxxxxxxxxxxxx")
ADMIN_KEY = os.environ.get("REDGW_ADMIN_API_KEY", "redgw_admin_xxxxxxxxxxxxxxxx")
INVALID_KEY = "invalid_api_key_xxxxx"              # 유효하지 않은 키 (401 테스트용)
