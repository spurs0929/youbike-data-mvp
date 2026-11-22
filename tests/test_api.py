"""Tests for src/api.py（API-2a、API-2b）與 src/app_main.py：端點、狀態碼與回應格式。

使用 InMemoryStationRepository 與假的連線池，不連資料庫、不連網。
真實 PostgreSQL 與連線池的整合測試在 tests/test_api_db.py。
"""

import math
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

import app_main
from api import create_app
from repository import (
    EARTH_RADIUS_M,
    MAX_KEYWORD_LENGTH,
    MAX_RESULTS,
    UNAVAILABLE_MESSAGE,
    InMemoryStationRepository,
    InvalidQueryError,
    PooledStationRepository,
    PostgresStationRepository,
    RepositoryUnavailableError,
    StationRepository,
    StationStatus,
)

UTC = timezone.utc
SLOT = datetime(2026, 11, 21, 8, 30, tzinfo=UTC)
CENTER = (25.0339, 121.5645)


def offset(north_m=0.0, east_m=0.0):
    lat, lng = CENTER
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlng = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return lat + dlat, lng + dlng


def station(station_id, name, coords=(None, None), *, valid=True, reason=None):
    lat, lng = coords
    return StationStatus(
        station_id=station_id,
        station_name=name,
        district="信義區",
        address="某路",
        latitude=lat,
        longitude=lng,
        total_spaces=20,
        available_bikes=5,
        available_spaces=15,
        is_active=True,
        is_valid=valid,
        invalid_reason=reason,
        info_time=SLOT - timedelta(minutes=1),
        slot_time=SLOT,
    )


STATIONS = [
    station("S1", "捷運市政府站", offset(north_m=100)),
    station("S2", "捷運台北101站", offset(north_m=300)),
    station("S3", "YouBike 100%站", offset(east_m=200), valid=False, reason="stale"),
    station("S4", "遠方站", offset(north_m=5000)),
    station("S5", "沒有座標站"),
]


@pytest.fixture
def client():
    return TestClient(create_app(InMemoryStationRepository(STATIONS)))


def ids(response):
    return [item["station_id"] for item in response.json()["items"]]


# ================================================================ 介面


def test_all_implementations_satisfy_protocol():
    assert isinstance(InMemoryStationRepository(), StationRepository)
    assert isinstance(PostgresStationRepository(conn=None), StationRepository)
    assert isinstance(PooledStationRepository(pool=None), StationRepository)


def test_in_memory_rejects_duplicate_station_id():
    with pytest.raises(ValueError):
        InMemoryStationRepository([station("S1", "a"), station("S1", "b")])


# ================================================================ 指定站點


def test_get_station(client):
    response = client.get("/stations/S1")
    assert response.status_code == 200
    body = response.json()
    assert body["station_id"] == "S1"
    assert body["station_name"] == "捷運市政府站"
    assert body["available_bikes"] == 5
    assert body["is_valid"] is True
    assert body["invalid_reason"] is None
    assert body["distance_m"] is None
    # 時間為 UTC
    parsed = datetime.fromisoformat(body["slot_time"].replace("Z", "+00:00"))
    assert parsed == SLOT
    assert parsed.utcoffset() == timedelta(0)


def test_get_station_invalid_snapshot_is_returned_with_reason(client):
    body = client.get("/stations/S3").json()
    assert body["is_valid"] is False
    assert body["invalid_reason"] == "stale"


def test_get_station_not_found_is_404(client):
    response = client.get("/stations/NOPE")
    assert response.status_code == 404


@pytest.mark.parametrize("station_id", ["%20", "1" * 33])
def test_get_station_invalid_id_is_422(client, station_id):
    assert client.get(f"/stations/{station_id}").status_code == 422


# ================================================================ 站名搜尋


def test_search(client):
    response = client.get("/stations", params={"q": "捷運"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert {item["station_id"] for item in body["items"]} == {"S1", "S2"}


def test_search_is_case_insensitive(client):
    assert ids(client.get("/stations", params={"q": "youbike"})) == ["S3"]


@pytest.mark.parametrize(("keyword", "expected"), [("%", ["S3"]), ("_", [])])
def test_search_wildcards_are_literal(client, keyword, expected):
    assert ids(client.get("/stations", params={"q": keyword})) == expected


def test_search_no_match_is_empty_list_not_404(client):
    response = client.get("/stations", params={"q": "不存在"})
    assert response.status_code == 200
    assert response.json() == {"count": 0, "items": []}


def test_search_limit(client):
    response = client.get("/stations", params={"q": "站", "limit": 2})
    assert response.json()["count"] == 2
    assert len(ids(response)) == 2


@pytest.mark.parametrize(
    "params",
    [
        {},  # 缺少 q
        {"q": ""},
        {"q": "   "},
        {"q": "站" * (MAX_KEYWORD_LENGTH + 1)},
        {"q": "站", "limit": 0},
        {"q": "站", "limit": MAX_RESULTS + 1},
        {"q": "站", "limit": "abc"},
        {"q": "站", "limit": 1.5},
    ],
)
def test_search_invalid_params_are_422(client, params):
    assert client.get("/stations", params=params).status_code == 422


# ================================================================ 附近查詢


def test_nearby_route_is_not_treated_as_station_id(client):
    lat, lng = CENTER
    response = client.get("/stations/nearby", params={"lat": lat, "lng": lng})
    assert response.status_code == 200
    assert "items" in response.json()


def test_nearby_sorted_by_distance_and_excludes_far_and_no_coords(client):
    lat, lng = CENTER
    response = client.get(
        "/stations/nearby", params={"lat": lat, "lng": lng, "radius_m": 1000}
    )
    body = response.json()
    assert [item["station_id"] for item in body["items"]] == ["S1", "S3", "S2"]
    assert body["count"] == 3
    distances = [item["distance_m"] for item in body["items"]]
    assert distances == sorted(distances)
    assert distances[0] == pytest.approx(100, abs=1)


def test_nearby_default_radius_is_500(client):
    lat, lng = CENTER
    assert ids(client.get("/stations/nearby", params={"lat": lat, "lng": lng})) == [
        "S1",
        "S3",
        "S2",
    ]
    # 300 公尺的 S2 在 500 內；改用 250 時排除
    assert ids(
        client.get("/stations/nearby", params={"lat": lat, "lng": lng, "radius_m": 250})
    ) == ["S1", "S3"]


def test_nearby_limit(client):
    lat, lng = CENTER
    response = client.get(
        "/stations/nearby", params={"lat": lat, "lng": lng, "limit": 1}
    )
    assert ids(response) == ["S1"]


def test_nearby_no_match_is_empty_list(client):
    response = client.get("/stations/nearby", params={"lat": 0, "lng": 0})
    assert response.status_code == 200
    assert response.json() == {"count": 0, "items": []}


@pytest.mark.parametrize(
    "params",
    [
        {"lng": 121.5},  # 缺少 lat
        {"lat": 25.0},  # 缺少 lng
        {"lat": "abc", "lng": 121.5},
        {"lat": 90.1, "lng": 121.5},
        {"lat": 25.0, "lng": -180.1},
        {"lat": "nan", "lng": 121.5},
        {"lat": 25.0, "lng": "inf"},
        {"lat": 25.0, "lng": 121.5, "radius_m": 0},
        {"lat": 25.0, "lng": 121.5, "radius_m": -1},
        {"lat": 25.0, "lng": 121.5, "radius_m": 3000.1},
        {"lat": 25.0, "lng": 121.5, "limit": 0},
        {"lat": 25.0, "lng": 121.5, "limit": MAX_RESULTS + 1},
    ],
)
def test_nearby_invalid_params_are_422(client, params):
    assert client.get("/stations/nearby", params=params).status_code == 422


def test_invalid_query_error_detail_is_message(client):
    response = client.get("/stations/nearby", params={"lat": 91, "lng": 0})
    assert response.status_code == 422
    assert "緯度" in response.json()["detail"]


# ================================================================ /health、/ready（API-2b）


class CountingCheck:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error


def test_health_is_200_and_does_not_touch_repository_or_check():
    check = CountingCheck(RepositoryUnavailableError(UNAVAILABLE_MESSAGE))
    with TestClient(create_app(UnavailableRepository(), readiness_check=check)) as c:
        response = c.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert check.calls == 0


def test_ready_without_check_is_200(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_runs_check_on_every_request():
    check = CountingCheck()
    c = TestClient(create_app(InMemoryStationRepository(), readiness_check=check))
    assert c.get("/ready").json() == {"status": "ready"}
    assert c.get("/ready").status_code == 200
    assert check.calls == 2


SECRET_CAUSE = "connection to server at 10.0.0.9 failed: password=hunter2"


def unavailable_error():
    """帶有敏感 __cause__ 的 RepositoryUnavailableError，用來確認回應不洩漏細節。"""
    try:
        raise RepositoryUnavailableError(
            UNAVAILABLE_MESSAGE
        ) from psycopg.OperationalError(SECRET_CAUSE)
    except RepositoryUnavailableError as exc:
        return exc


def test_ready_is_503_when_database_unavailable():
    check = CountingCheck(unavailable_error())
    c = TestClient(create_app(InMemoryStationRepository(), readiness_check=check))
    response = c.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "hunter2" not in response.text and "10.0.0.9" not in response.text


def test_ready_does_not_hide_other_errors():
    # 連不到資料庫以外的錯誤不是 503，照原樣丟出（TestClient 預設把伺服器例外丟回測試）
    check = CountingCheck(ZeroDivisionError("bug"))
    c = TestClient(create_app(InMemoryStationRepository(), readiness_check=check))
    with pytest.raises(ZeroDivisionError):
        c.get("/ready")


class UnavailableRepository:
    def get_station(self, station_id):
        raise unavailable_error()

    def search_by_name(self, keyword, limit=10):
        raise unavailable_error()

    def find_nearby(self, latitude, longitude, radius_m=500.0, limit=10):
        raise unavailable_error()


@pytest.mark.parametrize(
    "url",
    ["/stations/S1", "/stations?q=站", "/stations/nearby?lat=25&lng=121.5"],
)
def test_endpoints_are_503_when_database_unavailable(url):
    response = TestClient(create_app(UnavailableRepository())).get(url)
    assert response.status_code == 503
    assert response.json() == {"detail": UNAVAILABLE_MESSAGE}
    assert "hunter2" not in response.text and "10.0.0.9" not in response.text


# ================================================================ PooledStationRepository（假連線池）


class FakeConn:
    def __init__(self, *, broken=False, closed=False):
        self.broken = broken
        self.closed = closed
        self.executed = []

    @contextmanager
    def transaction(self):
        yield

    def execute(self, sql):
        self.executed.append(sql)
        return self

    def fetchone(self):
        return (1,)


class FakePool:
    """記錄借出、歸還、open/close 的假連線池。"""

    def __init__(self, conn=None, acquire_error=None):
        self.conn = conn or FakeConn()
        self.acquire_error = acquire_error
        self.borrowed = 0
        self.returned = 0
        self.open_calls = []
        self.close_calls = 0

    @contextmanager
    def connection(self):
        if self.acquire_error is not None:
            raise self.acquire_error
        self.borrowed += 1
        try:
            yield self.conn
        finally:
            self.returned += 1

    def open(self, wait=True):
        self.open_calls.append(wait)

    def close(self):
        self.close_calls += 1


@pytest.mark.parametrize(
    "error", [PoolTimeout("timeout"), PoolClosed("closed"), TooManyRequests("busy")]
)
def test_pool_acquire_failure_becomes_unavailable(error):
    repo = PooledStationRepository(FakePool(acquire_error=error))
    with pytest.raises(RepositoryUnavailableError) as info:
        repo.ping()
    assert str(info.value) == UNAVAILABLE_MESSAGE
    assert info.value.__cause__ is error


@pytest.mark.parametrize("conn", [FakeConn(broken=True), FakeConn(closed=True)])
def test_operational_error_on_broken_connection_becomes_unavailable(conn):
    pool = FakePool(conn)
    error = psycopg.OperationalError("server closed the connection")

    def action(_conn):
        raise error

    with pytest.raises(RepositoryUnavailableError) as info:
        PooledStationRepository(pool)._run(action)
    assert info.value.__cause__ is error
    assert pool.returned == pool.borrowed == 1


@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError("canceling statement due to statement timeout"),
        psycopg.ProgrammingError("relation does not exist"),
        RuntimeError("bug"),
    ],
)
def test_other_errors_are_not_hidden_and_connection_is_returned(error):
    # 連線仍正常的 OperationalError、SQL 錯誤、程式錯誤：照原樣丟出；連線一樣歸還
    pool = FakePool(FakeConn())

    def action(_conn):
        raise error

    with pytest.raises(type(error)) as info:
        PooledStationRepository(pool)._run(action)
    assert info.value is error
    assert pool.returned == pool.borrowed == 1


def test_connection_is_returned_after_success():
    pool = FakePool()
    assert PooledStationRepository(pool)._run(lambda conn: "ok") == "ok"
    assert pool.returned == pool.borrowed == 1


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.get_station(" "),
        lambda r: r.search_by_name("", 5),
        lambda r: r.search_by_name("站", MAX_RESULTS + 1),
        lambda r: r.find_nearby(91, 0),
        lambda r: r.find_nearby(25, 121, 0),
        lambda r: r.find_nearby(25, 121, 500, 0),
    ],
)
def test_invalid_params_do_not_borrow_connection(call):
    # 參數錯誤在借連線之前判斷：資料庫離線時，參數錯誤仍是 422 而不是 503
    pool = FakePool(acquire_error=PoolTimeout("should not borrow"))
    with pytest.raises(InvalidQueryError):
        call(PooledStationRepository(pool))
    assert pool.borrowed == 0


# ================================================================ app_main（API-2b）


@pytest.mark.parametrize(
    "environ", [{}, {"YOUBIKE_DATABASE_URL": ""}, {"YOUBIKE_DATABASE_URL": "  "}]
)
def test_missing_database_url_fails_at_startup(environ):
    with pytest.raises(app_main.MissingConfigError, match="YOUBIKE_DATABASE_URL"):
        app_main.create_production_app(environ)


def test_production_app_does_not_connect_until_lifespan():
    app = app_main.create_production_app(
        {"YOUBIKE_DATABASE_URL": "postgresql:///youbike"}
    )
    pool = app.state.pool
    assert pool.closed  # 建立 app 時不開啟連線池、不連線
    assert (pool.min_size, pool.max_size, pool.timeout) == (1, 4, 5.0)
    assert app_main.POOL_MAX_SIZE == 4


def test_lifespan_opens_without_waiting_and_closes_pool():
    pool = FakePool()
    app = app_main.create_app_with_pool(pool)
    assert pool.open_calls == [] and pool.close_calls == 0
    with TestClient(app) as c:
        assert pool.open_calls == [False]  # 明確 open(wait=False)
        assert c.get("/health").status_code == 200
    assert pool.close_calls == 1


def test_ready_uses_the_same_pool():
    pool = FakePool()
    with TestClient(app_main.create_app_with_pool(pool)) as c:
        assert c.get("/ready").json() == {"status": "ready"}
    assert pool.borrowed == pool.returned == 1
    assert pool.conn.executed == ["SELECT 1"]


def unused_tcp_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_database_offline_app_still_starts():
    # 指向沒有人監聽的 port：連線一定失敗，但 app 照樣啟動
    port = unused_tcp_port()
    url = f"postgresql://nobody@127.0.0.1:{port}/youbike?connect_timeout=1"
    pool = app_main.build_pool(url, timeout=0.2)
    with TestClient(app_main.create_app_with_pool(pool)) as c:
        assert c.get("/health").status_code == 200
        response = c.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert str(port) not in response.text
        assert c.get("/stations/S1").json() == {"detail": UNAVAILABLE_MESSAGE}
    assert pool.closed
