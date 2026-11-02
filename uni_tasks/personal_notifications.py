"""One Bot API poller, personal-project authorization for every recipient/action."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import html
import json
import logging
import os
import re
import time

import httpx

from .core import fingerprint, now, reminder_bucket
from .notification_links import Links, LinkError
from .notifications import Bot, BotError, instant
from .replication import API
from .campus import Campus, DEFAULTS, KYIV, quiet_at, task_course

KEYBOARD = {'keyboard': [[{'text': '📚 Мої завдання'}, {'text': '⏰ Найближчі дедлайни'}],
                         [{'text': '🌐 Відкрити сайт'}, {'text': '⚙️ Налаштування'}]],
            'resize_keyboard': True, 'is_persistent': True, 'input_field_placeholder': 'Обери дію в меню'}
BUTTON_COMMANDS = {'📚 Мої завдання': '/tasks', '⏰ Найближчі дедлайни': '/upcoming',
                   '🌐 Відкрити сайт': '/site', '⚙️ Налаштування': '/settings'}


class Notifications:
    def __init__(self, catalog, links, api, bot, clock=now, campus=None):
        self.catalog, self.links, self.api, self.bot, self.clock = catalog, links, api, bot, clock
        self.students = {s['user_id']: s for s in catalog['students']}
        self.campus=campus

    def preferences(self,binding):
        return self.campus.preferences(binding['user_id']) if self.campus else DEFAULTS

    def variant_label(self,task,binding):
        if not self.campus:return ''
        course=task_course(task,self.catalog.get('courses',[]))
        variant=self.campus.variants(binding['user_id']).get(course['id'],'') if course else ''
        return ' · Варіант '+variant if variant else ''

    def digests(self):
        if not self.campus:return 0
        errors=0
        for binding in self.links.all_bindings():
            try:
                at=self.clock()
                if not self.campus.digest_due(binding['user_id'],at):continue
                pid=self.project(binding)
                tasks=self.api.tasks(pid)
                if any(t.get('project_id')!=pid for t in tasks):raise ValueError('Unexpected task scope')
                today=at.astimezone(KYIV).date()
                groups={'Сьогодні':[],'Завтра':[],'Прострочені':[]}
                muted=self.campus.muted(binding['user_id'])
                for task in sorted(tasks,key=lambda t:t.get('due_date') or '9999'):
                    due=instant(task.get('due_date'))
                    if not due or task.get('done') or task['id'] in muted:continue
                    date=due.astimezone(KYIV).date()
                    group='Прострочені' if date<today else 'Сьогодні' if date==today else 'Завтра' if date==today+timedelta(days=1) else None
                    if group:groups[group].append(task)
                parts=['☀️ <b>Мій навчальний день</b> · '+today.strftime('%d.%m')]
                for label,items in groups.items():
                    parts.append(f'<b>{label}: {len(items)}</b>')
                    for task in items[:5]:
                        title=html.escape(task['title'][:120]+self.variant_label(task,binding))
                        parts.append(f'• <a href="{self.catalog["public_url"]}/tasks/{task["id"]}">{title}</a>')
                    if len(items)>5:parts.append(f'Ще {len(items)-5} — на сайті.')
                if self.campus.digest_due(binding['user_id'],self.clock()) and self.send(binding,text='\n'.join(parts),parse_mode='HTML',
                    link_preview_options={'is_disabled':True}):self.campus.digest_sent(binding['user_id'],at)
            except (BotError,httpx.HTTPError,ValueError,KeyError):errors+=1
        return errors

    def project(self, binding):
        student = self.students.get(binding['user_id'])
        if not student:
            raise ValueError('Unknown roster member')
        pid = student['personal_project_id']
        project = self.api.project(pid)
        if project.get('owner', {}).get('id') != binding['user_id'] or project.get('parent_project_id'):
            raise ValueError('Personal project owner mismatch')
        return pid

    def task(self, binding, task_id):
        pid = self.project(binding)
        task = self.api.get(task_id)
        if task.get('project_id') != pid:
            raise ValueError('Task outside personal list')
        return task

    def current(self, binding):
        current = self.links.binding(user_id=binding['user_id'])
        return bool(current and current['version'] == binding['version'])

    def send(self, binding, method='sendMessage', **data):
        if not self.current(binding):
            return False
        try:
            data.setdefault('reply_markup', KEYBOARD)
            self.bot.call(method, chat_id=binding['telegram_id'], **data)
        except BotError as exc:
            if exc.status == 403:
                self.links.disconnect(binding['user_id'], binding['version'])
            raise
        return True

    def welcome(self, binding):
        hours=', '.join(str(h) for h in self.preferences(binding)['reminder_hours'])
        text = ('Tasks · Навчальні завдання\n\nTelegram підключено. Інтервали нагадувань у годинах: '+hours+'. '
                'Користуйся кнопками внизу — команди вводити не потрібно.\n\nНезалежний студентський проєкт.')
        photo = self.catalog.get('bot_image_file_id')
        if photo:
            self.send(binding, method='sendPhoto', photo=photo, caption=text)
        else:
            self.send(binding, text=text)

    def task_list(self, binding, mode='all', page=0, message_id=None):
        pid = self.project(binding)
        tasks = [t for t in self.api.tasks(pid) if t.get('project_id') == pid and not t.get('done')]
        if mode == 'upcoming':
            tasks = [t for t in tasks if instant(t.get('due_date')) and
                     self.clock() <= instant(t['due_date']) <= self.clock() + timedelta(days=7)]
        tasks.sort(key=lambda t: (t.get('due_date') if instant(t.get('due_date')) else '9999', t['id']))
        pages = max(1, (len(tasks) + 7) // 8)
        page = min(page, pages - 1)
        title = '⏰ Дедлайни на найближчі 7 днів' if mode == 'upcoming' else '📚 Мої незавершені завдання'
        parts = ['<b>' + title + '</b>']
        for i, task in enumerate(tasks[page * 8:page * 8 + 8], start=page * 8 + 1):
            label = html.escape(task['title'][:155] + ('…' if len(task['title']) > 155 else ''))
            due = instant(task.get('due_date'))
            date = due.astimezone(ZoneInfo('Europe/Kyiv')).strftime('%d.%m · %H:%M') if due else 'Без дедлайну'
            date += html.escape(self.variant_label(task,binding))
            parts.append(f'{i}. <a href="{self.catalog["public_url"]}/tasks/{task["id"]}">{label}</a>\n    {date}')
        if not tasks:
            parts.append('Немає дедлайнів на найближчі 7 днів.' if mode == 'upcoming' else 'Незавершених завдань немає.')
        else:
            parts.append(f'Сторінка {page + 1} із {pages} · Завдань: {len(tasks)}')
        navigation = []
        if page:
            navigation.append({'text': '← Назад', 'callback_data': f'list:{mode}:{page - 1}'})
        if page + 1 < pages:
            navigation.append({'text': 'Далі →', 'callback_data': f'list:{mode}:{page + 1}'})
        markup = {'inline_keyboard': ([navigation] if navigation else []) + [[
            {'text': 'Відкрити мій список', 'url': self.catalog['public_url'] + f'/projects/{pid}'}]]}
        data = {'text': '\n\n'.join(parts), 'parse_mode': 'HTML', 'link_preview_options': {'is_disabled': True},
                'reply_markup': markup}
        if message_id and self.current(binding):
            self.bot.call('editMessageText', chat_id=binding['telegram_id'], message_id=message_id, **data)
        else:
            self.send(binding, **data)

    def process_update(self, update):
        message = update.get('message', {})
        callback = update.get('callback_query')
        chat = (callback or {}).get('message', message).get('chat', {})
        actor = (callback or message).get('from', {})
        actor_id = actor.get('id')
        if chat.get('type') != 'private' or type(actor_id) is not int or actor_id != chat.get('id'):
            return
        text = message.get('text', '').strip()
        text = BUTTON_COMMANDS.get(text, text)
        command, _, parameter = text.partition(' ')
        command = command.split('@')[0]
        if not callback and command == '/start' and parameter:
            try:
                binding = self.links.consume(parameter, actor_id, actor.get('username'))
                if binding['user_id'] not in self.students:
                    self.links.disconnect(binding['user_id'], binding['version'])
                    raise LinkError('unknown_user')
            except LinkError:
                self.bot.call('sendMessage', chat_id=actor_id,
                    text='Посилання недійсне, застаріло або цей Telegram уже підключений до іншого профілю. '
                         'Відкрий сторінку Telegram-сповіщень у своєму профілі та створи нове посилання: '
                         + self.catalog['public_url'] + '/telegram')
                return
            self.welcome(binding)
            return
        binding = self.links.binding(telegram_id=actor_id)
        if not binding or binding['user_id'] not in self.students:
            if callback:
                self.bot.call('answerCallbackQuery', callback_query_id=callback['id'], text='Спочатку підключи свій профіль.')
            elif command in ('/start', '/help', '/status', '/tasks', '/settings', '/upcoming', '/site'):
                self.bot.call('sendMessage', chat_id=actor_id, text='Підключи Telegram зі свого профілю: '
                              + self.catalog['public_url'] + '/telegram')
            return
        if callback:
            data = callback.get('data', '')
            pagination = re.fullmatch(r'list:(all|upcoming):(\d{1,4})', data)
            if pagination:
                self.bot.call('answerCallbackQuery', callback_query_id=callback['id'])
                self.task_list(binding, pagination[1], int(pagination[2]), callback.get('message', {}).get('message_id'))
                return
            if data in ('settings:stop', 'settings:disconnect', 'menu:home'):
                self.bot.call('answerCallbackQuery', callback_query_id=callback['id'])
                if data == 'settings:stop':
                    self.send(binding, text='Відключити нагадування? Завдання та прогрес залишаться.', reply_markup={
                        'inline_keyboard': [[{'text': 'Так, відключити', 'callback_data': 'settings:disconnect'},
                                             {'text': 'Залишити', 'callback_data': 'menu:home'}]]})
                elif data == 'settings:disconnect':
                    self.links.disconnect(binding['user_id'], binding['version'])
                    self.bot.call('sendMessage', chat_id=actor_id, text='Сповіщення відключено.',
                                  reply_markup={'remove_keyboard': True})
                else:
                    self.send(binding, text='Сповіщення залишаються увімкненими. Обери дію в меню.')
                return
            action, _, number = callback.get('data', '').partition(':')
            if action not in ('done', 'snooze') or not number.isdigit() or len(number) > 16:
                return
            try:
                task = self.task(binding, int(number))
            except (ValueError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (403, 404, 410):
                    raise
                self.bot.call('answerCallbackQuery', callback_query_id=callback['id'],
                              text='Завдання недоступне у твоєму особистому списку.')
                return
            if not self.current(binding):
                return
            if action == 'done':
                if not task.get('done'):
                    self.api.request('PATCH', f'/tasks/{task["id"]}', json={'done': True})
                answer = 'Позначено виконаним тільки у твоєму списку.'
            elif task.get('done') or not instant(task.get('due_date')):
                answer = 'Завдання вже виконано або дедлайн не заданий.'
            else:
                with self.links.connection() as db:
                    db.execute('INSERT OR REPLACE INTO snoozes VALUES (?,?,?,?)',
                        (binding['user_id'], task['id'], task['due_date'],
                         (self.clock() + timedelta(hours=1)).isoformat()))
                answer = 'Нагадаю через годину, якщо завдання ще актуальне.'
            self.bot.call('answerCallbackQuery', callback_query_id=callback['id'], text=answer)
        elif command == '/stop':
            self.links.disconnect(binding['user_id'], binding['version'])
            self.bot.call('sendMessage', chat_id=actor_id, text='Сповіщення відключено. Завдання залишилися у твоєму списку.',
                          reply_markup={'remove_keyboard': True})
        elif command == '/start':
            self.welcome(binding)
        elif command in ('/tasks', '/upcoming'):
            self.task_list(binding, 'upcoming' if command == '/upcoming' else 'all')
        elif command == '/site':
            pid = self.project(binding)
            self.send(binding, text='Відкрий свій особистий список:', reply_markup={'inline_keyboard': [[
                {'text': '🌐 Мої завдання на сайті', 'url': self.catalog['public_url'] + f'/projects/{pid}'}]]})
        elif command in ('/status', '/help', '/settings'):
            self.send(binding, text='Telegram підключено до твого особистого списку. Інтервали нагадувань у годинах: '+
                ', '.join(str(h) for h in self.preferences(binding)['reminder_hours'])+
                '. Час — Київ. Дайджест, тихі години й нагадування для окремих завдань налаштовуються на сайті.', reply_markup={
                    'inline_keyboard': [[{'text': 'Налаштування на сайті', 'url': self.catalog['public_url'] + '/telegram'}],
                                        [{'text': 'Відключити сповіщення', 'callback_data': 'settings:stop'}]]})

    def poll(self):
        errors = 0
        updates = self.bot.call('getUpdates', offset=self.links.get('offset', 0), timeout=0,
                                allowed_updates=['message', 'callback_query'])
        for update in updates:
            try:
                self.process_update(update)
            except (BotError, httpx.HTTPError, ValueError, KeyError):
                errors += 1
            # A malformed/failed command must not block every other student.
            self.links.set('offset', update['update_id'] + 1)
        return errors

    def bucket(self, task, binding):
        due = instant(task.get('due_date'))
        if task.get('done') or not due:
            return None
        at = self.clock()
        prefs=self.preferences(binding)
        if quiet_at(prefs,at) or (self.campus and task['id'] in self.campus.muted(binding['user_id'])):
            return None
        with self.links.connection() as db:
            snooze = db.execute('SELECT * FROM snoozes WHERE user_id=? AND task_id=?',
                               (binding['user_id'], task['id'])).fetchone()
        if snooze and snooze['due'] == task['due_date']:
            return 'snooze:' + snooze['wake'] if instant(snooze['wake']) <= at else None
        value = reminder_bucket(due, at, hours=prefs['reminder_hours'])
        # Connecting must not dump a week's already-overdue work into a new chat.
        if value == 'overdue' and due.timestamp() < binding['linked_at']:
            return None
        return value

    def reminders(self):
        errors = 0
        for binding in self.links.all_bindings():
            try:
                pid = self.project(binding)
                tasks = self.api.tasks(pid)
                if any(t.get('project_id') != pid for t in tasks):
                    raise ValueError('Unexpected task scope')
                for task in tasks:
                    bucket = self.bucket(task, binding)
                    if not bucket:
                        continue
                    key = fingerprint([binding['user_id'], task['id'], task['due_date'], bucket])
                    with self.links.connection() as db:
                        db.execute('''INSERT INTO outbox(key,user_id,task_id,due,bucket,version) VALUES (?,?,?,?,?,?)
                            ON CONFLICT(key) DO UPDATE SET sent=0,version=excluded.version WHERE outbox.sent=2''',
                            (key, binding['user_id'], task['id'], task['due_date'], bucket, binding['version']))
            except (httpx.HTTPError, ValueError, KeyError):
                errors += 1
        with self.links.connection() as db:
            queued = [dict(r) for r in db.execute('SELECT * FROM outbox WHERE sent=0 AND retry_at<=? ORDER BY retry_at,key LIMIT 10',
                                                 (time.time(),))]
        for row in queued:
            binding = self.links.binding(user_id=row['user_id'])
            try:
                if not binding or binding['version'] != row['version']:
                    self.finish(row, 2)
                    continue
                try:
                    task = self.task(binding, row['task_id'])
                except (ValueError, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (403, 404, 410):
                        raise
                    self.finish(row, 2)
                    continue
                bucket = self.bucket(task, binding)
                if task.get('due_date') != row['due'] or bucket != row['bucket']:
                    self.finish(row, 2)
                    continue
                due = instant(task['due_date']).astimezone(ZoneInfo('Europe/Kyiv'))
                heading = 'Дедлайн минув' if due <= self.clock() else 'Нагадування про дедлайн'
                text = f'⏰ <b>{heading}</b>\n{html.escape(task["title"][:1000])}\nДедлайн: {due:%d.%m.%Y %H:%M} (Київ)'
                text += html.escape(self.variant_label(task,binding))
                buttons = {'inline_keyboard': [[{'text': '✅ Виконано', 'callback_data': f'done:{task["id"]}'},
                    {'text': 'Через годину', 'callback_data': f'snooze:{task["id"]}'}],
                    [{'text': 'Відкрити моє завдання', 'url': self.catalog['public_url'] + f'/tasks/{task["id"]}'}]]}
                if self.send(binding, text=text, parse_mode='HTML', reply_markup=buttons):
                    self.finish(row, 1)
                    if row['bucket'].startswith('snooze:'):
                        with self.links.connection() as db:
                            db.execute('DELETE FROM snoozes WHERE user_id=? AND task_id=?', (row['user_id'], row['task_id']))
                            normal = reminder_bucket(instant(task['due_date']), self.clock(),hours=self.preferences(binding)['reminder_hours'])
                            if normal:
                                key = fingerprint([row['user_id'], task['id'], task['due_date'], normal])
                                db.execute('INSERT OR REPLACE INTO outbox VALUES (?,?,?,?,?,?,1,0)',
                                    (key, row['user_id'], task['id'], task['due_date'], normal, binding['version']))
            except BotError as exc:
                with self.links.connection() as db:
                    db.execute('UPDATE outbox SET retry_at=? WHERE key=?', (time.time() + max(1, exc.retry_after), row['key']))
                if exc.status != 403:
                    errors += 1
            except (httpx.HTTPError, ValueError, KeyError):
                errors += 1
                with self.links.connection() as db:
                    db.execute('UPDATE outbox SET retry_at=? WHERE key=?', (time.time() + 60, row['key']))
        return errors

    def finish(self, row, status):
        with self.links.connection() as db:
            db.execute('UPDATE outbox SET sent=? WHERE key=?', (status, row['key']))


def main():
    import fcntl
    os.umask(0o077)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    config = Path(os.getenv('NOTIFICATION_CONFIG', '/notification-config'))
    state = Path(os.getenv('NOTIFICATION_STATE', '/notification-state'))
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        catalog = json.loads((config / 'catalog.json').read_text('utf-8'))
        links = Links(state / 'telegram.db')
        api = API(catalog['vikunja_api'], (config / 'vikunja-token.txt').read_text('utf-8').strip())
        bot = Bot((config / 'bot-token.txt').read_text('utf-8').strip())
        service = Notifications(catalog, links, api, bot, campus=Campus())
        next_reminder = 0
        reminder_errors = 0
        while True:
            errors = 0
            try:
                errors += service.poll()
                if time.monotonic() >= next_reminder:
                    reminder_errors = service.reminders() + service.digests()
                    next_reminder = time.monotonic() + 20
            except Exception:
                errors += 1
            status = {'checked_at': datetime.now(timezone.utc).isoformat(), 'errors': errors + reminder_errors,
                      'connected_users': len(links.all_bindings())}
            (state / 'status.next').write_text(json.dumps(status), 'utf-8')
            (state / 'status.next').replace(state / 'status.json')
            service.campus.status('notifications',status)
            if errors:
                print('Notification cycle had errors; credentials and message contents omitted.', flush=True)
            time.sleep(3)


if __name__ == '__main__':
    main()
