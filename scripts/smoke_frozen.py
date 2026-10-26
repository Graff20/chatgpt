"""Check Windows frozen executables and bundled UI before publishing the bundle."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as td:
    data=Path(td)/'data'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    with (Path(td)/'server.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen([str(root/'dist/CehServer/CehServer.exe'),'--data',str(data),'--host','127.0.0.1','--port',str(port)],stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+45
            while True:
                if process.poll() is not None:raise RuntimeError('Frozen server exited before startup')
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=2) as response:
                        assert b'<html' in response.read().lower(),'Bundled interface is missing'
                    break
                except OSError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.25)
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/state',timeout=5) as response:
                assert json.load(response)['schema_version']==6
            subprocess.run([str(root/'dist/CehAdmin/CehAdmin.exe'),'backup','--data',str(data),'--output',str(Path(td)/'copy.sqlite3')],check=True,timeout=30)
            assert (Path(td)/'copy.sqlite3').read_bytes().startswith(b'SQLite format 3')
            print('Frozen server, bundled UI and backup utility passed')
        except Exception:
            log.flush()
            print((Path(td)/'server.log').read_text(encoding='utf-8',errors='replace'))
            raise
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
