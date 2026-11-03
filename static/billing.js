const paymentForm = document.querySelector('#payment-form');
if (paymentForm) {
    const method = paymentForm.querySelector('#payment-method');
    const received = paymentForm.querySelector('#received-amount');
    const expected = paymentForm.elements.expected_amount.value;
    const cents = (value) => {
        if (!/^\d+(\.\d{1,2})?$/.test(value)) return null;
        const [whole, fraction = ''] = value.split('.');
        return Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
    };
    const updateChange = () => {
        const cash = method.value === 'cash';
        received.readOnly = !cash;
        if (!cash) received.value = expected;
        const amount = cents(received.value);
        const change = amount === null ? null : amount - cents(expected);
        received.setCustomValidity(change === null || change < 0 ? '请填写不少于应收金额的实收金额，最多两位小数' : '');
        document.querySelector('#change-amount').textContent = change === null || change < 0 ? '-' : `¥${(change / 100).toFixed(2)}`;
    };
    method.addEventListener('change', updateChange);
    received.addEventListener('input', updateChange);
    updateChange();
    paymentForm.addEventListener('submit', () => {
        const button = paymentForm.querySelector('#confirm-payment');
        button.disabled = true;
        button.textContent = '正在保存收款…';
    });
    window.addEventListener('pageshow', () => {
        const button = paymentForm.querySelector('#confirm-payment');
        button.disabled = false;
        button.textContent = `确认收款 ¥${expected}`;
    });
}

const refundForm = document.querySelector('#refund-form');
if (refundForm) {
    const button = refundForm.querySelector('#confirm-refund');
    refundForm.addEventListener('submit', () => {
        button.disabled = true;
        button.textContent = '正在保存退费…';
    });
    window.addEventListener('pageshow', () => {
        button.disabled = false;
        button.textContent = '确认登记全额退费';
    });
}
