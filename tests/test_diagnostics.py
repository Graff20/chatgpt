import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from contextlib import closing
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from diagnose import inspect_database,make_report,running_server
from domain import SCHEMA,Store
from app import Server,APP_VERSION

class DiagnosticsTests(unittest.TestCase):
    def test_legacy_database_is_not_changed_or_migrated(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'legacy.sqlite3'
            with closing(sqlite3.connect(path)) as c:
                c.executescript(SCHEMA+';PRAGMA user_version=1;')
                c.execute("INSERT INTO items(name,kind,unit) VALUES('Не выводить название','raw','кг')")
                c.commit()
            before=path.read_bytes()
            result=inspect_database(path)
            self.assertEqual(result['schema_version'],1)
            self.assertEqual(result['counts']['items'],1)
            self.assertEqual(path.read_bytes(),before)
            self.assertFalse((Path(td)/'backups').exists())
    def test_readonly_inspection_includes_committed_wal(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'wal.sqlite3'
            with closing(sqlite3.connect(path)) as c:
                c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA wal_autocheckpoint=0')
                c.executescript(SCHEMA+';PRAGMA user_version=1;')
                c.execute("INSERT INTO items(name,kind,unit) VALUES('Мука','raw','кг')");c.commit()
                self.assertTrue(Path(str(path)+'-wal').exists())
                self.assertEqual(inspect_database(path)['counts']['items'],1)
    def test_report_finds_old_data_and_empty_copy_without_record_contents(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            old=Store(root/'old'/'data'/'ceh.sqlite3');old.save_item({'name':'Секретное название','kind':'raw','unit':'кг'})
            Store(root/'new'/'data'/'ceh.sqlite3')
            report=make_report(root,root/'new')
            self.assertIn(str(old.path),report)
            self.assertIn('ЕСТЬ ЗАПИСИ',report);self.assertIn('ПУСТАЯ БАЗА',report)
            self.assertNotIn('Секретное название',report)
            self.assertEqual(old.snapshot()['items'][0]['name'],'Секретное название')
    def test_runtime_reports_actual_explicit_database(self):
        with tempfile.TemporaryDirectory() as td:
            server=Server(('127.0.0.1',0),Path(td)/'custom-data')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                report=running_server(server.server_port)
                self.assertTrue(report['compatible'])
                self.assertEqual(report['runtime']['database_path'],str(server.store.path))
                self.assertEqual(report['runtime']['app_version'],APP_VERSION)
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/runtime') as r:
                    self.assertEqual(json.load(r)['database_path'],str(server.store.path))
            finally:server.shutdown();server.server_close();thread.join()
    def test_old_server_is_identified_without_writing(self):
        class OldHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body=json.dumps({'schema_version':1,'items':[],'partners':[]}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),OldHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            result=running_server(server.server_port)
            self.assertFalse(result['compatible']);self.assertIsNone(result['runtime'])
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
