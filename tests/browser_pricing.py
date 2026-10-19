"""Price matrix workflow in the real browser, including client assignment and saved orders."""
import sys,tempfile,threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Server
from demo import create_demo
from playwright.sync_api import sync_playwright,expect

with tempfile.TemporaryDirectory() as td:
 store=create_demo(Path(td)/'ceh.sqlite3');state=store.snapshot();product=next(i for i in state['items'] if i['name']=='Куриное филе сувид');client=next(p for p in state['partners'] if p['kind']=='client');raw=next(i for i in state['items'] if i['kind']=='raw')
 store.save_company(dict(name='ООО Кухня',address='Москва',phone='+7 900 123-45-67'))
 store.save_item(dict(product,sale_price=947,folder='Кулинария/Сувид',category='Курица'))
 server=Server(('127.0.0.1',0),td);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);page=browser.new_page(viewport={'width':1600,'height':1100},locale='ru-RU');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto(f'http://127.0.0.1:{server.server_port}');expect(page.locator('h1')).to_have_text('Производство под контролем');page.locator('#employee').fill('Менеджер');page.locator('#employee').dispatch_event('change')
   page.locator('#nav a[href="#prices"]').click();expect(page.locator('h1')).to_have_text('Прайсы');expect(page.locator('.price-product')).to_have_count(len([i for i in state['items'] if i['kind']=='finished']))
   expect(page.locator('.price-matrix')).to_contain_text('Кулинария');expect(page.locator('.price-matrix')).to_contain_text('Сувид');expect(page.locator('.price-matrix')).to_contain_text('Курица');expect(page.locator(f'.price-product[data-item="{raw["id"]}"]')).to_have_count(0)
   page.get_by_role('button',name='Свернуть всё',exact=True).click();expect(page.locator(f'.price-product[data-item="{product["id"]}"]')).not_to_be_visible();page.get_by_role('button',name='Раскрыть всё',exact=True).click()
   page.get_by_role('button',name='＋ Столбец цен').click();page.locator('[name=name]').fill('Дистрибьютор');page.locator('[name=adjustment]').fill('-15');page.locator('[name=round_step]').select_option('10');page.get_by_role('button',name='Предварительный просмотр').click();expect(page.locator('#modal')).to_contain_text('800,00');assert len(store.pricing()['lists'])==1;page.get_by_role('button',name='Применить изменения').click();expect(page.locator('#modal')).not_to_be_visible();lid=store.pricing()['lists'][-1]['id']
   cell=page.locator(f'[data-action="price-cell"][data-id="{product["id"]}"][data-list="{lid}"]');expect(cell).to_contain_text('800,00');cell.click();page.locator('[name=amount]').fill('777');expect(page.locator('[name=price_mode]')).to_have_value('manual');page.get_by_role('button',name='Предварительный просмотр').click();page.get_by_role('button',name='Применить изменения').click();expect(cell).to_contain_text('777,00');expect(cell).to_contain_text('Вручную')
   # Changing the source price keeps the manual exception.
   page.locator(f'[data-action="price-cell"][data-id="{product["id"]}"][data-list="1"]').click();page.locator('[name=amount]').fill('1000');page.get_by_role('button',name='Предварительный просмотр').click();page.get_by_role('button',name='Применить изменения').click();expect(cell).to_contain_text('777,00')
   cell.click();page.locator('[name=price_mode]').select_option('auto');page.get_by_role('button',name='Предварительный просмотр').click();page.get_by_role('button',name='Применить изменения').click();expect(cell).to_contain_text('850,00')
   # New finished goods enter the same tree and derived column automatically.
   new=store.save_item(dict(name='Филе новое',kind='finished',unit='кг',sale_price=200,folder='Кулинария/Сувид',category='Курица'));page.evaluate('refresh()');expect(page.locator(f'.price-product[data-item="{new}"]')).to_be_visible();expect(page.locator(f'[data-action="price-cell"][data-id="{new}"][data-list="{lid}"]')).to_contain_text('170,00')
   page.locator(f'.price-select[data-id="{new}"]').check();page.get_by_role('button',name='Автозаполнение выбранных').click();page.locator('[name=list_id]').select_option(str(lid));page.get_by_role('button',name='Предварительный просмотр').click();page.get_by_role('button',name='Применить изменения').click();expect(page.locator('#modal')).not_to_be_visible()
   with page.expect_download() as dl:page.locator('a[href*="type=prices"][href*="format=xlsx"]').click()
   assert dl.value.suggested_filename.endswith('.xlsx')
   page.locator('#nav a[href="#partners"]').click();page.locator(f'[data-action="edit-partner"][data-id="{client["id"]}"]').click();page.locator('[name=price_list_id]').select_option(str(lid));page.get_by_role('button',name='Сохранить',exact=True).click();expect(page.locator('#modal')).not_to_be_visible()
   page.locator('#nav a[href="#orders"]').click();page.get_by_role('button',name='＋ Новый заказ').click();page.locator('[name=client_id]').select_option(str(client['id']));page.locator('.r-item').select_option(str(product['id']));expect(page.locator('.r-price')).to_have_value('850');expect(page.locator('.r-price-note')).to_contain_text('Дистрибьютор');page.locator('.r-qty').fill('2');page.get_by_role('button',name='Сохранить',exact=True).click();expect(page.locator('#modal')).not_to_be_visible();oid=max(o['id'] for o in store.snapshot()['orders'])
   store.edit_prices(dict(token=store.pricing()['token'],list_id=1,changes=[dict(item_id=product['id'],mode='manual',amount=1200)],apply=True,responsible='М'));page.evaluate('refresh()');page.locator(f'[data-action="view-order"][data-id="{oid}"]').click();page.get_by_role('button',name='Изменить заказ',exact=True).click();expect(page.locator('.r-price')).to_have_value('850');page.get_by_role('button',name='Обновить цены по прайсу').click();expect(page.locator('#order-price-preview')).to_contain_text('1 020,00');expect(page.locator('.r-price')).to_have_value('850');page.get_by_role('button',name='Применить цены к этому заказу').click();expect(page.locator('.r-price')).to_have_value('1020');page.get_by_role('button',name='Сохранить',exact=True).click();expect(page.locator('#modal')).not_to_be_visible();assert next(l for l in store.snapshot()['order_lines'] if l['order_id']==oid)['price']==1020
   assert not errors,errors
   page.locator('#nav a[href="#prices"]').click();page.screenshot(path='/tmp/ceh-prices-050.png',full_page=True)
   # Visually identical folder labels must remain a single group in editor and print.
   for n,folder in enumerate(['Бисквитные палочки',' Бисквитные  палочки\u00a0','БИСКВИТНЫЕ ПАЛОЧКИ\u200b']):store.save_item(dict(name='Палочки проверка '+str(n),kind='finished',unit='уп.',sale_price=240,folder=folder,category=''))
   page.evaluate('refresh()');expect(page.locator('.price-group button').filter(has_text='Бисквитные палочки')).to_have_count(1)
   with page.expect_popup() as popup:page.locator('a[href*="type=prices"][href*="format=html"]').click()
   printed=popup.value;expect(printed.locator('h1')).to_have_text('Прайс-лист');expect(printed.locator('.price-meta')).to_contain_text('ООО Кухня');expect(printed.locator('.price-section')).to_have_count(5);expect(printed.locator('.price-section').filter(has_text='Папка: Бисквитные палочки')).to_have_count(1);expect(printed.locator('body')).not_to_contain_text('Без категории');expect(printed.locator('.price-list')).to_contain_text('1 020,00');printed.screenshot(path='/tmp/ceh-price-print-051.png',full_page=True);printed.close()
   browser.close();print('Browser pricing passed: structured finished catalogue, collapse/expand, formula preview, rounding, manual exception/reset, new items, bulk selection, XLSX, client price, preserved order and explicit repricing.')
 finally:server.shutdown();server.server_close();thread.join()
