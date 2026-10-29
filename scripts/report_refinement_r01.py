"""Offline R01 evaluation. Does not run models or change product data."""
import csv,hashlib,json,statistics,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.score_recognition import evaluate,write_report,safe_compare
from scripts.score_submission import FIELDS,KEY
from app.domain import FIELDS as LABELS
from app.triage import triage
from app.field_semantics import canonical_identifier
from app.observation_audit import audit
ROOT=Path(__file__).resolve().parents[1];RUN=ROOT/'runs/refinement-r01-20261029'
assert (RUN/'inference-complete.json').exists(),'Inference must freeze before scoring'
frozen=json.loads((RUN/'frozen/manifest.json').read_text())
for name in ['scripts/score_recognition.py','scripts/score_submission.py','../练习集/score.py']:
 assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==frozen['files'][name],f'Scorer changed: {name}'
for name,digest in frozen['baseline_result_sha256'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest
summary={};all_details={};field_labels={v:k for k,v in LABELS.items()}
with (ROOT.parent/'练习集/08_标准答案/合同要素.csv').open(encoding='utf-8-sig',newline='') as f:gold={r['档案号']:r for r in csv.DictReader(f)}
with (ROOT.parent/'练习集/08_标准答案/问题清单.csv').open(encoding='utf-8-sig',newline='') as f:problems=list(csv.DictReader(f))
for cohort in ['regression','holdout']:
 folder=RUN/cohort;score=evaluate(folder,ROOT.parent/'练习集/08_标准答案');write_report(score,folder/'scoring')
 docs=json.loads((folder/'documents.json').read_text());outcomes=[];details=[];correct_candidate=0;denominator=0;candidate_whole=0;canonical_correct=0;canonical_whole=0
 for info in docs:
  name=info['name'];base=folder/'recognition'/name
  result=json.loads((base/'result.json').read_text());outcome=json.loads((base/'outcome.json').read_text());outcomes.append(outcome)
  pages=[{'id':f'{name}-{i}','name':file,'kind':'image','path':str(ROOT.parent/'练习集/01_合同扫描件'/file)} for i,file in enumerate(info['files'],1)]
  document={'id':name,'name':name,'state':'needs_human','pages':pages,'result':result}
  route=triage(document)
  standard=next(d for d in score['details'] if d['name']==name)
  present=0;count=0
  for label in FIELDS:
   if safe_compare(label,gold[name][label])=='':continue
   count+=1
   rows=result.get('candidates',{}).get(field_labels[label],[])
   present+=any(safe_compare(label,c['value'])==safe_compare(label,gold[name][label]) for c in rows)
  correct_candidate+=present;denominator+=count;candidate_whole+=present==count
  canonical_rows=[row for row in score['field_rows'] if row['档案号']==name]
  def canonical_ok(row):
   if row['字段']=='合同编号' and row['候选状态']=='unique_observed':
    return canonical_identifier(row['唯一候选'])==canonical_identifier(row['标准答案'])
   return bool(row['相同']) or (row['候选状态']=='missing' and safe_compare(row['字段'],row['标准答案'])=='')
  canonical_correct+=sum(canonical_ok(row) for row in canonical_rows if row['计入非空分母'])
  canonical_whole+=all(canonical_ok(row) for row in canonical_rows)
  evidence_audit=audit(result.get('text_inference',{}))
  (base/'offline-observation-audit.json').write_text(json.dumps(evidence_audit,ensure_ascii=False,indent=2))
  warnings=result.get('text_inference',{}).get('warnings',[])
  observations=[{'field':key,'value':c['value'],'role':c.get('text_role'),'page':c.get('source_page_id'),'raw_text':c.get('raw_text'),'verified':c.get('verified'),'can_publish':c.get('can_publish')} for key,rows in result.get('candidates',{}).items() for c in rows if c.get('text_role') in {'crossed_out','alternative','current'}]
  truth_problems=[p for p in problems if p.get('档案号')==name]
  details.append({'name':name,'strict_field_correct':standard['correct_nonempty'],'denominator':count,'correct_value_present':present,
    'strict_key_all_correct':standard['key_all_correct'],'batch_eligible':route['eligible'],'reasons':route['reasons'],
    'seconds':outcome['seconds'],'canonical_all_correct':all(canonical_ok(row) for row in canonical_rows),'observation_audit':evidence_audit,'warnings':warnings,'observations':observations,'truth_problem_rows':truth_problems})
  (base/'offline-routing.json').write_text(json.dumps(route,ensure_ascii=False,indent=2))
 n=len(details);eligible=[d for d in details if d['batch_eligible']];wrong_pass=[d for d in eligible if not d['strict_key_all_correct']]
 wrong=[d for d in details if not d['strict_key_all_correct']];risk_docs=[d for d in details if d['truth_problem_rows']]
 summary[cohort]={'n':n,'strict_field_accuracy':score['field_accuracy']['selected_fixed_denominator'],
  'whole_contract':score['whole_contract'],'correct_value_candidate_coverage':{'correct':correct_candidate,'denominator':denominator,'accuracy':correct_candidate/denominator},
  'canonical_identifier_diagnostic':{'correct':canonical_correct,'denominator':denominator,'accuracy':canonical_correct/denominator,'all_15_correct':canonical_whole},'all_nonempty_values_present':candidate_whole,'mean_seconds':statistics.mean(o['seconds'] for o in outcomes),
  'completion_states':score['completion_states'],'offline_auto_contract_30':max(0,30/n*(2*score['whole_contract']['key_all_correct']-n)),
  'simulated_routing':{'eligible':len(eligible),'wrong_eligible':len(wrong_pass),'error_among_eligible':len(wrong_pass)/len(eligible) if eligible else None,
    'manual_takeover':n-len(eligible),'manual_takeover_rate':1-len(eligible)/n,'wrong_docs':len(wrong),
    'wrong_docs_blocked':sum(not d['batch_eligible'] for d in wrong),'truth_problem_documents':len(risk_docs),
    'truth_problem_documents_blocked':sum(not d['batch_eligible'] for d in risk_docs)},
  'sensitive_fields':{label:score['field_accuracy']['per_field'][label] for label in ['土地面积','年租金小写','年租金大写','宗地号','证件号码']}}
 all_details[cohort]=details
(folder.parent/'evaluation.json').write_text(json.dumps({'summary':summary,'details':all_details},ensure_ascii=False,indent=2))
lines=['# R01：编号原文规范与明确计租条款回归','',
'原结果、官方评分器及诊断脚本已冻结，摘要见frozen/manifest.json。生产识别独立运行：原LX-001—005为已知回归，LX-011—015为未参与本轮调参的留出样本。该留出组不是从未在项目历史中使用过的绝对盲集；本轮未在推理完成前读取其答案。规则在两组间不再修改。',
'', '| 指标 | 原5份回归 | 5份留出 |','|---|---:|---:|']
for label,key in [('严格字段准确率','strict_field_accuracy'),('正确值候选覆盖（非唯一值准确率）','correct_value_candidate_coverage'),('编号括号等价诊断（非官方分数）','canonical_identifier_diagnostic')]:
 lines.append('| '+label+' | '+' | '.join(f"{summary[c][key]['correct']}/{summary[c][key]['denominator']}（{summary[c][key]['accuracy']:.2%}）" for c in summary)+' |')
lines += ['| 关键12字段全对 | '+' | '.join(str(summary[c]['whole_contract']['key_all_correct'])+'/5' for c in summary)+' |',
 '| 全部15字段全对 | '+' | '.join(str(summary[c]['whole_contract']['all_15_correct'])+'/5' for c in summary)+' |',
 '| 平均耗时/秒 | '+' | '.join(f"{summary[c]['mean_seconds']:.2f}" for c in summary)+' |',
 '| 模拟批量准入 | '+' | '.join(str(summary[c]['simulated_routing']['eligible'])+'/5' for c in summary)+' |',
 '| 机器分流到人工 | '+' | '.join(str(summary[c]['simulated_routing']['manual_takeover'])+'/5' for c in summary)+' |',
 '| 离线假设全自动合同项/30 | '+' | '.join(str(summary[c]['offline_auto_contract_30']) for c in summary)+' |',
 '', '“正确值候选覆盖”只问正确值是否出现在候选中，可以包含涂改后值，不能代替严格准确率或业务采纳结果。原评分器对多值保守弃答，未通过删除旧值、选取答案或改处理方式来提分。',
 '', '错误放行采用模拟批量准入检查，不存在真实发布。若无任何准入，已放行错误率分母为0，记N/A，而非宣称真实错误率0%；同时报告错误档案被拦截数量。人工接管率是机器分流比例，不是真人完成率或人工用时。风险档案全部被拦截也可能只是全部转人工，不能视为风险类型识别已验收。',
 '', '## 逐合同','', '| 组 | 档案 | 严格正确 | 正确值存在 | 关键12全对 | 机器批量准入 | 秒 |','|---|---|---:|---:|---|---|---:|']
for cohort,rows in all_details.items():
 for d in rows:lines.append(f"| {cohort} | {d['name']} | {d['strict_field_correct']}/{d['denominator']} | {d['correct_value_present']}/{d['denominator']} | {d['strict_key_all_correct']} | {d['batch_eligible']} | {d['seconds']:.2f} |")
lines += ['', '## 敏感字段','', '| 组 | 字段 | 正确/应评 |','|---|---|---:|']
for cohort,info in summary.items():
 for label,m in info['sensitive_fields'].items():lines.append(f"| {cohort} | {label} | {m['correct']}/{m['selected_truth_nonempty']} |")
lines += ['', '## 可追溯与边界','',
'编号raw_text/raw_value保留模型原文，canonical_value只统一四位年份的配对括号及空白；用于合同引用比较，不修复数字、年份或前导零。正式评分仍按原始value和冻结评分器执行。计租方式允许明确条款或可见勾选，冲突保留，不根据金额推算。候选附原文观察、语义提取和待人工确认三层元数据。',
'生产调用使用默认方案3，并保留OCR全文、原图输入、27B复核稿、响应、usage、警告和尝试记录。独立目录运行，无产品数据库写入或发布。首轮测试记录钩子在线程池中缺少上下文，HTTP客户端调用前即失败；该无效工程试验完整保留于harness-failure，不计入模型性能。',
'本轮不承诺100%泛化，不把规则评分当正确概率，也不以金额合算证明涂改有效。']
(RUN/'回归与留出验证报告.md').write_text('\n'.join(lines)+'\n')
print(json.dumps(summary,ensure_ascii=False))
