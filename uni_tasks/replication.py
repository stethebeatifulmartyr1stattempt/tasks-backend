"""Personal task copies from explicitly selected shared subjects.

Separate identity, config and state: no mail, Telegram or enrollment credential.
The service never writes to source projects and never sends completion fields.
"""
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import json
import logging
import os
import sqlite3
import time

import httpx
from .campus import Campus

FIELDS = ('title', 'description', 'due_date', 'start_date', 'end_date', 'priority')


def marker(user_id, source_id):
    return f'<!-- pi42-personal:{user_id}:{source_id} -->'


def desired_fields(source, course, user_id, public_url, variant=''):
    description = source.get('description') or ''
    footer = ('<p><small>Особиста копія · ' + escape(course['title']) +
              ' · <a href="' + escape(public_url, quote=True) + '/tasks/' + str(source['id']) +
              '">Спільне завдання</a>. Позначка виконання належить тільки тобі.' +
              (' Варіант: '+escape(variant)+'.' if variant else '') + '</small></p>')
    return {key: source.get(key, 0 if key == 'priority' else '') for key in FIELDS} | {
        'description': description + footer + marker(user_id, source['id']) + f'<!-- pi42-course:{course["id"]} -->'}


class API:
    def __init__(self, base_url, token):
        self.http = httpx.Client(base_url=base_url, timeout=20,
                                 headers={'Authorization': 'Bearer ' + token})

    def request(self, method, path, **kwargs):
        result = self.http.request(method, path, **kwargs)
        result.raise_for_status()
        return result.json() if result.content else None

    def pages(self, path, **params):
        page = 1
        while True:
            result = self.request('GET', path, params={**params, 'page': page})
            yield from result.get('items') or []
            if page >= result.get('total_pages', 1):
                return
            page += 1

    def tasks(self, project):
        # Completed tasks must be included for idempotence and independent progress.
        return list(self.pages('/tasks', filter=f'project = {project}'))

    def members(self, project):
        return list(self.pages(f'/projects/{project}/users'))

    def project(self, project):
        return self.request('GET', f'/projects/{project}')

    def get(self, task):
        return self.request('GET', f'/tasks/{task}')

    def create(self, project, fields):
        return self.request('POST', f'/projects/{project}/tasks', json=fields)

    def patch(self, task, current, changes):
        # Test the fields observed before the merge. A concurrent edit causes a
        # retry next cycle, rather than overwriting it with a stale snapshot.
        # Never include done, done_at, percent_done, assignees or reminders.
        operations = [{'op': 'test', 'path': '/' + key, 'value': current[key]} for key in changes]
        operations += [{'op': 'replace', 'path': '/' + key, 'value': value} for key, value in changes.items()]
        return self.request('PATCH', f'/tasks/{task}', json=operations,
                            headers={'Content-Type': 'application/json-patch+json'})


class Replicator:
    def __init__(self, catalog, api, path, campus=None):
        self.catalog, self.api = catalog, api
        self.campus=campus
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS copies (
            user_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
            task_id INTEGER, baseline TEXT NOT NULL,
            PRIMARY KEY(user_id, source_id))''')
        self.db.commit()
        project_ids = [s['personal_project_id'] for s in catalog['students']]
        source_ids = {c['project_id'] for c in catalog['courses']}
        if len(project_ids) != len(set(project_ids)) or source_ids.intersection(project_ids):
            raise ValueError('Personal projects must be unique and separate from sources')

    def save(self, user, source, task, baseline):
        self.db.execute('INSERT OR REPLACE INTO copies VALUES (?,?,?,?)',
                        (user, source, task, json.dumps(baseline)))
        self.db.commit()

    def sync_task(self, student, course, source, personal):
        uid, sid = student['user_id'], source['id']
        pid = student['personal_project_id']
        desired = desired_fields(source, course, uid, self.catalog['public_url'],
            self.campus.variants(uid).get(course['id'],'') if self.campus else '')
        record = self.db.execute('SELECT * FROM copies WHERE user_id=? AND source_id=?', (uid, sid)).fetchone()
        if not record or record['task_id'] is None:
            # Recovery after create succeeded but the response/SQLite commit was lost.
            matches = [t for t in personal if marker(uid, sid) in (t.get('description') or '')]
            if len(matches) > 1:
                raise ValueError('Conflicting personal copy markers')
            if matches:
                current = matches[0]
                baseline = json.loads(record['baseline']) if record else desired
            else:
                # Pending record is durable before the non-idempotent HTTP create.
                self.save(uid, sid, None, desired)
                current = self.api.create(pid, desired | {'done': False})
                personal.append(current)
                baseline = {key: current.get(key) for key in FIELDS}
            self.save(uid, sid, current['id'], baseline)
            record = self.db.execute('SELECT * FROM copies WHERE user_id=? AND source_id=?', (uid, sid)).fetchone()
        try:
            current = self.api.get(record['task_id'])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                return  # A deleted personal copy stays deleted, including after rejoin.
            raise
        if current.get('project_id') != pid:
            return  # Respect a manual move; never follow it into a shared project.
        baseline = json.loads(record['baseline'])
        changes = {k: v for k, v in desired.items() if current.get(k) == baseline.get(k) and current.get(k) != v}
        if changes:
            saved = self.api.patch(current['id'], current, changes)
            # Only this write's response belongs to us. A later GET could already
            # contain a student's edit, which must never become our baseline.
            baseline.update({k: saved[k] for k in changes})
            self.save(uid, sid, current['id'], baseline)

    def cycle(self):
        errors = 0
        selected = {s['user_id']: [] for s in self.catalog['students']}
        for course in self.catalog['courses']:
            try:
                members = {m['username'] for m in self.api.members(course['project_id'])}
                sources = self.api.tasks(course['project_id'])
                # Defense against an API filter regression or private-project mixup.
                if any(t.get('project_id') != course['project_id'] for t in sources):
                    raise ValueError('Source project mismatch')
                for student in self.catalog['students']:
                    if student['user_id'] == self.catalog['owner_id'] or student['username'] in members:
                        selected[student['user_id']].append((course, sources))
            except (httpx.HTTPError, ValueError, KeyError):
                errors += 1
        for student in self.catalog['students']:
            if not selected[student['user_id']]:
                continue
            try:
                pid = student['personal_project_id']
                project = self.api.project(pid)
                if project.get('owner', {}).get('id') != student['user_id'] or project.get('parent_project_id'):
                    raise ValueError('Personal project ownership changed')
                personal = self.api.tasks(pid)
                for course, sources in selected[student['user_id']]:
                    # Recheck membership immediately before copying this course.
                    if student['user_id'] != self.catalog['owner_id'] and student['username'] not in {
                        m['username'] for m in self.api.members(course['project_id'])}:
                        continue
                    for source in sources:
                        try:
                            self.sync_task(student, course, source, personal)
                        except (httpx.HTTPError, ValueError, KeyError):
                            errors += 1
            except (httpx.HTTPError, ValueError, KeyError):
                errors += 1
        return errors


def main():
    import fcntl  # Production worker runs on Linux; lock prevents concurrent writers.
    os.umask(0o077)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    config = Path(os.getenv('REPLICATION_CONFIG', '/replication-config'))
    state = Path(os.getenv('REPLICATION_STATE', '/replication-state'))
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        catalog = json.loads((config / 'catalog.json').read_text('utf-8'))
        api = API(catalog['vikunja_api'], (config / 'token.txt').read_text('utf-8').strip())
        worker = Replicator(catalog, api, state / 'copies.db', campus=Campus())
        while True:
            try:
                errors = worker.cycle()
            except Exception:
                errors = 1
            status = {'checked_at': datetime.now(timezone.utc).isoformat(), 'errors': errors,
                      'copies': worker.db.execute('SELECT COUNT(*) FROM copies WHERE task_id IS NOT NULL').fetchone()[0]}
            (state / 'status.next').write_text(json.dumps(status), 'utf-8')
            (state / 'status.next').replace(state / 'status.json')
            worker.campus.status('replication',status)
            print(json.dumps(status), flush=True)
            time.sleep(20)


if __name__ == '__main__':
    main()
