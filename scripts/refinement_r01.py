"""Frozen R01 live inference. No answers or scoring imports; no product DB writes."""
import concurrent.futures
import hashlib
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.providers import CloudProvider,base_configuration
from app.recognition_workflow import configure
from app.cloud_cascade import run_cloud_cascade

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'runs/refinement-r01-20261029'


def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(data,ensure_ascii=False,indent=2))


def one(item):
    cohort,name,files=item;folder=RUN/cohort/'recognition'/name
    if (folder/'outcome.json').exists():return json.loads((folder/'outcome.json').read_text())
    pages=[{'id':f'{name}-{i}','name':p.name,'path':str(p),'source':str(p),'kind':'image'} for i,p in enumerate(files,1)]
    cfg=configure(base_configuration(),'plan3')
    cfg.update(enabled=True,authorized_scope='explicit_sample_hash',authorized_sha256=[hashlib.sha256(p.read_bytes()).hexdigest() for p in files])
    start=time.monotonic()
    result=run_cloud_cascade(pages,folder,budget=300,config=cfg)
    outcome={'name':name,'cohort':cohort,'pages':len(pages),'seconds':round(time.monotonic()-start,3),
             'completion_status':result.get('completion_status'),'external_calls':result.get('external_calls'),
             'published':False,'state':'needs_human'}
    save(folder/'outcome.json',outcome);print(json.dumps(outcome,ensure_ascii=False),flush=True)
    return outcome


def main():
    frozen=json.loads((RUN/'frozen/manifest.json').read_text())
    manifest={'cohorts':frozen['cohorts'],'inputs':{},'code':{},'concurrency':2,'workflow':'plan3',
              'policy':'same fixed rules for regression and holdout; answers only in separate offline scorer',
              'budget_seconds':300,'thinking_budget':1024,'max_tokens':12000,'product_db_writes':False}
    work={}
    for cohort,names in manifest['cohorts'].items():
        work[cohort]=[];docs=[]
        for name in names:
            files=sorted((ROOT.parent/'练习集/01_合同扫描件').glob(name+'_*.jpg'));assert files
            work[cohort].append((cohort,name,files));docs.append({'name':name,'files':[p.name for p in files]})
            for p in files:manifest['inputs'][p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
        save(RUN/cohort/'documents.json',docs)
    for name in ['app/providers.py','app/field_semantics.py','app/governance.py','app/business.py','app/evidence.py','app/text_pipeline.py','app/triage.py','app/confidence.py','scripts/refinement_r01.py']:
        p=ROOT/name;manifest['code'][name]=hashlib.sha256(p.read_bytes()).hexdigest()
        target=RUN/'code'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(p.read_bytes())
    if (RUN/'manifest.json').exists():assert json.loads((RUN/'manifest.json').read_text())==manifest
    save(RUN/'manifest.json',manifest)
    try:
        for cohort in ['regression','holdout']:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:list(executor.map(one,work[cohort]))
            save(RUN/cohort/'frozen.json',{'at':time.time()})
    finally:pass
    save(RUN/'inference-complete.json',{'at':time.time()})

if __name__=='__main__':main()
