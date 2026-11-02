import json
import threading
import unittest
from http.server import ThreadingHTTPServer

import httpx

from uni_tasks.enrollment import Enrollment, Problem, handler_for


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.shares = {}
        self.writes = []
        self.personal_shares = {}
        self.personal_owner = 2
        self.catalog = {'owner_id': 1, 'origins': ['https://tasks.example'],
            'replication_username': 'bot-personal-copies',
            'students': [{'user_id': 1, 'username': 'owner', 'personal_project_id': 19, 'personal_view_id': 73},
                         {'user_id': 2, 'username': 'student', 'personal_project_id': 4, 'personal_view_id': 13}],
            'courses': [{'id': 'python', 'title': 'Python', 'project_id': 30, 'view_id': 117}]}
        def upstream(request):
            if request.url.path == '/user':
                auth = request.headers.get('Authorization')
                if auth == 'Bearer student-session':
                    return httpx.Response(200, json={'id': 2, 'username': 'student'})
                if auth == 'Bearer owner-session':
                    return httpx.Response(200, json={'id': 1, 'username': 'owner'})
                if auth == 'Bearer stranger-session':
                    return httpx.Response(200, json={'id': 99, 'username': 'stranger'})
                return httpx.Response(401)
            if request.url.path.startswith('/projects/4'):
                self.assertEqual(request.headers['Authorization'], 'Bearer student-session')
                if request.url.path == '/projects/4':
                    return httpx.Response(200, json={'owner': {'id': self.personal_owner}})
                self.assertEqual(request.url.path, '/projects/4/users')
                if request.method == 'GET':
                    return httpx.Response(200, json={'items': list(self.personal_shares.values()), 'total_pages': 1})
                self.assertEqual(request.method, 'POST')
                data = json.loads(request.content)
                self.assertEqual(data, {'username': 'bot-personal-copies', 'permission': 1})
                self.personal_shares[data['username']] = data
                return httpx.Response(201, json=data)
            self.assertEqual(request.headers['Authorization'], 'Bearer scoped-service-token')
            self.assertTrue(request.url.path.startswith('/projects/30/users'))
            if request.method == 'GET':
                return httpx.Response(200, json={'items': list(self.shares.values()), 'total_pages': 1})
            self.writes.append((request.method, request.url.path, request.content))
            if request.method == 'POST':
                data = json.loads(request.content)
                self.assertEqual(data, {'username': 'student', 'permission': 0})
                self.shares['student'] = data
                return httpx.Response(201, json=data)
            self.assertEqual(request.method, 'DELETE')
            self.assertEqual(request.url.path, '/projects/30/users/student')
            del self.shares['student']
            return httpx.Response(204)
        self.http = httpx.Client(base_url='http://vikunja', transport=httpx.MockTransport(upstream))
        self.addCleanup(self.http.close)
        self.service = Enrollment(self.catalog, 'scoped-service-token', self.http)
        self.user = self.service.authenticate('Bearer student-session')

    def test_join_leave_are_idempotent_and_only_grant_read_access(self):
        self.assertFalse(self.service.list_subjects(self.user)['subjects'][0]['selected'])
        for _ in range(2):
            self.assertTrue(self.service.select(self.user, 'python', {'selected': True}, 'Bearer student-session')['selected'])
        for _ in range(2):
            self.assertFalse(self.service.select(self.user, 'python', {'selected': False})['selected'])
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(len(self.personal_shares), 1)

    def test_personal_ownership_must_be_verified_before_enrollment(self):
        self.personal_owner = 99
        with self.assertRaises(Problem) as error:
            self.service.select(self.user, 'python', {'selected': True}, 'Bearer student-session')
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.shares, {})
        self.assertEqual(self.personal_shares, {})

    def test_catalog_returns_only_callers_personal_list(self):
        result = self.service.list_subjects(self.user)
        self.assertEqual(result['personal_project_id'], 4)
        self.assertEqual(result['personal_view_id'], 13)
        self.assertNotIn('students', result)

    def test_forged_user_permission_project_and_malformed_boolean_are_rejected(self):
        for body in [{'selected': True, 'user_id': 1}, {'selected': True, 'permission': 2},
                     {'selected': True, 'project_id': 19}, {'selected': 'false'}, []]:
            with self.assertRaises(Problem) as error:
                self.service.select(self.user, 'python', body)
            self.assertEqual(error.exception.status, 422)
        with self.assertRaises(Problem) as error:
            self.service.select(self.user, '19', {'selected': True})
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.writes, [])

    def test_non_roster_users_and_expired_sessions_are_denied(self):
        for token, status in [(None, 401), ('Bearer expired', 401), ('Bearer stranger-session', 403)]:
            with self.assertRaises(Problem) as error:
                self.service.authenticate(token)
            self.assertEqual(error.exception.status, status)

    def test_owner_or_manually_elevated_access_is_not_changed(self):
        owner = self.service.authenticate('Bearer owner-session')
        self.assertTrue(self.service.list_subjects(owner)['subjects'][0]['locked'])
        with self.assertRaises(Problem):
            self.service.select(owner, 'python', {'selected': False})
        self.shares['student'] = {'username': 'student', 'permission': 2}
        with self.assertRaises(Problem):
            self.service.select(self.user, 'python', {'selected': False})
        self.assertEqual(self.writes, [])

    def test_http_auth_origin_validation_and_no_cache(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}') as client:
            self.assertEqual(client.get('/api/v2/campus/subjects').status_code, 401)
            headers = {'Authorization': 'Bearer student-session'}
            result = client.get('/api/v2/campus/subjects', headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers['Cache-Control'], 'no-store')
            self.assertNotIn('student', result.text)
            forbidden = client.put('/api/v2/campus/subjects/python',
                headers={**headers, 'Origin': 'https://attacker.example'}, json={'selected': True})
            self.assertEqual(forbidden.status_code, 403)
            self.assertEqual(client.put('/api/v2/campus/subjects/python', headers=headers,
                                       json={'selected': True}).status_code, 200)
            self.assertEqual(client.put('/api/v2/campus/subjects/python', headers=headers,
                                       json={'selected': False}).status_code, 200)


if __name__ == '__main__':
    unittest.main()
