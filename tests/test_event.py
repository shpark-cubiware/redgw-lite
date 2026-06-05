"""
=============================================================================
Stream (Event) API 테스트 (test_event.py)
=============================================================================

Redis Stream 타입을 사용하는 Event API 전체 기능을 테스트합니다.

Redis 키 형식: {ns}:evt:{key}
엔드포인트:
  POST /ns/{ns}/event/{key}                      — 이벤트 발행 (XADD)
  GET  /ns/{ns}/event/{key}                      — 이벤트 읽기 (XRANGE)
  POST /ns/{ns}/event/{key}/group                — Consumer Group 생성 (XGROUP CREATE)
  GET  /ns/{ns}/event/{key}/group/{group}/read   — 그룹 소비 (XREADGROUP)
  POST /ns/{ns}/event/{key}/group/{group}/ack    — 처리 확인 (XACK)
  GET  /ns/{ns}/event/{key}/info                 — 스트림 정보 (XINFO)

Stream 특성:
  - 메시지 영속성: Pub/Sub와 달리 메시지가 저장됨
  - Consumer Group: 여러 워커가 메시지를 분산 소비
  - ACK 메커니즘: 처리 완료를 확인하여 유실 방지
  - 메시지 ID: 자동 생성 (timestamp-sequence)

핵심 규칙:
  - Consumer Group 생성/ACK은 **read 권한**으로 처리 (구독 행위)
  - 이벤트 발행(XADD)만 write 권한 필요

테스트 시나리오:
  - 이벤트 발행 및 읽기
  - Consumer Group 전체 흐름 (생성 → 소비 → ACK)
  - 스트림 정보 조회 (XINFO)
  - 중복 Consumer Group 생성 시 409
  - 교차 네임스페이스 이벤트 구독
  - 여러 Consumer 분산 소비
  - 실전 시나리오: 주문 등록 이벤트, 인력 배정 완료 이벤트
=============================================================================
"""

from httpx import AsyncClient

from tests.conftest import HRM_KEY, ERP_KEY, CRM_KEY, MONITOR_KEY


class TestEventCrud:
    """Stream 타입 기본 테스트"""

    async def test_publish_and_read(self, client: AsyncClient):
        """
        이벤트 발행(XADD) 및 읽기(XRANGE).

        XADD: 스트림에 메시지를 추가하고 자동 생성된 ID를 반환합니다.
        XRANGE: last_id 이후의 메시지를 시간순으로 읽습니다.
        """
        # 이벤트 발행
        resp = await client.post(
            "/api/v1/ns/ERP/event/order-registered",
            headers={"X-API-Key": ERP_KEY},
            json={"data": {"order_id": "2024-001", "type": "일반주문", "region": "서울강남"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["id"] is not None  # 자동 생성 ID (예: "1706000000000-0")
        assert data["meta"]["type"] == "stream"

        # 이벤트 읽기 (last_id=0: 처음부터 읽기)
        resp = await client.get(
            "/api/v1/ns/ERP/event/order-registered?last_id=0&count=10",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        events = resp.json()["data"]["events"]
        assert len(events) == 1
        assert events[0]["data"]["order_id"] == "2024-001"

    async def test_publish_multiple_and_read(self, client: AsyncClient):
        """
        복수 이벤트 발행 후 순서대로 읽기.

        Stream은 발행 순서를 보장합니다 (메시지 ID가 시간순).
        """
        events_data = [
            {"order_id": "2024-001", "type": "일반주문"},
            {"order_id": "2024-002", "type": "긴급주문"},
            {"order_id": "2024-003", "type": "대량주문"},
        ]
        for ev in events_data:
            await client.post(
                "/api/v1/ns/ERP/event/order-events",
                headers={"X-API-Key": ERP_KEY},
                json={"data": ev},
            )

        # 전체 읽기
        resp = await client.get(
            "/api/v1/ns/ERP/event/order-events?last_id=0&count=100",
            headers={"X-API-Key": ERP_KEY},
        )
        events = resp.json()["data"]["events"]
        assert len(events) == 3
        # 발행 순서 보장
        assert events[0]["data"]["order_id"] == "2024-001"
        assert events[2]["data"]["order_id"] == "2024-003"

    async def test_read_with_count_limit(self, client: AsyncClient):
        """count 파라미터로 읽기 건수 제한"""
        for i in range(5):
            await client.post(
                "/api/v1/ns/ERP/event/many-events",
                headers={"X-API-Key": ERP_KEY},
                json={"data": {"seq": str(i)}},
            )

        resp = await client.get(
            "/api/v1/ns/ERP/event/many-events?last_id=0&count=2",
            headers={"X-API-Key": ERP_KEY},
        )
        events = resp.json()["data"]["events"]
        assert len(events) == 2

    async def test_read_count_negative_rejected(self, client: AsyncClient):
        """count 음수/0 → 422 (Redis DataError로 인한 500 대신 입력단 거부)"""
        resp = await client.get(
            "/api/v1/ns/ERP/event/many-events?last_id=0&count=-1",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 422

    async def test_read_group_count_negative_rejected(self, client: AsyncClient):
        """Consumer Group 읽기 count 음수 → 422"""
        await client.post(
            "/api/v1/ns/ERP/event/cg-count-evt",
            headers={"X-API-Key": ERP_KEY},
            json={"data": {"a": "1"}},
        )
        await client.post(
            "/api/v1/ns/ERP/event/cg-count-evt/group",
            headers={"X-API-Key": ERP_KEY},
            json={"group": "g1"},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/event/cg-count-evt/group/g1/read?consumer=w1&count=0",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 422


class TestConsumerGroup:
    """Consumer Group 전체 흐름 테스트"""

    async def test_consumer_group_full_flow(self, client: AsyncClient):
        """
        Consumer Group 전체 흐름: 생성 → 발행 → 소비 → ACK.

        1. XGROUP CREATE: 그룹 생성 (read 권한)
        2. XADD: 이벤트 발행 (write 권한)
        3. XREADGROUP: 그룹 내 consumer가 메시지 소비 (read 권한)
        4. XACK: 처리 완료 확인 (read 권한)

        ACK하지 않은 메시지는 pending 상태로 남아
        다른 consumer에게 재배정할 수 있습니다.
        """
        # 1. Consumer Group 생성 (CRM가 ERP 이벤트 구독)
        resp = await client.post(
            "/api/v1/ns/ERP/event/order-registered/group",
            headers={"X-API-Key": CRM_KEY},
            json={"group": "crm-analysis-group"},
        )
        assert resp.status_code == 200

        # 2. 이벤트 발행
        await client.post(
            "/api/v1/ns/ERP/event/order-registered",
            headers={"X-API-Key": ERP_KEY},
            json={"data": {"order_id": "2024-001", "type": "일반주문"}},
        )
        await client.post(
            "/api/v1/ns/ERP/event/order-registered",
            headers={"X-API-Key": ERP_KEY},
            json={"data": {"order_id": "2024-002", "type": "긴급주문"}},
        )

        # 3. CRM worker가 이벤트 소비
        resp = await client.get(
            "/api/v1/ns/ERP/event/order-registered/group/crm-analysis-group/read?consumer=worker-1&count=5",
            headers={"X-API-Key": CRM_KEY},
        )
        assert resp.status_code == 200
        events = resp.json()["data"]["events"]
        assert len(events) == 2

        # 4. ACK 처리 — 처리 완료된 이벤트 확인
        event_ids = [e["id"] for e in events]
        resp = await client.post(
            "/api/v1/ns/ERP/event/order-registered/group/crm-analysis-group/ack",
            headers={"X-API-Key": CRM_KEY},
            json={"ids": event_ids},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["acked"] == 2

    async def test_consumer_group_read_permission(self, client: AsyncClient):
        """
        Consumer Group 생성과 ACK은 read 권한으로 동작.

        그룹 생성은 '구독 행위'이므로 write가 아닌 read 권한으로 처리합니다.
        CRM→ERP는 read 허용이므로 Group 생성 가능.
        """
        # CRM → ERP read 허용 → Group 생성 OK
        resp = await client.post(
            "/api/v1/ns/ERP/event/permission-test/group",
            headers={"X-API-Key": CRM_KEY},
            json={"group": "crm-reader"},
        )
        assert resp.status_code == 200  # 403이 아님!

    async def test_duplicate_group_error(self, client: AsyncClient):
        """
        중복 Consumer Group 생성 시 409 Conflict.

        같은 이름의 그룹이 이미 존재하면 에러를 반환합니다.
        """
        await client.post(
            "/api/v1/ns/ERP/event/test-stream/group",
            headers={"X-API-Key": ERP_KEY},
            json={"group": "dup-group"},
        )
        resp = await client.post(
            "/api/v1/ns/ERP/event/test-stream/group",
            headers={"X-API-Key": ERP_KEY},
            json={"group": "dup-group"},
        )
        assert resp.status_code == 409

    async def test_read_no_new_messages(self, client: AsyncClient):
        """
        새 메시지 없을 때 Consumer Group read → 빈 리스트.

        XREADGROUP에서 새 메시지가 없으면 빈 응답을 반환합니다.
        """
        # 빈 스트림에 Group 생성
        await client.post(
            "/api/v1/ns/ERP/event/empty-stream/group",
            headers={"X-API-Key": ERP_KEY},
            json={"group": "empty-reader"},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/event/empty-stream/group/empty-reader/read?consumer=w1&count=5",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["events"] == []


class TestEventInfo:
    """스트림 정보 조회 테스트"""

    async def test_stream_info(self, client: AsyncClient):
        """
        XINFO STREAM — 스트림 메타데이터 조회.

        length, groups 수, 첫/마지막 메시지 ID 등을 반환합니다.
        """
        await client.post(
            "/api/v1/ns/ERP/event/info-test/group",
            headers={"X-API-Key": ERP_KEY},
            json={"group": "test-group"},
        )
        await client.post(
            "/api/v1/ns/ERP/event/info-test",
            headers={"X-API-Key": ERP_KEY},
            json={"data": {"order_id": "2024-001"}},
        )

        resp = await client.get(
            "/api/v1/ns/ERP/event/info-test/info",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["length"] == 1
        assert len(data["groups"]) >= 1


class TestEventScenarios:
    """실전 시나리오 테스트"""

    async def test_hrm_assign_complete_event(self, client: AsyncClient):
        """
        HRM 배정 완료 이벤트 → ERP 수신 (교차 네임스페이스).

        HRM가 인력 배정을 완료하면 이벤트를 발행하고,
        ERP가 교차 네임스페이스 read로 결과를 수신합니다.
        """
        # HRM가 배정 완료 이벤트 발행
        await client.post(
            "/api/v1/ns/HRM/event/assign-complete",
            headers={"X-API-Key": HRM_KEY},
            json={"data": {"req_id": "req-001", "score": "95", "staff_id": "kim-001"}},
        )

        # ERP가 HRM 이벤트 읽기 (ERP→HRM read 허용)
        resp = await client.get(
            "/api/v1/ns/HRM/event/assign-complete?last_id=0&count=10",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        events = resp.json()["data"]["events"]
        assert len(events) == 1
        assert events[0]["data"]["req_id"] == "req-001"

    async def test_order_lifecycle_events(self, client: AsyncClient):
        """
        주문 라이프사이클 이벤트 — 상태 변경 이력을 Stream으로 추적.

        Stream은 이벤트 소싱 패턴에 적합: 상태 변경을 순서대로 기록하고
        언제든 재생(replay)할 수 있습니다.
        """
        lifecycle = [
            {"event": "registered", "order_id": "2024-001", "by": "kim"},
            {"event": "assigned", "order_id": "2024-001", "to": "lee"},
            {"event": "item_added", "order_id": "2024-001", "type": "인력"},
            {"event": "closed", "order_id": "2024-001", "result": "solved"},
        ]
        for ev in lifecycle:
            await client.post(
                "/api/v1/ns/ERP/event/order:2024-001:lifecycle",
                headers={"X-API-Key": ERP_KEY},
                json={"data": ev},
            )

        # 전체 이력 조회
        resp = await client.get(
            "/api/v1/ns/ERP/event/order:2024-001:lifecycle?last_id=0&count=100",
            headers={"X-API-Key": ERP_KEY},
        )
        events = resp.json()["data"]["events"]
        assert len(events) == 4
        assert events[0]["data"]["event"] == "registered"
        assert events[3]["data"]["event"] == "closed"

    async def test_multi_system_event_subscription(self, client: AsyncClient):
        """
        다중 시스템 이벤트 구독 — ERP 이벤트를 CRM와 HRM가 각각 구독.

        각 시스템이 독립적인 Consumer Group으로 이벤트를 소비하므로,
        한 시스템의 소비가 다른 시스템에 영향을 주지 않습니다.
        """
        # CRM, ERP 각각 Consumer Group 생성
        for group, api_key in [("crm-group", CRM_KEY), ("erp-internal", ERP_KEY)]:
            await client.post(
                "/api/v1/ns/ERP/event/order-updates/group",
                headers={"X-API-Key": api_key},
                json={"group": group},
            )

        # 이벤트 발행
        await client.post(
            "/api/v1/ns/ERP/event/order-updates",
            headers={"X-API-Key": ERP_KEY},
            json={"data": {"order_id": "2024-001", "status": "처리중"}},
        )

        # CRM 그룹에서 읽기
        resp = await client.get(
            "/api/v1/ns/ERP/event/order-updates/group/crm-group/read?consumer=crm-w1&count=5",
            headers={"X-API-Key": CRM_KEY},
        )
        assert len(resp.json()["data"]["events"]) == 1

        # ERP 내부 그룹에서도 독립적으로 읽기
        resp = await client.get(
            "/api/v1/ns/ERP/event/order-updates/group/erp-internal/read?consumer=erp-w1&count=5",
            headers={"X-API-Key": ERP_KEY},
        )
        assert len(resp.json()["data"]["events"]) == 1


class TestEventBatchPublish:
    """이벤트 배치 발행 테스트"""

    async def test_batch_publish(self, client: AsyncClient):
        """
        여러 이벤트를 Pipeline XADD로 일괄 발행.

        단건 XADD를 여러 번 호출하는 대신 Pipeline으로 묶어 네트워크 왕복을 줄입니다.
        반환된 ids 개수가 요청한 events 수와 일치해야 합니다.
        """
        resp = await client.post(
            "/api/v1/ns/ERP/event/batch-stream/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"events": [
                {"order_id": "2024-001", "type": "일반주문"},
                {"order_id": "2024-002", "type": "긴급주문"},
                {"order_id": "2024-003", "type": "대량주문"},
            ]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["count"] == 3
        assert len(data["data"]["ids"]) == 3
        assert data["meta"]["type"] == "stream"

        # 발행된 이벤트가 실제로 읽히는지 확인
        resp = await client.get(
            "/api/v1/ns/ERP/event/batch-stream?last_id=0&count=100",
            headers={"X-API-Key": ERP_KEY},
        )
        events = resp.json()["data"]["events"]
        assert len(events) == 3
        assert events[0]["data"]["order_id"] == "2024-001"
        assert events[2]["data"]["order_id"] == "2024-003"

    async def test_batch_publish_empty_events(self, client: AsyncClient):
        """빈 events 배열 → 422 Validation Error."""
        resp = await client.post(
            "/api/v1/ns/ERP/event/batch-stream/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"events": []},
        )
        assert resp.status_code == 422

    async def test_batch_publish_empty_event_dict(self, client: AsyncClient):
        """events에 빈 dict 원소 → 422 (XADD {} 500 방지)."""
        resp = await client.post(
            "/api/v1/ns/ERP/event/batch-stream/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"events": [{"order_id": "1"}, {}]},
        )
        assert resp.status_code == 422

    async def test_batch_publish_write_permission_required(self, client: AsyncClient):
        """
        배치 발행은 write 권한 필요.

        CRM→ERP는 read만 허용이므로 403이어야 합니다.
        """
        resp = await client.post(
            "/api/v1/ns/ERP/event/batch-stream/batch",
            headers={"X-API-Key": CRM_KEY},
            json={"events": [{"key": "value"}]},
        )
        assert resp.status_code == 403


class TestEventValueSizeValidation:
    """이벤트 데이터 크기 검증 테스트"""

    async def test_event_data_too_large(self, client: AsyncClient):
        """
        이벤트 데이터 필드 값이 max_value_size 초과 시 400 오류.
        """
        large_value = "x" * (1048576 + 1)  # 1MB + 1byte
        resp = await client.post(
            "/api/v1/ns/ERP/event/size-test",
            headers={"X-API-Key": ERP_KEY},
            json={"data": {"payload": large_value}},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"


class TestEventTouch:
    """TTL 갱신 (Touch) 테스트"""

    async def test_touch_update_ttl(self, client: AsyncClient):
        """Stream TTL 갱신"""
        await client.post(
            "/api/v1/ns/ERP/event/touch-stream",
            headers={"X-API-Key": ERP_KEY},
            json={"data": {"action": "test"}},
        )
        resp = await client.put(
            "/api/v1/ns/ERP/event/touch-stream/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 7200},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["touched"] is True
        assert resp.json()["meta"]["ttl"] > 0

    async def test_touch_persist(self, client: AsyncClient):
        """TTL 제거 (PERSIST)"""
        await client.post(
            "/api/v1/ns/ERP/event/persist-stream",
            headers={"X-API-Key": ERP_KEY},
            json={"data": {"action": "persist"}},
        )
        resp = await client.put(
            "/api/v1/ns/ERP/event/persist-stream/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] == -1

    async def test_touch_not_found(self, client: AsyncClient):
        """존재하지 않는 키 touch → 404"""
        resp = await client.put(
            "/api/v1/ns/ERP/event/nonexistent/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 3600},
        )
        assert resp.status_code == 404


class TestEventAckValidation:
    """XACK ids 형식 검증 회귀 — 잘못된 stream ID는 500이 아니라 400(다른 엔드포인트와 일관)."""

    async def test_ack_invalid_stream_id_returns_400(self, client: AsyncClient):
        # 검증은 build_key/xack 이전에 수행되므로 스트림·그룹이 없어도 400.
        resp = await client.post(
            "/api/v1/ns/ERP/event/ack-bad-id/group/g1/ack",
            headers={"X-API-Key": ERP_KEY},
            json={"ids": ["not-a-valid-id"]},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_STREAM_ID"

    async def test_ack_mixed_valid_invalid_returns_400(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/ns/ERP/event/ack-bad-id/group/g1/ack",
            headers={"X-API-Key": ERP_KEY},
            json={"ids": ["1700000000000-0", "garbage"]},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_STREAM_ID"


class TestEventDelete:
    """전체 키 삭제 (DEL) — 스트림 통째 삭제"""

    async def test_delete_whole_stream(self, client: AsyncClient):
        """스트림 발행 후 전체 삭제 → 200, deleted=true. 이후 읽기 count=0(키 자체 소멸)."""
        await client.post(
            "/api/v1/ns/HRM/event/del-target",
            headers={"X-API-Key": HRM_KEY},
            json={"data": {"k": "v"}},
        )
        resp = await client.delete(
            "/api/v1/ns/HRM/event/del-target",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["deleted"] is True
        assert body["meta"]["type"] == "stream"

        # 읽기로 키 소멸을 양적 확인 (queue len==0 / group·rank count==0과 대칭)
        resp = await client.get(
            "/api/v1/ns/HRM/event/del-target?last_id=0&count=10",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["count"] == 0

    async def test_delete_absent_stream_404(self, client: AsyncClient):
        """없는 키 삭제 → 404 KEY_NOT_FOUND."""
        resp = await client.delete(
            "/api/v1/ns/HRM/event/no-such-key",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"]["code"] == "KEY_NOT_FOUND"

    async def test_delete_requires_write(self, client: AsyncClient):
        """read-only 키(MONITOR)는 전체 삭제 거부 → 403 NAMESPACE_DENIED."""
        await client.post(
            "/api/v1/ns/HRM/event/ro-guard",
            headers={"X-API-Key": HRM_KEY},
            json={"data": {"k": "v"}},
        )
        resp = await client.delete(
            "/api/v1/ns/HRM/event/ro-guard",
            headers={"X-API-Key": MONITOR_KEY},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "NAMESPACE_DENIED"
