(() => {
    const body = document.querySelector('#drugTable tbody');
    const add = document.querySelector('#add-drug');
    const format = cents => `\u00a5${(cents / 100).toFixed(2)}`;
    function refresh(changedSelect) {
        let total = 0;
        const counts = new Map();
        body.querySelectorAll('select').forEach(select => {
            if (select.value) counts.set(select.value, (counts.get(select.value) || 0) + 1);
        });
        for (const row of body.rows) {
            const select = row.querySelector('select');
            const option = select.selectedOptions[0];
            const version = row.querySelector('[name="revision[]"]');
            if (select === changedSelect) version.value = option?.dataset.revision || '';
            const valid = option?.dataset.revision && version.value === option.dataset.revision;
            select.setCustomValidity(!select.value ? '' : !valid ? '药品资料已变化，请重新选择并核对' : counts.get(select.value) > 1 ? '药品重复，请合并数量' : '');
            const quantity = Number(row.querySelector('[name="quantity[]"]').value);
            const cents = valid ? Number(option.dataset.cents) : 0;
            const subtotal = Number.isInteger(quantity) && quantity > 0 && quantity <= 99999 ? cents * quantity : 0;
            total += subtotal;
            row.querySelector('.drug-price').textContent = valid ? `${format(cents)} / ${option.dataset.unit}` : '\u2014';
            row.querySelector('.drug-subtotal').textContent = valid ? format(subtotal) : '\u2014';
            row.querySelector('.remove-drug').disabled = body.rows.length === 1;
        }
        document.querySelector('#prescription-total').textContent = format(total);
        add.disabled = body.rows.length >= 50 || !body.querySelector('option[data-revision]');
    }
    body.addEventListener('change', event => refresh(event.target.matches('select') ? event.target : null));
    body.addEventListener('input', () => refresh());
    body.addEventListener('click', event => {
        const button = event.target.closest('.remove-drug');
        if (button && body.rows.length > 1) { button.closest('tr').remove(); refresh(); }
    });
    add.addEventListener('click', () => {
        if (body.rows.length >= 50) return;
        const row = body.rows[0].cloneNode(true);
        row.querySelector('select').value = '';
        row.querySelectorAll('input').forEach(input => { input.value = input.name === 'quantity[]' ? '1' : ''; });
        body.appendChild(row);
        refresh();
    });
    refresh();
})();
