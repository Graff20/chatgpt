"""Opt-in authenticated shared server. Business transactions and audit share one SQLite DB."""
import hashlib,hmac,secrets,json,sqlite3,time
from datetime import datetime,date,timedelta
from contextlib import closing
from domain import ValidationError,required,day,dumps

ROLES={'admin','production','trade'}
SCHEMA='''
CREATE TABLE IF NOT EXISTS net_users(id INTEGER PRIMARY KEY,login TEXT NOT NULL UNIQUE,name TEXT NOT NULL,password TEXT NOT NULL,roles TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS net_sessions(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES net_users(id),expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS net_attempts(login TEXT PRIMARY KEY,failures INTEGER NOT NULL,until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS net_clients(client_id INTEGER PRIMARY KEY REFERENCES partners(id),owner_id INTEGER REFERENCES net_users(id),status TEXT NOT NULL DEFAULT 'prospect',approved INTEGER NOT NULL DEFAULT 0,promise_date TEXT NOT NULL DEFAULT '',promise_amount REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS net_rules(id INTEGER PRIMARY KEY,client_id INTEGER NOT NULL REFERENCES partners(id),assignee INTEGER NOT NULL REFERENCES net_users(id),kind TEXT NOT NULL,weekdays TEXT NOT NULL,interval_weeks INTEGER NOT NULL DEFAULT 1,at TEXT NOT NULL,start TEXT NOT NULL,last_generated TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS net_tasks(id INTEGER PRIMARY KEY,client_id INTEGER REFERENCES partners(id),assignee INTEGER NOT NULL REFERENCES net_users(id),title TEXT NOT NULL,kind TEXT NOT NULL,due TEXT NOT NULL,original_due TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'assigned',source TEXT NOT NULL,source_key TEXT UNIQUE,review INTEGER NOT NULL DEFAULT 0,priority TEXT NOT NULL DEFAULT 'normal',result TEXT NOT NULL DEFAULT '{}',history TEXT NOT NULL DEFAULT '[]',created_by INTEGER NOT NULL REFERENCES net_users(id));
CREATE TABLE IF NOT EXISTS net_contacts(id INTEGER PRIMARY KEY,client_id INTEGER NOT NULL REFERENCES partners(id),user_id INTEGER NOT NULL REFERENCES net_users(id),task_id INTEGER REFERENCES net_tasks(id),method TEXT NOT NULL,outcome TEXT NOT NULL,comment TEXT NOT NULL,created TEXT NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS net_audit(id INTEGER PRIMARY KEY,at TEXT NOT NULL,user_id INTEGER REFERENCES net_users(id),action TEXT NOT NULL,entity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS net_photos(id INTEGER PRIMARY KEY,document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id),mime TEXT NOT NULL,image BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS net_requests(user_id INTEGER NOT NULL REFERENCES net_users(id),request_id TEXT NOT NULL,path TEXT NOT NULL,body_hash TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(user_id,request_id));
'''

def password_hash(password):
    if len(password)<12 or len(password)>200:raise ValidationError('Пароль: от 12 до 200 символов')
    salt=secrets.token_hex(16);digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()
    return '600000$'+salt+'$'+digest

def verify(password,stored):
    rounds,salt,expected=stored.split('$');actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(rounds)).hex()
    return hmac.compare_digest(actual,expected)

class Network:
    def __init__(self,store):
        self.store=store
        with closing(store.connect()) as c:
            c.executescript(SCHEMA);c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('network_schema','1')");c.commit()
    @staticmethod
    def validate(store):
        required_columns={'net_photos':'id,document_id,mime,image','net_users':'id,login,name,password,roles,active','net_sessions':'token,user_id,expires','net_requests':'user_id,request_id,path,body_hash,response','net_clients':'client_id,owner_id,status,approved,promise_date,promise_amount','net_rules':'id,client_id,assignee,kind,weekdays,interval_weeks,at,start,last_generated,active','net_tasks':'id,client_id,assignee,title,kind,due,original_due,status,source,source_key,review,priority,result,history,created_by','net_contacts':'id,client_id,user_id,task_id,method,outcome,comment,created,data','net_audit':'id,at,user_id,action,entity','net_attempts':'login,failures,until'}
        with closing(store.connect()) as c:
            for table,columns in required_columns.items():
                try:c.execute('SELECT '+columns+' FROM '+table+' LIMIT 0')
                except sqlite3.Error:raise ValidationError('В копии отсутствуют поля сетевой базы: '+table)
            version=c.execute("SELECT value FROM settings WHERE key='network_schema'").fetchone()
            if not version or version[0]!='1':raise ValidationError('Версия сетевой базы не поддерживается')
            users=[dict(r) for r in c.execute('SELECT * FROM net_users')]
            if not any(u['active'] and 'admin' in json.loads(u['roles']) for u in users):raise ValidationError('В копии нет действующего администратора')
            for user in users:
                roles=json.loads(user['roles'])
                if not isinstance(roles,list) or not roles or set(roles)-ROLES:raise ValidationError('Повреждены роли сотрудников')
                try:
                    rounds,salt,digest=user['password'].split('$');valid=100000<=int(rounds)<=1000000 and len(bytes.fromhex(salt))==16 and len(bytes.fromhex(digest))==32
                except (ValueError,TypeError):valid=False
                if not valid:raise ValidationError('Повреждены данные входа сотрудника')

    @staticmethod
    def enabled(store):
        with closing(store.connect()) as c:return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='net_users'").fetchone() and c.execute('SELECT 1 FROM net_users LIMIT 1').fetchone())
    def public_user(self,row):return dict(id=row['id'],login=row['login'],name=row['name'],roles=json.loads(row['roles']),active=bool(row['active']))
    def bootstrap(self,login,name,password):
        if len(str(login))>100:raise ValidationError('Логин: не более 100 символов')
        with self.store.transaction() as c:
            if c.execute('SELECT 1 FROM net_users LIMIT 1').fetchone():raise ValidationError('Администратор уже создан. Используйте управление доступом')
            return c.execute('INSERT INTO net_users(login,name,password,roles) VALUES(?,?,?,?)',(required(login,'Логин').casefold(),required(name,'Имя'),password_hash(password),dumps(['admin']))).lastrowid
    def login(self,login,password):
        login=str(login).strip().casefold()[:100]
        if not isinstance(password,str) or len(password)>200:raise ValidationError('Неверный логин или пароль')
        with self.store.transaction() as c:
            row=c.execute('SELECT * FROM net_attempts WHERE login=?',(login,)).fetchone()
            if row and row['until']>time.time() and row['failures']>=5:raise ValidationError('Слишком много попыток. Повторите через 15 минут')
            user=c.execute('SELECT * FROM net_users WHERE login=?',(login,)).fetchone()
            good=verify(password,user['password']) if user else verify(password,'600000$'+'00'*16+'$'+'00'*32)
            if not user or not user['active'] or not good:
                failures=(row['failures'] if row and row['until']>time.time() else 0)+1
                c.execute('INSERT OR REPLACE INTO net_attempts VALUES(?,?,?)',(login,failures,time.time()+900))
                # Commit the throttle before returning a generic login error.
                error=True
            else:
                error=False;c.execute('DELETE FROM net_attempts WHERE login=?',(login,));token=secrets.token_urlsafe(32)
                c.execute('DELETE FROM net_sessions WHERE expires<?',(time.time(),))
                c.execute('INSERT INTO net_sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['id'],time.time()+43200))
        if error:raise ValidationError('Неверный логин или пароль')
        return token,self.public_user(user)
    def authenticate(self,token):
        with closing(self.store.connect()) as c:
            row=c.execute('SELECT u.* FROM net_sessions s JOIN net_users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.active=1',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
            return self.public_user(row) if row else None
    def logout(self,token):
        with self.store.transaction() as c:c.execute('DELETE FROM net_sessions WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),))
    def admin(self,user):
        if 'admin' not in user['roles']:raise PermissionError('Доступ только администратору')
    def owns(self,c,user,client):
        if 'admin' in user['roles']:return
        row=c.execute('SELECT owner_id FROM net_clients WHERE client_id=?',(client,)).fetchone()
        if not row or row['owner_id']!=user['id']:raise PermissionError('Клиент не назначен этому менеджеру')
    def audit(self,c,user,action,entity):c.execute('INSERT INTO net_audit(at,user_id,action,entity) VALUES(?,?,?,?)',(datetime.now().isoformat(),user['id'],action,str(entity)))
    def users(self):
        with closing(self.store.connect()) as c:return [self.public_user(r) for r in c.execute('SELECT * FROM net_users ORDER BY id')]
    def save_user(self,c,user,d):
        self.admin(user);roles=d.get('roles',[])
        if not roles or not isinstance(roles,list) or set(roles)-ROLES:raise ValidationError('Выберите роли сотрудника')
        if len(str(d.get('login','')))>100:raise ValidationError('Логин: не более 100 символов')
        name=required(d.get('name'),'Имя');login=required(d.get('login'),'Логин').casefold();active=bool(d.get('active',True))
        if d.get('id'):
            old=self.store.one(c,'net_users',d['id'])
            if 'admin' in json.loads(old['roles']) and (not active or 'admin' not in roles) and not any(r['id']!=old['id'] and r['active'] and 'admin' in json.loads(r['roles']) for r in c.execute('SELECT * FROM net_users')):raise ValidationError('Нельзя отключить последнего администратора')
            c.execute('UPDATE net_users SET login=?,name=?,roles=?,active=? WHERE id=?',(login,name,dumps(roles),int(active),old['id']))
            if d.get('password'):c.execute('UPDATE net_users SET password=? WHERE id=?',(password_hash(d['password']),old['id']))
            c.execute('DELETE FROM net_sessions WHERE user_id=?',(old['id'],));return old['id']
        return c.execute('INSERT INTO net_users(login,name,password,roles) VALUES(?,?,?,?)',(login,name,password_hash(d.get('password','')),dumps(roles))).lastrowid
    def client(self,c,user,d):
        admin='admin' in user['roles'];ident=int(d.get('id') or 0)
        if ident:self.owns(c,user,ident)
        if not admin and 'trade' not in user['roles']:raise PermissionError('Клиентов создаёт торговля или администратор')
        name=required(d.get('name'),'Название');phone=required(d.get('phone'),'Телефон');digits=''.join(x for x in phone if x.isdigit())
        if len(digits)<10:raise ValidationError('Укажите телефон с кодом')
        normalized=lambda s:''.join(x for x in s if x.isdigit())[-10:]
        duplicates=[p for p in c.execute("SELECT * FROM partners WHERE kind='client' AND id!=?",(ident,)) if normalized(p['phone'])==digits[-10:] or (d.get('inn') and p['inn']==str(d['inn'])) or (' '.join(p['name'].casefold().split())==' '.join(name.casefold().split()) and ' '.join(p['address'].casefold().split())==' '.join(str(d.get('address','')).casefold().split()))]
        if duplicates:raise ValidationError('Похожий клиент уже существует. Администратор проверит совпадение и назначение менеджера')
        old=self.store.one(c,'partners',ident) if ident else {}
        if not admin:
            data=dict(old,kind='client',name=name,phone=phone,address=str(d.get('address','')),contact=str(d.get('contact','')),inn=str(d.get('inn','')),notes=str(d.get('notes','')),manager=user['name'])
            if not ident:data.update(price_list_id=1,payment_days=0,debt_limit=0)
        else:data=dict(old,**{k:v for k,v in d.items() if k not in ('owner_id','status','kind','approved')},kind='client')
        data.pop('id',None)
        if ident:data['id']=ident
        ident=self.store.save_partner(data);owner=int(d.get('owner_id') or user['id']) if admin else user['id'];person=self.store.one(c,'net_users',owner)
        if not person['active']:raise ValidationError('Менеджер отключён')
        previous_meta=c.execute('SELECT * FROM net_clients WHERE client_id=?',(ident,)).fetchone()
        status=d.get('status',previous_meta['status'] if previous_meta else 'prospect')
        if status not in ('prospect','active','paused'):raise ValidationError('Неверный статус клиента')
        if admin and previous_meta and previous_meta['owner_id']!=owner and d.get('transfer_tasks',True):
            for task in c.execute("SELECT * FROM net_tasks WHERE client_id=? AND assignee=? AND status NOT IN ('done','cancelled')",(ident,previous_meta['owner_id'])).fetchall():
                history=json.loads(task['history'])+[{'at':datetime.now().isoformat(),'name':user['name'],'comment':'Передача клиента новому менеджеру','previous_assignee':task['assignee']}]
                c.execute('UPDATE net_tasks SET assignee=?,history=? WHERE id=?',(owner,dumps(history),task['id']))
            c.execute('UPDATE net_rules SET assignee=? WHERE client_id=? AND assignee=?',(owner,ident,previous_meta['owner_id']))
        c.execute('INSERT INTO net_clients(client_id,owner_id,status) VALUES(?,?,?) ON CONFLICT(client_id) DO UPDATE SET owner_id=excluded.owner_id,status=excluded.status',(ident,owner,status))
        if admin:c.execute('UPDATE net_clients SET approved=? WHERE client_id=?',(int(bool(d.get('approved',False))),ident))
        c.execute('UPDATE partners SET manager=? WHERE id=?',(person['name'],ident));return ident
    def rule(self,c,user,d):
        if d.get('id'):
            old=self.store.one(c,'net_rules',d['id']);self.owns(c,user,old['client_id']);reason=required(d.get('reason'),'Причина отключения графика')
            c.execute('UPDATE net_rules SET active=0 WHERE id=?',(old['id'],))
            for task in c.execute("SELECT * FROM net_tasks WHERE source_key LIKE ? AND due>? AND status IN ('assigned','waiting')",('rule:'+str(old['id'])+':%',datetime.now().isoformat(timespec='minutes'))).fetchall():
                c.execute("UPDATE net_tasks SET status='cancelled',history=? WHERE id=?",(dumps(json.loads(task['history'])+[{'at':datetime.now().isoformat(),'name':user['name'],'comment':reason,'event':'График отключён'}]),task['id']))
            return old['id']
        client=int(d.get('client_id') or 0);self.owns(c,user,client);self.store.partner(c,client,'client')
        owner=int(d.get('assignee') or user['id']) if 'admin' in user['roles'] else user['id'];self.store.one(c,'net_users',owner)
        weekdays=d.get('weekdays',[]);kind=d.get('kind');at=str(d.get('at','10:00'))
        if kind not in ('call','visit') or not weekdays or any(type(x)!=int or x not in range(7) for x in weekdays):raise ValidationError('Выберите дни и способ контакта')
        try:datetime.strptime(at,'%H:%M')
        except ValueError:raise ValidationError('Время в формате ЧЧ:ММ')
        interval=int(d.get('interval_weeks') or 1);start=day(d.get('start') or date.today().isoformat())
        if interval<1 or interval>52 or start<date.today().isoformat():raise ValidationError('Период 1–52 недели, начало не раньше сегодня')
        return c.execute('INSERT INTO net_rules(client_id,assignee,kind,weekdays,interval_weeks,at,start,last_generated) VALUES(?,?,?,?,?,?,?,?)',(client,owner,kind,dumps(weekdays),interval,at,start,(date.fromisoformat(start)-timedelta(days=1)).isoformat())).lastrowid
    def generate(self):
        state=self.store.snapshot();clients={p['id']:p for p in state['partners']};today=date.today()
        with self.store.transaction() as c:
            for r in c.execute('SELECT * FROM net_rules WHERE active=1').fetchall():
                dt=max(date.fromisoformat(r['start']),date.fromisoformat(r['last_generated'])+timedelta(days=1))
                horizon=today+timedelta(days=14)
                while dt<=horizon:
                    start=date.fromisoformat(r['start']);anchor=start-timedelta(days=start.weekday())
                    if dt.weekday() in json.loads(r['weekdays']) and ((dt-anchor).days//7)%r['interval_weeks']==0:
                        due=dt.isoformat()+'T'+r['at'];title=('Позвонить: ' if r['kind']=='call' else 'Посетить: ')+clients[r['client_id']]['name']
                        c.execute("INSERT OR IGNORE INTO net_tasks(client_id,assignee,title,kind,due,original_due,source,source_key,created_by) VALUES(?,?,?,?,?,?,'schedule',?,?)",(r['client_id'],r['assignee'],title,r['kind'],due,due,f'rule:{r["id"]}:{dt}',r['assignee']))
                    dt+=timedelta(days=1)
                c.execute('UPDATE net_rules SET last_generated=? WHERE id=?',(horizon.isoformat(),r['id']))
            for m in c.execute('SELECT * FROM net_clients').fetchall():
                client=clients.get(m['client_id']);person=c.execute('SELECT * FROM net_users WHERE id=? AND active=1',(m['owner_id'],)).fetchone()
                if not client or not person:continue
                key='debt:'+str(m['client_id']);task=c.execute('SELECT * FROM net_tasks WHERE source_key=?',(key,)).fetchone()
                if client['overdue']>0:
                    due=(m['promise_date'] if m['promise_date'] and m['promise_date']>today.isoformat() else today.isoformat())+'T18:00'
                    if not task:c.execute("INSERT INTO net_tasks(client_id,assignee,title,kind,due,original_due,source,source_key,created_by) VALUES(?,?,?,'debt',?,?,'debt',?,?)",(m['client_id'],m['owner_id'],'Просроченная оплата: '+client['name'],due,due,key,m['owner_id']))
                    elif task['status'] in ('done','cancelled'):c.execute("UPDATE net_tasks SET status='assigned',due=?,assignee=?,history=? WHERE id=?",(due,m['owner_id'],dumps(json.loads(task['history'])+[{'at':datetime.now().isoformat(),'event':'Новая просроченная задолженность'}]),task['id']))
                elif task and task['status'] not in ('done','cancelled'):c.execute("UPDATE net_tasks SET status='done',history=? WHERE id=?",(dumps(json.loads(task['history'])+[{'at':datetime.now().isoformat(),'event':'Просроченная задолженность погашена'}]),task['id']))
    def task(self,c,user,d):
        admin='admin' in user['roles'];ident=int(d.get('id') or 0)
        if not ident:
            assignee=int(d.get('assignee') or user['id']) if admin else user['id'];self.store.one(c,'net_users',assignee)
            client=int(d.get('client_id') or 0) or None
            if client:self.owns(c,user,client)
            due=str(d.get('due',''));from domain import timestamp
            due=timestamp(due);priority=d.get('priority','normal')
            if priority not in ('normal','high'):raise ValidationError('Неверный приоритет')
            return c.execute('INSERT INTO net_tasks(client_id,assignee,title,kind,due,original_due,source,review,priority,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)',(client,assignee,required(d.get('title'),'Задача'),d.get('kind','task'),due,due,'director' if admin else 'self',int(bool(d.get('review')) and admin),priority,user['id'])).lastrowid
        old=self.store.one(c,'net_tasks',ident)
        if not admin and old['assignee']!=user['id']:raise PermissionError('Задача другого сотрудника')
        status=d.get('status',old['status']);comment=required(d.get('comment'),'Результат или причина');due=str(d.get('due') or old['due']);from domain import timestamp
        due=timestamp(due)
        if status not in ('assigned','in_progress','waiting','review','done','cancelled'):raise ValidationError('Неверный статус')
        if old['source']=='debt' and status=='done' and not admin:raise ValidationError('Долг закрывается только после поступления оплаты')
        if not admin and status=='done' and old['review']:status='review'
        assignee=int(d.get('assignee') or old['assignee']) if admin else old['assignee'];self.store.one(c,'net_users',assignee)
        history=json.loads(old['history'])+[dict(at=datetime.now().isoformat(),user_id=user['id'],name=user['name'],previous_due=old['due'],previous_status=old['status'],previous_assignee=old['assignee'],status=status,comment=comment)]
        c.execute('UPDATE net_tasks SET status=?,due=?,assignee=?,result=?,history=? WHERE id=?',(status,due,assignee,dumps({'comment':comment}),dumps(history),ident));return ident
    def contact(self,c,user,d):
        from domain import number
        client=int(d.get('client_id') or 0);self.owns(c,user,client);self.store.partner(c,client,'client')
        task_id=int(d.get('task_id') or 0) or None
        if task_id:
            task=self.store.one(c,'net_tasks',task_id)
            if task['client_id']!=client or ('admin' not in user['roles'] and task['assignee']!=user['id']):raise PermissionError('Контакт не соответствует задаче')
        method=d.get('method');outcome=d.get('outcome');comment=required(d.get('comment'),'Результат контакта')
        if method not in ('call','visit','incoming') or outcome not in ('order','no_order','no_answer','missed','promise'):raise ValidationError('Выберите способ и результат контакта')
        if outcome=='order':
            order=self.store.one(c,'orders',d.get('order_id'))
            if order['client_id']!=client:raise ValidationError('Заказ другого клиента')
        if outcome=='promise':c.execute('UPDATE net_clients SET promise_date=?,promise_amount=? WHERE client_id=?',(day(d.get('promise_date')),number(d.get('promise_amount'),'Обещанная сумма'),client))
        if task_id and not ((task['kind']=='visit' and method!='visit') or (task['kind']=='call' and method=='incoming')):
            status='waiting' if outcome in ('no_answer','missed','promise') or task['source']=='debt' else 'done'
            if status=='waiting' and not d.get('next_due'):raise ValidationError('Назначьте дату повторного контакта')
            self.task(c,user,dict(id=task_id,status=status,comment=comment,due=d.get('next_due') or task['due']))
        return c.execute('INSERT INTO net_contacts(client_id,user_id,task_id,method,outcome,comment,created,data) VALUES(?,?,?,?,?,?,?,?)',(client,user['id'],task_id,method,outcome,comment,datetime.now().isoformat(),dumps({k:d[k] for k in ('promise_date','promise_amount','order_id') if k in d}))).lastrowid
    def workspace(self,user):
        self.generate();state=self.store.snapshot();admin='admin' in user['roles'];production='production' in user['roles']
        with closing(self.store.connect()) as c:
            meta={r['client_id']:dict(r) for r in c.execute('SELECT * FROM net_clients')}
            permitted={p['id'] for p in state['partners'] if p['kind']=='client' and (admin or production or meta.get(p['id'],{}).get('owner_id')==user['id'])}
            clients=[dict(**{k:p[k] for k in ('id','name','phone','contact','address','inn','manager','price_list_id','payment_days','debt_limit','notes','debt','overdue')},**{k:v for k,v in meta.get(p['id'],{}).items() if k!='client_id'}) for p in state['partners'] if p['id'] in permitted]
            tasks=[dict(r) for r in c.execute('SELECT * FROM net_tasks ORDER BY due,id') if admin or r['assignee']==user['id']]
            for t in tasks:t['history']=json.loads(t['history']);t['result']=json.loads(t['result'])
            contacts=[dict(r) for r in c.execute('SELECT * FROM net_contacts ORDER BY id DESC LIMIT 500') if admin or r['user_id']==user['id']]
            rules=[dict(r) for r in c.execute('SELECT * FROM net_rules') if admin or r['assignee']==user['id']]
        products=[{k:i[k] for k in ('id','name','kind','unit','folder','category','supply_type','mode','portion_weight','available','track_expiry','shelf_hours')} for i in state['items'] if i['active'] and (admin or production or i['kind']=='finished')]
        for i in products:i['free']=max(0,i['available']-sum(a['covered'] for a in state['allocations'] if a['item_id']==i['id']))
        quote={(v['list_id'],v['item_id']):v for v in state['pricing']['cells']}
        prices={p['id']:{i['id']:quote.get((p['price_list_id'],i['id']),{'price':None,'reason':'Нет цены'}) for i in products if i['kind']=='finished'} for p in clients}
        orders=[o for o in state['orders'] if o['client_id'] in permitted and o['status']!='deleted'];oids={o['id'] for o in orders}
        if production and not admin and 'trade' not in user['roles']:
            for p in clients:
                for k in ('debt','overdue','debt_limit','payment_days','notes'):p.pop(k,None)
        result=dict(pricing_token=state['pricing']['token'],user=user,version='0.6.0',clients=clients,products=products,prices=prices,tasks=tasks,contacts=contacts,rules=rules,orders=orders,lines=[l for l in state['order_lines'] if l['order_id'] in oids],company=state['company'],debts=[d for d in state['debts'] if d['client_id'] in permitted] if admin or 'trade' in user['roles'] else [],users=self.users() if admin else [],price_lists=state['pricing']['lists'] if admin else [])
        if admin or production:
            with closing(self.store.connect()) as c:result['receipt_photos']=[dict(r) for r in c.execute('SELECT p.id,p.document_id,d.date FROM net_photos p JOIN documents d ON d.id=p.document_id ORDER BY p.id DESC LIMIT 100')]
            result.update(recipes=state['recipes'],lots=[{k:l[k] for k in ('id','item_id','code','produced','expires','qty','blocked')} for l in state['lots']],employees=[{k:e[k] for k in ('id','name','active')} for e in state['employees']],suppliers=[{k:p[k] for k in ('id','name')} for p in state['partners'] if p['kind']=='supplier'])
        if admin:
            today=date.today().isoformat();team=[]
            with closing(self.store.connect()) as c:
                for person in result['users']:
                    authored={int(r['entity']) for r in c.execute("SELECT entity FROM net_audit WHERE user_id=? AND action='/api/orders' AND substr(at,1,10)=?",(person['id'],today)) if r['entity'].isdigit()}
                    owned=[o for o in orders if o['id'] in authored and o['created'][:10]==today];assigned=[t for t in tasks if t['assignee']==person['id'] and t['original_due'][:10]==today]
                    team.append(dict(name=person['name'],planned=len(assigned),completed=sum(t['status']=='done' for t in assigned),contacts=sum(x['user_id']==person['id'] and x['created'][:10]==today for x in contacts),orders=len(owned),order_amount=sum(l['qty']*l['price'] for l in state['order_lines'] if l['order_id'] in {o['id'] for o in owned})))
            result['team']=team
            with closing(self.store.connect()) as c:result['audit']=[dict(r) for r in c.execute('SELECT a.at,u.name,a.action,a.entity FROM net_audit a LEFT JOIN net_users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 100')]
        if admin:result['financial']=self.store.financial_report(date.today().replace(day=1).isoformat(),date.today().isoformat())
        return result
    def authorize(self,user,path,d=None):
        if 'admin' in user['roles']:return
        allowed_get={'/api/mobile/state','/api/auth/me','/api/client-version'}
        if path in allowed_get:return
        if path in ('/api/plan','/api/receipt-photo') and 'production' in user['roles']:return
        trade={'/api/orders','/api/net/clients','/api/net/rules','/api/net/tasks','/api/net/contacts'}
        prod={'/api/production','/api/receipt','/api/shipment','/api/writeoff','/api/inventory','/api/release','/api/jobs','/api/net/tasks'}
        if ('trade' in user['roles'] and path in trade) or ('production' in user['roles'] and path in prod):return
        raise PermissionError('Раздел недоступен для вашей роли')
    def prepare(self,c,user,path,d):
        d=json.loads(dumps(d));d['responsible']=user['name'];d['modified_by']=user['name']
        if path=='/api/orders' and 'admin' not in user['roles']:
            client=int(d.get('client_id') or 0);self.owns(c,user,client)
            if d.get('id'):self.owns(c,user,self.store.one(c,'orders',d['id'])['client_id'])
            d['manager']=user['name']
            for line in d.get('lines',[]):
                if line.get('price_mode') not in (None,'auto','saved'):raise PermissionError('Ручные цены доступны администратору')
                line['price_mode']=line.get('price_mode','auto')
        if path=='/api/shipment':
            if 'admin' not in user['roles'] and (d.get('override_reason') or float(d.get('discount_percent') or 0)):raise PermissionError('Скидки и превышение лимита согласует администратор')
            order=self.store.one(c,'orders',d.get('order_id'))
            meta=c.execute('SELECT * FROM net_clients WHERE client_id=?',(order['client_id'],)).fetchone()
            if meta and not meta['approved'] and 'admin' not in user['roles']:raise ValidationError('Нового клиента должен подтвердить администратор перед отгрузкой')
            c.execute("UPDATE net_clients SET status='active' WHERE client_id=?",(order['client_id'],))
        return d
    def resolve_request(self,user,request_id):
        if not request_id or len(request_id)>100:raise ValidationError('Неверный идентификатор отправки')
        with self.store.transaction() as c:
            old=c.execute('SELECT * FROM net_requests WHERE user_id=? AND request_id=?',(user['id'],request_id)).fetchone()
            if old:return dict(committed=old['path']!='cancelled',response=json.loads(old['response']))
            restore=c.execute("SELECT value FROM settings WHERE key='last_restore'").fetchone()
            if restore and json.loads(restore['value']).get('request_id')==request_id:return dict(committed=True,response={'id':'восстановление базы'})
            c.execute('INSERT INTO net_requests VALUES(?,?,?, ?,?)',(user['id'],request_id,'cancelled','',dumps({'cancelled':True})))
            self.audit(c,user,'request-cancel',request_id)
            return dict(committed=False,response={'cancelled':True})

    def request(self,user,path,d,request_id,action):
        if not request_id or len(request_id)>100:raise ValidationError('Нужен уникальный идентификатор операции')
        self.authorize(user,path,d);body_hash=hashlib.sha256(dumps(d).encode()).hexdigest()
        with self.store.transaction() as c:
            previous=c.execute('SELECT * FROM net_requests WHERE user_id=? AND request_id=?',(user['id'],request_id)).fetchone()
            if previous:
                if previous['path']=='cancelled':raise ValidationError('Прежняя отправка отменена. Проверьте данные и отправьте новую операцию')
                if previous['path']!=path or previous['body_hash']!=body_hash:raise ValidationError('Идентификатор операции уже использован с другими данными')
                return json.loads(previous['response'])
            prepared=self.prepare(c,user,path,d)
            handlers={'/api/net/users':self.save_user,'/api/net/clients':self.client,'/api/net/rules':self.rule,'/api/net/tasks':self.task,'/api/net/contacts':self.contact}
            ident=handlers[path](c,user,prepared) if path in handlers else action(prepared)
            if path=='/api/receipt' and prepared.get('photo'):
                import base64
                photo=prepared['photo'];raw=base64.b64decode(photo.get('data',''),validate=True);mime=photo.get('mime')
                if len(raw)>5*1024*1024 or not ((mime=='image/jpeg' and raw.startswith(b'\xff\xd8\xff')) or (mime=='image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n'))):raise ValidationError('Фото: JPEG/PNG, не более 5 МБ')
                c.execute('INSERT INTO net_photos(document_id,mime,image) VALUES(?,?,?)',(ident,mime,raw))
            response=ident if path=='/api/prices' else {'id':ident};self.audit(c,user,path,ident)
            c.execute('INSERT INTO net_requests VALUES(?,?,?,?,?)',(user['id'],request_id,path,body_hash,dumps(response)))
            return response
