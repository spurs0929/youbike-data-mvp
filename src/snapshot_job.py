"""Batch 4C-3：五分鐘快照寫入作業。

4C-3a（本次）只包含不需要資料庫的部分，全部是純函式或只讀檔案：

- 時間槽：批次歸槽、最近一個已結束的槽、`--slot` 參數解析。
- 批次紀錄：讀取 fetch_batches.jsonl、去重、挑選槽內批次。
- 原始檔：在資料目錄內讀取 gzip 並解析站點。
- 處理結果：依計數分類 result，組成一行 JSON。

資料庫寫入（同槽鎖、transaction／savepoint、各表寫入）與 CLI 入口屬於 4C-3b。
規格見定版 4.4.1、4.4.6 與 docs/schema.md 第六節。

本模組不讀取目前時間：需要「現在」的函式一律由呼叫端傳入，
相同輸入一定得到相同結果，方便重現問題與測試邊界。
"""

from __future__ import annotations

import gzip
import json
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fetch import FetchError, parse_stations
from snapshot import REASON_ORDER, BatchValidation

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
