"""Pacing edits are local, confirmed and deferred until both processes restart."""
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

from bosshunter.config import load_config, save_config
from bosshunter.recruiting.pacing import pacing_values, validate_pacing
from bosshunter.recruiting.service import RecruitingService
from bosshunter.web import server
from test_recruiting import FakeBrowser


class PacingSettingsTests(TestCase):
    def test_validation_rejects_invalid_ranges_and_non_finite_or_non_numeric_values(self):
        original = pacing_values({})
        for key, value in [('read_delay_min', 181), ('read_delay_max', float('inf')),
                           ('greet_delay_min', float('nan')), ('read_page_delay', -1),
                           ('greet_per_job_min', 1.5), ('greet_per_job_max', True),
                           ('read_delay_min', 10 ** 1000),
                           ('monitor_interval_seconds', 59), ('read_delay_min', '120')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_pacing({**original, key: value})
        with self.assertRaises(ValueError):
            validate_pacing({**original, 'api_key': 'unexpected'})
        with self.assertRaises(ValueError):
            validate_pacing({})
        self.assertEqual(validate_pacing(original), original)

    def test_config_save_does_not_change_running_request_intervals_or_job_round(self):
        with TemporaryDirectory() as folder:
            config = {'recruiting': {'read_delay_min': 120, 'read_delay_max': 180}, 'ai': {'model': 'old'}}
            service = RecruitingService(Path(folder) / 'recruiting.db', lambda: config, FakeBrowser())
            config['recruiting'].update(read_delay_min=30, read_delay_max=60, greet_per_job_min=2, greet_per_job_max=2)
            config['ai']['model'] = 'new'
            self.assertTrue(service.pacing_state()['restart_required'])
            self.assertEqual(service._recruiting_cfg()['greet_per_job_min'], 5)
            with patch.object(service.store, 'count_request') as counter, patch('bosshunter.recruiting.service.random.uniform', side_effect=lambda low, high: low):
                service._count_request('identity')
            self.assertEqual(counter.call_args.kwargs['min_interval'], 120)
            self.assertEqual(service.config_provider()['ai']['model'], 'new')
            restarted = RecruitingService(service.store.path, lambda: config, FakeBrowser())
            self.assertFalse(restarted.pacing_state()['restart_required'])
            self.assertEqual(restarted._recruiting_cfg()['read_delay_min'], 30)
            self.assertEqual(restarted._recruiting_cfg()['greet_per_job_min'], 2)

    def test_old_worker_keeps_restart_notice_after_web_only_restart(self):
        with TemporaryDirectory() as folder:
            config = {'recruiting': {}}
            old = RecruitingService(Path(folder) / 'recruiting.db', lambda: config, FakeBrowser())
            old.store.set_setting('worker_pacing', old._startup_pacing)
            old._write_heartbeat()
            config['recruiting']['monitor_interval_seconds'] = 900
            restarted_web = RecruitingService(old.store.path, lambda: config, FakeBrowser())
            self.assertTrue(restarted_web.pacing_state()['restart_required'])
            new_worker = RecruitingService(old.store.path, lambda: config, FakeBrowser())
            new_worker.store.set_setting('worker_pacing', new_worker._startup_pacing)
            new_worker._write_heartbeat()
            self.assertFalse(restarted_web.pacing_state()['restart_required'])
            self.assertTrue(old.pacing_state()['restart_required'])

    def test_legacy_worker_without_a_snapshot_requires_restart(self):
        with TemporaryDirectory() as folder:
            service = RecruitingService(Path(folder) / 'recruiting.db', lambda: {}, FakeBrowser())
            service._write_heartbeat()
            self.assertTrue(service.pacing_state()['restart_required'])


class PacingSettingsApiTests(TestCase):
    def setUp(self):
        self.original_base = server.BASE_DIR
        self.temporary = TemporaryDirectory()
        server.set_base_dir(Path(self.temporary.name))
        save_config({'ai': {'api_key': 'synthetic-key', 'model': 'synthetic-model'},
                     'recruiting': {'read_daily_limit': 123, 'auto_reply_daily_limit': 7},
                     'browser': {'proxy_port': 3456}}, server.CONFIG_PATH)
        self.saved = pacing_values(load_config(server.CONFIG_PATH))

    def tearDown(self):
        server.set_base_dir(self.original_base)
        self.temporary.cleanup()

    def post(self, payload, content_type='application/json'):
        environ, status = {}, {}
        setup_testing_defaults(environ)
        body = json.dumps(payload).encode('utf-8')
        environ.update(REQUEST_METHOD='POST', PATH_INFO='/api/recruiting/pacing',
                       CONTENT_TYPE=content_type, CONTENT_LENGTH=str(len(body)),
                       REMOTE_ADDR='127.0.0.1', **{'wsgi.input': io.BytesIO(body)})
        def start_response(value, headers, exc_info=None):
            status['value'] = int(value.split()[0])
        result = server.app(environ, start_response)
        try:
            data = json.loads(b''.join(result))
        finally:
            if hasattr(result, 'close'):
                result.close()
        return status['value'], data

    def test_save_requires_explicit_confirmation_and_never_calls_the_browser(self):
        with patch.object(server, '_write_config') as write, patch('bosshunter.recruiting.api.RecruitingService') as service:
            for confirmation in (False, None, 1, 'true'):
                code, _ = self.post({'confirmed': confirmation, 'expected': self.saved, 'values': self.saved})
                self.assertEqual(code, 400)
            write.assert_not_called()
            service.assert_not_called()

    def test_confirmed_save_merges_only_pacing_and_preserves_credentials_and_budget(self):
        values = {**self.saved, 'greet_delay_min': 90, 'greet_delay_max': 150}
        with patch('bosshunter.recruiting.api.RecruitingService') as service:
            code, data = self.post({'confirmed': True, 'expected': self.saved, 'values': values})
            service.assert_not_called()
        self.assertEqual(code, 200)
        self.assertEqual(data, {'ok': True, 'restart_required': True, 'restart_mode': 'manual'})
        config = load_config(server.CONFIG_PATH)
        self.assertEqual(pacing_values(config), values)
        self.assertEqual(config['recruiting']['read_daily_limit'], 123)
        self.assertEqual(config['recruiting']['auto_reply_daily_limit'], 7)
        self.assertEqual(config['ai']['api_key'], 'synthetic-key')
        self.assertNotIn('synthetic-key', server.CONFIG_PATH.read_text())
        self.assertEqual(config['browser']['proxy_port'], 3456)

    def test_stale_page_cannot_overwrite_newer_pacing(self):
        new_values = {**self.saved, 'greet_per_job_min': 2, 'greet_per_job_max': 2}
        self.assertEqual(self.post({'confirmed': True, 'expected': self.saved, 'values': new_values})[0], 200)
        with patch.object(server, '_write_config') as write:
            code, _ = self.post({'confirmed': True, 'expected': self.saved, 'values': self.saved})
            self.assertEqual(code, 409)
            write.assert_not_called()

    def test_simultaneous_pacing_edits_cannot_both_overwrite_the_same_revision(self):
        payload = {'confirmed': True, 'expected': self.saved,
                   'values': {**self.saved, 'monitor_interval_seconds': 900}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.post, [payload, payload]))
        self.assertEqual(sorted(code for code, _ in results), [200, 409])

    def test_invalid_parameters_and_content_type_do_not_write(self):
        with patch.object(server, '_write_config') as write:
            for values in ({}, {**self.saved, 'read_delay_min': 999}, {**self.saved, 'monitor_interval_seconds': 1}):
                self.assertEqual(self.post({'confirmed': True, 'expected': self.saved, 'values': values})[0], 400)
            self.assertEqual(self.post({}, 'text/plain')[0], 415)
            write.assert_not_called()
