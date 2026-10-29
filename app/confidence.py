"""Versioned, uncalibrated reliability scores. Never probabilities or human truth."""
import hashlib
import json
import time

from .domain import FIELDS
from .evidence import critical_fields, normalized_candidates, observed, reported_score, valid_anchor, comparison_value
from .recognition import assess

VERSION = 'reliability-v1.0'
GROUPS = {
    'calculation': ({'area', 'use', 'grade', 'start', 'end', 'mode', 'annual', 'rate_standard', 'annual_upper'}, 60),
    'identity': ({'tenant', 'lessor', 'parcel_no', 'identity', 'doc_type', 'original_contract_no'}, 30),
    'auxiliary': (set(FIELDS), 10),
}
SEVERE = {'disagreement', 'cross_field_conflict', 'invalid_format', 'visual_ambiguity',
          'checkbox_unresolved', 'text_revision_review', 'text_quote_unsupported', 'business_conflict', 'recognition_incomplete'}


def level(score):
    return 'high' if score >= 95 else 'medium' if score >= 80 else 'low'


def machine(doc, blockers=()):
    candidates = normalized_candidates((doc.get('result') or {}).get('candidates', {}))
    units = {p['id']: p for p in doc.get('pages', [])}
    critical = critical_fields(candidates, batch=True)
    # Contract numbers are optional; area/use remain mandatory source facts for new/renewed contracts.
    critical.discard('contract_no')
    risks = [*(doc.get('result') or {}).get('risks', []), *assess(candidates)]
    applicable = critical | {k for k, rows in candidates.items() if rows and k in FIELDS}
    fields = {}
    for key in sorted(applicable):
        rows = candidates.get(key, [])
        observations = [c for c in rows if observed(c, units)]
        anchors = [c for c in observations if valid_anchor(c, units)]
        values = {c['value'] for c in observations}
        native = any(c.get('kind') == 'native_extract' for c in anchors)
        scores = [reported_score(c) for c in (anchors or observations) if c.get('kind') != 'native_extract']
        unknown = not native and (not scores or any(s is None for s in scores))
        paths = len({(c.get('provider'), c.get('model')) for c in observations})
        relevant = [r for r in risks if r.get('field') == key]
        reasons = []
        # No score is imputed for an unreported model score. Native parsing is a separate signal.
        reliability = 100 if native else min((s * 100 for s in scores if s is not None), default=0)
        components = {'recognition': round(reliability, 2), 'traceability': 100 if anchors else 0,
                      'consistency': 100 if len(values) == 1 and (native or paths >= 2) else 50 if len(values) == 1 else 0,
                      'validation': 0 if any(r.get('code') in {'invalid_format', 'cross_field_conflict'} for r in relevant) else 100}
        score = sum(components[k] * w for k, w in [('recognition', .65), ('traceability', .20), ('consistency', .10), ('validation', .05)])
        if not observations:
            score = 0; reasons.append('缺少原文观察；上下文推断不能补事实')
        if not anchors:
            score = min(score, 89); reasons.append('缺少有效原文定位，不能评为高')
        if unknown:
            score = min(score, 89); reasons.append('识别分数未知，不能评为高')
        if native:
            reasons.append('原生结构可定位提取；不是模型正确概率')
        elif scores and not unknown:
            reasons.append('识别分数取有效观察中的最低值')
        if len(values) > 1:
            score = min(score, 49); reasons.append('原文候选冲突未解决')
        if paths < 2 and not native and observations:
            reasons.append('仅单一路径，缺少独立佐证')
        quality = any(r.get('code') == 'quality' and r.get('field') in {None, key} for r in risks)
        if quality:
            score = min(score, 69); reasons.append('原件清晰度存在疑点')
        if any(r.get('code') in SEVERE for r in relevant) or any(c.get('text_role') in {'crossed_out', 'alternative'} for c in rows):
            score = min(score, 49); reasons.append('格式、条款或涂改依据需人工核验')
        reasons += [r.get('reason', r.get('code', '')) for r in relevant if r.get('code') != 'visual_review']
        score = round(score, 2)
        fields[key] = {'value': next(iter(values)) if len(values) == 1 else None,
                       'candidates': rows, 'score': score, 'level': 'high' if score >= 90 else 'medium' if score >= 70 else 'low', 'critical': key in critical,
                       'score_unknown': unknown, 'unscored_corroboration': sum(reported_score(c) is None for c in observations if c not in anchors), 'components': components, 'reasons': list(dict.fromkeys(reasons)),
                       'anchors': [dict(unit_id=c['source_page_id'], bbox=c.get('bbox'), locator=c.get('source_locator')) for c in anchors],
                       'human_status': 'unverified'}
    used = set()
    active_groups = []
    for name, (keys, weight) in GROUPS.items():
        keys = (keys & fields.keys()) - used
        if keys:
            active_groups.append((name, keys, weight)); used |= keys
    total_weight = sum(w for _, _, w in active_groups)
    for name, keys, group_weight in active_groups:
        for key in keys:
            fields[key]['weight'] = group_weight / total_weight / len(keys) if total_weight else 0
            fields[key]['group'] = name
    overall = round(sum(f['score'] * f.get('weight', 0) for f in fields.values()), 2)
    minimum = min((dict(key=k, label=FIELDS[k], score=fields[k]['score']) for k in critical), key=lambda f: (f['score'], f['key']), default=None)
    rank = level(overall)
    if minimum and minimum['score'] < 70:
        rank = 'low'
    elif minimum and minimum['score'] < 90 and rank == 'high':
        rank = 'medium'
    result = {'version': VERSION, 'calibrated': False, 'score': overall, 'level': rank,
              'fields': fields, 'minimum_critical': minimum,
              'note': '规则评分，尚未校准为实际正确概率；人工核验不会修改原始评分'}
    return restrict(result, blockers)


def restrict(profile, blockers):
    # Copy instead of mutating the persisted machine profile with current business facts.
    result = {**profile, 'blockers': list(blockers)}
    if any(b.get('code') in SEVERE for b in blockers):
        result['level'] = 'low'
    elif blockers and result['level'] == 'high':
        result['level'] = 'medium'
    result['reasons'] = list(dict.fromkeys(b.get('message', b.get('reason', b.get('code', ''))) for b in blockers))
    field_pending = [k for k, f in result['fields'].items() if f['score'] < 90 or f['score_unknown']]
    result['pending_fields'] = field_pending
    result['pending_count'] = len(set(field_pending) | {b['field'] for b in blockers if b.get('field')}) + len({b.get('code') for b in blockers if not b.get('field') and b.get('code') != 'confidence_gate'})
    result['batch_eligible'] = result['level'] == 'high' and not blockers
    return result


def fingerprint(doc):
    payload = {'result': doc.get('result'), 'pages': doc.get('pages'), 'intake': doc.get('intake'), 'version': VERSION}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def persist(db, doc, profile):
    digest = fingerprint(doc)
    profile = {**profile, 'source_digest': digest}
    db.execute('INSERT OR IGNORE INTO machine_assessments(document_id,digest,rule_version,payload,created) VALUES(?,?,?,?,?)',
               (doc['id'], digest, VERSION, json.dumps(profile, ensure_ascii=False), time.time()))
    return json.loads(db.execute('SELECT payload FROM machine_assessments WHERE document_id=? AND digest=?', (doc['id'], digest)).fetchone()['payload'])


def verification(profile, fields):
    records = {}
    for key in sorted(set(profile['fields']) | set(fields)):
        info = profile['fields'].get(key, {})
        original, accepted = info.get('value'), fields.get(key, '')
        if original is None and not accepted:
            continue
        status = ('added' if not info.get('candidates') else
                  'verified' if original is not None and comparison_value(key, accepted) == original else 'corrected')
        records[key] = {'original_value': original, 'confirmed_value': accepted,
                        'status': status, 'original_score': info.get('score')}
    return records
