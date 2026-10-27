"""Batch 3：定時擷取，保存 gzip 原始檔與擷取批次紀錄。

請從專案根目錄執行：

    python src/collect.py          # 擷取一次（交給 cron 等排程器每分鐘呼叫）
    python src/collect.py --loop   # 自己每分鐘擷取一次（在本機先累積資料用）

每次擷取產生：
- 原始檔：data/raw/YYYY/MM/DD/<batch_id>.json.gz（日期為 UTC）
- 批次紀錄：data/fetch_batches.jsonl，一行一個批次

批次紀錄的欄位與設計文件的 fetch_batches 相同，
PostgreSQL 上線後再匯入資料表；在那之前先以 JSONL 暫存，
讓排程可以在資料庫建好之前就開始累積資料。

資料目錄可用環境變數 YOUBIKE_DATA_DIR 指定，預設為 data/。
"""

import argparse
import gzip
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fetch import FetchError, fetch_raw, parse_stations

DATA_DIR = Path(os.environ.get("YOUBIKE_DATA_DIR", "data"))
BATCH_LOG = "fetch_batches.jsonl"

# 與 transform.py 相同：來源時間是沒有時區資訊的台灣當地時間。
SOURCE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SOURCE_TZ = ZoneInfo("Asia/Taipei")

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


def new_batch_id(fetched_at: datetime) -> str:
    """產生擷取批次識別，例如 20261027T081500Z-3f9a1c。

    前半段是 UTC 時間，方便人工對照原始檔；
    後半段是隨機字串，避免同一秒內重試時撞號。
    """
    return f"{fetched_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def source_updated_at(stations: list[dict]) -> str | None:
    """取所有站點 srcUpdateTime 的最大值，轉成 UTC ISO 字串。

    無法解析時回傳 None，不讓這個欄位的問題擋下整批原始資料的保存；
    內容層級的驗證仍交給 transform.py。
    """
    try:
        latest = max(
            datetime.strptime(s["srcUpdateTime"], SOURCE_TIME_FORMAT).replace(
                tzinfo=SOURCE_TZ
            )
            for s in stations
        )
    except (TypeError, ValueError):
        return None
    return latest.astimezone(timezone.utc).isoformat()


def write_raw(body: bytes, path: Path) -> None:
    """以 gzip 寫入原始檔。

    先寫入暫存檔再改名，排程中途被中斷時不會留下寫到一半的 .json.gz。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, path)


def append_batch(record: dict, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / BATCH_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def collect_once(
    data_dir: Path = DATA_DIR,
    fetcher=fetch_raw,
    now=lambda: datetime.now(timezone.utc),
) -> dict:
    """擷取一次並記錄結果，回傳該批次的紀錄。

    - 下載失敗：不產生原始檔，批次標記為 failed。
    - 下載成功但內容無法使用：仍保存原始檔（方便追查上游改了什麼），
      批次標記為 failed。
    - 成功：保存原始檔，批次標記為 success。
    """
    fetched_at = now()
    batch_id = new_batch_id(fetched_at)
    record = {
        "batch_id": batch_id,
        "fetched_at": fetched_at.isoformat(),
        "source_updated_at": None,
        "status": STATUS_FAILED,
        "station_count": None,
        "raw_path": None,
        "error": None,
    }

    try:
        body = fetcher()
    except FetchError as e:
        record["error"] = str(e)
        append_batch(record, data_dir)
        return record

    relative = Path("raw") / f"{fetched_at:%Y/%m/%d}" / f"{batch_id}.json.gz"
    write_raw(body, data_dir / relative)
    record["raw_path"] = relative.as_posix()

    try:
        stations = parse_stations(body)
    except FetchError as e:
        record["error"] = str(e)
    else:
        record["status"] = STATUS_SUCCESS
        record["station_count"] = len(stations)
        record["source_updated_at"] = source_updated_at(stations)

    append_batch(record, data_dir)
    return record


def report(record: dict) -> None:
    if record["status"] == STATUS_SUCCESS:
        print(
            f"{record['batch_id']} ok  stations={record['station_count']} "
            f"source_updated_at={record['source_updated_at']}"
        )
    else:
        print(f"{record['batch_id']} FAILED  {record['error']}", file=sys.stderr)


def run_forever(data_dir: Path = DATA_DIR) -> None:
    """每到整分鐘擷取一次，直到按 Ctrl+C。

    單次失敗只記錄、不中止，下一分鐘照常擷取。
    """
    while True:
        time.sleep(60 - time.time() % 60)
        try:
            report(collect_once(data_dir))
        except OSError as e:  # 例如磁碟寫入失敗
            print(f"collect error: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="擷取 YouBike 原始資料")
    parser.add_argument("--loop", action="store_true", help="每分鐘持續擷取")
    args = parser.parse_args()

    if args.loop:
        try:
            run_forever()
        except KeyboardInterrupt:
            return 0

    record = collect_once()
    report(record)
    return 0 if record["status"] == STATUS_SUCCESS else 1


if __name__ == "__main__":
    sys.exit(main())
