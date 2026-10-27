"""Batch 1：取得臺北市 YouBike 2.0 即時 JSON，並輸出基本摘要。

請從專案根目錄執行：

    python src/fetch.py

這個模組只負責取得資料與驗證資料結構。
不進行資料轉換、儲存或排程。
"""

import json
import sys
from collections import Counter

import requests

URL = "https://tcgbusfs.blob.core.windows.net/dotapp/youbike/v2/youbike_immediate.json"

# 連線逾時與讀取逾時，單位為秒。
# 第一個數值 connect timeout 是建立連線最多等待 5 秒，
# 第二個數值 read timeout 是連線成功後讀取資料最多等待 30 秒。
TIMEOUT = (5, 30)

# 後續 Batch 會依賴的必要欄位。
# 上游資料過去曾修改欄位名稱，例如：
# tot / sbi / bemp -> Quantity / available_*
#
# 因此如果未來上游再次修改欄位名稱，
# 我們希望在資料入口就明確失敗，
# 而不是讓錯誤資料繼續流入後續處理流程。
REQUIRED_FIELDS = (
    "sno",
    "sna",
    "sarea",
    "ar",
    "latitude",
    "longitude",
    "Quantity",
    "available_rent_bikes",
    "available_return_bikes",
    "act",
    "infoTime",
    "srcUpdateTime",
    "updateTime",
)


class FetchError(Exception):
    """當上游資料無法取得，或回傳內容無法使用時，拋出此例外。"""


def fetch_stations(url: str = URL) -> list[dict]:
    """下載 Youbike 站點資料，並以 dict list 的形式回傳。

    以下情況會拋出 FetchError：
    - 網路連線問題
    - HTTP 錯誤
    - 回傳內容不是合法 JSON
    - JSON 結構不符合目前程式預期
    """
    return parse_stations(fetch_raw(url))


def fetch_raw(url: str = URL) -> bytes:
    """下載原始回應內容（bytes），不做任何解析。

    Batch 3 需要保存「上游實際回傳的內容」，
    因此下載與解析拆成兩步：先拿到原始 bytes，再交給 parse_stations。
    網路或 HTTP 錯誤會拋出 FetchError。
    """
    try:
        response = requests.get(url, timeout=TIMEOUT)
        response.raise_for_status()  # HTTP 錯誤會拋出 requests.HTTPError
    except requests.exceptions.Timeout as e:
        raise FetchError(f"Request timed out after {TIMEOUT} seconds") from e
    except requests.exceptions.HTTPError as e:
        raise FetchError(
            f"HTTP error occurred: {e.response.status_code} {e.response.reason}"
        ) from e
    except requests.exceptions.RequestException as e:
        raise FetchError(f"Network error: {e}") from e

    return response.content


def parse_stations(body: bytes) -> list[dict]:
    """將原始回應內容解析為站點 dict list，並驗證基本結構。"""
    try:
        data = json.loads(body)
    except ValueError as e:  # 包含 JSONDecodeError 與 UnicodeDecodeError
        raise FetchError("Response body is not valid JSON") from e

    validate_shape(data)
    return data


def validate_shape(data: list[dict]) -> None:
    """檢查後續流程所依賴的最基本資料結構。

    目前只檢查：
    - 最外層是否為 list
    - list 是否為空
    - 每筆資料是否為 dict
    - 必要欄位是否存在

    欄位型別、數值範圍、缺失值等內容層級的驗證，
    留到 Batch 2 使用 Pandas 處理。
    """
    if not isinstance(data, list):
        raise FetchError(f"Expected a JSON array, got {type(data).__name__}")
    if not data:
        raise FetchError("Received an empty list of stations")

    problems = []

    for index, record in enumerate(data):
        if not isinstance(record, dict):
            problems.append(f"record {index}: not an object")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in record]
        if missing:
            problems.append(
                f"record {index} (sno={record.get('sno')}): missing {missing}"
            )

    if problems:
        # 最多只列出前 5 個問題，避免訊息過長。
        shown = "\n ".join(problems[:5])
        raise FetchError(
            f"{len(problems)} of {len(data)} records failed validation:\n  {shown}"
        )


def summarize(stations: list[dict]) -> dict:
    """計算資料的基本統計摘要。

    此函式只讀取資料，不會修改或過濾原始站點資料。
    """
    districts = Counter(s["sarea"] for s in stations)
    act = Counter(s["act"] for s in stations)
    info_times = [s["infoTime"] for s in stations]

    return {
        "station_count": len(stations),
        "unique_sno_count": len({s["sno"] for s in stations}),
        "districts": dict(sorted(districts.items(), key=lambda kv: -kv[1])),
        "act_counts": dict(act),
        "src_update_times": sorted({s["srcUpdateTime"] for s in stations}),
        "info_time_min": min(info_times),
        "info_time_max": max(info_times),
        "field_names": sorted({key for s in stations for key in s}),
    }


def print_summary(summary: dict) -> None:
    """將資料摘要輸出到終端機。"""
    print(f"站點總數: {summary['station_count']}")
    print(f"唯一站點編號數量: {summary['unique_sno_count']}")
    print("各行政區站點數量:")
    for district, count in summary["districts"].items():
        print(f"  {district}: {count}")
    print("act 欄位統計:")
    for act_value, count in summary["act_counts"].items():
        print(f"  {act_value}: {count}")
    print("資料來源更新時間 (srcUpdateTime):")
    for update_time in summary["src_update_times"]:
        print(f"  {update_time}")
    print(f"infoTime 最小值: {summary['info_time_min']}")
    print(f"infoTime 最大值: {summary['info_time_max']}")
    print("欄位名稱:")
    for field_name in summary["field_names"]:
        print(f"  {field_name}")


def main() -> int:
    """執行一次資料取得與摘要輸出。

    成功時回傳 exit code 0;
    取得/驗證資料失敗時回傳 exit code 1。
    """
    try:
        stations = fetch_stations()
    except FetchError as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        return 1

    print_summary(summarize(stations))
    return 0


if __name__ == "__main__":
    sys.exit(main())
