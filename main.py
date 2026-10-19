"""FastAPI 入口：微信小店多店铺客服回调。

路由设计（多店共用一个域名时用 URL 路径区分店铺）：
    GET  /wxstore/{slug}/webhook   -> 首次提交 URL 时的 GET 验签
    POST /wxstore/{slug}/webhook   -> 接收消息推送
    GET  /health                   -> 健康检查

POST 处理必须在 5 秒内返回 success，否则微信重试。本服务策略：
    1. 验签 + 解密
    2. 解析 msg JSON
    3. enqueue 到 worker，立即返回 success
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse

import config
import crypto
import worker
from config import get_shop_by_slug

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动
    config.load_shops()
    worker.init_worker()
    log.info("loaded shops: %s", [s.shop_user_name for s in config.all_shops()])
    yield
    # 关停
    await worker.shutdown_worker()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "shops": len(config.all_shops())}


@app.get("/wxstore/{slug}/webhook")
async def webhook_get(slug: str, request: Request) -> PlainTextResponse:
    """GET 验签：首次在小店后台填写 URL 时由微信发起。"""
    shop = get_shop_by_slug(slug)
    if shop is None:
        return PlainTextResponse("shop not found", status_code=404)

    params = request.query_params
    signature = params.get("signature", "")
    timestamp = params.get("timestamp", "")
    nonce = params.get("nonce", "")
    echostr = params.get("echostr", "")

    if not crypto.verify_get(shop.token, timestamp, nonce, echostr, signature):
        log.warning("GET 验签失败: slug=%s ts=%s nonce=%s", slug, timestamp, nonce)
        return PlainTextResponse("verify failed", status_code=401)

    log.info("GET 验签通过: slug=%s", slug)
    return PlainTextResponse(echostr)


@app.post("/wxstore/{slug}/webhook")
async def webhook_post(slug: str, request: Request) -> PlainTextResponse:
    """POST 消息推送。

    立即 enqueue 并返回 success。5 秒超时硬要求。
    """
    shop = get_shop_by_slug(slug)
    if shop is None:
        return PlainTextResponse("shop not found", status_code=404)

    params = request.query_params
    timestamp = params.get("timestamp", "")
    nonce = params.get("nonce", "")
    openid = params.get("openid", "")
    encrypt_type = params.get("encrypt_type", "")
    msg_signature = params.get("msg_signature", "")

    body_bytes = await request.body()

    # 1. 解析外层 JSON
    try:
        outer = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        log.warning("POST body 非 JSON: slug=%s", slug)
        return PlainTextResponse("success")

    encrypt = (outer.get("Encrypt") or "").strip()
    if not encrypt:
        # 异常：没有 Encrypt 字段，按 success 返回避免重试
        log.warning("POST 缺 Encrypt: slug=%s", slug)
        return PlainTextResponse("success")

    # 2. POST 验签（注意用 msg_signature 而非 signature）
    if not crypto.verify_post(shop.token, timestamp, nonce, encrypt, msg_signature):
        log.warning("POST 验签失败: slug=%s ts=%s", slug, timestamp)
        return PlainTextResponse("success")

    # 3. 解密
    try:
        msg_str, appid = crypto.decrypt(shop.encoding_aes_key, encrypt)
        msg = json.loads(msg_str)
    except Exception:
        log.exception("解密失败 slug=%s", slug)
        return PlainTextResponse("success")

    # 4. 路由校验：解密后的 ToUserName 必须与店铺 shop_user_name 一致
    if msg.get("ToUserName") != shop.shop_user_name:
        log.warning("ToUserName mismatch: slug=%s ToUserName=%s expected=%s",
                    slug, msg.get("ToUserName"), shop.shop_user_name)
        return PlainTextResponse("success")

    # 5. 入队（msg_id 可能缺失）
    event = {
        "shop_user_name": shop.shop_user_name,
        "openid": openid or msg.get("FromUserName", ""),
        "msg_id": str(msg.get("msg_id", "")),
        "msg": msg,
    }
    try:
        worker.enqueue(event)
    except Exception:
        # Durable storage failed: ask WeChat to retry rather than acknowledge a lost message.
        log.error("enqueue failed slug=%s", slug)
        return PlainTextResponse("temporarily unavailable", status_code=503)

    # 微信要求：5 秒内返回 success 或空串，否则重试 10 次
    return PlainTextResponse("success")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
