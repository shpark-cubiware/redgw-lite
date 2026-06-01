"""설정 검증 테스트 — app/config_validator.py (config.yaml 공통 검증)

상용(full) 전용 설정 검증 테스트는 별도 파일로 분리되어 있다(공개판 derive 시 제거).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.config_validator import (
    Severity,
    ValidationReport,
    _validate_main_config,
    log_validation_report,
    validate_all_configs,
)


# ── 헬퍼 ─────────────────────────────────────────────────────


def _make_settings(**overrides):
    """테스트용 Settings mock 생성."""
    defaults = MagicMock()
    defaults.ttl = 86400
    defaults.max_value_size = 1048576

    redis = MagicMock()
    redis.url = "redis://localhost:6379/0"

    server = MagicMock()
    server.port = 8080

    admin = MagicMock()
    admin.api_key = "admin_key_12345"

    logging_cfg = MagicMock()
    logging_cfg.level = "INFO"

    client_hrm = MagicMock()
    client_hrm.api_key = "key_hrm_unique_001"
    client_hrm.namespaces = {"HRM": ["read", "write"], "shared": ["read", "write"]}

    client_erp = MagicMock()
    client_erp.api_key = "key_erp_unique_002"
    client_erp.namespaces = {"ERP": ["read", "write"]}

    settings = MagicMock()
    settings.server = server
    settings.redis = redis
    settings.clients = {"HRM": client_hrm, "ERP": client_erp}
    settings.defaults = defaults
    settings.admin = admin
    settings.logging = logging_cfg

    # Apply overrides
    for k, v in overrides.items():
        setattr(settings, k, v)

    return settings


# ── ValidationReport 테스트 ──────────────────────────────────


class TestValidationReport:
    def test_empty_report(self):
        report = ValidationReport()
        assert not report.has_fatal
        assert report.summary[Severity.FATAL] == 0
        assert report.summary[Severity.WARNING] == 0

    def test_add_issue(self):
        report = ValidationReport()
        report.add(Severity.WARNING, "test", "message", "hint")
        assert len(report.issues) == 1
        assert report.issues[0].severity == Severity.WARNING
        assert report.issues[0].hint == "hint"

    def test_has_fatal(self):
        report = ValidationReport()
        report.add(Severity.FATAL, "test", "fatal error")
        assert report.has_fatal

    def test_summary_counts(self):
        report = ValidationReport()
        report.add(Severity.FATAL, "a", "m1")
        report.add(Severity.WARNING, "b", "m2")
        report.add(Severity.WARNING, "c", "m3")
        report.add(Severity.INFO, "d", "m4")
        s = report.summary
        assert s[Severity.FATAL] == 1
        assert s[Severity.ERROR] == 0
        assert s[Severity.WARNING] == 2
        assert s[Severity.INFO] == 1


# ── config.yaml 검증 테스트 ──────────────────────────────────


class TestValidateMainConfig:
    def test_valid_config(self):
        settings = _make_settings()
        report = ValidationReport()
        _validate_main_config(settings, report)
        assert not report.has_fatal
        assert report.summary[Severity.ERROR] == 0

    def test_duplicate_api_keys(self):
        settings = _make_settings()
        settings.clients["ERP"].api_key = settings.clients["HRM"].api_key
        report = ValidationReport()
        _validate_main_config(settings, report)
        assert report.has_fatal

    def test_empty_admin_key(self):
        settings = _make_settings()
        settings.admin.api_key = ""
        report = ValidationReport()
        _validate_main_config(settings, report)
        warnings = [i for i in report.issues if i.severity == Severity.WARNING]
        assert any("admin.api_key" in w.message for w in warnings)

    def test_no_clients(self):
        settings = _make_settings()
        settings.clients = {}
        report = ValidationReport()
        _validate_main_config(settings, report)
        warnings = [i for i in report.issues if i.severity == Severity.WARNING]
        assert any("No clients" in w.message for w in warnings)

    def test_placeholder_api_key(self):
        settings = _make_settings()
        settings.clients["HRM"].api_key = "redgw_ak_hrm_xxxxxxxxxxxxxxxx"
        report = ValidationReport()
        _validate_main_config(settings, report)
        warnings = [i for i in report.issues if i.severity == Severity.WARNING]
        assert any("placeholder" in w.message for w in warnings)

    def test_invalid_redis_url(self):
        settings = _make_settings()
        settings.redis.url = "http://localhost:6379"
        report = ValidationReport()
        _validate_main_config(settings, report)
        errors = [i for i in report.issues if i.severity == Severity.ERROR]
        assert any("Redis URL" in e.message for e in errors)

    def test_valid_redis_url(self):
        settings = _make_settings()
        settings.redis.url = "redis://:password@host:6379/0"
        report = ValidationReport()
        _validate_main_config(settings, report)
        errors = [i for i in report.issues if i.severity == Severity.ERROR and "Redis URL" in i.message]
        assert len(errors) == 0

    def test_cors_wildcard_with_credentials_warns(self):
        settings = _make_settings()
        settings.server.cors_origins = ["*"]
        report = ValidationReport()
        _validate_main_config(settings, report)
        warnings = [i for i in report.issues if i.severity == Severity.WARNING]
        assert any("cors_origins contains '*'" in w.message for w in warnings)

    def test_cors_explicit_origins_no_warn(self):
        settings = _make_settings()
        settings.server.cors_origins = ["https://app.example.com"]
        report = ValidationReport()
        _validate_main_config(settings, report)
        assert not any("cors_origins contains '*'" in i.message for i in report.issues)

    def test_port_out_of_range(self):
        settings = _make_settings()
        settings.server.port = 99999
        report = ValidationReport()
        _validate_main_config(settings, report)
        warnings = [i for i in report.issues if "port" in i.message.lower()]
        assert len(warnings) > 0

    def test_invalid_namespace_permissions(self):
        settings = _make_settings()
        settings.clients["HRM"].namespaces = {"HRM": ["read", "execute"]}
        report = ValidationReport()
        _validate_main_config(settings, report)
        errors = [i for i in report.issues if i.severity == Severity.ERROR]
        assert any("invalid permissions" in e.message for e in errors)

    def test_negative_ttl(self):
        settings = _make_settings()
        settings.defaults.ttl = -1
        report = ValidationReport()
        _validate_main_config(settings, report)
        warnings = [i for i in report.issues if "TTL" in i.message]
        assert len(warnings) > 0

    def test_max_value_size_too_large(self):
        settings = _make_settings()
        settings.defaults.max_value_size = 99999999
        report = ValidationReport()
        _validate_main_config(settings, report)
        warnings = [i for i in report.issues if "max_value_size" in i.message]
        assert len(warnings) > 0

    def test_invalid_log_level(self):
        settings = _make_settings()
        settings.logging.level = "VERBOSE"
        report = ValidationReport()
        _validate_main_config(settings, report)
        warnings = [i for i in report.issues if "log level" in i.message.lower()]
        assert len(warnings) > 0

    def test_valid_log_level(self):
        settings = _make_settings()
        settings.logging.level = "DEBUG"
        report = ValidationReport()
        _validate_main_config(settings, report)
        warnings = [i for i in report.issues if "log level" in i.message.lower()]
        assert len(warnings) == 0


# ── log_validation_report 테스트 ──────────────────────────────


class TestLogValidationReport:
    def test_log_report_no_crash(self, tmp_path):
        """리포트 로깅이 에러 없이 실행된다."""
        from app.utils.file_logger import init_file_logger, reset_file_logger
        reset_file_logger()
        init_file_logger(str(tmp_path))

        try:
            report = ValidationReport()
            report.add(Severity.WARNING, "config.yaml", "Test warning", "Fix it")
            report.add(Severity.INFO, "config.yaml", "Test info")

            with patch("app.config_validator._load_settings_safe", return_value=_make_settings()):
                log_validation_report(report)

            log_file = next(tmp_path.glob("redgw.*.log"))
            content = log_file.read_text()
            assert "Validation Report" in content
            assert "[WARNING]" in content
            assert "[INFO]" in content
            assert "Fix it" in content
        finally:
            reset_file_logger()

    def test_log_report_fatal(self, tmp_path):
        """FATAL 이슈가 포함된 리포트."""
        from app.utils.file_logger import init_file_logger, reset_file_logger
        reset_file_logger()
        init_file_logger(str(tmp_path))

        try:
            report = ValidationReport()
            report.add(Severity.FATAL, "config.yaml", "Duplicate keys")

            with patch("app.config_validator._load_settings_safe", return_value=_make_settings()):
                log_validation_report(report)

            log_file = next(tmp_path.glob("redgw.*.log"))
            content = log_file.read_text()
            assert "[FATAL]" in content
            assert "1 FATAL" in content
        finally:
            reset_file_logger()


# ── validate_all_configs 통합 테스트 ──────────────────────────


class TestValidateAllConfigs:
    def test_basic_integration(self):
        """기본 통합 테스트 — config.yaml 로드 성공."""
        settings = _make_settings()
        with patch("app.config_validator._load_settings_safe", return_value=settings):
            report = validate_all_configs()
        assert isinstance(report, ValidationReport)

    def test_settings_load_failure(self):
        """config.yaml 로드 실패 시 ERROR 추가."""
        with patch("app.config_validator._load_settings_safe", return_value=None):
            report = validate_all_configs()
        errors = [i for i in report.issues if i.severity == Severity.ERROR]
        assert any("Failed to load" in e.message for e in errors)


# ─────────────────────────────────────────────────────────────
# AuditConfig — PR-1 신규
# ─────────────────────────────────────────────────────────────
class TestAuditConfig:
    """AuditConfig Pydantic 모델 검증 (Literal[0,1,2] 제약 + env 오버라이드)."""

    def test_audit_level_valid(self):
        """level=0/1/2 모두 허용."""
        from app.config import AuditConfig

        for lv in (0, 1, 2):
            cfg = AuditConfig(level=lv)
            assert cfg.level == lv

    def test_audit_level_invalid(self):
        """level=-1, 3, "1" 등은 Pydantic ValidationError."""
        from app.config import AuditConfig
        from pydantic import ValidationError

        for bad in (-1, 3, 99, "1", "high"):
            with pytest.raises(ValidationError):
                AuditConfig(level=bad)

    def test_audit_defaults(self):
        """미지정 시 디폴트값 검증."""
        from app.config import AuditConfig

        cfg = AuditConfig()
        assert cfg.level == 1
        assert cfg.directory == "/app/audit"
        assert cfg.max_file_size_mb == 10
        assert cfg.retention_days == 30
        assert cfg.max_total_size_mb == 1024
        assert cfg.disk_min_free_pct == 10
        assert cfg.payload_prefix_bytes == 0
        assert cfg.exclude_paths == ["/health", "/metrics", "/api/v1/metrics", "/"]
        assert cfg.per_worker_files is False

    def test_audit_env_override_level(self, monkeypatch):
        """REDGW_AUDIT_LEVEL 환경변수 오버라이드."""
        from app.config import _apply_env_overrides, Settings

        monkeypatch.setenv("REDGW_AUDIT_LEVEL", "2")
        settings = _apply_env_overrides(Settings())
        assert settings.audit.level == 2

    def test_audit_env_override_level_invalid_fatal(self, monkeypatch):
        """잘못된 REDGW_AUDIT_LEVEL은 SystemExit."""
        from app.config import _apply_env_overrides, Settings

        monkeypatch.setenv("REDGW_AUDIT_LEVEL", "5")
        with pytest.raises(SystemExit):
            _apply_env_overrides(Settings())

        monkeypatch.setenv("REDGW_AUDIT_LEVEL", "abc")
        with pytest.raises(SystemExit):
            _apply_env_overrides(Settings())

    def test_audit_env_override_directory(self, monkeypatch):
        """REDGW_AUDIT_DIRECTORY 환경변수 오버라이드."""
        from app.config import _apply_env_overrides, Settings

        monkeypatch.setenv("REDGW_AUDIT_DIRECTORY", "/var/audit-custom")
        settings = _apply_env_overrides(Settings())
        assert settings.audit.directory == "/var/audit-custom"

    def test_audit_env_override_max_total_mb(self, monkeypatch):
        """REDGW_AUDIT_MAX_TOTAL_MB 환경변수 오버라이드."""
        from app.config import _apply_env_overrides, Settings

        monkeypatch.setenv("REDGW_AUDIT_MAX_TOTAL_MB", "2048")
        settings = _apply_env_overrides(Settings())
        assert settings.audit.max_total_size_mb == 2048

    def test_audit_env_override_retention_days(self, monkeypatch):
        """REDGW_AUDIT_RETENTION_DAYS 환경변수 오버라이드."""
        from app.config import _apply_env_overrides, Settings

        monkeypatch.setenv("REDGW_AUDIT_RETENTION_DAYS", "90")
        settings = _apply_env_overrides(Settings())
        assert settings.audit.retention_days == 90
