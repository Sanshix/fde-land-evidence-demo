from app.deterministic_parser import labeled_segments, parse_text


def test_glued_identity_phone_requires_complete_labels_and_values():
    raw = '证件号码ABCDEFGHIJKLMNOPQR联系电话10000000000'
    assert labeled_segments(raw) == [('identity', 'ABCDEFGHIJKLMNOPQR'), ('phone', '10000000000')]
    result, audit = parse_text(raw, {'id': 'test-page'}, {'model_id': 'fixture', 'provider': 'test'})
    assert result['identity'][0]['value'] == 'ABCDEFGHIJKLMNOPQR'
    assert result['phone'][0]['value'] == '10000000000'
    assert all(x['raw_text'] == raw and x['response_locator'] == 'response/line1' for x in audit['observations'])
    normalized = '证件号码' + result['identity'][0]['value'] + '联系电话' + result['phone'][0]['value']
    again, _ = parse_text(normalized, {'id': 'test-page'}, {'model_id': 'fixture', 'provider': 'test'})
    assert again == result


def test_role_label_and_colons_are_consumed_locally():
    assert labeled_segments('证件号码（营业执照号）：ABCDEFGHIJKLMNOPQR联系电话：10000000000') == [
        ('identity', 'ABCDEFGHIJKLMNOPQR'), ('phone', '10000000000')]


def test_label_text_in_company_name_is_not_a_boundary():
    raw = '承租人（乙方）某某联系电话服务有限公司'
    assert labeled_segments(raw) == [('tenant', '某某联系电话服务有限公司')]


def test_incomplete_id_and_multiple_phone_values_do_not_become_facts():
    for raw in ['证件号码ABC123联系电话10000000000',
                '证件号码ABCDEFGHIJKLMNOPQR联系电话1000000000010000000001',
                '证件号码ABCDEFGHIJKLMNOPQR联系电话10000000000或10000000001']:
        assert len(labeled_segments(raw)) == 1
        result, audit = parse_text(raw, {'id': 'test-page'}, {'model_id': 'fixture', 'provider': 'test'})
        assert not result['identity']  # No truncated ID admitted; an independent complete phone label may still parse.
        assert audit['observations'][0]['candidate_status'] == 'needs_read'
