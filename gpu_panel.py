"""Optional, machine-wide GPU observation; independent of the experiment worker."""
import csv
from datetime import datetime
from pathlib import Path
import os
import re
import subprocess
import sys
import threading
import time

import gpu_watch
from monitor import read_chunk

FILE = re.compile(r'(trend-gpu\d+-\d{8}-\d{6}-\d{6}\.csv|counters-gpu\d+-\d{8}-\d{6}-\d{6}\.(?:sqlite|nsys-rep))\Z')
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
DAYS = 14
COUNTER_BUDGET = 512 * 1024**2


class GpuPanel:
    def __init__(self, data):
        self.root = data.resolve() / 'gpu'
        self.managed = self.root / 'panel'
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.mode = ''
        self.message = ''
        self.last_clean = 0

    def files(self, folder):
        if folder.resolve() != folder:
            raise ValueError('GPU 记录目录不能重定向到其他位置')
        if folder.is_dir():
            for p in folder.iterdir():
                if FILE.fullmatch(p.name) and p.resolve() == p and p.is_file():
                    yield p

    def clean(self):
        now = time.time()
        if now - self.last_clean < 86400:
            return
        for p in self.files(self.managed):
            if p.stat().st_mtime < now - DAYS * 86400:
                p.unlink()
        self.last_clean = now

    def status(self):
        with self.lock:
            self.clean()
            items = []
            for scope, folder in [('panel', self.managed), ('local', self.root)]:
                for p in self.files(folder):
                    if p.suffix == '.nsys-rep':
                        continue
                    items.append(dict(id=scope+'/'+p.name, name=p.name, changed=p.stat().st_mtime))
            return dict(mode=self.mode, message=self.message, manual_dir=str(self.root),
                        records=sorted(items, key=lambda i:i['changed'], reverse=True))

    def read(self, key):
        scope, _, name = key.partition('/')
        folder = {'panel': self.managed, 'local': self.root}.get(scope)
        if folder is None or not FILE.fullmatch(name) or not name.endswith(('.csv', '.sqlite')):
            raise ValueError('未知 GPU 记录')
        p = folder / name
        if p.resolve() != p or not p.is_file():
            raise ValueError('GPU 记录不存在')
        if p.suffix == '.sqlite':
            return dict(kind='counters', **gpu_watch.report(p))
        # Read only the tail, even for an overnight CLI recording.
        with p.open(encoding='utf-8-sig') as f:
            header = next(csv.reader([f.readline()], skipinitialspace=True))
        tail = read_chunk(p, -1, 16384, lines=True)['text'].splitlines()
        rows = [r for r in csv.reader(tail, skipinitialspace=True) if len(r) == len(header) and r != header]
        return dict(kind='trend', columns=header, rows=rows[-30:])

    def start(self, mode, metric_set):
        if mode not in ('trend','counters') or metric_set not in ('', 'gb20x-top', 'gb20x'):
            raise ValueError('未知的 GPU 采集选项')
        with self.lock:
            if self.mode:
                raise ValueError('已有 GPU 采集正在进行')
            if self.managed.resolve() != self.managed:
                raise ValueError('GPU 记录目录不能重定向到其他位置')
            self.managed.mkdir(parents=True, exist_ok=True)
            self.clean()
            if mode == 'counters' and sum(p.stat().st_size for p in self.files(self.managed)) >= COUNTER_BUDGET:
                raise ValueError('面板记录已达 512 MiB，请归档后再开始硬件短窗')
            self.cancel.clear()
            self.mode, self.message = mode, '正在采集整卡趋势' if mode == 'trend' else '正在检查计数器权限并采集短窗'
            threading.Thread(target=self.run, args=(mode, metric_set), daemon=True).start()

    def stop(self):
        with self.lock:
            if self.mode != 'trend':
                raise ValueError('当前无可停止的趋势采集')
            self.cancel.set()

    def run(self, mode, metric_set):
        message = '采集异常结束'
        try:
            if mode == 'trend':
                self.trend()
            else:
                cmd = [sys.executable, '-X', 'utf8', str(Path(gpu_watch.__file__).resolve()), 'counters',
                       '--seconds', '10', '--out-dir', str(self.managed)]
                if metric_set:
                    cmd.extend(['--metric-set', metric_set])
                result = subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace',
                                        creationflags=HIDDEN, timeout=120)
                if result.returncode:
                    detail = (result.stdout + '\n' + result.stderr).strip()[-1800:]
                    if 'Insufficient privilege' in detail or 'ERR_NVGPUCTRPERM' in detail:
                        detail = '当前账户无硬件计数器权限。可在下方“权限与保留期限”复制独立采集命令。\n' + detail
                    raise ValueError(detail)
            message = '采集已结束，记录可在下方查看。'
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            message = '采集未完成：' + str(exc)
        finally:
            with self.lock:
                self.mode, self.message = '', message

    def trend(self):
        deadline = time.monotonic() + 1800
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        path = self.managed / f'trend-gpu0-{stamp}.csv'
        cmd = ['nvidia-smi', '-i', '0', '--query-gpu='+gpu_watch.FIELDS, '--format=csv']
        first = True
        while not self.cancel.is_set() and time.monotonic() < deadline:
            tick = time.monotonic()
            result = subprocess.run(cmd, capture_output=True, text=True, errors='replace', creationflags=HIDDEN, timeout=8)
            if result.returncode:
                raise ValueError(result.stderr.strip() or result.stdout.strip() or 'nvidia-smi 无法读取 GPU 0')
            rows = list(csv.reader(result.stdout.splitlines(), skipinitialspace=True))
            if len(rows) != 2 or len(rows[1]) != len(gpu_watch.FIELDS.split(',')) or not rows[0] or rows[0][0] != 'timestamp':
                raise ValueError('nvidia-smi 没有返回完整的整卡样本')
            with path.open('x' if first else 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerows(rows if first else rows[1:])
            first = False
            self.cancel.wait(max(0, min(tick + 5, deadline) - time.monotonic()))
