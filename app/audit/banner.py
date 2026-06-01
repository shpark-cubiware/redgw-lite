"""Audit 시작 배너 — lifespan startup에서 호출.

배너 텍스트 생성 로직을 별도 함수로 추출해 단위 테스트 가능하게 한다.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.config import AuditConfig


def format_audit_banner(config: AuditConfig) -> list[str]:
    """레벨별 배너 텍스트를 줄 단위 리스트로 반환.

    Level 0: WARNING 3줄
    Level 1·2: Level/Directory/Disk/Usage/Limits/Policy 6줄 + 옵션 LOW DISK
    """
    if config.level == 0:
        return [
            "*** WARNING: AUDIT_LEVEL=0 - audit logging FULLY DISABLED ***",
            "*** No write/delete/auth events will be persisted ***",
            "*** Not recommended for production ***",
        ]

    level_name = {1: "BASIC", 2: "FULL"}.get(config.level, "?")
    lines: list[str] = [
        f"[AUDIT] Level         : {config.level} ({level_name})",
        f"[AUDIT] Directory     : {config.directory}",
    ]

    target = Path(config.directory)
    free_pct = 100.0
    if target.exists():
        try:
            total, used, free = shutil.disk_usage(str(target))
            free_pct = round(free / total * 100, 1) if total > 0 else 0.0
            lines.append(
                f"[AUDIT] Disk capacity : total={total/(1024**3):.1f} GB  "
                f"used={used/(1024**3):.1f} GB  free={free/(1024**3):.1f} GB "
                f"({free_pct}% free)"
            )
        except OSError as e:
            lines.append(f"[AUDIT] Disk capacity : (unavailable: {e})")

        bytes_total = 0
        file_count = 0
        try:
            for f in target.iterdir():
                if f.is_file() and f.name.startswith("audit."):
                    try:
                        bytes_total += f.stat().st_size
                        file_count += 1
                    except OSError:
                        continue
        except OSError:
            pass
        lines.append(
            f"[AUDIT] Current usage : {bytes_total/(1024**2):.1f} MB "
            f"across {file_count} files"
        )
    else:
        lines.append("[AUDIT] Disk capacity : (directory not yet created)")
        lines.append("[AUDIT] Current usage : 0.0 MB across 0 files")

    lines.append(
        f"[AUDIT] Limits        : max_total={config.max_total_size_mb} MB  "
        f"retention={config.retention_days} days  "
        f"rotation={config.max_file_size_mb} MB/file"
    )

    payload_policy = (
        "hash-only" if config.payload_prefix_bytes == 0
        else f"hash+prefix({config.payload_prefix_bytes}B)"
    )
    exclude = ",".join(config.exclude_paths)
    lines.append(
        f"[AUDIT] Policy        : payload={payload_policy}  exclude={exclude}  "
        f"per_worker={str(config.per_worker_files).lower()}  "
        f"correlation={str(config.correlation).lower()}"
    )

    if free_pct < config.disk_min_free_pct:
        lines.append(
            f"[AUDIT] *** LOW DISK: free {free_pct}% < threshold "
            f"{config.disk_min_free_pct}% ***"
        )

    return lines
