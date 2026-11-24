import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reporter import ExternalRun
import workbench as wb


def wait_for(function, timeout=12):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = function()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError('Timed out')


class ExternalWatchTests(unittest.TestCase):
    def setUp(self):
        qa = wb.ROOT / '.qa'; qa.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=qa)
        root = Path(self.temp.name).resolve()
        assert root.is_relative_to(qa.resolve())
        self.data = root / 'data'
        self.watch = root / 'sync_watch'
        self.watch.mkdir()
        self.server = wb.make_server(self.data, 0, watch_dirs=[self.watch], heartbeat_timeout=180.0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.token = self.request('/api/state')['token']

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        time.sleep(0.1)
        self.temp.cleanup()

    def request(self, path, payload=None):
        raw = None if payload is None else json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json', 'X-MLWB-Token': getattr(self, 'token', '')}
        with urllib.request.urlopen(urllib.request.Request(self.url + path, data=raw, headers=headers), timeout=5) as response:
            ctype = response.headers.get('Content-Type', '')
            return json.load(response) if 'application/json' in ctype else response.read()

    def test_external_lifecycle_monitor_artifacts_stop_and_action_boundaries(self):
        run = ExternalRun(
            self.watch,
            project='CloudProj',
            name='Cloud Exp',
            attempt_id='cloud_run_01',
            task_id='cloud_task_1',
            kind='training',
            group='formal',
            command=['python', '-u', 'train.py'],
            cwd='/content/repo',
            source_label='Colab T4',
            runtime={'platform': 'colab', 'gpu': 'Tesla T4'},
            heartbeat_interval=30.0,
        )
        with self.assertRaises(SystemExit) as cm:
            with run:
                snap = self.request('/api/state')
                self.assertEqual(snap['watch_dirs'], [str(self.watch)])
                ext_task = next(t for t in snap['tasks'] if t['id'] == 'ext_cloud_task_1')
                self.assertTrue(ext_task['external'])
                self.assertEqual(ext_task['source_label'], 'Colab T4')
                ext_row = next(a for a in snap['attempts'] if a['id'] == 'ext_cloud_run_01')
                self.assertEqual(ext_row['status'], 'running')
                self.assertEqual(ext_row['runtime']['gpu'], 'Tesla T4')

                run.log_file.write_bytes(b'epoch 1\n')
                run.reporter.progress('训练', 1, 10, {'loss': 0.5}, force=True)
                png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
                (run.artifacts_dir / 'preview.png').write_bytes(png)
                (run.artifacts_dir / 'secret.txt').write_text('hidden', encoding='utf-8')
                run.reporter.artifact('preview.png', '预测预览')

                first = self.request('/api/attempt/ext_cloud_run_01/monitor?since=-1')
                self.assertGreater(first['bytes_read'], 0)
                self.assertEqual(first['view']['stages']['训练']['done'], 1)
                self.assertEqual(first['view']['artifacts']['preview.png']['label'], '预测预览')
                self.assertEqual(first['view']['log'], 'epoch 1\n')
                self.assertEqual(first['view']['worker_log'], '')

                cached = self.request(f"/api/attempt/ext_cloud_run_01/monitor?since={first['revision']}")
                self.assertTrue(cached['unchanged'])
                self.assertEqual(cached['bytes_read'], 0)

                with run.log_file.open('ab') as f:
                    f.write(b'epoch 2\n')
                run.reporter.progress('训练', 2, 10, {'loss': 0.25}, force=True)
                delta = self.request(f"/api/attempt/ext_cloud_run_01/monitor?since={first['revision']}")
                self.assertFalse(delta['unchanged'])
                self.assertGreater(delta['bytes_read'], 0)
                self.assertEqual([p['v'] for p in delta['view']['metrics']['loss']], [0.5, 0.25])
                self.assertIn('epoch 2', delta['view']['log'])

                self.assertEqual(self.request('/api/attempt/ext_cloud_run_01/artifact?path=preview.png'), png)
                for bad in ('../meta.json', 'secret.txt'):
                    with self.assertRaises(urllib.error.HTTPError) as err:
                        self.request(f'/api/attempt/ext_cloud_run_01/artifact?path={bad}')
                    self.assertEqual(err.exception.code, 400)

                for endpoint, payload in (
                    ('/api/start', {'task_id': 'ext_cloud_task_1'}),
                    ('/api/stop', {'id': 'ext_cloud_run_01', 'force': True}),
                ):
                    with self.assertRaises(urllib.error.HTTPError) as err:
                        self.request(endpoint, payload)
                    self.assertEqual(err.exception.code, 400)
                self.assertFalse(run.stop_file.exists())

                self.request('/api/stop', {'id': 'ext_cloud_run_01'})
                self.assertTrue(run.stop_file.exists())
                self.assertTrue(run.reporter.stop_requested())
                self.assertEqual(next(a for a in self.request('/api/state')['attempts'] if a['id'] == 'ext_cloud_run_01')['status'], 'stopping')
                raise SystemExit(75)
        self.assertEqual(cm.exception.code, 75)

        final_row = next(a for a in self.request('/api/state')['attempts'] if a['id'] == 'ext_cloud_run_01')
        self.assertEqual(final_row['status'], 'stopped')
        self.assertEqual(final_row['exit_code'], 75)
        for endpoint, payload in (
            ('/api/resume', {'id': 'ext_cloud_run_01'}),
            ('/api/stop', {'id': 'ext_cloud_run_01'}),
        ):
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request(endpoint, payload)
            self.assertEqual(err.exception.code, 400)

    def test_external_run_does_not_block_local_single_active_slot(self):
        run = ExternalRun(self.watch, project='Cloud', name='Remote', attempt_id='remote_active', heartbeat_interval=30.0).start()
        try:
            demo = dict(
                name='Local Demo', project='Local', cwd=str(wb.ROOT), kind='demo', cooperative_stop=True,
                argv=[sys.executable, '-u', str(wb.ROOT / 'examples/demo.py'), '--delay', '0.005'],
                resume_argv=[],
            )
            local_task = self.request('/api/tasks', demo)['id']
            local_attempt = self.request('/api/start', {'task_id': local_task})['id']
            done_row = wait_for(lambda: (r if (r := wb.attempt(self.data, local_attempt))['status'] not in wb.ACTIVE else None))
            self.assertEqual(done_row['status'], 'completed')
            with wb.connect(self.data) as db:
                sql_ids = [r['id'] for r in db.execute('SELECT id FROM attempts').fetchall()]
            self.assertEqual(sql_ids, [local_attempt])
        finally:
            run.finish(status='completed', exit_code=0)

    def test_heartbeat_timeout_recovery_and_corrupt_meta_resilience(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.server = wb.make_server(self.data, 0, watch_dirs=[self.watch], heartbeat_timeout=0.15)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.token = self.request('/api/state')['token']

        bad_dir = self.watch / 'bad..name'; bad_dir.mkdir()
        (bad_dir / 'meta.json').write_text('{"project":"x","name":"y","status":"running"}', encoding='utf-8')
        corrupt = self.watch / 'corrupt_run'; corrupt.mkdir()
        (corrupt / 'meta.json').write_text('{"project":"x",', encoding='utf-8')
        bogus = self.watch / 'bad_status'; bogus.mkdir()
        (bogus / 'meta.json').write_text('{"project":"x","name":"y","status":"bogus"}', encoding='utf-8')

        run = ExternalRun(self.watch, project='Cloud', name='HB', attempt_id='hb_run', heartbeat_interval=30.0).start()
        self.assertFalse(run.heartbeat(force=False))
        time.sleep(0.25)
        rows = self.request('/api/state')['attempts']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'lost')

        self.assertTrue(run.heartbeat(force=True, runtime_updates={'step': 42}))
        rows = self.request('/api/state')['attempts']
        self.assertEqual(rows[0]['status'], 'running')
        self.assertEqual(rows[0]['runtime']['step'], 42)

    def test_external_free_group_and_archive_rules(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.server = wb.make_server(self.data, 0, watch_dirs=[self.watch], heartbeat_timeout=0.15)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.token = self.request('/api/state')['token']

        live = ExternalRun(
            self.watch, project='Cloud / Sub', name='Live Free', attempt_id='ext_free_live',
            task_id='free_live_task', group='  阶段一 · 探索  ', heartbeat_interval=30.0,
        ).start()
        snap = self.request('/api/state')
        live_task = next(t for t in snap['tasks'] if t['id'] == 'ext_free_live_task')
        self.assertEqual(live_task['group'], '阶段一 · 探索')
        self.assertGreater(live_task['last_activity'], 0)
        self.assertFalse(live_task['archived'])

        # Active external task must reject archiving while live.
        with self.assertRaises(urllib.error.HTTPError) as active_err:
            self.request('/api/archive', {'task_ids': ['ext_free_live_task'], 'archived': True})
        self.assertEqual(active_err.exception.code, 400)

        # Once timed out ('lost'), archiving must be allowed so stale lost runs can be cleared away.
        time.sleep(0.25)
        self.assertEqual(next(a for a in self.request('/api/state')['attempts'] if a['task_id'] == 'ext_free_live_task')['status'], 'lost')
        self.request('/api/archive', {'task_ids': ['ext_free_live_task'], 'archived': True})
        self.assertTrue(next(t for t in self.request('/api/state')['tasks'] if t['id'] == 'ext_free_live_task')['archived'])

        live.finish(status='completed', exit_code=0)
        self.request('/api/archive', {'task_ids': ['ext_free_live_task'], 'archived': False})
        self.assertFalse(next(t for t in self.request('/api/state')['tasks'] if t['id'] == 'ext_free_live_task')['archived'])

    def test_superseded_lost_external_attempts_become_interrupted_and_freeze_duration(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.server = wb.make_server(self.data, 0, watch_dirs=[self.watch], heartbeat_timeout=0.15)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.token = self.request('/api/state')['token']

        # Two older interrupted/running attempts (one with heartbeat=None, one with heartbeat) + one newer completed attempt.
        now = time.time()
        for idx, (aid, hb, created_at, status) in enumerate((
            ('chain_1', now - 10.0, now - 12.0, 'running'),
            ('chain_2', None, now - 8.0, 'running'),
            ('chain_3', now - 1.0, now - 4.0, 'completed'),
        )):
            folder = self.watch / aid
            folder.mkdir(parents=True, exist_ok=True)
            meta = {
                'version': 1, 'project': 'GT', 'name': 'Chain', 'task_id': 'chain_task',
                'attempt_id': aid, 'status': status, 'created': created_at, 'started': created_at,
                'ended': now - 1.0 if status == 'completed' else None,
                'exit_code': 0 if status == 'completed' else None,
                'heartbeat': hb, 'cooperative_stop': True,
            }
            (folder / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')

        attempts = [a for a in self.request('/api/state')['attempts'] if a['task_id'] == 'ext_chain_task']
        by_id = {a['id']: a for a in attempts}
        self.assertEqual(by_id['ext_chain_3']['status'], 'completed')
        self.assertEqual(by_id['ext_chain_1']['status'], 'interrupted')
        self.assertEqual(by_id['ext_chain_2']['status'], 'interrupted')
        self.assertIsNotNone(by_id['ext_chain_1']['ended'])
        self.assertIsNotNone(by_id['ext_chain_2']['ended'])
        for superseded in ('ext_chain_1', 'ext_chain_2'):
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/api/stop', {'id': superseded})
            self.assertEqual(err.exception.code, 400)

        # Standalone attempt without heartbeat past timeout still derives 'lost'.
        orphan = self.watch / 'no_hb_lost'
        orphan.mkdir()
        (orphan / 'meta.json').write_text(json.dumps({
            'version': 1, 'project': 'GT', 'name': 'Orphan', 'task_id': 'orphan_task',
            'attempt_id': 'no_hb_lost', 'status': 'running', 'created': now - 5.0, 'heartbeat': None,
        }), encoding='utf-8')
        orphan_row = next(a for a in self.request('/api/state')['attempts'] if a['id'] == 'ext_no_hb_lost')
        self.assertEqual(orphan_row['status'], 'lost')

    def test_watch_dirs_api_persistence_and_active_unarchive_surfacing(self):
        extra = self.watch.parent / 'extra_watch'
        extra.mkdir()
        run = ExternalRun(extra, project='Cloud', name='Extra', task_id='extra_task', attempt_id='extra_01', heartbeat_interval=30.0).start()
        try:
            # Unauthorized & invalid watch_dirs rejected.
            req = urllib.request.Request(
                self.url + '/api/watch-dirs',
                data=json.dumps({'watch_dirs': [str(extra)]}).encode(),
                headers={'Content-Type': 'application/json', 'X-MLWB-Token': 'wrong'},
            )
            with self.assertRaises(urllib.error.HTTPError) as err403:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(err403.exception.code, 403)

            for bad in (['relative/path'], [str(extra / 'missing')], 'not-a-list'):
                with self.assertRaises(urllib.error.HTTPError) as err400:
                    self.request('/api/watch-dirs', {'watch_dirs': bad})
                self.assertEqual(err400.exception.code, 400)

            res = self.request('/api/watch-dirs', {'watch_dirs': [str(self.watch), str(extra)]})
            self.assertEqual(res['watch_dirs'], [str(self.watch.resolve()), str(extra.resolve())])
            snap = self.request('/api/state')
            self.assertTrue(any(a['id'] == 'ext_extra_01' for a in snap['attempts']))

            # Restart server WITHOUT passing watch_dirs: saved watch_dirs must persist automatically!
            self.server.shutdown(); self.server.server_close(); self.thread.join()
            self.server = wb.make_server(self.data, 0)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
            self.url = f'http://127.0.0.1:{self.server.server_port}'
            self.token = self.request('/api/state')['token']
            snap2 = self.request('/api/state')
            self.assertEqual(snap2['watch_dirs'], [str(self.watch.resolve()), str(extra.resolve())])
            self.assertTrue(any(a['id'] == 'ext_extra_01' for a in snap2['attempts']))

            # Archive while stopped, then start a new live attempt -> archived task automatically surfaces.
            run.finish(status='completed', exit_code=0)
            self.request('/api/archive', {'task_ids': ['ext_extra_task'], 'archived': True})
            self.assertTrue(next(t for t in self.request('/api/state')['tasks'] if t['id'] == 'ext_extra_task')['archived'])
            live2 = ExternalRun(extra, project='Cloud', name='Extra', task_id='extra_task', attempt_id='extra_02', heartbeat_interval=30.0).start()
            try:
                self.assertFalse(next(t for t in self.request('/api/state')['tasks'] if t['id'] == 'ext_extra_task')['archived'])
            finally:
                live2.finish(status='completed', exit_code=0)

            # Removing watch dirs takes effect immediately.
            cleared = self.request('/api/watch-dirs', {'watch_dirs': []})
            self.assertEqual(cleared['watch_dirs'], [])
            self.assertEqual(self.request('/api/state')['watch_dirs'], [])
        finally:
            run.finish(status='completed', exit_code=0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
