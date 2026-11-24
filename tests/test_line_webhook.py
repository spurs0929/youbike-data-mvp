"""Tests for src/line_webhook.py（LINE-0）與 app_main 的 webhook 掛載。

不連網、不需要資料庫：
- 簽章由測試自己用 HMAC-SHA256＋base64 計算（與 LINE 平台相同的算法），
  再交給 SDK 的 WebhookParser 驗證。
- 回覆以 reply_func 注入假函式；SDK 回覆路徑另以替換 RESTClientObject.request 檢查送出的 HTTP 請求。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time

import pytest
import urllib3
from fastapi import FastAPI
from fastapi.testclient import TestClient
from linebot.v3.messaging import ApiException
from linebot.v3.messaging.rest import RESTClientObject

import app_main
import line_webhook
from line_webhook import (
    MAX_BODY_BYTES,
    MAX_TEXT_LENGTH,
    REPLY_TIMEOUT_S,
    USER_LOG_ID_LENGTH,
    WEBHOOK_PATH,
    LineConfig,
    build_echo_text,
    create_line_router,
    derive_user_log_key,
    make_sdk_reply,
    read_body_limited,
    read_line_config,
    truncate_utf16,
    user_log_id,
    utf16_length,
)

SECRET = "test-channel-secret-0123456789"
TOKEN = "test-access-token-abcdefghij"
CONFIG = LineConfig(channel_secret=SECRET, channel_access_token=TOKEN)
USER_ID = "U0123456789abcdef0123456789abcdef"


def sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def base_event(event_type="message", reply_token="reply-token-1", event_id="01EVT1"):
    return {
        "type": event_type,
        "mode": "active",
        "timestamp": 1763870000000,
        "source": {"type": "user", "userId": USER_ID},
        "webhookEventId": event_id,
        "deliveryContext": {"isRedelivery": False},
        "replyToken": reply_token,
    }


def text_event(text, reply_token="reply-token-1", event_id="01EVT1"):
    event = base_event(reply_token=reply_token, event_id=event_id)
    event["message"] = {
        "type": "text",
        "id": "468789577898262530",
        "quoteToken": "quote-token-1",
        "text": text,
    }
    return event


def sticker_event():
    event = base_event(reply_token="reply-token-sticker", event_id="01EVT2")
    event["message"] = {
        "type": "sticker",
        "id": "1501597916",
        "quoteToken": "quote-token-2",
        "stickerId": "52002738",
        "packageId": "11537",
        "stickerResourceType": "ANIMATION",
    }
    return event


def follow_event():
    event = base_event("follow", reply_token="reply-token-follow", event_id="01EVT3")
    event["follow"] = {"isUnblocked": False}
    return event


def payload(*events) -> bytes:
    body = {"destination": "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "events": list(events)}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class SpyReply:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = dict(fail_on)  # reply_token -> exception

    def __call__(self, reply_token, text):
        self.calls.append((reply_token, text))
        if reply_token in self.fail_on:
            raise self.fail_on[reply_token]


def make_client(config=CONFIG, reply=None):
    app = FastAPI()
    app.include_router(create_line_router(config, reply_func=reply))

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app)


def post(client, body: bytes, signature: str | None = "auto", **headers):
    if signature == "auto":
        signature = sign(body)
    if signature is not None:
        headers["X-Line-Signature"] = signature
    headers.setdefault("Content-Type", "application/json")
    return client.post(WEBHOOK_PATH, content=body, headers=headers)


# ================================================================ 簽章驗證


def test_valid_signature_text_message_replies_once():
    spy = SpyReply()
    response = post(make_client(reply=spy), payload(text_event("你好")))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert spy.calls == [("reply-token-1", "收到：你好")]


@pytest.mark.parametrize(
    "signature",
    [
        "invalid",
        "",
        sign(payload(text_event("你好")), secret="another-secret"),
        sign(payload(text_event("被竄改前")), secret=SECRET),
    ],
    ids=["garbage", "empty", "wrong-secret", "tampered-body"],
)
def test_invalid_signature_is_rejected_without_processing(signature):
    spy = SpyReply()
    response = post(make_client(reply=spy), payload(text_event("你好")), signature)
    assert response.status_code == 400
    assert spy.calls == []


def test_missing_signature_header_is_rejected():
    spy = SpyReply()
    response = post(make_client(reply=spy), payload(text_event("你好")), None)
    assert response.status_code == 400
    assert response.json() == {"detail": "缺少簽章"}
    assert spy.calls == []


def test_verify_request_with_no_events_returns_200():
    # LINE 後台按「Verify」時送出 events 為空陣列的請求（簽章正確）
    spy = SpyReply()
    response = post(make_client(reply=spy), payload())
    assert response.status_code == 200
    assert spy.calls == []


# ================================================================ fail closed


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"LINE_CHANNEL_SECRET": SECRET},
        {"LINE_CHANNEL_ACCESS_TOKEN": TOKEN},
        {"LINE_CHANNEL_SECRET": "  ", "LINE_CHANNEL_ACCESS_TOKEN": TOKEN},
        {"LINE_CHANNEL_SECRET": SECRET, "LINE_CHANNEL_ACCESS_TOKEN": ""},
    ],
)
def test_missing_or_blank_keys_give_no_config(environ):
    assert read_line_config(environ) is None


def test_keys_are_read_and_stripped():
    config = read_line_config(
        {"LINE_CHANNEL_SECRET": f" {SECRET}\n", "LINE_CHANNEL_ACCESS_TOKEN": TOKEN}
    )
    assert config == CONFIG


@pytest.mark.parametrize("secret,token", [("", TOKEN), (SECRET, "")])
def test_line_config_rejects_empty_keys(secret, token):
    with pytest.raises(ValueError):
        LineConfig(channel_secret=secret, channel_access_token=token)


def test_line_config_repr_hides_keys():
    assert SECRET not in repr(CONFIG) and TOKEN not in repr(CONFIG)


@pytest.mark.parametrize("secret_used_by_attacker", ["", SECRET])
def test_unconfigured_webhook_fails_closed(secret_used_by_attacker):
    # 金鑰未設定：即使簽章用空字串 secret（或任何 secret）計算，也一律 503、不處理事件
    spy = SpyReply()
    client = make_client(config=None, reply=spy)
    body = payload(text_event("你好"))
    response = post(client, body, sign(body, secret=secret_used_by_attacker))
    assert response.status_code == 503
    assert spy.calls == []


# ================================================================ 事件處理


def test_non_text_and_unknown_events_are_skipped():
    unknown = base_event("brand_new_event_type", reply_token="reply-token-x")
    spy = SpyReply()
    body = payload(sticker_event(), follow_event(), unknown)
    response = post(make_client(reply=spy), body)
    assert response.status_code == 200
    assert spy.calls == []


def test_multiple_events_reply_to_each_text_in_order():
    spy = SpyReply()
    body = payload(
        text_event("第一則", reply_token="rt-a", event_id="E1"),
        sticker_event(),
        text_event("第二則", reply_token="rt-b", event_id="E2"),
    )
    response = post(make_client(reply=spy), body)
    assert response.status_code == 200
    assert spy.calls == [("rt-a", "收到：第一則"), ("rt-b", "收到：第二則")]


def test_reply_failure_is_logged_and_still_returns_200(caplog):
    spy = SpyReply(
        fail_on={
            "rt-a": ApiException(status=400, reason="Bad Request"),
            "rt-b": RuntimeError("network down"),
        }
    )
    body = payload(
        text_event("一", reply_token="rt-a", event_id="E1"),
        text_event("二", reply_token="rt-b", event_id="E2"),
        text_event("三", reply_token="rt-c", event_id="E3"),
    )
    with caplog.at_level(logging.INFO, logger="line_webhook"):
        response = post(make_client(reply=spy), body)
    assert response.status_code == 200
    assert [token for token, _ in spy.calls] == ["rt-a", "rt-b", "rt-c"]
    assert "ApiException，status=400" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "事件 3 筆，回覆 1 筆" in caplog.text


def test_echo_text_is_truncated_to_line_limit():
    long_text = "長" * MAX_TEXT_LENGTH
    spy = SpyReply()
    post(make_client(reply=spy), payload(text_event(long_text)))
    ((_, text),) = spy.calls
    assert len(text) == MAX_TEXT_LENGTH
    assert text == build_echo_text(long_text)
    assert text.startswith("收到：長")


# ================================================================ 文字長度（UTF-16）

APPLE = "\U0001f34e"  # 🍎：BMP 以外的字元，UTF-16 為 2 個 code unit


def test_utf16_length_counts_astral_characters_as_two():
    assert utf16_length("abc") == 3
    assert utf16_length("臺北") == 2
    assert utf16_length(APPLE) == 2
    assert utf16_length("a" + APPLE + "中") == 4
    assert utf16_length(APPLE) == len(APPLE.encode("utf-16-le")) // 2


@pytest.mark.parametrize(
    "text,limit,expected",
    [
        ("a" * 5, 5, "a" * 5),  # 剛好等於上限：不截斷
        ("a" * 6, 5, "a" * 5),
        ("abcd" + APPLE, 5, "abcd"),  # 只剩 1 個 code unit：emoji 整個捨棄，不切成半個
        ("abc" + APPLE, 5, "abc" + APPLE),  # 剛好放得下
        ("中" + APPLE + "a" + APPLE, 5, "中" + APPLE + "a"),
        ("", 5, ""),
    ],
)
def test_truncate_utf16_boundaries(text, limit, expected):
    result = truncate_utf16(text, limit)
    assert result == expected
    assert utf16_length(result) <= limit
    # 結果仍是合法的 UTF-16（沒有落單的 surrogate）
    result.encode("utf-16-le")


def test_echo_of_emoji_text_stays_within_line_limit():
    # LINE-0 以字元數截斷時，5,000 個 emoji 會變成 9,997 個 code unit，LINE 回 400
    spy = SpyReply()
    post(make_client(reply=spy), payload(text_event(APPLE * MAX_TEXT_LENGTH)))
    ((_, text),) = spy.calls
    assert utf16_length(text) <= MAX_TEXT_LENGTH
    assert utf16_length(text) >= MAX_TEXT_LENGTH - 1
    assert text.startswith("收到：" + APPLE)
    assert text == build_echo_text(APPLE * MAX_TEXT_LENGTH)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe\x00broken",
        b'{"destination": "U"}',
        b'{"destination": "U", "events": 1}',
        b'{"destination": "U", "events": ["x"]}',
        b"[]",
    ],
    ids=["not-json", "not-utf8", "no-events", "events-int", "event-str", "array"],
)
def test_malformed_body_with_valid_signature_is_400(body):
    spy = SpyReply()
    response = post(make_client(reply=spy), body)
    assert response.status_code == 400
    assert spy.calls == []


def test_oversized_body_is_rejected_before_parsing():
    spy = SpyReply()
    body = payload(text_event("x" * (MAX_BODY_BYTES + 1)))
    response = post(make_client(reply=spy), body)
    assert response.status_code == 413
    assert spy.calls == []


def chunks_of(body: bytes, size: int = 64 * 1024):
    # 以產生器送出時，httpx 用 chunked 傳送，不帶 Content-Length
    for start in range(0, len(body), size):
        yield body[start : start + size]


def test_oversized_chunked_body_without_content_length_is_413():
    spy = SpyReply()
    body = payload(text_event("x" * (MAX_BODY_BYTES + 1)))
    response = make_client(reply=spy).post(
        WEBHOOK_PATH,
        content=chunks_of(body),
        headers={"X-Line-Signature": sign(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert spy.calls == []


def test_chunked_body_within_limit_is_processed():
    spy = SpyReply()
    body = payload(text_event("分段傳送"))
    response = make_client(reply=spy).post(
        WEBHOOK_PATH,
        content=chunks_of(body, size=7),
        headers={"X-Line-Signature": sign(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert spy.calls == [("reply-token-1", "收到：分段傳送")]


def test_webhook_never_reads_whole_body_at_once(monkeypatch):
    # 端點必須經 read_body_limited 邊讀邊檢查，不能呼叫 request.body() 一次讀完
    from starlette.requests import Request

    async def forbidden_body(self):
        raise AssertionError("request.body() must not be used")

    monkeypatch.setattr(Request, "body", forbidden_body)
    spy = SpyReply()
    response = post(make_client(reply=spy), payload(text_event("你好")))
    assert response.status_code == 200
    assert spy.calls == [("reply-token-1", "收到：你好")]


def test_read_body_limited_stops_reading_once_over_limit():
    consumed = []

    class FakeRequest:
        async def stream(self):
            for index in range(100):
                consumed.append(index)
                yield b"x" * 10

    async def run(limit):
        return await read_body_limited(FakeRequest(), limit)

    assert asyncio.run(run(25)) is None
    assert len(consumed) == 3  # 第 3 塊累計 30 > 25 就停，不讀完 100 塊
    consumed.clear()
    assert asyncio.run(run(1000)) == b"x" * 1000
    assert len(consumed) == 100


# ================================================================ 日誌不含機密


def test_logs_never_contain_secrets_tokens_or_message_text(caplog):
    spy = SpyReply(fail_on={"reply-token-1": ApiException(status=401)})
    client = make_client(reply=spy)
    body = payload(text_event("私人訊息內容"))
    bad_signature = sign(body, secret="another-secret")
    with caplog.at_level(logging.DEBUG):
        post(client, body)  # 回覆失敗路徑
        post(client, body, bad_signature)  # 簽章錯誤路徑
        post(client, b"not json")  # 格式錯誤路徑
        post(make_client(config=None, reply=spy), body)  # 未設定路徑
    assert caplog.records  # 確實有寫日誌
    for secret in (SECRET, TOKEN, "reply-token-1", bad_signature, "私人訊息內容"):
        assert secret not in caplog.text
    assert USER_ID not in caplog.text


# ================================================================ userId 不進日誌


def test_logs_contain_hashed_user_id_but_never_raw_user_id(caplog):
    spy = SpyReply(fail_on={"rt-b": RuntimeError("boom")})
    group_event = text_event("群組訊息", reply_token="rt-c", event_id="E3")
    group_event["source"] = {"type": "group", "groupId": "Cgroup", "userId": USER_ID}
    body = payload(
        text_event("一", reply_token="rt-a", event_id="E1"),
        text_event("二", reply_token="rt-b", event_id="E2"),  # 回覆失敗路徑
        group_event,
        sticker_event(),
        follow_event(),
    )
    with caplog.at_level(logging.DEBUG):
        response = post(make_client(reply=spy), body)
    assert response.status_code == 200
    expected = user_log_id(USER_ID, derive_user_log_key(SECRET))
    assert f"user={expected}" in caplog.text
    assert caplog.text.count(f"user={expected}") == 5
    assert USER_ID not in caplog.text
    # 也不出現 userId 的片段（例如只截掉開頭的 U）
    assert USER_ID[1:17] not in caplog.text


def test_event_without_user_id_logs_placeholder(caplog):
    event = text_event("嗨")
    event["source"] = {
        "type": "group",
        "groupId": "Cgroup",
    }  # 未加好友的群組成員沒有 userId
    with caplog.at_level(logging.INFO, logger="line_webhook"):
        post(make_client(reply=SpyReply()), payload(event))
    assert "user=-" in caplog.text


def test_user_log_id_is_stable_short_and_keyed():
    key = derive_user_log_key(SECRET)
    first = user_log_id(USER_ID, key)
    assert first == user_log_id(USER_ID, key)
    assert USER_LOG_ID_LENGTH == 16
    assert len(first) == 16 and all(c in "0123456789abcdef" for c in first)
    assert first != user_log_id("U" + "f" * 32, key)
    assert first != user_log_id(USER_ID, derive_user_log_key("another-secret"))
    # 不是未加金鑰的 SHA-256（無法用公開的 userId 自行計算比對）
    assert first != hashlib.sha256(USER_ID.encode()).hexdigest()[:16]
    assert user_log_id(None, key) == "-" and user_log_id("", key) == "-"


# ================================================================ 不卡住事件迴圈


def test_reply_runs_in_threadpool_and_does_not_block_health():
    started = threading.Event()
    release = threading.Event()

    def slow_reply(reply_token, text):
        started.set()
        release.wait(timeout=10)

    # 用 with 讓所有請求共用同一個事件迴圈；不用 with 時 TestClient 每個請求各開一個迴圈，
    # 測不出「回覆卡住事件迴圈」。
    with make_client(reply=slow_reply) as client:
        _assert_health_not_blocked(client, started, release)


def _assert_health_not_blocked(client, started, release):
    results = {}

    def send_webhook():
        results["webhook"] = post(client, payload(text_event("你好"))).status_code

    def get_health():
        results["health"] = client.get("/health").status_code

    webhook_thread = threading.Thread(target=send_webhook)
    webhook_thread.start()
    try:
        assert started.wait(timeout=5)
        health_thread = threading.Thread(target=get_health)
        begin = time.monotonic()
        health_thread.start()
        health_thread.join(timeout=3)
        # 回覆仍在進行中（release 尚未 set），/health 必須已經回應
        assert results.get("health") == 200
        assert time.monotonic() - begin < 3
    finally:
        release.set()
        webhook_thread.join(timeout=10)
    assert results["webhook"] == 200


# ================================================================ SDK 回覆路徑


def test_sdk_reply_sends_expected_http_request(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200
        reason = "OK"
        # LINE Reply API 成功時的回應格式
        data = b'{"sentMessages": [{"id": "461230966842064897", "quoteToken": "q"}]}'

        def read(self):
            return self.data

        def getheaders(self):
            return {"content-type": "application/json"}

        def getheader(self, name, default=None):
            return self.getheaders().get(name.lower(), default)

    def fake_request(
        self,
        method,
        url,
        query_params=None,
        headers=None,
        body=None,
        post_params=None,
        _preload_content=True,
        _request_timeout=None,
    ):
        captured.update(
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout=_request_timeout,
        )
        return FakeResponse()

    monkeypatch.setattr(RESTClientObject, "request", fake_request)
    make_sdk_reply(TOKEN)("reply-token-1", "收到：你好")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.line.me/v2/bot/message/reply"
    assert captured["headers"]["Authorization"] == f"Bearer {TOKEN}"
    body = captured["body"]
    if isinstance(body, (bytes, str)):
        body = json.loads(body)
    assert body["replyToken"] == "reply-token-1"
    assert body["messages"] == [{"type": "text", "text": "收到：你好"}]
    assert captured["timeout"] == REPLY_TIMEOUT_S


def test_sdk_reply_timeout_reaches_urllib3(monkeypatch):
    # 確認安裝的 SDK 版本真的把 (連線, 讀取) 轉成 urllib3.Timeout，而不是忽略
    captured = {}

    class FakeUrllib3Response:
        status = 200
        reason = "OK"
        data = b'{"sentMessages": [{"id": "1", "quoteToken": "q"}]}'

        def __init__(self):
            self.headers = {"content-type": "application/json"}

    def fake_pool_request(self, method, url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return FakeUrllib3Response()

    monkeypatch.setattr(urllib3.PoolManager, "request", fake_pool_request)
    make_sdk_reply(TOKEN)("reply-token-1", "你好")

    timeout = captured["timeout"]
    assert isinstance(timeout, urllib3.Timeout)
    assert timeout.connect_timeout == REPLY_TIMEOUT_S[0]
    assert timeout.read_timeout == REPLY_TIMEOUT_S[1]


def test_sdk_invalid_signature_error_is_the_v3_class():
    # 舊版 linebot.exceptions 的同名類別接不到 v3 parser 丟出的例外；確認本模組用的是 v3
    from linebot.v3.exceptions import InvalidSignatureError

    assert line_webhook.InvalidSignatureError is InvalidSignatureError


# ================================================================ app_main 掛載


class TinyPool:
    def open(self, wait=True):
        pass

    def close(self):
        pass


def test_production_app_without_line_keys_fails_closed_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="app_main"):
        app = app_main.create_production_app(
            {"YOUBIKE_DATABASE_URL": "postgresql:///youbike"}
        )
    assert "LINE 金鑰未設定" in caplog.text
    body = payload()
    response = TestClient(app).post(
        WEBHOOK_PATH, content=body, headers={"X-Line-Signature": sign(body, "")}
    )
    assert response.status_code == 503


def test_production_app_with_line_keys_verifies_signature():
    app = app_main.create_production_app(
        {
            "YOUBIKE_DATABASE_URL": "postgresql:///youbike",
            "LINE_CHANNEL_SECRET": SECRET,
            "LINE_CHANNEL_ACCESS_TOKEN": TOKEN,
        }
    )
    client = TestClient(app)
    body = payload()  # Verify 請求：沒有事件，不會呼叫 Reply API
    assert post(client, body).status_code == 200
    assert post(client, body, "invalid").status_code == 400


def test_webhook_is_not_in_openapi_schema():
    app = app_main.create_app_with_pool(TinyPool(), line_config=CONFIG)
    assert WEBHOOK_PATH not in app.openapi()["paths"]
    assert "/stations" in app.openapi()["paths"]
