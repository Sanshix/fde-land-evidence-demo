"""Regression checks for matching, template activation and reminder range semantics."""
import io
import zipfile
from datetime import date

import pytest
from test_api import HEAD, client as client_fixture
from app import business, operations, outputs, store, templates

client = client_fixture
ADMIN = {**HEAD, 'X-FDE-Role': 'admin'}


def test_identity_matching_refines_multi_parcel_and_blocks_conflicts():
    rows = [
        {'权利性质': '租赁', '证件号码': 'ID-A', '权利人': '甲', '不动产单元号': '1-1-1', '坐落': '甲路1号'},
        {'权利性质': '租赁', '证件号码': 'ID-A', '权利人': '甲', '不动产单元号': '1-1-2', '坐落': '甲路2号'},
        {'权利性质': '租赁', '证件号码': 'ID-B', '权利人': '乙', '不动产单元号': '1-1-3', '坐落': '乙路1号'},
    ]
    source = {'registry': rows}
    assert business.registry_for({'identity': 'id-a', 'parcel_no': '1-1-2'}, source) == (rows[1], '证件号码')
    with pytest.raises(ValueError, match='多个登记候选'):
        business.registry_for({'identity': 'ID-A'}, source)
    with pytest.raises(ValueError, match='冲突'):
        business.registry_for({'identity': 'ID-A', 'parcel_no': '1-1-3'}, source)
    assert business.registry_for({'parcel_no': '1-1-3'}, source) == (rows[2], '宗地号')
    assert business.registry_for({'tenant': '乙'}, source) == (rows[2], '承租人')
    assert business.registry_for({'residence': '乙路1号'}, source) == (rows[2], '住所／坐落')


def test_reminder_boundaries_and_range_export(client):
    def pub(i, end):
        return {'id': i, 'parcel_id': str(i), 'payload': {'arrears': '0', 'annual': [], 'ledger': {'合同终止日': end}}}
    data = [pub(1,'2026-10-28'),pub(2,'2026-10-29'),pub(3,'2026-11-28'),pub(4,'2026-11-29'),pub(5,'2027-01-27'),pub(6,'2027-01-28')]
    items = operations.reminders(data, date(2026,10,29))
    assert [(r['publication'],r['level']) for r in items] == [(1,'红'),(2,'橙'),(3,'橙'),(4,'黄'),(5,'黄')]
    with store.connection() as db:
        for row in data:
            db.execute('INSERT INTO parcels(id,business_key) VALUES(?,?)',(row['parcel_id'],row['parcel_id']))
            row['payload']['documents']=['synthetic']
            store.publish(db,row['parcel_id'],row['payload'],'test')
    query = '?today=2026-10-29&start=2026-11-28&end=2026-11-29'
    rows=client.get('/api/reminders'+query).json()
    assert [r['date'] for r in rows] == ['2026-11-28','2026-11-29']
    ics=client.get('/api/reminders'+query+'&format=ics').text
    assert '20261128' in ics and '20261129' in ics and '20261028' not in ics
    assert client.get('/api/reminders?start=2026-12-01&end=2026-01-01').status_code == 400


def test_template_upload_used_by_generation_restore_and_permissions(client):
    raw=(templates.BUILTINS/templates.CATALOG['contract'][0]).read_bytes()
    buf=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(buf,'w') as dst:
        for entry in src.infolist():
            content=src.read(entry.filename)
            if entry.filename=='word/document.xml':
                content=content.replace('国有建设用地使用权租赁合同'.encode(),'自定义合同标题'.encode())
            dst.writestr(entry,content)
    uploaded=buf.getvalue()
    route='/api/templates/contract'
    assert client.post(route,files={'file':('custom.docx',uploaded)},headers=HEAD).status_code==403
    assert client.post(route,files={'file':('bad.docx',b'not a docx')},headers=ADMIN).status_code==400
    response=client.post(route,files={'file':('custom.docx',uploaded)},headers=ADMIN)
    assert response.status_code==200,response.text
    assert response.json()['source']=='已导入模板'
    assert client.get(route+'/document').content==uploaded
    generated=outputs.contract_docx({'承租人':'测试姓名'},'新签')
    with zipfile.ZipFile(io.BytesIO(generated)) as z:
        xml=z.read('word/document.xml').decode()
        assert '自定义合同标题' in xml and '测试姓名' in xml and '【承租人】' not in xml
    assert client.post(route+'/restore',json={},headers=ADMIN).json()['source']=='评测集预置'
    assert client.get(route+'/document').content==raw


def test_notice_template_can_be_reimported(client):
    raw=(templates.BUILTINS/templates.CATALOG['notice'][0]).read_bytes()
    assert client.post('/api/templates/notice',files={'file':('notice.docx',raw)},headers=ADMIN).status_code==200
    assert outputs.template('国有土地租金催缴通知书（模板）.docx').read_bytes()==raw
    assert client.get('/api/templates/no-such-kind').status_code==404


def test_page_level_prefill_and_map_detection_do_not_need_manual_unit_selection(client):
    from test_api import new_doc
    doc, _ = new_doc(client)
    unit = doc['pages'][0]['id']
    result = {'candidates': {'tenant': [{'value': '测试承租人', 'source_page_id': unit, 'model': 'synthetic',
                                      'score': .8, 'kind': 'vision', 'raw_text': '测试承租人'}],
                             'has_map': [{'value': '是', 'source_page_id': unit, 'model': 'synthetic',
                                          'score': .9, 'kind': 'vision', 'raw_text': '宗地图'}]}}
    with store.connection() as db:
        db.execute('UPDATE documents SET result=? WHERE id=?', (store.dumps(result), doc['id']))
    filled = client.get('/api/documents/'+doc['id']).json()['prefill']
    assert filled['fields']['has_map']=='是'
    assert filled['citations']['tenant']==unit
