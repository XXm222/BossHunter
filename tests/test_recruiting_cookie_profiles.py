"""Profile-specific Windows keys; synthetic cookies and no browser/network use."""
import base64
from http.cookiejar import Cookie, CookieJar
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import browser_cookie3
from Cryptodome.Cipher import AES

from bosshunter.recruiting.browser import AccountPauseError
from bosshunter.recruiting.local_session import LocalBossSession


def cookie(value='synthetic-session', domain='.zhipin.com', name='wt2'):
    return Cookie(0, name, value, None, False, domain, True, domain.startswith('.'),
                  '/', True, False, None, True, None, None, {}, False)


class CookieProfileTests(TestCase):
    def profile(self, root, key, network=True):
        root.mkdir(parents=True, exist_ok=True)
        (root / 'Local State').write_text(json.dumps({'os_crypt': {
            'encrypted_key': base64.b64encode(b'DPAPI' + key).decode('ascii')}}))
        database = root / 'Default' / ('Network/Cookies' if network else 'Cookies')
        database.parent.mkdir(parents=True, exist_ok=True)
        cipher = AES.new(key, AES.MODE_GCM, nonce=b'synthetic-iv')
        encrypted, tag = cipher.encrypt_and_digest(b'synthetic-session')
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE cookies (host_key, path, is_secure, expires_utc, name, value, encrypted_value, is_httponly)')
            db.execute('INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?)',
                       ('.zhipin.com', '/', 1, 0, 'wt2', '', b'v10' + cipher.nonce + encrypted + tag, 1))
        return database

    @staticmethod
    def fake_dpapi(cipher_text, is_key=False):
        if is_key:
            return None, cipher_text
        raise RuntimeError('AES cookies require the profile-specific key')

    def test_independent_windows_profile_decrypts_with_its_own_key_not_default_chrome_key(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            independent = base / 'BossHunter/chrome-profile'
            ordinary = base / 'Local/Google/Chrome/User Data'
            self.profile(independent, b'I' * 32)
            self.profile(ordinary, b'O' * 32)
            session = LocalBossSession(user_data_dir=str(independent))
            # Resolve Windows-style backslash paths on this non-Windows test host.
            with patch('bosshunter.recruiting.local_session.sys.platform', 'win32'), patch.object(browser_cookie3, '_expand_paths', return_value=str(ordinary / 'Local State')), patch.object(browser_cookie3, '_crypt_unprotect_data', side_effect=self.fake_dpapi):
                # Reproduce the old call: cookie_file alone picks the wrong key.
                with self.assertRaises(browser_cookie3.BrowserCookieError):
                    browser_cookie3.chrome(cookie_file=str(independent / 'Default/Network/Cookies'), domain_name='.zhipin.com')
                actual = session.load_cookies()
            self.assertEqual([(item.name, item.value) for item in actual], [('wt2', 'synthetic-session')])

    def test_default_windows_profile_and_legacy_cookie_path_use_matching_local_state(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self.profile(root, b'S' * 32, network=False)
            with patch('bosshunter.recruiting.local_session.sys.platform', 'win32'), patch('bosshunter.recruiting.local_session._default_chrome_user_data_dir', return_value=root), patch.object(browser_cookie3, '_crypt_unprotect_data', side_effect=self.fake_dpapi):
                actual = LocalBossSession().load_cookies()
            self.assertEqual([item.value for item in actual], ['synthetic-session'])

    def test_missing_independent_local_state_never_falls_back_to_another_profile(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self.profile(root, b'S' * 32)
            (root / 'Local State').unlink()
            with patch('bosshunter.recruiting.local_session.sys.platform', 'win32'), patch.object(browser_cookie3, 'chrome') as load:
                with self.assertRaisesRegex(AccountPauseError, 'Local State'):
                    LocalBossSession(user_data_dir=str(root)).load_cookies()
            load.assert_not_called()

    def test_macos_keeps_keychain_loading_and_returns_only_boss_scoped_cookies(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            database = self.profile(root, b'S' * 32)
            jar = CookieJar()
            jar.set_cookie(cookie())
            jar.set_cookie(cookie('unrelated', '.example.org'))
            jar.set_cookie(cookie('misleading-host', '.zhipin.com.example.org'))
            with patch('bosshunter.recruiting.local_session.sys.platform', 'darwin'), patch.object(browser_cookie3, 'chrome', return_value=jar) as load:
                actual = LocalBossSession(user_data_dir=str(root)).load_cookies()
            load.assert_called_once_with(cookie_file=str(database), domain_name='.zhipin.com')
            self.assertEqual([item.value for item in actual], ['synthetic-session'])

    def test_failed_decryption_reports_safe_error_type_without_secret_exception_text(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self.profile(root, b'S' * 32)
            with patch('bosshunter.recruiting.local_session.sys.platform', 'win32'), patch.object(browser_cookie3, 'chrome', side_effect=ValueError('synthetic-secret-cookie-value')):
                with self.assertRaises(AccountPauseError) as caught:
                    LocalBossSession(user_data_dir=str(root)).load_cookies()
            self.assertIn('ValueError', str(caught.exception))
            self.assertNotIn('synthetic-secret-cookie-value', str(caught.exception))
            self.assertNotIn('关闭 Chrome', str(caught.exception))
