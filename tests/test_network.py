"""Shared server authentication, authorization, CRM and transactional idempotency."""
import unittest,json,urllib.request,urllib.error,http.cookiejar,uuid,sqlite3
from contextlib import closing
from datetime import date,timedelta
import test_workflow as fixtures
from network import Network
from domain import Store,ValidationError,SCHEMA_VERSION

class ResaleTests(unittest.TestCase):
    setUp=fixtures.WorkflowTests.setUp
    tearDown=fixtures.WorkflowTests.tearDown
    item=fixtures.WorkflowTests.item
    recipe=fixtures.WorkflowTests.recipe
    receipt=fixtures.WorkflowTests.receipt
    produce=fixtures.WorkflowTests.produce
    state=fixtures.WorkflowTests.state
    def bought(self):return self.store.save_item(dict(name='Покупной десерт',kind='finished',unit='уп.',supply_type='resale',sale_price=200,track_expiry=False))
    def test_receipt_resale_and_shipment_cost_without_payroll(self):
        iid=self.bought();rid=self.receipt(item_id=iid,qty=10,price=100,expires='');i=next(i for i in self.state()['items'] if i['id']==iid);self.assertEqual(i['available'],10);self.assertEqual(i['planned_cost'],100)
        oid=self.store.save_order(dict(client_id=self.client,manager='М',due=self.future,lines=[dict(item_id=iid,qty=2)]));line=next(l for l in self.state()['order_lines'] if l['order_id']==oid)
        sid=self.store.shipment(dict(self.base,order_id=oid,lines=[dict(line_id=line['id'],qty=2)]));doc=next(d for d in self.state()['documents'] if d['id']==sid)
        self.assertEqual((doc['total'],doc['cost']),(400,200));self.assertEqual(doc['data']['lines'][0]['supply_type'],'resale');self.assertEqual(self.store.payroll(self.base['date'],self.base['date']),[])
    def test_resale_cannot_have_recipe_or_production(self):
        iid=self.bought()
        with self.assertRaises(ValidationError):self.recipe(iid,[dict(item_id=self.raw,net=1)])
        with self.assertRaises(ValidationError):self.store.production(dict(self.base,item_id=iid,qty=1,produced=self.made))
    def test_shortage_goes_only_to_purchase_plan(self):
        iid=self.bought();self.store.save_order(dict(client_id=self.client,manager='М',due=self.future,lines=[dict(item_id=iid,qty=3)]));plan=self.store.plan(self.future[:10]);self.assertEqual(plan['products'],[]);self.assertEqual(plan['ingredients'],[]);self.assertEqual(plan['purchases'][0]['purchase'],3)
        with self.assertRaises(ValidationError):self.store.save_job(dict(self.base,date=self.future[:10]))
    def test_used_type_cannot_be_changed_and_wages_cannot_be_set(self):
        iid=self.bought();self.receipt(item_id=iid,qty=2,expires='')
        with self.assertRaises(ValidationError):self.store.save_item(dict(id=iid,name='Покупной десерт',kind='finished',unit='уп.',supply_type='production'))
        with self.assertRaises(ValidationError):self.store.save_item(dict(name='Недопустимый',kind='finished',unit='уп.',supply_type='resale',labor_rate=10))
    def test_v5_migration_preserves_all_business_data(self):
        self.receipt();self.produce();before=self.state()
        with self.store.transaction() as c:c.execute('ALTER TABLE items DROP COLUMN supply_type');c.execute('PRAGMA user_version=5')
        after=Store(self.store.path).snapshot();self.assertEqual(after['schema_version'],SCHEMA_VERSION)
        for key in ('items','lots','documents','orders','order_lines','pricing'):self.assertEqual(before[key],after[key])
        self.assertTrue(list((self.store.path.parent/'backups').glob('before-v6-*')))

class NetworkTests(unittest.TestCase):
    setUpBase=fixtures.WorkflowTests.setUp
    tearDown=fixtures.WorkflowTests.tearDown
    item=fixtures.WorkflowTests.item
    recipe=fixtures.WorkflowTests.recipe
    state=fixtures.WorkflowTests.state
    def setUp(self):
        self.setUpBase();self.net=Network(self.store);self.net.bootstrap('admin','Директор','CorrectHorseBattery123!');self.token,self.admin=self.net.login('admin','CorrectHorseBattery123!')
        self.trade_id=self.req('/api/net/users',dict(login='trade',name='Менеджер',password='TradePassword123!',roles=['trade']))['id'];self.trade=self.net.public_user(self.get_user(self.trade_id))
        self.prod_id=self.req('/api/net/users',dict(login='production',name='Производство',password='ProductionPass123!',roles=['production']))['id'];self.prod=self.net.public_user(self.get_user(self.prod_id))
    def get_user(self,id):
        with closing(self.store.connect()) as c:return self.store.one(c,'net_users',id)
    def req(self,path,data,user=None,key=None,action=None):return self.net.request(user or self.admin,path,data,key or str(uuid.uuid4()),action)
    def new_client(self):return self.req('/api/net/clients',dict(name='Новый клиент',phone='89991234567',address='Москва'),self.trade)['id']
    def test_sessions_hashes_and_disable_revoke_access(self):
        self.assertEqual(self.net.authenticate(self.token)['id'],self.admin['id']);self.assertNotIn(self.token,str(self.get_user(self.admin['id'])))
        token,_=self.net.login('trade','TradePassword123!');self.assertTrue(self.net.authenticate(token));self.req('/api/net/users',dict(id=self.trade_id,login='trade',name='Менеджер',roles=['trade'],active=False));self.assertIsNone(self.net.authenticate(token))
        with self.assertRaises(ValidationError):self.req('/api/net/users',dict(id=self.admin['id'],login='admin',name='Директор',roles=['trade']))
    def test_unauthorized_apis_are_denied(self):
        for user,path in [(self.trade,'/api/state'),(self.trade,'/api/backup'),(self.prod,'/api/net/users'),(self.trade,'/api/prices'),(self.prod,'/api/money')]:
            with self.assertRaises(PermissionError):self.net.authorize(user,path,{})
    def test_trade_client_ownership_credit_protection_and_duplicates(self):
        cid=self.new_client();p=next(p for p in self.state()['partners'] if p['id']==cid);self.assertEqual(p['manager'],'Менеджер');self.assertEqual(p['payment_days'],0)
        self.req('/api/net/clients',dict(id=cid,name='Новый клиент',phone='89991234567',debt_limit=90000,payment_days=50,price_list_id=999),self.trade)
        p=next(p for p in self.state()['partners'] if p['id']==cid);self.assertEqual(p['payment_days'],0);self.assertEqual(p['price_list_id'],1)
        with self.assertRaises(PermissionError):self.req('/api/net/clients',dict(id=self.client,name='Нельзя',phone='89991234500'),self.trade)
    def test_workspace_excludes_other_clients_and_private_costs(self):
        cid=self.new_client();view=self.net.workspace(self.trade);self.assertEqual([c['id'] for c in view['clients']],[cid]);self.assertNotIn('financial',view);self.assertNotIn('recipes',view);self.assertNotIn('price',view['products'][0]);self.assertNotIn('planned_cost',view['products'][0]);self.assertNotIn('password',str(view))
        prod=self.net.workspace(self.prod);self.assertEqual(prod['debts'],[]);self.assertTrue(all('debt' not in c for c in prod['clients']))
    def test_exactly_once_order_and_payload_conflict(self):
        cid=self.new_client();d=dict(client_id=cid,due=self.future,manager='Подмена',lines=[dict(item_id=self.product,qty=1)]);key=str(uuid.uuid4());first=self.req('/api/orders',d,self.trade,key,self.store.save_order);second=self.req('/api/orders',d,self.trade,key,self.store.save_order);self.assertEqual(first,second);self.assertEqual(len(self.state()['orders']),1);self.assertEqual(self.state()['orders'][0]['manager'],'Менеджер')
        with self.assertRaises(ValidationError):self.req('/api/orders',dict(d,note='Другое'),self.trade,key,self.store.save_order)
        with closing(self.store.connect()) as c:self.assertEqual(c.execute("SELECT count(*) FROM net_audit WHERE action='/api/orders'").fetchone()[0],1)
    def test_failure_is_atomic_and_retry_key_is_not_consumed(self):
        cid=self.new_client();d=dict(client_id=cid,due=self.future,lines=[dict(item_id=self.product,qty=-1)]);key=str(uuid.uuid4())
        with self.assertRaises(ValidationError):self.req('/api/orders',d,self.trade,key,self.store.save_order)
        self.assertEqual(self.state()['orders'],[])
        d['lines'][0]['qty']=1;self.req('/api/orders',d,self.trade,key,self.store.save_order);self.assertEqual(len(self.state()['orders']),1)
    def test_price_preview_inside_request_does_not_rollback_outer_transaction(self):
        state=self.store.pricing();self.req('/api/prices',dict(token=state['token'],column=dict(name='Опт',source_id=1,adjustment=-15)),action=self.store.edit_prices);self.assertEqual(state,self.store.pricing())
    def test_rules_materialize_once_and_keep_independent_call_visit_goals(self):
        cid=self.new_client()
        for kind in ('call','visit'):self.req('/api/net/rules',dict(client_id=cid,weekdays=[date.today().weekday()],kind=kind,at='10:00'),self.trade)
        a=self.net.workspace(self.trade);b=self.net.workspace(self.trade);self.assertEqual(a['tasks'],b['tasks']);self.assertEqual({t['kind'] for t in a['tasks']},{'call','visit'});self.assertGreaterEqual(len(a['tasks']),2)
    def test_director_review_transfer_and_original_due_history(self):
        cid=self.new_client();tid=self.req('/api/net/tasks',dict(client_id=cid,assignee=self.trade_id,title='Посетить',due=self.future,review=True))['id']
        self.req('/api/net/tasks',dict(id=tid,status='done',comment='Результат встречи'),self.trade);task=next(t for t in self.net.workspace(self.admin)['tasks'] if t['id']==tid);self.assertEqual(task['status'],'review');self.assertEqual(task['original_due'],self.future)
        self.req('/api/net/tasks',dict(id=tid,status='done',comment='Принято'),self.admin);self.assertEqual(next(t for t in self.net.workspace(self.admin)['tasks'] if t['id']==tid)['status'],'done')
    def test_incoming_contact_does_not_close_scheduled_visit(self):
        cid=self.new_client();tid=self.req('/api/net/tasks',dict(client_id=cid,title='Визит',kind='visit',due=self.future),self.trade)['id']
        self.req('/api/net/contacts',dict(client_id=cid,task_id=tid,method='incoming',outcome='no_order',comment='Клиент сам позвонил'),self.trade)
        task=next(t for t in self.net.workspace(self.trade)['tasks'] if t['id']==tid);self.assertEqual(task['status'],'assigned')
    def test_transfer_moves_open_tasks_and_rules_with_history(self):
        cid=self.new_client();tid=self.req('/api/net/tasks',dict(client_id=cid,title='Перезвонить',due=self.future),self.trade)['id'];rid=self.req('/api/net/rules',dict(client_id=cid,kind='call',weekdays=[0],at='10:00'),self.trade)['id']
        self.req('/api/net/clients',dict(id=cid,name='Новый клиент',phone='89991234567',owner_id=self.admin['id'],approved=True,transfer_tasks=True),self.admin)
        task=next(t for t in self.net.workspace(self.admin)['tasks'] if t['id']==tid);self.assertEqual(task['assignee'],self.admin['id']);self.assertEqual(task['history'][-1]['previous_assignee'],self.trade['id']);self.assertEqual(self.net.workspace(self.trade)['clients'],[])
    def test_disabled_rule_cancels_future_tasks_without_losing_history(self):
        cid=self.new_client();rid=self.req('/api/net/rules',dict(client_id=cid,kind='call',weekdays=list(range(7)),at='23:59',interval_weeks=2),self.trade)['id'];self.net.workspace(self.trade)
        self.req('/api/net/rules',dict(id=rid,reason='Клиент изменил график'),self.trade);view=self.net.workspace(self.trade);self.assertTrue(all(t['status']=='cancelled' for t in view['tasks']));self.assertTrue(all(t['history'] for t in view['tasks']))
    def test_overdue_task_promise_and_actual_payment_are_separate(self):
        from datetime import datetime
        original=next(p for p in self.state()['partners'] if p['id']==self.client)
        self.req('/api/net/clients',dict(original,phone='89991234567',owner_id=self.trade_id,approved=True))
        dt=(date.today()-timedelta(days=5)).isoformat();made=dt+'T09:00';base=dict(date=dt,responsible='Технолог')
        self.store.receipt(dict(base,partner_id=self.supplier,lines=[dict(item_id=self.raw,qty=10,price=100,produced=made,expires=self.future)]))
        self.store.production(dict(base,item_id=self.product,qty=1,produced=made))
        oid=self.store.save_order(dict(client_id=self.client,manager='М',due=self.future,lines=[dict(item_id=self.product,qty=1,price=800)]));line=next(l for l in self.state()['order_lines'] if l['order_id']==oid)
        self.store.shipment(dict(base,order_id=oid,at=dt+'T12:00',lines=[dict(line_id=line['id'],qty=1)]));view=self.net.workspace(self.trade);debt=next(t for t in view['tasks'] if t['source']=='debt');self.assertEqual(view['clients'][0]['overdue'],800)
        tomorrow=(date.today()+timedelta(days=1)).isoformat()
        self.req('/api/net/contacts',dict(client_id=self.client,task_id=debt['id'],method='call',outcome='promise',comment='Оплатит завтра',promise_date=tomorrow,promise_amount=800,next_due=tomorrow+'T18:00'),self.trade)
        self.assertEqual(self.net.workspace(self.trade)['clients'][0]['overdue'],800)
        for amount in (300,500):self.req('/api/money',dict(kind='payment',partner_id=self.client,date=date.today().isoformat(),amount=amount),action=self.store.money)
        view=self.net.workspace(self.trade);self.assertEqual(view['clients'][0]['overdue'],0);self.assertEqual(next(t for t in view['tasks'] if t['id']==debt['id'])['status'],'done')
    def test_eight_concurrent_retries_commit_one_order(self):
        import concurrent.futures
        cid=self.new_client();data=dict(client_id=cid,due=self.future,lines=[dict(item_id=self.product,qty=1)]);key=str(uuid.uuid4())
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(lambda _:self.req('/api/orders',data,self.trade,key,self.store.save_order),range(8)))
        self.assertTrue(all(r==results[0] for r in results));self.assertEqual(len(self.state()['orders']),1)
    def test_network_copy_validation_rejects_missing_admin(self):
        Network.validate(self.store)
        with self.store.transaction() as c:c.execute('UPDATE net_users SET active=0')
        with self.assertRaises(ValidationError):Network.validate(self.store)
    def test_receipt_photo_is_atomic_with_stock_and_retries(self):
        import base64
        photo=dict(mime='image/png',data=base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'test').decode())
        data=dict(date=date.today().isoformat(),partner_id=self.supplier,lines=[dict(item_id=self.raw,qty=1,price=100,produced=self.made,expires=self.future)],photo=photo);key=str(uuid.uuid4())
        a=self.req('/api/receipt',data,self.prod,key,self.store.receipt);b=self.req('/api/receipt',data,self.prod,key,self.store.receipt);self.assertEqual(a,b)
        with closing(self.store.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM net_photos').fetchone()[0],1)
        before=self.state();data['photo']=dict(mime='image/svg+xml',data=base64.b64encode(b'<script/>').decode())
        with self.assertRaises(ValidationError):self.req('/api/receipt',data,self.prod,action=self.store.receipt)
        after=self.state()
        for field in ('documents','lots','items'):self.assertEqual(before[field],after[field])
    def test_late_cancelled_packet_never_posts_and_committed_request_is_not_reversed(self):
        cid=self.new_client();data=dict(client_id=cid,due=self.future,lines=[dict(item_id=self.product,qty=1)]);key=str(uuid.uuid4())
        resolved=self.net.resolve_request(self.trade,key);self.assertFalse(resolved['committed'])
        with self.assertRaises(ValidationError):self.req('/api/orders',data,self.trade,key,self.store.save_order)
        self.assertEqual(self.state()['orders'],[])
        key=str(uuid.uuid4());created=self.req('/api/orders',data,self.trade,key,self.store.save_order);resolved=self.net.resolve_request(self.trade,key);self.assertTrue(resolved['committed']);self.assertEqual(resolved['response'],created);self.assertEqual(len(self.state()['orders']),1)
    def test_no_answer_keeps_task_open_and_requires_follow_up(self):
        cid=self.new_client();tid=self.req('/api/net/tasks',dict(client_id=cid,title='Позвонить',due=self.future),self.trade)['id']
        data=dict(client_id=cid,task_id=tid,method='call',outcome='no_answer',comment='Не ответил')
        with self.assertRaises(ValidationError):self.req('/api/net/contacts',data,self.trade)
        self.req('/api/net/contacts',dict(data,next_due=self.future),self.trade);view=self.net.workspace(self.trade);self.assertEqual(view['tasks'][0]['status'],'waiting');self.assertEqual(len(view['contacts']),1)


class NetworkHttpTests(unittest.TestCase):
    import test_accounting as base
    setUpBase=base.HttpTests.setUp
    tearDown=base.HttpTests.tearDown
    def setUp(self):
        self.setUpBase();self.server.network=Network(self.server.store);self.server.network.bootstrap('admin','Директор','CorrectHorseBattery123!');self.jar=http.cookiejar.CookieJar();self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
    def request(self,path,data=None,key=None,opener=None,origin=None):
        headers={'Content-Type':'application/json','X-Request-ID':key or str(uuid.uuid4())}
        if origin:headers['Origin']=origin
        req=urllib.request.Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        with (opener or self.opener).open(req) as r:return r.status,json.loads(r.read()) if r.headers.get_content_type()=='application/json' else r.read()
    def login(self,login='admin',password='CorrectHorseBattery123!',opener=None):return self.request('/api/auth/login',dict(login=login,password=password),opener=opener)[1]
    def test_anonymous_denied_and_cookie_flags_and_logout(self):
        for path in ('/api/state','/api/backup','/api/export?type=stock'):
            with self.assertRaises(urllib.error.HTTPError) as e:self.request(path)
            self.assertEqual(e.exception.code,401)
        self.login();cookies=list(self.jar);self.assertEqual(len(cookies),1);self.assertIn('HttpOnly',cookies[0]._rest);self.assertEqual(cookies[0]._rest['SameSite'],'Strict');self.assertEqual(self.request('/api/state')[0],200);self.request('/api/auth/logout',{})
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/state')
    def test_trade_real_http_scope_and_read_only_private_apis(self):
        self.login();self.request('/api/net/users',dict(login='manager',name='Менеджер',password='TradePassword123!',roles=['trade']))
        manager=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()));self.login('manager','TradePassword123!',manager)
        self.request('/api/net/clients',dict(name='Кафе',phone='89991234567'),opener=manager)
        state=self.request('/api/mobile/state',opener=manager)[1];self.assertEqual(state['clients'][0]['name'],'Кафе');self.assertNotIn('financial',state)
        for path in ('/api/state','/api/backup','/api/financial','/api/pricing','/api/export?type=prices'):
            with self.assertRaises(urllib.error.HTTPError) as e:self.request(path,opener=manager)
            self.assertEqual(e.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/net/users',dict(login='hacker',name='X',password='BadPassword123!',roles=['admin']),opener=manager)
    def test_csrf_and_rate_limit_and_idempotency(self):
        with self.assertRaises(urllib.error.HTTPError) as e:self.request('/api/auth/login',dict(login='admin',password='CorrectHorseBattery123!'),origin='https://foreign.example')
        self.assertEqual(e.exception.code,403)
        self.login();key=str(uuid.uuid4());data=dict(name='Сырьё',kind='raw',unit='кг');a=self.request('/api/items',data,key)[1];b=self.request('/api/items',data,key)[1];self.assertEqual(a,b);self.assertEqual(len(self.server.store.snapshot()['items']),1)
        for n in range(5):
            with self.assertRaises(urllib.error.HTTPError):self.login('admin','WrongPassword123!')
        with self.assertRaises(urllib.error.HTTPError) as e:self.login()
        self.assertIn('попыток',json.loads(e.exception.read())['error'])
    def test_restore_preserves_roles_revokes_sessions_and_rebinds_transactions(self):
        import base64
        self.login();self.request('/api/items',dict(name='Исходное',kind='raw',unit='кг'));_,copy=self.request('/api/backup')
        self.request('/api/items',dict(name='После копии',kind='raw',unit='кг'))
        restored=self.request('/api/restore',dict(file=base64.b64encode(copy).decode(),confirm='ВОССТАНОВИТЬ'))[1];self.assertTrue(restored['reauth_required'])
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/state')
        self.login();self.request('/api/items',dict(name='После восстановления',kind='raw',unit='кг'))
        names=[i['name'] for i in self.request('/api/state')[1]['items']];self.assertEqual(names,['Исходное','После восстановления'])
    def test_retry_restore_does_not_erase_documents_created_after_first_restore(self):
        import base64
        self.login();self.request('/api/items',dict(name='Исходное',kind='raw',unit='кг'));_,copy=self.request('/api/backup');key=str(uuid.uuid4());data=dict(file=base64.b64encode(copy).decode(),confirm='ВОССТАНОВИТЬ')
        self.request('/api/restore',data,key);self.login();self.request('/api/items',dict(name='После восстановления',kind='raw',unit='кг'))
        result=self.request('/api/restore',data,key)[1];self.assertTrue(result['replayed']);self.assertEqual(len(self.request('/api/state')[1]['items']),2)
    def test_cancelled_restore_packet_cannot_replace_live_data(self):
        import base64
        self.login();self.request('/api/items',dict(name='Исходное',kind='raw',unit='кг'));_,copy=self.request('/api/backup');self.request('/api/items',dict(name='Не удалять',kind='raw',unit='кг'));key=str(uuid.uuid4())
        self.request('/api/request-resolve',dict(request_id=key))
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/restore',dict(file=base64.b64encode(copy).decode(),confirm='ВОССТАНОВИТЬ'),key)
        self.assertEqual(len(self.request('/api/state')[1]['items']),2)
    def test_signed_update_namespace_never_serves_the_database(self):
        folder=self.server.data/'updates';folder.mkdir();(folder/'ceh-android-2.apk').write_bytes(b'PK-pilot-software')
        _,payload=self.request('/downloads/ceh-android-2.apk');self.assertEqual(payload,b'PK-pilot-software')
        for path in ('/downloads/ceh.sqlite3','/downloads/../ceh.sqlite3','/downloads/ceh-android-999.apk'):
            with self.assertRaises(urllib.error.HTTPError):self.request(path)
        (self.server.data/'updates.json').write_text(json.dumps(dict(android=dict(latest=2,minimum=1,url='https://example.test/ceh-2.apk',sha256='a'*64),private_password='must-not-be-public')),encoding='utf-8-sig')
        payload=self.request('/api/client-version')[1];self.assertEqual(payload['android']['latest'],2);self.assertNotIn('private_password',payload)
    def test_restoring_network_copy_into_local_server_enables_authentication(self):
        import tempfile,threading,base64
        from app import Server
        self.login();self.request('/api/items',dict(name='В копии',kind='raw',unit='кг'));_,copy=self.request('/api/backup')
        with tempfile.TemporaryDirectory() as folder:
            local=Server(('127.0.0.1',0),folder);worker=threading.Thread(target=local.serve_forever,daemon=True);worker.start()
            try:
                url='http://127.0.0.1:'+str(local.server_port)
                request=urllib.request.Request(url+'/api/restore',data=json.dumps(dict(file=base64.b64encode(copy).decode(),confirm='ВОССТАНОВИТЬ')).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(request) as r:self.assertTrue(json.load(r)['reauth_required'])
                with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(url+'/api/state')
                self.assertEqual(e.exception.code,401)
            finally:local.shutdown();local.server_close();worker.join()
    def test_maintenance_blocks_writes_and_backup_has_accounts(self):
        self.login();(self.server.data/'maintenance.flag').touch()
        with self.assertRaises(urllib.error.HTTPError) as e:self.request('/api/items',dict(name='Нельзя',kind='raw',unit='кг'))
        self.assertEqual(e.exception.code,503);self.assertEqual(self.server.store.snapshot()['items'],[])
        (self.server.data/'maintenance.flag').unlink();_,raw=self.request('/api/backup');self.assertTrue(raw.startswith(b'SQLite format 3'))

class NativeArtifactTests(unittest.TestCase):
    def test_android_pilot_hash_and_sources_are_consistent(self):
        import pathlib,hashlib,zipfile,xml.etree.ElementTree as ET
        root=pathlib.Path(__file__).resolve().parents[1];apk=root/'downloads'/'ceh-android-pilot.apk';meta=json.loads(apk.with_suffix('.apk.json').read_text())
        self.assertEqual(meta['sha256'],hashlib.sha256(apk.read_bytes()).hexdigest());self.assertEqual(meta['package'],'ru.ceh.mobile')
        sources=sorted((root/'android'/'src').rglob('*.java'));self.assertEqual(meta['source_sha256'],hashlib.sha256((root/'android'/'AndroidManifest.xml').read_bytes()+b''.join(p.read_bytes() for p in sources)+b''.join(p.read_bytes() for p in sorted((root/'android'/'res').rglob('*')) if p.is_file())).hexdigest())
        with zipfile.ZipFile(apk) as archive:self.assertIn('classes.dex',archive.namelist());self.assertIn('AndroidManifest.xml',archive.namelist())
    def test_windows_client_only_accepts_https_root_urls(self):
        from desktop import server_url,newer
        self.assertEqual(server_url('https://ceh.example/'),'https://ceh.example');self.assertTrue(newer('0.6.1','0.6.0'))
        for url in ('http://ceh.example','https://user:pass@ceh.example','https://ceh.example/path','https://ceh.example?secret=1'):
            with self.assertRaises(ValueError):server_url(url)

if __name__=='__main__':unittest.main()
