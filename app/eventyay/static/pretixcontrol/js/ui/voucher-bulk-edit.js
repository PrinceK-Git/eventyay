document.querySelectorAll('[data-voucher-bulk-edit]').forEach((form) => {
    function syncFields() {
        form.querySelectorAll('[data-voucher-change-field]').forEach((fieldset) => {
            const toggle = fieldset.querySelector('[data-voucher-change-toggle]');
            fieldset.querySelectorAll('input, select, textarea, button').forEach((input) => {
                if (input !== toggle) {
                    input.disabled = !toggle.checked;
                }
            });
        });
    }

    function invalidatePreview() {
        form.querySelectorAll('[data-voucher-preview], [data-voucher-confirm]').forEach((element) => {
            element.hidden = true;
            element.classList.add('hidden');
        });
        const confirm = form.querySelector('[data-voucher-confirm]');
        if (confirm) {
            confirm.disabled = true;
        }
        const preview = form.querySelector('input[name="preview"]');
        if (preview) {
            preview.value = '';
        }
    }

    form.addEventListener('change', () => {
        syncFields();
        invalidatePreview();
    });
    form.addEventListener('input', invalidatePreview);
    syncFields();
});
