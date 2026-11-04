"""Batch 4A-2：驗證 migrations/001_init.sql 的資料表與約束。

需要一個 PostgreSQL 測試資料庫，以環境變數指定連線字串：

    YOUBIKE_TEST_DATABASE_URL=postgresql://youbike_test@localhost:5432/youbike_test_db

- 未設定時，本檔所有測試自動 skip，其餘測試照常不需連網。
- 已設定但無法連線、或資料庫名稱不含 "test" 時，測試直接失敗，不會靜默 skip。
- 密碼不寫在連線字串中，請用 PGPASSWORD 或 pgpass 檔提供。

隔離方式：每次測試執行時建立一個隨機命名的 schema，在其中套用 migration，
結束時只刪除這個 schema；每個測試函式再包在 transaction 中並於結束時回滾。
不會動到資料庫中其他 schema 的任何資料。
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

try:
    import psycopg
    from psycopg import errors
except ImportError:  # 未設定測試資料庫時允許不安裝；已設定時由 schema fixture 報錯
    psycopg = errors = None

DB_URL = os.environ.get("YOUBIKE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="未設定 YOUBIKE_TEST_DATABASE_URL，略過資料庫測試"
)

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "001_init.sql"
TABLES = {"fetch_batches", "stations", "station_snapshots", "station_hourly_stats"}

SLOT = datetime(2026, 11, 4, 8, 5, tzinfo=timezone.utc)  # UTC 整 5 分鐘
HOUR = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)  # UTC 整點


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema():
    """建立隨機命名的 schema 並套用 migration，結束時只刪除這個 schema。"""
    if psycopg is None:
        pytest.fail(
            "已設定 YOUBIKE_TEST_DATABASE_URL 但未安裝 psycopg，"
            "請執行 pip install -r requirements-dev.txt"
        )
    name = f"youbike_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        if "test" not in database.lower():
            pytest.fail(
                f"拒絕在資料庫 {database!r} 執行測試：名稱必須包含 'test'，"
                "避免誤用正式資料庫"
            )
        conn.execute(f'CREATE SCHEMA "{name}"')
        try:
            conn.execute(f'SET search_path TO "{name}"')
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
            yield name
        finally:
            conn.execute("ROLLBACK")  # 若有未結束的 transaction 先結束
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')


@pytest.fixture
def conn(schema):
    """每個測試一條連線、一個 transaction，結束時回滾。"""
    with psycopg.connect(DB_URL) as c:
        c.execute(f'SET search_path TO "{schema}"')
        yield c
        c.rollback()


def rejects(conn, error, sql, params=None):
    """在 savepoint 中執行 SQL，確認被資料庫以指定錯誤拒絕。"""
    with pytest.raises(error), conn.transaction():
        conn.execute(sql, params)


def accepts(conn, sql, params=None):
    with conn.transaction():
        return conn.execute(sql, params)


# ---------------------------------------------------------------------------
# 資料產生
# ---------------------------------------------------------------------------

BATCH_SQL = """
    INSERT INTO fetch_batches
      (batch_id, fetched_at, source_updated_at, status, station_count, raw_path, error)
    VALUES (%(batch_id)s, %(fetched_at)s, %(source_updated_at)s, %(status)s,
            %(station_count)s, %(raw_path)s, %(error)s)
    RETURNING id
"""

# 對應 collect.collect_once() 的三種輸出情境
BATCH_SUCCESS = {
    "batch_id": "20261104T080500Z-aaaaaa",
    "fetched_at": "2026-11-04T08:05:00.123456+00:00",
    "source_updated_at": "2026-11-04T08:04:30+00:00",
    "status": "success",
    "station_count": 1800,
    "raw_path": "raw/2026/11/04/20261104T080500Z-aaaaaa.json.gz",
    "error": None,
}
BATCH_DOWNLOAD_FAILED = {
    "batch_id": "20261104T080600Z-bbbbbb",
    "fetched_at": "2026-11-04T08:06:00+00:00",
    "source_updated_at": None,
    "status": "failed",
    "station_count": None,
    "raw_path": None,
    "error": "HTTP request failed: timeout",
}
BATCH_PARSE_FAILED = {
    **BATCH_DOWNLOAD_FAILED,
    "batch_id": "20261104T080700Z-cccccc",
    "raw_path": "raw/2026/11/04/20261104T080700Z-cccccc.json.gz",
    "error": "missing required fields",
}


def add_batch(conn, **overrides):
    row = {**BATCH_SUCCESS, **overrides}
    return accepts(conn, BATCH_SQL, row).fetchone()[0]


def add_station(conn, station_id="500101001"):
    accepts(
        conn,
        "INSERT INTO stations (station_id, first_seen_at, last_seen_at)"
        " VALUES (%s, %s, %s)",
        (station_id, SLOT, SLOT),
    )
    return station_id


SNAPSHOT_SQL = """
    INSERT INTO station_snapshots
      (station_id, slot_time, fetch_batch_id, info_time, is_active,
       total_spaces, available_bikes, available_spaces, is_valid, invalid_reason)
    VALUES (%(station_id)s, %(slot_time)s, %(fetch_batch_id)s, %(info_time)s,
            %(is_active)s, %(total_spaces)s, %(available_bikes)s,
            %(available_spaces)s, %(is_valid)s, %(invalid_reason)s)
"""


@pytest.fixture
def snapshot(conn):
    """回傳一筆合法的有效快照；測試以覆寫欄位的方式製造非法資料。"""
    return {
        "station_id": add_station(conn),
        "slot_time": SLOT,
        "fetch_batch_id": add_batch(conn),
        "info_time": SLOT - timedelta(seconds=40),
        "is_active": True,
        "total_spaces": 20,
        "available_bikes": 5,
        "available_spaces": 14,
        "is_valid": True,
        "invalid_reason": None,
    }


HOURLY_SQL = """
    INSERT INTO station_hourly_stats
      (station_id, hour_start, sum_available_bikes, sum_available_docks,
       empty_samples, low_samples, full_samples, valid_samples, expected_samples)
    VALUES (%(station_id)s, %(hour_start)s, %(sum_available_bikes)s,
            %(sum_available_docks)s, %(empty_samples)s, %(low_samples)s,
            %(full_samples)s, %(valid_samples)s, %(expected_samples)s)
"""


@pytest.fixture
def hourly(conn):
    return {
        "station_id": add_station(conn),
        "hour_start": HOUR,
        "sum_available_bikes": 60,
        "sum_available_docks": 168,
        "empty_samples": 2,
        "low_samples": 3,
        "full_samples": 1,
        "valid_samples": 11,
        "expected_samples": 12,
    }


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_migration_creates_tables_and_brin_index(conn, schema):
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        )
    }
    assert tables == TABLES

    method = conn.execute(
        "SELECT am.amname FROM pg_index i"
        " JOIN pg_class c ON c.oid = i.indexrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_am am ON am.oid = c.relam"
        " WHERE c.relname = 'station_snapshots_slot_time_brin' AND n.nspname = %s",
        (schema,),
    ).fetchone()
    assert method == ("brin",)


def test_migration_rerun_fails_without_partial_changes(schema):
    """重複執行會失敗，且整個檔案回滾，不會留下部分變更。"""
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute(f'SET search_path TO "{schema}"')
        with pytest.raises(errors.DuplicateTable):
            c.execute(MIGRATION.read_text(encoding="utf-8"))
        c.execute("ROLLBACK")
        count = c.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        ).fetchone()[0]
    assert count == len(TABLES)


# ---------------------------------------------------------------------------
# fetch_batches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row", [BATCH_SUCCESS, BATCH_DOWNLOAD_FAILED, BATCH_PARSE_FAILED]
)
def test_batch_accepts_collect_outputs(conn, row):
    add_batch(conn, **row)


def test_batch_success_may_have_null_source_time(conn):
    add_batch(conn, source_updated_at=None)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"status": "ok"}, id="unknown-status"),
        # schema.md 第八節 NULL 情境 3
        pytest.param({"station_count": None}, id="success-null-count"),
        pytest.param({"station_count": 0}, id="success-zero-count"),
        pytest.param({"raw_path": None}, id="success-null-raw-path"),
        pytest.param({"error": "boom"}, id="success-with-error"),
        pytest.param({**BATCH_DOWNLOAD_FAILED, "error": None}, id="failed-null-error"),
        pytest.param({**BATCH_DOWNLOAD_FAILED, "station_count": 5}, id="failed-count"),
        pytest.param(
            {**BATCH_DOWNLOAD_FAILED, "source_updated_at": "2026-11-04T08:00:00Z"},
            id="failed-source-time",
        ),
    ],
)
def test_batch_rejects_inconsistent_rows(conn, overrides):
    rejects(conn, errors.CheckViolation, BATCH_SQL, {**BATCH_SUCCESS, **overrides})


def test_batch_id_is_unique_and_on_conflict_returns_nothing(conn):
    """4C 的「批次列確保存在」：ON CONFLICT DO NOTHING 遇到既有列時不回傳 id。"""
    first = add_batch(conn)
    rejects(conn, errors.UniqueViolation, BATCH_SQL, BATCH_SUCCESS)

    sql = BATCH_SQL.replace("RETURNING", "ON CONFLICT (batch_id) DO NOTHING RETURNING")
    assert accepts(conn, sql, BATCH_SUCCESS).fetchone() is None
    found = conn.execute(
        "SELECT id FROM fetch_batches WHERE batch_id = %s",
        (BATCH_SUCCESS["batch_id"],),
    ).fetchone()
    assert found == (first,)


# ---------------------------------------------------------------------------
# stations
# ---------------------------------------------------------------------------


def test_station_allows_null_except_id_and_seen_times(conn):
    add_station(conn)
    row = conn.execute(
        "SELECT station_name, district, address, latitude, longitude FROM stations"
    ).fetchone()
    assert row == (None, None, None, None, None)


@pytest.mark.parametrize(
    "station_id, first, last, error",
    [
        pytest.param("  ", SLOT, SLOT, "CheckViolation", id="blank-id"),
        pytest.param(
            "500101001",
            SLOT,
            SLOT - timedelta(minutes=5),
            "CheckViolation",
            id="last-before-first",
        ),
        pytest.param("500101001", None, SLOT, "NotNullViolation", id="null-first"),
        pytest.param("500101001", SLOT, None, "NotNullViolation", id="null-last"),
    ],
)
def test_station_rejects_invalid_rows(conn, station_id, first, last, error):
    rejects(
        conn,
        getattr(errors, error),
        "INSERT INTO stations (station_id, first_seen_at, last_seen_at)"
        " VALUES (%s, %s, %s)",
        (station_id, first, last),
    )


# ---------------------------------------------------------------------------
# station_snapshots
# ---------------------------------------------------------------------------


def test_snapshot_accepts_valid_row(conn, snapshot):
    accepts(conn, SNAPSHOT_SQL, snapshot)


def test_snapshot_accepts_zero_capacity_as_valid(conn, snapshot):
    """實作規則 3：total_spaces = 0 先保留，不直接判定無效。"""
    accepts(
        conn,
        SNAPSHOT_SQL,
        {**snapshot, "total_spaces": 0, "available_bikes": 0, "available_spaces": 0},
    )


def test_snapshot_accepts_invalid_row_with_missing_values(conn, snapshot):
    """無效列只要有原因代碼，數值與 is_active 都可以是 NULL。"""
    accepts(
        conn,
        SNAPSHOT_SQL,
        {
            **snapshot,
            "info_time": None,
            "is_active": None,
            "total_spaces": None,
            "available_bikes": None,
            "available_spaces": None,
            "is_valid": False,
            "invalid_reason": "invalid_type",
        },
    )


@pytest.mark.parametrize(
    "overrides",
    [
        # schema.md 第八節 NULL 情境 1
        pytest.param({"total_spaces": None}, id="valid-null-total"),
        pytest.param({"available_bikes": None}, id="valid-null-bikes"),
        pytest.param({"available_spaces": None}, id="valid-null-spaces"),
        pytest.param({"info_time": None}, id="valid-null-info-time"),
        pytest.param({"is_active": None}, id="valid-unknown-active"),
        pytest.param({"is_active": False}, id="valid-inactive"),
        pytest.param({"total_spaces": -1}, id="valid-negative-total"),
        pytest.param({"available_bikes": -1}, id="valid-negative-bikes"),
        pytest.param({"available_bikes": 21}, id="valid-bikes-over-total"),
        pytest.param({"available_spaces": 21}, id="valid-spaces-over-total"),
        pytest.param({"invalid_reason": "stale"}, id="valid-with-reason"),
        # schema.md 第八節 NULL 情境 2
        pytest.param({"is_valid": False}, id="invalid-null-reason"),
        pytest.param({"is_valid": False, "invalid_reason": ""}, id="invalid-empty"),
        pytest.param(
            {"is_valid": False, "invalid_reason": "other"}, id="invalid-unknown-code"
        ),
    ],
)
def test_snapshot_rejects_invalid_rows(conn, snapshot, overrides):
    rejects(conn, errors.CheckViolation, SNAPSHOT_SQL, {**snapshot, **overrides})


@pytest.mark.parametrize(
    "reason",
    [
        "inactive",
        "stale",
        "missing_field",
        "invalid_type",
        "capacity_invalid",
        "count_invalid",
        "duplicate_conflict",
    ],
)
def test_snapshot_accepts_every_reason_code(conn, snapshot, reason):
    accepts(
        conn, SNAPSHOT_SQL, {**snapshot, "is_valid": False, "invalid_reason": reason}
    )


@pytest.mark.parametrize("tz", ["UTC", "Asia/Taipei", "Asia/Kolkata"])
def test_snapshot_slot_alignment_ignores_session_timezone(conn, snapshot, tz):
    """對齊檢查以 UTC epoch 計算，不受 session 時區影響（含非整點時差的時區）。"""
    conn.execute(f"SET TIME ZONE '{tz}'")
    accepts(conn, SNAPSHOT_SQL, snapshot)
    for bad in (timedelta(minutes=1), timedelta(seconds=1), timedelta(microseconds=1)):
        rejects(
            conn,
            errors.CheckViolation,
            SNAPSHOT_SQL,
            {**snapshot, "slot_time": SLOT + bad},
        )


def test_snapshot_primary_key_prevents_duplicate_slot(conn, snapshot):
    accepts(conn, SNAPSHOT_SQL, snapshot)
    rejects(conn, errors.UniqueViolation, SNAPSHOT_SQL, snapshot)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"station_id": "999999999"}, id="unknown-station"),
        pytest.param({"fetch_batch_id": 999999}, id="unknown-batch"),
    ],
)
def test_snapshot_requires_existing_station_and_batch(conn, snapshot, overrides):
    rejects(conn, errors.ForeignKeyViolation, SNAPSHOT_SQL, {**snapshot, **overrides})


# ---------------------------------------------------------------------------
# station_hourly_stats
# ---------------------------------------------------------------------------


def test_hourly_accepts_valid_row(conn, hourly):
    accepts(conn, HOURLY_SQL, hourly)


def test_hourly_accepts_zero_expected_samples(conn, hourly):
    """整小時暫停營運是否寫入由 4D 決定；約束本身允許 0。"""
    accepts(
        conn,
        HOURLY_SQL,
        {
            **hourly,
            "sum_available_bikes": 0,
            "sum_available_docks": 0,
            "empty_samples": 0,
            "low_samples": 0,
            "full_samples": 0,
            "valid_samples": 0,
            "expected_samples": 0,
        },
    )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"hour_start": HOUR + timedelta(minutes=5)}, id="not-aligned"),
        pytest.param({"sum_available_bikes": -1}, id="negative-sum"),
        pytest.param({"empty_samples": -1}, id="negative-empty"),
        pytest.param({"empty_samples": 6, "low_samples": 6}, id="empty+low>valid"),
        pytest.param({"full_samples": 12}, id="full>valid"),
        pytest.param({"valid_samples": -1}, id="negative-valid"),
        pytest.param({"expected_samples": 10}, id="valid>expected"),
        pytest.param({"valid_samples": 13, "expected_samples": 13}, id="over-12"),
    ],
)
def test_hourly_rejects_invalid_rows(conn, hourly, overrides):
    rejects(conn, errors.CheckViolation, HOURLY_SQL, {**hourly, **overrides})


def test_hourly_primary_key_prevents_duplicate_hour(conn, hourly):
    accepts(conn, HOURLY_SQL, hourly)
    rejects(conn, errors.UniqueViolation, HOURLY_SQL, hourly)
