"""Caller-scoped calendar and preferences; owner-only import review."""
import json
import httpx
from datetime import datetime
from .campus import task_course
from .core import fingerprint
from .errors import Problem


class CampusAPI:
    def caller(self, user, authorization, path, method='GET', **kwargs):
        response = self.http.request(method, path, headers={'Authorization': authorization,
            **kwargs.pop('headers', {})}, **kwargs)
        if response.status_code in (403,404):
            raise Problem(403,'Немає доступу до цього списку або завдання.')
        if not response.is_success:
            raise Problem(409 if response.status_code in (409,412,422) else 503,
                          'Дані змінилися або недоступні. Онови сторінку.')
        return response.json() if response.content else {}

    def personal(self, user, authorization):
        pid = self.students[user['id']]['personal_project_id']
        project = self.caller(user, authorization, f'/projects/{pid}')
        if project.get('owner', {}).get('id') != user['id'] or project.get('parent_project_id'):
            raise Problem(403, 'Особистий список недоступний.')
        return pid

    def calendar(self, user, authorization):
        pid = self.personal(user, authorization)
        result, page = [], 1
        variants, muted = self.campus.variants(user['id']), self.campus.muted(user['id'])
        while True:
            batch = self.caller(user, authorization, '/tasks', params={'filter':f'project = {pid}','page':page})
            for task in batch.get('items') or []:
                if task.get('project_id') != pid: raise Problem(503, 'Некоректна відповідь списку.')
                course = task_course(task, self.courses.values())
                result.append({k:task.get(k) for k in ('id','title','due_date','done')} | {
                    'course_id':course['id'] if course else '',
                    'variant':variants.get(course['id'],'') if course else '', 'muted':task['id'] in muted})
            if page >= batch.get('total_pages',1): break
            page += 1
        return {'tasks':result}

    def campus_action(self, user, authorization, path, method, body):
        if self.campus is None: raise Problem(503,'Налаштування тимчасово недоступні.')
        uid = user['id']
        try:
            if path == '/preferences':
                if method == 'GET': return self.campus.preferences(uid)
                if method == 'PUT': return self.campus.save_preferences(uid,body)
            if path == '/tasks' and method == 'GET': return self.calendar(user,authorization)
            parts = path.strip('/').split('/')
            if len(parts)==3 and parts[0]=='subjects' and parts[2]=='variant' and method=='PUT':
                course = self.courses.get(parts[1])
                if not course or not self.course_state(course,user)['selected']: raise Problem(403,'Спочатку обери дисципліну.')
                if not isinstance(body,dict) or set(body)!={'variant'}: raise ValueError('Передай номер варіанта.')
                return {'variant':self.campus.variant(uid,course['id'],body['variant'])}
            if len(parts)==3 and parts[0]=='tasks' and parts[2]=='notifications' and parts[1].isdigit() and method=='PUT':
                if not isinstance(body,dict) or set(body)!={'muted'} or type(body['muted']) is not bool:
                    raise ValueError('Передай muted: true або false.')
                pid = self.personal(user,authorization)
                task = self.caller(user,authorization,f'/tasks/{int(parts[1])}')
                if task.get('project_id')!=pid: raise Problem(403,'Це не твоє особисте завдання.')
                self.campus.mute(uid,task['id'],body['muted'])
                return body
            if path.startswith('/admin/'):
                if uid!=self.catalog['owner_id']: raise Problem(403,'Тільки для адміністратора.')
                if path=='/admin/imports' and method=='GET': return self.campus.panel()
                if len(parts)==3 and parts[1]=='reviews' and parts[2].isdigit() and method=='POST':
                    with self.lock: return self.resolve_review(user,authorization,int(parts[2]),body)
        except ValueError as exc: raise Problem(422,str(exc)) from None
        raise Problem(404,'Сторінку не знайдено.')

    def resolve_review(self,user,authorization,tid,body):
        if not isinstance(body,dict) or set(body)!={'action','version','title','course_id','due_date'}:
            raise ValueError('Некоректне рішення перевірки.')
        if any(not isinstance(value,str) for value in body.values()):
            raise ValueError('Поля рішення мають бути текстом.')
        with self.campus.db() as db: row=db.execute("SELECT * FROM reviews WHERE task_id=? AND state='open'",(tid,)).fetchone()
        if not row or row['version']!=body['version']: raise Problem(409,'Цю перевірку вже змінено. Онови сторінку.')
        if body['action'] not in ('approve','dismiss'): raise ValueError('Невідома дія.')
        payload=json.loads(row['payload'])
        current=self.caller(user,authorization,f'/tasks/{tid}')
        if review_version(current)!=row['version']: raise Problem(409,'Завдання змінилося. Онови чергу перевірки.')
        if body['action']=='approve':
            course=self.courses.get(body['course_id'])
            title=body['title']
            if not course or not isinstance(title,str) or not 1<=len(title.strip())<=250:
                raise ValueError('Обери дисципліну та введи назву до 250 символів.')
            due=body['due_date']
            if due:
                value=datetime.fromisoformat(due.replace('Z','+00:00'))
                if not value.tzinfo or not 2000<=value.year<=2100: raise ValueError('Дедлайн має містити часовий пояс.')
            changes={'title':title.strip().removeprefix('[Перевірити] '),
                     'due_date':due or '0001-01-01T00:00:00Z'}
            import re
            description=re.sub(r'<p>Потрібна перевірка:.*?</p>','',current.get('description',''),flags=re.S)
            description=re.sub(r'<!-- pi42-course:[a-z0-9-]+ -->','',description)
            changes['description']=description+f'<!-- pi42-course:{course["id"]} -->'
            if payload['shareable']: changes['project_id']=course['project_id']
        else:
            # Dismiss only the review, never silently complete a student's work.
            changes={}
        if changes:
            ops=[{'op':'test','path':'/'+k,'value':current[k]} for k in ('title','description','due_date','project_id','done')]
            ops += [{'op':'replace','path':'/'+k,'value':v} for k,v in changes.items()]
            self.caller(user,authorization,f'/tasks/{tid}','PATCH',json=ops,headers={'Content-Type':'application/json-patch+json'})
        with self.campus.db() as db:
            db.execute("UPDATE reviews SET state=? WHERE task_id=? AND version=?",(body['action'],tid,row['version']))
        return {'ok':True}


def review_version(task):
    return fingerprint({k:task.get(k) for k in ('title','description','due_date','project_id','done')})


def publish_reviews(api,store):
    """Refresh actual task snapshots; private source text never leaves admin API."""
    for row in store.db.execute('SELECT DISTINCT task_id,sources FROM tasks').fetchall():
        try:task=api.task(row['task_id'])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404,410):continue
            raise
        review_project=api.cfg.get('discipline_routing',{}).get('review_project_id')
        if task.get('done') or ('[Перевірити]' not in task['title'] and task['project_id']!=review_project):
            with store.campus.db() as db:db.execute("UPDATE reviews SET state='resolved' WHERE task_id=? AND state='open'",(task['id'],))
            continue
        refs=json.loads(row['sources'])
        sources=[]
        for key in refs:
            source=store.db.execute('SELECT payload FROM sources WHERE key=?',(key,)).fetchone()
            if source:sources.append(json.loads(source[0]))
        shared=bool(sources) and all(s.get('project_id',api.cfg['project_id'])==api.cfg['project_id'] and
            (s['key'].startswith('telegram:') or s.get('sharing_scope')=='course') for s in sources)
        course=next((c for c in api.cfg.get('discipline_routing',{}).get('courses',[]) if c['project_id']==task['project_id']),None)
        import re, html
        evidence=html.unescape(re.sub('<[^>]*>',' ',task.get('description','')))[:3000]
        store.campus.review(task['id'],{'title':task['title'],'due_date':task.get('due_date',''),
            'course_id':course['id'] if course else '', 'shareable':shared,'evidence':evidence,
            'sources':[{'title':v['title'],'url':v['url']} for v in refs.values()],
            'reason':next((s['attachment_review'] for s in sources if s.get('attachment_review')),'Перевір назву, дисципліну та дедлайн.')},review_version(task))
