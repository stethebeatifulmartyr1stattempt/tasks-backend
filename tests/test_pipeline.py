import base64
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from uni_tasks.core import Store, Item, resolve_due, reminder_bucket
from uni_tasks.google_source import body_text, allowed_sender, university_subject
from uni_tasks.telegram_source import source_for, enrich_reply
from uni_tasks.vikunja import apply_item
from uni_tasks.notifications import reminders, private_task, poll_updates
from uni_tasks.worker import finish_batch

UTC = timezone.utc


def fixture():
    source = {'key': 'telegram:1:23', 'title': 'Python', 'url': 'https://t.me/c/1/23',
        'project_id': 2, 'view_id': 5,
        'messages': [{'at': '2026-10-06T10:00:00+00:00', 'text': 'Лабораторна №4. Здати завтра о 18:00.', 'role': 'teacher'}]}
    item = Item(title='Лабораторна №4', course='Python', assignment_key='lab:4',
        evidence='Лабораторна №4.', source_timestamp=source['messages'][0]['at'],
        due_text='завтра о 18:00', due_evidence='Здати завтра о 18:00.',
        has_time=True, state='open', confidence=.99, review_reason=None)
    return item, source


class API:
    cfg = {'project_id': 2, 'view_id': 5, 'timezone': 'Europe/Kyiv'}

    def __init__(self):
        self.data = {}
        self.created = 0

    def tasks(self, *args):
        return list(self.data.values())

    def task(self, tid):
        return self.data[tid].copy()

    def request(self, method, path, json=None):
        self.created += 1
        task = {**json, 'id': self.created, 'project_id': 2, 'done': False}
        # Mimic Vikunja date serialization.
        if task.get('due_date'):
            task['due_date'] = task['due_date'].replace('+00:00', 'Z')
        self.data[self.created] = task
        return task.copy()

    def update(self, tid, fields, current=None):
        self.data[tid].update(fields)
        return self.data[tid].copy()


class PipelineTests(unittest.TestCase):
    def memory(self):
        store = Store(':memory:')
        self.addCleanup(store.db.close)
        return store

    def test_relative_deadline_uses_source_date(self):
        item, source = fixture()
        self.assertEqual(resolve_due(item, source), '2026-10-07T15:00:00+00:00')

    def test_date_only_ends_at_local_day(self):
        item, source = fixture()
        source['messages'][0]['text'] = 'Лабораторна №4. До 12.10.2026.'
        item.due_text = '12.10.2026'
        item.due_evidence = 'До 12.10.2026.'
        item.has_time = False
        self.assertEqual(resolve_due(item, source), '2026-10-12T20:59:00+00:00')

    def test_missing_year_does_not_move_past_deadline_one_year_forward(self):
        item, source = fixture()
        source['messages'][0].update(at='2026-10-15T10:00:00+00:00', text='Лабораторна №4. До 12.10.')
        item.source_timestamp = source['messages'][0]['at']
        item.due_text, item.due_evidence, item.has_time = '12.10', 'До 12.10.', False
        self.assertTrue(resolve_due(item, source).startswith('2026-10-12'))

    def test_unproven_deadline_is_not_saved(self):
        item, source = fixture()
        item.due_text = '2027-01-01'
        self.assertIsNone(resolve_due(item, source))
        item.due_text = 'завтра о 18:00'
        item.source_timestamp = '2026-10-26T10:00:00+00:00'
        self.assertIsNone(resolve_due(item, source))

    def test_ingest_deduplicates_but_requeues_edits(self):
        _, source = fixture()
        store = self.memory()
        store.ingest(source)
        row = store.pending()[0]
        store.succeeded(row)
        store.ingest(source)
        self.assertEqual(len(store.pending()), 0)
        source['messages'][0]['text'] += ' Дедлайн перенесено.'
        store.ingest(source)
        self.assertEqual(len(store.pending()), 1)

    def test_reposts_keep_one_task_and_completed_status(self):
        item, source = fixture()
        store, api = self.memory(), API()
        tid = apply_item(api, store, item, source)
        api.data[tid]['done'] = True
        source['key'] = 'telegram:2:90'
        apply_item(api, store, item, source)
        self.assertEqual(api.created, 1)
        self.assertTrue(api.data[tid]['done'])

    def test_human_deadline_edit_is_preserved(self):
        item, source = fixture()
        store, api = self.memory(), API()
        tid = apply_item(api, store, item, source)
        api.data[tid]['due_date'] = '2026-11-20T18:00:00Z'
        source['messages'][0]['text'] = 'Лабораторна №4. Здати 10.11.2026.'
        item.due_text, item.due_evidence, item.has_time = '10.11.2026', 'Здати 10.11.2026.', False
        apply_item(api, store, item, source)
        self.assertEqual(api.data[tid]['due_date'], '2026-11-20T18:00:00Z')

    def test_create_recovered_after_local_mapping_loss(self):
        item, source = fixture()
        store, api = self.memory(), API()
        apply_item(api, store, item, source)
        store.db.execute('DELETE FROM tasks')
        apply_item(api, store, item, source)
        self.assertEqual(api.created, 1)

    def test_past_submissions_do_not_create_pending_tasks(self):
        item, source = fixture()
        item.state = 'submitted'
        store, api = self.memory(), API()
        self.assertIsNone(apply_item(api, store, item, source))
        self.assertEqual(api.created, 0)

    def test_mime_alternative_does_not_duplicate_text(self):
        enc = lambda v: base64.urlsafe_b64encode(v.encode()).decode().rstrip('=')
        payload = {'mimeType': 'multipart/alternative', 'parts': [
            {'mimeType': 'text/plain', 'body': {'data': enc('Завдання №4')}},
            {'mimeType': 'text/html', 'body': {'data': enc('<p>Завдання №4</p>')}}]}
        self.assertEqual(body_text(payload), 'Завдання №4')

    def test_mail_sender_filters_do_not_match_similar_domains(self):
        cfg = {'gmail_senders': ['teacher@example.org'], 'gmail_domains': ['university.example']}
        self.assertTrue(allowed_sender('Teacher <teacher@example.org>', cfg))
        self.assertTrue(allowed_sender('name@edu.university.example', cfg))
        self.assertFalse(allowed_sender('name@evil-university.example', cfg))
        self.assertFalse(allowed_sender('name@university.example.evil.com', cfg))

    def test_nearest_reminder_bucket_after_downtime(self):
        at = datetime(2026, 10, 26, 12, tzinfo=UTC)
        self.assertEqual(reminder_bucket(at + timedelta(hours=2), at), '3')
        self.assertEqual(reminder_bucket(at + timedelta(minutes=30), at), '1')
        self.assertIsNone(reminder_bucket(at + timedelta(days=2), at))

    def test_caption_is_preserved_and_source_date_not_import_date(self):
        message = SimpleNamespace(id=10, raw_text='ЛР №4', date=datetime(2026, 10, 20, tzinfo=UTC))
        source = source_for(message, {'id': -1001234567890, 'title': 'Python'}, {'project_id': 2, 'view_id': 5})
        self.assertEqual(source['url'], 'https://t.me/c/1234567890/10')
        self.assertEqual(source['messages'][0]['at'], '2026-10-20T00:00:00+00:00')

    def test_forged_bot_task_id_cannot_modify_shared_task(self):
        api = API()
        api.data[1] = {'id': 1, 'project_id': 2}
        with self.assertRaises(ValueError):
            private_task(api, 1, {'notification_project_id': 19})

    def test_completed_or_changed_tasks_cancel_queued_reminder(self):
        api, store = API(), self.memory()
        api.data[1] = {'id': 1, 'project_id': 19, 'title': 'ЛР', 'done': True, 'due_date': '2026-10-27T12:00:00Z'}
        store.db.execute("INSERT INTO notifications(key,task_id,due,bucket) VALUES ('test',1,'2026-10-27T12:00:00Z','24')")
        bot = SimpleNamespace(call=lambda *a, **kw: self.fail('completed task notification sent'))
        with patch('uni_tasks.notifications.config', return_value={
                'owner_telegram_id': 7, 'notification_project_id': 19, 'notification_view_id': 73}):
            reminders(api, store, bot)
        self.assertEqual(store.db.execute('SELECT sent FROM notifications').fetchone()[0], 2)

    def test_stranger_cannot_bind_bot_without_pairing_code(self):
        store = self.memory()
        calls = []
        def call(method, **kwargs):
            calls.append(method)
            return [{'update_id': 1, 'message': {'chat': {'type': 'private'}, 'from': {'id': 999}, 'text': '/start wrong'}}]
        bot = SimpleNamespace(call=call)
        with patch('uni_tasks.notifications.config', return_value={}), patch('uni_tasks.notifications.secrets', return_value={'pairing_code': 'correct'}):
            poll_updates(API(), store, bot)
        self.assertEqual(calls, ['getUpdates'])
        self.assertEqual(store.get('bot_offset'), 2)

    def test_one_uncertain_item_does_not_drop_valid_tasks_from_same_message(self):
        item, source = fixture()
        store, api = self.memory(), API()
        store.ingest(source)
        row = store.pending()[0]
        uncertain = item.model_copy(update={'title': 'Непевне завдання', 'confidence': .3})
        result = {'ok': True, 'batch': {'messages': [{'source_key': source['key'], 'tasks': [uncertain.model_dump(), item.model_dump()]}]}}
        active = {'id': 'test-job', 'keys': [source['key']], 'digests': {source['key']: row['digest']}}
        finish_batch(api, store, active, result)
        self.assertEqual(api.created, 2)
        self.assertTrue(any('Лабораторна' in t['title'] for t in api.data.values()))
        self.assertTrue(any('Перевірити оголошення' in t['title'] for t in api.data.values()))
        self.assertEqual(len(store.pending()), 0)

    def test_reply_context_is_read_only_from_selected_peer_and_cached(self):
        store = self.memory()
        peer = object()
        chat = {'id': -1001234567890, 'title': 'Python'}
        cfg = {'project_id': 2, 'view_id': 5}
        message = SimpleNamespace(id=11, raw_text='Дедлайн завтра.', date=datetime(2026, 10, 26, tzinfo=UTC),
                                  reply_to=SimpleNamespace(reply_to_msg_id=10, reply_to_peer_id=None))
        calls = []
        class Reader:
            async def get_messages(self, requested_peer, ids):
                self.asserted = requested_peer is peer and ids == 10
                calls.append(self.asserted)
                return SimpleNamespace(raw_text='Лабораторна №4.', date=datetime(2026, 10, 20, tzinfo=UTC))
        reader = Reader()
        first = asyncio.run(enrich_reply(reader, peer, message, chat, cfg, store))
        store.ingest(first)
        second = asyncio.run(enrich_reply(reader, peer, message, chat, cfg, store))
        self.assertEqual(calls, [True])
        self.assertEqual(first, second)
        self.assertEqual(first['messages'][0]['role'], 'quoted_context')
        self.assertEqual(first['messages'][-1]['text'], 'Дедлайн завтра.')


if __name__ == '__main__':
    unittest.main()
