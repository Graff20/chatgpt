"""Release 0.3: piecework, allocations, exchanges, migration and print forms."""
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import test_accounting as fixtures
from domain import Store, ValidationError, SCHEMA_VERSION
from app import Handler
from exports import invoice_html, receipt_html

class WorkflowTests(unittest.TestCase):
    setUp=fixtures.AccountingTests.setUp
    tearDown=fixtures.AccountingTests.tearDown
    item=fixtures.AccountingTests.item
    recipe=fixtures.AccountingTests.recipe
    receipt=fixtures.AccountingTests.receipt
    produce=fixtures.AccountingTests.produce
    state=fixtures.AccountingTests.state
    def order(self,qty=1,**kw):
        d=dict(client_id=self.client,due=self.future,manager='Менеджер',lines=[{'item_id':self.product,'qty':qty,'price':800}]);d.update(kw)
        return self.store.save_order(d)
    shipment=fixtures.AccountingTests.shipment

    def priced_product(self,rate=40,**extra):
        self.store.save_item(dict(id=self.product,name='Филе сувид',kind='finished',unit='кг',shelf_days=14,labor_rate=rate,**extra))
        return self.store.save_employee({'name':'Иванов Сергей'})

    def test_piecework_reprices_cost_and_preserves_audit(self):
        a=self.priced_product(overhead_rate=5,overhead_percent=10)
        b=self.store.save_employee({'name':'Петрова Анна'})
        self.receipt();ident=self.produce(qty=2,workers=[{'employee_id':a,'qty':1.2},{'employee_id':b,'qty':.8}])
        doc=next(d for d in self.state()['documents'] if d['id']==ident)
        self.assertEqual(doc['data']['labor_cost'],80)
        self.assertEqual([w['amount'] for w in doc['data']['workers']],[48,32])
        self.assertAlmostEqual(doc['cost'],doc['data']['materials_cost']+80+doc['data']['overhead_cost'])
        self.store.save_item(dict(id=self.product,name='Филе сувид',kind='finished',unit='кг',labor_rate=100))
        self.store.save_employee({'id':a,'name':'Изменённое имя'})
        work=self.store.payroll(self.base['date'],self.base['date'])
        self.assertEqual(work[0]['rate'],100);self.assertEqual(work[0]['name'],'Иванов Сергей')
        self.assertEqual(sum(w['amount'] for w in work),200)
        updated=next(d for d in self.state()['documents'] if d['id']==ident)
        self.assertEqual(updated['data']['cost_history'][-1]['previous_labor_rate'],40)

    def test_workers_invalid_distribution_rolls_back(self):
        a=self.priced_product();self.receipt();before=self.state()
        for workers in ([],[{'employee_id':a,'qty':.5}],[{'employee_id':a,'qty':.5},{'employee_id':a,'qty':.5}]):
            with self.assertRaises(ValidationError):self.produce(workers=workers)
            self.assertEqual(before,self.state())

    def test_salary_payment_is_not_second_production_cost(self):
        a=self.priced_product();self.receipt();self.produce(workers=[{'employee_id':a,'qty':1}])
        ident=self.store.money(dict(self.base,kind='expense',category='Зарплата',employee_id=a,amount=35))
        doc=next(d for d in self.state()['documents'] if d['id']==ident)
        self.assertTrue(doc['data']['already_in_cost'])
        before=self.state()
        with self.assertRaises(ValidationError):self.store.money(dict(self.base,kind='expense',category='Зарплата',employee_id=a,amount=6))
        self.assertEqual(before,self.state())

    def test_exchange_keeps_debt_and_quarantines_return(self):
        self.receipt();self.produce(qty=3);sid=self.shipment(self.order(),1)
        before=self.state();client=next(p for p in before['partners'] if p['id']==self.client)
        doc=next(d for d in before['documents'] if d['id']==sid)
        eid=self.store.exchange(dict(self.base,shipment_id=sid,reason='Брак',lines=[{'line_id':doc['data']['lines'][0]['line_id'],'qty':.2}],replacements=[{'item_id':self.product,'qty':.2}]))
        after=self.state();self.assertEqual(next(p for p in after['partners'] if p['id']==self.client)['balance'],client['balance'])
        exchange=next(d for d in after['documents'] if d['id']==eid)
        self.assertEqual(exchange['total'],0)
        self.assertAlmostEqual(sum(l['qty'] for l in after['lots'] if l['blocked']),.2)
        with self.assertRaises(ValidationError):self.store.customer_return(dict(self.base,shipment_id=sid,reason='Повтор',lines=[{'line_id':doc['data']['lines'][0]['line_id'],'qty':1}]))
        with self.assertRaises(ValidationError):self.store.reverse(dict(self.base,document_id=sid,reason='Отмена'))
        self.store.reverse(dict(self.base,document_id=eid,reason='Ошибка обмена'))
        self.assertAlmostEqual(next(i for i in self.state()['items'] if i['id']==self.product)['available'],2)

    def test_return_after_partial_exchange_credits_only_returned_share(self):
        self.receipt();self.produce(qty=3);sid=self.shipment(self.order(),1)
        doc=next(d for d in self.state()['documents'] if d['id']==sid);line=doc['data']['lines'][0]
        self.store.exchange(dict(self.base,shipment_id=sid,reason='Обмен',lines=[{'line_id':line['line_id'],'qty':.2}],replacements=[{'item_id':self.product,'qty':.2}]))
        returned=self.store.customer_return(dict(self.base,shipment_id=sid,reason='Возврат остатка',lines=[{'line_id':line['line_id'],'qty':.8}]))
        self.assertEqual(next(d for d in self.state()['documents'] if d['id']==returned)['total'],640)
        self.assertEqual(next(p for p in self.state()['partners'] if p['id']==self.client)['balance'],160)

    def test_exchange_shortage_rolls_back_both_sides(self):
        self.receipt();self.produce();sid=self.shipment(self.order(),1)
        doc=next(d for d in self.state()['documents'] if d['id']==sid);before=self.state()
        with self.assertRaises(ValidationError):self.store.exchange(dict(self.base,shipment_id=sid,reason='Замена',lines=[{'line_id':doc['data']['lines'][0]['line_id'],'qty':1}],replacements=[{'item_id':self.product,'qty':1}]))
        self.assertEqual(before,self.state())

    def test_refund_cannot_exceed_advance(self):
        self.store.money(dict(self.base,kind='payment',partner_id=self.client,amount=100))
        self.store.refund(dict(self.base,partner_id=self.client,amount=60,reason='Возврат аванса'))
        self.assertEqual(next(p for p in self.state()['partners'] if p['id']==self.client)['balance'],-40)
        with self.assertRaises(ValidationError):self.store.refund(dict(self.base,partner_id=self.client,amount=41,reason='Возврат'))

    def test_allocations_use_each_lot_once_and_exact_delivery_time(self):
        self.receipt();self.produce(qty=3,expires=self.future)
        due=(self.now+timedelta(days=1)).isoformat(timespec='minutes')
        first=self.order(qty=2,due=due);second=self.order(qty=2,due=due)
        allocations=self.state()['allocations']
        self.assertEqual([a['covered'] for a in allocations],[2,1])
        self.assertEqual(sum(a['covered'] for a in allocations),3)
        with self.assertRaises(ValidationError):self.shipment(second,2)
        self.shipment(first,2)
        plan=self.store.plan(due[:10]);self.assertEqual(plan['products'][0]['shortage'],1)
        self.assertEqual(plan['products'][0]['shipped'],2)

    def test_later_order_cannot_steal_reserved_lot_and_can_use_next(self):
        self.receipt();self.produce(qty=1,expires=self.future);self.produce(qty=1,expires=(self.now+timedelta(days=10)).isoformat(timespec='minutes'))
        first=self.order(due=(self.now+timedelta(days=1)).isoformat(timespec='minutes'))
        second=self.order(due=(self.now+timedelta(days=2)).isoformat(timespec='minutes'))
        sid=self.shipment(second,1)
        doc=next(d for d in self.state()['documents'] if d['id']==sid)
        self.assertEqual(doc['data']['lines'][0]['lots'][0]['expires'],(self.now+timedelta(days=10)).isoformat(timespec='minutes'))
        self.shipment(first,1)

    def test_job_freezes_plan_and_marks_changes(self):
        self.priced_product(rate=0,production_buffer=10)
        order=self.order(qty=10)
        jid=self.store.save_job(dict(self.base,date=self.future[:10]))
        self.store.save_order(dict(id=order,client_id=self.client,due=self.future,manager='М',lines=[{'item_id':self.product,'qty':12,'price':800}]))
        plan=self.store.plan(self.future[:10]);progress=plan['jobs'][0]['progress'][0]
        self.assertEqual(progress['qty'],11);self.assertAlmostEqual(progress['change'],2.2)
        with self.assertRaises(ValidationError):self.store.save_job(dict(self.base,date=self.future[:10]))
        self.receipt(qty=30);self.produce(qty=2,expires=(self.now+timedelta(days=14)).isoformat(timespec='minutes'),job_id=jid)
        progress=self.store.plan(self.future[:10])['jobs'][0]['progress'][0];self.assertEqual(progress['produced'],2)
        self.store.save_job(dict(self.base,id=jid,action='close',date=self.future[:10]))
        with self.assertRaises(ValidationError):self.produce(job_id=jid)

    def test_logistics_allocated_once_and_salary_excluded(self):
        self.receipt();self.produce(qty=3);one=self.shipment(self.order(),1);two=self.shipment(self.order(),1)
        eid=self.store.money(dict(self.base,kind='expense',category='Логистика',amount=300,allocation_date=self.base['date']))
        expense=next(d for d in self.state()['documents'] if d['id']==eid)
        self.assertEqual(expense['data']['allocations'],[{'document_id':one,'amount':150},{'document_id':two,'amount':150}])
        _,rows=Handler.export_rows(SimpleNamespace(store=self.store),self.state(),'fullcost',{})
        self.assertEqual(rows[1][5],150)
        with self.assertRaises(ValidationError):self.store.reverse(dict(self.base,document_id=one,reason='Отмена'))

    def test_accept_job_changes_keeps_history_and_new_product(self):
        self.order(qty=2)
        jid=self.store.save_job(dict(self.base,date=self.future[:10]))
        other=self.item('Десерт','finished',shelf_days=14)
        self.recipe(other,[{'item_id':self.raw,'net':1}])
        self.store.save_order({'client_id':self.client,'manager':'М','due':self.future,'lines':[{'item_id':other,'qty':3,'price':100}]})
        progress=self.store.plan(self.future[:10])['jobs'][0]['progress']
        self.assertEqual(next(p for p in progress if p['item_id']==other)['change'],3)
        self.store.save_job(dict(self.base,date=self.future[:10],id=jid,action='review'))
        job=self.store.plan(self.future[:10])['jobs'][0]
        self.assertEqual(len(job['revisions']),1)
        self.assertTrue(any(p['item_id']==other for p in job['products']))
        self.assertTrue(all(abs(p['change'])<1e-6 for p in job['progress']))
        _,old_rows=Handler.export_rows(SimpleNamespace(store=self.store),self.state(),'job',{'id':[str(jid)],'revision':['1']})
        _,new_rows=Handler.export_rows(SimpleNamespace(store=self.store),self.state(),'job',{'id':[str(jid)]})
        self.assertFalse(any('Десерт' in row for row in old_rows))
        self.assertTrue(any('Десерт' in row for row in new_rows))

    def test_production_cannot_be_reversed_after_salary_payment(self):
        a=self.priced_product();self.receipt()
        production=self.produce(workers=[{'employee_id':a,'qty':1}])
        payment=self.store.money(dict(self.base,kind='expense',category='Зарплата',employee_id=a,amount=40))
        with self.assertRaises(ValidationError):self.store.reverse(dict(self.base,document_id=production,reason='Отмена'))
        self.store.reverse(dict(self.base,document_id=payment,reason='Ошибочная выплата'))
        self.store.reverse(dict(self.base,document_id=production,reason='Отмена'))
        self.assertEqual(self.store.payroll(self.base['date'],self.base['date']),[])

    def test_v2_upgrade_preserves_lots_orders_docs_and_backup(self):
        self.receipt();self.produce();self.order();before=self.state();path=self.store.path
        with closing(self.store.connect()) as c:
            c.execute('ALTER TABLE items DROP COLUMN supply_type')
            c.execute('ALTER TABLE partners DROP COLUMN price_list_id')
            c.execute('ALTER TABLE orders DROP COLUMN price_list_id')
            c.execute('ALTER TABLE order_lines DROP COLUMN price_origin')
            c.execute('DROP TABLE price_values');c.execute('DROP TABLE price_history');c.execute('DROP TABLE price_lists')
            c.execute('DROP TABLE employees');c.execute('DROP TABLE production_jobs')
            for col in ('labor_rate','overhead_rate','overhead_percent','production_buffer','portion_weight'):c.execute('ALTER TABLE items DROP COLUMN '+col)
            for col in ('order_mode','portions','shipped_portions','portion_weight'):c.execute('ALTER TABLE order_lines DROP COLUMN '+col)
            c.execute('ALTER TABLE recipes DROP COLUMN author');c.execute('PRAGMA user_version=2');c.commit()
        upgraded=Store(path);after=upgraded.snapshot()
        for line in before['order_lines']:line['price_origin']='saved'
        for key in ('lots','documents','orders','order_lines'):self.assertEqual(before[key],after[key])
        backups=list((path.parent/'backups').glob('before-v3-*'));self.assertTrue(backups)
        self.assertTrue(any(sqlite3.connect(b).execute('PRAGMA user_version').fetchone()[0]==2 for b in backups))
        self.assertEqual(Store(path).snapshot(),after);self.assertEqual(after['schema_version'],SCHEMA_VERSION)

    def test_print_invoice_and_blind_inventory(self):
        self.receipt();self.produce(qty=2);sid=self.shipment(self.order(),1,discount_percent=5)
        doc=next(d for d in self.state()['documents'] if d['id']==sid);html=invoice_html(doc).decode()
        self.assertIn('Счёт №',html);self.assertIn('class="totals"',html)
        self.assertLess(html.index('Скидка 5%'),html.index('ИТОГО'))
        _,rows=Handler.export_rows(SimpleNamespace(store=self.store),self.state(),'inventory_blank',{})
        self.assertNotIn('Учёт',str(rows));self.assertIn('Факт',str(rows))
        receipt=next(d for d in self.state()['documents'] if d['kind']=='receipt')
        self.assertIn('Сведения о партиях',receipt_html(receipt).decode())


class WorkflowHttpTests(unittest.TestCase):
    setUp=fixtures.HttpTests.setUp
    tearDown=fixtures.HttpTests.tearDown
    request=fixtures.HttpTests.request

    def test_new_print_forms_and_exports_and_v3_restore(self):
        import io,base64,zipfile
        from demo import create_demo
        store=create_demo(self.server.store.path)
        state=store.snapshot();employee=self.request('/api/employees',{'name':'Повар'})
        for typ in ('inventory_blank','inventory_empty','payroll','fullcost','recipe','calculation'):
            args='&id='+str(state['recipes'][0]['id']) if typ in ('recipe','calculation') else ''
            for fmt in ('html','json','csv','xlsx','docx'):
                _,raw,_=self.request('/api/export?type='+typ+'&format='+fmt+args)
                if fmt in ('xlsx','docx'):
                    with zipfile.ZipFile(io.BytesIO(raw)) as z:self.assertIsNone(z.testzip())
                elif fmt=='html':self.assertIn('<table',raw.decode())
        with closing(store.connect()) as c:
            backup=Path(self.tmp.name)/'v3-copy.sqlite3'
            with closing(sqlite3.connect(backup)) as target:c.backup(target)
        original=backup.read_bytes()
        encoded=base64.b64encode(original).decode()
        preview=json.loads(self.request('/api/restore-preview',{'file':encoded})[1])
        self.assertEqual(preview['schema_version'],SCHEMA_VERSION);self.assertEqual(preview['counts']['employees'],1)
        self.request('/api/restore',{'file':encoded,'confirm':'ВОССТАНОВИТЬ','expected_sha256':preview['sha256']})
        restored=self.server.store.snapshot()
        self.assertEqual(len(restored['items']),len(state['items']));self.assertEqual(len(restored['employees']),1)
        self.assertEqual(backup.read_bytes(),original)

if __name__=='__main__':unittest.main()
