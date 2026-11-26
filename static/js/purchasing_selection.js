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
  const totals = dialog.querySelector('[data-order-totals]');
  const priceNote = dialog.querySelector('[data-order-price-note]');
  // Money in whole cents as BigInt, so totals match the server exactly at any quantity.
  const toCents = text => { const [whole, part = ''] = String(text).split('.'); return BigInt(whole) * 100n + BigInt((part + '00').slice(0, 2)); };
  const formatRand = cents => `R ${(cents / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ')}.${(cents % 100n).toString().padStart(2, '0')}`;
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
        const supplied = supplierProducts[option.value]?.products || {};
        const eligible = productIds.every(id => Object.hasOwn(supplied, String(id)));
        option.disabled = !eligible;
        if (eligible) matching++;
      });
      if (supplier.selectedOptions[0]?.disabled) supplier.value = '';
      supplierHelp.textContent = matching === 0
        ? 'No supplier supplies all selected products. Remove a product or ask Super Admin to update supplier links.'
        : supplier.value ? 'This supplier supplies all selected products.' : 'Choose a supplier. Suppliers that do not supply every selected product are unavailable.';
    }
    create.disabled = !inputs.length || !valid || !supplier?.value || matching === 0;
    refreshPrices();
    [...body.querySelectorAll('[data-order-preview-row]')].forEach((row, index) => {
      row.querySelector('[data-order-product-input]').name = `items-${index}-product`;
      row.querySelector('[data-order-quantity]').name = `items-${index}-quantity`;
    });
    total.value = inputs.length;
  }

  // Unit price (the supplier's, or the catalogue reference price, tagged), line totals, VAT and total.
  function refreshPrices() {
    const entry = supplier?.value ? supplierProducts[supplier.value] : null;
    const incompleteNote = dialog.querySelector('[data-supplier-incomplete-note]');
    if (incompleteNote) incompleteNote.hidden = !entry?.incomplete;
    const rows = [...body.querySelectorAll('[data-order-preview-row]')];
    let subtotal = 0n;
    let complete = Boolean(entry) && rows.length > 0;
    rows.forEach(row => {
      const unitCell = row.querySelector('[data-order-unit-price]');
      const lineCell = row.querySelector('[data-order-line-total]');
      const quantity = row.querySelector('[data-order-quantity]');
      const info = entry?.products[row.dataset.productId];
      if (!unitCell || !lineCell) return;
      if (!info) {
        unitCell.textContent = '-';
        lineCell.textContent = '-';
        complete = false;
        return;
      }
      const cents = toCents(info.price);
      unitCell.textContent = formatRand(cents);
      if (info.reference) {
        const tag = document.createElement('span');
        tag.className = 'purchasing-price-tag';
        tag.textContent = 'Reference price';
        unitCell.append(tag);
      }
      if (quantity.validity.valid && Number.isSafeInteger(quantity.valueAsNumber)) {
        const line = cents * BigInt(quantity.valueAsNumber);
        lineCell.textContent = formatRand(line);
        subtotal += line;
      } else {
        lineCell.textContent = '-';
        complete = false;
      }
    });
    if (totals) totals.hidden = !complete;
    if (priceNote) priceNote.hidden = !entry;
    if (!complete) return;
    const rate = toCents(entry.vat_rate);  // 15.00% is 1500 hundredths of a percent
    const vat = (subtotal * rate + 5000n) / 10000n;  // rounded half-up to cents, like the server
    totals.querySelector('[data-order-subtotal]').textContent = formatRand(subtotal);
    totals.querySelector('[data-order-vat-label]').textContent = `VAT ${Number(entry.vat_rate)}%`;
    totals.querySelector('[data-order-vat]').textContent = formatRand(vat);
    totals.querySelector('[data-order-total]').textContent = formatRand(subtotal + vat);
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
      row.querySelector('[data-order-suggested]').textContent = box.dataset.suggestedQuantity === '' ? '-' : box.dataset.suggestedQuantity;
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
