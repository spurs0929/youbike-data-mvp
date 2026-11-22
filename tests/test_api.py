"""Tests for src/api.py（API-2a）：端點、狀態碼與回應格式。

使用 InMemoryStationRepository，不連資料庫、不連網。
"""

import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api import create_app
from repository import (
    EARTH_RADIUS_M,
    MAX_KEYWORD_LENGTH,
    MAX_RESULTS,
    InMemoryStationRepository,
    PostgresStationRepository,
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


def test_both_implementations_satisfy_protocol():
    assert isinstance(InMemoryStationRepository(), StationRepository)
    assert isinstance(PostgresStationRepository(conn=None), StationRepository)


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
