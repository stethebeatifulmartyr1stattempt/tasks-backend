from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

import httpx

from uni_tasks.core import now, fingerprint
from uni_tasks.enrollment import handler_for
from uni_tasks.notification_links import Links, LinkError
from uni_tasks.personal_notifications import Notifications
from uni_tasks.notifications import BotError
import test_enrollment


class FakeBot:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.fail = None

    def call(self, method, **data):
        if self.fail and method == 'sendMessage':
            raise self.fail
        self.calls.append((method, data))
        return self.updates if method == 'getUpdates' else {'message_id': len(self.calls)}


class FakeAPI:
    def __init__(self, at):
        due = (at + timedelta(hours=2)).isoformat()
        self.data = {11: {'id': 11, 'project_id': 4, 'title': 'Student A', 'due_date': due, 'done': False},
                     22: {'id': 22, 'project_id': 6, 'title': 'Student B', 'due_date': due, 'done': False},
                     33: {'id': 33, 'project_id': 30, 'title': 'Shared master', 'due_date': due, 'done': False}}
        self.writes = []

    def project(self, pid):
        return {'owner': {'id': {4: 2, 6: 3, 30: 1}[pid]}}

    def tasks(self, pid):
        return deepcopy([t for t in self.data.values() if t['project_id'] == pid])

    def get(self, task_id):
        if task_id not in self.data:
            httpx.Response(404, request=httpx.Request('GET', 'https://test/task')).raise_for_status()
        return deepcopy(self.data[task_id])

    def request(self, method, path, json):
        assert method == 'PATCH'
        assert json == {'done': True}
        tid = int(path.rsplit('/', 1)[1])
        self.writes.append(tid)
        self.data[tid].update(json)


def message(actor, text, private=True):
    return {'update_id': 1, 'message': {'chat': {'id': actor, 'type': 'private' if private else 'group'},
                                     'from': {'id': actor, 'username': 'user' + str(actor)}, 'text': text}}


def callback(actor, data):
    return {'update_id': 2, 'callback_query': {'id': 'callback-id', 'from': {'id': actor}, 'data': data,
        'message': {'chat': {'id': actor, 'type': 'private'}}}}


class PersonalNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.links = Links(Path(self.tmp.name) / 'telegram.db')
        self.at = now()
        self.catalog = {'public_url': 'https://tasks.example', 'students': [
            {'user_id': 2, 'username': 'a', 'personal_project_id': 4},
            {'user_id': 3, 'username': 'b', 'personal_project_id': 6}]}
        self.bot = FakeBot()
        self.api = FakeAPI(self.at)
        self.service = Notifications(self.catalog, self.links, self.api, self.bot, lambda: self.at)
        for uid, actor in [(2, 200), (3, 300)]:
            code, _ = self.links.issue(uid)
            self.links.consume(code, actor)

    def sent(self):
        return [d for m, d in self.bot.calls if m == 'sendMessage']

    def test_pairing_is_hashed_single_use_expiring_and_reissued(self):
        first, _ = self.links.issue(2, at=100)
        second, _ = self.links.issue(2, at=101)
        with self.links.connection() as db:
            stored = db.execute('SELECT digest FROM pairing WHERE user_id=2').fetchone()[0]
            self.assertNotIn(second, stored)
        for code, at in [(first, 102), (second, 701)]:
            with self.assertRaises(LinkError):
                self.links.consume(code, 201, at=at)
        self.links.consume(second, 201, at=700)
        with self.assertRaises(LinkError):
            self.links.consume(second, 202, at=700)

    def test_telegram_cannot_steal_another_profile(self):
        code, _ = self.links.issue(2)
        with self.assertRaises(LinkError):
            self.links.consume(code, 300)
        self.assertEqual(self.links.binding(telegram_id=300)['user_id'], 3)
        self.assertEqual(self.links.binding(user_id=2)['telegram_id'], 200)

    def test_group_and_different_actor_cannot_consume_link(self):
        code, _ = self.links.issue(2)
        self.service.process_update(message(201, '/start ' + code, private=False))
        forged = message(201, '/start ' + code)
        forged['message']['chat']['id'] = 999
        self.service.process_update(forged)
        self.assertEqual(self.links.binding(user_id=2)['telegram_id'], 200)
        self.service.process_update(message(201, '/start ' + code))
        self.assertEqual(self.links.binding(user_id=2)['telegram_id'], 201)

    def test_reminders_only_personal_and_no_repeated_bucket(self):
        self.assertEqual(self.service.reminders(), 0)
        self.assertEqual(sorted((m['chat_id'], 'Student A' in m['text']) for m in self.sent()), [(200, True), (300, False)])
        self.assertTrue(all('Shared master' not in m['text'] for m in self.sent()))
        self.service.reminders()
        self.assertEqual(len(self.sent()), 2)
        self.at += timedelta(hours=1, minutes=10)
        self.service.reminders()
        self.assertEqual(len(self.sent()), 4)

    def test_done_callback_cannot_touch_other_or_shared_tasks(self):
        for tid in (22, 33):
            self.service.process_update(callback(200, f'done:{tid}'))
        self.assertEqual(self.api.writes, [])
        self.service.process_update(callback(200, 'done:11'))
        self.assertEqual(self.api.writes, [11])
        self.assertFalse(self.api.data[22]['done'])
        self.assertFalse(self.api.data[33]['done'])
        self.service.reminders()
        self.assertEqual([m['chat_id'] for m in self.sent()], [300])

    def test_disconnect_invalidates_link_queue_and_old_callback(self):
        code, _ = self.links.issue(2)
        self.bot.fail = BotError(60, 429)
        self.service.reminders()
        self.links.disconnect(2)
        with self.assertRaises(LinkError):
            self.links.consume(code, 201)
        self.bot.fail = None
        self.service.process_update(callback(200, 'done:11'))
        self.assertEqual(self.api.writes, [])
        with self.links.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox WHERE user_id=2 AND sent=0').fetchone()[0], 0)

    def test_completed_changed_and_moved_tasks_cancel_retries(self):
        self.bot.fail = BotError(60, 429)
        self.service.reminders()
        self.bot.fail = None
        self.api.data[11]['done'] = True
        self.api.data[22]['project_id'] = 30
        with self.links.connection() as db:
            db.execute('UPDATE outbox SET retry_at=0')
        self.service.reminders()
        self.assertEqual(self.sent(), [])

    def test_changed_due_rechecks_current_task_before_delivery(self):
        self.bot.fail = BotError(60, 429)
        self.service.reminders()
        self.bot.fail = None
        self.api.data[11]['due_date'] = (self.at + timedelta(days=10)).isoformat()
        self.api.data[22]['done'] = True
        with self.links.connection() as db:
            db.execute('UPDATE outbox SET retry_at=0')
        self.service.reminders()
        self.assertEqual(self.sent(), [])

    def test_blocked_bot_disconnects_only_current_binding(self):
        self.bot.fail = BotError(60, 403)
        self.service.reminders()
        self.assertFalse(self.links.status(2)['connected'])
        self.assertFalse(self.links.status(3)['connected'])

    def test_snooze_waits_and_delivers_once_without_immediate_repeat(self):
        self.service.reminders()
        self.service.process_update(callback(200, 'snooze:11'))
        self.at += timedelta(minutes=50)
        self.service.reminders()
        self.assertEqual(len([m for m in self.sent() if m['chat_id'] == 200]), 1)
        self.at += timedelta(minutes=11)
        self.service.reminders()
        self.service.reminders()
        self.assertEqual(len([m for m in self.sent() if m['chat_id'] == 200]), 2)

    def test_new_connection_does_not_send_existing_overdue_tasks(self):
        self.api.data[11]['due_date'] = (self.at - timedelta(days=1)).isoformat()
        self.api.data[22]['due_date'] = '0001-01-01T00:00:00Z'
        self.service.reminders()
        self.assertEqual(self.sent(), [])

    def test_tasks_command_and_stop_are_per_user(self):
        self.service.process_update(message(200, '/tasks'))
        self.assertIn('Student A', self.sent()[0]['text'])
        self.assertNotIn('Student B', self.sent()[0]['text'])
        self.service.process_update(message(200, '/stop'))
        self.assertFalse(self.links.status(2)['connected'])
        self.assertTrue(self.links.status(3)['connected'])

    def test_offset_persists_across_worker_restart(self):
        self.bot.updates = [message(200, '/status')]
        self.bot.updates[0]['update_id'] = 500
        self.service.poll()
        restarted = Links(self.links.path)
        self.assertEqual(restarted.get('offset'), 501)
        self.assertTrue(restarted.status(2)['connected'])

    def test_keyboard_buttons_route_without_typed_commands(self):
        self.service.process_update(message(200, '/start'))
        keyboard = self.sent()[0]['reply_markup']
        self.assertTrue(keyboard['is_persistent'])
        self.assertEqual(len(keyboard['keyboard']), 2)
        for button in ['📚 Мої завдання', '⏰ Найближчі дедлайни', '🌐 Відкрити сайт', '⚙️ Налаштування']:
            before = len(self.sent())
            self.service.process_update(message(200, button))
            self.assertGreater(len(self.sent()), before)
        self.assertTrue(all(m['chat_id'] == 200 for m in self.sent()))

    def test_task_pagination_is_personal_and_edits_same_message(self):
        for tid in range(40, 50):
            self.api.data[tid] = self.api.data[11] | {'id': tid, 'title': f'Лабораторна {tid}'}
        self.service.process_update(message(200, '📚 Мої завдання'))
        self.assertIn('Сторінка 1 із 2', self.sent()[0]['text'])
        self.assertNotIn('Student B', self.sent()[0]['text'])
        update = callback(200, 'list:all:1')
        update['callback_query']['message']['message_id'] = 42
        self.service.process_update(update)
        edits = [d for m, d in self.bot.calls if m == 'editMessageText']
        self.assertEqual(edits[0]['message_id'], 42)
        self.assertIn('Сторінка 2 із 2', edits[0]['text'])

    def test_settings_disconnect_needs_confirmation_button(self):
        self.service.process_update(callback(200, 'settings:stop'))
        self.assertTrue(self.links.status(2)['connected'])
        self.service.process_update(callback(200, 'menu:home'))
        self.assertTrue(self.links.status(2)['connected'])
        self.service.process_update(callback(200, 'settings:disconnect'))
        self.assertFalse(self.links.status(2)['connected'])
        self.assertTrue(self.links.status(3)['connected'])

    def test_welcome_uses_kpi_picture_when_configured(self):
        self.catalog['bot_image_file_id'] = 'telegram-photo-id'
        self.service.process_update(message(200, '/start'))
        photo = next(d for m, d in self.bot.calls if m == 'sendPhoto')
        self.assertEqual(photo['photo'], 'telegram-photo-id')
        self.assertTrue(photo['reply_markup']['is_persistent'])


class TelegramEndpointTests(unittest.TestCase):
    setUp = test_enrollment.EnrollmentTests.setUp
    def test_telegram_endpoints_auth_owner_target_and_empty_body(self):
        with TemporaryDirectory() as tmp:
            self.service.links = Links(Path(tmp) / 'links.db')
            self.service.catalog['telegram_bot_username'] = 'Example Student'
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}') as client:
                    headers = {'Authorization': 'Bearer student-session'}
                    self.assertEqual(client.get('/api/v2/campus/telegram').status_code, 401)
                    self.assertEqual(client.post('/api/v2/campus/telegram/link', headers=headers,
                                                 json={'user_id': 1}).status_code, 422)
                    link = client.post('/api/v2/campus/telegram/link', headers=headers)
                    self.assertEqual(link.status_code, 200)
                    self.assertEqual(link.headers['Cache-Control'], 'no-store')
                    self.service.links.consume(link.json()['url'].split('start=')[1], 200)
                    own = client.get('/api/v2/campus/telegram', headers=headers).json()
                    self.assertTrue(own['connected'])
                    other = client.get('/api/v2/campus/telegram', headers={'Authorization': 'Bearer owner-session'}).json()
                    self.assertFalse(other['connected'])
                    self.assertNotIn('telegram_id', own)
                    result = client.delete('/api/v2/campus/telegram', headers=headers)
                    self.assertFalse(result.json()['connected'])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
