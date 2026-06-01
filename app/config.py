"""설정 로드 모듈 — config.yaml + 환경변수 오버라이드"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

logger = logging.getLogger("redgw.config")


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    workers: int = 4
    cors_origins: list[str] = []
    docs: bool = False


class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379/0"
    max_connections: int = 50
    decode_responses: bool = True
    socket_timeout: int = 5
    retry_on_timeout: bool = True


class ClientConfig(BaseModel):
    api_key: str
    description: str = ""
    namespaces: dict[str, list[str]] = {}


class DefaultsConfig(BaseModel):
    ttl: int = 86400
    max_list_length: int = 10000
    max_stream_length: int = 50000
    max_value_size: int = 1048576


class AdminConfig(BaseModel):
    api_key: str = ""


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: Literal["plain"] = "plain"
    access_log: bool = True


class AuditConfig(BaseModel):
    """감사 로그 설정. 운영 로그(LoggingConfig)와 독립.

    level: 0=OFF (운영 금지), 1=BASIC (디폴트), 2=FULL (read 포함).
    directory: 운영 로그(/app/log/)와 최상위 분리.
    payload_prefix_bytes: Level 2 옵트인 (디폴트 0=해시만, >0이면 첫 N 바이트 평문).
    per_worker_files: true시 파일명에 pid 부여 (멀티워커 인터리빙 회피).
    """

    level: Literal[0, 1, 2] = 1
    directory: str = "/app/audit"
    max_file_size_mb: int = 10
    retention_days: int = 30
    max_total_size_mb: int = 1024
    disk_min_free_pct: int = 10
    payload_prefix_bytes: int = 0
    # nginx가 /metrics·/prometheus/metrics를 모두 /api/v1/metrics로 rewrite하므로 앱이 보는
    # 실제 경로는 '/api/v1/metrics'다. bare '/metrics'만으론 정확일치 매칭이 안 돼(죽은 설정)
    # Level 2에서 scrape마다 감사 노이즈가 쌓인다 → 실제 라우트 경로도 포함.
    exclude_paths: list[str] = ["/health", "/metrics", "/api/v1/metrics", "/"]
    per_worker_files: bool = False
    # Q3 Correlation ID — API 쓰기의 origin(요청자·시각)을 Redis에 기록. 활성 시 핫패스에 Redis SET 1회 추가
    correlation: bool = False
    correlation_ttl_seconds: int = 86400


class Settings(BaseModel):
    server: ServerConfig = ServerConfig()
    redis: RedisConfig = RedisConfig()
    clients: dict[str, ClientConfig] = {}
    defaults: DefaultsConfig = DefaultsConfig()
    admin: AdminConfig = AdminConfig()
    logging: LoggingConfig = LoggingConfig()
    audit: AuditConfig = AuditConfig()


def load_config(path: str | None = None) -> Settings:
    """config.yaml을 로드하고 환경변수로 오버라이드한다."""
    explicit = path is not None or "REDGW_CONFIG" in os.environ
    if path is None:
        path = os.environ.get("REDGW_CONFIG", "config/config.yaml")

    config_path = Path(path)
    if not config_path.exists():
        if explicit:
            raise SystemExit(f"FATAL: Config file not found: {config_path}")
        logger.warning("Config file not found: %s — using defaults", config_path)
        return _apply_env_overrides(Settings())

    with open(config_path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    settings = Settings(**raw)
    return _apply_env_overrides(settings)


def validate_config(settings: Settings) -> list[str]:
    """설정 검증. 경고 목록 반환, 치명적 오류 시 SystemExit."""
    warnings: list[str] = []

    # 1) admin API 키 비어있으면 경고
    if not settings.admin.api_key:
        warnings.append("admin.api_key is empty — admin endpoints are unprotected")

    # 2) 클라이언트 API 키 중복 검사
    api_keys = [c.api_key for c in settings.clients.values()]
    if settings.admin.api_key:
        api_keys.append(settings.admin.api_key)
    seen: set[str] = set()
    duplicates: list[str] = []
    for k in api_keys:
        if k in seen:
            if k not in duplicates:
                duplicates.append(k)
        else:
            seen.add(k)
    if duplicates:
        raise SystemExit(
            "FATAL: Duplicate API keys detected — each client must have a unique key"
        )

    # 3) 클라이언트 없으면 경고
    if not settings.clients:
        warnings.append("No clients defined — only admin access will work")

    # 4) 플레이스홀더 API 키 감지 (.env 오버라이드 누락)
    _PLACEHOLDER_PATTERNS = ("xxxxxxxx", "CHANGE_ME")
    for client_id, cfg in settings.clients.items():
        if any(p in cfg.api_key for p in _PLACEHOLDER_PATTERNS):
            warnings.append(
                f"Client '{client_id}' API key looks like a placeholder"
                f" — set REDGW_CLIENT_{client_id}_API_KEY in .env"
            )
    if settings.admin.api_key and any(
        p in settings.admin.api_key for p in _PLACEHOLDER_PATTERNS
    ):
        warnings.append(
            "Admin API key looks like a placeholder"
            " — set REDGW_ADMIN_API_KEY in .env"
        )

    # 5) .env 환경변수 누락 감지 (config.yaml 기본값 사용 중)
    for client_id in settings.clients:
        env_key = f"REDGW_CLIENT_{client_id}_API_KEY"
        if not os.environ.get(env_key):
            warnings.append(
                f"Environment variable {env_key} not set"
                f" — client '{client_id}' uses config.yaml default key"
            )
    if not os.environ.get("REDGW_ADMIN_API_KEY"):
        warnings.append(
            "Environment variable REDGW_ADMIN_API_KEY not set"
            " — admin uses config.yaml default key"
        )

    # 6) 미사용 환경변수 감지 (config.yaml에 없는 클라이언트)
    for env_name in os.environ:
        m = re.fullmatch(r"REDGW_CLIENT_(\w+)_API_KEY", env_name)
        if m:
            cid = m.group(1)
            if cid not in settings.clients:
                warnings.append(
                    f"Environment variable {env_name} is set"
                    f" but client '{cid}' is not defined in config.yaml"
                )

    return warnings


def _apply_env_overrides(settings: Settings) -> Settings:
    """환경변수로 주요 설정을 오버라이드한다."""
    if redis_url := os.environ.get("REDGW_REDIS_URL"):
        settings.redis.url = redis_url
    if host := os.environ.get("REDGW_HOST"):
        settings.server.host = host
    if port := os.environ.get("REDGW_PORT"):
        try:
            settings.server.port = int(port)
        except ValueError:
            raise SystemExit(f"FATAL: REDGW_PORT must be integer — got {port!r}")
    if log_level := os.environ.get("REDGW_LOG_LEVEL"):
        settings.logging.level = log_level

    # CORS 허용 도메인 오버라이드 (쉼표 구분)
    if cors := os.environ.get("REDGW_CORS_ORIGINS"):
        settings.server.cors_origins = [o.strip() for o in cors.split(",") if o.strip()]

    # 클라이언트 API 키 오버라이드: REDGW_CLIENT_{CLIENT_ID}_API_KEY
    for client_id in settings.clients:
        env_key = f"REDGW_CLIENT_{client_id}_API_KEY"
        if val := os.environ.get(env_key):
            settings.clients[client_id].api_key = val

    # 관리자 API 키 오버라이드
    if admin_key := os.environ.get("REDGW_ADMIN_API_KEY"):
        settings.admin.api_key = admin_key

    # OpenAPI 문서 활성화 오버라이드
    if docs := os.environ.get("REDGW_DOCS_ENABLED"):
        settings.server.docs = docs.lower() in ("1", "true", "yes")

    # Audit 설정 오버라이드
    if audit_level := os.environ.get("REDGW_AUDIT_LEVEL"):
        try:
            lv = int(audit_level)
            if lv not in (0, 1, 2):
                raise SystemExit(
                    f"FATAL: REDGW_AUDIT_LEVEL must be 0, 1, or 2 — got {audit_level!r}"
                )
            settings.audit.level = lv  # type: ignore[assignment]
        except ValueError:
            raise SystemExit(
                f"FATAL: REDGW_AUDIT_LEVEL must be integer 0/1/2 — got {audit_level!r}"
            )
    if audit_dir := os.environ.get("REDGW_AUDIT_DIRECTORY"):
        settings.audit.directory = audit_dir
    if audit_max_total := os.environ.get("REDGW_AUDIT_MAX_TOTAL_MB"):
        try:
            settings.audit.max_total_size_mb = int(audit_max_total)
        except ValueError:
            raise SystemExit(
                f"FATAL: REDGW_AUDIT_MAX_TOTAL_MB must be integer — got {audit_max_total!r}"
            )
    if audit_retention := os.environ.get("REDGW_AUDIT_RETENTION_DAYS"):
        try:
            settings.audit.retention_days = int(audit_retention)
        except ValueError:
            raise SystemExit(
                f"FATAL: REDGW_AUDIT_RETENTION_DAYS must be integer — got {audit_retention!r}"
            )

    return settings


# WHY: DI 대신 전역 싱글턴 — gunicorn fork 후 각 worker에서 1회만 로드, 이후 캐싱
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_config()
    return _settings


def reset_settings() -> None:
    """테스트용 설정 리셋 (API 키 역매핑 캐시도 함께 무효화)"""
    global _settings
    _settings = None
    # 순환 import 방지를 위해 지연 import
    from app.auth.api_key import reset_key_map
    reset_key_map()
