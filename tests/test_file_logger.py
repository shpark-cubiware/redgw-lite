"""통합 파일 로거 테스트 — app/utils/file_logger.py

일자별 파일 분리 + 크기 기반 로테이션 + gzip 압축 + PID 포맷 테스트.
"""

from __future__ import annotations

import gzip
import os
import re
from datetime import datetime
from unittest.mock import patch

import pytest


def _today_log(tmp_path):
    """오늘 날짜 로그 파일 경로."""
    today = datetime.now().strftime("%Y%m%d")
    return tmp_path / f"redgw.{today}.log"


class TestFileLogger:
    """통합 파일 로거 단위 테스트."""

    def setup_method(self):
        """각 테스트 전 싱글턴 리셋."""
        from app.utils.file_logger import reset_file_logger
        reset_file_logger()

    def teardown_method(self):
        """각 테스트 후 싱글턴 리셋."""
        from app.utils.file_logger import reset_file_logger
        reset_file_logger()

    # ── 기본 동작 ────────────────────────────────────────────

    def test_init_creates_directory(self, tmp_path):
        """init_file_logger가 디렉토리를 자동 생성한다."""
        log_dir = tmp_path / "sub" / "dir"
        assert not log_dir.exists()

        from app.utils.file_logger import init_file_logger
        init_file_logger(str(log_dir))

        assert log_dir.exists()

    def test_init_creates_log_file_on_write(self, tmp_path):
        """write_log 호출 시 일자별 로그 파일이 생성된다."""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        write_log("MAIN", "test message")

        log_file = _today_log(tmp_path)
        assert log_file.exists()

    def test_daily_filename_format(self, tmp_path):
        """파일명이 redgw.yyyymmdd.log 패턴이다."""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        write_log("MAIN", "test")

        log_file = _today_log(tmp_path)
        assert log_file.exists()
        assert re.match(r"redgw\.\d{8}\.log", log_file.name)

    # ── 로그 포맷 ────────────────────────────────────────────

    def test_write_log_format(self, tmp_path):
        """로그 형식: {timestamp} ({level}) [{pid:>8}] [{tag:<8}] {message}"""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        write_log("MAIN", "RedGW starting...")

        log_file = _today_log(tmp_path)
        line = log_file.read_text().strip()
        assert "(I)" in line
        assert "[MAIN    ]" in line
        assert "RedGW starting..." in line

    def test_pid_in_log_format(self, tmp_path):
        """로그에 PID가 8자리 우측정렬로 포함된다."""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        write_log("MAIN", "pid test")

        log_file = _today_log(tmp_path)
        line = log_file.read_text().strip()
        pid = os.getpid()
        expected = f"[{pid:>8}]"
        assert expected in line

    def test_write_log_tag_padding(self, tmp_path):
        """태그가 8자 미만이면 오른쪽 공백 패딩."""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        write_log("STATUS", "test")

        log_file = _today_log(tmp_path)
        line = log_file.read_text().strip()
        assert "[STATUS  ]" in line

    def test_write_log_config_tag(self, tmp_path):
        """CONFIG 태그 기록."""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        write_log("CONFIG", "Validation report")

        log_file = _today_log(tmp_path)
        line = log_file.read_text().strip()
        assert "[CONFIG  ]" in line

    def test_write_log_level_warning(self, tmp_path):
        """WARNING 레벨 (W)."""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        write_log("CONFIG", "Placeholder key", level="W")

        log_file = _today_log(tmp_path)
        line = log_file.read_text().strip()
        assert "(W)" in line

    def test_write_log_level_error(self, tmp_path):
        """ERROR 레벨 (E)."""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        write_log("MAIN", "Redis unavailable", level="E")

        log_file = _today_log(tmp_path)
        line = log_file.read_text().strip()
        assert "(E)" in line

    def test_write_log_level_fatal(self, tmp_path):
        """FATAL 레벨 (F)."""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        write_log("MAIN", "FATAL error", level="F")

        log_file = _today_log(tmp_path)
        line = log_file.read_text().strip()
        assert "(F)" in line
        assert "FATAL error" in line

    def test_write_log_multiple_lines(self, tmp_path):
        """여러 줄 기록."""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        write_log("MAIN", "Line 1")
        write_log("CONFIG", "Line 2")
        write_log("STATUS", "Line 3")

        log_file = _today_log(tmp_path)
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 3
        assert "[MAIN    ]" in lines[0]
        assert "[CONFIG  ]" in lines[1]
        assert "[STATUS  ]" in lines[2]

    def test_write_log_auto_init(self, tmp_path):
        """초기화 전 write_log 호출 시 자동 초기화."""
        with patch("app.utils.file_logger._LOG_DIR", str(tmp_path)):
            from app.utils.file_logger import write_log
            write_log("MAIN", "auto init test")

        log_file = _today_log(tmp_path)
        assert log_file.exists()
        assert "auto init test" in log_file.read_text()

    def test_write_log_failure_silent(self, tmp_path):
        """파일 쓰기 실패 시 예외를 무시한다."""
        from app.utils.file_logger import init_file_logger, write_log
        init_file_logger(str(tmp_path))

        # 로거를 None으로 강제 설정하여 에러 유발
        import app.utils.file_logger as mod
        original = mod._file_logger
        mod._file_logger = None

        # 예외 없이 실행되어야 함
        write_log("MAIN", "should not crash")

        mod._file_logger = original

    def test_singleton_prevents_double_init(self, tmp_path):
        """init_file_logger 2회 호출 시 핸들러가 중복 추가되지 않는다."""
        from app.utils.file_logger import init_file_logger
        init_file_logger(str(tmp_path))
        init_file_logger(str(tmp_path))

        import app.utils.file_logger as mod
        assert len(mod._file_logger.handlers) == 1

    @pytest.mark.asyncio
    async def test_write_log_async(self, tmp_path):
        """비동기 래퍼가 동작한다."""
        from app.utils.file_logger import init_file_logger, write_log_async
        init_file_logger(str(tmp_path))

        await write_log_async("STATUS", "async test")

        log_file = _today_log(tmp_path)
        content = log_file.read_text()
        assert "async test" in content
        assert "[STATUS  ]" in content

    @pytest.mark.asyncio
    async def test_write_log_async_failure_silent(self, tmp_path):
        """비동기 래퍼의 파일 쓰기 실패 시 예외를 무시한다."""
        from app.utils.file_logger import init_file_logger, write_log_async
        init_file_logger(str(tmp_path))

        import app.utils.file_logger as mod
        original = mod._file_logger
        mod._file_logger = None

        # 예외 없이 실행되어야 함
        await write_log_async("MAIN", "should not crash")

        mod._file_logger = original

    def test_reset_file_logger(self, tmp_path):
        """reset_file_logger가 싱글턴을 리셋한다."""
        from app.utils.file_logger import init_file_logger, reset_file_logger
        init_file_logger(str(tmp_path))

        import app.utils.file_logger as mod
        assert mod._initialized is True
        assert mod._file_logger is not None

        reset_file_logger()
        assert mod._initialized is False
        assert mod._file_logger is None

    # ── 크기 기반 로테이션 ────────────────────────────────────

    def test_size_rollover_creates_backup(self, tmp_path):
        """파일 크기 초과 시 .1 백업이 생성된다."""
        from app.utils.file_logger import init_file_logger, write_log
        import app.utils.file_logger as mod

        # 아주 작은 크기로 설정하여 즉시 rollover 유발
        with patch.object(mod, "_MAX_BYTES", 100), \
             patch.object(mod, "_COMPRESS_BACKUP", False):
            init_file_logger(str(tmp_path))

            # 100바이트 이상 쓰기
            for i in range(10):
                write_log("MAIN", f"message {i} " + "x" * 50)

        log_file = _today_log(tmp_path)
        backup_1 = tmp_path / f"{log_file.name}.1"
        assert log_file.exists()
        assert backup_1.exists()

    def test_size_rollover_shifts_backups(self, tmp_path):
        """크기 초과 시 백업 번호가 시프트된다 (.1 → .2)."""
        from app.utils.file_logger import init_file_logger, write_log
        import app.utils.file_logger as mod

        with patch.object(mod, "_MAX_BYTES", 100), \
             patch.object(mod, "_COMPRESS_BACKUP", False):
            init_file_logger(str(tmp_path))

            # 여러 번 rollover 유발
            for i in range(30):
                write_log("MAIN", f"msg {i} " + "x" * 60)

        log_file = _today_log(tmp_path)
        backup_1 = tmp_path / f"{log_file.name}.1"
        backup_2 = tmp_path / f"{log_file.name}.2"
        assert log_file.exists()
        assert backup_1.exists()
        assert backup_2.exists()

    def test_backup_count_limit(self, tmp_path):
        """backupCount 초과 시 가장 오래된 백업이 삭제된다."""
        from app.utils.file_logger import init_file_logger, write_log
        import app.utils.file_logger as mod

        with patch.object(mod, "_MAX_BYTES", 80), \
             patch.object(mod, "_BACKUP_COUNT", 2), \
             patch.object(mod, "_COMPRESS_BACKUP", False):
            init_file_logger(str(tmp_path))

            # 많이 써서 백업 여러 개 생성
            for i in range(50):
                write_log("MAIN", f"msg {i} " + "x" * 50)

        log_file = _today_log(tmp_path)
        # backupCount=2이므로 .3 이상은 없어야 함
        backup_3 = tmp_path / f"{log_file.name}.3"
        assert not backup_3.exists()

    # ── gzip 압축 ────────────────────────────────────────────

    def test_gzip_compression(self, tmp_path):
        """크기 초과 시 이전 백업이 .gz로 압축된다."""
        from app.utils.file_logger import init_file_logger, write_log
        import app.utils.file_logger as mod

        with patch.object(mod, "_MAX_BYTES", 100), \
             patch.object(mod, "_COMPRESS_BACKUP", True):
            init_file_logger(str(tmp_path))

            for i in range(20):
                write_log("MAIN", f"msg {i} " + "x" * 60)

        log_file = _today_log(tmp_path)
        # .1.gz 파일이 존재해야 함
        gz_files = list(tmp_path.glob(f"{log_file.name}.*.gz"))
        assert len(gz_files) > 0

        # gzip 파일이 유효한 압축 파일인지 확인
        for gz_file in gz_files:
            with gzip.open(gz_file, "rb") as f:
                content = f.read()
                assert len(content) > 0

    # ── 일자 전환 ────────────────────────────────────────────

    def test_date_rollover_new_file(self, tmp_path):
        """날짜 변경 시 새 날짜 파일이 생성된다."""
        from app.utils.file_logger import init_file_logger, write_log
        import app.utils.file_logger as mod

        with patch.object(mod, "_COMPRESS_BACKUP", False):
            init_file_logger(str(tmp_path))
            write_log("MAIN", "day 1 message")

            # 핸들러의 날짜를 어제로 변경하여 rollover 유발
            handler = mod._file_logger.handlers[0]
            handler._current_date = "20260101"

            write_log("MAIN", "day 2 message")

        # 오늘 날짜 파일에 "day 2 message"가 있어야 함
        today_file = _today_log(tmp_path)
        assert today_file.exists()
        assert "day 2 message" in today_file.read_text()

    def test_date_rollover_compresses_old(self, tmp_path):
        """일자 전환 시 이전 날짜 로그가 .gz 압축된다."""
        from datetime import timedelta
        from app.utils.file_logger import init_file_logger, write_log
        import app.utils.file_logger as mod

        with patch.object(mod, "_COMPRESS_BACKUP", True):
            init_file_logger(str(tmp_path))
            write_log("MAIN", "old day message")

            # 핸들러의 날짜를 어제로 변경 (retention 이내여야 cleanup에 걸리지 않음)
            handler = mod._file_logger.handlers[0]
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            handler._current_date = yesterday

            # 어제 날짜 파일을 수동 생성 (핸들러가 참조할 파일)
            old_file = tmp_path / f"redgw.{yesterday}.log"
            old_file.write_text("old content\n")

            write_log("MAIN", "new day message")

        # P4: 압축은 백그라운드 thread — drain 후 assert
        from app.utils.file_logger import _get_gzip_executor
        import app.utils.file_logger as _flmod
        _get_gzip_executor().shutdown(wait=True)
        _flmod._gzip_executor = None  # 후속 테스트용 재생성 보장

        # 어제 날짜 파일이 .gz로 압축되어야 함
        old_gz = tmp_path / f"redgw.{yesterday}.log.gz"
        assert old_gz.exists()
        with gzip.open(old_gz, "rb") as f:
            assert b"old content" in f.read()

    # ── 클린업 ────────────────────────────────────────────

    def test_cleanup_removes_old_files(self, tmp_path):
        """retention_days 이전 파일이 삭제된다."""
        from app.utils.file_logger import init_file_logger
        import app.utils.file_logger as mod

        with patch.object(mod, "_RETENTION_DAYS", 3):
            # 10일 전 ~ 오늘까지 파일 생성
            for i in range(10):
                from datetime import timedelta
                d = datetime.now() - timedelta(days=i)
                f = tmp_path / f"redgw.{d.strftime('%Y%m%d')}.log"
                f.write_text(f"day {i}\n")
                # .gz 파일도
                gz = tmp_path / f"redgw.{d.strftime('%Y%m%d')}.log.gz"
                gz.write_text("compressed")

            init_file_logger(str(tmp_path))

        # 3일 이내 파일만 남아야 함 (오늘 포함 4일치)
        remaining = list(tmp_path.glob("redgw.*.log*"))
        # 핸들러가 생성한 오늘 파일 + 최근 3일 이내 파일만 잔존
        for f in remaining:
            m = re.match(r"redgw\.(\d{8})\.log", f.name)
            if m:
                from datetime import timedelta
                cutoff = datetime.now() - timedelta(days=3)
                assert m.group(1) >= cutoff.strftime("%Y%m%d")

    def test_cleanup_keeps_recent_files(self, tmp_path):
        """retention_days 이내 파일은 유지된다."""
        from app.utils.file_logger import init_file_logger, write_log
        import app.utils.file_logger as mod

        with patch.object(mod, "_RETENTION_DAYS", 7):
            # 3일 전 파일 생성
            from datetime import timedelta
            d = datetime.now() - timedelta(days=3)
            recent = tmp_path / f"redgw.{d.strftime('%Y%m%d')}.log"
            recent.write_text("recent\n")

            init_file_logger(str(tmp_path))
            write_log("MAIN", "today")

        # 3일 전 파일 유지
        assert recent.exists()

    def test_cleanup_on_init(self, tmp_path):
        """앱 시작(init) 시 클린업이 수행된다."""
        from app.utils.file_logger import init_file_logger
        import app.utils.file_logger as mod

        # 30일 전 파일 생성
        from datetime import timedelta
        old_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        old_file = tmp_path / f"redgw.{old_date}.log"
        old_file.write_text("ancient\n")

        with patch.object(mod, "_RETENTION_DAYS", 7):
            init_file_logger(str(tmp_path))

        # 30일 전 파일 삭제
        assert not old_file.exists()

    # ── 환경변수 오버라이드 ─────────────────────────────────

    def test_env_override_max_size(self, tmp_path):
        """REDGW_LOG_MAX_SIZE_MB 환경변수 오버라이드."""
        from app.utils.file_logger import DailySizeRotatingHandler

        handler = DailySizeRotatingHandler(
            log_dir=str(tmp_path),
            max_bytes=5 * 1024 * 1024,
        )
        assert handler.maxBytes == 5 * 1024 * 1024
        handler.close()

    def test_env_override_retention(self, tmp_path):
        """REDGW_LOG_RETENTION_DAYS 환경변수 오버라이드."""
        from app.utils.file_logger import DailySizeRotatingHandler

        handler = DailySizeRotatingHandler(
            log_dir=str(tmp_path),
            retention_days=14,
        )
        assert handler.retention_days == 14
        handler.close()

    def test_env_override_backup_count(self, tmp_path):
        """REDGW_LOG_BACKUP_COUNT 환경변수 오버라이드."""
        from app.utils.file_logger import DailySizeRotatingHandler

        handler = DailySizeRotatingHandler(
            log_dir=str(tmp_path),
            backup_count=10,
        )
        assert handler.backupCount == 10
        handler.close()

    # ── 재시작 시 append ──────────────────────────────────

    def test_append_on_restart(self, tmp_path):
        """앱 재시작 시 기존 파일에 append한다."""
        from app.utils.file_logger import init_file_logger, write_log, reset_file_logger
        init_file_logger(str(tmp_path))
        write_log("MAIN", "first run")
        reset_file_logger()

        # 두 번째 초기화 (재시작 시뮬레이션)
        init_file_logger(str(tmp_path))
        write_log("MAIN", "second run")

        log_file = _today_log(tmp_path)
        content = log_file.read_text()
        assert "first run" in content
        assert "second run" in content

    # ── max_total_size_mb 디렉토리 상한 (PR-1 신규) ───────────

    def _recent_date_str(self, days_ago: int = 0) -> str:
        """retention 안의 최근 날짜 생성 (YYYYMMDD)."""
        from datetime import timedelta
        return (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d")

    def test_max_total_size_threshold(self, tmp_path):
        """디렉토리 합산이 max_total_size_mb 초과 시 가장 오래된 .gz부터 삭제."""
        from app.utils.file_logger import DailySizeRotatingHandler

        # 더미 .gz 5개 생성 — 모두 retention 안(최근 5일)에 위치
        files = []
        for i in range(5):
            f = tmp_path / f"redgw.{self._recent_date_str(i)}.log.gz"
            f.write_bytes(b"x" * (200 * 1024))  # 각 200 KB, 총 1 MB
            os.utime(f, (1700000000 + i, 1700000000 + i))  # mtime 오래된 순
            files.append(f)

        handler = DailySizeRotatingHandler(
            log_dir=str(tmp_path),
            retention_days=30,
            max_total_size_mb=0,
        )
        handler.max_total_bytes = 500 * 1024  # 임계 500 KB
        handler._cleanup_by_total_size()

        remaining = sorted(tmp_path.glob("redgw.*.log.gz"))
        total = sum(f.stat().st_size for f in remaining)
        assert total <= 500 * 1024, f"total={total} should be <= {500*1024}"
        # 가장 오래된 (mtime 작은) 것부터 삭제 — files[0]은 사라져야 함
        assert not files[0].exists()
        handler.close()

    def test_max_total_size_unlimited(self, tmp_path):
        """max_total_size_mb=0이면 무제한 — 정리 호출되어도 아무것도 안 함."""
        from app.utils.file_logger import DailySizeRotatingHandler

        for i in range(3):
            (tmp_path / f"redgw.{self._recent_date_str(i)}.log.gz").write_bytes(b"x" * 1024)

        handler = DailySizeRotatingHandler(
            log_dir=str(tmp_path),
            retention_days=30,
            max_total_size_mb=0,
        )
        handler._cleanup_by_total_size()

        assert len(list(tmp_path.glob("redgw.*.log.gz"))) == 3
        handler.close()

    def test_retention_vs_total_size(self, tmp_path):
        """retention_days는 init 시 자동 적용, max_total_size_mb는 독립."""
        from app.utils.file_logger import DailySizeRotatingHandler

        # 오래된 (retention 초과) 파일 — 30일 전
        old_date = self._recent_date_str(30)
        old = tmp_path / f"redgw.{old_date}.log"
        old.write_bytes(b"x" * 1024)
        # 최근 (retention 안), 크기 큼
        recent_date = self._recent_date_str(2)
        recent = tmp_path / f"redgw.{recent_date}.log.gz"
        recent.write_bytes(b"x" * (300 * 1024))

        handler = DailySizeRotatingHandler(
            log_dir=str(tmp_path),
            retention_days=7,
            max_total_size_mb=0,
        )
        # cleanup_old_files는 init에서 호출 → old 삭제됨
        assert not old.exists()
        # recent는 유지 (retention 안)
        assert recent.exists()
        handler.close()

    # ── gzip 백그라운드 (P4) ──────────────────────────────────

    def test_gzip_background_thread(self, tmp_path):
        """gzip 압축이 백그라운드 thread에서 실행된다 (실패 시 fallback)."""
        from app.utils.file_logger import DailySizeRotatingHandler, _get_gzip_executor

        handler = DailySizeRotatingHandler(log_dir=str(tmp_path))

        target = tmp_path / "test_async.log"
        target.write_bytes(b"hello\n")

        # 비동기 압축 제출
        handler._compress_file_async(target)

        # 백그라운드 완료 대기 (executor 동기화)
        _get_gzip_executor().shutdown(wait=True)

        # 원본 삭제 + .gz 생성
        gz = tmp_path / "test_async.log.gz"
        assert gz.exists()
        assert not target.exists()
        # 압축 내용 검증
        assert gzip.decompress(gz.read_bytes()) == b"hello\n"

        # 다음 테스트를 위해 executor 재생성 (shutdown 후 재사용 불가)
        import app.utils.file_logger as mod
        mod._gzip_executor = None

        handler.close()

    def test_gzip_background_failure_preserves_original(self, tmp_path):
        """gzip 압축 실패 시 원본 파일 보존."""
        from app.utils.file_logger import DailySizeRotatingHandler

        handler = DailySizeRotatingHandler(log_dir=str(tmp_path))

        # 존재하지 않는 파일 → 압축 실패
        non_existent = tmp_path / "nope.log"
        handler._compress_file(non_existent)

        # 실패해도 .gz 파일은 정리됨 (예외 안 던짐)
        assert not (tmp_path / "nope.log.gz").exists()
        handler.close()

    # ── 비동기 압축 ↔ 로테이션 경합 방지 (join-pending) ──────

    def test_wait_pending_drains_compression(self, tmp_path):
        """_wait_pending_compressions가 진행 중 압축을 끝까지 대기한다."""
        from app.utils.file_logger import (
            DailySizeRotatingHandler, _get_gzip_executor,
            _wait_pending_compressions,
        )
        import app.utils.file_logger as mod

        handler = DailySizeRotatingHandler(log_dir=str(tmp_path), compress=True)
        target = tmp_path / "drain_test.log"
        target.write_bytes(b"payload\n")

        handler._compress_file_async(target)
        # 대기 후에는 압축이 끝나 .gz 생성 + 원본 삭제 + 추적 집합 비움
        _wait_pending_compressions()

        assert (tmp_path / "drain_test.log.gz").exists()
        assert not target.exists()
        # done_callback이 곧 집합을 비운다 (shutdown으로 콜백 완료 보장)
        _get_gzip_executor().shutdown(wait=True)
        mod._gzip_executor = None
        assert mod._pending_compressions == set()
        handler.close()

    def test_rollover_waits_for_pending_compression(self, tmp_path):
        """압축 진행 중 다음 로테이션이 대기 → 백업 유실·정상 .gz 오삭제 없음.

        경합 윈도우를 강제하려고 압축을 느리게 만든다. 수정 전이라면 2차
        로테이션의 _shift_backups가 압축 중인 .1을 .2(plain)로 옮겨 압축이
        FileNotFoundError로 실패하고 content A가 압축되지 않은 채 남거나
        유실된다. 수정 후에는 .2.gz로 정상 보존된다.
        """
        import time as _time
        from app.utils.file_logger import (
            DailySizeRotatingHandler, _get_gzip_executor, reset_file_logger,
        )
        import app.utils.file_logger as mod

        handler = DailySizeRotatingHandler(
            log_dir=str(tmp_path), max_bytes=200, backup_count=5, compress=True,
        )
        base = handler._log_path()

        # 압축을 느리게 만들어 경합 윈도우 강제
        orig_compress = handler._compress_file

        def slow_compress(fp):
            _time.sleep(0.25)
            orig_compress(fp)

        handler._compress_file = slow_compress  # 인스턴스 메서드 오버라이드

        # content A → 1차 로테이션: base→.1, 느린 compress(.1) 제출(진행 중)
        handler.stream.write("AAAA content\n")
        handler.stream.flush()
        handler.doRollover()

        # content B → 2차 로테이션: _wait_pending이 .1 압축 완료를 대기한 뒤
        # .1.gz(A)→.2.gz, base(B)→.1, compress(.1=B) 제출
        handler.stream.write("BBBB content\n")
        handler.stream.flush()
        handler.doRollover()

        _get_gzip_executor().shutdown(wait=True)
        mod._gzip_executor = None

        gz2 = tmp_path / f"{base.name}.2.gz"
        gz1 = tmp_path / f"{base.name}.1.gz"
        # content A가 .2.gz로 압축 보존 (유실/오삭제 없음)
        assert gz2.exists(), "content A 백업(.2.gz) 유실"
        assert gzip.decompress(gz2.read_bytes()) == b"AAAA content\n"
        # content B는 .1.gz로 보존
        assert gz1.exists()
        assert gzip.decompress(gz1.read_bytes()) == b"BBBB content\n"
        # 압축되지 않은 .2(plain) 잔재 없음
        assert not (tmp_path / f"{base.name}.2").exists()

        handler.close()
        reset_file_logger()

    # ── timestamp 캐싱 (P3) ──────────────────────────────────

    def test_timestamp_cache_same_second(self):
        """같은 초 내 다회 호출에서 strftime 0회 (캐시 hit)."""
        from app.utils.file_logger import _format_ts_cached
        import app.utils.file_logger as mod
        # 캐시 리셋
        mod._last_epoch_sec = -1
        mod._last_ts_str = ""

        with patch("app.utils.file_logger.datetime") as mock_dt:
            mock_dt.fromtimestamp.return_value = datetime(2026, 5, 29, 12, 34, 56)
            with patch("app.utils.file_logger.time") as mock_time:
                mock_time.time.return_value = 1748537696.123  # 고정 epoch
                # 첫 호출: strftime 1회
                ts1 = _format_ts_cached()
                # 두 번째 호출: 같은 초 → strftime 0회
                ts2 = _format_ts_cached()
                ts3 = _format_ts_cached()

        assert ts1 == ts2 == ts3 == "2026-05-29 12:34:56"
        # fromtimestamp는 1회만 호출됨 (캐시 hit 확인)
        assert mock_dt.fromtimestamp.call_count == 1

    def test_timestamp_cache_second_change(self):
        """초 경계 넘으면 strftime 재호출."""
        from app.utils.file_logger import _format_ts_cached
        import app.utils.file_logger as mod
        mod._last_epoch_sec = -1
        mod._last_ts_str = ""

        with patch("app.utils.file_logger.datetime") as mock_dt:
            mock_dt.fromtimestamp.side_effect = [
                datetime(2026, 5, 29, 12, 34, 56),
                datetime(2026, 5, 29, 12, 34, 57),
            ]
            with patch("app.utils.file_logger.time") as mock_time:
                mock_time.time.side_effect = [
                    1748537696.5,
                    1748537697.1,  # 1초 후
                ]
                ts1 = _format_ts_cached()
                ts2 = _format_ts_cached()

        assert ts1 == "2026-05-29 12:34:56"
        assert ts2 == "2026-05-29 12:34:57"
        # 초 경계 넘었으므로 fromtimestamp 2회 호출
        assert mock_dt.fromtimestamp.call_count == 2

    # ── pid 캐싱 (P2) ────────────────────────────────────────

    def test_pid_cached_at_import(self):
        """_PID_STR은 모듈 로드 시 1회 채택 (현재 pid의 8자리 우측 정렬)."""
        from app.utils.file_logger import _PID_STR
        expected = f"{os.getpid():>8}"
        assert _PID_STR == expected
        assert len(_PID_STR) == 8


class TestCompressRaceGuard:
    """압축 성공 후 원본 unlink 실패(cleanup이 먼저 삭제)해도 .gz를 보존한다(R5 MEDIUM)."""

    def setup_method(self):
        from app.utils.file_logger import reset_file_logger
        reset_file_logger()

    def teardown_method(self):
        from app.utils.file_logger import reset_file_logger
        reset_file_logger()

    def test_compress_preserves_gz_when_source_unlink_fails(self, tmp_path, monkeypatch):
        import gzip as _gz
        from pathlib import Path as _P
        from app.utils.file_logger import DailySizeRotatingHandler

        h = DailySizeRotatingHandler(str(tmp_path), prefix="redgw", compress=True)
        try:
            src = tmp_path / "redgw.20240101.log.1"
            src.write_text("some log data\n")

            real_unlink = _P.unlink

            def fake_unlink(self, *a, **k):
                # 압축 중 cleanup이 원본을 먼저 지운 상황을 시뮬레이션
                if self.name == "redgw.20240101.log.1":
                    raise FileNotFoundError("simulated: source already deleted")
                return real_unlink(self, *a, **k)

            monkeypatch.setattr(_P, "unlink", fake_unlink)
            h._compress_file(src)

            gz = tmp_path / "redgw.20240101.log.1.gz"
            assert gz.exists()                       # 원본 unlink 실패에도 .gz 보존
            with _gz.open(gz, "rt") as f:
                assert f.read() == "some log data\n"  # 내용 정상
        finally:
            h.close()
