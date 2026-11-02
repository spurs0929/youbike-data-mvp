# PostgreSQL Schema 規格（Batch 4A-1）

- 依據：《YouBike 2.0 專案設計定版 v1.1》第四～六節
- 狀態：**規格與 SQL 草案**。尚未建立資料庫，也尚未撰寫 migration；正式 DDL 於 4A-2 依本文件產生。
- 最後更新：2026-11-02

本文件中的標記：

- **【定版】**：v1.1 已確定的設計。
- **【4A-1 確認】**：本次規格審閱時確認的約束（2026-11-02）。
- **【待定】**：v1.1 附錄 A 的待議事項，或屬於後續 Batch 的實作細節。本文件只記錄，不做決定。

---

## 一、總覽

```
fetch_batches ──1:N──> station_snapshots <──N:1── stations
                                                     │
                                                    1:N
                                                     ▼
                                           station_hourly_stats
```

| 資料表 | 角色 | 寫入者 | 保留 |
|---|---|---|---|
| `fetch_batches` | 擷取批次紀錄，對應 `fetch_batches.jsonl` | 4C（用到的批次）、4B（歷史批次紀錄回補） | 長期 |
| `stations` | 站點主檔 | 4C | 長期，不刪除 |
| `station_snapshots` | 每站每 5 分鐘的觀測值 | 4C、歷史快照重建 | 7 天（4E 清理） |
| `station_hourly_stats` | 每站每小時彙總 | 4D | 長期 |

### 共通原則

1. **時間一律為 `timestamptz`，以 UTC 寫入**【定版】。`transform.py` 輸出的 Asia/Taipei 時間在寫入時轉換，不另存當地時間字串。
2. **資料庫約束是最後防線，不取代驗證**【4A-1 確認】。資料是否有效（`is_valid`）由 Python 的清洗與逐站驗證流程判斷；資料庫只拒絕「邏輯上不可能」的組合，例如標記為有效卻缺少數值。
3. **主檔可放寬，觀測資料要可判斷**【4A-1 確認】。`stations` 除了 `station_id` 與系統產生的 `first_seen_at`、`last_seen_at` 外，其餘站點欄位都允許 NULL，避免單站資料缺漏造成外鍵失敗或整批失敗。放寬 NULL 不代表該站的資料有效。
4. **CHECK 約束必須明確排除 NULL**【4A-1 確認】。PostgreSQL 的 CHECK 只在結果為 `FALSE` 時拒絕；結果為 NULL（UNKNOWN）時視為通過。因此約束中凡是比較可為 NULL 的欄位（`>`、`BETWEEN`、`IN` 等），都要另外寫 `IS NOT NULL`，否則 NULL 會讓整個條件變成 UNKNOWN 而被放行。

---

## 二、`fetch_batches`

### SQL 草案

```sql
CREATE TABLE fetch_batches (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  batch_id          text        NOT NULL UNIQUE,
  fetched_at        timestamptz NOT NULL,
  source_updated_at timestamptz,
  status            text        NOT NULL,
  station_count     integer,
  raw_path          text,
  error             text,

  CONSTRAINT fetch_batches_status_chk
    CHECK (status IN ('success', 'failed')),

  -- 與 collect.collect_once() 的實際輸出一致（見下方對照）
  CONSTRAINT fetch_batches_success_chk
    CHECK (status <> 'success' OR (
      station_count IS NOT NULL AND station_count > 0
      AND raw_path IS NOT NULL AND error IS NULL)),
  CONSTRAINT fetch_batches_failed_chk
    CHECK (status <> 'failed' OR (
      error IS NOT NULL AND station_count IS NULL AND source_updated_at IS NULL))
);
```

### 欄位語意

| 欄位 | 語意 |
|---|---|
| `id` | 資料庫內部識別，供 `station_snapshots.fetch_batch_id` 參照【定版：A4】 |
| `batch_id` | 擷取批次識別，例如 `20261027T081500Z-3f9a1c`。唯一鍵，匯入冪等的依據 |
| `fetched_at` | 實際擷取時間（UTC） |
| `source_updated_at` | 該批各站 `srcUpdateTime` 的最大值（UTC）；無法解析時為 NULL。**不用於新鮮度判斷**【定版：新鮮度看各站 `info_time`】 |
| `status` | 僅代表擷取階段。`success` = 下載成功且通過結構驗證；**不代表**快照已寫入 |
| `station_count` | 該批站點數；失敗時為 NULL |
| `raw_path` | 原始檔路徑，相對於資料目錄（`YOUBIKE_DATA_DIR`）；下載失敗時為 NULL。內容損壞的 `failed` 批次仍有值 |
| `error` | 失敗原因；成功時為 NULL |

### 與 `fetch_batches.jsonl` 的逐欄對照

以 `src/collect.py` 的 `collect_once()` 現有輸出為準，**不修改 JSONL 格式**。

| JSONL 欄位 | JSON 型別 | 資料表欄位 | 轉換規則 |
|---|---|---|---|
| — | — | `id` | 無對應，由資料庫產生 |
| `batch_id` | string | `batch_id` | 原樣寫入 |
| `fetched_at` | string，ISO 8601，含 `+00:00` | `fetched_at` | 直接轉為 `timestamptz`，含微秒 |
| `source_updated_at` | string（ISO 8601，含 `+00:00`）或 null | `source_updated_at` | 同上；null → NULL |
| `status` | `"success"` / `"failed"` | `status` | 原樣寫入 |
| `station_count` | int 或 null | `station_count` | 原樣寫入 |
| `raw_path` | string（POSIX 相對路徑）或 null | `raw_path` | 原樣寫入，不轉成絕對路徑 |
| `error` | string 或 null | `error` | 原樣寫入 |

三種輸出情境與約束的對應：

| 情境 | status | station_count | raw_path | source_updated_at | error |
|---|---|---|---|---|---|
| 下載失敗 | failed | null | null | null | 有 |
| 下載成功、內容無法解析 | failed | null | 有 | null | 有 |
| 成功 | success | > 0 | 有 | 有或 null | null |

- `station_count > 0`：`parse_stations()` 拒絕空 list，成功批次至少有一站。
- 成功批次的 `source_updated_at` 仍可能為 NULL（各站時間都無法解析時），因此不加 NOT NULL。

### 待定

- **【待定：4B】** JSONL 中違反上述約束的行，是否在匯入時視為損壞行並略過。
- **【待定：4B】** 同一個 `batch_id` 出現多次且內容不同時的處理（`docs/progress.md` 規格為保留第一筆並警告）。

---

## 三、`stations`

### SQL 草案

```sql
CREATE TABLE stations (
  station_id    text PRIMARY KEY,
  station_name  text,
  district      text,
  address       text,
  latitude      double precision,
  longitude     double precision,
  first_seen_at timestamptz NOT NULL,
  last_seen_at  timestamptz NOT NULL,

  CONSTRAINT stations_id_not_blank_chk CHECK (btrim(station_id) <> ''),
  CONSTRAINT stations_seen_order_chk   CHECK (last_seen_at >= first_seen_at)
);
```

### 欄位語意

| 欄位 | 來源（`transform.py`） | 語意 |
|---|---|---|
| `station_id` | `station_id`（原 `sno`） | 站點識別，文字型別。唯一必填的站點欄位 |
| `station_name` | `station_name`（已移除 `YouBike2.0_` 前綴） | 可為 NULL |
| `district` | `district`（原 `sarea`） | 可為 NULL |
| `address` | `address`（原 `ar`） | 可為 NULL |
| `latitude` / `longitude` | 同名 | 可為 NULL。**不在資料庫檢查座標範圍**，範圍異常由驗證流程處理 |
| `first_seen_at` | — | 該站第一次寫入快照時的 `slot_time`。供預期樣本規則「新站從首次出現的時間槽開始計算」使用 |
| `last_seen_at` | — | 該站最近一次寫入快照時的 `slot_time` |

- **【4A-1 確認】** 除 `station_id`、`first_seen_at`、`last_seen_at` 外，其餘站點欄位允許 NULL。
- `first_seen_at`、`last_seen_at` 由系統產生，不依賴來源資料品質，一定有值，因此 NOT NULL。
- **沒有 `station_id` 的原始資料無法寫入任何資料表**，在 4C 計為「跳過」（見第六節）。

### 待定

- **【待定：4C，附錄 A】** 站點新增、改名、停用時的更新策略：名稱、地址、座標是否以最新觀測值覆寫，以及新觀測值為 NULL 時是否覆寫既有值。
- **【待定：4C】** 歷史快照重建時寫入較早的 `slot_time`，`first_seen_at` 是否往前修正（建議取 `LEAST`）。

---

## 四、`station_snapshots`

### SQL 草案

```sql
CREATE TABLE station_snapshots (
  station_id       text        NOT NULL REFERENCES stations (station_id),
  slot_time        timestamptz NOT NULL,
  fetch_batch_id   bigint      NOT NULL REFERENCES fetch_batches (id),
  info_time        timestamptz,
  is_active        boolean,
  total_spaces     integer,
  available_bikes  integer,
  available_spaces integer,
  is_valid         boolean     NOT NULL,
  invalid_reason   text,

  PRIMARY KEY (station_id, slot_time),

  -- 時間槽對齊 UTC 整 5 分鐘
  CONSTRAINT snapshots_slot_aligned_chk
    CHECK (extract(epoch FROM slot_time) % 300 = 0),

  -- 有效 ⇔ 無原因；無效 ⇒ 必須是固定代碼之一（因此也不可能是空字串）
  CONSTRAINT snapshots_reason_chk
    CHECK (
      (is_valid AND invalid_reason IS NULL)
      OR (NOT is_valid AND invalid_reason IS NOT NULL AND invalid_reason IN (
            'inactive', 'stale', 'missing_field', 'invalid_type',
            'capacity_invalid', 'count_invalid', 'duplicate_conflict'))),

  -- 有效樣本的最低一致性（實作規則 1、3 的資料庫防線）
  CONSTRAINT snapshots_valid_values_chk
    CHECK (NOT is_valid OR (
      is_active IS TRUE
      AND info_time IS NOT NULL
      AND total_spaces IS NOT NULL
      AND available_bikes IS NOT NULL
      AND available_spaces IS NOT NULL
      AND total_spaces >= 0
      AND available_bikes  BETWEEN 0 AND total_spaces
      AND available_spaces BETWEEN 0 AND total_spaces))
);

CREATE INDEX station_snapshots_slot_time_brin
  ON station_snapshots USING brin (slot_time);
```

### 欄位語意

| 欄位 | 來源（`transform.py`） | 語意 |
|---|---|---|
| `station_id` | `station_id` | 參照 `stations` |
| `slot_time` | — | 5 分鐘時間槽起點（UTC）【4A-1 確認：對齊 UTC 整 5 分鐘】 |
| `fetch_batch_id` | — | 產生此快照的批次，參照 `fetch_batches.id`【定版：A4】 |
| `info_time` | `info_time` | 該站自身的資料時間，新鮮度判斷依據【定版】。無法解析時為 NULL |
| `is_active` | `is_active`（原 `act`） | `true` 營運中、`false` 暫停營運、**NULL 無法判斷** |
| `total_spaces` | `total_spaces`（原 `Quantity`） | 無法解析時為 NULL |
| `available_bikes` | `available_bikes`（原 `available_rent_bikes`） | 同上 |
| `available_spaces` | `available_spaces`（原 `available_return_bikes`） | 同上。彙總時對應 `sum_available_docks` |
| `is_valid` | — | 由驗證流程判斷，是否計入有效樣本 |
| `invalid_reason` | — | 無效原因代碼，見下表 |

未寫入快照的清洗欄位：`src_update_time`、`update_time`。批次層級的來源時間已記在 `fetch_batches.source_updated_at`。

無效資料的原始值不另存。需要追查時，透過 `fetch_batch_id → fetch_batches.raw_path` 回到原始檔。

### `invalid_reason` 代碼【4A-1 確認】

對應 v1.1 第六節「無效資料」：

| 代碼 | 意義 | v1.1 對應項目 |
|---|---|---|
| `inactive` | 站點暫停營運（`is_active = false`） | 暫停營運站點 |
| `stale` | `info_time` 未達新鮮度門檻 | 來源更新時間過期 |
| `missing_field` | 必要欄位缺失 | 欄位缺失 |
| `invalid_type` | 欄位值無法轉換為預期型別，或 `act` 不是 `"1"`／`"0"` | 資料型別不正確 |
| `capacity_invalid` | `total_spaces` 為負 | 車位容量異常 |
| `count_invalid` | 車輛數或車位數為負，或超過 `total_spaces` | 車輛數量異常 |
| `duplicate_conflict` | 同一回應中同一 `station_id` 內容不一致 | Batch 2 的衝突判斷 |

- 每列只記一個原因。新增代碼需修改 CHECK 約束（migration）。
- **【待定：4C】** 同時符合多個原因時的判斷順序。
- **【待定：4C】** `duplicate_conflict` 時同一 `station_id` 有多筆來源資料，該寫入哪一筆。
- `total_spaces = 0` 與容量 99 不屬於無效【定版：實作規則 3】，CHECK 也允許。

### 索引

- 主鍵 `(station_id, slot_time)`：防止重複寫入，同時支援「單站依時間查詢」。
- BRIN `(slot_time)`：快照依時間順序寫入，供 4D 彙總與 4E 清理的範圍查詢【定版】。
- `fetch_batch_id` 不建索引：`fetch_batches` 不刪除，外鍵檢查不需要反向索引。
- **【待定：API 階段】**「全站最新一個時間槽」的查詢效能。BRIN 不擅長 `max(slot_time)`，若實測太慢，再評估索引或查詢方式。

---

## 五、`station_hourly_stats`

### SQL 草案

```sql
CREATE TABLE station_hourly_stats (
  station_id          text        NOT NULL REFERENCES stations (station_id),
  hour_start          timestamptz NOT NULL,
  sum_available_bikes integer     NOT NULL,
  sum_available_docks integer     NOT NULL,
  empty_samples       smallint    NOT NULL,
  low_samples         smallint    NOT NULL,
  full_samples        smallint    NOT NULL,
  valid_samples       smallint    NOT NULL,
  expected_samples    smallint    NOT NULL,

  PRIMARY KEY (station_id, hour_start),

  -- 對齊 UTC 整點
  CONSTRAINT hourly_aligned_chk
    CHECK (extract(epoch FROM hour_start) % 3600 = 0),

  CONSTRAINT hourly_nonneg_chk
    CHECK (sum_available_bikes >= 0 AND sum_available_docks >= 0
       AND empty_samples >= 0 AND low_samples >= 0 AND full_samples >= 0),

  -- 實作規則 1：缺車、低車量、滿站都只從有效樣本計算
  --   缺車（0 台）與低車量（1～2 台）互斥，因此兩者相加不超過有效樣本
  CONSTRAINT hourly_subset_chk
    CHECK (empty_samples + low_samples <= valid_samples
       AND full_samples <= valid_samples),

  -- 實作規則 2：每小時最多 12 筆；有效樣本必定落在應有的時間槽內
  CONSTRAINT hourly_samples_chk
    CHECK (valid_samples BETWEEN 0 AND expected_samples
       AND expected_samples <= 12)
);
```

### 欄位語意

與 v1.1 5.2 相同【定版】，`low_samples` 為 v1.1 新增。

- `sum_available_docks` 為有效樣本 `available_spaces` 的總和（命名差異見 v1.1 5.2）。
- 保存 Sum 不保存 Average；平均、比例、覆蓋率於查詢時計算【定版】。
- `hour_start` 對齊 UTC 整點【4A-1 確認】。每日彙總以 Asia/Taipei 日期切分；台灣為 UTC+8 整時差，UTC 整點即台灣整點。
- Sum 欄位暫定 `integer`。Schema 沒有限制 `total_spaces` 的上限（容量 99 是允許值，不是上限），所以 Sum 沒有固定的最大值。**【待定：4D】** 依實際資料評估是否改用 `bigint`。

### 待定

- **【待定：4D，附錄 A】**「彙總完整」的判斷方式。本規格**不**為此新增欄位，4D 決定後若需要再以 migration 加入。
- **【待定：4D】** 整小時都暫停營運（`expected_samples = 0`）的站點是否寫入一列。CHECK 允許 0，不預先決定。
- **【待定：4D】** `expected_samples` 的計算細節；`hourly_samples_chk` 是依實作規則 1、2 推出的必然關係，若 4D 的定義與此不同，再修改約束。

---

## 六、4C 最新快照寫入流程（規格）

依 v1.1 4.2。具體交易實作留到 4C，本節只規定行為。

### 流程

1. **挑選批次**：從 `fetch_batches.jsonl` 找出該時間槽的 `status = success` 批次，讀取其 gzip。不能只看檔案是否存在【定版】。
2. **逐站驗證**：在 Python 中逐站驗證與清洗，產生 `is_valid` 與 `invalid_reason`。單站有問題只標記該站，不讓整批作廢【定版】。
3. **開始 transaction**。
4. **確保批次列存在**【定版】：
   ```sql
   INSERT INTO fetch_batches (batch_id, fetched_at, ...)
   VALUES (...)
   ON CONFLICT (batch_id) DO NOTHING
   RETURNING id;
   ```
   `DO NOTHING` 遇到既有列時不回傳任何列，此時再以 `SELECT id FROM fetch_batches WHERE batch_id = ...` 取得 `id`。寫入的值取自 JSONL 中該批次的紀錄。
   **這一步失敗時整個 transaction 回滾**，不寫入任何快照。
5. **逐站寫入，每站隔離**【4A-1 確認】：
   - 每站開始前建立 `SAVEPOINT`（或等效機制）。
   - 依序 upsert `stations`、寫入 `station_snapshots`。
   - 成功則 `RELEASE SAVEPOINT`；失敗則 `ROLLBACK TO SAVEPOINT`，只撤銷該站，其餘站點繼續。
   - 原因：PostgreSQL 的 transaction 一旦發生錯誤即進入失敗狀態，後續指令都會被拒絕。沒有 savepoint 時，一站失敗就會連帶整批失敗。
   - 驗證在步驟 2 完成，savepoint 是資料庫層的最後防線，正常情況下很少觸發。
6. **commit**，並輸出處理結果。

### 處理結果【4A-1 確認】

每次執行必須輸出以下計數：

| 計數 | 意義 |
|---|---|
| 寫入（有效） | 寫入快照且 `is_valid = true` |
| 寫入（無效） | 寫入快照且 `is_valid = false`，依原因代碼分列 |
| 跳過 | 無法寫入的資料，例如缺少 `station_id`；重跑時已存在的快照是否計入，依 4C 的重跑策略而定 |
| 失敗 | savepoint 回滾的站點 |

- **只要有任何站點失敗或跳過，結果不得回報為完全成功。**
- 這是 4C 的處理結果，與 `fetch_batches.status`（擷取階段）是兩回事，不回寫到 `fetch_batches`。
- 依 v1.1 5.4 不另建狀態表。**【待定：4C】** 計數只輸出到 stdout／排程日誌，或另外保存。

### 待定

- **【待定：4C，附錄 A】** 新鮮度門檻。
- **【待定：4C】**「該時間槽的批次」的時間窗定義（例如槽內最後一個成功批次），以及槽內沒有成功批次時的處理。
- **【待定：4C】** 同一時間槽重跑時，快照採 `ON CONFLICT DO NOTHING`（保留既有）或 `DO UPDATE`（覆寫）。
- **【待定：4C，附錄 A】** 逐站驗證層的實作方式（不重構 Batch 2 `clean()` 的前提下）。

---

## 七、實作規則對照

v1.1 第六節的實作規則與 4.2 的流程，與本規格的對應：

| 規則 | 對應的欄位、約束或步驟 | 資料庫負責 | 程式負責 |
|---|---|---|---|
| 規則 1：有效樣本 | `is_valid`、`snapshots_valid_values_chk`、`hourly_subset_chk` | 有效列必須營運中、有 `info_time`、數值合理；彙總計數不超過有效樣本 | 新鮮度判斷（門檻待定）、所有驗證 |
| 規則 2：預期樣本 | `stations.first_seen_at`、`is_active` 三值、`hourly_samples_chk` | 每小時最多 12 筆；有效不超過預期 | 4D 計算：新站從 `first_seen_at` 起算；`is_active = false` 不計入；`is_active` 為 NULL 或缺快照時**不可**當作暫停營運 |
| 規則 3：容量異常 | `snapshots_valid_values_chk`、`capacity_invalid`、`count_invalid` | 有效列的非負與上限檢查；允許 `total_spaces = 0` | 判定並寫入原因代碼 |
| 規則 4：彙總與清理 | `station_hourly_stats` 主鍵、BRIN 索引 | 主鍵支援 upsert 冪等 | 「彙總完整」判斷（待定）、當前小時不做最終彙總、4E 清理條件 |
| 4.2 批次挑選 | 第六節步驟 1 | — | 只選 `status = success` |
| 4.2 批次列確保存在 | 第六節步驟 4、`fetch_batches.batch_id` UNIQUE | `ON CONFLICT` 冪等 | 同一 transaction 內執行 |
| 4.2 逐站隔離 | 第六節步驟 5、`stations` 放寬 NULL | savepoint 回滾單站 | 結果計數，不得把部分失敗回報為完全成功 |
| 4.3 追溯性 | `fetch_batch_id` → `fetch_batches.id` | 外鍵 | — |

---

## 八、交給 4A-2 的事項

- 將第二～五節的 SQL 草案轉為正式 migration（工具與 PostgreSQL 驅動於 4A-2 決定，psycopg 3 為建議）。
- 以測試驗證各 CHECK 約束，特別是：
  - `extract(epoch FROM ...) % 300 = 0` 對齊檢查在不同 session 時區設定下行為一致。
  - 有效列缺少數值、無效列沒有原因、`invalid_reason` 為空字串時都會被拒絕。
  - **NULL 邊界**（共通原則 4）：以下每一種都必須被拒絕：
    - `is_valid = true` 且 `total_spaces`、`available_bikes`、`available_spaces` 任一為 NULL。
    - `is_valid = false` 且 `invalid_reason` 為 NULL。
    - `status = 'success'` 且 `station_count` 為 NULL。
  - `fetch_batches` 三種情境的合法資料都能寫入。
