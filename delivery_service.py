"""Recoverable WeChat/Weiyu image transfer and human outbox operations.

All state transitions persist before external side effects. A stable request_id
allows WeChat to deduplicate ambiguous send outcomes across retries.
"""
from __future__ import annotations

from delivery import DeliveryJournal
from media import download_image
from weiyu import WeiyuVisitorClient, current_agent_reply, is_customer_message, normalize_incoming
from wxstore import WxStoreClient


def mirror_incoming(journal: DeliveryJournal, job: dict, shop, mapping: tuple):
    payload = job['payload']
    event = payload['event']
    msg = event['msg']
    thread_uid, topic, visitor_uid = mapping
    visitor = WeiyuVisitorClient(shop)
    # Stable UID is saved before submission, including retries after a timeout.
    if not payload.get('message_uid'):
        payload['message_uid'] = visitor.message_uid(thread_uid)
        journal.update(job['key'], payload=payload)
    if not is_customer_message(msg):
        visitor.send_internal_note(thread_uid, topic, normalize_incoming(msg), payload['message_uid'])
    elif msg.get('msg_type') == 'image':
        if not payload.get('archived'):
            image = msg.get('image') or {}
            url = image.get('cos_url') or image.get('url')
            if not url:
                raise ValueError('微信图片消息缺少媒体地址')
            payload['archived'] = visitor.archive_image(url)
            journal.update(job['key'], payload=payload)
        visitor.send_visitor_image(thread_uid, topic, visitor_uid, '', event['msg_id'],
            archived=payload['archived'], message_uid=payload['message_uid'])
    else:
        visitor.send_visitor_message(thread_uid, topic, visitor_uid, normalize_incoming(msg),
            message_uid=payload['message_uid'], extra_fields={'wxMessageId': event['msg_id']})
    payload['mirrored'] = True
    journal.update(job['key'], payload=payload)


def record_cancellation(journal: DeliveryJournal, job: dict):
    result = job['result']
    if job['state'] == 'sent' or result.get('wx_msg_id'):
        state = 'recalled_local'
        result['notice'] = '仅微语侧撤回，微信侧未撤回。当前微信小店通道尚未接入适用的撤回接口。'
    elif result.get('submitted'):
        state = 'recall_unknown'
        result['notice'] = '微语已撤回；原微信发送结果未知，用户可能已收到。已停止重试，微信侧未确认撤回。'
    else:
        state = 'cancelled'
        result['notice'] = '消息已在微信发送前取消，不再上传、发送或重试。'
    journal.update(job['key'], state=state, result=result)


def deliver_outgoing(journal: DeliveryJournal, job: dict, shop, mysql):
    payload = job['payload']
    result = job['result']
    # Validate current sender, org, thread, visibility, type and recall status.
    current = current_agent_reply(mysql, job['source_id'], payload['thread_uid'], shop.weiyu_org_uid)
    if current is None:
        record_cancellation(journal, job)
        return
    # Freeze the original submitted content: an edited row must not change a retry.
    if current.get('content') != payload['content'] or current.get('message_type') != payload['message_type']:
        raise ValueError('原消息内容已改变，请人工核实后重新发送')
    wx = WxStoreClient(shop)
    if payload['message_type'] == 'IMAGE' and not result.get('cos_url'):
        image = download_image(payload['content'], shop.weiyu_base_url)
        result['cos_url'] = wx.cosupload(payload['openid'], 'image', image.data, image.filename)
        journal.update(job['key'], result=result)
    # Image download/upload can take seconds; a recall during transfer wins.
    latest = current_agent_reply(mysql, job['source_id'], payload['thread_uid'], shop.weiyu_org_uid)
    if latest is None:
        record_cancellation(journal, journal.get(job['key']))
        return
    if latest.get('content') != payload['content'] or latest.get('message_type') != payload['message_type']:
        raise ValueError('原消息内容已改变，请人工核实后重新发送')
    result['submitted'] = True
    journal.update(job['key'], result=result)
    # Use the same ID for retries, including a timeout after WeChat accepted it.
    request_id = job['key'][:32]
    if payload['message_type'] == 'IMAGE':
        response = wx.send_image(payload['openid'], result['cos_url'], request_id)
    else:
        response = wx.send_text(payload['openid'], payload['content'], request_id)
    result['wx_msg_id'] = response.get('msg_id', '')
    result['notice'] = '微信已接受' + ('图片' if payload['message_type'] == 'IMAGE' else '文字') + '发送（原消息 ' + job['source_id'] + '），不代表用户已读。'
    journal.update(job['key'], state='sent', result=result, error='')


def queue_notice(journal: DeliveryJournal, job: dict):
    """Persist delivery state without injecting operational chatter into chat.

    The workbench conversation is reserved for user, AI, human, and business
    event messages. Delivery details remain in the journal and service log.
    """
    return None

def deliver_notice(journal: DeliveryJournal, job: dict, shop):
    p = job['payload']
    WeiyuVisitorClient(shop).send_internal_note(p['thread_uid'], p['topic'], p['content'], p['message_uid'])
    journal.update(job['key'], state='sent')
