"""Conservative cross-source identities. Never merge by fuzzy title alone."""
from datetime import datetime
import re
from .core import fingerprint, normalize
from .disciplines import course_for


def numbered_work(title):
    text=normalize(title)
    kinds=[('quiz',r'(?:опитування|quiz)\s+(?:no\s*)?(\d+)'),
           ('cp',r'(?:комп ютерн\w* практикум|кп)\s+(?:no\s*)?(\d+)'),
           ('test',r'(?:тест(?:ування)?|test)\s+(?:no\s*)?(\d+)'),
           ('mkr',r'(?:мкр|модульн\w* контрольн\w* робот\w*)\s+(?:no\s*)?(\d+)'),
           ('practice',r'(?:практичн\w* робот\w*|практична|practice)\s+(?:no\s*)?(\d+)'),
           ('lab',r'(?:лабораторн\w*(?: робот\w*)?|лр|lab)\s*(?:no\s*)?(\d+)')]
    # A test about a lab is a different deliverable from the lab itself.
    testish=bool(re.search(r'тест|опитування|quiz',text))
    for kind,pattern in kinds:
        if testish and kind not in ('test','quiz'):continue
        numbers=re.findall(pattern,text)
        if len(set(numbers))==1:
            match=re.search(pattern,text)
            if re.match(r'\s+(?:та\s+|i\s+)?\d+',text[match.end():]):return None
            qualifier='virtual:' if re.search(r'вiртуальн|virtual',text) else ''
            part=re.search(r'(?:частин\w*|part)\s+(?:no\s*)?(\d+)',text)
            return qualifier+kind+':'+str(int(numbers[0]))+(':part:'+str(int(part[1])) if part else '')
    return None


def identity(course_id,title,stamp,origin):
    work=numbered_work(title)
    if not work:return None
    try:date=datetime.fromisoformat(stamp.replace('Z','+00:00'))
    except (ValueError,AttributeError):return None
    semester=f'{date.year}-{1 if date.month<8 else 2}'
    return fingerprint([origin,course_id,semester,work])


def for_item(item,source,cfg):
    if source.get('project_id',cfg['project_id'])!=cfg['project_id']:return None
    routing=cfg.get('discipline_routing',{})
    if not routing.get('enabled'):return None
    if source.get('sharing_scope')=='course':
        if not source['key'].startswith(('gmail-group:','classroom-group:')):return None
        course=next((c for c in routing.get('courses',[]) if c['id']==source.get('course_id')),None)
    elif source['key'].startswith('telegram:'):
        course=course_for(routing,item.course,source['key'],source.get('topic_id'))
    else:return None
    return identity(course['id'],item.title,item.source_timestamp,cfg['project_id']) if course else None


def seed(api,store):
    if store.get('cross_source_seed_v2'):return
    with store.db:store.db.execute('DELETE FROM identities')
    for course in api.cfg.get('discipline_routing',{}).get('courses',[]):
        for task in api.tasks(course['project_id']):
            if task.get('project_id')!=course['project_id']:raise ValueError('Unexpected project')
            if not store.db.execute('SELECT 1 FROM tasks WHERE task_id=?',(task['id'],)).fetchone():continue
            key=identity(course['id'],task['title'],task.get('created',''),api.cfg['project_id'])
            if key:
                with store.db:
                    store.db.execute('''INSERT INTO identities VALUES (?,?) ON CONFLICT(identity) DO UPDATE SET
                        task_id=CASE WHEN task_id=excluded.task_id THEN task_id ELSE 0 END''',(key,task['id']))
    store.set('cross_source_seed_v2',True)
