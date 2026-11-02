"""Bot API only. Never sends messages through the reader's user session."""
from datetime import datetime, timedelta
import hmac
import html
import time
from zoneinfo import ZoneInfo

import httpx

from .core import CONFIG, STATE, config, secrets, now, save_json, fingerprint, reminder_bucket, read_json


class BotError(Exception):
    def __init__(self, retry_after=60, status=None):
        self.retry_after = retry_after
        self.status = status


def instant(value):
    if not value or value.startswith('0001-'):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


class Bot:
    def __init__(self, token):
        self.client = httpx.Client(base_url='https://api.telegram.org/bot' + token + '/', timeout=25)

    def call(self, method, **data):
        response = self.client.post(method, json=data)
        if not response.is_success:
            try:
                retry = response.json().get('parameters', {}).get('retry_after', 60)
            except Exception:
                retry = 60
            raise BotError(retry, response.status_code)
        result = response.json()
        if not result.get('ok'):
            raise BotError(status=result.get('error_code'))
        return result['result']


def private_task(api, task_id, cfg):
    task = api.task(task_id)
    if task['project_id'] != cfg['notification_project_id']:
        raise ValueError('task is outside the authorized personal project')
    return task


def poll_updates(api, store, bot):
    cfg = config()
    updates = bot.call('getUpdates', offset=store.get('bot_offset', 0), timeout=0,
                       allowed_updates=['message', 'callback_query'])
    for update in updates:
        message = update.get('message', {})
        callback = update.get('callback_query')
        chat = (callback or {}).get('message', message).get('chat', {})
        actor = (callback or message).get('from', {}).get('id')
        text = message.get('text', '')
        if chat.get('type') != 'private' or not isinstance(actor, int):
            store.set('bot_offset', update['update_id'] + 1)
            continue
        if not cfg.get('owner_telegram_id') and text.startswith('/start '):
            supplied = text.split(maxsplit=1)[1]
            expected = secrets().get('pairing_code', '')
            if expected and hmac.compare_digest(supplied, expected):
                cfg['owner_telegram_id'] = actor
                save_json(CONFIG / 'config.json', cfg)
                sec = secrets()
                sec.pop('pairing_code', None)
                save_json(CONFIG / 'secrets.json', sec)
                bot.call('sendMessage', chat_id=actor, text='Підключено! Нагадуватиму про дедлайни з твого особистого списку. Команди: /tasks, /status.')
        if actor == cfg.get('owner_telegram_id'):
            if callback:
                action, _, number = callback.get('data', '').partition(':')
                if action in ('done', 'snooze') and number.isdigit():
                    try:
                        task = private_task(api, int(number), cfg)
                    except (ValueError, httpx.HTTPStatusError) as exc:
                        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (403, 404):
                            raise
                        bot.call('answerCallbackQuery', callback_query_id=callback['id'], text='Завдання вже недоступне у твоєму списку.')
                        store.set('bot_offset', update['update_id'] + 1)
                        continue
                    if action == 'done':
                        api.update(task['id'], {'done': True})
                        answer = 'Позначено виконаним.'
                    else:
                        store.set('snooze:' + number, {'wake': (now() + timedelta(hours=1)).isoformat(), 'due': task.get('due_date')})
                        answer = 'Нагадаю за годину, якщо завдання ще не виконане.'
                    bot.call('answerCallbackQuery', callback_query_id=callback['id'], text=answer)
            elif text.split('@')[0] in ('/tasks', '/start'):
                tasks = [t for t in api.tasks(cfg['notification_project_id'], cfg['notification_view_id']) if not t.get('done')]
                tasks.sort(key=lambda t: t.get('due_date') if instant(t.get('due_date')) else '9999')
                body = '\n'.join('• ' + t['title'] for t in tasks[:15]) or 'Незавершених завдань немає.'
                bot.call('sendMessage', chat_id=actor, text=body[:4000])
            elif text == '/status':
                state = read_json(STATE / 'status.json', {})
                bot.call('sendMessage', chat_id=actor, text='Джерела: ' + str(state.get('telegram', {}).get('state', 'невідомо')) + '\nПовідомлень очікують розбору: ' + str(state.get('pending', 0)))
        store.set('bot_offset', update['update_id'] + 1)


def reminders(api, store, bot):
    cfg = config()
    owner = cfg.get('owner_telegram_id')
    if not owner:
        return
    tasks = api.tasks(cfg['notification_project_id'], cfg['notification_view_id'])
    for task in tasks:
        due = instant(task.get('due_date'))
        if task.get('done') or not due:
            continue
        snooze = store.get('snooze:' + str(task['id']))
        bucket = reminder_bucket(due, now(), cfg.get('reminder_hours', [24, 3, 1]))
        if snooze and snooze.get('due') == task.get('due_date'):
            if instant(snooze['wake']) > now():
                continue
            bucket = 'snooze:' + snooze['wake']
        if not bucket:
            continue
        key = fingerprint([task['id'], task['due_date'], bucket])
        with store.db:
            store.db.execute('INSERT OR IGNORE INTO notifications(key,task_id,due,bucket) VALUES (?,?,?,?)',
                             (key, task['id'], task['due_date'], bucket))
    queued = store.db.execute('SELECT * FROM notifications WHERE sent=0 AND retry_at<=?', (time.time(),)).fetchall()
    for row in queued[:10]:
        try:
            current = private_task(api, row['task_id'], cfg)
        except (ValueError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (403, 404):
                raise
            with store.db:
                store.db.execute('UPDATE notifications SET sent=2 WHERE key=?', (row['key'],))
            continue
        if current.get('done') or current.get('due_date') != row['due']:
            with store.db:
                store.db.execute('UPDATE notifications SET sent=2 WHERE key=?', (row['key'],))
            continue
        snooze = store.get('snooze:' + str(current['id']))
        if snooze and instant(snooze['wake']) > now():
            continue
        due = instant(current['due_date']).astimezone(ZoneInfo(cfg['timezone']))
        heading = 'Дедлайн минув' if due <= now() else 'Нагадування про дедлайн'
        message = f'⏰ <b>{heading}</b>\n{html.escape(current["title"])}\nДедлайн: {due:%d.%m.%Y %H:%M} (Київ)'
        buttons = {'inline_keyboard': [[{'text': '✅ Виконано', 'callback_data': f'done:{current["id"]}'},
                                       {'text': 'Через годину', 'callback_data': f'snooze:{current["id"]}'}],
                                      [{'text': 'Відкрити', 'url': cfg['url'] + f'/tasks/{current["id"]}'}]]}
        try:
            bot.call('sendMessage', chat_id=owner, text=message, parse_mode='HTML', reply_markup=buttons)
        except BotError as exc:
            with store.db:
                store.db.execute('UPDATE notifications SET retry_at=? WHERE key=?', (time.time() + exc.retry_after, row['key']))
            continue
        with store.db:
            store.db.execute('UPDATE notifications SET sent=1 WHERE key=?', (row['key'],))
        if row['bucket'].startswith('snooze:'):
            store.set('snooze:' + str(current['id']), None)
