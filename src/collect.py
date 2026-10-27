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

狀態語意（status）：
- success：下載成功，且回應通過 fetch.validate_shape 的基本結構驗證
  （最外層為非空 list、每筆為 dict、必要欄位存在）。
  不代表 Pandas 清洗（transform.py）成功，也不代表已寫入資料庫；
  那些步驟屬於之後的批次，會有自己的狀態。
- failed：下載失敗，或回應無法通過上述驗證。

可復原性：
- 原始檔是事實來源，JSONL 是索引。
- 原始檔檔名即 batch_id，前半段是 fetched_at（UTC，精確到秒）；
  其餘欄位（station_count、source_updated_at、status）都可由原始檔重新算出。
  因此 JSONL 寫入失敗時，只要原始檔在，該批次就能從原始檔重建。
- 下載失敗的批次沒有原始檔，只存在於 JSONL；這類紀錄若寫入失敗，
  會完整輸出到 stderr，由排程器的日誌保留。

資料目錄可用環境變數 YOUBIKE_DATA_DIR 指定，預設為 data/。
"""

import argparse
import gzip
import json
import os
import sys
import time
import traceback
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

# 語意見模組說明：success 只代表下載與基本結構驗證成功。
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

INTERVAL_SECONDS = 60


class BatchLogError(OSError):
    """批次紀錄無法寫入 JSONL。

    繼承 OSError，讓既有「磁碟寫入失敗」的處理路徑一併接住。
    例外訊息包含完整紀錄（JSON），確保紀錄至少會出現在 stderr／排程日誌中。
    """

    def __init__(self, record: dict, cause: OSError):
        self.record = record
        super().__init__(
            f"Failed to append batch record ({cause}); record: "
            + json.dumps(record, ensure_ascii=False)
        )


def new_batch_id(fetched_at: datetime) -> str:
    """產生擷取批次識別，例如 20261027T081500Z-3f9a1c。

    前半段是 UTC 時間，方便人工對照原始檔；
    後半段是隨機字串，避免同一秒內重試時撞號。
    """
    return f"{fetched_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def _parse_source_time(record) -> datetime | None:
    """解析單一站點的 srcUpdateTime；任何異常都回傳 None，不拋出例外。"""
    if not isinstance(record, dict):
        return None
    value = record.get("srcUpdateTime")
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), SOURCE_TIME_FORMAT).replace(
            tzinfo=SOURCE_TZ
        )
    except ValueError:
        return None


def source_updated_at(stations) -> str | None:
    """取各站點 srcUpdateTime 中可解析者的最大值，轉成 UTC ISO 字串。

    正常流程中 parse_stations 已保證欄位存在，這裡的防禦是給：
    - 欄位值為空字串、None、數字或格式錯誤
    - 直接呼叫此函式、未經 parse_stations 驗證的資料
      （空 list、缺欄位、元素不是 dict、甚至不是 list）

    無法解析的站點會被略過；全部無法解析時回傳 None。
    此函式永不拋出例外，來源時間異常不應影響原始資料的保存，
    內容層級的驗證仍交給 transform.py。
    """
    if not isinstance(stations, list):
        return None
    times = [t for t in map(_parse_source_time, stations) if t is not None]
    if not times:
        return None
    return max(times).astimezone(timezone.utc).isoformat()


def write_raw(body: bytes, path: Path) -> None:
    """以 gzip 寫入原始檔。

    先寫入暫存檔再改名（os.replace 為原子操作），
    排程中途被中斷時不會留下寫到一半的 .json.gz。
    寫入或改名失敗時刪除暫存檔，再把原本的例外往外拋。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with gzip.open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
    except BaseException:
        # BaseException 也包含 KeyboardInterrupt，Ctrl+C 時同樣要清掉暫存檔。
        tmp.unlink(missing_ok=True)
        raise


def append_batch(record: dict, data_dir: Path) -> None:
    """將一筆批次紀錄附加到 JSONL。

    - 整行以單次 write 寫入，並 fsync，避免機器斷電時遺失已回報成功的紀錄。
    - 若上一行因先前的失敗沒有以換行結尾，先補一個換行，
      讓不完整的那一行與新紀錄分開，之後讀取時只需略過那一行。
    - 任何寫入失敗都轉為 BatchLogError，訊息中帶有完整紀錄。
    """
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with open(data_dir / BATCH_LOG, "ab+") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    line = b"\n" + line
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        raise BatchLogError(record, e) from e


def collect_once(
    data_dir: Path = DATA_DIR,
    fetcher=fetch_raw,
    now=lambda: datetime.now(timezone.utc),
) -> dict:
    """擷取一次並記錄結果，回傳該批次的紀錄。

    - 下載失敗：不產生原始檔，批次標記為 failed。
    - 下載成功但內容無法使用：仍保存原始檔（方便追查上游改了什麼），
      批次標記為 failed。
    - 成功：保存原始檔，批次標記為 success（語意見模組說明）。

    原始檔寫入失敗會拋出 OSError；批次紀錄寫入失敗會拋出 BatchLogError，
    此時原始檔（若有）已完整落地，可依模組說明復原。
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


def seconds_until_next_slot(now: float, interval: int = INTERVAL_SECONDS) -> float:
    """距離下一個整分鐘還有幾秒；剛好在整分鐘時回傳一整個間隔。"""
    return interval - now % interval


def run_forever(
    data_dir: Path = DATA_DIR,
    collect=collect_once,
    clock=time.time,
    sleep=time.sleep,
    max_runs: int | None = None,
) -> None:
    """每到整分鐘擷取一次，直到按 Ctrl+C。

    執行規則：
    - 只在整分鐘啟動，同一時間只有一次擷取在執行（不重疊）。
    - 單次執行超過一分鐘時，執行期間經過的整分鐘直接跳過、不補抓，
      並在 stderr 記錄跳過幾次；結束後等到下一個整分鐘再開始。
    - 單次執行拋出任何 Exception（包含非預期的程式錯誤）都只記錄
      traceback，下一分鐘照常擷取；KeyboardInterrupt 會往外傳以便結束。

    collect、clock、sleep、max_runs 只供測試替換使用。
    """
    runs = 0
    while max_runs is None or runs < max_runs:
        sleep(seconds_until_next_slot(clock()))
        started = clock()
        try:
            report(collect(data_dir))
        except Exception:  # noqa: BLE001 — 刻意接住所有錯誤，見 docstring
            print("collect error:", file=sys.stderr)
            traceback.print_exc()
        runs += 1

        skipped = int((clock() - started) // INTERVAL_SECONDS)
        if skipped:
            print(
                f"collect took longer than {INTERVAL_SECONDS}s; "
                f"skipped {skipped} slot(s)",
                file=sys.stderr,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="擷取 YouBike 原始資料")
    parser.add_argument("--loop", action="store_true", help="每分鐘持續擷取")
    args = parser.parse_args()

    if args.loop:
        try:
            run_forever()
        except KeyboardInterrupt:
            return 0

    try:
        record = collect_once()
    except OSError as e:  # 原始檔或批次紀錄寫入失敗；BatchLogError 會帶出完整紀錄
        print(f"collect error: {e}", file=sys.stderr)
        return 1
    report(record)
    return 0 if record["status"] == STATUS_SUCCESS else 1


if __name__ == "__main__":
    sys.exit(main())
