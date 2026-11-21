"""Batch 4C-2：快照的逐站驗證層（不接觸資料庫）。

輸入一批 status = success 的原始站點資料（fetch.parse_stations 的輸出）
與該批次的 fetched_at，輸出每站的驗證結果。規格見 docs/schema.md
第四節（invalid_reason 與判定順序）與第六節（逐站驗證、新鮮度）。

本模組只做驗證，不讀 JSONL／gzip、不連資料庫、不修改 transform.py。
選批次、同槽鎖、transaction／savepoint、寫入與計數屬於 4C-3。

流程：

1. 前處理：sno 去空白，NULL／空白跳過；同站多筆只比較寫入欄位，
   相同留一筆，不同判為 duplicate_conflict（不進入 clean()）。
2. 結構判定：以原始值判定 missing_field、invalid_type（不依 clean() 結果、
   不解析例外訊息），結構無效的站不進入 clean()。
3. 清洗：其餘站點整批呼叫一次 clean()（快速路徑）；失敗時逐站呼叫
   （備援路徑），單站失敗先把轉換失敗的可選欄位設為 NULL 再試一次。
   結構判定通過卻仍無法清洗，代表本模組的判定與 clean() 不一致，
   屬於程式錯誤，拋出 CleanMismatchError，不吞掉。
4. 可選欄位檢查：座標非有限值或超出範圍、文字去空白後為空 → NULL，
   不讓該站無效。兩條路徑都在 clean() 之後執行。
5. 值檢查：inactive → stale → capacity_invalid → count_invalid。

所有時間比較都以傳入的 fetched_at 為準，本模組不讀取目前時間，
同一批資料重跑結果相同。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from transform import SOURCE_TZ, TIME_FORMAT, TransformError, clean

# 新鮮度門檻（暫定值，見 docs/schema.md 第六節）。
# lag = fetched_at − info_time；lag > 門檻才算 stale，剛好等於門檻仍有效。
STALE_THRESHOLD = timedelta(minutes=10)

# PostgreSQL integer 的範圍；超出時判為 invalid_type。
PG_INT_MIN = -(2**31)
PG_INT_MAX = 2**31 - 1

# 必要欄位（sno 另在前處理處理）。
REQUIRED_VALUE_FIELDS = (
    "act",
    "infoTime",
    "Quantity",
    "available_rent_bikes",
    "available_return_bikes",
)
COUNT_FIELDS = ("Quantity", "available_rent_bikes", "available_return_bikes")

# 可選欄位：轉換失敗或值不合理時只清成 NULL，不讓該站無效。
OPTIONAL_TEXT_FIELDS = ("sna", "sarea", "ar")
OPTIONAL_COORD_FIELDS = ("latitude", "longitude")
OPTIONAL_TIME_FIELDS = ("srcUpdateTime", "updateTime")

# 判斷「同站多筆是否相同」時比較的欄位：只看快照與 stations 會寫入的欄位。
# srcUpdateTime、updateTime 不寫入，不同也不算衝突。
WRITE_FIELDS = (
    "sna",
    "sarea",
    "ar",
    "latitude",
    "longitude",
    "act",
    "infoTime",
    "Quantity",
    "available_rent_bikes",
    "available_return_bikes",
)

# invalid_reason 代碼，依判定順序排列（docs/schema.md 第四節）。
REASON_ORDER = (
    "duplicate_conflict",
    "missing_field",
    "invalid_type",
    "inactive",
    "stale",
    "capacity_invalid",
    "count_invalid",
)

PATH_FAST = "fast"  # 整批 clean() 一次成功
PATH_FALLBACK = "fallback"  # 整批失敗，改逐站 clean()
PATH_NONE = "none"  # 沒有任何站需要 clean()（全部跳過、衝突或結構無效）

_INT_TEXT = re.compile(r"[+-]?\d+")
_ACT_VALUES = {"1": True, "0": False}


class CleanMismatchError(RuntimeError):
    """結構判定通過，但 clean() 仍失敗。

    代表本模組的原始值判定與 transform.clean() 的規則不一致，
    是程式錯誤，不是資料問題，因此不轉成 invalid_reason。
    """


@dataclass(frozen=True)
class StationInfo:
    """寫入 stations 的站點資訊；欄位對齊 stations 資料表。

    只有 clean() 成功的站才有這份資訊（docs/schema.md 第三節）。
    """

    station_name: str | None
    district: str | None
    address: str | None
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class StationResult:
    """單站的驗證結果；欄位對齊 station_snapshots（slot_time、fetch_batch_id 除外）。

    info_time 為 UTC。info 為 None 表示 clean() 未執行
    （duplicate_conflict、missing_field、invalid_type），此時數值與 info_time 都是 None。
    """

    station_id: str
    is_valid: bool
    invalid_reason: str | None
    info_time: datetime | None
    is_active: bool | None
    total_spaces: int | None
    available_bikes: int | None
    available_spaces: int | None
    info: StationInfo | None


@dataclass(frozen=True)
class BatchValidation:
    """整批的驗證結果與批次層統計。

    - results：每個 station_id 一筆，依原始資料中第一次出現的順序。
    - skipped：sno 為 NULL、空白或不是文字／整數，無法寫入任何表的筆數。
    - negative_lag：info_time 晚於 fetched_at 的站數（只警告，不判無效）。
    - all_known_active_stale：至少有一個「營運中且有 info_time」的站，且這些站全數過期。
    - unknown_active：營運狀態無法判斷（is_active 為 None）的站數，整批過期告警時一併列出。
    - path：PATH_FAST、PATH_FALLBACK 或 PATH_NONE。
    """

    results: tuple[StationResult, ...]
    skipped: int
    negative_lag: int
    all_known_active_stale: bool
    unknown_active: int
    path: str


def validate_batch(stations: list[dict], fetched_at: datetime) -> BatchValidation:
    """驗證一批原始站點資料。不修改傳入的資料。

    fetched_at 必須帶時區，否則拋出 ValueError。
    """
    if not isinstance(fetched_at, datetime):
        raise TypeError("fetched_at must be a datetime")
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    fetched_at = fetched_at.astimezone(timezone.utc)

    # 1. 前處理：取 station_id，依第一次出現的順序分組。
    groups: dict[str, list[dict]] = {}
    skipped = 0
    for record in stations:
        station_id = _station_id(record.get("sno"))
        if station_id is None:
            skipped += 1
            continue
        groups.setdefault(station_id, []).append(record)

    results: dict[str, StationResult] = {}
    to_clean: dict[str, dict] = {}
    for station_id, records in groups.items():
        first = records[0]
        if any(not _same_write_fields(first, other) for other in records[1:]):
            results[station_id] = _unclean_result(
                station_id, "duplicate_conflict", _common_act(records)
            )
            continue

        # 2. 結構判定（原始值）。
        reason = _structural_reason(first)
        if reason is not None:
            results[station_id] = _unclean_result(
                station_id, reason, _parse_act(first.get("act"))
            )
            continue
        to_clean[station_id] = _prepared_record(first, station_id)

    # 3. 清洗：快速路徑，失敗時逐站備援。
    cleaned, path = _clean_all(to_clean)

    # 4、5. 可選欄位檢查與值檢查。
    negative_lag = 0
    for station_id, row in cleaned.items():
        result = _checked_result(station_id, row, fetched_at)
        if result.info_time is not None and result.info_time > fetched_at:
            negative_lag += 1
        results[station_id] = result

    ordered = tuple(results[station_id] for station_id in groups)
    return BatchValidation(
        results=ordered,
        skipped=skipped,
        negative_lag=negative_lag,
        all_known_active_stale=_all_known_active_stale(ordered, fetched_at),
        unknown_active=sum(1 for r in ordered if r.is_active is None),
        path=path,
    )


# ---------------------------------------------------------------- 前處理


def _station_id(raw) -> str | None:
    """sno 轉成去空白的文字；NULL、空白或無法當成識別碼的型別回傳 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    station_id = raw.strip()
    return station_id or None


def _same_value(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a):
        return math.isnan(b)
    return type(a) is type(b) and a == b


def _same_write_fields(a: dict, b: dict) -> bool:
    return all(_same_value(a.get(f), b.get(f)) for f in WRITE_FIELDS)


def _common_act(records: list[dict]) -> bool | None:
    """多筆的 act 一致且可判定時回傳該值，否則 None。"""
    values = {_parse_act(r.get("act")) for r in records}
    if len(values) == 1:
        return values.pop()
    return None


def _prepared_record(record: dict, station_id: str) -> dict:
    """複製一份交給 clean() 的資料；sno 換成去空白後的 station_id。"""
    prepared = dict(record)
    prepared["sno"] = station_id
    return prepared


# ---------------------------------------------------------------- 結構判定


def _is_missing(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, float):
        return math.isnan(value)
    return False


def _parse_act(value) -> bool | None:
    if isinstance(value, str):
        return _ACT_VALUES.get(value.strip())
    return None


def _parse_count(value) -> int | None:
    """原始數量值轉成整數；無法轉換或超出 PostgreSQL integer 範圍時回傳 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        number = int(value)
    elif isinstance(value, str) and _INT_TEXT.fullmatch(value.strip()):
        number = int(value.strip())
    else:
        return None
    if not PG_INT_MIN <= number <= PG_INT_MAX:
        return None
    return number


def _is_valid_time(value) -> bool:
    if not isinstance(value, str):
        return False
    # 只檢查格式；來源時間是台灣當地時間，明確標上時區（與 collect.py 相同）。
    try:
        datetime.strptime(value, TIME_FORMAT).replace(tzinfo=ZoneInfo(SOURCE_TZ))
    except ValueError:
        return False
    return True


def _structural_reason(record: dict) -> str | None:
    """以原始值判定 missing_field、invalid_type；兩者都不成立時回傳 None。"""
    if any(_is_missing(record.get(f)) for f in REQUIRED_VALUE_FIELDS):
        return "missing_field"
    if _parse_act(record.get("act")) is None:
        return "invalid_type"
    if not _is_valid_time(record.get("infoTime")):
        return "invalid_type"
    if any(_parse_count(record.get(f)) is None for f in COUNT_FIELDS):
        return "invalid_type"
    return None


def _unclean_result(
    station_id: str, reason: str, is_active: bool | None
) -> StationResult:
    return StationResult(
        station_id=station_id,
        is_valid=False,
        invalid_reason=reason,
        info_time=None,
        is_active=is_active,
        total_spaces=None,
        available_bikes=None,
        available_spaces=None,
        info=None,
    )


# ---------------------------------------------------------------- 清洗


def _clean_all(records: dict[str, dict]) -> tuple[dict[str, dict], str]:
    """回傳 {station_id: 清洗後的一列} 與走的路徑。"""
    if not records:
        return {}, PATH_NONE
    try:
        df = clean(pd.DataFrame(list(records.values())))
    except TransformError:
        return {sid: _clean_one(rec) for sid, rec in records.items()}, PATH_FALLBACK
    return _rows_by_id(df), PATH_FAST


def _clean_one(record: dict) -> dict:
    try:
        df = clean(pd.DataFrame([record]))
    except TransformError:
        try:
            df = clean(pd.DataFrame([_without_bad_optional(record)]))
        except TransformError as exc:
            raise CleanMismatchError(
                f"station {record.get('sno')!r} passed structural checks "
                f"but clean() failed: {exc}"
            ) from exc
    return df.to_dict("records")[0]


def _converts(value, convert) -> bool:
    try:
        convert(pd.Series([value], dtype=object))
    except (ValueError, TypeError):
        return False
    return True


def _to_float(series: pd.Series):
    return pd.to_numeric(series, errors="raise").astype("float64")


def _to_time(series: pd.Series):
    return pd.to_datetime(series, format=TIME_FORMAT).dt.tz_localize(SOURCE_TZ)


def _to_text(series: pd.Series):
    return series.astype("string")


def _without_bad_optional(record: dict) -> dict:
    """複製一份，只把 clean() 無法轉換的可選欄位設為 None，其餘欄位保留。"""
    converters = (
        [(f, _to_text) for f in OPTIONAL_TEXT_FIELDS]
        + [(f, _to_float) for f in OPTIONAL_COORD_FIELDS]
        + [(f, _to_time) for f in OPTIONAL_TIME_FIELDS]
    )
    fixed = dict(record)
    for field, convert in converters:
        if not _converts(fixed.get(field), convert):
            fixed[field] = None
    return fixed


def _rows_by_id(df: pd.DataFrame) -> dict[str, dict]:
    return {row["station_id"]: row for row in df.to_dict("records")}


# ---------------------------------------------------------------- 值檢查


def _na_to_none(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _text(value) -> str | None:
    value = _na_to_none(value)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _coord(value, limit: float) -> float | None:
    value = _na_to_none(value)
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value) or not -limit <= value <= limit:
        return None
    return value


def _checked_result(station_id: str, row: dict, fetched_at: datetime) -> StationResult:
    info_time = row["info_time"].to_pydatetime().astimezone(timezone.utc)
    is_active = bool(row["is_active"])
    total = int(row["total_spaces"])
    bikes = int(row["available_bikes"])
    spaces = int(row["available_spaces"])

    if not is_active:
        reason = "inactive"
    elif fetched_at - info_time > STALE_THRESHOLD:
        reason = "stale"
    elif total < 0:
        reason = "capacity_invalid"
    elif not (0 <= bikes <= total and 0 <= spaces <= total):
        reason = "count_invalid"
    else:
        reason = None

    return StationResult(
        station_id=station_id,
        is_valid=reason is None,
        invalid_reason=reason,
        info_time=info_time,
        is_active=is_active,
        total_spaces=total,
        available_bikes=bikes,
        available_spaces=spaces,
        info=StationInfo(
            station_name=_text(row["station_name"]),
            district=_text(row["district"]),
            address=_text(row["address"]),
            latitude=_coord(row["latitude"], 90.0),
            longitude=_coord(row["longitude"], 180.0),
        ),
    )


def _all_known_active_stale(
    results: tuple[StationResult, ...], fetched_at: datetime
) -> bool:
    """已知營運中（is_active = true 且有 info_time）的站至少一個，且全數過期。

    以落後時間判斷，不看 invalid_reason：容量異常（capacity_invalid 等）的站
    原因代碼不是 stale，但它的 info_time 仍能說明來源是否停滯。
    結構無效而沒有 info_time 的站無法判斷，不納入。
    """
    lags = [
        fetched_at - r.info_time
        for r in results
        if r.is_active is True and r.info_time is not None
    ]
    return bool(lags) and all(lag > STALE_THRESHOLD for lag in lags)
