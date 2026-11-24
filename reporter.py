"""Optional, standard-library-only reporting. Copy this module or add it to sys.path."""
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import time

_SAFE_ID = re.compile(r'^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')


class Reporter:
    def __init__(self, interval=1.0, events_file=None, stop_file=None, on_emit=None):
        self.path = str(events_file) if events_file is not None else os.environ.get('MLWB_EVENTS_FILE')
        self.stop_path = str(stop_file) if stop_file is not None else None
        self.on_emit = on_emit
        self.interval = interval
        self.last = 0.0
        self.stage = None

    def emit(self, event):
        if not self.path:
            return
        line = json.dumps(dict(event, time=time.time()), ensure_ascii=False, allow_nan=False) + '\n'
        if len(line.encode('utf-8')) > 32768:
            raise ValueError('Workbench event exceeds 32 KiB')
        with open(self.path, 'a', encoding='utf-8') as stream:
            stream.write(line)
        if self.on_emit is not None:
            self.on_emit()

    def progress(self, stage, done, total=None, metrics=None, unit='步', force=False):
        now = time.monotonic()
        if not force and stage == self.stage and done != total and now - self.last < self.interval:
            return
        if not math.isfinite(done) or done < 0 or (total is not None and (not math.isfinite(total) or total < done)):
            raise ValueError('Invalid progress')
        self.emit(dict(type='progress', stage=stage, done=done, total=total, unit=unit, metrics=metrics or {}))
        self.stage, self.last = stage, now

    def artifact(self, path, label):
        """Path relative to the task's artifact root (default: MLWB_WORK_DIR)."""
        self.emit(dict(type='artifact', path=str(path), label=label))

    def checkpoint(self, path):
        self.emit(dict(type='checkpoint', path=str(path)))

    def stop_requested(self):
        value = self.stop_path if self.stop_path is not None else os.environ.get('MLWB_STOP_FILE')
        return bool(value and Path(value).exists())


class ExternalRun:
    """4-file external/cloud run protocol writer (`meta.json`, `events.jsonl`, `stdout.log`, `stop.request`)."""

    def __init__(self, watch_dir, *, project, name, attempt_id=None, task_id=None, kind='experiment',
                 group='', cooperative_stop=True, command=None, cwd=None, source_label='',
                 runtime=None, heartbeat_interval=30.0):
        ident = attempt_id or f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        if not _SAFE_ID.fullmatch(ident):
            raise ValueError(f'Invalid external attempt_id: {ident!r}')
        self.watch_dir = Path(watch_dir)
        self.attempt_id = ident
        self.dir = self.watch_dir / ident
        self.meta_file = self.dir / 'meta.json'
        self.events_file = self.dir / 'events.jsonl'
        self.stop_file = self.dir / 'stop.request'
        self.log_file = self.dir / 'stdout.log'
        self.artifacts_dir = self.dir / 'artifacts'
        self.project = str(project)
        self.name = str(name)
        self.task_id = str(task_id) if task_id else ''
        self.kind = kind
        self.group = group
        self.cooperative_stop = bool(cooperative_stop)
        if command is None:
            self.command = list(sys.argv)
        elif isinstance(command, str):
            self.command = [command]
        else:
            self.command = list(command)
        self.cwd = str(cwd or os.getcwd())
        self.source_label = str(source_label)
        self.runtime = dict(runtime) if isinstance(runtime, dict) else {}
        self.heartbeat_interval = float(heartbeat_interval)
        self.created = time.time()
        self.started = None
        self.ended = None
        self.status = 'starting'
        self.exit_code = None
        self.error = ''
        self._last_hb = 0.0
        self._last_hb_mono = float('-inf')
        self.reporter = Reporter(events_file=self.events_file, stop_file=self.stop_file,
                                 on_emit=lambda: self.heartbeat(force=False))

    def _write_meta(self):
        payload = {
            'version': 1,
            'project': self.project,
            'name': self.name,
            'attempt_id': self.attempt_id,
            'status': self.status,
            'created': self.created,
            'started': self.started,
            'ended': self.ended,
            'exit_code': self.exit_code,
            'error': self.error,
            'heartbeat': self._last_hb or self.created,
            'cooperative_stop': self.cooperative_stop,
            'kind': self.kind,
            'group': self.group,
            'command': self.command,
            'cwd': self.cwd,
            'source_label': self.source_label,
            'runtime': self.runtime,
        }
        if self.task_id:
            payload['task_id'] = self.task_id
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = self.dir / f'meta.json.tmp.{os.getpid()}'
        try:
            tmp.write_text(text, encoding='utf-8')
            os.replace(tmp, self.meta_file)
        except OSError:
            self.meta_file.write_text(text, encoding='utf-8')
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        self.started = now
        self._last_hb = now
        self._last_hb_mono = time.monotonic()
        self.status = 'running'
        self._write_meta()
        return self

    def heartbeat(self, force=False, runtime_updates=None):
        if isinstance(runtime_updates, dict):
            self.runtime.update(runtime_updates)
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_hb_mono) < self.heartbeat_interval:
            return False
        self._last_hb = time.time()
        self._last_hb_mono = now_mono
        self._write_meta()
        return True

    def finish(self, status='completed', exit_code=0, error=None):
        now = time.time()
        self.ended = now
        self._last_hb = now
        self._last_hb_mono = time.monotonic()
        self.status = status
        self.exit_code = exit_code
        if error is not None:
            self.error = str(error)
        self._write_meta()

    def env(self):
        return {
            'MLWB_EVENTS_FILE': str(self.events_file),
            'MLWB_STOP_FILE': str(self.stop_file),
            'MLWB_RUN_DIR': str(self.dir),
            'MLWB_WORK_DIR': str(self.artifacts_dir),
        }

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, _tb):
        if exc_type is None:
            self.finish(status='completed', exit_code=0)
        elif issubclass(exc_type, SystemExit):
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            if code == 75 and self.reporter.stop_requested():
                self.finish(status='stopped', exit_code=75)
            elif code == 0:
                self.finish(status='completed', exit_code=0)
            else:
                self.finish(status='failed', exit_code=code, error=f'SystemExit({exc.code!r})')
        else:
            self.finish(status='failed', exit_code=1, error=repr(exc))
        return False
