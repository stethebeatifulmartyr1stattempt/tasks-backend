"""Telegram account access: history reads only, restricted to configured peers."""
from datetime import timedelta
import asyncio
import json
import sys

from telethon import TelegramClient, utils

from .core import STATE, config, secrets, now
from .documents import MAX_BYTES


def attachment_for(message):
    document = getattr(message, 'document', None)
    if not document or getattr(message, 'sticker', None):
        return None
    mime = getattr(document, 'mime_type', '') or ''
    if mime.startswith(('audio/', 'video/')):
        return None
    file = getattr(message, 'file', None)
    name = getattr(file, 'name', None) or 'Документ'
    return {'id': str(getattr(document, 'id', '')), 'name': name[:250], 'mime': mime,
            'size': getattr(document, 'size', 0) or 0}


def source_for(message, chat, cfg):
    text = message.raw_text or ''
    attachment = attachment_for(message)
    if not text.strip() and not attachment:
        return None
    if not text.strip():
        text = 'Вкладення без підпису: ' + attachment['name']
    peer_id = chat['id']
    reply = getattr(message, 'reply_to', None)
    topic_id = (getattr(reply, 'reply_to_top_id', None) or getattr(reply, 'reply_to_msg_id', None)) if getattr(reply, 'forum_topic', False) else None
    topic_courses = [c for c in cfg.get('discipline_routing', {}).get('courses', []) if any(
        t['chat_id'] == peer_id and t['topic_id'] == topic_id for t in c.get('telegram_topics', []))]
    title = chat['title'] + (' · ' + topic_courses[0]['title'] if len(topic_courses) == 1 else '')
    if peer_id < -1000000000000:
        url = f'https://t.me/c/{str(peer_id)[4:]}/{message.id}'
    else:
        # Legacy basic groups have no universally usable HTTPS message link.
        url = ''
    source = {
        'key': f'telegram:{peer_id}:{message.id}', 'title': title + f' · №{message.id}',
        'topic_id': topic_id,
        'url': url, 'project_id': cfg['project_id'], 'view_id': cfg['view_id'],
        'messages': [{'at': message.date.isoformat(), 'text': text[:24000], 'role': 'group'}],
    }
    if attachment:
        source['attachment'] = attachment
        source['attachment_review'] = 'Вміст вкладення ще не прочитано.'
    return source


async def extract_attachment(client, message, source):
    attachment = source['attachment']
    name = attachment['name'].casefold()
    kind = ('pdf' if name.endswith('.pdf') or attachment['mime'] == 'application/pdf' else
            'docx' if name.endswith('.docx') or attachment['mime'] ==
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document' else None)
    if not kind or attachment['size'] > MAX_BYTES:
        source['attachment_review'] = 'Формат або розмір вкладення потребує ручної перевірки.'
        return
    # Transient download failures propagate: the cursor must not pass a file
    # which was neither successfully read nor durably queued for review.
    data = await client.download_media(message, file=bytes)
    if not data or len(data) > MAX_BYTES:
        source['attachment_review'] = 'Не вдалося прочитати вкладення в межах допустимого розміру.'
        return
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'uni_tasks.documents', kind,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), timeout=20)
        if process.returncode:
            raise ValueError('Extraction failed')
        result = json.loads(output)
        source['messages'].insert(-1, {'at': message.date.isoformat(), 'role': 'attachment_text',
                                      'text': result['text']})
        if result['truncated']:
            source['attachment_review'] = 'Прочитано лише частину великого документа. Перевір решту вкладення.'
        else:
            source.pop('attachment_review', None)
    except (ValueError, KeyError, asyncio.TimeoutError):
        if process.returncode is None:
            process.kill()
            await process.wait()
        source['attachment_review'] = 'Документ захищений, пошкоджений, містить скан або не вдалося витягти текст.'


async def enrich_reply(client, peer, message, chat, cfg, store):
    source = source_for(message, chat, cfg)
    if not source:
        return None
    reply = getattr(message, 'reply_to', None)
    reply_id = getattr(reply, 'reply_to_msg_id', None)
    previous = store.db.execute('SELECT payload FROM sources WHERE key=?', (source['key'],)).fetchone()
    if previous:
        cached = json.loads(previous['payload'])
        if (cached['messages'][-1] == source['messages'][-1] and cached.get('topic_id') == source.get('topic_id')
                and cached.get('attachment') == source.get('attachment')
                and cached['title'] == source['title'] and (not reply_id or cached.get('context_loaded'))):
            return cached
    if source.get('attachment'):
        await extract_attachment(client, message, source)
    if reply_id:
        other = getattr(reply, 'reply_to_peer_id', None)
        if other is None or utils.get_peer_id(other) == chat['id']:
            parent = await client.get_messages(peer, ids=reply_id)
            if parent and parent.raw_text:
                source['messages'].insert(0, {'at': parent.date.isoformat(), 'role': 'quoted_context', 'text': parent.raw_text[:18000]})
        source['context_loaded'] = True
    return source


async def collect(store):
    cfg, sec = config(), secrets()
    if not sec.get('telegram_api_id') or not cfg.get('telegram_chats'):
        return {'state': 'awaiting_login_or_groups', 'stored': 0}
    client = TelegramClient(str(STATE / 'telegram'), sec['telegram_api_id'], sec['telegram_api_hash'],
                            receive_updates=False, flood_sleep_threshold=0)
    count = 0
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {'state': 'authorization_required', 'stored': 0}
        me = await client.get_me()
        if me.id != cfg.get('reader_telegram_id'):
            raise ValueError('reader identity differs from authorized configuration')
        cutoff = now() - timedelta(days=cfg.get('initial_days', 30))
        edit_cutoff = now() - timedelta(days=cfg.get('edit_lookback_days', 30))
        for chat in cfg['telegram_chats']:
            peer = await client.get_input_entity(chat['id'])
            if utils.get_peer_id(peer) != chat['id']:
                raise ValueError('unexpected Telegram peer')
            cursor_key = 'telegram_cursor:' + str(chat['id'])
            cursor = store.get(cursor_key, 0)
            highest = cursor
            async for message in client.iter_messages(peer, min_id=cursor):
                if not cursor and message.date < cutoff:
                    break
                source = await enrich_reply(client, peer, message, chat, cfg, store)
                if source:
                    store.ingest(source)
                    count += 1
                highest = max(highest, message.id)
            # The cursor advances only after every retrieved message was persisted.
            store.set(cursor_key, highest)
            if cursor:
                async for message in client.iter_messages(peer):
                    if message.date < edit_cutoff:
                        break
                    if message.edit_date:
                        source = await enrich_reply(client, peer, message, chat, cfg, store)
                        if source:
                            store.ingest(source)
        return {'state': 'synced', 'stored': count, 'groups': len(cfg['telegram_chats']), 'at': now().isoformat()}
    finally:
        await client.disconnect()
