"""Portion orders use planned kg for production and actual kg for invoices; financial expense reports."""
import json,sqlite3,unittest
from contextlib import closing
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
import test_workflow as fixtures
from domain import Store,ValidationError,remaining_quantity,remaining_portions,SCHEMA_VERSION
from app import Handler
from exports import invoice_html,document_table

class PortionFinanceTests(unittest.TestCase):
    setUp=fixtures.WorkflowTests.setUp
    tearDown=fixtures.WorkflowTests.tearDown
    item=fixtures.WorkflowTests.item
    recipe=fixtures.WorkflowTests.recipe
    receipt=fixtures.WorkflowTests.receipt
    produce=fixtures.WorkflowTests.produce
    state=fixtures.WorkflowTests.state
    def configure(self,weight=.2,rate=0):
        self.store.save_item({'id':self.product,'name':'Филе сувид','kind':'finished','unit':'кг','shelf_days':14,'portion_weight':weight,'labor_rate':rate,'sale_price':800})
    def order(self,count=10,item=None):
        return self.store.save_order({'client_id':self.client,'manager':'М','due':self.future,'lines':[{'item_id':item or self.product,'order_mode':'portions','portions':count,'price':800}]})
    def shipment(self,oid,kg,count):
        line=next(l for l in self.state()['order_lines'] if l['order_id']==oid)
        return self.store.shipment(dict(self.base,order_id=oid,lines=[{'line_id':line['id'],'qty':kg,'portions':count}]))
    def doc(self,id):return next(d for d in self.state()['documents'] if d['id']==id)
    def test_three_flavors_plan_portions_and_kg_separately(self):
        self.configure();a=self.store.save_item({'name':'Филе Прованские травы','kind':'finished','unit':'кг','portion_weight':.2})
        b=self.store.save_item({'name':'Филе Южные пряности','kind':'finished','unit':'кг','portion_weight':.2})
        self.recipe(a,[{'item_id':self.raw,'net':1}]);self.recipe(b,[{'item_id':self.raw,'net':1}])
        oid=self.store.save_order({'client_id':self.client,'manager':'М','due':self.future,'lines':[{'item_id':i,'order_mode':'portions','portions':n,'price':800} for i,n in [(self.product,10),(a,5),(b,15)]]})
        self.assertEqual([l['qty'] for l in self.state()['order_lines'] if l['order_id']==oid],[2,1,3])
        plan=self.store.plan(self.future[:10]);self.assertEqual(sorted(r['remaining_portions'] for r in plan['products']),[5,10,15])
        self.assertAlmostEqual(sum(r['produce'] for r in plan['products']),6)
    def test_invoice_charges_actual_weight_above_estimate_tolerance(self):
        self.configure();self.receipt(qty=30);self.produce(qty=10);oid=self.order()
        sid=self.shipment(oid,2.6,10);d=self.doc(sid)
        self.assertEqual(d['total'],2080);self.assertEqual(d['data']['lines'][0]['portions'],10)
        self.assertEqual(next(o for o in self.state()['orders'] if o['id']==oid)['status'],'completed')
        self.assertEqual(self.store.plan(self.future[:10])['products'][0]['produce'],0)
        html=invoice_html(d).decode();self.assertIn('10 порц.',html);self.assertIn('2,6',html);self.assertIn('2 080,00',html)
    def test_partial_portions_drive_remaining_not_actual_kg(self):
        self.configure();self.receipt(qty=30);self.produce(qty=10);oid=self.order()
        first=self.shipment(oid,1.3,4);line=next(l for l in self.state()['order_lines'] if l['order_id']==oid)
        self.assertEqual(remaining_portions(line),6);self.assertAlmostEqual(remaining_quantity(line),1.2)
        allocation=next(a for a in self.state()['allocations'] if a['order_id']==oid)
        self.assertAlmostEqual(allocation['remaining'],1.2);self.assertEqual(allocation['remaining_portions'],6)
        second=self.shipment(oid,1.7,6);self.assertEqual(next(o for o in self.state()['orders'] if o['id']==oid)['status'],'completed')
        self.store.reverse(dict(self.base,document_id=second,reason='Ошибка'))
        line=next(l for l in self.state()['order_lines'] if l['order_id']==oid);self.assertEqual(line['shipped_portions'],4);self.assertAlmostEqual(line['shipped'],1.3)
        self.store.reverse(dict(self.base,document_id=first,reason='Ошибка'))
        line=next(l for l in self.state()['order_lines'] if l['order_id']==oid);self.assertEqual(line['shipped_portions'],0);self.assertAlmostEqual(line['shipped'],0)
    def test_underweight_full_portion_delivery_completes_order(self):
        self.configure();self.receipt();self.produce(qty=3);oid=self.order();self.shipment(oid,1.5,10)
        self.assertFalse(any(a['order_id']==oid for a in self.state()['allocations']))
        self.assertEqual(next(o for o in self.state()['orders'] if o['id']==oid)['status'],'completed')
    def test_missing_weight_fractional_portions_and_pack_mode_rejected(self):
        before=self.state()
        with self.assertRaises(ValidationError):self.order()
        self.assertEqual(before,self.state());self.configure()
        for count in (0,1.5,-1):
            with self.assertRaises(ValidationError):self.order(count)
        pack=self.store.save_item({'name':'Палочки','kind':'finished','unit':'уп.','portion_weight':.2})
        with self.assertRaises(ValidationError):self.order(5,pack)
    def test_invalid_partial_shipping_rolls_back_count_and_weight(self):
        self.configure();self.receipt();self.produce(qty=3);oid=self.order();before=self.state()
        for kg,count in [(1,11),(1,1.5),(1,0),(0,1)]:
            with self.assertRaises(ValidationError):self.shipment(oid,kg,count)
            self.assertEqual(before,self.state())
    def test_portion_weight_snapshot_and_summary_remaining(self):
        self.configure();self.receipt(qty=30);self.produce(qty=10);oid=self.order();self.shipment(oid,1.3,4)
        self.configure(.25);line=next(l for l in self.state()['order_lines'] if l['order_id']==oid)
        self.assertEqual(line['portion_weight'],.2)
        other=self.order(5);sid=self.store.dispatch_summary(dict(self.base,order_ids=[oid,other]))
        data=self.doc(sid)['data'];self.assertEqual(data['lines'][0]['portions'],11);self.assertAlmostEqual(data['lines'][0]['qty'],2.45)
        self.assertEqual(data['orders'][0]['lines'][0]['portions'],6)
    def test_v3_migration_preserves_existing_data_and_creates_backup(self):
        self.receipt();self.produce();oid=self.store.save_order({'client_id':self.client,'manager':'М','due':self.future,'lines':[{'item_id':self.product,'qty':2,'price':800}]})
        before=self.state()
        with self.store.transaction() as c:
            c.execute('ALTER TABLE partners DROP COLUMN price_list_id')
            c.execute('ALTER TABLE orders DROP COLUMN price_list_id')
            c.execute('ALTER TABLE order_lines DROP COLUMN price_origin')
            c.execute('DROP TABLE price_values');c.execute('DROP TABLE price_history');c.execute('DROP TABLE price_lists')
            c.execute('ALTER TABLE items DROP COLUMN portion_weight')
            for col in ('order_mode','portions','shipped_portions','portion_weight'):c.execute('ALTER TABLE order_lines DROP COLUMN '+col)
            c.execute('PRAGMA user_version=3')
        self.store=Store(self.store.path);after=self.state()
        for key in ('lots','documents','orders'):self.assertEqual(before[key],after[key])
        line=next(l for l in after['order_lines'] if l['order_id']==oid);self.assertEqual(line['qty'],2);self.assertEqual(line['order_mode'],'quantity');self.assertEqual(line['portions'],0)
        self.assertEqual(after['schema_version'],SCHEMA_VERSION)
        backups=list((self.store.path.parent/'backups').glob('before-v4-*'));self.assertTrue(any(self.schema(b)==3 for b in backups))
        self.assertEqual(Store(self.store.path).snapshot(),after)
    @staticmethod
    def schema(path):
        with closing(sqlite3.connect(path)) as c:return c.execute('PRAGMA user_version').fetchone()[0]
    def test_expense_purpose_structure_and_cash_vs_profit(self):
        self.configure(rate=40);employee=self.store.save_employee({'name':'Повар'});self.receipt();self.produce(qty=2,workers=[{'employee_id':employee,'qty':2}]);oid=self.order(5);sid=self.shipment(oid,1,5)
        expenses=[]
        for cat,purpose,amount in [('Логистика','Доставка заказов',100),('Логистика','Топливо',50),('Аренда','Аренда цеха',200)]:expenses.append(self.store.money(dict(self.base,kind='expense',category=cat,purpose=purpose,recipient='Получатель',amount=amount)))
        self.store.money(dict(self.base,kind='expense',category='Зарплата',purpose='Выплата повару',employee_id=employee,amount=50))
        self.store.money(dict(self.base,kind='payment',partner_id=self.client,amount=400))
        f=self.store.financial_report(self.base['date'],self.base['date'])
        self.assertEqual(f['cash_expenses'],400);self.assertEqual(f['expenses'],350);self.assertEqual(f['payroll_paid'],50)
        self.assertEqual(next(r for r in f['categories'] if r['category']=='Логистика')['cash'],150)
        self.assertAlmostEqual(f['result'],800-self.doc(sid)['cost']-350,places=2);self.assertEqual(f['cash_change'],0)
        self.assertEqual(next(r for r in f['details'] if r['id']==expenses[0])['purpose'],'Доставка заказов')
        _,rows=document_table(self.doc(expenses[0]));self.assertIn(['На что потрачено','Доставка заказов'],rows)
        self.store.delete_document(dict(document_id=expenses[1],responsible='Р',reason='Ошибка'))
        self.assertEqual(self.store.financial_report(self.base['date'],self.base['date'])['cash_expenses'],350)
    def test_financial_period_filter_and_invalid_range(self):
        yesterday=(self.now-timedelta(days=1)).date().isoformat()
        self.store.money(dict(self.base,date=yesterday,kind='expense',category='Аренда',purpose='Вчера',amount=100))
        self.assertEqual(self.store.financial_report(self.base['date'],self.base['date'])['cash_expenses'],0)
        with self.assertRaises(ValidationError):self.store.financial_report(self.base['date'],yesterday)

class PortionFinanceHttpTests(unittest.TestCase):
    import test_accounting as fixtures
    setUp=fixtures.HttpTests.setUp
    tearDown=fixtures.HttpTests.tearDown
    request=fixtures.HttpTests.request
    def test_financial_exports_all_views_and_formats(self):
        import io,zipfile
        from demo import create_demo
        create_demo(self.server.store.path)
        self.request('/api/money',{'kind':'expense','date':datetime.now().date().isoformat(),'responsible':'Р','category':'Топливо','purpose':'Заправка автомобиля','amount':100})
        f=json.loads(self.request('/api/financial')[1]);self.assertTrue(any(r['purpose']=='Заправка автомобиля' for r in f['details']))
        for view in ('total','category','detail'):
            for fmt in ('html','csv','json','xlsx','docx'):
                _,raw,_=self.request(f'/api/export?type=financial&view={view}&format={fmt}')
                if fmt in ('xlsx','docx'):
                    with zipfile.ZipFile(io.BytesIO(raw)) as z:self.assertIsNone(z.testzip())
                elif fmt=='html':self.assertIn('Финансовые итоги',raw.decode())
        raw=json.loads(self.request('/api/export?type=financial&view=total&format=json')[1]);self.assertNotIn('Заправка автомобиля',str(raw))
        raw=json.loads(self.request('/api/export?type=financial&view=detail&format=json')[1]);self.assertIn('Заправка автомобиля',str(raw))


class LegacyPortionRestoreTests(unittest.TestCase):
    import test_accounting as fixtures
    setUp=fixtures.HttpTests.setUp
    tearDown=fixtures.HttpTests.tearDown
    request=fixtures.HttpTests.request
    def test_restore_genuine_v3_copy_preserves_data_and_source(self):
        import base64,hashlib
        from demo import create_demo
        store=create_demo(Path(self.tmp.name)/'old.sqlite3');before=store.snapshot()
        with store.transaction() as c:
            c.execute('ALTER TABLE partners DROP COLUMN price_list_id')
            c.execute('ALTER TABLE orders DROP COLUMN price_list_id')
            c.execute('ALTER TABLE order_lines DROP COLUMN price_origin')
            c.execute('DROP TABLE price_values');c.execute('DROP TABLE price_history');c.execute('DROP TABLE price_lists')
            c.execute('ALTER TABLE items DROP COLUMN portion_weight')
            for col in ('order_mode','portions','shipped_portions','portion_weight'):c.execute('ALTER TABLE order_lines DROP COLUMN '+col)
            c.execute('PRAGMA user_version=3')
        with closing(store.connect()) as c:c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        source=store.path.read_bytes();digest=hashlib.sha256(source).hexdigest();encoded=base64.b64encode(source).decode()
        preview=json.loads(self.request('/api/restore-preview',{'file':encoded})[1]);self.assertEqual(preview['schema_version'],3)
        self.request('/api/restore',{'file':encoded,'expected_sha256':preview['sha256'],'confirm':'ВОССТАНОВИТЬ'})
        state=json.loads(self.request('/api/state')[1]);self.assertEqual(state['schema_version'],SCHEMA_VERSION)
        for key in ('documents','lots','orders'):self.assertEqual(state[key],before[key])
        self.assertTrue(all(l['order_mode']=='quantity' and l['portions']==0 for l in state['order_lines']))
        self.assertEqual(hashlib.sha256(store.path.read_bytes()).hexdigest(),digest)

if __name__=='__main__':unittest.main()
