# 開發紀錄與設計決策

記錄每個批次實際完成的內容、做出的設計決策與理由，以及已知限制。
整體架構與後續規劃以「YouBike 2.0 專案設計定版 v1.3」為準，本文件只記錄已實作的部分。

- 最後更新：2026-11-24
- 測試數量：648 項（transform 28、collect 39、schema 61、check_health 13、snapshot 100、
  snapshot_job 129、snapshot_job_db 26、repository 67、repository_db 34、api 69、api_db 10、line_webhook 39、deploy_api 33）。
  未設定測試資料庫時為 517 passed、131 skipped（schema、snapshot_job_db、repository_db、api_db 共 131 項需要資料庫）。
  Windows 本機另有 1 項符號連結測試 skip。

---

## 總覽

```
YouBike Open Data
      │
      ▼
fetch.py      下載 → 結構驗證            （Batch 1）
      │
      ├──► collect.py  gzip 原始檔 + 批次紀錄   （Batch 3）
      │
      ▼
transform.py  Pandas 清洗與型別轉換      （Batch 2）

collect.py 產生的 gzip 原始檔 + fetch_batches.jsonl
      │
      ▼
snapshot_job.py  每 5 分鐘一個時間槽：挑批次、讀原始檔        （4C-3）
      │          └─ snapshot.py 逐站驗證（內部呼叫 clean()）  （4C-2）
      ▼
PostgreSQL  fetch_batches、stations、station_snapshots         （4A、4C-3）
      │
      ▼
repository.py  StationRepository：最新快照查詢（psycopg_pool 連線池）  （API-1、API-2b）
      │
      ▼
api.py / app_main.py  FastAPI 三個查詢端點＋/health、/ready      （API-2a、API-2b）
      └─ line_webhook.py  POST /line/webhook（LINE 簽章驗證、文字回覆）  （LINE-0）
```

`collect.py` 只負責把原始資料保存下來，不碰資料庫。
`snapshot_job.py` 讀取 `collect.py` 的輸出，經 `snapshot.py` 驗證後寫入 PostgreSQL。
兩者只透過資料目錄銜接，擷取不受資料庫狀態影響。

部署：GCP e2-micro 上以 cron 每分鐘執行 `collect.py`（D0），每 5 分鐘執行 `snapshot_job.py`（D1），見下方。
API 尚未部署：API-3a 的部署檔已在 repo（`deploy/youbike-api.service`、`deploy/api-db-role.sql`、`deploy/Caddyfile`），
VM 安裝在 API-3b。

---

## Batch 1：資料擷取（`src/fetch.py`）

### 功能

- 以 `requests` 下載即時 JSON。
- 逾時設定 `(5, 30)`：建立連線最多 5 秒，讀取最多 30 秒。
- 網路錯誤、HTTP 錯誤、非 JSON 回應、結構不符，一律轉為 `FetchError`，呼叫端只需處理一種例外。
- `summarize()` 輸出站點數、行政區分布、`act` 統計、時間範圍等摘要。

### 設計決策

- **在資料入口就失敗**：上游曾將欄位從 `tot / sbi / bemp` 改名為 `Quantity / available_*`。
  因此用 `REQUIRED_FIELDS` 檢查必要欄位，欄位再改名時立即失敗，不讓錯誤資料流入下游。
- **只驗證結構，不驗證內容**：這一層只檢查最外層是否為非空 list、每筆是否為 dict、必要欄位是否存在。
  型別、數值範圍、缺失值等內容驗證交給 Batch 2。
- **下載與解析分開**（Batch 3 時拆分）：`fetch_raw()` 只回傳原始 bytes，`parse_stations()` 負責解析與驗證，
  `fetch_stations()` 組合兩者，對外行為不變。拆分是為了讓 Batch 3 能保存上游實際回傳的內容。

---

## Batch 2：資料清洗與探索（`src/transform.py`、`src/explore.py`）

### 欄位對應

| 原始欄位 | 清洗後 | 型別 |
|---|---|---|
| sno | station_id | string |
| sna | station_name | string（移除 `YouBike2.0_` 前綴） |
| sarea | district | string |
| ar | address | string |
| latitude / longitude | latitude / longitude | float64 |
| Quantity | total_spaces | Int64 |
| available_rent_bikes | available_bikes | Int64 |
| available_return_bikes | available_spaces | Int64 |
| act | is_active | bool |
| infoTime / srcUpdateTime / updateTime | info_time / src_update_time / update_time | datetime（Asia/Taipei） |

捨棄的欄位：英文版本欄位（`sareaen`、`snaen`、`aren`，標點格式不一致且目前不用），
以及與 `infoTime` 重複的 `mday`、`infoDate`。

### 設計決策

- **資料有問題就停止，不自行猜測**：所有資料層級的問題都以 `TransformError` 離開模組；程式錯誤則維持原本的例外類型，不混在一起。
- **`station_id` 保留為文字**：它是識別碼，不會拿來做數學運算。
- **用 `string` 而非 `astype(str)`**：後者會把缺失值變成字面上的 `"None"`。
- **用 `Int64`（可為空的整數）**：缺失值保留為 `<NA>`，不會讓整欄被迫變成 float。
- **`act` 只接受 `"1"` / `"0"`**：出現其他值就停止，因為不知道它代表什麼。
- **時間明確指定為台灣時區**：來源字串沒有時區資訊，但實際是台灣當地時間。
- **同一次回應中的重複資料**：完全相同的列直接移除；同一個 `station_id` 卻內容不同則視為衝突並停止。
  不同次擷取之間的重複資料留待後續批次處理。

### 資料探索（`explore.py`）

只觀察、不修正。檢查項目包括：缺失值與空字串、重複資料、`act` 的值、站名前綴、
重複的時間欄位、數量是否為負或超過總車位、座標是否超出臺北市範圍、各站資料延遲程度。

`total_spaces - available_bikes - available_spaces` 的差值意義目前未知，只記錄分布，不自行解釋。

---

## Batch 3：定時擷取與原始資料保存（`src/collect.py`）

### 功能

- `python src/collect.py`：擷取一次，適合交給 cron 等排程器每分鐘呼叫。
- `python src/collect.py --loop`：自己每分鐘擷取一次，用於在部署平台決定前先在本機累積資料。
- 原始回應存成 `data/raw/YYYY/MM/DD/<batch_id>.json.gz`（日期為 UTC）。
- 每批的結果寫入 `data/fetch_batches.jsonl`，一行一批。

### 批次紀錄欄位

| 欄位 | 說明 |
|---|---|
| batch_id | `20261027T081500Z-3f9a1c`，UTC 擷取時間＋隨機字串，避免同一秒撞號 |
| fetched_at | 實際擷取時間（UTC） |
| source_updated_at | 各站 `srcUpdateTime` 最大值，轉為 UTC；無法解析時為 null |
| status | `success` / `failed` |
| station_count | 站點數，失敗時為 null |
| raw_path | 原始檔相對路徑，下載失敗時為 null |
| error | 失敗原因 |

欄位與設計文件的 `fetch_batches` 資料表相同。

### 設計決策

- **先用 JSONL，之後再匯入資料庫**：讓排程在 PostgreSQL 建好之前就能開始累積資料。
  這是暫時的保存方式，不是架構變更。
- **`success` 的語意**：只代表下載成功並通過基本結構驗證，
  **不代表** Pandas 清洗成功，也不代表已寫入資料庫。那些步驟屬於後續批次，會有自己的狀態。
- **內容壞掉也保存原始檔**：下載成功但無法解析時，仍保存原始檔並標記 `failed`，方便追查上游改了什麼。
- **原始檔是事實來源，JSONL 是索引**：檔名含擷取時間，其餘欄位都能從原始檔重新算出，
  所以 JSONL 寫入失敗時，只要原始檔在就能重建該批次。
- **來源時間異常不影響保存**：`source_updated_at()` 永不拋出例外；
  空資料、缺欄位、型別錯誤的站點會被略過，全部無效時回傳 null。

### 可靠性處理

- **原子寫入**：先寫 `.tmp` 再以 `os.replace` 改名，中斷時不會留下寫到一半的 `.json.gz`；
  寫入或改名失敗時會清除 `.tmp`（包含 Ctrl+C 中斷）。
  清除 `.tmp` 本身若也失敗，不會蓋掉原本的錯誤；殘留的 `.tmp` 不會被當成原始檔。
- **JSONL 寫入**：
  - 每行以單次 write 寫入並 fsync。
  - 若上一行因先前失敗沒有換行結尾，先補換行，讓殘行與新紀錄分開。
  - 寫入失敗時拋出 `BatchLogError`，訊息中帶有完整紀錄，至少會留在 stderr／排程日誌。

### 排程行為（`--loop`）

- 只在整分鐘啟動，同一時間只有一次擷取在執行。
- 執行期間跨越的整分鐘直接跳過、不補抓，並在 stderr 記錄跳過次數。
- 跳過次數以「實際跨越的整分鐘邊界」計算，不看執行時間長度：
  晚 0.5 秒啟動、執行 59.7 秒，雖然不到一分鐘，仍跨過下一個整分鐘，算跳過 1 次。
- 單次執行的任何例外都只記錄 traceback，下一分鐘照常擷取；Ctrl+C 會正常結束。

### 執行規定：單一寫入者

同一個資料目錄，**同時只能有一個 `collect.py` 行程在寫入**。

原因：`append_batch()` 檢查上一行結尾（seek／read）與寫入新紀錄（write）是兩個步驟，
不是原子操作。單一行程時沒有問題，多個行程同時寫入同一個 JSONL 可能互相干擾。

- 本機或常駐執行：使用 `--loop`，本身保證不重疊。
- 使用 cron：必須以 `flock -n` 包住，前一次還沒結束時直接略過這一分鐘：

  ```
  * * * * * cd /path/to/youbike-data-mvp && flock -n /tmp/youbike-collect.lock python src/collect.py >> collect.log 2>&1
  ```

- `--loop` 與 cron 不可同時對同一個資料目錄執行。

不在程式中加檔案鎖的理由：`fcntl.flock` 只能在 Linux／macOS 使用，
而單一寫入者的規定已能涵蓋目前的使用情境，不需為此增加程式複雜度。

### 已知限制

1. **cron 未加 flock 時可能重疊**：違反上述規定時，JSONL 可能出現多餘空行或錯置的換行；
   被 flock 略過的分鐘也不會寫入紀錄，只能從 `fetched_at` 的間隔看出。
2. **單次擷取時間沒有上限**：讀取逾時是針對每次讀取，不是整個請求，上游很慢時單次執行可能超過一分鐘。
3. **下載失敗的紀錄可能只剩日誌**：這類批次沒有原始檔，若 JSONL 也寫入失敗，紀錄只存在 stderr。
4. **JSONL 殘行**：中斷的殘行目前只是被隔開成獨立一行，由 Batch 4 匯入時略過（見下方規格）。
5. ~~尚未以真實資料長時間驗證~~：D0 已在 VM 上連續擷取 24 小時，1,432 批全部成功（見下方 D0）。

### Batch 4 匯入規格（尚未實作）

> 4C-3a 讀取 JSONL 挑選批次時已實作第 1、2 點的「讀取」部分（壞行略過、同 `batch_id` 保留第一筆並計數），
> 見下方 4C-3a。把整個 JSONL 匯入資料表（4B）仍未實作。

將 `fetch_batches.jsonl` 匯入 PostgreSQL `fetch_batches` 時必須遵守：

1. **逐行讀取，損壞的行跳過，不中止整批匯入**：
   空行、無法解析為 JSON、不是物件、缺少 `batch_id` 的行一律略過，並記錄行號與原因。
2. **依 `batch_id` 去重**：
   - `batch_id` 設為資料表的唯一鍵。
   - 以 `INSERT ... ON CONFLICT (batch_id) DO NOTHING` 寫入，重複匯入同一份檔案不會產生重複資料（冪等）。
   - 同一個 `batch_id` 在檔案中出現多次且內容不同時，保留第一筆並記錄警告。
3. **匯入結束時輸出統計**：成功筆數、跳過的損壞行數、重複筆數。
4. 有原始檔但 JSONL 中沒有紀錄的批次，可由原始檔重建（檔名即 `batch_id`，含擷取時間）；
   是否在 Batch 4 實作，開工時再決定。

---

## Batch 4A：Schema 規格與 migration（`docs/schema.md`、`migrations/001_init.sql`）

### 4A-1：Schema 規格

四張表 `fetch_batches`、`stations`、`station_snapshots`、`station_hourly_stats` 的欄位、約束與
4C 寫入流程，都記錄在 [schema.md](schema.md)。

### 4A-2：migration 與約束測試

- **工具選擇**：純 SQL migration ＋ psycopg 3，不使用 Alembic 或 SQLAlchemy。
- **依賴位置**：4A-2 時只有測試會連資料庫，`psycopg[binary]>=3.2,<4` 放在 `requirements-dev.txt`。
  4C-1 已移到 `requirements.txt`（見下方 Batch 4C）。
- **重複執行策略**：
  - 整個檔案包在單一 transaction，任何一步失敗都全部回滾。
  - 刻意不用 `IF NOT EXISTS`，避免舊定義被靜默沿用，讓資料庫和規格悄悄不一致。
  - 只對全新 schema 執行一次，重複執行會失敗。後續變更開新檔 `002_*.sql`，不修改已套用的檔案。
- **測試隔離**：
  - 以環境變數 `YOUBIKE_TEST_DATABASE_URL` 指定測試資料庫，未設定時全部 skip。
  - 已設定時，無法連線、未安裝 psycopg、或資料庫名稱不含 `test`，一律報錯而非 skip，
    避免「以為測過，其實全部略過」，也避免誤用正式資料庫。
  - 每次執行建立隨機 schema，結束時只刪除該 schema；每個測試包在 transaction 中並回滾。
- **驗證**：本機（Windows，PostgreSQL 16.15）128 項全部通過。將約束退回修正前版本時，恰好 5 項 NULL 測試失敗，
  確認測試確實抓得到 CHECK 遇到 NULL 視為通過的漏洞。

---

## Batch 4C：最新快照寫入（完成，2026-11-22）

### 4C 準備：實測（2026-11-07，repo 未修改）

本機 `collect.py --loop` 週六清晨 30 分鐘、30 批，每批 1,824 站：

- 營運中站點 `info_time` 落後 P99＝最大＝2.0 分；超過 5 分鐘的觀測為 0。
- 各站 `info_time` 高度同步，適合偵測來源更新停滯，無法判斷單站設備離線。
- gzip 每批約 104 KB，每月約 4.3 GB。
- 限制：只有離峰樣本，且未保存 `fetch_batches.jsonl`，無法確認期間是否有失敗批次。

### 4C-1：寫入規則與依賴

- 寫入規則依設計定版 v1.2 4.4 寫入 [schema.md](schema.md) 第三、四、六節，4C 相關【待定】全部改為【4C 確認】。
  重點：新鮮度 `fetched_at − info_time > 10 分鐘`（暫定）、已有快照的槽補跑沿用原批次、
  整批 `clean()` 快速路徑＋逐站備援、無效原因判定順序、`result` 四種分類。
- `psycopg[binary]>=3.2,<4` 從 `requirements-dev.txt` 移到 `requirements.txt`（dev 透過 `-r requirements.txt` 繼承）。
- 本次只改文件與依賴，程式與測試未變。

### 4C-2：逐站驗證層（`src/snapshot.py`，`8c70a5a`）

依 [schema.md](schema.md) 第四、六節。不接觸資料庫，`tests/test_snapshot.py` 100 項。
同一個 commit 在 `pyproject.toml` 固定 ruff `target-version = "py310"`（本機為 Python 3.10）。

規格沒寫明、實作時做的判斷：

1. 結構無效（`missing_field`、`invalid_type`）的站不進入 `clean()`，和衝突站一樣排除；
   所以會觸發逐站備援的只剩可選欄位錯誤。
2. 備援重試只把轉換失敗的可選欄位清成 NULL，其餘欄位保留。
3. 原始值判定比 `clean()` 嚴格：`act` 只接受文字 `"1"`／`"0"`；數量接受整數、整數字串、整數值浮點數；
   布林值、`28.5`、`"1e3"` 判 `invalid_type`。
4. `sno` 接受文字與整數；布林、浮點數、list 等無法當識別碼，計為跳過。
5. 整批過期告警只看「營運中且有 `info_time`」的站，以落後時間判斷，不看 `invalid_reason`。
6. `path` 除了 `fast`、`fallback` 外多一個 `none`：沒有任何站需要 `clean()` 時使用。

### 4C-3a：時間槽、批次挑選、原始檔讀取、結果分類（`src/snapshot_job.py`，`a067b78`）

純函式部分，不接觸資料庫，`tests/test_snapshot_job.py`。

7. JSONL 讀取時就套用 `fetch_batches` 的 CHECK 約束，違反者當壞行略過，確保選中的批次寫入時不會被拒絕。
8. JSONL 檔案不存在 → 拋出例外，不回報 `no_batch`（避免資料目錄設錯被掩蓋）。
9. 有 error 時，寫入、已存在、失敗計數歸零；跳過數照實回報（來自寫入前的驗證）。
10. 處理結果 JSON 另含 `source`（`jsonl`／`existing`）與 `warnings`（整批過期、負落後、JSONL 壞行數、重複 ID 數）。
11. 原始檔解壓上限 20 MB（實測每批解壓後約 0.93 MB，保留約 20 倍餘裕）；
    路徑必須在資料目錄內（絕對路徑、`..`、指向外部的符號連結都拒絕）。

### Code Review 修正（`d653018`）

- `select_batch` 只接受去重後的 `BatchLog`，避免呼叫端傳入未去重的紀錄而挑錯批次。
- `RunCounts.validate()`：每個計數必須是非負整數（不接受 bool），無效原因必須是已知代碼；
  建立時檢查一次，分類與輸出報告前再檢查一次。
- 新增「整批 `clean()`」與「逐站備援」兩條路徑的結果一致性測試。
- 檢查後替換（TOCTOU）記為已知限制（見下方）。

### 4C-3b：資料庫寫入與 CLI（`src/snapshot_job.py`，`7c96560`）

`run_slot()` 與 CLI，`tests/test_snapshot_job_db.py` 26 項整合測試（需要測試資料庫）。

12. 整個流程在**單一 transaction** 內：先 `pg_advisory_xact_lock(4031, slot_epoch // 300)`，再查既有快照。
    鍵 `4031` 在整個資料庫內有效，日後其他用途不可重用（[schema.md](schema.md) 第六節）。
13. 先以一次查詢取出該槽已有快照的站，計為已存在、不碰 `stations`；其餘每站一個 savepoint：
    先 upsert `stations`（外鍵需要），再寫快照。快照衝突時回滾 savepoint，`stations` 不留下更新。
14. 「不早於最近快照槽才覆寫」由單一 `INSERT … ON CONFLICT DO UPDATE` 保證，不採先 SELECT 再 UPDATE；
    跨槽並行已用兩條連線測過兩種順序。
15. 單站出錯時，若 transaction 已失效（例如連線中斷）就中止整筆，不把後續每站都記成失敗。
16. `run_slot` 只接受 autocommit 連線；同槽衝突、原始檔錯誤、資料庫錯誤 → 回滾並回報 `failed`；
    其他例外（程式錯誤）回滾後往外拋，由 CLI 輸出 `failed` 結果與 traceback。
17. CLI：不指定 `--slot` 處理最近已結束的槽；`--slot` 須已結束，且槽起點不早於「目前時間 − 7 天」（剛好 7 天仍可）。
    只有 `main()` 讀取目前時間（以語法樹測試把關）。

CLI 介面（使用者 11/22 確認）：

- 資料庫連線字串讀環境變數 `YOUBIKE_DATABASE_URL`，資料目錄讀 `YOUBIKE_DATA_DIR`。
- 結束碼：`success`、`partial`、`no_batch` 為 0；`failed` 為 1；參數錯誤為 2。
- 每次執行輸出一行 JSON 到 stdout；單站寫入失敗的訊息寫到 stderr，由 cron 一起導向日誌。

### 4C 已知限制（接受）

- **檢查後替換（TOCTOU）**：檢查路徑與開檔之間若被換成指向外部的符號連結，仍可能讀到外部檔案。
  前提是已能寫入資料目錄（VM 權限 700、只有 `youbike` 可寫），且內容仍須通過結構驗證與解壓上限，
  因此不引入 `O_NOFOLLOW`。
- 同槽鎖在讀 gzip 與驗證期間持續持有：正常約 0.1 秒，逐站備援最長約 7 秒。
- 每次執行讀完整個 JSONL：到 12/11 約 7 MB，可接受。

### 4C-3 真實資料驗收（2026-11-22）

- 資料：VM 上 UTC 11/22 01:00～02:00 的 60 個原始檔＋JSONL；程式：`7c96560`；
  環境：PostgreSQL 16.15（新建資料庫並套用 migration）。
- 12 個槽全部 `success`；每槽有效 1,805、`inactive` 21（合計 1,826），跳過、失敗、負落後、JSONL 壞行皆 0；
  每槽都挑到槽內最晚的批次。
- 同槽重跑：`success`、`source = existing`、`existing = 1826`、寫入 0、結束碼 0。
- 資料庫：`fetch_batches` 12、`stations` 1,826（名稱、座標全有）、快照 21,912（有效 21,660）、每槽來源批次最多 1 個。
- 營運中站點落後時間：中位數 1.98 分、最大 2.00 分（門檻 10 分）。

### 4C 效能紀錄（不代表 e2-micro，D1 再量）

| 環境 | 1,826 站第一次寫入 | 同槽重跑 | 只做 1,826 次 SAVEPOINT／RELEASE |
|---|---|---|---|
| 雲端容器（PostgreSQL 同機） | 0.86 秒 | 0.07 秒 | 0.24 秒 |
| 本機 Windows | 1.05 秒 | 0.11 秒 | 0.23 秒 |
| CLI 真實資料（含啟動 Python、載入 pandas） | 每槽約 1.2 秒 | 0.45 秒 | — |

每站 4 次資料庫往返（SAVEPOINT、upsert、insert、RELEASE），第一次寫入共約 7,310 次。

---

## D0：最小擷取部署（`deploy/`，`43140f8`、`90efb40`）

部署方式、安裝步驟與驗收紀錄見 [deploy/README.md](../deploy/README.md)，這裡只記重點。

- **環境**：GCP e2-micro（1 GB RAM、2 GB swap）、標準永久磁碟 30 GB、Ubuntu 24.04 LTS Minimal、Python 3.12.3。
  VM 上沒有任何金鑰。D0 時記為「防火牆只開 SSH」有誤：建立 VM 時已勾選允許 HTTP／HTTPS，
  且預設網路開放 RDP；當時 80、443、3389 都沒有程式在聽。11/22 D1-c 已收斂（見下方 D1）。
- **執行方式**：專用使用者 `youbike`；cron（UTC）每分鐘以 `flock -n` 執行 `collect.py`（單一寫入者），
  每 5 分鐘執行 `deploy/check_health.py`（最近成功擷取超過 10 分鐘、磁碟使用率超過門檻時輸出 `WARN`）。
- **日誌**：logrotate 每日、保留 14 天、單檔 20 MB；journald 上限 200 MB、14 天。原始 gzip 與 JSONL 12/11 前不刪。
- **驗收（定版 v1.3 11.2）**：
  - B（兩項警告各觸發一次）、C（資源基準）：2026-11-21 通過。
  - **A（連續 24 小時）：2026-11-22 通過。** UTC 11/21 06:16 至 11/22 06:20 共 1,432 批，success 1,432、failed 0；
    與「每分鐘一批、扣除刻意暫停的 14 分鐘、加上手動執行 1 筆」的推算相符。`WARN` 只有 B 驗收留下的一筆。
- **資料量**：每批原始檔約 105 KB，每天約 150 MB，到 12/11 約 3 GB（磁碟約 10%）。
  11/22 實測 4 小時增加 24 MB，與此一致。
- **資源（11/22 UTC 06:20）**：available 629 MB、swap 已用 22 MB、磁碟 17%、`data/` 148 MB。
- **已知缺口**：UTC 11/21 06:23～06:36 為驗收刻意暫停，約 14 分鐘無資料，不補抓。

---

## D1：完整部署（2026-11-22 完成）

部署步驟、設計與完整驗收紀錄見 [deploy/README.md](../deploy/README.md) 的 D1-a、D1-b、D1-c 各節。

- **D1-a：PostgreSQL 與 4C 排程**（`80d209c`）
  - PostgreSQL 16.15，`listen_addresses = ''`（只用 Unix socket、不開 TCP），peer 驗證，VM 上沒有資料庫密碼。
  - cron 每 5 分鐘的第 1 分鐘執行 4C（`flock -n -E 75`，被擋下時寫一行 `skipped`）。
  - 驗收：連續 12 槽 `success`；每槽快照數等於來源批次 `station_count`（1,826）；4C 單次最大記憶體約 96.6 MB、
    e2-micro 耗時約 5.8 秒；OOM 0。版本：Python 3.12.3、pandas 3.1.1、psycopg 3.3.9。
- **D1-b：固定 IP、網域、Caddy HTTPS**（`8ccd404`）
  - 固定 IP 35.230.112.185、`youbike-data-mvp.duckdns.org`（DuckDNS 手動設定 IP，VM 上沒有 Token）。
  - Caddy 2.11.9：只有 `/healthz` 回 200，其他路徑 404、內容為空、無 `Server` 標頭；Let's Encrypt 憑證自動續期。
  - Ubuntu 與 Caddy 皆納入 `unattended-upgrades`；Caddy 日誌只寫 journald。
  - 部署後 4C 連續 30 槽正常；available 636 MB、Caddy 約 40 MB、OOM 0。swap 2.5 小時增加 14 MB，持續觀察。
- **D1-c：SSH 只經 IAP**
  - 新增 `allow-ssh-from-iap`（35.235.240.0/20 → 22），停用 `default-allow-ssh`、`default-allow-rdp`。
  - 不經 IAP 直接連 22、3389 皆被擋下；瀏覽器 SSH 自動走 IAP，可正常登入。

**已知事項**：VM 的 pandas 為 3.1.1，本機 Python 3.10 無法安裝 pandas 3；依賴版本的可重現性在 12/6 封板前處理。

---

## API：站點查詢（`src/repository.py`、`src/api.py`、`src/app_main.py`）

### API-1：Repository（`5fe3fa8`、`4683101`）

- `StationRepository`（`typing.Protocol`）：`get_station`、`search_by_name`、`find_nearby`；API 與日後的 LLM 工具共用。
- `StationStatus`（frozen dataclass）：欄位照設計定版 8.4，另加 `distance_m`（只有附近查詢會填）。
- **「最新」**：每站以主鍵 `(station_id, slot_time)` 倒序取一筆，再以該批次 `fetched_at` 判斷
  `now − fetched_at ≤ 30 分鐘`；不在視窗內不回傳，**不以舊快照補位**；最新一筆無效時照樣回傳並附 `invalid_reason`。
- 站名搜尋 `ILIKE`（`\`、`%`、`_` 跳脫）；附近查詢以方框預篩（處理跨 ±180°、極點）再以 haversine 精算。
- 參數錯誤在查詢前丟 `InvalidQueryError`，不截斷；查無資料時單站 `None`、列表 `[]`。使用者輸入一律參數綁定。
- **時區修正**（`4683101`）：psycopg 依連線的 `TimeZone` 回傳 `timestamptz`，本機（Asia/Taipei）與雲端（UTC）不同；
  DTO 時間一律轉 UTC，測試涵蓋 `SET TIME ZONE 'Asia/Taipei'`。
- 效能（雲端，7 天份假資料 368 萬列）：單站 3 ms、全站名符合的搜尋 34 ms、3 公里附近 13 ms；
  查詢計畫為 `Index Scan Backward using station_snapshots_pkey`，不需新增索引。VM 實測留到 API-3。

### API-2a：FastAPI 端點與 Mock（`458070e`）

- `GET /stations/nearby`、`GET /stations?q=`、`GET /stations/{station_id}`；列表回 `{"count", "items"}`，單站查無 404。
- 參數錯誤 422：型別錯誤由 FastAPI 檢查（`detail` 為陣列），範圍與長度為 `InvalidQueryError`（`detail` 為一句中文）。
  兩種格式刻意維持。
- `create_app(repository)`：正式環境傳 PostgreSQL 實作，測試傳 `InMemoryStationRepository`（Mock），不需資料庫。
- 端點為同步函式（psycopg 同步連線），由 FastAPI 放到執行緒池執行。

### Code Review 修正（`bb46363`）

- 站名排序改為 `COLLATE "C"`（Unicode 碼位），與 Python、Mock 一致，不受資料庫定序影響；
  修正前在 ICU zh-TW 資料庫上排序不同。Mock 的站名比對註明為近似實作（`casefold` vs `ILIKE`）。
- 不採納：排除未來時間的 `fetched_at`（寫入與讀取在同一台 VM、同一時鐘；時鐘往回調時加上限會讓全部站消失）。

### API-2b：連線池、`/health`、`/ready`

- **連線池**（`psycopg_pool`）：`min_size=1`、`max_size=4`、借不到連線 5 秒逾時；借出前檢查連線（`check_connection`）。
  - 理由：VM `max_connections = 20`，扣除保留連線、4C 與手動 psql 後約剩 15；FastAPI 執行緒池預設 40，
    若每個請求開新連線，尖峰時可能耗盡連線並擋住 4C。連線池讓一個 app instance 最多 4 條，其餘請求排隊。
- `PooledStationRepository`：每次查詢以 `pool.connection()` 借還（含例外路徑），查詢內容沿用 `PostgresStationRepository`。
  參數先驗證，不合法時不借連線（資料庫離線時參數錯誤仍是 422）。
- **錯誤分類**：只有「連不到資料庫」轉為 `RepositoryUnavailableError` → HTTP 503：借不到連線、連線池已關閉、
  查詢途中連線中斷（`conn.broken`）。SQL 錯誤、連線仍正常的 `OperationalError`（例如查詢逾時）、程式錯誤照原樣丟出（500）。
  503 回應只有固定訊息，不含連線字串或錯誤細節；細節只寫伺服器端日誌。
- `/health`：不碰資料庫，固定 200。`/ready`：用同一個連線池執行 `SELECT 1`，失敗 503 `{"status": "unavailable"}`。
- `src/app_main.py`：`create_production_app()` 讀 `YOUBIKE_DATABASE_URL`（未設定或空白時直接失敗），
  在 FastAPI lifespan 以 `open(wait=False)` 開啟連線池、結束時關閉。資料庫離線時 app 照樣啟動，
  `/health` 200、`/ready` 503；資料庫恢復後連線池在背景重連，`/ready` 轉回 200。
  `api.py` 不匯入 psycopg，仍可只用 Mock 測試。
- 啟動方式（API-3 才加入 uvicorn）：`uvicorn app_main:create_production_app --factory`。
- 測試：單元 30 項（假連線池：錯誤分類、借還、open/close 呼叫）；整合 10 項（真 PostgreSQL，連線時區 Asia/Taipei）：
  - 連線數不超過 4（佔滿 4 條時第 5 個請求 503，歸還後 200）、併發請求後全部 `idle`；
  - 例外路徑（程式錯誤、SQL 錯誤、查詢逾時）後沒有 idle in transaction，且錯誤類型不變；
  - 連線被中斷（`pg_terminate_backend`）→ 503 類錯誤，之後自動換新連線；
  - lifespan 結束後連線池關閉、`pg_stat_activity` 中該 app 的連線歸零；
  - 以 TCP 轉送器模擬資料庫斷線：離線啟動 503 → 恢復 200 → 執行中斷線 503 → 再恢復 200。
- **已知限制**：連線池關閉後不能重新開啟，一個 app 物件只能經歷一次 lifespan（uvicorn 正常使用不受影響）。
  資料過期提示不在 API-2b 範圍（LINE Bot 顯示時再決定）。

### API-3a：部署檔（2026-11-23，只改 repo，VM 未變更）

- **`deploy/youbike-api.service`**：uvicorn 只綁 `127.0.0.1:8000`、單一 worker、`--no-access-log`（查詢字串含使用者座標）；
  以系統使用者 `youbike_api` 執行；程式與虛擬環境放 `/opt/youbike-api`（root 擁有、服務唯讀），不使用 `/home/youbike`；
  `MemoryHigh=200M`、`MemoryMax=300M`（暫定，API-3b 依記憶體、swap、OOM 實測調整）；`ProtectSystem=strict` 等權限收斂；
  LINE 金鑰以 `EnvironmentFile=-/etc/youbike/api.env` 預留（檔案不存在時照常啟動，webhook fail closed）。
- **`deploy/api-db-role.sql`**：唯讀角色 `youbike_api`。peer 驗證要求系統使用者名稱＝角色名稱，兩者都用底線命名，
  不依賴 `pg_ident.conf`。只有三張表的 SELECT；預設唯讀交易、查詢逾時 5 秒、閒置交易 30 秒、連線上限 6。可重複執行。
- **`deploy/Caddyfile`**：白名單、精確比對、預設 404。只轉送 `GET /health`、`GET /stations`、`GET /stations/nearby`、
  `GET /stations/{station_id}`（`^/stations/[A-Za-z0-9_-]{1,32}$`）、`POST /line/webhook`（body ≤ 1 MB）；
  `/ready`、`/docs`、`/openapi.json` 等不對外。`/healthz` 保留。
- **`deploy/README.md`〈API-3〉**：設計、安裝步驟（API-3b）、驗收、更新程式、回滾、已知限制。
- **測試**（`tests/test_deploy_api.py`，33 項，靜態檢查）：unit 檔身分、綁定、worker、access log、記憶體、權限收斂；
  Caddyfile 無萬用字元、白名單、站號正規式邊界、預設 404、只轉送本機；SQL 只有 SELECT、唯讀與逾時設定。
  突變測試：改 worker 數、綁 0.0.0.0、使用者改連字號、拿掉 `--no-access-log`、放寬記憶體與 `ProtectSystem`、
  加萬用字元或 `/ready`、放寬站號正規式、預設改 200、轉送到非本機、加 INSERT、關閉唯讀、多授權一張表，各至少 1 項失敗。
- **雲端預演**（Ubuntu 24.04＋PostgreSQL 16，與 VM 相同；未使用 systemd）：建立 `youbike` 資料庫與 migration、
  執行角色 SQL（兩次）、以 `youbike_api` 身分透過 peer 驗證連線；寫入被拒（唯讀交易、無表權限、無 `station_hourly_stats` 權限）；
  以 unit 檔的參數啟動 uvicorn，`/health`、`/ready`、三個查詢 200、沒有 `server` 標頭、webhook 503、連線 `application_name=youbike-api`、
  常駐記憶體約 113 MB（Python 3.13）；回滾 SQL（`DROP OWNED BY`＋`DROP ROLE`）後可重建。`systemd-analyze verify` 只回報
  `/opt/youbike-api/.venv/bin/uvicorn` 不存在（預期）。Caddyfile 未在雲端驗證，API-3b 以 `caddy validate` 檢查。

---

## LINE Bot（`src/line_webhook.py`）

### LINE-0：Webhook 與文字回覆（2026-11-23）

- **分工**：LINE 協定交給官方 SDK `line-bot-sdk` v3（`WebhookParser` 驗證 `X-Line-Signature` 並解析事件、
  `MessagingApi` 呼叫 Reply API）；路由、安全邊界與錯誤處理自己寫。冪等、限流、額度、查詢指令在後續階段。
- **端點**：`POST /line/webhook`（不列入 `/docs`）。文字訊息固定回「收到：<訊息>」，超過 LINE 上限 5,000 字截斷；
  貼圖、加好友、未知類型等事件略過。
- **狀態碼**：
  - 200：簽章正確並處理完畢，包含 LINE 後台 Verify 的空事件、略過的事件，以及回覆失敗（只寫日誌；
    回 500 會讓 LINE 重送，而 reply token 已用過或過期，重送只會再失敗）。
  - 400：缺少或錯誤的簽章、body 不是 UTF-8 或不是合法的 webhook JSON；不處理任何事件。
  - 413：body 超過 1 MiB。
  - 503：**金鑰未設定時 fail closed**。`LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN` 任一未設定或空白，
    webhook 一律 503，不讀 body、不驗證簽章（不會用空字串當 secret）；查詢 API 照常運作，啟動時寫一筆 WARNING。
- **不卡住事件迴圈**：讀原始 body 需要 async 端點；SDK 的回覆是同步 HTTP 呼叫，以 `run_in_threadpool` 執行。
- **日誌**：不記錄 secret、access token、reply token、簽章值、訊息內容與 **LINE userId**；回覆失敗只記例外類型與 HTTP status。
  每個事件記一行 `type=… user=…`，`user` 為「HMAC-SHA256(由 channel secret 衍生的金鑰, userId)」的十六進位前 8 碼
  （同一使用者代碼固定、無法反推；沒有 userId 時記 `-`）。衍生金鑰加上用途標籤，與簽章驗證分開。
- **SDK 注意事項**：
  - `InvalidSignatureError` 必須從 `linebot.v3.exceptions` 匯入；舊版 `linebot.exceptions` 的同名類別是不同的類別，
    接不到 v3 parser 的例外（簽章錯誤會變成 500）。
  - SDK 對格式不符的事件不丟例外，而是轉成 `UnknownEvent`（例如文字訊息缺少 `quoteToken`）；測試資料使用 LINE 實際格式。
  - 版本下限 3.14.1：3.14.0 在 Python 3.10 匯入時有語法錯誤；更早的 3.x 把 aiohttp 鎖在舊版。
- **依賴**：`line-bot-sdk>=3.14.1,<4`（連帶 aiohttp、pydantic 2 等，記憶體在 API-3 驗收時一起量）、
  `uvicorn>=0.30,<1`（本機以 ngrok 開發時啟動用；systemd 在 API-3）。
- **測試**（39 項，不連網）：簽章由測試以 HMAC-SHA256＋base64 自行計算；正確／錯誤／缺少／竄改 body／別的 secret；
  Verify 空事件；fail closed（含攻擊者以空字串 secret 簽章）；多事件依序回覆；回覆失敗仍 200 且不影響其他事件；
  格式錯誤 400；超過大小 413；日誌不含機密與原始 userId（含群組、回覆失敗、非文字事件路徑）、只出現 HMAC 代碼；回覆進行中 `/health` 仍可回應；以替換 HTTP 層檢查 SDK 送出的
  Reply API 請求（URL、`Authorization`、replyToken、訊息）；`app_main` 依環境變數掛載與 fail closed。
- 突變測試：略過簽章驗證、改用舊版例外類別、回覆不放執行緒、拿掉 fail closed、縮小例外範圍、日誌寫入簽章、日誌寫入原始 userId、userId 未加金鑰雜湊、
  不截斷、拿掉缺少簽章檢查，各至少 1 項測試失敗。

---

## 下一步

12/11 必要功能：資料擷取、資料庫、API、LINE Bot、LLM Function Calling。

1. **API-3b（部署）**：依 `deploy/README.md`〈API-3〉在 VM 安裝（API-3a 部署檔已完成）；
   VM 上的記憶體與查詢計畫驗收。
2. **LINE Bot**：`webhookEventId` 冪等、定位查詢、固定指令；限流與額度（簽章驗證與文字回覆已在 LINE-0 完成）。
   個資處理（2026-11-23 決定）：
   - **migration `002` 建表時，LINE userId 一律存 HMAC 值、不存原始值**，並設定保存期限（期限與清理方式建表時決定）。
     建議（建表時確認）：資料表用的 HMAC 金鑰與日誌用的衍生金鑰分開管理（例如獨立環境變數），
     避免 channel secret 重新發行後所有已存的雜湊值失效。日誌代碼隨 channel secret 改變可以接受。
   - follow（加好友）事件回覆歡迎訊息時**附隱私說明**：記錄哪些資料、用途、保存多久。
3. **LLM Function Calling**：最小範圍 1～2 個工具。送給 OpenAI 之前先做 **PII 遮罩**（例如電話、email、身分證字號等），
   且不送出 LINE userId。

已決定、不再列為待辦：擷取排程執行環境與原始檔儲存位置（A1、A2：GCP e2-micro、VM 持久磁碟）、
4C-2、4C-3、D1（2026-11-22 完成）、API-1、API-2a、API-2b（2026-11-22 完成）、LINE-0 程式與測試（2026-11-24；LINE 後台與手機實測待本機驗收）。
