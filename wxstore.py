"""微信小店自研客服 API 客户端。

参考：
- sendmsg: https://developers.weixin.qq.com/doc/store/shop/API/kf/api_sendmsg.html
- cosupload: https://developers.weixin.qq.com/doc/store/shop/API/kf/api_cosupload.html
- stable_token: https://developers.weixin.qq.com/doc/store/shop/API/apimgnt/common/api_getstableaccesstoken

限制：
- sendmsg 在用户发消息后 48 小时内可回复，最多 5 条
- 用户主动结束会话后无法继续发（errcode=10190013）
- 图片/视频/文件需先 cosupload 拿 cos_url 再 send
- 多店各自一份 access_token，靠 app_id 在 store.py 中心化缓存
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

import httpx

import store
from config import ShopConfig

log = logging.getLogger("wxstore")

_API_BASE = "https://api.weixin.qq.com"
_SENDMSG = "/channels/ec/commkf/sendmsg"
_COSUPLOAD = "/channels/ec/commkf/cosupload"
_STABLE_TOKEN = "/cgi-bin/stable_token"

# 失效或风控类错误码：返回 False 表示发送失败，调用方决定降级（如转人工）
_FATAL_ERRCODES = {
    10190008,  # 消息发送条数超限
    10190009,  # 会话超过 48h
    10190013,  # 用户已结束会话
}


class WxStoreError(RuntimeError):
    """微信小店 API 业务错误。"""

    def __init__(self, errcode: int, errmsg: str):
        super().__init__(f"errcode={errcode} errmsg={errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


class WxStoreClient:
    """单店铺客户端。每店铺一个实例。"""

    def __init__(self, shop: ShopConfig, timeout: float = 10.0):
        self.shop = shop
        self._timeout = timeout

    # ---- access_token ----

    def _fetch_token(self) -> tuple[str, int]:
        """从微信拉新 token。"""
        with httpx.Client(timeout=self._timeout) as c:
            r = c.post(
                _API_BASE + _STABLE_TOKEN,
                json={
                    "grant_type": "client_credential",
                    "appid": self.shop.app_id,
                    "secret": self.shop.app_secret,
                },
            )
            r.raise_for_status()
            data = r.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 7200))
        if not token:
            raise WxStoreError(
                data.get("errcode", -1),
                f"stable_token 失败: {data.get('errmsg', '')}",
            )
        return token, expires_in

    def _get_token(self) -> str:
        """中心化缓存取 token。"""
        return store.get_access_token(self.shop.app_id, self._fetch_token)

    # ---- 通用 POST ----

    def _post_sendmsg(self, payload: dict) -> dict:
        """调 sendmsg。返回 {msg_id, errmsg, errcode}。"""
        token = self._get_token()
        url = _API_BASE + _SENDMSG
        with httpx.Client(timeout=self._timeout) as c:
            r = c.post(url, params={"access_token": token}, json=payload)
            r.raise_for_status()
            data = r.json()
        log.info("sendmsg response: %s", data)
        if data.get("errcode", 0) != 0:
            raise WxStoreError(data.get("errcode", -1), data.get("errmsg", ""))
        return data

    # ---- 发送各类消息 ----

    def send_text(self, open_id: str, content: str,
                  request_id: Optional[str] = None) -> dict:
        return self._post_sendmsg({
            "request_id": request_id or str(uuid.uuid4()),
            "open_id": open_id,
            "msg_type": "text",
            "text": {"content": content},
        })

    def send_image(self, open_id: str, cos_url: str,
                  request_id: Optional[str] = None) -> dict:
        return self._post_sendmsg({
            "request_id": request_id or str(uuid.uuid4()),
            "open_id": open_id,
            "msg_type": "image",
            "image": {"cos_url": cos_url},
        })

    def send_file(self, open_id: str, cos_url: str,
                  request_id: Optional[str] = None) -> dict:
        return self._post_sendmsg({
            "request_id": request_id or str(uuid.uuid4()),
            "open_id": open_id,
            "msg_type": "file",
            "file": {"cos_url": cos_url},
        })

    def send_video(self, open_id: str, cos_url: str,
                  request_id: Optional[str] = None) -> dict:
        return self._post_sendmsg({
            "request_id": request_id or str(uuid.uuid4()),
            "open_id": open_id,
            "msg_type": "video",
            "video": {"cos_url": cos_url},
        })

    def send_product(self, open_id: str, product_id: str,
                     request_id: Optional[str] = None) -> dict:
        return self._post_sendmsg({
            "request_id": request_id or str(uuid.uuid4()),
            "open_id": open_id,
            "msg_type": "product_share",
            "product_share": {"product_id": product_id},
        })

    def send_order(self, open_id: str, order_id: str,
                   request_id: Optional[str] = None) -> dict:
        return self._post_sendmsg({
            "request_id": request_id or str(uuid.uuid4()),
            "open_id": open_id,
            "msg_type": "order_share",
            "order_share": {"order_id": order_id},
        })

    # ---- 多媒体上传 ----

    def cosupload(self, open_id: str, msg_type: str, file_bytes: bytes,
                  filename: str) -> str:
        """上传多媒体，返回 cos_url。

        msg_type 仅支持 image / video / file。
        """
        if msg_type not in ("image", "video", "file"):
            raise ValueError(f"cosupload 不支持的 msg_type: {msg_type}")
        token = self._get_token()
        url = _API_BASE + _COSUPLOAD
        files = {"file": (filename, file_bytes)}
        data = {"msg_type": msg_type, "open_id": open_id}
        with httpx.Client(timeout=self._timeout) as c:
            r = c.post(url, params={"access_token": token}, files=files, data=data)
            r.raise_for_status()
            resp = r.json()
        if resp.get("errcode", 0) != 0:
            raise WxStoreError(resp.get("errcode", -1), resp.get("errmsg", ""))
        cos_url = resp.get("cos_url")
        if not cos_url:
            raise WxStoreError(-1, f"cosupload 未返回 cos_url: {resp}")
        return cos_url

    # ---- 异常分类辅助 ----

    @staticmethod
    def is_session_closed(err: WxStoreError) -> bool:
        """用户已结束会话（不可再发）。"""
        return err.errcode == 10190013

    @staticmethod
    def is_rate_limited(err: WxStoreError) -> bool:
        """发送条数超限或会话超 48h。"""
        return err.errcode in (10190008, 10190009)
