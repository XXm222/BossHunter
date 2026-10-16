"""Profile-specific Windows keys; synthetic cookies and no browser/network use."""
import base64
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import Cookie, CookieJar
import json
from pathlib import Path
import sqlite3
import sys
from threading import Barrier, get_ident, local
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


class ComError(Exception):
    def __init__(self, hresult):
        self.hresult = hresult


class ThreadCom:
    """Model COM apartment ownership separately for each calling thread."""
    COINIT_APARTMENTTHREADED = 2
    error = ComError

    def __init__(self):
        self.state = local()
        self.calls = []
        self.failure = None

    def CoInitializeEx(self, model):
        self.calls.append(('init', get_ident()))
        if self.failure is not None:
            raise ComError(self.failure)
        if getattr(self.state, 'depth', 0) and self.state.model != model:
            raise ComError(-2147417850)
        self.state.model = model
        self.state.depth = getattr(self.state, 'depth', 0) + 1

    def CoUninitialize(self):
        self.calls.append(('uninit', get_ident()))
        self.state.depth -= 1


class CookieProfileTests(TestCase):
    def setUp(self):
        self.com = ThreadCom()
        self.com_modules = patch.dict(sys.modules, {'pythoncom': self.com})
        self.com_modules.start()
        self.addCleanup(self.com_modules.stop)

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
            self.assertEqual(self.com.calls, [])

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
            self.assertEqual(self.com.state.depth, 0)
            self.assertEqual([call[0] for call in self.com.calls], ['init', 'uninit'])

    def test_each_concurrent_cookie_reader_initialises_and_releases_its_own_thread(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self.profile(root, b'S' * 32)
            barrier = Barrier(2)

            def load(**options):
                self.assertEqual(self.com.state.depth, 1)
                barrier.wait(timeout=5)
                jar = CookieJar()
                jar.set_cookie(cookie())
                return jar

            def read():
                result = LocalBossSession(user_data_dir=str(root)).load_cookies()
                self.assertEqual(self.com.state.depth, 0)
                return [item.value for item in result]

            with patch('bosshunter.recruiting.local_session.sys.platform', 'win32'), patch.object(browser_cookie3, 'chrome', side_effect=load):
                with ThreadPoolExecutor(max_workers=2) as workers:
                    results = [workers.submit(read) for _ in range(2)]
                    self.assertEqual([result.result(timeout=10) for result in results],
                                     [['synthetic-session'], ['synthetic-session']])
            initialised = [thread for action, thread in self.com.calls if action == 'init']
            released = [thread for action, thread in self.com.calls if action == 'uninit']
            self.assertEqual(len(set(initialised)), 2)
            self.assertCountEqual(initialised, released)
            self.assertNotIn(get_ident(), initialised)

    def test_existing_apartment_keeps_its_original_com_reference(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self.profile(root, b'S' * 32)
            jar = CookieJar()
            jar.set_cookie(cookie())
            for model in (0, self.com.COINIT_APARTMENTTHREADED):
                with self.subTest(model=model):
                    self.com.calls.clear()
                    self.com.state.model = model
                    self.com.state.depth = 1
                    with patch('bosshunter.recruiting.local_session.sys.platform', 'win32'), patch.object(browser_cookie3, 'chrome', return_value=jar):
                        LocalBossSession(user_data_dir=str(root)).load_cookies()
                    self.assertEqual(self.com.state.depth, 1)
                    self.assertEqual(self.com.state.model, model)
                    expected = ['init'] if model == 0 else ['init', 'uninit']
                    self.assertEqual([call[0] for call in self.com.calls], expected)

    def test_com_initialisation_failure_stops_before_loading_and_does_not_release(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self.profile(root, b'S' * 32)
            self.com.failure = -2147024882  # E_OUTOFMEMORY, not an existing apartment.
            with patch('bosshunter.recruiting.local_session.sys.platform', 'win32'), patch.object(browser_cookie3, 'chrome') as load:
                with self.assertRaisesRegex(AccountPauseError, 'ComError'):
                    LocalBossSession(user_data_dir=str(root)).load_cookies()
            load.assert_not_called()
            self.assertEqual([call[0] for call in self.com.calls], ['init'])

    def test_com_is_ready_before_browser_cookie_dependency_import(self):
        import builtins
        original_import = builtins.__import__
        imported = []

        def import_checked(name, *args, **kwargs):
            if name == 'browser_cookie3':
                self.assertEqual(self.com.state.depth, 1)
                imported.append(name)
            return original_import(name, *args, **kwargs)

        with TemporaryDirectory() as folder:
            root = Path(folder)
            self.profile(root, b'S' * 32)
            jar = CookieJar()
            jar.set_cookie(cookie())
            with patch('bosshunter.recruiting.local_session.sys.platform', 'win32'), patch.object(browser_cookie3, 'chrome', return_value=jar), patch('builtins.__import__', side_effect=import_checked):
                LocalBossSession(user_data_dir=str(root)).load_cookies()
            self.assertEqual(imported, ['browser_cookie3'])
