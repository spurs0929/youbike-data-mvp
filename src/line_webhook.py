"""LINE Messaging API webhook（LINE-0）：簽章驗證、事件解析、回覆文字訊息。

分工：
- LINE 協定交給官方 SDK（line-bot-sdk v3）：X-Line-Signature 驗證與事件解析用
  linebot.v3.webhook.WebhookParser，回覆用 linebot.v3.messaging.MessagingApi。
- 本模組負責路由、安全邊界與錯誤處理；冪等（webhookEventId）、限流、額度、
  站點查詢與 LLM 在後續階段加入。

端點：POST /line/webhook

狀態碼：
- 200：簽章正確並處理完畢（包含 LINE 後台按 Verify 時送來的空事件、略過的事件、
  回覆失敗）。回覆失敗只寫日誌：回 500 會讓 LINE 重送，而目前還沒有冪等處理。
- 400：缺少或錯誤的 X-Line-Signature、body 不是 UTF-8 或不是合法的 webhook JSON。
  不處理任何事件。
- 413：body 超過 MAX_BODY_BYTES。Content-Length 超過時不讀 body；沒有 Content-Length（chunked）時
  以 request.stream() 邊讀邊累計，一超過就停止讀取，不會先把整個 body 讀進記憶體。
- 503：LINE 金鑰未設定（fail closed）。不讀 body、不驗證簽章、不處理事件；
  不會用空字串當 secret 去驗證簽章。

金鑰（環境變數，兩個都要有）：
- LINE_CHANNEL_SECRET：驗證簽章用。
- LINE_CHANNEL_ACCESS_TOKEN：呼叫 Reply API 用。
未設定或空白時 read_line_config() 回傳 None，webhook 一律 503；其餘 API 照常運作。

日誌：不記錄 secret、access token、reply token、簽章值、訊息內容與 LINE userId。
除錯需要辨識使用者時，只記 user_log_id()：以 channel secret 衍生的金鑰對 userId 做 HMAC-SHA256，
取十六進位前 16 碼（64 bit）。同一個 userId 每次得到相同代碼，但無法由代碼反推 userId。

注意：
- 讀原始 body 需要 async 端點；SDK 的 MessagingApi 是同步 HTTP 呼叫，
  一律以 run_in_threadpool 執行，等待 LINE 回應時不會卡住事件迴圈（/health 等照常回應）。
- Reply API 明確設定 timeout（REPLY_TIMEOUT_S，連線／讀取）：SDK 預設不設 timeout，
  LINE 沒有回應時會一直佔住執行緒池（查詢 API 的同步端點也用同一個執行緒池）。
- LINE 以 UTF-16 code unit 計算文字長度（emoji 等 BMP 以外的字元算 2）；
  截斷一律用 truncate_utf16()，不用 Python 的字元數。
- InvalidSignatureError 必須從 linebot.v3.exceptions 匯入；舊版 linebot.exceptions
  的同名類別是不同的類別，接不到 v3 WebhookParser 丟出的例外。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/line/webhook"
SECRET_ENV = "LINE_CHANNEL_SECRET"
ACCESS_TOKEN_ENV = "LINE_CHANNEL_ACCESS_TOKEN"
SIGNATURE_HEADER = "X-Line-Signature"

# LINE 的 webhook body 通常只有幾 KB；設上限避免讀入過大的請求。
MAX_BODY_BYTES = 1024 * 1024
# LINE 文字訊息上限 5,000 字，以 UTF-16 code unit 計算（見 truncate_utf16）。
MAX_TEXT_LENGTH = 5000
# Reply API 的 (連線, 讀取) timeout 秒數；SDK 轉成 urllib3.Timeout(connect=…, read=…)。
REPLY_TIMEOUT_S = (3.0, 5.0)
ECHO_PREFIX = "收到："
# 日誌用的使用者代碼：HMAC 金鑰由 channel secret 衍生（加上用途標籤，與簽章驗證用途分開）
USER_LOG_KEY_LABEL = b"youbike-line-user-log-v1"
USER_LOG_ID_LENGTH = 16

ReplyFunc = Callable[[str, str], None]  # (reply_token, text) -> None


@dataclass(frozen=True)
class LineConfig:
    channel_secret: str
    channel_access_token: str

    def __post_init__(self) -> None:
        if not self.channel_secret or not self.channel_access_token:
            raise ValueError("LINE channel secret 與 access token 都不可為空")

    def __repr__(self) -> str:  # 避免金鑰出現在日誌或錯誤訊息
        return "LineConfig(channel_secret=***, channel_access_token=***)"


def read_line_config(environ: Mapping[str, str] | None = None) -> LineConfig | None:
    """讀 LINE 金鑰；任一個未設定或空白時回傳 None（webhook 將 fail closed）。"""
    env = os.environ if environ is None else environ
    secret = env.get(SECRET_ENV, "").strip()
    token = env.get(ACCESS_TOKEN_ENV, "").strip()
    if not secret or not token:
        return None
    return LineConfig(channel_secret=secret, channel_access_token=token)


def make_sdk_reply(channel_access_token: str) -> ReplyFunc:
    """以 SDK 的 MessagingApi 回覆一則文字訊息（同步，會發出 HTTP 請求）。"""
    configuration = Configuration(access_token=channel_access_token)

    def reply(reply_token: str, text: str) -> None:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token, messages=[TextMessage(text=text)]
                ),
                _request_timeout=REPLY_TIMEOUT_S,
            )

    return reply


def derive_user_log_key(channel_secret: str) -> bytes:
    return hmac.new(
        channel_secret.encode("utf-8"), USER_LOG_KEY_LABEL, hashlib.sha256
    ).digest()


def user_log_id(user_id: str | None, key: bytes) -> str:
    """日誌用的使用者代碼：HMAC-SHA256(key, userId) 的前 16 碼；沒有 userId 時回傳 "-"。"""
    if not user_id:
        return "-"
    digest = hmac.new(key, user_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:USER_LOG_ID_LENGTH]


def utf16_length(text: str) -> int:
    """LINE 計算的文字長度：UTF-16 code unit 數（BMP 以外的字元，例如多數 emoji，算 2）。"""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def truncate_utf16(text: str, limit: int = MAX_TEXT_LENGTH) -> str:
    """依 LINE 的計算方式截斷到 limit 個 UTF-16 code unit 以內。

    以 Python 字元（code point）為單位捨棄，不會把一個 emoji 的兩個 code unit 切開；
    剩一個 code unit 的空間時遇到 emoji，整個捨棄。
    """
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return text[:index]
    return text


def build_echo_text(user_text: str) -> str:
    """LINE-0 的固定回覆：「收到：<使用者訊息>」，超過 LINE 上限時截斷。"""
    return truncate_utf16(ECHO_PREFIX + user_text)


async def read_body_limited(request: Request, limit: int) -> bytes | None:
    """邊讀邊累計 body 大小；超過 limit 時立刻停止讀取並回傳 None。"""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def create_line_router(
    config: LineConfig | None,
    *,
    reply_func: ReplyFunc | None = None,
) -> APIRouter:
    """建立 webhook 路由。

    - config 為 None：路由仍存在，但一律回 503，不做任何處理（fail closed）。
    - reply_func：只供測試注入；正式環境用 make_sdk_reply()。
    """
    router = APIRouter()
    parser = WebhookParser(config.channel_secret) if config is not None else None
    user_log_key = (
        derive_user_log_key(config.channel_secret) if config is not None else b""
    )
    if config is not None and reply_func is None:
        reply_func = make_sdk_reply(config.channel_access_token)

    @router.post(WEBHOOK_PATH, include_in_schema=False)
    async def line_webhook(request: Request):
        if parser is None or reply_func is None:
            logger.warning("LINE webhook 收到請求，但金鑰未設定，回 503")
            return _error(503, "LINE webhook 未設定")

        signature = request.headers.get(SIGNATURE_HEADER, "")
        if not signature:
            logger.warning("LINE webhook：缺少 %s", SIGNATURE_HEADER)
            return _error(400, "缺少簽章")

        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return _error(413, "內容過大")
        raw = await read_body_limited(request, MAX_BODY_BYTES)
        if raw is None:
            return _error(413, "內容過大")

        try:
            body = raw.decode("utf-8")
            events = parser.parse(body, signature)
        except InvalidSignatureError:
            # 例外訊息含簽章值，不寫進日誌。
            logger.warning("LINE webhook：簽章驗證失敗")
            return _error(400, "簽章驗證失敗")
        except (ValueError, KeyError, TypeError):
            # 簽章正確但內容不是合法的 webhook JSON（UnicodeDecodeError、JSONDecodeError 都是 ValueError）。
            logger.warning("LINE webhook：內容格式錯誤")
            return _error(400, "內容格式錯誤")

        replied = 0
        for event in events:
            source = getattr(event, "source", None)
            logger.info(
                "LINE webhook：事件 type=%s user=%s",
                getattr(event, "type", "-"),
                user_log_id(getattr(source, "user_id", None), user_log_key),
            )
            if not (
                isinstance(event, MessageEvent)
                and isinstance(event.message, TextMessageContent)
                and event.reply_token
            ):
                continue
            text = build_echo_text(event.message.text)
            try:
                await run_in_threadpool(reply_func, event.reply_token, text)
                replied += 1
            except Exception as exc:  # noqa: BLE001 — 刻意接住所有回覆錯誤，只寫日誌
                # 回覆失敗（LINE 回 4xx/5xx、網路錯誤）不影響其他事件，也不回 500：
                # 回 500 會讓 LINE 重送，而 reply token 已用過或過期，重送只會再失敗。
                logger.warning(
                    "LINE webhook：回覆失敗（%s，status=%s）",
                    type(exc).__name__,
                    getattr(exc, "status", "-"),
                )
        logger.info("LINE webhook：事件 %d 筆，回覆 %d 筆", len(events), replied)
        return {"status": "ok"}

    return router
