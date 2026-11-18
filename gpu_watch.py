"""Optional, out-of-process NVIDIA telemetry. Does not launch a training job."""
import argparse
import csv
from contextlib import closing
from datetime import datetime
from pathlib import Path
import os
import re
import shutil
import sqlite3
import subprocess
import sys


FIELDS = ('timestamp,uuid,pci.bus_id,utilization.gpu,utilization.memory,memory.used,'
          'memory.total,power.draw,temperature.gpu,clocks.current.sm,clocks.current.memory')
DEFAULT_DIR = Path(__file__).resolve().parent / 'data' / 'gpu'


def nsys():
    if path := shutil.which('nsys'):
        return path
    if os.name == 'nt':
        root = Path(os.environ['ProgramFiles']) / 'NVIDIA Corporation'
        paths = root.glob('Nsight Systems */target-windows-x64/nsys.exe')
        newest = max(paths, key=lambda p: tuple(map(int, re.findall(r'\d+', p.parents[1].name))), default=None)
        if newest:
            return str(newest)
    raise SystemExit('Nsight Systems CLI not found. Install it or add nsys to PATH.')


def destination(args):
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    return args.out_dir.resolve() / f'{args.mode}-gpu{args.device}-{stamp}'


def trend(args):
    path = destination(args).with_suffix('.csv')
    print(f'Whole-device trend -> {path} (Ctrl+C to stop)', flush=True)
    cmd = ['nvidia-smi', '-i', str(args.device), '--query-gpu=' + FIELDS,
           '--format=csv', f'--loop={args.interval}']
    with path.open('xb') as out:
        try:
            subprocess.run(cmd, stdout=out, check=True, timeout=args.seconds)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            pass  # The native sampler has been stopped; verify that it emitted data.
    with path.open(encoding='utf-8', newline='') as src:
        rows = csv.reader(src, skipinitialspace=True)
        header, first = next(rows, []), next(rows, [])
        if not header or header[0] != 'timestamp' or len(first) != len(FIELDS.split(',')):
            raise SystemExit('No complete telemetry sample. Inspect the CSV and nvidia-smi diagnostics.')


def counters(args):
    exe = nsys()
    result = subprocess.run([exe, 'profile', '--gpu-metrics-devices=help'], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors='replace')
    print(result.stdout.rstrip(), flush=True)
    result.check_returncode()
    if 'None of the installed GPUs are supported' in result.stdout:
        raise SystemExit('GPU metrics unavailable in this session. No capture started.')
    if args.mode == 'check':
        return
    stem = destination(args)
    cmd = [exe, 'profile', '--trace=none', '--sample=none', '--cpuctxsw=none',
           f'--gpu-metrics-devices={args.device}', '--gpu-metrics-frequency=100',
           f'--duration={args.seconds}', '--export=sqlite', f'--output={stem}']
    if args.metric_set:
        cmd.append(f'--gpu-metrics-set={args.metric_set}')
    # A CPU-only timer supplies the target lifetime. The GPU counters are device-wide.
    subprocess.run(cmd + [sys.executable, '-I', '-S', '-c', f'import time; time.sleep({args.seconds})'], check=True)
    print(stem.with_suffix('.nsys-rep'))
    summary(stem.with_suffix('.sqlite'))


# Only these counters have known display units. Other fields remain in the report.
PERCENT = ('GR Engine Active', 'SMs Active', 'SM Issue', 'SM Issue Stage Throughput',
           'Tensor Active', 'SM Tensor Pipe Active', 'VRAM Total Bandwidth',
           'DRAM Read Bandwidth', 'DRAM Write Bandwidth')
VRAM = ('VRAM Read Bandwidth', 'VRAM Write Bandwidth', 'VRAM Total Bandwidth')


def report(db):
    db = db.resolve()
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        rows = conn.execute('SELECT g.typeId, d.metricName, AVG(g.value), MIN(g.value), '
                            'MAX(g.value), COUNT(g.value) FROM GPU_METRICS g '
                            'JOIN TARGET_INFO_GPU_METRICS d USING(typeId,metricId) '
                            'WHERE g.value IS NOT NULL GROUP BY g.typeId, d.metricId '
                            'ORDER BY g.typeId, d.metricId').fetchall()
        # Nsight 2025.6 exports sector rates before its display multiplier. Only
        # apply this conversion when the capture itself identifies the mapping.
        configs = conn.execute("SELECT value FROM StringIds WHERE instr(value, 'metricSets:') > 0").fetchall()
    if not rows:
        raise ValueError(f'No GPU counter samples in {db}.')
    scales = {name + ' [Throughput %]': 1 for name in PERCENT}
    if len(configs) == 1 and len({row[0] for row in rows}) == 1:
        config = configs[0][0]
        block = re.search(r'(?m)^- sub: &bandwidth_sectors\r?\n((?:[ \t]+[^\n]+\n)+)', config)
        if block and (factor := re.search(r'name: GB/s,\s+suffix: \.sum\.per_second, multiplier: ([\d.eE+-]+)', block[1])):
            for name in VRAM:
                if re.search(r'name: "' + re.escape(name) + r'", id: dram__sectors\w*, subMetrics: \*bandwidth_sectors\b', config):
                    scales[name + ' [GB/s]'] = float(factor[1])
    metrics, notes = [], []
    for stream in dict.fromkeys(row[0] for row in rows):
        series = [row for row in rows if row[0] == stream]
        selected = [row for row in series if row[1] in scales]
        if not selected:
            notes.append('未知指标集，以下为 SQLite 原值，未换算显示单位。')
        for _, name, avg, lo, hi, count in selected or series:
            k = scales.get(name, 1)
            metrics.append(dict(stream=str(stream), name=name, mean=avg*k, low=lo*k, high=hi*k, samples=count))
        if selected and any(row[1] == n+' [GB/s]' and row[1] not in scales for row in series for n in VRAM):
            notes.append('报告未确认显存换算倍率，省略 GB/s。')
    return dict(series=len(rows), metrics=metrics, notes=notes)


def summary(db):
    result = report(db)
    print(f'{db}\n{result["series"]} metric series; sample mean, minimum, maximum and count:')
    for note in result['notes']:
        print(note)
    print(f'{"Metric":<46} {"mean":>10} {"min":>10} {"max":>10} {"samples":>9}')
    streams = {m['stream'] for m in result['metrics']}
    for m in result['metrics']:
        label = m['name'] if len(streams) == 1 else m['stream'] + ': ' + m['name']
        print(f'{label:<46} {m["mean"]:10.2f} {m["low"]:10.2f} {m["high"]:10.2f} {m["samples"]:9d}')


def positive(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    tr = modes.add_parser('trend', help='nvidia-smi CSV until Ctrl+C or --seconds')
    tr.add_argument('--interval', type=positive, default=2, help='sample interval, seconds (default: 2)')
    tr.add_argument('--seconds', type=positive)
    modes.add_parser('check', help='list Nsight GPU metrics devices and permission diagnostics')
    read = modes.add_parser('summary', help='read an existing Nsight SQLite report; no capture')
    read.add_argument('db', type=Path)
    hw = modes.add_parser('counters', help='short Nsight device-wide window, 100 Hz')
    hw.add_argument('--seconds', type=int, choices=range(1, 61), metavar='1..60', default=10)
    hw.add_argument('--metric-set', help='Nsight metric set; omit for native device default')
    for p in (tr, hw):
        p.add_argument('--device', type=int, default=0)
        p.add_argument('--out-dir', type=Path, default=DEFAULT_DIR)
    args = parser.parse_args()
    try:
        if args.mode == 'summary':
            summary(args.db)
        else:
            (trend if args.mode == 'trend' else counters)(args)
    except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == '__main__':
    main()
