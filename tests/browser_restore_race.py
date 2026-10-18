import sys,tempfile,threading,sqlite3
from pathlib import Path
from contextlib import closing
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Server
from domain import SCHEMA
from playwright.sync_api import sync_playwright,expect
with tempfile.TemporaryDirectory() as td:
 root=Path(td);backup=root/'original.sqlite3'
 with closing(sqlite3.connect(backup)) as c:
  c.executescript(SCHEMA+';PRAGMA user_version=1;');c.execute("INSERT INTO items(name,kind,unit) VALUES('Сахар','raw','кг')");c.commit()
 server=Server(('127.0.0.1',0),root/'data');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);page=browser.new_page(viewport={'width':1400,'height':1000});page.goto(f'http://127.0.0.1:{server.server_port}/#settings');expect(page.locator('#restore-file')).to_be_visible()
   page.evaluate('''() => { window.oldState=JSON.stringify(S);window.realFetch=window.fetch;window.delayedOnce=false;window.fetch=(...a)=>{if(String(a[0]).endsWith('/api/state')&&!window.delayedOnce){window.delayedOnce=true;return new Promise(resolve=>window.releaseOld=()=>resolve(new Response(window.oldState,{status:200,headers:{'Content-Type':'application/json'}})));}return window.realFetch(...a);};window.oldRequest=refreshWhenIdle(); }''')
   page.locator('#restore-file').set_input_files(backup);page.locator('#restore-confirm').fill('ВОССТАНОВИТЬ');page.get_by_role('button',name='Восстановить базу',exact=True).click();expect(page.locator('#toast')).to_contain_text('База восстановлена')
   print('After restore: UI=',page.evaluate('S.items.length'),'database=',len(server.store.snapshot()['items']))
   page.evaluate('window.releaseOld();window.oldRequest')
   assert page.evaluate('S.items.length')==1
   assert len(server.store.snapshot()['items'])==1
   expect(page.locator('#restore-result')).to_contain_text('Номенклатура: 1')
   print('Restore race passed: delayed pre-restore state cannot hide restored rows.')
   browser.close()
 finally:server.shutdown();server.server_close();thread.join()
