"""异步队列消费 worker。

职责：
1. 接收 main.py 入队的事件，立即返回 success 由 main 完成
2. 幂等检查（msg_id 去重）
3. 调微语对话
4. 命中关键词或 need_human=true 或微语异常 -> 标记转人工并回提示
5. 否则调 send_text 回复
6. 任何环节异常都尝试发兜底文案，失败则兜底转人工
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import time as _time
from typing import Optional

from delivery import DeliveryJournal
from conversation import Conversations
import delivery_service

import store
from config import ShopConfig, get_shop_by_to_user
from weiyu import WeiyuClient, WeiyuVisitorClient, WeiyuError, normalize_incoming, poll_agent_replies, is_customer_message
from config import load_weiyu_mysql
from wxstore import WxStoreClient, WxStoreError

log = logging.getLogger("worker")

# 全局 worker 单例
_queue: "asyncio.Queue[dict] | None" = None
_worker_task: Optional[asyncio.Task] = None
_poll_task: Optional[asyncio.Task] = None
_delivery_task: Optional[asyncio.Task] = None
_journal: Optional[DeliveryJournal] = None


def journal() -> DeliveryJournal:
    global _journal
    if _journal is None:
        _journal = DeliveryJournal()
    return _journal


def enqueue(event: dict) -> None:
    """main.py 入口调用：把事件塞进队列。"""
    global _queue
    if _queue is None:
        raise RuntimeError("worker 未初始化，请先调用 init_worker")
    import hashlib
    # Commit before callback acknowledgement. Queue is only a wake-up hint;
    # durable jobs survive queue saturation, exceptions and process restarts.
    source_id = event.get('msg_id') or hashlib.sha256(_json.dumps(event, sort_keys=True).encode()).hexdigest()
    journal().enqueue('incoming', event['shop_user_name'], source_id,
        {'openid': event['openid'], 'event': event})
    if not _queue.full():
        _queue.put_nowait(True)


def init_worker() -> None:
    """FastAPI startup 时调用。创建队列并启动后台 worker 任务。"""
    global _queue, _worker_task, _poll_task, _delivery_task
    journal()
    if _queue is None:
        _queue = asyncio.Queue(maxsize=10000)
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_run())
    if _poll_task is None or _poll_task.done():
        _poll_task = asyncio.create_task(_poll_agent_replies_loop())
    if _delivery_task is None or _delivery_task.done():
        _delivery_task = asyncio.create_task(_delivery_loop())


async def shutdown_worker() -> None:
    """FastAPI shutdown 时调用。"""
    global _worker_task, _poll_task, _delivery_task, _queue
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    if _poll_task is not None:
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
        _poll_task = None
    if _delivery_task is not None:
        _delivery_task.cancel()
        try:
            await _delivery_task
        except asyncio.CancelledError:
            pass
        _delivery_task = None
    _queue = None


async def _run() -> None:
    """Recover incoming messages in customer order, without losing callback acknowledgements."""
    while True:
        try:
            jobs = journal().due(kind='incoming')
            if not jobs:
                try:
                    await asyncio.wait_for(_queue.get(), timeout=1)
                    _queue.task_done()
                except asyncio.TimeoutError:
                    pass
                continue
            for job in jobs:
                if not journal().claim(job['key']):
                    continue
                try:
                    await _process(job['payload']['event'], job['key'])
                    journal().update(job['key'], state='sent', error='')
                except Exception as error:
                    _delivery_failure(journal().get(job['key']), error)
        except asyncio.CancelledError:
            break
        except Exception as error:
            log.error('incoming delivery loop failed: %s', type(error).__name__)
            await asyncio.sleep(3)


async def _process(event: dict, delivery_key: str = "") -> None:
    """处理单条事件。

    event 结构（由 main.py 装配）：
        {
          "shop_user_name": str,
          "openid": str,
          "msg_id": str,
          "msg": dict,        # 解密后的明文 msg JSON
        }
    """
    shop_user_name = event["shop_user_name"]
    openid = event["openid"]
    msg_id = str(event.get("msg_id", ""))
    msg = event["msg"]

    shop = get_shop_by_to_user(shop_user_name)
    if shop is None:
        log.error("未找到店铺配置: %s", shop_user_name)
        return

    # 1. 幂等去重
    if not delivery_key and msg_id and store.is_duplicate(shop_user_name, msg_id):
        log.info("duplicate msg_id skipped: shop=%s msg_id=%s",
                 shop_user_name, msg_id)
        return

    # 3. 规整入站消息为 LLM 文本
    user_text = normalize_incoming(msg)
    log.info("incoming: shop=%s openid=%s msg_id=%s type=%s",
             shop_user_name, openid, msg_id, msg.get("msg_type"))

    # 4. 所有用户消息都同步到微语工作台（客服可见）
    if delivery_key:
        job = journal().get(delivery_key)
        if not job['payload'].get('mirrored'):
            p = job['payload']
            mapping = (p['thread_uid'], p['topic'], p['visitor_uid']) if p.get('thread_uid') else await _ensure_thread(shop, openid)
            job['payload'].update(thread_uid=mapping[0], topic=mapping[1], visitor_uid=mapping[2])
            journal().update(delivery_key, payload=job['payload'])
            await asyncio.to_thread(delivery_service.mirror_incoming, journal(), job, shop, mapping)
            if not is_customer_message(msg):
                return
    elif not is_customer_message(msg):
        mapping = await _ensure_thread(shop, openid)
        visitor = WeiyuVisitorClient(shop)
        await asyncio.to_thread(visitor.send_internal_note, mapping[0], mapping[1], normalize_incoming(msg),
                                 visitor.message_uid(mapping[0]))
        return
    elif msg.get("msg_type") == "image":
        await _sync_image_to_weiyu(shop, openid, msg, msg_id)
    else:
        await _sync_to_weiyu(shop, openid, user_text)

    # Mirror first, then defer without extending the first unanswered timestamp.
    if delivery_key:
        if Conversations(journal()).defer(delivery_key,
                legacy_human=store.is_agent_takeover(shop_user_name, openid)):
            return
    elif store.is_agent_takeover(shop_user_name, openid):
        return

    # 6. 调微语 AI 回复（同步 HTTP，放在 asyncio.to_thread 避免阻塞事件循环）
    weiyu = WeiyuClient(shop)
    try:
        result = await asyncio.to_thread(weiyu.chat, openid, user_text)
    except WeiyuError as e:
        log.warning("weiyu failed: %s", e)
        await _send_ai_reply(shop, openid, shop.default_welcome, verify_human=bool(delivery_key))
        return
    except Exception:
        log.exception("weiyu unexpected error")
        await _send_ai_reply(shop, openid, shop.default_welcome, verify_human=bool(delivery_key))
        return

    # 7. AI 回复：同步到微语 + 发送到微信
    reply_text = (result.get("reply") or "").strip()
    if not reply_text:
        log.warning("empty reply from weiyu, skip send")
        return
    await _send_ai_reply(shop, openid, reply_text, verify_human=bool(delivery_key))


# ---- 内部辅助 ----

def _hit_human_keyword(shop: ShopConfig, text: str) -> bool:
    for kw in shop.keywords_to_human:
        if kw and kw in text:
            return True
    return False


async def _send_ai_reply(shop: ShopConfig, openid: str, reply_text: str, *, verify_human: bool = False) -> None:
    """AI 回复：同步到微语工作台（客服身份）+ 发送到微信用户。"""
    if verify_human:
        await _collect_shop_replies(shop)
    # AI 生成期间轮询器可能已经检测到人工回复；不能只在生成前检查。
    if store.is_agent_takeover(shop.shop_user_name, openid):
        log.info("cancel AI reply after human takeover: shop=%s", shop.shop_user_name)
        return
    # 同步 AI 回复到微语（以客服身份，客服在工作台可见 AI 说了什么）
    await _sync_agent_msg_to_weiyu(shop, openid, reply_text)
    if verify_human:
        await _collect_shop_replies(shop)
    # 微语同步也会让出执行权，发送前再次检查人工接管状态。
    if store.is_agent_takeover(shop.shop_user_name, openid):
        log.info("cancel AI send after human takeover: shop=%s", shop.shop_user_name)
        return
    # 发送到微信
    wxstore = WxStoreClient(shop)
    try:
        await asyncio.to_thread(wxstore.send_text, openid, reply_text)
    except WxStoreError as e:
        log.error("send ai reply failed: %s", e)
        if WxStoreClient.is_session_closed(e) or WxStoreClient.is_rate_limited(e):
            log.warning("session closed or rate limited, give up: %s", e)
    except Exception:
        log.exception("send ai reply unexpected error")


async def _sync_agent_msg_to_weiyu(shop: ShopConfig, openid: str, content: str) -> None:
    """把 AI 回复以客服身份同步到微语工作台。"""
    mapped = store.get_weiyu_thread(shop.shop_user_name, openid)
    if mapped is None:
        log.warning("no weiyu thread mapped, skip sync agent msg: openid=%s", openid)
        return
    thread_uid, topic, _visitor_uid = mapped
    visitor = WeiyuVisitorClient(shop)
    try:
        await asyncio.to_thread(visitor.send_agent_message, thread_uid, topic, content, _visitor_uid)
    except Exception:
        log.warning("sync agent msg to weiyu failed")


async def _sync_to_weiyu(shop: ShopConfig, openid: str, content: str) -> None:
    """把微信用户消息同步到微语工作台（已转人工的会话）。"""
    mapped = store.get_weiyu_thread(shop.shop_user_name, openid)
    visitor = WeiyuVisitorClient(shop)
    try:
        if mapped is None:
            # 首次同步：创建访客 + 会话（昵称用 openid 前 8 位，方便客服区分用户）
            nickname = f"微信用户_{openid[:8]}"
            visitor_uid = await asyncio.to_thread(visitor.create_visitor, openid, nickname)
            thread_uid, topic = await asyncio.to_thread(visitor.create_agent_thread, visitor_uid)
            store.set_weiyu_thread(shop.shop_user_name, openid, thread_uid, topic, visitor_uid)
        else:
            thread_uid, topic, visitor_uid = mapped
        # 新旧会话均通过 API 修复订阅；订阅失败不能阻止用户消息保存到微语。
        try:
            await asyncio.to_thread(visitor.ensure_realtime_subscription, topic)
        except Exception:
            log.exception("weiyu realtime subscription failed: thread=%s", thread_uid)
        await asyncio.to_thread(visitor.send_visitor_message, thread_uid, topic, visitor_uid, content)
    except Exception as e:
        log.exception("sync to weiyu failed")


async def _ensure_thread(shop: ShopConfig, openid: str):
    visitor = WeiyuVisitorClient(shop)
    mapped = store.get_weiyu_thread(shop.shop_user_name, openid)
    if mapped is None:
        visitor_uid = await asyncio.to_thread(visitor.create_visitor, openid, f"微信用户_{openid[:8]}")
        thread_uid, topic = await asyncio.to_thread(visitor.create_agent_thread, visitor_uid)
        mapped = thread_uid, topic, visitor_uid
    store.set_weiyu_thread(shop.shop_user_name, openid, *mapped)
    try:
        await asyncio.to_thread(visitor.ensure_realtime_subscription, mapped[1])
    except Exception as error:
        log.warning('realtime subscription failed: %s', type(error).__name__)
    return mapped


async def _sync_image_to_weiyu(shop: ShopConfig, openid: str, msg: dict, msg_id: str):
    mapping = await _ensure_thread(shop, openid)
    image = msg.get('image') or {}
    await asyncio.to_thread(WeiyuVisitorClient(shop).send_visitor_image, *mapping,
        image.get('cos_url') or image.get('url') or '', msg_id)


async def _collect_shop_replies(shop):
    """Collect current human replies before sends and timeout transitions as well as on poll."""
    threads = store.all_weiyu_threads(shop.shop_user_name)
    if not threads:
        return
    replies = await asyncio.to_thread(poll_agent_replies, load_weiyu_mysql(),
        [t[0] for t in threads], {t[2] for t in threads},
        max(journal().outbox_start(), _time.time() - 2 * 86400))
    for rep in replies:
        msg_uid = rep['msg_uid']
        if store.is_agent_reply_sent(shop.shop_user_name, msg_uid):
            continue
        openid = _find_openid_by_thread(shop.shop_user_name, rep['thread_uid'])
        mapping = next((t for t in threads if t[0] == rep['thread_uid']), None)
        if not openid or mapping is None:
            continue
        p = {'openid': openid, 'thread_uid': rep['thread_uid'], 'topic': mapping[1],
             'visitor_uid': mapping[2], 'message_type': rep.get('message_type', 'TEXT'),
             'content': rep['content']}
        # Explicit agent-only bridge command; never forwarded to the WeChat user.
        if p['message_type'] == 'TEXT' and p['content'].strip() in ('/交回AI', '/ai'):
            key = journal().enqueue('control', shop.shop_user_name, msg_uid, p)
            control = journal().get(key)
            if control['state'] != 'sent':
                Conversations(journal()).release(shop.shop_user_name, openid, explicit=True)
                store.clear_agent_takeover(shop.shop_user_name, openid)
                journal().update(key, state='sent', result={'notice': '已交回 AI；有待回复消息才会继续回复，没有待回复时保持安静。'})
                delivery_service.queue_notice(journal(), journal().get(key))
            continue
        key = journal().enqueue('outgoing', shop.shop_user_name, msg_uid, p)
        job = journal().get(key)
        if not job['payload'].get('takeover_marked'):
            if Conversations(journal()).human_reply(shop.shop_user_name, openid, rep['created_at']):
                store.set_agent_takeover(shop.shop_user_name, openid)
            job['payload']['takeover_marked'] = True
            journal().update(key, payload=job['payload'])


async def _poll_agent_replies_loop() -> None:
    log.info('agent reply poller started')
    from config import all_shops
    while True:
        try:
            await asyncio.sleep(3)
            for shop in all_shops():
                await _collect_shop_replies(shop)
        except asyncio.CancelledError:
            break
        except Exception as error:
            log.error('poller error: %s', type(error).__name__)


def _find_openid_by_thread(shop_user_name: str, thread_uid: str):
    """从 Redis 映射中反查 openid。"""
    r = store.get_redis()
    pattern = f"wxstore:weiyu_thread:{shop_user_name}:*"
    for key in r.scan_iter(match=pattern, count=100):
        # decode_responses=True 时 key 已经是 str；保持兼容
        key_str = key.decode() if isinstance(key, (bytes, bytearray)) else key
        raw = r.get(key)
        if not raw:
            continue
        try:
            d = _json.loads(raw)
            if d.get("t") == thread_uid:
                return key_str.split(":")[-1]
        except Exception as e:
            log.warning("find_openid parse failed for %s: %s", key_str, e)
            continue
    return None


def _delivery_failure(job: dict, error: Exception):
    from media import MediaError
    attempts = job['attempts']
    terminal = isinstance(error, (MediaError, ValueError)) or attempts >= 8
    if isinstance(error, WxStoreError) and WxStoreClient.is_session_closed(error):
        terminal = True
    reason = str(error) if isinstance(error, MediaError) else ('微信错误码 ' + str(error.errcode) if isinstance(error, WxStoreError) else type(error).__name__)
    state = 'failed' if terminal else 'retry'
    result = job['result']
    result['notice'] = ('图片接收' if job['kind'] == 'incoming' else '微信消息发送') + '失败（' + reason + '）。' + (
        '需要人工处理后重试；后续同会话消息暂停，避免乱序。' if terminal else '正在自动重试；后续同会话消息等待，避免乱序。')
    journal().update(job['key'], state=state, error=reason,
        next_at=_time.time() + min(300, 3 * 2 ** min(attempts, 7)), result=result)
    delivery_service.queue_notice(journal(), journal().get(job['key']))
    log.warning('delivery %s: key=%s reason=%s', state, job['key'][:12], reason)


async def _delivery_loop():
    mysql = load_weiyu_mysql()
    checked_at = 0
    while True:
        try:
            conversations = Conversations(journal())
            for conversation in conversations.due():
                shop = get_shop_by_to_user(conversation['shop'])
                if shop is None:
                    continue
                # Poll freshly before releasing: a delayed DB batch must not cause
                # an old timeout to override a newer human reply.
                await _collect_shop_replies(shop)
                released = conversations.release(conversation['shop'], conversation['openid'],
                    expected_version=conversation['version'])
                if released is not None:
                    store.clear_agent_takeover(conversation['shop'], conversation['openid'])
                    mapped = store.get_weiyu_thread(conversation['shop'], conversation['openid'])
                    if mapped:
                        key = journal().enqueue('control', conversation['shop'],
                            'timeout:' + conversation['openid'] + ':' + str(released['version']), {
                                'openid': conversation['openid'], 'thread_uid': mapped[0], 'topic': mapped[1]})
                        journal().update(key, state='sent', result={'notice': '人工 180 秒未回复，已交回 AI 处理待回复内容。'})
                        delivery_service.queue_notice(journal(), journal().get(key))
            # Reconcile all tracked sent/pending/failed messages, including jobs
            # blocked behind earlier failures; cancellation must bypass retries.
            if _time.time() - checked_at >= 15:
                for job in journal().tracked_outgoing():
                    shop = get_shop_by_to_user(job['shop'])
                    if shop is None:
                        continue
                    current = await asyncio.to_thread(delivery_service.current_agent_reply, mysql,
                        job['source_id'], job['payload']['thread_uid'], shop.weiyu_org_uid)
                    if current is None:
                        delivery_service.record_cancellation(journal(), job)
                        delivery_service.queue_notice(journal(), journal().get(job['key']))
                checked_at = _time.time()
            for kind in ('outgoing', 'notice'):
                for job in journal().due(kind=kind):
                    shop = get_shop_by_to_user(job['shop'])
                    if shop is None or not journal().claim(job['key']):
                        continue
                    try:
                        if kind == 'outgoing':
                            await asyncio.to_thread(delivery_service.deliver_outgoing, journal(), job, shop, mysql)
                            if journal().get(job['key'])['state'] == 'sent':
                                store.mark_agent_reply_sent(shop.shop_user_name, job['source_id'])
                            delivery_service.queue_notice(journal(), journal().get(job['key']))
                        else:
                            await asyncio.to_thread(delivery_service.deliver_notice, journal(), job, shop)
                    except Exception as error:
                        _delivery_failure(journal().get(job['key']), error)
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break
        except Exception as error:
            log.error('delivery loop failed: %s', type(error).__name__)
            await asyncio.sleep(3)
