"""Local, single-user ML task workbench. No third-party dependencies."""
import argparse
from contextlib import contextmanager
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import uuid
from monitor import Monitor, read_chunk
from gpu_panel import GpuPanel

ROOT = Path(__file__).resolve().parent
ACTIVE = ('starting', 'running', 'stopping')
RESUMABLE = ('failed', 'stopped', 'terminated', 'interrupted')


class FileLock:
    def __init__(self, path):
        self.file = open(path, 'a+b')
        if self.file.tell() == 0:
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise

    def close(self):
        if self.file.closed:
            return
        if os.name == 'nt':
            import msvcrt
            self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


def is_locked(path):
    if not path.exists():
        return False
    try:
        lock = FileLock(path)
    except OSError as error:
        if isinstance(error, PermissionError) or error.errno in (11, 13, 36):
            return True
        raise
    lock.close()
    return False


@contextmanager
def connect(data):
    db = sqlite3.connect(Path(data) / 'index.sqlite', timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(data):
    data.mkdir(parents=True, exist_ok=True)
    (data / 'attempts').mkdir(exist_ok=True)
    (data / 'work').mkdir(exist_ok=True)
    with connect(data) as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, spec TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(
          id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent TEXT, task_id TEXT NOT NULL,
          spec TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
          started REAL, ended REAL, exit_code INTEGER, error TEXT, pid INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active ON attempts((1))
          WHERE status IN ('starting','running','stopping');
        ''')


def validate_task(value):
    if not isinstance(value, dict):
        raise ValueError('任务配置必须是对象')
    spec = {}
    for key in ('name', 'project', 'cwd'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f'缺少 {key}')
        spec[key] = value[key].strip()
    cwd = Path(spec['cwd']).expanduser()
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError('工作目录必须是现有的绝对目录')
    spec['cwd'] = str(cwd.resolve())
    for key in ('argv', 'resume_argv'):
        argv = value.get(key, [])
        if not isinstance(argv, list) or (key == 'argv' and not argv) or any(not isinstance(a, str) or '\0' in a for a in argv):
            raise ValueError(f'{key} 必须是字符串数组')
        if argv:
            executable = Path(argv[0])
            if not executable.is_absolute() or not executable.is_file():
                raise ValueError('首项必须是可执行文件的完整路径')
            if os.name == 'nt' and executable.suffix.lower() != '.exe':
                raise ValueError('Windows 请指定 .exe；脚本作为参数传入')
        spec[key] = argv
    spec['kind'] = value.get('kind', 'experiment')
    if spec['kind'] not in ('experiment', 'training', 'inference', 'demo'):
        raise ValueError('未知任务类型')
    spec['group'] = value.get('group', '')
    if spec['group'] not in ('', 'preflight', 'formal'):
        raise ValueError('分组必须是前检、正式实验或未分组')
    spec['cooperative_stop'] = bool(value.get('cooperative_stop', False))
    artifact_root = value.get('artifact_root', '')
    if not isinstance(artifact_root, str):
        raise ValueError('产物目录必须是字符串')
    if artifact_root and (not Path(artifact_root).is_absolute() or not Path(artifact_root).is_dir()):
        raise ValueError('自定义产物根必须是现有绝对目录，留空使用运行工作区')
    spec['artifact_root'] = str(Path(artifact_root).resolve()) if artifact_root else ''
    return spec


def attempt(data, ident):
    with connect(data) as db:
        row = db.execute('SELECT * FROM attempts WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError('执行记录不存在')
    result = dict(row)
    result['spec'] = json.loads(result['spec'])
    return result


def reconcile(data):
    with connect(data) as db:
        rows = db.execute("SELECT id,status,created FROM attempts WHERE status IN ('starting','running','stopping')").fetchall()
        for row in rows:
            # The launcher needs a short window to acquire the worker lease.
            if row['status'] == 'starting' and time.time() - row['created'] < 15:
                continue
            if not is_locked(data / 'attempts' / row['id'] / 'worker.lock'):
                db.execute("UPDATE attempts SET status='interrupted',ended=?,error=? WHERE id=? AND status IN ('starting','running','stopping')",
                           (time.time(), '执行器已退出，未记录正常结束；请检查日志后显式恢复。', row['id']))


def state(data):
    reconcile(data)
    with connect(data) as db:
        tasks = [dict(id=row['id'], **json.loads(row['spec'])) for row in db.execute('SELECT * FROM tasks ORDER BY rowid DESC')]
        rows = [dict(row) for row in db.execute('SELECT * FROM attempts ORDER BY created DESC LIMIT 100')]
    for row in rows:
        row['spec'] = json.loads(row['spec'])
    return dict(tasks=tasks, attempts=rows, server_time=time.time())


def view_preferences(data, value=None):
    if value is not None:
        if (set(value) != {'groups', 'task_id', 'attempt_id'}
                or not isinstance(value['groups'], dict)
                or any(type(opened) is not bool for opened in value['groups'].values())
                or not isinstance(value['task_id'], str)
                or not isinstance(value['attempt_id'], str)):
            raise ValueError('界面偏好需要分组开关、任务标识和执行标识')
    with connect(data) as db:
        if value is not None:
            db.execute('INSERT INTO preferences VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('view', json.dumps(value, ensure_ascii=False)))
        row = db.execute('SELECT value FROM preferences WHERE key=?', ('view',)).fetchone()
    return json.loads(row['value']) if row else dict(groups={}, task_id='', attempt_id='')


def save_task(data, value):
    spec = validate_task(value)
    ident = value.get('id') or uuid.uuid4().hex
    if not isinstance(ident, str) or len(ident) > 100:
        raise ValueError('无效任务标识')
    with connect(data) as db:
        # Older registration scripts omit UI grouping; keep an existing choice.
        if 'group' not in value:
            old = db.execute('SELECT spec FROM tasks WHERE id=?', (ident,)).fetchone()
            if old:
                spec['group'] = json.loads(old['spec']).get('group', '')
        db.execute('INSERT INTO tasks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET spec=excluded.spec',
                   (ident, json.dumps(spec, ensure_ascii=False)))
    return ident


def start(data, task_id=None, parent=None):
    reconcile(data)
    ident = uuid.uuid4().hex
    with connect(data) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM attempts WHERE status IN ('starting','running','stopping')").fetchone():
            raise ValueError('已有活动任务；首版一次运行一个任务')
        if parent:
            old = db.execute('SELECT * FROM attempts WHERE id=?', (parent,)).fetchone()
            if old is None or old['status'] not in RESUMABLE:
                raise ValueError('该执行当前不能恢复')
            if db.execute('SELECT 1 FROM attempts WHERE parent=?', (parent,)).fetchone():
                raise ValueError('此执行已有后续尝试，请选择最新尝试')
            spec = json.loads(old['spec'])
            if not spec['resume_argv']:
                raise ValueError('任务未声明恢复命令')
            task_id, run_id = old['task_id'], old['run_id']
        else:
            row = db.execute('SELECT spec FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            spec, run_id = json.loads(row['spec']), ident
        validate_task(spec)
        folder = data / 'attempts' / ident
        folder.mkdir()
        (data / 'work' / run_id).mkdir(exist_ok=True)
        db.execute('INSERT INTO attempts(id,run_id,parent,task_id,spec,status,created) VALUES (?,?,?,?,?,?,?)',
                   (ident, run_id, parent, task_id, json.dumps(spec, ensure_ascii=False), 'starting', time.time()))
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
    try:
        with open(folder / 'worker.log', 'ab') as log:
            proc = subprocess.Popen([sys.executable, '-u', str(ROOT / 'worker.py'), '--data', str(data), '--id', ident],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=flags, start_new_session=(os.name != 'nt'))
        # Retain and reap our child without tying its lifetime to the HTTP service.
        threading.Thread(target=proc.wait, daemon=True).start()
    except OSError as error:
        with connect(data) as db:
            db.execute("UPDATE attempts SET status='failed',ended=?,error=? WHERE id=?", (time.time(), str(error), ident))
        raise ValueError(f'执行器启动失败：{error}') from error
    return ident


def request_stop(data, ident, force=False):
    reconcile(data)
    row = attempt(data, ident)
    if row['status'] not in ACTIVE:
        raise ValueError('该执行已经结束')
    if not force and not row['spec']['cooperative_stop']:
        raise ValueError('脚本未声明协作停止能力，可选择明确的强制终止')
    folder = data / 'attempts' / ident
    (folder / ('force.stop' if force else 'stop.request')).touch()
    with connect(data) as db:
        db.execute("UPDATE attempts SET status='stopping' WHERE id=? AND status IN ('starting','running','stopping')", (ident,))


def artifact_path(data, ident, name):
    row = attempt(data, ident)
    root = Path(row['spec']['artifact_root'] or data / 'work' / row['run_id']).resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError('产物不存在或超出登记根目录')
    # Only paths explicitly reported by this attempt may be downloaded.
    events = data / 'attempts' / ident / 'events.jsonl'
    registered = False
    if events.exists():
        with events.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(event, dict) and event.get('type') == 'artifact' and event.get('path') == name:
                    registered = True; break
    if not registered:
        raise ValueError('该文件未登记为本次产物')
    return target


def make_server(data, port=8765):
    initialize(data)
    token = secrets.token_urlsafe(32)
    monitor = Monitor(data)
    gpu = GpuPanel(data)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def valid_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def respond(self, value, code=200, mime='application/json; charset=utf-8', attachment=False):
            raw = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if attachment:
                self.send_header('Content-Disposition', 'attachment')
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Browser navigation does not change task state.

        def do_GET(self):
            if not self.valid_host():
                return self.respond({'error': 'Host rejected'}, 403)
            url = urlsplit(self.path); query = parse_qs(url.query)
            parts = url.path.strip('/').split('/')
            try:
                if url.path == '/api/state':
                    return self.respond(dict(state(data), token=token))
                if url.path == '/api/gpu':
                    return self.respond(gpu.status())
                if url.path == '/api/gpu/record':
                    return self.respond(gpu.read(query.get('id', [''])[0]))
                if url.path == '/api/view':
                    return self.respond(view_preferences(data))
                if len(parts) == 4 and parts[:2] == ['api', 'attempt']:
                    ident, action = parts[2:]
                    row = attempt(data, ident)
                    folder = data / 'attempts' / row['id']
                    if action == 'monitor':
                        return self.respond(monitor.snapshot(row, int(query.get('since', ['-1'])[0])))
                    if action in ('log', 'events', 'worker-log'):
                        filename = {'log': 'stdout.log', 'events': 'events.jsonl', 'worker-log': 'worker.log'}[action]
                        return self.respond(read_chunk(folder / filename, int(query.get('cursor', ['0'])[0]), lines=action == 'events'))
                    if action == 'export':
                        return self.respond(row, attachment=True)
                    if action == 'artifact':
                        target = artifact_path(data, ident, query.get('path', [''])[0])
                        # Avoid buffering weights/large output arrays into server RAM.
                        image = target.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.gif')
                        with target.open('rb') as stream:
                            self.send_response(200)
                            self.send_header('Content-Type', mimetypes.guess_type(target.name)[0] if image else 'application/octet-stream')
                            self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
                            self.send_header('X-Content-Type-Options', 'nosniff')
                            if not image: self.send_header('Content-Disposition', 'attachment')
                            self.end_headers()
                            while chunk := stream.read(65536): self.wfile.write(chunk)
                        return
                static = {'/': 'index.html', '/app.js': 'app.js', '/gpu.js': 'gpu.js', '/style.css': 'style.css'}
                if url.path in static:
                    file = ROOT / 'web' / static[url.path]
                    return self.respond(file.read_bytes(), mime={'/': 'text/html', '/app.js': 'text/javascript', '/gpu.js': 'text/javascript', '/style.css': 'text/css'}[url.path] + '; charset=utf-8')
                self.respond({'error': 'Not found'}, 404)
            except (ValueError, OSError, sqlite3.Error) as error:
                self.respond({'error': str(error)}, 400)

        def do_POST(self):
            origin = self.headers.get('Origin')
            if not self.valid_host() or self.headers.get('X-MLWB-Token') != token or (origin and origin != 'http://' + self.headers['Host']):
                return self.respond({'error': '请求来源或令牌无效'}, 403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size <= 0 or size > 262144 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('需要不超过 256 KiB 的 JSON 请求')
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict): raise ValueError('请求必须是对象')
                if self.path == '/api/gpu/start':
                    gpu.start(value.get('mode'), value.get('metric_set', ''))
                    return self.respond({'ok': True})
                if self.path == '/api/gpu/stop':
                    gpu.stop()
                    return self.respond({'ok': True})
                if self.path == '/api/view':
                    view_preferences(data, value)
                    return self.respond({'ok': True})
                if self.path == '/api/tasks':
                    return self.respond({'id': save_task(data, value)})
                if self.path == '/api/demo':
                    demo = dict(id='cpu-demo', name='CPU 流程演示', project='Workbench Demo', cwd=str(ROOT),
                        argv=[sys.executable, '-u', str(ROOT / 'examples' / 'demo.py')],
                        resume_argv=[sys.executable, '-u', str(ROOT / 'examples' / 'demo.py'), '--resume'],
                        kind='demo', cooperative_stop=True)
                    return self.respond({'id': save_task(data, demo)})
                if self.path == '/api/start':
                    return self.respond({'id': start(data, value.get('task_id'))})
                if self.path == '/api/resume':
                    return self.respond({'id': start(data, parent=value.get('id'))})
                if self.path == '/api/stop':
                    request_stop(data, value.get('id'), value.get('force') is True)
                    return self.respond({'ok': True})
                self.respond({'error': 'Not found'}, 404)
            except (ValueError, OSError, sqlite3.Error) as error:
                self.respond({'error': str(error)}, 400)

    class Server(ThreadingHTTPServer):
        def server_close(self):
            gpu.cancel.set()
            super().server_close()

    return Server(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--data', type=Path, default=ROOT / 'data')
    args = parser.parse_args()
    data = args.data.resolve(); initialize(data)
    try:
        lock = FileLock(data / 'server.lock')
    except OSError:
        parser.error('该数据目录已有工作台服务；请打开已有页面')
    try:
        server = make_server(data, args.port)
    except OSError as error:
        lock.close()
        parser.error(f'无法绑定本机端口：{error}；使用 --port 0 由系统分配可用端口')
    (data / 'server.json').write_text(json.dumps({'pid': os.getpid(), 'url': f'http://127.0.0.1:{server.server_port}', 'started': time.time()}), encoding='utf-8')
    print(f'ML Workbench: http://127.0.0.1:{server.server_port}\nData: {data}\nClosing this server does not stop workers.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close(); lock.close()


if __name__ == '__main__':
    main()
