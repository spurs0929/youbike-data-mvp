"""Tests for src/snapshot_job.py（4C-3a：時間槽、批次紀錄、原始檔、處理結果）。

全部使用假資料、暫存目錄與固定時間，不連網、不連資料庫。
"""

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import snapshot_job as job
from collect import BATCH_LOG, collect_once
from fetch import REQUIRED_FIELDS
from snapshot import BatchValidation
from snapshot_job import (
    RESULT_FAILED,
    RESULT_NO_BATCH,
    RESULT_PARTIAL,
    RESULT_SUCCESS,
    BatchLog,
    RawBatchError,
    RunCounts,
    build_report,
    classify_result,
    format_report,
    latest_closed_slot,
    load_raw_stations,
    parse_batch_line,
    parse_slot,
    read_batch_log,
    resolve_raw_path,
    select_batch,
    slot_of,
)

UTC = timezone.utc
TAIPEI = timezone(timedelta(hours=8))
SLOT = datetime(2026, 11, 21, 8, 5, tzinfo=UTC)


def at(minute, second=0, microsecond=0):
    """2026-11-21 08:mm:ss UTC。"""
    return datetime(2026, 11, 21, 8, minute, second, microsecond, tzinfo=UTC)


# ================================================================ 時間槽


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (at(5), at(5)),  # 剛好在槽起點，屬於本槽
        (at(9, 59, 999999), at(5)),  # 槽結束前一微秒
        (at(10), at(10)),  # 槽結束點屬於下一槽（半開區間）
        (at(0), at(0)),
        (at(7, 30), at(5)),
    ],
)
def test_slot_of_half_open_interval(moment, expected):
    assert slot_of(moment) == expected


def test_slot_of_converts_to_utc_first():
    # 台灣 16:07 = UTC 08:07 → 槽 08:05（UTC）
    result = slot_of(datetime(2026, 11, 21, 16, 7, tzinfo=TAIPEI))
    assert result == at(5)
    assert result.utcoffset() == timedelta(0)


def test_slot_of_crosses_day_boundary_in_utc():
    # 台灣 11/22 07:58 = UTC 11/21 23:58 → 槽為 UTC 11/21 23:55
    result = slot_of(datetime(2026, 11, 22, 7, 58, tzinfo=TAIPEI))
    assert result == datetime(2026, 11, 21, 23, 55, tzinfo=UTC)


def test_slot_of_rejects_naive_datetime():
    with pytest.raises(ValueError):
        slot_of(at(5).replace(tzinfo=None))


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(10), at(5)),  # 08:10 整：目前槽剛開始，最近結束的是 08:05
        (at(14, 59, 999999), at(5)),
        (at(15), at(10)),
        (
            datetime(2026, 11, 21, 0, 2, tzinfo=UTC),
            datetime(2026, 11, 20, 23, 55, tzinfo=UTC),
        ),
    ],
)
def test_latest_closed_slot(now, expected):
    assert latest_closed_slot(now) == expected


def test_latest_closed_slot_rejects_naive_now():
    with pytest.raises(ValueError):
        latest_closed_slot(at(10).replace(tzinfo=None))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-21T08:05:00Z", at(5)),
        ("2026-11-21T08:05:00+00:00", at(5)),
        ("2026-11-21T16:05:00+08:00", at(5)),
        (" 2026-11-21T08:05:00z ", at(5)),
        # 本地時間未對齊，但轉成 UTC 後對齊（08:06+00:01 = UTC 08:05）
        ("2026-11-21T08:06:00+00:01", at(5)),
    ],
)
def test_parse_slot_accepts_aligned_utc(text, expected):
    result = parse_slot(text)
    assert result == expected
    assert result.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-21T08:05:00",  # 沒有時區
        "2026-11-21T08:07:00Z",  # 未對齊五分鐘
        "2026-11-21T08:05:30Z",  # 有秒數
        "2026-11-21T08:05:00.000001Z",  # 有微秒
        # 本地時間對齊，但轉成 UTC 後未對齊（08:05+00:01 = UTC 08:04）
        "2026-11-21T08:05:00+00:01",
        "abc",
        "",
    ],
)
def test_parse_slot_rejects(text):
    with pytest.raises(ValueError):
        parse_slot(text)


# ================================================================ 批次紀錄


def batch(
    fetched_at=None,
    batch_id=None,
    status="success",
    **overrides,
) -> dict:
    """一筆 JSONL 紀錄，格式與 collect.collect_once() 相同。預設擷取時間 08:06。"""
    if fetched_at is None:
        fetched_at = at(6)
    if batch_id is None:
        batch_id = f"{fetched_at:%Y%m%dT%H%M%SZ}-aaaaaa"
    if status == "success":
        record = {
            "batch_id": batch_id,
            "fetched_at": fetched_at.isoformat(),
            "source_updated_at": (fetched_at - timedelta(seconds=68)).isoformat(),
            "status": "success",
            "station_count": 1826,
            "raw_path": f"raw/{fetched_at:%Y/%m/%d}/{batch_id}.json.gz",
            "error": None,
        }
    else:
        record = {
            "batch_id": batch_id,
            "fetched_at": fetched_at.isoformat(),
            "source_updated_at": None,
            "status": "failed",
            "station_count": None,
            "raw_path": None,
            "error": "Request timed out",
        }
    record["status"] = status
    record.update(overrides)
    return record


def write_log(tmp_path, lines) -> Path:
    """lines 可混用 dict（轉成 JSON）與 str／bytes（原樣寫入）。"""
    path = tmp_path / BATCH_LOG
    with open(path, "wb") as f:
        for line in lines:
            if isinstance(line, dict):
                line = json.dumps(line, ensure_ascii=False)
            if isinstance(line, str):
                line = line.encode("utf-8")
            f.write(line + b"\n")
    return path


def test_parse_batch_line_matches_collect_output(tmp_path):
    body = json.dumps([dict.fromkeys(REQUIRED_FIELDS, "x")]).encode()
    collect_once(data_dir=tmp_path, fetcher=lambda: body, now=lambda: at(6, 1, 234567))

    log = read_batch_log(tmp_path / BATCH_LOG)

    (record,) = log.records
    assert record.status == "success"
    assert record.fetched_at == at(6, 1, 234567)
    assert record.slot == at(5)
    assert record.raw_path.startswith("raw/2026/11/21/")
    assert log.bad_lines == 0


@pytest.mark.parametrize(
    "line",
    [
        "{not json",
        '{"batch_id": "x", "status": "success"',  # 寫到一半
        "[]",
        json.dumps(batch(batch_id="")),
        json.dumps(batch(status="unknown")),
        json.dumps(batch(fetched_at=at(6).replace(tzinfo=None))),  # 沒有時區
        json.dumps({**batch(), "fetched_at": None}),
        json.dumps(batch(station_count="1826")),
        json.dumps(batch(station_count=True)),
        json.dumps(batch(station_count=0)),  # 違反 success 約束
        json.dumps(batch(raw_path=None)),  # 違反 success 約束
        json.dumps(batch(error="boom")),  # 違反 success 約束
        json.dumps(batch(status="failed", error=None)),  # 違反 failed 約束
        json.dumps(batch(status="failed", station_count=3)),
        json.dumps(batch(source_updated_at="not a time")),
    ],
)
def test_parse_batch_line_rejects(line):
    with pytest.raises((ValueError, TypeError)):
        parse_batch_line(line)


def test_read_batch_log_skips_bad_lines_and_blank_lines(tmp_path):
    path = write_log(
        tmp_path,
        [
            batch(at(6)),
            "",
            "{broken",
            b"\xff\xfe not utf-8",
            batch(at(7)),
            '{"batch_id": "half',  # 最後一行寫到一半
        ],
    )

    log = read_batch_log(path)

    assert [r.fetched_at for r in log.records] == [at(6), at(7)]
    assert log.bad_lines == 3
    assert log.duplicate_ids == 0


def test_read_batch_log_keeps_first_of_duplicate_batch_id(tmp_path):
    path = write_log(
        tmp_path,
        [
            batch(at(6), batch_id="dup", status="failed"),
            batch(at(9), batch_id="dup", status="success"),
        ],
    )

    log = read_batch_log(path)

    (record,) = log.records
    assert record.status == "failed"
    assert record.fetched_at == at(6)
    assert log.duplicate_ids == 1


def test_read_batch_log_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_batch_log(tmp_path / BATCH_LOG)


def test_read_batch_log_does_not_modify_file(tmp_path):
    path = write_log(tmp_path, [batch(at(6)), "{broken"])
    before = path.read_bytes()

    read_batch_log(path)

    assert path.read_bytes() == before


def log_of(tmp_path, lines) -> BatchLog:
    return read_batch_log(write_log(tmp_path, lines))


def test_select_latest_success_in_slot(tmp_path):
    log = log_of(
        tmp_path,
        [
            batch(at(5)),
            batch(at(8, 30)),
            batch(at(7)),
            batch(at(9, 59, 999999), status="failed"),  # 較新但失敗
        ],
    )

    chosen = select_batch(log, SLOT)

    assert chosen.fetched_at == at(8, 30)


def test_select_ignores_batches_outside_slot(tmp_path):
    log = log_of(
        tmp_path,
        [
            batch(at(4, 59, 999999)),  # 上一槽
            batch(at(6)),
            batch(at(10)),  # 下一槽
        ],
    )
    assert select_batch(log, SLOT).fetched_at == at(6)


def test_select_tie_breaks_by_batch_id(tmp_path):
    log = log_of(
        tmp_path,
        [
            batch(at(6), batch_id="20261121T080600Z-cccccc"),
            batch(at(6), batch_id="20261121T080600Z-ffffff"),
            batch(at(6), batch_id="20261121T080600Z-aaaaaa"),
        ],
    )
    assert select_batch(log, SLOT).batch_id == "20261121T080600Z-ffffff"


def test_select_returns_none_without_success_in_slot(tmp_path):
    log = log_of(
        tmp_path,
        [
            batch(at(6), status="failed"),
            batch(at(10)),  # 相鄰槽的成功批次不借用
        ],
    )
    assert select_batch(log, SLOT) is None


def test_select_uses_deduplicated_records(tmp_path):
    # 同一 batch_id 第一筆為 failed，後面的 success 重複紀錄不得覆蓋
    log = log_of(
        tmp_path,
        [
            batch(at(8), batch_id="dup", status="failed"),
            batch(at(8), batch_id="dup", status="success"),
            batch(at(6), batch_id="other"),
        ],
    )
    assert select_batch(log, SLOT).batch_id == "other"


def test_select_accepts_slot_in_other_timezone(tmp_path):
    log = log_of(tmp_path, [batch(at(6))])
    taipei_slot = SLOT.astimezone(TAIPEI)
    assert select_batch(log, taipei_slot).fetched_at == at(6)


def test_select_rejects_unaligned_slot(tmp_path):
    log = log_of(tmp_path, [batch(at(6))])
    with pytest.raises(ValueError):
        select_batch(log, at(6))


def test_select_requires_batch_log(tmp_path):
    # 不接受任意 list：避免呼叫端略過 read_batch_log 的去重
    records = list(log_of(tmp_path, [batch(at(6))]).records)
    with pytest.raises(TypeError):
        select_batch(records, SLOT)


def test_batch_log_rejects_duplicate_batch_id(tmp_path):
    (failed,) = log_of(
        tmp_path, [batch(at(8), batch_id="dup", status="failed")]
    ).records
    (success,) = log_of(tmp_path, [batch(at(8), batch_id="dup")]).records

    with pytest.raises(ValueError, match="duplicate"):
        BatchLog(records=(failed, success), bad_lines=0, duplicate_ids=0)


def test_batch_log_rejects_non_record_items(tmp_path):
    with pytest.raises(TypeError):
        BatchLog(records=(batch(at(6)),), bad_lines=0, duplicate_ids=0)
    with pytest.raises(TypeError):
        BatchLog(records=[], bad_lines=0, duplicate_ids=0)


def test_duplicate_success_after_failed_is_never_selected(tmp_path):
    # 回歸測試：重複紀錄夾在中間、且比第一筆更晚，仍以第一筆（failed）為準
    log = log_of(
        tmp_path,
        [
            batch(at(6), batch_id="dup", status="failed"),
            batch(at(7), batch_id="other"),
            batch(at(9), batch_id="dup", status="success"),
        ],
    )

    assert log.duplicate_ids == 1
    assert select_batch(log, SLOT).batch_id == "other"


# ================================================================ 原始檔


def station(sno="500101001") -> dict:
    record = dict.fromkeys(REQUIRED_FIELDS, "x")
    record["sno"] = sno
    return record


def write_raw(data_dir, raw_path, payload) -> Path:
    path = data_dir / raw_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(payload, bytes):
        payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with gzip.open(path, "wb") as f:
        f.write(payload)
    return path


RAW = "raw/2026/11/21/20261121T080600Z-aaaaaa.json.gz"


def test_load_raw_stations(tmp_path):
    write_raw(tmp_path, RAW, [station("1"), station("2")])

    stations = load_raw_stations(tmp_path, RAW)

    assert [s["sno"] for s in stations] == ["1", "2"]


def test_load_raw_stations_from_collect_output(tmp_path):
    body = json.dumps([station("1")]).encode()
    record = collect_once(data_dir=tmp_path, fetcher=lambda: body, now=lambda: at(6))

    assert load_raw_stations(tmp_path, record["raw_path"]) == [station("1")]


def test_missing_raw_file(tmp_path):
    with pytest.raises(RawBatchError, match="not found"):
        load_raw_stations(tmp_path, RAW)


def test_corrupt_gzip(tmp_path):
    path = tmp_path / RAW
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not gzip")

    with pytest.raises(RawBatchError, match="corrupt"):
        load_raw_stations(tmp_path, RAW)


def test_truncated_gzip(tmp_path):
    path = write_raw(tmp_path, RAW, [station(str(i)) for i in range(200)])
    path.write_bytes(path.read_bytes()[:-20])

    with pytest.raises(RawBatchError, match="corrupt"):
        load_raw_stations(tmp_path, RAW)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        [],  # 空 list
        {"stations": []},  # 不是 list
        [{"sno": "1"}],  # 缺少必要欄位
    ],
)
def test_raw_content_fails_validation(tmp_path, payload):
    write_raw(tmp_path, RAW, payload)

    with pytest.raises(RawBatchError, match="validation"):
        load_raw_stations(tmp_path, RAW)


def test_decompressed_size_limit(tmp_path):
    payload = json.dumps([station("1")]).encode()
    write_raw(tmp_path, RAW, payload)

    assert load_raw_stations(tmp_path, RAW, max_bytes=len(payload))
    with pytest.raises(RawBatchError, match="exceeds"):
        load_raw_stations(tmp_path, RAW, max_bytes=len(payload) - 1)


def test_size_limit_stops_reading_early(tmp_path, monkeypatch):
    # 壓縮後很小、解壓後很大的檔案：只讀到上限加 1 byte 就停止
    write_raw(tmp_path, RAW, b"[" + b" " * 5_000_000 + b"]")
    reads = []
    real_open = gzip.open

    def tracking_open(*args, **kwargs):
        f = real_open(*args, **kwargs)
        original_read = f.read

        def read(size=-1):
            reads.append(size)
            return original_read(size)

        f.read = read
        return f

    monkeypatch.setattr(job.gzip, "open", tracking_open)
    with pytest.raises(RawBatchError, match="exceeds"):
        load_raw_stations(tmp_path, RAW, max_bytes=1000)
    assert reads == [1001]


@pytest.mark.parametrize(
    "raw_path",
    [
        "../outside.json.gz",
        "raw/../../outside.json.gz",
        "/etc/passwd",
        "",
        "   ",
        ".",
        "raw/..",
    ],
)
def test_raw_path_must_stay_inside_data_dir(tmp_path, raw_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_raw(tmp_path, "outside.json.gz", [station("1")])

    with pytest.raises(RawBatchError):
        load_raw_stations(data_dir, raw_path)


def test_raw_path_symlink_outside_data_dir_is_rejected(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "raw").mkdir(parents=True)
    outside = write_raw(tmp_path, "outside.json.gz", [station("1")])
    link = data_dir / "raw" / "link.json.gz"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("此環境無法建立符號連結（例如 Windows 未開啟開發人員模式）")

    with pytest.raises(RawBatchError, match="escapes"):
        load_raw_stations(data_dir, "raw/link.json.gz")


def test_resolve_raw_path_inside_data_dir(tmp_path):
    resolved = resolve_raw_path(tmp_path, "raw/./2026/11/21/x.json.gz")
    assert resolved == (tmp_path / "raw/2026/11/21/x.json.gz").resolve()


# ================================================================ 處理結果


def counts(**kwargs) -> RunCounts:
    return RunCounts(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "batch_found", "error", "expected"),
    [
        # success：處理完成，沒有跳過或失敗
        (
            {"written_valid": 1800, "written_invalid": {"inactive": 20}},
            True,
            None,
            RESULT_SUCCESS,
        ),
        # success：純重跑、全部已存在
        ({"existing": 1826}, True, None, RESULT_SUCCESS),
        # success：全部寫入但都是無效（例如整批過期）
        ({"written_invalid": {"stale": 1826}}, True, None, RESULT_SUCCESS),
        # partial：有寫入，且有跳過
        ({"written_valid": 1825, "skipped": 1}, True, None, RESULT_PARTIAL),
        # partial：有寫入，且有單站失敗
        ({"written_valid": 1825, "failed": 1}, True, None, RESULT_PARTIAL),
        # partial：只有已存在，但有跳過
        ({"existing": 1825, "skipped": 1}, True, None, RESULT_PARTIAL),
        # failed：選到批次，但沒有任何快照保留
        ({"skipped": 3}, True, None, RESULT_FAILED),
        ({"failed": 1826}, True, None, RESULT_FAILED),
        ({}, True, None, RESULT_FAILED),
        # failed：transaction 回滾、衝突、gzip 缺損（呼叫端傳入 error）
        ({"written_valid": 1826}, True, "transaction rolled back", RESULT_FAILED),
        ({}, True, "raw file not found", RESULT_FAILED),
        ({}, False, "multiple fetch_batch_id in slot", RESULT_FAILED),
        # no_batch：槽內沒有成功批次
        ({}, False, None, RESULT_NO_BATCH),
    ],
)
def test_classify_result(kwargs, batch_found, error, expected):
    assert (
        classify_result(counts(**kwargs), batch_found=batch_found, error=error)
        == expected
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"written_valid": -1},
        {"existing": 1.0},
        {"skipped": True},
        {"failed": None},
        {"written_invalid": {"unknown_reason": 1}},
        {"written_invalid": {"stale": -1}},
    ],
)
def test_run_counts_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        RunCounts(**kwargs)


def chosen_batch(tmp_path):
    (record,) = log_of(tmp_path, [batch(at(6, 1, 500000))]).records
    return record


def validation(**kwargs) -> BatchValidation:
    values = {
        "results": (),
        "skipped": 0,
        "negative_lag": 0,
        "all_known_active_stale": False,
        "unknown_active": 0,
        "path": "fast",
    }
    values.update(kwargs)
    return BatchValidation(**values)


def test_build_report_success(tmp_path):
    record = chosen_batch(tmp_path)
    report = build_report(
        SLOT,
        counts(
            written_valid=1800,
            written_invalid={"stale": 2, "inactive": 20, "count_invalid": 1},
            skipped=0,
        ),
        batch=record,
        source=job.SOURCE_JSONL,
        validation=validation(negative_lag=3),
        log=BatchLog(records=(record,), bad_lines=1, duplicate_ids=2),
    )

    assert report == {
        "slot": "2026-11-21T08:05:00+00:00",
        "result": "success",
        "batch_id": record.batch_id,
        "fetched_at": "2026-11-21T08:06:01.500000+00:00",
        "source": "jsonl",
        "counts": {
            "written_valid": 1800,
            # 依判定順序排列，計數為 0 的原因不列出
            "written_invalid": {"inactive": 20, "stale": 2, "count_invalid": 1},
            "existing": 0,
            "skipped": 0,
            "failed": 0,
        },
        "warnings": {
            "stale_alert": None,
            "negative_lag": 3,
            "jsonl_bad_lines": 1,
            "jsonl_duplicate_ids": 2,
        },
        "error": None,
    }


def test_build_report_error_does_not_report_uncommitted_counts(tmp_path):
    record = chosen_batch(tmp_path)
    report = build_report(
        SLOT,
        counts(
            written_valid=1000,
            written_invalid={"stale": 5},
            existing=3,
            skipped=2,
            failed=1,
        ),
        batch=record,
        source=job.SOURCE_JSONL,
        error="transaction rolled back: boom",
    )

    assert report["result"] == "failed"
    assert report["counts"] == {
        "written_valid": 0,
        "written_invalid": {},
        "existing": 0,
        "skipped": 2,
        "failed": 0,
    }
    assert report["error"] == "transaction rolled back: boom"


def test_build_report_no_batch():
    report = build_report(SLOT, counts())

    assert report["result"] == "no_batch"
    assert report["batch_id"] is None
    assert report["fetched_at"] is None
    assert report["source"] is None


def test_build_report_stale_alert_with_unknown_stations(tmp_path):
    record = chosen_batch(tmp_path)
    report = build_report(
        SLOT,
        counts(written_invalid={"stale": 1820, "invalid_type": 6}),
        batch=record,
        source=job.SOURCE_JSONL,
        validation=validation(all_known_active_stale=True, unknown_active=6),
    )

    # 整批過期但所有列都已保存：result 仍為 success，附帶告警
    assert report["result"] == "success"
    assert report["warnings"]["stale_alert"] == {
        "message": "已知營運中站點全數過期（營運狀態未知 6 站）",
        "unknown_active": 6,
    }


def test_build_report_stale_alert_without_unknown_stations(tmp_path):
    report = build_report(
        SLOT,
        counts(written_invalid={"stale": 1826}),
        batch=chosen_batch(tmp_path),
        source=job.SOURCE_JSONL,
        validation=validation(all_known_active_stale=True),
    )
    assert report["warnings"]["stale_alert"]["message"] == "營運中站點全數過期"


def test_build_report_converts_slot_to_utc():
    report = build_report(SLOT.astimezone(TAIPEI), counts())
    assert report["slot"] == "2026-11-21T08:05:00+00:00"


def test_build_report_rejects_unknown_source(tmp_path):
    with pytest.raises(ValueError):
        build_report(
            SLOT, counts(existing=1), batch=chosen_batch(tmp_path), source="db"
        )


def test_format_report_is_one_line_json(tmp_path):
    report = build_report(
        SLOT,
        counts(written_invalid={"stale": 1}),
        batch=chosen_batch(tmp_path),
        source=job.SOURCE_JSONL,
        validation=validation(all_known_active_stale=True),
    )

    line = format_report(report)

    assert "\n" not in line
    assert json.loads(line) == report
    assert "營運中站點全數過期" in line  # 中文不轉成 \\u 跳脫


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: setattr(c, "written_valid", -1),
        lambda c: setattr(c, "existing", True),
        lambda c: setattr(c, "skipped", 1.5),
        lambda c: setattr(c, "failed", None),
        lambda c: c.written_invalid.update({"unknown_reason": 1}),
        lambda c: c.written_invalid.update({"stale": -2}),
        lambda c: setattr(c, "written_invalid", [("stale", 1)]),
    ],
)
def test_counts_modified_after_init_are_rejected(tmp_path, mutate):
    c = counts(written_valid=10, written_invalid={"stale": 1})
    mutate(c)

    with pytest.raises((ValueError, TypeError)):
        classify_result(c, batch_found=True)
    with pytest.raises((ValueError, TypeError)):
        build_report(SLOT, c, batch=chosen_batch(tmp_path), source=job.SOURCE_JSONL)


def test_report_does_not_share_state_with_counts(tmp_path):
    c = counts(written_valid=10, written_invalid={"stale": 1})
    report = build_report(
        SLOT, c, batch=chosen_batch(tmp_path), source=job.SOURCE_JSONL
    )

    c.written_valid = 99
    c.written_invalid["stale"] = 50

    assert report["counts"]["written_valid"] == 10
    assert report["counts"]["written_invalid"] == {"stale": 1}


def test_default_counts_are_not_shared_between_instances():
    a, b = counts(), counts()
    a.written_invalid["stale"] = 1
    assert b.written_invalid == {}


# ================================================================ 不讀取目前時間


def test_module_does_not_read_current_time():
    source = (Path(__file__).parent.parent / "src" / "snapshot_job.py").read_text(
        encoding="utf-8"
    )
    assert "now(" not in source
    assert "time.time" not in source
    assert "today(" not in source
