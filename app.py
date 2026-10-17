"""Local HTTP application, Windows/Linux. Start: python app.py --open."""
import argparse
import base64
import json
import os
import sqlite3
import threading
import traceback
import sys
import webbrowser
from html import escape
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from contextlib import closing
from domain import Store, ValidationError, dumps, SCHEMA_VERSION
from exports import document_table, export, invoice_html

ROOT=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))
DATA_ROOT=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else ROOT
LOCK=threading.RLock()
MAX_BODY=64*1024*1024
APP_VERSION='0.2.2'

ASSETS=('index.html','app.js','style.css')

def check_assets():
    missing=[]
    for name in ASSETS:
        try:
            (ROOT/'static'/name).read_bytes()
        except OSError:
            missing.append('static/'+name)
    if missing:
        raise ValidationError('Не найдены или недоступны файлы интерфейса: '+', '.join(missing)+
            '. Распакуйте всю папку программы, включая static. Папку data сохраните. База не открывалась.')

def recovery_page(filename):
    return ('''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Цех — восстановление интерфейса</title><style>body{font:16px "Segoe UI",Arial;background:#f4f5f0;color:#20372f;margin:0;padding:40px 20px}main{max-width:760px;margin:auto;background:white;border:1px solid #dce2d9;padding:30px;border-radius:12px}li{margin:14px 0;line-height:1.6}a{color:#1c5947}code{background:#eef3e9;padding:3px 5px}</style><main>
<h1>Не удалось открыть файлы интерфейса</h1><p>Файл <code>'''+escape(filename)+'''</code> отсутствует или недоступен. Это сообщение само по себе не означает потерю базы.</p>
<p><a href="/api/backup">Скачать резервную копию текущей базы</a></p><ol>
<li>Остановите все окна запуска «Цех» сочетанием Ctrl+C.</li>
<li>Сохраните всю папку <code>data</code> отдельно.</li>
<li>Распакуйте полный архив программы. Рядом с <code>app.py</code> должна быть папка <code>static</code> с файлами <code>index.html</code>, <code>app.js</code>, <code>style.css</code>.</li>
<li>Перенесите вашу сохранённую папку <code>data</code> в папку программы. Не заменяйте её пустой базой.</li>
<li>Запустите один экземпляр <code>start.bat</code>, затем обновите страницу.</li></ol>
<p>Если ошибка повторится, передайте последние строки ошибки из окна запуска. Не удаляйте <code>data</code>.</p></main></html>''').encode('utf-8')

class Handler(BaseHTTPRequestHandler):
    server_version='Ceh/'+APP_VERSION
    def log_message(self,fmt,*args):
        # Do not log query parameters or request bodies.
        print(f'[{datetime.now():%H:%M:%S}] {args[1] if len(args)>1 else "request"}')
    @property
    def store(self): return self.server.store
    def send(self,status,data,ctype='application/json; charset=utf-8',filename=None):
        if not isinstance(data,bytes): data=dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type',ctype)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','SAMEORIGIN')
        self.send_header('Referrer-Policy','same-origin')
        if filename: self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        try:
            with LOCK: self.get()
        except (ValidationError,ValueError,KeyError) as e: self.send(400,{'error':str(e)})
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception:
            traceback.print_exc(); self.send(500,{'error':'Ошибка сервера. Подробности в окне запуска.'})
    def get(self):
        u=urlparse(self.path); q=parse_qs(u.query); path=u.path
        if path=='/api/runtime': return self.send(200,self.server.runtime_info())
        if path=='/api/state':
            state=self.store.snapshot()
            state['runtime']=self.server.runtime_info()
            return self.send(200,state)
        if path=='/api/route': return self.send(200,self.store.route(q.get('date',[date.today().isoformat()])[0],q.get('driver_id',[None])[0]))
        if path=='/api/plan': return self.send(200,self.store.plan(q.get('date',[date.today().isoformat()])[0]))
        if path=='/api/backup':
            target=self.server.data/'backups'/f'manual-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3'
            self.server.backup(target)
            return self.send(200,target.read_bytes(),'application/vnd.sqlite3','ceh-backup.sqlite3')
        if path=='/api/export':
            state=self.store.snapshot(); typ=q.get('type',['stock'])[0]; fmt=q.get('format',['xlsx'])[0]
            title,rows=self.export_rows(state,typ,q)
            if typ=='document' and fmt=='html':
                doc=next((d for d in state['documents'] if d['id']==int(q.get('id',['0'])[0])),None)
                if doc and doc['kind']=='shipment':
                    return self.send(200,invoice_html(doc),'text/html; charset=utf-8')
            content,mime=export(title,rows,fmt)
            return self.send(200,content,mime,None if fmt=='html' else 'ceh-export.'+fmt)
        files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if path not in files: return self.send(404,{'error':'Не найдено'})
        p=ROOT/'static'/files[path]
        mime={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'}[p.suffix]
        try:
            content=p.read_bytes()
        except OSError:
            traceback.print_exc()
            if path=='/': return self.send(503,recovery_page('static/'+files[path]),'text/html; charset=utf-8')
            return self.send(503,{'error':'Недоступен файл static/'+files[path]+'. Восстановите папку static из полного архива программы.'})
        self.send(200,content,mime)
    def export_rows(self,state,typ,q):
        items={i['id']:i for i in state['items']}
        ident=int(q.get('id',['0'])[0])
        if typ=='document':
            doc=next((x for x in state['documents'] if x['id']==ident),None)
            if not doc: raise ValidationError('Документ не найден')
            return document_table(doc)
        if typ in ('recipe','calculation'):
            recipe=next((x for x in state['recipes'] if x['id']==ident),None)
            if not recipe: raise ValidationError('ТТК не найдена')
            item=items[recipe['item_id']]
            rows=[['Версия',recipe['version']],['Базовый выход',recipe['yield_qty'],item['unit']]]
            rows += [['Ингредиент','Ед.','Обработка','Брутто','Мех. потери, %','Нетто после мех.','Тепл. потери, %','Выход','Цена, ₽','Стоимость, ₽']]
            total=0
            for l in recipe['lines']:
                price=items[l['item_id']]['planned_cost']; value=price*l['gross']; total+=value
                rows.append([l['name'],l['unit'],l['treatment'],round(l['gross'],6),l['mechanical'],round(l['gross']*(1-l['mechanical']/100),6),l['thermal'],l['net'],round(price,2),round(value,2)])
            rows += [['Итого, ₽',round(total,2)],['Себестоимость единицы, ₽',round(total/recipe['yield_qty'],2)],['Технология',recipe['technology']],['Качество',recipe['quality']],['Хранение',recipe['storage']]]
            return ('Технологическая карта' if typ=='recipe' else 'Калькуляционная карта')+f' — {item["name"]}, в{recipe["version"]}',rows
        if typ=='order':
            order=next((x for x in state['orders'] if x['id']==ident),None)
            if not order: raise ValidationError('Заказ не найден')
            client=next(p for p in state['partners'] if p['id']==order['client_id'])
            rows=[['Клиент',client['name']],['К отгрузке',order['due']],['Менеджер',order['manager']],['Товар','Заказано','Отгружено','Ед.','Цена, ₽']]
            rows += [[items[l['item_id']]['name'],l['qty'],l['shipped'],items[l['item_id']]['unit'],l['price']] for l in state['order_lines'] if l['order_id']==ident]
            return f'Заказ № {ident}',rows
        if typ=='plan':
            plan=self.store.plan(q.get('date',[date.today().isoformat()])[0])
            rows=[['Товар','Заказано','Годный остаток','Потребность ранних заказов','Свободно','Произвести','Ед.']]
            rows += [[r['name'],r['ordered'],r['available'],r['prior_demand'],r['free'],r['produce'],r['unit']] for r in plan['products']]
            rows += [[],['Сырьё','Требуется','Доступно сейчас','Дефицит','Ед.']]
            rows += [[r['name'],r['qty'],r['available'],r['shortage'],r['unit']] for r in plan['ingredients']]
            return 'Наряд на '+plan['date'],rows
        if typ=='route':
            route=self.store.route(q.get('date',[date.today().isoformat()])[0],q.get('driver_id',[None])[0])
            rows=[['Отправитель',route['company']['name']],['Дата',route['date']],[],['Водитель / авто','Порядок','Заказ / время','Клиент','Адрес','Телефон','Контактное лицо','Накладные','Продукция: план / факт','Сумма накладных, ₽','Осталось оплатить, ₽','Отметка / подпись']]
            for n,stop in enumerate(route['stops'],1):
                goods='; '.join(f"{g['name']}: {g['planned']} / {g['actual']} {g['unit']}" for g in stop['goods'])
                rows.append([stop['driver_name']+' · '+stop['driver_phone']+' / '+stop['vehicle'],stop['position'] or n,str(stop['order_id'])+' / '+stop['due'][11:],stop['client'],stop['address'],stop['phone'],stop['contact'],', '.join(map(str,stop['invoice_numbers'])),goods,stop['total'],stop['to_collect'],''])
            rows.append(['Итого','','','','','','','','',round(sum(s['total'] for s in route['stops']),2),round(sum(s['to_collect'] for s in route['stops']),2),''])
            return 'Маршрутный лист — '+route['date'],rows
        if typ=='debts': return 'Задолженность',[['Клиент','Накладная','Дата','Срок оплаты','Долг, ₽','Просрочен']]+[[d['client'],d['document_id'],d['date'],d['due'],d['amount'],'К оплате сегодня' if d['pending'] else 'Просрочен' if d['overdue'] else 'Долг'] for d in state['debts']]
        if typ=='analytics':
            start=q.get('from',['0001-01-01'])[0]; end=q.get('to',['9999-12-31'])[0]
            group=q.get('group',['product'])[0]
            if group not in ('product','client','day'): raise ValidationError('Неверная группировка')
            result={}
            for doc in state['documents']:
                if doc['kind'] not in ('shipment','return') or doc['reversed'] or not start<=doc['date']<=end: continue
                sign=-1 if doc['kind']=='return' else 1
                for l in doc['data']['lines']:
                    # Units stay separate even when grouping by client/date.
                    label=l['name'] if group=='product' else doc['data']['client_name'] if group=='client' else doc['date']
                    key=(label,l['unit']); r=result.setdefault(key,[label,l['unit'],0,0,0])
                    r[2]+=sign*l['qty']; r[3]+=sign*l.get('line_total',l['qty']*l['price']); r[4]+=sign*sum(x['qty']*x['unit_cost'] for x in l['lots'])
            all_columns={'qty':(2,'Количество'),'revenue':(3,'Выручка, ₽'),'cost':(4,'Себестоимость, ₽'),'profit':(5,'Валовая прибыль, ₽')}
            selected=[k for k in q.get('columns',['qty,revenue,cost,profit'])[0].split(',') if k in all_columns]
            return 'Продажи и валовая прибыль',[['Группа','Ед.']+[all_columns[k][1] for k in selected]]+[[r[0],r[1]]+[(r+[r[3]-r[4]])[all_columns[k][0]] for k in selected] for r in result.values()]
        if typ=='prices':
            rows=[['Дата','Сырьё','Поставщик','Цена, ₽','Ед.','Количество']]
            for d in state['documents']:
                if d['kind']=='receipt' and not d['reversed'] and q.get('from',['0001-01-01'])[0]<=d['date']<=q.get('to',['9999-12-31'])[0]:
                    rows += [[d['date'],l['name'],d['data']['supplier_name'],l['price'],l['unit'],l['qty']] for l in d['data']['lines']]
            return 'История закупочных цен',rows
        if typ=='stock':
            rows=[['Партия','Товар','Ед.','Изготовлено','Годен до','Остаток','Цена партии, ₽','Статус']]
            rows += [[l['code'],items[l['item_id']]['name'],items[l['item_id']]['unit'],l['produced'],l['expires'] or 'Без срока',l['qty'],l['unit_cost'],'Карантин' if l['blocked'] else 'Просрочено' if l['expires'] and l['expires']<=state['now'] else 'Годно'] for l in state['lots'] if l['qty']>1e-7]
            return 'Остатки / ведомость для ревизии',rows
        raise ValidationError('Неверный тип выгрузки')
    def do_POST(self):
        try:
            # Reject cross-origin browser requests and non-JSON form submissions.
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                return self.send(403,{'error':'Запрос с другого сайта запрещён'})
            if self.headers.get_content_type()!='application/json':
                return self.send(415,{'error':'Требуется JSON'})
            size=int(self.headers.get('Content-Length','0'))
            if size<1 or size>MAX_BODY: return self.send(413,{'error':'Размер запроса недопустим (лимит 64 МБ)'})
            d=json.loads(self.rfile.read(size))
            if not isinstance(d,dict): raise ValidationError('Ожидается объект JSON')
            with LOCK:
                if self.path=='/api/restore': return self.restore(d)
                actions={'/api/drivers':self.store.save_driver,'/api/company':self.store.save_company,'/api/assign-delivery':self.store.assign_delivery,'/api/close-day':self.store.close_day,'/api/reverse':self.store.reverse,'/api/return':self.store.customer_return,'/api/release':self.store.release_lot,'/api/items':self.store.save_item,'/api/partners':self.store.save_partner,'/api/recipes':self.store.save_recipe,
                    '/api/receipt':self.store.receipt,'/api/production':self.store.production,'/api/orders':self.store.save_order,
                    '/api/close-order':self.store.close_order,'/api/shipment':self.store.shipment,'/api/money':self.store.money,
                    '/api/writeoff':self.store.writeoff,'/api/inventory':self.store.inventory}
                action=actions.get(self.path)
                if not action: return self.send(404,{'error':'Не найдено'})
                self.send(200,{'id':action(d)})
        except sqlite3.IntegrityError as e:
            self.send(400,{'error':'Проверьте уникальность наименования и связи справочников.'})
        except (ValidationError,ValueError,TypeError,KeyError) as e: self.send(400,{'error':str(e)})
        except Exception:
            traceback.print_exc(); self.send(500,{'error':'Ошибка сервера. Операция не выполнена; проверьте окно запуска.'})
    def restore(self,d):
        if d.get('confirm')!='ВОССТАНОВИТЬ': raise ValidationError('Введите ВОССТАНОВИТЬ для подтверждения')
        raw=base64.b64decode(d.get('file',''),validate=True)
        if not raw.startswith(b'SQLite format 3\x00'): raise ValidationError('Это не резервная копия SQLite')
        temp=self.server.data/'restore-candidate.sqlite3'
        if temp.exists(): temp.unlink()
        temp.write_bytes(raw)
        try:
            with closing(sqlite3.connect(f'file:{temp.as_posix()}?mode=ro',uri=True)) as c:
                c.execute('PRAGMA trusted_schema=OFF')
                if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValidationError('Повреждённая база')
                if c.execute('PRAGMA user_version').fetchone()[0] not in (1,SCHEMA_VERSION): raise ValidationError('Неподдерживаемая версия базы')
                for table in ('items','partners','lots','movements','recipes','documents','orders','order_lines'):
                    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone(): raise ValidationError('Неверная структура базы')
                if c.execute('PRAGMA foreign_key_check').fetchone(): raise ValidationError('Повреждены связи базы')
                if c.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone(): raise ValidationError('Копия содержит неподдерживаемые объекты')
            candidate=Store(temp)
            candidate.snapshot()  # Validate required columns and JSON before touching live data.
            with closing(candidate.connect()) as c:
                c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                c.execute('PRAGMA journal_mode=DELETE')
            self.server.backup(self.server.data/'backups'/f'before-restore-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3')
            with closing(self.store.connect()) as c:
                c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                c.execute('PRAGMA journal_mode=DELETE')
            os.replace(temp,self.store.path)
            self.server.store=Store(self.store.path)
            self.send(200,{'ok':True})
        finally:
            temp.unlink(missing_ok=True)

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,data):
        check_assets()
        self.data=Path(data).resolve()
        self.started_at=datetime.now().isoformat(timespec='seconds')
        super().__init__(address,Handler,bind_and_activate=False)
        try:
            # Bind first: a second process must not create or migrate another database.
            self.server_bind()
            self.data.mkdir(parents=True,exist_ok=True)
            self.store=Store(self.data/'ceh.sqlite3')
            (self.data/'backups').mkdir(exist_ok=True)
            self.backup(self.data/'backups'/f'start-{date.today().isoformat()}.sqlite3',replace=False)
            self.server_activate()
        except Exception:
            self.server_close()
            raise
    def runtime_info(self):
        return {'app_version':APP_VERSION,'schema_version':SCHEMA_VERSION,'pid':os.getpid(),
                'started_at':self.started_at,'program_directory':str(DATA_ROOT.resolve()),
                'data_directory':str(self.data),'database_path':str(self.store.path.resolve())}

    def backup(self,target,replace=True):
        if target.exists() and not replace:return
        with closing(self.store.connect()) as src, closing(sqlite3.connect(target)) as dst: src.backup(dst)

def main():
    p=argparse.ArgumentParser(description='Цех — учёт производства')
    p.add_argument('--host',default='127.0.0.1'); p.add_argument('--port',type=int,default=8765)
    p.add_argument('--data',default=str(DATA_ROOT/'data')); p.add_argument('--open',action='store_true')
    args=p.parse_args()
    try:
        server=Server((args.host,args.port),args.data)
    except ValidationError as e:
        print('Запуск отменён:',e)
        return 1
    except OSError as e:
        print('Не удалось запустить сервер:',e)
        print('Если порт занят, остановите прежнее окно запуска и повторите. Папку data не удаляйте.')
        return 1
    print(f'Цех {APP_VERSION} · процесс {os.getpid()} · http://127.0.0.1:{server.server_port}\nПрограмма: {DATA_ROOT.resolve()}\nБаза: {server.store.path}\nДля остановки: Ctrl+C')
    if args.host=='0.0.0.0': print('Сетевой режим: доступ разрешён в доверенной локальной сети. Не открывайте этот порт в интернет.')
    if args.open: webbrowser.open(f'http://127.0.0.1:{server.server_port}')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=='__main__':sys.exit(main())
