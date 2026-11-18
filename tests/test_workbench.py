import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workbench as wb


def wait_for(function, timeout=12):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = function()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError('Timed out')


def alive(pid):
    if os.name != 'nt':
        try: os.kill(pid, 0); return True
        except ProcessLookupError: return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle: return False
    result = kernel.WaitForSingleObject(handle, 0) == 258
    kernel.CloseHandle(handle)
    return result


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        qa = wb.ROOT / '.qa'; qa.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=qa)
        self.data = Path(self.temp.name).resolve()
        assert self.data.is_relative_to(qa.resolve())
        wb.initialize(self.data)
        self.server = wb.make_server(self.data, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.token = self.request('/api/state')['token']

    def tearDown(self):
        for row in wb.state(self.data)['attempts']:
            if row['status'] in wb.ACTIVE:
                wb.request_stop(self.data, row['id'], True)
                self.finished(row['id'])
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        # Workers write their terminal record just before releasing handles.
        time.sleep(0.25)
        self.temp.cleanup()

    def request(self, path, payload=None, **headers):
        raw = None if payload is None else json.dumps(payload).encode()
        defaults = {'Content-Type':'application/json', 'X-MLWB-Token':getattr(self, 'token', '')}
        defaults.update(headers)
        with urllib.request.urlopen(urllib.request.Request(self.url + path, data=raw, headers=defaults), timeout=5) as response:
            return json.load(response)

    def spec(self, argv=None):
        return dict(name='QA task', project='QA', cwd=str(wb.ROOT), kind='demo', cooperative_stop=True,
            argv=argv or [sys.executable, '-u', str(wb.ROOT/'examples/demo.py'), '--delay', '0.01'],
            resume_argv=[sys.executable, '-u', str(wb.ROOT/'examples/demo.py'), '--delay', '0.001', '--resume'])

    def finished(self, ident):
        return wait_for(lambda: (row if (row := wb.attempt(self.data, ident))['status'] not in wb.ACTIVE else None))

    def test_complete_events_artifacts_and_no_duplicate_start(self):
        task = self.request('/api/tasks', self.spec())['id']
        ident = self.request('/api/start', {'task_id':task})['id']
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request('/api/start', {'task_id':task})
        self.assertEqual(rejected.exception.code, 400)
        row = self.finished(ident); self.assertEqual(row['status'], 'completed')
        events = [json.loads(line) for line in (self.data/'attempts'/ident/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual({e['stage'] for e in events if e['type']=='progress'}, {'准备','训练循环 · 演示','评价 · 演示','批量推理 · 演示'})
        self.assertEqual(wb.artifact_path(self.data, ident, 'preview.png').read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        with self.assertRaises(ValueError): wb.artifact_path(self.data, ident, '../index.sqlite')
        (self.data/'work'/row['run_id']/'unreported.txt').write_text('private')
        with self.assertRaises(ValueError): wb.artifact_path(self.data, ident, 'unreported.txt')
        self.assertIn('四阶段演示完成', self.request(f'/api/attempt/{ident}/log')['text'])

    def test_cooperative_stop_resume_and_immutable_snapshot(self):
        task = wb.save_task(self.data, self.spec())
        ident = wb.start(self.data, task)
        checkpoint = self.data/'work'/ident/'checkpoint.json'
        wait_for(checkpoint.exists)
        self.request('/api/stop', {'id':ident})
        self.assertEqual(self.finished(ident)['status'], 'stopped')
        value = self.spec(); value.update(id=task, name='changed definition', resume_argv=[])
        wb.save_task(self.data, value)
        resumed = self.request('/api/resume', {'id':ident})['id']
        row = self.finished(resumed)
        self.assertEqual(row['status'], 'completed'); self.assertEqual(row['parent'], ident)
        self.assertEqual(row['run_id'], ident); self.assertEqual(row['spec']['name'], 'QA task')
        self.assertEqual(wb.attempt(self.data, ident)['status'], 'stopped')
        with self.assertRaises(ValueError): wb.start(self.data, parent=ident)

    def test_task_groups_persist_without_rewriting_execution(self):
        spec = dict(self.spec(), group='preflight')
        task = self.request('/api/tasks', spec)['id']
        ident = self.request('/api/start', {'task_id': task})['id']
        original = self.finished(ident)
        # Reclassifying a task must not change its completed attempt or logs.
        events = (self.data/'attempts'/ident/'events.jsonl').read_bytes()
        spec.update(id=task, group='formal')
        self.request('/api/tasks', spec)
        self.assertEqual(self.request('/api/state')['tasks'][0]['group'], 'formal')
        self.assertEqual(wb.attempt(self.data, ident), original)
        self.assertEqual((self.data/'attempts'/ident/'events.jsonl').read_bytes(), events)
        wb.initialize(self.data)
        self.assertEqual(wb.state(self.data)['tasks'][0]['group'], 'formal')
        del spec['group']
        self.request('/api/tasks', spec)
        self.assertEqual(wb.state(self.data)['tasks'][0]['group'], 'formal')
        # Legacy definitions still work, but an explicit empty value clears grouping.
        self.request('/api/tasks', dict(spec, group=''))
        self.assertEqual(wb.state(self.data)['tasks'][0]['group'], '')
        for invalid in ('unknown', None, []):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request('/api/tasks', dict(spec, group=invalid))
            self.assertEqual(rejected.exception.code, 400)
        legacy = self.request('/api/tasks', self.spec())['id']
        self.assertEqual(next(t for t in wb.state(self.data)['tasks'] if t['id'] == legacy)['group'], '')

    def test_failure_exit_and_launch_failure_are_visible(self):
        task = wb.save_task(self.data, self.spec([sys.executable,'-c','raise RuntimeError("QA failure")']))
        ident = wb.start(self.data, task); row = self.finished(ident)
        self.assertEqual(row['status'],'failed'); self.assertNotEqual(row['exit_code'],0)
        self.assertIn('QA failure', (self.data/'attempts'/ident/'stdout.log').read_text())
        # An executable path can exist but still not be a valid OS executable.
        fake = self.data/'not-a-program.exe'; fake.write_text('not executable')
        task = wb.save_task(self.data, self.spec([str(fake)]))
        row = self.finished(wb.start(self.data, task))
        self.assertEqual(row['status'],'failed'); self.assertTrue(row['error'])

    def test_view_preferences_survive_server_restart_and_are_isolated(self):
        self.assertEqual(self.request('/api/view'), dict(groups={}, task_id='', attempt_id=''))
        task = self.request('/api/tasks', self.spec())['id']
        original = wb.state(self.data)
        prefs = dict(groups={'["QA","formal"]': True, '["QA","preflight"]': False}, task_id=task, attempt_id='')
        self.request('/api/view', prefs)
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.server = wb.make_server(self.data, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.token = self.request('/api/state')['token']
        self.assertEqual(self.request('/api/view'), prefs)
        other = self.data/'other'; wb.initialize(other)
        self.assertEqual(wb.view_preferences(other), dict(groups={}, task_id='', attempt_id=''))
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request('/api/view', prefs, **{'X-MLWB-Token':'wrong'})
        self.assertEqual(rejected.exception.code, 403)
        for value in (dict(prefs, groups=[]), dict(prefs, groups={'bad':'true'}), dict(prefs, task_id=None), {}):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request('/api/view', value)
            self.assertEqual(rejected.exception.code, 400)
        self.assertEqual(self.request('/api/view'), prefs)
        after = wb.state(self.data)
        self.assertEqual(after['tasks'], original['tasks'])
        self.assertEqual(after['attempts'], original['attempts'])

    def test_source_and_request_boundaries(self):
        for headers in ({'X-MLWB-Token':'wrong'}, {'Origin':'https://untrusted.example'}, {'Host':'evil.example'}):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request('/api/demo', {}, **headers)
            self.assertEqual(rejected.exception.code,403)
        self.assertEqual(wb.state(self.data)['tasks'],[])
        with self.assertRaises(urllib.error.HTTPError): self.request('/api/tasks', self.spec(['python','train.py']))
        self.assertEqual(wb.state(self.data)['attempts'],[])

    def test_incremental_event_reader_preserves_partial_record(self):
        path=self.data/'events.jsonl'; path.write_bytes(b'{"type":')
        first=wb.read_chunk(path,lines=True);self.assertEqual(first['cursor'],0)
        with path.open('ab') as out: out.write(b'"checkpoint"}\n')
        second=wb.read_chunk(path,first['cursor'],lines=True)
        self.assertEqual(json.loads(second['text'])['type'],'checkpoint')
        self.assertEqual(wb.read_chunk(path,second['cursor'],lines=True)['text'],'')
        path.write_text('中文日志',encoding='utf-8')
        chunks=[];cursor=0
        while cursor<path.stat().st_size:
            chunk=wb.read_chunk(path,cursor,limit=4);chunks.append(chunk['text']);cursor=chunk['cursor']
        self.assertEqual(''.join(chunks),'中文日志')

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object behavior')
    def test_force_stop_and_worker_crash_kill_owned_descendants(self):
        code = 'import os,sys,subprocess,time,pathlib; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); pathlib.Path(os.environ["MLWB_WORK_DIR"],"child.pid").write_text(str(p.pid)); time.sleep(60)'
        for crash in (False, True):
            task=wb.save_task(self.data,self.spec([sys.executable,'-u','-c',code]))
            ident=wb.start(self.data,task)
            pidfile=self.data/'work'/ident/'child.pid';wait_for(pidfile.exists)
            grandchild=int(pidfile.read_text());child=wb.attempt(self.data,ident)['pid']
            self.assertTrue(alive(grandchild)); self.assertTrue(alive(child))
            if crash:
                owner=json.loads((self.data/'attempts'/ident/'owner.json').read_text())
                os.kill(owner['pid'],signal.SIGTERM)
                wait_for(lambda:not is_active(self.data,ident))
                self.assertEqual(wb.attempt(self.data,ident)['status'],'interrupted')
            else:
                self.request('/api/stop',{'id':ident,'force':True})
                self.assertEqual(self.finished(ident)['status'],'terminated')
            wait_for(lambda:not alive(child) and not alive(grandchild))

    def test_http_service_process_restart_does_not_stop_task(self):
        other=self.data/'service';other.mkdir()
        logfile=other/'service.log'
        def launch():
            with logfile.open('wb') as log:
                proc=subprocess.Popen([sys.executable,'-u',str(wb.ROOT/'workbench.py'),'--data',str(other),'--port','0'],stdout=log,stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            def ready():
                text=logfile.read_text()
                for line in text.splitlines():
                    if line.startswith('ML Workbench: '):return line.split(' ',2)[2]
                if proc.poll() is not None:raise AssertionError(text)
            return proc,wait_for(ready)
        process,url=launch();ident=None
        try:
            token=json.load(urllib.request.urlopen(url+'/api/state'))['token']
            spec=self.spec([sys.executable,'-u',str(wb.ROOT/'examples/demo.py'),'--delay','0.06'])
            def post(path,value):
                request=urllib.request.Request(url+path,data=json.dumps(value).encode(),headers={'Content-Type':'application/json','X-MLWB-Token':token})
                return json.load(urllib.request.urlopen(request))
            task=post('/api/tasks',spec)['id'];ident=post('/api/start',{'task_id':task})['id']
            wait_for(lambda:wb.attempt(other,ident)['status']=='running')
            pid=wb.attempt(other,ident)['pid']
            process.kill();process.wait();self.assertTrue(alive(pid))
            process,url=launch()
            snapshot=json.load(urllib.request.urlopen(url+'/api/state'))
            self.assertEqual(snapshot['attempts'][0]['status'],'running')
            wait_for(lambda:wb.attempt(other,ident)['status']=='completed')
        finally:
            if ident and wb.attempt(other,ident)['status'] in wb.ACTIVE:
                wb.request_stop(other,ident,True);wait_for(lambda:wb.attempt(other,ident)['status'] not in wb.ACTIVE)
            process.kill();process.wait()


def is_active(data,ident):
    wb.reconcile(data)
    return wb.attempt(data,ident)['status'] in wb.ACTIVE


if __name__=='__main__':unittest.main(verbosity=2)
