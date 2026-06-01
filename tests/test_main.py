"""main.py — STATUS 상태 모니터 단일 로거 선출 테스트.

gunicorn 멀티워커 환경에서 STATUS 로그가 워커 수만큼 중복되던 문제를 Redis NX 락
선출로 1줄로 줄인다(이전 startup flock 게이트는 컨테이너에서 워커 간 배타 실패).
"""

import pytest

from app.redis_client import get_redis_manager


class TestStatusMonitorSingleWriter:
    """STATUS 로깅이 멀티워커 중 1개만 기록되도록 NX 락으로 선출되는지 검증."""

    @pytest.mark.asyncio
    async def test_nx_lock_serializes_writers(self, client):
        """락이 선점되면 다른 워커의 set nx는 실패한다(=이번 주기 기록 skip)."""
        from app.main import _STATUS_LEADER_KEY, _STATUS_LEADER_TTL

        r = get_redis_manager().get_client()
        await r.delete(_STATUS_LEADER_KEY)

        # 1번째 워커: 락 획득
        first = await r.set(_STATUS_LEADER_KEY, "pid-1", nx=True, ex=_STATUS_LEADER_TTL)
        assert first  # 획득 성공

        # 2번째 워커: 같은 주기 — 실패해야 중복 기록이 차단된다
        second = await r.set(_STATUS_LEADER_KEY, "pid-2", nx=True, ex=_STATUS_LEADER_TTL)
        assert not second

        # 값은 1번째 워커 것이 유지된다(선점자 우선)
        assert (await r.get(_STATUS_LEADER_KEY)) in (b"pid-1", "pid-1")
        await r.delete(_STATUS_LEADER_KEY)

    @pytest.mark.asyncio
    async def test_lock_ttl_shorter_than_interval(self):
        """락 TTL은 인터벌보다 짧아야 한다 — 락 보유 워커 사망 시 다음 주기에 다른
        워커가 자동 인수할 수 있어야 STATUS가 영구 중단되지 않는다."""
        from app.main import _STATUS_INTERVAL, _STATUS_LEADER_TTL

        assert 0 < _STATUS_LEADER_TTL < _STATUS_INTERVAL

    @pytest.mark.asyncio
    async def test_emit_status_line_writes_status_tag(self, client, monkeypatch):
        """_emit_status_line은 Redis 메모리/keys 등 상태를 STATUS 한 줄로 기록한다."""
        from app import main as main_mod

        captured: list = []

        async def fake_write(tag, message, level="I"):
            captured.append((tag, message, level))

        # _emit_status_line 내부의 `from app.utils.file_logger import write_log_async`가
        # 가리키는 모듈 속성을 패치한다.
        monkeypatch.setattr("app.utils.file_logger.write_log_async", fake_write)

        r = get_redis_manager().get_client()
        await main_mod._emit_status_line(r)

        assert len(captured) == 1
        tag, msg, _level = captured[0]
        assert tag == "STATUS"
        assert "Redis" in msg and "keys:" in msg
