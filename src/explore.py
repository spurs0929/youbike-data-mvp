"""Batch 2：檢查原始資料、清理資料，並檢查清理後的結果。

請從專案根目錄執行：

    python src/explore.py                 # 抓取即時資料
    python src/explore.py data/raw.json   # 使用已儲存的回應（方便重複測試）

此腳本只負責觀察與輸出結果。
它印出的每個數字都只是針對「單次 API 回應」的觀察結果，
不代表這些特徵永遠是資料來源的固定規則。
"""

import json
import sys

import pandas as pd

from fetch import FetchError, fetch_stations, validate_shape
from transform import NAME_PREFIX, TransformError, clean

# 台北市的大致經緯度範圍，只用來找出明顯異常的座標。
LAT_RANGE = (24.95, 25.22)
LON_RANGE = (121.45, 121.67)


def load(path: str | None) -> list[dict]:
    if path is None:
        return fetch_stations()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    validate_shape(data)
    return data


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def inspect_raw(raw: pd.DataFrame) -> None:
    section("1. 原始資料大小")
    print(f"資料列數: {len(raw)}   欄位數: {raw.shape[1]}")

    section("2. 原始欄位：資料型別 / 缺失值 / 空字串 / 不重複值數量")

    # 缺失資料也可能以 "" 或 "  " 的形式出現，
    # 而這些情況無法被 isna() 偵測到。
    is_blank = raw.apply(
        lambda col: col.map(lambda v: isinstance(v, str) and v.strip() == "")
    )

    overview = pd.DataFrame(
        {
            "資料型別": raw.dtypes.astype(str),
            "缺失值": raw.isna().sum(),
            "空字串": is_blank.sum(),
            "不重複值": raw.nunique(),
        }
    )
    print(overview.to_string())

    section("3. 此次回應中的重複資料")
    print(f"完全相同的資料列:          {int(raw.duplicated().sum())}")
    print(f"具有相同 sno 的資料列:     {int(raw['sno'].duplicated(keep=False).sum())}")
    print(
        f"具有相同 sna（站名）的資料列: {int(raw['sna'].duplicated(keep=False).sum())}"
    )

    section("4. 資料清理決策的檢查依據")
    print(f"act 出現的值: {raw['act'].value_counts(dropna=False).to_dict()}")

    print(
        f"站名沒有 '{NAME_PREFIX}' 前綴的資料列: "
        f"{int((~raw['sna'].astype('string').str.startswith(NAME_PREFIX)).sum())}"
    )

    if "mday" in raw:
        print(
            f"mday != infoTime 的資料列: {int((raw['mday'] != raw['infoTime']).sum())}"
        )

    if "infoDate" in raw:
        mismatch = raw["infoDate"] != raw["infoTime"].astype("string").str.slice(0, 10)
        print(f"infoDate != infoTime 日期的資料列: {int(mismatch.sum())}")

    print(
        f"srcUpdateTime != updateTime 的資料列: "
        f"{int((raw['srcUpdateTime'] != raw['updateTime']).sum())}"
    )

    if "sareaen" in raw:
        pairs = raw.groupby("sarea")["sareaen"].unique()
        print("sarea -> sareaen 的英文拼法：")
        for district, spellings in pairs.items():
            print(f"  {district}: {list(spellings)}")


def inspect_clean(df: pd.DataFrame) -> None:
    section("5. 清理後的 DataFrame")
    print(f"資料列數: {len(df)}   欄位數: {df.shape[1]}")

    print(
        pd.DataFrame(
            {
                "資料型別": df.dtypes.astype(str),
                "缺失值": df.isna().sum(),
            }
        ).to_string()
    )

    print()
    print(df.head(3).T.to_string())

    section("6. 數值檢查（只回報，不自動修正）")

    counts = df[["total_spaces", "available_bikes", "available_spaces"]]

    print(f"存在負數的資料列:                 {int((counts < 0).any(axis=1).sum())}")

    print(f"total_spaces = 0 的資料列:         {int((df['total_spaces'] == 0).sum())}")

    print(
        f"total_spaces = 99 的資料列:        "
        f"{int((df['total_spaces'] == 99).sum())}   "
        f"最大值: {df['total_spaces'].max()}"
    )

    print(
        f"available_bikes > total_spaces:    "
        f"{int((df['available_bikes'] > df['total_spaces']).sum())}"
    )

    print(
        f"available_spaces > total_spaces:   "
        f"{int((df['available_spaces'] > df['total_spaces']).sum())}"
    )

    out_of_box = ~df["latitude"].between(*LAT_RANGE) | ~df["longitude"].between(
        *LON_RANGE
    )

    print(f"座標超出台北市檢查範圍的資料列:    {int(out_of_box.sum())}")

    # 計算 total_spaces - available_bikes - available_spaces。
    # 目前不知道這個差值實際代表什麼，因此只觀察、不自行解釋或修正。
    diff = df["total_spaces"] - df["available_bikes"] - df["available_spaces"]

    print("total_spaces - available_bikes - available_spaces （差值意義目前未知）：")

    print(
        f"  = 0: {int((diff == 0).sum())}   "
        f"> 0: {int((diff > 0).sum())}   "
        f"< 0: {int((diff < 0).sum())}   "
        f"最小值: {diff.min()}   最大值: {diff.max()}"
    )

    section("7. 啟用與停用站點（不過濾任何資料）")

    summary = df.groupby("is_active").agg(
        stations=("station_id", "size"),
        zero_bikes=("available_bikes", lambda s: int((s == 0).sum())),
        zero_spaces=("available_spaces", lambda s: int((s == 0).sum())),
    )

    print(summary.to_string())

    section("8. 時間欄位")

    for col in ["src_update_time", "update_time", "info_time"]:
        print(
            f"{col:16} "
            f"不重複值: {df[col].nunique():5}   "
            f"最早: {df[col].min()}   "
            f"最晚: {df[col].max()}"
        )

    age = (df["src_update_time"] - df["info_time"]).dt.total_seconds() / 60

    print("各站 info_time 比 src_update_time 落後多久：")

    print(
        f"  <= 2 分鐘: {int((age <= 2).sum())}   "
        f"2～10 分鐘: {int(((age > 2) & (age <= 10)).sum())}   "
        f"10 分鐘～1 天: {int(((age > 10) & (age <= 1440)).sum())}   "
        f"> 1 天: {int((age > 1440).sum())}"
    )

    stale = df.loc[
        age > 10,
        ["station_id", "station_name", "is_active", "info_time"],
    ].sort_values("info_time")

    if len(stale):
        print("info_time 最舊的資料（最多顯示 10 筆）：")
        print(stale.head(10).to_string(index=False))

    section("9. 各行政區站點數")

    print(df.groupby("district").size().sort_values(ascending=False).to_string())


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        stations = load(path)
    except (FetchError, OSError, ValueError) as exc:
        print(f"無法載入資料: {exc}", file=sys.stderr)
        return 1

    raw = pd.DataFrame(stations)
    inspect_raw(raw)

    try:
        df = clean(raw)
    except TransformError as exc:
        print(f"\n資料清理已停止: {exc}", file=sys.stderr)
        return 1

    inspect_clean(df)
    return 0


if __name__ == "__main__":
    sys.exit(main())
