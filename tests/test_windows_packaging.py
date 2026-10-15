"""Portable tests for launcher safety; no Windows processes/platform traffic."""
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from bosshunter.config import load_config, save_config

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('windows_launcher', ROOT / 'packaging/windows/launcher.py')
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class WindowsPackagingTests(TestCase):
    def test_fresh_config_is_separate_and_preserves_recruiting_pacing(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            path, config = launcher.prepare_config(base)
            self.assertEqual(path, base / 'config.yaml')
            self.assertEqual(config['browser']['recruiting_user_data_dir'], str(base / 'chrome-profile'))
            self.assertFalse(config['browser']['auto_start_proxy'])
            self.assertEqual(config['recruiting']['read_delay_min'], 120)
            self.assertEqual(config['recruiting']['read_delay_max'], 180)
            self.assertEqual(config['recruiting']['monitor_interval_seconds'], 600)
            self.assertFalse((base / '.config.credentials.yaml').exists())
            self.assertFalse((base / 'data/recruiting.db').exists())

    def test_existing_configuration_credentials_and_data_survive(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            path, config = launcher.prepare_config(base)
            config['ai']['api_key'] = 'synthetic-key'
            config['recruiting']['read_daily_limit'] = 123
            config['browser']['auto_start_proxy'] = True
            save_config(config, path)
            data = base / 'existing-data'
            data.write_text('retained')
            launcher.prepare_config(base)
            saved = load_config(path)
            self.assertEqual(saved['ai']['api_key'], 'synthetic-key')
            self.assertNotIn('synthetic-key', path.read_text())
            self.assertEqual(saved['recruiting']['read_daily_limit'], 123)
            self.assertEqual(data.read_text(), 'retained')

    def test_nonlocal_proxy_rejected(self):
        with TemporaryDirectory() as folder:
            path, config = launcher.prepare_config(Path(folder))
            config['browser']['proxy_host'] = '0.0.0.0'
            save_config(config, path)
            with self.assertRaisesRegex(RuntimeError, '127.0.0.1'):
                launcher.prepare_config(Path(folder))

    def test_frozen_child_uses_executable_and_preserves_spaced_paths(self):
        with patch.object(launcher.sys, 'frozen', True, create=True), patch.object(launcher.sys, 'executable', 'C:/Program Files/BossHunter.exe'):
            self.assertEqual(launcher.child_command('web', Path('C:/User Data/config.yaml')),
                             ['C:/Program Files/BossHunter.exe', '--service', 'web', 'C:/User Data/config.yaml'])

    def test_port_conflict_never_spawns_or_kills_unrelated_processes(self):
        with TemporaryDirectory() as folder, patch.object(launcher, 'port_free', return_value=False), patch.object(launcher.subprocess, 'Popen') as spawn:
            controller = launcher.Controller(Path(folder))
            with self.assertRaisesRegex(RuntimeError, '8686'):
                controller.start()
            spawn.assert_not_called()
            self.assertEqual(controller.processes, [])

    def test_startup_failure_cleans_up_its_owned_processes(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'logs').mkdir()
            controller = launcher.Controller(base)
            process, job = Mock(), Mock()
            process.poll.return_value = None
            def fail_spawn(*args):
                controller.processes.append(process)
                raise RuntimeError('synthetic startup error')
            with patch.object(launcher, 'port_free', return_value=True), patch.object(launcher, 'find_chrome', return_value=Path('chrome.exe')), patch.object(launcher, 'node_path', return_value=Mock(is_file=lambda: True)), patch.object(launcher, 'WindowsJob', return_value=job), patch.object(controller, 'spawn', side_effect=fail_spawn):
                with self.assertRaisesRegex(RuntimeError, 'synthetic startup error'):
                    controller.start()
            process.terminate.assert_called_once()
            job.close.assert_called_once()
            self.assertEqual(controller.processes, [])

    def test_stop_handles_already_exited_children_and_is_idempotent(self):
        controller = launcher.Controller(Path('.'))
        exited, active, job = Mock(), Mock(), Mock()
        exited.poll.return_value = 0
        active.poll.return_value = None
        controller.processes = [exited, active]
        controller.job = job
        controller.stop()
        controller.stop()
        exited.terminate.assert_not_called()
        active.terminate.assert_called_once()
        job.close.assert_called_once()

    def test_cancellation_interrupts_health_wait_without_network(self):
        controller = launcher.Controller(Path('.'))
        controller.cancel.set()
        with patch.object(launcher, 'http_read') as read:
            with self.assertRaisesRegex(RuntimeError, '取消'):
                controller.wait_ready('http://127.0.0.1:9222/json/version', lambda _: True)
            read.assert_not_called()

    def test_user_directory_does_not_depend_on_source_checkout(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': 'C:/Users/Test/AppData/Local'}):
            self.assertEqual(launcher.user_root(), Path('C:/Users/Test/AppData/Local/BossHunter'))

    def test_spec_resource_manifest_contains_only_distribution_resources(self):
        captured = {}
        fake_a = Mock(pure=[], scripts=[], binaries=[], datas=[])
        def analysis(*args, **kwargs):
            captured.update(kwargs)
            return fake_a
        hook = Mock(collect_all=lambda _: ([], [], []), collect_submodules=lambda _: [], copy_metadata=lambda *a, **kw: [])
        with TemporaryDirectory() as folder, patch.dict(os.environ, {'BOSSHUNTER_BUILD_NODE': folder}), patch.dict('sys.modules', {'PyInstaller': Mock(), 'PyInstaller.utils': Mock(), 'PyInstaller.utils.hooks': hook}):
            namespace = {'SPECPATH': str(ROOT / 'packaging/windows'), 'Analysis': analysis,
                         'PYZ': Mock(), 'EXE': Mock(), 'COLLECT': Mock()}
            exec(compile((ROOT / 'packaging/windows/BossHunter.spec').read_text(), 'BossHunter.spec', 'exec'), namespace)
        sources = [Path(source) for source, _ in captured['datas']]
        self.assertIn(ROOT / 'src/bosshunter/browser/runtime/chat-observer.mjs', sources)
        self.assertNotIn(ROOT / 'config.yaml', sources)
        self.assertNotIn(ROOT / 'data', sources)
        self.assertFalse(any('__pycache__' in str(source) for source in sources))
