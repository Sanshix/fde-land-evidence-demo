"""Compare five-document replay reports without altering either scorer or the answers."""

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path


def state(row):
    if row['候选状态'].startswith('withheld'):
        return 'conflict'
    if row['候选状态'] == 'missing':
        return 'missing'
    return 'correct' if row['相同'] else 'wrong'


def bracket_equal(row):
    def canonical(value):
        value = re.sub(r'\s+', '', value).translate(str.maketrans('（）', '()'))
        for left, right in [('(', ')'), ('〔', '〕'), ('[', ']'), ('【', '】')]:
            value = re.sub(re.escape(left) + r'(\d{4})' + re.escape(right), r'〔\1〕', value)
        return value
    return (row['字段'] == '合同编号' and state(row) == 'wrong'
            and canonical(row['唯一候选']) == canonical(row['标准答案']))


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('comparison output must be new')
    reports = [json.loads((p / 'scoring/recognition_score.json').read_text())
               for p in (args.baseline, args.candidate)]
    manifests = [json.loads((p / 'replay_manifest.json').read_text()) for p in (args.baseline, args.candidate)]
    assert manifests[0]['input_sha256'] == manifests[1]['input_sha256']
    assert len(manifests[0]['documents']) == len(manifests[1]['documents']) == 5
    assert reports[0]['answers_sha256'] == reports[1]['answers_sha256']
    rows, migrations, source_metrics = [], Counter(), Counter()
    candidate_results = {name: json.loads((args.candidate / 'recognition' / name / 'result.json').read_text())
                         for name in manifests[1]['documents']}
    fields = {'合同编号': 'contract_no', '承租人': 'tenant', '宗地号': 'parcel_no', '土地面积': 'area',
              '合同类型': 'doc_type', '签订日期': 'signed', '证件号码': 'identity', '联系电话': 'phone',
              '坐落': 'address', '用途': 'use', '租赁起始日': 'start', '租赁终止日': 'end',
              '计租方式': 'mode', '年租金小写': 'annual', '年租金大写': 'annual_upper'}
    for old, new in zip(reports[0]['field_rows'], reports[1]['field_rows'], strict=True):
        assert (old['档案号'], old['字段'], old['计入非空分母']) == (new['档案号'], new['字段'], new['计入非空分母'])
        before, after = state(old), state(new)
        key = fields[new['字段']]
        result = candidate_results[new['档案号']]
        candidates = result['candidates'].get(key, [])
        observed = result.get('candidate_observations', result['candidates']).get(key, [])
        adjudication = result.get('field_adjudication', {}).get(key, {})
        rejected = [o for stage in result['replay_stages']
                    for o in stage['diagnostics'].get('observations', [])
                    if o.get('field') == key and o.get('candidate_status') != 'valid']
        anchors = [{'page': c.get('source_page_id'), 'response': c.get('replay_source', {}).get('response_id'),
                    'locator': c.get('response_locator'), 'raw_value': c.get('raw_value'),
                    'value': c['value'], 'role': c.get('text_role'), 'rule': c.get('parse_provenance', {}).get('rule_version')}
                   for c in observed]
        if new['计入非空分母']:
            migrations[before + '→' + after] += 1
            if after == 'correct':
                source_metrics['correct_fields'] += 1
                source_metrics['correct_fields_with_response_locator'] += any(c.get('response_locator') for c in candidates)
                source_metrics['correct_fields_with_claimed_bbox'] += any(c.get('bbox') for c in candidates)
                source_metrics['correct_fields_with_human_verified_source'] += any(c.get('verified') for c in candidates)
        rows.append({'document': new['档案号'], 'field': new['字段'], 'evaluated': new['计入非空分母'],
                     'before': before, 'after': after, 'migration': before + '→' + after,
                     'before_value': old['唯一候选'], 'after_value': new['唯一候选'], 'expected': new['标准答案'],
                     'before_alternatives': old['冲突候选'], 'after_alternatives': new['冲突候选'],
                     'adjudication_status': adjudication.get('status', ''),
                     'adjudication': json.dumps(adjudication, ensure_ascii=False),
                     'year_brackets_only': bracket_equal(new), 'rejected_observations': json.dumps(rejected, ensure_ascii=False),
                     'candidate_sources': json.dumps(anchors, ensure_ascii=False)})
    metrics = []
    for report in reports:
        evaluated = [r for r in report['field_rows'] if r['计入非空分母']]
        counts = dict(Counter(state(r) for r in evaluated))
        metrics.append({'counts': counts, 'denominator': len(evaluated),
                        'strict': {k: report['field_accuracy'][k] for k in
                                   ('selected_fixed_denominator', 'precision_unique_produced', 'coverage_selected')},
                        'bracket_only_scoring_mismatches': sum(bracket_equal(r) for r in evaluated),
                        'supplementary_syntax_correct': counts.get('correct', 0) + sum(bracket_equal(r) for r in evaluated),
                        'key_all_correct': report['whole_contract']['key_all_correct']})
    dangerous = [r for r in rows if r['evaluated'] and (
        r['before'] == 'correct' and r['after'] != 'correct'
        or r['before'] == 'conflict' and r['after'] == 'wrong')]
    factual_danger = [r for r in dangerous if not r['year_brackets_only']]
    report = {'documents': manifests[1]['documents'], 'baseline': metrics[0], 'candidate': metrics[1],
              'migrations': dict(migrations), 'strict_danger_count': len(dangerous),
              'nonformat_danger_count': len(factual_danger), 'danger_details': dangerous,
              'source_metrics': dict(source_metrics), 'new_external_calls': 0,
              'notes': ['Primary counts use the unchanged frozen scorer; no denominator change.',
                        'Supplementary syntax counts only flag balanced four-digit year bracket variants; they do not replace primary results.',
                        'Response locators and claimed boxes are not human-verified original locations.',
                        'A unique valid candidate can coexist with rejected evidence and cannot authorize publication.']}
    args.output.mkdir(parents=True)
    (args.output / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    write_csv(args.output / 'field-migrations.csv', rows)
    write_csv(args.output / 'document-comparison.csv', [
        {'document': old['name'], 'denominator': old['truth_nonempty'], 'before_correct': old['correct_nonempty'],
         'after_correct': new['correct_nonempty'], 'before_unique': old['covered_nonempty'], 'after_unique': new['covered_nonempty'],
         'before_conflicts': old['withheld_conflicts'], 'after_conflicts': new['withheld_conflicts']}
        for old, new in zip(reports[0]['details'], reports[1]['details'], strict=True)])
    print(json.dumps({k: v for k, v in report.items() if k not in {'danger_details', 'notes'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
