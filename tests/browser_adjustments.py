"""Acceptance of compact ingredient tree, retroactive wages, removal and order summaries."""
import sys,tempfile,threading,re
from pathlib import Path
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Server
from demo import create_demo
from playwright.sync_api import sync_playwright,expect

with tempfile.TemporaryDirectory() as td:
 store=create_demo(Path(td)/'ceh.sqlite3');initial=store.snapshot();employee=store.save_employee({'name':'Повар Иван'});other=store.save_employee({'name':'Повар Анна'})
 product=next(i for i in initial['items'] if i['name']=='Куриное филе сувид')
 server=Server(('127.0.0.1',0),td);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);page=browser.new_page(viewport={'width':1440,'height':1000},locale='ru-RU')
   errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto(f'http://127.0.0.1:{server.server_port}');expect(page.locator('h1')).to_have_text('Производство под контролем');page.locator('#employee').fill('Руководитель');page.locator('#employee').dispatch_event('change')
   page.locator('#nav a[href="#recipes"]').click();page.locator('[data-action="edit-recipe"]').first.click()
   assert page.locator('#recipe-lines .item-search').count()==0
   assert page.locator('#recipe-lines tr').first.bounding_box()['height']<55
   page.locator('.r-net').first.fill('1.234')
   page.locator('.ingredient-button').first.click();expect(page.locator('.ingredient-panel')).to_be_visible()
   # Escape dismisses just the picker, preserving the open recipe and typed values.
   page.keyboard.press('Escape');expect(page.locator('.ingredient-panel')).not_to_be_visible();expect(page.locator('#modal')).to_be_visible();expect(page.locator('.r-net').first).to_have_value('1.234')
   page.locator('.ingredient-button').first.click()
   raw_folder=page.locator('.ingredient-tree summary').filter(has_text=re.compile(r'^📁 Сырьё$')).last
   if not raw_folder.evaluate('(e)=>e.parentElement.open'):raw_folder.click()
   page.locator('.ingredient-tree summary').filter(has_text=re.compile(r'^📁 Бакалея$')).click()
   page.locator('.ingredient-choice').filter(has_text='Мука пшеничная').click()
   expect(page.locator('.ingredient-name').first).to_have_text('Мука пшеничная');expect(page.locator('.r-net').first).to_have_value('1.234');expect(page.locator('.ingredient-panel')).not_to_be_visible()
   page.screenshot(path='/tmp/ceh-compact-recipe.png',full_page=True)
   page.locator('[data-action="close-modal"]').first.click()
   # User scenario: production with an assigned employee but zero rate, followed by editing card.
   page.locator('#nav a[href="#production"]').click();page.get_by_role('button',name='＋ Выпуск на склад').click();page.locator('[name=item_id]').select_option(str(product['id']));page.locator('[name=qty]').fill('2');page.locator('.worker-person').select_option(str(employee));page.locator('.worker-qty').fill('2');page.get_by_role('button',name='Провести выпуск').click();expect(page.locator('#modal')).not_to_be_visible()
   production=max(d['id'] for d in store.snapshot()['documents'] if d['kind']=='production')
   before=store.snapshot();qty={l['id']:l['qty'] for l in before['lots']}
   page.locator('#nav a[href="#catalog"]').click();page.locator('[data-filter="catalog.q"]').fill('сувид');page.locator(f'[data-action="edit-item"][data-id="{product["id"]}"]').click();page.locator('[name=labor_rate]').fill('50');page.get_by_role('button',name='Сохранить',exact=True).click();expect(page.locator('#modal')).not_to_be_visible()
   assert {l['id']:l['qty'] for l in store.snapshot()['lots']}==qty
   assert next(d for d in store.snapshot()['documents'] if d['id']==production)['data']['labor_cost']==100
   page.locator('#nav a[href="#payroll"]').click();expect(page.locator('body')).to_contain_text('100,00')
   # Correct the performer in an existing release, with no repeated stock posting.
   page.locator('#nav a[href="#documents"]').click();page.locator(f'[data-action="view-document"][data-id="{production}"]').click();page.get_by_role('button',name='Исполнители и начисления',exact=True).click();page.locator('.worker-person').select_option(str(other));page.get_by_role('button',name='Сохранить и пересчитать',exact=True).click();expect(page.locator('#modal')).not_to_be_visible()
   workers=next(d for d in store.snapshot()['documents'] if d['id']==production)['data']['workers'];assert workers[0]['employee_id']==other and workers[0]['amount']==100
   # Select two customer orders across filters and print the consolidated goods list.
   page.locator('#nav a[href="#orders"]').click();orders=[o for o in store.snapshot()['orders'] if o['status']=='open'];assert len(orders)==2
   page.locator(f'.order-select[data-id="{orders[0]["id"]}"]').check();page.locator('[data-filter="orders.q"]').fill('Соседи');expect(page.locator('.order-select')).to_have_count(1);page.locator('.order-select').check();expect(page.locator('#selected-orders-count')).to_have_text('Выбрано заказов: 2')
   page.get_by_role('button',name='Сводная ведомость',exact=True).click()
   before=store.snapshot()
   with page.expect_popup() as popup:page.get_by_role('button',name='Сформировать и печатать ведомость',exact=True).click()
   printed=popup.value;expect(printed.locator('h1')).to_contain_text('Сводная ведомость');expect(printed.locator('body')).to_contain_text('План к отгрузке');printed.close();expect(page.locator('#modal')).not_to_be_visible()
   state=store.snapshot();summary=next(d for d in state['documents'] if d['kind']=='dispatch_summary');assert {l['unit']:l['qty'] for l in summary['data']['lines']}=={'кг':10,'уп.':24};assert state['lots']==before['lots'] and state['order_lines']==before['order_lines']
   page.locator('#nav a[href="#documents"]').click();page.locator(f'[data-action="view-document"][data-id="{summary["id"]}"]').click();page.get_by_role('button',name='Удалить документ',exact=True).click();page.locator('[name=reason]').fill('Тест удаления');page.get_by_role('button',name='Удалить документ',exact=True).click();expect(page.locator('#modal')).not_to_be_visible();expect(page.locator(f'[data-action="view-document"][data-id="{summary["id"]}"]')).to_have_count(0)
   page.locator('[data-filter="documents.deleted"]').check();expect(page.locator(f'[data-action="view-document"][data-id="{summary["id"]}"]')).to_be_visible();expect(page.get_by_text('Удалён',exact=True)).to_be_visible()
   # Order removal also clears its production demand; the saved summary remains available in audit.
   page.locator('#nav a[href="#orders"]').click();page.locator('[data-filter="orders.q"]').fill('');page.locator(f'[data-action="view-order"][data-id="{orders[0]["id"]}"]').click();page.get_by_role('button',name='Удалить заказ',exact=True).click();page.locator('[name=reason]').fill('Дубль');page.get_by_role('button',name='Удалить заказ',exact=True).click();expect(page.locator('#modal')).not_to_be_visible()
   expect(page.locator(f'.order-select[data-id="{orders[0]["id"]}"]')).to_have_count(0)
   assert not errors,errors
   browser.close();print('Browser 0.3.1 passed: compact tree picker/Escape/unsaved values, zero-rate retroactive accrual, performer correction, selection across filters, summary printing, document audit deletion, order removal.')
 finally:server.shutdown();server.server_close();thread.join()
