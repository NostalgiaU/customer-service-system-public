import base64
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

from config import ShopConfig
from weiyu import WeiyuVisitorClient

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jI1cAAAAASUVORK5CYII=')


def shop():
    return ShopConfig('gh_test', 'app', 'secret', 'token', 'key', 'http://weiyu.test', 'auth')


class ImageIngressTest(unittest.TestCase):
    def test_incoming_image_is_archived_and_sent_as_image_not_placeholder(self):
        captured = []
        def handle(request):
            if request.url.host == 'channels.weixin.qq.com':
                return httpx.Response(200, content=PNG, headers={'content-type': 'image/png'})
            if request.url.path == '/api/v1/upload/file':
                self.assertIn(PNG, request.content)
                return httpx.Response(200, json={'code': 200, 'data': {'fileUrl': 'http://weiyu.test/file/image.png'}})
            if request.url.path.endswith('/message/meta'):
                return httpx.Response(200, json={'code': 200, 'data': {'messageUid': 'image-msg'}})
            if request.url.path.endswith('/message/send'):
                captured.append(json.loads(json.loads(request.content)['json']))
                return httpx.Response(200, json={'code': 200})
            raise AssertionError(str(request.url))
        original = httpx.Client
        with patch('httpx.Client', side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
            WeiyuVisitorClient(shop()).send_visitor_image('thread', 'topic', 'visitor',
                'https://channels.weixin.qq.com/shop/commkf/downloadmedia?encrypted_param=test', 'wx-image')
        self.assertEqual(captured[0]['type'], 'IMAGE')
        content = json.loads(captured[0]['content'])
        self.assertEqual(content['url'], 'http://weiyu.test/file/image.png')
        self.assertEqual(json.loads(captured[0]['extra'])['wxMessageId'], 'wx-image')


class PersistentDeliveryTest(unittest.TestCase):
    def test_retry_survives_reopen_and_completed_job_is_not_requeued(self):
        from delivery import DeliveryJournal
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'delivery.sqlite3')
            first = DeliveryJournal(path)
            key = first.enqueue('image_in', 'shop', 'message', {'openid': 'visitor'})
            first.update(key, state='retry', next_at=0, error='图片下载失败')
            second = DeliveryJournal(path)
            self.assertEqual(second.due()[0]['error'], '图片下载失败')
            second.update(key, state='sent')
            second.enqueue('image_in', 'shop', 'message', {'openid': 'wrong'})
            self.assertEqual(second.due(), [])
            self.assertEqual(second.get(key)['payload']['openid'], 'visitor')


class DurableOutboxTest(unittest.TestCase):
    def setUp(self):
        from delivery import DeliveryJournal
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = DeliveryJournal(str(Path(self.directory.name) / 'jobs.sqlite3'))
        self.cfg = shop()

    def job(self):
        key = self.journal.enqueue('outgoing', self.cfg.shop_user_name, 'human-image', {
            'openid': 'visitor', 'thread_uid': 'thread', 'topic': 'topic',
            'visitor_uid': 'visitor-id', 'message_type': 'IMAGE', 'content': '/file/image.png'})
        return self.journal.get(key)

    def test_recall_during_upload_cancels_before_send(self):
        from delivery_service import deliver_outgoing
        job = self.job()
        with patch('delivery_service.current_agent_reply', side_effect=[job['payload'], None]), \
             patch('delivery_service.download_image') as download, \
             patch('delivery_service.WxStoreClient') as wx:
            download.return_value.data = PNG
            download.return_value.filename = 'image.png'
            wx.return_value.cosupload.return_value = 'wx-image-url'
            deliver_outgoing(self.journal, job, self.cfg, object())
            wx.return_value.send_image.assert_not_called()
        self.assertEqual(self.journal.get(job['key'])['state'], 'cancelled')

    def test_retry_reuses_upload_and_stable_request_id(self):
        from delivery_service import deliver_outgoing
        job = self.job()
        with patch('delivery_service.current_agent_reply', return_value=job['payload']), \
             patch('delivery_service.download_image') as download, \
             patch('delivery_service.WxStoreClient') as wx:
            download.return_value.data = PNG
            download.return_value.filename = 'image.png'
            wx.return_value.cosupload.return_value = 'wx-image-url'
            wx.return_value.send_image.side_effect = [httpx.ReadTimeout('unknown result'), {'msg_id': 'wx-id'}]
            with self.assertRaises(httpx.ReadTimeout):
                deliver_outgoing(self.journal, job, self.cfg, object())
            deliver_outgoing(self.journal, self.journal.get(job['key']), self.cfg, object())
            self.assertEqual(wx.return_value.cosupload.call_count, 1)
            sends = wx.return_value.send_image.call_args_list
            self.assertEqual(sends[0].args, sends[1].args)
            self.assertEqual(sends[0].args[0], 'visitor')
            self.assertEqual(self.journal.get(job['key'])['result']['wx_msg_id'], 'wx-id')

    def test_failed_older_message_blocks_later_same_customer_only(self):
        first = self.job()
        self.journal.update(first['key'], state='retry', next_at=9999999999)
        self.journal.enqueue('outgoing', self.cfg.shop_user_name, 'later', {'openid': 'visitor'})
        other = self.journal.enqueue('outgoing', self.cfg.shop_user_name, 'other', {'openid': 'other'})
        self.assertEqual([job['key'] for job in self.journal.due(kind='outgoing')], [other])


    def test_recall_after_unknown_send_never_blindly_retries(self):
        from delivery_service import deliver_outgoing
        job = self.job()
        self.journal.update(job['key'], result={'submitted': True, 'cos_url': 'old-upload'})
        with patch('delivery_service.current_agent_reply', return_value=None), patch('delivery_service.WxStoreClient') as wx:
            deliver_outgoing(self.journal, self.journal.get(job['key']), self.cfg, object())
            wx.assert_not_called()
        self.assertEqual(self.journal.get(job['key'])['state'], 'recall_unknown')

    def test_recall_after_acceptance_preserves_wechat_message_mapping(self):
        from delivery_service import record_cancellation
        job = self.job()
        self.journal.update(job['key'], state='sent', result={'submitted': True, 'wx_msg_id': 'remote-message'})
        record_cancellation(self.journal, self.journal.get(job['key']))
        result = self.journal.get(job['key'])
        self.assertEqual(result['state'], 'recalled_local')
        self.assertEqual(result['result']['wx_msg_id'], 'remote-message')
        self.assertIn('微信侧未撤回', result['result']['notice'])

    def test_real_image_http_payload_uses_correct_user_and_stable_id(self):
        from delivery_service import deliver_outgoing
        job = self.job()
        calls = []
        def handle(request):
            calls.append(request)
            if request.url.host == 'weiyu.test':
                self.assertNotIn('authorization', request.headers)
                return httpx.Response(200, content=PNG)
            if request.url.path.endswith('/cosupload'):
                self.assertIn(b'visitor', request.content)
                self.assertIn(PNG, request.content)
                return httpx.Response(200, json={'errcode': 0, 'cos_url': 'wx-upload'})
            if request.url.path.endswith('/sendmsg'):
                data = json.loads(request.content)
                self.assertEqual(data['open_id'], 'visitor')
                self.assertEqual(data['msg_type'], 'image')
                self.assertEqual(data['image']['cos_url'], 'wx-upload')
                self.assertEqual(data['request_id'], job['key'][:32])
                return httpx.Response(200, json={'errcode': 0, 'msg_id': 'actual-wx-id'})
            raise AssertionError(request.url.path)
        original = httpx.Client
        with patch('delivery_service.current_agent_reply', return_value=job['payload']), \
             patch('wxstore.WxStoreClient._get_token', return_value='test-token'), \
             patch('httpx.Client', side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
            deliver_outgoing(self.journal, job, self.cfg, object())
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.journal.get(job['key'])['result']['wx_msg_id'], 'actual-wx-id')


class DurableIngressTest(unittest.TestCase):
    def test_delivery_status_does_not_create_chat_notice(self):
        from delivery_service import queue_notice
        from delivery import DeliveryJournal
        with tempfile.TemporaryDirectory() as directory:
            journal = DeliveryJournal(str(Path(directory) / 'notice.sqlite3'))
            job = journal.enqueue('outgoing', 'shop', 'status', {
                'openid': 'visitor', 'thread_uid': 'thread', 'topic': 'topic'})
            journal.update(job, state='sent', result={'notice': '微信已接受文字发送'})
            queue_notice(journal, journal.get(job))
            self.assertEqual(journal.due(kind='notice'), [])

    def test_callback_ack_queue_is_only_a_hint_and_replay_is_deduplicated(self):
        import asyncio
        import worker
        from delivery import DeliveryJournal
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'inbox.sqlite3')
            journal = DeliveryJournal(path)
            event = {'shop_user_name': 'test', 'openid': 'visitor', 'msg_id': 'wx-image',
                     'msg': {'msg_type': 'image', 'image': {'cos_url': 'https://channels.weixin.qq.com/image'}}}
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                with patch.object(worker, '_journal', journal), patch.object(worker, '_queue', asyncio.Queue(maxsize=1)):
                    worker.enqueue(event)
                    worker.enqueue(event)
            finally:
                asyncio.set_event_loop(None)
                loop.close()
            reopened = DeliveryJournal(path)
            jobs = reopened.due(kind='incoming')
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]['payload']['event'], event)


class MediaBoundaryTest(unittest.TestCase):
    def test_redirect_to_untrusted_host_is_blocked_without_credentials(self):
        from media import download_image, MediaError
        requests = []
        def handle(request):
            requests.append(request)
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        original = httpx.Client
        with patch('httpx.Client', side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
            with self.assertRaises(MediaError):
                download_image('https://channels.weixin.qq.com/image?encrypted_param=secret', from_wechat=True)
        self.assertEqual(len(requests), 1)

    def test_html_and_oversize_are_rejected(self):
        from media import download_image, MediaError
        original = httpx.Client
        for body, headers in [(b'<html>fake</html>', {'content-type': 'image/png'}),
                              (PNG, {'content-length': str(31 * 1024 * 1024)})]:
            with self.subTest(headers=headers), patch('httpx.Client', side_effect=lambda **kw: original(
                    transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body, headers=headers)), **kw)):
                with self.assertRaises(MediaError):
                    download_image('http://weiyu.test/file/test.png', 'http://weiyu.test')

    def test_malformed_port_has_sanitized_error(self):
        from media import download_image, MediaError
        with self.assertRaises(MediaError) as caught:
            download_image('https://channels.weixin.qq.com:bad/file?encrypted_param=secret', from_wechat=True)
        self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
