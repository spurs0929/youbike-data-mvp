-- 001_init.sql：建立 Batch 4 的四張資料表與 BRIN 索引
--
-- 依據：docs/schema.md（Batch 4A-1 Schema 規格）。表格定義需與規格一致，
-- 修改時兩邊一起改。
--
-- 執行方式（在專案根目錄）：
--   psql -v ON_ERROR_STOP=1 -h <host> -U <user> -d <database> -f migrations/001_init.sql
--
-- 重複執行策略：
--   - 整個檔案包在單一 transaction 中，任何一步失敗都會全部回滾，不會只建一半。
--   - 刻意不用 CREATE TABLE IF NOT EXISTS：已存在的表若定義不同，IF NOT EXISTS
--     會靜默略過，讓舊定義繼續沿用。這裡寧可重複執行時直接失敗（relation already
--     exists），也不讓資料庫和規格悄悄不一致。
--   - 因此本檔只對「尚未建立過這四張表」的 schema 執行一次。之後的變更以
--     002_*.sql 等新檔案進行，不修改已套用的檔案。

BEGIN;

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

  -- 與 collect.collect_once() 的實際輸出一致（見 docs/schema.md 第二節）
  CONSTRAINT fetch_batches_success_chk
    CHECK (status <> 'success' OR (
      station_count IS NOT NULL AND station_count > 0
      AND raw_path IS NOT NULL AND error IS NULL)),
  CONSTRAINT fetch_batches_failed_chk
    CHECK (status <> 'failed' OR (
      error IS NOT NULL AND station_count IS NULL AND source_updated_at IS NULL))
);

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

COMMIT;
