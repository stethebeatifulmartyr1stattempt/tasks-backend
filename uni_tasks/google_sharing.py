"""Only explicitly mapped class announcements may enter shared subjects."""
from email.utils import getaddresses, parseaddr
import re
from urllib.parse import urlsplit

from .core import fingerprint, normalize


def mapped_course(cfg, field, value):
    routing = cfg.get('discipline_routing', {})
    if not routing.get('enabled') or not routing.get('google_enabled'):
        return None
    matches = [c for c in routing.get('courses', []) if str(value) in {str(v) for v in c.get(field, [])}]
    return matches[0] if len(matches) == 1 else None


def fresh_mail_text(text):
    """Exclude quoted conversations and signature/footer content before extraction."""
    lines = []
    for line in text.splitlines():
        clean = line.strip()
        if clean.startswith('>') or re.match(r'^(On .+wrote:|.*(?:пише|писав|написав|wrote):)\s*$', clean, re.I):
            break
        if re.match(r'^(BR,?|Best regards,?|З повагою,?|--+)$', clean, re.I):
            break
        lines.append(line)
    return '\n'.join(lines).strip()


def private_content(text, cfg):
    lowered = normalize(text)
    if any(normalize(term) in lowered for term in cfg.get('google_private_terms', []) if len(term) > 3):
        return True
    return bool(re.search(r'(ваш[ауі]\s+(?:робот|звіт|оцінк)|переробіть|ваш[іа]\s+бал|your\s+(?:grade|submission))', text, re.I))


def mail_course(head, message, cfg, text):
    routing = cfg.get('discipline_routing', {})
    if not routing.get('enabled') or not routing.get('google_enabled'):
        return None
    if {'SENT', 'DRAFT', 'SPAM', 'TRASH', 'CATEGORY_PROMOTIONS'} & set(message.get('labelIds', [])):
        return None
    sender = parseaddr(head.get('from', ''))[1].casefold()
    recipients = {addr.casefold() for _, addr in getaddresses([head.get('to', ''), head.get('cc', '')]) if addr}
    subject = normalize(head.get('subject', ''))
    if private_content(head.get('subject', '') + '\n' + text, cfg):
        return None
    matches = []
    for course in routing.get('courses', []):
        for rule in course.get('gmail_rules', []):
            if (sender == rule['sender'].casefold()
                    and normalize(rule['subject_contains']) in subject
                    and len(recipients) >= max(3, rule.get('min_recipients', 5))):
                matches.append(course)
                break
    return matches[0] if len(matches) == 1 else None


def group_mail_source(head, message, cfg, text, at):
    clean = fresh_mail_text(text)
    course = mail_course(head, message, cfg, clean)
    if not clean or not course:
        return None
    # Same RFC message received in several mailboxes is one announcement.
    identity = head.get('message-id', '').strip() or fingerprint([at, clean])
    return {'key': 'gmail-group:' + fingerprint(identity)[:32],
            'title': 'Оголошення викладача · ' + course['title'], 'url': '',
            'project_id': cfg['project_id'], 'view_id': cfg['view_id'],
            'sharing_scope': 'course', 'course_id': course['id'],
            'messages': [{'at': at, 'role': 'teacher', 'text': clean[:24000]}]}


def material_links(work):
    links = []
    for material in work.get('materials', []):
        for kind in ('driveFile', 'youtubeVideo', 'link', 'form'):
            value = material.get(kind)
            if not value:
                continue
            if kind == 'driveFile':
                value = value.get('driveFile', {})
            url = value.get('alternateLink') or value.get('url') or value.get('formUrl')
            if url and urlsplit(url).scheme == 'https' and not urlsplit(url).username:
                links.append({'title': value.get('title') or 'Матеріал завдання', 'url': url})
    return links


def shared_classroom_source(course, work, cfg, stamp, due):
    mapped = mapped_course(cfg, 'classroom_ids', course['id'])
    # The API documents an omitted assigneeMode as ALL_STUDENTS. Never publish
    # individualized assignments, submission objects, grades or student files.
    if (not mapped or work.get('state') != 'PUBLISHED'
            or work.get('assigneeMode', 'ALL_STUDENTS') != 'ALL_STUDENTS'
            or work.get('individualStudentsOptions')):
        return None
    text = work['title'] + '\n' + work.get('description', '')
    if private_content(text, cfg):
        return None
    return {'key': f"classroom-group:{course['id']}:{work['id']}",
            'title': 'Classroom · ' + mapped['title'],
            'url': work.get('alternateLink', course.get('alternateLink', 'https://classroom.google.com/')),
            'project_id': cfg['project_id'], 'view_id': cfg['view_id'],
            'sharing_scope': 'course', 'course_id': mapped['id'],
            'structured': {'title': work['title'], 'course': mapped['title'],
                           'key': f"classroom:{course['id']}:{work['id']}", 'due': due, 'state': 'open',
                           'body': text[:24000], 'materials': material_links(work)},
            'messages': [{'at': stamp, 'role': 'teacher', 'text': text[:24000]}]}
