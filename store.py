"""Redis 存储层：幂等去重 + access_token 中心化刷新 + 转人工状态。

多店共用一个 Redis。键命名空间：
    wxstore:dedup:{shop_user_name}:{msg_id}        # 幂等去重
    wxstore:token:{app_id}                         # access_token 缓存
    wxstore:token:lock:{app_id}                    # token 刷新锁
    wxstore:handoff:{shop_user_name}:{openid}      # 转人工状态（hash）
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Optional

import logging
import redis
log = logging.getLogger("store")

_redis: Optional[redis.Redis] = None
_token_lock = threading.Lock()


def get_redis() -> redis.Redis:
    """获取 Redis 客户端单例。"""
    global _redis
    if _redis is None:
        url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        _redis = redis.Redis.from_url(url, decode_responses=True)
    return _redis


def is_duplicate(shop_user_name: str, msg_id: str, ttl: int = 86400) -> bool:
    """幂等去重。返回 True 表示重复（已处理过），False 表示首次。"""
    key = f"wxstore:dedup:{shop_user_name}:{msg_id}"
    # SET key value NX EX ttl：仅当 key 不存在时设置，并设过期
    ok = get_redis().set(key, "1", nx=True, ex=ttl)
    return not ok  # ok=True 表示新建成功（首次），False 表示已存在（重复）


def get_handoff(shop_user_name: str, openid: str) -> Optional[dict]:
    """读取转人工状态。返回 dict 或 None。"""
    key = f"wxstore:handoff:{shop_user_name}:{openid}"
    raw = get_redis().hgetall(key)
    if not raw:
        return None
    return raw


def set_handoff(shop_user_name: str, openid: str, reason: str, ttl: int = 1800) -> None:
    """标记转人工状态，默认 30 分钟过期。"""
    key = f"wxstore:handoff:{shop_user_name}:{openid}"
    pipe = get_redis().pipeline()
    pipe.hset(key, mapping={
        "reason": reason,
        "ts": str(int(time.time())),
    })
    pipe.expire(key, ttl)
    pipe.execute()


def clear_handoff(shop_user_name: str, openid: str) -> None:
    """清除转人工状态（用户主动结束会话 / 后台人工接管完成后调用）。"""
    key = f"wxstore:handoff:{shop_user_name}:{openid}"
    get_redis().delete(key)


def get_access_token(app_id: str, fetcher: Callable[[], tuple[str, int]]) -> str:
    """中心化获取 access_token，避免多店多进程并发刷新覆盖。

    fetcher: 无参回调，返回 (token_str, expires_in_seconds)。
    缓存提前 5 分钟过期，避免边界期失效。
    """
    key = f"wxstore:token:{app_id}"
    lock_key = f"wxstore:token:lock:{app_id}"
    r = get_redis()

    cached = r.get(key)
    if cached:
        return cached

    # 加分布式锁，只让一个 worker 去刷新
    got_lock = r.set(lock_key, "1", nx=True, ex=30)
    if not got_lock:
        # 其他进程在刷新，最多等 5 秒
        for _ in range(50):
            time.sleep(0.1)
            cached = r.get(key)
            if cached:
                return cached
        # 等不到就报错，由调用方降级
        raise RuntimeError(f"等待 access_token 刷新超时: app_id={app_id}")

    try:
        token, expires_in = fetcher()
        if not token or expires_in <= 0:
            raise RuntimeError(f"access_token 刷新失败: app_id={app_id}")
        # 提前 5 分钟过期
        r.setex(key, max(expires_in - 300, 60), token)
        return token
    finally:
        r.delete(lock_key)


# ---- 微语会话映射：openid -> (thread_uid, topic, visitor_uid) ----
_WEIYU_THREAD_PREFIX = "wxstore:weiyu_thread"
_WEIYU_THREAD_TTL = 2 * 24 * 3600  # 2 天


def set_weiyu_thread(shop_user_name: str, openid: str,
                     thread_uid: str, topic: str, visitor_uid: str) -> None:
    key = f"{_WEIYU_THREAD_PREFIX}:{shop_user_name}:{openid}"
    val = json.dumps({"t": thread_uid, "p": topic, "v": visitor_uid})
    r = get_redis()
    r.setex(key, _WEIYU_THREAD_TTL, val)
    log.info("weiyu thread mapped: shop=%s openid=%s thread=%s",
             shop_user_name, openid, thread_uid)


def get_weiyu_thread(shop_user_name: str, openid: str):
    key = f"{_WEIYU_THREAD_PREFIX}:{shop_user_name}:{openid}"
    r = get_redis()
    raw = r.get(key)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return d["t"], d["p"], d["v"]
    except (json.JSONDecodeError, KeyError):
        return None


def all_weiyu_threads(shop_user_name: str) -> list:
    r = get_redis()
    pattern = f"{_WEIYU_THREAD_PREFIX}:{shop_user_name}:*"
    out = []
    for key in r.scan_iter(match=pattern, count=100):
        raw = r.get(key)
        if not raw:
            continue
        try:
            d = json.loads(raw)
            out.append((d["t"], d["p"], d["v"]))
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def clear_weiyu_thread(shop_user_name: str, openid: str) -> None:
    key = f"{_WEIYU_THREAD_PREFIX}:{shop_user_name}:{openid}"
    r = get_redis()
    r.delete(key)


# ---- 客服回复去重 ----
_AGENT_REPLY_PREFIX = "wxstore:agent_reply_sent"
_AGENT_REPLY_TTL = 2 * 24 * 3600


def is_agent_reply_sent(shop_user_name: str, msg_uid: str) -> bool:
    key = f"{_AGENT_REPLY_PREFIX}:{shop_user_name}:{msg_uid}"
    return bool(get_redis().exists(key))


def mark_agent_reply_sent(shop_user_name: str, msg_uid: str) -> None:
    key = f"{_AGENT_REPLY_PREFIX}:{shop_user_name}:{msg_uid}"
    get_redis().setex(key, _AGENT_REPLY_TTL, "1")


# ---- 人工接管状态：客服在工作台回复后，AI 停止回复 ----
_TAKEOVER_PREFIX = "wxstore:takeover"
_TAKEOVER_TTL = 2 * 24 * 3600  # 2 天


def is_agent_takeover(shop_user_name: str, openid: str) -> bool:
    """该会话是否已被人工客服接管（接管后 AI 不再回复）。"""
    key = f"{_TAKEOVER_PREFIX}:{shop_user_name}:{openid}"
    return bool(get_redis().exists(key))


def set_agent_takeover(shop_user_name: str, openid: str) -> None:
    """标记人工接管。客服在工作台回复后调用。"""
    key = f"{_TAKEOVER_PREFIX}:{shop_user_name}:{openid}"
    get_redis().setex(key, _TAKEOVER_TTL, "1")
    log.info("agent takeover marked: shop=%s openid=%s", shop_user_name, openid)


def clear_agent_takeover(shop_user_name: str, openid: str) -> None:
    """解除人工接管（会话结束 / 客服主动释放后调用）。"""
    key = f"{_TAKEOVER_PREFIX}:{shop_user_name}:{openid}"
    get_redis().delete(key)
