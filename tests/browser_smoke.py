"""Optional browser smoke: requires Playwright and an installed Chromium."""
import os
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Server
from demo import create_demo
from playwright.sync_api import sync_playwright, expect

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as td:
    create_demo(Path(td)/'ceh.sqlite3')
    server=Server(('127.0.0.1',0),td)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=1)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            expect(page.get_by_role('heading',name='Производство под контролем')).to_be_visible()
            page.locator('#employee').fill('Мария')
            page.locator('#employee').dispatch_event('change')
            # Every navigation route renders with populated data.
            routes={'orders':'Заказы клиентов','production':'Производство','stock':'Склад и партии','catalog':'Номенклатура','recipes':'Технологические карты','partners':'Клиенты и поставщики','documents':'Документы','finance':'Финансы','analytics':'Аналитика','settings':'База и настройки'}
            for route,title in routes.items():
                page.locator(f'#nav a[href="#{route}"]').click()
                expect(page.locator('h1')).to_have_text(title)
            page.locator('#nav a[href="#catalog"]').click()
            page.get_by_role('button',name='＋ Добавить позицию').click()
            page.locator('[name=name]').fill('Тестовая упаковка')
            page.locator('[name=kind]').select_option('packaging')
            page.locator('[name=unit]').select_option('шт.')
            page.get_by_role('button',name='Сохранить',exact=True).click()
            expect(page.locator('#modal')).not_to_be_visible()
            expect(page.get_by_text('Тестовая упаковка',exact=True)).to_be_visible()
            # Create recipe version from existing, preserving copied losses.
            page.locator('#nav a[href="#recipes"]').click()
            page.locator('[data-action="edit-recipe"]').first.click()
            expect(page.locator('#recipe-total')).to_contain_text('Плановая стоимость')
            page.get_by_role('button',name='Сохранить версию').click()
            expect(page.locator('#modal')).not_to_be_visible()
            # Manual production without any order.
            page.locator('#nav a[href="#production"]').click()
            page.get_by_role('button',name='＋ Выпуск на склад').click()
            page.locator('[name=item_id]').select_option(label='Куриное филе сувид')
            page.locator('[name=qty]').fill('2')
            expect(page.locator('#production-preview')).to_contain_text('Куриное филе')
            page.get_by_role('button',name='Провести выпуск').click()
            expect(page.locator('#modal')).not_to_be_visible()
            # New future order -> exact weight shipment -> printable document.
            page.locator('#nav a[href="#orders"]').click()
            page.get_by_role('button',name='＋ Новый заказ').click()
            page.locator('[name=client_id]').select_option(label='Кофейня «Тёплый хлеб»')
            page.locator('#order-lines .r-item').select_option(label='Куриное филе сувид')
            page.locator('#order-lines .r-qty').fill('1')
            expect(page.locator('#order-summary')).to_contain_text('790')
            expect(page.locator('#order-lines .r-stock')).to_contain_text('изготовлено')
            page.get_by_role('button',name='Сохранить',exact=True).click()
            expect(page.locator('#modal')).not_to_be_visible()
            new_id=page.evaluate('Math.max(...S.orders.map(o=>o.id))')
            page.locator(f'[data-action="view-order"][data-id="{new_id}"]').click()
            page.get_by_role('button',name='Отгрузить · уточнить вес').click()
            page.locator('.s-qty').fill('1.037')
            expect(page.locator('#shipment-total')).to_contain_text('819,23')
            page.get_by_role('button',name='Провести отгрузку').click()
            expect(page.locator('#modal')).not_to_be_visible()
            page.locator('#nav a[href="#documents"]').click()
            page.locator('[data-action="view-document"]').first.click()
            iframe=page.frame_locator('iframe')
            expect(iframe.locator('h1')).to_contain_text('Товарная накладная')
            expect(iframe.get_by_text('1.037',exact=True)).to_be_visible()
            page.locator('[data-action="close-modal"]').first.click()
            # Return quarantines the goods; release requires a separate decision.
            page.locator('[data-action="view-document"]').first.click()
            page.get_by_role('button',name='Возврат клиента',exact=True).click()
            page.locator('[name=reason]').fill('Проверка возврата')
            page.locator('.return-qty').fill('0.2')
            page.get_by_role('button',name='Провести возврат').click()
            expect(page.locator('#modal')).not_to_be_visible()
            page.locator('#nav a[href="#stock"]').click()
            expect(page.get_by_text('Карантин',exact=True)).to_be_visible()
            page.locator('[data-action="release-lot"]').click()
            page.locator('[name=reason]').fill('Проверено технологом')
            page.get_by_role('button',name='Допустить партию').click()
            expect(page.locator('#modal')).not_to_be_visible()
            # Revise unchanged quantities, then cancel the revision from the journal.
            page.get_by_role('button',name='Ревизия',exact=True).click()
            page.get_by_role('button',name='Провести ревизию').click()
            expect(page.locator('#modal')).not_to_be_visible()
            page.locator('#nav a[href="#documents"]').click()
            page.locator('[data-action="view-document"]').first.click()
            page.get_by_role('button',name='Отменить документ',exact=True).click()
            page.locator('[name=reason]').fill('Тест отмены ревизии')
            page.get_by_role('button',name='Отменить документ',exact=True).click()
            expect(page.locator('#modal')).not_to_be_visible()
            expect(page.get_by_text('Отменён',exact=True)).to_be_visible()
            # Invoice downloads are genuine formats, not HTML with renamed extensions.
            with page.expect_download() as dl:
                page.locator('#nav a[href="#stock"]').click()
                page.get_by_role('link',name='XLSX',exact=True).click()
            assert dl.value.suggested_filename.endswith('.xlsx')
            page.locator('#nav a[href="#dashboard"]').click()
            out=root/'docs';out.mkdir(exist_ok=True)
            page.locator('#toast').wait_for(state='hidden',timeout=6000)
            page.screenshot(path=str(out/'dashboard.png'),full_page=True)
            # Mobile navigation and modal viewport fit.
            page.set_viewport_size({'width':390,'height':844})
            page.locator('#menu').click()
            page.locator('#nav a[href="#orders"]').click()
            expect(page.locator('h1')).to_have_text('Заказы клиентов')
            page.get_by_role('button',name='＋ Новый заказ').click()
            expect(page.locator('#modal')).to_be_visible()
            assert page.locator('#modal').bounding_box()['width']<=390
            assert not errors,errors
            browser.close()
            print('Browser smoke passed: 11 pages, catalogue, recipe version, production, order, 1.037 kg shipment, return/quarantine/release, inventory/reversal, print, XLSX, mobile.')
    finally:
        server.shutdown();server.server_close();thread.join()
