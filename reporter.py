"""Optional, standard-library-only reporting. Copy this module or add it to sys.path."""
import json
import math
import os
import time
from pathlib import Path


class Reporter:
    def __init__(self, interval=1.0):
        self.path = os.environ.get('MLWB_EVENTS_FILE')
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
        value = os.environ.get('MLWB_STOP_FILE')
        return bool(value and Path(value).exists())
