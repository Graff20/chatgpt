"""Acceptance of the agreed 0.3 workflow through the browser and actual printed invoice."""
import os,sys,tempfile,threading
from pathlib import Path
from datetime import datetime,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Server
from demo import create_demo
from playwright.sync_api import sync_playwright,expect

with tempfile.TemporaryDirectory() as td:
 store=create_demo(Path(td)/'ceh.sqlite3');state=store.snapshot()
 product=next(i for i in state['items'] if i['name']=='Куриное филе сувид')
 employee=store.save_employee({'name':'Иванов Сергей'})
 store.save_item(dict(product,labor_rate=40,overhead_rate=5,shelf_days=14))
 server=Server(('127.0.0.1',0),td);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
   page=browser.new_page(viewport={'width':1366,'height':900},locale='ru-RU');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto(f'http://127.0.0.1:{server.server_port}');expect(page.locator('h1')).to_have_text('Производство под контролем')
   page.locator('#employee').fill('Технолог');page.locator('#employee').dispatch_event('change')
   for route in ['catalog','recipes','orders','production','stock','payroll','finance','analytics','logistics','settings']:
    page.locator(f'#nav a[href="#{route}"]').click();expect(page.locator('h1')).to_be_visible()
   page.locator('#nav a[href="#catalog"]').click();page.get_by_role('button',name='Раскрыть всё').click()
   expect(page.get_by_text('Куриное филе сувид',exact=True)).to_be_visible()
   page.get_by_role('button',name='Свернуть всё').click();expect(page.get_by_text('Куриное филе сувид',exact=True)).not_to_be_visible()
   page.locator('[data-filter="catalog.q"]').fill('сувид');expect(page.get_by_text('Куриное филе сувид',exact=True)).to_be_visible()
   page.locator('[data-action="edit-item"]').click();expect(page.locator('[name=shelf_days]')).to_have_value('14');expect(page.locator('[name=labor_rate]')).to_have_value('40')
   page.locator('[name=production_buffer]').fill('3');page.get_by_role('button',name='Сохранить',exact=True).click();expect(page.locator('#modal')).not_to_be_visible()
   page.locator('#nav a[href="#recipes"]').click();page.locator('[data-action="edit-recipe"]').first.click()
   expect(page.locator('[name=author]')).to_have_value('Технолог')
   assert page.locator('#recipe-lines').evaluate('(e)=>e.closest(".table-wrap").scrollWidth<=e.closest(".table-wrap").clientWidth'), 'Recipe requires horizontal scroll'
   page.get_by_role('button',name='На весь экран / свернуть').click();assert page.locator('#modal').bounding_box()['width']>1300
   page.get_by_role('button',name='Сохранить версию').click();expect(page.locator('#modal')).not_to_be_visible()
   page.locator('#nav a[href="#stock"]').click()
   with page.expect_popup() as popup:page.get_by_role('link',name='Ведомость для подсчёта',exact=True).click()
   sheet=popup.value;expect(sheet.locator('body')).to_contain_text('Факт');assert 'Учёт' not in sheet.locator('body').inner_text();sheet.close()
   page.locator('#nav a[href="#production"]').click();page.get_by_role('button',name='＋ Выпуск на склад').click()
   page.locator('[name=item_id]').select_option(str(product['id']));page.locator('[name=qty]').fill('2');page.locator('.worker-person').select_option(str(employee));page.locator('.worker-qty').fill('2')
   page.get_by_role('button',name='Провести выпуск').click();expect(page.locator('#modal')).not_to_be_visible()
   page.locator('#nav a[href="#payroll"]').click();expect(page.locator('body')).to_contain_text('80,00')
   page.locator('#nav a[href="#production"]').click();tomorrow=(datetime.now()+timedelta(days=1)).strftime('%Y-%m-%d');page.locator('#plan-date').fill(tomorrow);page.locator('#plan-date').dispatch_event('change')
   page.get_by_role('button',name='Передать наряд в работу').click();page.get_by_role('button',name='Передать в работу',exact=True).click();expect(page.locator('#modal')).not_to_be_visible();expect(page.get_by_text('В работе',exact=False).first).to_be_visible()
   client=next(p for p in state['partners'] if p['kind']=='client')
   r=page.request.post(f'http://127.0.0.1:{server.server_port}/api/orders',data={'client_id':client['id'],'manager':'М','due':tomorrow+'T12:00','lines':[{'item_id':product['id'],'qty':2,'price':790}]});assert r.ok
   page.evaluate('refresh()');expect(page.get_by_text('Добавлено +',exact=False).first).to_be_visible()
   page.locator('#nav a[href="#orders"]').click();page.get_by_role('button',name='＋ Новый заказ').click()
   page.locator('[name=client_id]').select_option(str(client['id']));page.locator('[name=due]').fill(datetime.now().isoformat(timespec='minutes'));page.locator('.r-item').select_option(str(product['id']));page.locator('.r-qty').fill('1')
   page.get_by_role('button',name='Сохранить',exact=True).click();expect(page.locator('#modal')).not_to_be_visible();orderid=page.evaluate('Math.max(...S.orders.map(o=>o.id))')
   page.locator(f'[data-action="view-order"][data-id="{orderid}"]').click();page.get_by_role('button',name='Отгрузить · уточнить вес').click()
   page.locator('.s-qty').fill('1.037');page.locator('[name=discount_percent]').fill('5')
   with page.expect_popup() as popup:page.get_by_role('button',name='Сохранить отгрузку и печатать счёт').click()
   invoice=popup.value;expect(invoice.locator('h1')).to_contain_text('Счёт №');expect(invoice.locator('.totals')).to_contain_text('Скидка 5%');expect(invoice.locator('.totals')).to_contain_text('778,27');assert invoice.locator('.invoice-lines th').count()==6
   box=invoice.locator('.totals').bounding_box();tablebox=invoice.locator('.invoice-lines').bounding_box();assert abs(box['x']+box['width']-tablebox['x']-tablebox['width'])<2
   invoice.close();expect(page.locator('#modal')).not_to_be_visible()
   page.locator('#nav a[href="#documents"]').click();page.locator('[data-action="view-document"]').first.click();page.get_by_role('button',name='Физобмен',exact=True).click()
   page.locator('[name=reason]').fill('Повреждение упаковки');page.locator('.exchange-qty').fill('0.1');page.locator('.replacement-qty').fill('0.1')
   page.get_by_role('button',name='Провести физобмен').click();expect(page.locator('#modal')).not_to_be_visible()
   page.locator('#nav a[href="#stock"]').click();expect(page.get_by_text('Карантин',exact=True)).to_be_visible()
   page.locator('#nav a[href="#analytics"]').click();expect(page.get_by_text('Полная себестоимость продаж',exact=True)).to_be_visible()
   assert not errors,errors
   browser.close();print('Browser 0.3 passed: tree/search, day-based expiry, rates, compact/full-screen TTK, blind sheet, piecework, frozen job changes, actual-weight invoice/discount/right alignment, exchange/quarantine, full cost.')
 finally:server.shutdown();server.server_close();thread.join()
