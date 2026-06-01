"""설정 검증 오케스트레이터 — config.yaml 정합성 검증 + 리포트 출력

앱 시작 시 config.yaml을 검증하고 결과를 log/redgw.log + stdout에 기록한다.
추가 설정 파일 검증은 상용(full) 전용 모듈이 있을 때만 위임 수행하며, 없으면 건너뛴다.

심각도 4단계: FATAL(시작 불가) / ERROR(기능 제한) / WARNING(주의) / INFO(참고)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("redgw.config_validator")


# ── 데이터 모델 ──────────────────────────────────────────────


class Severity(Enum):
    FATAL = "FATAL"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass
class ConfigIssue:
    severity: Severity
    source: str  # 예: "config.yaml" | "cross-config"
    message: str
    hint: str = ""


@dataclass
class ValidationReport:
    issues: list[ConfigIssue] = field(default_factory=list)

    @property
    def has_fatal(self) -> bool:
        return any(i.severity == Severity.FATAL for i in self.issues)

    @property
    def summary(self) -> dict[Severity, int]:
        counts = dict.fromkeys(Severity, 0)
        for i in self.issues:
            counts[i.severity] += 1
        return counts

    def add(self, severity: Severity, source: str, message: str, hint: str = "") -> None:
        self.issues.append(ConfigIssue(severity=severity, source=source, message=message, hint=hint))


# Severity → 파일 로그 레벨 / stdout 로그 함수 매핑
_SEVERITY_FILE_LEVEL = {
    Severity.FATAL: "F",
    Severity.ERROR: "E",
    Severity.WARNING: "W",
    Severity.INFO: "I",
}


_LOG_FN_MAP = {
    Severity.FATAL: logger.critical,
    Severity.ERROR: logger.error,
    Severity.WARNING: logger.warning,
    Severity.INFO: logger.info,
}


def _log_issues(report: ValidationReport, tag: str) -> None:
    """검증 이슈를 redgw.log + stdout에 출력하는 공통 헬퍼."""
    from app.utils.file_logger import write_log

    for issue in report.issues:
        lvl = _SEVERITY_FILE_LEVEL[issue.severity]
        msg = f"[{issue.severity.value}] {issue.source}: {issue.message}"
        write_log(tag, msg, level=lvl)
        if issue.hint:
            write_log(tag, f"  -> {issue.hint}", level=lvl)
            _LOG_FN_MAP[issue.severity](msg + f" -> {issue.hint}")
        else:
            _LOG_FN_MAP[issue.severity](msg)


# ── 통합 검증 ────────────────────────────────────────────────


def validate_all_configs() -> ValidationReport:
    """설정 파일을 통합 검증한다.

    config.yaml은 항상 검증하고, 추가 설정 검증은 상용(full) 전용 모듈이 있을 때만
    위임 수행한다. 공개판(lite)에는 해당 모듈이 없으므로 ImportError를 흡수하고 건너뛴다.
    """
    report = ValidationReport()

    # config.yaml
    settings = _load_settings_safe()
    if settings:
        _validate_main_config(settings, report)
    else:
        report.add(Severity.ERROR, "config.yaml", "Failed to load config.yaml")



    return report


def log_validation_report(report: ValidationReport) -> None:
    """검증 리포트를 redgw.log + stdout에 출력한다."""
    from app.utils.file_logger import write_log

    tag = "CONFIG"

    write_log(tag, "=" * 60)
    write_log(tag, "  RedGW Configuration Validation Report")
    write_log(tag, "=" * 60)

    # 설정 요약
    settings = _load_settings_safe()
    if settings:
        client_count = len(settings.clients)
        admin_set = "set" if settings.admin.api_key else "NOT SET"
        write_log(tag, f"config.yaml          : {client_count} clients, admin key {admin_set}")
        for cid, cfg in settings.clients.items():
            masked = _mask_api_key(cfg.api_key)
            desc = f" ({cfg.description})" if getattr(cfg, "description", None) else ""
            write_log(tag, f"  - {cid}: key={masked}{desc}")



    write_log(tag, "-" * 60)

    # 요약 카운트
    s = report.summary
    write_log(
        tag,
        f"Summary: {s[Severity.FATAL]} FATAL, {s[Severity.ERROR]} ERROR, "
        f"{s[Severity.WARNING]} WARNING, {s[Severity.INFO]} INFO",
    )

    _log_issues(report, tag)

    write_log(tag, "=" * 60)


# ── 개별 검증 함수 ──────────────────────────────────────────


def _validate_main_config(settings: Any, report: ValidationReport) -> None:
    """config.yaml 추가 검증."""
    source = "config.yaml"

    # 1) API 키 중복 검사 (FATAL)
    api_keys: list[str] = [c.api_key for c in settings.clients.values()]
    if settings.admin.api_key:
        api_keys.append(settings.admin.api_key)
    seen: set[str] = set()
    duplicates: set[str] = set()
    for k in api_keys:
        if k in seen:
            duplicates.add(k)
        seen.add(k)
    if duplicates:
        report.add(Severity.FATAL, source, "Duplicate API keys detected",
                    "각 클라이언트에 고유한 API 키를 설정하세요")

    # 2) admin API 키 비어있음
    if not settings.admin.api_key:
        report.add(Severity.WARNING, source, "admin.api_key is empty",
                    "REDGW_ADMIN_API_KEY 환경변수를 설정하세요")

    # 3) 클라이언트 없음
    if not settings.clients:
        report.add(Severity.WARNING, source, "No clients defined",
                    "config.yaml에 클라이언트를 추가하세요")

    # 4) 플레이스홀더 API 키 감지
    _PLACEHOLDER = ("xxxxxxxx", "CHANGE_ME")
    for client_id, cfg in settings.clients.items():
        if any(p in cfg.api_key for p in _PLACEHOLDER):
            report.add(Severity.WARNING, source,
                        f"Client '{client_id}' API key looks like a placeholder",
                        f".env에 REDGW_CLIENT_{client_id}_API_KEY를 설정하세요")

    # 5) Redis URL 형식 검증
    redis_url = settings.redis.url
    if not re.match(r"^redis://", redis_url):
        report.add(Severity.ERROR, source, f"Invalid Redis URL: {redis_url}",
                    "redis://[:password@]host:port/db 형식이어야 합니다")

    # 6) 서버 포트 범위
    if not (1 <= settings.server.port <= 65535):
        report.add(Severity.WARNING, source,
                    f"Server port {settings.server.port} out of range",
                    "port 값은 1~65535 범위여야 합니다")

    # 7) 네임스페이스 권한 값 검증
    valid_perms = {"read", "write"}
    for client_id, cfg in settings.clients.items():
        for ns, perms in cfg.namespaces.items():
            invalid = [p for p in perms if p not in valid_perms]
            if invalid:
                report.add(Severity.ERROR, source,
                            f"Client '{client_id}' namespace '{ns}' has invalid permissions: {invalid}",
                            "허용 권한: read, write")

    # 8) TTL 범위
    if settings.defaults.ttl <= 0:
        report.add(Severity.WARNING, source,
                    f"Default TTL ({settings.defaults.ttl}) is not positive",
                    "TTL은 양수여야 합니다")

    # 9) max_value_size 범위
    mvs = settings.defaults.max_value_size
    if mvs < 1 or mvs > 10485760:
        report.add(Severity.WARNING, source,
                    f"max_value_size ({mvs}) outside recommended range",
                    "1~10485760(10MB) 범위를 권장합니다")

    # 10) 로그 레벨 유효성
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if settings.logging.level.upper() not in valid_levels:
        report.add(Severity.WARNING, source,
                    f"Invalid log level: {settings.logging.level}",
                    f"유효한 레벨: {', '.join(sorted(valid_levels))}")

    # 11) CORS 와일드카드 + 자격증명 조합 경고
    # CORSMiddleware는 allow_credentials=True로 등록되며, cors_origins에 "*"가 있으면
    # Starlette가 요청 Origin을 반사(Access-Control-Allow-Origin)하면서 동시에
    # Access-Control-Allow-Credentials: true를 보내 모든 출처의 자격증명 포함 요청을 허용한다.
    if "*" in settings.server.cors_origins:
        report.add(Severity.WARNING, source,
                    "cors_origins contains '*' while CORS runs with allow_credentials=True",
                    "와일드카드 대신 명시적 오리진을 나열하세요('*'는 모든 출처의 "
                    "자격증명 포함 요청을 허용).")


# ── 헬퍼 ─────────────────────────────────────────────────────


def _mask_api_key(key: str) -> str:
    """API 키를 마스킹한다. 앞 10자 + **** + 마지막 4자."""
    if not key or len(key) <= 14:
        return "****"
    return key[:10] + "****" + key[-4:]


def _load_settings_safe() -> Any | None:
    """config.yaml을 안전하게 로드한다."""
    try:
        from app.config import get_settings
        return get_settings()
    except Exception:
        return None
