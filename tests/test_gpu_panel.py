import csv
import sqlite3
from contextlib import closing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gpu_panel import GpuPanel


class GpuPanelTests(unittest.TestCase):
    def setUp(self):
        qa = Path(__file__).resolve().parents[1] / '.qa'
        qa.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=qa)
        self.addCleanup(tmp.cleanup)
        self.gpu = GpuPanel(Path(tmp.name))
        self.gpu.managed.mkdir(parents=True)

    def test_retention_and_history_boundaries(self):
        g = self.gpu
        old = 'trend-gpu0-20260101-100000-100000.csv'
        keep = 'trend-gpu0-20261119-100000-100000.csv'
        paths = [g.root/old, g.managed/old, g.managed/'operator-notes.txt', g.managed/keep]
        for p in paths:
            p.write_text('timestamp,value\n')
        for p in paths[:3]:
            os.utime(p, (1,1))
        self.assertEqual({i['id'] for i in g.status()['records']}, {'local/'+old, 'panel/'+keep})
        self.assertFalse(paths[1].exists()); self.assertTrue(paths[2].exists())
        for key in ['panel/../'+old,'local/../../index.sqlite','unknown/'+keep]:
            with self.assertRaises(ValueError): g.read(key)
        with paths[-1].open('a',newline='') as f:
            csv.writer(f).writerows((f't{i}', i) for i in range(40))
        rows = g.read('panel/'+keep)['rows']
        self.assertEqual((len(rows),rows[0][0],rows[-1][0]), (30,'t10','t39'))

    def test_offline_counter_values_are_distinct_from_sample_counts(self):
        name = 'counters-gpu0-20260101-100000-000001.sqlite'
        with closing(sqlite3.connect(self.gpu.root / name)) as db:
            db.executescript('''
                CREATE TABLE GPU_METRICS(typeId, metricId, value);
                CREATE TABLE TARGET_INFO_GPU_METRICS(typeId, metricId, metricName);
                CREATE TABLE StringIds(value);
                INSERT INTO TARGET_INFO_GPU_METRICS VALUES
                    (1, 1, 'SM Tensor Pipe Active [Throughput %]'), (1, 2, 'SM Issue [Throughput %]');
                INSERT INTO GPU_METRICS VALUES (1, 1, 4), (1, 1, 10), (1, 2, 20), (1, 2, 40);
            ''')
        result = self.gpu.read('local/' + name)
        self.assertEqual({m['name']: (m['mean'], m['low'], m['high'], m['samples'])
                          for m in result['metrics']},
                         {'SM Tensor Pipe Active [Throughput %]': (7, 4, 10, 2),
                          'SM Issue [Throughput %]': (30, 20, 40, 2)})

    def test_repeat_start_and_stop_own_sampler(self):
        g = self.gpu
        entered = threading.Event()
        def wait_for_stop():
            entered.set(); g.cancel.wait(3)
        with patch.object(g, 'trend', side_effect=wait_for_stop):
            g.start('trend',''); self.assertTrue(entered.wait(2))
            with self.assertRaises(ValueError): g.start('counters','')
            g.stop()
            deadline = time.monotonic()+2
            while g.status()['mode'] and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(g.status()['mode'],'')
        with self.assertRaises(ValueError): g.start('counters','arbitrary-command')

    def test_counter_budget_leaves_trends_and_old_cli_files_alone(self):
        path = self.gpu.managed/'counters-gpu0-20261119-100000-000001.sqlite'
        # Lower the budget for this fixture instead of allocating a real large file.
        path.write_bytes(b'123')
        with patch('gpu_panel.COUNTER_BUDGET', 3):
            with self.assertRaisesRegex(ValueError,'512 MiB'): self.gpu.start('counters','')
        self.assertEqual(path.read_bytes(),b'123')
