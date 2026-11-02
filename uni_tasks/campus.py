"""Application metadata only: no Telegram sessions, OAuth tokens or mail bodies."""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json
import os
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

KYIV = ZoneInfo('Europe/Kyiv')
DEFAULTS = {'reminder_hours':[24,3,1], 'quiet_enabled':False, 'quiet_start':'22:00', 'quiet_end':'08:00',
            'digest_enabled':False, 'digest_time':'08:00'}


def validate_preferences(value):
    if not isinstance(value,dict) or set(value) != set(DEFAULTS):
        raise ValueError('Передай всі налаштування сповіщень без зайвих полів.')
    hours=value['reminder_hours']
    if not isinstance(hours,list) or not 1<=len(hours)<=5 or any(type(h) is not int or not 1<=h<=168 for h in hours):
        raise ValueError('Обери від 1 до 5 інтервалів, кожен від 1 до 168 годин.')
    for key in ('quiet_enabled','digest_enabled'):
        if type(value[key]) is not bool: raise ValueError('Некоректний перемикач.')
    for key in ('quiet_start','quiet_end','digest_time'):
        if not isinstance(value[key],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value[key]):
            raise ValueError('Час має бути у форматі ГГ:ХХ.')
    if value['quiet_enabled'] and value['quiet_start']==value['quiet_end']:
        raise ValueError('Початок і кінець тихих годин мають відрізнятися.')
    return value | {'reminder_hours':sorted(set(hours),reverse=True)}


def quiet_at(preferences, at):
    if not preferences['quiet_enabled']: return False
    clock=at.astimezone(KYIV).strftime('%H:%M')
    start,end=preferences['quiet_start'],preferences['quiet_end']
    return start<=clock<end if start<end else clock>=start or clock<end


def task_course(task,courses):
    match=re.search(r'<!-- pi42-course:([a-z0-9-]+) -->',task.get('description') or '')
    if match:
        return next((c for c in courses if c['id']==match[1]),None)
    title=task.get('title','').removeprefix('[Перевірити] ')
    matches=[c for c in courses if title.startswith(c['title']+':')]
    return matches[0] if len(matches)==1 else None


class Campus:
    def __init__(self,path=None):
        self.path=Path(path or Path(os.getenv('CAMPUS_STATE','/campus-state'))/'campus.db')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS preferences(user_id INTEGER PRIMARY KEY,payload TEXT NOT NULL,digest_since REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS variants(user_id INTEGER,course TEXT,value TEXT,PRIMARY KEY(user_id,course));
                CREATE TABLE IF NOT EXISTS muted(user_id INTEGER,task_id INTEGER,PRIMARY KEY(user_id,task_id));
                CREATE TABLE IF NOT EXISTS digests(user_id INTEGER,day TEXT,sent_at REAL,PRIMARY KEY(user_id,day));
                CREATE TABLE IF NOT EXISTS status(name TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews(task_id INTEGER PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'open',version TEXT NOT NULL);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def preferences(self,uid):
        with self.db() as db: row=db.execute('SELECT payload FROM preferences WHERE user_id=?',(uid,)).fetchone()
        return DEFAULTS | (json.loads(row[0]) if row else {})

    def save_preferences(self,uid,value,at=None):
        value=validate_preferences(value)
        with self.db() as db:
            row=db.execute('SELECT * FROM preferences WHERE user_id=?',(uid,)).fetchone()
            was_enabled=row and json.loads(row['payload'])['digest_enabled']
            since=row['digest_since'] if row else 0
            if value['digest_enabled'] and not was_enabled: since=time.time() if at is None else at
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?,?)',(uid,json.dumps(value),since))
        return value

    def variants(self,uid):
        with self.db() as db:return dict(db.execute('SELECT course,value FROM variants WHERE user_id=?',(uid,)))

    def variant(self,uid,course,value):
        if not isinstance(value,str) or len(value.strip())>32 or any(ord(c)<32 for c in value):
            raise ValueError('Варіант — до 32 символів.')
        with self.db() as db:db.execute('INSERT OR REPLACE INTO variants VALUES (?,?,?)',(uid,course,value.strip()))
        return value.strip()

    def muted(self,uid):
        with self.db() as db:return {r[0] for r in db.execute('SELECT task_id FROM muted WHERE user_id=?',(uid,))}

    def mute(self,uid,tid,muted):
        with self.db() as db:
            if muted:db.execute('INSERT OR IGNORE INTO muted VALUES (?,?)',(uid,tid))
            else:db.execute('DELETE FROM muted WHERE user_id=? AND task_id=?',(uid,tid))

    def digest_due(self,uid,at):
        prefs=self.preferences(uid)
        if not prefs['digest_enabled'] or quiet_at(prefs,at):return False
        local=at.astimezone(KYIV)
        hh,mm=map(int,prefs['digest_time'].split(':'))
        target=local.replace(hour=hh,minute=mm,second=0,microsecond=0)
        if local<target:return False
        with self.db() as db:
            row=db.execute('SELECT digest_since FROM preferences WHERE user_id=?',(uid,)).fetchone()
            sent=db.execute('SELECT 1 FROM digests WHERE user_id=? AND day=?',(uid,local.date().isoformat())).fetchone()
        return bool(row and row[0]<=target.timestamp() and not sent)

    def digest_sent(self,uid,at):
        with self.db() as db:db.execute('INSERT OR IGNORE INTO digests VALUES (?,?,?)',(uid,at.astimezone(KYIV).date().isoformat(),at.timestamp()))

    def status(self,name,value):
        with self.db() as db:db.execute('INSERT OR REPLACE INTO status VALUES (?,?)',(name,json.dumps(value)))

    def review(self,tid,payload,version):
        with self.db() as db:
            db.execute('''INSERT INTO reviews VALUES (?,?,'open',?) ON CONFLICT(task_id) DO UPDATE SET
                payload=excluded.payload,state='open',version=excluded.version WHERE version!=excluded.version''',
                (tid,json.dumps(payload,ensure_ascii=False),version))

    def panel(self):
        with self.db() as db:
            return {'services':{r[0]:json.loads(r[1]) for r in db.execute('SELECT * FROM status')},
                'reviews':[json.loads(r['payload']) | {'task_id':r['task_id'],'version':r['version']}
                           for r in db.execute("SELECT * FROM reviews WHERE state='open' ORDER BY task_id DESC LIMIT 100")]}
