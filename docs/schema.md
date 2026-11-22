# PostgreSQL Schema 規格（Batch 4A-1）

- 依據：《YouBike 2.0 專案設計定版 v1.2》第四～六節（4C 寫入規則見定版 4.4）
- 狀態：第二～五節的 SQL 已轉為正式 migration `migrations/001_init.sql`（4A-2），並由 `tests/test_schema.py` 驗證。第六節的 4C 寫入規則已由 `src/snapshot.py`（4C-2）、`src/snapshot_job.py`（4C-3）實作，並由 `tests/test_snapshot*.py` 驗證。
- 最後更新：2026-11-22

本文件中的標記：

- **【定版】**：設計定版已確定的設計。
- **【4A-1 確認】**：4A-1 規格審閱時確認的約束（2026-11-02）。
- **【4C 確認】**：4C 開工前確認的寫入規則（2026-11-07，定版 v1.2 4.4）。
- **【4C-3 定案】**：4C 規格中標為「實作時確認」的細節，於 4C-3 實作時決定（2026-11-22）。
- **【待定】**：定版附錄 A 的待議事項，或屬於後續 Batch 的實作細節。本文件只記錄，不做決定。

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
| `fetched_at` | 擷取時間（UTC），為 HTTP 請求**開始**時間。4C 時間槽歸屬與新鮮度計算的依據【4C 確認】 |
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
  4C 讀取 JSONL 挑選批次時已採「視為壞行略過、計入警告」（4C-3a）；4B 匯入是否沿用，4B 開工時決定。
- **【待定：4B】** 同一個 `batch_id` 出現多次且內容不同時的處理（`docs/progress.md` 規格為保留第一筆並警告）。
  4C 讀取時已採「保留第一筆、其餘計入重複 ID 數」（4C-3a）。

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
| `latitude` / `longitude` | 同名 | 可為 NULL。**不在資料庫檢查座標範圍**；非有限值或超出範圍（緯度 −90～90、經度 −180～180）時由驗證流程轉為 NULL【4C 確認】 |
| `first_seen_at` | — | 該站第一次寫入快照時的 `slot_time`。供預期樣本規則「新站從首次出現的時間槽開始計算」使用 |
| `last_seen_at` | — | 該站最近一次寫入快照時的 `slot_time` |

- **【4A-1 確認】** 除 `station_id`、`first_seen_at`、`last_seen_at` 外，其餘站點欄位允許 NULL。
- `first_seen_at`、`last_seen_at` 由系統產生，不依賴來源資料品質，一定有值，因此 NOT NULL。
- **沒有 `station_id` 的原始資料無法寫入任何資料表**，在 4C 計為「跳過」（見第六節）。

### 更新策略【4C 確認】

```sql
INSERT INTO stations (station_id, station_name, district, address,
                      latitude, longitude, first_seen_at, last_seen_at)
VALUES (...)
ON CONFLICT (station_id) DO UPDATE SET
  station_name = CASE WHEN EXCLUDED.last_seen_at >= stations.last_seen_at
                      THEN COALESCE(EXCLUDED.station_name, stations.station_name)
                      ELSE stations.station_name END,
  -- district、address、latitude、longitude 同樣寫法
  first_seen_at = LEAST(stations.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = GREATEST(stations.last_seen_at, EXCLUDED.last_seen_at);
```

- 名稱、行政區、地址、座標：只用**不早於該站最近快照槽**的非 NULL 值覆寫；新值為 NULL 時保留舊值。
- `first_seen_at` 取 `LEAST`、`last_seen_at` 取 `GREATEST`，歷史快照重建寫入較早的槽時會自動往前修正。
- 只有 `clean()` 成功的站提供站點資訊；結構無效的站只確保列存在並更新時間欄位。
- 重跑時因快照已存在而被略過的站，不更新 `stations`（第六節）。
- 站點從來源消失時不處理，也不保存改名歷史。
- **已知限制（接受）**：以站點最近快照槽為界。先寫入 08:10 的無效快照、再補 08:00 時，08:00 只往前修正 `first_seen_at`，不補齊名稱；下一個有效槽會補上。

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
| `info_time` | `info_time` | 來源回傳的該站資料時間，新鮮度判斷依據【定版】。無法解析時為 NULL |
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
| `missing_field` | 必要欄位的原始值為 NULL 或空白 | 欄位缺失 |
| `invalid_type` | 必要欄位非空但無法轉換為預期型別、`act` 不是 `"1"`／`"0"`，或數值超出 PostgreSQL `integer` 範圍 | 資料型別不正確 |
| `capacity_invalid` | `total_spaces` 為負 | 車位容量異常 |
| `count_invalid` | 車輛數或車位數為負，或超過 `total_spaces` | 車輛數量異常 |
| `duplicate_conflict` | 同一回應中同一 `station_id` 內容不一致 | Batch 2 的衝突判斷 |

- 每列只記一個原因。新增代碼需修改 CHECK 約束（migration）。
- `total_spaces = 0` 與容量 99 不屬於無效【定版：實作規則 3】，CHECK 也允許。
- **必要欄位**：`sno`、`act`、`infoTime`、`Quantity`、`available_rent_bikes`、`available_return_bikes`。`sno` 為 NULL 或空白時不寫入任何表，計為「跳過」。

#### 判定順序【4C 確認】

同時符合多個原因時，取第一個成立者：

`duplicate_conflict` → `missing_field` → `invalid_type` → `inactive` → `stale` → `capacity_invalid` → `count_invalid`

- `missing_field`、`invalid_type` **以原始值判定**，不依 `clean()` 是否成功，也不解析例外訊息，確保整批與逐站兩條路徑得到相同結果（第六節）。
- `duplicate_conflict`：同一 `station_id` 多筆時，只比較快照與 `stations` 會用到的欄位；全部相同則保留一筆、不算衝突。

#### 無效列的欄位值【4C 確認】

| 原因 | 寫入的值 |
|---|---|
| `duplicate_conflict` | 只寫一列。數值與 `info_time` 為 NULL；多筆的 `act` 一致且可判定時才寫 `is_active` |
| `missing_field`、`invalid_type` | 數值與 `info_time` 為 NULL；`act` 可判定時**獨立**寫入 `is_active` |
| `inactive` 以後 | `clean()` 已成功，寫入完整清洗後的值 |

保留 `is_active` 的原因：4D 計算 `expected_samples` 依 `is_active` 判斷，不依 `invalid_reason`（第七節規則 2）。

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

依定版 v1.2 4.2、4.4。本節規定行為；實作為 `src/snapshot.py`（逐站驗證，4C-2）與 `src/snapshot_job.py`（時間槽、批次、寫入，4C-3），實作時的判斷記錄在 [progress.md](progress.md) 的 Batch 4C。

### 時間槽與批次【4C 確認】

- 時間槽為 UTC 五分鐘半開區間 `[T, T+5 分)`，以批次 `fetched_at` 歸槽。
- 排程每 5 分鐘處理**最近一個已結束**的槽；`--slot` 參數供手動補跑指定槽，限已結束、且槽起點不早於「目前時間 − 7 天」（快照保存 7 天）【4C-3 定案】。
- **零快照的槽**：從 `fetch_batches.jsonl` 選批次，取槽內 `status = success` 中 `fetched_at` 最新者；同時戳依 `batch_id` 排序取最後一個。不能只看檔案是否存在【定版】。
- **已有快照的槽**：不從 JSONL 重新選批次。沿用該槽唯一的 `fetch_batch_id`，查 `fetch_batches.raw_path` 讀原始檔，只補缺少的站。
- JSONL 於擷取完成後才寫入，槽結束時仍可能有槽內開始的擷取尚未落地。上述規則確保這類晚到批次不會讓同一槽混入兩份來源。
- 排程與擷取正常時，快照約每 5 分鐘更新，資料時間另受來源延遲影響；槽內無成功批次時，上一筆快照可能保留更久。

### 新鮮度【4C 確認】

- `lag = fetched_at − info_time`，`fetched_at` 為選中批次的擷取時間，不使用 `slot_time` 或執行當下時間（重跑與歷史重建結果可重現）。
- `lag > 10 分鐘` 判為 `stale`；剛好 10 分鐘仍有效。10 分鐘為暫定值，寫成常數。
- 負落後（`info_time` 晚於 `fetched_at`）不判無效，只在處理結果中另計警告。

### 逐站驗證【4C 確認】

範圍：只處理 `status = success` 的批次。`fetch.validate_shape()` 已保證每站都有所有必要鍵（任一站缺鍵會讓整批 `failed`），所以逐站隔離的對象是成功批次內的 NULL、空白、型別錯誤與值異常。

不修改 Batch 2 的 `clean()`：

1. **前處理**：`sno` 統一為去空白字串，NULL 或空白則跳過。同一 `station_id` 多筆依第四節判斷是否為 `duplicate_conflict`；衝突的站不進入 `clean()`。
2. **快速路徑**：其餘站點整批呼叫一次 `clean()`。
3. **備援路徑**：整批丟出 `TransformError` 時，逐站以單列 DataFrame 呼叫 `clean()`。單站失敗時，先將可選欄位的值轉為 NULL 後重試一次，保留其餘正常欄位。
4. **值檢查**：依第四節的判定順序給出 `is_valid` 與 `invalid_reason`。

**兩條路徑必須一致**：

- 結構類原因（`missing_field`、`invalid_type`）以原始值判定（第四節）。
- 可選欄位的值檢查在 `clean()` 之後執行，兩條路徑都生效，只清除該欄位、不讓該站無效：
  - 座標非有限值或超出範圍 → NULL。例如座標 `999` 能通過 `clean()` 的浮點轉換，仍須轉為 NULL。
  - 文字欄位去空白後為空 → NULL。
- 可選欄位：名稱、行政區、地址、座標、`srcUpdateTime`、`updateTime`。

實測成本（1,824 站）：整批 `clean()` 約 11 ms，逐站約 7 秒，因此只在失敗時走備援。

### 流程

整個流程在**同一個 transaction** 內執行，同槽鎖跟著 transaction 開始與釋放【4C-3 定案】。

1. **開始 transaction 並取得同槽鎖**【4C-3 定案】：
   ```sql
   SELECT pg_advisory_xact_lock(4031, <slot 的 UNIX 秒數> / 300);
   ```
   - 第一個鍵 `4031` 固定，代表「4C 同槽鎖」；第二個鍵每個槽一個整數，由程式計算 `slot_epoch // 300`（`slot_lock_key()`）。
   - **advisory lock 的鍵在整個資料庫內有效，`4031` 日後不可用於其他用途。**
   - 排程與手動補跑處理同一槽時，後到者在這裡等待；不同槽互不影響。
   - 取得鎖之後才查詢既有快照，避免兩個行程都以為該槽是零快照而選了不同批次。
2. **決定來源批次**：依「時間槽與批次」。
   - 槽內無成功批次 → 不寫入，結果 `no_batch`。
   - 該槽已有多個不同的 `fetch_batch_id` → 不寫入，結果 `failed`，回報衝突。
   - 選中批次的 gzip 缺失或損壞 → 結果 `failed`；已有快照的槽保留既有快照。
3. **逐站驗證**：依「逐站驗證」，產生 `is_valid` 與 `invalid_reason`。驗證期間仍持有同槽鎖。
4. ~~開始 transaction~~：已併入步驟 1【4C-3 定案】。`pg_advisory_xact_lock` 必須在 transaction 內取得，所以 transaction 從步驟 1 就開始。保留這個編號，讓其他文件與程式註解引用的步驟 5、6 不變。
5. **確保批次列存在**【定版】（來源為 JSONL 時；沿用既有批次時已有 `id`）：
   ```sql
   INSERT INTO fetch_batches (batch_id, fetched_at, ...)
   VALUES (...)
   ON CONFLICT (batch_id) DO NOTHING
   RETURNING id;
   ```
   `DO NOTHING` 遇到既有列時不回傳任何列，此時再以 `SELECT id FROM fetch_batches WHERE batch_id = ...` 取得 `id`。寫入的值取自 JSONL 中該批次的紀錄。
   **這一步失敗時整個 transaction 回滾**，不寫入任何快照。
6. **逐站寫入，每站隔離**【4A-1 確認】：
   - 先以一次查詢取出該槽已有快照的站，計為「已存在」，不寫入、不更新 `stations`【4C-3 定案】。
   - 其餘每站建立一個 `SAVEPOINT`，在其中依序：
     1. upsert `stations`（第三節；快照的外鍵需要站點列先存在）。
     2. 寫入 `station_snapshots`，採 `ON CONFLICT (station_id, slot_time) DO NOTHING`【4C 確認】。
   - 快照因已存在而沒有寫入時，`ROLLBACK TO SAVEPOINT`，同一個 savepoint 內的 `stations` 更新一併撤銷，計為「已存在」【4C-3 定案】。
   - 成功則 `RELEASE SAVEPOINT`；資料庫錯誤則 `ROLLBACK TO SAVEPOINT`，只撤銷該站並計為「失敗」，其餘站點繼續。
   - 回滾 savepoint 後 transaction 若已失效（例如連線中斷），不是單站問題，中止整筆 transaction【4C-3 定案】。
   - 原因：PostgreSQL 的 transaction 一旦發生錯誤即進入失敗狀態，後續指令都會被拒絕。沒有 savepoint 時，一站失敗就會連帶整批失敗。
   - 驗證在步驟 3 完成，savepoint 是資料庫層的最後防線，正常情況下很少觸發。
7. **commit**，並輸出處理結果。同槽衝突、原始檔錯誤、資料庫錯誤時回滾整個 transaction，結果為 `failed`，計數不報成已寫入。

驗證規則改變時，舊快照不會因重跑而更新；需先刪除該範圍再重跑。

### 處理結果【4C 確認】

每次執行輸出**一行 JSON** 到日誌（由 cron 導向檔案）。依定版 5.4 不另建狀態表，也不回寫 `fetch_batches`。

| `result` | 條件 |
|---|---|
| `success` | 處理完成，且沒有跳過或單站寫入失敗。純重跑且全部已存在也算 |
| `partial` | 至少一站已寫入或原本已存在，且有跳過或失敗 |
| `failed` | 整筆 transaction 回滾；選到批次但沒有任何快照成功保留；同槽多批次衝突；gzip 缺失或損壞 |
| `no_batch` | 槽內沒有成功的擷取批次 |

計數：

| 計數 | 意義 |
|---|---|
| 寫入（有效） | 本次寫入快照且 `is_valid = true` |
| 寫入（無效） | 本次寫入快照且 `is_valid = false`，依原因代碼分列 |
| 已存在 | 快照已存在而略過（重跑），不算跳過 |
| 跳過 | 無法寫入的資料，例如 `sno` 為 NULL 或空白 |
| 失敗 | savepoint 回滾的站點 |

- transaction 回滾時，不得把暫存計數報成已寫入。
- **整批過期告警**：至少有一個已知營運中站（`is_active = true`），且這些站全數為 `stale` 時，只告警一次。仍有營運狀態未知的站時，訊息寫「已知營運中站點全數過期」並列出未知站數。逐站仍照常寫入 `stale` 列。
- 整批過期但所有無效列都已成功保存時，`result` 可為 `success`，並附帶過期告警。
- 負落後數另列為警告。
- 快照存在只證明該槽至少寫入一站，不代表完整。

### 待定

- **【待定：API 階段】** 查詢端如何提示資料過期。`max(info_time)` 可能被單一較新的站掩蓋其他過期站。
- **【待定：部署】** 新鮮度門檻依平日尖峰實測複核（D0 累積約一週後）；4C 排程的日誌與輪替（D1）。

---

## 七、實作規則對照

定版第六節的實作規則與 4.2、4.4 的流程，與本規格的對應：

| 規則 | 對應的欄位、約束或步驟 | 資料庫負責 | 程式負責 |
|---|---|---|---|
| 規則 1：有效樣本 | `is_valid`、`snapshots_valid_values_chk`、`hourly_subset_chk` | 有效列必須營運中、有 `info_time`、數值合理；彙總計數不超過有效樣本 | 新鮮度判斷（`lag > 10 分鐘` 為 stale，第六節）、所有驗證 |
| 規則 2：預期樣本 | `stations.first_seen_at`、`is_active` 三值、`hourly_samples_chk` | 每小時最多 12 筆；有效不超過預期 | 4D 計算：新站從 `first_seen_at` 起算；`is_active = false` 不計入；`is_active` 為 NULL 或缺快照時**不可**當作暫停營運 |
| 規則 3：容量異常 | `snapshots_valid_values_chk`、`capacity_invalid`、`count_invalid` | 有效列的非負與上限檢查；允許 `total_spaces = 0` | 判定並寫入原因代碼 |
| 規則 4：彙總與清理 | `station_hourly_stats` 主鍵、BRIN 索引 | 主鍵支援 upsert 冪等 | 「彙總完整」判斷（待定）、當前小時不做最終彙總、4E 清理條件 |
| 4.2、4.4.1 批次挑選 | 第六節「時間槽與批次」、步驟 1～2 | — | 只選 `status = success`；已有快照的槽沿用原批次；同槽互斥 |
| 4.2 批次列確保存在 | 第六節步驟 5、`fetch_batches.batch_id` UNIQUE | `ON CONFLICT` 冪等 | 同一 transaction 內執行 |
| 4.2、4.4.3 逐站隔離 | 第六節「逐站驗證」、步驟 6、`stations` 放寬 NULL | savepoint 回滾單站 | 整批與逐站路徑結果一致；`result` 分類，不得把部分失敗回報為完全成功 |
| 4.3 追溯性 | `fetch_batch_id` → `fetch_batches.id` | 外鍵 | — |

---

## 八、交給 4A-2 的事項

已於 4A-2 完成（`migrations/001_init.sql`、`tests/test_schema.py`）。保留本節作為測試範圍的紀錄。

- 將第二～五節的 SQL 草案轉為正式 migration（4A-2 決定：純 SQL ＋ psycopg 3）。
- 以測試驗證各 CHECK 約束，特別是：
  - `extract(epoch FROM ...) % 300 = 0` 對齊檢查在不同 session 時區設定下行為一致。
  - 有效列缺少數值、無效列沒有原因、`invalid_reason` 為空字串時都會被拒絕。
  - **NULL 邊界**（共通原則 4）：以下每一種都必須被拒絕：
    - `is_valid = true` 且 `total_spaces`、`available_bikes`、`available_spaces` 任一為 NULL。
    - `is_valid = false` 且 `invalid_reason` 為 NULL。
    - `status = 'success'` 且 `station_count` 為 NULL。
  - `fetch_batches` 三種情境的合法資料都能寫入。
