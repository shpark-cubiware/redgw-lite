"""네임스페이스 접근 권한 검사 모듈"""

from __future__ import annotations

from app.schemas.common import ClientInfo
from app.utils.response import error

# fire-and-forget task에 대한 강한 참조 유지 — 미보관 시 실행 중 task가 GC되어
# 메트릭 INCR가 유실될 수 있다(asyncio 공식 문서: 루프는 task 강참조를 유지하지 않음).
_background_tasks: set = set()


def _fire_metric(reason: str) -> None:
    """비동기 메트릭 호출을 fire-and-forget. 동기 함수에서 호출 가능.

    get_event_loop()는 Python 3.12+ 코루틴 외부에서 deprecation 경고.
    get_running_loop()는 실행 중 루프만 명시적으로 반환 → 코루틴 안에서만
    동작. 권한 거부는 async 라우터 핸들러 안에서 발생하므로 안전.
    """
    try:
        import asyncio
        from app.utils.metrics import record_auth_failure_async
        loop = asyncio.get_running_loop()
        task = loop.create_task(record_auth_failure_async(reason))
        # 완료 전 GC 방지 — set에 보관하고 완료 시 자동 제거.
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        # 실행 중 루프 없음 — 호출 컨텍스트 안전망, 무시
        pass
    except Exception:
        pass


def require_admin(client: ClientInfo) -> None:
    """admin 클라이언트가 아니면 403 예외 발생."""
    if client.client_id != "admin":
        try:
            from app.audit import set_pending_event
            set_pending_event(event="admin_required", error_code="ADMIN_REQUIRED")
        except Exception:
            pass
        _fire_metric("admin_required")
        raise error("ADMIN_REQUIRED", "Admin privileges required", status=403)


class NamespaceGuard:
    """네임스페이스 접근 권한 검사"""

    @staticmethod
    def check_access(client: ClientInfo, ns: str, operation: str) -> bool:
        """
        클라이언트의 네임스페이스 접근 권한 확인

        Args:
            client: 인증된 클라이언트 정보
            ns: 접근하려는 네임스페이스
            operation: "read" 또는 "write"

        Returns:
            True if access is allowed
        """
        # 와일드카드 권한 확인
        if "*" in client.namespaces:
            if operation in client.namespaces["*"]:
                return True

        # 특정 네임스페이스 권한 확인
        if ns in client.namespaces:
            if operation in client.namespaces[ns]:
                return True

        return False

    @staticmethod
    def require_access(client: ClientInfo, ns: str, operation: str) -> None:
        """권한이 없으면 403 예외 발생"""
        if not NamespaceGuard.check_access(client, ns, operation):
            try:
                from app.audit import set_pending_event
                set_pending_event(event="namespace_denied", error_code="NAMESPACE_DENIED")
            except Exception:
                pass
            _fire_metric("namespace_denied")
            raise error(
                "NAMESPACE_DENIED",
                f"Access to namespace '{ns}' with '{operation}' permission is not allowed for client '{client.client_id}'",
                status=403,
            )


# 편의 함수
def require_read(client: ClientInfo, ns: str) -> None:
    NamespaceGuard.require_access(client, ns, "read")


def require_write(client: ClientInfo, ns: str) -> None:
    NamespaceGuard.require_access(client, ns, "write")
