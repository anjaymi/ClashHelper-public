"""Loopback-only browser editor backed by the existing GitHub CLI login."""

from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
import webbrowser

from scripts.prepare_sites import parse_sites

ROOT = Path(__file__).resolve().parent
COOKIE = 'clashhelper_session'


class UserError(Exception):
    pass


def load_settings():
    try:
        settings = json.loads((ROOT / '.clashhelper.json').read_text(encoding='utf-8'))
        repo = settings['repository']
        gist_id = settings['gist_id']
        if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError()
        if not isinstance(gist_id, str) or not re.fullmatch(r'[0-9a-f]{20,40}', gist_id):
            raise ValueError()
        return repo, gist_id
    except (OSError, ValueError, KeyError, TypeError):
        raise UserError('请按 README 创建并填写 .clashhelper.json，设置你自己的 GitHub 仓库和 Secret Gist ID。') from None


def protect(data, decrypt=False):
    """Windows DPAPI, scoped to the signed-in Windows user."""
    if os.name != 'nt':
        raise UserError('本机地址记录需要 Windows；请在 Windows 上打开管理页。')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise UserError('Windows 无法读取或保存本机加密记录。')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


class LocalStore:
    def __init__(self, path=ROOT / '.subscriptions.enc', repo=None):
        self.path = Path(path)
        self.repo = repo

    def read(self):
        if not self.path.exists():
            return {'urls': [], 'saved_at': None}
        try:
            data = json.loads(protect(self.path.read_bytes(), decrypt=True))
            if data.get('repo') != self.repo or not isinstance(data.get('urls'), list):
                raise ValueError()
            return data
        except (OSError, ValueError, UserError):
            raise UserError('本机记录无法解密，请使用原 Windows 账号打开，或重新填写地址。') from None

    def encode(self, urls):
        data = dict(repo=self.repo, urls=urls, saved_at=datetime.now(timezone.utc).isoformat())
        return protect(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def write(self, encrypted):
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_bytes(encrypted)
        temporary.replace(self.path)


class GitHub:
    def __init__(self, repo, gist_id):
        self.repo = repo
        self.gist_id = gist_id
        self.info = None
        self.info_at = 0

    def run(self, args, *, value=None, error='GitHub 操作失败，请检查网络和 GitHub 登录。'):
        try:
            result = subprocess.run(['gh', *args], input=value, capture_output=True,
                                    encoding='utf-8', timeout=45,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired):
            raise UserError(error) from None
        if result.returncode:
            # gh output could include sensitive data. Never forward it to the page or log.
            raise UserError(error)
        return result.stdout.strip()

    def secret_names(self):
        return {item['name'] for item in json.loads(self.run(
            ['secret', 'list', '--repo', self.repo, '--json', 'name']))}

    def snapshot(self):
        if self.info is None or time.monotonic() - self.info_at > 60:
            with ThreadPoolExecutor(max_workers=2) as pool:
                account = pool.submit(self.run, ['api', 'user', '--jq', '.login'])
                names = pool.submit(self.secret_names)
                secret_names = names.result()
                account_name = account.result()
                self.info = dict(account=account_name, repo=self.repo,
                                 configured='SUBSCRIPTIONS' in secret_names,
                                 advanced='SITES_YAML' in secret_names,
                                 subscription_url=f'https://gist.githubusercontent.com/{account_name}/{self.gist_id}/raw/clash.yaml')
                self.info_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs_future = pool.submit(self.run, ['run', 'list', '--repo', self.repo, '--workflow', 'merge.yml',
                                                '--limit', '1', '--json', 'status,conclusion,url,createdAt,databaseId'])
            report_future = pool.submit(self.run, ['api', f'gists/{self.gist_id}', '--jq', '.files["status.json"].content // ""'])
            runs = json.loads(runs_future.result())
            report_text = report_future.result()
            report = json.loads(report_text) if report_text else None
        return dict(self.info, run=runs[0] if runs else None, report=report)

    def save(self, urls):
        if 'SITES_YAML' in self.secret_names():
            raise UserError('仓库正在使用高级筛选配置。请先在 GitHub 删除 SITES_YAML，再通过此页面管理地址。')
        self.run(['secret', 'set', 'SUBSCRIPTIONS', '--repo', self.repo],
                 value='\n'.join(urls) + '\n', error='地址未保存成功，请检查 GitHub 登录、仓库权限或网络。')
        self.info = None

    def trigger(self):
        self.run(['workflow', 'run', 'merge.yml', '--repo', self.repo, '--ref', 'main'],
                 error='更新任务未启动，请检查网络后点击“立即更新”重试。')


class Manager:
    def __init__(self, github=None, store=None):
        self.github = github or GitHub(*load_settings())
        self.store = store or LocalStore(repo=self.github.repo)
        self.lock = threading.Lock()

    def state(self):
        with self.lock:
            result = self.github.snapshot()
            try:
                local = self.store.read()
                result.update(urls=local['urls'], saved_at=local.get('saved_at'), local_warning=None)
            except UserError as exc:
                result.update(urls=[], saved_at=None, local_warning=str(exc))
            return result

    def save(self, urls):
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise UserError('请填写有效的订阅地址。')
        text = '\n'.join(url.strip() for url in urls if url.strip())
        if len(text.encode('utf-8')) > 45000:
            raise UserError('地址总长度过大，请减少订阅数量。')
        try:
            sites = parse_sites(text, '')
        except ValueError:
            raise UserError('请填写完整的 HTTP 或 HTTPS 订阅链接，每个输入框一个。') from None
        if not sites:
            raise UserError('请至少填写一个订阅地址。')
        cleaned = [site['url'] for site in sites]
        if not self.lock.acquire(blocking=False):
            raise UserError('正在处理上一项操作，请稍后再试。')
        try:
            encrypted = self.store.encode(cleaned)
            self.github.save(cleaned)
            warning = None
            try:
                self.store.write(encrypted)
            except OSError:
                warning = 'GitHub 已保存，本机记录写入失败；下次打开需要重新填写。'
            try:
                self.github.trigger()
            except UserError:
                return dict(saved=True, started=False, count=len(cleaned), urls=cleaned,
                            message='地址已保存，更新任务暂未启动。请点击“立即更新”重试。', warning=warning)
            return dict(saved=True, started=True, count=len(cleaned), urls=cleaned,
                        message=f'已保存 {len(cleaned)} 个订阅，更新任务已提交。', warning=warning)
        finally:
            self.lock.release()

    def trigger(self):
        with self.lock:
            names = self.github.secret_names()
            if not names.intersection({'SUBSCRIPTIONS', 'SITES_YAML'}):
                raise UserError('请先填写订阅并点击“保存并更新”。')
            self.github.trigger()
        return dict(started=True, message='更新任务已提交，稍后会替换合并订阅。')


class ManagerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager=None):
        super().__init__(address, Handler)
        self.manager = manager or Manager()
        self.session = secrets.token_urlsafe(32)
        self.origin = f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def log_error(self, *args):
        pass

    def authenticated(self):
        if self.headers.get('Host') != urlsplit(self.server.origin).netloc:
            return False
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            value = cookie[COOKIE].value if COOKIE in cookie else ''
            return secrets.compare_digest(value, self.server.session)
        except Exception:
            return False

    def send(self, status, data, mime='application/json; charset=utf-8', cookie=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlsplit(self.path)
        key = parse_qs(parsed.query).get('key', [''])[0]
        if (parsed.path == '/' and self.headers.get('Host') == urlsplit(self.server.origin).netloc
                and secrets.compare_digest(key, self.server.session)):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', f'{COOKIE}={self.server.session}; HttpOnly; SameSite=Strict; Path=/')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            return
        if not self.authenticated():
            self.send(HTTPStatus.FORBIDDEN, '请双击“Clash 订阅管理”快捷方式打开。'.encode('utf-8'), 'text/plain; charset=utf-8')
            return
        try:
            if parsed.path == '/':
                self.send(HTTPStatus.OK, (ROOT / 'ui' / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            elif parsed.path == '/api/ping':
                self.send(HTTPStatus.OK, dict(app='ClashHelper', pid=os.getpid()))
            elif parsed.path == '/api/state':
                self.send(HTTPStatus.OK, self.server.manager.state())
            else:
                self.send(HTTPStatus.NOT_FOUND, dict(error='页面不存在。'))
        except UserError as exc:
            self.send(HTTPStatus.BAD_GATEWAY, dict(error=str(exc)))
        except Exception:
            self.send(HTTPStatus.INTERNAL_SERVER_ERROR, dict(error='操作失败，请重新打开管理页。'))

    def do_POST(self):
        if not self.authenticated() or self.headers.get('Origin') != self.server.origin:
            self.send(HTTPStatus.FORBIDDEN, dict(error='请从本机管理页操作。'))
            return
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self.send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, dict(error='请求格式错误。'))
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            self.send(HTTPStatus.BAD_REQUEST, dict(error='请求格式错误。'))
            return
        try:
            if self.path == '/api/save':
                result = self.server.manager.save(body.get('urls'))
            elif self.path == '/api/update':
                result = self.server.manager.trigger()
            elif self.path == '/api/quit':
                result = dict(message='管理页已退出。再次双击快捷方式即可打开。')
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send(HTTPStatus.NOT_FOUND, dict(error='接口不存在。'))
                return
            self.send(HTTPStatus.OK, result)
        except UserError as exc:
            self.send(HTTPStatus.BAD_REQUEST, dict(error=str(exc)))
        except Exception:
            self.send(HTTPStatus.INTERNAL_SERVER_ERROR, dict(error='操作失败，请重试；凭据不会输出到日志。'))


def open_existing(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        parsed = urlsplit(record['origin'])
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
            return False
        request = Request(record['origin'] + '/api/ping',
                          headers={'Cookie': f"{COOKIE}={record['session']}"})
        with urlopen(request, timeout=2) as response:
            if json.load(response).get('app') != 'ClashHelper':
                return False
        webbrowser.open(record['origin'] + '/?key=' + record['session'])
        return True
    except (OSError, ValueError, KeyError):
        return False


def main():
    state_path = ROOT / '.manager.json'
    if open_existing(state_path):
        return
    try:
        manager = Manager()
    except UserError as exc:
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, str(exc), 'ClashHelper', 0)
        else:
            print(str(exc))
        return
    try:
        server = ManagerServer(('127.0.0.1', 8768), manager)
    except OSError:
        server = ManagerServer(('127.0.0.1', 0), manager)
    record = dict(origin=server.origin, session=server.session, pid=os.getpid())
    temporary = state_path.with_name(state_path.name + '.tmp')
    temporary.write_text(json.dumps(record), encoding='utf-8')
    temporary.replace(state_path)
    webbrowser.open(server.origin + '/?key=' + server.session)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            if json.loads(state_path.read_text(encoding='utf-8')).get('pid') == os.getpid():
                state_path.unlink()
        except (OSError, ValueError):
            pass


if __name__ == '__main__':
    main()
