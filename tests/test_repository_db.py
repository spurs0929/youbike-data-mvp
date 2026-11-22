"""API-1：PostgresStationRepository 的 PostgreSQL 整合測試。

需要測試資料庫，設定方式與 tests/test_schema.py 相同：

    YOUBIKE_TEST_DATABASE_URL=postgresql://youbike_test@localhost:5432/youbike_test_db

- 未設定時，本檔所有測試自動 skip。
- 已設定但無法連線、或資料庫名稱不含 "test" 時，測試直接失敗。
- 密碼以 PGPASSWORD 或 pgpass 檔提供，不寫在連線字串中。

隔離方式：建立一個隨機命名的 schema 並套用 migration，結束時只刪除這個 schema；
每個測試開始前清空該 schema 的資料表。「現在」以 clock 注入固定時間。
"""

import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

from repository import (
    EARTH_RADIUS_M,
    FRESHNESS_WINDOW,
    PostgresStationRepository,
    StationStatus,
)

try:
    import psycopg
except ImportError:  # 未設定測試資料庫時允許不安裝
    psycopg = None

DB_URL = os.environ.get("YOUBIKE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="未設定 YOUBIKE_TEST_DATABASE_URL，略過資料庫測試"
)

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "001_init.sql"

UTC = timezone.utc
NOW = datetime(2026, 11, 21, 8, 32, 30, tzinfo=UTC)
TAIPEI_101 = (25.0339, 121.5645)


def slot_of(moment):
    return moment.replace(
        minute=moment.minute - moment.minute % 5, second=0, microsecond=0
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema():
    if psycopg is None:
        pytest.fail("已設定 YOUBIKE_TEST_DATABASE_URL 但未安裝 psycopg")
    name = f"youbike_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        if "test" not in database.lower():
            pytest.fail(f"拒絕在資料庫 {database!r} 執行測試：名稱必須包含 'test'")
        conn.execute(f'CREATE SCHEMA "{name}"')
        try:
            conn.execute(f'SET search_path TO "{name}"')
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
            yield name
        finally:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')


@pytest.fixture
def db(schema):
    conn = psycopg.connect(DB_URL, autocommit=True, options=f"-c search_path={schema}")
    conn.execute(
        "TRUNCATE station_snapshots, station_hourly_stats, stations, fetch_batches"
        " RESTART IDENTITY CASCADE"
    )
    yield conn
    conn.close()


@pytest.fixture
def repo(db):
    return PostgresStationRepository(db, clock=lambda: NOW)


# ---------------------------------------------------------------------------
# 資料產生
# ---------------------------------------------------------------------------

_batch_seq = count(1)


def add_batch(db, fetched_at):
    """新增一個 success 批次，回傳 fetch_batches.id。"""
    n = next(_batch_seq)
    return db.execute(
        "INSERT INTO fetch_batches (batch_id, fetched_at, status, station_count, raw_path)"
        " VALUES (%s, %s, 'success', 1, %s) RETURNING id",
        (f"b{n}", fetched_at, f"raw/b{n}.json.gz"),
    ).fetchone()[0]


def add_station(db, station_id, name=None, lat=None, lng=None):
    db.execute(
        "INSERT INTO stations (station_id, station_name, district, address,"
        " latitude, longitude, first_seen_at, last_seen_at)"
        " VALUES (%s, %s, '信義區', '某路', %s, %s, %s, %s)",
        (station_id, name or f"站{station_id}", lat, lng, NOW, NOW),
    )


def add_snapshot(db, station_id, fetched_at, *, valid=True, bikes=5, reason=None):
    """在 fetched_at 所屬的槽寫入一筆快照（每次一個新批次）。"""
    batch_pk = add_batch(db, fetched_at)
    if valid:
        values = (True, 20, bikes, 20 - bikes, True, None)
    else:
        values = (True, 20, bikes, 20 - bikes, False, reason or "stale")
    db.execute(
        "INSERT INTO station_snapshots (station_id, slot_time, fetch_batch_id,"
        " info_time, is_active, total_spaces, available_bikes, available_spaces,"
        " is_valid, invalid_reason)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (station_id, slot_of(fetched_at), batch_pk, fetched_at - timedelta(minutes=1))
        + values,
    )


def fresh(minutes_ago=2):
    return NOW - timedelta(minutes=minutes_ago)


def offset(lat, lng, north_m=0.0, east_m=0.0):
    """從 (lat, lng) 往北、往東各移動若干公尺的近似位置（小距離用）。"""
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlng = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return lat + dlat, lng + dlng


# ================================================================ 最新快照與視窗


def test_get_station_returns_latest_snapshot_only(db, repo):
    add_station(db, "S1")
    add_snapshot(db, "S1", fresh(17), bikes=1)
    add_snapshot(db, "S1", fresh(12), bikes=2)
    add_snapshot(db, "S1", fresh(2), bikes=3)

    status = repo.get_station("S1")
    assert isinstance(status, StationStatus)
    assert status.available_bikes == 3
    assert status.slot_time == slot_of(fresh(2))
    assert status.slot_time.utcoffset() == timedelta(0)
    assert status.distance_m is None


def test_get_station_dto_fields(db, repo):
    add_station(db, "S1", name="捷運市政府站", lat=25.04, lng=121.56)
    add_snapshot(db, "S1", fresh(2), bikes=7)
    status = repo.get_station("  S1  ")  # 前後空白會去除
    assert status == StationStatus(
        station_id="S1",
        station_name="捷運市政府站",
        district="信義區",
        address="某路",
        latitude=25.04,
        longitude=121.56,
        total_spaces=20,
        available_bikes=7,
        available_spaces=13,
        is_active=True,
        is_valid=True,
        invalid_reason=None,
        info_time=fresh(2) - timedelta(minutes=1),
        slot_time=slot_of(fresh(2)),
    )


def test_window_boundary_exactly_30_minutes_is_included(db, repo):
    add_station(db, "S1")
    add_snapshot(db, "S1", NOW - FRESHNESS_WINDOW)
    assert repo.get_station("S1") is not None


def test_window_just_over_30_minutes_is_excluded(db, repo):
    add_station(db, "S1")
    add_snapshot(db, "S1", NOW - FRESHNESS_WINDOW - timedelta(microseconds=1))
    assert repo.get_station("S1") is None


def test_window_uses_fetched_at_not_info_time(db, repo):
    # info_time 很舊（來源停滯），但擷取時間在視窗內 → 仍回傳（過期由 invalid_reason 表示）
    add_station(db, "S1")
    batch_pk = add_batch(db, fresh(2))
    db.execute(
        "INSERT INTO station_snapshots (station_id, slot_time, fetch_batch_id,"
        " info_time, is_active, total_spaces, available_bikes, available_spaces,"
        " is_valid, invalid_reason)"
        " VALUES ('S1', %s, %s, %s, true, 20, 5, 15, false, 'stale')",
        (slot_of(fresh(2)), batch_pk, NOW - timedelta(hours=3)),
    )
    status = repo.get_station("S1")
    assert status is not None
    assert status.invalid_reason == "stale"


def test_old_latest_snapshot_is_not_returned(db, repo):
    add_station(db, "S1")
    add_snapshot(db, "S1", fresh(45))
    add_snapshot(db, "S1", fresh(40))
    assert repo.get_station("S1") is None


def test_latest_invalid_snapshot_is_not_replaced_by_older_valid(db, repo):
    # 最新一筆無效時照樣回傳，不改用較舊的有效快照補位
    add_station(db, "S1")
    add_snapshot(db, "S1", fresh(12), valid=True, bikes=9)
    add_snapshot(db, "S1", fresh(2), valid=False, reason="stale", bikes=1)
    status = repo.get_station("S1")
    assert status.is_valid is False
    assert status.invalid_reason == "stale"
    assert status.available_bikes == 1


def test_station_without_snapshot_returns_none(db, repo):
    add_station(db, "S1")
    assert repo.get_station("S1") is None
    assert repo.get_station("NOPE") is None


def test_clock_is_converted_to_utc(db):
    add_station(db, "S1")
    add_snapshot(db, "S1", NOW - FRESHNESS_WINDOW)
    taipei_now = NOW.astimezone(timezone(timedelta(hours=8)))
    repo = PostgresStationRepository(db, clock=lambda: taipei_now)
    assert repo.get_station("S1") is not None


def test_query_does_not_leave_transaction_open(db, repo):
    add_station(db, "S1")
    add_snapshot(db, "S1", fresh())
    repo.get_station("S1")
    repo.search_by_name("站")
    repo.find_nearby(*TAIPEI_101)
    assert db.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_works_with_non_autocommit_connection(schema, db):
    add_station(db, "S1")
    add_snapshot(db, "S1", fresh())
    with psycopg.connect(DB_URL, options=f"-c search_path={schema}") as conn:
        repo = PostgresStationRepository(conn, clock=lambda: NOW)
        assert repo.get_station("S1") is not None
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


# ================================================================ 站名搜尋


def test_search_is_case_insensitive(db, repo):
    # 只比對集合：大小寫字母的排序依資料庫 collation 而定，本機與雲端可能不同
    for sid, name in [
        ("3", "YouBike2.0_捷運B站"),
        ("1", "youbike2.0_捷運A站"),
        ("2", "其他"),
    ]:
        add_station(db, sid, name=name)
        add_snapshot(db, sid, fresh())
    assert {s.station_id for s in repo.search_by_name("YOUBIKE")} == {"1", "3"}


def test_search_orders_by_name_then_station_id(db, repo):
    for sid in ["B", "A", "C"]:
        add_station(db, sid, name="同名站")
        add_snapshot(db, sid, fresh())
    add_station(db, "Z", name="同名站之前")  # 名稱較長，排在後面
    add_snapshot(db, "Z", fresh())
    result = repo.search_by_name("同名站")
    assert [s.station_id for s in result] == ["A", "B", "C", "Z"]


def test_search_excludes_stations_outside_window(db, repo):
    add_station(db, "1", name="捷運站一")
    add_snapshot(db, "1", fresh())
    add_station(db, "2", name="捷運站二")
    add_snapshot(db, "2", fresh(40))
    assert [s.station_id for s in repo.search_by_name("捷運")] == ["1"]


def test_search_limit(db, repo):
    for i in range(25):
        add_station(db, f"S{i:02d}", name=f"站{i:02d}")
        add_snapshot(db, f"S{i:02d}", fresh())
    result = repo.search_by_name("站", limit=20)
    assert len(result) == 20
    assert [s.station_id for s in result] == [f"S{i:02d}" for i in range(20)]
    assert len(repo.search_by_name("站", limit=3)) == 3


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("%", {"pct", "back_pct"}),
        ("_", {"under"}),
        ("\\", {"back", "back_pct"}),
        ("\\%", {"back_pct"}),
        ("100%", {"pct"}),
    ],
)
def test_search_treats_wildcards_as_literal(db, repo, keyword, expected):
    names = {
        "pct": "滿100%站",
        "under": "A_B站",
        "back": "C\\D站",
        "back_pct": "E\\%F站",
        "plain": "AxB站",  # 若 _ 被當成萬用字元，"A_B" 會誤配到這裡
    }
    for sid, name in names.items():
        add_station(db, sid, name=name)
        add_snapshot(db, sid, fresh())
    assert {s.station_id for s in repo.search_by_name(keyword)} == expected


def test_search_no_match_returns_empty_list(db, repo):
    add_station(db, "1", name="捷運站")
    add_snapshot(db, "1", fresh())
    assert repo.search_by_name("不存在") == []


def test_search_input_is_bound_not_concatenated(db, repo):
    add_station(db, "1", name="捷運站")
    add_snapshot(db, "1", fresh())
    assert repo.search_by_name("'; DROP TABLE stations; --") == []
    assert repo.get_station("1' OR '1'='1") is None
    assert db.execute("SELECT count(*) FROM stations").fetchone()[0] == 1


# ================================================================ 附近查詢


def test_nearby_filters_by_circle_not_box(db, repo):
    lat, lng = TAIPEI_101
    radius = 500.0
    # 正北 499 公尺：圓內
    add_station(db, "IN", None, *offset(lat, lng, north_m=499))
    # 東北角 (400, 400)：在方框內，但距離約 566 公尺 > 500
    add_station(db, "CORNER", None, *offset(lat, lng, north_m=400, east_m=400))
    # 正東 520 公尺：方框外
    add_station(db, "OUT", None, *offset(lat, lng, east_m=520))
    for sid in ["IN", "CORNER", "OUT"]:
        add_snapshot(db, sid, fresh())

    result = repo.find_nearby(lat, lng, radius_m=radius)
    assert [s.station_id for s in result] == ["IN"]
    assert result[0].distance_m == pytest.approx(499, abs=1)


def test_nearby_sorted_by_distance_then_station_id(db, repo):
    lat, lng = TAIPEI_101
    for sid, north in [("FAR", 300), ("NEAR", 100), ("MID_B", 200), ("MID_A", 200)]:
        add_station(db, sid, None, *offset(lat, lng, north_m=north))
        add_snapshot(db, sid, fresh())
    result = repo.find_nearby(lat, lng, radius_m=1000)
    assert [s.station_id for s in result] == ["NEAR", "MID_A", "MID_B", "FAR"]
    distances = [s.distance_m for s in result]
    assert distances == sorted(distances)


def test_nearby_limit_keeps_closest(db, repo):
    lat, lng = TAIPEI_101
    for i in range(25):
        sid = f"S{i:02d}"
        add_station(db, sid, None, *offset(lat, lng, north_m=10 * (i + 1)))
        add_snapshot(db, sid, fresh())
    result = repo.find_nearby(lat, lng, radius_m=3000, limit=20)
    assert len(result) == 20
    assert [s.station_id for s in result] == [f"S{i:02d}" for i in range(20)]


def test_nearby_excludes_outside_window_and_missing_coordinates(db, repo):
    lat, lng = TAIPEI_101
    add_station(db, "OK", None, *offset(lat, lng, north_m=50))
    add_snapshot(db, "OK", fresh())
    add_station(db, "OLD", None, *offset(lat, lng, north_m=60))
    add_snapshot(db, "OLD", fresh(40))
    add_station(db, "NOCOORD")
    add_snapshot(db, "NOCOORD", fresh())
    assert [s.station_id for s in repo.find_nearby(lat, lng)] == ["OK"]


def test_nearby_returns_invalid_latest_with_reason(db, repo):
    lat, lng = TAIPEI_101
    add_station(db, "S1", None, *offset(lat, lng, north_m=50))
    add_snapshot(db, "S1", fresh(12), valid=True)
    add_snapshot(db, "S1", fresh(2), valid=False, reason="inactive")
    (status,) = repo.find_nearby(lat, lng)
    assert status.invalid_reason == "inactive"


def test_nearby_across_antimeridian(db, repo):
    add_station(db, "EAST", None, 0.0, 179.999)
    add_station(db, "WEST", None, 0.0, -179.999)
    add_station(db, "FAR", None, 0.0, 170.0)
    for sid in ["EAST", "WEST", "FAR"]:
        add_snapshot(db, sid, fresh())
    result = repo.find_nearby(0.0, 179.9995, radius_m=1000)
    assert {s.station_id for s in result} == {"EAST", "WEST"}
    result = repo.find_nearby(0.0, -179.9995, radius_m=1000)
    assert {s.station_id for s in result} == {"EAST", "WEST"}


def test_nearby_near_pole(db, repo):
    add_station(db, "P1", None, 89.999, 0.0)
    add_station(db, "P2", None, 89.999, 180.0)  # 隔著極點，約 222 公尺
    for sid in ["P1", "P2"]:
        add_snapshot(db, sid, fresh())
    result = repo.find_nearby(89.999, 0.0, radius_m=500)
    assert [s.station_id for s in result] == ["P1", "P2"]


def test_nearby_no_match_returns_empty_list(db, repo):
    assert repo.find_nearby(*TAIPEI_101) == []
