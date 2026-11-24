"""Local, single-user ML task workbench. No third-party dependencies."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
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
SAFE_ID = re.compile(r'^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
EXTERNAL_STATUSES = ACTIVE + ('completed', 'failed', 'stopped', 'terminated', 'interrupted')
VALID_KINDS = ('experiment', 'training', 'inference', 'demo')
VALID_GROUPS = ('', 'preflight', 'formal')


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
    if spec['kind'] not in VALID_KINDS:
        raise ValueError('未知任务类型')
    spec['group'] = value.get('group', '')
    if spec['group'] not in VALID_GROUPS:
        raise ValueError('分组必须是前检、正式实验或未分组')
    spec['cooperative_stop'] = bool(value.get('cooperative_stop', False))
    artifact_root = value.get('artifact_root', '')
    if not isinstance(artifact_root, str):
        raise ValueError('产物目录必须是字符串')
    if artifact_root and (not Path(artifact_root).is_absolute() or not Path(artifact_root).is_dir()):
        raise ValueError('自定义产物根必须是现有绝对目录，留空使用运行工作区')
    spec['artifact_root'] = str(Path(artifact_root).resolve()) if artifact_root else ''
    return spec


def _finite_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def parse_watch_dirs(values):
    seen = set()
    result = []
    for raw in values or ():
        text = str(raw).strip()
        if not text:
            continue
        resolved = Path(text).expanduser().resolve()
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return tuple(result)


def scan_external(watch_dirs, heartbeat_timeout=180.0):
    ext_attempts = []
    seen_attempt_ids = set()
    now = time.time()
    for idx, wdir in enumerate(watch_dirs):
        if not wdir.is_dir():
            continue
        try:
            subdirs = sorted((p for p in wdir.iterdir() if p.is_dir()), key=lambda p: p.name)
        except OSError:
            continue
        for sub in subdirs:
            if not SAFE_ID.fullmatch(sub.name):
                continue
            meta_file = sub / 'meta.json'
            if not meta_file.is_file():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding='utf-8'))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(meta, dict):
                continue
            project = meta.get('project')
            name = meta.get('name')
            raw_status = meta.get('status')
            if (not isinstance(project, str) or not project.strip()
                    or not isinstance(name, str) or not name.strip()
                    or raw_status not in EXTERNAL_STATUSES):
                continue
            project = project.strip()
            name = name.strip()
            kind = meta.get('kind', 'experiment')
            if kind not in VALID_KINDS:
                kind = 'experiment'
            group = meta.get('group', '')
            if group not in VALID_GROUPS:
                group = ''
            created = _finite_number(meta.get('created'))
            if created is None:
                try:
                    created = meta_file.stat().st_mtime
                except OSError:
                    created = now
            started = _finite_number(meta.get('started'))
            ended = _finite_number(meta.get('ended'))
            hb = _finite_number(meta.get('heartbeat'))
            exit_code = meta.get('exit_code')
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                exit_code = None
            error = str(meta.get('error') or '')
            cooperative_stop = bool(meta.get('cooperative_stop', False))

            attempt_id = f'ext_{sub.name}'
            if attempt_id in seen_attempt_ids:
                attempt_id = f'ext_{idx}_{sub.name}'
            seen_attempt_ids.add(attempt_id)

            raw_tid = meta.get('task_id')
            if isinstance(raw_tid, str) and SAFE_ID.fullmatch(raw_tid):
                task_id = f'ext_{raw_tid}'
            else:
                task_id = f"ext_{hashlib.sha1(f'{project}\\0{name}'.encode('utf-8')).hexdigest()[:16]}"

            if raw_status in ACTIVE:
                if hb is not None and (now - hb) > heartbeat_timeout:
                    derived_status = 'lost'
                elif (sub / 'stop.request').exists():
                    derived_status = 'stopping'
                else:
                    derived_status = raw_status
            else:
                derived_status = raw_status

            runtime = meta.get('runtime')
            if not isinstance(runtime, dict):
                runtime = {}
            source_label = str(meta.get('source_label') or runtime.get('platform') or '外部').strip() or '外部'

            cmd = meta.get('command', [])
            if isinstance(cmd, list) and all(isinstance(a, str) for a in cmd):
                argv = cmd
            elif isinstance(cmd, str) and cmd.strip():
                argv = [cmd]
            else:
                argv = []
            cwd = meta.get('cwd') if isinstance(meta.get('cwd'), str) and meta.get('cwd').strip() else str(sub)
            artifacts_root = str((sub / 'artifacts').resolve())
            spec = {
                'name': name,
                'project': project,
                'cwd': cwd,
                'argv': argv,
                'resume_argv': [],
                'kind': kind,
                'group': group,
                'cooperative_stop': cooperative_stop,
                'artifact_root': artifacts_root,
            }
            ext_attempts.append({
                'id': attempt_id,
                'run_id': str(meta.get('run_id') or attempt_id),
                'parent': meta.get('parent') if isinstance(meta.get('parent'), str) else None,
                'task_id': task_id,
                'spec': spec,
                'status': derived_status,
                'raw_status': raw_status,
                'created': created,
                'started': started,
                'ended': ended,
                'exit_code': exit_code,
                'error': error,
                'pid': None,
                'external': True,
                'attempt_dir': str(sub.resolve()),
                'watch_dir': str(wdir),
                'heartbeat': hb,
                'heartbeat_timeout': heartbeat_timeout,
                'runtime': runtime,
                'source_label': source_label,
            })

    ext_attempts.sort(key=lambda r: r['created'], reverse=True)
    tasks_by_id = {}
    for row in ext_attempts:
        tid = row['task_id']
        if tid not in tasks_by_id:
            tasks_by_id[tid] = {
                'id': tid,
                **row['spec'],
                'external': True,
                'source_label': row['source_label'],
                'watch_dir': row['watch_dir'],
            }
    return list(tasks_by_id.values()), ext_attempts


def attempt(data, ident):
    with connect(data) as db:
        row = db.execute('SELECT * FROM attempts WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError('执行记录不存在')
    result = dict(row)
    result['spec'] = json.loads(result['spec'])
    return result


def resolve_attempt(data, ident, watch_dirs=(), heartbeat_timeout=180.0):
    if isinstance(ident, str) and ident.startswith('ext_'):
        _, ext_attempts = scan_external(watch_dirs, heartbeat_timeout)
        for row in ext_attempts:
            if row['id'] == ident:
                return row
        raise ValueError('执行记录不存在')
    return attempt(data, ident)


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


def state(data, watch_dirs=(), heartbeat_timeout=180.0):
    reconcile(data)
    with connect(data) as db:
        tasks = [dict(id=row['id'], **json.loads(row['spec'])) for row in db.execute('SELECT * FROM tasks ORDER BY rowid DESC')]
        rows = [dict(row) for row in db.execute('SELECT * FROM attempts ORDER BY created DESC LIMIT 100')]
    for row in rows:
        row['spec'] = json.loads(row['spec'])
    ext_tasks, ext_attempts = scan_external(watch_dirs, heartbeat_timeout)
    all_attempts = sorted(rows + ext_attempts, key=lambda r: r['created'], reverse=True)[:100]
    return dict(tasks=tasks + ext_tasks, attempts=all_attempts, server_time=time.time(), watch_dirs=[str(p) for p in watch_dirs])


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
    if (isinstance(task_id, str) and task_id.startswith('ext_')) or (isinstance(parent, str) and parent.startswith('ext_')):
        raise ValueError('外部/云端运行请在远端环境启动或恢复')
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


def request_stop(data, ident, force=False, watch_dirs=(), heartbeat_timeout=180.0):
    if isinstance(ident, str) and ident.startswith('ext_'):
        if force:
            raise ValueError('外部/云端运行不支持强制终止，请使用协作安全停止或在远端环境停止')
        row = resolve_attempt(data, ident, watch_dirs, heartbeat_timeout)
        if row.get('raw_status') not in ACTIVE:
            raise ValueError('该执行已经结束')
        if not row['spec'].get('cooperative_stop'):
            raise ValueError('脚本未声明协作停止能力，请在远端环境停止')
        (Path(row['attempt_dir']) / 'stop.request').write_text(
            json.dumps({'requested_at': time.time(), 'source': 'ml-workbench'}, ensure_ascii=False),
            encoding='utf-8',
        )
        return
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


def artifact_path(data, ident, name, watch_dirs=(), heartbeat_timeout=180.0):
    row = resolve_attempt(data, ident, watch_dirs, heartbeat_timeout)
    if row.get('external'):
        attempt_dir = Path(row['attempt_dir']).resolve()
        root = (attempt_dir / 'artifacts').resolve()
        events = attempt_dir / 'events.jsonl'
    else:
        root = Path(row['spec']['artifact_root'] or data / 'work' / row['run_id']).resolve()
        events = data / 'attempts' / ident / 'events.jsonl'
    target = (root / name).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError('产物不存在或超出登记根目录')
    # Only paths explicitly reported by this attempt may be downloaded.
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


def make_server(data, port=8765, watch_dirs=(), heartbeat_timeout=180.0):
    initialize(data)
    watch_dirs = parse_watch_dirs(watch_dirs)
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
                    return self.respond(dict(state(data, watch_dirs, heartbeat_timeout), token=token))
                if url.path == '/api/gpu':
                    return self.respond(gpu.status())
                if url.path == '/api/gpu/record':
                    return self.respond(gpu.read(query.get('id', [''])[0]))
                if url.path == '/api/view':
                    return self.respond(view_preferences(data))
                if len(parts) == 4 and parts[:2] == ['api', 'attempt']:
                    ident, action = parts[2:]
                    row = resolve_attempt(data, ident, watch_dirs, heartbeat_timeout)
                    folder = Path(row['attempt_dir']) if row.get('external') else (data / 'attempts' / row['id'])
                    if action == 'monitor':
                        return self.respond(monitor.snapshot(row, int(query.get('since', ['-1'])[0])))
                    if action in ('log', 'events', 'worker-log'):
                        filename = {'log': 'stdout.log', 'events': 'events.jsonl', 'worker-log': 'worker.log'}[action]
                        return self.respond(read_chunk(folder / filename, int(query.get('cursor', ['0'])[0]), lines=action == 'events'))
                    if action == 'export':
                        return self.respond(row, attachment=True)
                    if action == 'artifact':
                        target = artifact_path(data, ident, query.get('path', [''])[0], watch_dirs, heartbeat_timeout)
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
                    request_stop(data, value.get('id'), value.get('force') is True, watch_dirs, heartbeat_timeout)
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
    parser.add_argument('--watch-dir', action='append', type=Path, default=[], help='外部/云端运行同步观测目录（可多次指定）')
    parser.add_argument('--heartbeat-timeout', type=float, default=180.0, help='外部运行心跳超时判定秒数')
    args = parser.parse_args()
    data = args.data.resolve(); initialize(data)
    raw_watch = args.watch_dir or [p for p in os.environ.get('MLWB_WATCH_DIR', '').split(os.pathsep) if p.strip()]
    watch_dirs = parse_watch_dirs(raw_watch)
    try:
        lock = FileLock(data / 'server.lock')
    except OSError:
        parser.error('该数据目录已有工作台服务；请打开已有页面')
    try:
        server = make_server(data, args.port, watch_dirs=watch_dirs, heartbeat_timeout=args.heartbeat_timeout)
    except OSError as error:
        lock.close()
        parser.error(f'无法绑定本机端口：{error}；使用 --port 0 由系统分配可用端口')
    (data / 'server.json').write_text(json.dumps({'pid': os.getpid(), 'url': f'http://127.0.0.1:{server.server_port}', 'started': time.time(), 'watch_dirs': [str(p) for p in watch_dirs]}), encoding='utf-8')
    watch_line = f"\nWatch dirs: {', '.join(str(p) for p in watch_dirs)}" if watch_dirs else ''
    print(f'ML Workbench: http://127.0.0.1:{server.server_port}\nData: {data}{watch_line}\nClosing this server does not stop workers.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close(); lock.close()


if __name__ == '__main__':
    main()
