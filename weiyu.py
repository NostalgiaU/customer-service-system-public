"""微语（bytedesk）对话客户端。

将用户消息送给微语（多租户，每店铺一个租户），拿到 AI 回复，并解析是否需要转人工。

本机运行版本已通过 POST /api/v1/chat/completions 验证可返回 AI 文本。
该接口使用微语服务端配置的 ChatModel；模型、知识库及历史上下文需分别核对。
"""
from __future__ import annotations

import logging
from datetime import timezone
from typing import Optional

import httpx

from config import ShopConfig

log = logging.getLogger("weiyu")

# 微语 RobotController 暴露的 OpenAI 兼容对话接口。
_CHAT_PATH = "/api/v1/chat/completions"

# 转人工触发信号：微语回复中包含这些标记之一即视为请求转人工
_HUMAN_KEYWORDS = ("[转人工]", "[人工客服]", "[需要人工]", "[NEED_HUMAN]")


class WeiyuError(RuntimeError):
    pass


class WeiyuClient:
    """单店铺对应的微语租户客户端。"""

    def __init__(self, shop: ShopConfig, timeout: float = 30.0):
        self.shop = shop
        self._timeout = timeout

    def chat(self, openid: str, message: str) -> dict:
        """与微语对话，返回 {"reply": str, "need_human": bool}。

        openid 用作会话隔离的 user 标识；message 是规整后的纯文本。
        """
        url = self.shop.weiyu_base_url.rstrip("/") + _CHAT_PATH
        headers = {
            "Authorization": f"Bearer {self.shop.weiyu_tenant_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-4o",  # 占位，由微语侧实际配置决定
            "stream": False,
            "user": openid,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": message},
            ],
        }
        try:
            with httpx.Client(timeout=self._timeout) as c:
                r = c.post(url, headers=headers, json=payload)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as e:
            log.warning("weiyu chat http error: %s", e)
            raise WeiyuError(f"微语调用失败: {e}") from e

        reply = self._extract_reply(data)
        need_human = any(k in reply for k in _HUMAN_KEYWORDS)
        if need_human:
            # 去掉转人工标记再发给用户
            for k in _HUMAN_KEYWORDS:
                reply = reply.replace(k, "")
            reply = reply.strip()
        return {"reply": reply, "need_human": need_human}

    # ---- 内部辅助 ----

    def _system_prompt(self) -> str:
        """每店铺的系统提示。后续可从微语租户配置拉取，这里先用默认。"""
        return (
            "你是微信小店的人工客服助手。规则：\n"
            "1) 用中文回答，简洁直接。\n"
            "2) 仅基于店铺商品和常见问题回答，超出范围请回复[转人工]标记。\n"
            "3) 涉及订单状态、退款、投诉、纠纷，一律回复[转人工]。\n"
            "4) 不要捏造价格、库存、物流。\n"
            "5) 如用户明确要求找人工客服，回复[转人工]。\n"
        )

    def _extract_reply(self, data: dict) -> str:
        """从 OpenAI 兼容返回体抽取回复文本。"""
        try:
            choices = data.get("choices") or []
            if not choices:
                # 兼容微语原生格式 {content: "..."} 或 {data: {content}}
                for k in ("content", "reply", "answer"):
                    if k in data and isinstance(data[k], str):
                        return data[k]
                inner = data.get("data")
                if isinstance(inner, dict):
                    for k in ("content", "reply", "answer"):
                        if k in inner and isinstance(inner[k], str):
                            return inner[k]
                raise WeiyuError(f"无法解析微语返回: {data}")
            msg = choices[0].get("message") or {}
            content = msg.get("content") or ""
            return content.strip()
        except WeiyuError:
            raise
        except Exception as e:
            raise WeiyuError(f"解析微语返回失败: {e}; raw={data}") from e


SUPPORTED_INBOUND_TYPES = frozenset({
    "text", "voice", "image", "file", "video", "product_share", "order_share", "reference", "evaluate_result"
})


def is_customer_message(msg: dict) -> bool:
    return msg.get("msg_type") in SUPPORTED_INBOUND_TYPES


def normalize_incoming(msg: dict) -> str:
    """把 8 种 msg_type 的入站消息转成 LLM 可读的纯文本。

    入站 msg JSON 结构示例（关键字段）：
        {
          "FromUserName": "oXXXX", "ToUserName": "gh_xxx",
          "msg_type": "text|image|file|video|voice|product_share|order_share|reference",
          "text":          {"content": "..."},
          "image":         {"cos_url": "..."},
          "file":          {"cos_url": "..."},
          "video":         {"play_length": 24, "cos_url": "...", "ld_img_url": "..."},
          "voice":         {"translated_msg": "...", "play_length": 2287, "translation_succ": true},
          "product_share": {"product_id": "..."},
          "order_share":   {"order_id": "..."},
          "reference":     {"content": "...", "ref_msg": {...}},
          "evaluate_result":{"value": 1, "msg_id": 456, "evaluate_time": ...}
        }
    """
    mtype = msg.get("msg_type", "")
    if mtype == "text":
        return f"用户消息：{msg.get('text', {}).get('content', '')}"
    if mtype == "voice":
        voice = msg.get("voice", {}) or {}
        if voice.get("translation_succ"):
            return f"用户语音（已转文字）：{voice.get('translated_msg', '')}"
        return "用户发来一段语音（无转写文本）"
    if mtype == "image":
        return "用户发来一张图片（图片内容需客服查看）"
    if mtype == "file":
        return "用户发来一个文件（文件内容需客服查看）"
    if mtype == "video":
        return "用户发来一段视频（视频内容需客服查看）"
    if mtype == "product_share":
        pid = msg.get("product_share", {}).get("product_id", "")
        return f"用户分享了商品卡片，product_id={pid}"
    if mtype == "order_share":
        oid = msg.get("order_share", {}).get("order_id", "")
        return f"用户分享了订单卡片，order_id={oid}"
    if mtype == "reference":
        ref = msg.get("reference", {}) or {}
        content = ref.get("content", "")
        ref_msg = ref.get("ref_msg", {}) or {}
        return (
            f"用户引用了一条历史消息并回复。引用内容："
            f"{ref_msg.get('content', '')}（来自 {ref_msg.get('from_name', '客服')}），"
            f"用户补充：{content}"
        )
    if mtype == "evaluate_result":
        ev = msg.get("evaluate_result", {}) or {}
        value = ev.get("value")
        score_text = {1: "不够好", 2: "一般", 3: "挺好"}.get(value, "未知")
        return f"用户对本次服务评价：{score_text}"
    if msg.get("Event") == "channels_ec_aftersale_update" or msg.get("finder_shop_aftersale_status_update"):
        update = msg.get("finder_shop_aftersale_status_update") or {}
        after_sale_id = update.get("after_sale_order_id") or update.get("afterSaleOrderId")
        order_id = update.get("order_id") or update.get("orderId")
        if not after_sale_id and not order_id:
            return (
                "微信小店售后状态更新（仅内部事件）：本次回调未提供售后单号或订单号，"
                "请到微信小店后台按用户和时间核对。"
            )
        return (
            "微信小店售后状态更新（仅内部事件）："
            f"售后单号={after_sale_id or '未提供'}，"
            f"订单号={order_id or '未提供'}，"
            f"状态={update.get('status', '未知')}"
        )
    if msg.get("MsgType") == "event" or msg.get("Event"):
        return f"微信小店系统事件（仅内部事件）：{msg.get('Event') or '未知事件'}"
    return f"用户发来一条类型未识别的消息（msg_type={mtype}）"



# =========================================================================
# 微语访客/会话/消息 API（用于把微信消息同步到微语工作台，供人工客服查看）
# =========================================================================

import json as _json
import time as _time

import pymysql


class WeiyuVisitorClient:
    """调用微语匿名访客 API，把微信用户消息同步到微语客服工作台。"""

    def __init__(self, shop, timeout: float = 15.0):
        self.shop = shop
        self._base = shop.weiyu_base_url.rstrip("/")
        self._timeout = timeout
        self._org_uid = shop.weiyu_org_uid
        self._agent_uid = shop.weiyu_agent_uid

    def resolve_agent_user_uid(self) -> str:
        """返回用于 MQTT/主题订阅的登录用户 UID。

        微语的客服实体 UID（agent.uid）和登录用户 UID（agent.userUid）不同。
        订阅写给后者，否则消息可能只落库，工作台要刷新才看得到。
        """
        configured = getattr(self.shop, "weiyu_agent_user_uid", "")
        if configured:
            return configured
        url = f"{self._base}/visitor/api/v1/agent/query/uid"
        with httpx.Client(timeout=self._timeout) as cli:
            r = cli.get(url, params={"uid": self._agent_uid, "orgUid": self._org_uid})
            r.raise_for_status()
            data = r.json()
        if data.get("code") != 200 or not data.get("data"):
            raise WeiyuError(f"查询微语登录用户失败: {data}")
        agent = data["data"]
        user_uid = agent.get("userUid")
        if not user_uid:
            raise WeiyuError("客服接口未返回 userUid，请配置 weiyu_agent_user_uid")
        return user_uid

    def ensure_realtime_subscription(self, topic: str) -> None:
        """通过微语接口订阅实时消息，保留服务端订阅事件及缓存更新。

        subscribe 使用当前 token 身份，先检查它就是目标客服的登录用户，
        防止将会话订阅给错误账户。已有会话也要调用，以修复已过期的订阅。
        """
        expected_uid = self.resolve_agent_user_uid()
        headers = {"Authorization": f"Bearer {self.shop.weiyu_tenant_token}"}
        with httpx.Client(timeout=self._timeout, headers=headers) as cli:
            response = cli.get(f"{self._base}/api/v1/user/profile")
            response.raise_for_status()
            profile = response.json()
            if profile.get("code") != 200 or (profile.get("data") or {}).get("uid") != expected_uid:
                raise WeiyuError("微语 token 登录用户与目标客服 userUid 不一致，无法订阅会话")
            for current_topic in (topic, f"{topic}_internal"):
                response = cli.post(f"{self._base}/api/v1/topic/subscription/subscribe",
                                    json={"topic": current_topic})
                response.raise_for_status()
                if response.json().get("code") != 200:
                    raise WeiyuError("微语实时订阅失败")

    def create_visitor(self, openid: str, nickname: str = "微信用户") -> str:
        """创建或获取微语访客，返回 visitor_uid。"""
        url = f"{self._base}/visitor/api/v1/init"
        payload = {
            "orgUid": self._org_uid,
            "visitorUid": openid,
            "nickname": nickname,
            "channel": "WECHAT",
        }
        with httpx.Client(timeout=self._timeout) as cli:
            r = cli.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        if data.get("code") != 200 or not data.get("data"):
            raise WeiyuError(f"创建访客失败: {data}")
        visitor = data["data"]
        # 微语自动生成的 uid 才是内部 visitor_uid
        return visitor.get("uid") or visitor.get("visitorUid") or openid

    def create_agent_thread(self, visitor_uid: str) -> tuple[str, str]:
        """请求人工客服会话，返回 (thread_uid, topic)。"""
        url = f"{self._base}/visitor/api/v1/thread"
        payload = {
            "orgUid": self._org_uid,
            "visitorUid": visitor_uid,
            "nickname": "微信用户",
            "channel": "WECHAT",
            "type": "AGENT",
            "sid": self._agent_uid,
        }
        with httpx.Client(timeout=self._timeout) as cli:
            r = cli.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        if data.get("code") != 200 or not data.get("data"):
            raise WeiyuError(f"创建会话失败: {data}")
        thread = data["data"].get("thread") or {}
        thread_uid = thread.get("uid")
        topic = thread.get("topic")
        if not thread_uid or not topic:
            raise WeiyuError(f"会话返回缺少 uid/topic: {data}")
        return thread_uid, topic

    def send_visitor_message(self, thread_uid: str, topic: str,
                             visitor_uid: str, content: str, *, message_type: str = 'TEXT',
                             message_uid: str = '', extra_fields: Optional[dict] = None) -> str:
        """把微信用户消息发到微语会话（客服在工作台可见）。"""
        # 1. 申请消息元信息（拿 messageUid，否则微语不持久化）
        msg_uid = message_uid or self.message_uid(thread_uid)

        # 2. 发送消息
        send_url = f"{self._base}/visitor/api/v1/message/send"
        # 必须带 orgUid，否则微语后端持久化时 org_uid=NULL，
        # 客服工作台按组织过滤会看不到访客消息
        # extra.orgUid 才是微语后端持久化时读取 org_uid 的来源
        extra = _json.dumps({"orgUid": self._org_uid, "source": "wxstore_visitor", **(extra_fields or {})}, ensure_ascii=False)
        msg_proto = {
            "uid": msg_uid,
            "type": message_type,
            "content": content,
            "status": "SUCCESS",
            "channel": "WECHAT",
            "extra": extra,
            "orgUid": self._org_uid,
            "thread": {"uid": thread_uid, "topic": topic, "type": "AGENT", "orgUid": self._org_uid,
                       "user": {"uid": visitor_uid, "nickname": "微信用户", "type": "VISITOR"}},
            "user": {"uid": visitor_uid, "nickname": "微信用户", "type": "VISITOR", "orgUid": self._org_uid},
        }
        with httpx.Client(timeout=self._timeout) as cli:
            r = cli.post(send_url, json={"json": _json.dumps(msg_proto, ensure_ascii=False)})
            r.raise_for_status()
            data = r.json()
        if data.get("code") != 200:
            raise WeiyuError(f"发送访客消息失败: {data}")
        log.debug("visitor message synced to weiyu: thread=%s", thread_uid)
        return msg_uid

    def message_uid(self, thread_uid: str) -> str:
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(f'{self._base}/visitor/api/v1/thread/message/meta', json={'uid': thread_uid})
            response.raise_for_status()
            result = response.json()
        uid = (result.get('data') or {}).get('messageUid')
        if result.get('code') != 200 or not uid:
            raise WeiyuError('申请消息标识失败')
        return uid

    def archive_image(self, cos_url: str) -> dict:
        from media import download_image
        image = download_image(cos_url, from_wechat=True)
        with httpx.Client(timeout=30) as client:
            response = client.post(f'{self._base}/api/v1/upload/file',
                headers={'Authorization': f'Bearer {self.shop.weiyu_tenant_token}'},
                files={'file': (image.filename, image.data, image.mime)},
                data={'fileName': image.filename, 'fileType': image.mime, 'kbType': 'CHAT', 'orgUid': self._org_uid})
            response.raise_for_status()
            result = response.json()
        url = (result.get('data') or {}).get('fileUrl')
        if result.get('code') != 200 or not url:
            raise WeiyuError('图片转存微语失败')
        return {'url': url, 'thumbnail': url, 'mimeType': image.mime,
                'filename': image.filename, 'size': str(len(image.data))}

    def send_visitor_image(self, thread_uid: str, topic: str, visitor_uid: str,
                           cos_url: str, wx_message_id: str = '', *,
                           archived: Optional[dict] = None, message_uid: str = '') -> str:
        image = archived or self.archive_image(cos_url)
        return self.send_visitor_message(thread_uid, topic, visitor_uid, _json.dumps(image),
            message_type='IMAGE', message_uid=message_uid, extra_fields={'wxMessageId': wx_message_id})

    def send_internal_note(self, thread_uid: str, topic: str, content: str, message_uid: str) -> None:
        """Staff-only delivery receipt, excluded from the WeChat forwarding poller."""
        internal_topic = topic if topic.endswith('_internal') else topic + '_internal'
        proto = {
            'uid': message_uid, 'type': 'TEXT', 'content': _json.dumps({'text': content}, ensure_ascii=False),
            'status': 'SUCCESS', 'channel': 'WECHAT', 'orgUid': self._org_uid,
            'extra': _json.dumps({'orgUid': self._org_uid, 'source': 'wxstore_delivery', 'visibility': 'internal'}),
            'thread': {'uid': thread_uid, 'topic': internal_topic, 'type': 'AGENT', 'orgUid': self._org_uid},
            'user': {'uid': self._agent_uid, 'nickname': '微信投递状态', 'type': 'AGENT', 'agent': True},
        }
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(f'{self._base}/visitor/api/v1/message/send', json={'json': _json.dumps(proto)})
            response.raise_for_status()
            if response.json().get('code') != 200:
                raise WeiyuError('投递状态备注写入失败')

    def send_agent_message(self, thread_uid: str, topic: str, content: str, visitor_uid: str = "") -> None:
        """把 AI 回复以客服身份发到微语会话（客服在工作台可见 AI 说了什么）。

        复用 visitor send 接口，但 user 设为客服身份（agent=true, type=AGENT）。
        """
        # 1. 申请消息元信息
        meta_url = f"{self._base}/visitor/api/v1/thread/message/meta"
        with httpx.Client(timeout=self._timeout) as cli:
            r = cli.post(meta_url, json={"uid": thread_uid})
            r.raise_for_status()
            meta = r.json()
        if meta.get("code") != 200 or not meta.get("data"):
            raise WeiyuError(f"申请消息元信息失败: {meta}")
        msg_uid = meta["data"].get("messageUid")

        # 2. 以客服身份发送（agent=true, type=AGENT, uid=客服 uid）
        send_url = f"{self._base}/visitor/api/v1/message/send"
        # extra.orgUid 才是微语后端持久化时读取 org_uid 的来源
        # source 是桥接层内部标识。人工轮询据此排除 AI 自己写入的消息，
        # 避免 AI 回复再次被当作人工回复转发给微信用户。
        extra = _json.dumps({"orgUid": self._org_uid, "source": "wxstore_ai"}, ensure_ascii=False)
        msg_proto = {
            "uid": msg_uid,
            "type": "TEXT",
            "content": _json.dumps({"text": content}, ensure_ascii=False),
            "status": "SUCCESS",
            "channel": "WECHAT",
            "extra": extra,
            "orgUid": self._org_uid,
            "thread": {"uid": thread_uid, "topic": topic, "type": "AGENT", "orgUid": self._org_uid,
                       "user": {"uid": visitor_uid, "nickname": "微信用户", "type": "VISITOR"}},
            "user": {"uid": self._agent_uid, "nickname": "AI客服", "type": "AGENT",
                     "agent": True, "orgUid": self._org_uid},
        }
        with httpx.Client(timeout=self._timeout) as cli:
            r = cli.post(send_url, json={"json": _json.dumps(msg_proto, ensure_ascii=False)})
            r.raise_for_status()
            data = r.json()
        if data.get("code") != 200:
            raise WeiyuError(f"发送AI客服消息失败: {data}")
        log.debug("agent(ai) message synced to weiyu: thread=%s", thread_uid)


# ---- 为客服订阅会话 topic（否则工作台收不到实时消息推送）----

def subscribe_topic_for_agent(mysql_cfg, topic: str, agent_uid: str = "df_super_uid") -> None:
    """往 bytedesk_core_topic_subscription 插入订阅记录，让客服能收到该会话的实时消息推送。

    微语工作台前端通过 MQTT 接收消息，后端查询 topic_subscription 表找到订阅者。
    桥接层创建的会话没有经过工作台"接受"流程，客服默认未订阅，需要手动插入。
    """
    import uuid as _uuid
    try:
        conn = pymysql.connect(
            host=mysql_cfg.host, port=mysql_cfg.port,
            user=mysql_cfg.user, password=mysql_cfg.password,
            database=mysql_cfg.database, charset="utf8mb4",
        )
        with conn.cursor() as cur:
            # 已存在则跳过
            cur.execute(
                "SELECT id FROM bytedesk_core_topic_subscription "
                "WHERE topic=%s AND user_uid=%s AND is_deleted=0",
                (topic, agent_uid))
            if cur.fetchone():
                return
            uid = str(int(_time.time() * 1000)) + str(_uuid.uuid4().int)[:6]
            cur.execute(
                "INSERT INTO bytedesk_core_topic_subscription "
                "(uuid, user_uid, topic, type, level_type, platform_type, is_deleted, version, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'CHAT', 'ORGANIZATION', 'BYTEDESK', 0, 0, NOW(), NOW())",
                (uid, agent_uid, topic))
            conn.commit()
        conn.close()
        log.debug("subscribed topic for agent: topic=%s agent=%s", topic, agent_uid)
    except Exception as e:
        log.warning("subscribe topic failed: %s", e)


# ---- 轮询微语数据库，获取客服回复 ----

def poll_agent_replies(mysql_cfg, thread_uids: list[str],
                       visitor_uids: set[str],
                       since_ts: float) -> list[dict]:
    """查询微语 bytedesk_core_message 表，返回客服新回复。

    筛选条件：
    - thread 属于 thread_uids（已转人工的会话）
    - message_type = TEXT
    - user_uid 不在 visitor_uids（排除访客自己发的，只取客服发的）
    - created_at > since_ts（增量）

    返回: [{msg_uid, content, thread_uid, created_at}]
    """
    if not thread_uids:
        return []
    conn = pymysql.connect(
        host=mysql_cfg.host, port=mysql_cfg.port,
        user=mysql_cfg.user, password=mysql_cfg.password,
        database=mysql_cfg.database, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        # 微语以 UTC 写入 created_at（DATETIME）。MySQL 主机可能使用北京时间，
        # UNIX_TIMESTAMP 必须在 UTC 会话中计算，否则新回复会被偏移八小时并漏选。
        init_command="SET time_zone = '+00:00'",
    )
    try:
        with conn.cursor() as cur:
            # message 表 thread_id 是外键到 bytedesk_core_thread.id（不是 visitor_thread）
            # visitor_thread 表在离线留言模式下是空的，必须查 core_thread
            placeholders = ",".join(["%s"] * len(thread_uids))
            cur.execute(
                f"SELECT uuid, id FROM bytedesk_core_thread WHERE uuid IN ({placeholders})",
                thread_uids,
            )
            thread_map = {row["uuid"]: row["id"] for row in cur.fetchall()}
            if not thread_map:
                return []
            thread_ids = list(thread_map.values())
            id_ph = ",".join(["%s"] * len(thread_ids))
            # 查客服回复：TEXT 类型且不是访客发的
            visitor_ph = ",".join(["%s"] * len(visitor_uids)) if visitor_uids else "''"
            sql = (
                f"SELECT uuid, content, extra, message_user, user_uid, thread_id, created_at, message_type, status, is_deleted "
                f"FROM bytedesk_core_message "
                f"WHERE thread_id IN ({id_ph}) "
                f"AND is_deleted = 0 "
                f"AND message_type IN ('TEXT', 'IMAGE') "
                f"AND user_uid NOT IN ({visitor_ph}) "
                f"AND UNIX_TIMESTAMP(created_at) > %s "
                f"ORDER BY created_at ASC"
            )
            params = thread_ids + list(visitor_uids) + [since_ts]
            cur.execute(sql, params)
            rows = cur.fetchall()
        out = []
        for row in rows:
            if not public_agent_reply(row):
                continue
            # thread_id 反查 uuid
            thread_uid = None
            for tu, tid in thread_map.items():
                if tid == row["thread_id"]:
                    thread_uid = tu
                    break
            # content 是 JSON 字符串 {"text":"..."}，需解析提取纯文本
            raw = row["content"] or ""
            text = raw
            image_url = ""
            try:
                obj = _json.loads(raw)
                if isinstance(obj, dict):
                    text = obj.get("text") or obj.get("content") or raw
                    image_url = obj.get("url") or ""
            except Exception:
                pass
            out.append({
                "msg_uid": row["uuid"],
                "content": text if row.get("message_type") != "IMAGE" else (image_url or raw),
                "message_type": row.get("message_type", "TEXT"),
                "thread_uid": thread_uid,
                "created_at": row["created_at"].replace(tzinfo=timezone.utc).timestamp() if row["created_at"] else 0,
            })
        return out
    finally:
        conn.close()


def public_agent_reply(row: dict) -> bool:
    deleted = row.get('is_deleted', 0)
    if isinstance(deleted, (bytes, bytearray)):
        deleted = int.from_bytes(deleted, 'big')
    if deleted or str(row.get('status', '')).upper() in ('RECALL', 'RECALLED', 'DELETED'):
        return False
    if row.get('message_type', 'TEXT') not in ('TEXT', 'IMAGE'):
        return False
    try:
        sender = _json.loads(row.get('message_user') or '{}')
        extra = _json.loads(row.get('extra') or '{}')
    except (TypeError, ValueError):
        return False
    if not isinstance(sender, dict) or not isinstance(extra, dict):
        return False
    return (sender.get('type') == 'AGENT' and not sender.get('robot') and not sender.get('system')
        and not str(extra.get('source', '')).startswith('wxstore_')
        and extra.get('visibility', 'public') == 'public' and sender.get('nickname') != 'AI客服')


def current_agent_reply(mysql_cfg, message_uid: str, thread_uid: str, org_uid: str):
    """Read current state by exact message/thread/org, including deleted records.

    Missing/ineligible records return None. Database errors propagate, so an outage
    must never be mistaken for a successful cancellation.
    """
    conn = pymysql.connect(host=mysql_cfg.host, port=mysql_cfg.port,
        user=mysql_cfg.user, password=mysql_cfg.password, database=mysql_cfg.database,
        charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
        init_command="SET time_zone = '+00:00'", connect_timeout=5, read_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT m.* FROM bytedesk_core_message m
                JOIN bytedesk_core_thread t ON m.thread_id=t.id
                WHERE m.uuid=%s AND t.uuid=%s AND m.org_uid=%s""", (message_uid, thread_uid, org_uid))
            row = cur.fetchone()
        if not row or not public_agent_reply(row):
            return None
        raw = row.get('content') or ''
        try:
            obj = _json.loads(raw)
            if isinstance(obj, dict):
                raw = obj.get('url', '') if row['message_type'] == 'IMAGE' else (obj.get('text') or obj.get('content') or raw)
        except (TypeError, ValueError):
            pass
        return {'content': raw, 'message_type': row['message_type']}
    finally:
        conn.close()
