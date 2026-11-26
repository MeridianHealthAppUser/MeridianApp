(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('subscription_payments', lifecycle => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  document.querySelectorAll('dialog.subscription-payments').forEach(dialog => {
    listen(dialog, 'close', () => {
      const url = new URL(window.location.href);
      if (url.searchParams.get('payments') !== dialog.id.replace('subscription-payments-', '')) return;
      url.searchParams.delete('payments');
      url.hash = '';
      const target = url.pathname + url.search;
      if (window.MeridianNavigation?.replaceUrl) window.MeridianNavigation.replaceUrl(target);
      else window.history.replaceState(window.history.state, '', target);
    });
    const boxes = [...dialog.querySelectorAll('[data-payment-select]')];
    const all = dialog.querySelector('[data-payment-select-all]');
    // The Bulk discount dialog sits beside this one; its form owns the table's checkboxes.
    const bulk = document.getElementById(dialog.id.replace('subscription-payments-', 'bulk-discount-dialog-'));
    const bulkForm = bulk?.querySelector('[data-bulk-discount-form]');
    const refreshSelection = () => {
      const count = boxes.filter(box => box.checked).length;
      all.checked = count > 0 && count === boxes.length;
      all.indeterminate = count > 0 && count < boxes.length;
      all.disabled = !boxes.length;
      if (!bulkForm) return;
      const scope = bulkForm.elements.scope.value;
      const label = bulkForm.querySelector('[data-payment-selected-count]');
      label.hidden = scope !== 'selected';
      label.textContent = count ? `${count} payment${count === 1 ? '' : 's'} selected in the table.` : 'Tick the payments to discount in the table first.';
      bulkForm.querySelector('[data-bulk-effective-date]').hidden = scope !== 'future';
      bulkForm.querySelector('button[type="submit"]').disabled = scope === 'selected' && !count;
    };
    boxes.forEach(box => listen(box, 'change', () => {
      if (box.checked && bulkForm) bulkForm.elements.scope.value = 'selected';
      refreshSelection();
    }));
    listen(all, 'change', () => {
      boxes.forEach(box => { box.checked = all.checked; });
      if (all.checked && bulkForm) bulkForm.elements.scope.value = 'selected';
      refreshSelection();
    });
    if (bulkForm) {
      listen(bulkForm.elements.scope, 'change', refreshSelection);
      listen(bulk, 'dialog:open', refreshSelection);
    }
    refreshSelection();
  });
  document.querySelectorAll('[data-line-discount-form]').forEach(form => {
    const preview = () => {
      const raw = form.elements.value.value;
      const output = form.querySelector('[data-discount-preview]');
      const discount = Number(raw);
      if (raw === '' || !Number.isFinite(discount) || discount < 0 || (form.elements.kind.value === 'percent' && discount > 100)) {
        output.textContent = 'Enter a discount to preview the new amount';
        return;
      }
      const base = Math.round(Number(form.dataset.discountBase) * 100);
      const value = Math.round(discount * 100);
      const cents = Math.max(0, form.elements.kind.value === 'percent'
        ? Math.floor((base * (10000 - value) + 5000) / 10000) : base - value);
      output.textContent = `New amount: R${(cents / 100).toFixed(2)}`;
    };
    listen(form, 'input', preview);
    listen(form, 'change', preview);
  });
  // A payment's discount opens in a dialog of its own (dialogs.js): straight to its amount.
  document.querySelectorAll('dialog.subscription-line-dialog').forEach(dialog => {
    listen(dialog, 'dialog:open', () => dialog.querySelector('input[name="value"]')?.focus());
  });
});
