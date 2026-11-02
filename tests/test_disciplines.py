import unittest
from types import SimpleNamespace

from uni_tasks.core import Store
from uni_tasks.disciplines import course_for, target_project
from uni_tasks.vikunja import apply_item
from test_pipeline import API, fixture

ROUTING = {'enabled': True, 'review_project_id': 2, 'review_view_id': 5, 'courses': [
    {'id': 'python', 'title': 'Python', 'project_id': 30, 'view_id': 117,
     'aliases': ['Python для аналізу даних'], 'telegram_chat_ids': [-1001]},
    {'id': 'web', 'title': 'Веб-програмування', 'project_id': 31, 'view_id': 121,
     'aliases': [], 'telegram_chat_ids': [-1002]},
]}
CFG = {**API.cfg, 'discipline_routing': ROUTING}


class RoutingTests(unittest.TestCase):
    def test_private_mail_never_enters_a_subject_project(self):
        for kind in ['gmail', 'classroom', 'telegram']:
            self.assertEqual(target_project(CFG, SimpleNamespace(course='Python'),
                {'key': kind + ':1:2', 'project_id': 19, 'view_id': 73}), (19, 73))

    def test_unknown_and_conflicting_subjects_stay_private(self):
        for course, key in [('Python в електроніці', 'telegram:1:2'), ('', 'telegram:1:2'),
                            ('Python', 'telegram:-1002:2'), ('Уточнення', 'telegram:-1001:2')]:
            self.assertEqual(target_project(CFG, SimpleNamespace(course=course), {'key': key}), (2, 5))

    def test_exact_alias_or_single_subject_channel_routes_correctly(self):
        self.assertEqual(course_for(ROUTING, ' PYTHON для аналізу даних ', 'telegram:1:2')['id'], 'python')
        self.assertEqual(course_for(ROUTING, '', 'telegram:-1002:2')['id'], 'web')

    def test_reposts_after_migration_keep_id_completion_and_manual_deadline(self):
        item, source = fixture()
        api, store = API(), Store(':memory:')
        self.addCleanup(store.db.close)
        first = apply_item(api, store, item, source)
        api.data[first].update(project_id=30, done=True, due_date='2027-01-10T00:00:00Z')
        api.cfg = CFG
        source = {**source, 'key': 'telegram:1:24'}
        again = apply_item(api, store, item, source)
        self.assertEqual(first, again)
        self.assertEqual(api.created, 1)
        self.assertEqual(api.data[first]['project_id'], 30)
        self.assertTrue(api.data[first]['done'])
        self.assertEqual(api.data[first]['due_date'], '2027-01-10T00:00:00Z')

    def test_new_task_created_in_subject_and_recovered_after_lost_commit(self):
        item, source = fixture()
        class RoutedAPI(API):
            cfg = CFG
            def request(self, method, path, json=None):
                result = super().request(method, path, json=json)
                self.data[result['id']]['project_id'] = int(path.split('/')[2])
                return self.task(result['id'])
            def tasks(self, project=None, *_args):
                return [v for v in self.data.values() if v['project_id'] == project]
        api, store = RoutedAPI(), Store(':memory:')
        self.addCleanup(store.db.close)
        task = apply_item(api, store, item, source)
        self.assertEqual(api.data[task]['project_id'], 30)
        store.db.execute('DELETE FROM tasks')
        api.data[task]['project_id'] = 31  # The owner moved it before recovery.
        self.assertEqual(apply_item(api, store, item, source), task)
        self.assertEqual(api.created, 1)


if __name__ == '__main__':
    unittest.main()
