"""Windows desktop bootstrap. No platform actions until the user starts it."""
from __future__ import annotations

import json
import multiprocessing
import ntpath
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from contextlib import contextmanager, nullcontext

APP_MUTEX = 'Local\\BossHunter.Desktop'
_dll_lock = threading.RLock()


def external_environment(env=None):
    result = dict(os.environ if env is None else env)
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        return result
    root = ntpath.normcase(ntpath.abspath(str(resource_root())))
    paths = []
    for entry in result.get('PATH', '').split(';'):
        if not entry:
            continue
        candidate = ntpath.normcase(ntpath.abspath(entry.strip('"')))
        try:
            bundled = ntpath.commonpath([root, candidate]) == root
        except ValueError:  # A different drive cannot be inside the bundle.
            bundled = False
        if not bundled:
            paths.append(entry)
    result['PATH'] = ';'.join(paths)
    return result


@contextmanager
def external_dll_search():
    """Do not pass PyInstaller's Python DLL search directory to Chrome/Node."""
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        yield
        return
    import ctypes
    from ctypes import wintypes as w
    with _dll_lock:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetDllDirectoryW.argtypes = [w.DWORD, w.LPWSTR]
        kernel.GetDllDirectoryW.restype = w.DWORD
        kernel.SetDllDirectoryW.argtypes = [w.LPCWSTR]
        kernel.SetDllDirectoryW.restype = w.BOOL
        size = kernel.GetDllDirectoryW(0, None)
        previous = None
        if size:
            buffer = ctypes.create_unicode_buffer(size + 1)
            if not kernel.GetDllDirectoryW(len(buffer), buffer):
                raise ctypes.WinError(ctypes.get_last_error())
            previous = buffer.value
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not kernel.SetDllDirectoryW(previous):
                raise ctypes.WinError(ctypes.get_last_error())


def open_dashboard():
    with external_dll_search():
        webbrowser.open('http://127.0.0.1:8686/recruiting')


def resource_root():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2] / 'src'


def user_root():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise RuntimeError('未找到 LOCALAPPDATA，请在 Windows 普通用户会话中启动。')
    return Path(base) / 'BossHunter'


def node_path():
    return resource_root() / 'vendor/node/node.exe'


def child_command(role, config):
    prefix = [sys.executable]
    if not getattr(sys, 'frozen', False):
        prefix.append(str(Path(__file__).resolve()))
    return prefix + ['--service', role, str(config)]


def find_chrome():
    for name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        base = os.environ.get(name)
        if base:
            path = Path(base) / 'Google/Chrome/Application/chrome.exe'
            if path.is_file():
                return path
    raise RuntimeError('未找到 Google Chrome。请先安装 Chrome，再点击启动系统。')


def prepare_config(base):
    from bosshunter.config import DEFAULTS, load_config, save_config
    path = base / 'config.yaml'
    if not path.exists():
        config = json.loads(json.dumps(DEFAULTS))
        config['browser'].update(recruiting_user_data_dir=str(base / 'chrome-profile'),
                                 auto_start_proxy=False)
        config['recruiting']['monitor_interval_seconds'] = 600
        save_config(config, path)
    config = load_config(path)
    browser = config['browser']
    if browser.get('proxy_host', '127.0.0.1') != '127.0.0.1':
        raise RuntimeError('桌面版浏览器代理仅支持本机地址 127.0.0.1。')
    # The launcher owns the proxy lifetime; service processes must not spawn
    # detached replacement proxies after it has been stopped.
    if browser.get('auto_start_proxy'):
        browser['auto_start_proxy'] = False
        save_config(config, path)
    profile = Path(browser.get('recruiting_user_data_dir') or base / 'chrome-profile').expanduser()
    if not profile.is_absolute():
        profile = base / profile
    if str(profile) != browser.get('recruiting_user_data_dir'):
        browser['recruiting_user_data_dir'] = str(profile)
        save_config(config, path)
    return path, config


def port_free(port):
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def http_read(url):
    # Local health checks must not travel through the user's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=1) as response:
        return response.read()


class WindowsJob:
    """Kill only owned backend process trees if the launcher exits/crashes."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.c = ctypes
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        self.k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.k.CreateJobObjectW.restype = w.HANDLE
        self.k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.k.SetInformationJobObject.restype = w.BOOL
        self.k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.k.AssignProcessToJobObject.restype = w.BOOL
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.k.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.c.WinError(self.c.get_last_error())

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


class Controller:
    def __init__(self, base):
        self.base = base
        self.processes = []
        self.logs = []
        self.job = None
        self.cancel = threading.Event()
        self.lock = threading.RLock()

    def spawn(self, args, name, env, *, external=False):
        log = (self.base / 'logs' / (name + '.log')).open('ab')
        self.logs.append(log)
        with external_dll_search() if external else nullcontext():
            process = subprocess.Popen(args, cwd=self.base,
                                       env=external_environment(env) if external else env,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        self.processes.append(process)
        self.job.assign(process)
        return process

    def wait_ready(self, url, check, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                raise RuntimeError('启动已取消。')
            if any(p.poll() is not None for p in self.processes):
                raise RuntimeError('后台进程提前退出，请打开日志查看原因。')
            try:
                if check(http_read(url)):
                    return
            except (OSError, ValueError):
                pass
            self.cancel.wait(.3)
        raise RuntimeError('启动超时，请检查浏览器或后台日志：' + url)

    def start(self):
        with self.lock:
            try:
                config_path, config = prepare_config(self.base)
                browser = config['browser']
                proxy_port = int(browser.get('proxy_port', 3456))
                chrome_port = int(browser.get('chrome_ports', [9222])[0])
                if len({8686, proxy_port, chrome_port}) != 3 or not all(1 <= p <= 65535 for p in (8686, proxy_port, chrome_port)):
                    raise RuntimeError('Web、代理与 Chrome 调试端口需为不同的有效端口。')
                for port in (8686, proxy_port):
                    if not port_free(port):
                        raise RuntimeError(f'端口 {port} 已被使用，请先停止旧服务。本程序不会终止其他进程。')
                chrome = find_chrome()
                if not node_path().is_file():
                    raise RuntimeError('安装文件缺少内置 Node.js，请重新构建或安装。')
                env = os.environ.copy()
                env['BOSSHUNTER_NODE_PATH'] = str(node_path())
                env['BOSSHUNTER_BROWSER_PROXY_PORT'] = str(proxy_port)
                env['BOSSHUNTER_CHROME_PORTS'] = ','.join(map(str, browser.get('chrome_ports', [chrome_port])))
                env['BOSSHUNTER_ENABLE_PORT_GUARD'] = 'true' if browser.get('enable_port_guard', True) else 'false'
                profile = Path(browser['recruiting_user_data_dir'])
                if not port_free(chrome_port):
                    raise RuntimeError(f'Chrome 调试端口 {chrome_port} 已被使用。请关闭之前由本程序启动的独立 Chrome 后重试。')
                self.job = WindowsJob()
                self.spawn([str(chrome), f'--remote-debugging-port={chrome_port}',
                                      '--remote-debugging-address=127.0.0.1', f'--user-data-dir={profile}',
                                      '--no-first-run', '--no-default-browser-check', 'https://www.zhipin.com/web/chat/index'],
                           'chrome', env, external=True)
                self.wait_ready(f'http://127.0.0.1:{chrome_port}/json/version',
                                lambda body: bool(json.loads(body).get('webSocketDebuggerUrl')))
                script = resource_root() / 'bosshunter/browser/runtime/cdp-proxy.mjs'
                self.spawn([str(node_path()), str(script)], 'proxy', env, external=True)
                self.wait_ready(f'http://127.0.0.1:{proxy_port}/health',
                                lambda body: json.loads(body).get('runtime') == 'bosshunter')
                self.spawn(child_command('web', config_path), 'web', env)
                self.wait_ready('http://127.0.0.1:8686/recruiting', lambda body: b'<html' in body.lower())
                self.spawn(child_command('recruiting-worker', config_path), 'worker', env)
                self.cancel.wait(1)
                if self.cancel.is_set() or any(p.poll() is not None for p in self.processes):
                    raise RuntimeError('启动取消或后台进程退出，请查看日志。')
                open_dashboard()
            except Exception:
                self.stop()
                raise

    def stop(self):
        with self.lock:
            for process in reversed(self.processes):
                if process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass  # A process may exit between poll and terminate.
            if self.job:
                self.job.close()
                self.job = None
            for process in self.processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.processes.clear()
            for log in self.logs:
                log.close()
            self.logs.clear()


def check_cookie_worker():
    """Check packaged COM/WMI access in a worker, without reading any cookies."""
    if sys.platform != 'win32':
        return
    from threading import Thread
    from bosshunter.recruiting.local_session import _cookie_com_context
    errors = []

    def check():
        try:
            with _cookie_com_context():
                import browser_cookie3  # noqa: F401
                import wmi
                connection = wmi.WMI()  # Local service only; no shadow creation.
                del connection  # Release COM objects before leaving the apartment.
        except Exception as exc:
            errors.append(exc)

    worker = Thread(target=check, daemon=True, name='cookie-com-smoke-test')
    worker.start()
    worker.join(timeout=30)
    if worker.is_alive():
        raise RuntimeError('Windows cookie COM/WMI worker check timed out.')
    if errors:
        raise RuntimeError('Windows cookie COM/WMI worker check failed.') from errors[0]


def smoke_test(destination):
    """Validate frozen imports, native libraries and shipped files, offline."""
    result = {'ok': False}
    try:
        from bosshunter.web.server import FRONTEND_DIR, SCHEMA_PATH
        from bosshunter.recruiting.service import RecruitingService  # noqa: F401
        import browser_cookie3  # noqa: F401
        check_cookie_worker()
        import pymupdf  # noqa: F401
        import xhtml2pdf  # noqa: F401
        from rapidocr_onnxruntime import RapidOCR
        from patchright.sync_api import sync_playwright
        from zoneinfo import ZoneInfo
        import tkinter
        tkinter.Tcl().eval('info patchlevel')
        assert (FRONTEND_DIR / 'index.html').is_file()
        assert SCHEMA_PATH.is_file()
        assert (resource_root() / 'bosshunter/browser/runtime/chat-observer.mjs').is_file()
        assert (resource_root() / 'bosshunter/data/boss_cities.json').is_file()
        ZoneInfo('Asia/Shanghai')
        with external_dll_search():
            version = subprocess.check_output([str(node_path()), '--version'], text=True,
                                              env=external_environment(),
                                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0).strip()
        assert int(version.lstrip('v').split('.')[0]) >= 22
        with sync_playwright():
            pass  # Starts only the bundled local driver, not a browser.
        RapidOCR()  # Check bundled ONNX models and native runtime.
        result.update(ok=True, node_version=version)
    except Exception:
        result['error'] = traceback.format_exc()
    Path(destination).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['ok'] else 1


def service(role, path):
    base = Path(path).parent
    with (base / 'logs' / (role + '-python.log')).open('a', encoding='utf-8', buffering=1) as log:
        sys.stdout = sys.stderr = log
        from bosshunter.main import cli
        args = ['--config', path, role]
        if role == 'web':
            args += ['--port', '8686', '--no-open']
        try:
            cli(args=args)
        except Exception:
            traceback.print_exc()
            raise


def desktop():
    import ctypes
    from ctypes import wintypes
    import tkinter as tk
    from tkinter import messagebox, ttk
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, False, APP_MUTEX)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        messagebox.showinfo('BossHunter', '启动器已经运行，请查看现有窗口。')
        return
    base = user_root()
    (base / 'logs').mkdir(parents=True, exist_ok=True)
    controller = Controller(base)
    events = queue.Queue()
    root = tk.Tk()
    root.title('BossHunter 启动器')
    root.geometry('600x300')
    status = tk.StringVar(value='未启动。首次使用请先安装 Chrome，启动后登录 BOSS 并配置模型。')
    ttk.Label(root, text='BossHunter 招聘助手', font=('', 18)).pack(pady=18)
    ttk.Label(root, textvariable=status, wraplength=550).pack(padx=20, pady=10)
    row = ttk.Frame(root)
    row.pack(pady=15)
    busy, closing, running = False, False, False

    def action(function, done):
        def work():
            try:
                function()
                events.put((done, None))
            except Exception as exc:
                with (base / 'logs/launcher.log').open('a', encoding='utf-8') as log:
                    traceback.print_exc(file=log)
                events.put((done, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def start():
        nonlocal busy
        busy = True
        start_button.configure(state='disabled')
        controller.cancel.clear()
        status.set('正在启动 Chrome、代理和后台服务，请稍候……')
        action(controller.start, 'started')

    def stop():
        nonlocal busy
        busy = True
        controller.cancel.set()
        stop_button.configure(state='disabled')
        status.set('正在停止后台服务……')
        action(controller.stop, 'stopped')

    def close():
        nonlocal closing
        if closing:
            return
        closing = True
        controller.cancel.set()
        stop()

    def poll():
        nonlocal busy, running
        while not events.empty():
            done, error = events.get_nowait()
            if closing and done != 'stopped':
                continue
            busy = False
            running = done == 'started' and error is None
            if closing:
                root.destroy()
                return
            status.set(error or ('运行中。请在网页内完成配置；自动任务由网页开关控制。' if running else '后台服务及本次启动的独立 Chrome 已停止，可重新启动。'))
            start_button.configure(state='disabled' if running else 'normal')
            stop_button.configure(state='normal' if running else 'disabled')
        if running and not busy and any(p.poll() is not None for p in controller.processes):
            stop()
            status.set('后台进程异常退出，正在清理。请查看日志后重试。')
        root.after(250, poll)

    start_button = ttk.Button(row, text='启动系统', command=start)
    start_button.pack(side='left', padx=6)
    stop_button = ttk.Button(row, text='停止系统', command=stop, state='disabled')
    stop_button.pack(side='left', padx=6)
    ttk.Button(row, text='打开页面', command=open_dashboard).pack(side='left', padx=6)
    ttk.Button(row, text='查看日志', command=lambda: os.startfile(base / 'logs')).pack(side='left', padx=6)
    ttk.Label(root, text='关闭启动器会停止后台服务和本次独立 Chrome；普通 Chrome 不受影响，数据保留。', wraplength=550).pack(pady=8)
    root.protocol('WM_DELETE_WINDOW', close)
    root.after(250, poll)
    try:
        root.mainloop()
    finally:
        controller.cancel.set()
        controller.stop()
        kernel.CloseHandle(mutex)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    if len(sys.argv) == 3 and sys.argv[1] == '--smoke-test':
        with Path(sys.argv[2]).with_suffix('.log').open('a', encoding='utf-8', buffering=1) as log:
            sys.stdout = sys.stderr = log
            result = smoke_test(sys.argv[2])
        raise SystemExit(result)
    if len(sys.argv) == 4 and sys.argv[1] == '--service' and sys.argv[2] in {'web', 'recruiting-worker'}:
        service(sys.argv[2], sys.argv[3])
    elif sys.platform != 'win32':
        raise SystemExit('The desktop launcher requires Windows x64.')
    else:
        desktop()
