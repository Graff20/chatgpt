"""Portable document exports: UTF-8 CSV, XLSX, DOCX and printable HTML."""
import csv
import io
import json
import zipfile
from html import escape
from xml.sax.saxutils import escape as xml_escape

KINDS={'receipt':'Приёмка сырья','production':'Выпуск продукции','shipment':'Счёт','writeoff':'Акт списания','inventory':'Ревизионная ведомость','payment':'Оплата клиента','expense':'Расход','reversal':'Отмена документа','return':'Возврат клиента','release':'Допуск партии','exchange':'Физобмен','refund':'Возврат денег клиенту','dispatch_summary':'Сводная ведомость к отгрузке'}

def document_table(doc):
    data=doc['data']; rows=[]
    if doc['kind'] in ('receipt','shipment','return'):
        if doc['kind']=='shipment':
            rows=[['№','Наименование','Ед. измерения','Количество','Цена, ₽','Сумма, ₽']]
            for n,x in enumerate(data['lines'],1): rows.append([n,x['name'],x['unit'],x['qty'],x.get('gross_price',x['price']),x.get('gross_total',x.get('line_total',round(x['qty']*x['price'],2)))])
        else:
            rows=[['Наименование','Количество','Ед.','Цена, ₽','Сумма, ₽']]
            for x in data['lines']: rows.append([x['name'],x['qty'],x['unit'],x['price'],x.get('line_total',round(x['qty']*x['price'],2))])
        blanks=['']*(len(rows[0])-2)
        if data.get('discount_percent'):
            rows.append(['Сумма']+blanks+[data['subtotal']])
            rows.append([f"Скидка {data['discount_percent']}%"]+blanks+[data['discount_amount']])
        rows.append(['Итого']+blanks+[doc['total']])
        if doc['kind']=='shipment': rows.append(['']*(len(rows[0])-1)+['Без НДС'])
    elif doc['kind']=='production':
        rows=[['Выпущено',data['name'],data['qty'],data['unit']],['Стоимость выпуска, ₽',round(doc['cost'],2)],['Сырьё / полуфабрикат','Количество','Ед.','Стоимость, ₽']]
        for x in data['inputs']: rows.append([x['name'],x['qty'],x['unit'],round(sum(l['qty']*l['unit_cost'] for l in x['lots']),2)])
    elif doc['kind']=='exchange':
        rows=[['Исходная отгрузка',data['shipment_id']],['Причина',data['reason']],['Возвращено','Ед.','Количество']]+[[x['name'],x['unit'],x['qty']] for x in data['lines']]
        rows += [[],['Выдана замена','Ед.','Количество']]+[[x['name'],x['unit'],x['qty']] for x in data['replacements']]
        rows += [['Изменение долга, ₽',0],['Стоимость выданной замены, ₽',round(data['replacement_cost'],2)]]
    elif doc['kind']=='expense':
        rows=[['Статья расхода',data['category']],['На что потрачено',data.get('purpose') or doc['note'] or 'Назначение не указано'],['Кому оплачено',data.get('recipient') or data.get('employee_name') or '—'],['Сумма, ₽',doc['total']]]
        if data.get('already_in_cost'):rows.append(['Учёт в результате','Выплата начисленной зарплаты; в себестоимости уже учтена'])
        if data.get('allocations'):
            rows += [[],['Распределено на счёт №','Сумма, ₽']]+[[r['document_id'],r['amount']] for r in data['allocations']]
    elif doc['kind']=='dispatch_summary':
        rows=[['Количество','Неотгруженный остаток' if data['mode']=='remaining' else 'Полное количество заказа'],['Заказы',', '.join('№ '+str(o['id']) for o in data['orders'])],[],['№','Наименование','Ед.','План к отгрузке']]
        rows += [[n,l['name'],l['unit'],l['qty']] for n,l in enumerate(data['lines'],1)]
        rows += [[],['Расшифровка по заказам'],['Заказ','Покупатель','Дата отгрузки','Товар','Количество','Ед.']]
        rows += [[o['id'],o['client_name'],o['due'],l['name'],l['qty'],l['unit']] for o in data['orders'] for l in o['lines']]
        rows += [[],['Подготовил','________________ / ________________'],['Скомплектовал','________________ / ________________']]
    elif doc['kind']=='inventory':
        rows=[['Партия','Наименование','Ед.','Учёт','Факт','Отклонение','Причина']]
        rows += [[x['lot_id'],x['name'],x['unit'],x['expected'],x['actual'],x['difference'],x['reason']] for x in data['lines']]
    elif doc['kind']=='writeoff':
        rows=[['Наименование','Количество','Ед.','Причина','Стоимость, ₽'],[data['name'],data['qty'],data['unit'],data['reason'],round(doc['cost'],2)]]
    elif doc['kind'] in ('reversal','release'):
        rows=[['Документ / партия',data.get('target_id',data.get('lot_id'))],['Причина',data['reason']]]
    else:
        rows=[['Операция','Сумма, ₽'],[data.get('client_name',data.get('category','')),doc['total']]]
    if doc['kind']=='dispatch_summary' and any(l.get('portions') for l in data['lines']):
        rows += [[],['Заказанные порции (вес выше ориентировочный)'],['Наименование','Порций']]+[[l['name'],l['portions']] for l in data['lines'] if l.get('portions')]
    if doc['kind']=='receipt': rows=[['Получатель',data.get('company_name','Моё производство')],['Документ поставщика',data.get('supplier_document','')],['Склад',data.get('warehouse','')]]+rows
    if doc['kind']=='production': rows += [[],['Исполнитель','Выработка','Ед.','Расценка, ₽','Начислено, ₽']]+[[w['name'],w['qty'],w['unit'],w['rate'],w['amount']] for w in data.get('workers',[])]
    if doc['kind']=='return': rows=[['Исходная отгрузка',data['shipment_id']],['Причина возврата',data['reason']]]+rows
    if doc['kind']=='refund': rows=[['Основание',data['reason']]]+rows
    meta=([['Удалён',data['deleted']['at']]] if data.get('deleted') else [])+([['Статус','ОТМЕНЁН']] if doc.get('reversed') else [])+[['Дата',doc['date']],['Ответственный',doc['responsible']]]
    for key in ('sender_name','sender_address','sender_phone','client_name','delivery_address','delivery_phone','delivery_contact','driver_name','vehicle','supplier_name','due','order_id','override_reason'):
        if data.get(key) or (doc['kind']=='shipment' and key in ('sender_name','client_name','delivery_address','delivery_phone','delivery_contact')): meta.append([{'sender_name':'Кто отгружает','sender_address':'Адрес отправителя','sender_phone':'Телефон отправителя','delivery_address':'Адрес доставки','delivery_phone':'Телефон покупателя','delivery_contact':'Контактное лицо покупателя','driver_name':'Водитель','vehicle':'Автомобиль','supplier_name':'Поставщик','client_name':'Клиент','due':'Оплатить до','order_id':'Заказ','override_reason':'Согласование лимита'}[key],data.get(key) or '—'])
    if doc['note']: meta.append(['Примечание',doc['note']])
    return f'{KINDS[doc["kind"]]} № {doc["id"]}',meta+[[]]+rows

def spreadsheet(rows):
    def col(n):
        s=''
        while n: n,r=divmod(n-1,26); s=chr(65+r)+s
        return s
    parts=[]
    for i,row in enumerate(rows,1):
        cells=[]
        for j,v in enumerate(row,1):
            ref=f'{col(j)}{i}'
            if isinstance(v,(int,float)) and not isinstance(v,bool): cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else: cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{xml_escape(str(v))}</t></is></c>')
        parts.append(f'<row r="{i}">{"".join(cells)}</row>')
    files={
        '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml':'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Документ" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml':'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"/></sheetViews><cols><col min="1" max="20" width="26" customWidth="1"/></cols><sheetData>'+''.join(parts)+'</sheetData></worksheet>'}
    return archive(files)

def archive(files):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
        for k,v in files.items(): z.writestr(k,'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'+v)
    return b.getvalue()

def word(title,rows):
    ns='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    def paragraph(t): return '<w:p><w:r><w:t xml:space="preserve">'+xml_escape(str(t))+'</w:t></w:r></w:p>'
    table=''.join('<w:tr>'+''.join('<w:tc>'+paragraph(v)+'</w:tc>' for v in row)+'</w:tr>' for row in rows)
    return archive({
        '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        '_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        'word/document.xml':f'<w:document xmlns:w="{ns}"><w:body>'+paragraph(title)+'<w:tbl><w:tblPr><w:tblBorders>'+''.join(f'<w:{x} w:val="single" w:sz="4" w:color="CCCCCC"/>' for x in ('top','left','bottom','right','insideH','insideV'))+'</w:tblBorders></w:tblPr>'+table+'</w:tbl><w:sectPr><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/></w:sectPr></w:body></w:document>'})

def export(title,rows,fmt):
    rows=[[title],[]]+rows
    if fmt=='csv':
        b=io.StringIO(newline=''); w=csv.writer(b,delimiter=';')
        # Prevent spreadsheet formula execution for untrusted catalogue names.
        w.writerows([["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in r] for r in rows])
        return b.getvalue().encode('utf-8-sig'),'text/csv; charset=utf-8'
    if fmt=='xlsx': return spreadsheet(rows),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    if fmt=='docx': return word(title,rows[2:]),'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    if fmt=='json': return json.dumps({'title':title,'rows':rows[2:]},ensure_ascii=False,indent=2).encode(),'application/json; charset=utf-8'
    if fmt=='html':
        return printable_html(title,rows[2:]),'text/html; charset=utf-8'
    raise ValueError('Неподдерживаемый формат')


def display(value):
    if isinstance(value,float):
        return f'{value:,.6f}'.rstrip('0').rstrip('.').replace(',',' ').replace('.',',')
    if isinstance(value,str) and len(value) in (10,16,19,26) and value[4:5]=='-' and value[7:8]=='-':
        try:
            from datetime import datetime
            return datetime.fromisoformat(value).strftime('%d.%m.%Y' + (' %H:%M' if len(value)>10 else ''))
        except ValueError: pass
    return str(value if value is not None else '')

PRINT_STYLE = """body{font:13px Arial,sans-serif;color:#20372f;margin:28px}h1{font-size:23px}h2{font-size:17px;margin-top:26px}p{line-height:1.65}table{border-collapse:collapse;width:100%;margin:12px 0}td,th{border:1px solid #ccd6d3;padding:8px;text-align:left;overflow-wrap:anywhere}th{background:#f3f6ef}tr{break-inside:avoid}.totals{width:310px;margin-left:auto}.totals td{text-align:right;border:0}.sign{display:flex;justify-content:space-between;margin-top:40px}.cancelled{color:#a13d32}button{padding:10px;margin-bottom:16px}@media print{button{display:none}body{margin:0}@page{size:A4 landscape;margin:12mm}thead{display:table-header-group}}"""

def printable_html(title,rows):
    body='<table>'+''.join('<tr>'+''.join('<td>'+escape(display(v))+'</td>' for v in r)+'</tr>' for r in rows)+'</table>'
    return print_page(title,body)

def print_page(title,body,portrait=False):
    style=PRINT_STYLE+('@media print{@page{size:A4 portrait}}' if portrait else '')
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><title>'+escape(title)+'</title><style>'+style+'</style><button onclick="window.print()">Печать / сохранить PDF</button><h1>'+escape(title)+'</h1>'+body+'</html>').encode()

def invoice_html(doc):
    """Print actual saved shipment; never recompute from mutable cards."""
    d=doc['data'];e=lambda v:escape(str(v or '—'))
    cash=lambda x:f'{x:,.2f}'.replace(',',' ').replace('.',',')
    body=('<p class="cancelled">УДАЛЁН</p>' if d.get('deleted') else '<p class="cancelled">ОТМЕНЁН</p>' if doc.get('reversed') else '')
    body+=f'<p><strong>Поставщик (Кто отгружает):</strong> {e(d.get("sender_name",doc["responsible"]))}<br>Адрес: {e(d.get("sender_address"))}<br>Телефон поставщика: {e(d.get("sender_phone"))}<br><strong>Покупатель:</strong> {e(d["client_name"])}<br><strong>Дата отгрузки:</strong> {e(display(doc["date"]))}<br><strong>Адрес доставки:</strong> {e(d.get("delivery_address"))}<br><strong>Контактное лицо:</strong> {e(d.get("delivery_contact"))}<br><strong>Телефон:</strong> {e(d.get("delivery_phone"))}</p>'
    body+='<table class="invoice-lines"><thead><tr><th>№</th><th>Наименование</th><th>Ед. измерения</th><th>Количество</th><th>Цена, ₽</th><th>Сумма, ₽</th></tr></thead><tbody>'
    for n,l in enumerate(d['lines'],1):
        portion_note=('<br><small>'+str(l['portions'])+' порц.</small>') if l.get('portions') else ''
        body+=f'<tr><td>{n}</td><td>{e(l["name"])}{portion_note}</td><td>{e(l["unit"])}</td><td>{e(display(l["qty"]))}</td><td>{cash(l.get("gross_price",l["price"]))}</td><td>{cash(l.get("gross_total",l.get("line_total",l["qty"]*l["price"])))}</td></tr>'
    body+='</tbody></table><table class="totals"><tr><td>Сумма</td><td>'+cash(d.get('subtotal',doc['total']))+' ₽</td></tr>'
    if d.get('discount_percent'):body+='<tr><td>Скидка '+e(display(d['discount_percent']))+'%</td><td>'+cash(d['discount_amount'])+' ₽</td></tr>'
    body+='<tr><td><strong>ИТОГО</strong></td><td><strong>'+cash(doc['total'])+' ₽</strong></td></tr><tr><td></td><td>Без НДС</td></tr></table>'
    body+=f'<p>Водитель: {e(d.get("driver_name"))} · Автомобиль: {e(d.get("vehicle"))}</p>'
    if doc.get('note'):body+='<p>'+e(doc['note'])+'</p>'
    return print_page(f'Счёт № {doc["id"]:06} от {display(doc["date"])}',body,True)

def receipt_html(doc):
    d=doc['data'];e=lambda v:escape(display(v))
    body=('<p class="cancelled">УДАЛЁН</p>' if d.get('deleted') else '<p class="cancelled">ОТМЕНЁН</p>' if doc.get('reversed') else '')
    body+=f'<p><strong>Получатель:</strong> {e(d.get("company_name","Моё производство"))}<br><strong>Поставщик:</strong> {e(d["supplier_name"])}<br>Документ поставщика: {e(d.get("supplier_document") or "—")}<br>Склад: {e(d.get("warehouse") or "Основной склад")}<br>Ответственный за приёмку: {e(doc["responsible"])}</p>'
    body+='<table><thead><tr><th>№</th><th>Наименование</th><th>Ед.</th><th>Количество</th><th>Цена, ₽</th><th>Сумма, ₽</th></tr></thead><tbody>'
    for n,l in enumerate(d['lines'],1):body+='<tr>'+''.join('<td>'+e(v)+'</td>' for v in [n,l['name'],l['unit'],l['qty'],round(l['price'],2),round(l['qty']*l['price'],2)])+'</tr>'
    body+=f'</tbody></table><p style="text-align:right"><strong>Итого: {doc["total"]:,.2f} ₽</strong><br>Без НДС.</p><h2>Сведения о партиях</h2><table><tr><th>Наименование</th><th>Номер партии</th><th>Дата изготовления</th><th>Годен до</th></tr>'
    for l in d['lines']:body+='<tr>'+''.join('<td>'+e(v)+'</td>' for v in [l['name'],l.get('code','—'),l.get('produced','—'),l.get('expires') or 'Не учитывается'])+'</tr>'
    body+='</table><p>Примечание: '+e(doc.get('note') or '—')+'</p><div class="sign"><span>Сдал: __________ / '+e(d.get('handed_by') or '____________')+'</span><span>Принял: __________ / '+e(doc['responsible'])+'</span></div>'
    return print_page(f'Приёмка сырья № {doc["id"]:06} от {display(doc["date"])}',body)


def production_html(doc):
    d=doc['data'];e=lambda v:escape(display(v))
    body=('<p class="cancelled">УДАЛЁН</p>' if d.get('deleted') else '<p class="cancelled">ОТМЕНЁН</p>' if doc.get('reversed') else '')
    body+=f'<p>Склад: {e(d.get("warehouse") or "Основной склад")}<br>Основание: {e(d.get("basis") or "На свободный склад")}<br>Ответственный: {e(doc["responsible"])}</p>'
    body+='<h2>Готовая продукция</h2><table><tr><th>Наименование</th><th>Ед.</th><th>План</th><th>Фактический выпуск</th></tr><tr>'+''.join('<td>'+e(v)+'</td>' for v in [d['name'],d['unit'],d.get('planned_qty',d['qty']),d['qty']])+'</tr></table>'
    recipe=d['recipe']
    body+=f'<p>ТТК № {recipe["id"]:06}, версия {recipe["version"]}<br>Номер партии: {e(d.get("code") or d["lot_id"])}<br>Изготовлено: {e(d.get("produced") or "—")}'
    if d.get('expires'):body+='<br>Годен до: '+e(d['expires'])
    body+='</p><h2>Списание сырья и упаковки</h2><table><tr><th>Наименование</th><th>Партия</th><th>Ед.</th><th>По ТТК на фактический выпуск</th><th>Списано</th></tr>'
    for l in d['inputs']:
        body+='<tr>'+''.join('<td>'+e(v)+'</td>' for v in [l['name'],', '.join(x.get('code',str(x['lot_id'])) for x in l['lots']),l['unit'],l['qty'],l['qty']])+'</tr>'
    body+='</table><h2>Исполнители и выработка</h2><table><tr><th>Сотрудник</th><th>Выработка</th><th>Ед.</th></tr>'
    for w in d.get('workers',[]):body+='<tr>'+''.join('<td>'+e(v)+'</td>' for v in [w['name'],w['qty'],w['unit']])+'</tr>'
    body+='</table><p>Примечание: '+e(doc.get('note') or '—')+'</p><div class="sign"><span>Изготовил: __________ / __________</span><span>Принял на склад: __________ / __________</span></div>'
    return print_page(f'Выпуск продукции № {doc["id"]:06} от {display(doc["date"])}',body)
