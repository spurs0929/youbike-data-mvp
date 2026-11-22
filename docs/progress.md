# 開發紀錄與設計決策

記錄每個批次實際完成的內容、做出的設計決策與理由，以及已知限制。
整體架構與後續規劃以「YouBike 2.0 專案設計定版 v1.3」為準，本文件只記錄已實作的部分。

- 最後更新：2026-11-22
- 測試數量：396 項（transform 28、collect 39、schema 61、check_health 13、snapshot 100、
  snapshot_job 129、snapshot_job_db 26）。
  未設定測試資料庫時為 309 passed、87 skipped（schema 與 snapshot_job_db 共 87 項需要資料庫）。

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
```

`collect.py` 只負責把原始資料保存下來，不碰資料庫。
`snapshot_job.py` 讀取 `collect.py` 的輸出，經 `snapshot.py` 驗證後寫入 PostgreSQL。
兩者只透過資料目錄銜接，擷取不受資料庫狀態影響。

部署：GCP e2-micro 上以 cron 每分鐘執行 `collect.py`（D0，見下方）。
`snapshot_job.py` 的排程屬於 D1，尚未部署。

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
  防火牆只開 SSH，VM 上沒有任何金鑰。
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

## 下一步

12/11 必要功能：資料擷取、資料庫、API、LINE Bot、LLM Function Calling。

1. **D1**：VM 上部署 PostgreSQL 與 4C 排程、Caddy HTTPS、記憶體檢查、VM 與本機套件版本對齊。
2. **API**（FastAPI）：`StationRepository` 介面、DTO、PostgreSQL 實作。
3. **LINE Bot**：簽章驗證、`webhookEventId` 冪等、定位查詢、固定指令；限流與額度。
4. **LLM Function Calling**：最小範圍 1～2 個工具。

已決定、不再列為待辦：擷取排程執行環境與原始檔儲存位置（A1、A2：GCP e2-micro、VM 持久磁碟）、
4C-2、4C-3（2026-11-22 完成）。
