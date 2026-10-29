"""Exactly five practice docs: frozen replay, unchanged strict score, separate format views and review states."""
import argparse
import collections
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R = ROOT / 'runs/practice-governance-20261029'
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('batch', type=int, choices=range(1, 11))
args = p.parse_args()
names = [f'LX-{n:03}' for n in range((args.batch - 1) * 5 + 1, args.batch * 5 + 1)]
out = R / f'batch{args.batch}.json'
assert not out.exists(), 'Checkpoint already exists: inspect it rather than overwrite.'
os.environ['FDE_SAMPLES_DIR'] = str(ROOT.parent / '练习集')
code = R / 'code'
baseline = R / f'baseline/batch{args.batch}'
if not baseline.exists():
    subprocess.run([sys.executable, str(ROOT / 'scripts/replay_deterministic.py'), '--source',
        str(ROOT / 'runs/competition-full-20261028-final'), '--code-root', str(code),
        '--documents', *names, '--output', str(baseline)], check=True, stdout=subprocess.DEVNULL)
if not (baseline / 'scoring/recognition_score.json').exists():
    subprocess.run([sys.executable, str(code / 'scripts/score_recognition.py'), '--run', str(baseline),
        '--answers', str(ROOT.parent / '练习集/08_标准答案'), '--output', str(baseline / 'scoring')],
        check=True, stdout=subprocess.DEVNULL)
# No network is permissible even in diagnostics or future dependencies.
def offline(*_a, **_k):
    raise RuntimeError('network prohibited in practice rule replay')
socket.socket.connect = offline
socket.create_connection = offline
sys.path.insert(0, str(code))
from app import business  # noqa: E402 (network disabled and frozen module root set first)
from app.review_dimensions import material_reference, receipt_groups, review_dimensions  # noqa: E402
from app.value_views import comparison_view, conflict_diagnosis  # noqa: E402
from scripts.score_recognition import LABEL_IDS, safe_compare, unique_candidate  # noqa: E402

report = json.loads((baseline / 'scoring/recognition_score.json').read_text())
prior = json.loads((ROOT / f'runs/overnight-20261029-round2/replay-practice-candidate/batch{args.batch}/scoring/recognition_score.json').read_text())
def state(x):
    return 'conflict' if x['候选状态'].startswith('withheld') else 'missing' if x['候选状态'] == 'missing' else 'correct' if x['相同'] else 'wrong'
old = {(x['档案号'], x['字段']): x for x in prior['field_rows']}
regressions = []
for row in report['field_rows']:
    before = old[(row['档案号'], row['字段'])]
    if row['计入非空分母'] and (state(row) != state(before) or row['唯一候选'] != before['唯一候选']):
        regressions.append({'document': row['档案号'], 'field': row['字段'], 'before': state(before), 'after': state(row)})
assert not regressions, regressions
strict = collections.Counter(state(x) for x in report['field_rows'] if x['计入非空分母'])
normalized = collections.Counter()
fields = []
for name in names:
    result = json.loads((baseline / 'recognition' / name / 'result.json').read_text())
    for row in [x for x in report['field_rows'] if x['档案号'] == name]:
        key = LABEL_IDS[row['字段']]
        candidates = result['candidates'].get(key, [])
        projected = [{**x, 'value': comparison_view(key, x['value'])['score_comparison_value']} for x in candidates]
        value, status, alternatives = unique_candidate(key, projected)
        gold = comparison_view(key, row['标准答案'])['score_comparison_value']
        correct = value is not None and safe_compare(row['字段'], value) == safe_compare(row['字段'], gold)
        new_state = 'conflict' if status.startswith('withheld') else 'missing' if status == 'missing' else 'correct' if correct else 'wrong'
        if row['计入非空分母']:
            normalized[new_state] += 1
        diagnosis = conflict_diagnosis(key, candidates)
        fields.append({'document': name, 'field': key, 'label': row['字段'], 'nonempty_gold': row['计入非空分母'],
            'strict_state': state(row), 'strict_value': row['唯一候选'], 'gold': row['标准答案'],
            'format_state': new_state, 'format_value': value, 'format_alternatives': alternatives,
            'facts_changed': False, 'gold_used_only_in_score_layer': True,
            'views': [comparison_view(key, x['value']) for x in candidates], 'diagnosis': diagnosis,
            'evidence': [{'value': x['value'], 'raw_text': x.get('raw_text'), 'page_id': x.get('source_page_id'),
                'bbox': x.get('bbox'), 'text_role': x.get('text_role'),
                'provenance': x.get('parse_provenance'), 'replay_source': x.get('replay_source')} for x in candidates],
            'result_path': str(baseline / 'recognition' / name / 'result.json')})
# Business inputs come only from source sheets and previously reviewed drafts, never answers.
source = business.sources()
dbpath = ROOT / 'runs/overnight-20261029-round2/practice-data/fde.sqlite'
db = sqlite3.connect(f'file:{dbpath}?mode=ro', uri=True)
before = {t: db.execute(f'select count(*) from {t}').fetchone()[0] for t in ['documents', 'review_drafts', 'publications', 'notices', 'audit']}
docs = {n: {'id': i, 'state': s, 'review': json.loads(review) if review else None} for n, i, s, review in db.execute('select name,id,state,review from documents')}
drafts = {n: json.loads(payload) for n, payload in db.execute('select name,payload from documents join review_drafts on id=document_id')}
fs = {n: (d['review'] or drafts[n])['fields'] for n, d in docs.items()}
formulas = {x['document']: x for batch in range(1, 11) for x in json.loads((ROOT / f'runs/overnight-20261029-round3-ui/assessment-practice-ui-final/batch{batch}.json').read_text())['records']}
states = []
for name in names:
    material = material_reference(name, fs)
    matches = business.suggest(fs[name], source)
    weak = [x for x in matches['payments'] if x['match_strength'] == 'weak']
    formula = formulas[name].get('product')
    pub = None
    if docs[name]['state'] == 'published':
        row = db.execute('select payload from publications where parcel_id=(select parcel_id from documents where id=?) order by id desc limit 1', (docs[name]['id'],)).fetchone()
        pub = json.loads(row[0]) if row else None
    dimension = review_dimensions(document_state=docs[name]['state'], unresolved=drafts[name].get('unresolved_fields', []),
                                 formula=formula, weak_rows=weak, material=material, publication=pub)
    states.append({'document': name, 'material_relation': material, 'dimensions': dimension,
                   'strong_candidate_source_rows': [str(x['序号']) for x in matches['payments'] if x['match_strength'] == 'strong'],
                   'weak_candidate_receipt_groups': receipt_groups(weak),
                   'automatic_receipt_assignments': 0, 'new_publications': 0})
assert before == {t: db.execute(f'select count(*) from {t}').fetchone()[0] for t in before}
db.close()
benefits = [x for x in fields if x['nonempty_gold'] and x['strict_state'] != 'correct' and x['format_state'] == 'correct']
assert not any(x['strict_state'] == 'correct' and x['format_state'] != 'correct' for x in fields)
summary = {'batch': args.batch, 'documents': names, 'strict': dict(strict), 'format_only': dict(normalized),
           'denominator': sum(strict.values()), 'strict_fact_gains': 0, 'format_matches_gained': len(benefits),
           'dangerous_regressions': regressions, 'conflict_categories': dict(collections.Counter(x['diagnosis']['category'] for x in fields if x['nonempty_gold'] and x['strict_state'] == 'conflict')),
           'fields': fields, 'business_states': states, 'automatic_receipt_assignments': 0,
           'new_publications': 0, 'database_counts_unchanged': before, 'new_cloud_calls': 0, 'evaluation_read': False}
out.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(json.dumps({k: summary[k] for k in ['batch', 'documents', 'strict', 'format_only', 'denominator', 'strict_fact_gains', 'format_matches_gained', 'conflict_categories', 'dangerous_regressions']}, ensure_ascii=False))
