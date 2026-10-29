"""Durable isolated demo runs. Reset archives an entire test DB; formal data is never deleted."""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextvars import ContextVar
from pathlib import Path

from . import business, rules, store
from .cloud_cascade import profile_signature
from .demo_report import build

ROOT = Path(__file__).resolve().parents[1]
_THREADS = {}
_LOCKS = {}
_GUARD = threading.Lock()
_SOURCE_CACHE = {}
ACTIVE = ('queued', 'running', 'reporting')
_CONTROL_ROOT = ContextVar('demo_control_root', default=None)


def root():
    return _CONTROL_ROOT.get() or Path(os.environ.get('FDE_DEMO_ROOT', store.DATA / 'test-demo')).resolve()


def space_lock(identifier):
    with _GUARD:
        return _LOCKS.setdefault((str(root()), identifier), threading.Lock())


@contextlib.contextmanager
def control():
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(base / 'control.sqlite', timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS test_runs(
        id TEXT PRIMARY KEY,dataset TEXT NOT NULL,mode TEXT NOT NULL,state TEXT NOT NULL,
        actor TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,fingerprint TEXT NOT NULL,
        payload TEXT NOT NULL,archived INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_demo ON test_runs((1))
        WHERE state IN ('queued','running','reporting');
        CREATE TABLE IF NOT EXISTS demo_audit(id INTEGER PRIMARY KEY,at REAL,actor TEXT,action TEXT,run_id TEXT,payload TEXT);''')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get(identifier):
    if not re.fullmatch('[a-f0-9]{16}', identifier or ''):
        raise ValueError('测试运行编号无效')
    with control() as db:
        row = db.execute('SELECT * FROM test_runs WHERE id=?', (identifier,)).fetchone()
    if row is None:
        raise ValueError('测试运行不存在')
    return {**dict(row), **json.loads(row['payload'])}


def folder(identifier):
    if not re.fullmatch('[a-f0-9]{16}', identifier or ''):
        raise ValueError('测试运行编号无效')
    return root() / 'runs' / identifier


def save(run, **updates):
    run.update(updates)
    run['updated'] = time.time()
    with control() as db:
        db.execute('UPDATE test_runs SET state=?,updated=?,payload=?,archived=? WHERE id=?',
                   (run['state'], run['updated'], store.dumps(run), int(run.get('archived', 0)), run['id']))


def audit(who, action, identifier, payload):
    with control() as db:
        db.execute('INSERT INTO demo_audit(at,actor,action,run_id,payload) VALUES(?,?,?,?,?)',
                   (time.time(), who, action, identifier, store.dumps(payload)))


def configs():
    path = Path(os.environ.get('FDE_DEMO_CONFIG', ROOT / 'config/demo-datasets.json'))
    return json.loads(path.read_text())['datasets']


def preflight(dataset, mode):
    config = configs().get(dataset)
    if not config or mode not in {'replay', 'live'}:
        raise ValueError('请选择练习题/评测题及明确的识别模式')
    directory = Path(os.environ.get(config['directory_env'], ROOT / config['directory'])).resolve()
    if not directory.is_dir():
        raise ValueError(f"未找到{config['label']}目录；请配置 {config['directory_env']}")
    missing = [n for n in business.REQUIRED_SOURCES if not (directory / n).is_file()]
    if missing:
        raise ValueError('测试数据缺少业务表：' + '、'.join(missing))
    try:
        rules.validate(business.sources(directory)['rates'])
    except Exception as exc:
        raise ValueError('测试业务表无法读取或费率无效：' + type(exc).__name__) from exc
    cache = (ROOT / config['cache']).resolve()
    if not (cache / 'documents.json').is_file() or not (cache / 'providers.json').is_file():
        raise ValueError('缺少声明的模拟材料/冻结证据清单；不会自动切换实时模式')
    cache_config = json.loads((cache / 'providers.json').read_text())
    cache_profile = profile_signature(cache_config)
    if cache_profile != config['cache_profile']:
        raise ValueError('缓存模型条件与声明配置不符，请核对缓存清单')
    documents = json.loads((cache / 'documents.json').read_text())
    if not documents or len({d['name'] for d in documents}) != len(documents):
        raise ValueError('测试清单为空或含重复档案号')
    hashes, total_pages = {}, 0
    for doc in documents:
        if not re.fullmatch(config['prefix'] + r'-\d{3}', doc['name']):
            raise ValueError('测试清单含不属于该数据集的档案号')
        if not doc.get('files'):
            raise ValueError('文档清单缺文件')
        for filename in doc['files']:
            if Path(filename).name != filename:
                raise ValueError('测试文件名不允许跨目录')
            path = directory / '01_合同扫描件' / filename
            if not path.is_file():
                raise ValueError('测试数据缺文件：' + filename)
            hashes[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
        # Declared fixtures are separate scanned images; no guessed PDF page counts.
        total_pages += len(doc['files'])
        if mode == 'live':
            proof = cache / 'recognition' / doc['name'] / 'result.json'
            if not proof.is_file():
                raise ValueError('实时演示缺少已授权模拟材料的哈希清单')
            known = {p.get('source_sha256') for p in json.loads(proof.read_text()).get('pages', [])}
            if not all(hashes[f] in known for f in doc['files']):
                raise ValueError('实时演示材料不在已核实的模拟样本哈希清单中')
    auxiliary_hashes = {n: hashlib.sha256((directory / n).read_bytes()).hexdigest() for n in business.REQUIRED_SOURCES}
    answers_file = directory / '08_标准答案/合同要素.csv'
    if dataset == 'practice' and answers_file.is_file():
        auxiliary_hashes['08_标准答案/合同要素.csv'] = hashlib.sha256(answers_file.read_bytes()).hexdigest()
    version = {'code_sha256': hashlib.sha256(''.join(hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted((ROOT / 'app').glob('*.py'))).encode()).hexdigest(),
               'parser': __import__('app.deterministic_parser', fromlist=['VERSION']).VERSION,
               'cache_profile': cache_profile, 'models': [{'provider': p.get('provider'), 'model_id': p.get('model_id')} for p in cache_config['models'].values() if p], 'model_mode': 'historical_declared_profiles' if mode == 'replay' else 'current_configured_adapter'}
    return {'samples': str(directory), 'cache': str(cache), 'cache_profile': cache_profile,
            'documents': [{'name': d['name'], 'files': d['files']} for d in documents],
            'documents_expected': len(documents), 'files_expected': sum(len(d['files']) for d in documents),
            'pages_expected': total_pages, 'source_hashes': hashes, 'auxiliary_hashes': auxiliary_hashes, 'version': version}


def workspace(run):
    if run.get('archived'):
        raise ValueError('该测试空间已归档，请恢复或返回正式空间')
    samples = run['manifest']['samples']
    key = (run['id'], samples)
    if key not in _SOURCE_CACHE:
        frozen_sources = folder(run['id']) / 'business-sources'
        _SOURCE_CACHE[key] = business.sources(frozen_sources if frozen_sources.is_dir() else Path(samples))
    return {'id': run['id'], 'dataset': run['dataset'], 'mode': run['mode'],
            'data': folder(run['id']) / 'data', 'samples': samples, 'source': _SOURCE_CACHE[key],
            'cache': run['manifest']['cache'], 'cache_profile': run['manifest']['cache_profile'],
            'model_config': run.get('model_config'), 'budget_seconds': 240}


def initialize():
    recover()


def runner_lock(nonblocking=True):
    root().mkdir(parents=True, exist_ok=True)
    handle = (root() / 'runner.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
    except OSError:
        handle.close()
        return None
    return handle


def recover():
    handle = runner_lock()
    if handle is None:
        return
    try:
        with control() as db:
            rows = db.execute("SELECT id,payload FROM test_runs WHERE state IN ('queued','running','reporting')").fetchall()
        for row in rows:
            if (str(root()), row['id']) in _THREADS:
                continue
            run = get(row['id'])
            # A newly scheduled thread may not yet hold the process lock.
            if time.time() - run['created'] < 3:
                continue
            run.update(state='interrupted', error='后台进程中断；未自动重发可能已提交的实时请求')
            save(run)
            try:
                with store.use_workspace(workspace(run)):
                    store.init()
                    build(run, folder(run['id']), run['items'])
            except Exception:
                pass  # The terminal failure stays visible even if report generation is unavailable.
    finally:
        handle.close()


def list_runs():
    recover()
    with control() as db:
        ids = [r[0] for r in db.execute('SELECT id FROM test_runs ORDER BY created DESC LIMIT 30')]
    return [public(get(i)) for i in ids]


def public(run):
    return {k: run.get(k) for k in ['id', 'dataset', 'mode', 'state', 'actor', 'created', 'updated',
                                   'progress', 'error', 'archived']} | {'has_report': (folder(run['id']) / 'report.json').is_file(),
                                   'expected': run['manifest']['documents_expected']}


def start(dataset, mode, who, role, env):
    manifest = preflight(dataset, mode)
    model_config = env['configuration']() if mode == 'live' else None
    if mode == 'live':
        if not model_config.get('enabled') or model_config.get('authorized_scope') != 'competition_simulated':
            raise ValueError('实时演示需已有启用且授权范围为比赛模拟材料的模型配置')
        def contains_secret(value):
            if isinstance(value, dict):
                return any((k.lower() in {'api_key', 'token', 'secret', 'password'} and bool(v)) or contains_secret(v) for k, v in value.items())
            return isinstance(value, list) and any(contains_secret(v) for v in value)
        if contains_secret(model_config):
            raise ValueError('实时配置须引用既有环境变量，不能将内嵌密钥保存到测试记录')
        manifest['version']['live_profile'] = profile_signature(model_config)
        manifest['version']['models'] = [{'provider': p.get('provider'), 'model_id': p.get('model_id')} for p in model_config.get('models', {}).values() if p]
    fingerprint = hashlib.sha256(store.dumps({'dataset': dataset, 'mode': mode, 'manifest': manifest}).encode()).hexdigest()
    with control() as db:
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT * FROM test_runs WHERE state IN ('queued','running','reporting')").fetchone()
        if active:
            if active['dataset'] == dataset and active['mode'] == mode:
                return {**dict(active), **json.loads(active['payload'])}, True
            raise ValueError('已有其他测试运行中，请先完成当前运行')
        previous = db.execute('SELECT * FROM test_runs WHERE fingerprint=? AND archived=0 ORDER BY created DESC LIMIT 1', (fingerprint,)).fetchone()
        if previous:
            return {**dict(previous), **json.loads(previous['payload'])}, True
        identifier = store.uid()
        run = {'id': identifier, 'dataset': dataset, 'mode': mode, 'state': 'queued', 'actor': who,
               'created': time.time(), 'updated': time.time(), 'manifest': manifest,
               'items': [{'name': d['name'], 'files': d['files']} for d in manifest['documents']],
               'model_config': model_config, 'archived': 0, 'progress': {'processed': 0, 'batch': 0}, 'error': None}
        db.execute('INSERT INTO test_runs VALUES(?,?,?,?,?,?,?,?,?,0)',
                   (identifier, dataset, mode, 'queued', who, run['created'], run['updated'], fingerprint, store.dumps(run)))
    data = folder(identifier) / 'data'
    data.mkdir(parents=True)
    frozen_sources = folder(identifier) / 'business-sources'
    frozen_sources.mkdir()
    for name in business.REQUIRED_SOURCES:
        raw = (Path(manifest['samples']) / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest['auxiliary_hashes'][name]:
            save(run, state='failed', error='业务表在启动期间变化，未启动识别')
            raise ValueError('业务表在启动期间变化，未启动识别')
        (frozen_sources / name).write_bytes(raw)
    with store.use_workspace(workspace(run)):
        store.init()
        rules.initialize(workspace(run)['source'])
    formal_root = root()
    stop = threading.Event()
    thread = threading.Thread(target=_run, args=(identifier, formal_root, role, env, stop), daemon=True, name='demo-' + identifier)
    _THREADS[(str(formal_root), identifier)] = (thread, stop)
    thread.start()
    audit(who, 'start_test_run', identifier, {'dataset': dataset, 'mode': mode})
    return run, False


def _run(identifier, formal_root, role, env, stop):
    # The root is explicit; thread contexts do not inherit the request's selected workspace.
    root_token = _CONTROL_ROOT.set(formal_root)
    handle = runner_lock()
    for _ in range(10):
        if handle is not None or stop.wait(0.1):
            break
        handle = runner_lock()
    if handle is None:
        run = get(identifier)
        save(run, state='failed', error='测试执行器被占用；未启动识别')
        _THREADS.pop((str(formal_root), identifier), None)
        _CONTROL_ROOT.reset(root_token)
        return
    try:
        run = get(identifier)
        save(run, state='running')
        with store.use_workspace(workspace(run)):
            token = store.operation_role.set(role)
            try:
                for start_index in range(0, len(run['items']), 5):
                    if stop.is_set():
                        break
                    batch_started = time.time()
                    chunk = run['items'][start_index:start_index + 5]
                    for item in chunk:
                        if stop.is_set():
                            break
                        try:
                            payload = [(f, (Path(run['manifest']['samples']) / '01_合同扫描件' / f).read_bytes()) for f in item['files']]
                            for filename, raw in payload:
                                if hashlib.sha256(raw).hexdigest() != run['manifest']['source_hashes'][filename]:
                                    raise ValueError('原始数据在启动后发生变化，未导入')
                            result = env['import_files'](payload, item['name'], run['actor'])
                            item.update(document_id=result['id'], duplicate=result['duplicate'])
                            with store.connection() as db:
                                db.execute("UPDATE jobs SET strategy=? WHERE document_id=? AND state='queued'",
                                           ('demo_replay' if run['mode'] == 'replay' else 'cascade', result['id']))
                        except Exception as exc:
                            item['error'] = str(exc)[:500]
                        save(run)
                    while not stop.is_set():
                        did = env['worker_once'](stop=stop)
                        if not did:
                            did = env['worker_once'](fallback=True, stop=stop)
                        if not did:
                            break
                    run['progress'] = {'processed': min(start_index + 5, len(run['items'])), 'batch': start_index // 5 + 1}
                    run.setdefault('batches', []).append({'number': start_index // 5 + 1, 'size': len(chunk), 'elapsed_seconds': round(time.time() - batch_started, 3)})
                    save(run)
                    if os.environ.get('FDE_DEMO_BATCH_PAUSE'):
                        stop.wait(min(float(os.environ['FDE_DEMO_BATCH_PAUSE']), 2))
                save(run, state='interrupted' if stop.is_set() else 'reporting')
                report = build(run, folder(identifier), run['items'])
                errors = report['actual']['recognition_states']
                terminal = 'interrupted' if stop.is_set() else 'completed' if set(errors) <= {'completed'} else 'partial'
                save(run, state=terminal)
                report['state'] = terminal
                (folder(identifier) / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            finally:
                store.operation_role.reset(token)
    except Exception as exc:
        run = get(identifier)
        save(run, state='failed', error=str(exc)[:500])
        try:
            with store.use_workspace(workspace(run)):
                build(run, folder(identifier), run['items'])
        except Exception:
            pass  # Terminal failure remains visible; never retry unknown cloud work.
    finally:
        handle.close()
        _THREADS.pop((str(formal_root), identifier), None)
        _CONTROL_ROOT.reset(root_token)


def shutdown():
    key = str(root())
    for (base, _), (thread, stop) in list(_THREADS.items()):
        if base == key:
            stop.set()
            thread.join(timeout=3)


def reset_plan(identifier):
    run = get(identifier)
    if run['state'] in ACTIVE:
        raise ValueError('测试仍在运行，禁止重置，避免迟到写入')
    if run.get('archived'):
        raise ValueError('该测试空间已经归档')
    with store.use_workspace(workspace(run)), store.connection() as db:
        tables = ['documents', 'review_drafts', 'publications', 'notices', 'jobs', 'audit']
        counts = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in tables}
        latest_audit = db.execute('SELECT coalesce(max(id),0) FROM audit').fetchone()[0]
    token = hashlib.sha256(store.dumps([identifier, counts, latest_audit]).encode()).hexdigest()
    return {'run_id': identifier, 'dataset': run['dataset'], 'counts': counts, 'token': token,
            'effect': '仅归档本测试空间及衍生数据；正式合同、原始数据集和配置不变，可恢复'}


def reset(identifier, token, who):
    with space_lock(identifier):
        plan = reset_plan(identifier)
        if token != plan['token']:
            raise ValueError('测试数据已变化，请重新查看重置范围')
        run = get(identifier)
        data = folder(identifier) / 'data'
        archive = folder(identifier) / ('archived-data-' + store.uid())
        data.rename(archive)
        save(run, archived=1, archive_name=archive.name)
        audit(who, 'archive_test_space', identifier, plan['counts'])
        return {'archived': True, 'restore_run_id': identifier, 'counts': plan['counts']}


def restore(identifier, who):
    with space_lock(identifier):
        run = get(identifier)
        if not run.get('archived'):
            return {'restored': True, 'id': identifier}
        archive = folder(identifier) / run['archive_name']
        if archive.parent != folder(identifier) or not archive.is_dir() or (folder(identifier) / 'data').exists():
            raise ValueError('归档不可恢复或目标已存在，未覆盖任何数据')
        archive.rename(folder(identifier) / 'data')
        save(run, archived=0)
        audit(who, 'restore_test_space', identifier, {})
        return {'restored': True, 'id': identifier}
