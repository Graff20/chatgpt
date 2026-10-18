import base64
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from contextlib import closing
import test_accounting as fixtures
from domain import SCHEMA

class RestorePreviewTests(unittest.TestCase):
    setUp=fixtures.HttpTests.setUp
    tearDown=fixtures.HttpTests.tearDown
    request=fixtures.HttpTests.request

    def source(self,name=None):
        path=Path(self.tmp.name)/'source.sqlite3'
        with closing(sqlite3.connect(path)) as c:
            c.executescript(SCHEMA+';PRAGMA user_version=1;')
            if name:c.execute('INSERT INTO items(name,kind,unit) VALUES(?,\'raw\',\'кг\')',(name,))
            c.commit()
        return base64.b64encode(path.read_bytes()).decode()
    def test_preview_reports_records_without_migration_or_replacement(self):
        self.server.store.save_item({'name':'Текущий товар','kind':'raw','unit':'кг'})
        source=self.source('Старая позиция');before=self.server.store.snapshot()
        backups=set((Path(self.tmp.name)/'backups').glob('*'))
        preview=json.loads(self.request('/api/restore-preview',{'file':source})[1])
        self.assertEqual(preview['counts']['items'],1);self.assertEqual(preview['schema_version'],1)
        self.assertEqual(self.server.store.snapshot(),before)
        self.assertEqual(set((Path(self.tmp.name)/'backups').glob('*')),backups)
        self.assertFalse(list(Path(self.tmp.name).glob('restore-candidate-*')))
    def test_empty_source_is_rejected_before_working_database_is_changed(self):
        self.server.store.save_item({'name':'Сохранить товар','kind':'raw','unit':'кг'})
        source=self.source();before=self.server.store.snapshot()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.request('/api/restore',{'file':source,'confirm':'ВОССТАНОВИТЬ','require_nonempty':True})
        self.assertEqual(cm.exception.code,400)
        self.assertIn('копия пустая',json.loads(cm.exception.read())['error'])
        self.assertEqual(self.server.store.snapshot(),before)
        self.assertFalse(list(Path(self.tmp.name).glob('restore-candidate-*')))
    def test_restoration_reports_actual_counts_and_checked_digest(self):
        source=self.source('Найденная позиция')
        preview=json.loads(self.request('/api/restore-preview',{'file':source})[1])
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/restore',{'file':source,'confirm':'ВОССТАНОВИТЬ','expected_sha256':'incorrect'})
        self.assertEqual(self.server.store.snapshot()['items'],[])
        result=json.loads(self.request('/api/restore',{'file':source,'confirm':'ВОССТАНОВИТЬ','expected_sha256':preview['sha256'],'require_nonempty':True})[1])
        self.assertEqual(result['counts']['items'],1)
        self.assertEqual(result['database_path'],str(self.server.store.path))
        self.assertEqual(json.loads(self.request('/api/state')[1])['items'][0]['name'],'Найденная позиция')

if __name__=='__main__':unittest.main()
