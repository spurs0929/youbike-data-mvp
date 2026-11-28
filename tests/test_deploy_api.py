"""API-3a 部署檔的靜態檢查：防止之後修改時意外放寬安全設定。

只讀取 deploy/ 底下的文字檔，不需要 systemd、Caddy 或資料庫。
實際語法檢查在 VM 上做（systemd-analyze verify、caddy validate），見 deploy/README.md〈API-3〉。
"""

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def _read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def _without_comments(text: str, marker: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(marker)
    )


@pytest.fixture(scope="module")
def unit() -> dict[str, list[str]]:
    """把 unit 檔解析成 {鍵: [值, ...]}（續行以反斜線合併）。"""
    text = _without_comments(_read("youbike-api.service"), "#")
    text = re.sub(r"\\\n\s*", " ", text)
    settings: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings.setdefault(key.strip(), []).append(value.strip())
    return settings


@pytest.fixture(scope="module")
def caddy() -> str:
    return _without_comments(_read("Caddyfile"), "#")


@pytest.fixture(scope="module")
def role_sql() -> str:
    return _without_comments(_read("api-db-role.sql"), "--")


# ================================================================ systemd unit


def test_unit_runs_as_youbike_api_matching_db_role(unit):
    # peer 驗證：系統使用者名稱必須與資料庫角色名稱完全相同（底線，不是連字號）
    assert unit["User"] == ["youbike_api"]
    assert unit["Group"] == ["youbike_api"]
    assert unit["Environment"].count("YOUBIKE_DATABASE_URL=postgresql:///youbike") == 1


def test_unit_binds_localhost_single_worker_without_access_log(unit):
    (exec_start,) = unit["ExecStart"]
    args = exec_start.split()
    assert args[0] == "/opt/youbike-api/.venv/bin/uvicorn"
    assert "app_main:create_production_app" in args and "--factory" in args
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--workers") + 1] == "1"
    assert "--no-access-log" in args  # 查詢字串含使用者座標
    assert "0.0.0.0" not in exec_start


def test_unit_limits_concurrency(unit):
    (exec_start,) = unit["ExecStart"]
    args = exec_start.split()
    assert args[args.index("--limit-concurrency") + 1] == "32"


def test_unit_memory_limits(unit):
    assert unit["MemoryHigh"] == ["200M"]
    assert unit["MemoryMax"] == ["300M"]


def test_unit_line_keys_file_is_optional_and_not_in_repo(unit):
    assert unit["EnvironmentFile"] == ["-/etc/youbike/api.env"]
    joined = " ".join(v for values in unit.values() for v in values)
    assert "LINE_CHANNEL" not in joined  # 金鑰不寫在 unit 檔


@pytest.mark.parametrize(
    "key,value",
    [
        ("NoNewPrivileges", "yes"),
        ("ProtectSystem", "strict"),
        ("ProtectHome", "yes"),
        ("PrivateTmp", "yes"),
        ("CapabilityBoundingSet", ""),
        ("RestrictAddressFamilies", "AF_UNIX AF_INET AF_INET6"),
    ],
)
def test_unit_hardening(unit, key, value):
    assert unit[key] == [value]


def test_unit_does_not_use_youbike_home(unit):
    joined = " ".join(v for values in unit.values() for v in values)
    assert "/home/youbike" not in joined


# ================================================================ Caddyfile


def test_caddy_has_no_wildcard_paths(caddy):
    assert "*" not in caddy  # 不使用 /stations* 這類萬用字元


def test_caddy_default_handler_is_404(caddy):
    # 最後一個 handle 沒有比對條件，一律 404
    assert re.search(r"handle\s*\{\s*respond 404\s*\}\s*\}\s*$", caddy)


def test_caddy_allowlist_is_exact(caddy):
    paths = re.findall(r"^\s*path\s+(.+)$", caddy, flags=re.MULTILINE)
    assert sorted(p.strip() for p in paths) == [
        "/health /stations /stations/nearby",
        "/healthz",
        "/line/webhook",
    ]
    regexps = re.findall(r"^\s*path_regexp\s+(.+)$", caddy, flags=re.MULTILINE)
    assert regexps == ["^/stations/[A-Za-z0-9_-]{1,32}$"]
    methods = re.findall(r"^\s*method\s+(.+)$", caddy, flags=re.MULTILINE)
    assert sorted(m.strip() for m in methods) == ["GET", "GET", "GET", "POST"]


@pytest.mark.parametrize(
    "path,allowed",
    [
        ("/stations/500101001", True),
        ("/stations/nearby", True),
        ("/stations/ABC_1-2", True),
        ("/stations/" + "1" * 32, True),
        ("/stations/" + "1" * 33, False),
        ("/stations/", False),
        ("/stations/a/b", False),
        ("/stations/a.b", False),
        ("/stations/../ready", False),
        ("/stations/%2e%2e", False),
    ],
)
def test_caddy_station_regexp(caddy, path, allowed):
    (pattern,) = re.findall(r"^\s*path_regexp\s+(\S+)$", caddy, flags=re.MULTILINE)
    assert bool(re.match(pattern, path)) is allowed


@pytest.mark.parametrize("hidden", ["/ready", "/docs", "/redoc", "/openapi.json"])
def test_caddy_does_not_expose_internal_paths(caddy, hidden):
    assert hidden not in caddy


def test_caddy_proxies_only_to_localhost_8000(caddy):
    targets = re.findall(r"reverse_proxy\s+(\S+)", caddy)
    assert targets and set(targets) == {"127.0.0.1:8000"}


def test_caddy_webhook_body_limit_and_server_header(caddy):
    assert re.search(r"request_body\s*\{\s*max_size 1MB\s*\}", caddy)
    assert "header -Server" in caddy


def test_caddy_removes_via_header_at_site_level(caddy):
    # reverse_proxy 會加「Via: 1.1 Caddy」；必須在站台層刪除，所有轉送路由都適用
    site = re.search(
        r"youbike-data-mvp\.duckdns\.org\s*\{(.*)\}\s*$", caddy, flags=re.DOTALL
    )
    assert site
    before_first_matcher = site.group(1).split("@", 1)[0]
    assert re.search(r"^\s*header -Via\s*$", before_first_matcher, flags=re.MULTILINE)


def test_caddy_healthz_is_get_only(caddy):
    # 每個 handle 都要經具名 matcher（含 method），只有最後的預設 404 沒有條件
    assert "handle /" not in caddy
    assert re.search(
        r"@healthz\s*\{\s*method GET\s*path /healthz\s*\}\s*handle @healthz\s*\{\s*respond \"ok\" 200",
        caddy,
    )


# ================================================================ 資料庫角色


def test_role_name_matches_unit_user(role_sql):
    assert "CREATE ROLE youbike_api LOGIN" in role_sql


def test_role_is_select_only_on_three_tables(role_sql):
    grants = re.findall(r"GRANT\s+(.+?)\s+TO\s+youbike_api", role_sql, flags=re.DOTALL)
    assert [" ".join(g.split()) for g in grants] == [
        "CONNECT ON DATABASE youbike",
        "USAGE ON SCHEMA public",
        "SELECT ON TABLE stations, station_snapshots, fetch_batches",
    ]
    upper = role_sql.upper()
    for forbidden in (
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "ALL PRIVILEGES",
        "PASSWORD",
    ):
        assert forbidden not in upper
    assert "NOSUPERUSER" in upper and "NOCREATEROLE" in upper


def test_role_defense_in_depth_settings(role_sql):
    assert "SET default_transaction_read_only = on" in role_sql
    assert "SET statement_timeout = '5s'" in role_sql
    assert "CONNECTION LIMIT 6" in role_sql


# ================================================================ 權限檢查 SQL


def _role_check_statements() -> tuple[list[str], list[str]]:
    """把檢查腳本切成（SQL 查詢, DO 區塊）。

    DO 區塊（$$ … $$）內含分號，先整塊取出；其餘以分號與 psql 反斜線指令（\\gset、\\echo、\\if…）切割。
    """
    text = _without_comments(_read("api-db-role-check.sql"), "--")
    blocks = re.findall(r"^DO \$\$.*?^\$\$;", text, flags=re.MULTILINE | re.DOTALL)
    for block in blocks:
        text = text.replace(block, "")
    sql_lines = [
        ";" if line.lstrip().startswith("\\") else line for line in text.splitlines()
    ]
    queries = [s.strip() for s in "\n".join(sql_lines).split(";") if s.strip()]
    return queries, blocks


def test_role_check_sql_is_read_only():
    # 檢查腳本只能查詢：每個敘述都以 SELECT 或 WITH 開頭；
    # 唯一的例外是不通過時讓 psql 非零結束的 DO 區塊，裡面只有 RAISE EXCEPTION
    queries, blocks = _role_check_statements()
    assert len(queries) == 9  # 7 項檢查＋info＋總結
    for statement in queries:
        assert statement.split()[0].upper() in {"SELECT", "WITH"}, statement
    assert len(blocks) == 1
    body = " ".join(blocks[0].split())
    assert re.fullmatch(r"DO \$\$ BEGIN RAISE EXCEPTION '[^']*'; END \$\$;", body), body


def test_role_check_sql_exits_non_zero_when_any_check_fails():
    text = _read("api-db-role-check.sql")
    lines = [line.strip() for line in text.splitlines()]
    # ON_ERROR_STOP 寫在檔案內，不依賴命令列的 -v ON_ERROR_STOP=1
    assert "\\set ON_ERROR_STOP on" in lines
    assert lines.index("\\set ON_ERROR_STOP on") < next(
        i for i, line in enumerate(lines) if line.startswith("SELECT")
    )
    # 7 項檢查各自存進 c1_～c7_，總結要把 7 項全部 AND 起來
    for n in range(1, 8):
        assert f"\\gset c{n}_" in lines
        assert f":'c{n}_ok'::boolean" in text
    assert "\\if :all_ok" in lines
    else_part = text[text.index("\\else") : text.index("\\endif")]
    assert "RAISE EXCEPTION" in else_part


def test_role_check_sql_covers_effective_privileges():
    text = _read("api-db-role-check.sql")
    for needle in (
        "pg_auth_members",  # 角色繼承
        "has_table_privilege",
        "has_sequence_privilege",
        "aclexplode",  # PUBLIC 授權
        "has_schema_privilege",
        "has_database_privilege",
        "ARRAY['fetch_batches:SELECT', 'station_snapshots:SELECT', 'stations:SELECT']",
    ):
        assert needle in text
