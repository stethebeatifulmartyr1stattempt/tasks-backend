"""Authenticated subject selection; grants only the caller read-only access.

This service has a separate bot credential limited to project-user shares on
the catalog projects. It has no source-account credentials or task-write scope.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlsplit
import json
import logging
import os
import re
from .notification_links import Links
from .campus import Campus
from .campus_api import CampusAPI
from .errors import Problem

import httpx


class Enrollment(CampusAPI):
    def __init__(self, catalog, token, http=None, links=None, campus=None):
        self.catalog = catalog
        self.courses = {c['id']: c for c in catalog['courses']}
        self.students = {s['user_id']: s for s in catalog['students']}
        self.http = http or httpx.Client(base_url=catalog['vikunja_api'], timeout=15)
        self.token = token
        self.lock = Lock()
        self.links = links
        self.campus = campus

    def telegram(self, user, method, authorization):
        if self.links is None:
            raise Problem(503, 'Підключення Telegram тимчасово недоступне.')
        with self.lock:
            if method == 'POST':
                self.ensure_personal_access(user, authorization)
                code, expires = self.links.issue(user['id'])
                return {'url': f'https://t.me/{self.catalog["telegram_bot_username"]}?start={code}',
                        'expires_at': expires}
            if method == 'DELETE':
                self.links.disconnect(user['id'])
            return self.links.status(user['id'])

    def authenticate(self, authorization):
        if not authorization or not authorization.startswith('Bearer ') or len(authorization) > 8192:
            raise Problem(401, 'Увійди у свій обліковий запис.', 11)
        response = self.http.get('/user', headers={'Authorization': authorization})
        if response.status_code in (401, 403):
            raise Problem(401, 'Увійди у свій обліковий запис.', 11)
        if response.status_code != 200:
            raise Problem(503, 'Не вдалося перевірити вхід. Спробуй пізніше.')
        user = response.json()
        student = self.students.get(user.get('id'))
        if not student or student['username'] != user.get('username') or user.get('bot_owner_id'):
            raise Problem(403, 'Вибір дисциплін доступний учасникам ПІ-42.')
        return user

    def request(self, method, path, **kwargs):
        response = self.http.request(method, path,
            headers={'Authorization': 'Bearer ' + self.token}, **kwargs)
        if not response.is_success:
            raise Problem(503, 'Не вдалося змінити доступ. Онови сторінку й спробуй ще раз.')
        return response.json() if response.content else None

    def member(self, course, user):
        page = 1
        while True:
            result = self.request('GET', f'/projects/{course["project_id"]}/users',
                                  params={'page': page})
            match = next((m for m in result.get('items') or [] if m['username'] == user['username']), None)
            if match or page >= result.get('total_pages', 1):
                return match
            page += 1

    def course_state(self, course, user):
        owner = user['id'] == self.catalog['owner_id']
        member = None if owner else self.member(course, user)
        return {k: course[k] for k in ('id', 'title', 'project_id', 'view_id')} | {
            'selected': owner or member is not None,
            'locked': owner or bool(member and member.get('permission') != 0),
            'variant': self.campus.variants(user['id']).get(course['id'],'') if self.campus else '',
        }

    def list_subjects(self, user):
        with self.lock:
            return {'subjects': [self.course_state(c, user) for c in self.courses.values()],
                    'administrator': user['id'] == self.catalog['owner_id'],
                    **{key: self.students[user['id']][key]
                       for key in ('personal_project_id', 'personal_view_id')}}

    def ensure_personal_access(self, user, authorization):
        # Use the caller's session only for this fixed, ownership-checked share.
        # It is never persisted or passed to the background worker.
        project_id = self.students[user['id']]['personal_project_id']
        headers = {'Authorization': authorization}
        response = self.http.get(f'/projects/{project_id}', headers=headers)
        if response.status_code != 200 or response.json().get('owner', {}).get('id') != user['id']:
            raise Problem(403, 'Не вдалося підтвердити власника особистого списку.')
        username = self.catalog['replication_username']
        page = 1
        while True:
            response = self.http.get(f'/projects/{project_id}/users', headers=headers, params={'page': page})
            if response.status_code != 200:
                raise Problem(503, 'Не вдалося підключити особистий список. Спробуй ще раз.')
            result = response.json()
            member = next((m for m in result.get('items') or [] if m['username'] == username), None)
            if member:
                if member.get('permission') != 1:
                    raise Problem(409, 'Перевір права сервісу копіювання в особистому списку.')
                return
            if page >= result.get('total_pages', 1):
                break
            page += 1
        response = self.http.post(f'/projects/{project_id}/users', headers=headers,
                                  json={'username': username, 'permission': 1})
        if not response.is_success:
            raise Problem(503, 'Не вдалося підключити особистий список. Спробуй ще раз.')

    def select(self, user, subject_id, body, authorization=None):
        if not isinstance(body, dict) or set(body) != {'selected'} or type(body['selected']) is not bool:
            raise Problem(422, 'Передай лише selected: true або false.')
        course = self.courses.get(subject_id)
        if not course:
            raise Problem(404, 'Дисципліну не знайдено.')
        with self.lock:
            state = self.course_state(course, user)
            if state['locked']:
                raise Problem(409, 'Адміністратор зберігає доступ до всіх своїх дисциплін.')
            if body['selected']:
                if not authorization:
                    raise Problem(401, 'Увійди у свій обліковий запис.', 11)
                self.ensure_personal_access(user, authorization)
            if body['selected'] != state['selected']:
                path = f'/projects/{course["project_id"]}/users'
                if body['selected']:
                    self.request('POST', path, json={'username': user['username'], 'permission': 0})
                else:
                    self.request('DELETE', path + '/' + quote(user['username'], safe=''))
            result = self.course_state(course, user)
            if result['selected'] != body['selected']:
                raise Problem(503, 'Зміна доступу ще не підтверджена. Онови сторінку.')
            return result


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'Campus'

        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, *_args):
            pass  # Do not record credentials, queries or student identifiers.

        def send_json(self, status, data):
            content = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def handle_request(self):
            path = urlsplit(self.path).path
            if self.command == 'GET' and path == '/health':
                self.send_json(200, {'status': 'ok'})
                return
            try:
                origin = self.headers.get('Origin')
                if origin and origin not in service.catalog['origins']:
                    raise Problem(403, 'Це джерело запиту не дозволене.')
                user = service.authenticate(self.headers.get('Authorization'))
                if self.command == 'GET' and path == '/api/v2/campus/subjects':
                    result = service.list_subjects(user)
                elif ((self.command in ('GET', 'DELETE') and path == '/api/v2/campus/telegram') or
                      (self.command == 'POST' and path == '/api/v2/campus/telegram/link')):
                    if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
                        raise Problem(422, 'Цей запит не приймає даних користувача.')
                    result = service.telegram(user, self.command, self.headers.get('Authorization'))
                elif self.command == 'PUT' and re.fullmatch(r'/api/v2/campus/subjects/[a-z0-9-]+', path):
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise Problem(415, 'Очікується application/json.')
                    if self.headers.get('Transfer-Encoding'):
                        raise Problem(400, 'Некоректний запит.')
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                    except ValueError:
                        raise Problem(400, 'Некоректна довжина запиту.') from None
                    if not 0 < length <= 256:
                        raise Problem(413, 'Завеликий або порожній запит.')
                    try:
                        body = json.loads(self.rfile.read(length))
                    except (ValueError, UnicodeError):
                        raise Problem(400, 'Некоректний JSON.') from None
                    result = service.select(user, path.rsplit('/', 1)[1], body,
                                            self.headers.get('Authorization'))
                elif path.startswith('/api/v2/campus/'):
                    body = None
                    if self.command in ('PUT','POST'):
                        if self.headers.get('Transfer-Encoding'):
                            raise Problem(400,'Некоректний запит.')
                        if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                            raise Problem(415,'Очікується application/json.')
                        try: length=int(self.headers.get('Content-Length','0'))
                        except ValueError: raise Problem(400,'Некоректна довжина запиту.') from None
                        if not 0<length<=8192: raise Problem(413,'Завеликий або порожній запит.')
                        try: body=json.loads(self.rfile.read(length))
                        except (ValueError,UnicodeError): raise Problem(400,'Некоректний JSON.') from None
                    result=service.campus_action(user,self.headers.get('Authorization'),path.removeprefix('/api/v2/campus'),self.command,body)
                else:
                    raise Problem(404, 'Сторінку не знайдено.')
                self.send_json(200, result)
            except Problem as exc:
                self.send_json(exc.status, {'status': exc.status, 'detail': exc.detail,
                                            **({'code': exc.code} if exc.code else {})})
            except (httpx.HTTPError, ValueError, KeyError):
                self.send_json(503, {'status': 503, 'detail': 'Сервіс тимчасово недоступний. Спробуй ще раз.'})

        do_GET = handle_request
        do_PUT = handle_request
        do_POST = handle_request
        do_DELETE = handle_request

    return Handler


def main():
    os.umask(0o077)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    root = Path(os.getenv('ENROLLMENT_CONFIG', '/enrollment-config'))
    service = Enrollment(json.loads((root / 'catalog.json').read_text('utf-8')),
                         (root / 'token.txt').read_text('utf-8').strip(),
                         links=Links(Path(os.getenv('NOTIFICATION_STATE', '/notification-state')) / 'telegram.db'), campus=Campus())
    ThreadingHTTPServer(('0.0.0.0', 8094), handler_for(service)).serve_forever()


if __name__ == '__main__':
    main()
