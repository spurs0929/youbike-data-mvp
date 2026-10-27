import gzip
import json
import sys
from datetime import datetime, timezone
from itertools import pairwise

import pytest

from collect import BATCH_LOG, collect_once, source_updated_at
from fetch import REQUIRED_FIELDS, FetchError, parse_stations

FIXED_NOW = datetime(2026, 10, 27, 8, 15, 0, tzinfo=timezone.utc)


def station(sno="500101001", src_update="2026-10-27 16:14:30"):
    record = {field: "x" for field in REQUIRED_FIELDS}
    record.update(sno=sno, srcUpdateTime=src_update)
    return record


def body_of(stations) -> bytes:
    return json.dumps(stations, ensure_ascii=False).encode("utf-8")


def run(tmp_path, fetcher):
    return collect_once(data_dir=tmp_path, fetcher=fetcher, now=lambda: FIXED_NOW)


def read_log(tmp_path):
    lines = (tmp_path / BATCH_LOG).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_success_saves_identical_gzip_and_logs_batch(tmp_path):
    body = body_of([station("1"), station("2")])

    record = run(tmp_path, lambda: body)

    assert record["status"] == "success"
    assert record["station_count"] == 2
    assert record["raw_path"].startswith("raw/2026/10/27/20261027T081500Z-")
    with gzip.open(tmp_path / record["raw_path"], "rb") as f:
        assert f.read() == body
    assert read_log(tmp_path) == [record]


def test_fetched_at_is_utc(tmp_path):
    record = run(tmp_path, lambda: body_of([station()]))
    assert record["fetched_at"] == "2026-10-27T08:15:00+00:00"


def test_source_updated_at_is_latest_and_converted_to_utc():
    stations = [
        station(src_update="2026-10-27 16:10:00"),
        station(src_update="2026-10-27 16:14:30"),
    ]
    assert source_updated_at(stations) == "2026-10-27T08:14:30+00:00"


def test_unparseable_source_time_does_not_block_batch(tmp_path):
    record = run(tmp_path, lambda: body_of([station(src_update="bad")]))
    assert record["status"] == "success"
    assert record["source_updated_at"] is None


def test_network_failure_logs_failed_batch_without_raw_file(tmp_path):
    def broken():
        raise FetchError("Request timed out")

    record = run(tmp_path, broken)

    assert record["status"] == "failed"
    assert record["error"] == "Request timed out"
    assert record["raw_path"] is None
    assert not (tmp_path / "raw").exists()
    assert read_log(tmp_path) == [record]


@pytest.mark.parametrize("body", [b"<html>maintenance</html>", b"[]", b"{}"])
def test_unusable_body_is_still_saved_for_debugging(tmp_path, body):
    record = run(tmp_path, lambda: body)

    assert record["status"] == "failed"
    assert record["error"]
    with gzip.open(tmp_path / record["raw_path"], "rb") as f:
        assert f.read() == body


def test_batches_in_same_second_get_distinct_ids(tmp_path):
    body = body_of([station()])
    first = run(tmp_path, lambda: body)
    second = run(tmp_path, lambda: body)

    assert first["batch_id"] != second["batch_id"]
    assert [r["batch_id"] for r in read_log(tmp_path)] == [
        first["batch_id"],
        second["batch_id"],
    ]


def test_no_temp_file_left_behind(tmp_path):
    run(tmp_path, lambda: body_of([station()]))
    assert not list(tmp_path.rglob("*.tmp"))


def test_parse_stations_rejects_invalid_json():
    with pytest.raises(FetchError, match="not valid JSON"):
        parse_stations(b"not json")


# --- P2-1：gzip 寫入失敗時清理暫存檔 -------------------------------------


def test_write_raw_failure_removes_temp_and_reraises(tmp_path, monkeypatch):
    import collect

    real_open = collect.gzip.open

    def failing_open(path, mode):
        f = real_open(path, mode)
        f.write(b"partial")
        f.close()
        raise OSError("disk full")

    monkeypatch.setattr(collect.gzip, "open", failing_open)
    target = tmp_path / "raw" / "x.json.gz"

    with pytest.raises(OSError, match="disk full"):
        collect.write_raw(b"data", target)

    assert not target.exists()
    assert not list(tmp_path.rglob("*.tmp"))


def test_write_raw_rename_failure_removes_temp(tmp_path, monkeypatch):
    import collect

    def failing_replace(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(collect.os, "replace", failing_replace)
    target = tmp_path / "raw" / "x.json.gz"

    with pytest.raises(OSError, match="rename failed"):
        collect.write_raw(b"data", target)

    assert not target.exists()
    assert not list(tmp_path.rglob("*.tmp"))


def test_raw_write_failure_does_not_log_batch(tmp_path, monkeypatch):
    import collect

    def failing_write_raw(body, path):
        raise OSError("disk full")

    monkeypatch.setattr(collect, "write_raw", failing_write_raw)

    with pytest.raises(OSError, match="disk full"):
        run(tmp_path, lambda: body_of([station()]))

    assert not (tmp_path / BATCH_LOG).exists()


# --- P2-2：JSONL 寫入可靠性 ------------------------------------------------


def test_log_failure_after_raw_saved_keeps_recoverable_info(tmp_path, monkeypatch):
    import collect

    body = body_of([station("1"), station("2")])

    def failing_fsync(fd):
        raise OSError("io error")

    monkeypatch.setattr(collect.os, "fsync", failing_fsync)

    with pytest.raises(collect.BatchLogError) as excinfo:
        run(tmp_path, lambda: body)

    record = excinfo.value.record
    assert record["status"] == "success"
    # 例外訊息帶有完整紀錄，會出現在 stderr／排程日誌
    assert record["batch_id"] in str(excinfo.value)
    assert json.dumps(record, ensure_ascii=False) in str(excinfo.value)
    # 原始檔已完整落地，可從原始檔重建該批次
    raw = tmp_path / record["raw_path"]
    with gzip.open(raw, "rb") as f:
        assert f.read() == body
    assert raw.name.startswith("20261027T081500Z-")


def test_batch_log_error_is_an_oserror():
    from collect import BatchLogError

    assert issubclass(BatchLogError, OSError)


def test_failed_download_record_survives_in_error_when_log_fails(tmp_path, monkeypatch):
    import collect

    def broken():
        raise FetchError("Request timed out")

    def failing_fsync(fd):
        raise OSError("io error")

    monkeypatch.setattr(collect.os, "fsync", failing_fsync)

    with pytest.raises(collect.BatchLogError) as excinfo:
        run(tmp_path, broken)

    assert "Request timed out" in str(excinfo.value)


def test_append_after_truncated_line_starts_on_new_line(tmp_path):
    (tmp_path / BATCH_LOG).write_bytes(b'{"batch_id": "half-writ')

    record = run(tmp_path, lambda: body_of([station()]))

    lines = (tmp_path / BATCH_LOG).read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"batch_id": "half-writ'
    assert json.loads(lines[1]) == record


def test_main_reports_log_failure_and_exits_1(tmp_path, monkeypatch, capsys):
    import collect

    def failing_collect_once():
        raise collect.BatchLogError({"batch_id": "b1"}, OSError("io error"))

    monkeypatch.setattr(collect, "collect_once", failing_collect_once)
    monkeypatch.setattr(sys, "argv", ["collect.py"])

    assert collect.main() == 1
    assert '"batch_id": "b1"' in capsys.readouterr().err


# --- P2-3：來源時間防禦 ----------------------------------------------------


@pytest.mark.parametrize(
    "stations",
    [
        [],
        None,
        "not a list",
        [{}],  # 缺 srcUpdateTime
        ["not a dict"],
        [{"srcUpdateTime": None}],
        [{"srcUpdateTime": 1761552870}],
        [{"srcUpdateTime": ""}],
        [{"srcUpdateTime": "2026/10/27 16:14:30"}],
    ],
)
def test_source_updated_at_never_raises_on_bad_input(stations):
    assert source_updated_at(stations) is None


def test_source_updated_at_ignores_bad_stations_and_uses_valid_ones():
    stations = [
        {"srcUpdateTime": None},
        "not a dict",
        {},
        station(src_update="2026-10-27 16:10:00"),
        station(src_update="garbage"),
    ]
    assert source_updated_at(stations) == "2026-10-27T08:10:00+00:00"


def test_bad_source_time_keeps_raw_and_success(tmp_path):
    body = body_of([station(src_update="")])

    record = run(tmp_path, lambda: body)

    assert record["status"] == "success"
    assert record["source_updated_at"] is None
    with gzip.open(tmp_path / record["raw_path"], "rb") as f:
        assert f.read() == body


# --- P2-4：排程穩定性 ------------------------------------------------------


class FakeClock:
    def __init__(self, start):
        self.now = start
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def loop(tmp_path, clock, collect, max_runs):
    import collect as collect_module

    collect_module.run_forever(
        data_dir=tmp_path,
        collect=collect,
        clock=clock.time,
        sleep=clock.sleep,
        max_runs=max_runs,
    )


def ok_record():
    return {
        "batch_id": "b",
        "status": "success",
        "station_count": 1,
        "source_updated_at": None,
        "error": None,
    }


def test_loop_starts_on_whole_minutes(tmp_path):
    clock = FakeClock(start=1_000_000_015.0)
    starts = []

    def collect(data_dir):
        starts.append(clock.now)
        clock.now += 5
        return ok_record()

    loop(tmp_path, clock, collect, max_runs=3)

    assert all(t % 60 == 0 for t in starts)
    assert [b - a for a, b in pairwise(starts)] == [60, 60]


def test_loop_recovers_from_unexpected_errors(tmp_path, capsys):
    clock = FakeClock(start=1_000_000_000.0)
    calls = []

    def collect(data_dir):
        calls.append(clock.now)
        if len(calls) == 1:
            raise RuntimeError("unexpected bug")
        if len(calls) == 2:
            raise OSError("disk full")
        return ok_record()

    loop(tmp_path, clock, collect, max_runs=3)

    assert len(calls) == 3
    err = capsys.readouterr().err
    assert "unexpected bug" in err and "disk full" in err


def test_loop_does_not_swallow_keyboard_interrupt(tmp_path):
    clock = FakeClock(start=1_000_000_000.0)

    def collect(data_dir):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        loop(tmp_path, clock, collect, max_runs=3)


def test_slow_run_skips_missed_slots_without_overlap(tmp_path, capsys):
    clock = FakeClock(start=1_000_000_000.0)
    windows = []

    def collect(data_dir):
        start = clock.now
        clock.now += 125 if not windows else 5  # 第一次跑了 2 分 5 秒
        windows.append((start, clock.now))
        return ok_record()

    loop(tmp_path, clock, collect, max_runs=2)

    (s1, e1), (s2, _) = windows
    assert s2 >= e1  # 不重疊
    assert s2 % 60 == 0  # 仍在整分鐘啟動
    assert s2 - s1 == 180  # 中間的兩個整分鐘被跳過、不補抓
    assert "skipped 2 slot(s)" in capsys.readouterr().err


def test_seconds_until_next_slot():
    from collect import seconds_until_next_slot

    assert seconds_until_next_slot(960_000_015.0) == 45  # 960_000_000 是整分鐘
    assert seconds_until_next_slot(960_000_060.0) == 60  # 剛好整分鐘 → 等下一個


# --- 最後一輪：暫存檔清理失敗、跳過統計的邊界 ------------------------------


def test_cleanup_failure_does_not_mask_original_error(tmp_path, monkeypatch):
    import collect

    def failing_replace(src, dst):
        raise OSError("rename failed")

    def failing_unlink(self, missing_ok=False):
        raise PermissionError("cannot delete tmp")

    monkeypatch.setattr(collect.os, "replace", failing_replace)
    monkeypatch.setattr(collect.Path, "unlink", failing_unlink)

    with pytest.raises(OSError, match="rename failed"):
        collect.write_raw(b"data", tmp_path / "raw" / "x.json.gz")


class LateClock(FakeClock):
    """每次 sleep 都晚醒 delay 秒，模擬排程啟動延遲。"""

    def __init__(self, start, delay):
        super().__init__(start)
        self.delay = delay

    def sleep(self, seconds):
        super().sleep(seconds + self.delay)


def run_with(tmp_path, clock, durations):
    starts = []

    def collect(data_dir):
        starts.append(clock.now)
        clock.now += durations[len(starts) - 1]
        return ok_record()

    loop(tmp_path, clock, collect, max_runs=len(durations))
    return starts


def test_late_start_crossing_boundary_counts_as_skipped(tmp_path, capsys):
    # 晚 0.5 秒啟動、只跑 59.7 秒（不到一分鐘），但已跨過下一個整分鐘
    clock = LateClock(start=960_000_010.0, delay=0.5)

    starts = run_with(tmp_path, clock, durations=[59.7, 1])

    assert starts[1] - starts[0] == 120
    assert "skipped 1 slot(s)" in capsys.readouterr().err


def test_late_start_without_crossing_boundary_skips_nothing(tmp_path, capsys):
    clock = LateClock(start=960_000_010.0, delay=0.5)

    starts = run_with(tmp_path, clock, durations=[59.4, 1])

    assert starts[1] - starts[0] == 60
    assert "skipped" not in capsys.readouterr().err


def test_ending_exactly_on_boundary_counts_as_skipped(tmp_path, capsys):
    clock = FakeClock(start=960_000_010.0)

    starts = run_with(tmp_path, clock, durations=[60, 1])

    assert starts[1] - starts[0] == 120
    assert "skipped 1 slot(s)" in capsys.readouterr().err
