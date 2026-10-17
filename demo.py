"""Create an isolated demonstration database; never modifies the working database."""
from datetime import datetime,timedelta,date
from pathlib import Path
from domain import Store


def create_demo(path):
    s=Store(path)
    if s.snapshot()['items']:
        raise SystemExit('Демонстрационная база уже заполнена; существующие данные сохранены.')
    staff='Анна · технолог';now=datetime.now().replace(second=0,microsecond=0)
    base={'date':date.today().isoformat(),'responsible':staff}
    supplier=s.save_partner({'kind':'supplier','name':'Фермерский двор','inn':'7701234567','contact':'Алексей','phone':'+7 (900) 123-45-67'})
    client=s.save_partner({'kind':'client','name':'Кофейня «Тёплый хлеб»','manager':'Мария','payment_days':7,'debt_limit':50000,'order_days':'Пн, Ср, Пт','reminder':'manager'})
    client2=s.save_partner({'kind':'client','name':'Бистро «Соседи»','manager':'Иван','payment_days':3,'order_days':'Ежедневно','reminder':'self'})
    def item(name,kind,unit='кг',**kw):return s.save_item(dict(name=name,kind=kind,unit=unit,**kw))
    chicken=item('Куриное филе','raw',folder='Сырьё / Мясо',price=340,minimum=5,losses=[{'name':'Сувид','mechanical':5,'thermal':12}])
    flour=item('Мука пшеничная','raw',folder='Сырьё / Бакалея',price=65,minimum=10)
    sugar=item('Сахар','raw',folder='Сырьё / Бакалея',price=85,minimum=5)
    butter=item('Масло сливочное 82,5%','raw',folder='Сырьё / Молочные',price=850,minimum=3)
    eggs=item('Яйцо куриное','raw','шт.',folder='Сырьё / Яйцо',price=12,minimum=30)
    wrap=item('Пакет для палочек','packaging','шт.',folder='Упаковка',price=8,minimum=20)
    fillet=item('Куриное филе сувид','finished',folder='Кулинария / Мясо',category='Основные блюда',sale_price=790,shelf_hours=72,minimum=2)
    sticks=item('Бисквитные палочки · 12 шт.','finished','уп.',folder='Кондитерская / Выпечка',category='К кофе',sale_price=240,shelf_hours=120,pack_size=12,minimum=5)
    dough=item('Бисквитное тесто','semi',folder='Полуфабрикаты',mode='virtual')
    s.save_recipe({'item_id':fillet,'yield_qty':1,'lines':[{'item_id':chicken,'net':1,'mechanical':5,'thermal':12,'treatment':'Сувид'}],'technology':'Подготовить филе, упаковать, приготовить по утверждённому температурному режиму. Охладить.','storage':'Хранить в холодильнике, срок по маркировке партии.','quality':'Целая упаковка, однородный цвет.'})
    s.save_recipe({'item_id':dough,'yield_qty':1,'lines':[{'item_id':flour,'net':.4},{'item_id':sugar,'net':.3},{'item_id':butter,'net':.3}]})
    s.save_recipe({'item_id':sticks,'yield_qty':10,'lines':[{'item_id':dough,'net':1.5,'thermal':10,'treatment':'Выпечка'},{'item_id':eggs,'net':5},{'item_id':wrap,'net':10}],'technology':'Сформовать, выпечь, охладить, расфасовать по 12 штук.'})
    s.receipt(dict(base,partner_id=supplier,lines=[{'item_id':i,'qty':q,'price':p,'produced':(now-timedelta(days=1)).isoformat(timespec='minutes'),'expires':(now+timedelta(days=20)).isoformat(timespec='minutes'),'code':f'С-{i:03}'} for i,q,p in [(chicken,25,350),(flour,20,65),(sugar,10,85),(butter,5,850),(eggs,60,12),(wrap,100,8)]]))
    s.production(dict(base,item_id=fillet,qty=8,produced=(now-timedelta(minutes=30)).isoformat(timespec='minutes'),code='К-001'))
    s.production(dict(base,item_id=sticks,qty=20,produced=(now-timedelta(minutes=20)).isoformat(timespec='minutes'),code='Б-001'))
    order=s.save_order({'client_id':client,'due':(now+timedelta(days=1)).replace(hour=9,minute=0).isoformat(timespec='minutes'),'manager':'Мария','lines':[{'item_id':fillet,'qty':3,'price':790},{'item_id':sticks,'qty':8,'price':240}]})
    s.save_order({'client_id':client2,'due':(now+timedelta(days=1)).replace(hour=11,minute=0).isoformat(timespec='minutes'),'manager':'Иван','lines':[{'item_id':fillet,'qty':7,'price':790},{'item_id':sticks,'qty':16,'price':240}]})
    # A separate completed order demonstrates exact-weight shipping and financial reports.
    old=s.save_order({'client_id':client,'due':now.isoformat(timespec='minutes'),'manager':'Мария','lines':[{'item_id':fillet,'qty':1,'price':790}]})
    line=next(l for l in s.snapshot()['order_lines'] if l['order_id']==old)
    s.shipment(dict(base,order_id=old,lines=[{'line_id':line['id'],'qty':1.037}]))
    s.money(dict(base,kind='payment',partner_id=client,amount=500))
    s.money(dict(base,kind='expense',category='Логистика',amount=300))
    return s

if __name__=='__main__':
    root=Path(__file__).resolve().parent
    create_demo(root/'data'/'demo'/'ceh.sqlite3')
    print('Демо готово. Запуск: python app.py --data data/demo --port 8766 --open')
