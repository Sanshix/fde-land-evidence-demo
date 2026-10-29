"""Layout block extraction preserves ambiguous values and never invents source geometry."""

from copy import deepcopy

import pytest

from app.ocr_layout import baidu_candidates

PROFILE = {"provider": "baidu_ai_ocr", "model_id": "synthetic-ocr"}
PAGE = {"id": "actual-request-page"}


def document(layouts, **metadata):
    return {"pages": [{"meta": {"page_width": 1240, "page_height": 1754}, "page_angle": 0,
                       "display_size_ratio": 1, "layouts": layouts, **metadata}]}


def block(text, **extras):
    return {"text": text, "type": "table", "layout_id": "layout-one", "score": .99,
            "position": [100, 200, 900, 300],
            "polygon": [[100, 200], [1000, 200], [1000, 500], [100, 500]], **extras}


def extract(raw, box=(0, 0, 1240, 1754)):
    return baidu_candidates(raw, PAGE, PROFILE, box)


def values(candidates, field):
    return {c["value"] for c in candidates[field]}


def test_html_contract_fields_keep_source_and_ambiguous_area():
    html = """<table>
    <tr><td>承租人（乙方）</td><td colspan="3">模拟公司</td></tr>
    <tr><td>证件号码</td><td>99ABC1234567890ZZZ</td><td>联系电话</td><td>10000000001</td></tr>
    <tr><td>地 号</td><td>07-04-0118</td></tr>
    <tr><td>坐 落</td><td>测试大街199号</td></tr>
    <tr><td>土地面积（ $ m^{2} $）</td><td>187.18 207.18</td><td>土地用途</td><td>住宅用地</td></tr>
    <tr><td>租赁期限</td><td>2017年4月1日至2027年3月31日</td></tr>
    <tr><td>年租金标准</td><td>1. 60元/ $ m^{{2}} $·年</td></tr>
    <tr><td>年租金（小写）</td><td>435.08元</td><td>年租金（大写）</td><td>肆佰叁拾伍元零捌分</td></tr>
    <tr><td>计租方式</td><td>☐固定 ☑随基准地价调整</td></tr></table>"""
    candidates, diagnostics = extract(document([block(html)]))
    assert values(candidates, "tenant") == {"模拟公司"}
    assert values(candidates, "identity") == {"99ABC1234567890ZZZ"}
    assert values(candidates, "area") == set()
    area_observation = next(o for o in diagnostics['observations'] if o['field']=='area')
    assert area_observation['raw_value'] == '187.18 207.18'
    assert area_observation['candidate_status'] == 'needs_read' 
    assert values(candidates, "start") == {"2017-04-01"}
    assert values(candidates, "end") == {"2027-03-31"}
    assert values(candidates, "mode") == {"随标准调整"}
    assert values(candidates, "rate_standard") == {"1.60"}  # Layout syntax normalizes decimal spacing, not unit exponents.
    assert values(candidates, "annual") == {"435.08"}
    for candidate in (c for rows in candidates.values() for c in rows):
        assert candidate["source_page_id"] == PAGE["id"]
        assert candidate["source_locator"] is None
        assert candidate["response_locator"].startswith("response/pages/0/layouts/0/")
        assert candidate["bbox"] == pytest.approx([100, 200, 1000, 500])
        assert candidate["score"] is None and candidate["score_meaning"] == "not_provided"
        assert candidate["kind"] == "ocr" and not candidate["verified"] and not candidate["can_publish"]
    assert diagnostics["coordinate_frame_verified"] is True
    assert diagnostics["layout_score_is_not_text_confidence"] is True


def test_unknown_checkbox_is_not_inferred_and_two_checks_remain_conflict():
    raw = document([block("<table><tr><td>计租方式</td><td>☐固定 ☐随基准地价调整</td></tr></table>")])
    assert not extract(raw)[0]["mode"]
    raw["pages"][0]["layouts"][0]["text"] = "<table><tr><td>计租方式</td><td>☑固定 ☑随基准地价调整</td></tr></table>"
    assert values(extract(raw)[0], "mode") == {"固定", "随标准调整"}


def test_renewal_and_original_contract_reference_use_actual_observation():
    raw = document([
        block("国有建设用地使用权租赁合同", type="doc_title"),
        block("合同编号：××国土资租（2017）007号", type="text"),
        block("本合同为原合同（编号：××国土资租（2012）006号）期满续签。", type="text"),
    ])
    candidates, _ = extract(raw)
    assert values(candidates, "doc_type") == {"续期"}
    assert values(candidates, "contract_no") == {"××国土资租(2017)007号"}
    assert values(candidates, "original_contract_no") == {"××国土资租(2012)006号"}
    # A body clause mentioning a lease does not identify a document as a new contract.
    page_two, _ = extract(document([block("本租赁合同双方签字后生效。", type="text")]))
    assert not page_two["doc_type"]


def test_change_agreement_uses_explicit_new_tenant_only():
    raw = document([block("国有土地租赁合同变更协议", type="doc_title"), block("""<table>
    <tr><td>原承租人</td><td>原承租人甲</td></tr>
    <tr><td>新承租人</td><td>新承租人乙</td></tr></table>""")])
    candidates, _ = extract(raw)
    assert values(candidates, "doc_type") == {"变更"}
    assert values(candidates, "tenant") == {"新承租人乙"}


@pytest.mark.parametrize("changes", [
    {"meta": {}}, {"meta": {"page_width": 1754, "page_height": 1240}},
    {"page_angle": 90}, {"page_angle_warning": "orientation uncertain"},
    {"display_size_ratio": .5}, {"meta": {"page_width": 1240, "page_height": 1754, "transform": [1, 0, 0, 1, 5, 7]}},
])
def test_unverified_frames_retain_text_but_never_map_boxes(changes):
    raw = document([block("土地面积：12.50", type="text")], **changes)
    candidates, diagnostics = extract(raw)
    assert values(candidates, "area") == {"12.50"}
    assert candidates["area"][0]["bbox"] is None
    assert not diagnostics["coordinate_frame_verified"]


def test_position_and_polygon_disagreement_cannot_supply_primary_evidence():
    candidates, _ = extract(document([block("土地面积：12.50", type="text",
                                              polygon=[[10, 20], [900, 20], [900, 500], [10, 500]])]))
    assert candidates["area"][0]["bbox"] is None


def test_known_resized_sent_frame_maps_to_original_crop():
    raw = document([block("土地面积：12.50", type="text", position=[100, 200, 300, 400], polygon=None)],
                   meta={"page_width": 1555, "page_height": 2200})
    candidates, diagnostics = extract(raw, box=[20, 40, 2500, 3548])
    assert diagnostics["coordinate_frame_verified"] is True
    assert candidates["area"][0]["bbox"] == pytest.approx([20 + 100 / 1555 * 2480,
        40 + 200 / 2200 * 3508, 20 + 400 / 1555 * 2480, 40 + 600 / 2200 * 3508])


def test_foreign_second_page_is_not_assigned_to_single_request():
    raw = document([block("土地面积：12.50", type="text")])
    raw["pages"].append(deepcopy(raw["pages"][0]))
    candidates, diagnostics = extract(raw)
    assert not any(candidates.values())
    assert diagnostics["rejected"] == "single_input_page_response_count_mismatch"


def test_table_html_can_be_linked_to_layout_without_fabricating_cell_regions():
    table = block("", table_html="", position=[100, 200, 900, 300])
    raw = document([table])
    raw["pages"][0]["tables"] = [{"layout_id": "layout-one", "table_html": "<table><tr><td>承租人（乙方）</td><td>模拟乙</td></tr></table>"}]
    candidate = extract(raw)[0]["tenant"][0]
    assert candidate["bbox"] == pytest.approx([100, 200, 1000, 500])
    assert "非精确单元格" in candidate["reason"]


def test_legacy_plain_text_response_does_not_invent_coordinates():
    candidates, diagnostics = extract({"pages": [{"text": "土地面积：１２３.４５平方米\n年租金：120元"}]})
    assert values(candidates, "area") == {"123.45"}
    assert candidates["area"][0]["bbox"] is None
    assert candidates["area"][0]["score"] is None
    assert diagnostics["text_only"] is True
