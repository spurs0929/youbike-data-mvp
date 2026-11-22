"""Tests for src/repository.py（API-1）：參數驗證、LIKE 跳脫、距離與方框計算。

不連資料庫。資料庫查詢的整合測試見 tests/test_repository_db.py。
"""

import math
from datetime import datetime, timezone

import pytest

import repository as repo
from repository import (
    EARTH_RADIUS_M,
    MAX_KEYWORD_LENGTH,
    MAX_RADIUS_M,
    MAX_RESULTS,
    InvalidQueryError,
    PostgresStationRepository,
    bounding_box,
    escape_like,
    haversine_m,
)

NOW = datetime(2026, 11, 21, 8, 30, tzinfo=timezone.utc)


class _NoDatabase:
    """任何存取都直接失敗：用來證明參數錯誤時不會碰資料庫。"""

    def __getattr__(self, name):
        raise AssertionError(f"參數錯誤時不應存取資料庫（{name}）")


@pytest.fixture
def no_db_repo():
    return PostgresStationRepository(_NoDatabase(), clock=lambda: NOW)


# ================================================================ 參數驗證


@pytest.mark.parametrize("limit", [0, -1, MAX_RESULTS + 1, 100])
def test_limit_out_of_range_rejected_not_truncated(no_db_repo, limit):
    with pytest.raises(InvalidQueryError):
        no_db_repo.search_by_name("捷運", limit=limit)
    with pytest.raises(InvalidQueryError):
        no_db_repo.find_nearby(25.04, 121.56, limit=limit)


@pytest.mark.parametrize("limit", [True, 1.0, "5", None])
def test_limit_must_be_int(no_db_repo, limit):
    with pytest.raises(InvalidQueryError):
        no_db_repo.search_by_name("捷運", limit=limit)


def test_limit_bounds_accepted():
    assert repo.validate_limit(1) == 1
    assert repo.validate_limit(MAX_RESULTS) == MAX_RESULTS


@pytest.mark.parametrize("radius", [0, -1, MAX_RADIUS_M + 0.001, math.inf, math.nan])
def test_radius_out_of_range_rejected(no_db_repo, radius):
    with pytest.raises(InvalidQueryError):
        no_db_repo.find_nearby(25.04, 121.56, radius_m=radius)


@pytest.mark.parametrize("radius", [True, "500", None])
def test_radius_must_be_number(no_db_repo, radius):
    with pytest.raises(InvalidQueryError):
        no_db_repo.find_nearby(25.04, 121.56, radius_m=radius)


def test_radius_bounds_accepted():
    assert repo.validate_radius(MAX_RADIUS_M) == MAX_RADIUS_M
    assert repo.validate_radius(0.1) == 0.1


@pytest.mark.parametrize(
    ("lat", "lng"),
    [
        (90.0001, 121.0),
        (-90.0001, 121.0),
        (25.0, 180.0001),
        (25.0, -180.0001),
        (math.nan, 121.0),
        (25.0, math.inf),
        ("25", 121.0),
        (25.0, None),
        (True, 121.0),
    ],
)
def test_coordinates_rejected(no_db_repo, lat, lng):
    with pytest.raises(InvalidQueryError):
        no_db_repo.find_nearby(lat, lng)


@pytest.mark.parametrize(("lat", "lng"), [(90, 180), (-90, -180), (0, 0), (25.04, 121)])
def test_coordinates_bounds_accepted(lat, lng):
    assert repo.validate_coordinates(lat, lng) == (float(lat), float(lng))


@pytest.mark.parametrize("keyword", ["", "   ", "\t\n", None, 123])
def test_keyword_rejected(no_db_repo, keyword):
    with pytest.raises(InvalidQueryError):
        no_db_repo.search_by_name(keyword)


def test_keyword_length_counted_after_strip(no_db_repo):
    text = "站" * MAX_KEYWORD_LENGTH
    assert repo.validate_keyword(f"  {text}  ") == text
    with pytest.raises(InvalidQueryError):
        no_db_repo.search_by_name(text + "站")


@pytest.mark.parametrize("station_id", ["", "  ", None, 500101001, "1" * 33])
def test_station_id_rejected(no_db_repo, station_id):
    with pytest.raises(InvalidQueryError):
        no_db_repo.get_station(station_id)


def test_invalid_query_error_is_value_error():
    assert issubclass(InvalidQueryError, ValueError)


# ================================================================ LIKE 跳脫


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("市政府", "市政府"),
        ("100%", "100\\%"),
        ("a_b", "a\\_b"),
        ("a\\b", "a\\\\b"),
        ("\\%", "\\\\\\%"),  # 先跳脫 \ 本身，再跳脫 %
    ],
)
def test_escape_like(text, expected):
    assert escape_like(text) == expected


# ================================================================ 距離


def test_haversine_zero_distance():
    assert haversine_m(25.04, 121.56, 25.04, 121.56) == 0.0


def test_haversine_one_degree_latitude():
    expected = EARTH_RADIUS_M * math.pi / 180
    assert haversine_m(25.0, 121.0, 26.0, 121.0) == pytest.approx(expected)


def test_haversine_across_antimeridian():
    # 179.999° 與 -179.999° 在赤道相距 0.002°，不是 359.998°
    expected = EARTH_RADIUS_M * math.radians(0.002)
    assert haversine_m(0, 179.999, 0, -179.999) == pytest.approx(expected)


def test_haversine_antipodal_does_not_fail():
    # 對蹠點的中間值可能因浮點誤差略大於 1，必須被限制在 [0, 1]
    assert haversine_m(0, 0, 0, 180) == pytest.approx(EARTH_RADIUS_M * math.pi)


# ================================================================ 方框


def _point_at(lat, lng, distance_m, bearing_deg):
    """從 (lat, lng) 沿方位角走 distance_m 後的位置（球面公式）。"""
    d = distance_m / EARTH_RADIUS_M
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lng)
    p2 = math.asin(
        math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b)
    )
    l2 = l1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    lng2 = (math.degrees(l2) + 540) % 360 - 180
    return math.degrees(p2), lng2


def _in_box(box, lat, lng):
    if not box.lat_min <= lat <= box.lat_max:
        return False
    if box.lng_min is None:
        return True
    if box.wraps:
        return lng >= box.lng_min or lng <= box.lng_max
    return box.lng_min <= lng <= box.lng_max


@pytest.mark.parametrize(
    ("lat", "lng"),
    [
        (25.04, 121.56),
        (0.0, 0.0),
        (-33.9, 151.2),
        (60.0, 10.0),
        (0.0, 179.99),
        (0.0, -179.99),
    ],
)
def test_bounding_box_contains_whole_circle(lat, lng):
    radius = MAX_RADIUS_M
    box = bounding_box(lat, lng, radius)
    for bearing in range(0, 360, 5):
        p_lat, p_lng = _point_at(lat, lng, radius, bearing)
        assert _in_box(box, p_lat, p_lng), (bearing, p_lat, p_lng)


def test_bounding_box_wraps_east():
    box = bounding_box(0.0, 179.99, MAX_RADIUS_M)
    assert box.wraps
    assert box.lng_min < 180.0 and box.lng_max < -179.0


def test_bounding_box_wraps_west():
    box = bounding_box(0.0, -179.99, MAX_RADIUS_M)
    assert box.wraps
    assert box.lng_min > 179.0 and box.lng_max > -180.0


def test_bounding_box_no_wrap_in_taipei():
    box = bounding_box(25.04, 121.56, MAX_RADIUS_M)
    assert not box.wraps
    assert box.lng_min < 121.56 < box.lng_max


@pytest.mark.parametrize("lat", [89.99, -89.99, 90.0, -90.0])
def test_bounding_box_near_pole_has_no_longitude_limit(lat):
    box = bounding_box(lat, 0.0, MAX_RADIUS_M)
    assert box.lng_min is None and box.lng_max is None
    assert -90.0 <= box.lat_min <= box.lat_max <= 90.0


# ================================================================ clock


class _NoQuery:
    def transaction(self):
        raise AssertionError("不應執行到查詢")


def test_naive_clock_rejected():
    r = PostgresStationRepository(_NoQuery(), clock=lambda: NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        r._cutoff()


def test_cutoff_uses_utc_and_window():
    r = PostgresStationRepository(_NoQuery(), clock=lambda: NOW)
    assert r._cutoff() == NOW - repo.FRESHNESS_WINDOW
