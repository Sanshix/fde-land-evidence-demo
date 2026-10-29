"""Synthetic local grammar, counterexamples and provenance checks."""
from app.deterministic_parser import admit_value, parse_text
from app.extraction import prose_candidates

page={'id':'synthetic'}
profile={'model_id':'offline','provider':'offline'}
def values(text,key):
    result,_=parse_text(text,page,profile)
    return {c['value'] for c in result[key]}

cases=[
 ('第五条 合同期内年租金不作调整。','mode',{'固定'}),
 ('年租金标准随市人民政府公布的基准地价调整而相应调整，乙方按调整后的标准缴纳。','mode',{'随标准调整'}),
 ('如计租方式为随标准调整的，年租金标准随县人民政府公布的基准地价调整而相应调整。','mode',set()),
 ('原合同期内年租金不作调整。','mode',set()),
 ('本合同并非合同期内年租金不作调整。','mode',set()),
 ('第五条 合同期内年租金不作调整，但另行约定的除外。','mode',set()),
 ('计租方式：□固定 □随标准调整','mode',set()),
 ('计租方式：□固定 √不明文字','mode',set()),
 ('土地面积（m²）2','area',{'2'}),
 ('土地面积：2㎡','area',{'2'}),
 ('土地面积：2 3平方米','area',set()),
 ('承租人（乙方）：测试企业（分支）','tenant',{'测试企业(分支)'}),
 ('签订日期：2031年1月2日 2031年1月2日','signed',{'2031-01-02'}),
 ('签订日期：2031年1月2日 2031年1月3日','signed',set()),
 ('租赁期限：2031-01-01至2032-01-01 或另行约定','start',set()),
 ('证件号码：001234567890123456 联系电话：10000000001','identity',{'001234567890123456'}),
 ('年租金（大写）：1.25元/㎡·年','annual_upper',set()),
 ('年租金（大写）：贰元整','annual_upper',{'贰元整'}),
]
def test_rent_clause_admission_probes():
    _run_probes()

def _run_probes():
    results=[]
    for text,key,expected in cases:
        actual=values(text,key)
        assert actual==expected,(text,actual,expected)
        results.append({'text':text,'field':key,'expected':sorted(expected),'actual':sorted(actual),'pass':True})
    assert admit_value('annual_upper','1.25元/m²·年')[1]=='numeric_unit_rate_not_uppercase_amount'
    positions=prose_candidates([{'text':'签订日期：2031年1月2日','locator':'a'},
                               {'text':'签订日期：2032年2月3日','locator':'b'}],'synthetic','offline','offline')['signed']
    assert [p['response_locator'] for p in positions]==['a','b']
    for text,_key,_ in cases:
        one,_=parse_text(text,page,profile)
        two,_=parse_text(text,page,profile)
        assert one==two
