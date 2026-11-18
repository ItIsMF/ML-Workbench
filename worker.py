"""Detached owner of one attempt. HTTP service lifetime is independent."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from workbench import FileLock, attempt, connect


def windows_job():
    """Place this worker in a kill-on-close job BEFORE creating any child."""
    if os.name != 'nt':
        return None

    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64), ('flags', wintypes.DWORD),
                    ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]

    class Io(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', Io), ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]; kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    if not job: raise ctypes.WinError(ctypes.get_last_error())
    limits = Extended(); limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    # Keep the handle until process exit. Closing it here also terminates this worker.
    return job


def run(data, ident):
    row = attempt(data, ident); folder = data / 'attempts' / ident
    lock = FileLock(folder / 'worker.lock')
    process = None
    try:
        job = windows_job()
        (folder / 'owner.json').write_text(json.dumps({'pid': os.getpid(), 'attempt': ident}), encoding='utf-8')
        spec = row['spec']
        environment = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8',
            MLWB_EVENTS_FILE=str(folder / 'events.jsonl'), MLWB_STOP_FILE=str(folder / 'stop.request'),
            MLWB_RUN_DIR=str(folder), MLWB_WORK_DIR=str(data / 'work' / row['run_id']))
        argv = spec['resume_argv'] if row['parent'] else spec['argv']
        if (folder / 'force.stop').exists():
            raise RuntimeError('启动前已收到强制终止请求')
        with open(folder / 'stdout.log', 'ab', buffering=0) as log:
            process = subprocess.Popen(argv, cwd=spec['cwd'], env=environment, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                start_new_session=(os.name != 'nt'))
            with connect(data) as db:
                db.execute("UPDATE attempts SET status=CASE WHEN status='stopping' THEN status ELSE 'running' END,started=?,pid=? WHERE id=?", (time.time(), process.pid, ident))
            forced = False
            while process.poll() is None:
                if (folder / 'force.stop').exists():
                    forced = True
                    if os.name == 'nt': process.kill()
                    else: os.killpg(process.pid, signal.SIGKILL)
                    break
                time.sleep(0.2)
            code = process.wait()
        status = 'terminated' if forced else ('stopped' if code == 75 and (folder / 'stop.request').exists() else ('completed' if code == 0 else 'failed'))
        with connect(data) as db:
            db.execute('UPDATE attempts SET status=?,ended=?,exit_code=? WHERE id=?', (status, time.time(), code, ident))
    except Exception as error:
        if process and process.poll() is None:
            process.kill(); process.wait()
        with connect(data) as db:
            db.execute("UPDATE attempts SET status='failed',ended=?,error=? WHERE id=?", (time.time(), repr(error), ident))
        raise
    finally:
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--id', required=True)
    args = parser.parse_args()
    run(args.data.resolve(), args.id)
