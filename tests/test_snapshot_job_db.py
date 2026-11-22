"""Batch 4C-3b：snapshot_job.run_slot() 與 CLI 的 PostgreSQL 整合測試。

需要測試資料庫，設定方式與 tests/test_schema.py 相同：

    YOUBIKE_TEST_DATABASE_URL=postgresql://youbike_test@localhost:5432/youbike_test_db

- 未設定時，本檔所有測試自動 skip。
- 已設定但無法連線、或資料庫名稱不含 "test" 時，測試直接失敗。
- 密碼以 PGPASSWORD 或 pgpass 檔提供，不寫在連線字串中。

隔離方式：建立一個隨機命名的 schema 並套用 migration，結束時只刪除這個 schema。
run_slot 會自行 COMMIT，因此每個測試開始前清空該 schema 的資料表，
不使用 transaction 回滾。原始檔與 JSONL 寫在 pytest 的暫存目錄。
"""

import gzip
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import snapshot_job as job
from collect import BATCH_LOG
from fetch import REQUIRED_FIELDS

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
TAIPEI = timezone(timedelta(hours=8))


def at(minute, second=0):
    """2026-11-21 08:mm:ss UTC。"""
    return datetime(2026, 11, 21, 8, minute, second, tzinfo=UTC)


S0, S5, S10 = at(0), at(5), at(10)


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
def connect(schema):
    """開新連線（autocommit、search_path 指向測試 schema），測試結束時全部關閉。"""
    opened = []

    def _connect():
        conn = psycopg.connect(
            DB_URL, autocommit=True, options=f"-c search_path={schema}"
        )
        opened.append(conn)
        return conn

    yield _connect
    for conn in opened:
        conn.close()


@pytest.fixture
def db(connect):
    conn = connect()
    conn.execute(
        "TRUNCATE station_snapshots, station_hourly_stats, stations, fetch_batches"
        " RESTART IDENTITY CASCADE"
    )
    return conn


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# 資料產生
# ---------------------------------------------------------------------------


def taipei(moment: datetime) -> str:
    return moment.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")


def station(sno, fetched_at, **overrides) -> dict:
    """一筆原始站點資料；infoTime 預設比擷取時間早 2 分鐘。"""
    record = dict.fromkeys(REQUIRED_FIELDS, "")
    record.update(
        {
            "sno": sno,
            "sna": f"YouBike2.0_{sno}站",
            "sarea": "大安區",
            "ar": f"{sno}號",
            "act": "1",
            "srcUpdateTime": taipei(fetched_at - timedelta(seconds=68)),
            "updateTime": taipei(fetched_at - timedelta(seconds=68)),
            "infoTime": taipei(fetched_at - timedelta(minutes=2)),
            "Quantity": 20,
            "available_rent_bikes": 5,
            "available_return_bikes": 15,
            "latitude": 25.03,
            "longitude": 121.54,
        }
    )
    record.update(overrides)
    return record


def add_batch(data_dir, fetched_at, stations, *, suffix="aaaaaa", write_raw=True):
    """寫入原始檔並在 JSONL 附加一筆 success 紀錄，格式與 collect.py 相同。"""
    batch_id = f"{fetched_at:%Y%m%dT%H%M%SZ}-{suffix}"
    raw_path = f"raw/{fetched_at:%Y/%m/%d}/{batch_id}.json.gz"
    if write_raw:
        path = data_dir / raw_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wb") as f:
            f.write(json.dumps(stations, ensure_ascii=False).encode("utf-8"))
    record = {
        "batch_id": batch_id,
        "fetched_at": fetched_at.isoformat(),
        "source_updated_at": (fetched_at - timedelta(seconds=68)).isoformat(),
        "status": "success",
        "station_count": len(stations),
        "raw_path": raw_path,
        "error": None,
    }
    with open(data_dir / BATCH_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def add_failed_batch(data_dir, fetched_at):
    record = {
        "batch_id": f"{fetched_at:%Y%m%dT%H%M%SZ}-ffffff",
        "fetched_at": fetched_at.isoformat(),
        "source_updated_at": None,
        "status": "failed",
        "station_count": None,
        "raw_path": None,
        "error": "Request timed out",
    }
    with open(data_dir / BATCH_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def snapshots(conn, slot=None):
    sql = (
        "SELECT s.station_id, s.slot_time, b.batch_id, s.is_valid, s.invalid_reason,"
        " s.available_bikes FROM station_snapshots s"
        " JOIN fetch_batches b ON b.id = s.fetch_batch_id"
    )
    params = ()
    if slot is not None:
        sql += " WHERE s.slot_time = %s"
        params = (slot,)
    rows = conn.execute(sql + " ORDER BY 1, 2", params).fetchall()
    return {(r[0], r[1]): r[2:] for r in rows}


def station_row(conn, station_id):
    return conn.execute(
        "SELECT station_name, district, address, latitude, longitude,"
        " first_seen_at, last_seen_at FROM stations WHERE station_id = %s",
        (station_id,),
    ).fetchone()


def table_counts(conn):
    return tuple(
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("fetch_batches", "stations", "station_snapshots")
    )


# ---------------------------------------------------------------------------
# 基本流程
# ---------------------------------------------------------------------------


def test_first_run_writes_all_tables(db, data_dir):
    batch = add_batch(
        data_dir,
        at(6),
        [
            station("A", at(6)),
            station("B", at(6), act="0"),
            station("C", at(6), Quantity="abc"),
            station(None, at(6)),
        ],
    )

    report = job.run_slot(db, data_dir, S5)

    assert report["result"] == "partial"  # 有 1 筆跳過
    assert report["source"] == "jsonl"
    assert report["batch_id"] == batch["batch_id"]
    assert report["counts"] == {
        "written_valid": 1,
        "written_invalid": {"invalid_type": 1, "inactive": 1},
        "existing": 0,
        "skipped": 1,
        "failed": 0,
    }
    assert snapshots(db) == {
        ("A", S5): (batch["batch_id"], True, None, 5),
        ("B", S5): (batch["batch_id"], False, "inactive", 5),
        ("C", S5): (batch["batch_id"], False, "invalid_type", None),
    }
    assert station_row(db, "A") == ("A站", "大安區", "A號", 25.03, 121.54, S5, S5)
    # 結構無效的站只確保列存在並更新時間欄位
    assert station_row(db, "C") == (None, None, None, None, None, S5, S5)

    row = db.execute(
        "SELECT batch_id, fetched_at, status, station_count, raw_path, error"
        " FROM fetch_batches"
    ).fetchall()
    assert row == [
        (batch["batch_id"], at(6), "success", 4, batch["raw_path"], None),
    ]


def test_rerun_counts_everything_as_existing(db, data_dir):
    add_batch(data_dir, at(6), [station("A", at(6)), station("B", at(6))])
    job.run_slot(db, data_dir, S5)
    before = (snapshots(db), station_row(db, "A"), table_counts(db))

    report = job.run_slot(db, data_dir, S5)

    assert report["result"] == "success"
    assert report["source"] == "existing"
    assert report["counts"]["existing"] == 2
    assert report["counts"]["written_valid"] == 0
    assert (snapshots(db), station_row(db, "A"), table_counts(db)) == before


def test_no_batch(db, data_dir):
    add_failed_batch(data_dir, at(6))
    add_batch(data_dir, at(10), [station("A", at(10))])  # 相鄰槽，不借用

    report = job.run_slot(db, data_dir, S5)

    assert report["result"] == "no_batch"
    assert table_counts(db) == (0, 0, 0)


def test_requires_autocommit_connection(connect, data_dir):
    conn = connect()
    conn.autocommit = False
    with pytest.raises(ValueError, match="autocommit"):
        job.run_slot(conn, data_dir, S5)


# ---------------------------------------------------------------------------
# D：來源批次一致性
# ---------------------------------------------------------------------------


def fail_station(monkeypatch, station_id):
    """讓指定站的快照寫入違反 CHECK 約束（資料庫錯誤），模擬單站寫入失敗。"""
    original = job._snapshot_params

    def patched(result, slot, batch_pk):
        params = original(result, slot, batch_pk)
        if result.station_id == station_id:
            params["invalid_reason"] = "bogus"
            params["is_valid"] = False
        return params

    monkeypatch.setattr(job, "_snapshot_params", patched)


def test_late_batch_does_not_change_source_of_written_slot(db, data_dir, monkeypatch):
    """定版完成定義的關鍵測試：首次部分寫入後，JSONL 才出現同槽較新的成功批次，
    補跑仍須使用原批次。"""
    first = add_batch(
        data_dir, at(6), [station("A", at(6)), station("B", at(6))], suffix="111111"
    )
    with monkeypatch.context() as m:
        fail_station(m, "B")
        report = job.run_slot(db, data_dir, S5)
    assert report["result"] == "partial"
    assert report["counts"]["failed"] == 1

    # 較新的批次晚到，內容也不同
    add_batch(
        data_dir,
        at(8),
        [
            station("A", at(8), available_rent_bikes=9, available_return_bikes=11),
            station("B", at(8), available_rent_bikes=9, available_return_bikes=11),
        ],
        suffix="222222",
    )

    report = job.run_slot(db, data_dir, S5)

    assert report["result"] == "success"
    assert report["source"] == "existing"
    assert report["batch_id"] == first["batch_id"]
    assert report["counts"]["written_valid"] == 1
    assert report["counts"]["existing"] == 1
    assert snapshots(db) == {
        ("A", S5): (first["batch_id"], True, None, 5),
        ("B", S5): (first["batch_id"], True, None, 5),
    }
    # 晚到的批次沒有被寫進 fetch_batches
    assert table_counts(db)[0] == 1


def insert_foreign_snapshot(conn, station_id, slot, batch_id):
    """在槽內放一筆來自另一個批次的快照（模擬同槽多來源）。"""
    pk = conn.execute(
        "INSERT INTO fetch_batches (batch_id, fetched_at, status, station_count,"
        " raw_path) VALUES (%s, %s, 'success', 1, 'raw/x.json.gz') RETURNING id",
        (batch_id, slot + timedelta(minutes=1)),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO stations (station_id, first_seen_at, last_seen_at)"
        " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (station_id, slot, slot),
    )
    conn.execute(
        "INSERT INTO station_snapshots (station_id, slot_time, fetch_batch_id,"
        " is_valid, invalid_reason) VALUES (%s, %s, %s, false, 'missing_field')",
        (station_id, slot, pk),
    )


def test_multiple_source_batches_in_slot_fail_without_writing(db, data_dir):
    add_batch(data_dir, at(6), [station("A", at(6)), station("B", at(6))])
    job.run_slot(db, data_dir, S5)
    insert_foreign_snapshot(db, "Z", S5, "20261121T080900Z-zzzzzz")
    before = (snapshots(db), table_counts(db))

    report = job.run_slot(db, data_dir, S5)

    assert report["result"] == "failed"
    assert "multiple fetch_batch_id" in report["error"]
    assert report["counts"]["written_valid"] == 0
    assert report["counts"]["existing"] == 0
    assert (snapshots(db), table_counts(db)) == before


def test_missing_gzip_for_empty_slot_writes_nothing(db, data_dir):
    add_batch(data_dir, at(6), [station("A", at(6))], write_raw=False)

    report = job.run_slot(db, data_dir, S5)

    assert report["result"] == "failed"
    assert "RawBatchError" in report["error"]
    assert table_counts(db) == (0, 0, 0)


def test_missing_gzip_for_existing_slot_keeps_snapshots(db, data_dir):
    batch = add_batch(data_dir, at(6), [station("A", at(6))])
    job.run_slot(db, data_dir, S5)
    (data_dir / batch["raw_path"]).unlink()
    before = (snapshots(db), table_counts(db))

    report = job.run_slot(db, data_dir, S5)

    assert report["result"] == "failed"
    assert report["source"] == "existing"
    assert (snapshots(db), table_counts(db)) == before


# ---------------------------------------------------------------------------
# B：savepoint 與原子性
# ---------------------------------------------------------------------------


def test_single_station_failure_is_isolated_without_station_update(
    db, data_dir, monkeypatch
):
    # B 在較早的槽已有站點資料
    add_batch(data_dir, at(1), [station("B", at(1), sna="YouBike2.0_舊名")])
    job.run_slot(db, data_dir, S0)
    old_b = station_row(db, "B")

    add_batch(
        data_dir,
        at(6),
        [
            station("A", at(6)),
            station("B", at(6), sna="YouBike2.0_新名", latitude=25.5),
            station("C", at(6)),
        ],
    )
    fail_station(monkeypatch, "B")
    report = job.run_slot(db, data_dir, S5)

    assert report["result"] == "partial"
    assert report["counts"]["failed"] == 1
    assert report["counts"]["written_valid"] == 2
    assert set(snapshots(db, S5)) == {("A", S5), ("C", S5)}
    # B 的 stations upsert 與失敗的快照在同一個 savepoint，一起回滾
    assert station_row(db, "B") == old_b
    assert station_row(db, "B")[0] == "舊名"
    assert station_row(db, "B")[6] == S0


def test_failed_new_station_leaves_no_station_row(db, data_dir, monkeypatch):
    add_batch(data_dir, at(6), [station("A", at(6)), station("NEW", at(6))])
    fail_station(monkeypatch, "NEW")

    job.run_slot(db, data_dir, S5)

    assert station_row(db, "NEW") is None
    assert station_row(db, "A") is not None


def test_snapshot_conflict_rolls_back_station_update(db, data_dir, monkeypatch):
    """預先查詢被繞過時，快照 insert 衝突仍會回滾同一 savepoint 內的 stations 更新。"""
    add_batch(data_dir, at(6), [station("A", at(6)), station("B", at(6))])
    job.run_slot(db, data_dir, S5)
    db.execute("UPDATE stations SET station_name = 'MANUAL'")
    monkeypatch.setattr(job, "_existing_station_ids", lambda conn, slot: set())

    report = job.run_slot(db, data_dir, S5)

    assert report["result"] == "success"
    assert report["counts"]["existing"] == 2
    assert report["counts"]["written_valid"] == 0
    names = db.execute("SELECT DISTINCT station_name FROM stations").fetchall()
    assert names == [("MANUAL",)]


def test_batch_row_failure_rolls_back_everything(db, data_dir, monkeypatch):
    add_batch(data_dir, at(6), [station("A", at(6))])

    def broken(conn, batch):
        conn.execute("SELECT 1 / 0")

    monkeypatch.setattr(job, "_ensure_batch_row", broken)
    report = job.run_slot(db, data_dir, S5)

    assert report["result"] == "failed"
    assert "DivisionByZero" in report["error"]
    assert table_counts(db) == (0, 0, 0)


def test_error_after_some_writes_rolls_back_and_reports_zero(db, data_dir, monkeypatch):
    """寫了幾站之後 transaction 才失敗：全部回滾，計數不報成已寫入。"""
    add_batch(data_dir, at(6), [station(s, at(6)) for s in "ABCD"])
    original = job._write_station
    calls = []

    def flaky(conn, result, slot, batch_pk):
        calls.append(result.station_id)
        if len(calls) == 3:
            # 在 savepoint 之外讓整個 transaction 進入錯誤狀態
            conn.execute("SELECT 1 / 0")
        return original(conn, result, slot, batch_pk)

    monkeypatch.setattr(job, "_write_station", flaky)
    report = job.run_slot(db, data_dir, S5)

    assert calls == ["A", "B", "C"]
    assert report["result"] == "failed"
    assert report["counts"]["written_valid"] == 0
    assert report["counts"]["written_invalid"] == {}
    assert table_counts(db) == (0, 0, 0)


def test_unexpected_python_error_rolls_back_and_propagates(db, data_dir, monkeypatch):
    add_batch(data_dir, at(6), [station(s, at(6)) for s in "ABC"])
    original = job._write_station

    def boom(conn, result, slot, batch_pk):
        if result.station_id == "C":
            raise RuntimeError("bug")
        return original(conn, result, slot, batch_pk)

    monkeypatch.setattr(job, "_write_station", boom)
    with pytest.raises(RuntimeError):
        job.run_slot(db, data_dir, S5)
    assert table_counts(db) == (0, 0, 0)


# ---------------------------------------------------------------------------
# C：stations 跨槽更新
# ---------------------------------------------------------------------------


def run_station(db, data_dir, fetched_at, **overrides):
    add_batch(
        data_dir,
        fetched_at,
        [station("A", fetched_at, **overrides)],
        suffix=uuid.uuid4().hex[:6],
    )
    report = job.run_slot(db, data_dir, job.slot_of(fetched_at))
    assert report["result"] == "success", report
    return report


def test_newer_slot_overwrites_station_info(db, data_dir):
    run_station(db, data_dir, at(6), sna="YouBike2.0_舊名", latitude=25.0)
    run_station(db, data_dir, at(11), sna="YouBike2.0_新名", latitude=25.1)

    row = station_row(db, "A")
    assert (row[0], row[3], row[5], row[6]) == ("新名", 25.1, S5, S10)


def test_older_slot_written_later_does_not_overwrite(db, data_dir):
    run_station(db, data_dir, at(11), sna="YouBike2.0_新名", latitude=25.1)
    run_station(db, data_dir, at(6), sna="YouBike2.0_舊名", latitude=25.0)

    row = station_row(db, "A")
    assert row[0] == "新名"
    assert row[3] == 25.1
    assert (row[5], row[6]) == (S5, S10)  # first_seen 往前修正、last_seen 不變


def test_null_values_do_not_overwrite_existing_values(db, data_dir):
    run_station(db, data_dir, at(6))
    run_station(
        db, data_dir, at(11), sna="YouBike2.0_", sarea="  ", ar=None, latitude=999
    )

    assert station_row(db, "A") == ("A站", "大安區", "A號", 25.03, 121.54, S5, S10)


def test_structurally_invalid_newer_slot_only_updates_times(db, data_dir):
    run_station(db, data_dir, at(6))
    add_batch(data_dir, at(11), [station("A", at(11), infoTime=None)], suffix="bbbbbb")
    job.run_slot(db, data_dir, S10)

    assert station_row(db, "A") == ("A站", "大安區", "A號", 25.03, 121.54, S5, S10)


def concurrent_station_update(connect, data_dir, first_slot, first_name, later_run):
    """另一條連線先在 first_slot 更新站點 A 並保持 transaction 未結束，
    此時以 run_slot 寫入另一個槽；確認 run_slot 會等待，而且結果正確。"""
    holder = connect()
    worker = connect()
    result = {}

    def target():
        result["report"] = job.run_slot(worker, data_dir, later_run)

    params = {
        "station_id": "A",
        "station_name": first_name,
        "district": "大安區",
        "address": "A號",
        "latitude": 25.2,
        "longitude": 121.5,
        "slot": first_slot,
    }
    with holder.transaction():
        holder.execute(job._UPSERT_STATION, params)
        thread = threading.Thread(target=target)
        thread.start()
        thread.join(timeout=1.0)
        blocked = thread.is_alive()  # 等待 A 的列鎖
    thread.join(timeout=10)
    assert not thread.is_alive()
    return blocked, result["report"]


def test_concurrent_older_slot_waits_and_does_not_overwrite(db, connect, data_dir):
    add_batch(data_dir, at(6), [station("A", at(6), sna="YouBike2.0_舊名")])

    blocked, report = concurrent_station_update(connect, data_dir, S10, "新名", S5)

    assert blocked
    assert report["result"] == "success"
    row = station_row(db, "A")
    assert row[0] == "新名"
    assert (row[5], row[6]) == (S5, S10)


def test_concurrent_newer_slot_waits_and_overwrites(db, connect, data_dir):
    add_batch(data_dir, at(11), [station("A", at(11), sna="YouBike2.0_新名")])

    blocked, report = concurrent_station_update(connect, data_dir, S5, "舊名", S10)

    assert blocked
    assert report["result"] == "success"
    row = station_row(db, "A")
    assert row[0] == "新名"
    assert (row[5], row[6]) == (S5, S10)


# ---------------------------------------------------------------------------
# A：同槽鎖
# ---------------------------------------------------------------------------


def try_lock(conn, slot):
    return conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s, %s)",
        (job.LOCK_NAMESPACE, job.slot_lock_key(slot)),
    ).fetchone()[0]


def test_slot_lock_blocks_same_slot_only_and_releases_on_commit(db, connect):
    holder = connect()
    other = connect()

    with holder.transaction():
        job.acquire_slot_lock(holder, S5)
        with other.transaction():
            assert try_lock(other, S5) is False  # 同槽互斥
            assert try_lock(other, S10) is True  # 不同槽可以並行

    with other.transaction():
        assert try_lock(other, S5) is True  # transaction 結束後鎖已釋放


def test_slot_lock_released_on_rollback(db, connect):
    holder = connect()
    other = connect()

    with pytest.raises(RuntimeError), holder.transaction():
        job.acquire_slot_lock(holder, S5)
        raise RuntimeError("rollback")

    with other.transaction():
        assert try_lock(other, S5) is True


def test_run_slot_takes_lock_before_reading_existing_snapshots(db, connect, data_dir):
    """另一個寫入者持有同槽鎖並寫入較早的批次 X 時，run_slot 必須等待；
    之後看到既有快照，沿用 X，而不是自行從 JSONL 挑較新的批次 Y。"""
    add_batch(
        data_dir, at(6), [station("A", at(6)), station("B", at(6))], suffix="xxxxxx"
    )
    add_batch(
        data_dir, at(8), [station("A", at(8)), station("B", at(8))], suffix="yyyyyy"
    )
    holder = connect()
    worker = connect()
    result = {}

    def target():
        result["report"] = job.run_slot(worker, data_dir, S5)

    with holder.transaction():
        job.acquire_slot_lock(holder, S5)
        thread = threading.Thread(target=target)
        thread.start()
        thread.join(timeout=1.0)
        assert thread.is_alive()  # 等待同槽鎖
        insert_foreign_snapshot(holder, "A", S5, "20261121T080600Z-xxxxxx")
    thread.join(timeout=10)

    report = result["report"]
    assert report["source"] == "existing"
    assert report["batch_id"] == "20261121T080600Z-xxxxxx"
    batch_ids = {v[0] for v in snapshots(db, S5).values()}
    assert batch_ids == {"20261121T080600Z-xxxxxx"}


# ---------------------------------------------------------------------------
# CLI 端對端
# ---------------------------------------------------------------------------


def test_cli_end_to_end(db, connect, data_dir, capsys):
    add_batch(data_dir, at(6), [station("A", at(6))])
    now = at(12)

    code = job.main(
        ["--slot", "2026-11-21T08:05:00Z"], now=now, connect=connect, data_dir=data_dir
    )
    first = json.loads(capsys.readouterr().out)
    code_again = job.main([], now=at(10, 30), connect=connect, data_dir=data_dir)
    second = json.loads(capsys.readouterr().out)

    assert (code, first["result"], first["counts"]["written_valid"]) == (
        0,
        "success",
        1,
    )
    assert (code_again, second["result"], second["counts"]["existing"]) == (
        0,
        "success",
        1,
    )


def test_cli_failed_exit_code(db, connect, data_dir, capsys):
    add_batch(data_dir, at(6), [station("A", at(6))], write_raw=False)

    code = job.main([], now=at(10, 30), connect=connect, data_dir=data_dir)

    assert code == 1
    assert json.loads(capsys.readouterr().out)["result"] == "failed"


# ---------------------------------------------------------------------------
# E：效能紀錄（不設門檻，只記錄；本機 PostgreSQL，連線在同一台機器）
# ---------------------------------------------------------------------------


def test_performance_1826_stations(db, connect, data_dir, record_property, capsys):
    stations = [station(f"5001{i:05d}", at(6)) for i in range(1826)]
    add_batch(data_dir, at(6), stations)

    t0 = time.perf_counter()
    first = job.run_slot(db, data_dir, S5)
    t1 = time.perf_counter()
    again = job.run_slot(db, data_dir, S5)
    t2 = time.perf_counter()

    # 只量 savepoint 本身的成本：同樣次數的 SAVEPOINT／RELEASE
    probe = connect()
    t3 = time.perf_counter()
    with probe.transaction():
        for _ in range(1826):
            with probe.transaction():
                pass
    t4 = time.perf_counter()

    assert first["counts"]["written_valid"] == 1826
    assert again["counts"]["existing"] == 1826

    timings = {
        "first_write_s": round(t1 - t0, 3),
        "rerun_all_existing_s": round(t2 - t1, 3),
        "savepoint_only_1826_s": round(t4 - t3, 3),
        # 每站：SAVEPOINT、stations upsert、快照 insert、RELEASE
        "round_trips_per_new_station": 4,
        "round_trips_first_write": 1826 * 4 + 6,
    }
    for key, value in timings.items():
        record_property(key, value)
    with capsys.disabled():
        print(f"\n[4C-3b performance] {json.dumps(timings)}")
