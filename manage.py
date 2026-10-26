"""Local-only administrator bootstrap and recovery; never exposed as an HTTP endpoint."""
import argparse,getpass,sqlite3
from pathlib import Path
from contextlib import closing
from datetime import datetime
from domain import Store
from network import Network,password_hash

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['init-admin','reset-password','backup']);p.add_argument('--data',default='data');p.add_argument('--output');args=p.parse_args()
    path=Path(args.data);store=Store(path/'ceh.sqlite3');target=Path(args.output) if args.output else path/'backups'/('before-access-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3');target.parent.mkdir(exist_ok=True)
    with closing(store.connect()) as src,closing(sqlite3.connect(target)) as dst:src.backup(dst)
    if args.command=='backup':print('Создана согласованная SQLite-копия:',target);return
    network=Network(store);login=input('Логин: ').strip().casefold();name=input('Имя: ').strip() if args.command=='init-admin' else ''
    password=getpass.getpass('Пароль (минимум 12 символов): ')
    if password!=getpass.getpass('Повторите пароль: '):raise SystemExit('Пароли не совпадают')
    if args.command=='init-admin':network.bootstrap(login,name,password)
    else:
        with store.transaction() as c:
            user=c.execute('SELECT id FROM net_users WHERE login=?',(login,)).fetchone()
            if not user:raise SystemExit('Пользователь не найден')
            c.execute('UPDATE net_users SET password=? WHERE id=?',(password_hash(password),user['id']));c.execute('DELETE FROM net_sessions WHERE user_id=?',(user['id'],))
    print('Готово. Пароль не сохраняется в открытом виде. Копия базы:',target)

if __name__=='__main__':main()
