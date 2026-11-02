from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
import base64
import json
from unittest.mock import patch

from uni_tasks.core import Store, fingerprint
from uni_tasks.google_sharing import group_mail_source, shared_classroom_source, fresh_mail_text
from uni_tasks.disciplines import target_project
from uni_tasks.telegram_source import source_for
from uni_tasks.worker import process
from uni_tasks.vikunja import apply_item
from uni_tasks.google_source import collect_gmail
from test_pipeline import API, fixture

CFG = {**API.cfg, 'private_project_id': 19, 'private_view_id': 73,
       'google_private_terms': ['Student Surname', 'private@example.org'],
       'discipline_routing': {'enabled': True, 'google_enabled': True,
        'review_project_id': 2, 'review_view_id': 5, 'courses': [
            {'id': 'python', 'title': 'Python', 'project_id': 30, 'view_id': 117,
             'classroom_ids': ['123'], 'telegram_topics': [{'chat_id': -1001, 'topic_id': 44}],
             'gmail_rules': [{'sender': 'teacher@example.org', 'subject_contains': 'Python', 'min_recipients': 3}]}]}}
HEAD = {'from': 'Teacher <teacher@example.org>', 'subject': 'Python: завдання для групи',
        'to': 'a@example.org,b@example.org,c@example.org', 'message-id': '<announcement@school>'}
AT = '2026-10-06T10:00:00+00:00'


class GoogleSharingTests(unittest.TestCase):
    def mail(self, head=None, message=None, text='Лабораторна №4. Здати завтра о 18:00.'):
        return group_mail_source(head or HEAD, message or {'labelIds': ['INBOX']}, CFG, text, AT)

    def test_broadcast_is_shared_without_mailbox_or_recipient_addresses(self):
        source = self.mail()
        self.assertEqual(source['course_id'], 'python')
        self.assertEqual(source['url'], '')
        self.assertNotIn('example.org', str(source))

    def test_same_mail_in_multiple_accounts_deduplicates(self):
        self.assertEqual(self.mail()['key'], self.mail(head={**HEAD, 'to': HEAD['to']+',d@example.org'})['key'])

    def test_personal_feedback_outgoing_unknown_senders_and_unknown_subjects_are_private(self):
        for head, message, text in [
            ({**HEAD, 'to': 'me@example.org'}, None, 'ЛР4'),
            ({**HEAD, 'from': 'stranger@example.org'}, None, 'ЛР4'),
            ({**HEAD, 'subject': 'Something else'}, None, 'ЛР4'),
            (None, {'labelIds': ['SENT']}, 'ЛР4'),
            (None, None, 'Student Surname, переробіть ЛР4'),
            (None, None, 'Ваша оцінка за ЛР4: 10'),
        ]:
            self.assertIsNone(self.mail(head, message, text))

    def test_reply_quotes_and_signature_are_not_shared(self):
        text = 'Лабораторна №4.\nBR,\nTeacher\nOn yesterday someone wrote:\n> private@example.org'
        self.assertEqual(self.mail(text=text)['messages'][0]['text'], 'Лабораторна №4.')
        self.assertEqual(fresh_mail_text('ЛР4\n> private thread'), 'ЛР4')

    def test_classroom_group_ignores_grades_submissions_and_private_materials(self):
        work = {'id': '9', 'state': 'PUBLISHED', 'title': 'ЛР4', 'description': 'Виконати лабораторну.',
                'studentSubmissions': [{'assignedGrade': 2, 'studentId': 'secret'}],
                'materials': [{'link': {'url': 'https://example.org/lab', 'title': 'Умова'}}]}
        source = shared_classroom_source({'id': '123'}, work, CFG, AT, None)
        self.assertEqual(source['structured']['state'], 'open')
        self.assertEqual(len(source['structured']['materials']), 1)
        self.assertNotIn('secret', str(source))
        self.assertNotIn('assignedGrade', str(source))
        for changes in [{'assigneeMode': 'INDIVIDUAL_STUDENTS'}, {'state': 'DRAFT'},
                        {'individualStudentsOptions': {'studentIds': ['me']}}, {'description': 'Student Surname'}]:
            self.assertIsNone(shared_classroom_source({'id': '123'}, work | changes, CFG, AT, None))
        self.assertIsNone(shared_classroom_source({'id': 'unmapped'}, work, CFG, AT, None))

    def test_only_explicit_shared_google_sources_route_to_subject(self):
        item, _ = fixture()
        self.assertEqual(target_project(CFG, item, self.mail()), (30, 117))
        for source in [{'key': 'gmail:a:b', 'project_id': 19, 'view_id': 73},
                       {'key': 'classroom:123:9', 'project_id': 19, 'view_id': 73}]:
            self.assertEqual(target_project(CFG, item, source), (19, 73))
        self.assertEqual(target_project(CFG, item.model_copy(update={'course': 'Other'}), self.mail()), (2, 5))

    def test_submission_cannot_complete_or_create_shared_assignment(self):
        item, _ = fixture()
        api, store = API(), Store(':memory:')
        self.addCleanup(store.db.close)
        api.cfg = CFG
        self.assertIsNone(apply_item(api, store, item.model_copy(update={'state': 'submitted'}), self.mail()))
        self.assertEqual(api.created, 0)

    def test_topic_identity_supplies_course_even_without_words_in_message(self):
        message = SimpleNamespace(id=45, raw_text='ЛР4', date=datetime(2026, 10, 6, tzinfo=timezone.utc),
                                  reply_to=SimpleNamespace(forum_topic=True, reply_to_msg_id=44, reply_to_top_id=None))
        source = source_for(message, {'id': -1001, 'title': 'Group'}, CFG)
        self.assertIn('Python', source['title'])
        item, _ = fixture()
        self.assertEqual(target_project(CFG, item.model_copy(update={'course': ''}), source), (30, 117))

    def test_reviewed_disposition_skips_only_unchanged_evidence(self):
        _, source = fixture()
        store = Store(':memory:')
        self.addCleanup(store.db.close)
        store.ingest(source)
        store.set('reviewed:' + source['key'], {'messages_digest': fingerprint(source['messages']), 'items': []})
        process(API(), store)
        self.assertEqual(len(store.pending()), 0)
        source['messages'][0]['text'] += ' Дедлайн змінився.'
        store.ingest(source)
        self.assertNotEqual(store.get('reviewed:' + source['key'])['messages_digest'], fingerprint(source['messages']))

    def test_broadcast_does_not_create_duplicate_private_task_but_personal_reply_stays_private(self):
        cfg = CFG | {'gmail_senders': ['teacher@example.org']}
        def message(mid, labels, body, head):
            return {'id': mid, 'threadId': 'thread', 'labelIds': labels, 'internalDate': '1791280800000',
                    'payload': {'mimeType': 'text/plain', 'headers': [{'name': k, 'value': v} for k,v in head.items()],
                    'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()}}}
        broadcast = message('m1', ['INBOX'], 'Лабораторна №4.', HEAD)
        reply = message('m2', ['SENT'], 'Мій особистий звіт', HEAD | {'from': 'me@example.org'})
        for messages, expected_private in [([broadcast], False), ([broadcast, reply], True)]:
            store = Store(':memory:')
            self.addCleanup(store.db.close)
            def get(_session, url, params=None):
                if url.endswith('/profile'): return {'historyId': '1'}
                if '/messages/' in url: return broadcast
                if '/threads/' in url: return {'messages': messages}
                raise AssertionError(url)
            with patch('uni_tasks.google_source.get', side_effect=get), patch('uni_tasks.google_source.pages', return_value=[{'id': 'm1'}]):
                collect_gmail(store, 'me@example.org', None, cfg)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM sources WHERE key LIKE 'gmail-group:%'").fetchone()[0], 1)
            private = store.db.execute("SELECT payload FROM sources WHERE key LIKE 'gmail:%'").fetchone()
            self.assertEqual(bool(private), expected_private)
            if private:
                self.assertEqual([m['text'] for m in json.loads(private[0])['messages']], ['Мій особистий звіт'])


if __name__ == '__main__':
    unittest.main()
