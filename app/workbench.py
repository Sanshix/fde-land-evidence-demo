"""Business workbench additions. All effective changes use immutable events and previews."""
import hashlib
import json
import time
from datetime import date

from fastapi import HTTPException, Request

from . import rules, store
from .business import calculate_documents
from .domain import AS_OF, dec


def initialize(db):
    if db is not None:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS business_events(id TEXT PRIMARY KEY,parcel_id TEXT NOT NULL,kind TEXT NOT NULL,
          payload TEXT NOT NULL,created REAL NOT NULL,actor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sms_tasks(id TEXT PRIMARY KEY,notice_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,
          status TEXT NOT NULL,created REAL NOT NULL,actor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS output_provenance(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sms_receipts(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,payload TEXT NOT NULL,
          created REAL NOT NULL);
        ''')
        for table in ('business_events', 'sms_receipts', 'output_provenance'):
            for op in ('UPDATE', 'DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable history'); END")


def event_source(db, source):
    # Frozen evidence input list makes supplemental records apply to the same contract chain.
    try:
        events = [json.loads(r[0]) for r in db.execute('SELECT payload FROM business_events ORDER BY created,id')]
    except Exception as exc:
        import sqlite3
        if not isinstance(exc, sqlite3.OperationalError):
            raise
        events = []
    active = {r[0] for r in db.execute("SELECT id FROM documents WHERE state!='withdrawn'")}
    for row in db.execute("SELECT intake FROM documents WHERE state='published'"):
        active.update(json.loads(row[0] or '{}').get('revision_ancestors', []))
    events = [e for e in events if e.get('evidence_document') in active]
    return {**source, 'business_events': events}


def fingerprint(value):
    return hashlib.sha256(store.dumps(value).encode()).hexdigest()


def register(app, env):
    actor = env['actor']


    @app.get('/api/contracts')
    def contracts():
        with store.connection() as db:
            return [{**dict(r), 'status': 'pending_confirmation', 'values': json.loads(r['payload'])} for r in db.execute(
                'SELECT * FROM generated_contracts ORDER BY created DESC')]

    @app.get('/api/contracts/{identifier}')
    def contract_detail(identifier: str):
        with store.connection() as db:
            row = db.execute('SELECT * FROM generated_contracts WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise HTTPException(404)
            return {**dict(row), 'status': 'pending_confirmation', 'values': json.loads(row['payload'])}

    @app.get('/api/parcels/{identifier}/history')
    def history(identifier: str):
        with store.connection() as db:
            return {'publications': [{**dict(r), 'payload': json.loads(r['payload'])} for r in db.execute(
                'SELECT * FROM publications WHERE parcel_id=? ORDER BY id DESC', (identifier,))],
                'events': [dict(r) for r in db.execute('SELECT * FROM business_events WHERE parcel_id=? ORDER BY created DESC', (identifier,))]}

    @app.post('/api/parcels/{identifier}/events/preview')
    async def event_preview(identifier: str, request: Request):
        actor(request)
        body = await request.json()
        with store.connection() as db:
            return prepare_event(db, identifier, body)

    def prepare_event(db, identifier, body):
        latest = store.latest(db, identifier)
        if not latest or not latest['payload'].get('documents'):
            raise HTTPException(404, '无有效台账')
        if latest['id'] != body.get('publication'):
            raise HTTPException(409, '台账已变化，请重新选择')
        if not str(body.get('reason', '')).strip():
            raise HTTPException(400, '请填写原件依据与核对说明')
        if not body.get('evidence_confirmed'):
            raise HTTPException(400, '须确认已对照缴费或终止依据原件核验')
        evidence = body.get('evidence_document')
        doc = db.execute('SELECT id,state FROM documents WHERE id=?', (evidence,)).fetchone()
        if not doc or doc['state'] in {'withdrawn', 'queued', 'recognizing'}:
            raise HTTPException(400, '请先上传并核对有效依据材料')
        try:
            payload = {'kind': body['kind'], 'documents': latest['payload']['documents'],
                       'evidence_document': evidence, 'reason': str(body['reason'])[:2000]}
            if body['kind'] == 'payment':
                amount = dec(body['amount'])
                if not amount.is_finite() or amount <= 0 or amount > 1000000000:
                    raise ValueError('缴费金额须为有效正数')
                year = int(body['year'])
                if not 1900 <= year <= AS_OF.year:
                    raise ValueError('缴费归属年度超出当前核算范围')
                receipt = str(body.get('receipt', '')).strip()
                if not receipt:
                    raise ValueError('须填写票据号')
                for row in db.execute("SELECT payload FROM business_events WHERE kind='payment'"):
                    if json.loads(row[0])['payment']['票据号'] == receipt:
                        raise ValueError('该票据已确认入账，不可重复归集')
                if any(str(p.get('票据号', '')).strip() == receipt for p in env['source_data']()['payments']):
                    raise ValueError('票据已在导入缴费来源中，请通过材料核对选择原记录')
                payload['payment'] = {'序号': 'manual-' + receipt, '票据号': receipt, '金额（元）': str(amount),
                    '缴费年度': str(year), '所属年度': str(year), '缴费日期': date.fromisoformat(body['date']).isoformat(),
                    '缴款人': latest['payload']['recipient'], '宗地号': latest['payload'].get('registry_unit', '')}
            elif body['kind'] == 'termination':
                payload['stop'] = date.fromisoformat(body['date']).isoformat()
                starts = [d['review']['fields'].get('start') for d in (store.doc_row(r) for r in db.execute("SELECT * FROM documents WHERE parcel_id=? AND state='published'", (identifier,))) if d['review']['fields'].get('start')]
                if starts and payload['stop'] < min(starts):
                    raise ValueError('终止日不能早于原合同起始日')
                if payload['stop'] > AS_OF.isoformat():
                    raise ValueError('终止日在核算截至日之后，请先作为待办材料保留')
            else:
                raise ValueError('仅支持缴费或终止；合同变更请走材料核验流程')
            docs = [store.doc_row(r) for r in db.execute("SELECT * FROM documents WHERE parcel_id=? AND state='published'", (identifier,))]
            source = rules.source_for(db, env['source_data']())
            after = calculate_documents(docs, {**source, 'business_events': [*source.get('business_events', []), payload]})
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        result = {'before': latest, 'after': after, 'event': payload}
        return {**result, 'preview_hash': fingerprint(result)}

    @app.post('/api/parcels/{identifier}/events/confirm')
    async def event_confirm(identifier: str, request: Request):
        who = actor(request)
        body = await request.json()
        if not body.get('confirmed'):
            raise HTTPException(400, '请复核逐年差异后确认')
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            result = prepare_event(db, identifier, body)
            if body.get('preview_hash') != result['preview_hash']:
                raise HTTPException(409, '输入或台账变化，请重新预览')
            key = store.uid()
            db.execute('INSERT INTO business_events VALUES(?,?,?,?,?,?)',
                       (key, identifier, body['kind'], store.dumps(result['event']), time.time(), who))
            pub = store.publish(db, identifier, result['after'], who)
            store.audit(db, who, 'business_event_confirmed', key, result['event'])
            return pub

    @app.post('/api/notices/{identifier}/send')
    async def send(identifier: str, request: Request):
        who = actor(request)
        body = await request.json()
        channel = body.get('channel', 'simulation')
        adapter = None
        if channel != 'simulation':
            from .sms import configured_adapter
            try:
                adapter = configured_adapter()
            except (ValueError, ImportError, AttributeError) as exc:
                raise HTTPException(400, '真实通道未配置，禁止发送') from exc
        outcome = body.get('outcome', 'success')
        if outcome not in {'success', 'failure', 'timeout'}:
            raise HTTPException(400, '无效模拟结果')
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM sms_tasks WHERE notice_id=?', (identifier,)).fetchone()
            if previous:
                return dict(previous)
            row = db.execute('SELECT * FROM notices WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise HTTPException(404)
            n = json.loads(row['payload'])
            publication = db.execute('SELECT parcel_id FROM publications WHERE id=?', (row['publication_id'],)).fetchone()
            latest = store.latest(db, publication[0])
            if not latest or latest['id'] != row['publication_id'] or dec(latest['payload']['arrears']) <= 0:
                raise HTTPException(409, '通知对应台账已变化或已无欠缴，请重新生成')
            event = db.execute('SELECT status FROM notice_events WHERE notice_id=? ORDER BY id DESC LIMIT 1', (identifier,)).fetchone()
            if not event or event[0] != '已审核':
                raise HTTPException(400, '请先审核当前通知')
            if not n['phone_valid'] or not n['settings_configured']:
                raise HTTPException(400, '电话或缴费地点配置不完整')
            key = store.uid()
            payload = {'channel': channel, 'simulation': adapter is None, 'content': n['sms_draft'], 'publication': row['publication_id']}
            db.execute('INSERT INTO sms_tasks VALUES(?,?,?,?,?,?)', (key, identifier, store.dumps(payload), '发送中', time.time(), who))
            store.audit(db, who, 'sms_task_created', key, payload)
        # A crash/timeout after dispatch leaves an existing pending task. Never auto-resend it.
        if adapter is not None:
            try:
                outcome = adapter.send(key, n['phone'], n['sms_draft'])
                if outcome not in {'success', 'failure', 'timeout'}:
                    outcome = 'timeout'
            except Exception:
                outcome = 'timeout'
        prefix = '模拟' if adapter is None else ''
        status = {'success': prefix + '发送成功', 'failure': prefix + '发送失败', 'timeout': '结果未知'}[outcome]
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if outcome != 'timeout':
                db.execute('INSERT OR IGNORE INTO sms_receipts VALUES(?,?,?,?)',
                           (key + '-receipt', key, store.dumps({'status': status, 'simulation': adapter is None}), time.time()))
            db.execute('UPDATE sms_tasks SET status=? WHERE id=?', (status, key))
            db.execute('INSERT INTO notice_events(notice_id,status,note,created,actor) VALUES(?,?,?,?,?)',
                       (identifier, status, '模拟通道；不发送真实短信' if adapter is None else '已配置通道返回', time.time(), who))
            store.audit(db, who, 'sms_result', key, {'status': status, **payload})
        return {'id': key, 'status': status, **payload}

    @app.post('/api/notices/{identifier}/simulation-receipt')
    async def simulation_receipt(identifier: str, request: Request):
        who = actor(request)
        body = await request.json()
        if body.get('status') not in {'success', 'failure'} or not body.get('receipt_id'):
            raise HTTPException(400, '须填写回执号和成功/失败状态')
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT * FROM sms_tasks WHERE notice_id=?', (identifier,)).fetchone()
            if not task or not json.loads(task['payload']).get('simulation'):
                raise HTTPException(400, '仅支持模拟任务回执')
            receipt_id = task['id'] + ':' + str(body['receipt_id'])[:100]
            existing = db.execute('SELECT payload FROM sms_receipts WHERE id=?', (receipt_id,)).fetchone()
            if existing:
                if json.loads(existing[0]) != {'status': body['status']}:
                    raise HTTPException(409, '同一回执号内容冲突')
                return {'id': task['id'], 'status': task['status']}
            if task['status'] not in {'结果未知', '发送中'}:
                raise HTTPException(409, '任务已结束；不接受覆盖结果')
            status = '模拟发送成功' if body['status'] == 'success' else '模拟发送失败'
            db.execute('INSERT INTO sms_receipts VALUES(?,?,?,?)', (receipt_id, task['id'], store.dumps({'status': body['status']}), time.time()))
            db.execute('UPDATE sms_tasks SET status=? WHERE id=?', (status, task['id']))
            db.execute('INSERT INTO notice_events(notice_id,status,note,created,actor) VALUES(?,?,?,?,?)',
                       (identifier, status, '模拟回执', time.time(), who))
            store.audit(db, who, 'sms_simulation_receipt', task['id'], body)
            return {'id': task['id'], 'status': status}

    @app.get('/api/notices/{identifier}/tasks')
    def tasks(identifier: str):
        with store.connection() as db:
            return [dict(r) | {'receipts': [dict(x) for x in db.execute('SELECT * FROM sms_receipts WHERE task_id=?', (r['id'],))]} for r in db.execute('SELECT * FROM sms_tasks WHERE notice_id=?', (identifier,))]
