"""Regression scenarios for v2: expiry, cash settlement, invoices and delivery."""
import json
import base64
import io
import zipfile
import sqlite3
import tempfile
import unittest
from datetime import date,datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from contextlib import closing
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from domain import Store,SCHEMA,SCHEMA_VERSION,ValidationError
from exports import document_table,invoice_html
import test_accounting as accounting

class UpdateTests(unittest.TestCase):
    # Reuse fixture helpers without rediscovering inherited test methods.
    setUp=accounting.AccountingTests.setUp
    tearDown=accounting.AccountingTests.tearDown
    item=accounting.AccountingTests.item
    recipe=accounting.AccountingTests.recipe
    receipt=accounting.AccountingTests.receipt
    produce=accounting.AccountingTests.produce
    state=accounting.AccountingTests.state
    order=accounting.AccountingTests.order
    shipment=accounting.AccountingTests.shipment
    def test_unlimited_expiry_receipt_consumption_and_plan(self):
        self.store.save_item({'id':self.raw,'name':'Филе','kind':'raw','unit':'кг','track_expiry':False})
        self.store.receipt(dict(self.base,partner_id=self.supplier,lines=[{'item_id':self.raw,'qty':10,'price':100,'produced':self.made}]))
        self.store.save_item({'id':self.product,'name':'Филе сувид','kind':'finished','unit':'кг','track_expiry':False})
        self.produce(qty=2)
        state=self.state()
        self.assertTrue(all(l['expires']=='' for l in state['lots']))
        self.assertEqual(next(i for i in state['items'] if i['id']==self.product)['expired'],0)
        self.order(qty=1)
        plan=self.store.plan(self.future[:10])
        self.assertEqual(plan['products'][0]['available'],2)
        self.assertEqual(plan['products'][0]['produce'],0)
        self.shipment(1,1)
    def test_enabled_expiry_requires_date_on_receipt(self):
        with self.assertRaises(ValidationError):
            self.store.receipt(dict(self.base,partner_id=self.supplier,lines=[{'item_id':self.raw,'qty':10,'price':100,'produced':self.made}]))
        self.assertEqual(self.state()['documents'],[])
    def test_fefo_unlimited_lots_are_last(self):
        self.store.save_item({'id':self.raw,'name':'Филе','kind':'raw','unit':'кг','track_expiry':False})
        self.store.receipt(dict(self.base,partner_id=self.supplier,lines=[{'item_id':self.raw,'qty':10,'price':100,'produced':self.made}]))
        self.receipt(price=200)
        ident=self.produce()
        doc=next(d for d in self.state()['documents'] if d['id']==ident)
        self.assertEqual(doc['data']['inputs'][0]['lots'][0]['unit_cost'],200)
    def test_today_cash_pending_then_automatic_debt_next_day(self):
        self.receipt();self.produce(qty=3,expires=self.future)
        self.store.save_partner({'id':self.client,'kind':'client','name':'Кафе','debt_limit':100})
        self.shipment(self.order(),1)
        client=next(p for p in self.state()['partners'] if p['id']==self.client)
        self.assertEqual(client['debt'],0);self.assertEqual(client['pending_today'],800)
        next_date=date.today()+timedelta(days=1)
        class NextDate(date):
            @classmethod
            def today(cls):return next_date
        class NextDatetime(datetime):
            @classmethod
            def now(cls,tz=None):return datetime.now(tz)+timedelta(days=1)
        with patch('domain.date',NextDate),patch('domain.datetime',NextDatetime):
            state=self.state();client=next(p for p in state['partners'] if p['id']==self.client)
            self.assertEqual(client['debt'],800);self.assertEqual(client['pending_today'],0)
            order=self.order()
            with self.assertRaisesRegex(ValidationError,'лимит'):self.store.shipment(dict(self.base,date=next_date.isoformat(),order_id=order,lines=[{'line_id':state['order_lines'][0]['id']+1,'qty':1}]))
            self.store.money(dict(self.base,date=next_date.isoformat(),kind='payment',partner_id=self.client,amount=800))
            self.assertEqual(next(p for p in self.state()['partners'] if p['id']==self.client)['debt'],0)
            self.store.shipment(dict(self.base,date=next_date.isoformat(),order_id=order,lines=[{'line_id':state['order_lines'][0]['id']+1,'qty':1}]))
    def test_same_day_payment_prevents_debt(self):
        self.receipt();self.produce();self.shipment(self.order(),1)
        self.store.money(dict(self.base,kind='payment',partner_id=self.client,amount=800))
        self.store.close_day(self.base)
        client=next(p for p in self.state()['partners'] if p['id']==self.client)
        self.assertEqual(client['debt'],0);self.assertEqual(client['pending_today'],0)
    def test_close_day_converts_pending_and_prevents_more_shipments(self):
        self.receipt();self.produce(qty=3);self.shipment(self.order(),1)
        self.store.close_day(self.base)
        self.assertEqual(next(p for p in self.state()['partners'] if p['id']==self.client)['debt'],800)
        with self.assertRaises(ValidationError):self.shipment(self.order(),1)
        self.store.money(dict(self.base,kind='payment',partner_id=self.client,amount=300))
        self.assertEqual(next(p for p in self.state()['partners'] if p['id']==self.client)['debt'],500)
        with self.assertRaises(ValidationError):self.store.close_day(self.base)
    def test_discount_invoice_snapshots_and_return_value(self):
        self.store.save_company({'name':'Наш цех','address':'Адрес цеха','phone':'123'})
        self.store.save_partner({'id':self.client,'kind':'client','name':'Кафе','address':'Ленина 1','phone':'456','contact':'Анна'})
        self.receipt();self.produce(qty=3)
        order=self.order(qty=2)
        driver=self.store.save_driver({'name':'Иван','phone':'789','vehicle':'А123АА'})
        self.store.assign_delivery({'order_id':order,'driver_id':driver,'route_position':2})
        ident=self.shipment(order,2.037,discount_percent=10)
        doc=next(d for d in self.state()['documents'] if d['id']==ident)
        self.assertEqual(doc['total'],1466.64)
        self.assertEqual(doc['data']['discount_amount'],162.96)
        self.assertEqual(doc['data']['delivery_address'],'Ленина 1')
        self.assertEqual(doc['data']['driver_name'],'Иван')
        self.store.save_company({'name':'Другое имя'})
        self.store.save_partner({'id':self.client,'kind':'client','name':'Новое имя','address':'Другой адрес'})
        html=invoice_html(doc).decode()
        for value in ('Наш цех','Ленина 1','456','Анна','Иван','1 466,64'):
            self.assertIn(value,html)
        title,rows=document_table(doc)
        self.assertIn(['Адрес доставки','Ленина 1'],rows)
        returned=self.store.customer_return(dict(self.base,shipment_id=ident,reason='Возврат',lines=[{'line_id':doc['data']['lines'][0]['line_id'],'qty':2.037}]))
        self.assertEqual(next(d for d in self.state()['documents'] if d['id']==returned)['total'],1466.64)
        self.assertEqual(next(p for p in self.state()['partners'] if p['id']==self.client)['balance'],0)
    def test_discount_bounds_and_atomic_rejection(self):
        self.receipt();self.produce();order=self.order();before=self.state()
        for discount in (-1,101,'nan'):
            with self.assertRaises(ValidationError):self.shipment(order,1,discount_percent=discount)
        self.assertEqual(self.state(),before)
    def test_full_discount_and_split_returns_never_exceed_net_invoice(self):
        self.receipt();self.produce(qty=3)
        order=self.store.save_order({'client_id':self.client,'manager':'М','due':self.future,'lines':[{'item_id':self.product,'qty':3,'price':.01}]})
        ident=self.shipment(order,3,discount_percent=50)
        doc=next(d for d in self.state()['documents'] if d['id']==ident)
        self.assertEqual(doc['total'],.02)
        for _ in range(3):self.store.customer_return(dict(self.base,shipment_id=ident,reason='Частичный возврат',lines=[{'line_id':doc['data']['lines'][0]['line_id'],'qty':1}]))
        self.assertEqual(next(p for p in self.state()['partners'] if p['id']==self.client)['balance'],0)
    def test_routes_driver_filter_order_and_cash_registration(self):
        a=self.store.save_driver({'name':'Иван','vehicle':'Авто 1'})
        b=self.store.save_driver({'name':'Пётр','vehicle':'Авто 2'})
        o1=self.order();o2=self.order();o3=self.order()
        self.store.assign_delivery({'order_id':o1,'driver_id':a,'route_position':2,'delivery_address':'Адрес 1','delivery_contact':'Анна','delivery_phone':'123'})
        self.store.assign_delivery({'order_id':o2,'driver_id':a,'route_position':1})
        self.store.assign_delivery({'order_id':o3,'driver_id':b})
        route=self.store.route(self.future[:10],a)
        self.assertEqual([s['order_id'] for s in route['stops']],[o2,o1])
        self.assertEqual(route['stops'][1]['address'],'Адрес 1')
        self.receipt();self.produce(qty=2)
        self.shipment(o1,1,discount_percent=10)
        self.store.money(dict(self.base,kind='payment',partner_id=self.client,amount=200,driver_id=a))
        stop=next(s for s in self.store.route(self.future[:10],a)['stops'] if s['order_id']==o1)
        self.assertEqual(stop['total'],720);self.assertEqual(stop['to_collect'],520)
        self.assertEqual(stop['goods'][0]['actual'],1)
        self.assertEqual(len(self.store.route(self.future[:10],0)['stops']),0)
    def test_migration_preserves_legacy_rows_and_keeps_backup(self):
        path=Path(self.tmp.name)/'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as c:
            c.executescript(SCHEMA+'\nPRAGMA user_version=1;')
            c.execute("INSERT INTO items(name,kind,unit,price) VALUES('Мука','raw','кг',75)")
            c.execute("INSERT INTO partners(kind,name,address) VALUES('client','Клиент','Адрес')")
            c.execute("INSERT INTO documents(kind,date,responsible,total,data,created) VALUES('payment',?,'Мария',123,'{}',?)",(date.today().isoformat(),self.made))
            c.execute("INSERT INTO items(name,kind,unit) VALUES('Хлеб','finished','кг')")
            c.execute("INSERT INTO documents(kind,date,responsible,total,data,created) VALUES('receipt',?,'Мария',225,'{\"lines\":[]}',?)",(date.today().isoformat(),self.made))
            c.execute("INSERT INTO lots(item_id,code,produced,expires,qty,unit_cost,document_id) VALUES(1,'М-001',?,?,3,75,2)",(self.made,self.future))
            c.execute("INSERT INTO movements(document_id,lot_id,item_id,qty,unit_cost) VALUES(2,1,1,3,75)")
            c.execute("INSERT INTO recipes(item_id,version,yield_qty,lines,created) VALUES(2,1,1,?,?)",(json.dumps([{'item_id':1,'name':'Мука','unit':'кг','net':1,'gross':1,'mechanical':0,'thermal':0,'treatment':''}]),self.made))
            c.execute("INSERT INTO orders(client_id,due,manager,created) VALUES(1,?,'Менеджер',?)",(self.future,self.made))
            c.execute("INSERT INTO order_lines(order_id,item_id,qty,price) VALUES(1,2,1,100)")
            c.commit()
        migrated=Store(path);state=migrated.snapshot()
        self.assertEqual(state['schema_version'],SCHEMA_VERSION)
        self.assertEqual(state['items'][0]['name'],'Мука')
        self.assertEqual(state['items'][0]['track_expiry'],1)
        self.assertEqual(state['documents'][0]['total'],123)
        self.assertEqual(state['lots'][0]['qty'],3)
        self.assertEqual(state['lots'][0]['expires'],self.future)
        self.assertEqual(state['recipes'][0]['lines'][0]['item_id'],1)
        self.assertEqual(state['order_lines'][0]['qty'],1)
        backups=list((path.parent/'backups').glob('before-v2-*'))
        self.assertTrue(backups)
        found=False
        for backup in backups:
            with closing(sqlite3.connect(backup)) as old:
                found=found or (old.execute('PRAGMA user_version').fetchone()[0]==1 and bool(old.execute("SELECT 1 FROM items WHERE name='Мука'").fetchone()))
        self.assertTrue(found)
        self.assertEqual(Store(path).snapshot(),state)

class HttpUpdateTests(unittest.TestCase):
    setUp=accounting.HttpTests.setUp
    tearDown=accounting.HttpTests.tearDown
    request=accounting.HttpTests.request

    def test_legacy_backup_restore_upgrades_to_v2(self):
        legacy=Path(self.tmp.name)/'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as c:
            c.executescript(SCHEMA+';PRAGMA user_version=1;')
            c.execute("INSERT INTO items(name,kind,unit) VALUES('Сахар','raw','кг')")
            c.commit()
        self.request('/api/restore',{'file':base64.b64encode(legacy.read_bytes()).decode(),'confirm':'ВОССТАНОВИТЬ'})
        state=json.loads(self.request('/api/state')[1])
        self.assertEqual(state['schema_version'],2)
        self.assertEqual(state['items'][0]['name'],'Сахар')
        self.request('/api/drivers',{'name':'Иван'})
        self.assertEqual(len(json.loads(self.request('/api/state')[1])['drivers']),1)

    def test_route_and_discounted_invoice_exports_all_formats(self):
        from demo import create_demo
        store=create_demo(self.server.store.path)
        state=store.snapshot();driver=store.save_driver({'name':'Иван','vehicle':'А123АА'})
        order=state['orders'][0]
        self.request('/api/assign-delivery',{'order_id':order['id'],'driver_id':driver,'delivery_address':'Адрес доставки'})
        route=json.loads(self.request('/api/route?date='+order['due'][:10]+'&driver_id='+str(driver))[1])
        self.assertEqual(len(route['stops']),1)
        self.assertEqual(route['stops'][0]['address'],'Адрес доставки')
        shipment=next(d for d in state['documents'] if d['kind']=='shipment')
        for typ,args in [('route','&date='+order['due'][:10]+'&driver_id='+str(driver)),('document','&id='+str(shipment['id']))]:
            for fmt in ('html','json','csv','xlsx','docx'):
                _,content,headers=self.request('/api/export?type='+typ+'&format='+fmt+args)
                if fmt in ('xlsx','docx'):
                    with zipfile.ZipFile(io.BytesIO(content)) as z:self.assertIsNone(z.testzip())
                elif fmt=='html':self.assertIn('Иван' if typ=='route' else 'Кто отгружает',content.decode())

if __name__=='__main__':unittest.main()
