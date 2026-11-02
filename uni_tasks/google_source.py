"""Read-only Gmail and Classroom imports, one OAuth token per Google account."""
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from html.parser import HTMLParser
from urllib.parse import quote

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession, Request

from .core import CONFIG, config, now, read_json, save_json
from .google_sharing import group_mail_source, shared_classroom_source


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'li', 'tr'):
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, value):
        if not self.hidden:
            self.text.append(value)


def body_text(part):
    mime = part.get('mimeType', '')
    parts = part.get('parts', [])
    if parts:
        if mime == 'multipart/alternative':
            preferred = next((p for p in parts if p.get('mimeType') == 'text/plain'), parts[-1])
            return body_text(preferred)
        return '\n'.join(body_text(p) for p in parts)
    data = part.get('body', {}).get('data', '')
    if not data or mime not in ('text/plain', 'text/html'):
        return ''
    decoded = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', 'replace')
    if mime == 'text/html':
        parser = PlainHTML()
        parser.feed(decoded)
        decoded = ''.join(parser.text)
    return decoded.strip()


def headers(message):
    return {h['name'].lower(): h['value'] for h in message.get('payload', {}).get('headers', [])}


def allowed_sender(address, cfg):
    address = parseaddr(address)[1].casefold()
    domain = address.rpartition('@')[2]
    return address in {s.casefold() for s in cfg.get('gmail_senders', [])} or any(
        domain == d or domain.endswith('.' + d) for d in cfg.get('gmail_domains', []))


def university_subject(subject, cfg):
    return bool(re.search(r'лаборатор|\bМКР\b|залік|курсов|ПІ[- ]?4[12]|контрольн', subject, re.I)) or any(
        name.casefold() in subject.casefold() for name in cfg.get('course_names', []))


def get(session, url, params=None):
    response = session.get(url, params=params, timeout=40)
    response.raise_for_status()
    return response.json()


def pages(session, url, field, **params):
    while True:
        result = get(session, url, params)
        yield from result.get(field, [])
        token = result.get('nextPageToken')
        if not token:
            break
        params['pageToken'] = token


def collect_gmail(store, account, session, cfg):
    base = 'https://gmail.googleapis.com/gmail/v1/users/me'
    key = 'gmail_history:' + account
    history = store.get(key)
    last = store.get('gmail_last:' + account)
    candidates = set()
    try:
        if history:
            final_history = history
            token = None
            while True:
                params = {'startHistoryId': history, 'historyTypes': 'messageAdded', 'maxResults': 500}
                if token:
                    params['pageToken'] = token
                result = get(session, base + '/history', params)
                for event in result.get('history', []):
                    candidates.update(x['message']['id'] for x in event.get('messagesAdded', []))
                final_history = result.get('historyId', final_history)
                token = result.get('nextPageToken')
                if not token:
                    break
        else:
            raise LookupError('full sync')
    except Exception as exc:
        if not isinstance(exc, LookupError) and getattr(getattr(exc, 'response', None), 'status_code', None) != 404:
            raise
        final_history = get(session, base + '/profile')['historyId']
        since = datetime.fromisoformat(last) - timedelta(days=2) if last else now() - timedelta(days=cfg.get('initial_days', 30))
        terms = ['from:' + s for s in cfg.get('gmail_senders', []) + cfg.get('gmail_domains', [])]
        terms += ['subject:' + word for word in ['лабораторна','лабораторні','МКР','залік','ПІ-42','курсовий']]
        query = '-in:trash -in:spam -in:sent -category:promotions after:' + str(int(since.timestamp())) + ' {' + ' '.join(terms) + '}'
        candidates.update(m['id'] for m in pages(session, base + '/messages', 'messages', q=query, maxResults=500))
    # One bounded backfill after sharing rules change; normal sync stays incremental.
    revision = cfg.get('discipline_routing', {}).get('google_revision', 0)
    threads = {r[0].split(':', 2)[2] for r in store.db.execute(
        'SELECT key FROM sources WHERE key LIKE ?', ('gmail:' + account + ':%',))} if (
            revision and store.get('gmail_sharing_revision:' + account) != revision) else set()
    for mid in sorted(candidates):
        meta = get(session, base + '/messages/' + mid, {'format': 'metadata', 'metadataHeaders': ['From', 'Subject']})
        if {'SPAM', 'TRASH', 'CATEGORY_PROMOTIONS'} & set(meta.get('labelIds', [])):
            continue
        head = headers(meta)
        sender = parseaddr(head.get('from', ''))[1].casefold()
        if cfg.get('classroom_ready') and sender.rpartition('@')[2].endswith('classroom.google.com'):
            continue
        if 'SENT' in meta.get('labelIds', []):
            if not store.db.execute('SELECT 1 FROM sources WHERE key=?', ('gmail:' + account + ':' + meta['threadId'],)).fetchone():
                continue
        elif not (allowed_sender(head.get('from', ''), cfg) or university_subject(head.get('subject', ''), cfg)):
            continue
        threads.add(meta['threadId'])
    for tid in sorted(threads):
        thread = get(session, base + '/threads/' + tid, {'format': 'full'})
        messages = []
        subject = ''
        for message in thread.get('messages', []):
            head = headers(message)
            sent = 'SENT' in message.get('labelIds', [])
            if not sent and not (allowed_sender(head.get('from', ''), cfg) or university_subject(head.get('subject', ''), cfg)):
                continue
            subject = head.get('subject', subject)
            text = body_text(message.get('payload', {}))
            if text:
                at = datetime.fromtimestamp(int(message['internalDate']) / 1000, timezone.utc).isoformat()
                shared = group_mail_source(head, message, cfg, text, at)
                if shared:
                    store.ingest(shared)
                    # Do not also generate a second task in the owner's private
                    # thread. Personal replies/feedback below remain private.
                    continue
                messages.append({
                    'at': at,
                    'role': 'student_submission' if sent else 'teacher', 'text': text[:18000],
                })
        if messages:
            # Personal correspondence is never copied to the shared class project.
            store.ingest({'key': 'gmail:' + account + ':' + tid, 'title': subject,
                'url': 'https://mail.google.com/mail/u/?authuser=' + quote(account) + '#all/' + tid,
                'project_id': cfg['private_project_id'], 'view_id': cfg['private_view_id'],
                'messages': messages[-20:]})
    store.set(key, str(final_history))
    store.set('gmail_last:' + account, now().isoformat())
    store.set('gmail_sharing_revision:' + account, revision)
    return {'state': 'synced', 'threads': len(threads), 'at': now().isoformat()}


def collect_classroom(store, account, session, cfg):
    base = 'https://classroom.googleapis.com/v1'
    count = 0
    for course in pages(session, base + '/courses', 'courses', courseStates='ACTIVE', studentId='me', pageSize=100):
        submissions = {s['courseWorkId']: s.get('state') for s in pages(session,
            base + '/courses/' + course['id'] + '/courseWork/-/studentSubmissions',
            'studentSubmissions', userId='me', pageSize=100)}
        for work in pages(session, base + '/courses/' + course['id'] + '/courseWork', 'courseWork', pageSize=100):
            stamp = work.get('updateTime', work.get('creationTime'))
            due = None
            date, clock = work.get('dueDate'), work.get('dueTime', {})
            if date:
                due = datetime(date['year'], date['month'], date['day'], clock.get('hours', 0), clock.get('minutes', 0), clock.get('seconds', 0), tzinfo=timezone.utc).isoformat()
            if datetime.fromisoformat(stamp.replace('Z', '+00:00')) < now() - timedelta(days=cfg.get('initial_days', 30)) and (not due or datetime.fromisoformat(due) < now()):
                continue
            text = work['title'] + '\n' + work.get('description', '')
            shared = shared_classroom_source(course, work, cfg, stamp, due)
            if shared:
                store.ingest(shared)
                # Private progress is not a property of the shared assignment.
                # Existing personal task IDs/progress are adopted during migration.
                count += 1
                continue
            store.ingest({'key': 'classroom:' + course['id'] + ':' + work['id'],
                'title': course['name'], 'url': work.get('alternateLink', course.get('alternateLink', 'https://classroom.google.com/')),
                'project_id': cfg['private_project_id'], 'view_id': cfg['private_view_id'],
                'structured': {'title': work['title'], 'course': course['name'], 'key': 'classroom:' + work['id'], 'due': due,
                    'state': 'submitted' if submissions.get(work['id']) in ('TURNED_IN', 'RETURNED') else 'open'},
                'messages': [{'at': stamp, 'role': 'teacher', 'text': text[:24000]}]})
            count += 1
    return {'state': 'synced', 'assignments': count, 'at': now().isoformat()}


def collect(store):
    cfg = config()
    results = {}
    for account in cfg.get('google_accounts', []):
        path = CONFIG / ('google-' + account + '.json')
        if not path.exists():
            results[account] = {'state': 'authorization_required'}
            continue
        try:
            creds = Credentials.from_authorized_user_info(read_json(path))
            if not creds.valid:
                creds.refresh(Request())
                save_json(path, json.loads(creds.to_json()))
            session = AuthorizedSession(creds)
            result = {}
            local_cfg = dict(cfg)
            for name, fn in [('classroom', collect_classroom), ('gmail', collect_gmail)]:
                try:
                    result[name] = fn(store, account, session, local_cfg)
                    if name == 'classroom':
                        local_cfg['classroom_ready'] = True
                except Exception as exc:
                    result[name] = {'state': 'error', 'error': type(exc).__name__}
            session.close()
            results[account] = result
        except Exception as exc:
            results[account] = {'state': 'authorization_error', 'error': type(exc).__name__}
    return results
