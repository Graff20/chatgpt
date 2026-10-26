"""Customer price columns, Decimal calculations and atomic preview/apply."""
import hashlib
import json
from contextlib import closing
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR

class PricingMixin:
    def pricing_state(self,c):
        lists=[dict(r) for r in c.execute('SELECT * FROM price_lists ORDER BY id')]
        values=[dict(r) for r in c.execute('SELECT * FROM price_values ORDER BY list_id,item_id')]
        items=[dict(r) for r in c.execute("SELECT id,name,kind,mode,active,folder,category,unit,sale_price FROM items ORDER BY id")]
        token=hashlib.sha256(json.dumps([lists,values,items],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        bylist={r['id']:r for r in lists};overrides={(v['list_id'],v['item_id']):v for v in values};cache={}
        def cell(lid,item,trail=()):
            key=(lid,item['id'])
            if key in cache:return cache[key]
            if lid in trail:raise ValueError('Циклическая зависимость прайсов')
            col=bylist[lid];v=overrides.get(key);mode=v['mode'] if v else ('manual' if lid==1 else 'auto' if col['source_id'] else 'manual')
            price=None;reason=''
            if mode=='blocked':reason='Не продаётся'
            elif mode=='manual':
                price=item['sale_price'] if lid==1 else (v['amount'] if v else None)
                if price is None:reason='Нет цены'
            elif col['source_id']:
                source=cell(col['source_id'],item,trail+(lid,))
                if source['price'] is None:reason='Нет исходной цены' if source['mode']!='blocked' else 'Не продаётся в исходном прайсе'
                else:
                    raw=Decimal(str(source['price']));change=Decimal(str(col['adjustment']))
                    raw=raw*(1+change/100) if col['adjustment_type']=='percent' else raw+change
                    if raw<0:reason='Расчётная цена меньше нуля'
                    else:
                        step=Decimal(str(col['round_step']));rounding={'nearest':ROUND_HALF_UP,'up':ROUND_CEILING,'down':ROUND_FLOOR}[col['round_mode']]
                        price=float(((raw/step).quantize(Decimal('1'),rounding=rounding)*step).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
                        if price>1e10:price=None;reason='Слишком большая цена'
            else:reason='Нет правила расчёта'
            result=dict(list_id=lid,item_id=item['id'],mode=mode,price=price,reason=reason)
            cache[key]=result;return result
        cells=[cell(col['id'],i) for i in items if i['kind'] in ('finished','semi') and i['mode']=='stock' for col in lists]
        return dict(lists=lists,cells=cells,token=token)

    def pricing(self):
        with closing(self.connect()) as c:
            c.execute('BEGIN');return self.pricing_state(c)

    def edit_prices(self,d):
        from domain import ValidationError,required,number
        with self.transaction() as c:
            c.execute('SAVEPOINT price_edit')
            before=self.pricing_state(c)
            if d.get('token')!=before['token']:raise ValidationError('Прайс изменился. Обновите таблицу и повторите предварительный просмотр')
            config=d.get('column');lid=int(d.get('list_id') or (config or {}).get('id') or 0)
            if config is not None:
                name=required(config.get('name'),'Название прайса')
                if len(name)>100:raise ValidationError('Название прайса: не более 100 символов')
                if c.execute('SELECT 1 FROM price_lists WHERE name=? COLLATE NOCASE AND id!=?',(name,lid)).fetchone():raise ValidationError('Прайс с таким названием уже существует')
                source=int(config.get('source_id') or 0) or None
                if source:self.one(c,'price_lists',source)
                if lid==1 and source:raise ValidationError('Базовый прайс заполняется вручную')
                typ=config.get('adjustment_type','percent');round_mode=config.get('round_mode','nearest')
                if typ not in ('percent','amount') or round_mode not in ('nearest','up','down'):raise ValidationError('Неверное правило расчёта')
                try:adjustment=float(str(config.get('adjustment',0)).replace(',','.'))
                except (ValueError,TypeError):raise ValidationError('Укажите изменение цены числом')
                import math
                if not math.isfinite(adjustment) or abs(adjustment)>1e8 or (typ=='percent' and adjustment < -100):raise ValidationError('Недопустимое изменение цены')
                step=number(config.get('round_step',.01),'Шаг округления')
                if step not in (.01,1,5,10):raise ValidationError('Выберите округление до копеек, 1, 5 или 10 рублей')
                args=(name,source,typ,adjustment,step,round_mode)
                if lid:
                    self.one(c,'price_lists',lid)
                    c.execute('UPDATE price_lists SET name=?,source_id=?,adjustment_type=?,adjustment=?,round_step=?,round_mode=? WHERE id=?',args+(lid,))
                else:lid=c.execute('INSERT INTO price_lists(name,source_id,adjustment_type,adjustment,round_step,round_mode) VALUES(?,?,?,?,?,?)',args).lastrowid
                graph={r['id']:r['source_id'] for r in c.execute('SELECT id,source_id FROM price_lists')}
                for start in graph:
                    trail=set();at=start
                    while at is not None:
                        if at in trail:raise ValidationError('Циклическая зависимость прайсов запрещена')
                        trail.add(at);at=graph[at]
            self.one(c,'price_lists',lid)
            changes=d.get('changes',[])
            if not isinstance(changes,list) or len(changes)>5000:raise ValidationError('Слишком много изменений')
            for change in changes:
                item=self.one(c,'items',change.get('item_id'))
                if item['kind']!='finished' or item['mode']!='stock':raise ValidationError('В таблице цен доступна только готовая складская продукция')
                mode=change.get('mode')
                if mode not in ('auto','manual','blocked'):raise ValidationError('Неверный режим ячейки')
                if mode=='auto' and (lid==1 or not self.one(c,'price_lists',lid)['source_id']):raise ValidationError('Сначала задайте исходный прайс для столбца')
                old=c.execute('SELECT mode FROM price_values WHERE list_id=? AND item_id=?',(lid,item['id'])).fetchone()
                if d.get('skip_exceptions') and old and old['mode'] in ('manual','blocked'):continue
                amount=number(change.get('amount'),'Цена',False) if mode=='manual' else None
                if amount is not None and round(amount,2)!=amount:raise ValidationError('Цена: не более двух знаков после запятой')
                if lid==1 and mode=='manual':c.execute('UPDATE items SET sale_price=? WHERE id=?',(amount,item['id']))
                c.execute('INSERT INTO price_values(list_id,item_id,mode,amount) VALUES(?,?,?,?) ON CONFLICT(list_id,item_id) DO UPDATE SET mode=excluded.mode,amount=excluded.amount',(lid,item['id'],mode,amount))
            after=self.pricing_state(c)
            previous={(r['list_id'],r['item_id']):r for r in before['cells']}
            names={r['id']:r['name'] for r in c.execute("SELECT id,name FROM items WHERE kind='finished'")};columns={r['id']:r['name'] for r in after['lists']}
            diff=[]
            for r in after['cells']:
                old=previous.get((r['list_id'],r['item_id']))
                if old!=r and r['item_id'] in names:diff.append(dict(item_id=r['item_id'],name=names[r['item_id']],list_name=columns[r['list_id']],old=old,new=r))
            if not d.get('apply'):
                c.execute('ROLLBACK TO price_edit');c.execute('RELEASE price_edit');return dict(list_id=lid,changes=diff,token=before['token'])
            c.execute('INSERT INTO price_history(created,responsible,changes) VALUES(datetime(\'now\',\'localtime\'),?,?)',(required(d.get('responsible'),'Ответственный'),json.dumps(dict(request=d,changes=diff),ensure_ascii=False)))
            c.execute('RELEASE price_edit')
            return dict(list_id=lid,changed=len(diff))

    def customer_price(self,c,client,item):
        lid=client['price_list_id'];state=self.pricing_state(c)
        return next((r for r in state['cells'] if r['list_id']==lid and r['item_id']==item['id']),dict(price=None,mode='manual',reason='Нет цены'))
