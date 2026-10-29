"""Non-publishing arithmetic for one contract's stated period.

Payment uncertainty must not turn into zero paid or a reliable debt. This view
does not extend expired contracts or decide links, alterations or legal effect.
"""
from calendar import monthrange
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .domain import AS_OF, GRADES, USES, upper_money


def contract_obligation(fields, rates, reliefs=(), *, unresolved=(), as_of=AS_OF, termination_month=None):
    f = dict(fields)
    if f.get('doc_type') == '变更':
        raise ValueError('变更件不能独立确定应缴，请关联原合同')
    mode = f.get('mode')
    required = {'mode', 'start', 'end'} | ({'annual'} if mode == '固定' else {'area', 'use', 'grade'})
    direct_financial_uncertainty = {'end_date_alteration', 'end_date_crossout_adjudication', 'mode_unselected'}
    if mode == '固定':
        direct_financial_uncertainty |= {'annual_upper', 'annual_alteration', 'annual_crossout_adjudication',
                                         'annual_upper_conflict', 'amount_rate_conflict'}
    else:
        direct_financial_uncertainty |= {'area_alteration', 'area_crossout_adjudication',
                                         'original_area_crossout_requires_adjudication'}
    if set(unresolved) & (required | direct_financial_uncertainty):
        raise ValueError('计租要素仍未确定')
    if mode not in {'固定', '随标准调整'}:
        raise ValueError('计租方式未确定')
    try:
        start, finish = date.fromisoformat(f['start']), date.fromisoformat(f['end'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('本期起止日期不完整或无效') from exc
    if start > finish:
        raise ValueError('本期起始日期晚于终止日期')

    def number(value):
        try:
            v = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError('计租数值无效') from exc
        if not v.is_finite() or v < 0:
            raise ValueError('计租数值必须为非负有限数')
        return v

    def cents(value):
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    if mode == '固定':
        fixed = cents(number(f.get('annual')))
        if f.get('annual_upper') and upper_money(f['annual_upper']) != fixed:
            raise ValueError('大小写年租金不一致，需先明确采用依据')
    else:
        if f.get('use') not in USES or f.get('grade') not in GRADES:
            raise ValueError('浮动计租的用途或级别未确定')
        area = number(f.get('area'))
        if area <= 0:
            raise ValueError('面积必须大于0')
    finish_capped = min(finish, date(as_of.year, 12, 31))
    termination = None
    if termination_month is not None:
        if not isinstance(termination_month, str) or len(termination_month) != 7:
            raise ValueError('收回月份需为YYYY-MM，不能推测具体日')
        try:
            month_start = date.fromisoformat(termination_month + '-01')
        except ValueError as exc:
            raise ValueError('收回月份无效') from exc
        if (month_start.year, month_start.month) < (start.year, start.month):
            raise ValueError('收回月份早于本合同起始月份')
        # Internal month-end arithmetic is not a source claim about the day.
        cutoff = date(month_start.year, month_start.month, monthrange(month_start.year, month_start.month)[1])
        finish_capped = min(finish_capped, cutoff)
        termination = {'month': termination_month, 'day': None, 'precision': 'month', 'legal_effect': 'not_determined'}
    annual = []
    for year in range(start.year, finish_capped.year + 1):
        left = max(start, date(year, 1, 1))
        right = min(finish_capped, date(year, 12, 31))
        if left > right:
            continue
        months = right.month - left.month + 1
        basis = {'kind': 'contract_fixed_annual'}
        if mode == '固定':
            base = fixed
        else:
            available = [r for r in rates if r['用途'] == f['use'] and str(r['执行起始日'])[:10] <= f'{year}-01-01']
            if not available:
                raise ValueError(f'{year}年缺适用费率')
            latest_day = max(str(r['执行起始日'])[:10] for r in available)
            latest = [r for r in available if str(r['执行起始日'])[:10] == latest_day]
            if len(latest) != 1:
                raise ValueError('适用费率存在重复版本')
            rate = number(latest[0][f['grade']])
            base = cents(area * rate)
            basis = {'kind': 'approved_rate_table', 'effective_date': latest_day, 'use': f['use'], 'grade': f['grade'], 'rate': str(rate), 'area': str(area)}
        due = cents(base * months / 12)
        applicable = [r for r in reliefs if int(r['减免起始年度']) <= year <= int(r['减免终止年度'])]
        if len(applicable) > 1:
            raise ValueError('同年多个减免依据未明确叠加规则')
        if applicable:
            raw = str(applicable[0]['减免比例']).strip()
            ratio = number(raw.rstrip('%')) / (100 if raw.endswith('%') else 1)
            if ratio > 1:
                raise ValueError('减免比例不在0到1之间')
            due = cents(due * (1 - ratio))
            basis['relief_approval'] = applicable[0]['批准文号']
        annual.append({'year': year, 'months': months, 'due': str(due), 'paid': None, 'arrears': None, 'basis': basis})
    return {'status': 'obligation_preview_only', 'scope': 'stated_contract_period',
            'start': start.isoformat(), 'end': finish.isoformat(), 'as_of': as_of.isoformat(), 'termination': termination,
            'annual': annual, 'due': str(cents(sum((Decimal(x['due']) for x in annual), Decimal(0)))),
            'paid': None, 'arrears': None, 'overpaid': None, 'payment_status': 'unverified',
            'rounding_policy': 'half_up', 'calculation_dependencies': sorted(required),
            'unresolved_items_retained': sorted(set(unresolved)),
            'legal_effect': 'not_determined', 'business_complete': False, 'publication_allowed': False,
            'note': '仅本合同载明期间的应缴算术预览；不延续到期合同，不代表宗地累计欠缴或核验办结。'}
