import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import unittest
from unittest.mock import patch

from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from uni_tasks import auth


class FakeQR:
    url = 'tg://login?token=nonexistent-test-token'

    def __init__(self, error=None):
        self.error = error
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)

    async def wait(self, timeout):
        assert 0 < timeout <= 30
        if self.error:
            raise self.error


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.password = None
        self.qr_calls = 0

    async def qr_login(self):
        self.qr_calls += 1
        return FakeQR(self.error)

    async def sign_in(self, **kwargs):
        self.password = kwargs.get('password')


class AuthTests(unittest.TestCase):
    def scope_change(self, granted):
        from google_auth_oauthlib.flow import InstalledAppFlow
        from oauthlib.oauth2.rfc6749.parameters import parse_token_response
        scopes = ['openid', 'https://www.googleapis.com/auth/userinfo.email',
            'https://www.googleapis.com/auth/gmail.readonly',
            'https://www.googleapis.com/auth/classroom.courses.readonly',
            'https://www.googleapis.com/auth/classroom.coursework.me.readonly']
        flow = InstalledAppFlow.from_client_config({'installed': {
            'client_id': 'test-client', 'client_secret': 'test-secret',
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token'}}, scopes)
        token = {'access_token': 'test-access', 'refresh_token': 'test-refresh',
            'expires_in': 3600, 'token_type': 'Bearer', 'scope': ' '.join(granted(scopes))}
        try:
            parse_token_response(json.dumps(token), scope=scopes)
        except Warning as warning:
            return flow, warning, scopes
        self.fail('Expected a real OAuthlib scope-change warning')

    def test_google_canonical_classroom_alias_preserves_credentials(self):
        canonical = 'https://www.googleapis.com/auth/classroom.student-submissions.me.readonly'
        flow, warning, scopes = self.scope_change(lambda items: items[:-1] + [canonical])
        creds = auth.google_credentials_after_scope_change(flow, warning, scopes)
        self.assertEqual(creds.refresh_token, 'test-refresh')
        self.assertEqual(creds.token, 'test-access')
        self.assertIn(canonical, creds.scopes)
        self.assertNotIn(scopes[-1], creds.scopes)

    def test_google_partial_consent_is_rejected(self):
        flow, warning, scopes = self.scope_change(lambda items: items[:2])
        with self.assertRaisesRegex(ValueError, 'не надав усі дозволи'):
            auth.google_credentials_after_scope_change(flow, warning, scopes)
        self.assertFalse(flow.oauth2session.token)

    def test_google_unrequested_scope_is_rejected(self):
        flow, warning, scopes = self.scope_change(lambda items: items + ['https://www.googleapis.com/auth/gmail.modify'])
        with self.assertRaisesRegex(ValueError, 'додаткові дозволи'):
            auth.google_credentials_after_scope_change(flow, warning, scopes)
        self.assertFalse(flow.oauth2session.token)

    def test_google_unrelated_warning_is_not_suppressed(self):
        with self.assertRaisesRegex(Warning, 'unrelated'):
            auth.google_credentials_after_scope_change(None, Warning('unrelated'), [])

    def test_bot_token_rejects_truncated_copy_and_api_hash(self):
        for value in ('', 'a' * 32, '123456789:ABC...', '123456789:ABC…'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                auth.clean_bot_token(value)

    def test_bot_token_accepts_full_value_and_surrounding_quotes(self):
        token = '123456789:' + 'A' * 35
        self.assertEqual(auth.clean_bot_token('  "' + token + '"  '), token)

    def test_qr_success(self):
        client = FakeClient()
        with patch.object(auth, 'qr_terminal') as display, contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(auth.login_with_qr(client))
        display.assert_called_once_with(FakeQR.url)
        self.assertEqual(client.qr_calls, 1)

    def test_qr_two_factor_password(self):
        client = FakeClient(SessionPasswordNeededError(request=None))
        with patch.object(auth, 'qr_terminal'), patch.object(auth.getpass, 'getpass', return_value='test-password'), contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(auth.login_with_qr(client))
        self.assertEqual(client.password, 'test-password')

    def test_expired_qr_has_bounded_retries(self):
        client = FakeClient(asyncio.TimeoutError())
        with patch.object(auth, 'qr_terminal'), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'очікування QR'):
            asyncio.run(auth.login_with_qr(client))
        self.assertEqual(client.qr_calls, 5)

    def test_terminal_qr_renders_without_printing_login_url(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            auth.qr_terminal(FakeQR.url)
        self.assertNotIn(FakeQR.url, out.getvalue())
        self.assertIn('\x1b[30;47m', out.getvalue())


if __name__ == '__main__':
    unittest.main()
