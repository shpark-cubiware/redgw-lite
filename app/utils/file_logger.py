"""통합 파일 로거 — log/redgw.yyyymmdd.log

일자별 파일 분리 + 크기 기반 로테이션 + gzip 압축 + 자동 클린업.

포맷: {timestamp} ({level}) [{pid:>8}] [{tag:<8}] {message}
태그: MAIN, CONFIG, STATUS
시간대: 로컬 시간 (KST) 사용

멀티워커(gunicorn) 환경에서 시작 로그 중복 방지:
  acquire_startup_lock()으로 첫 번째 워커만 시작 시퀀스를 기록한다.
"""

from __future__ import annotations

import asyncio
import atexit
import fcntl
import gzip
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from logging.handlers import BaseRotatingHandler
from pathlib import Path
from typing import IO

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 로그 정책 상수 — 환경변수로 오버라이드 가능
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_LOG_DIR = os.environ.get("REDGW_LOG_DIR", "/app/log")
_LOG_PREFIX = "redgw"                                                          # 파일명 접두어
_MAX_BYTES = int(os.environ.get("REDGW_LOG_MAX_SIZE_MB", "10")) * 1024 * 1024  # 10 MB
_BACKUP_COUNT = int(os.environ.get("REDGW_LOG_BACKUP_COUNT", "5"))             # 일자 내 백업 수
_RETENTION_DAYS = int(os.environ.get("REDGW_LOG_RETENTION_DAYS", "7"))         # 보존 일수
_DATE_FORMAT = "%Y%m%d"                                                        # 파일명 일자 포맷
_COMPRESS_BACKUP = True                                                        # 백업 gzip 압축 여부

_file_logger: logging.Logger | None = None
_initialized: bool = False

_LEVEL_MAP = {
    "I": logging.INFO,
    "W": logging.WARNING,
    "E": logging.ERROR,
    "C": logging.CRITICAL,
    "F": logging.CRITICAL,
}

# 시작 로그 중복 방지용 파일 락
_startup_lock_fd: IO[str] | None = None
_is_startup_logger: bool = False

# P2: pid는 fork 후 불변. 모듈 로드 시 1회 캐싱 (기본은 import 시점, fork 후 다시 import되면 자동 갱신)
_PID_STR = f"{os.getpid():>8}"

# P3: 초 단위 timestamp 캐싱. 같은 초 내 다회 호출에서 strftime 비용 절감
_last_epoch_sec: int = -1
_last_ts_str: str = ""

# P4: gzip 압축 백그라운드 thread. 핸들러 rollover 호출자(워커)를 블록하지 않음
# 단일 워커(daemon)로 직렬 처리 — 같은 디렉토리 동시 압축 경합 회피
_gzip_executor: ThreadPoolExecutor | None = None

# 진행 중인 gzip 압축 Future 추적 — 다음 로테이션의 rename이 압축 중인 파일을
# 이동/삭제하는 경합(백업 유실·정상 .gz 오삭제)을 막는다. doRollover 진입 시
# _wait_pending_compressions()로 모두 완료시킨 뒤 rename한다. (프로세스 내 보호)
_pending_compressions: set[Future] = set()
_pending_lock = threading.Lock()


def _get_gzip_executor() -> ThreadPoolExecutor:
    """gzip 백그라운드 워커 lazy init. atexit에서 wait 보장."""
    global _gzip_executor
    if _gzip_executor is None:
        _gzip_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="redgw-gzip")
        atexit.register(lambda: _gzip_executor.shutdown(wait=True) if _gzip_executor else None)
    return _gzip_executor


def _discard_pending_compression(future: Future) -> None:
    """압축 완료 시 추적 집합에서 제거 (done_callback — gzip 스레드에서 실행)."""
    with _pending_lock:
        _pending_compressions.discard(future)


def _wait_pending_compressions() -> None:
    """진행 중인 gzip 압축이 모두 끝날 때까지 대기.

    로테이션(_shift_backups의 rename)이 백그라운드 압축 중인 파일을 이동/삭제해
    백업이 유실되거나 정상 .gz가 잘못 삭제되는 경합을 막는다. 공통 경로에서는
    이전 압축이 이미 끝나 즉시 반환하고, 로그 버스트로 직전 로테이션의 압축이
    아직 진행 중일 때만 잠깐 블록한다. doRollover는 핸들러 락 아래에서 실행되며
    gzip 스레드는 그 락을 잡지 않으므로 교착 없음.
    """
    with _pending_lock:
        futures = list(_pending_compressions)
    for fut in futures:
        try:
            fut.result()
        except Exception:
            pass  # 압축 실패는 _compress_file에서 이미 debug 로깅됨


def _format_ts_cached() -> str:
    """초 단위 timestamp 캐싱. 같은 초 내 호출은 캐시 hit (strftime 호출 0)."""
    global _last_epoch_sec, _last_ts_str
    now = time.time()
    sec = int(now)
    if sec != _last_epoch_sec:
        _last_ts_str = datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M:%S")
        _last_epoch_sec = sec
    return _last_ts_str


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DailySizeRotatingHandler — 일자별 파일 + 크기 기반 로테이션
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class DailySizeRotatingHandler(BaseRotatingHandler):
    """일자별 파일 분리 + 크기 기반 백업 로테이션.

    - 파일명: redgw.20260306.log
    - 크기 초과 시: redgw.20260306.log.1, .2, ... (이전 백업은 .gz 압축)
    - 일자 변경 시: 이전 파일 .gz 압축 + 새 날짜 파일 전환
    - retention_days 이전 파일 자동 삭제 (init + 일자 전환 시)
    """

    def __init__(
        self,
        log_dir: str,
        prefix: str = _LOG_PREFIX,
        max_bytes: int = _MAX_BYTES,
        backup_count: int = _BACKUP_COUNT,
        retention_days: int = _RETENTION_DAYS,
        compress: bool = _COMPRESS_BACKUP,
        date_format: str = _DATE_FORMAT,
        encoding: str = "utf-8",
        max_total_size_mb: int = 0,
    ):
        self.log_dir = Path(log_dir)
        self.prefix = prefix
        self.maxBytes = max_bytes
        self.backupCount = backup_count
        self.retention_days = retention_days
        self.compress = compress
        self.date_format = date_format
        # 디렉토리 전체 상한 (0=무제한). 회전 시 합산 검사 후 초과분만큼 가장 오래된 .gz부터 삭제
        self.max_total_bytes = max_total_size_mb * 1024 * 1024 if max_total_size_mb > 0 else 0

        # 파일명에서 날짜 추출용 정규식 (인스턴스별 1회 컴파일)
        self._date_re = re.compile(
            rf"^{re.escape(self.prefix)}\.(\d{{8}})\.log"
        )
        # P1: 백업 정렬용 (.gz / .N 파일 매칭). 디렉토리 총량 감시에 사용
        self._archive_re = re.compile(
            rf"^{re.escape(self.prefix)}\.\d{{8}}\.log(?:\.\d+)?(?:\.gz)?$"
        )

        self._current_date = datetime.now().strftime(self.date_format)
        filename = str(self._log_path())

        super().__init__(filename, mode="a", encoding=encoding)

        # P1: 현재 파일의 누적 바이트 수 — shouldRollover에서 seek/tell 회피용
        try:
            self._current_bytes = Path(filename).stat().st_size if Path(filename).exists() else 0
        except OSError:
            self._current_bytes = 0

        # 핸들러 초기화 시 오래된 파일 정리
        self.cleanup_old_files()
        self._cleanup_by_total_size()

    def _log_path(self, date_str: str | None = None) -> Path:
        """주어진 날짜(또는 현재 날짜)의 로그 파일 경로 반환."""
        d = date_str or self._current_date
        return self.log_dir / f"{self.prefix}.{d}.log"

    def shouldRollover(self, record: logging.LogRecord) -> int:
        """일자 변경 또는 크기 초과 시 rollover 필요.

        P1: seek/tell 호출 제거. 내부 바이트 카운터(_current_bytes)로 판정 (약간의 오버슈트 허용).
        P3: strftime은 캐시된 timestamp의 일자 부분 사용 — 같은 초 내 호출은 strftime 0회.

        format()은 본 메서드 1회만 호출 — 회전 안 함 시 카운터를 미리 갱신해
        후속 emit/write가 실제로 쓸 크기를 반영한다. write 실패 시 카운터에
        약간의 부정확이 누적 가능하지만, 이는 maxBytes 임계 약간 일찍/늦게
        도달하는 정도이며 운영 영향 없음.
        """
        ts = _format_ts_cached()
        today = ts[0:4] + ts[5:7] + ts[8:10]  # "YYYY-MM-DD" → "YYYYMMDD"
        if today != self._current_date:
            return 1
        if self.maxBytes > 0:
            msg = self.format(record)
            msg_bytes = len(msg.encode("utf-8")) + 1  # +1 for newline
            if self._current_bytes + msg_bytes >= self.maxBytes:
                return 1
            # 회전 안 함 — 예상 쓰기 크기를 카운터에 반영
            self._current_bytes += msg_bytes
        return 0

    def doRollover(self) -> None:
        """크기 초과 → 백업 시프트, 일자 변경 → 새 파일 전환.

        P4: gzip 압축은 백그라운드 thread로 위임. 호출 워커 블록 회피.
        """
        # 직전 로테이션이 위임한 gzip 압축이 아직 진행 중이면 먼저 완료시킨다.
        # 그래야 이어지는 백업 rename이 압축 중인 파일을 건드리지 않는다(경합 차단).
        # 공통 경로(압축이 이미 끝남)에서는 즉시 통과한다.
        _wait_pending_compressions()

        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]

        ts = _format_ts_cached()
        today = ts[0:4] + ts[5:7] + ts[8:10]

        if today != self._current_date:
            # ── 일자 전환 ──
            old_path = self._log_path(self._current_date)
            if old_path.exists() and self.compress:
                self._compress_file_async(old_path)
            self._current_date = today
            self.cleanup_old_files()
        else:
            # ── 크기 초과: 백업 시프트 ──
            self._shift_backups()

        # 새 파일 열기 + 카운터 리셋
        self.baseFilename = str(self._log_path())
        self.stream = self._open()
        self._current_bytes = 0

        # 디렉토리 총량 감시 (max_total_size_mb)
        self._cleanup_by_total_size()

    def _shift_backups(self) -> None:
        """기존 백업 번호를 증가시키고 현재 파일을 .1로 이동."""
        base = self._log_path()

        # 높은 번호부터 역순으로 시프트: .N → .N+1, 최대치는 삭제
        for i in range(self.backupCount, 0, -1):
            src_gz = Path(f"{base}.{i}.gz")
            src_plain = Path(f"{base}.{i}")

            if i == self.backupCount:
                src_gz.unlink(missing_ok=True)
                src_plain.unlink(missing_ok=True)
            else:
                dst_gz = Path(f"{base}.{i + 1}.gz")
                dst_plain = Path(f"{base}.{i + 1}")
                if src_gz.exists():
                    src_gz.rename(dst_gz)
                elif src_plain.exists():
                    src_plain.rename(dst_plain)

        # 현재 파일 → .1
        if base.exists():
            target = Path(f"{base}.1")
            base.rename(target)
            if self.compress:
                self._compress_file_async(target)

    def _compress_file(self, filepath: Path) -> None:
        """파일을 gzip 압축 후 원본 삭제. 압축 본체 성공 시 .gz는 무조건 보존.

        압축 성공과 원본 삭제를 분리한다 — 합쳐 두면 cleanup 스레드가 압축 중 원본을
        먼저 지웠을 때 filepath.unlink()가 FileNotFoundError를 내고 except가 정상 생성된
        .gz까지 삭제해 그날 로그가 전손된다(in-process 경합). 압축 자체가 실패한 경우만
        불완전 .gz를 제거하고, 성공 시 원본 삭제는 best-effort로 격리한다.
        """
        gz_path = Path(f"{filepath}.gz")
        try:
            with open(filepath, "rb") as f_in:
                with gzip.open(gz_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
        except Exception as e:
            logging.getLogger("redgw.file").debug(
                "File compression failed for %s: %s", filepath, e,
            )
            gz_path.unlink(missing_ok=True)   # 압축 본체 실패 → 불완전 .gz만 제거
            return
        # 압축 성공 → .gz 보존. 원본 삭제는 best-effort(이미 지워졌어도 .gz는 유지).
        try:
            filepath.unlink()
        except OSError:
            pass

    def _compress_file_async(self, filepath: Path) -> None:
        """P4: gzip 압축을 백그라운드 thread에 위임. 호출 워커 블록 회피.

        제출한 Future를 _pending_compressions에 등록한다. 다음 로테이션이
        _wait_pending_compressions()로 완료를 기다려 rename 경합을 막는다.
        """
        future = _get_gzip_executor().submit(self._compress_file, filepath)
        # 완료 전에 집합에 먼저 넣고 콜백을 단다(이미 끝났으면 콜백이 즉시 제거).
        with _pending_lock:
            _pending_compressions.add(future)
        future.add_done_callback(_discard_pending_compression)

    def cleanup_old_files(self) -> None:
        """retention_days 이전의 로그 파일을 삭제."""
        cutoff_str = (
            datetime.now() - timedelta(days=self.retention_days)
        ).strftime(self.date_format)

        for fpath in self.log_dir.iterdir():
            m = self._date_re.match(fpath.name)
            if m and m.group(1) < cutoff_str:
                try:
                    fpath.unlink()
                except Exception as e:
                    logging.getLogger("redgw.file").debug(
                        "retention cleanup failed for %s: %s", fpath, e,
                    )

    def _cleanup_by_total_size(self) -> None:
        """디렉토리 합산 크기가 max_total_bytes를 초과하면 가장 오래된 .gz부터 삭제.

        max_total_bytes=0이면 무제한 (검사 안 함).
        현재 활성 파일(self.baseFilename)만 삭제 대상에서 제외한다. 하드 사이즈 상한을
        보장하기 위해, 상한 초과 시에는 가장 최근 백업도 삭제 후보에 포함된다(가장
        오래된 .gz/.N부터 제거). 감사 핸들러의 disk_full 회계가 이 하드 상한에 의존한다.
        """
        if self.max_total_bytes <= 0:
            return

        try:
            entries: list[tuple[Path, int, float]] = []  # (path, size, mtime)
            for fpath in self.log_dir.iterdir():
                if not self._archive_re.match(fpath.name):
                    continue
                try:
                    st = fpath.stat()
                    entries.append((fpath, st.st_size, st.st_mtime))
                except OSError:
                    continue

            total = sum(e[1] for e in entries)
            if total <= self.max_total_bytes:
                return

            # 활성 baseFilename은 삭제 대상 제외
            active = Path(self.baseFilename).resolve()
            # 오래된 것부터 정렬 (mtime 오름차순)
            entries.sort(key=lambda e: e[2])

            deleted = 0
            for fpath, size, _ in entries:
                if total <= self.max_total_bytes:
                    break
                try:
                    if fpath.resolve() == active:
                        continue
                except OSError:
                    continue
                # .gz 또는 .N 백업만 삭제 (오늘자 원본 .log는 가능하면 보존하되, 한계 초과 시 같이 정리)
                try:
                    fpath.unlink()
                    total -= size
                    deleted += 1
                except OSError:
                    continue

            # audit 핸들러일 경우 드롭 카운터에 누적 (disk_full)
            if deleted > 0 and self.prefix.startswith("audit"):
                try:
                    from app.audit.logger import increment_dropped_disk_full
                    increment_dropped_disk_full(deleted)
                except Exception:
                    pass
        except Exception as e:
            logging.getLogger("redgw.file").debug(
                "max_total_size cleanup skipped: %s", e,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 공개 API (시그니처 변경 없음)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def init_file_logger(log_dir: str | None = None) -> None:
    """DailySizeRotatingHandler 초기화. 앱 시작 시 1회 호출."""
    global _file_logger, _initialized

    if _initialized:
        return

    target_dir = Path(log_dir or _LOG_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("redgw.file")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # 기존 핸들러 제거 (테스트 격리용)
    logger.handlers.clear()

    # 운영 로그 디렉토리 총량 상한 — 환경변수 REDGW_LOG_MAX_TOTAL_MB (0=무제한)
    max_total_mb = int(os.environ.get("REDGW_LOG_MAX_TOTAL_MB", "0"))

    handler = DailySizeRotatingHandler(
        log_dir=str(target_dir),
        prefix=_LOG_PREFIX,
        max_bytes=_MAX_BYTES,
        backup_count=_BACKUP_COUNT,
        retention_days=_RETENTION_DAYS,
        compress=_COMPRESS_BACKUP,
        date_format=_DATE_FORMAT,
        max_total_size_mb=max_total_mb,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    _file_logger = logger
    _initialized = True


def write_log(tag: str, message: str, level: str = "I") -> None:
    """태그 포함 한 줄 기록 (동기).

    Args:
        tag: 로그 태그 (MAIN, CONFIG, STATUS)
        message: 로그 메시지
        level: I(INFO), W(WARNING), E(ERROR), F(FATAL)

    P2: pid는 모듈 캐시 사용. P3: timestamp는 초 단위 캐시 사용.
    """
    if not _initialized:
        init_file_logger()

    try:
        ts = _format_ts_cached()
        line = f"{ts} ({level}) [{_PID_STR}] [{tag:<8}] {message}"
        py_level = _LEVEL_MAP.get(level, logging.INFO)
        _file_logger.log(py_level, line)  # type: ignore[union-attr]
    except Exception:
        pass  # 파일 로깅 실패는 무시 (메인 동작에 영향 없음)


async def write_log_async(tag: str, message: str, level: str = "I") -> None:
    """비동기 래퍼 — asyncio.to_thread()."""
    try:
        await asyncio.to_thread(write_log, tag, message, level)
    except Exception:
        pass


def acquire_startup_lock() -> bool:
    """시작 로그 중복 방지용 파일 락 획득.

    gunicorn 멀티워커 환경에서 첫 번째 워커만 시작 시퀀스(MAIN, CONFIG)를
    기록하도록 fcntl.flock(LOCK_EX | LOCK_NB) 기반 비차단 락을 사용한다.

    Returns:
        True: 이 워커가 시작 로그를 기록해야 함
        False: 다른 워커가 이미 기록 중 — 스킵
    """
    global _startup_lock_fd, _is_startup_logger

    log_dir = Path(os.environ.get("REDGW_LOG_DIR", _LOG_DIR))
    log_dir.mkdir(parents=True, exist_ok=True)
    lock_path = log_dir / ".redgw_startup.lock"

    try:
        _startup_lock_fd = open(lock_path, "w")
        fcntl.flock(_startup_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _startup_lock_fd.write(str(os.getpid()))
        _startup_lock_fd.flush()
        _is_startup_logger = True
        return True
    except OSError:
        if _startup_lock_fd:
            try:
                _startup_lock_fd.close()
            except Exception:
                pass
            _startup_lock_fd = None
        _is_startup_logger = False
        return False


def release_startup_lock() -> None:
    """시작 로그 락 해제 (종료 시 호출)."""
    global _startup_lock_fd, _is_startup_logger

    if _startup_lock_fd:
        try:
            fcntl.flock(_startup_lock_fd, fcntl.LOCK_UN)
            _startup_lock_fd.close()
        except Exception:
            pass
        _startup_lock_fd = None
        # 락 파일 삭제는 보유 워커만 수행한다. 비보유 워커가 지우면 다른 워커가
        # 락을 재획득해 시작 로그가 중복 방출될 수 있다(dedup 보증 약화).
        lock_path = Path(os.environ.get("REDGW_LOG_DIR", _LOG_DIR)) / ".redgw_startup.lock"
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    _is_startup_logger = False


def is_startup_logger() -> bool:
    """이 워커가 시작 로그를 기록하는 워커인지 반환."""
    return _is_startup_logger


def reset_file_logger() -> None:
    """테스트용 싱글턴 리셋."""
    global _file_logger, _initialized, _startup_lock_fd, _is_startup_logger
    # 진행 중인 압축을 비우고 추적 집합을 정리 (테스트 간 상태 격리)
    _wait_pending_compressions()
    with _pending_lock:
        _pending_compressions.clear()
    if _file_logger:
        for h in _file_logger.handlers[:]:
            h.close()
            _file_logger.removeHandler(h)
    _file_logger = None
    _initialized = False
    if _startup_lock_fd:
        try:
            _startup_lock_fd.close()
        except Exception:
            pass
    _startup_lock_fd = None
    _is_startup_logger = False
