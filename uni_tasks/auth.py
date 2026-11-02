from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import secrets as random_secrets
import sys
from datetime import datetime

import httpx
from telethon import TelegramClient, utils
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError, FloodWaitError

from .core import CONFIG, STATE, config, secrets, normalize, save_json, read_json, now


def update_secrets(values):
    save_json(CONFIG / 'secrets.json', {**secrets(), **values})


async def telegram():
    """Interactive login only; the background process never prompts for codes."""
    print('Входь у НОВИЙ акаунт для читання університетських груп.')
    cached = secrets()
    api_id_text = input('API ID з my.telegram.org/apps' + (' (Enter = збережений)' if cached.get('telegram_api_id') else '') + ': ').strip()
    api_id = int(api_id_text or cached.get('telegram_api_id', ''))
    api_hash = getpass.getpass('API hash (приховано)' + (' (Enter = збережений)' if cached.get('telegram_api_hash') else '') + ': ').strip() or cached.get('telegram_api_hash', '')
    if len(api_hash) != 32 or not all(c in '0123456789abcdef' for c in api_hash.lower()):
        raise ValueError('API hash має містити 32 шістнадцяткові символи')
    update_secrets({'telegram_api_id': api_id, 'telegram_api_hash': api_hash})
    STATE.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(STATE / 'telegram'), api_id, api_hash,
        device_model='University task importer', system_version='Linux', app_version='1.0')
    try:
        await client.connect()
        if not await client.is_user_authorized():
            mode = input('Спосіб входу: 1 = QR-код (рекомендовано), 2 = код повідомлення [1]: ').strip()
            if mode == '2':
                await login_with_code(client)
            else:
                await login_with_qr(client)
        me = await client.get_me()
        if me.bot:
            raise ValueError('Потрібен акаунт користувача, а не бот')
        cfg = config()
        cfg['reader_telegram_id'] = me.id
        save_json(CONFIG / 'config.json', cfg)
        print('Вхід успішний. Ця сесія не надсилає повідомлень і не позначає їх прочитаними.')
        await choose_groups(client)
    finally:
        await client.disconnect()
        for p in STATE.glob('telegram.session*'):
            p.chmod(0o600)


def qr_terminal(url):
    import qrcode
    qr = qrcode.QRCode(border=3, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))
    print('\n', end='')
    for y in range(0, len(matrix), 2):
        line = ''.join((' ', '▄', '▀', '█')[2 * int(a) + int(b)] for a, b in zip(matrix[y], matrix[y + 1]))
        print('\x1b[30;47m' + line + '\x1b[0m')
    print('')


async def login_with_qr(client):
    print('На телефоні перемкнися на НОВИЙ акаунт: Налаштування → Пристрої → Підключити пристрій.')
    for attempt in range(5):
        qr = await client.qr_login()
        qr_terminal(qr.url)
        print('Відскануй QR на телефоні. Прострочений QR оновиться автоматично.')
        try:
            await qr.wait(timeout=max(1, (qr.expires - datetime.now(qr.expires.tzinfo)).total_seconds()))
            return
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass('Пароль двоетапної перевірки (приховано): '))
            return
        except asyncio.TimeoutError:
            print('QR оновлюється…')
    raise ValueError('Час очікування QR минув. Запусти підключення знову.')


async def login_with_code(client):
    import time
    phone = input('Номер нового акаунта, з +кодом країни: ').strip()
    sent = await client.send_code_request(phone)
    sent_at = time.monotonic()
    resent = False
    attempts = 0
    while attempts < 3:
        kind = type(getattr(sent, 'type', None)).__name__
        hints = {
            'SentCodeTypeApp': 'Код надіслано у службовий чат Telegram в уже авторизованому акаунті.',
            'SentCodeTypeSms': 'Telegram обрав доставку через SMS.',
            'SentCodeTypeCall': 'Telegram обрав доставку телефонним дзвінком.',
            'SentCodeTypeEmailCode': 'Telegram обрав доставку на пошту, прив’язану до входу.',
        }
        print(hints.get(kind, 'Тип доставки, повернутий Telegram: ' + kind))
        code = getpass.getpass('Код входу (приховано); r = повторний запит, q = вихід: ').strip()
        if code.lower() == 'q':
            raise ValueError('Вхід скасовано. Можна повторити через QR.')
        if code.lower() == 'r':
            wait = max(0, int(getattr(sent, 'timeout', 0) or 0) - int(time.monotonic() - sent_at))
            if wait:
                print(f'Telegram дозволяє повторний запит через {wait} с.')
            elif resent or not getattr(sent, 'next_type', None):
                print('Повторний запит зараз недоступний. Спробуй вхід через QR.')
            else:
                sent = await client.send_code_request(phone)
                resent = True
                sent_at = time.monotonic()
            continue
        if not code:
            continue
        try:
            await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            return
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass('Пароль двоетапної перевірки (приховано): '))
            return
        except PhoneCodeInvalidError:
            attempts += 1
            print('Код не підходить. Потрібен код входу для цього номера, а не API ID чи API hash.')
        except PhoneCodeExpiredError:
            raise ValueError('Код прострочено. Повтори підключення через QR.')
    raise ValueError('Три неправильні спроби. Повтори підключення через QR.')


async def choose_groups(client=None):
    own_client = client is None
    if own_client:
        sec = secrets()
        client = TelegramClient(str(STATE / 'telegram'), sec['telegram_api_id'], sec['telegram_api_hash'])
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise ValueError('Спершу виконай вхід у Telegram')
    try:
        dialogs = [d async for d in client.iter_dialogs() if d.is_group or d.is_channel]
        cfg = config()
        selected = []
        print('\nДоступні групи (нічого не приєднується автоматично):')
        for i, d in enumerate(dialogs, 1):
            print(f'{i}: {d.name}')
        requested = cfg.get('telegram_titles', [])
        suggested = [i for i, d in enumerate(dialogs, 1) if normalize(d.name) in {normalize(t) for t in requested}]
        print('\nПотрібні назви: ' + '; '.join(requested))
        print('Знайдені точні збіги: ' + (','.join(map(str, suggested)) or 'немає'))
        answer = input('Номери груп через кому; Enter = точні збіги: ').strip()
        indices = [int(x.strip()) for x in answer.split(',')] if answer else suggested
        if not indices:
            print('Групи ще не вибрані. Додай акаунт до груп і запусти налаштування груп повторно.')
            return
        if any(i < 1 or i > len(dialogs) for i in indices):
            raise ValueError('Неправильний номер групи')
        for i in sorted(set(indices)):
            d = dialogs[i - 1]
            # Dialog enumeration stores entity access hashes in Telethon's session.
            selected.append({'id': utils.get_peer_id(d.entity), 'title': d.name})
        cfg['telegram_chats'] = selected
        save_json(CONFIG / 'config.json', cfg)
        print(f'Збережено {len(selected)} груп для читання.')
    finally:
        if own_client:
            await client.disconnect()


def clean_bot_token(raw):
    token = raw.strip().strip('`\'"')
    if not re.fullmatch(r'\d{5,20}:[A-Za-z0-9_-]{30,120}', token):
        raise ValueError('Неповний або неправильний формат токена. Натисни Copy у BotFather. Потрібні цифри, двокрапка і довгий ключ; без трьох крапок. Встав через Shift+Insert.')
    return token


def bot():
    expected = config().get('bot_username', '').lstrip('@').lower()
    if not expected:
        raise ValueError('Спочатку задай bot_username у приватному config.json.')
    token = clean_bot_token(getpass.getpass('Токен налаштованого бота від BotFather (приховано): '))
    with httpx.Client(timeout=30) as h:
        r = h.post(f'https://api.telegram.org/bot{token}/getMe')
        if not r.is_success or not r.json().get('ok'):
            if r.status_code == 429:
                raise ValueError('Telegram обмежив кількість запитів. Зачекай перед повторною спробою.')
            if r.status_code >= 500:
                raise ValueError(f'Telegram API тимчасово недоступний (HTTP {r.status_code}). Спробуй пізніше.')
            raise ValueError(f'Telegram відхилив токен (HTTP {r.status_code}). Скопіюй актуальний токен заново кнопкою Copy у BotFather; старий або відкликаний токен не працюватиме.')
        identity = r.json()['result']
        if identity.get('username', '').lower() != expected:
            raise ValueError('Токен належить іншому боту; перевір bot_username у config.json')
        print('Telegram підтвердив бота @' + identity['username'])
        webhook = h.post(f'https://api.telegram.org/bot{token}/getWebhookInfo').json()
        if webhook.get('result', {}).get('url'):
            raise ValueError('Бот уже має webhook. Його потрібно відключити перед використанням тут.')
    pairing = random_secrets.token_urlsafe(24)
    update_secrets({'bot_token': token, 'pairing_code': pairing})
    print('Токен збережено. Відкрий це посилання зі свого ОСОБИСТОГО Telegram і натисни Start:')
    print(f'https://t.me/{identity["username"]}?start={pairing}')
    print('Лише цей одноразовий код дозволить прив’язати одержувача нагадувань.')


def google_credentials_after_scope_change(flow, warning, requested):
    """Accept only Google's known Classroom scope alias, never missing/extra grants."""
    if not all(hasattr(warning, name) for name in ('token', 'new_scope')):
        raise warning
    old = 'https://www.googleapis.com/auth/classroom.coursework.me.readonly'
    canonical = 'https://www.googleapis.com/auth/classroom.student-submissions.me.readonly'
    def normalized(values):
        return {canonical if scope == old else scope for scope in values}
    granted = list(warning.new_scope)
    expected, actual = normalized(requested), normalized(granted)
    if expected - actual:
        raise ValueError('Google не надав усі дозволи. Повтори підключення й вибери читання Gmail, курсів та власних робіт Classroom.') from None
    if actual - expected:
        raise ValueError('Google повернув неочікувані додаткові дозволи; токен не збережено.') from None
    # OAuthlib already validated the token before raising its scope-change Warning.
    # Preserve Google's actual granted scope names for future refreshes.
    flow.oauth2session.scope = granted
    flow.oauth2session.token = dict(warning.token)
    return flow.credentials


def google(account):
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import AuthorizedSession
    cfg = config()
    if account not in cfg.get('google_accounts', []):
        raise ValueError('Ця пошта відсутня у списку дозволених акаунтів')
    client_file = CONFIG / 'google-client.json'
    if not client_file.exists():
        raise ValueError('Спершу завантаж OAuth client JSON типу Desktop app у /config/google-client.json')
    scopes = [
        'openid',
        'https://www.googleapis.com/auth/userinfo.email',
        'https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/classroom.courses.readonly',
        'https://www.googleapis.com/auth/classroom.coursework.me.readonly',
    ]
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes)
    port = int(os.environ.get('GOOGLE_AUTH_PORT', '8765'))
    if not 1024 <= port <= 65535:
        raise ValueError('Порт авторизації має бути від 1024 до 65535')
    print(f'У браузері обери саме {account}. SSH-тунель має переадресовувати порт {port}.')
    try:
        creds = flow.run_local_server(host='127.0.0.1', bind_addr='0.0.0.0', port=port,
            open_browser=False, timeout_seconds=600, login_hint=account,
            success_message='Google response received. Check the terminal for the connection result. You may close this tab.',
            prompt='consent select_account', access_type='offline')
    except Warning as warning:
        creds = google_credentials_after_scope_change(flow, warning, scopes)
    session = AuthorizedSession(creds)
    profile = session.get('https://openidconnect.googleapis.com/v1/userinfo', timeout=30)
    profile.raise_for_status()
    actual = profile.json()['email'].casefold()
    if actual != account.casefold():
        raise ValueError(f'Обраний інший акаунт ({actual}); токен не збережено')
    if not creds.refresh_token:
        raise ValueError('Google не повернув refresh token; повтори вхід із дозволом offline access')
    save_json(CONFIG / ('google-' + account + '.json'), json.loads(creds.to_json()))
    lifetime = flow.oauth2session.token.get('refresh_token_expires_in')
    save_json(STATE / ('google-authorization-' + account + '.json'), {
        'account': account, 'authorized_at': now().isoformat(),
        'refresh_token_expires_in': lifetime,
        'scopes': list(creds.scopes or []),
    })
    print(f'{account}: дозвіл на читання Gmail і Classroom збережено.')
    print('Google refresh token lifetime: ' + (str(lifetime) + ' seconds' if lifetime is not None else 'no fixed lifetime reported'))


def status():
    cfg, sec = config(), secrets()
    saved = (STATE / 'telegram.session').exists()
    print('Telegram-сесія: ' + ('вхід раніше завершено' if saved and cfg.get('reader_telegram_id') else 'потрібен вхід'))
    print('Груп для читання: ' + str(len(cfg.get('telegram_chats', []))))
    print('Telegram-бот: ' + ('токен збережено' if sec.get('bot_token') else 'потрібен токен'))
    print('Одержувач нагадувань: ' + ('прив’язаний' if cfg.get('owner_telegram_id') else 'потрібен /start за особистим посиланням'))
    for account in cfg.get('google_accounts', []):
        print(account + ': ' + ('токен збережено' if (CONFIG / ('google-' + account + '.json')).exists() else 'потрібен дозвіл Google'))
    print('Наявність токена ще не підтверджує успішну синхронізацію; дивись стан сервісу.')
    if (STATE / 'status.json').exists():
        print((STATE / 'status.json').read_text('utf-8'))


def main():
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    try:
        if action == 'telegram':
            asyncio.run(telegram())
        elif action == 'groups':
            asyncio.run(choose_groups())
        elif action == 'bot':
            bot()
        elif action == 'google':
            google(sys.argv[2])
        elif action == 'status':
            status()
        else:
            raise ValueError('Доступно: telegram, groups, bot, google EMAIL, status')
    except (KeyboardInterrupt, EOFError):
        print('\nНалаштування перервано.')
        raise SystemExit(1)
    except FloodWaitError as exc:
        print(f'Telegram обмежив спроби входу. Зачекай {exc.seconds} с. Не надсилай запити повторно до цього часу.')
        raise SystemExit(1)
    except Exception as exc:
        # Never include HTTP URLs containing the bot token in an exception.
        print('Налаштування не завершено: ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
