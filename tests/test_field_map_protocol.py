"""Known structural alternatives never change evidence values or confer verification."""
import copy
import json

import pytest

from app.providers import ProviderResponseError, parse_reply, response_json

ENTRY = {'value':'001234567890123456','raw_text':'证件号码 001234567890123456',
         'bbox':[.1,.2,.8,.3],'status':'candidate'}
PROFILE = {'provider':'offline-fixture','model_id':'synthetic'}


def test_field_map_preserves_raw_shape_locator_and_leading_zero():
    raw = {'fields':{'identity':ENTRY},'warnings':[]}
    before = copy.deepcopy(raw)
    decoded = response_json(json.dumps(raw), {'raw_response_text':json.dumps(raw)})
    result = parse_reply(decoded, {'id':'synthetic'}, [0,0,100,100], PROFILE)
    c = result['identity'][0]
    assert c['value']==ENTRY['value'] and c['raw_value']==ENTRY['value']
    assert c['response_locator']=='response/fields/identity'
    assert c['parse_provenance']['response_locator']==c['response_locator']
    assert result.diagnostics['protocol_shape']=='field_map_v1'
    assert not c['verified'] and not c['can_publish']
    assert raw==before and decoded==raw
    assert parse_reply(decoded, {'id':'synthetic'}, [0,0,100,100], PROFILE)==result


@pytest.mark.parametrize('fields',[
    {'identity':'001234567890123456'}, {'unknown':ENTRY},
    {'identity':ENTRY | {'field':'phone'}}, None,
])
def test_ambiguous_or_unsupported_map_is_rejected(fields):
    evidence={'raw_response_text':json.dumps({'fields':fields})}
    with pytest.raises(ProviderResponseError) as caught:
        response_json(evidence['raw_response_text'],evidence)
    assert caught.value.evidence==evidence


def test_duplicate_json_keys_do_not_silently_choose_last_value():
    raw='{"fields":{"identity":{"value":"first","value":"second"}}}'
    with pytest.raises(ProviderResponseError):
        response_json(raw,{'raw_response_text':raw})


def test_array_format_keeps_existing_source_path():
    raw={'fields':[ENTRY | {'field':'identity'}]}
    result=parse_reply(raw,{'id':'synthetic'},[0,0,100,100],PROFILE)
    assert result['identity'][0]['response_locator']=='response/fields/0'
