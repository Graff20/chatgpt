"""Regression tests for dependent valuation, removal, and selected-order summaries."""
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
import test_workflow as fixtures
from domain import Store,ValidationError
from exports import document_table

class AdjustmentTests(unittest.TestCase):
    setUp=fixtures.WorkflowTests.setUp
    tearDown=fixtures.WorkflowTests.tearDown
    item=fixtures.WorkflowTests.item
    recipe=fixtures.WorkflowTests.recipe
    receipt=fixtures.WorkflowTests.receipt
    produce=fixtures.WorkflowTests.produce
    state=fixtures.WorkflowTests.state
    order=fixtures.WorkflowTests.order
    shipment=fixtures.WorkflowTests.shipment
    def employee(self):return self.store.save_employee({'name':'Повар'})
    def change_rate(self,rate,**extra):
        return self.store.save_item(dict(id=self.product,name='Филе сувид',kind='finished',unit='кг',labor_rate=rate,shelf_days=14,modified_by='Руководитель',**extra))
    def doc(self,ident):return next(d for d in self.state()['documents'] if d['id']==ident)

    def test_zero_rate_then_card_change_accrues_assigned_workers(self):
        employee=self.employee();self.receipt();production=self.produce(qty=2,workers=[{'employee_id':employee,'qty':2}])
        before=self.state();self.assertEqual(self.doc(production)['data']['labor_cost'],0)
        self.change_rate(45)
        self.assertEqual(self.doc(production)['data']['labor_cost'],90)
        self.assertEqual(self.store.payroll(self.base['date'],self.base['date'])[0]['amount'],90)
        after=self.state()
        self.assertEqual([(l['id'],l['qty'],l['expires']) for l in before['lots']],[(l['id'],l['qty'],l['expires']) for l in after['lots']])
        with closing(self.store.connect()) as c:
            for m in c.execute('SELECT * FROM movements'):
                self.assertEqual(m['unit_cost'],c.execute('SELECT unit_cost FROM lots WHERE id=?',(m['lot_id'],)).fetchone()[0])

    def test_recalculation_updates_descendants_and_keeps_customer_money(self):
        employee=self.employee();self.receipt();production=self.produce(qty=3,workers=[{'employee_id':employee,'qty':3}])
        sid=self.shipment(self.order(),1,discount_percent=10);line=self.doc(sid)['data']['lines'][0]
        returned=self.store.customer_return(dict(self.base,shipment_id=sid,reason='Возврат',lines=[{'line_id':line['line_id'],'qty':.2}]))
        # Emulate a 0.3 return without the new explicit source link.
        with self.store.transaction() as c:
            data=json.loads(c.execute('SELECT data FROM documents WHERE id=?',(returned,)).fetchone()[0]);data['lines'][0]['lots'][0].pop('source_lot_id',None)
            c.execute('UPDATE documents SET data=? WHERE id=?',(json.dumps(data),returned))
        before=self.state();old_cost=self.doc(sid)['cost'];old_return=self.doc(returned)['cost']
        self.change_rate(40)
        self.assertAlmostEqual(self.doc(sid)['cost'],old_cost+40)
        self.assertAlmostEqual(self.doc(returned)['cost'],old_return+8)
        self.assertEqual(self.doc(sid)['total'],720)
        self.assertEqual(self.doc(returned)['total'],144)
        self.assertEqual(before['debts'],self.state()['debts'])
        self.assertIn('source_lot_id',self.doc(returned)['data']['lines'][0]['lots'][0])

    def test_nested_production_cost_propagation(self):
        employee=self.employee();self.receipt();self.produce(qty=3,workers=[{'employee_id':employee,'qty':3}])
        child=self.store.save_item({'name':'Набор филе','kind':'finished','unit':'уп.'})
        self.recipe(child,[{'item_id':self.product,'net':1}])
        child_doc=self.store.production(dict(self.base,item_id=child,qty=1,produced=self.now.isoformat(timespec='minutes')))
        before=self.doc(child_doc)['cost'];self.change_rate(50)
        self.assertAlmostEqual(self.doc(child_doc)['cost'],before+50)
        self.assertAlmostEqual(self.doc(child_doc)['data']['inputs'][0]['lots'][0]['unit_cost'],self.doc(child_doc)['cost'])

    def test_exchange_writeoff_and_inventory_valuations_follow_source(self):
        employee=self.employee();self.receipt();self.produce(qty=4,workers=[{'employee_id':employee,'qty':4}]);sid=self.shipment(self.order(),1)
        exchange=self.store.exchange(dict(self.base,shipment_id=sid,reason='Обмен',lines=[{'line_id':self.doc(sid)['data']['lines'][0]['line_id'],'qty':.2}],replacements=[{'item_id':self.product,'qty':.2}]))
        returned_lot=next(l for l in self.state()['lots'] if l['blocked'])
        writeoff=self.store.writeoff(dict(self.base,lot_id=returned_lot['id'],qty=.1,reason='Брак'))
        remaining=next(l for l in self.state()['lots'] if l['item_id']==self.product and not l['blocked'])
        inv=self.store.inventory(dict(self.base,lines=[{'lot_id':remaining['id'],'expected':remaining['qty'],'actual':remaining['qty']-.1,'reason':'Недостача'}]))
        old_w=self.doc(writeoff)['cost'];old_i=self.doc(inv)['cost'];self.change_rate(30)
        self.assertAlmostEqual(self.doc(writeoff)['cost'],old_w+3)
        self.assertAlmostEqual(self.doc(inv)['cost'],old_i-3)
        self.assertAlmostEqual(self.doc(exchange)['cost'],0)
        self.assertEqual(self.doc(exchange)['total'],0)

    def test_startup_repairs_existing_zero_accrual_once_with_backup(self):
        employee=self.employee();self.receipt();production=self.produce(workers=[{'employee_id':employee,'qty':1}])
        with self.store.transaction() as c:
            c.execute('UPDATE items SET labor_rate=70 WHERE id=?',(self.product,));c.execute("DELETE FROM settings WHERE key='cost_dependencies_v1'")
        self.store=Store(self.store.path)
        self.assertEqual(self.doc(production)['data']['labor_cost'],70)
        backups=list((self.store.path.parent/'backups').glob('before-cost-recalculation-*'));self.assertTrue(backups)
        before=self.state();Store(self.store.path);self.assertEqual(before,self.state())
        with closing(sqlite3.connect(backups[-1])) as c:self.assertEqual(json.loads(c.execute('SELECT data FROM documents WHERE id=?',(production,)).fetchone()[0])['labor_cost'],0)

    def test_missing_workers_are_visible_and_can_be_assigned_later(self):
        self.receipt();production=self.produce();self.change_rate(60)
        self.assertTrue(self.doc(production)['data']['pending_workers'])
        employee=self.employee();self.store.update_production_workers(dict(document_id=production,responsible='Руководитель',reason='Указали исполнителя',workers=[{'employee_id':employee,'qty':1}]))
        self.assertNotIn('pending_workers',self.doc(production)['data'])
        self.assertEqual(self.doc(production)['data']['workers'][0]['amount'],60)
        self.assertTrue(self.doc(production)['data']['worker_history'])

    def test_invalid_worker_edit_is_atomic_and_existing_archived_worker_allowed(self):
        employee=self.employee();self.receipt();production=self.produce(workers=[{'employee_id':employee,'qty':1}]);before=self.state()
        with self.assertRaises(ValidationError):self.store.update_production_workers(dict(document_id=production,responsible='Р',reason='Ошибка',workers=[{'employee_id':employee,'qty':2}]))
        self.assertEqual(before,self.state())
        self.store.save_employee({'id':employee,'name':'Повар','active':False})
        self.store.update_production_workers(dict(document_id=production,responsible='Р',reason='Уточнение',workers=[{'employee_id':employee,'qty':1}]))

    def test_delete_shipment_restores_stock_order_and_money(self):
        self.receipt();self.produce(qty=2);order=self.order();sid=self.shipment(order,1)
        self.store.delete_document(dict(document_id=sid,responsible='Р',reason='Ошибка'))
        state=self.state();self.assertTrue(self.doc(sid)['data']['deleted']);self.assertTrue(self.doc(sid)['reversed'])
        self.assertEqual(next(i for i in state['items'] if i['id']==self.product)['available'],2)
        self.assertEqual(next(o for o in state['orders'] if o['id']==order)['status'],'open')
        self.assertEqual(next(p for p in state['partners'] if p['id']==self.client)['balance'],0)
        with self.assertRaises(ValidationError):self.store.delete_document(dict(document_id=sid,responsible='Р',reason='Ещё раз'))

    def test_deleting_dependency_is_blocked_and_existing_reversal_not_repeated(self):
        self.receipt();self.produce();production=next(d for d in self.state()['documents'] if d['kind']=='production')['id'];self.shipment(self.order(),1)
        before=self.state()
        with self.assertRaises(ValidationError):self.store.delete_document(dict(document_id=production,responsible='Р',reason='Ошибка'))
        self.assertEqual(before,self.state())
        expense=self.store.money(dict(self.base,kind='expense',category='Логистика',amount=100))
        self.store.reverse(dict(self.base,document_id=expense,reason='Отмена'))
        count=len(self.state()['documents']);self.store.delete_document(dict(document_id=expense,responsible='Р',reason='Скрыть'))
        self.assertEqual(len(self.state()['documents']),count)

    def test_delete_release_restores_quarantine_and_order_deletion_removes_demand(self):
        self.receipt();self.produce(qty=2);order=self.order();sid=self.shipment(order,1)
        self.store.customer_return(dict(self.base,shipment_id=sid,reason='Возврат',lines=[{'line_id':self.doc(sid)['data']['lines'][0]['line_id'],'qty':.2}]))
        lot=next(l for l in self.state()['lots'] if l['blocked']);release=self.store.release_lot(dict(self.base,lot_id=lot['id'],reason='Проверили'))
        self.store.delete_document(dict(document_id=release,responsible='Р',reason='Ошибочный допуск'))
        self.assertTrue(next(l for l in self.state()['lots'] if l['id']==lot['id'])['blocked'])
        other=self.order();self.store.delete_order({'id':other,'responsible':'Р','reason':'Дубль'})
        self.assertFalse(any(a['order_id']==other for a in self.state()['allocations']))
        with self.assertRaises(ValidationError):self.store.delete_order({'id':order,'responsible':'Р','reason':'Ошибка'})

    def test_summary_sums_only_selected_remaining_quantities_and_is_snapshot(self):
        self.receipt();self.produce(qty=5);a=self.order(qty=2);b=self.order(qty=3);self.order(qty=99);self.shipment(a,1)
        before=self.state();ident=self.store.dispatch_summary(dict(self.base,order_ids=[a,b]));data=self.doc(ident)['data']
        self.assertEqual(data['lines'][0]['qty'],4);self.assertEqual(len(data['orders']),2)
        self.assertEqual(before['lots'],self.state()['lots']);self.assertEqual(before['order_lines'],self.state()['order_lines'])
        self.store.save_order({'id':b,'client_id':self.client,'manager':'М','due':self.future,'lines':[{'item_id':self.product,'qty':1,'price':800}]})
        self.assertEqual(self.doc(ident)['data'],data)
        title,rows=document_table(self.doc(ident));self.assertIn('Сводная ведомость',title);self.assertIn('План к отгрузке',str(rows))

    def test_summary_full_quantities_separates_units_and_invalid_is_atomic(self):
        pack=self.store.save_item({'name':'Палочки','kind':'finished','unit':'уп.'})
        order=self.store.save_order({'client_id':self.client,'manager':'М','due':self.future,'lines':[{'item_id':self.product,'qty':2,'price':800},{'item_id':pack,'qty':3,'price':100}]})
        ident=self.store.dispatch_summary(dict(self.base,order_ids=[order],mode='ordered'))
        self.assertEqual({(l['unit'],l['qty']) for l in self.doc(ident)['data']['lines']},{('кг',2),('уп.',3)})
        before=self.state()
        for ids in ([],[order,order],[999]):
            with self.assertRaises(ValidationError):self.store.dispatch_summary(dict(self.base,order_ids=ids))
        self.assertEqual(before,self.state())


class AdjustmentHttpTests(unittest.TestCase):
    import test_accounting as accounting
    setUp=accounting.HttpTests.setUp
    tearDown=accounting.HttpTests.tearDown
    request=accounting.HttpTests.request

    def test_summary_export_delete_and_recalculation_endpoints(self):
        import io,zipfile
        from demo import create_demo
        store=create_demo(self.server.store.path);state=store.snapshot()
        ids=[o['id'] for o in state['orders'] if o['status']=='open']
        base={'responsible':'Руководитель','date':datetime.now().date().isoformat()}
        result=json.loads(self.request('/api/dispatch-summary',dict(base,order_ids=ids))[1]);ident=result['id']
        for fmt in ('html','csv','json','docx','xlsx'):
            _,raw,_=self.request(f'/api/export?type=document&id={ident}&format={fmt}')
            if fmt in ('docx','xlsx'):
                with zipfile.ZipFile(io.BytesIO(raw)) as z:self.assertIsNone(z.testzip())
            elif fmt=='html':self.assertIn('Сводная ведомость',raw.decode())
        self.request('/api/recalculate',dict(base,reason='Проверка'))
        self.request('/api/delete-document',dict(base,document_id=ident,reason='Дубль'))
        state=json.loads(self.request('/api/state')[1]);doc=next(d for d in state['documents'] if d['id']==ident)
        self.assertTrue(doc['reversed']);self.assertTrue(doc['data']['deleted'])

if __name__=='__main__':unittest.main()
