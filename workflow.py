"""Production planning, piecework and exchanges. Uses the same SQLite transactions as Store."""
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, date

class WorkflowMixin:
    @staticmethod
    def migrate_v3(c):
        c.executescript('''BEGIN IMMEDIATE;
            ALTER TABLE items ADD COLUMN labor_rate REAL NOT NULL DEFAULT 0;
            ALTER TABLE items ADD COLUMN overhead_rate REAL NOT NULL DEFAULT 0;
            ALTER TABLE items ADD COLUMN overhead_percent REAL NOT NULL DEFAULT 0;
            ALTER TABLE items ADD COLUMN production_buffer REAL NOT NULL DEFAULT 0;
            ALTER TABLE recipes ADD COLUMN author TEXT NOT NULL DEFAULT '';
            CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE production_jobs(id INTEGER PRIMARY KEY, date TEXT NOT NULL, responsible TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', products TEXT NOT NULL, created TEXT NOT NULL, revisions TEXT NOT NULL DEFAULT '[]');
            PRAGMA user_version=3; COMMIT;''')

    def company(self,c):
        return json.loads(c.execute("SELECT value FROM settings WHERE key='company'").fetchone()[0])

    def save_employee(self,d):
        from domain import required
        name=required(d.get('name'),'ФИО сотрудника')
        with self.transaction() as c:
            if d.get('id'):
                self.one(c,'employees',d['id'])
                c.execute('UPDATE employees SET name=?,active=? WHERE id=?',(name,int(bool(d.get('active',True))),d['id']))
                return d['id']
            return c.execute('INSERT INTO employees(name,active) VALUES(?,?)',(name,int(bool(d.get('active',True))))).lastrowid

    def production_workers(self,c,item,q,d,existing_ids=()):
        from domain import ValidationError, money_round, EPS
        workers=[]; seen=set()
        for row in d.get('workers',[]):
            person=self.one(c,'employees',row.get('employee_id'))
            if (not person['active'] and person['id'] not in existing_ids) or person['id'] in seen:raise ValidationError('Исполнитель в архиве или повторяется')
            seen.add(person['id']);qty=self.quantity(item,row.get('qty'))
            workers.append(dict(employee_id=person['id'],name=person['name'],qty=qty,unit=item['unit'],rate=item['labor_rate'],amount=money_round(qty*item['labor_rate'])))
        if workers and abs(sum(w['qty'] for w in workers)-q)>EPS:raise ValidationError('Выработка исполнителей должна совпадать с фактическим выпуском')
        if item['labor_rate'] and not workers:raise ValidationError('При сдельной расценке укажите исполнителей и их выработку')
        return workers,money_round(sum(w['amount'] for w in workers))

    def production_job(self,c,item,d):
        from domain import ValidationError
        if not d.get('job_id'):return None
        job=self.one(c,'production_jobs',d['job_id'])
        if job['status']!='active':raise ValidationError('Наряд закрыт')
        if not any(p['item_id']==item['id'] for p in json.loads(job['products'])):raise ValidationError('Продукция отсутствует в наряде')
        return job['id']

    def allocate_orders(self,c,exclude_order=None):
        """Allocate each lot once, FEFO, by promised delivery time (including completed portions)."""
        from domain import EPS,remaining_quantity,remaining_portions
        now=datetime.now().isoformat(timespec='minutes')
        lots=[dict(r) for r in c.execute("SELECT * FROM lots WHERE qty>? AND blocked=0 AND produced<=? ORDER BY CASE WHEN expires='' THEN '9999' ELSE expires END,id",(EPS,now))]
        result=[]
        orders=c.execute("SELECT l.*,o.due,o.client_id,o.driver_id,o.manager FROM order_lines l JOIN orders o ON o.id=l.order_id WHERE o.status='open' ORDER BY o.due,o.id,l.id").fetchall()
        for row in orders:
            if exclude_order and row['order_id']==int(exclude_order):continue
            remaining=remaining_quantity(row);needed=remaining;chosen=[]
            for lot in lots:
                if lot['item_id']!=row['item_id'] or (lot['expires'] and lot['expires']<=max(row['due'],now)):continue
                take=min(needed,lot['qty'])
                if take>EPS:
                    lot['qty']-=take;needed-=take;chosen.append({'lot_id':lot['id'],'code':lot['code'],'qty':take})
                if needed<=EPS:break
            result.append(dict(order_id=row['order_id'],line_id=row['id'],item_id=row['item_id'],due=row['due'],client_id=row['client_id'],driver_id=row['driver_id'],manager=row['manager'],ordered=row['qty'],shipped=row['shipped'],remaining=remaining,order_mode=row['order_mode'],portions=row['portions'],shipped_portions=row['shipped_portions'],remaining_portions=remaining_portions(row),portion_weight=row['portion_weight'],covered=remaining-needed,shortage=max(0,needed),lots=chosen))
        return result

    def production_plan(self,due):
        from domain import day,EPS,ValidationError
        due=day(due)
        with closing(self.connect()) as c:
            c.execute('BEGIN')
            alloc=self.allocate_orders(c); rows=[];ingredients={}
            ids=sorted({a['item_id'] for a in alloc if a['due'][:10]==due} | {r[0] for r in c.execute("SELECT DISTINCT l.item_id FROM order_lines l JOIN orders o ON o.id=l.order_id WHERE substr(o.due,1,10)=? AND o.status NOT IN ('closed','deleted')",(due,))})
            for iid in ids:
                item=self.one(c,'items',iid);lines=[a for a in alloc if a['due'][:10]==due and a['item_id']==iid]
                ordered=sum(a['remaining'] for a in lines);covered=sum(a['covered'] for a in lines);need=sum(a['shortage'] for a in lines)
                target=need*(1+item['production_buffer']/100)
                if item['unit'] in ('шт.','уп.'):target=math.ceil(target-EPS)
                error=''
                if target>EPS:
                    try:
                        for key,qty in self.requirements(c,iid,target).items():ingredients[key]=ingredients.get(key,0)+qty
                    except ValidationError as e:error=str(e)
                prior=sum(a['remaining'] for a in alloc if a['due'][:10]<due and a['item_id']==iid)
                available=c.execute("SELECT coalesce(sum(qty),0) FROM lots WHERE item_id=? AND blocked=0 AND produced<=? AND (expires='' OR expires>?)",(iid,datetime.now().isoformat(timespec='minutes'),due+'T23:59')).fetchone()[0]
                totals=c.execute("SELECT coalesce(sum(l.qty),0),coalesce(sum(l.shipped),0) FROM order_lines l JOIN orders o ON o.id=l.order_id WHERE substr(o.due,1,10)=? AND o.status NOT IN ('closed','deleted') AND l.item_id=?",(due,iid)).fetchone()
                rows.append(dict(item_id=iid,name=item['name'],unit=item['unit'],ordered=ordered,remaining_portions=sum(a['remaining_portions'] for a in lines),shipped=totals[1],total_ordered=totals[0],available=available,covered=covered,free=covered,prior_demand=prior,produce=target,shortage=need,buffer=item['production_buffer'],error=error))
            inputs=[];now=datetime.now().isoformat(timespec='minutes')
            for iid,qty in ingredients.items():
                item=self.one(c,'items',iid)
                if item['unit'] in ('шт.','уп.'):qty=math.ceil(qty-EPS)
                available=c.execute("SELECT coalesce(sum(qty),0) FROM lots WHERE item_id=? AND blocked=0 AND produced<=? AND (expires='' OR expires>?)",(iid,now,now)).fetchone()[0]
                inputs.append(dict(name=item['name'],unit=item['unit'],qty=qty,available=available,shortage=max(0,qty-available)))
            jobs=[]
            for r in c.execute('SELECT * FROM production_jobs WHERE date=? ORDER BY id DESC',(due,)):
                job=dict(r);job['products']=json.loads(job['products']);job['progress']=[]
                for p in job['products']:
                    done=sum(json.loads(d['data'])['qty'] for d in c.execute("SELECT id,data FROM documents WHERE kind='production' AND json_extract(data,'$.job_id')=? AND json_extract(data,'$.item_id')=?",(job['id'],p['item_id'])) if d['id'] not in self.reversed_ids(c))
                    live=next((x for x in rows if x['item_id']==p['item_id']),None)
                    remaining=max(0,p['qty']-done);current=live['produce'] if live else 0
                    job['progress'].append(dict(p,produced=done,remaining=remaining,current=current,change=current-remaining))
                if job['status']=='active':
                    known={p['item_id'] for p in job['products']}
                    for live in rows:
                        if live['item_id'] not in known and live['produce']>EPS:
                            job['progress'].append(dict(item_id=live['item_id'],name=live['name'],unit=live['unit'],qty=0,produced=0,remaining=0,current=live['produce'],change=live['produce']))
                job['revisions']=json.loads(job['revisions'])
                jobs.append(job)
            return dict(date=due,products=rows,ingredients=inputs,orders=[a for a in alloc if a['due'][:10]==due],jobs=jobs,prior_uncovered=[a for a in alloc if a['due'][:10]<due and a['shortage']>EPS])

    def save_job(self,d):
        from domain import day,required,dumps,ValidationError
        # Plan and write under one immediate transaction, so a parallel save cannot replace the reviewed state.
        with self.transaction() as c:
            dt=day(d.get('date'));who=required(d.get('responsible'),'Ответственный')
            if d.get('action')=='close':
                self.one(c,'production_jobs',d.get('id'));c.execute("UPDATE production_jobs SET status='closed' WHERE id=?",(d['id'],));return d['id']
            if d.get('action')=='review':
                job=self.one(c,'production_jobs',d.get('id'))
                if job['status']!='active' or job['date']!=dt:raise ValidationError('Наряд закрыт или дата не совпадает')
                plan=self.production_plan(dt);review=next(j for j in plan['jobs'] if j['id']==job['id'])
                if any(r['error'] for r in plan['products']):raise ValidationError('Проверьте ТТК новых позиций наряда')
                products=[dict(item_id=p['item_id'],name=p['name'],unit=p['unit'],qty=p['produced']+p['current']) for p in review['progress']]
                history=json.loads(job['revisions']);history.append({'at':datetime.now().isoformat(),'responsible':who,'previous_products':json.loads(job['products'])})
                c.execute('UPDATE production_jobs SET products=?,revisions=? WHERE id=?',(dumps(products),dumps(history),job['id']))
                return job['id']
            if c.execute("SELECT 1 FROM production_jobs WHERE date=? AND status='active'",(dt,)).fetchone():raise ValidationError('На дату уже есть наряд в работе. Закройте его перед созданием нового')
            plan=self.production_plan(dt)
            products=[dict(item_id=r['item_id'],name=r['name'],unit=r['unit'],qty=r['produce'],portions=r.get('remaining_portions',0)) for r in plan['products'] if r['produce']>1e-7]
            if not products:raise ValidationError('Нет потребности в производстве')
            if any(r['error'] for r in plan['products']):raise ValidationError('Для всех товаров наряда нужны корректные ТТК')
            return c.execute('INSERT INTO production_jobs(date,responsible,products,created) VALUES(?,?,?,?)',(dt,who,dumps(products),datetime.now().isoformat())).lastrowid

    def exchange(self,d):
        from domain import ValidationError,required,dumps,EPS,timestamp,money_round
        with self.transaction() as c:
            original=self.one(c,'documents',d.get('shipment_id'))
            if original['kind']!='shipment' or original['id'] in self.reversed_ids(c):raise ValidationError('Выберите действующую отгрузку')
            reason=required(d.get('reason'),'Причина обмена');source=json.loads(original['data'])
            if str(d.get('date',date.today().isoformat()))<original['date']:raise ValidationError('Обмен не может быть раньше отгрузки')
            ident=self.doc(c,'exchange',dict(d,partner_id=original['partner_id']));incoming=[];outgoing=[];returned_cost=0;issued_cost=0;seen=set()
            prior={}
            for doc in c.execute("SELECT id,data FROM documents WHERE kind IN ('return','exchange')"):
                if doc['id']==ident or doc['id'] in self.reversed_ids(c):continue
                data=json.loads(doc['data'])
                if data.get('shipment_id')==original['id']:
                    for l in data['lines']:prior[l['line_id']]=prior.get(l['line_id'],0)+l['qty']
            for row in d.get('lines',[]):
                line=next((l for l in source['lines'] if l['line_id']==int(row.get('line_id',0))),None)
                if not line or line['line_id'] in seen:raise ValidationError('Неверная строка обмена')
                seen.add(line['line_id']);item=self.one(c,'items',line['item_id']);q=self.quantity(item,row.get('qty'));already=prior.get(line['line_id'],0)
                if already+q>line['qty']+EPS:raise ValidationError('Возврат и обмен превышают отгруженное количество')
                left=q;skip=already;lots=[]
                for old in line['lots']:
                    skipped=min(skip,old['qty']);skip-=skipped;take=min(left,old['qty']-skipped)
                    if take<=EPS:continue
                    lid=self.add_lot(c,ident,item,take,old['unit_cost'],old['produced'],old['expires'],f'ОБ-{ident}-{old["lot_id"]}',preserve_expiry=True)
                    c.execute('UPDATE lots SET blocked=1 WHERE id=?',(lid,));lots.append(dict(lot_id=lid,qty=take,unit_cost=old['unit_cost'],source_lot_id=old['lot_id']));returned_cost+=take*old['unit_cost'];left-=take
                    if left<=EPS:break
                incoming.append(dict(item_id=item['id'],name=item['name'],unit=item['unit'],qty=q,price=0,line_total=0,original_value=money_round(q*line['price']),line_id=line['line_id'],lots=lots))
            at=timestamp(d.get('at',datetime.now().isoformat(timespec='minutes')))
            if at[:10]!=str(d.get('date',date.today().isoformat())) or at>datetime.now().isoformat(timespec='minutes'):raise ValidationError('Неверное время обмена')
            for row in d.get('replacements',[]):
                item=self.one(c,'items',row.get('item_id'))
                if item['kind'] not in ('finished','semi') or item['mode']=='virtual':raise ValidationError('Замена должна быть складской продукцией')
                q=self.quantity(item,row.get('qty'));value,lots=self.consume(c,ident,item['id'],q,at);issued_cost+=value
                outgoing.append(dict(item_id=item['id'],name=item['name'],unit=item['unit'],qty=q,lots=lots))
            if not incoming or not outgoing:raise ValidationError('Укажите возвращённую продукцию и выдаваемую замену')
            # Both sides must have the same agreed sale value; otherwise use a return plus a new shipment.
            returned_value=sum(l['qty']*next(x['price'] for x in source['lines'] if x['line_id']==l['line_id']) for l in incoming)
            original_ids={l['item_id'] for l in incoming}
            if any(l['item_id'] not in original_ids for l in outgoing):
                replacement_value=sum(l['qty']*self.one(c,'items',l['item_id'])['sale_price'] for l in outgoing)
            else:
                replacement_value=sum(l['qty']*next(x['price'] for x in source['lines'] if x['item_id']==l['item_id']) for l in outgoing)
            if abs(returned_value-replacement_value)>.01:raise ValidationError('Для обмена без доплаты стоимость замены должна совпадать. Иначе оформите возврат и новую отгрузку')
            data=dict(shipment_id=original['id'],client_name=source['client_name'],reason=reason,lines=incoming,replacements=outgoing,returned_cost=returned_cost,replacement_cost=issued_cost)
            c.execute('UPDATE documents SET cost=?,data=? WHERE id=?',(issued_cost-returned_cost,dumps(data),ident))
            return ident

    def refund(self,d):
        from domain import ValidationError,number,required,dumps,money_round
        with self.transaction() as c:
            client=self.partner(c,d.get('partner_id'),'client');amount=money_round(number(d.get('amount'),'Сумма возврата денег'))
            if amount<=0:raise ValidationError('Сумма должна быть не меньше 0,01 ₽')
            if amount>max(0,-self.balance(c,client['id']))+.0000001:raise ValidationError('Возврат денег превышает аванс клиента')
            return self.doc(c,'refund',dict(d,partner_id=client['id']),total=amount,data={'client_name':client['name'],'reason':required(d.get('reason'),'Основание возврата денег')})

    def payroll(self,start,end):
        state=self.snapshot();rows=[]
        for doc in state['documents']:
            if doc['kind']!='production' or doc['reversed'] or not start<=doc['date']<=end:continue
            for w in doc['data'].get('workers',[]):rows.append(dict(w,date=doc['date'],document_id=doc['id'],product=doc['data']['name']))
        return rows

    def expense_details(self,c,d,amount):
        from domain import required,ValidationError,money_round,day
        data={'category':required(d.get('category'),'Статья расхода'),'allocations':[],'purpose':str(d.get('purpose') or d.get('note') or '').strip(),'recipient':str(d.get('recipient') or '').strip()}
        if data['category'].strip().lower()=='зарплата' and not d.get('employee_id'):raise ValidationError('Для выплаты зарплаты выберите сотрудника: начисление уже включено в себестоимость')
        if d.get('employee_id'):
            person=self.one(c,'employees',d['employee_id']);accrued=0;paid=0;reversed_ids=self.reversed_ids(c)
            for doc in c.execute("SELECT id,kind,data,total FROM documents WHERE kind IN ('production','expense')"):
                if doc['id'] in reversed_ids:continue
                payload=json.loads(doc['data'])
                if doc['kind']=='production':accrued+=sum(w['amount'] for w in payload.get('workers',[]) if w['employee_id']==person['id'])
                elif payload.get('employee_id')==person['id'] and payload.get('already_in_cost'):paid+=doc['total']
            if amount>accrued-paid+.001:raise ValidationError('Выплата превышает начисленную сотруднику зарплату')
            data.update(employee_id=person['id'],employee_name=person['name'],already_in_cost=True)
            return data
        if d.get('allocation_date'):
            dt=day(d['allocation_date']);driver=d.get('allocation_driver_id')
            docs=[]
            for r in c.execute("SELECT * FROM documents WHERE kind='shipment' AND date=? ORDER BY id",(dt,)):
                if r['id'] in self.reversed_ids(c):continue
                payload=json.loads(r['data'])
                if driver and payload.get('driver_id')!=int(driver):continue
                docs.append(dict(r))
            if not docs:raise ValidationError('На дату и водителя нет проведённых отгрузок для распределения расхода')
            total=sum(r['total'] for r in docs);left=amount
            for n,r in enumerate(docs):
                share=left if n==len(docs)-1 else money_round(amount*(r['total']/total if total else 1/len(docs)))
                share=min(share,left);left=money_round(left-share);data['allocations'].append({'document_id':r['id'],'amount':share})
            data.update(allocation_date=dt,allocation_driver_id=int(driver) if driver else None)
        return data
