"""Checkbox print selection and explicit Yes/No cascade confirmation."""
import sys,tempfile,threading,io,zipfile,xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Server
from demo import create_demo
from playwright.sync_api import sync_playwright,expect

with tempfile.TemporaryDirectory() as td:
 store=create_demo(Path(td)/'ceh.sqlite3');store.edit_prices(dict(token=store.pricing()['token'],column=dict(name='Дистрибьютор',source_id=1,adjustment=-15,round_step=10),apply=True,responsible='Р'))
 receipt=next(d['id'] for d in store.snapshot()['documents'] if d['kind']=='receipt');plan=store.deletion_preview(dict(document_id=receipt));assert plan['dependents']
 server=Server(('127.0.0.1',0),td);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);page=browser.new_page(viewport={'width':1600,'height':1100},locale='ru-RU');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto(f'http://127.0.0.1:{server.server_port}/#prices');expect(page.locator('h1')).to_have_text('Прайсы');page.locator('#employee').fill('Руководитель');page.locator('#employee').dispatch_event('change')
   page.get_by_role('checkbox',name='Печатать Розница',exact=True).uncheck()
   with page.expect_popup() as popup:page.locator('a[href*="type=prices"][href*="format=html"]').click()
   printed=popup.value;expect(printed.locator('thead')).to_contain_text('Дистрибьютор');expect(printed.locator('thead')).not_to_contain_text('Розница');expect(printed.locator('thead th')).to_have_count(4);printed.close()
   with page.expect_download() as dl:page.locator('a[href*="type=prices"][href*="format=xlsx"]').click()
   raw=Path(dl.value.path()).read_bytes()
   with zipfile.ZipFile(io.BytesIO(raw)) as z:text=' '.join(ET.fromstring(z.read('xl/worksheets/sheet1.xml')).itertext())
   assert 'Дистрибьютор' in text and 'Розница' not in text
   page.get_by_role('checkbox',name='Печатать Дистрибьютор',exact=True).uncheck();expect(page.locator('#price-export-actions a')).to_have_count(0);expect(page.locator('#price-export-actions')).to_contain_text('Отметьте столбцы')
   page.get_by_role('checkbox',name='Печатать Розница',exact=True).check()
   page.locator('#nav a[href="#documents"]').click();before=store.snapshot();page.locator(f'[data-action="delete-document"][data-id="{receipt}"]').click();expect(page.locator('#modal')).to_contain_text('зависимые документы');expect(page.get_by_role('button',name='Да, удалить все',exact=True)).to_be_visible();expect(page.locator('#modal tbody tr')).to_have_count(len(plan['documents']))
   page.get_by_role('button',name='Нет',exact=True).click();expect(page.locator('#modal')).not_to_be_visible();assert store.snapshot()['documents']==before['documents'] and store.snapshot()['lots']==before['lots']
   # A concurrent change cannot expand the confirmed chain silently.
   page.locator(f'[data-action="delete-document"][data-id="{receipt}"]').click();page.locator('[name=reason]').fill('Ошибочная приёмка');store.money(dict(date=store.snapshot()['now'][:10],responsible='Р',kind='expense',category='Аренда',amount=10));page.get_by_role('button',name='Да, удалить все',exact=True).click();expect(page.locator('#form-error')).to_contain_text('изменились');assert not next(d for d in store.snapshot()['documents'] if d['id']==receipt)['reversed'];page.get_by_role('button',name='Нет',exact=True).click()
   page.locator(f'[data-action="delete-document"][data-id="{receipt}"]').click();page.locator('[name=reason]').fill('Ошибочная приёмка');page.get_by_role('button',name='Да, удалить все',exact=True).click();expect(page.locator('#modal')).not_to_be_visible()
   state=store.snapshot();assert all(next(d for d in state['documents'] if d['id']==r['id'])['data'].get('deleted') for r in plan['documents']);assert all(l['qty']==0 for l in state['lots']);assert any(d['kind']=='expense' and not d['reversed'] for d in state['documents'])
   page.locator('#nav a[href="#prices"]').click();expect(page.get_by_role('checkbox',name='Печатать Розница',exact=True)).to_be_checked();expect(page.get_by_role('checkbox',name='Печатать Дистрибьютор',exact=True)).not_to_be_checked()
   assert not errors,errors;browser.close();print('Browser 0.5.2 passed: selected HTML/XLSX columns, empty selection, retained checks, No preserves data, stale cascade blocked, Yes deletes complete chain atomically with unrelated expenses retained.')
 finally:server.shutdown();server.server_close();thread.join()
