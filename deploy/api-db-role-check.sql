-- YouBike API-3b：唯讀資料庫角色 youbike_api 的有效權限檢查（唯讀，不修改任何東西）
--
-- 執行方式（VM，以 postgres 超級使用者連到 youbike 資料庫）：
--   sudo -u postgres psql -d youbike -v ON_ERROR_STOP=1 -At -F ' | ' -f /opt/youbike-api/deploy/api-db-role-check.sql
--
-- 為什麼不能只看 default_transaction_read_only：
--   它只是「預設」交易模式，連線後可以用 SET default_transaction_read_only = off 或 BEGIN READ WRITE 蓋掉，
--   不是安全邊界。真正擋住寫入的是資料表權限（ACL），所以這裡直接查「有效權限」：
--   has_*_privilege() 會把角色繼承、PUBLIC 權限都算進去，結果就是這個角色實際能做的事。
--
-- 輸出：每項一行「項目 | ok | 實際內容」。ok 全部為 t 才算通過。
-- 最後一行 info:database_temp 只是記錄（PUBLIC 預設可建立暫存資料表，見 deploy/README.md〈已知限制〉），不算檢查項目。

-- 1. 角色繼承：不屬於任何其他角色（否則會繼承那些角色的權限）
SELECT 'member_of',
       count(*) = 0,
       coalesce(string_agg(r.rolname, ', '), '(none)')
FROM pg_auth_members AS m
JOIN pg_roles AS r ON r.oid = m.roleid
WHERE m.member = 'youbike_api'::regrole;

-- 2. 角色屬性：沒有任何特權，連線上限 6
SELECT 'attributes',
       NOT (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)
           AND rolconnlimit = 6,
       format('super=%s createrole=%s createdb=%s replication=%s bypassrls=%s connlimit=%s',
              rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolconnlimit)
FROM pg_roles
WHERE rolname = 'youbike_api';

-- 3. 資料表、檢視表的有效權限：只有三張表的 SELECT
WITH privs AS (
    SELECT c.oid::regclass::text || ':' || p.priv AS item
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) AS p(priv)
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%'
      AND has_table_privilege('youbike_api', c.oid, p.priv)
)
SELECT 'table_privileges',
       coalesce(array_agg(item ORDER BY item), '{}')
           = ARRAY['fetch_batches:SELECT', 'station_snapshots:SELECT', 'stations:SELECT'],
       coalesce(string_agg(item, ', ' ORDER BY item), '(none)')
FROM privs;

-- 4. 序列（自動編號）的有效權限：沒有任何權限
WITH privs AS (
    SELECT c.oid::regclass::text || ':' || p.priv AS item
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    CROSS JOIN unnest(ARRAY['USAGE', 'SELECT', 'UPDATE']) AS p(priv)
    WHERE c.relkind = 'S'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND has_sequence_privilege('youbike_api', c.oid, p.priv)
)
SELECT 'sequence_privileges',
       count(*) = 0,
       coalesce(string_agg(item, ', ' ORDER BY item), '(none)')
FROM privs;

-- 5. PUBLIC（所有角色）在資料表、序列上的授權：沒有
SELECT 'public_grants',
       count(*) = 0,
       coalesce(string_agg(c.oid::regclass::text || ':' || a.privilege_type, ', '), '(none)')
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
CROSS JOIN LATERAL aclexplode(c.relacl) AS a
WHERE a.grantee = 0
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%';

-- 6. 在任何使用者 schema 建立物件的權限：沒有
SELECT 'schema_create',
       NOT coalesce(bool_or(has_schema_privilege('youbike_api', n.oid, 'CREATE')), false),
       coalesce(string_agg(n.nspname, ', ') FILTER (
           WHERE has_schema_privilege('youbike_api', n.oid, 'CREATE')), '(none)')
FROM pg_namespace AS n
WHERE n.nspname NOT LIKE 'pg\_%'
  AND n.nspname <> 'information_schema';

-- 7. 在資料庫建立 schema 的權限：沒有
SELECT 'database_create',
       NOT has_database_privilege('youbike_api', 'youbike', 'CREATE'),
       has_database_privilege('youbike_api', 'youbike', 'CREATE')::text;

-- 記錄：暫存資料表（PUBLIC 預設權限，不算檢查項目）
SELECT 'info:database_temp',
       NULL,
       has_database_privilege('youbike_api', 'youbike', 'TEMP')::text;
