const appointmentForm = document.querySelector('#appointment-form');
if (appointmentForm) {
    const slot = document.querySelector('#appointment-slot');
    function updateFee() {
        const fee = slot.selectedOptions[0]?.dataset.fee || '';
        appointmentForm.elements.expected_fee.value = fee;
        document.querySelector('#appointment-fee').textContent = fee ? `¥${fee}` : '-';
        document.querySelector('#appointment-slot-summary').textContent = fee ? slot.selectedOptions[0].textContent : '';
    }
    slot.addEventListener('change', updateFee);
    updateFee();
    document.querySelector('#appointment-patient').addEventListener('change', event => {
        document.querySelector('#appointment-search [name="patient_id"]').value = event.target.value;
    });
    appointmentForm.addEventListener('submit', () => {
        document.querySelector('#confirm-appointment').disabled = true;
    });
    window.addEventListener('pageshow', () => {
        document.querySelector('#confirm-appointment').disabled = ![...slot.options].some(option => option.value && !option.disabled) || appointmentForm.elements.patient_id.options.length < 2;
    });
}
