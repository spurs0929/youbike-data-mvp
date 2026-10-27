# YouBike 2.0 資料處理與智慧查詢系統

以 Python 擷取臺北市 YouBike 2.0 即時 Open Data，清洗、保存並累積歷史資料，
最終提供站點查詢 API 與 LINE Bot 智慧查詢。

作品主軸：**Python Backend + Data Pipeline + LLM Integration**

> 目前處於資料管線階段。下方「開發進度」只列出已實作的功能，
> 其餘為規劃中項目，尚未完成。

## 開發進度

| 階段 | 內容 | 狀態 |
|---|---|---|
| Batch 1 | 資料擷取、HTTP 逾時、JSON 結構驗證 | ✅ 完成 |
| Batch 2 | Pandas 清洗、型別轉換、資料探索 | ✅ 完成 |
| Batch 3 | 每分鐘擷取、gzip 原始檔保存、批次紀錄 | ✅ 完成 |
| Phase 1 其餘 | PostgreSQL 持久化、五分鐘快照、每小時彙總 | 規劃中 |
| Phase 2 | 站點查詢 API、Redis 快取 | 規劃中 |
| Phase 3 | LINE Bot（Webhook、定位查詢） | 規劃中 |
| Phase 4 | OpenAI Function Calling、使用額度控制 | 規劃中 |

各批次的設計決策與細節見 [docs/progress.md](docs/progress.md)。

## 資料來源

臺北市 YouBike 2.0 即時資料：

```
https://tcgbusfs.blob.core.windows.net/dotapp/youbike/v2/youbike_immediate.json
```

## 專案結構

```
src/
  fetch.py      Batch 1：下載與結構驗證
  transform.py  Batch 2：Pandas 清洗
  explore.py    Batch 2：資料探索報告
  collect.py    Batch 3：定時擷取、保存原始檔與批次紀錄
tests/
  test_transform.py
  test_collect.py
docs/
  progress.md   開發紀錄與設計決策
```

## 環境需求

- Python 3.10 以上

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

## 使用方式

所有指令都從專案根目錄執行。

```bash
# 擷取一次並印出資料摘要
python src/fetch.py

# 資料探索報告（可指定已存檔的 JSON，方便重複測試）
python src/explore.py
python src/explore.py data/raw.json

# 擷取一次並保存原始檔（適合交給 cron 每分鐘呼叫）
python src/collect.py

# 每分鐘持續擷取，按 Ctrl+C 結束
python src/collect.py --loop
```

`collect.py` 預設將資料寫入 `data/`，可用環境變數 `YOUBIKE_DATA_DIR` 指定其他位置。

> **同一個資料目錄同時只能有一個 `collect.py` 在寫入。**
> 使用 cron 時必須用 `flock -n` 包住，且不可與 `--loop` 同時執行，詳見
> [docs/progress.md](docs/progress.md#執行規定單一寫入者)。
>
> ```
> * * * * * cd /path/to/youbike-data-mvp && flock -n /tmp/youbike-collect.lock python src/collect.py >> collect.log 2>&1
> ```

### 輸出內容

```
data/
  raw/YYYY/MM/DD/<batch_id>.json.gz   原始回應，gzip 壓縮（日期為 UTC）
  fetch_batches.jsonl                 擷取批次紀錄，一行一批
```

`batch_id` 格式為 `20261027T081500Z-3f9a1c`：前半段是 UTC 擷取時間，後半段為隨機字串。

批次紀錄的 `status`：

- `success`：下載成功，且通過基本結構驗證。**不代表**清洗或資料庫寫入成功。
- `failed`：下載失敗，或內容無法通過結構驗證。內容有問題時仍會保存原始檔，方便追查。

## 測試

```bash
pytest
ruff check .
ruff format --check .
```

測試全部使用假資料，不需連網。

## 授權與資料使用

資料來源為臺北市政府開放資料。本專案為職訓結訓作品與個人作品集用途。
