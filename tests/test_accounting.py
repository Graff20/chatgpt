import base64
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import zipfile
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from domain import Store, ValidationError
from app import Server
from exports import export

class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'db.sqlite3')
        self.now=datetime.now().replace(second=0,microsecond=0)
        self.made=(self.now-timedelta(hours=1)).isoformat(timespec='minutes')
        self.future=(self.now+timedelta(days=7)).isoformat(timespec='minutes')
        self.base={'date':date.today().isoformat(),'responsible':'Технолог'}
        self.supplier=self.store.save_partner({'kind':'supplier','name':'Поставщик'})
        self.client=self.store.save_partner({'kind':'client','name':'Кафе','payment_days':3})
        self.raw=self.item('Филе','raw')
        self.product=self.item('Филе сувид','finished',sale_price=800)
        self.recipe(self.product,[{'item_id':self.raw,'net':1,'mechanical':10,'thermal':20}])
    def tearDown(self): self.tmp.cleanup()
    def item(self,name,kind,**kw):return self.store.save_item(dict(name=name,kind=kind,unit='кг',**kw))
    def recipe(self,ident,lines):return self.store.save_recipe({'item_id':ident,'yield_qty':1,'lines':lines})
    def receipt(self,qty=10,price=100,item_id=None,expires=None,**kw):
        return self.store.receipt(dict(self.base,partner_id=self.supplier,lines=[dict(item_id=item_id or self.raw,qty=qty,price=price,produced=self.made,expires=expires or self.future,**kw)]))
    def produce(self,qty=1,**kw):return self.store.production(dict(self.base,item_id=self.product,qty=qty,produced=self.now.isoformat(timespec='minutes'),**kw))
    def state(self):return self.store.snapshot()
    def order(self,qty=1,**kw):return self.store.save_order(dict(client_id=self.client,due=self.future,manager='Менеджер',lines=[{'item_id':self.product,'qty':qty,'price':800}],**kw))
    def shipment(self,order,qty,**kw):
        line=next(l for l in self.state()['order_lines'] if l['order_id']==order)
        return self.store.shipment(dict(self.base,order_id=order,lines=[{'line_id':line['id'],'qty':qty}],**kw))
    def test_production_without_order_and_sequential_losses(self):
        self.receipt(); ident=self.produce()
        state=self.state(); raw=next(i for i in state['items'] if i['id']==self.raw)
        self.assertAlmostEqual(raw['available'],10-1/.9/.8)
        doc=next(d for d in state['documents'] if d['id']==ident)
        self.assertAlmostEqual(doc['cost'],100/.9/.8)
        self.assertEqual(len(state['orders']),0)
        self.assertEqual(next(i for i in state['items'] if i['id']==self.product)['available'],1)
    def test_shortage_rolls_back_entire_production(self):
        self.receipt(qty=1); before=self.state()
        with self.assertRaises(ValidationError):self.produce()
        self.assertEqual(before,self.state())
    def test_multiline_shortage_rolls_back_first_ingredient(self):
        other=self.item('Соль','raw')
        self.recipe(self.product,[{'item_id':self.raw,'net':1},{'item_id':other,'net':.1}])
        self.receipt();before=self.state()
        with self.assertRaises(ValidationError):self.produce()
        self.assertEqual(before,self.state())
    def test_expired_raw_cannot_be_used(self):
        expired=(self.now-timedelta(minutes=20)).isoformat(timespec='minutes')
        self.receipt(expires=expired)
        with self.assertRaises(ValidationError):self.produce()
    def test_fefo_chooses_earliest_expiry(self):
        self.receipt(qty=2,price=100)
        self.receipt(qty=2,price=200,expires=(self.now+timedelta(hours=8)).isoformat(timespec='minutes'))
        ident=self.produce();doc=next(d for d in self.state()['documents'] if d['id']==ident)
        self.assertEqual(doc['data']['inputs'][0]['lots'][0]['unit_cost'],200)
    def test_nested_virtual_semifinished(self):
        semi=self.item('Смесь','semi',mode='virtual')
        self.recipe(semi,[{'item_id':self.raw,'net':2}])
        self.recipe(self.product,[{'item_id':semi,'net':.5}])
        self.receipt();ident=self.produce()
        doc=next(d for d in self.state()['documents'] if d['id']==ident)
        self.assertAlmostEqual(doc['data']['inputs'][0]['qty'],1)
    def test_stock_semifinished_consumed_without_double_writeoff(self):
        semi=self.item('Смесь','semi')
        self.recipe(semi,[{'item_id':self.raw,'net':1}])
        self.recipe(self.product,[{'item_id':semi,'net':1}])
        self.receipt()
        self.store.production(dict(self.base,item_id=semi,qty=2,produced=self.now.isoformat(timespec='minutes')))
        self.produce()
        self.assertEqual(next(i for i in self.state()['items'] if i['id']==self.raw)['available'],8)
    def test_cycle_rejected_and_old_version_retained(self):
        semi=self.item('Цикл','semi',mode='virtual')
        self.recipe(semi,[{'item_id':self.product,'net':1}])
        with self.assertRaises(ValidationError):self.recipe(self.product,[{'item_id':semi,'net':1}])
        self.assertEqual(len(self.state()['recipes']),2)
    def test_exact_weight_partial_shipment_and_debt(self):
        self.receipt();self.produce(qty=3);order=self.order(qty=2)
        self.shipment(order,.9)
        self.assertEqual(self.state()['orders'][0]['status'],'open')
        self.shipment(order,1.137)
        s=self.state();self.assertEqual(s['orders'][0]['status'],'completed')
        self.assertAlmostEqual(s['order_lines'][0]['shipped'],2.037)
        self.assertAlmostEqual(next(p for p in s['partners'] if p['id']==self.client)['balance'],1629.6)
    def test_tolerance_and_limit_roll_back(self):
        self.receipt();self.produce(qty=3);order=self.order()
        before=self.state()
        with self.assertRaises(ValidationError):self.shipment(order,1.5)
        self.assertEqual(before,self.state())
        self.store.save_partner({'id':self.client,'kind':'client','name':'Кафе','debt_limit':100})
        shipped=self.shipment(order,1)  # Today's new receivable does not itself block dispatch.
        with self.store.transaction() as c:
            c.execute('UPDATE documents SET date=? WHERE id=?',((date.today()-timedelta(days=1)).isoformat(),shipped))
        new_order=self.order()
        before=self.state()
        with self.assertRaises(ValidationError):self.shipment(new_order,1)
        self.assertEqual(before,self.state())
        self.shipment(new_order,1,override_reason='Согласовано директором')
    def test_payment_and_advance(self):
        self.store.money(dict(self.base,kind='payment',partner_id=self.client,amount=1000))
        self.receipt();self.produce();self.shipment(self.order(),1)
        s=self.state();self.assertEqual(s['debts'],[])
        self.assertEqual(next(p for p in s['partners'] if p['id']==self.client)['balance'],-200)
    def test_expired_writeoff_with_reason_and_responsible(self):
        self.receipt(expires=(self.now-timedelta(minutes=1)).isoformat(timespec='minutes'))
        lot=self.state()['lots'][0]
        self.store.writeoff(dict(self.base,lot_id=lot['id'],qty=10,reason='Просрочка'))
        self.assertEqual(self.state()['lots'][0]['qty'],0)
        with self.assertRaises(ValidationError):self.store.writeoff(dict(self.base,lot_id=lot['id'],qty=1,reason=''))
    def test_inventory_and_stale_sheet(self):
        self.receipt();lot=self.state()['lots'][0]
        self.store.inventory(dict(self.base,lines=[{'lot_id':lot['id'],'expected':10,'actual':8,'reason':'Недостача'}]))
        self.assertEqual(self.state()['lots'][0]['qty'],8)
        with self.assertRaises(ValidationError):self.store.inventory(dict(self.base,lines=[{'lot_id':lot['id'],'expected':10,'actual':9,'reason':'Повтор'}]))
    def test_units_and_indivisibility(self):
        self.receipt(qty=1000,unit='г')
        self.assertEqual(self.state()['lots'][0]['qty'],1)
        pack=self.store.save_item({'name':'Палочки','kind':'finished','unit':'уп.'})
        with self.assertRaises(ValidationError):self.store.save_order({'client_id':self.client,'due':self.future,'manager':'М','lines':[{'item_id':pack,'qty':1.5}]})
    def test_last_receipt_price_and_historical_snapshot(self):
        self.receipt(price=100);p=self.produce();self.receipt(price=200)
        s=self.state();self.assertAlmostEqual(next(i for i in s['items'] if i['id']==self.product)['planned_cost'],200/.9/.8)
        self.assertAlmostEqual(next(d for d in s['documents'] if d['id']==p)['cost'],100/.9/.8)
    def test_plan_excludes_lots_expired_by_target_and_prior_orders(self):
        self.receipt();self.produce(qty=2)
        self.order(qty=3)
        plan=self.store.plan(self.future[:10])
        self.assertEqual(plan['products'][0]['available'],0)
        self.assertEqual(plan['products'][0]['produce'],3)
        self.assertGreater(plan['ingredients'][0]['qty'],3)
    def test_concurrent_shipments_cannot_oversell(self):
        self.receipt();self.produce();orders=[self.order(),self.order()];results=[]
        def worker(o):
            try:self.shipment(o,1);results.append('ok')
            except ValidationError:results.append('shortage')
        ts=[threading.Thread(target=worker,args=(o,)) for o in orders]
        for t in ts:t.start()
        for t in ts:t.join()
        self.assertEqual(sorted(results),['ok','shortage'])
        self.assertEqual(next(i for i in self.state()['items'] if i['id']==self.product)['available'],0)
    def test_reversal_restores_stock_and_debt_then_allows_reproduction(self):
        receipt=self.receipt();production=self.produce();shipment=self.shipment(self.order(),1)
        with self.assertRaises(ValidationError):self.store.reverse(dict(self.base,document_id=production,reason='Ошибка'))
        self.store.reverse(dict(self.base,document_id=shipment,reason='Ошибка веса'))
        self.assertEqual(next(p for p in self.state()['partners'] if p['id']==self.client)['balance'],0)
        self.assertEqual(self.state()['orders'][0]['status'],'open')
        self.store.reverse(dict(self.base,document_id=production,reason='Ошибка выпуска'))
        self.assertAlmostEqual(next(i for i in self.state()['items'] if i['id']==self.raw)['available'],10)
        self.produce()
        with self.assertRaises(ValidationError):self.store.reverse(dict(self.base,document_id=receipt,reason='Ошибка'))
    def test_reverse_receipt_uses_previous_last_price(self):
        self.receipt(price=100);new=self.receipt(price=200)
        self.store.reverse(dict(self.base,document_id=new,reason='Дубль'))
        self.assertEqual(next(i for i in self.state()['items'] if i['id']==self.raw)['planned_cost'],100)
        with self.assertRaises(ValidationError):self.store.reverse(dict(self.base,document_id=new,reason='Повтор'))
    def test_return_quarantine_credit_release_and_overreturn(self):
        self.receipt();self.produce(qty=2);shipment=self.shipment(self.order(),1)
        line=next(d for d in self.state()['documents'] if d['id']==shipment)['data']['lines'][0]
        data=dict(self.base,shipment_id=shipment,reason='Возврат клиента',lines=[{'line_id':line['line_id'],'qty':.4}])
        returned=self.store.customer_return(data)
        state=self.state();lot=next(l for l in state['lots'] if l['document_id']==returned)
        self.assertTrue(lot['blocked'])
        self.assertAlmostEqual(next(p for p in state['partners'] if p['id']==self.client)['balance'],480)
        self.assertAlmostEqual(sum(d['amount'] for d in state['debts']),480)
        self.assertEqual(next(i for i in state['items'] if i['id']==self.product)['available'],1)
        self.store.release_lot(dict(self.base,lot_id=lot['id'],reason='Проверено технологом'))
        self.assertAlmostEqual(next(i for i in self.state()['items'] if i['id']==self.product)['available'],1.4)
        with self.assertRaises(ValidationError):self.store.customer_return(dict(data,lines=[{'line_id':line['line_id'],'qty':.7}]))
    def test_cancel_return_then_cancel_shipment(self):
        self.receipt();self.produce();shipment=self.shipment(self.order(),1)
        line=next(d for d in self.state()['documents'] if d['id']==shipment)['data']['lines'][0]
        returned=self.store.customer_return(dict(self.base,shipment_id=shipment,reason='Возврат',lines=[{'line_id':line['line_id'],'qty':.5}]))
        with self.assertRaises(ValidationError):self.store.reverse(dict(self.base,document_id=shipment,reason='Ошибка'))
        self.store.reverse(dict(self.base,document_id=returned,reason='Ошибка возврата'))
        self.store.reverse(dict(self.base,document_id=shipment,reason='Ошибка отгрузки'))
        self.assertEqual(next(i for i in self.state()['items'] if i['id']==self.product)['available'],1)
    def test_return_remains_unavailable_until_release(self):
        self.receipt();self.produce();shipment=self.shipment(self.order(),1)
        line=next(d for d in self.state()['documents'] if d['id']==shipment)['data']['lines'][0]
        self.store.customer_return(dict(self.base,shipment_id=shipment,reason='Возврат',lines=[{'line_id':line['line_id'],'qty':1}]))
        with self.assertRaises(ValidationError):self.shipment(self.order(),1)
    def test_exports_valid_archives_xml_and_formula_protection(self):
        for fmt in ('xlsx','docx'):
            content,_=export('Документ',[['Товар','Количество'],['<сырьё>',1]],fmt)
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                self.assertIsNone(z.testzip())
                for name in z.namelist(): ET.fromstring(z.read(name))
        content,_=export('Данные',[['=CMD()','+1','@x',-1]],'csv')
        self.assertIn("'=CMD()",content.decode('utf-8-sig'))
    def test_restart_preserves_data(self):
        self.receipt();self.produce();a=self.state()
        other=Store(self.store.path)
        self.assertEqual(a,other.snapshot())

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.server=Server(('127.0.0.1',0),self.tmp.name)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,path,data=None,origin=None):
        headers={'Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        req=urllib.request.Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        with urllib.request.urlopen(req) as r:return r.status,r.read(),r.headers
    def test_api_static_backup_restore(self):
        for path in ('/','/style.css','/app.js','/api/state'):
            self.assertEqual(self.request(path)[0],200)
        self.request('/api/items',{'name':'Мука','kind':'raw','unit':'кг'})
        _,backup,_=self.request('/api/backup')
        self.request('/api/items',{'name':'Сахар','kind':'raw','unit':'кг'})
        self.request('/api/restore',{'confirm':'ВОССТАНОВИТЬ','file':base64.b64encode(backup).decode()})
        state=json.loads(self.request('/api/state')[1]);self.assertEqual([i['name'] for i in state['items']],['Мука'])
        self.assertTrue(list((Path(self.tmp.name)/'backups').glob('before-restore-*')))
    def test_invalid_restore_does_not_damage_live_db(self):
        self.request('/api/items',{'name':'Мука','kind':'raw','unit':'кг'})
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/restore',{'confirm':'ВОССТАНОВИТЬ','file':base64.b64encode(b'bad file').decode()})
        self.assertEqual(len(json.loads(self.request('/api/state')[1])['items']),1)
    def test_cross_origin_write_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/items',{'name':'Мука'},origin='https://other.example')
        self.assertEqual(cm.exception.code,403)

if __name__=='__main__':unittest.main()
