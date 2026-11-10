(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('purchase_order_receiving', lifecycle => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
  document.querySelectorAll('[data-purchase-return-url]').forEach(dialog => {
    listen(dialog, 'close', () => {
      const url = dialog.dataset.purchaseReturnUrl;
      if (window.MeridianNavigation?.replaceUrl) window.MeridianNavigation.replaceUrl(url);
      else window.history.replaceState(window.history.state, '', url);
    });
  });
  document.querySelectorAll('[data-purchase-receive-form]').forEach(form => {
    const rows = [...form.querySelectorAll('[data-receive-line]')];
    const button = form.querySelector('[data-record-purchase-receipt]');
    function refresh() {
      let valid = true, total = 0;
      rows.forEach(row => {
        const input = row.querySelector('[data-receive-quantity]');
        const quantity = input.value === '' ? 0 : input.valueAsNumber;
        row.querySelectorAll('input[name$="-batch"], input[name$="-expires_on"]').forEach(field => { field.required = false; });
        if (!Number.isSafeInteger(quantity) || quantity < 0 || quantity > Number(row.dataset.remaining)) valid = false;
        else total += quantity;
      });
      button.disabled = !valid || total === 0;
      button.textContent = total > 0 && valid ? `Receive ${total.toLocaleString()} units into stock` : 'Receive into stock';
    }
    listen(form, 'input', refresh);
    listen(form.querySelector('[data-receive-all]'), 'click', () => {
      rows.forEach(row => { row.querySelector('[data-receive-quantity]').value = row.dataset.remaining; });
      refresh();
    });
    listen(form, 'submit', event => { refresh(); if (button.disabled || !form.reportValidity()) event.preventDefault(); });
    refresh();
  });
});
