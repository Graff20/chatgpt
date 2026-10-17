import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Server
from domain import ValidationError

class StartupTests(unittest.TestCase):
    def test_incomplete_install_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as td:
            data=Path(td)/'working-data'
            with patch('app.ROOT',Path(td)/'incomplete-install'):
                with self.assertRaisesRegex(ValidationError,'static/index.html'):
                    Server(('127.0.0.1',0),data)
            self.assertFalse(data.exists())

    def test_busy_port_does_not_create_another_database(self):
        with tempfile.TemporaryDirectory() as td:
            first=Server(('127.0.0.1',0),Path(td)/'first')
            try:
                second_data=Path(td)/'second'
                with self.assertRaises(OSError):Server(first.server_address,second_data)
                self.assertFalse(second_data.exists())
            finally:first.server_close()

    def test_missing_interface_has_recovery_page_and_database_download(self):
        with tempfile.TemporaryDirectory() as td:
            server=Server(('127.0.0.1',0),Path(td)/'data')
            server.store.save_item({'name':'Мука','kind':'raw','unit':'кг'})
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f'http://127.0.0.1:{server.server_port}'
            try:
                with patch('app.ROOT',Path(td)/'moved-program-folder'):
                    with self.assertRaises(urllib.error.HTTPError) as cm:
                        urllib.request.urlopen(url+'/')
                    self.assertEqual(cm.exception.code,503)
                    self.assertIn('Скачать резервную копию',cm.exception.read().decode())
                    with urllib.request.urlopen(url+'/api/backup') as r:
                        self.assertTrue(r.read().startswith(b'SQLite format 3'))
                self.assertEqual(server.store.snapshot()['items'][0]['name'],'Мука')
            finally:
                server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
