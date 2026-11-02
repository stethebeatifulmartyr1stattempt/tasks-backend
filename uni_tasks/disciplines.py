"""Route shared imports into projects whose membership enforces visibility."""
from .core import normalize


def course_for(routing, course, source_key, topic_id=None):
    courses = routing.get('courses', [])
    name = normalize(course)
    if name == normalize('Уточнення'):
        return None
    named = [c for c in courses if name and name in {
        normalize(c['title']), *(normalize(a) for a in c.get('aliases', []))}]
    parts = source_key.split(':')
    peer = parts[1] if len(parts) == 3 and parts[0] == 'telegram' else None
    by_chat = [c for c in courses if peer in {str(p) for p in c.get('telegram_chat_ids', [])}]
    by_topic = [c for c in courses if any(str(t['chat_id']) == peer and t['topic_id'] == topic_id
                for t in c.get('telegram_topics', []))] if topic_id else []
    if len(named) > 1 or len(by_chat) > 1 or len(by_topic) > 1:
        return None
    if by_topic:
        by_chat = by_topic
    if named and by_chat and named[0]['id'] != by_chat[0]['id']:
        return None
    # Unknown named subjects must not inherit a possibly unrelated channel.
    if name and not named:
        return None
    return named[0] if named else (by_chat[0] if by_chat else None)


def target_project(cfg, item, source):
    origin = source.get('project_id', cfg['project_id'])
    view = source.get('view_id', cfg['view_id'])
    routing = cfg.get('discipline_routing', {})
    if (routing.get('enabled') and routing.get('google_enabled') and origin == cfg['project_id']
            and source['key'].startswith(('gmail-group:', 'classroom-group:'))
            and source.get('sharing_scope') == 'course'):
        matches = [c for c in routing.get('courses', []) if c['id'] == source.get('course_id')]
        course = matches[0] if len(matches) == 1 else None
        if course and (not item.course or normalize(item.course) in {
                normalize(course['title']), *(normalize(a) for a in course.get('aliases', []))}):
            return course['project_id'], course['view_id']
        return routing['review_project_id'], routing['review_view_id']
    if (not routing.get('enabled') or origin != cfg['project_id']
            or not source['key'].startswith('telegram:')):
        return origin, view
    course = course_for(routing, item.course, source['key'], source.get('topic_id'))
    if course:
        return course['project_id'], course['view_id']
    return routing['review_project_id'], routing['review_view_id']


def recovery_projects(cfg, origin, target):
    projects = [target]
    routing = cfg.get('discipline_routing', {})
    if routing.get('enabled') and origin == cfg['project_id']:
        projects += [routing['review_project_id'], origin]
        projects += [c['project_id'] for c in routing['courses']]
    return list(dict.fromkeys(projects))
