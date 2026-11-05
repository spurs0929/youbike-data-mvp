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
| Batch 4A | Schema 規格與 migration | ✅ 完成 |
| Phase 1 其餘 | 五分鐘快照寫入、每小時彙總 | 規劃中 |
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
migrations/
  001_init.sql  Batch 4A：建立四張資料表與索引
tests/
  test_transform.py
  test_collect.py
  test_schema.py  Batch 4A：資料表約束測試（需要測試資料庫）
docs/
  progress.md   開發紀錄與設計決策
  schema.md     資料表規格與約束
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

## 資料庫設定

資料表規格見 [docs/schema.md](docs/schema.md)。目前只完成 schema 與約束測試，
應用程式尚未寫入資料庫，因此只有 `tests/test_schema.py` 會連線。

需要 PostgreSQL 16 與 `psql`。建立測試用的使用者與資料庫（密碼由提示輸入，不寫在指令中）：

```bash
createuser -U postgres --pwprompt youbike_test
createdb -U postgres -O youbike_test -E UTF8 -T template0 youbike_test_db
```

### 套用 migration

```bash
psql -v ON_ERROR_STOP=1 -h <host> -U <user> -d <database> -f migrations/001_init.sql
```

- 同一個 schema 只能執行一次，重複執行會失敗（`relation already exists`），且不留下部分變更。
- 之後的變更以 `002_*.sql` 等新檔案進行，不修改已套用的檔案。
- **跑測試不需要手動套用**：測試會自行在隨機 schema 中套用。
  手動試跑時請另建一個全新、可丟棄的資料庫，不要用 `youbike_test_db`。

## 測試

```bash
pytest
ruff check .
ruff format --check .
```

除資料庫測試外都使用假資料，不需連網。

資料庫測試（`tests/test_schema.py`）由環境變數 `YOUBIKE_TEST_DATABASE_URL` 控制：

| 情況 | 結果 |
|---|---|
| 未設定 | 資料庫測試全部 skip，其餘照常執行 |
| 已設定，但無法連線 | 報錯，不會 skip |
| 已設定，但未安裝 psycopg | 報錯，不會 skip |
| 已設定，但資料庫名稱不含 `test` | 報錯，不會 skip，也不會建立任何物件 |

每次執行會建立隨機命名的 schema（`youbike_test_xxxxxxxx`），結束時只刪除該 schema，
每個測試也會回滾，不影響資料庫中的其他資料。

連線字串不要包含密碼，請以 `PGPASSWORD` 或 pgpass 檔提供。

```bash
# Linux／macOS
read -rs PGPASSWORD && export PGPASSWORD
export YOUBIKE_TEST_DATABASE_URL="postgresql://youbike_test@localhost:5432/youbike_test_db"
pytest -rs
```

Windows PowerShell 請用提示輸入。直接寫 `$env:PGPASSWORD = "..."` 時，
密碼中的特殊字元可能被 PowerShell 改掉，導致 `password authentication failed`：

```powershell
$s = Read-Host "youbike_test 密碼" -AsSecureString
$env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
Remove-Variable s
$env:YOUBIKE_TEST_DATABASE_URL = "postgresql://youbike_test@localhost:5432/youbike_test_db"
pytest -rs
```

也可以改用 pgpass 檔，就不需要設定 `PGPASSWORD`。Windows 的位置是
`%APPDATA%\postgresql\pgpass.conf`，Linux／macOS 是 `~/.pgpass`（權限須為 `600`）。內容一行：

```
localhost:5432:youbike_test_db:youbike_test:<你的密碼>
```

此檔位於家目錄，不在專案內，不會進入 Git。

## 授權與資料使用

資料來源為臺北市政府開放資料。本專案為職訓結訓作品與個人作品集用途。
