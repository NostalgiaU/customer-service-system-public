"""Persistent human/AI ownership and first-unanswered-message deadlines."""
from __future__ import annotations

import json
import time

from weiyu import normalize_incoming


class Conversations:
    def __init__(self, journal):
        self.journal = journal
        with journal.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS conversations (
                shop TEXT NOT NULL, openid TEXT NOT NULL, owner TEXT NOT NULL,
                human_at REAL NOT NULL DEFAULT 0, pending_since REAL,
                version INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(shop,openid))''')

    def get(self, shop, openid):
        with self.journal.connection() as db:
            row = db.execute('SELECT * FROM conversations WHERE shop=? AND openid=?', (shop, openid)).fetchone()
        return dict(row) if row else None

    def human_reply(self, shop, openid, replied_at):
        with self.journal.connection() as db:
            db.execute("INSERT OR IGNORE INTO conversations(shop,openid,owner) VALUES(?,?,'human')", (shop, openid))
            row = db.execute('SELECT * FROM conversations WHERE shop=? AND openid=?', (shop, openid)).fetchone()
            if replied_at <= row['human_at']:
                return False
            db.execute("UPDATE conversations SET owner='human',human_at=?,pending_since=NULL,version=version+1 WHERE shop=? AND openid=?",
                       (replied_at, shop, openid))
            # Only messages preceding this human reply are answered. Newer messages
            # retain their original receive time, even if the DB poll was delayed.
            rows = db.execute("SELECT key,payload,created_at FROM deliveries WHERE kind='incoming' AND shop=? AND json_extract(payload,'$.openid')=?",
                              (shop, openid)).fetchall()
            pending = []
            for item in rows:
                payload = json.loads(item['payload'])
                if item['created_at'] <= replied_at:
                    payload['answered_by_human'] = True
                    payload['deferred'] = False
                    db.execute('UPDATE deliveries SET payload=? WHERE key=?', (json.dumps(payload), item['key']))
                elif payload.get('deferred'):
                    pending.append(item['created_at'])
            if pending:
                db.execute('UPDATE conversations SET pending_since=? WHERE shop=? AND openid=?', (min(pending), shop, openid))
            return True

    def defer(self, key, *, legacy_human=False):
        """Return True if AI should remain silent; use callback receipt time, never retry time."""
        with self.journal.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT * FROM deliveries WHERE key=?', (key,)).fetchone()
            payload = json.loads(job['payload'])
            shop, openid = job['shop'], payload['openid']
            if legacy_human:
                db.execute("INSERT OR IGNORE INTO conversations(shop,openid,owner) VALUES(?,?,'human')", (shop, openid))
            row = db.execute('SELECT * FROM conversations WHERE shop=? AND openid=?', (shop, openid)).fetchone()
            if payload.get('answered_by_human'):
                return True
            if not row or row['owner'] != 'human':
                return False
            if job['created_at'] <= row['human_at']:
                payload['answered_by_human'] = True
            else:
                payload['deferred'] = True
                start = min(row['pending_since'] or job['created_at'], job['created_at'])
                db.execute('UPDATE conversations SET pending_since=? WHERE shop=? AND openid=?', (start, shop, openid))
            db.execute('UPDATE deliveries SET payload=? WHERE key=?', (json.dumps(payload), key))
            return True

    def due(self, now=None):
        with self.journal.connection() as db:
            rows = db.execute("SELECT * FROM conversations WHERE owner='human' AND pending_since IS NOT NULL AND pending_since<=?", ((now if now is not None else time.time()) - 180,)).fetchall()
        return [dict(row) for row in rows]

    def release(self, shop, openid, *, expected_version=None, now=None, explicit=False):
        """Atomically change owner and enqueue one aggregate reply, only if messages wait."""
        now = now if now is not None else time.time()
        with self.journal.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM conversations WHERE shop=? AND openid=?', (shop, openid)).fetchone()
            if row is None:
                if explicit:
                    db.execute("INSERT INTO conversations(shop,openid,owner) VALUES(?,?,'ai')", (shop, openid))
                return None
            if expected_version is not None and row['version'] != expected_version:
                return None
            if not explicit and (row['owner'] != 'human' or row['pending_since'] is None or row['pending_since'] + 180 > now):
                return None
            db.execute("UPDATE conversations SET owner='ai',pending_since=NULL,version=version+1 WHERE shop=? AND openid=?", (shop, openid))
            jobs = db.execute("SELECT * FROM deliveries WHERE kind='incoming' AND shop=? AND json_extract(payload,'$.openid')=? AND json_extract(payload,'$.deferred')=1 ORDER BY created_at", (shop, openid)).fetchall()
            messages, mapping = [], {}
            for item in jobs:
                payload = json.loads(item['payload'])
                messages.append(normalize_incoming(payload['event']['msg']))
                mapping.update({k: payload[k] for k in ('thread_uid', 'topic', 'visitor_uid') if k in payload})
                payload['deferred'] = False
                db.execute('UPDATE deliveries SET payload=? WHERE key=?', (json.dumps(payload), item['key']))
            if messages:
                import hashlib
                source = 'resume-' + str(row['version'] + 1)
                key = hashlib.sha256(json.dumps(['incoming', shop, openid + ':' + source]).encode()).hexdigest()
                payload = {'openid': openid, 'mirrored': True, **mapping, 'event': {
                    'shop_user_name': shop, 'openid': openid, 'msg_id': source,
                    'msg': {'msg_type': 'text', 'text': {'content': '人工暂未继续回复，请基于以下待回复消息继续帮助用户（不要把这些内部说明发给用户）：\n' + '\n'.join(messages)}}}}
                db.execute("INSERT OR IGNORE INTO deliveries(key,kind,shop,source_id,payload,created_at,updated_at) VALUES(?,'incoming',?,?,?,?,?)",
                           (key, shop, openid + ':' + source, json.dumps(payload), now, now))
            return {'pending_count': len(messages), 'version': row['version'] + 1}
