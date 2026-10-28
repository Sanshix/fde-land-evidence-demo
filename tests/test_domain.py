from copy import deepcopy
from decimal import Decimal

import pytest

from app.domain import cadastre, compute, money, upper_money, validate_fields
from app.recognition import assess, context_candidate, extract


def fields(**changes):
    value = {
        "tenant": "模拟测试人",
        "parcel_no": "9-9-999",
        "address": "测试街1号",
        "area": "10",
        "use": "住宅",
        "grade": "一级",
        "start": "2026-01-01",
        "end": "2026-12-31",
        "mode": "固定",
        "annual": "120",
        "phone": "10000000000",
    }
    return value | changes


RATES = [{"执行起始日": "2022-01-01", "用途": "住宅", "一级": "2.4"}]


def contract(**changes):
    return {"id": "synthetic-contract", "fields": fields(**changes)}


def payment(number="1", amount="30", year="2026", day="2026-06-01", **changes):
    return {
        "票据号": number,
        "金额（元）": amount,
        "所属年度": year,
        "缴费日期": day,
        "缴款人": "模拟测试人",
    } | changes


def test_financial_decimal_and_uppercase():
    assert money("1.005") == Decimal("1.01")
    assert upper_money("伍拾柒元陆角叁分") == Decimal("57.63")
    with pytest.raises(ValueError):
        upper_money("疑似五十元")


def test_months_inclusive_and_end_of_year_continuation():
    r = compute([contract(start="2026-02-20", end="2027-02-19")], RATES)
    assert r["due"] == "110.00"
    r = compute([contract(start="2025-01-01", end="2025-12-31")], RATES)
    assert r["due"] == "240.00"  # 2026 full year, not January–September


def test_uppercase_fixed_and_variable_rates():
    r = compute([contract(annual="75.63", annual_upper="伍拾柒元陆角叁分")], RATES)
    assert r["due"] == "57.63"
    assert r["issues"][0]["type"] == "年租金大小写不一致"
    assert compute([contract(mode="随标准调整", area="10.01")], RATES)["due"] == "24.02"


def test_relief_after_annual_and_separate_contract_rounding():
    r = compute(
        [contract()], RATES, reliefs=[{"减免起始年度": 2026, "减免终止年度": 2026, "减免比例": "50%"}]
    )
    assert r["due"] == "60.00"
    a, b = (
        contract(start="2026-01-01", end="2026-06-30", annual="100.01"),
        contract(start="2026-07-01", end="2027-01-01", annual="100.01"),
    )
    b["id"] = "new"
    assert compute([a, b], RATES)["due"] == "100.02"


def test_specified_overpayment_does_not_clear_another_year():
    p = payment(amount="150", year="2025")
    r = compute([contract(start="2025-01-01")], RATES, [p])
    # 口径七（二）：注明年度的超额部分为多缴，不冲抵其他年度；逐年明细的“实缴”含该年度多缴（同标准答案）
    assert r["annual"][0]["paid"] == "150.00" and r["annual"][0]["arrears"] == "0.00"
    assert r["annual"][1]["paid"] == "0.00"
    assert r["overpaid"] == "30.00"
    assert r["arrears"] == "90.00"


def test_unspecified_earliest_and_receipt_dedupe():
    p = payment(amount="150", year=None)
    r = compute([contract(start="2025-01-01")], RATES, [p, deepcopy(p)])
    assert r["annual"][0]["paid"] == "120.00"
    assert r["annual"][1]["paid"] == "30.00"
    assert r["paid"] == "150.00"
    assert any(i["type"] == "缴费记录异常" and "重复" in i["reason"] for i in r["issues"])
    # 同票据号内容不一致：按对产权人有利（金额较大）暂计，交业务层列入存疑待确认
    conflict = compute([contract()], RATES, [p, p | {"金额（元）": "151"}])
    assert conflict["paid"] == "151.00" and conflict["conflicts"][0]["difference"] == "1.00"


def test_unannotated_payment_runs_forward_past_payment_year():
    # 口径七（三）：依次向后冲抵，不以缴费日期截断（练习集 LX-009 2023 年补缴 2023 至 2025 年度）
    r = compute([contract(start="2024-01-01")], RATES, [payment(amount="240", year=None, day="2024-03-01")])
    assert [a["paid"] for a in r["annual"]] == ["120.00", "120.00", "0.00"]


def test_negative_balance_and_payment_after_base_date_counted():
    assert compute([contract()], RATES, [payment(amount="200")])["arrears"] == "-80.00"
    # 口径八：实缴之和不以基准日截断（标准答案 LX-031/LX-040 的 2026-11 缴款计入），并在归集明细中注明
    r = compute([contract()], RATES, [payment(day="2026-11-01")])
    assert r["paid"] == "30.00" and "晚于核算基准日" in r["allocations"][0]["note"]


def test_overlap_requires_explicit_human_scenario():
    a, b = contract(start="2025-01-01"), contract(start="2026-01-01")
    with pytest.raises(ValueError, match="重叠"):
        compute([a, b], RATES)


@pytest.mark.parametrize(
    "change", [{"area": "NaN"}, {"mode": ""}, {"annual": "-1"}, {"start": "wrong"}, {"start": "2027-01-01"}]
)
def test_invalid_critical_inputs_block(change):
    assert validate_fields(fields(**change))


def test_old_cadastre_mapping():
    assert cadastre("7-4-118") == cadastre("000000007004GB00118F00070291")
    assert cadastre("07040118") is None


def test_high_confidence_agreement_does_not_confirm():
    cs = {
        "area": [
            {"value": "207.18", "score": 0.999, "bbox": [1, 1, 5, 5], "model": "a"},
            {"value": "207.18", "score": 0.999, "bbox": [1, 1, 5, 5], "model": "b"},
        ]
    }
    risks = assess(cs)
    assert any(r["code"] == "visual_review" for r in risks)
    cs["area"][1]["value"] = "187.18"
    assert any(r["code"] == "disagreement" for r in assess(cs))


def test_context_inference_cannot_be_published():
    # Explicit synthetic payload tests the contract, not a real LLM response.
    result = context_candidate(
        {
            "field": "area",
            "value": "187.18",
            "reason": "登记值看似吻合，但原件不清",
            "citations": [{"page": "synthetic", "box": [1, 1, 3, 3]}],
        }
    )
    assert result["can_publish"] is False
    assert result["verified"] is False
    assert context_candidate({"status": "cannot_determine"})["status"] == "cannot_determine"
    with pytest.raises(ValueError):
        context_candidate({"field": "area", "value": "187.18"})


def test_plain_checkbox_label_not_inferred():
    c = extract(
        [{"text": "口固定 口随基准地价调整", "score": 0.99, "box": [0, 0, 50, 20]}], "page", "test-stub"
    )
    assert c["mode"] == []
