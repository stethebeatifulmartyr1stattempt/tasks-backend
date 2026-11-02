import asyncio
import json
import logging
import os
import time

from .core import Batch, CONFIG, STATE, SPOOL, Item, Store, config, secrets, now, fingerprint, read_json, save_json
from . import telegram_source, google_source
from .vikunja import Vikunja, apply_item
from .notifications import Bot, poll_updates, reminders
from .campus import Campus
from .campus_api import publish_reviews
from .dedup import seed


def connection_errors(state):
    errors=0
    def failed(value):return value.get('state','') in ('error','authorization_error','authorization_required')
    for value in (state.get('telegram',{}),state.get('parser',{}),state.get('importer',{})):
        errors+=int(failed(value))
    for account in state.get('google',{}).values():
        errors+=int(failed(account))
        errors+=sum(int(failed(value)) for value in account.values() if isinstance(value,dict))
    return errors


def finish_batch(api, store, active, result):
    rows = {r['key']: r for r in store.db.execute('SELECT * FROM sources WHERE key IN (' + ','.join('?' for _ in active['keys']) + ')', active['keys'])}
    if not result.get('ok'):
        for row in rows.values():
            store.failed(row, result.get('error', 'parser_error'))
    else:
        batch = Batch.model_validate(result['batch'])
        if set(r.source_key for r in batch.messages) != set(active['keys']):
            raise ValueError('Unexpected result source keys')
        for extracted in batch.messages:
            row = rows[extracted.source_key]
            if row['digest'] != active['digests'][row['key']]:
                continue  # A source edit arrived while extraction was running.
            source = json.loads(row['payload'])
            try:
                review_needed = False
                for item in extracted.tasks:
                    try:
                        apply_item(api, store, item, source)
                    except ValueError:
                        review_needed = True
                if review_needed:
                    review_source(api, store, source)
                store.succeeded(row)
            except Exception as exc:
                store.failed(row, type(exc).__name__)
    store.set('active_job', None)
    for suffix in ('.request.json', '.result.json'):
        (SPOOL / (active['id'] + suffix)).unlink(missing_ok=True)


def review_source(api, store, source):
    # Keep an original announcement available for human review without inventing
    # an assignment from unsupported model output or retrying it forever.
    review = Item(title='Перевірити оголошення: ' + source['title'][:150],
        course='Уточнення', assignment_key='review:' + source['key'],
        evidence=source['messages'][-1]['text'][:1200],
        source_timestamp=source['messages'][-1]['at'], due_text=None,
        due_evidence=None, has_time=False, state='open', confidence=.7,
        review_reason=source.get('attachment_review') or
            'Автоматичне виділення завдання потребує перевірки. Переглянь оригінальне оголошення.')
    apply_item(api, store, review, source)


def process(api, store):
    active = store.get('active_job')
    if active:
        result = read_json(SPOOL / (active['id'] + '.result.json'))
        if result:
            finish_batch(api, store, active, result)
        elif time.time() - active['created'] > 600:
            finish_batch(api, store, active, {'ok': False, 'error': 'parser_timeout'})
        return
    rows = store.pending(8)
    unstructured = []
    for row in rows:
        source = json.loads(row['payload'])
        decision = store.get('reviewed:' + source['key'])
        if decision and decision['messages_digest'] == fingerprint(source['messages']):
            # Human-reviewed extraction/disposition is authoritative for this exact
            # announcement. A subsequent text edit still re-enters the parser.
            for value in decision.get('items', []):
                apply_item(api, store, Item.model_validate(value), source)
            store.succeeded(row)
            continue
        if source.get('attachment_review'):
            try:
                review_source(api, store, source)
                store.succeeded(row)
            except Exception as exc:
                store.failed(row, type(exc).__name__)
            continue
        if source.get('structured'):
            data = source['structured']
            try:
                item = Item(title=data['title'], course=data['course'], assignment_key=data['key'],
                    evidence=source['messages'][0]['text'][:1500], source_timestamp=source['messages'][0]['at'],
                    due_text=None, due_evidence=None, has_time=True, state=data.get('state', 'open'), confidence=1, review_reason=None)
                apply_item(api, store, item, source)
                store.succeeded(row)
            except Exception as exc:
                store.failed(row, type(exc).__name__)
        else:
            unstructured.append(row)
    if not unstructured:
        return
    parser_state = read_json(SPOOL / 'parser-status.json', {}).get('state')
    if parser_state == 'authorization_required':
        return
    calls = store.get('parser_calls', [])
    calls = [at for at in calls if time.time() - at < 3600]
    if len(calls) >= config().get('max_parse_batches_per_hour', 60):
        return
    sources, selected = [], []
    for row in unstructured:
        source = json.loads(row['payload'])
        candidate = {k: source[k] for k in ('key', 'title', 'messages')}
        candidate['messages'] = [{**m, 'text': m['text'][:24000 if m.get('role') == 'attachment_text' else 6000]}
                                 for m in candidate['messages'][-8:]]
        if sources and len(json.dumps(sources + [candidate])) > 65000:
            break
        sources.append(candidate)
        selected.append(row)
    cfg = config()
    job = {'courses': cfg.get('course_names', []),
           'course_contexts': cfg.get('course_contexts', {}), 'sources': sources}
    job_id = fingerprint(job)
    active = {'id': job_id, 'created': time.time(), 'keys': [r['key'] for r in selected],
              'digests': {r['key']: r['digest'] for r in selected}}
    store.set('active_job', active)
    save_json(SPOOL / (job_id + '.request.json'), job)
    store.set('parser_calls', calls + [time.time()])


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('telethon').setLevel(logging.WARNING)
    store = Store()
    store.campus=Campus()
    api = Vikunja()
    SPOOL.mkdir(parents=True, exist_ok=True)
    state = {}
    next_sync = next_bot = next_review = 0
    while True:
        if time.monotonic() >= next_sync:
            try:
                state['telegram'] = asyncio.run(telegram_source.collect(store))
            except Exception as exc:
                state['telegram'] = {'state': 'error', 'error': type(exc).__name__}
            state['google'] = google_source.collect(store)
            next_sync = time.monotonic() + config().get('poll_seconds', 180)
        try:
            seed(api,store)
            process(api, store)
            state['importer'] = {'state': 'running'}
        except Exception as exc:
            state['importer'] = {'state': 'error', 'error': type(exc).__name__}
        if config().get('external_notifications'):
            state['bot'] = {'state': 'managed_by_notifications_service'}
        elif time.monotonic() >= next_bot:
            token = secrets().get('bot_token')
            if token:
                try:
                    bot = Bot(token)
                    poll_updates(api, store, bot)
                    reminders(api, store, bot)
                    bot.client.close()
                    state['bot'] = {'state': 'connected' if config().get('owner_telegram_id') else 'awaiting_start'}
                except Exception as exc:
                    state['bot'] = {'state': 'error', 'error': type(exc).__name__}
            else:
                state['bot'] = {'state': 'token_required'}
            next_bot = time.monotonic() + 30
        state['parser'] = read_json(SPOOL / 'parser-status.json', {'state': 'not_started'})
        state['pending'] = store.db.execute('SELECT COUNT(*) FROM sources WHERE processed IS NULL OR processed!=digest').fetchone()[0]
        state['stored_messages'] = store.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
        state['failed_sources'] = {r[0]: r[1] for r in store.db.execute('SELECT error,COUNT(*) FROM sources WHERE error IS NOT NULL GROUP BY error')}
        state['at'] = now().isoformat()
        save_json(STATE / 'status.json', state)
        store.campus.status('importer',{'checked_at':state['at'],'pending':state['pending'],
            'stored_messages':state['stored_messages'],'errors':sum(state['failed_sources'].values())+connection_errors(state),
            'telegram':state.get('telegram',{}).get('state','unknown'),
            'google':[{'state':value.get('state') or ', '.join(name+': '+result.get('state','unknown')
                for name,result in value.items() if isinstance(result,dict))} for value in state.get('google',{}).values() if isinstance(value,dict)],
            'parser':state['parser'].get('state','unknown')})
        if time.monotonic()>=next_review:
            try:publish_reviews(api,store)
            except Exception:logging.warning('Import review refresh failed; will retry.')
            next_review=time.monotonic()+180
        time.sleep(3)


if __name__ == '__main__':
    main()
