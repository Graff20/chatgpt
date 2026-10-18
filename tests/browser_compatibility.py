"""Browser regression: new static files served by an old running Python process."""
import tempfile
import threading
import sys
import os
import json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Server,APP_VERSION
from playwright.sync_api import sync_playwright,expect

with tempfile.TemporaryDirectory() as td:
    server=Server(('127.0.0.1',0),td)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
            page=browser.new_page();errors=[];posts=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda req:posts.append(req.url) if req.method=='POST' else None)
            old_state=server.store.snapshot();old_state['schema_version']=1;old_state.pop('drivers')
            page.route('**/api/state',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(old_state)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            expect(page.get_by_role('heading',name='Нужно перезапустить сервер')).to_be_visible()
            expect(page.locator('#content')).to_contain_text('Ctrl+C')
            self_error=page.evaluate("api('orders',{}).then(()=>null,e=>e.code)")
            assert self_error=='VERSION_MISMATCH',self_error
            assert not posts,posts
            assert not errors,errors
            # Start using matching server data again, then verify visible actual file path.
            page.unroute('**/api/state')
            page.reload()
            expect(page.locator('h1')).to_have_text('Производство под контролем')
            page.locator('#nav a[href="#settings"]').click()
            expect(page.locator('#content')).to_contain_text(str(server.store.path))
            expect(page.locator('#content')).to_contain_text(APP_VERSION)
            browser.close()
            print('Compatibility check passed: old server detected, no TypeError, writes blocked, actual database path shown after reload.')
    finally:server.shutdown();server.server_close();thread.join()
