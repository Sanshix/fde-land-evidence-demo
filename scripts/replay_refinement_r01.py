"""Replay frozen model field JSON through current admission rules; no new model calls."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from app.providers import parse_reply
from app.domain import FIELDS
from app.observation_audit import audit
from scripts.score_recognition import evaluate,write_report
ROOT=Path(__file__).resolve().parents[1];RUN=ROOT/'runs/refinement-r01-20261029'
assert (RUN/'inference-complete.json').exists()
summary={}
for cohort in ['regression','holdout']:
 source=RUN/cohort;dest=RUN/'integration-replay'/cohort;dest.mkdir(parents=True,exist_ok=True)
 docs=json.loads((source/'documents.json').read_text());(dest/'documents.json').write_text(json.dumps(docs,ensure_ascii=False,indent=2))
 for document in docs:
  folder=source/'recognition'/document['name'];output=dest/'recognition'/document['name'];output.mkdir(parents=True,exist_ok=True)
  r=json.loads((folder/'result.json').read_text());inference=r.get('text_inference',{})
  if inference.get('status')=='completed':
   raw=inference.get('raw_response',{});candidates={key:[] for key in FIELDS};diagnostics=[]
   for index,file in enumerate(document['files'],1):
    pid=f"{document['name']}-{index}";selected=[f for f in raw.get('fields',[]) if isinstance(f,dict) and (type(f.get('page')) is int and f['page']==index or 'page' not in f and f.get('page_id')==pid)]
    with Image.open(ROOT.parent/'练习集/01_合同扫描件'/file) as im:w,h=im.size
    parsed=parse_reply({'fields':selected},{'id':pid},[0,0,w,h],{'model_id':inference['requested_model'],'provider':inference['provider'],'response_id':inference.get('response_id')})
    for key,rows in parsed.items():candidates[key].extend(rows)
    diagnostics+=getattr(parsed,'diagnostics',{}).get('observations',[])
   r['candidates']=candidates;r['integration_replay_diagnostics']=diagnostics
   r['observation_audit']=audit({**inference,'candidates':candidates})
  r['replayed_from']=str(folder/'result.json')
  (output/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
  (output/'outcome.json').write_bytes((folder/'outcome.json').read_bytes())
 report=evaluate(dest,ROOT.parent/'练习集/08_标准答案');write_report(report,dest/'scoring')
 original=json.loads((source/'scoring/recognition_score.json').read_text())
 summary[cohort]={'strict_field_accuracy':report['field_accuracy']['selected_fixed_denominator'],
                  'whole_contract':report['whole_contract'],
                  'same_strict_field_result_as_inference':report['field_accuracy']['selected_fixed_denominator']==original['field_accuracy']['selected_fixed_denominator']}
(RUN/'integration-replay/summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False))
