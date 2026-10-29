import pytest
from app.field_semantics import canonical_identifier, explicit_modes
from app.providers import parse_reply
from app.governance import normalize


def test_identifier_only_normalizes_balanced_year_brackets():
    assert canonical_identifier('资租（2025）007号') == canonical_identifier('资租〔2025〕007号')
    assert canonical_identifier('资租(2025)0007号') == '资租〔2025〕0007号'
    assert canonical_identifier('资租(2025]007号') == '资租(2025]007号'
    assert canonical_identifier('资租〔2025〕007号') != canonical_identifier('资租〔2025〕017号')
    assert canonical_identifier('资租(2025)007号') != canonical_identifier('资租(2024)007号')


@pytest.mark.parametrize('raw,expected',[
 ('第五条 合同期内年租金不作调整。',['固定']),
 ('年租金标准随县人民政府公布的基准地价调整而相应调整。',['随标准调整']),
 ('□固定 □随基准地价调整',[]),
 ('☑固定 □随基准地价调整',['固定']),
 ('年租金不作调整。☑随基准地价调整',['固定','随标准调整']),
 ('不是合同期内年租金不作调整',[]),
 ('年租金是否保持不变',[]),
 ('年租金100元，期限10年',[]),
])
def test_mode_requires_explicit_source(raw,expected):
    assert explicit_modes(raw)==expected
    if '固定' not in raw or '随标准调整' not in raw:
        assert normalize('mode',raw)==expected


def test_source_and_canonical_are_separate_and_conflicts_survive():
    raw='合同编号：资租〔2025〕007号'
    out=parse_reply({'fields':[
      {'field':'contract_no','value':'资租(2025)007号','raw_text':raw},
      {'field':'mode','value':'年租金不作调整。☑随基准地价调整','raw_text':'年租金不作调整。☑随基准地价调整','basis':'clause'},
      {'field':'area','value':'10','raw_text':'原10改为20','text_role':'crossed_out'},
      {'field':'area','value':'20','raw_text':'原10改为20','text_role':'current'}]}, {'id':'p'},[0,0,100,100],{'model_id':'test','provider':'mock'})
    c=out['contract_no'][0]
    assert c['raw_text']==raw and c['raw_value']=='资租(2025)007号'
    assert c['canonical_value']=='资租〔2025〕007号'
    assert {c['value'] for c in out['mode']}=={'固定','随标准调整'}
    assert {c['text_role'] for c in out['area']}=={'crossed_out','current'}
    assert all(not c['verified'] and not c['can_publish'] for c in out['area'])


def test_linked_contracts_use_canonical_year_without_repairing_digits():
    import json
    from app.business import linked_parcels
    class DB:
        def execute(self,*args):
            return [{'id':'known','parcel_id':'parcel','review':json.dumps({'fields':{'contract_no':'资租〔2025〕007号'}})}]
    assert linked_parcels(DB(),{'original_contract_no':'资租（2025）007号'})=={'parcel'}
    assert linked_parcels(DB(),{'original_contract_no':'资租（2025）017号'})==set()
    assert linked_parcels(DB(),{})==set()


def test_alteration_warning_never_disappears_or_confirms_effectiveness():
    from app.observation_audit import audit
    response={'warnings':[{'page_id':'p','type':'涂改','reason':'4018.25划掉改为3998.25'}],
              'candidates':{'annual':[{'value':'3998.25','text_role':'current','source_page_id':'p'}]}}
    row=audit(response)['alteration_pages'][0]
    assert row['status']=='missing_before_after_pair' and row['warnings_preserved']==response['warnings']
    assert row['business_adoption']=='requires_human_confirmation' and not row['verified']
    response['candidates']['annual'].append({'value':'4018.25','text_role':'crossed_out','source_page_id':'p'})
    row=audit(response)['alteration_pages'][0]
    assert row['status']=='paired_candidates_present' and not row['verified']
