"""Actual user scenario: 10/5/15 portions, weight-based invoice, expense drilldown and report views."""
import sys,tempfile,threading,json
from pathlib import Path
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Server
from demo import create_demo
from playwright.sync_api import sync_playwright,expect

with tempfile.TemporaryDirectory() as td:
 store=create_demo(Path(td)/'ceh.sqlite3');state=store.snapshot();classic=next(i for i in state['items'] if i['name']=='Куриное филе сувид');raw=next(i for i in state['items'] if i['name']=='Куриное филе');pack=next(i for i in state['items'] if i['unit']=='уп.')
 store.save_item(dict(classic,portion_weight=.2));products=[(classic['id'],10,2.35)]
 for name,weight,count,price,actual in [('Филе сувид Прованские травы',.18,5,830,1.12),('Филе сувид Южные пряности',.22,15,850,3.05)]:
  iid=store.save_item(dict(name=name,kind='finished',unit='кг',portion_weight=weight,shelf_days=14,sale_price=price))
  store.save_recipe(dict(item_id=iid,yield_qty=1,lines=[dict(item_id=raw['id'],net=1)]))
  store.production(dict(item_id=iid,qty=5,date=datetime.now().date().isoformat(),produced=datetime.now().isoformat(timespec='minutes'),responsible='Технолог'))
  products.append((iid,count,actual))
 server=Server(('127.0.0.1',0),td);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);page=browser.new_page(viewport={'width':1600,'height':1100},locale='ru-RU');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto(f'http://127.0.0.1:{server.server_port}');expect(page.locator('h1')).to_have_text('Производство под контролем');page.locator('#employee').fill('Менеджер');page.locator('#employee').dispatch_event('change')
   page.locator('#nav a[href="#orders"]').click();page.get_by_role('button',name='＋ Новый заказ').click();page.locator('[name=client_id]').select_option(label='Кофейня «Тёплый хлеб»');page.locator('[name=due]').fill(datetime.now().isoformat(timespec='minutes'))
   for n,(iid,count,actual) in enumerate(products):
    if n:page.get_by_role('button',name='＋ Позиция',exact=True).click()
    row=page.locator('#order-lines tr').nth(n);row.locator('.r-item').select_option(str(iid));row.locator('.r-unit').select_option(label='порц.');row.locator('.r-qty').fill(str(count));expect(row.locator('.r-tol')).to_be_disabled();expect(row.locator('.r-portion-weight')).to_be_enabled()
   page.get_by_role('button',name='＋ Позиция',exact=True).click();row=page.locator('#order-lines tr').last;row.locator('.r-item').select_option(str(pack['id']));row.locator('.r-qty').fill('4');assert row.locator('.r-unit option').filter(has_text='порц.').count()==0
   expect(page.locator('#order-summary')).to_contain_text('ориентировочному весу');page.get_by_role('button',name='Сохранить',exact=True).click();expect(page.locator('#modal')).not_to_be_visible();oid=page.evaluate('Math.max(...S.orders.map(o=>o.id))')
   page.locator(f'[data-action="view-order"][data-id="{oid}"]').click();expect(page.locator('.modal-body')).to_contain_text('10 порц.');page.get_by_role('button',name='Отгрузить · уточнить вес').click()
   expect(page.locator('.s-portions')).to_have_count(3);assert page.locator('.s-qty').first.input_value()==''
   page.get_by_role('button',name='Провести отгрузку',exact=True).click();expect(page.locator('#form-error')).to_contain_text('фактический вес')
   for n,(_,count,actual) in enumerate(products):page.locator('.s-qty').nth(n).fill(str(actual))
   expect(page.locator('.s-qty').last).to_have_value('4');expect(page.locator('#shipment-total')).to_contain_text('6 338,60')
   with page.expect_popup() as popup:page.get_by_role('button',name='Сохранить отгрузку и печатать счёт').click()
   invoice=popup.value;expect(invoice.locator('h1')).to_contain_text('Счёт №');expect(invoice.locator('.totals')).to_contain_text('6 338,60');expect(invoice.locator('.invoice-lines')).to_contain_text('10 порц.');expect(invoice.locator('.invoice-lines tbody tr')).to_have_count(4);invoice.close();expect(page.locator('#modal')).not_to_be_visible()
   state=store.snapshot();assert next(o for o in state['orders'] if o['id']==oid)['status']=='completed';assert [l['shipped_portions'] for l in state['order_lines'] if l['order_id']==oid]==[10,5,15,0]
   page.locator('#nav a[href="#finance"]').click();expect(page.get_by_text('Финансовые итоги за период',exact=True)).to_be_visible();page.get_by_role('button',name='＋ Расход',exact=True).click();page.locator('[name=category]').fill('Логистика');page.locator('[name=purpose]').fill('Доставка заказов курьером Петровым');page.locator('[name=recipient]').fill('Петров');page.locator('[name=amount]').fill('1250');page.get_by_role('button',name='Провести',exact=True).click();expect(page.locator('#modal')).not_to_be_visible()
   expense=max(d['id'] for d in store.snapshot()['documents'] if d['kind']=='expense');page.locator('[data-filter="finance.view"]').select_option('detail');page.get_by_role('button',name='Доставка заказов курьером Петровым',exact=True).click();expect(page.locator('#modal .notice')).to_contain_text('Петров');expect(page.locator('#modal .notice')).to_contain_text('1 250,00');page.locator('[data-action="close-modal"]').first.click()
   page.locator('[data-filter="finance.view"]').select_option('total');expect(page.locator('.finance-summary').last).to_contain_text('1 550,00');page.get_by_role('button',name='Посмотреть, на что потрачено').click();expect(page.locator('#modal')).to_contain_text('Доставка заказов курьером Петровым');page.locator('[data-action="close-modal"]').first.click()
   with page.expect_download() as download:page.locator('a[href*="type=financial"][href*="format=docx"]').click()
   assert download.value.suggested_filename.endswith('.docx')
   page.locator('#nav a[href="#documents"]').click();page.get_by_role('button',name=f'Удалить документ № {expense}',exact=True).click();page.locator('[name=reason]').fill('Дубль платежа');page.get_by_role('button',name='Удалить документ',exact=True).click();expect(page.locator('#modal')).not_to_be_visible();expect(page.get_by_role('button',name=f'Удалить документ № {expense}',exact=True)).to_have_count(0)
   page.locator('#nav a[href="#finance"]').click();expect(page.locator('.finance-summary').last).to_contain_text('300,00')
   # A delayed financial response must not replace another page after navigation.
   page.evaluate('''async () => {const f=await api('financial');window.savedFetch=window.fetch;let once=false;window.fetch=(...args)=>{if(String(args[0]).includes('/api/financial?')&&!once){once=true;return new Promise(resolve=>window.releaseFinancial=()=>resolve(new Response(JSON.stringify(f),{status:200,headers:{'Content-Type':'application/json'}})));}return window.savedFetch(...args);};window.pendingFinancialRender=render();}''')
   page.locator('#nav a[href="#orders"]').click();expect(page.locator('h1')).to_have_text('Заказы клиентов');page.evaluate('window.releaseFinancial();window.pendingFinancialRender');expect(page.locator('h1')).to_have_text('Заказы клиентов');page.evaluate('window.fetch=window.savedFetch')
   assert not errors,errors
   browser.close();print('Browser 0.4 passed: 10/5/15 portions plus packages, mandatory actual weight, exact invoice and count-based completion, expense purpose/drilldown, financial total/structure/detail, DOCX, direct journal removal.')
 finally:server.shutdown();server.server_close();thread.join()
