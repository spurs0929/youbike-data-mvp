"""API-2b：真實 PostgreSQL＋psycopg_pool 連線池的整合測試。

需要測試資料庫，設定方式與 tests/test_repository_db.py 相同：

    YOUBIKE_TEST_DATABASE_URL=postgresql://youbike_test@localhost:5432/youbike_test_db

- 未設定時，本檔所有測試自動 skip。
- 已設定但無法連線、或資料庫名稱不含 "test" 時，測試直接失敗。

驗證項目：
- 經連線池查詢三個端點；連線時區設為 Asia/Taipei，回應時間仍為 UTC。
- 請求（含例外路徑）後沒有 idle in transaction；連線數不超過 max_size=4。
- SQL 錯誤、連線仍正常的 OperationalError 不會被包成 503。
- 連線中斷轉為 RepositoryUnavailableError，之後連線池換新連線。
- lifespan 結束後連線池關閉、連線全部釋放。
- 資料庫離線 → /ready 503；恢復 → 200（以本檔的 TCP 轉送器模擬資料庫斷線）。

每個測試以唯一的 application_name 建立自己的連線池，從 pg_stat_activity 只看自己的連線。
"""

import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app_main
from repository import PooledStationRepository, RepositoryUnavailableError

try:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from psycopg_pool import PoolTimeout
except ImportError:  # 未設定測試資料庫時允許不安裝
    psycopg = None

DB_URL = os.environ.get("YOUBIKE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="未設定 YOUBIKE_TEST_DATABASE_URL，略過資料庫測試"
)

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "001_init.sql"

UTC = timezone.utc
NOW = datetime(2026, 11, 21, 8, 32, 30, tzinfo=UTC)
SLOT = datetime(2026, 11, 21, 8, 30, tzinfo=UTC)
TAIPEI_101 = (25.0339, 121.5645)
IN_TRANSACTION = ("idle in transaction", "idle in transaction (aborted)")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema():
    if psycopg is None:
        pytest.fail("已設定 YOUBIKE_TEST_DATABASE_URL 但未安裝 psycopg／psycopg-pool")
    name = f"youbike_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        if "test" not in database.lower():
            pytest.fail(f"拒絕在資料庫 {database!r} 執行測試：名稱必須包含 'test'")
        conn.execute(f'CREATE SCHEMA "{name}"')
        try:
            conn.execute(f'SET search_path TO "{name}"')
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
            seed(conn)
            yield name
        finally:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')


@pytest.fixture(scope="module")
def admin():
    """觀察 pg_stat_activity 用的連線（不屬於任何被測連線池）。"""
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture
def app_name():
    return f"youbike-api-test-{uuid.uuid4().hex[:8]}"


def pool_conninfo(schema, url=DB_URL):
    # 連線時區刻意設為 Asia/Taipei（本機情境），確認回應仍為 UTC
    return make_conninfo(
        url, options=f"-c search_path={schema} -c TimeZone=Asia/Taipei"
    )


def make_pool(conninfo, app_name, **overrides):
    return app_main.build_pool(
        conninfo, kwargs={"application_name": app_name}, **overrides
    )


def seed(conn):
    conn.execute(
        "INSERT INTO fetch_batches (batch_id, fetched_at, status, station_count, raw_path)"
        " VALUES ('b1', %s, 'success', 1, 'raw/b1.json.gz')",
        (NOW - timedelta(minutes=2),),
    )
    batch_pk = conn.execute("SELECT id FROM fetch_batches").fetchone()[0]
    lat, lng = TAIPEI_101
    conn.execute(
        "INSERT INTO stations (station_id, station_name, district, address,"
        " latitude, longitude, first_seen_at, last_seen_at)"
        " VALUES ('S1', '捷運台北101站', '信義區', '某路', %s, %s, %s, %s)",
        (lat, lng, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO station_snapshots (station_id, slot_time, fetch_batch_id,"
        " info_time, is_active, total_spaces, available_bikes, available_spaces,"
        " is_valid, invalid_reason)"
        " VALUES ('S1', %s, %s, %s, true, 20, 5, 15, true, NULL)",
        (SLOT, batch_pk, NOW - timedelta(minutes=3)),
    )


def states(admin, app_name):
    rows = admin.execute(
        "SELECT state FROM pg_stat_activity WHERE application_name = %s",
        (app_name,),
    ).fetchall()
    return [row[0] for row in rows]


def wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def assert_no_open_transaction(admin, app_name):
    found = states(admin, app_name)
    assert not [s for s in found if s in IN_TRANSACTION], found
    assert len(found) <= app_main.POOL_MAX_SIZE, found


# ---------------------------------------------------------------------------
# 端點經由連線池
# ---------------------------------------------------------------------------


def test_endpoints_through_pool_with_taipei_session_time_zone(schema, admin, app_name):
    pool = make_pool(pool_conninfo(schema), app_name)
    app = app_main.create_app_with_pool(pool, clock=lambda: NOW)
    with TestClient(app) as client:
        assert client.get("/ready").json() == {"status": "ready"}

        # 確認測試真的在 Asia/Taipei 連線時區下執行
        with pool.connection() as conn:
            assert conn.execute("SHOW TimeZone").fetchone()[0] == "Asia/Taipei"

        body = client.get("/stations/S1").json()
        slot = datetime.fromisoformat(body["slot_time"].replace("Z", "+00:00"))
        assert slot == SLOT
        assert slot.utcoffset() == timedelta(0)

        found = client.get("/stations", params={"q": "101"}).json()
        assert [item["station_id"] for item in found["items"]] == ["S1"]

        lat, lng = TAIPEI_101
        near = client.get("/stations/nearby", params={"lat": lat, "lng": lng}).json()
        assert [item["station_id"] for item in near["items"]] == ["S1"]

        assert client.get("/stations/NOPE").status_code == 404
        assert_no_open_transaction(admin, app_name)


def test_concurrent_requests_stay_within_pool_limit(schema, admin, app_name):
    from concurrent.futures import ThreadPoolExecutor

    pool = make_pool(pool_conninfo(schema), app_name)
    app = app_main.create_app_with_pool(pool, clock=lambda: NOW)
    lat, lng = TAIPEI_101
    urls = [
        "/ready",
        "/stations/S1",
        "/stations?q=站",
        f"/stations/nearby?lat={lat}&lng={lng}",
    ]
    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=16) as executor:
            codes = list(executor.map(lambda u: client.get(u).status_code, urls * 10))
        assert codes == [200] * len(codes)
        assert pool.get_stats().get("pool_size", 0) <= app_main.POOL_MAX_SIZE
        found = states(admin, app_name)
        assert 1 <= len(found) <= app_main.POOL_MAX_SIZE
        assert all(state == "idle" for state in found), found


def test_pool_never_exceeds_max_size(schema, admin, app_name):
    pool = make_pool(pool_conninfo(schema), app_name, timeout=0.5)
    app = app_main.create_app_with_pool(pool, clock=lambda: NOW)
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        held = [pool.getconn() for _ in range(app_main.POOL_MAX_SIZE)]
        try:
            assert len(states(admin, app_name)) == app_main.POOL_MAX_SIZE
            # 第 5 條借不到：直接借逾時，經 API 則是 503
            with pytest.raises(PoolTimeout):
                pool.getconn(timeout=0.2)
            response = client.get("/ready")
            assert response.status_code == 503
            assert response.json() == {"status": "unavailable"}
            assert client.get("/stations/S1").status_code == 503
            assert len(states(admin, app_name)) == app_main.POOL_MAX_SIZE
        finally:
            for conn in held:
                pool.putconn(conn)
        assert client.get("/ready").status_code == 200
        assert client.get("/stations/S1").status_code == 200
        assert_no_open_transaction(admin, app_name)


# ---------------------------------------------------------------------------
# 例外路徑：連線歸還、transaction 清理、不掩蓋錯誤
# ---------------------------------------------------------------------------


@pytest.fixture
def opened_pool(schema, app_name):
    pool = make_pool(pool_conninfo(schema), app_name, timeout=2)
    pool.open(wait=True)
    yield pool
    pool.close()


def raise_after_query(conn):
    conn.execute("SELECT 1")  # 非 autocommit：已隱含開始 transaction
    raise RuntimeError("程式錯誤")


def undefined_table(conn):
    with conn.transaction():
        conn.execute("SELECT * FROM no_such_table")


def statement_timeout(conn):
    with conn.transaction():
        conn.execute("SET LOCAL statement_timeout = 10")
        conn.execute("SELECT pg_sleep(1)")


@pytest.mark.parametrize(
    "action, expected",
    [
        (raise_after_query, RuntimeError),
        (undefined_table, "UndefinedTable"),
        (statement_timeout, "QueryCanceled"),
    ],
)
def test_errors_are_not_hidden_and_leave_no_open_transaction(
    opened_pool, admin, app_name, action, expected
):
    if isinstance(expected, str):
        expected = getattr(psycopg.errors, expected)
    repo = PooledStationRepository(opened_pool)
    with pytest.raises(expected) as info:
        repo._run(action)
    assert not isinstance(info.value, RepositoryUnavailableError)

    # 連線已歸還，且回到 idle（連線池在歸還時把未結束的 transaction 回滾）
    assert wait_for(
        lambda: (
            opened_pool.get_stats().get("pool_available", 0)
            == opened_pool.get_stats().get("pool_size", -1)
        )
    )
    assert_no_open_transaction(admin, app_name)
    repo.ping()  # 同一個連線池仍可使用


def test_sql_error_in_endpoint_is_500_not_503(schema, admin, app_name):
    # search_path 指向不存在的 schema：SELECT 1 正常，但查詢資料表是 SQL 錯誤
    missing = f"youbike_test_missing_{uuid.uuid4().hex[:8]}"

    def make_app():
        # 連線池關閉後不能重新開啟，每個 TestClient 各建一個
        pool = make_pool(pool_conninfo(missing), app_name)
        return app_main.create_app_with_pool(pool, clock=lambda: NOW)

    with TestClient(make_app()) as client:
        assert client.get("/ready").status_code == 200
        with pytest.raises(psycopg.errors.UndefinedTable):
            client.get("/stations/S1")
        assert_no_open_transaction(admin, app_name)
    with TestClient(make_app(), raise_server_exceptions=False) as client:
        assert client.get("/stations/S1").status_code == 500
        assert_no_open_transaction(admin, app_name)


def test_terminated_connection_becomes_unavailable_then_recovers(
    opened_pool, admin, app_name
):
    repo = PooledStationRepository(opened_pool)
    with pytest.raises(RepositoryUnavailableError) as info:
        repo._run(
            lambda conn: conn.execute("SELECT pg_terminate_backend(pg_backend_pid())")
        )
    assert isinstance(info.value.__cause__, psycopg.OperationalError)
    repo.ping()  # 壞掉的連線已被換掉
    assert_no_open_transaction(admin, app_name)


# ---------------------------------------------------------------------------
# lifespan
# ---------------------------------------------------------------------------


def test_lifespan_shutdown_releases_all_connections(schema, admin, app_name):
    pool = make_pool(pool_conninfo(schema), app_name)
    app = app_main.create_app_with_pool(pool, clock=lambda: NOW)
    assert pool.closed
    with TestClient(app) as client:
        assert client.get("/stations/S1").status_code == 200
        assert wait_for(lambda: len(states(admin, app_name)) >= 1)
        assert_no_open_transaction(admin, app_name)
    assert pool.closed
    assert wait_for(lambda: states(admin, app_name) == [])
    with pytest.raises(RepositoryUnavailableError):
        PooledStationRepository(pool).ping()


# ---------------------------------------------------------------------------
# 資料庫離線 → 恢復
# ---------------------------------------------------------------------------


class TcpProxy:
    """把 127.0.0.1:port 轉送到資料庫；stop() 切斷所有連線，模擬資料庫離線。"""

    def __init__(self, host, port):
        self.target = (host, port)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self._lock = threading.Lock()
        self._sockets = set()
        self._running = False
        self._thread = None

    def start(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen()
        listener.settimeout(0.1)
        self._running = True
        self._thread = threading.Thread(
            target=self._accept, args=(listener,), daemon=True
        )
        self._thread.start()

    def _accept(self, listener):
        with listener:
            while self._running:
                try:
                    client, _ = listener.accept()
                except TimeoutError:  # socket.timeout 在 3.10 起是 TimeoutError 的別名
                    continue
                except OSError:
                    return
                try:
                    upstream = socket.create_connection(self.target, timeout=5)
                except OSError:
                    client.close()
                    continue
                client.settimeout(None)
                upstream.settimeout(None)
                with self._lock:
                    self._sockets.update((client, upstream))
                for src, dst in ((client, upstream), (upstream, client)):
                    threading.Thread(
                        target=self._pump, args=(src, dst), daemon=True
                    ).start()

    def _pump(self, src, dst):
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            self._close(src)
            self._close(dst)

    def _close(self, sock):
        with self._lock:
            self._sockets.discard(sock)
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            self._close(sock)


def wait_ready(client, timeout=30.0):
    return wait_for(lambda: client.get("/ready").status_code == 200, timeout=timeout)


def test_ready_goes_503_while_database_offline_and_200_after_recovery(
    schema, admin, app_name
):
    params = conninfo_to_dict(DB_URL)
    host = params.get("host") or "localhost"
    if host.startswith("/") or "," in host:
        pytest.skip("此測試需要以 TCP 連線的測試資料庫")
    proxy = TcpProxy(host, int(params.get("port") or 5432))
    url = make_conninfo(pool_conninfo(schema), host="127.0.0.1", port=proxy.port)
    pool = make_pool(url, app_name, timeout=1)
    app = app_main.create_app_with_pool(pool, clock=lambda: NOW)
    try:
        # 1. 資料庫離線時啟動：app 照樣啟動
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 503

            # 2. 資料庫恢復：連線池在背景重連，/ready 轉為 200
            proxy.start()
            assert wait_ready(client)
            assert client.get("/stations/S1").status_code == 200

            # 3. 執行中斷線：既有連線被切斷，借出前檢查失敗 → 503
            proxy.stop()
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 503

            # 4. 再次恢復
            proxy.start()
            assert wait_ready(client)
            assert client.get("/stations/S1").status_code == 200
            assert_no_open_transaction(admin, app_name)
        assert wait_for(lambda: states(admin, app_name) == [])
    finally:
        proxy.stop()
