import tempfile
import unittest
from pathlib import Path

from delivery import DeliveryJournal
from conversation import Conversations


class OwnershipTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = DeliveryJournal(str(Path(self.directory.name)/'delivery.sqlite3'))
        self.convos = Conversations(self.journal)

    def incoming(self, source, received_at):
        key = self.journal.enqueue('incoming', 'shop', source, {'openid':'visitor', 'mirrored':True,
            'event':{'msg_type':'unused', 'msg':{'msg_type':'text','text':{'content':source}}}})
        with self.journal.connection() as db:
            db.execute('UPDATE deliveries SET created_at=? WHERE key=?',(received_at,key))
        self.journal.update(key,state='sent')
        return key

    def test_first_unanswered_message_owns_deadline_and_restart_preserves_it(self):
        self.convos.human_reply('shop','visitor',100)
        self.assertEqual(self.convos.due(now=1000),[])  # no user message, no timer
        first=self.incoming('first',110)
        self.assertTrue(self.convos.defer(first))
        later=self.incoming('later',200)
        self.assertTrue(self.convos.defer(later))
        self.assertEqual(self.convos.get('shop','visitor')['pending_since'],110)
        reopened=Conversations(DeliveryJournal(str(self.journal.path)))
        self.assertEqual(reopened.due(now=289),[])
        due=reopened.due(now=290)[0]
        result=reopened.release('shop','visitor',expected_version=due['version'],now=290)
        self.assertEqual(result['pending_count'],2)
        resumes=[j for j in self.journal.due() if j['source_id'].startswith('visitor:resume')]
        self.assertEqual(len(resumes),1)
        self.assertIn('first',resumes[0]['payload']['event']['msg']['text']['content'])
        self.assertIsNone(reopened.release('shop','visitor',expected_version=due['version'],now=300))

    def test_human_reply_clears_wait_and_stale_timer_cannot_release(self):
        self.convos.human_reply('shop','visitor',100)
        self.convos.defer(self.incoming('question',110))
        stale=self.convos.due(now=290)[0]
        self.convos.human_reply('shop','visitor',291)
        self.assertIsNone(self.convos.get('shop','visitor')['pending_since'])
        self.assertIsNone(self.convos.release('shop','visitor',expected_version=stale['version'],now=400))
        self.assertFalse(self.journal.get(self.journal.enqueue('incoming','shop','question',{}))['payload']['deferred'])

    def test_explicit_release_without_waiting_message_sends_nothing(self):
        self.convos.human_reply('shop','visitor',100)
        self.assertEqual(self.convos.release('shop','visitor',explicit=True)['pending_count'],0)
        self.assertEqual(self.journal.due(),[])
        self.assertEqual(self.convos.get('shop','visitor')['owner'],'ai')

    def test_delayed_poll_does_not_clear_message_that_arrived_after_human_reply(self):
        self.convos.human_reply('shop','visitor',100)
        key=self.incoming('newer',130)
        self.convos.defer(key)
        self.convos.human_reply('shop','visitor',120)
        self.assertEqual(self.convos.get('shop','visitor')['pending_since'],130)
        self.assertTrue(self.journal.get(key)['payload']['deferred'])


if __name__=='__main__':unittest.main()
