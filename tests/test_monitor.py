from contextlib import closing
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor
import workbench as wb


def progress(step, stage='train'):
    return json.dumps(dict(type='progress', time=100 + step, stage=stage, done=step,
                           total=10000, metrics={'loss':step / 10000})) + '\n'


class MonitorTests(unittest.TestCase):
    def setUp(self):
        qa = wb.ROOT / '.qa'; qa.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=qa)
        self.data = Path(self.temp.name).resolve()
        assert self.data.is_relative_to(qa.resolve())
        wb.initialize(self.data)
        self.monitor = monitor.Monitor(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def row(self, ident='a', project='project', task='task'):
        folder = self.data/'attempts'/ident; folder.mkdir(exist_ok=True)
        return dict(id=ident, task_id=task, spec={'project':project})

    def events(self, row):
        return self.data/'attempts'/row['id']/'events.jsonl'

    def test_persistent_project_isolation_append_and_no_replay(self):
        rows = [self.row('a'), self.row('b', 'another-project', 'other-task'), self.row('c')]
        self.events(rows[0]).write_text(''.join(progress(i) for i in range(5000)),encoding='utf-8')
        for index,row in enumerate(rows[1:]): self.events(row).write_text(progress(index+20),encoding='utf-8')
        snapshots=[]
        for row in rows:
            while True:
                result=self.monitor.snapshot(row)
                if not result['more']:break
            snapshots.append(result)
        self.assertEqual(snapshots[0]['view']['stages']['train']['done'],4999)
        self.assertEqual(len(snapshots[0]['view']['metrics']['loss']),1000)
        self.assertEqual(snapshots[1]['view']['stages']['train']['done'],20)
        self.assertEqual(snapshots[2]['view']['stages']['train']['done'],21)
        # Simulate a new HTTP-service instance/browser port and A -> B -> A.
        restarted=monitor.Monitor(self.data)
        with patch.object(monitor,'read_chunk', wraps=monitor.read_chunk) as reader:
            for row in (rows[0],rows[1],rows[0],rows[2]):
                self.assertEqual(restarted.snapshot(row)['bytes_read'],0)
            reader.assert_not_called()
        delta=progress(5000)
        before_size=self.events(rows[0]).stat().st_size
        with self.events(rows[0]).open('a',encoding='utf-8') as stream:stream.write(delta)
        with patch.object(monitor,'read_chunk', wraps=monitor.read_chunk) as reader:
            updated=restarted.snapshot(rows[0],snapshots[0]['revision'])
            self.assertEqual(reader.call_count,1)
            self.assertGreater(reader.call_args.args[1],0)
        self.assertEqual(updated['bytes_read'],self.events(rows[0]).stat().st_size-before_size)
        self.assertEqual(updated['view']['stages']['train']['done'],5000)
        self.assertEqual(updated['view']['metrics']['loss'][-1]['v'],.5)
        self.assertTrue(restarted.snapshot(rows[0],updated['revision'])['unchanged'])

    def test_boolean_metrics_repair_old_view_without_losing_real_errors(self):
        row = self.row(); path = self.events(row)
        base = dict(type='progress', time=100, stage='train', done=1, total=3)
        records = [dict(base, metrics={'supported': True, 'loss': .25}),
                   dict(base, time=101, done=2, metrics={'supported': False, 'bad': 'oops'}),
                   dict(base, done=True), dict(base, time=False), dict(base, total=True)]
        path.write_text(''.join(json.dumps(r)+'\n' for r in records), encoding='utf-8')
        (path.parent/'stdout.log').write_text('log stays cached\n',encoding='utf-8')
        first = self.monitor.snapshot(row)
        with closing(sqlite3.connect(self.monitor.database)) as db, db:
            cache = json.loads(db.execute('SELECT payload FROM views').fetchone()[0])
            cache.pop('event_format'); cache['view']['metrics'].pop('supported'); cache['view']['errors'] = 6
            db.execute('UPDATE views SET payload=?',(json.dumps(cache),))
        repaired = monitor.Monitor(self.data).snapshot(row, first['revision'])
        self.assertFalse(repaired['unchanged']); self.assertEqual(repaired['bytes_read'],path.stat().st_size)
        v = repaired['view']; self.assertEqual(v['errors'],4); self.assertEqual(v['stages']['train']['done'],2)
        flags = [p['v'] for p in v['metrics']['supported']]; self.assertEqual(flags,[1,0]); self.assertTrue(all(type(x) is int for x in flags))
        self.assertEqual([p['v'] for p in v['metrics']['loss']],[.25]); self.assertEqual(v['log'],first['view']['log'])
        self.assertEqual(self.monitor.snapshot(row,repaired['revision'])['bytes_read'],0)

    def test_backfill_silent_attempt_event_tail_then_read_only_appends(self):
        row = self.row(); path = self.events(row)
        path.write_text(''.join(progress(i) for i in range(40)),encoding='utf-8')
        first = self.monitor.snapshot(row)
        with closing(sqlite3.connect(self.monitor.database)) as db, db:
            cache = json.loads(db.execute('SELECT payload FROM views').fetchone()[0])
            cache['event_format'] = 2; cache['view'].pop('event_tail')
            db.execute('UPDATE views SET payload=?',(json.dumps(cache),))
        restored = self.monitor.snapshot(row,first['revision']); self.assertEqual(restored['view']['log'],'')
        self.assertEqual(restored['view']['event_tail'],[[100+i,f'train · {i}/10000'] for i in range(8,40)])
        line = json.dumps(dict(type='checkpoint',time=141,path='latest.pt'))+'\n'
        with path.open('ab') as f: f.write(line.encode('utf8'))
        appended = self.monitor.snapshot(row,restored['revision'])
        self.assertEqual(appended['bytes_read'],len(line.encode('utf8')))
        self.assertEqual(appended['view']['event_tail'][-1],[141,'检查点：latest.pt'])
        self.assertTrue(self.monitor.snapshot(row,appended['revision'])['unchanged'])

    def test_partial_invalid_records_and_log_tails(self):
        row=self.row();path=self.events(row)
        path.write_bytes(progress(1).encode()[:-2])
        self.assertEqual(self.monitor.snapshot(row)['view']['errors'],0)
        with patch.object(monitor,'read_chunk', wraps=monitor.read_chunk) as reader:
            self.monitor.snapshot(row);reader.assert_not_called()
        with path.open('ab') as stream:stream.write(progress(1).encode()[-2:] + b'broken\n' + progress(2).encode())
        folder=path.parent
        (folder/'stdout.log').write_text('long log\n'*10000+'末尾\n',encoding='utf-8')
        (folder/'worker.log').write_text('诊断\n',encoding='utf-8')
        first=self.monitor.snapshot(row)
        self.assertEqual(first['view']['errors'],1)
        self.assertEqual(len(first['view']['metrics']['loss']),2)
        self.assertEqual(first['view']['log'].splitlines()[-1],'末尾')
        self.assertLessEqual(len(first['view']['log']),49152)
        with (folder/'stdout.log').open('a',encoding='utf-8') as stream:stream.write('新增中文\n')
        with (folder/'worker.log').open('a',encoding='utf-8') as stream:stream.write('新增诊断\n')
        after=self.monitor.snapshot(row)
        self.assertEqual(after['view']['log'].count('新增中文'),1)
        self.assertEqual(after['view']['worker_log'].splitlines(),['诊断','新增诊断'])
        self.assertEqual(after['view']['errors'],1)

    def test_truncate_replace_and_rewrite_rebuild_only_affected_stream(self):
        row=self.row();path=self.events(row)
        path.write_text(progress(8),encoding='utf-8');self.monitor.snapshot(row)
        path.write_text(progress(1,stage='new'),encoding='utf-8')
        result=self.monitor.snapshot(row)
        self.assertEqual(list(result['view']['stages']),['new'])
        replacement=path.with_suffix('.replacement');replacement.write_text(progress(10,'replacement')*3,encoding='utf-8');replacement.replace(path)
        result=self.monitor.snapshot(row)
        self.assertEqual(list(result['view']['stages']),['replacement'])
        path.write_text(''.join(progress(i,'rewrite') for i in range(20)),encoding='utf-8')
        result=self.monitor.snapshot(row)
        self.assertEqual(list(result['view']['stages']),['rewrite'])
        self.assertEqual(len(result['view']['metrics']['loss']),20)
        log=path.parent/'stdout.log';log.write_text('old log',encoding='utf-8');self.monitor.snapshot(row)
        log.write_text('new',encoding='utf-8');result=self.monitor.snapshot(row)
        self.assertEqual(result['view']['log'],'new')
        self.assertEqual(len(result['view']['metrics']['loss']),20)

    def test_http_monitor_restart_and_concurrent_readers(self):
        row=self.row()
        with wb.connect(self.data) as db:
            db.execute('INSERT INTO attempts(id,run_id,task_id,spec,status,created) VALUES (?,?,?,?,?,?)',
                       ('a','a','task',json.dumps(row['spec']),'completed',100))
        self.events(row).write_text(progress(5),encoding='utf-8')
        def request_cycle(since):
            server=wb.make_server(self.data,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/attempt/a/monitor?since={since}') as response:
                    return json.load(response)
            finally:server.shutdown();server.server_close();thread.join()
        first=request_cycle(-1)
        second=request_cycle(-1)
        self.assertEqual(first['view'],second['view']);self.assertEqual(second['bytes_read'],0)
        self.assertTrue(request_cycle(second['revision'])['unchanged'])
        with self.events(row).open('a',encoding='utf-8') as stream:stream.write(progress(6))
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(lambda _:self.monitor.snapshot(row),range(3)))
        self.assertEqual(sum(r['bytes_read']>0 for r in results),1)
        self.assertTrue(all(len(r['view']['metrics']['loss'])==2 for r in results))


if __name__=='__main__':unittest.main(verbosity=2)
