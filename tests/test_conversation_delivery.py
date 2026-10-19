import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from config import ShopConfig
from weiyu import WeiyuVisitorClient, poll_agent_replies, normalize_incoming, is_customer_message


def shop():
    return ShopConfig('gh_test', 'app', 'secret', 'token', 'key', 'http://weiyu.test', 'auth')


class RealtimeSubscriptionTest(unittest.TestCase):
    def test_aftersale_status_event_is_internal_and_does_not_look_like_customer_chat(self):
        event = {
            'MsgType': 'event', 'Event': 'channels_ec_aftersale_update',
            'finder_shop_aftersale_status_update': {
                'after_sale_order_id': 'after-1', 'order_id': 'order-1',
                'status': 'MERCHANT_WAIT_RECEIPT',
            },
        }
        self.assertFalse(is_customer_message(event))
        text = normalize_incoming(event)
        self.assertIn('售后状态更新', text)
        self.assertIn('MERCHANT_WAIT_RECEIPT', text)

    def test_aftersale_event_without_identifiers_is_explicitly_unresolved(self):
        text = normalize_incoming({
            'MsgType': 'event', 'Event': 'channels_ec_aftersale_update',
            'finder_shop_aftersale_status_update': {'status': 'MERCHANT_WAIT_RECEIPT'},
        })
        self.assertIn('未提供售后单号或订单号', text)
        self.assertIn('后台', text)

    def test_subscribes_login_user_before_syncing_existing_conversation(self):
        calls = []
        def handle(request):
            calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
            if request.url.path.endswith('/agent/query/uid'):
                return httpx.Response(200, json={'code': 200, 'data': {'uid': 'df_ag_uid', 'userUid': 'login-user'}})
            if request.url.path.endswith('/user/profile'):
                return httpx.Response(200, json={'code': 200, 'data': {'uid': 'login-user'}})
            return httpx.Response(200, json={'code': 200, 'data': {}})
        original = httpx.Client
        with patch('weiyu.httpx.Client', side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
            WeiyuVisitorClient(shop()).ensure_realtime_subscription('org/agent/df_ag_uid/visitor')
        subscriptions = [body['topic'] for method, path, body in calls if path.endswith('/subscribe')]
        self.assertEqual(subscriptions, ['org/agent/df_ag_uid/visitor', 'org/agent/df_ag_uid/visitor_internal'])


class HumanReplyPollingTest(unittest.TestCase):
    def test_mysql_bit_zero_is_not_a_recall(self):
        from weiyu import public_agent_reply
        row = {'is_deleted': b'\x00', 'message_type': 'IMAGE', 'message_user': '{"type":"AGENT"}', 'extra': '{}'}
        self.assertTrue(public_agent_reply(row))
        row['is_deleted'] = b'\x01'
        self.assertFalse(public_agent_reply(row))

    def test_poll_connection_uses_utc_for_stored_message_times(self):
        from datetime import datetime, timezone
        from config import WeiyuMysqlConfig
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, *args): pass
            def fetchall(self):
                if not hasattr(self, 'read_threads'):
                    self.read_threads = True
                    return [{'uuid': 'thread', 'id': 1}]
                return [dict(uuid='human', content='reply', user_uid='agent', thread_id=1,
                             created_at=datetime(2026, 10, 15, 13, 0),
                             message_user='{"type":"AGENT"}', extra='{}')]
        class Connection:
            def cursor(self): return Cursor()
            def close(self): pass
        with patch('weiyu.pymysql.connect', return_value=Connection()) as connect:
            replies = poll_agent_replies(WeiyuMysqlConfig(), ['thread'], {'visitor'}, 0)
        self.assertEqual(connect.call_args.kwargs.get('init_command'), "SET time_zone = '+00:00'")
        self.assertEqual(replies[0]['created_at'], datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc).timestamp())

    def test_only_public_human_reply_is_forwardable(self):
        from datetime import datetime
        rows = []
        for uid, sender, extra in [
            ('human', {'type': 'AGENT', 'nickname': '客服'}, {'visibility': 'public'}),
            ('ai', {'type': 'AGENT', 'nickname': '任意名称'}, {'source': 'wxstore_ai'}),
            ('legacy-ai', {'type': 'AGENT', 'nickname': 'AI客服'}, {}),
            ('note', {'type': 'AGENT'}, {'visibility': 'internal'}),
            ('system', {'type': 'SYSTEM'}, {}),
        ]:
            rows.append(dict(uuid=uid, content='{"text":"reply"}', user_uid='agent',
                             thread_id=1, created_at=datetime.now(),
                             message_user=json.dumps(sender), extra=json.dumps(extra)))
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, *args): pass
            def fetchall(self):
                if not hasattr(self, 'read_threads'):
                    self.read_threads = True
                    return [{'uuid': 'thread', 'id': 1}]
                return rows
        class Connection:
            def cursor(self): return Cursor()
            def close(self): pass
        from config import WeiyuMysqlConfig
        with patch('weiyu.pymysql.connect', return_value=Connection()):
            replies = poll_agent_replies(WeiyuMysqlConfig(), ['thread'], {'visitor'}, 0)
        self.assertEqual([r['msg_uid'] for r in replies], ['human'])


class AITakeoverRaceTest(unittest.IsolatedAsyncioTestCase):
    async def test_human_reply_during_generation_cancels_ai_reply(self):
        import worker
        cfg = shop()
        with patch('worker.get_shop_by_to_user', return_value=cfg), \
             patch('worker.store.is_duplicate', return_value=False), \
             patch('worker.store.is_agent_takeover', side_effect=[False, True]), \
             patch('worker._sync_to_weiyu', new_callable=AsyncMock), \
             patch('worker.WeiyuClient') as ai, \
             patch('worker._sync_agent_msg_to_weiyu', new_callable=AsyncMock) as mirror, \
             patch('worker.WxStoreClient') as wx:
            ai.return_value.chat.return_value = {'reply': 'AI generated reply'}
            await worker._process({'shop_user_name': cfg.shop_user_name, 'openid': 'visitor',
                                   'msg_id': 'incoming',
                                   'msg': {'msg_type': 'text', 'text': {'content': 'hello'}}})
            mirror.assert_not_awaited()
            wx.return_value.send_text.assert_not_called()

    async def test_human_reply_during_mirror_sync_prevents_wechat_send(self):
        import worker
        with patch('worker.store.is_agent_takeover', side_effect=[False, True]), \
             patch('worker._sync_agent_msg_to_weiyu', new_callable=AsyncMock), \
             patch('worker.WxStoreClient') as wx:
            await worker._send_ai_reply(shop(), 'visitor', 'AI reply')
            wx.return_value.send_text.assert_not_called()


class CallbackIngressTest(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_callback_reaches_queue_without_any_outbound_send(self):
        import base64
        import crypto
        import main
        cfg = shop()
        cfg.encoding_aes_key = base64.b64encode(b'x' * 32).decode().rstrip('=')
        msg = {'ToUserName': cfg.shop_user_name, 'FromUserName': 'test-visitor',
               'msg_id': 'probe-message', 'msg_type': 'text', 'text': {'content': 'hello'}}
        encrypted = crypto.encrypt(cfg.encoding_aes_key, cfg.app_id, json.dumps(msg))
        params = {'timestamp': '123456', 'nonce': 'probe',
                  'msg_signature': crypto.make_post_signature(cfg.token, '123456', 'probe', encrypted)}
        with patch('main.get_shop_by_slug', return_value=cfg), \
             patch('main.worker.enqueue') as enqueue:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                         base_url='http://callback.test') as client:
                response = await client.post('/wxstore/test/webhook', params=params,
                                             json={'Encrypt': encrypted})
                self.assertEqual((response.status_code, response.text), (200, 'success'))
                enqueue.assert_called_once_with({'shop_user_name': cfg.shop_user_name,
                                                'openid': 'test-visitor',
                                                'msg_id': 'probe-message', 'msg': msg})
                enqueue.reset_mock()
                params['msg_signature'] = 'invalid'
                await client.post('/wxstore/test/webhook', params=params, json={'Encrypt': encrypted})
                enqueue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
