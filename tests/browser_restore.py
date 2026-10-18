"""Regression: native file picker return/focus and periodic refresh must retain restore input."""
import hashlib
import os
import sqlite3
import tempfile
import threading
import sys
from contextlib import closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Server
from domain import SCHEMA
from playwright.sync_api import sync_playwright,expect

with tempfile.TemporaryDirectory() as td:
    root=Path(td)
    backup=root/'before-v2-20261017-214434-448006.sqlite3'
    with closing(sqlite3.connect(backup)) as c:
        c.executescript(SCHEMA+';PRAGMA user_version=1;')
        c.execute("INSERT INTO items(name,kind,unit) VALUES('Сахар из копии','raw','кг')")
        c.execute("INSERT INTO partners(kind,name) VALUES('client','Клиент из копии')")
        c.commit()
    original_hash=hashlib.sha256(backup.read_bytes()).hexdigest()
    bad=root/'invalid.sqlite3';bad.write_bytes(b'not a SQLite database')
    server=Server(('127.0.0.1',0),root/'working-data')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1400,'height':1000});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#settings')
            file=page.locator('#restore-file');confirm=page.locator('#restore-confirm')
            expect(file).to_be_visible()
            # Focus can arrive before the chooser's change event.
            with page.expect_file_chooser() as fc:file.click()
            assert page.evaluate('restorePicking')
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            fc.value.set_files(backup)
            expect(page.locator('#restore-chosen')).to_contain_text(backup.name)
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            page.evaluate('refreshWhenIdle()')  # periodic callback, without a 30-second sleep
            assert file.evaluate('(e)=>e.files[0].name')==backup.name
            confirm.fill('ВОССТАНОВИТЬ')
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            page.evaluate('refreshWhenIdle()')
            expect(confirm).to_have_value('ВОССТАНОВИТЬ')
            assert file.evaluate('(e)=>e.files.length')==1
            # Failed validation leaves the selected file and confirmation intact for correction.
            file.set_input_files(bad)
            page.get_by_role('button',name='Восстановить базу',exact=True).click()
            expect(page.locator('#toast')).to_contain_text('Это не резервная копия SQLite')
            expect(confirm).to_have_value('ВОССТАНОВИТЬ')
            assert file.evaluate('(e)=>e.files[0].name')==bad.name
            # Simulate a request already in flight before a file was selected.
            file.set_input_files([]);confirm.fill('')
            page.evaluate("""() => {
                window.savedFetch=window.fetch;
                window.fetch=(...args)=>String(args[0]).endsWith('/api/state')
                    ? new Promise(resolve=>{window.releaseState=()=>resolve(new Response(JSON.stringify(S),{status:200,headers:{'Content-Type':'application/json'}}));})
                    : window.savedFetch(...args);
                window.backgroundRequest=refreshWhenIdle();
            }""")
            page.wait_for_function("typeof window.releaseState==='function'")
            file.set_input_files(backup);confirm.fill('ВОССТАНОВИТЬ')
            page.evaluate('window.releaseState(); window.backgroundRequest')
            assert file.evaluate('(e)=>e.files[0].name')==backup.name
            expect(confirm).to_have_value('ВОССТАНОВИТЬ')
            page.evaluate('window.fetch=window.savedFetch')
            # Restore the legacy copy through the same controls the user has.
            page.get_by_role('button',name='Восстановить базу',exact=True).click()
            expect(page.locator('#toast')).to_contain_text('База восстановлена')
            page.locator('#nav a[href="#catalog"]').click()
            expect(page.get_by_text('Сахар из копии',exact=True)).to_be_visible()
            assert server.store.snapshot()['partners'][0]['name']=='Клиент из копии'
            assert list((root/'working-data'/'backups').glob('before-restore-*'))
            assert hashlib.sha256(backup.read_bytes()).hexdigest()==original_hash
            # Cancelling the native chooser must not permanently suspend background refresh.
            page.locator('#nav a[href="#settings"]').click()
            with page.expect_file_chooser() as fc:page.locator('#restore-file').click()
            page.locator('#restore-file').dispatch_event('cancel')
            assert not page.evaluate('restorePicking')
            awaitable=page.evaluate('refreshWhenIdle()')
            assert not errors,errors
            browser.close()
            print('Restore browser regression passed: focus before/after selection, periodic/in-flight refresh, confirmation retention, failed restore retention, v1 restoration, unchanged source, picker cancel.')
    finally:
        server.shutdown();server.server_close();thread.join()
