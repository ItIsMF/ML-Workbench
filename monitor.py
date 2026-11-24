"""Persistent, derived views of task output. Never modifies task logs or checkpoints."""
import codecs
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading


def read_chunk(path, cursor=0, limit=65536, lines=False):
    if not path.exists():
        return dict(text='', cursor=0, more=False, size=0)
    with open(path, 'rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        offset = max(0, size - limit) if cursor < 0 else min(cursor, size)
        stream.seek(offset)
        if cursor < 0 and offset:
            stream.readline()
        offset = stream.tell()
        raw = stream.read(limit)
        if lines and raw and not raw.endswith(b'\n'):
            end = raw.rfind(b'\n')
            if end >= 0:
                raw = raw[:end + 1]
            elif len(raw) < limit:
                raw = b''
            else:
                raise ValueError('事件超过读取上限，检查脚本输出')
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        text = decoder.decode(raw, final=False)
        cursor = offset + len(raw) - len(decoder.getstate()[0])
    return dict(text=text, cursor=cursor, more=cursor < size, size=size)


def empty_events():
    return dict(stages={}, metrics={}, artifacts={}, event_tail=[], last_event=0, errors=0, checkpoint='')


def consume(view, event):
    def finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not isinstance(event, dict) or not finite(event.get('time')):
        raise ValueError('Invalid event time')
    kind = event.get('type')
    if kind == 'progress':
        stage, done, total = event.get('stage'), event.get('done'), event.get('total')
        if not isinstance(stage, str) or not finite(done) or done < 0 or (total is not None and (not finite(total) or total < done)):
            raise ValueError('Invalid progress')
        if stage not in view['stages'] and len(view['stages']) >= 64:
            raise ValueError('Stage display limit')
        values = event.get('metrics', {})
        if not isinstance(values, dict):
            raise ValueError('Invalid metrics')
        view['stages'][stage] = dict(stage=stage, done=done, total=total, unit=str(event.get('unit', '')))
        count = str(done) if total is None else f'{done}/{total}'
        message = f"{stage} · {count} {view['stages'][stage]['unit']}".rstrip()
        for key, value in values.items():
            if isinstance(value, bool):
                value = int(value)  # A flag is a 0/1 chart series, never a timestamp or step count.
            if not finite(value) or (key not in view['metrics'] and len(view['metrics']) >= 64):
                view['errors'] += 1
                continue
            points = view['metrics'].setdefault(key, [])
            points.append(dict(t=event['time'], v=value))
            del points[:-1000]
    elif kind == 'artifact':
        path = event.get('path')
        if not isinstance(path, str) or (path not in view['artifacts'] and len(view['artifacts']) >= 100):
            raise ValueError('Invalid artifact')
        view['artifacts'][path] = dict(path=path, label=str(event.get('label', path)))
        message = '产物：' + path
    elif kind == 'checkpoint':
        view['checkpoint'] = str(event.get('path', ''))
        message = '检查点：' + view['checkpoint']
    else:
        raise ValueError('Unknown event type')
    view['event_tail'].append([event['time'], ' '.join(message.splitlines())[:240]])
    del view['event_tail'][:-32]
    view['last_event'] = event['time']


def fingerprint(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def boundary(path, cursor):
    if not cursor:
        return ''
    with path.open('rb') as stream:
        stream.seek(max(0, cursor - 64))
        return hashlib.sha256(stream.read(min(64, cursor))).hexdigest()


class Monitor:
    def __init__(self, data):
        self.data = Path(data)
        self.lock = threading.Lock()
        self.database = self.data / 'monitor.sqlite'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS views(
                project TEXT, task TEXT, attempt TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(project,task,attempt))''')

    def snapshot(self, row, since=-1):
        key = (row['spec']['project'], row['task_id'], row['id'])
        with self.lock, closing(sqlite3.connect(self.database)) as db, db:
            saved = db.execute('SELECT payload FROM views WHERE project=? AND task=? AND attempt=?', key).fetchone()
            cache = json.loads(saved[0]) if saved else dict(revision=0, files={}, view=dict(empty_events(), log='', worker_log=''))
            changed = not saved
            if cache.get('event_format', 1) < 3:
                view = cache['view']
                # Replay only when repairing old bool warnings or backfilling an event-only console.
                if (cache.get('event_format', 1) < 2 and view['errors']) or (view['last_event'] and not view['log']):
                    cache['files'].pop('events', None)
                view.setdefault('event_tail', [])
                cache['event_format'] = 3
                changed = True
            read_bytes = 0
            pending = False
            folder = Path(row['attempt_dir']) if row.get('attempt_dir') else (self.data / 'attempts' / row['id'])
            streams = (('events', 'events.jsonl'), ('log', 'stdout.log')) if row.get('external') else (('events', 'events.jsonl'), ('log', 'stdout.log'), ('worker_log', 'worker.log'))
            for name, filename in streams:
                path = folder / filename
                stamp = fingerprint(path)
                old = cache['files'].get(name)
                if old and stamp == old['stamp'] and not old['more']:
                    continue
                reset = old is None or stamp is None or old['stamp'] is None
                if not reset:
                    reset = stamp[:2] != old['stamp'][:2] or stamp[2] < old['cursor']
                    reset |= stamp != old['stamp'] and stamp[2] <= old['stamp'][2]
                    if not reset and stamp != old['stamp']:
                        reset = boundary(path, old['cursor']) != old['boundary']
                if reset:
                    if name == 'events': cache['view'].update(empty_events())
                    else: cache['view'][name] = ''
                cursor = (0 if name == 'events' else -1) if reset else old['cursor']
                chunk = read_chunk(path, cursor, limit=1024*1024 if name == 'events' else 65536, lines=name == 'events')
                read_bytes += max(0, chunk['cursor'] - max(0, cursor)) if cursor >= 0 else len(chunk['text'].encode('utf-8'))
                if name == 'events':
                    for line in chunk['text'].splitlines():
                        if line.strip():
                            try: consume(cache['view'], json.loads(line))
                            except (ValueError, TypeError, OverflowError): cache['view']['errors'] += 1
                else:
                    cache['view'][name] = (cache['view'][name] + chunk['text'])[-(49152 if name == 'log' else 16384):]
                # A partial final event waits for its writer, rather than spinning.
                more = chunk['more'] and bool(chunk['text'])
                pending |= more
                cache['files'][name] = dict(stamp=stamp, cursor=chunk['cursor'], boundary=boundary(path, chunk['cursor']), more=more)
                changed = True
            if changed:
                cache['revision'] += 1
                db.execute('INSERT OR REPLACE INTO views VALUES (?,?,?,?)', (*key, json.dumps(cache, ensure_ascii=False, allow_nan=False)))
            result = dict(revision=cache['revision'], more=pending, bytes_read=read_bytes, unchanged=since == cache['revision'])
            if not result['unchanged']:
                result['view'] = cache['view']
            return result
