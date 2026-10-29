"""Offline scoring only; run after paired inference freezes. No model clients."""
import csv,json,math,statistics,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.score_recognition import evaluate,write_report
ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'runs/paired-qwen-ocr-10-20261029'
assert (RUN/'inference-complete.json').is_file(),'Inference must finish before loading answers'
reports={};outcomes={};summary={}
for plan in ['plan1','plan2']:
 r=evaluate(RUN/plan,ROOT.parent/'练习集/08_标准答案');write_report(r,RUN/plan/'scoring');reports[plan]=r
 docs=json.loads((RUN/plan/'documents.json').read_text())
 rows=[json.loads((RUN/plan/'recognition'/d['name']/'outcome.json').read_text()) for d in docs];outcomes[plan]=rows
 times=sorted(x['seconds'] for x in rows);correct=r['whole_contract']['key_all_correct'];n=len(rows)
 summary[plan]={'n':n,'pages':sum(x['pages'] for x in rows),'mean_seconds':statistics.mean(times),'p50_seconds':statistics.median(times),'p95_seconds':times[math.ceil(.95*n)-1],
 'total_contract_seconds':sum(times),'reported_total_tokens':sum((stage.get('usage') or {}).get('total_tokens',0) for row in rows for stage in row['stages']),'field_accuracy':r['field_accuracy']['selected_fixed_denominator'],
 'unique_output_precision':r['field_accuracy']['precision_unique_produced'],'coverage':r['field_accuracy']['coverage_selected'],
 'whole_contract':r['whole_contract'],'completion_states':r['completion_states'],
 'offline_auto_contract_component_30':max(0,30/n*(2*correct-n)),
 'offline_signed_contract_points':30/n*(2*correct-n),'actual_business_score':None,
 'per_field':r['field_accuracy']['per_field']}
paired=[]
for a,b,da,db in zip(outcomes['plan1'],outcomes['plan2'],reports['plan1']['details'],reports['plan2']['details']):
 assert a['name']==b['name']==da['name']==db['name']
 paired.append({'档案号':a['name'],'页数':a['pages'],'方案1秒':a['seconds'],'方案2秒':b['seconds'],'方案2减1秒':round(b['seconds']-a['seconds'],3),'方案1正确字段':da['correct_nonempty'],'方案2正确字段':db['correct_nonempty'],'应评字段':da['truth_nonempty'],'方案1关键12全对':da['key_all_correct'],'方案2关键12全对':db['key_all_correct']})
with (RUN/'逐合同对比.csv').open('w',encoding='utf-8-sig',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(paired[0]));w.writeheader();w.writerows(paired)
(RUN/'comparison.json').write_text(json.dumps({'summary':summary,'paired':paired},ensure_ascii=False,indent=2))
lines=['# 同一批10份合同：27B直读与OCR后推理对比','',
'样本为练习集按编号排序前10份（LX-001—LX-010），使用全部页面；先选样、冻结两组识别结果，再读取答案。两条方案交替先后次序，合同并发2；合同耗时含全部请求与解析，不含任务排队。未重试失败请求、未手工改答案、未修改业务数据库或发布。',
'', '| 指标 | 方案1：27B图片直读 | 方案2：OCR→27B纯文字 |','|---|---:|---:|']
for label,key in [('平均耗时（秒/合同）','mean_seconds'),('中位耗时P50（秒）','p50_seconds'),('P95耗时（秒）','p95_seconds')]:lines.append(f"| {label} | {summary['plan1'][key]:.2f} | {summary['plan2'][key]:.2f} |")
for label,key in [('固定非空分母字段准确率','field_accuracy'),('唯一输出精度','unique_output_precision'),('字段输出覆盖率','coverage')]:
 def fmt(p):
  x=summary[p][key];return f"{x['correct']}/{x['denominator']}（{x['accuracy']:.2%}）" if x['accuracy'] is not None else 'N/A'
 lines.append(f"| {label} | {fmt('plan1')} | {fmt('plan2')} |")
for label,key in [('关键12字段整份全对','key_all_correct'),('全部15字段整份全对','all_15_correct')]:lines.append(f"| {label} | {summary['plan1']['whole_contract'][key]}/10 | {summary['plan2']['whole_contract'][key]}/10 |")
lines.append(f"| 返回usage合计Token | {summary['plan1']['reported_total_tokens']} | {summary['plan2']['reported_total_tokens']} |")
lines += [f"| 离线模拟合同项（满分30） | {summary['plan1']['offline_auto_contract_component_30']:.1f} | {summary['plan2']['offline_auto_contract_component_30']:.1f} |",'',
'## 评分边界','',
'复用 scripts/score_recognition.py 与现有评分字段、数值/日期规范化、关键12字段定义；缺失、失败、冲突、涂改前后多值都不靠标准答案择优。上下文修正值只作为模型最终候选，并要求原文引用；方案2无图像坐标，不因此伪造bbox。',
'', '“离线模拟合同项”仅把本次10份按官方合同项规则归一：每份关键12字段全对+1，否则−1，30/10缩放并截断到0。它假设全按自动输出比较，不能当成实际自动放行或产品发布分。未执行真人复核、宗地匹配、租金核算、问题清单、催缴流程，其余70分不评，官方100分总分为未测。',
'', '本轮评的是结构化字段，与逐字OCR准确率不同：练习集没有逐字全文金标，不能计算字符错误率CER，也未验证表格定位或证据坐标准确率。10份顺序样本不能证明对全部50份或新合同的泛化表现。P95按最近秩定义，n=10时等于最大值。',
'', '## 逐份结果','', '| 合同 | 页数 | 方案1秒 | 方案2秒 | 方案1正确字段 | 方案2正确字段 | 应评字段 |','|---|---:|---:|---:|---:|---:|---:|']
for x in paired:lines.append('| '+ ' | '.join(str(x[k]) for k in ['档案号','页数','方案1秒','方案2秒','方案1正确字段','方案2正确字段','应评字段'])+' |')
lines += ['', '逐字段明细与差异分别位于 plan1/scoring、plan2/scoring；请求、响应、usage、单阶段耗时、失败详情位于各自 recognition/<档案号>/。manifest.json记录样本摘要、模型预算、选择方法及测试脚本摘要。调用金额未配置官方单价，不把未知费用写成0。']
(RUN/'对比报告.md').write_text('\n'.join(lines)+'\n')
print(json.dumps(summary,ensure_ascii=False))
