"""Portable document exports: UTF-8 CSV, XLSX, DOCX and printable HTML."""
import csv
import io
import json
import zipfile
from html import escape
from xml.sax.saxutils import escape as xml_escape

KINDS={'receipt':'Приёмка сырья','production':'Выпуск продукции','shipment':'Товарная накладная','writeoff':'Акт списания','inventory':'Ревизионная ведомость','payment':'Оплата клиента','expense':'Расход','reversal':'Отмена документа','return':'Возврат клиента','release':'Допуск партии'}

def document_table(doc):
    data=doc['data']; rows=[]
    if doc['kind'] in ('receipt','shipment','return'):
        rows=[['Наименование','Количество','Ед.','Цена, ₽','Сумма, ₽']]
        for x in data['lines']: rows.append([x['name'],x['qty'],x['unit'],x['price'],round(x['qty']*x['price'],2)])
        rows.append(['Итого','','','',doc['total']])
    elif doc['kind']=='production':
        rows=[['Выпущено',data['name'],data['qty'],data['unit']],['Стоимость выпуска, ₽',round(doc['cost'],2)],['Сырьё / полуфабрикат','Количество','Ед.','Стоимость, ₽']]
        for x in data['inputs']: rows.append([x['name'],x['qty'],x['unit'],round(sum(l['qty']*l['unit_cost'] for l in x['lots']),2)])
    elif doc['kind']=='inventory':
        rows=[['Партия','Наименование','Ед.','Учёт','Факт','Отклонение','Причина']]
        rows += [[x['lot_id'],x['name'],x['unit'],x['expected'],x['actual'],x['difference'],x['reason']] for x in data['lines']]
    elif doc['kind']=='writeoff':
        rows=[['Наименование','Количество','Ед.','Причина','Стоимость, ₽'],[data['name'],data['qty'],data['unit'],data['reason'],round(doc['cost'],2)]]
    elif doc['kind'] in ('reversal','release'):
        rows=[['Документ / партия',data.get('target_id',data.get('lot_id'))],['Причина',data['reason']]]
    else:
        rows=[['Операция','Сумма, ₽'],[data.get('client_name',data.get('category','')),doc['total']]]
    meta=([['Статус','ОТМЕНЁН']] if doc.get('reversed') else [])+[['Дата',doc['date']],['Ответственный',doc['responsible']]]
    for key in ('supplier_name','client_name','due','order_id','override_reason'):
        if data.get(key): meta.append([{'supplier_name':'Поставщик','client_name':'Клиент','due':'Оплатить до','order_id':'Заказ','override_reason':'Согласование лимита'}[key],data[key]])
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
        html='<!doctype html><html lang="ru"><meta charset="utf-8"><title>'+escape(title)+'</title><style>body{font:14px Arial;margin:32px;color:#162c28}table{border-collapse:collapse;width:100%}td{border:1px solid #ccd6d3;padding:9px}h1{font-size:24px}button{padding:10px;margin-bottom:20px}@media print{button{display:none}@page{size:landscape}}</style><button onclick="window.print()">Печать / сохранить PDF</button><h1>'+escape(title)+'</h1><table>'+''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in r)+'</tr>' for r in rows[2:])+'</table></html>'
        return html.encode(),'text/html; charset=utf-8'
    raise ValueError('Неподдерживаемый формат')
