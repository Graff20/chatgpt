"""Transactional production accounting. Python 3.11+, no third-party dependencies."""
import sqlite3
import json
import math
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date, timedelta
from pathlib import Path
from contextlib import contextmanager, closing

SCHEMA_VERSION = 3
EPS = 1e-7

def money_round(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
UNITS = {'кг', 'л', 'шт.', 'уп.'}

class ValidationError(ValueError):
    pass

def number(value, name='Количество', positive=True):
    try:
        n = float(str(value).replace(',', '.'))
    except (ValueError, TypeError):
        raise ValidationError(f'{name}: укажите число')
    if not math.isfinite(n) or (n <= 0 if positive else n < 0) or n > 1e10:
        raise ValidationError(f'{name}: недопустимое значение')
    return n

def required(value, label):
    value = str(value or '').strip()
    if not value:
        raise ValidationError(f'Заполните поле «{label}»')
    return value

def day(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValidationError('Неверная дата')

def timestamp(value):
    try:
        return datetime.fromisoformat(str(value)).isoformat(timespec='minutes')
    except ValueError:
        raise ValidationError('Неверная дата и время')

def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False)

SCHEMA = '''
CREATE TABLE items (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 folder TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL,
 minimum REAL NOT NULL DEFAULT 0, price REAL NOT NULL DEFAULT 0, sale_price REAL NOT NULL DEFAULT 0,
 shelf_hours REAL NOT NULL DEFAULT 24, mode TEXT NOT NULL DEFAULT 'stock',
 losses TEXT NOT NULL DEFAULT '[]', pack_size REAL NOT NULL DEFAULT 1, notes TEXT NOT NULL DEFAULT '',
 active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE partners (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, inn TEXT NOT NULL DEFAULT '',
 contact TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
 address TEXT NOT NULL DEFAULT '', manager TEXT NOT NULL DEFAULT '', order_days TEXT NOT NULL DEFAULT '',
 reminder TEXT NOT NULL DEFAULT 'manager', payment_days INTEGER NOT NULL DEFAULT 0,
 debt_limit REAL NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE recipes (
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), version INTEGER NOT NULL,
 yield_qty REAL NOT NULL, lines TEXT NOT NULL, technology TEXT NOT NULL DEFAULT '',
 storage TEXT NOT NULL DEFAULT '', quality TEXT NOT NULL DEFAULT '', created TEXT NOT NULL,
 UNIQUE(item_id, version)
);
CREATE TABLE documents (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, date TEXT NOT NULL,
 partner_id INTEGER REFERENCES partners(id), responsible TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '', total REAL NOT NULL DEFAULT 0,
 cost REAL NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL
);
CREATE TABLE lots (
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), code TEXT NOT NULL,
 produced TEXT NOT NULL, expires TEXT NOT NULL, qty REAL NOT NULL CHECK(qty >= -0.0000001),
 unit_cost REAL NOT NULL, supplier_id INTEGER REFERENCES partners(id),
 document_id INTEGER NOT NULL REFERENCES documents(id), blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE movements (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
 lot_id INTEGER NOT NULL REFERENCES lots(id), item_id INTEGER NOT NULL REFERENCES items(id),
 qty REAL NOT NULL, unit_cost REAL NOT NULL
);
CREATE TABLE orders (
 id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES partners(id),
 due TEXT NOT NULL, manager TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 note TEXT NOT NULL DEFAULT '', created TEXT NOT NULL
);
CREATE TABLE order_lines (
 id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
 item_id INTEGER NOT NULL REFERENCES items(id), qty REAL NOT NULL,
 shipped REAL NOT NULL DEFAULT 0, price REAL NOT NULL, tolerance REAL NOT NULL DEFAULT 10
);
CREATE INDEX lots_item ON lots(item_id, expires);
CREATE INDEX movements_doc ON movements(document_id);
CREATE INDEX docs_date ON documents(date);
CREATE INDEX orders_due ON orders(due);
'''

from workflow import WorkflowMixin
from adjustments import AdjustmentsMixin

class Store(WorkflowMixin, AdjustmentsMixin):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as c:
            version = c.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                if c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValidationError('Неизвестная схема базы; автоматическое пересоздание запрещено')
                c.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + '\nPRAGMA user_version=1; COMMIT;')
                version = 1
            if version == 1:
                # A consistent, recoverable backup precedes every upgrade of a user database.
                backup = self.path.parent/'backups'/f'before-v2-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3'
                backup.parent.mkdir(exist_ok=True)
                with closing(sqlite3.connect(backup)) as target:
                    c.backup(target)
                c.executescript('''BEGIN IMMEDIATE;
                    ALTER TABLE items ADD COLUMN track_expiry INTEGER NOT NULL DEFAULT 1;
                    CREATE TABLE drivers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', vehicle TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
                    ALTER TABLE orders ADD COLUMN driver_id INTEGER REFERENCES drivers(id);
                    ALTER TABLE orders ADD COLUMN route_position INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE orders ADD COLUMN delivery_address TEXT NOT NULL DEFAULT '';
                    ALTER TABLE orders ADD COLUMN delivery_phone TEXT NOT NULL DEFAULT '';
                    ALTER TABLE orders ADD COLUMN delivery_contact TEXT NOT NULL DEFAULT '';
                    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO settings VALUES('company','{"name":"Моё производство","address":"","phone":"","inn":""}');
                    CREATE TABLE day_closures(date TEXT PRIMARY KEY, responsible TEXT NOT NULL, created TEXT NOT NULL);
                    PRAGMA user_version=2;
                    COMMIT;''')
                version = 2
            if version == 2:
                backup = self.path.parent/'backups'/f'before-v3-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3'
                backup.parent.mkdir(exist_ok=True)
                with closing(sqlite3.connect(backup)) as target:
                    c.backup(target)
                self.migrate_v3(c)
                version = 3
            if version != SCHEMA_VERSION:
                raise ValidationError('Версия базы не поддерживается этим приложением')

        self.initialise_cost_dependencies()

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=FULL')
        return c

    @contextmanager
    def transaction(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def one(self, c, table, ident):
        # All table names are internal constants, never supplied by HTTP callers.
        r = c.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
        if not r:
            raise ValidationError('Запись не найдена')
        return dict(r)

    def quantity(self, item, value, unit=None):
        q = number(value)
        unit = unit or item['unit']
        if unit == item['unit']:
            pass
        elif (unit, item['unit']) in [('г', 'кг'), ('мл', 'л')]:
            q /= 1000
        else:
            raise ValidationError(f'Нельзя перевести {unit} в {item["unit"]}')
        if item['unit'] in ('шт.', 'уп.') and abs(q - round(q)) > EPS:
            raise ValidationError('Штуки и упаковки должны быть целыми')
        return q

    def partner(self, c, ident, kind):
        p = self.one(c, 'partners', ident)
        if p['kind'] != kind:
            raise ValidationError('Неверный тип контрагента')
        return p

    def save_item(self, d):
        name = required(d.get('name'), 'Наименование')
        kind = d.get('kind', 'raw')
        if kind not in ('raw', 'semi', 'finished', 'packaging'):
            raise ValidationError('Неверный вид номенклатуры')
        unit = d.get('unit', 'кг')
        if unit not in UNITS:
            raise ValidationError('Неверная единица')
        mode = d.get('mode', 'stock')
        if mode not in ('stock', 'virtual') or (mode == 'virtual' and kind != 'semi'):
            raise ValidationError('Разложение по рецептуре доступно для полуфабриката')
        losses = d.get('losses', [])
        for loss in losses:
            required(loss.get('name'), 'Обработка')
            for k in ('mechanical', 'thermal'):
                loss[k] = number(loss.get(k, 0), 'Потери', False)
                if loss[k] >= 100:
                    raise ValidationError('Потери должны быть меньше 100%')
        shelf_hours = 0
        if d.get('track_expiry', True):
            shelf_hours = (number(d['shelf_days'], 'Срок годности, суток') * 24
                           if 'shelf_days' in d else number(d.get('shelf_hours') or 24, 'Срок годности'))
            shelf_hours = number(shelf_hours, 'Срок годности')
        values = [name, kind, str(d.get('folder', '')), str(d.get('category', '')), unit,
                  number(d.get('minimum', 0), 'Минимум', False), number(d.get('price', 0), 'Цена', False),
                  number(d.get('sale_price', 0), 'Цена продажи', False), shelf_hours,
                  mode, dumps(losses), number(d.get('pack_size', 1), 'Состав упаковки'), str(d.get('notes', '')),
                  int(bool(d.get('active', True))), int(bool(d.get('track_expiry', True)))]+[number(d.get(k,0),k,False) for k in ('labor_rate','overhead_rate','overhead_percent','production_buffer')]
        if (kind in ('raw','packaging') or mode=='virtual') and any(number(d.get(k,0),k,False)>0 for k in ('labor_rate','overhead_rate','overhead_percent')):
            raise ValidationError('Расценку и производственные расходы задавайте складскому блюду или полуфабрикату; для сырья и виртуального полуфабриката оставьте нули')
        cols = 'name,kind,folder,category,unit,minimum,price,sale_price,shelf_hours,mode,losses,pack_size,notes,active,track_expiry,labor_rate,overhead_rate,overhead_percent,production_buffer'
        with self.transaction() as c:
            if d.get('id'):
                old = self.one(c, 'items', d['id'])
                if any(old[k] != dval for k, dval in [('unit', unit), ('kind', kind), ('mode', mode)]):
                    referenced = c.execute('SELECT 1 FROM movements WHERE item_id=? UNION SELECT 1 FROM order_lines WHERE item_id=?', (old['id'], old['id'])).fetchone()
                    in_recipe = any(any(int(x['item_id']) == old['id'] for x in json.loads(r[0])) for r in c.execute('SELECT lines FROM recipes'))
                    if referenced or in_recipe or c.execute('SELECT 1 FROM recipes WHERE item_id=?', (old['id'],)).fetchone():
                        raise ValidationError('Единицу, вид и режим используемой позиции менять нельзя; создайте новую карточку')
                c.execute('UPDATE items SET ' + ','.join(k+'=?' for k in cols.split(',')) + ' WHERE id=?', values + [d['id']])
                self.recalculate_costs(c,{old['id']},str(d.get('modified_by') or 'Изменение карточки'), 'Изменение расценок и производственных норм карточки')
                return d['id']
            return c.execute(f'INSERT INTO items ({cols}) VALUES ({",".join("?" for _ in values)})', values).lastrowid

    def save_partner(self, d):
        kind = d.get('kind', 'client')
        if kind not in ('client', 'supplier'):
            raise ValidationError('Неверный вид контрагента')
        name = required(d.get('name'), 'Наименование')
        inn = str(d.get('inn', '')).strip()
        if inn and (not inn.isdigit() or len(inn) not in (10, 12)):
            raise ValidationError('ИНН должен содержать 10 или 12 цифр')
        fields = ['kind','name','inn','contact','phone','email','address','manager','order_days','reminder','payment_days','debt_limit','notes']
        values = [kind, name, inn] + [str(d.get(k, '')) for k in fields[3:10]]
        payment_days = number(d.get('payment_days', 0), 'Отсрочка', False)
        if payment_days != int(payment_days) or payment_days > 3650:
            raise ValidationError('Отсрочка: целое число дней от 0 до 3650')
        values += [int(payment_days), number(d.get('debt_limit', 0), 'Лимит', False), str(d.get('notes', ''))]
        with self.transaction() as c:
            if d.get('id'):
                old = self.one(c, 'partners', d['id'])
                if old['kind'] != kind:
                    raise ValidationError('Тип контрагента менять нельзя')
                c.execute('UPDATE partners SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', values+[d['id']])
                return d['id']
            return c.execute('INSERT INTO partners ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')', values).lastrowid

    def latest_recipe(self, c, item_id):
        r = c.execute('SELECT * FROM recipes WHERE item_id=? ORDER BY version DESC LIMIT 1', (item_id,)).fetchone()
        if not r:
            raise ValidationError(f'Нет ТТК: {self.one(c, "items", item_id)["name"]}')
        result = dict(r)
        result['lines'] = json.loads(result['lines'])
        return result

    def save_recipe(self, d):
        with self.transaction() as c:
            product = self.one(c, 'items', d.get('item_id'))
            if product['kind'] not in ('semi', 'finished'):
                raise ValidationError('ТТК создаётся для блюда или полуфабриката')
            yield_qty = self.quantity(product, d.get('yield_qty'))
            lines = []
            for row in d.get('lines', []):
                item = self.one(c, 'items', row.get('item_id'))
                net = self.quantity(item, row.get('net'), row.get('unit'))
                m = number(row.get('mechanical', 0), 'Механические потери', False)
                t = number(row.get('thermal', 0), 'Тепловые потери', False)
                if m >= 100 or t >= 100:
                    raise ValidationError('Потери должны быть меньше 100%')
                gross = net / ((1-m/100)*(1-t/100))
                if item['unit'] in ('шт.', 'уп.') and abs(gross-round(gross)) > EPS:
                    raise ValidationError('Потери для штучного ингредиента дают дробное количество')
                lines.append(dict(item_id=item['id'], name=item['name'], unit=item['unit'], net=net,
                                  mechanical=m, thermal=t, gross=gross, treatment=str(row.get('treatment', ''))))
            if not lines:
                raise ValidationError('Добавьте ингредиенты')
            # Detect cycles across every recipe edge, including stock semi-finished items.
            def visit(ident, path):
                if ident in path:
                    raise ValidationError('Циклическая рецептура: продукт содержит сам себя')
                rs = lines if ident == product['id'] else None
                if rs is None:
                    r = c.execute('SELECT lines FROM recipes WHERE item_id=? ORDER BY version DESC LIMIT 1', (ident,)).fetchone()
                    rs = json.loads(r[0]) if r else []
                for line in rs:
                    visit(line['item_id'], path | {ident})
            visit(product['id'], set())
            v = c.execute('SELECT coalesce(max(version),0)+1 FROM recipes WHERE item_id=?',(product['id'],)).fetchone()[0]
            return c.execute('INSERT INTO recipes(item_id,version,yield_qty,lines,technology,storage,quality,created,author) VALUES(?,?,?,?,?,?,?,?,?)',
                (product['id'], v, yield_qty, dumps(lines), d.get('technology',''), d.get('storage',''), d.get('quality',''), datetime.now().isoformat(), str(d.get('author') or d.get('responsible') or 'Не указан'))).lastrowid

    def requirements(self, c, item_id, quantity, path=None):
        path = set() if path is None else path
        if item_id in path:
            raise ValidationError('Циклическая рецептура')
        recipe = self.latest_recipe(c, item_id)
        result = {}
        for line in recipe['lines']:
            item = self.one(c, 'items', line['item_id'])
            amount = line['gross'] * quantity / recipe['yield_qty']
            children = self.requirements(c, item['id'], amount, path | {item_id}) if item['mode'] == 'virtual' else {item['id']: amount}
            for k,v in children.items():
                result[k] = result.get(k,0) + v
        return result

    def planned_cost(self, c, item_id, path=None):
        item = self.one(c, 'items', item_id)
        if item['kind'] in ('raw', 'packaging'):
            # Last receipt by business date, not latest edited item price.
            r = c.execute("SELECT l.unit_cost FROM lots l JOIN documents d ON d.id=l.document_id WHERE l.item_id=? AND d.kind='receipt' AND NOT EXISTS (SELECT 1 FROM documents r WHERE r.kind='reversal' AND json_extract(r.data,'$.target_id')=d.id) ORDER BY d.date DESC,d.id DESC,l.id DESC LIMIT 1",(item_id,)).fetchone()
            return r[0] if r else item['price']
        path = set() if path is None else path
        if item_id in path:
            raise ValidationError('Циклическая рецептура')
        try:
            recipe = self.latest_recipe(c, item_id)
        except ValidationError:
            return item['price']
        materials = sum(x['gross']*self.planned_cost(c,x['item_id'],path|{item_id}) for x in recipe['lines'])/recipe['yield_qty']
        return materials + item['labor_rate'] + item['overhead_rate'] + materials*item['overhead_percent']/100

    def doc(self, c, kind, d, total=0, cost=0, data=None):
        dt = day(d.get('date', date.today().isoformat()))
        if dt > date.today().isoformat():
            raise ValidationError('Фактический документ нельзя проводить будущей датой; используйте заказ')
        return c.execute('INSERT INTO documents(kind,date,partner_id,responsible,note,total,cost,data,created) VALUES(?,?,?,?,?,?,?,?,?)',
            (kind, dt, d.get('partner_id') or None, required(d.get('responsible'),'Ответственный'), str(d.get('note','')), total, cost, dumps(data or {}), datetime.now().isoformat())).lastrowid

    def add_lot(self, c, doc_id, item, qty, price, produced, expires, code='', supplier=None, preserve_expiry=False):
        produced = timestamp(produced)
        expires = timestamp(expires) if expires else ''
        if item['track_expiry'] and not expires and not preserve_expiry:
            raise ValidationError('Укажите срок годности партии или отключите его учёт в карточке')
        if expires and expires <= produced:
            raise ValidationError('Срок годности должен быть позже даты изготовления')
        doc_date = self.one(c,'documents',doc_id)['date']
        if produced[:10] > doc_date:
            raise ValidationError('Изготовление не может быть позже даты документа')
        ident = c.execute('INSERT INTO lots(item_id,code,produced,expires,qty,unit_cost,supplier_id,document_id) VALUES(?,?,?,?,?,?,?,?)',
            (item['id'], code or f'{doc_id}-{item["id"]}', produced, expires, qty, price, supplier, doc_id)).lastrowid
        c.execute('INSERT INTO movements(document_id,lot_id,item_id,qty,unit_cost) VALUES(?,?,?,?,?)', (doc_id,ident,item['id'],qty,price))
        return ident

    def consume(self, c, doc_id, item_id, qty, at, lot_id=None, allow_expired=False, reserved=None):
        item = self.one(c,'items',item_id)
        lots = c.execute('SELECT * FROM lots WHERE item_id=? AND qty>? '+('AND id=? ' if lot_id else '')+"ORDER BY (expires=''),expires,produced,id",
            (item_id,EPS,lot_id) if lot_id else (item_id,EPS)).fetchall()
        remaining, cost, used = qty, 0, []
        for lot in lots:
            if not allow_expired and (lot['blocked'] or (lot['expires'] and lot['expires'] <= at) or lot['produced'] > at):
                continue
            # Never consume stock that arrived after the business date.
            if self.one(c,'documents',lot['document_id'])['date'] > at[:10]:
                continue
            take = min(remaining,max(0,lot['qty']-(reserved or {}).get(lot['id'],0)))
            if take <= EPS:
                continue
            c.execute('UPDATE lots SET qty=max(0,qty-?) WHERE id=?',(take,lot['id']))
            c.execute('INSERT INTO movements(document_id,lot_id,item_id,qty,unit_cost) VALUES(?,?,?,?,?)',(doc_id,lot['id'],item_id,-take,lot['unit_cost']))
            used.append(dict(lot_id=lot['id'],code=lot['code'],qty=take,unit_cost=lot['unit_cost'],produced=lot['produced'],expires=lot['expires']))
            cost += take*lot['unit_cost']
            remaining -= take
        if remaining > EPS:
            raise ValidationError(f'Недостаточно доступного остатка: {item["name"]}. Не хватает {remaining:.3f} {item["unit"]}')
        return cost, used

    def receipt(self, d):
        with self.transaction() as c:
            supplier = self.partner(c,d.get('partner_id'),'supplier')
            if not d.get('lines'):
                raise ValidationError('Добавьте строки приёмки')
            ident = self.doc(c,'receipt',d)
            lines, total = [],0
            for row in d['lines']:
                item = self.one(c,'items',row.get('item_id'))
                if item['kind'] not in ('raw','packaging'):
                    raise ValidationError('Готовую продукцию и полуфабрикаты приходуйте документом выпуска')
                q = self.quantity(item,row.get('qty'),row.get('unit'))
                price = number(row.get('price'),'Цена',False)
                lot = self.add_lot(c,ident,item,q,price,row.get('produced'),row.get('expires'),row.get('code',''),supplier['id'])
                lines.append(dict(item_id=item['id'],name=item['name'],qty=q,unit=item['unit'],price=price,lot_id=lot,code=self.one(c,'lots',lot)['code'],produced=row.get('produced'),expires=row.get('expires') or ''))
                total += q*price
            c.execute('UPDATE documents SET total=?,data=? WHERE id=?',(total,dumps({'lines':lines,'supplier_name':supplier['name'],'company_name':self.company(c)['name'],'supplier_document':str(d.get('supplier_document','')),'warehouse':str(d.get('warehouse','')),'handed_by':str(d.get('handed_by',''))}),ident))
            return ident

    def production(self, d):
        with self.transaction() as c:
            item = self.one(c,'items',d.get('item_id'))
            if item['kind'] not in ('finished','semi') or item['mode']=='virtual':
                raise ValidationError('Выпуск доступен для складского блюда или полуфабриката')
            q = self.quantity(item,d.get('qty'),d.get('unit'))
            recipe = self.latest_recipe(c,item['id'])
            needs = self.requirements(c,item['id'],q)
            made = timestamp(d.get('produced'))
            dt = day(d.get('date',date.today().isoformat()))
            if made[:10] != dt or made > datetime.now().isoformat(timespec='minutes'):
                raise ValidationError('Дата изготовления должна совпадать с датой выпуска и не быть в будущем')
            expires = (d.get('expires') or (datetime.fromisoformat(made)+timedelta(hours=item['shelf_hours'])).isoformat(timespec='minutes')) if item['track_expiry'] else ''
            ident = self.doc(c,'production',d)
            cost, inputs = 0,[]
            for iid, amount in needs.items():
                ingredient = self.one(c,'items',iid)
                # indivisible ingredients round upward at the batch level, recorded explicitly.
                if ingredient['unit'] in ('шт.','уп.'):
                    amount = math.ceil(amount-EPS)
                value, lots = self.consume(c,ident,iid,amount,made)
                cost += value
                inputs.append(dict(item_id=iid,name=ingredient['name'],unit=ingredient['unit'],qty=amount,lots=lots))
            materials_cost = cost
            workers, labor_cost = self.production_workers(c,item,q,d)
            overhead_cost = money_round(q*item['overhead_rate']+materials_cost*item['overhead_percent']/100)
            cost += labor_cost + overhead_cost
            job = self.production_job(c,item,d)
            lot_id = self.add_lot(c,ident,item,q,cost/q,made,expires,d.get('code',''))
            data = dict(item_id=item['id'],name=item['name'],qty=q,unit=item['unit'],recipe=recipe,inputs=inputs,lot_id=lot_id,workers=workers,labor_rate=item['labor_rate'],materials_cost=materials_cost,labor_cost=labor_cost,overhead_cost=overhead_cost,overhead_rate=item['overhead_rate'],overhead_percent=item['overhead_percent'],job_id=job,planned_qty=number(d.get('planned_qty') or q,'Плановый выпуск'),basis=str(d.get('basis') or ('По наряду № '+str(job) if job else 'На свободный склад')),warehouse=str(d.get('warehouse','Основной склад')),produced=made,expires=expires,code=self.one(c,'lots',lot_id)['code'])
            c.execute('UPDATE documents SET cost=?,data=? WHERE id=?',(cost,dumps(data),ident))
            return ident

    def save_order(self, d):
        with self.transaction() as c:
            client = self.partner(c,d.get('client_id'),'client')
            due = timestamp(d.get('due'))
            manager = required(d.get('manager'),'Менеджер')
            if not d.get('lines'):
                raise ValidationError('Добавьте товары')
            if d.get('id'):
                old = self.one(c,'orders',d['id'])
                if old['status'] != 'open' or c.execute('SELECT 1 FROM order_lines WHERE order_id=? AND shipped>?',(old['id'],EPS)).fetchone():
                    raise ValidationError('Заказ после отгрузки или закрытия редактировать нельзя')
                ident = old['id']
                c.execute('UPDATE orders SET client_id=?,due=?,manager=?,note=? WHERE id=?',(client['id'],due,manager,d.get('note',''),ident))
                c.execute('DELETE FROM order_lines WHERE order_id=?',(ident,))
            else:
                ident = c.execute('INSERT INTO orders(client_id,due,manager,note,created) VALUES(?,?,?,?,?)',(client['id'],due,manager,d.get('note',''),datetime.now().isoformat())).lastrowid
            seen = set()
            for line in d['lines']:
                item = self.one(c,'items',line.get('item_id'))
                if item['kind'] not in ('finished','semi') or item['mode']=='virtual' or not item['active']:
                    raise ValidationError('Выберите активную складскую продукцию')
                if item['id'] in seen:
                    raise ValidationError('Объедините одинаковые товары в одну строку')
                seen.add(item['id'])
                qty = self.quantity(item,line.get('qty'),line.get('unit'))
                tolerance = number(line.get('tolerance',10),'Допуск',False)
                if tolerance > 100:
                    raise ValidationError('Допуск от 0 до 100%')
                c.execute('INSERT INTO order_lines(order_id,item_id,qty,price,tolerance) VALUES(?,?,?,?,?)',(ident,item['id'],qty,number(line.get('price',item['sale_price']),'Цена',False),tolerance))
            self.order_delivery(c,ident,d,client)
            return ident

    def order_delivery(self,c,ident,d,client):
        existing=self.one(c,'orders',ident)
        driver_id=d.get('driver_id',existing['driver_id']) or None
        if driver_id:
            driver=self.one(c,'drivers',driver_id)
            if not driver['active'] and driver['id']!=existing['driver_id']:
                raise ValidationError('Водитель в архиве')
        position=number(d.get('route_position',existing['route_position']), 'Порядок доставки',False)
        if position!=int(position): raise ValidationError('Порядок доставки должен быть целым числом')
        c.execute('UPDATE orders SET driver_id=?,route_position=?,delivery_address=?,delivery_phone=?,delivery_contact=? WHERE id=?',
            (driver_id,int(position),str(d.get('delivery_address',existing['delivery_address'] or client['address'])),
             str(d.get('delivery_phone',existing['delivery_phone'] or client['phone'])),
             str(d.get('delivery_contact',existing['delivery_contact'] or client['contact'])),ident))

    def close_order(self,d):
        with self.transaction() as c:
            order = self.one(c,'orders',d.get('id'))
            if order['status'] != 'open':
                raise ValidationError('Заказ уже закрыт')
            required(d.get('responsible'),'Ответственный')
            reason = required(d.get('reason'),'Причина закрытия')
            c.execute("UPDATE orders SET status='closed',note=note||? WHERE id=?",(f'\nЗакрыт: {reason}. {d["responsible"]}',order['id']))
            return order['id']

    def shipment(self,d):
        with self.transaction() as c:
            order = self.one(c,'orders',d.get('order_id'))
            if order['status'] != 'open':
                raise ValidationError('Заказ закрыт')
            client = self.partner(c,order['client_id'],'client')
            doc_date=day(d.get('date',date.today().isoformat()))
            if c.execute('SELECT 1 FROM day_closures WHERE date=?',(doc_date,)).fetchone():
                raise ValidationError('День уже закрыт. Новые отгрузки за эту дату запрещены')
            self.order_delivery(c,order['id'],d,client)
            order=self.one(c,'orders',order['id'])
            discount=number(d.get('discount_percent',0),'Скидка',False)
            if discount>100: raise ValidationError('Скидка от 0 до 100%')
            d = dict(d,partner_id=client['id'])
            ident = self.doc(c,'shipment',d)
            at = timestamp(d.get('at',datetime.now().isoformat(timespec='minutes')))
            if at[:10] != day(d.get('date',date.today().isoformat())) or at > datetime.now().isoformat(timespec='minutes'):
                raise ValidationError('Время отгрузки должно соответствовать дате документа и не быть в будущем')
            total,cost,rows = 0,0,[]
            seen=set()
            for row in d.get('lines',[]):
                line = self.one(c,'order_lines',row.get('line_id'))
                if line['order_id'] != order['id'] or line['id'] in seen:
                    raise ValidationError('Неверная строка заказа')
                seen.add(line['id'])
                item = self.one(c,'items',line['item_id'])
                q = self.quantity(item,row.get('qty'),row.get('unit'))
                remaining = line['qty']-line['shipped']
                if remaining <= EPS:
                    raise ValidationError('Строка уже отгружена')
                if line['shipped']+q > line['qty']*(1+line['tolerance']/100)+EPS:
                    raise ValidationError(f'{item["name"]}: превышен допуск {line["tolerance"]}%')
                reserved={}
                for allocation in self.allocate_orders(c,exclude_order=order['id']):
                    if (allocation['due'],allocation['order_id']) < (order['due'],order['id']):
                        for lot in allocation['lots']:reserved[lot['lot_id']]=reserved.get(lot['lot_id'],0)+lot['qty']
                v,lots = self.consume(c,ident,item['id'],q,at,reserved=reserved)
                cost += v
                gross_total=money_round(q*line['price'])
                line_total=money_round(Decimal(str(gross_total))*(1-Decimal(str(discount))/100))
                total += line_total
                c.execute('UPDATE order_lines SET shipped=shipped+? WHERE id=?',(q,line['id']))
                rows.append(dict(item_id=item['id'],name=item['name'],unit=item['unit'],qty=q,price=line_total/q,gross_price=line['price'],gross_total=gross_total,line_total=line_total,discount_amount=money_round(gross_total-line_total),lots=lots,line_id=line['id']))
            if not rows:
                raise ValidationError('Добавьте отгружаемое количество')
            if client['debt_limit'] and self.settled_debt(c,client['id'])>=client['debt_limit']-EPS and not d.get('override_reason'):
                raise ValidationError('Долг по итогам прошлых дней достиг лимита. Внесите оплату или укажите согласование отгрузки')
            due = (date.fromisoformat(day(d.get('date',date.today().isoformat())))+timedelta(days=client['payment_days'])).isoformat()
            company=json.loads(c.execute("SELECT value FROM settings WHERE key='company'").fetchone()[0])
            driver=self.one(c,'drivers',order['driver_id']) if order['driver_id'] else None
            data = dict(order_id=order['id'],client_name=client['name'],due=due,lines=rows,override_reason=d.get('override_reason',''),
                discount_percent=discount,subtotal=money_round(sum(r['gross_total'] for r in rows)),
                discount_amount=money_round(sum(r['discount_amount'] for r in rows)),
                sender_name=required(d.get('sender_name',company['name']),'Кто отгружает'),sender_address=company.get('address',''),sender_phone=company.get('phone',''),
                delivery_address=order['delivery_address'],delivery_phone=order['delivery_phone'],delivery_contact=order['delivery_contact'],
                driver_id=order['driver_id'],driver_name=driver['name'] if driver else '',driver_phone=driver['phone'] if driver else '',vehicle=driver['vehicle'] if driver else '')
            c.execute('UPDATE documents SET total=?,cost=?,data=? WHERE id=?',(round(total,2),cost,dumps(data),ident))
            outstanding = c.execute('SELECT 1 FROM order_lines WHERE order_id=? AND qty-shipped>?',(order['id'],EPS)).fetchone()
            if not outstanding or d.get('close_order'):
                if outstanding and not str(d.get('short_reason','')).strip():
                    raise ValidationError('Укажите причину закрытия недопоставленного заказа')
                c.execute("UPDATE orders SET status='completed',note=note||? WHERE id=?",('\n'+str(d.get('short_reason','')),order['id']))
            return ident

    def settled_debt(self,c,client_id,as_of=None):
        as_of=day(as_of or date.today().isoformat())
        closed=bool(c.execute('SELECT 1 FROM day_closures WHERE date=?',(as_of,)).fetchone())
        reversed_ids=self.reversed_ids(c)
        docs=[dict(r) for r in c.execute('SELECT * FROM documents WHERE partner_id=? AND date<=?',(client_id,as_of)) if r['id'] not in reversed_ids]
        invoices={r['id']:r['total'] for r in docs if r['kind']=='shipment' and (r['date']<as_of or closed)}
        for r in docs:
            if r['kind']=='return':
                invoice=json.loads(r['data'])['shipment_id']
                if invoice in invoices: invoices[invoice]-=r['total']
        payments=sum(r['total']*(1 if r['kind']=='payment' else -1) for r in docs if r['kind'] in ('payment','refund'))
        return money_round(max(0,sum(invoices.values())-payments))

    def close_day(self,d):
        dt=day(d.get('date',date.today().isoformat()))
        if dt>date.today().isoformat():raise ValidationError('Нельзя закрыть будущий день')
        with self.transaction() as c:
            who=required(d.get('responsible'),'Ответственный')
            if c.execute('SELECT 1 FROM day_closures WHERE date=?',(dt,)).fetchone():
                raise ValidationError('Этот день уже закрыт')
            c.execute('INSERT INTO day_closures VALUES(?,?,?)',(dt,who,datetime.now().isoformat()))
        return dt

    def reversed_ids(self,c):
        return {json.loads(r[0])['target_id'] for r in c.execute("SELECT data FROM documents WHERE kind='reversal'")}

    def balance(self,c,client_id):
        reversed_ids=self.reversed_ids(c)
        return sum(r['total']*(1 if r['kind'] in ('shipment','refund') else -1) for r in c.execute("SELECT * FROM documents WHERE partner_id=? AND kind IN ('shipment','payment','return','refund')",(client_id,)) if r['id'] not in reversed_ids)

    def reverse(self,d):
        with self.transaction() as c:
            return self.reverse_in_transaction(c,d)

    def reverse_in_transaction(self,c,d):
        target=self.one(c,'documents',d.get('document_id'))
        if target['kind']=='reversal' or target['id'] in self.reversed_ids(c):
            raise ValidationError('Документ уже отменён или сам является отменой')
        reason=required(d.get('reason'),'Причина отмены')
        if day(d.get('date',date.today().isoformat())) < target['date']:
            raise ValidationError('Отмена не может быть раньше документа')
        data=json.loads(target['data'])
        if target['kind']=='production':
            for worker in data.get('workers',[]):
                accrued=0;paid=0
                for row in c.execute("SELECT id,kind,data,total FROM documents WHERE kind IN ('production','expense')"):
                    if row['id'] in self.reversed_ids(c) or row['id']==target['id']:continue
                    payload=json.loads(row['data'])
                    if row['kind']=='production':accrued+=sum(w['amount'] for w in payload.get('workers',[]) if w['employee_id']==worker['employee_id'])
                    elif payload.get('employee_id')==worker['employee_id'] and payload.get('already_in_cost'):paid+=row['total']
                if paid>accrued+EPS:raise ValidationError('Сначала отмените выплату зарплаты по этому выпуску')
        if target['kind']=='shipment':
            if any(any(a['document_id']==target['id'] for a in json.loads(e['data']).get('allocations',[])) for e in c.execute("SELECT id,data FROM documents WHERE kind='expense'") if e['id'] not in self.reversed_ids(c)):
                raise ValidationError('Сначала отмените расходы, распределённые на этот счёт')
            if any(json.loads(r['data']).get('shipment_id')==target['id'] and r['id'] not in self.reversed_ids(c) for r in c.execute("SELECT id,data FROM documents WHERE kind IN ('return','exchange')")):
                raise ValidationError('Сначала отмените возвраты по этой отгрузке')
        if target['kind']=='release':
            if c.execute("SELECT 1 FROM movements WHERE lot_id=? AND document_id>? AND document_id NOT IN (SELECT id FROM documents WHERE kind='reversal') AND document_id NOT IN (SELECT json_extract(data,'$.target_id') FROM documents WHERE kind='reversal')",(data['lot_id'],target['id'])).fetchone():
                raise ValidationError('По допущенной партии есть последующие движения; сначала отмените их')
        moves=[dict(r) for r in c.execute('SELECT * FROM movements WHERE document_id=?',(target['id'],))]
        # Newer dependent documents must be reversed first, keeping traceability intact.
        for m in moves:
            if c.execute("SELECT 1 FROM movements WHERE lot_id=? AND id>? AND document_id NOT IN (SELECT id FROM documents WHERE kind='reversal') AND document_id NOT IN (SELECT json_extract(data,'$.target_id') FROM documents WHERE kind='reversal')",(m['lot_id'],m['id'])).fetchone():
                raise ValidationError('По партии есть последующие движения. Отмените зависимые документы в обратном порядке')
            lot=self.one(c,'lots',m['lot_id'])
            if lot['qty']-m['qty'] < -EPS:
                raise ValidationError('Нельзя отменить: продукция партии уже израсходована')
        ident=self.doc(c,'reversal',d,data={'target_id':target['id'],'target_kind':target['kind'],'reason':reason})
        for m in reversed(moves):
            c.execute('UPDATE lots SET qty=max(0,qty-?) WHERE id=?',(m['qty'],m['lot_id']))
            c.execute('INSERT INTO movements(document_id,lot_id,item_id,qty,unit_cost) VALUES(?,?,?,?,?)',(ident,m['lot_id'],m['item_id'],-m['qty'],m['unit_cost']))
        if target['kind']=='release':c.execute('UPDATE lots SET blocked=1 WHERE id=?',(data['lot_id'],))
        if target['kind']=='shipment':
            for line in data['lines']:
                c.execute('UPDATE order_lines SET shipped=max(0,shipped-?) WHERE id=?',(line['qty'],line['line_id']))
            c.execute("UPDATE orders SET status='open' WHERE id=?",(data['order_id'],))
        return ident

    def customer_return(self,d):
        with self.transaction() as c:
            original=self.one(c,'documents',d.get('shipment_id'))
            if original['kind']!='shipment' or original['id'] in self.reversed_ids(c):
                raise ValidationError('Выберите действующую накладную')
            data=json.loads(original['data'])
            if day(d.get('date',date.today().isoformat())) < original['date']:
                raise ValidationError('Возврат не может быть раньше отгрузки')
            reason=required(d.get('reason'),'Причина возврата')
            reversed_ids=self.reversed_ids(c)
            previous={}
            previous_values={}
            for row in c.execute("SELECT id,data FROM documents WHERE kind IN ('return','exchange')"):
                if row['id'] in reversed_ids:continue
                prior=json.loads(row['data'])
                if prior['shipment_id']==original['id']:
                    for line in prior['lines']:
                        previous[line['line_id']]=previous.get(line['line_id'],0)+line['qty']
                        previous_values[line['line_id']]=previous_values.get(line['line_id'],0)+line.get('original_value',line.get('line_total',line['qty']*line['price']))
            d=dict(d,partner_id=original['partner_id'])
            ident=self.doc(c,'return',d)
            total,cost,rows,seen=0,0,[],set()
            for row in d.get('lines',[]):
                line=next((l for l in data['lines'] if l['line_id']==int(row.get('line_id',0))),None)
                if not line or line['line_id'] in seen:raise ValidationError('Неверная строка возврата')
                seen.add(line['line_id'])
                it=self.one(c,'items',line['item_id']);q=self.quantity(it,row.get('qty'))
                already=previous.get(line['line_id'],0)
                if q+already>line['qty']+EPS:raise ValidationError('Возврат превышает отгруженное количество')
                # Re-create original lot provenance and cost; all returns are quarantined.
                remain=q;skip=already;lots=[]
                for old in line['lots']:
                    used_skip=min(skip,old['qty']);skip-=used_skip
                    take=min(remain,old['qty']-used_skip)
                    if take<=EPS:continue
                    lid=self.add_lot(c,ident,it,take,old['unit_cost'],old['produced'],old['expires'],f'В-{ident}-{old["lot_id"]}',preserve_expiry=True)
                    c.execute('UPDATE lots SET blocked=1 WHERE id=?',(lid,))
                    lots.append(dict(lot_id=lid,qty=take,unit_cost=old['unit_cost'],produced=old['produced'],expires=old['expires'],source_lot_id=old['lot_id']))
                    remain-=take;cost+=take*old['unit_cost']
                    if remain<=EPS:break
                remaining_value=money_round(line.get('line_total',line['qty']*line['price'])-previous_values.get(line['line_id'],0))
                line_total=remaining_value if abs(q+already-line['qty'])<EPS else min(remaining_value,money_round(q*line['price']))
                total+=line_total
                rows.append(dict(item_id=it['id'],name=it['name'],unit=it['unit'],qty=q,price=line['price'],line_total=line_total,lots=lots,line_id=line['line_id']))
            if not rows:raise ValidationError('Добавьте количество возврата')
            c.execute('UPDATE documents SET total=?,cost=?,data=? WHERE id=?',(round(total,2),cost,dumps(dict(shipment_id=original['id'],client_name=data['client_name'],reason=reason,lines=rows)),ident))
            return ident

    def release_lot(self,d):
        with self.transaction() as c:
            lot=self.one(c,'lots',d.get('lot_id'))
            if not lot['blocked']:raise ValidationError('Партия уже доступна')
            if lot['expires'] and lot['expires']<=datetime.now().isoformat(timespec='minutes'):
                raise ValidationError('Просроченную партию нельзя допустить к продаже')
            reason=required(d.get('reason'),'Основание допуска')
            ident=self.doc(c,'release',d,data={'lot_id':lot['id'],'reason':reason,'name':self.one(c,'items',lot['item_id'])['name']})
            c.execute('UPDATE lots SET blocked=0 WHERE id=?',(lot['id'],))
            return ident

    def money(self,d):
        with self.transaction() as c:
            kind = d.get('kind')
            if kind not in ('payment','expense'):
                raise ValidationError('Неверная денежная операция')
            amount = round(number(d.get('amount'),'Сумма'),2)
            if amount <= 0:
                raise ValidationError('Сумма должна быть не меньше 0,01 ₽')
            if kind == 'payment':
                client = self.partner(c,d.get('partner_id'),'client')
                driver=self.one(c,'drivers',d['driver_id']) if d.get('driver_id') else None
                data = {'client_name':client['name'],'driver_id':driver['id'] if driver else None,'driver_name':driver['name'] if driver else ''}
            else:
                d = dict(d,partner_id=None)
                data = self.expense_details(c,d,amount)
            return self.doc(c,kind,d,total=amount,data=data)

    def writeoff(self,d):
        with self.transaction() as c:
            lot = self.one(c,'lots',d.get('lot_id'))
            item = self.one(c,'items',lot['item_id'])
            q = self.quantity(item,d.get('qty'))
            reason = required(d.get('reason'),'Причина списания')
            ident = self.doc(c,'writeoff',d)
            cost,used = self.consume(c,ident,item['id'],q,day(d.get('date',date.today().isoformat()))+'T23:59',lot['id'],True)
            c.execute('UPDATE documents SET cost=?,data=? WHERE id=?',(cost,dumps(dict(name=item['name'],qty=q,unit=item['unit'],reason=reason,lots=used)),ident))
            return ident

    def inventory(self,d):
        with self.transaction() as c:
            if not d.get('lines'):
                raise ValidationError('Добавьте партии для ревизии')
            ident = self.doc(c,'inventory',d)
            if day(d.get('date',date.today().isoformat())) != date.today().isoformat():
                raise ValidationError('Ревизия проводится на текущую дату')
            rows,cost,seen = [],0,set()
            for line in d['lines']:
                lot=self.one(c,'lots',line.get('lot_id'))
                if lot['id'] in seen:
                    raise ValidationError('Партия повторяется')
                seen.add(lot['id'])
                item=self.one(c,'items',lot['item_id'])
                if 'expected' not in line or abs(number(line['expected'],'Учётный остаток',False)-lot['qty'])>EPS:
                    raise ValidationError('Остаток изменился во время ревизии. Обновите ведомость и повторите подсчёт')
                actual=number(line.get('actual'),'Факт',False)
                if actual and item['unit'] in ('шт.','уп.') and abs(actual-round(actual))>EPS:
                    raise ValidationError('Штуки и упаковки должны быть целыми')
                diff=actual-lot['qty']
                reason=required(line.get('reason'),'Причина отклонения') if abs(diff)>EPS else ''
                c.execute('UPDATE lots SET qty=? WHERE id=?',(actual,lot['id']))
                c.execute('INSERT INTO movements(document_id,lot_id,item_id,qty,unit_cost) VALUES(?,?,?,?,?)',(ident,lot['id'],item['id'],diff,lot['unit_cost']))
                cost+=diff*lot['unit_cost']
                rows.append(dict(lot_id=lot['id'],name=item['name'],unit=item['unit'],expected=lot['qty'],actual=actual,difference=diff,reason=reason))
            c.execute('UPDATE documents SET cost=?,data=? WHERE id=?',(cost,dumps({'lines':rows}),ident))
            return ident

    def snapshot(self):
        with closing(self.connect()) as c:
            c.execute('BEGIN')
            now=datetime.now().isoformat(timespec='minutes')
            result={t:[dict(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY id')] for t in ('items','partners','recipes','lots','orders','order_lines','documents','drivers','employees','production_jobs')}
            for r in result['recipes']:
                r['lines']=json.loads(r['lines'])
            for i in result['items']:
                i['losses']=json.loads(i['losses'])
                i['planned_cost']=self.planned_cost(c,i['id'])
                i['available']=sum(l['qty'] for l in result['lots'] if l['item_id']==i['id'] and not l['blocked'] and l['produced']<=now and (not l['expires'] or now<l['expires']))
                i['expired']=sum(l['qty'] for l in result['lots'] if l['item_id']==i['id'] and l['expires'] and l['expires']<=now)
                i['demand']=sum(max(0,l['qty']-l['shipped']) for l in result['order_lines'] if l['item_id']==i['id'] and any(o['id']==l['order_id'] and o['status']=='open' for o in result['orders']))
            reversed_ids=self.reversed_ids(c)
            for doc in result['documents']:
                doc['reversed']=doc['id'] in reversed_ids
            # FIFO allocation of received money to shipments for debt ageing; prepayments retained.
            debts=[]
            for p in result['partners']:
                if p['kind']!='client': continue
                p['balance']=self.balance(c,p['id'])
                p['debt']=self.settled_debt(c,p['id'])
                p['pending_today']=money_round(max(0,p['balance']-p['debt']))
                funds=sum(x['total']*(1 if x['kind']=='payment' else -1) for x in result['documents'] if x['partner_id']==p['id'] and x['kind'] in ('payment','refund') and not x['reversed'])
                overdue=0
                for doc in sorted((x for x in result['documents'] if x['partner_id']==p['id'] and x['kind']=='shipment' and not x['reversed']),key=lambda x:(x['date'],x['id'])):
                    credits=sum(x['total'] for x in result['documents'] if x['kind']=='return' and not x['reversed'] and json.loads(x['data'])['shipment_id']==doc['id'])
                    invoice=max(0,doc['total']-credits)
                    paid=min(funds,invoice); funds-=paid
                    amount=round(invoice-paid,2)
                    due=json.loads(doc['data'])['due']
                    if amount>0:
                        pending=doc['date']==date.today().isoformat() and not c.execute('SELECT 1 FROM day_closures WHERE date=?',(doc['date'],)).fetchone()
                        late=not pending and due<date.today().isoformat()
                        overdue+=amount if late else 0
                        debts.append(dict(client=p['name'],client_id=p['id'],document_id=doc['id'],date=doc['date'],due=due,amount=amount,overdue=late,pending=pending))
                p['overdue']=round(overdue,2)
            result['debts']=debts
            result['company']=json.loads(c.execute("SELECT value FROM settings WHERE key='company'").fetchone()[0])
            result['day_closed']=bool(c.execute('SELECT 1 FROM day_closures WHERE date=?',(date.today().isoformat(),)).fetchone())
            for doc in result['documents']:
                doc['data']=json.loads(doc['data'])
            for job in result['production_jobs']:
                job['products']=json.loads(job['products'])
            result['allocations']=self.allocate_orders(c)
            result['now']=now
            result['schema_version']=SCHEMA_VERSION
            return result

    def plan(self,due):
        return self.production_plan(due)

    def save_driver(self,d):
        name=required(d.get('name'),'Имя водителя')
        values=(name,str(d.get('phone','')),str(d.get('vehicle','')),int(bool(d.get('active',True))))
        with self.transaction() as c:
            if d.get('id'):
                self.one(c,'drivers',d['id'])
                c.execute('UPDATE drivers SET name=?,phone=?,vehicle=?,active=? WHERE id=?',values+(d['id'],))
                return d['id']
            return c.execute('INSERT INTO drivers(name,phone,vehicle,active) VALUES(?,?,?,?)',values).lastrowid

    def save_company(self,d):
        company={'name':required(d.get('name'),'Наименование отправителя')}
        company.update({k:str(d.get(k,'')) for k in ('address','phone','inn')})
        with self.transaction() as c:
            c.execute("UPDATE settings SET value=? WHERE key='company'",(dumps(company),))
        return 1

    def assign_delivery(self,d):
        with self.transaction() as c:
            order=self.one(c,'orders',d.get('order_id'))
            self.order_delivery(c,order['id'],d,self.partner(c,order['client_id'],'client'))
            return order['id']

    def route(self,dt,driver_id=None):
        dt=day(dt)
        state=self.snapshot()
        orders=[o for o in state['orders'] if o['due'][:10]==dt and o['status'] not in ('closed','deleted') and (driver_id is None or (o['driver_id'] or 0)==int(driver_id))]
        orders.sort(key=lambda o:(o['driver_id'] or 0,o['route_position'] or 999999,o['due'],o['id']))
        stops=[]
        for o in orders:
            client=next(p for p in state['partners'] if p['id']==o['client_id'])
            driver=next((d for d in state['drivers'] if d['id']==o['driver_id']),None)
            docs=[d for d in state['documents'] if d['kind']=='shipment' and not d['reversed'] and d['data']['order_id']==o['id']]
            goods=[]
            for l in state['order_lines']:
                if l['order_id']!=o['id']:continue
                it=next(i for i in state['items'] if i['id']==l['item_id'])
                goods.append({'name':it['name'],'unit':it['unit'],'planned':l['qty'],'actual':l['shipped']})
            stops.append(dict(order_id=o['id'],driver_id=o['driver_id'],driver_name=driver['name'] if driver else 'Не назначен',driver_phone=driver['phone'] if driver else '',vehicle=driver['vehicle'] if driver else '',
                position=o['route_position'],due=o['due'],client=client['name'],address=o['delivery_address'] or client['address'],phone=o['delivery_phone'] or client['phone'],contact=o['delivery_contact'] or client['contact'],
                goods=goods,invoice_numbers=[d['id'] for d in docs],total=money_round(sum(d['total'] for d in docs)),
                to_collect=money_round(sum(x['amount'] for x in state['debts'] if x['document_id'] in {doc['id'] for doc in docs})),
                planned_total=money_round(sum(l['qty']*l['price'] for l in state['order_lines'] if l['order_id']==o['id'])),note=o['note']))
        return {'date':dt,'company':state['company'],'stops':stops}
