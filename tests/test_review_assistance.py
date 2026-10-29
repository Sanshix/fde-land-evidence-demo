from app.review_assistance import assistance, prioritize


def document(identifier, parcel=None, state="needs_human", candidates=None):
    return {"id": identifier, "state": state, "pages": [{"id": "page-1"}],
            "review": {"fields": {"parcel_no": parcel}} if parcel else None,
            "result": {"candidates": candidates or {}, "risks": []}}


def test_seal_region_is_never_invented_and_cross_document_regions_are_excluded():
    doc = document("one", candidates={
        "area": [{"value": "100", "text_role": "crossed_out", "source_page_id": "page-1", "bbox": [1, 2, 20, 30]},
                 {"value": "120", "text_role": "current", "source_page_id": "page-1", "bbox": [21, 2, 40, 30]}],
        "altered_sealed": [{"value": "是", "source_page_id": "outside", "bbox": [1, 2, 20, 30]}]})
    data = assistance(doc)
    assert data["altered"] and data["field"] == "area"
    assert [c["value"] for c in data["candidates"]] == ["100", "120"]
    assert data["seal_regions"] == []
    assert "未取得签章区域" in data["seal_region_note"]
    assert "amount_impact" not in data


def test_real_seal_region_retained_without_treating_candidate_as_human_decision():
    doc = document("one", candidates={"altered_field": [{"value": "土地面积"}],
        "altered_sealed": [{"value": "否", "source_page_id": "page-1", "bbox": [1, 2, 20, 30]}]})
    data = assistance(doc)
    assert data["altered"] and len(data["seal_regions"]) == 1
    assert "decision" not in data


def test_last_known_pending_contract_is_prioritized_over_simple_unrelated_contract():
    docs = [document("published", "07-04-0118", "published"),
            document("last", "7-4-118", "held"), document("simple", "7-4-119")]
    order = prioritize(docs)
    assert order["last"]["rank"] < order["simple"]["rank"]
    assert order["last"]["pending_in_group"] == 1 and order["last"]["published_in_group"] == 1
    assert "当前已知材料链" in order["last"]["reason"]


def test_conflicting_parcel_candidates_do_not_create_a_group_and_drafts_are_explicit():
    doc = document("conflict", candidates={"parcel_no": [{"value": "1-2-3"}, {"value": "1-2-4"}]})
    assert prioritize([doc])["conflict"]["group_key"] is None
    assert prioritize([doc], {"conflict": {"fields": {"parcel_no": "1-2-3"}}})["conflict"]["group_key"] == "1-2-3"
