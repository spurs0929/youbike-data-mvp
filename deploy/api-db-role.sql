-- YouBike API-3：API 專用的唯讀資料庫角色 youbike_api
--
-- 執行方式（VM，以 postgres 超級使用者連到 youbike 資料庫）：
--   sudo -u postgres psql -d youbike -v ON_ERROR_STOP=1 -f /opt/youbike-api/deploy/api-db-role.sql
--
-- - 角色名稱與系統使用者 youbike_api 相同（底線），peer 驗證直接對應，不使用 pg_ident.conf。
-- - 沒有密碼：只允許本機 Unix socket（pg_hba 的 local … peer），postgresql-youbike.conf 不監聽 TCP。
-- - 只授予 API 查詢用到的三張表的 SELECT（src/repository.py：stations、station_snapshots、fetch_batches）。
--   station_hourly_stats 尚未使用，不授權。之後新增資料表（migration 002 起）需要時另行 GRANT。
-- - 縱深防禦：預設唯讀交易、單一查詢逾時、連線數上限（連線池最多 4 條，留 2 條給手動檢查）。
-- - 可重複執行（角色已存在時只更新設定與權限）。

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'youbike_api') THEN
        CREATE ROLE youbike_api LOGIN;
    END IF;
END
$$;

ALTER ROLE youbike_api
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 6;

ALTER ROLE youbike_api SET default_transaction_read_only = on;
ALTER ROLE youbike_api SET statement_timeout = '5s';
ALTER ROLE youbike_api SET idle_in_transaction_session_timeout = '30s';

GRANT CONNECT ON DATABASE youbike TO youbike_api;
GRANT USAGE ON SCHEMA public TO youbike_api;
GRANT SELECT ON TABLE stations, station_snapshots, fetch_batches TO youbike_api;
