from __future__ import annotations

import html
import json
from datetime import datetime

import httpx

from .core import config, secrets, fingerprint, normalize, resolve_due, now
from .disciplines import target_project, recovery_projects
from .google_sharing import private_content
from .dedup import for_item


class Vikunja:
    def __init__(self):
        self.cfg = config()
        self.http = httpx.Client(base_url=self.cfg['vikunja_api'],
          headers={'Authorization': 'Bearer ' + secrets()['vikunja_token']}, timeout=30)

    def request(self, method, path, **kwargs):
        r = self.http.request(method, path, **kwargs)
        r.raise_for_status()
        return r.json()

    def tasks(self, project_id=None, view_id=None):
        results, page = [], 1
        while True:
            batch = self.request('GET', '/tasks', params={'filter': f"project = {project_id or self.cfg['project_id']}",
                'page': page, 'per_page': 100, 'sort_by': 'id'}) or []
            results.extend(batch)
            # The server may clamp per_page below the requested value. Continue
            # until an empty page; never miss completed tasks hidden by a UI view.
            if not batch:
                return results
            page += 1

    def task(self, task_id):
        return self.request('GET', f'/tasks/{task_id}')

    def update(self, task_id, fields, current=None):
        # Use the exact snapshot on which the merge decision was based. Fetching
        # again here would bless an intervening manual edit as importer-owned.
        current = self.task(task_id) if current is None else current
        changes = {k: v for k, v in fields.items() if not same_field(k, current.get(k), v)}
        if not changes:
            return current
        checked = dict.fromkeys([*changes, 'project_id'])
        operations = [{'op': 'test', 'path': '/' + k, 'value': current[k]}
                      for k in checked if k in current]
        operations += [{'op': 'replace', 'path': '/' + k, 'value': v} for k, v in changes.items()]
        # Existing read/create clients use v1; partial writes belong to v2.
        base = str(self.http.base_url).rstrip('/').removesuffix('/v1')
        if not str(self.http.base_url).rstrip('/').endswith('/v2'):
            base += '/v2'
        response = self.http.request('PATCH', base + f'/tasks/{task_id}', json=operations,
                                    headers={'Content-Type': 'application/json-patch+json'})
        if response.status_code == 304:
            return current | changes
        response.raise_for_status()
        return response.json()


def task_key(item, project_id=2):
    return fingerprint([project_id, normalize(item.course), normalize(item.assignment_key)])[:24]


def same_field(field, a, b):
    if field == 'due_date' and a and b:
        try:
            return datetime.fromisoformat(a.replace('Z', '+00:00')) == datetime.fromisoformat(b.replace('Z', '+00:00'))
        except ValueError:
            return False
    return a == b


def apply_item(api, store, item, source):
    shared_google = source.get('sharing_scope') == 'course' and source['key'].startswith(('gmail-group:', 'classroom-group:'))
    if shared_google and (item.state != 'open' or private_content(item.title + '\n' + item.evidence, api.cfg)):
        return None
    text = '\n'.join(m['text'] for m in source['messages'])
    if not item.evidence or item.evidence not in text:
        raise ValueError('extraction evidence is absent from source')
    if item.confidence < .65 or not item.assignment_key.strip() or not item.title.strip():
        raise ValueError('extraction needs manual interpretation')
    origin_project = source.get('project_id', api.cfg['project_id'])
    project_id, view_id = target_project(api.cfg, item, source)
    # Routing must not change identity: existing links and deduplication survive
    # migration and a human moving an uncertain task into the correct subject.
    key = task_key(item, origin_project)
    row = store.db.execute('SELECT * FROM tasks WHERE canonical=?', (key,)).fetchone()
    identity=for_item(item,source,api.cfg)
    match=store.db.execute('SELECT task_id FROM identities WHERE identity=?',(identity,)).fetchone() if identity else None
    alias=False
    ambiguous=bool(match and not match['task_id'])
    if not row and match and match['task_id']:
        row=store.db.execute('SELECT * FROM tasks WHERE task_id=? LIMIT 1',(match['task_id'],)).fetchone()
        alias=bool(row)
    if ambiguous:
        project_id=api.cfg['discipline_routing']['review_project_id']
    due = source.get('structured', {}).get('due') or resolve_due(item, source, api.cfg.get('timezone', 'Europe/Kyiv'))
    review = ambiguous or item.confidence < .9 or item.review_reason or (item.due_text and not due)
    if due and due < now().isoformat() and item.state == 'open' and not shared_google:
        review = True
    title = f'{item.course}: {item.title}' if item.course else item.title
    title = ('[Перевірити] ' if review else '') + title
    marker = f'<!-- uni-task-manager:{key} -->'
    refs = json.loads(row['sources']) if row else {}
    new_source=source['key'] not in refs
    latest=max((v.get('at','') for v in refs.values()),default='')
    content=fingerprint([item.evidence,source.get('structured')])
    source_changed=refs.get(source['key'],{}).get('content')!=content
    refs[source['key']] = {'url': source['url'], 'title': source['title'], 'at':item.source_timestamp,'content':content}
    previous = json.loads(row['fields']) if row else {}
    detail = '<p>' + html.escape(item.evidence).replace('\n', '<br>') + '</p>'
    if shared_google and source.get('structured'):
        detail = '<p>' + html.escape(source['structured'].get('body', item.evidence)).replace('\n', '<br>') + '</p>'
        detail += '<p>' + '<br>'.join('<a href="' + html.escape(m['url'], quote=True) + '">' +
            html.escape(m['title']) + '</a>' for m in source['structured'].get('materials', [])) + '</p>'
    detail += '<p>' + '<br>'.join(f'<a href="{html.escape(v["url"], quote=True)}">{html.escape(v["title"])}</a>' if v['url'] else html.escape(v['title']) for v in refs.values()) + '</p>'
    if review:
        detail += '<p>Потрібна перевірка: ' + html.escape(item.review_reason or 'перевір дедлайн і стан завдання') + '</p>'
    if item.state == 'submitted':
        detail += '<p>У листуванні є підтвердження надсилання роботи. Перевір, чи викладач її прийняв.</p>'
    if item.state == 'cancelled':
        detail += '<p>У джерелі є повідомлення про скасування. Перевір перед закриттям.</p>'
    desired = {'title': title[:250], 'description': detail + marker}
    if row and (alias or len(refs)>1):
        desired['title']=previous.get('title',desired['title'])
        # Preserve rich Classroom materials when another channel mentions the same work.
        desired['description']=previous.get('description','')
        if source_changed:
            label='Додаткове джерело' if new_source else 'Оновлення джерела'
            desired['description'] += '<p>'+label+' ('+html.escape(item.source_timestamp[:10])+'): <a href="'+html.escape(source['url'],quote=True)+'">'+html.escape(source['title'])+'</a></p>'+detail
    if due:
        desired['due_date'] = due
    elif shared_google and source.get('structured'):
        desired['due_date'] = '0001-01-01T00:00:00Z'
    if latest and item.source_timestamp<latest:
        desired.pop('due_date',None)
    if not row:
        # Recover a create whose HTTP response or local commit was lost.
        found = None
        for candidate in recovery_projects(api.cfg, origin_project, project_id):
            found = next((t for t in api.tasks(candidate) if marker in t.get('description', '')), None)
            if found:
                break
        if found:
            current = found
        elif item.state in ('submitted', 'cancelled'):
            return None  # Historical submissions aren't new pending assignments.
        else:
            current = api.request('PUT', f'/projects/{project_id}/tasks', json=desired)
        task_id = current['id']
        previous = {k: current.get(k, v) for k, v in desired.items()}
        previous['done'] = current.get('done', False)
    else:
        task_id = row['task_id']
        current = api.task(task_id)
        changes = {}
        for field, value in desired.items():
            # Leave a user-edited title/description/deadline alone.
            if same_field(field, current.get(field), previous.get(field)) or (field == 'due_date' and not current.get(field, '').startswith(('20', '19'))):
                changes[field] = value
        if source.get('structured') and item.state == 'submitted' and not current.get('done') and not previous.get('done'):
            changes['done'] = True
        # Never reopen a completed task or automatically close a cancellation.
        if changes:
            saved = api.update(task_id, changes, current=current)
            changes = {k: saved.get(k, v) for k, v in changes.items()}
        previous = {**previous, **changes}
    with store.db:
        store.db.execute('UPDATE tasks SET fields=?,sources=? WHERE task_id=?',
          (json.dumps(previous),json.dumps(refs,ensure_ascii=False),task_id))
        store.db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?,?,?)',
          (key, task_id, json.dumps(previous), json.dumps(refs, ensure_ascii=False)))
        if identity and not ambiguous:
            store.db.execute('INSERT OR IGNORE INTO identities VALUES (?,?)',(identity,task_id))
    return task_id
