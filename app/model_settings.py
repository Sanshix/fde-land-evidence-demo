"""Administrator selection, transactionally persisted with audit and optimistic versioning."""
import copy
import hashlib
import ipaddress
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from . import store
from .recognition_workflow import configure, WORKFLOW, PLAN2, PLAN3, HIGH_END

CATALOG = Path(__file__).resolve().parents[1] / 'config/reasoning-models.json'
KEY = 'reasoning_model_selection'


def catalog():
    return json.loads(CATALOG.read_text())


def read_selection(db):
    row = db.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def base_hash(base):
    return hashlib.sha256(store.dumps(base).encode()).hexdigest()


def apply_selection(base):
    if not store.database_file().exists():
        return base
    try:
        with store.connection() as db:
            selection = read_selection(db)
    except sqlite3.OperationalError as error:
        if 'no such table' not in str(error):
            raise
        return base
    if not selection or selection.get('base_hash') != base_hash(base):
        return base
    if selection.get('workflow_primary'):
        return {**configure(base, selection['workflow_primary']), 'enabled': selection['enabled']}
    profile = selection['profile']
    # A local selection must never inherit cloud fallback stages.
    return {**copy.deepcopy(base), 'enabled': selection['enabled'],
            'models': {'primary': profile}, 'workflow': None, 'document_context': False, 'page_budget_seconds': 120,
            'reason': '管理员选择的开源视觉推理模型；单路径候选，须人工核验'}


def status(base, db):
    saved = read_selection(db)
    active = saved if saved and saved.get('base_hash') == base_hash(base) else None
    workflow_primary = None
    if base.get('workflow') == PLAN3:
        model = base.get('models', {}).get('secondary', {}).get('model_id')
        workflow_primary = next((key for key, value in HIGH_END.items() if value == model), 'plan3')
    elif base.get('workflow') in {WORKFLOW, PLAN2}:
        workflow_primary = base.get('models', {}).get('primary', {}).get('model_id')
    return {**catalog(), 'version': saved.get('version', 0) if saved else 0,
            'base_hash': base_hash(base), 'selection': active,
            'workflow_primary': active.get('workflow_primary') if active else workflow_primary,
            'enabled': active['enabled'] if active else base.get('enabled', False),
            'base_changed': bool(saved and (saved.get('profile') or saved.get('workflow_primary')) and not active)}


def save(base, db, body, who):
    current = status(base, db)
    if body.get('version') != current['version'] or body.get('base_hash') != current['base_hash']:
        raise RuntimeError('模型配置已变化，请重新加载后选择')
    version = current['version'] + 1
    if body.get('reset') is True:
        selection = {'version': version}  # tombstone retains version against stale writes
    elif 'workflow_primary' in body:
        if type(body.get('enabled')) is not bool:
            raise ValueError('请明确是否启用识别')
        config = configure(base, body['workflow_primary'])
        selection = {'version': version, 'base_hash': base_hash(base),
                     'workflow_primary': body['workflow_primary'], 'profile': config['models']['primary'],
                     'enabled': body['enabled'], 'deployment': 'aliyun'}
    else:
        item = next((m for m in current['models'] if m['id'] == body.get('model')), None)
        if not item:
            raise ValueError('请选择目录中的开源视觉模型')
        if type(body.get('enabled')) is not bool:
            raise ValueError('请明确是否启用识别')
        deployment = body.get('deployment')
        if deployment == 'local':
            endpoint = str(body.get('endpoint', '')).strip().rstrip('/')
            url = urlparse(endpoint)
            try:
                address = ipaddress.ip_address(url.hostname or '')
                private = (address.is_loopback or address in ipaddress.ip_network('10.0.0.0/8')
                           or address in ipaddress.ip_network('172.16.0.0/12')
                           or address in ipaddress.ip_network('192.168.0.0/16'))
            except ValueError:
                private = url.hostname == 'localhost'
            if not private or url.scheme not in ('http', 'https') or url.username or url.password or url.query or url.fragment:
                raise ValueError('本地端点须使用回环或RFC1918内网IP，且不含凭据、查询参数')
            profile = {'provider': 'vllm-intranet', 'base_url': endpoint, 'api_key_env': '',
                       'model_id': item['local_model_id']}
        elif deployment == 'aliyun':
            old = next((p for p in base.get('models', {}).values() if p.get('provider') == 'aliyun'), None)
            if not old or not old.get('authorized'):
                raise ValueError('请先在部署配置中设置获授权的阿里云端点与密钥环境变量')
            if urlparse(old.get('base_url', '')).hostname in item.get('aliyun_unavailable_hosts', []):
                raise ValueError('该模型在当前阿里云北京端点实测返回404；可选择Qwen3.5-27B或使用本地部署')
            profile = {k: old[k] for k in ('provider', 'base_url', 'api_key_env') if k in old}
            profile['model_id'] = item['aliyun_model_id']
        else:
            raise ValueError('部署方式不合法')
        profile.update(catalog_id=item['id'], open_weights=True, authorized=True,
                       license=item['license'], model_card_url=item['model_card_url'],
                       identity_verified=False, weight_revision=None, stream=True,
                       enable_thinking=True, thinking_budget=4096, max_tokens=12000)
        if item['id'] == 'qwen3.5-27b':
            profile['full_transcription'] = True
        if item.get('thinking_only'):
            profile['thinking_only'] = True
        selection = {'version': version, 'base_hash': base_hash(base), 'profile': profile,
                     'enabled': body['enabled'], 'deployment': deployment}
    db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (KEY, store.dumps(selection)))
    store.audit(db, who, 'model_selection', KEY, {'before': current['selection'], 'after': selection})
    return selection
