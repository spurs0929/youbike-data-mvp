# 開發紀錄與設計決策

記錄每個批次實際完成的內容、做出的設計決策與理由，以及已知限制。
整體架構與後續規劃以「YouBike 2.0 專案設計定版 v1.0」為準，本文件只記錄已實作的部分。

- 最後更新：2026-10-27
- 測試數量：63 項（transform 28 項、collect 35 項）

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
```

目前三個模組彼此獨立：`collect.py` 只負責把原始資料保存下來，
尚未串接 `transform.py`，也尚未寫入資料庫。

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
- **JSONL 寫入**：
  - 每行以單次 write 寫入並 fsync。
  - 若上一行因先前失敗沒有換行結尾，先補換行，讓殘行與新紀錄分開。
  - 寫入失敗時拋出 `BatchLogError`，訊息中帶有完整紀錄，至少會留在 stderr／排程日誌。

### 排程行為（`--loop`）

- 只在整分鐘啟動，同一時間只有一次擷取在執行。
- 單次執行超過一分鐘時，期間經過的整分鐘直接跳過、不補抓，並在 stderr 記錄跳過次數。
- 單次執行的任何例外都只記錄 traceback，下一分鐘照常擷取；Ctrl+C 會正常結束。

### 已知限制

1. **cron 模式可能重疊**：不重疊只在 `--loop` 模式保證。改用 cron 時建議加上 `flock -n`。
2. **單次擷取時間沒有上限**：讀取逾時是針對每次讀取，不是整個請求，上游很慢時單次執行可能超過一分鐘。
3. **下載失敗的紀錄可能只剩日誌**：這類批次沒有原始檔，若 JSONL 也寫入失敗，紀錄只存在 stderr。
4. **JSONL 殘行**：中斷的殘行目前只是被隔開成獨立一行，之後匯入時讀取端需略過無法解析的行。
5. **尚未以真實資料長時間驗證**：測試皆使用假資料。

---

## 下一步

1. 以真實資料執行 `collect.py --loop`，確認擷取正常。
2. 實測每批原始檔大小，估算每日與 90 天的儲存量。
3. 決定擷取排程的執行環境與原始檔的儲存位置（設計文件附錄 A 的 A1、A2）。
4. 建立 PostgreSQL 資料表（`stations`、`fetch_batches`、`station_snapshots`），
   並將 `fetch_batches.jsonl` 匯入。
