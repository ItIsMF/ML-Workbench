"""CPU-only synthetic workflow demo. No ML model, dataset, or scientific result."""
import argparse
import json
import math
import os
from pathlib import Path
import struct
import sys
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reporter import Reporter


def png(path):
    width, height = 256, 128
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            value = (math.sin(x / 23) * math.cos(y / 18) + 1) / 2
            rows.extend((int(30 + 80 * value), int(75 + 150 * value), int(110 + 120 * value)))
    def chunk(name, data):
        return struct.pack('!I', len(data)) + name + data + struct.pack('!I', zlib.crc32(name + data))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--delay', type=float, default=0.15)
    parser.add_argument('--fail-at', type=int, default=-1)
    args = parser.parse_args()
    if 'MLWB_WORK_DIR' not in os.environ:
        parser.error('请通过工作台运行演示，或显式设置 MLWB_WORK_DIR')
    work = Path(os.environ['MLWB_WORK_DIR']); work.mkdir(parents=True, exist_ok=True)
    checkpoint = work / 'checkpoint.json'
    position = json.loads(checkpoint.read_text())['position'] if args.resume else 0
    reporter = Reporter(interval=0.4)
    phases = [('准备', 8), ('训练循环 · 演示', 80), ('评价 · 演示', 16), ('批量推理 · 演示', 16)]
    print(f'CPU 演示，不使用模型；从步骤 {position} 开始。', flush=True)
    offset = 0
    for name, count in phases:
        for step in range(count + 1):
            current = offset + step
            if current < position: continue
            if current == args.fail_at: raise RuntimeError('用户指定的演示失败点')
            # Fixed small CPU workload plus pacing; not a training speed benchmark.
            value = sum(math.sin(i + current) ** 2 for i in range(2500))
            reporter.progress(name, step, count, {'demo_loss': math.exp(-current / 32) + 0.015 * math.sin(current), 'demo_signal': value / 2500})
            temp = checkpoint.with_suffix('.tmp')
            temp.write_text(json.dumps({'position': current}), encoding='utf-8'); temp.replace(checkpoint)
            if step % 10 == 0: print(f'{name}: {step}/{count}', flush=True)
            if reporter.stop_requested():
                reporter.checkpoint('checkpoint.json')
                print('检查点已保存；协作停止。', flush=True)
                return 75
            time.sleep(args.delay)
        offset += count
    png(work / 'preview.png')
    (work / 'summary.json').write_text(json.dumps({'demo': True, 'steps': offset, 'meaning': '工作台流程验证，无科学结论'}, ensure_ascii=False), encoding='utf-8')
    reporter.artifact('preview.png', '合成预览 · 非研究结果')
    reporter.artifact('summary.json', '演示摘要')
    reporter.artifact('checkpoint.json', '演示检查点')
    print('四阶段演示完成。', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
