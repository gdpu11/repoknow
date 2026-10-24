"""Start/stop the local service without installing a system daemon."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('REPOKNOW_DATA_DIR', ROOT / '.data')).resolve()
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
PID = DATA / 'server.pid'
PORT = int(os.environ.get('REPOKNOW_PORT', '8090'))


def running():
    if not PID.exists():
        return None
    pid = int(PID.read_text())
    command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                             capture_output=True, text=True).stdout
    return pid if str(ROOT) in command and 'app.main:create_app' in command else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'stop', 'status'])
    args = parser.parse_args()
    with (DATA / 'manager.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid = running()
        if args.command == 'status':
            print(f'运行中 PID={pid} · http://localhost:{PORT}' if pid else '已停止')
            return
        if args.command == 'stop':
            if pid:
                os.kill(pid, signal.SIGTERM)
                for _ in range(40):
                    if not running():
                        break
                    time.sleep(0.25)
            PID.unlink(missing_ok=True)
            print('本地服务已停止，索引和凭证保留。')
            return
        if pid:
            print(f'本地服务已运行：http://localhost:{PORT}')
            return
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(('127.0.0.1', PORT)) == 0:
                raise SystemExit(f'端口 {PORT} 已被其他进程占用，未启动或停止任何其他服务。')
        python = ROOT / '.venv/bin/python'
        if not python.exists():
            raise SystemExit('请先在项目目录执行 uv sync，创建 .venv 和安装依赖。')
        with (DATA / 'server.log').open('ab') as log:
            process = subprocess.Popen([str(python), '-m', 'uvicorn', 'app.main:create_app',
                '--factory', '--app-dir', str(ROOT), '--host', '127.0.0.1', '--port', str(PORT)],
                cwd=ROOT, env=os.environ | {'REPOKNOW_DATA_DIR': str(DATA)},
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        PID.write_text(str(process.pid))
        for _ in range(40):
            if process.poll() is not None:
                PID.unlink(missing_ok=True)
                raise SystemExit('启动失败，请查看 .data/server.log。')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health', timeout=1) as response:
                    if response.status == 200:
                        print(f'本地知识库已启动：http://localhost:{PORT}')
                        print('首次管理员登录信息：' + str(DATA / 'admin.json'))
                        return
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(0.25)
        raise SystemExit('服务仍在启动，请检查 .data/server.log。')


if __name__ == '__main__':
    main()
