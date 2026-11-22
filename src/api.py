"""YouBike 2.0 查詢 API（FastAPI）。

所有查詢經由 StationRepository（設計定版 v1.3 第七節、8.4），本模組不寫 SQL。
以 create_app(repository) 建立 app：正式環境傳入 PostgresStationRepository，
測試傳入 InMemoryStationRepository，不需要資料庫。

端點：
- GET /stations/nearby?lat=&lng=&radius_m=&limit=  附近站點（依距離排序）
- GET /stations?q=&limit=                         站名搜尋
- GET /stations/{station_id}                       指定站點；查無資料回 404
- GET /health                                     存活檢查，不碰資料庫，固定 200
- GET /ready                                      就緒檢查：執行 readiness_check（正式環境為
                                                  連線池 SELECT 1）；資料庫無法使用時 503

回應：
- 列表回傳 {"count": n, "items": [...]}；查無資料時 count 為 0、items 為空陣列，不回 404。
- 時間欄位為 UTC（ISO 8601）。
- 參數錯誤一律 422：型別錯誤、缺少參數由 FastAPI 檢查；範圍與長度由 Repository
  丟出 InvalidQueryError，再轉為 422。超過上限不會被自動截斷。
  兩種 422 的 detail 格式不同：FastAPI 型別檢查為陣列，InvalidQueryError 為一句中文（刻意維持）。
- 資料庫無法使用（RepositoryUnavailableError）一律 503，回應只有固定訊息，
  不含連線字串或錯誤細節；細節只寫進伺服器端日誌。

資料庫連線池與正式環境設定在 app_main.py；本模組不匯入 psycopg，可只用 Mock 測試。
尚未包含：資料過期提示（LINE Bot 階段再決定）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from repository import (
    DEFAULT_LIMIT,
    DEFAULT_RADIUS_M,
    UNAVAILABLE_MESSAGE,
    InvalidQueryError,
    RepositoryUnavailableError,
    StationRepository,
    StationStatus,
)

logger = logging.getLogger(__name__)


class StationList(BaseModel):
    count: int
    items: list[StationStatus]


def _station_list(items: list[StationStatus]) -> StationList:
    return StationList(count=len(items), items=items)


async def _invalid_query_handler(request: Request, exc: InvalidQueryError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def _log_unavailable(where: str, exc: RepositoryUnavailableError) -> None:
    cause = exc.__cause__
    logger.warning(
        "%s：資料庫無法使用（%s：%s）",
        where,
        type(cause).__name__ if cause else "-",
        cause,
    )


async def _unavailable_handler(request: Request, exc: RepositoryUnavailableError):
    _log_unavailable(request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": UNAVAILABLE_MESSAGE})


def create_app(
    repository: StationRepository,
    *,
    readiness_check: Callable[[], None] | None = None,
    lifespan=None,
) -> FastAPI:
    """建立 app。

    - readiness_check：/ready 呼叫的檢查函式，失敗時丟 RepositoryUnavailableError。
      未提供時（例如只用 Mock）/ready 一律回 200。
    - lifespan：FastAPI lifespan，正式環境用來開啟與關閉連線池（見 app_main.py）。
    """
    app = FastAPI(title="YouBike 2.0 API", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(InvalidQueryError, _invalid_query_handler)
    app.add_exception_handler(RepositoryUnavailableError, _unavailable_handler)

    @app.get("/health")
    def health() -> dict:
        # 只代表程式活著；不碰資料庫，資料庫離線時仍回 200。
        return {"status": "ok"}

    @app.get(
        "/ready",
        responses={503: {"description": "資料庫無法使用"}},
    )
    def ready():
        if readiness_check is not None:
            try:
                readiness_check()
            except RepositoryUnavailableError as exc:
                _log_unavailable("/ready", exc)
                return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ready"}

    # 同步函式：FastAPI 會放到執行緒池執行，不會卡住事件迴圈（psycopg 為同步連線）。
    # /stations/nearby 必須宣告在 /stations/{station_id} 之前，否則 "nearby" 會被當成站號。

    @app.get("/stations/nearby", response_model=StationList)
    def find_nearby(
        lat: float,
        lng: float,
        radius_m: float = DEFAULT_RADIUS_M,
        limit: int = DEFAULT_LIMIT,
    ) -> StationList:
        return _station_list(repository.find_nearby(lat, lng, radius_m, limit))

    @app.get("/stations", response_model=StationList)
    def search_stations(q: str, limit: int = DEFAULT_LIMIT) -> StationList:
        return _station_list(repository.search_by_name(q, limit))

    @app.get(
        "/stations/{station_id}",
        response_model=StationStatus,
        responses={404: {"description": "查無站點，或沒有最近 30 分鐘內的快照"}},
    )
    def get_station(station_id: str) -> StationStatus:
        status = repository.get_station(station_id)
        if status is None:
            raise HTTPException(
                status_code=404, detail="查無站點，或沒有最近 30 分鐘內的快照"
            )
        return status

    return app
