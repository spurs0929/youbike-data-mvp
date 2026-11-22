"""StationRepository：API 與 LLM 工具共用的站點查詢介面（設計定版 v1.3 8.4）。

- `StationStatus`：共用 DTO，欄位沿用 schema 命名。
- `StationRepository`：查詢介面（typing.Protocol）。Mock 實作與 PostgreSQL
  實作遵守相同方法與回傳型別，API 階段只替換實作，不改介面。
- `PostgresStationRepository`：直接查詢 PostgreSQL 最新快照。
- `InMemoryStationRepository`：Mock 實作，供 API 與 LLM 工具測試使用，不需要資料庫。
- `PooledStationRepository`：正式環境使用；每次查詢向 psycopg_pool 連線池借一條連線，
  交給 `PostgresStationRepository` 查詢後歸還（API-2b）。

「最新」的定義：
  每站先以主鍵 (station_id, slot_time) 倒序取出最新的一筆快照，再以該快照所屬
  批次的擷取時間 `fetched_at`（HTTP 請求開始時間，不是來源的 info_time）判斷是否
  在 FRESHNESS_WINDOW 內：`now - fetched_at <= 30 分鐘` 才回傳（剛好 30 分鐘仍回傳）。
  最新一筆不在視窗內時，該站不回傳，也不會改用更舊的快照補位；最新一筆為無效
  （例如 stale）時照樣回傳並附 invalid_reason，同樣不改用較舊的有效快照。

參數錯誤一律在查詢資料庫之前丟出 InvalidQueryError（ValueError 子類別），
由 API 層轉為 HTTP 422；超過上限的參數不會被自動截斷。資料庫連線失敗（借不到連線、
連線中斷）轉為 RepositoryUnavailableError，由 API 層轉為 HTTP 503。查無資料時，
單站查詢回傳 None，列表查詢回傳空 list。所有使用者輸入都以參數綁定傳給 SQL。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------- 常數

FRESHNESS_WINDOW = timedelta(minutes=30)
MAX_RESULTS = 20  # 單次查詢最多回傳筆數（Repository 層上限）
DEFAULT_LIMIT = 10
MAX_RADIUS_M = 3000.0
DEFAULT_RADIUS_M = 500.0
MAX_KEYWORD_LENGTH = 50  # 去除前後空白後的字元數
MAX_STATION_ID_LENGTH = 32
EARTH_RADIUS_M = 6_371_008.8  # 地球平均半徑；方框預篩與 haversine 使用同一個值

# 方框預篩額外放寬的角度（度），避免浮點誤差把剛好在半徑上的站排除；
# 最終是否在半徑內一律以 haversine 判斷。
_BOX_MARGIN_DEG = 1e-9


class InvalidQueryError(ValueError):
    """查詢參數不合法（型別、範圍、長度）。API 層轉為 HTTP 422。"""


class RepositoryUnavailableError(RuntimeError):
    """資料庫暫時無法使用：借不到連線、連線池已關閉、連線中斷。API 層轉為 HTTP 503。

    只用於「連不到資料庫」；SQL 錯誤、程式邏輯錯誤照原樣往外丟，不包成這個例外。
    訊息固定，不含連線字串或底層錯誤細節（原始例外保留在 __cause__，只供伺服器端記錄）。
    """


UNAVAILABLE_MESSAGE = "資料庫暫時無法使用"


# ---------------------------------------------------------------- DTO


@dataclass(frozen=True)
class StationStatus:
    """單一站點的最新狀態。時間欄位皆為具時區的 UTC。"""

    station_id: str
    station_name: str | None
    district: str | None
    address: str | None
    latitude: float | None
    longitude: float | None
    total_spaces: int | None
    available_bikes: int | None
    available_spaces: int | None
    is_active: bool | None
    is_valid: bool
    invalid_reason: str | None
    info_time: datetime | None
    slot_time: datetime
    distance_m: float | None = None  # 只有附近查詢會填


# ---------------------------------------------------------------- 介面


@runtime_checkable
class StationRepository(Protocol):
    def get_station(self, station_id: str) -> StationStatus | None:
        """指定站點的最新狀態；查無或沒有視窗內的快照時回傳 None。"""
        ...

    def search_by_name(
        self, keyword: str, limit: int = DEFAULT_LIMIT
    ) -> list[StationStatus]:
        """站名包含 keyword（不分大小寫）的站，依站名、站號排序（以 Unicode 碼位比較）。"""
        ...

    def find_nearby(
        self,
        latitude: float,
        longitude: float,
        radius_m: float = DEFAULT_RADIUS_M,
        limit: int = DEFAULT_LIMIT,
    ) -> list[StationStatus]:
        """半徑內的站，依距離由近到遠排序（同距離依站號）。"""
        ...


# ---------------------------------------------------------------- 參數驗證


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_limit(limit) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise InvalidQueryError("limit 必須是整數")
    if not 1 <= limit <= MAX_RESULTS:
        raise InvalidQueryError(f"limit 必須介於 1～{MAX_RESULTS}")
    return limit


def validate_coordinates(latitude, longitude) -> tuple[float, float]:
    if not _is_number(latitude) or not _is_number(longitude):
        raise InvalidQueryError("座標必須是數字")
    lat, lng = float(latitude), float(longitude)
    if not (math.isfinite(lat) and math.isfinite(lng)):
        raise InvalidQueryError("座標必須是有限值")
    if not -90.0 <= lat <= 90.0:
        raise InvalidQueryError("緯度必須介於 -90～90")
    if not -180.0 <= lng <= 180.0:
        raise InvalidQueryError("經度必須介於 -180～180")
    return lat, lng


def validate_radius(radius_m) -> float:
    if not _is_number(radius_m):
        raise InvalidQueryError("radius_m 必須是數字")
    radius = float(radius_m)
    if not math.isfinite(radius) or not 0.0 < radius <= MAX_RADIUS_M:
        raise InvalidQueryError(f"radius_m 必須大於 0 且不超過 {MAX_RADIUS_M:g}")
    return radius


def validate_keyword(keyword) -> str:
    if not isinstance(keyword, str):
        raise InvalidQueryError("keyword 必須是字串")
    text = keyword.strip()
    if not text:
        raise InvalidQueryError("keyword 不可為空白")
    if len(text) > MAX_KEYWORD_LENGTH:
        raise InvalidQueryError(f"keyword 不可超過 {MAX_KEYWORD_LENGTH} 字")
    return text


def validate_station_id(station_id) -> str:
    if not isinstance(station_id, str):
        raise InvalidQueryError("station_id 必須是字串")
    text = station_id.strip()
    if not text:
        raise InvalidQueryError("station_id 不可為空白")
    if len(text) > MAX_STATION_ID_LENGTH:
        raise InvalidQueryError(f"station_id 不可超過 {MAX_STATION_ID_LENGTH} 字")
    return text


# ---------------------------------------------------------------- 計算


def escape_like(text: str) -> str:
    """跳脫 LIKE 的特殊字元（搭配 SQL 的 ESCAPE '\\'）。先跳脫 \\ 本身。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """兩點球面距離（公尺）。中間值限制在 [0, 1]，避免浮點誤差使 asin／sqrt 失敗。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    h = min(1.0, max(0.0, h))
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class BoundingBox:
    """半徑 radius 的圓的外接方框（度）。

    - lng_min／lng_max 皆為 None：方框涵蓋極點，不限制經度。
    - wraps 為 True：跨越 ±180°，條件為 lng >= lng_min OR lng <= lng_max。
    """

    lat_min: float
    lat_max: float
    lng_min: float | None
    lng_max: float | None
    wraps: bool = False


def bounding_box(latitude: float, longitude: float, radius_m: float) -> BoundingBox:
    angular = radius_m / EARTH_RADIUS_M  # 弧度
    lat = math.radians(latitude)
    lat_min = math.degrees(lat - angular) - _BOX_MARGIN_DEG
    lat_max = math.degrees(lat + angular) + _BOX_MARGIN_DEG
    if lat_max >= 90.0 or lat_min <= -90.0:
        return BoundingBox(max(lat_min, -90.0), min(lat_max, 90.0), None, None)
    # 球面上半徑 r 的圓，經度最大偏移為 asin(sin(r) / cos(lat))
    delta = math.degrees(math.asin(math.sin(angular) / math.cos(lat)))
    delta += _BOX_MARGIN_DEG
    lng_min, lng_max = longitude - delta, longitude + delta
    if lng_min < -180.0:
        return BoundingBox(lat_min, lat_max, lng_min + 360.0, lng_max, wraps=True)
    if lng_max > 180.0:
        return BoundingBox(lat_min, lat_max, lng_min, lng_max - 360.0, wraps=True)
    return BoundingBox(lat_min, lat_max, lng_min, lng_max)


# ---------------------------------------------------------------- PostgreSQL 實作

# {where} 只會代入本模組固定的 SQL 片段；使用者輸入一律以 %(name)s 參數綁定。
_LATEST_SQL = """
SELECT s.station_id, s.station_name, s.district, s.address,
       s.latitude, s.longitude,
       snap.total_spaces, snap.available_bikes, snap.available_spaces,
       snap.is_active, snap.is_valid, snap.invalid_reason,
       snap.info_time, snap.slot_time
FROM stations AS s
CROSS JOIN LATERAL (
    SELECT ss.total_spaces, ss.available_bikes, ss.available_spaces,
           ss.is_active, ss.is_valid, ss.invalid_reason,
           ss.info_time, ss.slot_time, fb.fetched_at
    FROM station_snapshots AS ss
    JOIN fetch_batches AS fb ON fb.id = ss.fetch_batch_id
    WHERE ss.station_id = s.station_id
    ORDER BY ss.slot_time DESC
    LIMIT 1
) AS snap
WHERE snap.fetched_at >= %(cutoff)s
  AND ({where})
"""

_WHERE_ID = "s.station_id = %(station_id)s"
_WHERE_NAME = "s.station_name ILIKE %(pattern)s ESCAPE '\\'"
_WHERE_LAT = "s.latitude BETWEEN %(lat_min)s AND %(lat_max)s"
_WHERE_LNG_ANY = "s.longitude IS NOT NULL"
_WHERE_LNG = "s.longitude BETWEEN %(lng_min)s AND %(lng_max)s"
_WHERE_LNG_WRAP = "(s.longitude >= %(lng_min)s OR s.longitude <= %(lng_max)s)"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    # psycopg 依連線的 TimeZone 設定回傳 timestamptz（例如 Asia/Taipei）；
    # DTO 約定時間欄位一律為 UTC，因此在這裡統一轉換，不依賴資料庫設定。
    return None if value is None else value.astimezone(timezone.utc)


def _row_to_status(row) -> StationStatus:
    status = StationStatus(*row)
    return replace(
        status,
        info_time=_as_utc(status.info_time),
        slot_time=_as_utc(status.slot_time),
    )


class PostgresStationRepository:
    """以 psycopg 3 連線查詢最新快照。

    conn 由呼叫端建立與關閉；每次查詢包在一個 transaction 內並於結束時結束，
    不會讓連線停在 idle in transaction。只執行 SELECT。
    clock 回傳「現在」的具時區時間，測試時可注入固定時間。
    """

    def __init__(self, conn, *, clock: Callable[[], datetime] = _utc_now):
        self._conn = conn
        self._clock = clock

    def _cutoff(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock 必須回傳具時區的時間")
        return now.astimezone(timezone.utc) - FRESHNESS_WINDOW

    def _query(self, where: str, params: dict, order_limit: str = "") -> list:
        sql = _LATEST_SQL.format(where=where) + order_limit
        with self._conn.transaction():
            return self._conn.execute(
                sql, {**params, "cutoff": self._cutoff()}
            ).fetchall()

    def get_station(self, station_id: str) -> StationStatus | None:
        sid = validate_station_id(station_id)
        rows = self._query(_WHERE_ID, {"station_id": sid})
        return _row_to_status(rows[0]) if rows else None

    def search_by_name(
        self, keyword: str, limit: int = DEFAULT_LIMIT
    ) -> list[StationStatus]:
        text = validate_keyword(keyword)
        limit = validate_limit(limit)
        rows = self._query(
            _WHERE_NAME,
            {"pattern": f"%{escape_like(text)}%", "limit": limit},
            # COLLATE "C"：依 UTF-8 位元組（＝Unicode 碼位）比較，與 Python 的 str 排序一致，
            # 不受資料庫 collation 影響；否則本機（Windows）與 VM 的中文站名順序可能不同，
            # 有筆數上限時連回傳哪些站都會不同。
            'ORDER BY s.station_name COLLATE "C", s.station_id COLLATE "C"'
            " LIMIT %(limit)s",
        )
        return [_row_to_status(row) for row in rows]

    def find_nearby(
        self,
        latitude: float,
        longitude: float,
        radius_m: float = DEFAULT_RADIUS_M,
        limit: int = DEFAULT_LIMIT,
    ) -> list[StationStatus]:
        lat, lng = validate_coordinates(latitude, longitude)
        radius = validate_radius(radius_m)
        limit = validate_limit(limit)

        box = bounding_box(lat, lng, radius)
        params = {"lat_min": box.lat_min, "lat_max": box.lat_max}
        if box.lng_min is None:
            lng_where = _WHERE_LNG_ANY
        else:
            params.update(lng_min=box.lng_min, lng_max=box.lng_max)
            lng_where = _WHERE_LNG_WRAP if box.wraps else _WHERE_LNG
        rows = self._query(f"{_WHERE_LAT} AND {lng_where}", params)

        # 方框只是預篩：以 haversine 精算後過濾半徑、排序、限制筆數
        results = []
        for row in rows:
            status = _row_to_status(row)
            distance = haversine_m(lat, lng, status.latitude, status.longitude)
            if distance <= radius:
                results.append(replace(status, distance_m=distance))
        results.sort(key=lambda s: (s.distance_m, s.station_id))
        return results[:limit]


class PooledStationRepository:
    """以 psycopg_pool 連線池查詢；查詢內容與 PostgresStationRepository 相同（API-2b）。

    - 每次查詢以 `pool.connection()` 借一條連線，結束時（包含例外）歸還連線池；
      查詢本身由 PostgresStationRepository 執行，包在一個 transaction 內並結束。
    - 參數先驗證，不合法時直接丟 InvalidQueryError，不借連線。
    - 轉成 RepositoryUnavailableError 的只有：
      1. 借不到連線（PoolTimeout）、連線池已關閉（PoolClosed）、等待者過多（TooManyRequests）；
      2. 查詢途中的 OperationalError，且連線已中斷（`conn.broken` 或 `conn.closed`）。
      其他錯誤（SQL 錯誤、連線仍正常的 OperationalError 例如查詢逾時、程式錯誤）照原樣丟出。
    - 連線池的建立、開啟、關閉由呼叫端負責（見 app_main.py）。

    psycopg 與 psycopg_pool 在方法內才匯入，只用 Mock 時本模組不需要這兩個套件。
    """

    def __init__(self, pool, *, clock: Callable[[], datetime] = _utc_now):
        self._pool = pool
        self._clock = clock

    def _run(self, action: Callable):
        import psycopg
        from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

        try:
            with self._pool.connection() as conn:
                try:
                    return action(conn)
                except psycopg.OperationalError as exc:
                    if conn.broken or conn.closed:
                        raise RepositoryUnavailableError(UNAVAILABLE_MESSAGE) from exc
                    raise
        except (PoolTimeout, PoolClosed, TooManyRequests) as exc:
            raise RepositoryUnavailableError(UNAVAILABLE_MESSAGE) from exc

    def _repository(self, conn) -> PostgresStationRepository:
        return PostgresStationRepository(conn, clock=self._clock)

    def ping(self) -> None:
        """就緒檢查：借一條連線執行 SELECT 1。失敗時丟 RepositoryUnavailableError。"""

        def action(conn):
            with conn.transaction():
                conn.execute("SELECT 1").fetchone()

        self._run(action)

    def get_station(self, station_id: str) -> StationStatus | None:
        validate_station_id(station_id)
        return self._run(lambda conn: self._repository(conn).get_station(station_id))

    def search_by_name(
        self, keyword: str, limit: int = DEFAULT_LIMIT
    ) -> list[StationStatus]:
        validate_keyword(keyword)
        validate_limit(limit)
        return self._run(
            lambda conn: self._repository(conn).search_by_name(keyword, limit)
        )

    def find_nearby(
        self,
        latitude: float,
        longitude: float,
        radius_m: float = DEFAULT_RADIUS_M,
        limit: int = DEFAULT_LIMIT,
    ) -> list[StationStatus]:
        validate_coordinates(latitude, longitude)
        validate_radius(radius_m)
        validate_limit(limit)
        return self._run(
            lambda conn: self._repository(conn).find_nearby(
                latitude, longitude, radius_m, limit
            )
        )


# ---------------------------------------------------------------- Mock 實作


class InMemoryStationRepository:
    """Mock 實作：資料放在記憶體，不連資料庫。

    傳入的每筆 StationStatus 視為該站「已通過 30 分鐘視窗的最新快照」，
    因此不再判斷視窗。參數驗證、搜尋與距離規則和 PostgreSQL 實作共用同一套函式：
    - 站名搜尋：不分大小寫的子字串比對，%、_、\\ 都是一般文字；依站名、站號排序
      （以 Unicode 碼位比較，與 PostgreSQL 版的 COLLATE "C" 一致）。
    - 附近查詢：haversine 距離 <= 半徑；依距離、站號排序；沒有座標的站不回傳。

    站名比對是近似實作：Mock 用 str.casefold()，PostgreSQL 用 ILIKE（依資料庫的
    字元分類轉小寫）。中文與 ASCII 字母兩者結果相同，少數特殊字元（例如德文 ß）可能不同。
    搜尋語意以 PostgreSQL 版與其整合測試為準。
    """

    def __init__(self, stations: Iterable[StationStatus] = ()):
        self._stations: dict[str, StationStatus] = {}
        for status in stations:
            if status.station_id in self._stations:
                raise ValueError(f"站號重複：{status.station_id}")
            self._stations[status.station_id] = replace(status, distance_m=None)

    def get_station(self, station_id: str) -> StationStatus | None:
        return self._stations.get(validate_station_id(station_id))

    def search_by_name(
        self, keyword: str, limit: int = DEFAULT_LIMIT
    ) -> list[StationStatus]:
        text = validate_keyword(keyword).casefold()
        limit = validate_limit(limit)
        found = [
            s
            for s in self._stations.values()
            if s.station_name is not None and text in s.station_name.casefold()
        ]
        found.sort(key=lambda s: (s.station_name, s.station_id))
        return found[:limit]

    def find_nearby(
        self,
        latitude: float,
        longitude: float,
        radius_m: float = DEFAULT_RADIUS_M,
        limit: int = DEFAULT_LIMIT,
    ) -> list[StationStatus]:
        lat, lng = validate_coordinates(latitude, longitude)
        radius = validate_radius(radius_m)
        limit = validate_limit(limit)
        results = []
        for status in self._stations.values():
            if status.latitude is None or status.longitude is None:
                continue
            distance = haversine_m(lat, lng, status.latitude, status.longitude)
            if distance <= radius:
                results.append(replace(status, distance_m=distance))
        results.sort(key=lambda s: (s.distance_m, s.station_id))
        return results[:limit]
