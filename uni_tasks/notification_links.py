"""Private Telegram bindings shared by the authenticated API and bot worker."""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import json
import secrets
import sqlite3
import time


class LinkError(ValueError):
    pass


class Links:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS bindings (
                    user_id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT, version TEXT NOT NULL, linked_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pairing (
                    user_id INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL,
                    expires_at REAL NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                    key TEXT PRIMARY KEY, user_id INTEGER NOT NULL, task_id INTEGER NOT NULL,
                    due TEXT NOT NULL, bucket TEXT NOT NULL, version TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS snoozes (
                    user_id INTEGER NOT NULL, task_id INTEGER NOT NULL, due TEXT, wake TEXT,
                    PRIMARY KEY(user_id, task_id));
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def issue(self, user_id, at=None):
        at = time.time() if at is None else at
        raw = 'n_' + secrets.token_urlsafe(24)
        with self.connection() as db:
            db.execute('DELETE FROM pairing WHERE expires_at<=?', (at,))
            db.execute('INSERT OR REPLACE INTO pairing VALUES (?,?,?,?)',
                       (user_id, sha256(raw.encode()).hexdigest(), at + 600, at))
        return raw, at + 600

    def consume(self, raw, telegram_id, username=None, at=None):
        at = time.time() if at is None else at
        if not isinstance(raw, str) or not 16 <= len(raw) <= 64 or type(telegram_id) is not int or telegram_id <= 0:
            raise LinkError('invalid')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            pairing = db.execute('SELECT * FROM pairing WHERE digest=? AND expires_at>?',
                                 (sha256(raw.encode()).hexdigest(), at)).fetchone()
            if not pairing:
                raise LinkError('expired')
            existing = db.execute('SELECT user_id FROM bindings WHERE telegram_id=?', (telegram_id,)).fetchone()
            if existing and existing['user_id'] != pairing['user_id']:
                raise LinkError('already_linked')
            uid = pairing['user_id']
            db.execute('INSERT OR REPLACE INTO bindings VALUES (?,?,?,?,?)',
                       (uid, telegram_id, (username or '')[:64] or None, secrets.token_hex(12), at))
            db.execute('DELETE FROM pairing WHERE user_id=?', (uid,))
            db.execute('DELETE FROM outbox WHERE user_id=? AND sent=0', (uid,))
            db.execute('DELETE FROM snoozes WHERE user_id=?', (uid,))
            return dict(db.execute('SELECT * FROM bindings WHERE user_id=?', (uid,)).fetchone())

    def disconnect(self, user_id, version=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM bindings WHERE user_id=?', (user_id,)).fetchone()
            if version and (not current or current['version'] != version):
                return
            db.execute('DELETE FROM bindings WHERE user_id=?', (user_id,))
            db.execute('DELETE FROM pairing WHERE user_id=?', (user_id,))
            db.execute('DELETE FROM outbox WHERE user_id=? AND sent=0', (user_id,))
            db.execute('DELETE FROM snoozes WHERE user_id=?', (user_id,))

    def binding(self, user_id=None, telegram_id=None):
        with self.connection() as db:
            row = db.execute('SELECT * FROM bindings WHERE user_id=?' if user_id is not None else
                             'SELECT * FROM bindings WHERE telegram_id=?',
                             (user_id if user_id is not None else telegram_id,)).fetchone()
            return dict(row) if row else None

    def all_bindings(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM bindings')]

    def status(self, user_id):
        binding = self.binding(user_id=user_id)
        return {'connected': bool(binding), 'telegram_username': binding['username'] if binding else None,
                'reminder_hours': [24, 3, 1], 'timezone': 'Europe/Kyiv'}

    def get(self, key, default=None):
        with self.connection() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))
