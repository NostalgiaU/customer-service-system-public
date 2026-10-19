"""Durable image inbox / human reply outbox; contains no application credentials."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class DeliveryJournal:
    def __init__(self, path: str = ''):
        self.path = Path(path or os.getenv('DELIVERY_DB_PATH', str(Path(__file__).parent / '.runtime/delivery.sqlite3')))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.name == '.runtime':
            self.path.parent.chmod(0o700)
        with self.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
                key TEXT PRIMARY KEY, kind TEXT NOT NULL, shop TEXT NOT NULL, source_id TEXT NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS delivery_due ON deliveries(state,next_at)')
            db.execute('CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO settings(name,value) VALUES('outbox_start',?)", (str(time.time()),))
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, kind: str, shop: str, source_id: str, payload: dict) -> str:
        key = hashlib.sha256(json.dumps([kind, shop, source_id]).encode()).hexdigest()
        now = time.time()
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO deliveries(key,kind,shop,source_id,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                (key, kind, shop, source_id, json.dumps(payload), now, now))
        return key

    def get(self, key: str):
        with self.connection() as db:
            row = db.execute('SELECT * FROM deliveries WHERE key=?', (key,)).fetchone()
        return self.decode(row) if row else None

    def outbox_start(self):
        with self.connection() as db:
            return float(db.execute("SELECT value FROM settings WHERE name='outbox_start'").fetchone()[0])

    @staticmethod
    def decode(row):
        value = dict(row)
        value['payload'] = json.loads(value['payload'])
        value['result'] = json.loads(value['result'])
        return value

    def due(self, limit: int = 50, *, kind: str = ''):
        now = time.time()
        with self.connection() as db:
            rows = db.execute("""SELECT d.* FROM deliveries d
                WHERE d.state IN ('pending','retry','sending') AND d.next_at<=? AND d.lease_until<=?
                AND (?='' OR d.kind=?)
                AND NOT EXISTS (SELECT 1 FROM deliveries older
                    WHERE older.kind=d.kind AND older.shop=d.shop
                    AND json_extract(older.payload,'$.openid')=json_extract(d.payload,'$.openid')
                    AND older.state IN ('pending','retry','sending','failed')
                    AND (older.created_at<d.created_at OR (older.created_at=d.created_at AND older.rowid<d.rowid)))
                ORDER BY d.created_at,d.rowid LIMIT ?""", (now, now, kind, kind, limit)).fetchall()
        return [self.decode(row) for row in rows]

    def tracked_outgoing(self):
        # Only post-upgrade messages are tracked; never attempt recalls of legacy history.
        with self.connection() as db:
            rows = db.execute("""SELECT * FROM deliveries WHERE kind='outgoing'
                AND state IN ('pending','retry','sending','failed','sent')
                AND created_at>? ORDER BY created_at""", (time.time() - 2 * 86400,)).fetchall()
        return [self.decode(row) for row in rows]

    def deferred_incoming(self, shop: str, openid: str):
        with self.connection() as db:
            rows = db.execute("""SELECT * FROM deliveries
                WHERE kind='incoming' AND shop=? AND state='sent'
                AND json_extract(payload,'$.openid')=? AND json_extract(payload,'$.deferred')=1
                ORDER BY created_at""", (shop, openid)).fetchall()
        return [self.decode(row) for row in rows]

    def claim(self, key: str) -> bool:
        now = time.time()
        with self.connection() as db:
            cursor = db.execute("UPDATE deliveries SET state='sending',attempts=attempts+1,lease_until=?,updated_at=? WHERE key=? AND state IN ('pending','retry','sending') AND lease_until<=? AND next_at<=?",
                (now + 180, now, key, now, now))
        return cursor.rowcount == 1

    def update(self, key: str, **fields):
        allowed = {'state', 'next_at', 'error', 'payload', 'result', 'lease_until'}
        if not fields.keys() <= allowed:
            raise ValueError('invalid delivery field')
        fields['updated_at'] = time.time()
        for name in ('payload', 'result'):
            if name in fields:
                fields[name] = json.dumps(fields[name])
        if 'state' in fields and fields['state'] != 'sending':
            fields['lease_until'] = 0
        with self.connection() as db:
            db.execute('UPDATE deliveries SET ' + ','.join(name + '=?' for name in fields) + ' WHERE key=?', [*fields.values(), key])
