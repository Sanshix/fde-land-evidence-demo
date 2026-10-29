"""Offline assessment after plan 3 outputs freeze; historical baselines are explicit."""
import csv,json,statistics,shutil,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.score_recognition import evaluate,write_report
ROOT=Path(__file__).resolve().parents[1];RUN=ROOT/'runs/plan3-five-20261029';PRIOR=ROOT/'runs/paired-qwen-ocr-10-20261029'
assert (RUN/'inference-complete.json').exists(),'Do not score before inference completes'
docs=json.loads((RUN/'plan3/documents.json').read_text());summary={};scores={};outcomes={}
for plan in ['plan1','plan2','plan3']:
 if plan!='plan3':
  dst=RUN/('historical-'+plan);dst.mkdir(exist_ok=True)
  (dst/'documents.json').write_text(json.dumps(docs,ensure_ascii=False,indent=2))
  for doc in docs:
   target=dst/'recognition'/doc['name'];target.mkdir(parents=True,exist_ok=True)
   for file in ['result.json','outcome.json']:shutil.copy2(PRIOR/plan/'recognition'/doc['name']/file,target/file)
 else:dst=RUN/'plan3'
 score=evaluate(dst,ROOT.parent/'练习集/08_标准答案');write_report(score,dst/'scoring');scores[plan]=score
 rows=[json.loads((dst/'recognition'/d['name']/'outcome.json').read_text()) for d in docs];outcomes[plan]={r['name']:r for r in rows}
 n=len(rows);correct=score['whole_contract']['key_all_correct']
 summary[plan]={'n':n,'pages':sum(r['pages'] for r in rows),'mean_seconds':statistics.mean(r['seconds'] for r in rows),'median_seconds':statistics.median(r['seconds'] for r in rows),'max_seconds':max(r['seconds'] for r in rows),
  'mean_ocr_seconds':sum(s['seconds'] for r in rows for s in r['stages'] if s['stage']=='ocr')/n,
  'mean_qwen_seconds':sum(s['seconds'] for r in rows for s in r['stages'] if s['stage']=='qwen')/n,
  'field_accuracy':score['field_accuracy']['selected_fixed_denominator'],'whole_contract':score['whole_contract'],
  'completion_states':score['completion_states'],'offline_auto_contract_30':max(0,30/n*(2*correct-n)),
  'reported_total_tokens':sum((s.get('usage') or {}).get('total_tokens',0) for r in rows for s in r['stages'])}
paired=[]
for d in docs:
 name=d['name'];row={'档案号':name,'页数':len(d['files'])}
 for plan in summary:
  row[plan+'秒']=outcomes[plan][name]['seconds'];detail=next(x for x in scores[plan]['details'] if x['name']==name)
  row[plan+'正确字段']=detail['correct_nonempty'];row['应评字段']=detail['truth_nonempty']
 paired.append(row)
with (RUN/'逐合同对比.csv').open('w',encoding='utf-8-sig',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(paired[0]));w.writeheader();w.writerows(paired)
(RUN/'comparison.json').write_text(json.dumps({'summary':summary,'paired':paired},ensure_ascii=False,indent=2))
lines=['# 方案3五份合同测试','',
'同一批LX-001—LX-005，共10页。方案3本次重新逐页调用OCR，随后单次27B请求同时提供全部原图和逐页OCR全文。方案1/2复用上轮相同5份的冻结结果，属于历史配对比较，不是同时运行的严格对照或新的盲测。每份一次，不因已知错误修改提示词或评分；思考预算1024、输出上限12000，合同并发2。',
'', '| 指标 | 方案1：原图 | 方案2：OCR→纯文本27B | 方案3：OCR→原图+全文27B |','|---|---:|---:|---:|']
for label,key in [('平均秒/合同','mean_seconds'),('中位秒/合同','median_seconds'),('最慢秒/合同','max_seconds'),('平均OCR耗时','mean_ocr_seconds'),('平均27B耗时','mean_qwen_seconds')]:lines.append('| '+label+' | '+' | '.join(f'{summary[p][key]:.2f}' for p in summary)+' |')
lines.append('| 字段准确率 | '+' | '.join(f"{summary[p]['field_accuracy']['correct']}/{summary[p]['field_accuracy']['denominator']}（{summary[p]['field_accuracy']['accuracy']:.2%}）" for p in summary)+' |')
for label,key in [('关键12字段全对','key_all_correct'),('全部15字段全对','all_15_correct')]:lines.append('| '+label+' | '+' | '.join(str(summary[p]['whole_contract'][key])+'/5' for p in summary)+' |')
lines.append('| 离线模拟合同项/30 | '+' | '.join(str(summary[p]['offline_auto_contract_30']) for p in summary)+' |')
lines+=['','## 逐合同','', '| 档案 | 方案1秒 | 方案2秒 | 方案3秒 | 方案1正确 | 方案2正确 | 方案3正确 | 分母 |','|---|---:|---:|---:|---:|---:|---:|---:|']
for row in paired:lines.append('| '+' | '.join(str(row[k]) for k in ['档案号','plan1秒','plan2秒','plan3秒','plan1正确字段','plan2正确字段','plan3正确字段','应评字段'])+' |')
lines+=['','## 口径与边界','',
'沿用score_recognition.py：固定非空分母，缺失/失败计入分母，冲突和涂改多值不按答案择优，严格保留原始编号括号差异的扣分。离线合同项假设全自动：每份关键12全对+1，否则−1，按30/5缩放并截断0；不是实际放行分。其余70分未测，完整业务总分未测。',
'本轮没有逐字全文金标，字段准确率不代表全文OCR字符准确率。记录的时间包含整个合同OCR及27B处理，不含队列等待。历史比较有服务负载和生成随机性影响，方案3也使用新OCR响应，因此差值不能严格归因于增加图片。原始OCR、27B请求/响应、usage、候选与失败证据均保留。未修改当前方案2默认配置，未更改业务库。']
(RUN/'方案3对比报告.md').write_text('\n'.join(lines)+'\n')
print(json.dumps(summary,ensure_ascii=False))
