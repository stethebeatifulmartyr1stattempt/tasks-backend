from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import dateparser
from pydantic import BaseModel, ConfigDict, Field

CONFIG = Path(os.getenv('UNI_CONFIG', '/config'))
STATE = Path(os.getenv('UNI_STATE', '/state'))
SPOOL = Path(os.getenv('UNI_SPOOL', '/spool'))
UTC = timezone.utc


def now():
    return datetime.now(UTC)


def read_json(path, default=None):
    return json.loads(path.read_text('utf-8')) if path.exists() else default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def config():
    return read_json(CONFIG / 'config.json', {})


def secrets():
    return read_json(CONFIG / 'secrets.json', {})


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def normalize(value):
    value = unicodedata.normalize('NFKC', value).casefold().replace('і', 'i')
    return re.sub(r'[^\w]+', ' ', value).strip()


class Item(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(max_length=220)
    course: str = Field(max_length=150)
    assignment_key: str = Field(max_length=120)
    evidence: str = Field(max_length=1500)
    source_timestamp: str
    due_text: str | None
    due_evidence: str | None
    has_time: bool
    state: str = Field(pattern='^(open|submitted|cancelled)$')
    confidence: float = Field(ge=0, le=1)
    review_reason: str | None


class Extraction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_key: str
    tasks: list[Item]


class Batch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    messages: list[Extraction]


def resolve_due(item: Item, source: dict, tz='Europe/Kyiv'):
    """Anchor relative dates to the actual message; never use today's date."""
    if not item.due_text:
        return None
    matching = [m for m in source['messages'] if m['at'] == item.source_timestamp]
    if not item.due_evidence or not any(item.due_evidence in m['text'] for m in matching):
        return None
    if item.due_text not in item.due_evidence:
        return None
    stamp = datetime.fromisoformat(item.source_timestamp).astimezone(ZoneInfo(tz))
    # Lesson-relative deadlines need a timetable we do not have.
    if re.search(r'занят|пар[иу]|урок|наступн|наступающ|next\s+(class|lesson)', item.due_text, re.I):
        return None
    numeric = re.search(r'(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)', item.due_text)
    if numeric and not re.search(r'завтра|сьогодні|tomorrow|today|через', item.due_text, re.I):
        day, month = int(numeric[1]), int(numeric[2])
        year = int(numeric[3]) if numeric[3] else None
        if year and year < 100:
            year += 2000
        clock = re.search(r'(\d{1,2}):(\d{2})', item.due_text)
        hour, minute = (int(clock[1]), int(clock[2])) if clock else (0, 0)
        candidates = []
        for candidate_year in ([year] if year else [stamp.year - 1, stamp.year, stamp.year + 1]):
            try:
                candidates.append(datetime(candidate_year, month, day, hour, minute, tzinfo=ZoneInfo(tz)))
            except ValueError:
                pass
        value = min(candidates, key=lambda d: abs((d - stamp).total_seconds())) if candidates else None
    else:
        value = dateparser.parse(item.due_text, languages=['uk', 'ru', 'en'], settings={
            'RELATIVE_BASE': stamp.replace(tzinfo=None), 'TIMEZONE': tz,
            'RETURN_AS_TIMEZONE_AWARE': True, 'DATE_ORDER': 'DMY',
            'PREFER_DATES_FROM': 'current_period',
        })
    if value is None:
        return None
    if not item.has_time:
        value = value.replace(hour=23, minute=59, second=0, microsecond=0)
    return value.astimezone(UTC).isoformat()


class Store:
    def __init__(self, path=None):
        if path is None:
            STATE.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path or STATE / 'worker.db')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=10000;
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (
          key TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL,
          processed TEXT, attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0,
          error TEXT, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
          canonical TEXT PRIMARY KEY, task_id INTEGER NOT NULL,
          fields TEXT NOT NULL, sources TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS identities (identity TEXT PRIMARY KEY,task_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications (
          key TEXT PRIMARY KEY, task_id INTEGER NOT NULL, due TEXT NOT NULL,
          bucket TEXT NOT NULL, sent INTEGER DEFAULT 0, retry_at REAL DEFAULT 0);
        ''')

    def get(self, key, default=None):
        r = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value)))

    def ingest(self, source):
        digest = fingerprint(source)
        with self.db:
            self.db.execute('''INSERT INTO sources(key,digest,payload,updated) VALUES (?,?,?,?)
              ON CONFLICT(key) DO UPDATE SET digest=excluded.digest,payload=excluded.payload,
              updated=excluded.updated,attempts=0,retry_at=0,error=NULL
              WHERE digest != excluded.digest''',
              (source['key'], digest, json.dumps(source, ensure_ascii=False), time.time()))

    def pending(self, limit=8):
        return self.db.execute('''SELECT * FROM sources WHERE (processed IS NULL OR processed!=digest)
          AND retry_at<=? ORDER BY json_extract(payload,'$.messages[#-1].at') DESC,updated LIMIT ?''', (time.time(), limit)).fetchall()

    def succeeded(self, row):
        with self.db:
            self.db.execute('UPDATE sources SET processed=?,error=NULL WHERE key=? AND digest=?',
                            (row['digest'], row['key'], row['digest']))

    def failed(self, row, error):
        with self.db:
            self.db.execute('UPDATE sources SET attempts=attempts+1,error=?,retry_at=? WHERE key=?',
              (error[:160], time.time() + min(3600, 60 * 2 ** min(row['attempts'], 6)), row['key']))


def reminder_bucket(due, at, hours=(24, 3, 1)):
    remaining = (due - at).total_seconds() / 3600
    if remaining <= 0:
        return 'overdue' if remaining > -24 * 7 else None
    eligible = [h for h in hours if remaining <= h]
    return str(min(eligible)) if eligible else None
