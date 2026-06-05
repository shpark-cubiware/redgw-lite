"""
=============================================================================
Sorted Set (Rank) API 테스트 (test_rank.py)
=============================================================================

Redis Sorted Set 타입을 사용하는 Rank API 전체 기능을 테스트합니다.

Redis 키 형식: {ns}:rank:{key}
엔드포인트:
  POST   /ns/{ns}/rank/{key}               — 멤버+스코어 추가 (ZADD)
  GET    /ns/{ns}/rank/{key}               — 범위 조회 (ZRANGE/ZREVRANGE)
  GET    /ns/{ns}/rank/{key}/score/{member} — 스코어 조회 (ZSCORE)
  GET    /ns/{ns}/rank/{key}/between        — 스코어 범위 조회 (ZRANGEBYSCORE)
  DELETE /ns/{ns}/rank/{key}/{member}       — 멤버 제거 (ZREM)
  POST   /ns/{ns}/rank/{key}/incr          — 스코어 증감 (ZINCRBY)
  GET    /ns/{ns}/rank/{key}/pop           — 최소/최대 추출 (ZPOPMIN/ZPOPMAX)

Sorted Set 특성:
  - 각 멤버에 실수(float) 스코어가 부여됨
  - 스코어 기준 자동 정렬
  - O(log N) 삽입/삭제, O(log N + M) 범위 조회
  - 동일 멤버의 스코어 업데이트 가능 (ZADD NX가 아닌 한)

테스트 시나리오:
  - 기본 CRUD (추가, 범위조회, 스코어조회, 삭제)
  - 범위 조회 (오름차순/내림차순, 스코어 범위)
  - 스코어 증감 (ZINCRBY)
  - 최소/최대 추출 (ZPOPMIN, ZPOPMAX)
  - 주문 우선순위 관리
  - 인력 배정 스코어 랭킹
  - 교차 네임스페이스 읽기
=============================================================================
"""

from httpx import AsyncClient

from tests.conftest import HRM_KEY, ERP_KEY, MONITOR_KEY


class TestRankCrud:
    """Sorted Set 타입 기본 CRUD 테스트"""

    async def test_add_and_range(self, client: AsyncClient):
        """
        주문 우선순위 등록 및 조회.

        ZADD로 멤버와 스코어를 추가하고 ZRANGE로 범위 조회합니다.
        reverse=true이면 스코어 높은 순 (ZREVRANGE).
        """
        for order, score in [("order-001", 10), ("order-002", 5), ("order-003", 8)]:
            await client.post(
                "/api/v1/ns/ERP/rank/priority:orders",
                headers={"X-API-Key": ERP_KEY},
                json={"member": order, "score": score},
            )

        # 스코어 높은 순 (reverse=true → ZREVRANGE)
        resp = await client.get(
            "/api/v1/ns/ERP/rank/priority:orders?start=0&stop=9&reverse=true",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["meta"]["type"] == "zset"
        members = data["data"]["members"]
        assert members[0]["member"] == "order-001"  # 스코어 10 (최고)
        assert members[0]["score"] == 10.0
        assert members[-1]["member"] == "order-002"  # 스코어 5 (최저)

    async def test_range_ascending(self, client: AsyncClient):
        """오름차순 범위 조회 (기본 ZRANGE)"""
        for order, score in [("order-A", 30), ("order-B", 10), ("order-C", 20)]:
            await client.post(
                "/api/v1/ns/ERP/rank/asc-test",
                headers={"X-API-Key": ERP_KEY},
                json={"member": order, "score": score},
            )

        resp = await client.get(
            "/api/v1/ns/ERP/rank/asc-test?start=0&stop=-1",
            headers={"X-API-Key": ERP_KEY},
        )
        members = resp.json()["data"]["members"]
        # 오름차순: B(10) → C(20) → A(30)
        assert members[0]["member"] == "order-B"
        assert members[2]["member"] == "order-A"

    async def test_score_query(self, client: AsyncClient):
        """멤버의 개별 스코어 조회 (ZSCORE)"""
        await client.post(
            "/api/v1/ns/ERP/rank/priority:orders",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "order-001", "score": 10},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/rank/priority:orders/score/order-001",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["score"] == 10.0

    async def test_score_not_found(self, client: AsyncClient):
        """존재하지 않는 멤버 스코어 조회 → 404"""
        # 빈 sorted set에서 조회
        await client.post(
            "/api/v1/ns/ERP/rank/empty-rank",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "exists", "score": 1},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/rank/empty-rank/score/nonexistent",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 404

    async def test_between_range(self, client: AsyncClient):
        """
        스코어 범위 조회 (ZRANGEBYSCORE).

        활용 예: 인력 배정 스코어 80점 이상인 후보만 조회.
        """
        for pid, score in [("EMP-001", 95), ("EMP-002", 72), ("EMP-003", 88), ("EMP-004", 60)]:
            await client.post(
                "/api/v1/ns/HRM/rank/assign-scores:req-001",
                headers={"X-API-Key": HRM_KEY},
                json={"member": pid, "score": score},
            )

        resp = await client.get(
            "/api/v1/ns/HRM/rank/assign-scores:req-001/between?min=80&max=100",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200
        members = resp.json()["data"]["members"]
        assert len(members) == 2  # EMP-001(95), EMP-003(88)
        scores = {m["member"]: m["score"] for m in members}
        assert scores["EMP-001"] == 95.0
        assert scores["EMP-003"] == 88.0

    async def test_remove_member(self, client: AsyncClient):
        """멤버 제거 (ZREM)"""
        await client.post(
            "/api/v1/ns/ERP/rank/priority:orders",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "order-to-remove", "score": 1},
        )
        resp = await client.delete(
            "/api/v1/ns/ERP/rank/priority:orders/order-to-remove",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200

    async def test_update_score(self, client: AsyncClient):
        """
        기존 멤버의 스코어 업데이트.

        ZADD는 기존 멤버가 있으면 스코어를 덮어씁니다.
        주문 우선순위 변경 시 유용합니다.
        """
        await client.post(
            "/api/v1/ns/ERP/rank/priority:update-test",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "order-001", "score": 5},
        )
        # 스코어 업데이트
        await client.post(
            "/api/v1/ns/ERP/rank/priority:update-test",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "order-001", "score": 99},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/rank/priority:update-test/score/order-001",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["score"] == 99.0


class TestRankIncrAndPop:
    """스코어 증감(ZINCRBY) 및 추출(ZPOPMIN/ZPOPMAX) 테스트"""

    async def test_incr_score(self, client: AsyncClient):
        """
        스코어 원자적 증감 (ZINCRBY).

        활용 예: 주문에 새로운 항목이 추가될 때마다 우선순위 +3.
        """
        await client.post(
            "/api/v1/ns/ERP/rank/priority:orders",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "order-001", "score": 5},
        )
        resp = await client.post(
            "/api/v1/ns/ERP/rank/priority:orders/incr",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "order-001", "delta": 3},
        )
        assert resp.json()["data"]["score"] == 8.0

    async def test_decr_score(self, client: AsyncClient):
        """음수 delta로 스코어 감소"""
        await client.post(
            "/api/v1/ns/ERP/rank/value:customers",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "customer-A", "score": 10},
        )
        resp = await client.post(
            "/api/v1/ns/ERP/rank/value:customers/incr",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "customer-A", "delta": -4},
        )
        assert resp.json()["data"]["score"] == 6.0

    async def test_pop_min(self, client: AsyncClient):
        """
        최소 스코어 추출 (ZPOPMIN).

        활용 예: 작업 스케줄링 — 타임스탬프가 가장 오래된(작은) 작업부터 처리.
        """
        await client.post(
            "/api/v1/ns/shared/rank/schedule:jobs",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "job-A", "score": 100},
        )
        await client.post(
            "/api/v1/ns/shared/rank/schedule:jobs",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "job-B", "score": 50},
        )

        # 최소 스코어 추출 → job-B(50)
        resp = await client.get(
            "/api/v1/ns/shared/rank/schedule:jobs/pop?direction=min",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["member"] == "job-B"
        assert resp.json()["data"]["score"] == 50.0

    async def test_pop_max(self, client: AsyncClient):
        """
        최대 스코어 추출 (ZPOPMAX).

        활용 예: 긴급도가 가장 높은 주문부터 배정.
        """
        await client.post(
            "/api/v1/ns/ERP/rank/urgent:orders",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "order-low", "score": 2},
        )
        await client.post(
            "/api/v1/ns/ERP/rank/urgent:orders",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "order-high", "score": 99},
        )

        # 최대 스코어 추출 → order-high(99)
        resp = await client.get(
            "/api/v1/ns/ERP/rank/urgent:orders/pop?direction=max",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["member"] == "order-high"
        assert resp.json()["data"]["score"] == 99.0

    async def test_pop_empty(self, client: AsyncClient):
        """빈 Sorted Set에서 pop → 404"""
        resp = await client.get(
            "/api/v1/ns/ERP/rank/empty-rank/pop?direction=min",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 404


class TestRankScenarios:
    """실전 시나리오 테스트"""

    async def test_order_priority_management(self, client: AsyncClient):
        """
        주문 우선순위 관리 전체 흐름.

        1. 여러 주문 등록 (각각 긴급도 스코어)
        2. 긴급도 높은 순으로 조회
        3. 새 항목 추가 → 스코어 증가
        4. 가장 긴급한 주문 추출 → 배정
        """
        orders = [("order-001", 5), ("order-002", 9), ("order-003", 3)]
        for order, score in orders:
            await client.post(
                "/api/v1/ns/ERP/rank/priority:active",
                headers={"X-API-Key": ERP_KEY},
                json={"member": order, "score": score},
            )

        # 긴급도 높은 순 조회
        resp = await client.get(
            "/api/v1/ns/ERP/rank/priority:active?start=0&stop=-1&reverse=true",
            headers={"X-API-Key": ERP_KEY},
        )
        members = resp.json()["data"]["members"]
        assert members[0]["member"] == "order-002"  # 스코어 9

        # order-003에 긴급 항목 추가 → +10
        await client.post(
            "/api/v1/ns/ERP/rank/priority:active/incr",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "order-003", "delta": 10},
        )

        # 이제 order-003(13)이 최고 긴급
        resp = await client.get(
            "/api/v1/ns/ERP/rank/priority:active/pop?direction=max",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["member"] == "order-003"
        assert resp.json()["data"]["score"] == 13.0

    async def test_staff_assign_ranking(self, client: AsyncClient):
        """
        인력 배정 결과 랭킹 — HRM에서 생성, ERP에서 조회.

        HRM가 배정 스코어를 Sorted Set에 저장하면,
        ERP가 교차 네임스페이스 read로 80점 이상 후보만 조회.
        """
        candidates = [
            ("EMP-001", 95), ("EMP-002", 72), ("EMP-003", 88),
            ("EMP-004", 60), ("EMP-005", 91),
        ]
        for pid, score in candidates:
            await client.post(
                "/api/v1/ns/HRM/rank/assign:req-2024-001",
                headers={"X-API-Key": HRM_KEY},
                json={"member": pid, "score": score},
            )

        # ERP가 HRM 결과에서 80점 이상 후보 조회
        resp = await client.get(
            "/api/v1/ns/HRM/rank/assign:req-2024-001/between?min=80&max=100",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 200
        members = resp.json()["data"]["members"]
        assert len(members) == 3  # EMP-001(95), EMP-003(88), EMP-005(91)

    async def test_job_scheduler(self, client: AsyncClient):
        """
        작업 스케줄러 — 타임스탬프 기반 우선순위.

        스코어로 실행 예정 시간(Unix timestamp)을 사용하면,
        ZPOPMIN으로 가장 오래된 작업부터 순차 처리 가능.
        """
        jobs = [
            ("report-gen", 1706000100),
            ("backup", 1706000050),
            ("cleanup", 1706000200),
        ]
        for job, ts in jobs:
            await client.post(
                "/api/v1/ns/shared/rank/scheduler:pending",
                headers={"X-API-Key": ERP_KEY},
                json={"member": job, "score": ts},
            )

        # 가장 이른 작업 추출 → backup
        resp = await client.get(
            "/api/v1/ns/shared/rank/scheduler:pending/pop?direction=min",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.json()["data"]["member"] == "backup"


class TestRankTtl:
    """Sorted Set TTL 설정 테스트"""

    async def test_add_with_ttl(self, client: AsyncClient):
        """
        멤버 추가 시 TTL 지정 → Redis EXPIRE 적용.

        TTL 파라미터를 전달하면 Sorted Set에 만료 시간이 설정됩니다.
        """
        resp = await client.post(
            "/api/v1/ns/ERP/rank/ttl-test",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "temp-item", "score": 100.0, "ttl": 300},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert 0 < data["meta"]["ttl"] <= 300

    async def test_incr_preserves_ttl(self, client: AsyncClient):
        """ZINCRBY는 기존 TTL을 보존 → incr 응답 meta.ttl이 보존된 TTL을 보고해야 한다
        (incr_kv와 대칭. 이전엔 ttl 미보고로 항상 -1이 누출됐다)."""
        await client.post(
            "/api/v1/ns/ERP/rank/incr-ttl",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "m1", "score": 10.0, "ttl": 300},
        )
        resp = await client.post(
            "/api/v1/ns/ERP/rank/incr-ttl/incr",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "m1", "delta": 5.0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["score"] == 15.0
        assert 0 < data["meta"]["ttl"] <= 300


class TestRankBatch:
    """Sorted Set 배치 테스트"""

    async def test_batch_add_single_key(self, client: AsyncClient):
        """
        단일 키에 여러 멤버+스코어 일괄 추가 (ZADD multi).

        한 번의 호출로 여러 멤버-스코어 쌍을 추가합니다.
        """
        resp = await client.post(
            "/api/v1/ns/ERP/rank/batch-scores/batch",
            headers={"X-API-Key": ERP_KEY},
            json={
                "members": [
                    {"member": "order-001", "score": 10},
                    {"member": "order-002", "score": 30},
                    {"member": "order-003", "score": 20},
                ],
                "ttl": 300,
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["added"] == 3
        assert data["total"] == 3
        assert resp.json()["meta"]["ttl"] > 0

        # 조회하여 확인
        resp = await client.get(
            "/api/v1/ns/ERP/rank/batch-scores?start=0&stop=-1&reverse=true",
            headers={"X-API-Key": ERP_KEY},
        )
        members = resp.json()["data"]["members"]
        assert len(members) == 3
        assert members[0]["member"] == "order-002"  # 가장 높은 점수

    async def test_batch_read_multi_key(self, client: AsyncClient):
        """
        여러 Sorted Set을 한 번에 조회.

        Pipeline ZRANGE로 여러 키의 상위 N개를 일괄 조회합니다.
        """
        # 데이터 준비
        for key, members in [("rank:a", [("x", 10), ("y", 20)]), ("rank:b", [("p", 5), ("q", 15)])]:
            for m, s in members:
                await client.post(
                    f"/api/v1/ns/ERP/rank/{key}",
                    headers={"X-API-Key": ERP_KEY},
                    json={"member": m, "score": s},
                )

        # 배치 조회
        resp = await client.post(
            "/api/v1/ns/ERP/rank/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"keys": ["rank:a", "rank:b", "rank:noexist"], "start": 0, "stop": -1},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["count"] == 2
        assert len(data["values"]["rank:a"]) == 2
        assert len(data["values"]["rank:b"]) == 2
        assert "rank:noexist" not in data["values"]

    async def test_batch_read_reverse(self, client: AsyncClient):
        """배치 조회 시 reverse=true → 높은 점수부터"""
        await client.post(
            "/api/v1/ns/ERP/rank/rev-test/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"members": [{"member": "low", "score": 1}, {"member": "high", "score": 99}]},
        )
        resp = await client.post(
            "/api/v1/ns/ERP/rank/batch",
            headers={"X-API-Key": ERP_KEY},
            json={"keys": ["rev-test"], "reverse": True, "start": 0, "stop": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["values"]["rev-test"][0]["member"] == "high"


class TestRankTouch:
    """TTL 갱신 (Touch) 테스트"""

    async def test_touch_update_ttl(self, client: AsyncClient):
        """Sorted Set TTL 갱신"""
        await client.post(
            "/api/v1/ns/ERP/rank/touch-zset",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "order-1", "score": 5},
        )
        resp = await client.put(
            "/api/v1/ns/ERP/rank/touch-zset/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 7200},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["touched"] is True
        assert resp.json()["meta"]["ttl"] > 0

    async def test_touch_persist(self, client: AsyncClient):
        """TTL 제거 (PERSIST)"""
        await client.post(
            "/api/v1/ns/ERP/rank/persist-zset",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "item", "score": 1},
        )
        resp = await client.put(
            "/api/v1/ns/ERP/rank/persist-zset/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] == -1

    async def test_touch_not_found(self, client: AsyncClient):
        """존재하지 않는 키 touch → 404"""
        resp = await client.put(
            "/api/v1/ns/ERP/rank/nonexistent/touch",
            headers={"X-API-Key": ERP_KEY},
            json={"ttl": 3600},
        )
        assert resp.status_code == 404


class TestRankValueSizeValidation:
    """멤버 크기 검증 테스트 — kv/map/queue와 동일한 1MB 가드"""

    async def test_add_rank_member_too_large(self, client: AsyncClient):
        """1MB 초과 멤버 → 400 VALUE_TOO_LARGE"""
        large_member = "x" * (1048576 + 1)
        resp = await client.post(
            "/api/v1/ns/HRM/rank/oversized-zset",
            headers={"X-API-Key": HRM_KEY},
            json={"member": large_member, "score": 1.0},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"

    async def test_batch_add_rank_member_too_large(self, client: AsyncClient):
        """배치 추가에서 1MB 초과 멤버 → 400 VALUE_TOO_LARGE"""
        large_member = "x" * (1048576 + 1)
        resp = await client.post(
            "/api/v1/ns/HRM/rank/oversized-zset/batch",
            headers={"X-API-Key": HRM_KEY},
            json={"members": [{"member": large_member, "score": 1.0}]},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"

    async def test_incr_rank_member_too_large(self, client: AsyncClient):
        """incr에서 1MB 초과 멤버 → 400 VALUE_TOO_LARGE"""
        large_member = "x" * (1048576 + 1)
        resp = await client.post(
            "/api/v1/ns/HRM/rank/oversized-zset/incr",
            headers={"X-API-Key": HRM_KEY},
            json={"member": large_member, "delta": 1.0},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "VALUE_TOO_LARGE"

    async def test_add_rank_member_at_limit_ok(self, client: AsyncClient):
        """1MB 이하 멤버는 정상 처리"""
        member = "x" * 1048576
        resp = await client.post(
            "/api/v1/ns/HRM/rank/at-limit-zset",
            headers={"X-API-Key": HRM_KEY},
            json={"member": member, "score": 1.0},
        )
        assert resp.status_code == 200


class TestRankNanScore:
    """NaN score 입력 → 500이 아닌 400 INVALID_VALUE (incr_rank와 대칭).

    httpx의 json=은 float('nan') 직렬화를 거부하므로, stdlib json이 수용하는
    bare NaN 리터럴을 raw content로 보낸다(FastAPI는 stdlib json.loads로 수용).
    """

    _JSON = {"X-API-Key": ERP_KEY, "Content-Type": "application/json"}

    async def test_add_rank_nan_score(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/ns/ERP/rank/nan-zset",
            headers=self._JSON,
            content='{"member":"m1","score":NaN}',
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_VALUE"

    async def test_batch_add_rank_nan_score(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/ns/ERP/rank/nan-zset/batch",
            headers=self._JSON,
            content='{"members":[{"member":"m1","score":1.0},{"member":"m2","score":NaN}]}',
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_VALUE"

    async def test_nan_score_does_not_touch_existing_ttl(self, client: AsyncClient):
        """회귀(LOW): nan 거부가 키 TTL을 바꾸지 않는다.

        미수정 시 같은 파이프라인의 PERSIST(ttl=0)/EXPIRE가 ZADD 실패와 무관하게 실행돼
        거부된 쓰기인데도 TTL이 제거/재설정됐다. Redis 접촉 전 사전검증으로 차단한다.
        """
        from tests.conftest import ADMIN_KEY
        # TTL 있는 rank 키 생성
        await client.post(
            "/api/v1/ns/ERP/rank/nan-ttl", headers=self._JSON,
            content='{"member":"m1","score":1.0,"ttl":200}',
        )
        # nan + ttl=0 요청 → 400. 미수정 시 PERSIST가 TTL을 제거(-1)했다.
        resp = await client.post(
            "/api/v1/ns/ERP/rank/nan-ttl", headers=self._JSON,
            content='{"member":"m2","score":NaN,"ttl":0}',
        )
        assert resp.status_code == 400
        # TTL이 여전히 양수(원래 200대)인지 확인 — PERSIST가 실행되지 않았다
        info = await client.get(
            "/api/v1/admin/info/ERP/zset/nan-ttl",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert info.status_code == 200
        assert info.json()["data"]["ttl"] > 0

    async def test_between_rank_nan_bound(self, client: AsyncClient):
        await client.post(
            "/api/v1/ns/ERP/rank/between-nan",
            headers={"X-API-Key": ERP_KEY},
            json={"member": "m1", "score": 5.0},
        )
        resp = await client.get(
            "/api/v1/ns/ERP/rank/between-nan/between?min=nan&max=100",
            headers={"X-API-Key": ERP_KEY},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_VALUE"

    # ── inf 점수 거부 — inf는 ZADD가 저장하나 JSON 응답에서 null로 직렬화돼 회수 불가 ──

    async def test_add_rank_inf_score(self, client: AsyncClient):
        """score=inf → 400(nan과 동일 정책). 미수정 시 Redis에 inf 저장 후 응답이 null로 누수."""
        resp = await client.post(
            "/api/v1/ns/ERP/rank/inf-zset",
            headers=self._JSON,
            content='{"member":"m1","score":Infinity}',
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_VALUE"

    async def test_batch_add_rank_inf_score(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/ns/ERP/rank/inf-zset/batch",
            headers=self._JSON,
            content='{"members":[{"member":"m1","score":1.0},{"member":"m2","score":-Infinity}]}',
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_VALUE"

    async def test_incr_rank_inf_delta(self, client: AsyncClient):
        """incr delta=inf → 400. +inf에 -inf delta는 NaN ResponseError(500) 유발."""
        resp = await client.post(
            "/api/v1/ns/ERP/rank/inf-zset/incr",
            headers=self._JSON,
            content='{"member":"m1","delta":Infinity}',
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_VALUE"


class TestRankBatchTtlZero:
    """rank 단일키 batch add ttl=0도 기존 TTL을 제거한다(R5 커버리지)."""

    async def test_batch_add_ttl_zero_clears_existing_ttl(self, client: AsyncClient):
        await client.post(
            "/api/v1/ns/HRM/rank/rbtz", headers={"X-API-Key": HRM_KEY},
            json={"member": "m1", "score": 1.0, "ttl": 120},
        )
        resp = await client.post(
            "/api/v1/ns/HRM/rank/rbtz/batch", headers={"X-API-Key": HRM_KEY},
            json={"members": [{"member": "m2", "score": 2.0}], "ttl": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["ttl"] == -1


class TestRankDelete:
    """전체 키 삭제 (DEL) — 멤버 단위 ZREM과 구분되는 통째 삭제"""

    async def test_delete_whole_rank(self, client: AsyncClient):
        """Sorted Set 생성 후 전체 삭제 → 200, deleted=true. 이후 전체 조회 count=0(키 자체 소멸)."""
        await client.post(
            "/api/v1/ns/HRM/rank/del-target",
            headers={"X-API-Key": HRM_KEY},
            json={"member": "m1", "score": 1.0},
        )
        resp = await client.delete(
            "/api/v1/ns/HRM/rank/del-target",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["deleted"] is True
        assert body["meta"]["type"] == "zset"

        # 전체 조회로 키 소멸을 양적 확인 (멤버 부재가 아니라 키 자체가 비어 있음)
        resp = await client.get(
            "/api/v1/ns/HRM/rank/del-target?start=0&stop=-1",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.json()["data"]["count"] == 0

    async def test_delete_absent_rank_404(self, client: AsyncClient):
        """없는 키 삭제 → 404 KEY_NOT_FOUND."""
        resp = await client.delete(
            "/api/v1/ns/HRM/rank/no-such-key",
            headers={"X-API-Key": HRM_KEY},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"]["code"] == "KEY_NOT_FOUND"

    async def test_delete_requires_write(self, client: AsyncClient):
        """read-only 키(MONITOR)는 전체 삭제 거부 → 403 NAMESPACE_DENIED."""
        await client.post(
            "/api/v1/ns/HRM/rank/ro-guard",
            headers={"X-API-Key": HRM_KEY},
            json={"member": "m1", "score": 1.0},
        )
        resp = await client.delete(
            "/api/v1/ns/HRM/rank/ro-guard",
            headers={"X-API-Key": MONITOR_KEY},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "NAMESPACE_DENIED"
