from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENT = Decimal('0.01')
MAX_AMOUNT = Decimal('99999999.99')
PAYMENT_METHODS = {'cash': '现金', 'card': '银行卡', 'wechat': '微信', 'alipay': '支付宝'}


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_money(value, label='金额'):
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f'{label}请输入有效金额') from None
    if not amount.is_finite() or amount < 0 or amount > MAX_AMOUNT:
        raise ValueError(f'{label}须在 0 至 {MAX_AMOUNT} 元之间')
    if amount != money(amount):
        raise ValueError(f'{label}最多保留两位小数')
    return money(amount)


def bill_summary(bill):
    registration = bill.registration
    registration_item = {
        'name': '门诊挂号费',
        'spec': f'{registration.doctor.department.name} / {registration.doctor.name}',
        'quantity': 1,
        'unit_price': money(registration.fee),
        'amount': money(registration.fee),
    }
    legacy_combined = False
    error = None
    if bill.prescription:
        items = [
            {'name': item.drug_name, 'spec': (' / '.join(filter(None, (item.catalog.specification,
                item.catalog.unit, item.dosage))) if item.catalog else item.dosage or '药品费用'),
             'quantity': item.quantity, 'unit_price': money(item.unit_price),
             'amount': money(item.unit_price) * item.quantity}
            for item in bill.prescription.items
        ]
        drug_total = sum((item['amount'] for item in items), Decimal('0.00'))
        # Only recognize the unambiguous historical one-bill-per-visit format.
        if (registration.fee > 0 and len(registration.bills) == 1
                and money(bill.total_amount) == drug_total + registration_item['amount']):
            items.append(registration_item)
            legacy_combined = True
    else:
        items = [registration_item]
    total = sum((item['amount'] for item in items), Decimal('0.00'))
    if not items or any(item['quantity'] <= 0 or item['unit_price'] < 0 for item in items):
        error = '费用项目无效，请核对原挂号或处方。'
    elif total != money(bill.total_amount) or not 0 <= total <= MAX_AMOUNT:
        error = '费用明细合计与账单应收不一致，请先核对，暂不能收款。'
    if bill.is_void:
        error = '挂号已取消，此未缴账单已作废，不能收款。'
    return {'items': items, 'total': total, 'error': error, 'legacy_combined': legacy_combined}


def refund_eligibility(bill):
    if not bill.is_paid:
        return '待缴费账单不能退费。'
    if not bill.payment:
        return '此历史账单缺少原收款流水，请先核对原凭证，暂不能办理退费。'
    if bill.refund:
        return '此账单已全额退费，不能重复退费。'
    if bill.payment.amount_cents <= 0:
        return '零金额账单没有可退金额。'
    if bill.payment.amount != money(bill.total_amount) or bill_summary(bill)['error']:
        return '原收款金额、账单或费用明细不一致，请先核对，暂不能退费。'
    return None
