"""Offline paired first-stage OCR report. Never used during inference."""
import csv
import difflib
import hashlib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT/'runs/vl-firststage-20261029'
MODELS = ['qwen-vl-ocr-2025-11-20', 'qwen3-vl-plus']
PRICES = {MODELS[0]: (.3, .5), MODELS[1]: (1, 10), 'qwen3.5-27b': (.6, 4.8)}
sys.path.insert(0, str(ROOT))
from scripts.score_recognition import evaluate, write_report


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def cost(usage, model):
    return ((usage or {}).get('prompt_tokens', 0)*PRICES[model][0]
            + (usage or {}).get('completion_tokens', 0)*PRICES[model][1])/1e6


def csv_write(path, rows):
    if not rows:
        return
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    assert (RUN/'inference-complete.json').exists(), 'Wait for all inference before scoring'
    manifest = json.loads((RUN/'manifest.json').read_text())
    summary, scores, ocr_scores, outcomes = {}, {}, {}, {}
    for model in MODELS:
        folder = RUN/model
        score = evaluate(folder, ROOT.parent/'练习集/08_标准答案')
        ocr_score = evaluate(RUN/(model+'-ocr-only'), ROOT.parent/'练习集/08_标准答案')
        write_report(score, folder/'scoring')
        write_report(ocr_score, RUN/(model+'-ocr-only')/'scoring')
        scores[model], ocr_scores[model] = score, ocr_score
        rows = [json.loads((folder/'recognition'/d['name']/'outcome.json').read_text()) for d in manifest['contracts']]
        outcomes[model] = {r['name']: r for r in rows}
        ocr_stages = [s for r in rows for s in r['stages'] if s['stage'] == 'ocr']
        review_stages = [s for r in rows for s in r['stages'] if s['stage'] == 'review']
        metric = score['field_accuracy']['selected_fixed_denominator']
        summary[model] = {'accuracy': metric, 'score_10': 10*metric['accuracy'],
            'whole_contract': score['whole_contract'], 'completion_states': score['completion_states'],
            'precision': score['field_accuracy']['precision_unique_produced'],
            'unexpected_nonempty': score['field_accuracy']['unexpected_nonempty'],
            'mean_total_seconds': statistics.mean(r['seconds'] for r in rows),
            'mean_ocr_seconds_per_contract': sum(s['seconds'] for s in ocr_stages)/len(rows),
            'mean_review_seconds_per_contract': sum(s['seconds'] for s in review_stages)/len(rows),
            'mean_ocr_seconds_per_page': statistics.mean(s['seconds'] for s in ocr_stages),
            'ocr_cost_yuan': sum(cost(s.get('usage'), model) for s in ocr_stages),
            'review_cost_yuan': sum(cost(s.get('usage'), 'qwen3.5-27b') for s in review_stages),
            'ocr_calls': len(ocr_stages), 'review_calls': len(review_stages),
            'missing_usage': sum(not s.get('usage') for r in rows for s in r['stages']),
            'ocr_parse_diagnostic': ocr_score['field_accuracy']['selected_fixed_denominator']}
        summary[model]['total_cost_yuan'] = summary[model]['ocr_cost_yuan']+summary[model]['review_cost_yuan']
    migrations = []
    for label, collection in [('final', scores), ('ocr_parse_diagnostic', ocr_scores)]:
        old_rows = {(r['档案号'], r['字段']): r for r in collection[MODELS[0]]['field_rows']}
        for row in collection[MODELS[1]]['field_rows']:
            old = old_rows[(row['档案号'], row['字段'])]
            if row['相同'] != old['相同']:
                migrations.append({'stage': label, '档案号': row['档案号'], '字段': row['字段'],
                    'change': 'gain' if row['相同'] else 'regression', 'nonempty_gold': row['计入非空分母'],
                    'baseline_status': old['候选状态'], 'new_status': row['候选状态'],
                    'baseline_value': old['唯一候选'], 'new_value': row['唯一候选'], 'gold': row['标准答案']})
    csv_write(RUN/'字段得失对比.csv', migrations)
    page_diffs, paired, checks = [], [], []
    def no_ocr_text(body):
        body = json.loads(json.dumps(body))
        for message in body['messages']:
            message['content'] = [p for p in message['content']
                                  if 'untrusted_ocr_text' not in p.get('text', '')]
        return body
    for d in manifest['contracts']:
        name = d['name']
        row = {'档案号': name, '页数': len(d['files'])}
        for model in MODELS:
            detail = next(r for r in scores[model]['details'] if r['name'] == name)
            row[model+'正确'] = detail['correct_nonempty']
            row['分母'] = detail['truth_nonempty']
            row[model+'秒'] = outcomes[model][name]['seconds']
        paired.append(row)
        reqs = [json.loads((RUN/m/'recognition'/name/'review-request.json').read_text()) for m in MODELS]
        if len(reqs) == 2:
            assert no_ocr_text(reqs[0]) == no_ocr_text(reqs[1])
        for i in range(1, len(d['files'])+1):
            records = [json.loads((RUN/m/'recognition'/name/f'{name}-{i}-ocr-response.json').read_text()) for m in MODELS]
            texts = [r['transcription']['text'] for r in records]
            requests = [json.loads((RUN/m/'recognition'/name/f'{name}-{i}-ocr-request.json').read_text()) for m in MODELS]
            contents = [r['messages'][0]['content'] for r in requests]
            for typ in ['text', 'image_url']:
                assert [p for p in contents[0] if p['type'] == typ] == [p for p in contents[1] if p['type'] == typ]
            checks.append({'name': name, 'input_page': i, 'ocr_same_text_and_image': True, 'review_same_except_ocr_text': True})
            page_diffs.append({'name': name, 'input_page': i, 'same_text': texts[0] == texts[1],
                               'old_sha256': hashlib.sha256(texts[0].encode()).hexdigest(),
                               'new_sha256': hashlib.sha256(texts[1].encode()).hexdigest()})
            (RUN/f'{name}-{i}-OCR差异.txt').write_text(''.join(difflib.unified_diff(texts[0].splitlines(True), texts[1].splitlines(True),
                                                    fromfile=MODELS[0], tofile=MODELS[1])))
    save(RUN/'comparison.json', {'summary': summary, 'paired': paired, 'migrations': migrations, 'page_diffs': page_diffs})
    save(RUN/'input-equivalence.json', checks)
    csv_write(RUN/'逐合同对比.csv', paired)
    lines = ['# 方案3第一阶段替换VL Plus对比报告', '',
        '2026-10-29，同批LX-001—LX-010，10份合同24页，两个方案均重新调用OCR与27B，不复用上轮识别结果。'
        '只把首阶段qwen-vl-ocr-2025-11-20替换为qwen3-vl-plus；第二阶段均是27B读取原图与该组OCR全文。', '',
        'OCR逐页纯文本输出，VL-Plus关闭思考，两组均temperature=0、max_tokens=3000、相同英文转写指令；'
        '第二阶段思考预算1024、最大输出12000、相同提示词与解析器。合同总并发3，每条链每份一次，无重试或兜底。'
        '评分在所有推理结束后才读答案，固定分母141；评分=正确率×10，保留冲突、缺失与失败扣分。', '',
        '| 指标 | 专用OCR → 27B | VL-Plus OCR → 27B |', '|---|---:|---:|']
    def line(label, values):
        lines.append('| '+label+' | '+' | '.join(values)+' |')
    line('最终评分 / 10', [f"{summary[m]['score_10']:.2f}" for m in MODELS])
    line('最终字段准确率', [f"{summary[m]['accuracy']['correct']}/141（{summary[m]['accuracy']['accuracy']:.2%}）" for m in MODELS])
    line('全部15字段全对', [f"{summary[m]['whole_contract']['all_15_correct']}/10" for m in MODELS])
    line('唯一输出精度', [f"{summary[m]['precision']['accuracy']:.2%}" for m in MODELS])
    line('空金标额外输出', [str(summary[m]['unexpected_nonempty']) for m in MODELS])
    for label, key in [('平均总秒/合同', 'mean_total_seconds'), ('平均OCR秒/合同', 'mean_ocr_seconds_per_contract'),
                       ('平均OCR秒/页', 'mean_ocr_seconds_per_page'), ('平均27B秒/合同', 'mean_review_seconds_per_contract')]:
        line(label, [f'{summary[m][key]:.2f}' for m in MODELS])
    for label, key in [('24页OCR估算元', 'ocr_cost_yuan'), ('10份27B估算元', 'review_cost_yuan'), ('完整10份估算元', 'total_cost_yuan')]:
        line(label, [f'{summary[m][key]:.4f}' for m in MODELS])
    line('最终协议终态', [str(summary[m]['completion_states']) for m in MODELS])
    lines += ['', '## 首遍文字与最终字段的区别', '',
        '没有逐字全文金标，所以本轮无法给出OCR字符准确率。作为辅助诊断，将两组首遍文本交给同一确定性解析器，'
        '再对15类合同字段评分。它测的是“全文到字段的可解析覆盖”，同时受表格、换行、标签格式与解析器能力影响，不能作为OCR纯识别准确率。', '',
        '| 首遍文字经固定解析器 | 专用OCR | VL-Plus OCR |', '|---|---:|---:|']
    line('字段诊断正确数', [f"{summary[m]['ocr_parse_diagnostic']['correct']}/141" for m in MODELS])
    lines += ['', '## 逐合同', '', '| 档案 | 页数 | 原方案正确 | VL首阶段正确 | 分母 |', '|---|---:|---:|---:|---:|']
    for r in paired:
        lines.append('| '+' | '.join(str(r[k]) for k in ['档案号', '页数', MODELS[0]+'正确', MODELS[1]+'正确', '分母'])+' |')
    gains = [r for r in migrations if r['stage'] == 'final' and r['nonempty_gold'] and r['change'] == 'gain']
    losses = [r for r in migrations if r['stage'] == 'final' and r['nonempty_gold'] and r['change'] == 'regression']
    lines += ['', f'最终字段新增正确{len(gains)}项、原正确退化{len(losses)}项，净变化{len(gains)-len(losses):+d}项。'
              '逐字段原值、状态与答案见[字段得失对比](../runs/vl-firststage-20261029/字段得失对比.csv)。', '',
        '## 差别发生在哪里', '',
        '**保住前导零是真实收益。** LX-007专用OCR两页均转为00000008006GB00184F00065084，'
        'VL-Plus两页为000000008006GB00184F00065084，与金标一致；该收益保留到了27B最终字段。'
        'LX-007面积在VL链被27B同时保留569.46旧值与579.46新值，因此暂缓给分；'
        '原OCR链只输出579.46并得分。这项失分反映更完整的涂改候选，不能归类为识别变差。', '',
        '**更多原文未必增加最终得分。** LX-001专用OCR只保留4018.25，VL-Plus保留“4018.25 3998.25”。'
        '但都未明确标记划改角色，最终27B两条链都只输出3998.25。VL首遍多留了信息，最终字段没有新增收益，'
        '涂改前后候选完整性也没有随之通过。', '',
        '**错误OCR仍会影响有原图的27B。** LX-004原图坐落与标准答案为站前街38号，'
        '专用OCR为38号，VL-Plus两处均写成36号；最终27B沿用了36号。'
        '这是一处首遍错字贯穿双输入复核的实例，说明“加了原图”不能保证模型纠正OCR。', '',
        '**其余两项最终迁移不能直接归因于OCR识别能力。** LX-003最终合同类型由新签变为续期，'
        '但两组首遍都含有“期满续签”，差别发生在下游理解/生成；'
        'LX-004合同编号从正确的〔〕退为()，两组首遍原本都不是〔〕，也包含下游字形归一与生成波动。', '',
        '**首遍字段诊断下降主要受格式兼容影响。** VL-Plus在LX-002、LX-008输出Markdown表格，'
        '证件号、电话等文字实际存在，固定解析器却未接收部分值；LX-008承租人还带入表格竖线。'
        '因此123→121不能解释为“VL纯OCR少认对两个字/字段”。第二阶段27B能恢复这些值，'
        '但该格式也会影响首遍确定性解析和后续其它文字消费者。', '',
        '## 解释边界', '',
        '相同输入指令与图片已逐页校验；27B请求除OCR全文内容外逐份完全相同。专用OCR按官方协议图在前，'
        'VL-Plus按通用Chat协议文在前；不声称两个不同API具有完全一致的内部图像编码。', '',
        '这批是开发回归集，各条链仅一次，27B即使temperature=0仍可能有生成波动。'
        '首阶段模型变化、其实际转写变化与下游生成均包含在本轮链路结果里，不把单次端到端差值当作稳定因果收益。'
        '上轮原方案133/141、9.43分是历史参考，本轮重新运行的原方案才是本轮对照。', '',
        '成本按[阿里云北京官方价格](https://help.aliyun.com/zh/model-studio/model-pricing)估算：'
        '专用OCR输入/输出0.3/0.5，VL-Plus 1/10，27B 0.6/4.8元每百万Token。'
        '按实际返回usage计算，不抵扣缓存、免费额度和账号折扣；实际账单未取得，探针费用不包含在合同表中。', '',
        '本轮只增加实验脚本和结果报告，未切换生产默认、重启原服务或写入业务库。'
        '代码、请求、原始响应、逐页文字差异、usage、终态、字段诊断与输入等价校验位于`runs/vl-firststage-20261029/`。']
    a, b = (summary[m] for m in MODELS)
    lines[2:2] = [
        '**本轮有局部OCR收益，但没有提高方案3最终字段得分，暂不建议替换默认首阶段。** '
        f"原OCR链{a['accuracy']['correct']}/141（{a['accuracy']['accuracy']:.2%}，{a['score_10']:.2f}分），"
        f"VL首阶段链{b['accuracy']['correct']}/141（{b['accuracy']['accuracy']:.2%}，{b['score_10']:.2f}分）。"
        '最终新增2项、失去3项，其中1项失分来自更保守地保留涂改冲突，不能简单说VL整体OCR更差。', '',
        f"首阶段平均{a['mean_ocr_seconds_per_contract']:.2f}→{b['mean_ocr_seconds_per_contract']:.2f}秒/合同，"
        f"端到端{a['mean_total_seconds']:.2f}→{b['mean_total_seconds']:.2f}秒/合同；"
        f"24页OCR标价估算{a['ocr_cost_yuan']:.4f}→{b['ocr_cost_yuan']:.4f}元（{b['ocr_cost_yuan']/a['ocr_cost_yuan']:.2f}倍），"
        f"完整链路每合同约{a['total_cost_yuan']/10:.4f}→{b['total_cost_yuan']/10:.4f}元。"
        'Token差价本身很小，能否减少人工核对才决定商业价值；本轮未测人工耗时。', '']
    lines += ['', '## 建议', '',
        '当前保留专用OCR为方案3首阶段。VL-Plus值得继续作为长编号、涂改和勾选材料的补充转写候选，'
        '但该定向路由尚未实现或验证，不能把当前结果包装成“加一次高端OCR就能自动提升准确率”。'
        '如继续测试，应将输出约束为稳定的逐行文本、显式保留划改角色，并用新的样本重复比较；'
        '改提示词后，两条链必须一起复测。这里仅测试VL关闭思考的OCR模式，未测试其思考模式或专门调优提示词。', '',
        '本次正式合同调用68次（48次逐页OCR、20次27B复核），另有1次合成图连通探针。'
        '全部收到有效API响应，无重试；字段评分、原图摘要、输入等价与usage完整性均离线核验，'
        '脚本编译和差异格式检查通过。未将工程检查计入模型分数。']
    (ROOT/'docs/方案3首阶段VL-Plus-OCR对比报告-2026-10-29.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
