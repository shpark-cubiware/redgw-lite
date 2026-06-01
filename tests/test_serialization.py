"""
=============================================================================
응답 직렬화 / 셰이프 계약 테스트 (test_serialization.py)
=============================================================================

Phase 1 (응답 직렬화) + Phase 2 (이중 직렬화 제거) 검증.

응답 직렬화는 FastAPI 내장 경로를 사용한다(0.131+ Pydantic 코어 Rust 직렬화,
라우터 `-> dict` 반환 타입에 적용). 커스텀 응답클래스(ORJSONResponse)는 deprecated이라 미사용.

목적:
  - FastAPI 내장 직렬화로 기존 응답과 키/값/타입이 동등한가
  - ttl(int), None, 한글(UTF-8), 중첩 dict가 올바르게 직렬화되는가
  - app.utils.response 헬퍼(ok/ok_simple/error/not_found)의 dict 셰이프가
    기존 Pydantic 스키마(ApiResponse/KeyMeta/ErrorResponse)와 일치하는가

DB 격리·픽스처는 conftest.py 참조.
=============================================================================
"""

from httpx import AsyncClient

from tests.conftest import HRM_KEY


class TestResponseSerialization:
    """FastAPI 내장 직렬화 동등성 (Phase 1)."""

    async def test_content_type_is_json(self, client: AsyncClient):
        """응답이 application/json Content-Type을 유지한다."""
        resp = await client.put(
            "/api/v1/ns/HRM/kv/ser_ct",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "v"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")

    async def test_korean_value_roundtrip(self, client: AsyncClient):
        """한글(멀티바이트 UTF-8) 값이 깨지지 않고 왕복한다 (ensure_ascii 무관)."""
        val = "한글값-가나다 ABC 123"
        resp = await client.put(
            "/api/v1/ns/HRM/kv/ser_kr",
            headers={"X-API-Key": HRM_KEY},
            json={"value": val},
        )
        assert resp.status_code == 200
        # 원시 바이트에 U+FFFD(치환문자)나 깨진 시퀀스가 없어야 한다
        assert "\ufffd" not in resp.text  # U+FFFD 치환문자 escape
        resp = await client.get(
            "/api/v1/ns/HRM/kv/ser_kr",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["value"] == val

    async def test_ttl_is_integer(self, client: AsyncClient):
        """meta.ttl은 정수형으로 직렬화된다 (orjson int 직렬화).

        ttl 미지정 시 기본 TTL(86400)이 적용되므로 양수, 또는 무TTL 키는 -1.
        핵심은 bool이 아닌 int로 직렬화되는지다 (FastAPI 내장 int 직렬화).
        """
        await client.put(
            "/api/v1/ns/HRM/kv/ser_ttl",
            headers={"X-API-Key": HRM_KEY},
            json={"value": "v"},  # ttl 미지정 → 기본 TTL 적용
        )
        resp = await client.get(
            "/api/v1/ns/HRM/kv/ser_ttl",
            headers={"X-API-Key": HRM_KEY},
        )
        ttl = resp.json()["meta"]["ttl"]
        assert isinstance(ttl, int) and not isinstance(ttl, bool)
        assert ttl == -1 or ttl > 0

    async def test_nested_dict_payload(self, client: AsyncClient):
        """중첩 dict(map 전체 조회)가 올바르게 직렬화된다."""
        await client.put(
            "/api/v1/ns/HRM/map/ser_map",
            headers={"X-API-Key": HRM_KEY},
            json={"fields": {"a": "1", "b": "한글"}},
        )
        resp = await client.get(
            "/api/v1/ns/HRM/map/ser_map",
            headers={"X-API-Key": HRM_KEY},
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["data"]["fields"] == {"a": "1", "b": "한글"}


class TestResponseShapeContract:
    """app.utils.response 헬퍼의 dict 셰이프 계약 (Phase 2: Pydantic 제거 후에도 불변)."""

    def test_ok_shape(self):
        from app.utils.response import ok

        r = ok({"value": "x"}, ns="HRM", key="k", type="string", ttl=60)
        assert r == {
            "ok": True,
            "data": {"value": "x"},
            "meta": {"ns": "HRM", "key": "k", "type": "string", "ttl": 60},
        }

    def test_ok_default_ttl(self):
        from app.utils.response import ok

        r = ok({"v": 1}, ns="HRM", key="k", type="string")
        assert r["meta"]["ttl"] == -1

    def test_ok_simple_shape(self):
        from app.utils.response import ok_simple

        r = ok_simple({"count": 3})
        # 메타 없는 단순 성공 응답: 기존 model_dump()와 동일하게 meta=None 포함
        assert r == {"ok": True, "data": {"count": 3}, "meta": None}

    def test_error_shape(self):
        from app.utils.response import error

        exc = error("INVALID_KEY", "bad key", status=400)
        # HTTPException.detail이 ErrorResponse 셰이프를 유지한다
        assert exc.status_code == 400
        assert exc.detail == {
            "ok": False,
            "error": {"code": "INVALID_KEY", "message": "bad key"},
        }

    def test_not_found_shape(self):
        from app.utils.response import not_found

        exc = not_found("mykey", "HRM")
        assert exc.status_code == 404
        assert exc.detail["ok"] is False
        assert exc.detail["error"]["code"] == "KEY_NOT_FOUND"


class TestErrorResponseSerialization:
    """에러 응답이 올바른 셰이프로 직렬화된다 (Phase 1 + 2)."""

    async def test_404_shape(self, client: AsyncClient):
        resp = await client.get(
            "/api/v1/ns/HRM/kv/does_not_exist_xyz",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 404
        # FastAPI 기본 HTTPException 핸들러가 detail을 {"detail": ...}로 감싼다.
        # error() 헬퍼의 detail dict 셰이프({"ok":False,"error":{...}})는 그 안에 유지된다.
        detail = resp.json()["detail"]
        assert detail["ok"] is False
        assert detail["error"]["code"] == "KEY_NOT_FOUND"
        assert isinstance(detail["error"]["message"], str)
