"""Auditable document removal and dependent cost/payroll recalculation."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime,date

class AdjustmentsMixin:
    def initialise_cost_dependencies(self):
        with closing(self.connect()) as c:
            if c.execute("SELECT 1 FROM settings WHERE key='cost_dependencies_v1'").fetchone():return
            if c.execute("SELECT 1 FROM documents WHERE kind='production'").fetchone():
                backup=self.path.parent/'backups'/f'before-cost-recalculation-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3'
                backup.parent.mkdir(exist_ok=True)
                with closing(sqlite3.connect(backup)) as target:c.backup(target)
        with self.transaction() as c:
            if c.execute("SELECT 1 FROM settings WHERE key='cost_dependencies_v1'").fetchone():return
            ids={r[0] for r in c.execute('SELECT id FROM items')}
            self.recalculate_costs(c,ids,'Обновление 0.3.1','Первичный пересчёт по текущим расценкам')
            c.execute("INSERT INTO settings(key,value) VALUES('cost_dependencies_v1','1')")

    def recalculate_costs(self,c,item_ids,who,reason):
        """Replay costs in document creation order; quantities and client amounts stay intact."""
        from domain import money_round,dumps,ValidationError
        items={r['id']:dict(r) for r in c.execute('SELECT * FROM items')}
        lots={r['id']:dict(r) for r in c.execute('SELECT * FROM lots')}
        reversed_ids=self.reversed_ids(c);changed=[]
        def refresh_used(used):
            for lot in used:
                lot['unit_cost']=lots[lot['lot_id']]['unit_cost']
            return sum(l['qty']*l['unit_cost'] for l in used)
        def put_lot(lid,value):
            if abs(lots[lid]['unit_cost']-value)>1e-10:
                lots[lid]['unit_cost']=value
                c.execute('UPDATE lots SET unit_cost=? WHERE id=?',(value,lid))
        for doc in list(c.execute('SELECT * FROM documents ORDER BY id')):
            if doc['id'] in reversed_ids or doc['kind']=='reversal':continue
            data=json.loads(doc['data']);before=json.loads(doc['data']);cost=doc['cost']
            if doc['kind']=='production':
                it=items[data['item_id']];materials=sum(refresh_used(x['lots']) for x in data['inputs'])
                targeted=it['id'] in item_ids
                rate=it['labor_rate'] if targeted else data.get('labor_rate',0)
                overhead_rate=it['overhead_rate'] if targeted else data.get('overhead_rate',0)
                percent=it['overhead_percent'] if targeted else data.get('overhead_percent',0)
                workers=data.get('workers',[])
                for w in workers:w.update(rate=rate,amount=money_round(w['qty']*rate))
                labor=money_round(sum(w['amount'] for w in workers)) if workers else money_round(data['qty']*rate)
                overhead=money_round(data['qty']*overhead_rate+materials*percent/100)
                cost=materials+labor+overhead
                data.update(materials_cost=materials,labor_rate=rate,labor_cost=labor,overhead_rate=overhead_rate,overhead_percent=percent,overhead_cost=overhead,workers=workers)
                if rate and not workers:data['pending_workers']=True
                else:data.pop('pending_workers',None)
                put_lot(data['lot_id'],cost/data['qty'])
            elif doc['kind']=='shipment':cost=sum(refresh_used(l['lots']) for l in data['lines'])
            elif doc['kind'] in ('return','exchange'):
                incoming=0
                for line in data['lines']:
                    for lot in line['lots']:
                        lid=lot['lot_id'];origin=lot.get('source_lot_id')
                        if not origin:
                            code=lots[lid]['code'];prefix=('В-' if doc['kind']=='return' else 'ОБ-')+str(doc['id'])+'-'
                            if code.startswith(prefix) and code[len(prefix):].isdigit():origin=int(code[len(prefix):])
                        if origin not in lots:raise ValidationError(f'Невозможно установить исходную партию возврата № {doc["id"]}; пересчёт не выполнен')
                        lot['source_lot_id']=origin;put_lot(lid,lots[origin]['unit_cost']);lot['unit_cost']=lots[origin]['unit_cost'];incoming+=lot['qty']*lot['unit_cost']
                cost=incoming
                if doc['kind']=='exchange':
                    outgoing=sum(refresh_used(l['lots']) for l in data['replacements'])
                    data.update(returned_cost=incoming,replacement_cost=outgoing);cost=outgoing-incoming
            elif doc['kind']=='writeoff':cost=refresh_used(data['lots'])
            elif doc['kind']=='inventory':cost=sum(l['difference']*lots[l['lot_id']]['unit_cost'] for l in data['lines'])
            if data!=before or abs(cost-doc['cost'])>1e-8:
                history=data.setdefault('cost_history',[])
                history.append({'at':datetime.now().isoformat(),'responsible':who,'reason':reason,'previous_cost':doc['cost'],
                                'previous_labor_rate':before.get('labor_rate'), 'previous_workers':before.get('workers',[])})
                c.execute('UPDATE documents SET cost=?,data=? WHERE id=?',(cost,dumps(data),doc['id']));changed.append(doc['id'])
        # Reversals and inventories must retain the same valuation as their source lots too.
        c.execute('UPDATE movements SET unit_cost=(SELECT unit_cost FROM lots WHERE lots.id=movements.lot_id) WHERE unit_cost!=(SELECT unit_cost FROM lots WHERE lots.id=movements.lot_id)')
        return changed

    def recalculate(self,d):
        from domain import required
        who=required(d.get('responsible'),'Ответственный')
        with self.transaction() as c:
            ids={r[0] for r in c.execute('SELECT id FROM items')}
            self.recalculate_costs(c,ids,who,str(d.get('reason') or 'Пересчёт по текущим расценкам карточек'))
        return 1

    def update_production_workers(self,d):
        from domain import required,dumps,ValidationError
        with self.transaction() as c:
            doc=self.one(c,'documents',d.get('document_id'));data=json.loads(doc['data'])
            if doc['kind']!='production' or doc['id'] in self.reversed_ids(c):raise ValidationError('Изменять исполнителей можно у действующего выпуска')
            who=required(d.get('responsible'),'Ответственный');reason=required(d.get('reason'),'Причина исправления')
            item=self.one(c,'items',data['item_id'])
            workers,labor=self.production_workers(c,item,data['qty'],d,existing_ids={w['employee_id'] for w in data.get('workers',[])})
            if not workers:raise ValidationError('Укажите исполнителей и распределите выработку')
            data.setdefault('worker_history',[]).append({'at':datetime.now().isoformat(),'responsible':who,'reason':reason,'previous_workers':data.get('workers',[])})
            data['workers']=workers;c.execute('UPDATE documents SET data=? WHERE id=?',(dumps(data),doc['id']))
            self.recalculate_costs(c,{item['id']},who,reason)
            return doc['id']

    def delete_document(self,d):
        from domain import required,dumps,ValidationError
        with self.transaction() as c:
            doc=self.one(c,'documents',d.get('document_id'));data=json.loads(doc['data'])
            if data.get('deleted'):raise ValidationError('Документ уже удалён')
            if doc['kind']=='reversal':raise ValidationError('Служебная запись отмены сохраняется в истории')
            who=required(d.get('responsible'),'Ответственный');reason=required(d.get('reason'),'Причина удаления')
            if doc['id'] not in self.reversed_ids(c):
                reversal=self.reverse_in_transaction(c,dict(d,date=date.today().isoformat()))
                service=self.one(c,'documents',reversal);payload=json.loads(service['data']);payload['deletion']=True
                c.execute('UPDATE documents SET data=? WHERE id=?',(dumps(payload),reversal))
            data['deleted']={'at':datetime.now().isoformat(),'responsible':who,'reason':reason}
            c.execute('UPDATE documents SET data=? WHERE id=?',(dumps(data),doc['id']))
            return doc['id']

    def delete_order(self,d):
        from domain import required,ValidationError
        with self.transaction() as c:
            order=self.one(c,'orders',d.get('id'))
            if order['status']=='deleted':raise ValidationError('Заказ уже удалён')
            if any(r['id'] not in self.reversed_ids(c) for r in c.execute("SELECT id FROM documents WHERE kind='shipment' AND json_extract(data,'$.order_id')=?",(order['id'],))):
                raise ValidationError('Сначала удалите или отмените отгрузки по заказу')
            who=required(d.get('responsible'),'Ответственный');reason=required(d.get('reason'),'Причина удаления')
            c.execute("UPDATE orders SET status='deleted',note=note||? WHERE id=?",(f'\nУдалён {datetime.now().isoformat()}: {reason}. {who}',order['id']))
            return order['id']

    def dispatch_summary(self,d):
        from domain import required,ValidationError,dumps,remaining_quantity,remaining_portions
        raw=d.get('order_ids',[])
        if not isinstance(raw,list) or not raw or len(raw)>5000:raise ValidationError('Отметьте заказы для сводной ведомости')
        ids=[int(v) for v in raw]
        if len(ids)!=len(set(ids)):raise ValidationError('Заказ повторяется')
        mode=d.get('mode','remaining')
        if mode not in ('remaining','ordered'):raise ValidationError('Неверный режим количества')
        with self.transaction() as c:
            products={};orders=[]
            for oid in ids:
                order=self.one(c,'orders',oid)
                if order['status']=='deleted' or (mode=='remaining' and order['status']=='closed'):raise ValidationError(f'Заказ № {oid} удалён или закрыт вручную')
                partner=self.one(c,'partners',order['client_id'])
                details=[]
                for line in c.execute('SELECT * FROM order_lines WHERE order_id=? ORDER BY id',(oid,)):
                    qty=remaining_quantity(line) if mode=='remaining' else line['qty']
                    portions=remaining_portions(line) if mode=='remaining' else line['portions']
                    if qty<=1e-7:continue
                    item=self.one(c,'items',line['item_id']);r=products.setdefault(item['id'],dict(item_id=item['id'],name=item['name'],unit=item['unit'],qty=0,portions=0));r['qty']+=qty;r['portions']+=portions
                    details.append(dict(item_id=item['id'],name=item['name'],qty=qty,unit=item['unit'],portions=portions,order_mode=line['order_mode']))
                orders.append(dict(id=oid,client_name=partner['name'],due=order['due'],manager=order['manager'],lines=details))
            if not products:raise ValidationError('В выбранных заказах нет количества к отгрузке; для повторной ведомости выберите полное количество заказа')
            lines=sorted(products.values(),key=lambda r:r['name'].casefold())
            return self.doc(c,'dispatch_summary',d,data={'mode':mode,'orders':orders,'lines':lines})

    def financial_report(self,start,end):
        from domain import day,ValidationError,money_round
        start=day(start);end=day(end)
        if start>end:raise ValidationError('Начало периода должно быть не позже окончания')
        docs=[d for d in self.snapshot()['documents'] if not d['reversed'] and start<=d['date']<=end]
        sales=[d for d in docs if d['kind'] in ('shipment','return')]
        revenue=money_round(sum(d['total']*(-1 if d['kind']=='return' else 1) for d in sales))
        cogs=money_round(sum(d['cost']*(-1 if d['kind']=='return' else 1) for d in sales))
        expenses=[d for d in docs if d['kind']=='expense'];groups={};details=[]
        for d in expenses:
            data=d['data'];category=data.get('category') or 'Без статьи';cash=d['total'];salary=cash if data.get('already_in_cost') else 0;deductible=cash-salary
            r=groups.setdefault(category,dict(category=category,cash=0,deductible=0,payroll_paid=0,count=0))
            r['cash']+=cash;r['deductible']+=deductible;r['payroll_paid']+=salary;r['count']+=1
            details.append(dict(id=d['id'],date=d['date'],category=category,purpose=data.get('purpose') or d['note'] or 'Назначение не указано',recipient=data.get('recipient') or data.get('employee_name') or '',amount=cash,deductible=deductible,already_in_cost=bool(data.get('already_in_cost')),responsible=d['responsible']))
        cash=money_round(sum(d['total'] for d in expenses));deductible=money_round(sum(d['total'] for d in expenses if not d['data'].get('already_in_cost')))
        writeoffs=money_round(sum(d['cost'] for d in docs if d['kind']=='writeoff'));exchanges=money_round(sum(d['cost'] for d in docs if d['kind']=='exchange'))
        payroll=money_round(cash-deductible);payments=money_round(sum(d['total'] for d in docs if d['kind']=='payment'));refunds=money_round(sum(d['total'] for d in docs if d['kind']=='refund'))
        return dict(start=start,end=end,revenue=revenue,cogs=cogs,gross_profit=money_round(revenue-cogs),cash_expenses=cash,expenses=deductible,payroll_paid=payroll,writeoffs=writeoffs,exchanges=exchanges,result=money_round(revenue-cogs-deductible-writeoffs-exchanges),payments=payments,refunds=refunds,cash_change=money_round(payments-refunds-cash),categories=sorted(groups.values(),key=lambda r:(-r['cash'],r['category'])),details=sorted(details,key=lambda d:(d['date'],d['id'])),expense_count=len(expenses))
