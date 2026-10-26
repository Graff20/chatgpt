"""Native Windows client; the shared database always stays on the server."""
import json,os,pathlib,threading,urllib.request,urllib.parse,hashlib,tempfile,subprocess,sys
VERSION='0.6.0'
ROOT=pathlib.Path(os.environ.get('LOCALAPPDATA',pathlib.Path.home()))/'CehClient'

def server_url(value):
    value=value.strip().rstrip('/');u=urllib.parse.urlparse(value)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:raise ValueError('Укажите HTTPS-адрес сервера без пути и параметров')
    return value

def configure():
    import tkinter as tk
    from tkinter import simpledialog,messagebox
    root=tk.Tk();root.withdraw();path=ROOT/'connection.json';old=''
    if path.exists():old=json.loads(path.read_text()).get('server','')
    while True:
        value=simpledialog.askstring('Подключение Цех','HTTPS-адрес общей базы:',initialvalue=old,parent=root)
        if value is None:root.destroy();return None
        try:value=server_url(value)
        except ValueError as e:messagebox.showerror('Подключение',str(e));continue
        ROOT.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps({'server':value}));root.destroy();return value

def newer(a,b):
    return tuple(int(x) for x in a.split('.'))>tuple(int(x) for x in b.split('.'))

def check_update(window,base):
    try:
        with urllib.request.urlopen(base+'/api/client-version',timeout=15) as r:info=json.load(r)['windows']
        if not newer(info.get('latest',VERSION),VERSION):return
        url=info.get('url','');expected=info.get('sha256','').lower()
        if not url.startswith('https://') or len(expected)!=64:return
        mandatory=newer(info.get('minimum',VERSION),VERSION)
        if not window.create_confirmation_dialog('Обновление Цех','Доступна версия '+info['latest']+'. Скачать проверенный установщик?'):
            if mandatory:window.destroy()
            return
        target=pathlib.Path(tempfile.mkdtemp(prefix='ceh-update-'))/'CehClientSetup.exe';digest=hashlib.sha256();size=0
        with urllib.request.urlopen(url,timeout=60) as r,target.open('wb') as out:
            if not r.geturl().startswith('https://'):raise ValueError('Незащищённый адрес обновления')
            while True:
                block=r.read(65536)
                if not block:break
                size+=len(block)
                if size>250*1024*1024:raise ValueError('Размер установщика превышен')
                digest.update(block);out.write(block)
        if digest.hexdigest()!=expected:target.unlink();raise ValueError('Контрольная сумма установщика не совпадает')
        if target.read_bytes()[:2]!=b'MZ':target.unlink();raise ValueError('Неверный формат установщика')
        subprocess.Popen([str(target)]);window.destroy()
    except Exception as e:
        # Update failure never overwrites the installed executable or user settings.
        print('Обновление не применено:',str(e))

def main():
    import webview
    path=ROOT/'connection.json'
    if '--configure' in sys.argv or not path.exists():base=configure()
    else:
        try:base=server_url(json.loads(path.read_text())['server'])
        except Exception:base=configure()
    if not base:return
    window=webview.create_window('Цех · учёт и управление',base+'/mobile',width=1280,height=850,min_size=(800,600))
    checked=threading.Event()
    def loaded():
        if checked.is_set():return
        checked.set();threading.Thread(target=check_update,args=(window,base),daemon=True).start()
    window.events.loaded+=loaded
    webview.start(gui='edgechromium',private_mode=False,storage_path=str(ROOT/'webview'))

if __name__=='__main__':main()
