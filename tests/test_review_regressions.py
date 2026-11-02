from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from pypdf import PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject

from uni_tasks.core import Store
from uni_tasks.documents import extract, MAX_BYTES
from uni_tasks.telegram_source import source_for, enrich_reply, collect
from uni_tasks.vikunja import Vikunja, apply_item
from uni_tasks.worker import process
from test_pipeline import API, fixture
import test_replication as replication_fixtures


def document(text='Submit laboratory 5 by 10 November.'):
    data = BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>'+text+'</w:t></w:r></w:p></w:body></w:document>')
    return data.getvalue()


class ImportWriteTests(unittest.TestCase):
    def client(self, transport):
        api = Vikunja.__new__(Vikunja)
        api.http = httpx.Client(base_url='https://test/api/v1', transport=httpx.MockTransport(transport))
        self.addCleanup(api.http.close)
        return api

    def test_concurrent_completion_is_not_included_in_import_patch(self):
        task = {'id': 1, 'project_id': 2, 'title': 'Lab', 'description': '', 'done': False}
        def transport(request):
            if request.method == 'GET':
                snapshot = deepcopy(task)
                task['done'] = True
                return httpx.Response(200, json=snapshot)
            self.assertEqual(request.method, 'PATCH')
            self.assertEqual(request.url.path, '/api/v2/tasks/1')
            self.assertEqual(request.headers['content-type'], 'application/json-patch+json')
            for operation in json.loads(request.content):
                key = operation['path'][1:]
                self.assertNotEqual(key, 'done')
                if operation['op'] == 'test': self.assertEqual(task[key], operation['value'])
                else: task[key] = operation['value']
            return httpx.Response(200, json=task)
        result = self.client(transport).update(1, {'description': 'New instructions'})
        self.assertTrue(result['done'])
        self.assertEqual(result['description'], 'New instructions')

    def test_conflicting_edit_uses_original_merge_snapshot_and_fails_closed(self):
        current = {'id': 1, 'project_id': 2, 'title': 'Old title', 'done': False}
        def transport(request):
            self.assertEqual(request.method, 'PATCH', 'Must not bless a newer GET as the baseline')
            ops = json.loads(request.content)
            self.assertIn({'op': 'test', 'path': '/title', 'value': 'Old title'}, ops)
            self.assertIn({'op': 'test', 'path': '/project_id', 'value': 2}, ops)
            return httpx.Response(409, json={'detail': 'Concurrent edit'})
        with self.assertRaises(httpx.HTTPStatusError):
            self.client(transport).update(1, {'title': 'Imported title'}, current=current)
        self.assertEqual(current['title'], 'Old title')

    def test_apply_passes_the_snapshot_used_for_manual_edit_detection(self):
        item, source = fixture()
        store, api = Store(':memory:'), API()
        self.addCleanup(store.db.close)
        tid = apply_item(api, store, item, source)
        item.title = 'Updated title'
        original = deepcopy(api.data[tid])
        with patch.object(api, 'update', wraps=api.update) as update:
            apply_item(api, store, item, source)
            self.assertEqual(update.call_args.kwargs['current'], original)


class ReplicationRaceTests(unittest.TestCase):
    setUp = replication_fixtures.ReplicationTests.setUp
    def test_edit_after_patch_is_not_saved_as_replication_baseline(self):
        self.worker.cycle()
        tid = self.api.tasks(4)[0]['id']
        original_patch = self.api.patch
        def patch_then_edit(task, current, changes):
            saved = original_patch(task, current, changes)
            self.api.data[task]['title'] = 'My manual title'
            return saved
        self.api.patch = patch_then_edit
        self.api.data[1]['title'] = 'Teacher revision 2'
        self.assertEqual(self.worker.cycle(), 0)
        self.api.patch = original_patch
        self.assertEqual(self.worker.cycle(), 0)
        self.assertEqual(self.api.data[tid]['title'], 'My manual title')


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.db.close)
        self.chat = {'id': -1001234567890, 'title': 'Measurement'}
        self.cfg = {'project_id': 2, 'view_id': 5}
        self.data = document()
        self.message = SimpleNamespace(id=123, raw_text='', date=datetime(2026, 10, 27, tzinfo=timezone.utc),
            document=SimpleNamespace(id=12345, mime_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document', size=len(self.data)),
            file=SimpleNamespace(name='practice-5.docx'), reply_to=None, edit_date=None)

    def test_captionless_document_is_a_durable_reviewable_source(self):
        source = source_for(self.message, self.chat, self.cfg)
        self.assertEqual(source['url'], 'https://t.me/c/1234567890/123')
        self.assertIn('practice-5.docx', source['messages'][0]['text'])
        self.assertTrue(source['attachment_review'])

    def test_docx_text_is_extracted_and_unchanged_file_not_downloaded_again(self):
        reads = []
        async def download(*args, **kwargs):
            reads.append(1)
            return self.data
        reader = SimpleNamespace(download_media=download)
        source = asyncio.run(enrich_reply(reader, None, self.message, self.chat, self.cfg, self.store))
        self.assertNotIn('attachment_review', source)
        self.assertIn('laboratory 5', source['messages'][0]['text'])
        self.store.ingest(source)
        self.assertEqual(asyncio.run(enrich_reply(reader, None, self.message, self.chat, self.cfg, self.store)), source)
        self.assertEqual(len(reads), 1)
        self.message.document.id += 1
        asyncio.run(enrich_reply(reader, None, self.message, self.chat, self.cfg, self.store))
        self.assertEqual(len(reads), 2)

    def test_pdf_text_extraction(self):
        pdf = PdfWriter()
        page = pdf.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject('/Type'):NameObject('/Font'), NameObject('/Subtype'):NameObject('/Type1'), NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'):pdf._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b'BT /F1 12 Tf 20 200 Td (Submit laboratory 5.) Tj ET')
        page[NameObject('/Contents')] = pdf._add_object(content)
        data = BytesIO(); pdf.write(data)
        self.assertIn('Submit laboratory 5.', extract(data.getvalue(), 'pdf')['text'])

    def test_unreadable_or_oversized_document_becomes_one_private_review_task(self):
        self.message.document.size = MAX_BYTES + 1
        source = asyncio.run(enrich_reply(SimpleNamespace(), None, self.message, self.chat, self.cfg, self.store))
        self.store.ingest(source)
        api = API()
        process(api, self.store)
        process(api, self.store)
        self.assertEqual(api.created, 1)
        self.assertEqual(api.data[1]['project_id'], 2)
        self.assertIn('practice-5.docx', api.data[1]['description'])
        self.assertEqual(len(self.store.pending()), 0)

    def test_download_failure_does_not_advance_chat_cursor(self):
        message = self.message
        class Reader:
            async def connect(self): pass
            async def disconnect(self): pass
            async def is_user_authorized(self): return True
            async def get_me(self): return SimpleNamespace(id=1)
            async def get_input_entity(self, _): return object()
            async def iter_messages(self, *args, **kwargs): yield message
            async def download_media(self, *args, **kwargs): raise OSError('network interrupted')
        cfg = self.cfg | {'reader_telegram_id':1, 'telegram_chats':[self.chat]}
        self.store.set('telegram_cursor:'+str(self.chat['id']), 100)
        with patch('uni_tasks.telegram_source.config', return_value=cfg), \
             patch('uni_tasks.telegram_source.secrets', return_value={'telegram_api_id':1, 'telegram_api_hash':'test'}), \
             patch('uni_tasks.telegram_source.TelegramClient', return_value=Reader()), \
             patch('uni_tasks.telegram_source.utils.get_peer_id', return_value=self.chat['id']):
            with self.assertRaises(OSError): asyncio.run(collect(self.store))
        self.assertEqual(self.store.get('telegram_cursor:'+str(self.chat['id'])), 100)

    def test_corrupt_document_is_queued_for_review(self):
        async def download(*args, **kwargs): return b'corrupt'
        source = asyncio.run(enrich_reply(SimpleNamespace(download_media=download), None, self.message, self.chat, self.cfg, self.store))
        self.assertIn('attachment_review', source)
