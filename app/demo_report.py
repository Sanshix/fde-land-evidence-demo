"""Reports score machine candidates after inference; never expose raw evidence or secrets."""
import collections
import hashlib
import json
import re
import time
from pathlib import Path

from . import store
from .value_views import comparison_view


def build(run, folder, items):
    folder = Path(folder)
    inputs = folder / 'report-inputs'
    inputs.mkdir(exist_ok=True)
    (inputs / 'documents.json').write_text(json.dumps(run['manifest']['documents'], ensure_ascii=False))
    statuses, problems, field_states = collections.Counter(), collections.Counter(), collections.Counter()
    usage = {'new_prompt_tokens': 0, 'new_completion_tokens': 0, 'missing_usage_documents': 0, 'historical_prompt_tokens': 0, 'historical_completion_tokens': 0}
    pages_actual = files_actual = unique_imports = external_calls = 0
    seen_documents = set()
    document_rows = []
    with store.connection() as db:
        for item in items:
            document = store.get_doc(item.get('document_id')) if item.get('document_id') else None
            result = (document or {}).get('result') or {}
            job = db.execute('SELECT state,error FROM jobs WHERE document_id=? ORDER BY created DESC LIMIT 1',
                             (item.get('document_id'),)).fetchone() if document else None
            status = (('import_failed' if item.get('error') else 'not_run') if not document else job['state'] if job else 'not_run')
            status = {'done': 'completed', 'interrupted': 'unknown', 'queued': 'not_run', 'running': 'unknown',
                      'fallback_queued': 'not_run', 'fallback_running': 'unknown'}.get(status, status)
            statuses[status] += 1
            if document and document['id'] not in seen_documents:
                seen_documents.add(document['id'])
                unique_imports += not item.get('duplicate', False)
                pages_actual += len(document['pages'])
                files_actual += len((document.get('intake') or {}).get('files', []))
            for risk in result.get('risks', []):
                problems[risk.get('code', 'unknown')] += 1
            from scripts.score_recognition import unique_candidate
            for key, candidates in result.get('candidates', {}).items():
                field_states[unique_candidate(key, candidates)[1]] += 1
            external_calls += result.get('external_calls', 0)
            if result:
                usages = [stage.get('usage') for p in result.get('pages', []) for stage in p.get('stages', [])]
                if result.get('primary', {}).get('usage'):
                    usages.append(result['primary']['usage'])
                if not any(usages):
                    usage['missing_usage_documents'] += 1
                for u in usages:
                    if u:
                        prefix = 'new' if run['mode'] == 'live' else 'historical'
                        usage[prefix + '_prompt_tokens'] += u.get('prompt_tokens', 0)
                        usage[prefix + '_completion_tokens'] += u.get('completion_tokens', 0)
            dest = inputs / 'recognition' / item['name']
            dest.mkdir(parents=True, exist_ok=True)
            (dest / 'result.json').write_text(json.dumps(result, ensure_ascii=False))
            (dest / 'outcome.json').write_text(json.dumps({'completion_status': status, 'published': False}, ensure_ascii=False))
            document_rows.append({'name': item['name'], 'document_id': item.get('document_id'), 'status': status,
                                  'error': re.sub(r'(?i)(access_token|api_key|authorization|token|secret)([=: ]+)[^&\s]+', r'\1\2[redacted]', str(item.get('error') or (job['error'] if job else '') or ''))})
    report = {'run_id': run['id'], 'dataset': run['dataset'], 'mode': run['mode'], 'state': run['state'],
              'test_only': True, 'new_ocr': external_calls > 0, 'external_calls_new': external_calls, 'created': run['created'],
              'elapsed_seconds': round(time.time() - run['created'], 2),
              'expected': {k: run['manifest'][k] for k in ['documents_expected', 'files_expected', 'pages_expected']},
              'actual': {'documents_imported': sum(bool(i.get('document_id')) for i in items),
                         'unique_documents_imported': unique_imports, 'files_imported': files_actual,
                         'pages_imported': pages_actual, 'duplicate_imports': sum(bool(i.get('duplicate')) for i in items),
                         'recognition_states': dict(statuses), 'pending_verification': sum(store.get_doc(i)['state'] != 'published' for i in seen_documents),
                         'published': sum(store.get_doc(i)['state'] == 'published' for i in seen_documents)},
              'completion_rate': statuses['completed'] / run['manifest']['documents_expected'],
              'problems': dict(problems), 'candidate_states': dict(field_states),
              'version': run['manifest']['version'], 'source_manifest_sha256': hashlib.sha256(store.dumps({'scans': run['manifest']['source_hashes'], 'auxiliary': run['manifest'].get('auxiliary_hashes', {})}).encode()).hexdigest(), 'batches': run.get('batches', []), 'usage': usage,
              'scoring': {'available': False, 'reason': '评测集无官方金标，准确率和答案评分为N/A'},
              'documents': document_rows, 'notice': '识别结束不等于核验、合同有效或业务发布。未决内容不自动放行。'}
    try:
        if run['dataset'] == 'practice':
            answers = Path(run['manifest']['samples']) / '08_标准答案'
            if (answers / '合同要素.csv').is_file():
                expected_hash = run['manifest'].get('auxiliary_hashes', {}).get('08_标准答案/合同要素.csv')
                if expected_hash and hashlib.sha256((answers / '合同要素.csv').read_bytes()).hexdigest() != expected_hash:
                    raise ValueError('答案在运行期间变化，未混用版本评分')
                from scripts.score_recognition import LABEL_IDS, evaluate, safe_compare, unique_candidate
                score = evaluate(inputs, answers)
                if score['documents']['selected_with_answers'] != run['manifest']['documents_expected']:
                    raise ValueError('金标档案覆盖不完整，不缩小分母评分')
                normalized_correct = 0
                for row in score['field_rows']:
                    if not row['计入非空分母']:
                        continue
                    key = LABEL_IDS[row['字段']]
                    data = json.loads((inputs / 'recognition' / row['档案号'] / 'result.json').read_text())
                    candidates = [{**c, 'value': comparison_view(key, c['value'])['score_comparison_value']}
                                  for c in data.get('candidates', {}).get(key, [])]
                    value, _, _ = unique_candidate(key, candidates)
                    expected = comparison_view(key, row['标准答案'])['score_comparison_value']
                    normalized_correct += value is not None and safe_compare(row['字段'], value) == safe_compare(row['字段'], expected)
                accuracy = score['field_accuracy']
                fixed = accuracy['selected_fixed_denominator']
                report['scoring'] = {'available': True, 'type': '系统字段自测，不是官方竞赛综合分',
                    'strict': fixed, 'precision': accuracy['precision_unique_produced'],
                    'coverage': accuracy['coverage_selected'], 'unexpected_nonempty': accuracy['unexpected_nonempty'],
                    'whole_contract': score['whole_contract'], 'fields': accuracy['per_field'],
                    'score_100': round(100 * fixed['correct'] / fixed['denominator'], 2) if fixed['denominator'] else None,
                    'maximum': 100, 'formula': '100 × 正确唯一字段数 / 本次清单固定非空金标字段数（失败和缺失仍占分母）',
                    'normalized_format': {'correct': normalized_correct, 'denominator': fixed['denominator'],
                                          'notice': '格式比较单列，不是新增OCR读对，也不改变严格评分'}}
            else:
                report['scoring'] = {'available': False, 'reason': '练习答案未配置，评分N/A；未填预置分数'}
    except (ValueError, KeyError, OSError, TypeError) as exc:
        report['scoring'] = {'available': False, 'reason': '评分失败（' + type(exc).__name__ + '）；处理结果仍保留，未填预置分数'}
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (folder / 'report.md').write_text(markdown(report))
    return report


def markdown(r):
    a, s = r['actual'], r['scoring']
    lines = ['# 合同测试演示报告', '', f"运行：{r['run_id']}；数据集：{r['dataset']}；模式：{'历史缓存回放（非新OCR）' if r['mode']=='replay' else '实时识别'}", '',
             f"应处理 {r['expected']['documents_expected']} 份 / {r['expected']['pages_expected']} 页；导入 {a['documents_imported']} 份 / {a['pages_imported']} 页；唯一文档 {a['unique_documents_imported']}；重复 {a['duplicate_imports']}。",
             f"识别终态：{json.dumps(a['recognition_states'], ensure_ascii=False)}；完成率 {r['completion_rate']:.2%}；待核验 {a['pending_verification']}；发布 {a['published']}。", '',
             f"耗时 {r['elapsed_seconds']} 秒。{r['notice']}", '', '## 评分']
    if s['available']:
        lines += [f"严格正确 {s['strict']['correct']}/{s['strict']['denominator']}；系统自测 {s['score_100']}/100。", s['formula'],
                  f"唯一输出精度：{json.dumps(s['precision'], ensure_ascii=False)}；覆盖率：{json.dumps(s['coverage'], ensure_ascii=False)}。",
                  f"格式比较：{s['normalized_format']['correct']}/{s['normalized_format']['denominator']}；不计作OCR收益。", s['type']]
    else:
        lines += ['准确率、答案评分：N/A。' + s['reason']]
    lines += ['', '## 版本及问题', json.dumps(r['version'], ensure_ascii=False),
              '问题分布：' + json.dumps(r['problems'], ensure_ascii=False), '', '## 文档状态', '| 文档 | 状态 | 失败原因 |', '|---|---|---|']
    for d in r['documents']:
        lines.append(f"| {d['name']} | {d['status']} | {str(d.get('error') or '').replace('|', '/')} |")
    return '\n'.join(lines) + '\n'
