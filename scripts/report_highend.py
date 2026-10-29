"""Offline reporting after the complete model comparison is frozen."""
import csv
import hashlib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.score_recognition import evaluate, write_report
from app.observation_audit import audit

RUN = ROOT / 'runs/highend-models-20261029'
MODELS = ['qwen3.5-27b', 'qwen3.7-plus', 'qwen3-vl-plus']
PRICES = {'qwen3.5-27b': (0.6, 4.8), 'qwen3.7-plus': (1.6, 6.4), 'qwen3-vl-plus': (1, 10)}
PRICE_URL = 'https://help.aliyun.com/zh/model-studio/model-pricing'


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def csv_write(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    assert (RUN / 'inference-complete.json').exists(), 'Wait for all inference before opening answers'
    manifest = json.loads((RUN / 'manifest.json').read_text())
    scores, summary, outcomes, observations = {}, {}, {}, []
    for model in MODELS:
        folder = RUN / model
        score = evaluate(folder, ROOT.parent / '练习集/08_标准答案')
        write_report(score, folder / 'scoring')
        scores[model] = score
        rows = [json.loads((folder / 'recognition' / d['name'] / 'outcome.json').read_text())
                for d in manifest['contracts']]
        outcomes[model] = {r['name']: r for r in rows}
        usages = [r['stages'][0].get('usage') or {} for r in rows]
        cost = sum((u.get('prompt_tokens', 0) * PRICES[model][0]
                    + u.get('completion_tokens', 0) * PRICES[model][1]) / 1e6 for u in usages)
        n = len(rows)
        metric = score['field_accuracy']['selected_fixed_denominator']
        summary[model] = {
            'n': n, 'pages': sum(r['pages'] for r in rows), 'field_accuracy': metric,
            'score_10': round(10 * metric['accuracy'], 2),
            'precision_unique_produced': score['field_accuracy']['precision_unique_produced'],
            'coverage': score['field_accuracy']['coverage_selected'],
            'unexpected_nonempty': score['field_accuracy']['unexpected_nonempty'],
            'whole_contract': score['whole_contract'], 'completion_states': score['completion_states'],
            'mean_review_seconds': statistics.mean(r['seconds'] for r in rows),
            'median_review_seconds': statistics.median(r['seconds'] for r in rows),
            'max_review_seconds': max(r['seconds'] for r in rows),
            'prompt_tokens': sum(u.get('prompt_tokens', 0) for u in usages),
            'completion_tokens': sum(u.get('completion_tokens', 0) for u in usages),
            'usage_missing_calls': sum(not u for u in usages),
            'estimated_review_yuan': cost, 'estimated_review_yuan_per_contract': cost / n,
            'price_input_output_per_million': PRICES[model],
            'loss_statuses': {s: sum(r['计入非空分母'] and not r['相同'] and r['候选状态'] == s
                for r in score['field_rows']) for s in sorted({r['候选状态'] for r in score['field_rows']})}}
        for d in manifest['contracts']:
            path = folder / 'recognition' / d['name'] / 'qwen-response.json'
            if path.exists():
                response = json.loads(path.read_text())
                observations.append({'name': d['name'], 'model': model,
                    'returned_model': response.get('returned_model'),
                    'rejected': response.get('rejected'), 'warnings': response.get('warnings'),
                    'alteration_audit': audit(response)})
    baseline = {(r['档案号'], r['字段']): r for r in scores[MODELS[0]]['field_rows']}
    transitions = []
    for model in MODELS[1:]:
        gains, regressions = [], []
        for row in scores[model]['field_rows']:
            key = (row['档案号'], row['字段'])
            old = baseline[key]
            if row['相同'] != old['相同']:
                change = {'model': model, '档案号': key[0], '字段': key[1],
                          'change': 'gain' if row['相同'] else 'regression',
                          '计入非空分母': row['计入非空分母'],
                          'baseline_status': old['候选状态'], 'new_status': row['候选状态'],
                          'baseline_value': old['唯一候选'], 'new_value': row['唯一候选'],
                          'gold': row['标准答案']}
                transitions.append(change)
                if row['计入非空分母']:
                    (gains if row['相同'] else regressions).append(change)
        summary[model].update(gains=len(gains), regressions=len(regressions),
                             net_fields=len(gains) - len(regressions))
    paired = []
    for d in manifest['contracts']:
        row = {'档案号': d['name'], '页数': len(d['files'])}
        for model in MODELS:
            detail = next(x for x in scores[model]['details'] if x['name'] == d['name'])
            row['分母'] = detail['truth_nonempty']
            row[model + '正确'] = detail['correct_nonempty']
            row[model + '秒'] = outcomes[model][d['name']]['seconds']
        paired.append(row)
    # Byte-equivalent messages within each contract; only model identity changes.
    input_checks = {}
    for d in manifest['contracts']:
        hashes = []
        for model in MODELS:
            request = json.loads((RUN / model / 'recognition' / d['name'] / 'qwen-request.json').read_text())
            request.pop('model')
            hashes.append(hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest())
        input_checks[d['name']] = {'same_request_except_model': len(set(hashes)) == 1, 'sha256': hashes}
    assert all(c['same_request_except_model'] for c in input_checks.values())
    save(RUN / 'input-equivalence.json', input_checks)
    save(RUN / 'comparison.json', {'summary': summary, 'paired': paired, 'transitions': transitions})
    save(RUN / 'observation-audit.json', observations)
    csv_write(RUN / '逐合同对比.csv', paired)
    if transitions:
        csv_write(RUN / '字段得失对比.csv', transitions)

    lines = ['# 高端模型同口径测试报告', '', '日期：2026-10-29。', '',
        '三模型同测LX-001—LX-010；逐份使用完全相同的原图、冻结OCR全文及当前生产提示词。'
        '只替换方案3的第二阶段模型：27B、Qwen3.7-Plus、Qwen3-VL-Plus。'
        '每份一次，总并发3，temperature=0、思考预算1024、最大输出12000；没有失败重试、备用模型或按答案选候选。', '',
        '评分为现有固定非空金标字段正确率×10，采用10分制；表中为原始值，不额外主观加权。'
        '冲突、涂改保留和缺失均不给匹配分；失败仍计入分母。这个分数评价“正确且可唯一输出的字段覆盖”，'
        '并不等于纯视觉识别能力、整份可发布率或比赛业务总分。', '',
        '| 指标 | 27B同轮基线 | Qwen3.7-Plus | Qwen3-VL-Plus |', '|---|---:|---:|---:|']
    def table_row(label, values):
        lines.append('| ' + label + ' | ' + ' | '.join(values) + ' |')
    table_row('评分 / 10', [f"{summary[m]['score_10']:.2f}" for m in MODELS])
    table_row('字段准确率', [f"{summary[m]['field_accuracy']['correct']}/{summary[m]['field_accuracy']['denominator']}（{summary[m]['field_accuracy']['accuracy']:.2%}）" for m in MODELS])
    table_row('关键12字段全对', [f"{summary[m]['whole_contract']['key_all_correct']}/10" for m in MODELS])
    table_row('全部15字段全对', [f"{summary[m]['whole_contract']['all_15_correct']}/10" for m in MODELS])
    table_row('唯一候选精度', [f"{summary[m]['precision_unique_produced']['correct']}/{summary[m]['precision_unique_produced']['denominator']}（{summary[m]['precision_unique_produced']['accuracy']:.2%}）" for m in MODELS])
    table_row('空答案字段额外输出', [str(summary[m]['unexpected_nonempty']) for m in MODELS])
    for label, key in [('平均复核秒/合同', 'mean_review_seconds'), ('中位复核秒/合同', 'median_review_seconds'), ('最慢复核秒/合同', 'max_review_seconds')]:
        table_row(label, [f'{summary[m][key]:.2f}' for m in MODELS])
    table_row('输入 / 输出Token', [f"{summary[m]['prompt_tokens']:,} / {summary[m]['completion_tokens']:,}" for m in MODELS])
    table_row('复核估算元/合同', [f"{summary[m]['estimated_review_yuan_per_contract']:.4f}" for m in MODELS])
    table_row('10份复核估算元', [f"{summary[m]['estimated_review_yuan']:.4f}" for m in MODELS])
    table_row('协议终态', [str(summary[m]['completion_states']) for m in MODELS])
    lines += ['', '## 相对同轮27B的得失', '', '| 模型 | 新增正确字段 | 原正确退化 | 净变化 | 准确率差（百分点） |', '|---|---:|---:|---:|---:|']
    for m in MODELS[1:]:
        s = summary[m]
        lines.append(f"| {m} | {s['gains']} | {s['regressions']} | {s['net_fields']:+d} | {(s['field_accuracy']['accuracy']-summary[MODELS[0]]['field_accuracy']['accuracy'])*100:+.2f} |")
    lines += ['', '字段迁移原值、候选状态与标准答案见[字段得失对比](../runs/highend-models-20261029/字段得失对比.csv)。'
              '涂改前后均保留而被评分器暂缓的字段，不能直接判为OCR读错；须结合下方证据审查。', '',
              '## 逐合同', '', '| 档案 | 页数 | 分母 | 27B正确 | 3.7正确 | VL正确 |', '|---|---:|---:|---:|---:|---:|']
    for r in paired:
        lines.append('| ' + ' | '.join(str(r[k]) for k in ['档案号', '页数', '分母', *[m+'正确' for m in MODELS]]) + ' |')
    lines += ['', '## 关键失分与风险', '',
        '**3.7-Plus净少3个可唯一输出的正确字段，但原因不能统称为读错。** '
        'LX-001的3998.25因模型同时保留旧值4018.25而被暂缓；原图确有划改，3.7的证据比只保留新值的27B完整。'
        'LX-004大写金额的raw_text正确写有“壹仟叁佰捌拾陆元柒角捌分”，结构化value却漏掉“仟”；'
        '这是同一响应内原文与字段值不一致，不能靠更大模型自动消除。'
        'LX-008宗地号的原始value与答案相同，但它把第二张输入图标成page=3，因本请求只有两张图而拒收；'
        '这是输入顺序页号与材料自身页码的协议错位，不能当作没认出宗地号。三项均未修补后重计分。', '',
        '**VL-Plus新增1项、退化2项，另多输出2个金标为空的签订日期。** '
        '收益是LX-008合同编号括号恢复为〔〕；损失是LX-001宗地号8-3-176读成8.3-176，'
        '并将已划掉的4018.25作为current值。原图可见划改后的3998.25，故这项存在实质金额风险。'
        'LX-005和LX-008的签订日期输出与空金标不符，计入额外输出；未进一步逐页核实其来源，'
        '不将这两项直接断言为“编造”。', '',
        '协议完整完成分别为27B 7/10、3.7-Plus 9/10、VL-Plus 2/10；三者API均10/10收到响应且没有调用异常。'
        '“部分完成”包含附加字段页号无效、转写页标记错误等情况，不代表每个应评字段都错。'
        '原始拒收记录分别为27B 15条unknown_page_number及2条转写校验记录；'
        '3.7为2条unknown_page_number及2条转写校验记录；VL为41条unknown_page_number及4条转写校验记录。'
        '这些是记录条数，不是互不重复的错误字段数。', '',
        '27B的8项失分中，6项是合同编号括号差异，1项是LX-007宗地号少一个前导零，1项是LX-007面积涂改暂缓。'
        '更换模型没有解决共同的编号字形与前导零问题；这些仍须保留原文、定位及复核。']
    lines += ['', '## 与此前方案的关系', '',
        '此前方案1/2在相同前10份合同上的冻结输出，以及旧方案3前5份，均在下面离线重评分。'
        '它们的提示词和OCR版本与本轮不同，只能作为历史参考；本轮三个模型之间才是模型变量对照。', '',
        '| 历史方案 | 样本 | 正确字段 | 评分 / 10 | 全15字段全对 |', '|---|---:|---:|---:|---:|']
    historical = {}
    for label, folder in [('方案1：27B原图', ROOT/'runs/paired-qwen-ocr-10-20261029/plan1'),
                          ('方案2：OCR→纯文本27B', ROOT/'runs/paired-qwen-ocr-10-20261029/plan2'),
                          ('旧方案3：OCR→原图+全文27B', ROOT/'runs/plan3-five-20261029/plan3')]:
        score = evaluate(folder, ROOT.parent/'练习集/08_标准答案')
        historical[label] = score
        m = score['field_accuracy']['selected_fixed_denominator']
        n = score['documents']['selected']
        lines.append(f"| {label} | {n} | {m['correct']}/{m['denominator']}（{m['accuracy']:.2%}） | {10*m['accuracy']:.2f} | {score['whole_contract']['all_15_correct']}/{n} |")
    save(RUN/'historical-rescored.json', historical)
    lines += ['', '## 成本与证据边界', '',
        '本轮耗时仅包含第二阶段复核，三个模型均排除相同的缓存OCR耗时；不能直接与历史端到端秒数比较。'
        '原始OCR耗时和费用是三条链共同项，重用不代表上线时免费。', '',
        f'按[阿里云官方北京价格]({PRICE_URL})于2026-10-29核对：'
        '27B输入/输出0.6/4.8元每百万Token；3.7-Plus滚动别名限时8折为1.6/6.4；VL-Plus为1/10。'
        '本轮所有输入均低于32K，输出包含思考Token。估算不扣免费额度、缓存或账号折扣，实际账单未取得。'
        '正式测试30次复核与3次合成图探针分别留存usage；表中费用不含探针和此前OCR。', '',
        '该批是已使用过答案诊断的开发回归集，每模型每份仅一次，10份合同不足以证明泛化或稳定胜出。'
        '字段相关，不能把每个字段当独立样本计算显著性；也没有逐字全文金标，因此未报告字符级准确率。'
        '只在全部推理结束、inference-complete落盘后，离线评分器才读取标准答案；未改答案或评分器。', '',
        '两款Plus作为商业API接入，不声称开源权重或本地离线交付。系统管理新增可选项，默认仍为27B方案3；'
        '原有授权范围、版本冲突、审计、原图与全文双输入、转写版本、人工复核门禁保留。', '',
        '证据目录：`runs/highend-models-20261029/`。包括模型清单、合成图探针、源代码快照与摘要、'
        '原图摘要、冻结OCR、实际请求、原始协议响应、候选、终态、usage、逐字段评分和输入等价校验。']
    base_stats = summary[MODELS[0]]
    tradeoffs = []
    for model in MODELS[1:]:
        s = summary[model]
        tradeoffs.append(f"{model}平均多{s['mean_review_seconds']-base_stats['mean_review_seconds']:.2f}秒"
                         f"（+{(s['mean_review_seconds']/base_stats['mean_review_seconds']-1)*100:.1f}%），"
                         f"每合同复核估算多{s['estimated_review_yuan_per_contract']-base_stats['estimated_review_yuan_per_contract']:.4f}元。")
    lines[4:4] = [
        '**本轮没有观察到换高端模型带来更高的可用字段准确率。** '
        '27B为9.43分，3.7-Plus为9.22分，VL-Plus为9.36分；全部15字段全对分别4/10、3/10、2/10。'
        '建议保留27B作为默认，3.7-Plus保留为涂改材料的人工复核备选；本轮不支持将VL-Plus替换为默认。'
        '这只适用于当前10份开发样本、单次生成和1024思考预算，不能推断所有场景的模型能力排序。', '',
        ''.join(tradeoffs) + '绝对Token费用差很小，当前更主要的代价是更长等待与字段/证据返工；'
        '尚未实测人工节时，不能据此声称高端方案有更高业务收益。', '']
    lines += ['', '## 接入与后续选择', '',
        '系统管理可选“高端方案：OCR → Qwen3.7-Plus原图＋全文复核”和对应VL-Plus方案。'
        '工作流ID分别为plan3-qwen3.7-plus、plan3-qwen3-vl-plus；没有将商业模型标为Apache-2.0开源权重。'
        '代码已接入并在隔离服务验证，未重启原工作服务或改写其管理员选择；加载新后端需重启原服务。', '',
        '工程回归371项通过（11.94秒）；高端模型专项5项通过，包括流式双阶段、原图＋OCR输入、'
        '转写版本、材料外发范围、权限和配置持久化。浏览器验证了选项可见、键盘选择及无控制台错误，'
        '并修复长模型名导致的窄屏溢出。这些工程检查不计入模型准确率分数。', '',
        '下一轮更值得先验证页号协议和字段值与原文的一致性约束，再对未参与调参的样本重复对照；'
        '提示词应明确区分输入图片序号与纸面页码，发现value和raw_text不一致时留待复核，'
        '保留前导零与编号字形的原文信息。改规则后必须三模型一起重测，不能只修Plus并与旧27B成绩比较。']
    report = ROOT/'docs/高端模型精准度对比报告-2026-10-29.md'
    report.write_text('\n'.join(lines)+'\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
