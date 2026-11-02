from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import httpx
from uni_tasks.campus import Campus, DEFAULTS, quiet_at
from uni_tasks.core import Store
from uni_tasks.dedup import numbered_work, identity
from uni_tasks.vikunja import apply_item
from uni_tasks.enrollment import Enrollment, Problem
from uni_tasks.campus_api import review_version
from test_pipeline import fixture, API
import test_personal_notifications


class CampusTests(unittest.TestCase):
    def setUp(self):
        tmp=TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.campus=Campus(Path(tmp.name)/'campus.db')

    def test_quiet_midnight_and_day_boundaries(self):
        prefs=DEFAULTS|{'quiet_enabled':True}
        self.assertTrue(quiet_at(prefs,datetime.fromisoformat('2026-10-27T22:00:00+02:00')))
        self.assertTrue(quiet_at(prefs,datetime.fromisoformat('2026-10-28T07:59:00+02:00')))
        self.assertFalse(quiet_at(prefs,datetime.fromisoformat('2026-10-28T08:00:00+02:00')))
        prefs.update(quiet_start='12:00',quiet_end='14:00')
        self.assertTrue(quiet_at(prefs,datetime.fromisoformat('2026-10-27T13:00:00+02:00')))
        self.assertFalse(quiet_at(prefs,datetime.fromisoformat('2026-10-27T23:00:00+02:00')))

    def test_digest_next_morning_once_across_restart_and_dst(self):
        at=datetime.fromisoformat('2026-10-24T12:00:00+03:00')
        self.campus.save_preferences(2,DEFAULTS|{'digest_enabled':True},at.timestamp())
        self.assertFalse(self.campus.digest_due(2,at))
        morning=datetime.fromisoformat('2026-10-25T08:00:00+02:00')
        self.assertTrue(self.campus.digest_due(2,morning))
        self.campus.digest_sent(2,morning)
        self.assertFalse(Campus(self.campus.path).digest_due(2,morning+timedelta(hours=1)))
        self.assertTrue(self.campus.digest_due(2,morning+timedelta(days=1)))
        self.assertFalse(self.campus.digest_due(3,morning))

    def test_invalid_preferences_and_personal_scope(self):
        for change in ({'reminder_hours':[0]},{'reminder_hours':[True]},{'quiet_start':'25:00'},{'quiet_enabled':'true'}):
            with self.assertRaises(ValueError):self.campus.save_preferences(2,DEFAULTS|change)
        self.campus.variant(2,'web','9');self.campus.mute(2,77,True)
        self.assertEqual(self.campus.variants(3),{})
        self.assertEqual(self.campus.muted(3),set())

    def test_source_authorization_errors_mark_service_unhealthy(self):
        from uni_tasks.worker import connection_errors
        self.assertEqual(connection_errors({'google':{'private-account':{'gmail':{'state':'authorization_required'},'classroom':{'state':'synced'}}}}),1)
        self.assertEqual(connection_errors({'telegram':{'state':'synced'},'parser':{'state':'idle'}}),0)


class NotificationPreferencesTests(unittest.TestCase):
    def setUp(self):
        test_personal_notifications.PersonalNotificationTests.setUp(self)
        self.campus=Campus(Path(self.tmp.name)/'campus.db')
        self.service.campus=self.campus

    def test_mute_and_quiet_revalidate_already_queued(self):
        self.campus.mute(2,11,True)
        self.service.reminders()
        self.assertEqual([d['chat_id'] for m,d in self.bot.calls if m=='sendMessage'],[300])
        self.campus.mute(2,11,False)
        self.campus.save_preferences(2,DEFAULTS|{'quiet_enabled':True,'quiet_start':'00:00','quiet_end':'23:59'})
        self.at=self.at.replace(hour=10)
        self.api.data[11]['due_date']=(self.at+timedelta(hours=2)).isoformat()
        self.service.reminders()
        self.assertNotIn(200,[d['chat_id'] for m,d in self.bot.calls if m=='sendMessage'])

    def test_custom_interval_and_digest_exclude_done_muted_other_user(self):
        binding=self.links.binding(user_id=2)
        self.campus.save_preferences(2,DEFAULTS|{'reminder_hours':[48]})
        self.api.data[11]['due_date']=(self.at+timedelta(hours=30)).isoformat()
        self.assertEqual(self.service.bucket(self.api.data[11],binding),'48')
        self.at=datetime.fromisoformat('2026-10-28T09:00:00+02:00')
        self.api.data[11]['due_date']=(self.at+timedelta(hours=2)).isoformat()
        self.campus.save_preferences(2,DEFAULTS|{'digest_enabled':True},self.at.timestamp()-86400)
        self.service.digests();self.service.digests()
        sent=[d for m,d in self.bot.calls if m=='sendMessage']
        self.assertEqual(len(sent),1)
        self.assertIn('Student A',sent[0]['text']);self.assertNotIn('Student B',sent[0]['text'])
        self.at+=timedelta(days=1)
        self.campus.mute(2,11,True)
        self.service.digests()
        self.assertNotIn('Student A',self.bot.calls[-1][1]['text'])


class DedupTests(unittest.TestCase):
    def test_distinct_deliverables_and_semesters(self):
        self.assertEqual(numbered_work('Лабораторна робота №4'),'lab:4')
        self.assertEqual(numbered_work('ЛР4'),'lab:4')
        self.assertIsNone(numbered_work('ЛР 4 та 5'))
        self.assertIsNone(numbered_work('Скласти тест-контроль до ЛР №4'))
        self.assertNotEqual(numbered_work('Віртуальна ЛР4'),numbered_work('ЛР4'))
        self.assertNotEqual(numbered_work('ЛР4 частина 1'),numbered_work('ЛР4 частина 2'))
        self.assertNotEqual(identity('web','ЛР4','2026-09-01',2),identity('web','ЛР4','2027-02-01',2))

    def test_three_sources_one_task_preserve_completion_and_links(self):
        api=API();api.cfg=deepcopy(api.cfg)|{'discipline_routing':{'enabled':True,'google_enabled':True,
            'review_project_id':2,'review_view_id':5,'courses':[{'id':'python','title':'Python','project_id':30,'view_id':117}]}}
        store=Store(':memory:');self.addCleanup(store.db.close)
        item,source=fixture();item.title='Лабораторна робота №4'
        first=apply_item(api,store,item,source)
        api.data[first]['done']=True
        for index,kind in enumerate(('classroom-group','gmail-group')):
            other=deepcopy(source);other.update(key=f'{kind}:new:{index}',sharing_scope='course',course_id='python',url=f'https://example.com/{index}')
            newer=item.model_copy(update={'assignment_key':f'other-{index}'})
            self.assertEqual(apply_item(api,store,newer,other),first)
        self.assertEqual(api.created,1);self.assertTrue(api.data[first]['done'])
        self.assertIn('https://example.com/0',api.data[first]['description'])
        self.assertIn('https://example.com/1',api.data[first]['description'])
        updated=deepcopy(other)
        updated['messages'][0]['text']='Змінена інструкція: додати висновок.'
        update=newer.model_copy(update={'evidence':updated['messages'][0]['text']})
        apply_item(api,store,update,updated)
        description=api.data[first]['description']
        self.assertIn('Оновлення джерела',description)
        self.assertIn('додати висновок',description)
        apply_item(api,store,update,updated)
        self.assertEqual(api.data[first]['description'],description)
        private=deepcopy(source);private.update(key='gmail:private',project_id=19)
        self.assertNotEqual(apply_item(api,store,item,private),first)


class CampusAuthorizationTests(unittest.TestCase):
    def test_service_entrypoint_uses_the_same_error_type(self):
        import runpy
        module=runpy.run_module('uni_tasks.enrollment',run_name='service_entrypoint')
        self.assertIs(module['Problem'],Problem)

    def setUp(self):
        tmp=TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.campus=Campus(Path(tmp.name)/'campus.db')
        self.task={'id':77,'project_id':4,'title':'Review','description':'evidence','due_date':'0001-01-01T00:00:00Z','done':False}
        self.calls=[]
        def handle(req):
            self.calls.append(req)
            if req.url.path.endswith('/projects/4'):return httpx.Response(200,json={'owner':{'id':2}})
            if req.url.path.endswith('/tasks/77'):return httpx.Response(200,json=self.task)
            if req.url.path.endswith('/tasks'):return httpx.Response(200,json={'items':[self.task],'total_pages':1})
            return httpx.Response(404)
        self.http=httpx.Client(base_url='https://example.test/api/v2',transport=httpx.MockTransport(handle));self.addCleanup(self.http.close)
        self.service=Enrollment({'owner_id':1,'students':[{'user_id':2,'username':'a','personal_project_id':4}],
            'courses':[{'id':'web','title':'Web','project_id':30,'view_id':117}]},'limited-token',http=self.http,campus=self.campus)
        self.user={'id':2,'username':'a'}

    def test_calendar_uses_caller_token_and_rejects_leaked_scope(self):
        result=self.service.calendar(self.user,'Bearer caller')
        self.assertEqual(len(result['tasks']),1)
        self.assertTrue(all(r.headers['authorization']=='Bearer caller' for r in self.calls))
        self.task['project_id']=6
        with self.assertRaises(Problem):self.service.calendar(self.user,'Bearer caller')

    def test_student_cannot_open_admin_or_mute_another_project(self):
        with self.assertRaises(Problem) as exc:self.service.campus_action(self.user,'Bearer caller','/admin/imports','GET',None)
        self.assertEqual(exc.exception.status,403)
        self.task['project_id']=6
        with self.assertRaises(Problem):self.service.campus_action(self.user,'Bearer caller','/tasks/77/notifications','PUT',{'muted':True})
        self.assertEqual(self.campus.muted(2),set())

    def test_upstream_forbidden_remains_permission_error(self):
        with httpx.Client(base_url='https://example.test/api/v2',transport=httpx.MockTransport(lambda _:httpx.Response(403))) as client:
            self.service.http=client
            with self.assertRaises(Problem) as exc:self.service.personal(self.user,'Bearer caller')
            self.assertEqual(exc.exception.status,403)

    def test_private_review_cannot_be_published_and_stale_write_rejected(self):
        task=self.task;version=review_version(task)
        self.campus.review(77,{'shareable':False},version)
        body={'action':'approve','version':version,'title':'Checked','course_id':'web','due_date':''}
        self.service.campus_action({'id':1},'Bearer owner','/admin/reviews/77','POST',body)
        import json
        ops=json.loads(self.calls[-1].content)
        self.assertFalse(any(o['op']=='replace' and o['path']=='/project_id' for o in ops))
        with self.assertRaises(Problem):self.service.campus_action({'id':1},'Bearer owner','/admin/reviews/77','POST',body)
