"""正式環境進入點：讀環境變數 → 建立連線池與 Repository → create_app（API-2b）。

啟動方式（uvicorn 自 LINE-0 起列入 requirements.txt；systemd 在 API-3）：

    uvicorn app_main:create_production_app --factory

環境變數：
- YOUBIKE_DATABASE_URL（必要）：與 4C 相同，VM 上為 postgresql:///youbike（Unix socket＋peer 驗證）。
  未設定或空白時，create_production_app() 直接丟 MissingConfigError，app 不會啟動。
- LINE_CHANNEL_SECRET、LINE_CHANNEL_ACCESS_TOKEN（LINE-0）：兩個都有設定時 /line/webhook 才會處理請求；
  任一個未設定或空白時 webhook 一律回 503（fail closed），查詢 API 照常運作，啟動時寫一筆 WARNING。

連線池（psycopg_pool）：
- min_size=1、max_size=4：一個 app instance 最多 4 條連線。VM 的 max_connections=20，
  扣除保留連線、4C 與手動 psql 後仍有餘裕；FastAPI 執行緒池（預設 40）再多請求也只會排隊借連線。
- timeout=5 秒：借不到連線時丟 PoolTimeout → RepositoryUnavailableError → HTTP 503。
- check=ConnectionPool.check_connection：借出前確認連線仍可用（資料庫重啟後，
  舊連線會被換掉，不會讓第一個請求失敗）。代價是每次借用多一次本機往返。
- open(wait=False)：在 lifespan 開啟，不等待第一條連線建立。資料庫離線時 app 照樣啟動，
  /health 回 200、/ready 回 503；資料庫恢復後連線池在背景重連，/ready 轉回 200。
- lifespan 結束時 close()，連線全部關閉。

api.py 不匯入本模組；測試可以只用 InMemoryStationRepository。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

from api import create_app
from line_webhook import LineConfig, create_line_router, read_line_config
from repository import PooledStationRepository

logger = logging.getLogger(__name__)

DATABASE_URL_ENV = "YOUBIKE_DATABASE_URL"
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 4
POOL_TIMEOUT_S = 5.0
APPLICATION_NAME = "youbike-api"  # 顯示在 pg_stat_activity.application_name


class MissingConfigError(RuntimeError):
    """缺少必要的環境變數。"""


def read_database_url(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    url = env.get(DATABASE_URL_ENV, "").strip()
    if not url:
        raise MissingConfigError(
            f"缺少環境變數 {DATABASE_URL_ENV}（PostgreSQL 連線字串，"
            "例如 VM 上為 postgresql:///youbike）"
        )
    return url


def build_pool(conninfo: str, **overrides) -> ConnectionPool:
    """建立尚未開啟的連線池。overrides 只供測試調整（例如縮短 timeout）。"""
    settings = {
        "min_size": POOL_MIN_SIZE,
        "max_size": POOL_MAX_SIZE,
        "timeout": POOL_TIMEOUT_S,
        "check": ConnectionPool.check_connection,
        "kwargs": {"application_name": APPLICATION_NAME},
        "name": APPLICATION_NAME,
    }
    settings.update(overrides)
    # open=False：建立時不連線，由 lifespan 以 open(wait=False) 開啟。
    return ConnectionPool(conninfo, open=False, **settings)


def create_app_with_pool(
    pool: ConnectionPool,
    *,
    clock: Callable[[], datetime] | None = None,
    line_config: LineConfig | None = None,
) -> FastAPI:
    """以指定的連線池建立 app；lifespan 負責開啟與關閉連線池。

    line_config 為 None 時 /line/webhook 一律 503（fail closed）。clock 只供測試注入。
    """
    repo_kwargs = {} if clock is None else {"clock": clock}
    repository = PooledStationRepository(pool, **repo_kwargs)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open(wait=False)
        try:
            yield
        finally:
            # close() 會等背景工作結束，放到執行緒執行，不卡住事件迴圈。
            await run_in_threadpool(pool.close)

    app = create_app(repository, readiness_check=repository.ping, lifespan=lifespan)
    app.include_router(create_line_router(line_config))
    app.state.pool = pool
    return app


def create_production_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    """正式環境的 app 工廠。缺少 YOUBIKE_DATABASE_URL 時丟 MissingConfigError。"""
    database_url = read_database_url(environ)
    line_config = read_line_config(environ)
    if line_config is None:
        logger.warning(
            "LINE 金鑰未設定（LINE_CHANNEL_SECRET、LINE_CHANNEL_ACCESS_TOKEN），"
            "/line/webhook 將一律回 503"
        )
    return create_app_with_pool(build_pool(database_url), line_config=line_config)
