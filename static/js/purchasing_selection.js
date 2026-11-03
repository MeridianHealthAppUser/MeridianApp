(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('purchasing_selection', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
  const table = document.querySelector('[data-purchasing-selection]');
  const dialog = document.getElementById('purchasing-order-preview');
  const template = document.getElementById('purchasing-order-preview-row');
  if (!table || !dialog || !template) return;

  const button = table.querySelector('[data-order-selected]');
  const count = table.querySelector('[data-selected-products-count]');
  const clear = table.querySelector('[data-clear-product-selection]');
  const body = dialog.querySelector('[data-order-preview-rows]');
  const summary = dialog.querySelector('[data-order-preview-summary]');
  const form = dialog.querySelector('[data-purchase-create-form]');
  const create = dialog.querySelector('[data-create-purchase-order]');
  const total = form.querySelector('[name="items-TOTAL_FORMS"]');
  const supplier = form.querySelector('[name="supplier"]');
  const supplierHelp = form.querySelector('[data-purchase-supplier-help]');
  const supplierProducts = JSON.parse(document.getElementById('purchase-supplier-products')?.textContent || '{}');
  const quantities = new Map();
  let searching = false;
  const boxes = () => [...table.querySelectorAll('[data-select-product]')];
  const selected = () => boxes().filter(box => box.checked);

  function refreshSelection() {
    const available = boxes();
    const number = selected().length;
    button.disabled = searching || number === 0;
    clear.hidden = number === 0;
    count.textContent = number ? `${number} product${number === 1 ? '' : 's'} selected` : 'Select products to order';
    table.querySelectorAll('[data-select-all-products]').forEach(control => {
      control.disabled = available.length === 0;
      control.checked = available.length > 0 && available.every(box => box.checked);
      control.indeterminate = number > 0 && !control.checked;
    });
    available.forEach(box => box.closest('tr').classList.toggle('is-selected', box.checked));
  }

  function refreshSummary() {
    const inputs = [...body.querySelectorAll('[data-order-quantity]')];
    const valid = inputs.every(input => input.validity.valid && Number.isSafeInteger(input.valueAsNumber));
    const units = valid ? inputs.reduce((total, input) => total + input.valueAsNumber, 0) : null;
    summary.textContent = `${inputs.length} product${inputs.length === 1 ? '' : 's'} · ${valid ? `${units.toLocaleString()} unit${units === 1 ? '' : 's'}` : 'Set order quantities'}`;
    const productIds = [...body.querySelectorAll('[data-order-product-input]')].map(input => Number(input.value));
    let matching = 0;
    if (supplier) {
      [...supplier.options].forEach(option => {
        if (!option.value) return;
        const supplied = supplierProducts[option.value] || [];
        const eligible = productIds.every(id => supplied.includes(id));
        option.disabled = !eligible;
        if (eligible) matching++;
      });
      if (supplier.selectedOptions[0]?.disabled) supplier.value = '';
      supplierHelp.textContent = matching === 0
        ? 'No supplier supplies all selected products. Remove a product or ask Super Admin to update supplier links.'
        : supplier.value ? 'This supplier supplies all selected products.' : 'Choose a supplier. Suppliers that do not supply every selected product are unavailable.';
    }
    create.disabled = !inputs.length || !valid || !supplier?.value || matching === 0;
    [...body.querySelectorAll('[data-order-preview-row]')].forEach((row, index) => {
      row.querySelector('[data-order-product-input]').name = `items-${index}-product`;
      row.querySelector('[data-order-quantity]').name = `items-${index}-quantity`;
    });
    total.value = inputs.length;
  }

  function renderPreview() {
    if (dialog.dataset.serverBound === 'true') {
      refreshSummary();
      return;
    }
    body.replaceChildren();
    selected().forEach(box => {
      const row = template.content.firstElementChild.cloneNode(true);
      const product = box.dataset.productName;
      const strength = box.dataset.productStrength;
      const input = row.querySelector('[data-order-quantity]');
      const label = row.querySelector('[data-order-quantity-label]');
      const suggestion = Number(box.dataset.suggestedQuantity);
      row.dataset.productId = box.value;
      row.querySelector('[data-order-product-input]').value = box.value;
      row.querySelector('[data-order-product-name]').textContent = product;
      row.querySelector('[data-order-product-detail]').textContent = [strength, box.dataset.productCategory].filter(Boolean).join(' · ');
      row.querySelector('[data-order-suggested]').textContent = box.dataset.suggestedQuantity === '' ? '—' : box.dataset.suggestedQuantity;
      input.id = `purchase-order-quantity-${box.value}`;
      input.value = quantities.has(box.value) ? quantities.get(box.value) : (suggestion > 0 ? String(suggestion) : '');
      label.htmlFor = input.id;
      label.textContent = `Order quantity for ${product}${strength ? ` ${strength}` : ''}`;
      row.querySelector('[data-remove-order-product]').setAttribute('aria-label', `Remove ${product}${strength ? ` ${strength}` : ''}`);
      body.append(row);
    });
    refreshSummary();
    body.querySelector('[data-order-quantity]')?.focus();
  }

  listen(table, 'change', event => {
    const control = event.target;
    if (control.matches('[data-select-all-products]')) {
      boxes().forEach(box => {
        box.checked = control.checked;
        if (!box.checked) quantities.delete(box.value);
      });
    } else if (control.matches('[data-select-product]')) {
      if (!control.checked) quantities.delete(control.value);
    } else return;
    delete dialog.dataset.serverBound;
    refreshSelection();
  });

  listen(clear, 'click', () => {
    boxes().forEach(box => { box.checked = false; });
    quantities.clear();
    refreshSelection();
  });
  listen(dialog, 'dialog:open', renderPreview);
  listen(supplier, 'change', refreshSummary);
  listen(dialog, 'input', event => {
    if (!event.target.matches('[data-order-quantity]')) return;
    quantities.set(event.target.closest('[data-order-preview-row]').dataset.productId, event.target.value);
    refreshSummary();
  });
  listen(dialog, 'click', event => {
    const remove = event.target.closest('[data-remove-order-product]');
    if (!remove) return;
    const row = remove.closest('[data-order-preview-row]');
    const index = [...body.children].indexOf(row);
    boxes().filter(box => box.value === row.dataset.productId).forEach(box => { box.checked = false; });
    quantities.delete(row.dataset.productId);
    row.remove();
    refreshSelection();
    refreshSummary();
    if (!body.children.length) {
      dialog.close();
      boxes()[0]?.focus();
    } else {
      const remaining = [...body.querySelectorAll('[data-remove-order-product]')];
      remaining[Math.min(index, remaining.length - 1)]?.focus();
    }
  });
  listen(form, 'submit', event => {
    refreshSummary();
    if (create.disabled || !form.reportValidity()) event.preventDefault();
  });
  listen(document, 'messages:searching', () => {
    searching = true;
    button.disabled = true;
  });
  // A new search replaces the current page's products. Never order an invisible stale selection.
  listen(document, 'messages:searched', () => {
    searching = false;
    if (dialog.open) dialog.close();
    body.replaceChildren();
    quantities.clear();
    boxes().forEach(box => { box.checked = false; });
    refreshSelection();
  });
  refreshSelection();
  if (dialog.dataset.serverBound === 'true') refreshSummary();
});
