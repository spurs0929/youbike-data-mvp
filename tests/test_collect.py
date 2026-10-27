import gzip
import json
from datetime import datetime, timezone

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
