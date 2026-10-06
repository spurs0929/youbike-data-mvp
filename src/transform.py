import pandas as pd

# 原始欄位 -> 清理後的欄位名稱。未列在這裡的欄位會被捨棄：
#   sareaen / snaen / aren  英文版本欄位，標點格式不一致，且目前不使用
#   mday / infoDate         與 infoTime 重複（explore.py 會檢查這件事）

RENAME = {
    "sno": "station_id",
    "sna": "station_name",
    "sarea": "district",
    "ar": "address",
    "latitude": "latitude",
    "longitude": "longitude",
    "Quantity": "total_spaces",
    "available_rent_bikes": "available_bikes",
    "available_return_bikes": "available_spaces",
    "act": "is_active",
    "infoTime": "info_time",
    "srcUpdateTime": "src_update_time",
    "updateTime": "update_time",
}

TEXT_COLUMNS = ["station_id", "station_name", "district", "address"]
COUNT_COLUMNS = ["total_spaces", "available_bikes", "available_spaces"]
COORD_COLUMNS = ["latitude", "longitude"]
TIME_COLUMNS = ["info_time", "src_update_time", "update_time"]

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SOURCE_TZ = "Asia/Taipei"
NAME_PREFIX = "YouBike2.0_"
ACT_VALUES = {"1": True, "0": False}

class TransformError(Exception):
    """資料存在問題時拋出此例外，清理流程不該默認忽略資料問題。"""

def to_dataframe(stations: list[dict]) -> pd.DataFrame:
    """將原始資料轉換為清理後的資料格式。"""
    return clean(pd.DataFrame(stations))

def clean(raw: pd.DataFrame) -> pd.DataFrame:
    # 1. 只保留會使用的欄位，並改成適合資料庫使用的欄位名稱。
    df = raw[list(RENAME)].rename(columns=RENAME)

    # 2. 處理文字欄位。"string" 會讓缺失值保持為 <NA>；
    #    如果使用 astype(str)，則會把缺失值轉成字面上的 "None"。
    #    station_id 刻意保留為文字，因為它是識別碼，不會拿來做數學運算。
    for col in TEXT_COLUMNS:
        df[col] = df[col].astype("string").str.strip()
    df["station_name"] = df["station_name"].str.removeprefix(NAME_PREFIX)

    # 3. 將 act 的 "1"/"0" 轉換成 True/False。
    #    如果出現其他值就停止處理：因為不知道其他值代表什麼，所以不能自行猜測。
    act = df["is_active"].astype("string").str.strip()
    unexpected = sorted(act[~act.isin(list(ACT_VALUES))].dropna().unique())
    if unexpected or act.isna().any():
        raise TransformError(f"Unexpected act values: {unexpected} (missing: {int(act.isna().sum())})")
    df["is_active"] = act.map(ACT_VALUES).astype(bool)

    # 4. 處理數量欄位。Int64（大寫 I）是 pandas 支援空值的整數型別：
    #    遇到缺失值時會保留為 <NA>，而不是迫使整個欄位轉成 float。
    for col in COUNT_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="raise").astype("Int64")

    for col in COORD_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="raise").astype("float64")

    # 5. 處理時間。來源字串本身沒有包含時區資訊；
    #    這些時間是台灣當地時間，因此在儲存之前明確指定為台灣時區。
    for col in TIME_COLUMNS:
        df[col] = pd.to_datetime(df[col], format=TIME_FORMAT).dt.tz_localize(SOURCE_TZ)

    # 6. 處理「同一次 API 回應」中的重複資料。
    #    完全相同的資料列可以安全移除。
    #    如果相同 station_id 卻有不同內容，代表資料發生衝突，
    #    此處無法自行判斷哪筆才正確，因此直接停止處理。
    #    （不同 fetch 之間的重複資料會在 Batch 3 處理。）
    df = df.drop_duplicates()
    conflicting = df.loc[df["station_id"].duplicated(keep=False), "station_id"].unique()
    if len(conflicting):
        raise TransformError(f"Same station_id with different values: {list(conflicting)[:10]}")

    return df.reset_index(drop=True)