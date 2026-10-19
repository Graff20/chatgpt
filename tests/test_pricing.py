"""Price matrix, dependency recalculation and historical document preservation."""
import json,sqlite3,unittest
from contextlib import closing
import test_workflow as fixtures
from domain import Store,ValidationError,SCHEMA_VERSION
from app import Handler

class PricingTests(unittest.TestCase):
    setUp=fixtures.WorkflowTests.setUp
    tearDown=fixtures.WorkflowTests.tearDown
    item=fixtures.WorkflowTests.item
    state=fixtures.WorkflowTests.state
    recipe=fixtures.WorkflowTests.recipe
    receipt=fixtures.WorkflowTests.receipt
    produce=fixtures.WorkflowTests.produce
    shipment=fixtures.WorkflowTests.shipment
    def quote(self,lid=1,iid=None):return next(r for r in self.store.pricing()['cells'] if r['list_id']==lid and r['item_id']==(iid or self.product))
    def apply(self,**data):return self.store.edit_prices(dict(token=self.store.pricing()['token'],apply=True,responsible='Менеджер',**data))['list_id']
    def column(self,name='Дистрибьютор',**kw):return self.apply(column=dict(name=name,source_id=1,adjustment_type='percent',adjustment=-15,round_step=1,round_mode='nearest',**kw))
    def client_list(self,lid):self.store.save_partner(dict(next(p for p in self.state()['partners'] if p['id']==self.client),price_list_id=lid))
    def order(self,**line):return self.store.save_order(dict(client_id=self.client,manager='М',due=self.future,lines=[dict(item_id=self.product,qty=1,**line)]))
    def test_preview_rolls_back_and_matches_apply_with_decimal_rounding(self):
        self.apply(list_id=1,changes=[dict(item_id=self.product,mode='manual',amount=947)])
        req=dict(token=self.store.pricing()['token'],column=dict(name='Дистрибьютор',source_id=1,adjustment=-15,round_step=10))
        preview=self.store.edit_prices(req);self.assertEqual(len(self.store.pricing()['lists']),1)
        change=next(r for r in preview['changes'] if r['item_id']==self.product);self.assertEqual(change['new']['price'],800)
        lid=self.store.edit_prices(dict(req,apply=True,responsible='М'))['list_id'];self.assertEqual(self.quote(lid)['price'],800)
        with closing(self.store.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM price_history').fetchone()[0],2)
    def test_rounding_directions_and_fixed_amount(self):
        for direction,expected in [('nearest',680),('up',680),('down',680)]:
            lid=self.apply(column=dict(name=direction,source_id=1,adjustment_type='percent',adjustment=-15,round_step=10,round_mode=direction));self.assertEqual(self.quote(lid)['price'],expected)
        self.apply(list_id=1,changes=[dict(item_id=self.product,mode='manual',amount=947)])
        expected={'nearest':800,'up':810,'down':800}
        for col in self.store.pricing()['lists'][1:]:self.assertEqual(self.quote(col['id'])['price'],expected[col['name']])
        lid=self.apply(column=dict(name='С доставкой',source_id=1,adjustment_type='amount',adjustment=50,round_step=.01));self.assertEqual(self.quote(lid)['price'],997)
    def test_manual_override_block_and_reset_survive_source_updates(self):
        lid=self.column();other=self.store.save_item(dict(name='Палочки',kind='finished',unit='уп.',sale_price=170))
        self.apply(list_id=lid,changes=[dict(item_id=self.product,mode='manual',amount=777),dict(item_id=other,mode='blocked')])
        self.apply(list_id=1,changes=[dict(item_id=self.product,mode='manual',amount=1000),dict(item_id=other,mode='manual',amount=200)])
        self.assertEqual(self.quote(lid)['price'],777);self.assertEqual(self.quote(lid,other)['mode'],'blocked')
        self.apply(list_id=lid,changes=[dict(item_id=self.product,mode='auto')]);self.assertEqual(self.quote(lid)['price'],850)
    def test_chained_rules_and_cycle_are_atomic(self):
        a=self.column();b=self.apply(column=dict(name='Доставка',source_id=a,adjustment_type='amount',adjustment=50,round_step=1));self.assertEqual(self.quote(b)['price'],730)
        before=self.store.pricing()
        with self.assertRaises(ValidationError):self.apply(column=dict(id=a,name='Цикл',source_id=b,adjustment=-15))
        self.assertEqual(before,self.store.pricing())
        with self.assertRaises(ValidationError):self.apply(column=dict(id=1,name='Розница',source_id=a))
    def test_blank_column_missing_price_manual_zero_and_negative_calculation(self):
        lid=self.apply(column=dict(name='Особый',source_id=None));self.assertIsNone(self.quote(lid)['price']);self.client_list(lid)
        with self.assertRaisesRegex(ValidationError,'Нет цены'):self.order()
        self.apply(list_id=lid,changes=[dict(item_id=self.product,mode='manual',amount=0)])
        oid=self.order();self.assertEqual(next(r for r in self.state()['order_lines'] if r['order_id']==oid)['price'],0)
        negative=self.apply(column=dict(name='Ошибка',source_id=1,adjustment_type='amount',adjustment=-1000));self.assertIsNone(self.quote(negative)['price'])
    def test_new_finished_item_gets_derived_price_and_catalog_structure(self):
        lid=self.column();i=self.store.save_item(dict(name='Десерт новый',kind='finished',unit='уп.',folder='Кондитерская/Десерты',category='Муссы',sale_price=200))
        self.assertEqual(self.quote(lid,i)['price'],170)
        h=object.__new__(Handler);h.server=type('Server',(),{'store':self.store})()
        title,rows=h.export_rows(self.state(),'prices',{})
        row=next(r for r in rows if 'Десерт новый' in r);self.assertEqual(row[1:3],['Десерт новый','уп.']);self.assertIn(['Папка: Кондитерская/Десерты'],rows);self.assertIn(['Категория: Муссы'],rows)
        self.assertFalse(any('Филе' in r for r in rows)) # raw ingredient excluded
    def test_bulk_skips_exceptions_and_only_selected_products(self):
        lid=self.column();other=self.item('Другое','finished',sale_price=100)
        self.apply(list_id=lid,changes=[dict(item_id=self.product,mode='manual',amount=777)])
        self.apply(list_id=lid,skip_exceptions=True,changes=[dict(item_id=self.product,mode='auto')]);self.assertEqual(self.quote(lid)['price'],777);self.assertEqual(self.quote(lid,other)['price'],85)
    def test_blocked_product_cannot_be_ordered_even_manual(self):
        lid=self.column();self.client_list(lid);self.apply(list_id=lid,changes=[dict(item_id=self.product,mode='blocked')])
        for line in ({},{'price':800}):
            with self.assertRaises(ValidationError):self.order(**line)
        self.assertEqual(self.state()['orders'],[])
    def test_saved_order_and_invoice_keep_prices_after_repricing(self):
        lid=self.column();self.client_list(lid);oid=self.order();before=next(l for l in self.state()['order_lines'] if l['order_id']==oid);self.assertEqual(before['price'],680)
        self.receipt();self.produce();sid=self.shipment(oid,1);doc=next(d for d in self.state()['documents'] if d['id']==sid);before=next(l for l in self.state()['order_lines'] if l['order_id']==oid)
        self.apply(list_id=1,changes=[dict(item_id=self.product,mode='manual',amount=1000)])
        self.assertEqual(self.quote(lid)['price'],850);self.assertEqual(before,next(l for l in self.state()['order_lines'] if l['order_id']==oid));self.assertEqual(doc,next(d for d in self.state()['documents'] if d['id']==sid))
    def test_open_order_edit_preserves_saved_price_and_explicit_reprice(self):
        lid=self.column();self.client_list(lid);oid=self.order();self.apply(list_id=1,changes=[dict(item_id=self.product,mode='manual',amount=1000)])
        payload=dict(id=oid,client_id=self.client,manager='М',due=self.future,lines=[dict(item_id=self.product,qty=2,price=680,price_mode='saved')]);self.store.save_order(payload)
        self.assertEqual(next(l for l in self.state()['order_lines'] if l['order_id']==oid)['price'],680)
        payload['lines'][0].update(price=850,price_mode='auto');self.store.save_order(payload);self.assertEqual(next(l for l in self.state()['order_lines'] if l['order_id']==oid)['price'],850)
    def test_stale_preview_and_stale_auto_order_are_rejected(self):
        token=self.store.pricing()['token'];self.column()
        with self.assertRaisesRegex(ValidationError,'изменился'):self.store.edit_prices(dict(token=token,column=dict(name='Другое'),apply=True,responsible='М'))
        with self.assertRaisesRegex(ValidationError,'изменился'):self.store.save_order(dict(client_id=self.client,manager='М',due=self.future,price_token=token,lines=[dict(item_id=self.product,qty=1,price_mode='auto')]))
        with self.assertRaisesRegex(ValidationError,'изменилась'):self.order(price_mode='auto',price=5)
    def test_invalid_column_and_raw_cell_do_not_mutate_prices(self):
        before=self.store.pricing()
        for kwargs in [dict(column=dict(name='Розница')),dict(column=dict(name='Неверный',adjustment=-101)),dict(column=dict(name='Неверный',round_step=3)),dict(list_id=1,changes=[dict(item_id=self.raw,mode='manual',amount=10)])]:
            with self.assertRaises(ValidationError):self.apply(**kwargs)
            self.assertEqual(before,self.store.pricing())
    def test_v4_upgrade_preserves_legacy_prices_stock_and_documents(self):
        self.receipt();self.produce();self.order(price=750);before=self.state();path=self.store.path
        with self.store.transaction() as c:
            c.execute('ALTER TABLE partners DROP COLUMN price_list_id');c.execute('ALTER TABLE orders DROP COLUMN price_list_id');c.execute('ALTER TABLE order_lines DROP COLUMN price_origin')
            c.execute('DROP TABLE price_values');c.execute('DROP TABLE price_history');c.execute('DROP TABLE price_lists');c.execute('PRAGMA user_version=4')
        store=Store(path);after=store.snapshot();self.assertEqual(after['schema_version'],SCHEMA_VERSION)
        for key in ('lots','documents','items'):self.assertEqual(before[key],after[key])
        self.assertEqual(after['order_lines'][0]['price'],750);self.assertEqual(after['order_lines'][0]['price_origin'],'saved');self.assertEqual(store.pricing()['cells'][0]['price'],800)
        with closing(sqlite3.connect(next((path.parent/'backups').glob('before-v5-*')))) as c:self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],4)
        self.assertEqual(after,Store(path).snapshot())

class PricingHttpTests(unittest.TestCase):
    import test_accounting as fixtures
    setUp=fixtures.HttpTests.setUp
    tearDown=fixtures.HttpTests.tearDown
    request=fixtures.HttpTests.request
    def test_preview_response_exports_and_backup_restore_prices(self):
        import io,zipfile,base64
        iid=json.loads(self.request('/api/items',dict(name='Филе готовое',kind='finished',unit='кг',sale_price=947,folder='Кулинария/Сувид',category='Курица'))[1])['id']
        token=json.loads(self.request('/api/pricing')[1])['token'];payload=dict(token=token,column=dict(name='Дистрибьютор',source_id=1,adjustment=-15,round_step=10))
        preview=json.loads(self.request('/api/prices',payload)[1]);self.assertEqual(preview['changes'][0]['new']['price'],800)
        result=json.loads(self.request('/api/prices',dict(payload,apply=True,responsible='М'))[1]);self.assertEqual(result['list_id'],2)
        original=json.loads(self.request('/api/pricing')[1]);_,backup,_=self.request('/api/backup')
        for fmt in ('html','csv','json','xlsx','docx'):
            _,raw,_=self.request('/api/export?type=prices&format='+fmt)
            if fmt in ('xlsx','docx'):
                with zipfile.ZipFile(io.BytesIO(raw)) as z:self.assertIsNone(z.testzip())
            else:self.assertIn('Дистрибьютор',raw.decode('utf-8-sig'))
        self.request('/api/prices',dict(token=original['token'],list_id=2,changes=[dict(item_id=iid,mode='manual',amount=777)],apply=True,responsible='М'))
        self.request('/api/restore',dict(file=base64.b64encode(backup).decode(),confirm='ВОССТАНОВИТЬ'))
        self.assertEqual(json.loads(self.request('/api/pricing')[1]),original)
        self.assertEqual(self.request('/pricing.js')[0],200)
    def test_price_print_and_every_export_have_company_date_categories_and_exact_prices(self):
        import io,zipfile,xml.etree.ElementTree as ET
        from datetime import date
        self.request('/api/company',dict(name='ООО Кухня',inn='1234567890',address='Москва, ул. Кухонная, 1',phone='+7 900 123-45-67'))
        iid=json.loads(self.request('/api/items',dict(name='Филе сувид',kind='finished',unit='кг',sale_price=947,folder='Кулинария/Сувид',category='Птица'))[1])['id']
        token=json.loads(self.request('/api/pricing')[1])['token']
        self.request('/api/prices',dict(token=token,column=dict(name='Дистрибьютор',source_id=1,adjustment=-15,round_step=10),apply=True,responsible='М'))
        token=json.loads(self.request('/api/pricing')[1])['token']
        self.request('/api/prices',dict(token=token,list_id=2,changes=[dict(item_id=iid,mode='manual',amount=777.25)],apply=True,responsible='М'))
        before=json.loads(self.request('/api/state')[1])
        for fmt in ('html','csv','json','xlsx','docx'):
            _,raw,_=self.request('/api/export?type=prices&format='+fmt)
            if fmt in ('xlsx','docx'):
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    xml=z.read('word/document.xml' if fmt=='docx' else 'xl/worksheets/sheet1.xml')
                    text=' '.join(ET.fromstring(xml).itertext())
                    if fmt=='docx':self.assertIn(b'gridSpan',xml)
            else:text=raw.decode('utf-8-sig')
            for value in ('Прайс-лист','ООО Кухня',date.today().strftime('%d.%m.%Y'),'Папка: Кулинария/Сувид','Категория: Птица','Дистрибьютор','Филе сувид'):
                self.assertIn(value,text)
            self.assertIn('777,25' if fmt in ('html','docx') else '777.25',text)
            if fmt=='html':self.assertIn('colspan="5"',text);self.assertIn('<thead>',text)
        after=json.loads(self.request('/api/state')[1])
        for key in ('items','partners','recipes','lots','orders','order_lines','documents','pricing'):self.assertEqual(before[key],after[key])
        # Purchasing analytics still exports purchase prices, independently of sale-price columns.
        text=self.request('/api/export?type=purchase_prices&format=html')[1].decode();self.assertIn('История закупочных цен',text);self.assertNotIn('Дистрибьютор',text)

    def test_genuine_v4_restore_upgrades_without_changing_source(self):
        import base64,hashlib,tempfile
        from pathlib import Path
        from demo import create_demo
        store=create_demo(Path(self.tmp.name)/'v4.sqlite3');before=store.snapshot()
        with store.transaction() as c:
            c.execute('ALTER TABLE partners DROP COLUMN price_list_id');c.execute('ALTER TABLE orders DROP COLUMN price_list_id');c.execute('ALTER TABLE order_lines DROP COLUMN price_origin')
            c.execute('DROP TABLE price_values');c.execute('DROP TABLE price_history');c.execute('DROP TABLE price_lists');c.execute('PRAGMA user_version=4')
        with closing(store.connect()) as c:c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        source=store.path.read_bytes();digest=hashlib.sha256(source).hexdigest();encoded=base64.b64encode(source).decode()
        preview=json.loads(self.request('/api/restore-preview',dict(file=encoded))[1]);self.assertEqual(preview['schema_version'],4)
        self.request('/api/restore',dict(file=encoded,confirm='ВОССТАНОВИТЬ',expected_sha256=preview['sha256']))
        after=json.loads(self.request('/api/state')[1]);self.assertEqual(after['schema_version'],SCHEMA_VERSION)
        for key in ('documents','lots','items'):self.assertEqual(before[key],after[key])
        self.assertEqual(after['pricing']['lists'][0]['name'],'Розница');self.assertEqual(hashlib.sha256(store.path.read_bytes()).hexdigest(),digest)

if __name__=='__main__':unittest.main()

