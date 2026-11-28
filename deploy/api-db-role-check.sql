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
-- info:database_temp 只是記錄（PUBLIC 預設可建立暫存資料表，見 deploy/README.md〈已知限制〉），不算檢查項目。
--
-- 結束代碼（執行後用 echo $? 確認）：
--   0：7 項全部為 t，最後一行是「result | t | ...」。
--   3：任何一項為 f（最後一行印出「result | f | ...」後以錯誤結束），或任何查詢失敗（例如角色不存在）。
-- 做法：每項查詢以 \gset 存進 psql 變數（c1_～c7_），以 \echo 印出；最後以 \if 判斷是否全部為 t。
-- 不通過時執行一個只丟出錯誤的 DO 區塊，讓 psql 因 ON_ERROR_STOP 以非零代碼結束（DO 區塊不讀寫任何資料）。

\set ON_ERROR_STOP on

-- 1. 角色繼承：不屬於任何其他角色（否則會繼承那些角色的權限）
SELECT 'member_of' AS item,
       count(*) = 0 AS ok,
       coalesce(string_agg(r.rolname, ', '), '(none)') AS detail
FROM pg_auth_members AS m
JOIN pg_roles AS r ON r.oid = m.roleid
WHERE m.member = 'youbike_api'::regrole
\gset c1_
\echo :c1_item '|' :c1_ok '|' :c1_detail

-- 2. 角色屬性：沒有任何特權，連線上限 6
SELECT 'attributes' AS item,
       NOT (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)
           AND rolconnlimit = 6 AS ok,
       format('super=%s createrole=%s createdb=%s replication=%s bypassrls=%s connlimit=%s',
              rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolconnlimit) AS detail
FROM pg_roles
WHERE rolname = 'youbike_api'
\gset c2_
\echo :c2_item '|' :c2_ok '|' :c2_detail

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
SELECT 'table_privileges' AS item,
       coalesce(array_agg(item ORDER BY item), '{}')
           = ARRAY['fetch_batches:SELECT', 'station_snapshots:SELECT', 'stations:SELECT'] AS ok,
       coalesce(string_agg(item, ', ' ORDER BY item), '(none)') AS detail
FROM privs
\gset c3_
\echo :c3_item '|' :c3_ok '|' :c3_detail

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
SELECT 'sequence_privileges' AS item,
       count(*) = 0 AS ok,
       coalesce(string_agg(item, ', ' ORDER BY item), '(none)') AS detail
FROM privs
\gset c4_
\echo :c4_item '|' :c4_ok '|' :c4_detail

-- 5. PUBLIC（所有角色）在資料表、序列上的授權：沒有
SELECT 'public_grants' AS item,
       count(*) = 0 AS ok,
       coalesce(string_agg(c.oid::regclass::text || ':' || a.privilege_type, ', '), '(none)') AS detail
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
CROSS JOIN LATERAL aclexplode(c.relacl) AS a
WHERE a.grantee = 0
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
\gset c5_
\echo :c5_item '|' :c5_ok '|' :c5_detail

-- 6. 在任何使用者 schema 建立物件的權限：沒有
SELECT 'schema_create' AS item,
       NOT coalesce(bool_or(has_schema_privilege('youbike_api', n.oid, 'CREATE')), false) AS ok,
       coalesce(string_agg(n.nspname, ', ') FILTER (
           WHERE has_schema_privilege('youbike_api', n.oid, 'CREATE')), '(none)') AS detail
FROM pg_namespace AS n
WHERE n.nspname NOT LIKE 'pg\_%'
  AND n.nspname <> 'information_schema'
\gset c6_
\echo :c6_item '|' :c6_ok '|' :c6_detail

-- 7. 在資料庫建立 schema 的權限：沒有
SELECT 'database_create' AS item,
       NOT has_database_privilege('youbike_api', 'youbike', 'CREATE') AS ok,
       has_database_privilege('youbike_api', 'youbike', 'CREATE')::text AS detail
\gset c7_
\echo :c7_item '|' :c7_ok '|' :c7_detail

-- 記錄：暫存資料表（PUBLIC 預設權限，不算檢查項目）
SELECT 'info:database_temp' AS item,
       has_database_privilege('youbike_api', 'youbike', 'TEMP')::text AS detail
\gset info_
\echo :info_item '| |' :info_detail

-- 總結：7 項全部為 t 才結束代碼 0
SELECT :'c1_ok'::boolean AND :'c2_ok'::boolean AND :'c3_ok'::boolean AND :'c4_ok'::boolean
       AND :'c5_ok'::boolean AND :'c6_ok'::boolean AND :'c7_ok'::boolean AS all_ok
\gset
\if :all_ok
\echo 'result | t | 7 項全部通過'
\else
\echo 'result | f | 有檢查項目未通過（見上方第二欄為 f 的項目）'
DO $$
BEGIN
    RAISE EXCEPTION 'api-db-role-check: 有檢查項目未通過';
END
$$;
\endif
