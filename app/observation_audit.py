"""Check alteration evidence completeness, never reconstruct old facts from arithmetic."""
import re


def audit(response):
    flagged={}
    for warning in response.get('warnings',[]):
        if not isinstance(warning,dict):continue
        raw=str(warning.get('type',''))+' '+str(warning.get('reason',''))
        if re.search('涂改|划掉|划去|覆盖原|改写',raw):
            flagged.setdefault(warning.get('page_id'),[]).append(warning)
    rows=[]
    for pid,warnings in flagged.items():
        fields={}
        for key,values in response.get('candidates',{}).items():
            selected=[c for c in values if c.get('source_page_id')==pid]
            before=[c for c in selected if c.get('text_role')=='crossed_out']
            after=[c for c in selected if c.get('text_role')=='current']
            if before or after:
                fields[key]={'before':before,'after':after}
        pairs=[key for key,values in fields.items() if values['before'] and values['after']]
        rows.append({'source_page_id':pid,'warnings_preserved':warnings,'fields':fields,
                     'candidate_pairs':pairs,'status':'paired_candidates_present' if pairs else 'missing_before_after_pair',
                     'business_adoption':'requires_human_confirmation','verified':False})
    return {'alteration_pages':rows,'note':'完整候选不代表涂改有效；未形成前后候选对时显式保留缺失，不按金额合算补旧值。'}
