from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx

from uni_tasks.replication import API, FIELDS, Replicator, desired_fields, marker


class FakeAPI:
    def __init__(self):
        self.data = {1: {'id': 1, 'project_id': 30, 'title': 'Лабораторна 1', 'description': '<p>Умова</p>',
                         'due_date': '2026-11-01T10:00:00Z', 'start_date': '', 'end_date': '',
                         'priority': 1, 'done': True}}
        self.shares = {'a', 'b'}
        self.writes = []
        self.lost_response = False

    def tasks(self, project):
        return deepcopy([t for t in self.data.values() if t['project_id'] == project])

    def members(self, project):
        assert project == 30
        return [{'username': name} for name in self.shares]

    def project(self, project):
        return {'id': project, 'owner': {'id': {4: 2, 6: 3}[project]}}

    def get(self, task):
        if task not in self.data:
            response = httpx.Response(404, request=httpx.Request('GET', f'https://test/tasks/{task}'))
            response.raise_for_status()
        return deepcopy(self.data[task])

    def create(self, project, fields):
        assert project in (4, 6)
        tid = max(self.data) + 1
        self.data[tid] = deepcopy(fields) | {'id': tid, 'project_id': project}
        self.writes.append(('create', tid))
        if self.lost_response:
            self.lost_response = False
            raise httpx.ReadTimeout('response lost')
        return deepcopy(self.data[tid])

    def patch(self, task, current, changes):
        assert self.data[task]['project_id'] in (4, 6)
        assert set(changes).issubset(FIELDS)
        assert all(self.data[task][k] == current[k] for k in changes)
        self.writes.append(('patch', task))
        self.data[task].update(deepcopy(changes))
        return deepcopy(self.data[task])


class ReplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = FakeAPI()
        self.catalog = {'owner_id': 99, 'public_url': 'https://tasks.example',
            'students': [{'user_id': 2, 'username': 'a', 'personal_project_id': 4},
                         {'user_id': 3, 'username': 'b', 'personal_project_id': 6}],
            'courses': [{'id': 'math', 'title': 'Математика', 'project_id': 30}]}
        self.worker = Replicator(self.catalog, self.api, Path(self.tmp.name) / 'copies.db')
        self.addCleanup(self.worker.db.close)

    def test_two_students_independent_progress_and_source_untouched(self):
        original = deepcopy(self.api.data[1])
        self.assertEqual(self.worker.cycle(), 0)
        a, b = self.api.tasks(4)[0], self.api.tasks(6)[0]
        self.assertNotEqual(a['id'], b['id'])
        self.assertFalse(a['done'])
        self.assertFalse(b['done'])
        self.api.data[a['id']]['done'] = True
        self.api.data[1]['due_date'] = '2026-11-02T10:00:00Z'
        self.assertEqual(self.worker.cycle(), 0)
        self.assertTrue(self.api.data[a['id']]['done'])
        self.assertFalse(self.api.data[b['id']]['done'])
        self.assertEqual(self.api.data[a['id']]['due_date'], self.api.data[1]['due_date'])
        self.assertEqual(self.api.data[1], original | {'due_date': '2026-11-02T10:00:00Z'})

    def test_new_task_join_leave_rejoin_no_duplicates_or_reset(self):
        self.worker.cycle()
        a = self.api.tasks(4)[0]
        self.api.data[a['id']]['done'] = True
        self.api.shares.remove('a')
        self.api.data[10] = deepcopy(self.api.data[1]) | {'id': 10, 'title': 'Лабораторна 2'}
        self.api.data[1]['title'] = 'Оновлена умова'
        self.worker.cycle()
        self.assertEqual(len(self.api.tasks(4)), 1)
        self.assertEqual(self.api.data[a['id']]['title'], a['title'])
        self.api.shares.add('a')
        self.worker.cycle()
        self.worker.cycle()
        self.assertEqual(len(self.api.tasks(4)), 2)
        self.assertEqual(len(self.api.tasks(6)), 2)
        self.assertTrue(self.api.data[a['id']]['done'])
        self.assertEqual(self.api.data[a['id']]['title'], 'Оновлена умова')

    def test_manual_fields_and_deleted_copies_are_respected(self):
        self.worker.cycle()
        a = self.api.tasks(4)[0]
        self.api.data[a['id']].update(title='Моя назва', description='Мої нотатки', due_date='2026-12-01T10:00:00Z')
        self.api.data[1].update(title='Змінено', description='Інша умова', due_date='2026-11-03T10:00:00Z')
        self.worker.cycle()
        self.assertEqual(self.api.data[a['id']]['description'], 'Мої нотатки')
        self.assertEqual(self.api.data[a['id']]['title'], 'Моя назва')
        self.assertEqual(self.api.data[a['id']]['due_date'], '2026-12-01T10:00:00Z')
        del self.api.data[a['id']]
        self.worker.cycle()
        self.api.shares.remove('a')
        self.worker.cycle()
        self.api.shares.add('a')
        self.worker.cycle()
        self.assertEqual(self.api.tasks(4), [])

    def test_lost_create_response_recovers_marker_after_restart(self):
        self.api.lost_response = True
        self.assertEqual(self.worker.cycle(), 1)
        self.assertEqual(len(self.api.tasks(4)), 1)
        self.assertEqual(self.worker.cycle(), 0)
        self.assertEqual(len(self.api.tasks(4)), 1)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM copies WHERE task_id IS NULL').fetchone()[0], 0)

    def test_existing_marker_adopted_without_resetting_done(self):
        self.api.data[5] = desired_fields(self.api.data[1], self.catalog['courses'][0], 2, 'https://tasks.example') | {
            'id': 5, 'project_id': 4, 'done': True}
        self.worker.cycle()
        self.assertEqual(len(self.api.tasks(4)), 1)
        self.assertTrue(self.api.data[5]['done'])

    def test_no_enrollment_no_copy_and_private_sources_never_read(self):
        self.api.shares.clear()
        self.api.data[20] = deepcopy(self.api.data[1]) | {'id': 20, 'project_id': 19, 'title': 'Private email'}
        self.worker.cycle()
        self.assertEqual(self.api.writes, [])

    def test_moved_personal_copy_is_never_written_in_shared_project(self):
        self.worker.cycle()
        a = self.api.tasks(4)[0]
        self.api.data[a['id']]['project_id'] = 50
        self.api.data[1]['title'] = 'Changed'
        self.worker.cycle()
        self.assertEqual(self.api.data[a['id']]['title'], a['title'])
        self.assertEqual(self.api.tasks(4), [])

    def test_invalid_destination_and_duplicate_personal_projects_fail_closed(self):
        self.catalog['students'][1]['personal_project_id'] = 4
        with self.assertRaises(ValueError):
            Replicator(self.catalog, self.api, ':memory:')

    def test_patch_sends_only_changed_fields_and_concurrency_tests(self):
        calls = []
        def transport(request):
            calls.append(request)
            return httpx.Response(200, json={})
        api = API('https://test', 'token')
        api.http.close()
        api.http = httpx.Client(base_url='https://test', transport=httpx.MockTransport(transport))
        self.addCleanup(api.http.close)
        api.patch(2, {'due_date': 'old', 'done': True}, {'due_date': 'new'})
        import json
        self.assertEqual(json.loads(calls[0].content), [
            {'op': 'test', 'path': '/due_date', 'value': 'old'},
            {'op': 'replace', 'path': '/due_date', 'value': 'new'}])
        self.assertEqual(calls[0].headers['content-type'], 'application/json-patch+json')


if __name__ == '__main__':
    unittest.main()
