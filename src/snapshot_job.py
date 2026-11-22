"""Batch 4C-3：五分鐘快照寫入作業。

用法（CLI）::

    YOUBIKE_DATABASE_URL=postgresql://user@host:5432/db \
    YOUBIKE_DATA_DIR=/home/youbike/data \
    python src/snapshot_job.py [--slot 2026-11-21T08:05:00Z]

- 不指定 --slot：處理最近一個已結束的槽（排程用）。
- --slot：手動補跑指定的槽；必須已結束，且槽起點不早於目前時間 7 天前（定版 4.2）。
- 密碼不放進連線字串，以 PGPASSWORD 或 pgpass 檔提供。
- 每次執行輸出一行 JSON 到 stdout。結束碼：success／partial／no_batch 為 0，
  failed 為 1，參數錯誤為 2（argparse）。

組成：

- 4C-3a（純函式，不連資料庫）：時間槽、讀取 fetch_batches.jsonl 與挑選批次、
  在資料目錄內讀取 gzip、依計數分類結果並組成一行 JSON。
- 4C-3b：run_slot() 在單一 transaction 內取得同槽鎖、決定來源批次、
  逐站以 savepoint 寫入 stations 與 station_snapshots；以及 CLI 入口 main()。

規格見定版 4.4.1、4.4.5、4.4.6 與 docs/schema.md 第三、六節。

除了 CLI 入口 main() 之外，本模組不讀取目前時間：需要「現在」的函式
一律由呼叫端傳入，相同輸入一定得到相同結果，方便重現問題與測試邊界。
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import traceback
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.pq import TransactionStatus

from collect import BATCH_LOG, DATA_DIR
from fetch import FetchError, parse_stations
from snapshot import REASON_ORDER, BatchValidation, StationResult, validate_batch

SLOT_LENGTH = timedelta(minutes=5)

# 解壓後原始檔的上限。實測每批約 0.93 MB（docs/progress.md），保留約 20 倍餘裕；
# 超過時視為損壞，避免異常檔案（例如壓縮炸彈）耗盡 e2-micro 的 1 GB 記憶體。
MAX_RAW_BYTES = 20 * 1024 * 1024

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

RESULT_SUCCESS = "success"
RESULT_PARTIAL = "partial"
RESULT_FAILED = "failed"
RESULT_NO_BATCH = "no_batch"

# 來源批次從哪裡決定：零快照的槽從 JSONL 挑；已有快照的槽沿用原批次（4C-3b）。
SOURCE_JSONL = "jsonl"
SOURCE_EXISTING = "existing"


# ---------------------------------------------------------------- 時間


def _to_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_aware(text: str) -> datetime:
    """解析帶時區的 ISO 8601 字串並轉成 UTC。

    Python 3.10 的 fromisoformat 不接受結尾的 "Z"，先換成 "+00:00"。
    不是字串時拋出 TypeError，沒有時區或格式錯誤時拋出 ValueError。
    """
    if not isinstance(text, str):
        raise TypeError("expected an ISO 8601 string")
    text = text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return _to_utc(datetime.fromisoformat(text), "time")


def slot_of(moment: datetime) -> datetime:
    """moment 所屬的時間槽起點：UTC 五分鐘半開區間 [T, T+5 分)。"""
    utc = _to_utc(moment, "moment")
    return utc.replace(minute=utc.minute - utc.minute % 5, second=0, microsecond=0)


def is_slot_start(moment: datetime) -> bool:
    """轉成 UTC 後是否剛好對齊五分鐘。"""
    utc = _to_utc(moment, "moment")
    return slot_of(utc) == utc


def latest_closed_slot(now: datetime) -> datetime:
    """最近一個已結束的槽。now 由呼叫端提供。

    例：now = 08:10:00 時，目前的槽 [08:10, 08:15) 剛開始，
    最近一個已結束的是 [08:05, 08:10)，回傳 08:05。
    """
    return slot_of(now) - SLOT_LENGTH


def parse_slot(text: str) -> datetime:
    """解析 `--slot` 參數，回傳 UTC 的槽起點。

    必須帶時區（例如 2026-11-21T08:05:00Z 或 2026-11-21T16:05:00+08:00）；
    先轉成 UTC，再檢查是否對齊五分鐘。不符合時拋出 ValueError。
    """
    try:
        slot = _parse_aware(text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"--slot 需要帶時區的 ISO 8601 時間：{text!r}") from exc
    if not is_slot_start(slot):
        raise ValueError(
            f"--slot 轉成 UTC 後未對齊五分鐘：{text!r} → {slot.isoformat()}"
        )
    return slot


# ---------------------------------------------------------------- 批次紀錄


@dataclass(frozen=True)
class BatchRecord:
    """fetch_batches.jsonl 的一行；欄位與 fetch_batches 資料表相同。

    讀取時已依資料表的 CHECK 約束檢查過，4C-3b 寫入 fetch_batches 時不會被拒絕。
    """

    batch_id: str
    fetched_at: datetime
    source_updated_at: datetime | None
    status: str
    station_count: int | None
    raw_path: str | None
    error: str | None

    @property
    def slot(self) -> datetime:
        return slot_of(self.fetched_at)


@dataclass(frozen=True)
class BatchLog:
    """讀取結果。records 依檔案順序、每個 batch_id 只留第一筆。

    - bad_lines：無法解析或欄位不合法而略過的行（不含空行）。
    - duplicate_ids：與前面某筆有效紀錄 batch_id 相同而略過的行。

    建立時檢查 batch_id 不重複：select_batch 只接受 BatchLog，
    因此挑選批次時一定是去重後的資料。「保留第一筆」由 read_batch_log 負責。
    """

    records: tuple[BatchRecord, ...]
    bad_lines: int
    duplicate_ids: int

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple) or not all(
            isinstance(r, BatchRecord) for r in self.records
        ):
            raise TypeError("records must be a tuple of BatchRecord")
        ids = [r.batch_id for r in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("BatchLog.records contains duplicate batch_id")


def _optional_str(value) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("expected a string or null")


def parse_batch_line(line: str) -> BatchRecord:
    """解析並檢查 JSONL 的一行；不合法時拋出 ValueError 或 TypeError。"""
    data = json.loads(line)
    if not isinstance(data, dict):
        raise TypeError("record is not an object")

    batch_id = data.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("batch_id must be a non-empty string")

    status = data.get("status")
    if status not in (STATUS_SUCCESS, STATUS_FAILED):
        raise ValueError(f"unknown status: {status!r}")

    fetched_at = _parse_aware(data.get("fetched_at"))
    source = data.get("source_updated_at")
    source_updated_at = None if source is None else _parse_aware(source)

    station_count = data.get("station_count")
    if station_count is not None and (
        isinstance(station_count, bool) or not isinstance(station_count, int)
    ):
        raise ValueError("station_count must be an integer or null")
    raw_path = _optional_str(data.get("raw_path"))
    error = _optional_str(data.get("error"))

    # 與 migrations/001_init.sql 的 fetch_batches CHECK 約束一致。
    if status == STATUS_SUCCESS and not (
        station_count is not None
        and station_count > 0
        and raw_path is not None
        and error is None
    ):
        raise ValueError("success record violates fetch_batches_success_chk")
    if status == STATUS_FAILED and not (
        error is not None and station_count is None and source_updated_at is None
    ):
        raise ValueError("failed record violates fetch_batches_failed_chk")

    return BatchRecord(
        batch_id=batch_id,
        fetched_at=fetched_at,
        source_updated_at=source_updated_at,
        status=status,
        station_count=station_count,
        raw_path=raw_path,
        error=error,
    )


def read_batch_log(path: Path) -> BatchLog:
    """讀取 fetch_batches.jsonl。只讀不寫。

    - 寫到一半的行（collect.append_batch 失敗時可能留下）、無法解析或欄位
      不合法的行，略過並計入 bad_lines，不讓整份紀錄無法使用。
    - 同一個 batch_id 出現多次時保留第一筆，其餘計入 duplicate_ids。
      去重在挑選批次之前完成，後面的重複紀錄不會覆蓋第一筆的狀態。
    - 檔案不存在時拋出 FileNotFoundError：通常是資料目錄設錯，
      不能當成「沒有批次」回報 no_batch。
    """
    records: list[BatchRecord] = []
    seen: set[str] = set()
    bad_lines = 0
    duplicate_ids = 0
    with open(path, "rb") as f:
        for raw_line in f:
            if not raw_line.strip():
                continue
            try:
                record = parse_batch_line(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, TypeError):
                bad_lines += 1
                continue
            if record.batch_id in seen:
                duplicate_ids += 1
                continue
            seen.add(record.batch_id)
            records.append(record)
    return BatchLog(
        records=tuple(records), bad_lines=bad_lines, duplicate_ids=duplicate_ids
    )


def select_batch(log: BatchLog, slot: datetime) -> BatchRecord | None:
    """零快照的槽：挑槽內 status = success 中 fetched_at 最新者。

    fetched_at 相同時依 batch_id 排序取最後一個。槽內沒有成功批次時回傳 None
    （之後回報 no_batch），不借用相鄰槽的批次。
    只接受 read_batch_log 回傳的 BatchLog（已去重），不接受任意 list，
    避免呼叫端略過去重，讓後面的重複紀錄覆蓋第一筆。
    """
    if not isinstance(log, BatchLog):
        raise TypeError("select_batch requires a BatchLog from read_batch_log")
    slot = _to_utc(slot, "slot")
    if not is_slot_start(slot):
        raise ValueError(f"slot is not aligned to 5 minutes: {slot.isoformat()}")
    candidates = [
        r for r in log.records if r.status == STATUS_SUCCESS and r.slot == slot
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.fetched_at, r.batch_id))


# ---------------------------------------------------------------- 原始檔


class RawBatchError(Exception):
    """原始檔缺失、損壞、超出大小上限、路徑不合法，或內容無法通過結構驗證。

    依定版 4.4.1，這些情況該槽的結果都是 failed。
    """


def resolve_raw_path(data_dir: Path, raw_path: str) -> Path:
    """把 raw_path 轉成資料目錄內的實際路徑。

    raw_path 是相對於資料目錄的 POSIX 路徑（collect.py 的格式）。
    絕對路徑、含 `..` 而跑出資料目錄、或經由符號連結指到目錄外，
    一律拋出 RawBatchError。

    已知限制（檢查後替換）：檢查路徑與實際開檔之間，若有人把檔案換成指向
    目錄外的符號連結，仍可能讀到外部檔案。能這麼做的人必須已經可以寫入資料目錄；
    VM 上資料目錄權限為 700、只有專用的 youbike 使用者可寫，且讀到的內容
    仍須通過 parse_stations 與解壓上限，因此不另外以 O_NOFOLLOW 等方式處理。
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise RawBatchError("raw_path is empty")
    base = Path(data_dir).resolve()
    candidate = (base / raw_path).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise RawBatchError(f"raw_path escapes the data directory: {raw_path!r}")
    return candidate


def load_raw_stations(
    data_dir: Path, raw_path: str, max_bytes: int = MAX_RAW_BYTES
) -> list[dict]:
    """讀取並解析一批原始檔，回傳站點 list。只讀不寫。

    解壓後超過 max_bytes 即停止讀取，不把整個檔案載入記憶體。
    任何讀取或解析失敗都轉成 RawBatchError。
    """
    path = resolve_raw_path(data_dir, raw_path)
    try:
        with gzip.open(path, "rb") as f:
            body = f.read(max_bytes + 1)
    except FileNotFoundError as exc:
        raise RawBatchError(f"raw file not found: {raw_path}") from exc
    except (OSError, EOFError, zlib.error) as exc:
        # gzip.BadGzipFile 是 OSError 的子類別；檔案被截斷時拋 EOFError。
        raise RawBatchError(f"raw file is corrupt: {raw_path} ({exc})") from exc
    if len(body) > max_bytes:
        raise RawBatchError(f"raw file exceeds {max_bytes} bytes: {raw_path}")
    try:
        return parse_stations(body)
    except FetchError as exc:
        raise RawBatchError(f"raw file failed validation: {raw_path} ({exc})") from exc


# ---------------------------------------------------------------- 處理結果


@dataclass
class RunCounts:
    """定版 4.4.6 的五項計數。只計已 commit 的結果，由 4C-3b 填入。

    - written_valid：本次寫入且 is_valid = true。
    - written_invalid：本次寫入且 is_valid = false，依原因代碼分列。
    - existing：快照已存在而略過（重跑），不算跳過。
    - skipped：無法寫入的資料，例如 sno 為 NULL 或空白。
    - failed：savepoint 回滾的站點。
    """

    written_valid: int = 0
    written_invalid: dict[str, int] = field(default_factory=dict)
    existing: int = 0
    skipped: int = 0
    failed: int = 0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """檢查所有計數（型別錯誤拋 TypeError，數值不合法拋 ValueError）。

        初始化時檢查一次，分類與輸出報告前再檢查一次，
        避免建立後被改成負數、bool 或未知原因代碼的值進入報告。"""
        if not isinstance(self.written_invalid, dict):
            raise TypeError("written_invalid must be a dict")
        for name in ("written_valid", "existing", "skipped", "failed"):
            _check_count(name, getattr(self, name))
        for reason, count in self.written_invalid.items():
            if reason not in REASON_ORDER:
                raise ValueError(f"unknown invalid_reason: {reason!r}")
            _check_count(f"written_invalid[{reason}]", count)

    @property
    def written(self) -> int:
        return self.written_valid + sum(self.written_invalid.values())

    @property
    def kept(self) -> int:
        """該槽目前保留的快照數：本次寫入加上原本已存在。"""
        return self.written + self.existing


def _check_count(name: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def classify_result(
    counts: RunCounts, *, batch_found: bool, error: str | None = None
) -> str:
    """依定版 4.4.6 判定 result。

    依序判斷：

    1. error 不是 None → failed。代表整筆 transaction 回滾、同槽多批次衝突、
       或 gzip 缺失／損壞（呼叫端負責在這些情況傳入 error）。
    2. 沒有選到批次 → no_batch。
    3. 選到批次但沒有任何快照保留（寫入與已存在都是 0）→ failed。
    4. 有跳過或失敗 → partial（此時必定至少一站已寫入或已存在）。
    5. 其餘 → success。純重跑、全部已存在也算 success。

    判斷前重新檢查計數；不合法時拋出 ValueError。
    """
    counts.validate()
    if error is not None:
        return RESULT_FAILED
    if not batch_found:
        return RESULT_NO_BATCH
    if counts.kept == 0:
        return RESULT_FAILED
    if counts.skipped or counts.failed:
        return RESULT_PARTIAL
    return RESULT_SUCCESS


def stale_alert(validation: BatchValidation | None) -> dict | None:
    """整批過期告警：只在已知營運中站點全數過期時產生一次。"""
    if validation is None or not validation.all_known_active_stale:
        return None
    unknown = validation.unknown_active
    if unknown:
        message = f"已知營運中站點全數過期（營運狀態未知 {unknown} 站）"
    else:
        message = "營運中站點全數過期"
    return {"message": message, "unknown_active": unknown}


def build_report(
    slot: datetime,
    counts: RunCounts,
    *,
    batch: BatchRecord | None = None,
    source: str | None = None,
    validation: BatchValidation | None = None,
    log: BatchLog | None = None,
    error: str | None = None,
) -> dict:
    """組成一次執行的處理結果（定版 4.4.6）。

    有 error 時代表 transaction 已回滾或根本沒有寫入：寫入、已存在、失敗
    一律回報 0，不把暫存計數報成已寫入；跳過數來自寫入前的驗證，照實回報。
    """
    slot = _to_utc(slot, "slot")
    if source not in (None, SOURCE_JSONL, SOURCE_EXISTING):
        raise ValueError(f"unknown source: {source!r}")

    result = classify_result(counts, batch_found=batch is not None, error=error)
    if error is not None:
        counts = RunCounts(skipped=counts.skipped)

    return {
        "slot": slot.isoformat(),
        "result": result,
        "batch_id": batch.batch_id if batch else None,
        "fetched_at": batch.fetched_at.isoformat() if batch else None,
        "source": source,
        "counts": {
            "written_valid": counts.written_valid,
            "written_invalid": {
                reason: counts.written_invalid[reason]
                for reason in REASON_ORDER
                if counts.written_invalid.get(reason)
            },
            "existing": counts.existing,
            "skipped": counts.skipped,
            "failed": counts.failed,
        },
        "warnings": {
            "stale_alert": stale_alert(validation),
            "negative_lag": validation.negative_lag if validation else 0,
            "jsonl_bad_lines": log.bad_lines if log else 0,
            "jsonl_duplicate_ids": log.duplicate_ids if log else 0,
        },
        "error": error,
    }


def format_report(report: dict) -> str:
    """處理結果轉成一行 JSON（cron 導向日誌檔）。"""
    return json.dumps(report, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- 資料庫寫入（4C-3b）

DATABASE_URL_ENV = "YOUBIKE_DATABASE_URL"

# pg_advisory_xact_lock(LOCK_NAMESPACE, slot_lock_key(slot))。
# 第一個鍵固定，與日後其他用途的 advisory lock 區分；第二個鍵每個槽不同。
LOCK_NAMESPACE = 4031

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class SlotConflictError(Exception):
    """同一個槽已有多個不同的 fetch_batch_id，或沿用的原批次資料不完整。"""


def slot_lock_key(slot: datetime) -> int:
    """同槽鎖的第二個鍵：slot 的 UNIX 秒數 // 300（每個槽一個整數）。"""
    slot = _to_utc(slot, "slot")
    if not is_slot_start(slot):
        raise ValueError(f"slot is not aligned to 5 minutes: {slot.isoformat()}")
    return (slot - _EPOCH) // SLOT_LENGTH


def acquire_slot_lock(conn, slot: datetime) -> None:
    """在目前的 transaction 內取得同槽鎖；transaction 結束時自動釋放。

    其他連線處理同一個槽時會在這裡等待，不同槽互不影響。
    """
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, slot_lock_key(slot))
    )


def _slot_batch_ids(conn, slot: datetime) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT fetch_batch_id FROM station_snapshots"
        " WHERE slot_time = %s ORDER BY 1",
        (slot,),
    ).fetchall()
    return [row[0] for row in rows]


def _batch_by_pk(conn, pk: int) -> BatchRecord:
    row = conn.execute(
        "SELECT batch_id, fetched_at, source_updated_at, status, station_count,"
        " raw_path, error FROM fetch_batches WHERE id = %s",
        (pk,),
    ).fetchone()
    if row is None:  # 外鍵保證存在；保險起見仍檢查
        raise SlotConflictError(f"fetch_batches id {pk} not found")
    batch_id, fetched_at, source_updated_at, status, count, raw_path, error = row
    if status != STATUS_SUCCESS or raw_path is None:
        raise SlotConflictError(f"existing batch {batch_id} is not a usable success")
    return BatchRecord(
        batch_id=batch_id,
        fetched_at=fetched_at.astimezone(timezone.utc),
        source_updated_at=(
            None
            if source_updated_at is None
            else source_updated_at.astimezone(timezone.utc)
        ),
        status=status,
        station_count=count,
        raw_path=raw_path,
        error=error,
    )


def _ensure_batch_row(conn, batch: BatchRecord) -> int:
    """確保 fetch_batches 有這一批並回傳 id（docs/schema.md 第六節步驟 5）。"""
    row = conn.execute(
        """
        INSERT INTO fetch_batches
          (batch_id, fetched_at, source_updated_at, status,
           station_count, raw_path, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (batch_id) DO NOTHING
        RETURNING id
        """,
        (
            batch.batch_id,
            batch.fetched_at,
            batch.source_updated_at,
            batch.status,
            batch.station_count,
            batch.raw_path,
            batch.error,
        ),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM fetch_batches WHERE batch_id = %s", (batch.batch_id,)
        ).fetchone()
    return row[0]


def _existing_station_ids(conn, slot: datetime) -> set[str]:
    rows = conn.execute(
        "SELECT station_id FROM station_snapshots WHERE slot_time = %s", (slot,)
    ).fetchall()
    return {row[0] for row in rows}


# 「只用不早於該站最近快照槽的非 NULL 值覆寫」由單一 INSERT ... ON CONFLICT
# DO UPDATE 完成：比較與更新在同一個敘述內、對該列加鎖後進行，
# 不同槽同時更新同一站時，後到者會等前者 commit，再以最新的 last_seen_at 判斷。
# SET 中的 stations.* 都是更新前的舊值。
_UPSERT_STATION = """
INSERT INTO stations
  (station_id, station_name, district, address, latitude, longitude,
   first_seen_at, last_seen_at)
VALUES (%(station_id)s, %(station_name)s, %(district)s, %(address)s,
        %(latitude)s, %(longitude)s, %(slot)s, %(slot)s)
ON CONFLICT (station_id) DO UPDATE SET
  station_name = CASE WHEN EXCLUDED.last_seen_at >= stations.last_seen_at
                      THEN COALESCE(EXCLUDED.station_name, stations.station_name)
                      ELSE stations.station_name END,
  district     = CASE WHEN EXCLUDED.last_seen_at >= stations.last_seen_at
                      THEN COALESCE(EXCLUDED.district, stations.district)
                      ELSE stations.district END,
  address      = CASE WHEN EXCLUDED.last_seen_at >= stations.last_seen_at
                      THEN COALESCE(EXCLUDED.address, stations.address)
                      ELSE stations.address END,
  latitude     = CASE WHEN EXCLUDED.last_seen_at >= stations.last_seen_at
                      THEN COALESCE(EXCLUDED.latitude, stations.latitude)
                      ELSE stations.latitude END,
  longitude    = CASE WHEN EXCLUDED.last_seen_at >= stations.last_seen_at
                      THEN COALESCE(EXCLUDED.longitude, stations.longitude)
                      ELSE stations.longitude END,
  first_seen_at = LEAST(stations.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = GREATEST(stations.last_seen_at, EXCLUDED.last_seen_at)
"""

_INSERT_SNAPSHOT = """
INSERT INTO station_snapshots
  (station_id, slot_time, fetch_batch_id, info_time, is_active,
   total_spaces, available_bikes, available_spaces, is_valid, invalid_reason)
VALUES (%(station_id)s, %(slot)s, %(fetch_batch_id)s, %(info_time)s, %(is_active)s,
        %(total_spaces)s, %(available_bikes)s, %(available_spaces)s,
        %(is_valid)s, %(invalid_reason)s)
ON CONFLICT (station_id, slot_time) DO NOTHING
RETURNING 1
"""


def _station_params(result: StationResult, slot: datetime) -> dict:
    info = result.info
    return {
        "station_id": result.station_id,
        "station_name": info.station_name if info else None,
        "district": info.district if info else None,
        "address": info.address if info else None,
        "latitude": info.latitude if info else None,
        "longitude": info.longitude if info else None,
        "slot": slot,
    }


def _snapshot_params(result: StationResult, slot: datetime, batch_pk: int) -> dict:
    return {
        "station_id": result.station_id,
        "slot": slot,
        "fetch_batch_id": batch_pk,
        "info_time": result.info_time,
        "is_active": result.is_active,
        "total_spaces": result.total_spaces,
        "available_bikes": result.available_bikes,
        "available_spaces": result.available_spaces,
        "is_valid": result.is_valid,
        "invalid_reason": result.invalid_reason,
    }


class _SnapshotExists(Exception):
    """快照已存在：用來回滾該站的 savepoint，撤銷同一 savepoint 內的 stations 更新。"""


def _write_station(conn, result: StationResult, slot: datetime, batch_pk: int) -> bool:
    """在一個 savepoint 內寫入一站：先 upsert stations（外鍵需要），再寫快照。

    快照已存在時拋出 _SnapshotExists，savepoint 回滾，stations 不留下任何更新。
    資料庫錯誤（psycopg.Error）同樣回滾 savepoint 後往外拋，由呼叫端計為失敗。
    """
    with conn.transaction():
        conn.execute(_UPSERT_STATION, _station_params(result, slot))
        inserted = conn.execute(
            _INSERT_SNAPSHOT, _snapshot_params(result, slot, batch_pk)
        ).fetchone()
        if inserted is None:
            raise _SnapshotExists(result.station_id)
    return True


def run_slot(conn, data_dir: Path, slot: datetime) -> dict:
    """處理一個槽並回傳處理結果（build_report 的格式）。

    conn 必須是 autocommit 模式且不在 transaction 中：本函式自行 BEGIN／COMMIT，
    同槽鎖跟著這個 transaction 開始與釋放。整個流程在同一個 transaction 內：

    1. 取得同槽鎖（在查詢既有快照之前）。
    2. 決定來源批次：已有快照的槽沿用唯一的 fetch_batch_id（多個 → failed）；
       零快照的槽從 JSONL 挑（挑不到 → no_batch）。
    3. 讀取 gzip 並逐站驗證（缺失或損壞 → failed，既有快照不動）。
    4. 確保 fetch_batches 有這一批。
    5. 已有快照的站計為已存在；其餘每站一個 savepoint 寫入。
    6. COMMIT。

    同槽衝突、原始檔錯誤與資料庫錯誤都會回滾整個 transaction 並回報 failed，
    計數不會被報成已寫入。其他例外（例如程式錯誤）回滾後往外拋。
    """
    slot = _to_utc(slot, "slot")
    if not is_slot_start(slot):
        raise ValueError(f"slot is not aligned to 5 minutes: {slot.isoformat()}")
    if not conn.autocommit:
        raise ValueError("run_slot requires an autocommit connection")

    state: dict = {"batch": None, "source": None, "validation": None, "log": None}
    counts = RunCounts()
    try:
        with conn.transaction():
            acquire_slot_lock(conn, slot)

            batch_ids = _slot_batch_ids(conn, slot)
            if len(batch_ids) > 1:
                raise SlotConflictError(
                    f"slot has multiple fetch_batch_id: {batch_ids}"
                )
            if batch_ids:
                batch = _batch_by_pk(conn, batch_ids[0])
                state.update(batch=batch, source=SOURCE_EXISTING)
            else:
                log = read_batch_log(Path(data_dir) / BATCH_LOG)
                batch = select_batch(log, slot)
                state.update(batch=batch, source=SOURCE_JSONL, log=log)
                if batch is None:
                    return build_report(slot, counts, log=log)

            stations = load_raw_stations(data_dir, batch.raw_path)
            validation = validate_batch(stations, batch.fetched_at)
            state["validation"] = validation
            counts = RunCounts(skipped=validation.skipped)

            batch_pk = batch_ids[0] if batch_ids else _ensure_batch_row(conn, batch)
            existing = _existing_station_ids(conn, slot)
            for result in validation.results:
                if result.station_id in existing:
                    counts.existing += 1
                    continue
                try:
                    _write_station(conn, result, slot, batch_pk)
                except _SnapshotExists:
                    counts.existing += 1
                    continue
                except psycopg.Error as exc:
                    # savepoint 回滾後 transaction 應回到正常狀態；若整筆已經失效
                    # （例如連線中斷），不是單站問題，中止整筆 transaction。
                    if conn.info.transaction_status != TransactionStatus.INTRANS:
                        raise
                    counts.failed += 1
                    print(
                        f"station {result.station_id!r} failed: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if result.is_valid:
                    counts.written_valid += 1
                else:
                    reason = result.invalid_reason
                    counts.written_invalid[reason] = (
                        counts.written_invalid.get(reason, 0) + 1
                    )
    except (SlotConflictError, RawBatchError, psycopg.Error) as exc:
        return build_report(
            slot,
            counts,
            batch=state["batch"],
            source=state["source"],
            validation=state["validation"],
            log=state["log"],
            error=f"{type(exc).__name__}: {exc}",
        )

    return build_report(
        slot,
        counts,
        batch=state["batch"],
        source=state["source"],
        validation=state["validation"],
        log=state["log"],
    )


# ---------------------------------------------------------------- CLI

# 定版 4.2：4D／4E 完成前，手動補跑範圍以 7 天為限。
BACKFILL_LIMIT = timedelta(days=7)


def check_backfill_slot(slot: datetime, now: datetime) -> datetime:
    """手動補跑的槽必須已結束，且槽起點不早於 now − 7 天（剛好 7 天仍可）。"""
    slot = _to_utc(slot, "slot")
    now = _to_utc(now, "now")
    if slot > latest_closed_slot(now):
        raise ValueError(f"--slot 尚未結束：{slot.isoformat()}")
    if slot < now - BACKFILL_LIMIT:
        raise ValueError(f"--slot 超過 7 天補跑範圍：{slot.isoformat()}")
    return slot


def _parse_args(argv, now: datetime) -> datetime:
    parser = argparse.ArgumentParser(description="寫入一個五分鐘時間槽的站點快照")
    parser.add_argument(
        "--slot",
        help="手動補跑的槽起點，需帶時區，例如 2026-11-21T08:05:00Z（限 7 天內）",
    )
    args = parser.parse_args(argv)
    if args.slot is None:
        return latest_closed_slot(now)
    try:
        return check_backfill_slot(parse_slot(args.slot), now)
    except ValueError as exc:
        parser.error(str(exc))  # 結束碼 2
        raise  # parser.error 不會返回；保留給型別檢查


def main(argv=None, *, now=None, connect=None, data_dir=None) -> int:
    """CLI 入口。now、connect、data_dir 只供測試替換。"""
    if now is None:
        now = datetime.now(timezone.utc)
    slot = _parse_args(argv, now)
    data_dir = DATA_DIR if data_dir is None else Path(data_dir)

    try:
        if connect is None:
            url = os.environ.get(DATABASE_URL_ENV)
            if not url:
                raise RuntimeError(f"{DATABASE_URL_ENV} is not set")
            conn = psycopg.connect(url, autocommit=True)
        else:
            conn = connect()
        with conn:
            report = run_slot(conn, data_dir, slot)
    except Exception as exc:  # noqa: BLE001 — 任何錯誤都要留下一行 JSON 結果
        traceback.print_exc()
        report = build_report(slot, RunCounts(), error=f"{type(exc).__name__}: {exc}")

    print(format_report(report))
    return 1 if report["result"] == RESULT_FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
