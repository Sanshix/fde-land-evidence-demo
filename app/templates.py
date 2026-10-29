"""Versioned DOCX templates. Built-ins ship with the application; uploads never overwrite history."""
import hashlib
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from . import store

BUILTINS = Path(__file__).resolve().parents[1] / 'config/templates'
CATALOG = {
    'contract': ('国有建设用地使用权租赁合同（模板）.docx', {'承租人', '坐落', '起始日期', '终止日期', '年租金小写'}),
    'notice': ('国有土地租金催缴通知书（模板）.docx', {'催缴对象', '坐落', '欠缴金额'}),
}


def describe(kind):
    if kind not in CATALOG:
        raise HTTPException(404, '未知模板类型')
    name, required = CATALOG[kind]
    with store.connection() as db:
        row = db.execute('SELECT value FROM settings WHERE key=?', ('template:'+kind,)).fetchone()
    import json
    saved = json.loads(row[0]) if row else None
    return {'kind': kind, 'name': saved['name'] if saved else name, 'source': '已导入模板' if saved else '评测集预置',
            'sha256': saved['sha256'] if saved else hashlib.sha256((BUILTINS/name).read_bytes()).hexdigest(),
            'required_placeholders': sorted(required)}


def active_path(kind):
    info = describe(kind)
    return (BUILTINS / CATALOG[kind][0]) if info['source'] == '评测集预置' else store.data_dir()/'objects'/(info['sha256']+'.docx')


def validate(raw, kind):
    if not raw or len(raw) > 5*1024*1024:
        raise ValueError('模板大小须为 1 字节至 5 MB')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if sum(i.file_size for i in z.infolist()) > 20*1024*1024:
                raise ValueError('模板解压后不得超过 20 MB')
            if any('vbaProject' in i.filename or i.filename.startswith('word/embeddings/') for i in z.infolist()):
                raise ValueError('模板不得包含宏或嵌入对象')
            for entry in z.infolist():
                if entry.filename.endswith('.rels'):
                    relationships = ET.fromstring(z.read(entry))
                    if any(r.get('TargetMode') == 'External' for r in relationships):
                        raise ValueError('模板不得包含外部链接或远程资源')
            root = ET.fromstring(z.read('word/document.xml'))
            ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            text = '\n'.join(''.join(p.itertext()) for p in root.iter(ns+'p'))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError('请上传有效的 DOCX 模板') from exc
    fields = set(re.findall(r'【([^【】]+)】', text))
    missing = CATALOG[kind][1] - fields
    if missing:
        raise ValueError('缺少必需占位符：'+ '、'.join('【'+k+'】' for k in sorted(missing)))


def register(app, actor):
    @app.get('/api/templates/{kind}')
    def template_info(kind: str):
        return describe(kind)

    @app.get('/api/templates/{kind}/document')
    def download(kind: str, builtin: bool = False):
        info = describe(kind)
        path = BUILTINS/CATALOG[kind][0] if builtin else active_path(kind)
        return FileResponse(path, filename=CATALOG[kind][0] if builtin else info['name'])

    @app.post('/api/templates/{kind}')
    async def upload(kind: str, request: Request, file: UploadFile = File(...)):
        who = actor(request, admin=True)
        describe(kind)
        raw = await file.read(5*1024*1024+1)
        if not (file.filename or '').lower().endswith('.docx'):
            raise HTTPException(400, '只支持 DOCX 模板')
        try:
            validate(raw, kind)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        digest = hashlib.sha256(raw).hexdigest()
        folder = store.data_dir()/'objects'; folder.mkdir(exist_ok=True)
        target = folder/(digest+'.docx')
        # Content addressed paths make simultaneous imports safe and preserve previous versions.
        import os, tempfile
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as temp:
            temp.write(raw); temporary = temp.name
        os.replace(temporary, target)
        payload = {'name': Path(file.filename).name, 'sha256': digest}
        with store.connection() as db:
            db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('template:'+kind, store.dumps(payload)))
            store.audit(db, who, 'template_import', kind, payload)
        return describe(kind)

    @app.post('/api/templates/{kind}/restore')
    def restore(kind: str, request: Request):
        who = actor(request, admin=True)
        describe(kind)
        with store.connection() as db:
            db.execute('DELETE FROM settings WHERE key=?', ('template:'+kind,))
            store.audit(db, who, 'template_restore_builtin', kind, {})
        return describe(kind)
