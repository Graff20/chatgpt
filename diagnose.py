"""Read-only discovery of local Ceh databases. Does not import Store or run migrations."""
import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from contextlib import closing
from datetime import datetime
from pathlib import Path

TABLES=('items','partners','recipes','lots','orders','documents')
SKIP={'.git','.venv','.venv-build','node_modules','__pycache__','Windows','$RECYCLE.BIN','System Volume Information'}

def inspect_database(path):
    path=Path(path).resolve()
    result={'path':str(path),'size':path.stat().st_size,'modified':datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds')}
    try:
        # mode=ro includes committed WAL data; immutable=1 would incorrectly ignore that data.
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as c:
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(TABLES)<=tables:
                result['error']='Не распознана как база Цех';return result
            result['schema_version']=c.execute('PRAGMA user_version').fetchone()[0]
            result['counts']={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in TABLES}
    except (sqlite3.Error,OSError) as e:
        result['error']=str(e)
    return result

def discover(root,max_depth=5,max_entries=10000,max_databases=200):
    root=Path(root).resolve()
    stack=[(root,0)];paths=[];seen=0;warnings=[]
    while stack:
        folder,depth=stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    seen+=1
                    if seen>max_entries:
                        warnings.append('Достигнут лимит поиска файлов. Выберите более узкую папку через --search-root.')
                        return paths,warnings
                    if entry.is_symlink():continue
                    if entry.is_dir(follow_symlinks=False) and depth<max_depth and entry.name not in SKIP:
                        stack.append((Path(entry.path),depth+1))
                    elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in ('.sqlite3','.sqlite','.db'):
                        paths.append(Path(entry.path))
                        if len(paths)>=max_databases:
                            warnings.append('Найдено 200 файлов баз. Поиск ограничен; выберите более узкую папку.')
                            return paths,warnings
        except OSError as e:
            warnings.append(f'Не удалось прочитать папку {folder}: {e}')
    return sorted(paths),warnings

def running_server(port=8765):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{int(port)}/api/state',timeout=4) as r:
            header=r.headers.get('Server','');state=json.load(r)
        return {'server':header,'schema_version':state.get('schema_version'),
                'runtime':state.get('runtime'),
                'counts':{t:len(state.get(t,[])) for t in TABLES},
                'compatible':state.get('schema_version')==5 and isinstance(state.get('drivers'),list) and (state.get('runtime') or {}).get('app_version')=='0.5.3'}
    except (OSError,ValueError,TypeError) as e:
        return {'error':str(e)}

def make_report(root,program_dir,server=None):
    paths,warnings=discover(root)
    lines=['ЦЕХ — ДИАГНОСТИКА БАЗ (БЕЗ ИЗМЕНЕНИЯ ДАННЫХ)',
           f'Папка этой копии программы: {program_dir}',
           f'База по умолчанию этой копии: {Path(program_dir)/"data"/"ceh.sqlite3"}',
           f'Искали в: {Path(root).resolve()}',
           'Время: '+datetime.now().isoformat(timespec='seconds'),'']
    if server:
        lines.append('ЗАПУЩЕННЫЙ СЕРВЕР')
        if server.get('error'):lines.append('Не удалось прочитать сервер: '+server['error'])
        else:
            lines.append(f"Версия ответа: {server['server']}; схема {server['schema_version']}")
            if not server['compatible']:
                lines.append('ВНИМАНИЕ: запущен старый или несовместимый сервер. Остановите старое окно и запустите актуальный start.bat.')
            runtime=server.get('runtime') or {}
            lines.append('Используемая база: '+runtime.get('database_path','Старый сервер не сообщает путь. Посмотрите строку «База:» в его окне запуска.'))
            if runtime.get('pid'):lines.append(f"Процесс {runtime['pid']}; программа {runtime['program_directory']}")
            lines.append('Записи в ответе сервера: '+', '.join(f'{k}={v}' for k,v in server['counts'].items()))
        lines.append('')
    lines.append('НАЙДЕННЫЕ ФАЙЛЫ')
    for path in paths:
        try: info=inspect_database(path)
        except OSError as e:
            lines.append(str(path)+': '+str(e));continue
        lines.extend([info['path'],f"Размер {info['size']} байт; изменён {info['modified']}"])
        if info.get('error'):lines.append('Ошибка: '+info['error'])
        else:
            counts=info['counts']
            lines.append(('ЕСТЬ ЗАПИСИ' if sum(counts.values()) else 'ПУСТАЯ БАЗА')+f" · схема {info['schema_version']}")
            lines.append('Номенклатура: {items}; контрагенты: {partners}; ТТК: {recipes}; партии: {lots}; заказы: {orders}; документы: {documents}'.format(**counts))
        lines.append('')
    if not paths:lines.append('В выбранной папке базы не найдены. Проверьте прежнюю папку программы, резервные копии и Корзину Windows.')
    lines.extend(warnings)
    lines.extend(['','Скрипт не изменял базы и не выполнял миграции. Данные карточек/документов в отчёт не включены.',
                  'Не выбирайте копию только по дате или размеру: смотрите наличие ожидаемых записей.',
                  'Не удаляйте и не заменяйте файлы автоматически. Перед восстановлением остановите сервер и сохраните все текущие папки data.',
                  'Старая рабочая база с WAL переносится вместе с папкой data при остановленном сервере; для восстановления через интерфейс используйте согласованную резервную копию.'])
    return '\n'.join(lines)

def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    program=Path(__file__).resolve().parent
    p=argparse.ArgumentParser(description='Поиск баз Цех без изменения данных')
    p.add_argument('--search-root',type=Path,default=program.parent)
    p.add_argument('--port',type=int,default=8765)
    p.add_argument('--output',type=Path,default=program/'diagnostics.txt')
    args=p.parse_args()
    report=make_report(args.search_root,program,running_server(args.port))
    print(report)
    try:
        args.output.write_text(report,encoding='utf-8-sig')
        print('\nОтчёт сохранён: '+str(args.output.resolve()))
    except OSError as e:print('\nНе удалось сохранить отчёт: '+str(e))

if __name__=='__main__':main()
