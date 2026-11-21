"""deploy/check_health.py 的單元測試：假 JSONL、假時間、假磁碟使用率，不連網。"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# deploy/ 不在 pytest 的 pythonpath（只有 src/），以檔案路徑載入。
_PATH = Path(__file__).resolve().parent.parent / "deploy" / "check_health.py"
_spec = importlib.util.spec_from_file_location("check_health", _PATH)
check_health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_health)

NOW = datetime(2026, 11, 21, 6, 0, 0, tzinfo=timezone.utc)
MAX_AGE = timedelta(minutes=10)


def record(minutes_ago, status="success"):
    fetched_at = NOW - timedelta(minutes=minutes_ago)
    return {
        "batch_id": f"{fetched_at:%Y%m%dT%H%M%SZ}-abcdef",
        "fetched_at": fetched_at.isoformat(),
        "status": status,
    }


def write_log(data_dir, records, tail=""):
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    text = "".join(line + "\n" for line in lines) + tail
    (data_dir / check_health.BATCH_LOG).write_text(text, encoding="utf-8")


def run(data_dir, disk=16.0, threshold=80.0):
    return check_health.check(
        data_dir=data_dir,
        now=NOW,
        max_age=MAX_AGE,
        disk_percent=disk,
        disk_threshold=threshold,
    )


def test_recent_success_is_ok(tmp_path):
    write_log(tmp_path, [record(3), record(2), record(1)])
    ok, line = run(tmp_path)
    assert ok
    assert line.startswith("2026-11-21T06:00:00Z OK")
    assert "age=1.0m" in line
    assert "disk=16%" in line


def test_last_success_older_than_max_age_warns(tmp_path):
    write_log(tmp_path, [record(12)])
    ok, line = run(tmp_path)
    assert not ok
    assert " WARN " in line
    assert "stale: last success 12.0m ago" in line


def test_exactly_max_age_is_still_ok(tmp_path):
    write_log(tmp_path, [record(10)])
    ok, _ = run(tmp_path)
    assert ok


def test_failed_records_after_success_are_ignored(tmp_path):
    # 最後幾筆都是 failed：以最後一筆 success 為準，因此 12 分鐘前 → 警告。
    write_log(tmp_path, [record(12), record(2, "failed"), record(1, "failed")])
    ok, line = run(tmp_path)
    assert not ok
    assert "age=12.0m" in line


def test_no_success_record_warns(tmp_path):
    write_log(tmp_path, [record(2, "failed"), record(1, "failed")])
    ok, line = run(tmp_path)
    assert not ok
    assert "last_success=none" in line
    assert "no_success" in line


def test_missing_batch_log_warns(tmp_path):
    ok, line = run(tmp_path)
    assert not ok
    assert "missing:" in line


def test_partial_and_malformed_lines_are_skipped(tmp_path):
    # 最後一行寫到一半（沒有換行），中間夾一行不是 JSON、一行缺 fetched_at。
    no_time = {"status": "success"}
    write_log(
        tmp_path, [record(1)], tail="not json\n" + json.dumps(no_time) + '\n{"stat'
    )
    ok, line = run(tmp_path)
    assert ok
    assert "age=1.0m" in line


def test_disk_at_threshold_warns(tmp_path):
    write_log(tmp_path, [record(1)])
    ok, line = run(tmp_path, disk=80.0)
    assert not ok
    assert "disk: usage 80% >= 80%" in line


def test_disk_below_threshold_is_ok(tmp_path):
    write_log(tmp_path, [record(1)])
    ok, _ = run(tmp_path, disk=79.4)
    assert ok


def test_both_warnings_are_reported(tmp_path):
    write_log(tmp_path, [record(30)])
    ok, line = run(tmp_path, disk=95.0)
    assert not ok
    assert "stale:" in line and "disk:" in line


def test_check_does_not_modify_data_dir(tmp_path):
    write_log(tmp_path, [record(1)])
    log = tmp_path / check_health.BATCH_LOG
    before = log.read_bytes()
    run(tmp_path)
    assert log.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [check_health.BATCH_LOG]


@pytest.mark.parametrize(
    ("disk_threshold", "expected_code"),
    [("100", 0), ("0", 1)],
)
def test_main_exit_code(tmp_path, capsys, disk_threshold, expected_code):
    write_log(
        tmp_path,
        [{"fetched_at": datetime.now(timezone.utc).isoformat(), "status": "success"}],
    )
    code = check_health.main(
        ["--data-dir", str(tmp_path), "--disk-threshold", disk_threshold]
    )
    assert code == expected_code
    out = capsys.readouterr()
    assert (out.out if expected_code == 0 else out.err).strip()
