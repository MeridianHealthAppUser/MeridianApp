(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('inventory_receipts', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  document.querySelectorAll('[data-inventory-receipt]').forEach(form => {
    if (form.meridianReceipt) return;
    form.meridianReceipt = true;
    const container = form.querySelector('[data-receipt-rows]');
    const template = form.querySelector('[data-receipt-empty-row]');
    const total = form.querySelector('[name="items-TOTAL_FORMS"]');
    const add = form.querySelector('[data-add-receipt-row]');
    const count = form.querySelector('[data-receipt-count]');
    const maximum = Number(form.querySelector('[name="items-MAX_NUM_FORMS"]').value || 100);
    const rows = () => [...container.querySelectorAll('[data-receipt-row]')];
    // Deleted server-rendered rows can be discarded before indexing the visible forms.
    rows().filter(row => row.querySelector('[name$="-DELETE"]')?.checked).forEach(row => row.remove());
    function refresh() {
      rows().forEach((row, index) => {
        row.querySelector('[data-receipt-legend]').textContent = `Medication ${index + 1}`;
        row.querySelectorAll('*').forEach(element => {
          ['name', 'id', 'for', 'aria-describedby'].forEach(attribute => {
            const value = element.getAttribute(attribute);
            if (value) element.setAttribute(attribute, value.replace(/items-(?:\d+|__prefix__)-/g, `items-${index}-`));
          });
        });
        row.querySelector('[data-remove-receipt-row]').disabled = rows().length === 1;
      });
      total.value = rows().length;
      add.disabled = rows().length >= maximum;
      count.textContent = `${rows().length} medication row${rows().length === 1 ? '' : 's'}`;
    }
    listen(add, 'click', () => {
      if (rows().length >= maximum) return;
      const fragment = template.content.cloneNode(true);
      container.append(fragment);
      refresh();
      rows().at(-1).querySelector('select').focus();
    });
    listen(container, 'click', event => {
      const remove = event.target.closest('[data-remove-receipt-row]');
      if (!remove || rows().length <= 1) return;
      const row = remove.closest('[data-receipt-row]');
      const next = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      refresh();
      next?.querySelector('select')?.focus();
    });
    refresh();
    lifecycle?.cleanup(() => { delete form.meridianReceipt; });
  });
});
