"""Confirmed atomic dependency deletion and selected price columns."""
import unittest,json
from unittest.mock import patch
import test_adjustments as fixtures
from domain import ValidationError

class CascadeTests(unittest.TestCase):
    setUp=fixtures.AdjustmentTests.setUp
    tearDown=fixtures.AdjustmentTests.tearDown
    item=fixtures.AdjustmentTests.item
    recipe=fixtures.AdjustmentTests.recipe
    receipt=fixtures.AdjustmentTests.receipt
    produce=fixtures.AdjustmentTests.produce
    state=fixtures.AdjustmentTests.state
    order=fixtures.AdjustmentTests.order
    shipment=fixtures.AdjustmentTests.shipment
    doc=fixtures.AdjustmentTests.doc
    employee=fixtures.AdjustmentTests.employee
    change_rate=fixtures.AdjustmentTests.change_rate
    def delete(self,root,preview=None):
        preview=preview or self.store.deletion_preview({'document_id':root})
        return self.store.delete_document(dict(document_id=root,responsible='Р',reason='Ошибка цепочки',cascade=True,expected_token=preview['token']))
    def chain(self):
        r=self.receipt();p=self.produce(qty=2);o=self.order();s=self.shipment(o,1);return r,p,o,s
    def test_preview_readonly_and_full_cascade_restores_stock_and_debt(self):
        r,p,o,s=self.chain();other=self.store.money(dict(self.base,kind='expense',category='Аренда',amount=100));before=self.state()
        preview=self.store.deletion_preview({'document_id':r});self.assertEqual([d['id'] for d in preview['documents']],[s,p,r]);self.assertEqual(before,self.state())
        self.delete(r,preview);state=self.state();self.assertTrue(all(self.doc(d)['data'].get('deleted') for d in (r,p,s)))
        self.assertFalse(self.doc(other)['reversed']);self.assertTrue(all(l['qty']==0 for l in state['lots']));self.assertEqual(next(p for p in state['partners'] if p['id']==self.client)['debt'],0);self.assertEqual(next(x for x in state['orders'] if x['id']==o)['status'],'open')
        with self.assertRaises(ValidationError):self.delete(r,preview)
    def test_returns_release_writeoff_and_allocated_expense(self):
        r,p,o,s=self.chain();ret=self.store.customer_return(dict(self.base,shipment_id=s,reason='Брак',lines=[dict(line_id=self.doc(s)['data']['lines'][0]['line_id'],qty=.2)]))
        lot=next(l for l in self.state()['lots'] if l['blocked']);release=self.store.release_lot(dict(self.base,lot_id=lot['id'],reason='Проверили'));writeoff=self.store.writeoff(dict(self.base,lot_id=lot['id'],qty=.1,reason='Порча'))
        expense=self.store.money(dict(self.base,kind='expense',category='Логистика',amount=100,allocation_date=self.base['date']))
        preview=self.store.deletion_preview({'document_id':s});ids=[d['id'] for d in preview['documents']]
        self.assertEqual(set(ids),{s,ret,release,writeoff,expense});self.assertLess(ids.index(writeoff),ids.index(release));self.assertLess(ids.index(release),ids.index(ret))
        self.delete(s,preview);self.assertTrue(all(self.doc(d)['reversed'] for d in ids));self.assertFalse(self.doc(p)['reversed'])
    def test_payroll_payout_included_if_accrual_becomes_insufficient(self):
        employee=self.employee();self.change_rate(50);self.receipt();p=self.produce(qty=2,workers=[dict(employee_id=employee,qty=2)])
        payout=self.store.money(dict(self.base,kind='expense',category='Зарплата',amount=100,employee_id=employee));preview=self.store.deletion_preview({'document_id':p});self.assertEqual([d['id'] for d in preview['documents']],[payout,p]);self.delete(p,preview)
        self.assertTrue(self.doc(payout)['data']['deleted']);self.assertEqual(self.store.payroll(self.base['date'],self.base['date']),[])
    def test_pooled_payroll_covered_by_other_accrual_is_preserved(self):
        employee=self.employee();self.change_rate(50);self.receipt();p1=self.produce(qty=2,workers=[dict(employee_id=employee,qty=2)]);p2=self.produce(qty=2,workers=[dict(employee_id=employee,qty=2)])
        payout=self.store.money(dict(self.base,kind='expense',category='Зарплата',amount=50,employee_id=employee));plan=self.store.deletion_preview({'document_id':p2});self.assertNotIn(payout,[d['id'] for d in plan['documents']]);self.delete(p2,plan);self.assertFalse(self.doc(payout)['reversed']);self.assertFalse(self.doc(p1)['reversed'])
    def test_stale_confirmation_is_rejected(self):
        r,p,o,s=self.chain();preview=self.store.deletion_preview({'document_id':p});self.shipment(self.order(),1);before=self.state()
        with self.assertRaisesRegex(ValidationError,'изменились'):self.delete(p,preview)
        self.assertEqual(before,self.state())
    def test_midway_failure_rolls_back_all_reversals(self):
        r,p,o,s=self.chain();before=self.state();original=self.store.reverse_in_transaction
        def fail(c,d):
            if d['document_id']==p:raise ValidationError('Проверка отката')
            return original(c,d)
        with patch.object(self.store,'reverse_in_transaction',side_effect=fail):
            with self.assertRaisesRegex(ValidationError,'Проверка отката'):self.delete(r)
        self.assertEqual(before,self.state())
    def test_order_cascade_keeps_production(self):
        r,p,o,s=self.chain();preview=self.store.deletion_preview({'order_id':o});self.assertEqual([d['id'] for d in preview['documents']],[s])
        self.store.delete_order(dict(id=o,responsible='Р',reason='Дубль',cascade=True,expected_token=preview['token']));self.assertTrue(self.doc(s)['reversed']);self.assertFalse(self.doc(p)['reversed']);self.assertEqual(next(x for x in self.state()['orders'] if x['id']==o)['status'],'deleted')
    def test_already_reversed_document_does_not_repeat_movements(self):
        expense=self.store.money(dict(self.base,kind='expense',category='Прочее',amount=10));self.store.reverse(dict(self.base,document_id=expense,reason='Ошибка'));n=len(self.state()['documents']);self.delete(expense);self.assertEqual(len(self.state()['documents']),n)

class ColumnHttpTests(unittest.TestCase):
    import test_accounting as fixtures
    setUp=fixtures.HttpTests.setUp
    tearDown=fixtures.HttpTests.tearDown
    request=fixtures.HttpTests.request
    def test_selected_columns_all_formats_and_preview_endpoint(self):
        import io,zipfile,xml.etree.ElementTree as ET
        self.request('/api/items',dict(name='Филе готовое',kind='finished',unit='кг',sale_price=947));token=json.loads(self.request('/api/pricing')[1])['token']
        self.request('/api/prices',dict(token=token,column=dict(name='Дистрибьютор',source_id=1,adjustment=-15,round_step=10),apply=True,responsible='М'))
        for fmt in ('html','docx','xlsx','csv','json'):
            _,raw,_=self.request('/api/export?type=prices&price_columns=2&format='+fmt)
            if fmt in ('docx','xlsx'):
                with zipfile.ZipFile(io.BytesIO(raw)) as z:text=' '.join(ET.fromstring(z.read('word/document.xml' if fmt=='docx' else 'xl/worksheets/sheet1.xml')).itertext())
            else:text=raw.decode('utf-8-sig')
            self.assertIn('Дистрибьютор',text);self.assertNotIn('Розница',text);self.assertIn('800',text);self.assertNotIn('947',text)
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/export?type=prices&price_columns=999&format=html')
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/export?type=prices&price_columns=&format=html')
        expense=json.loads(self.request('/api/money',dict(kind='expense',date=self.server.store.snapshot()['now'][:10],responsible='М',category='Аренда',amount=10))[1])['id']
        plan=json.loads(self.request('/api/deletion-preview',dict(document_id=expense))[1]);self.assertEqual(plan['dependents'],[]);self.assertEqual(plan['documents'][0]['id'],expense)

if __name__=='__main__':unittest.main()
