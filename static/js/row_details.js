(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('row_details', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
/* A table row's details, in the row below it (James, 26 Nov 2026: a non-prescription request's items on Patient
   orders and the record's Treatment tab). A [data-row-toggle] button shows or hides the row its aria-controls names.
   Listened for on the document, so rows the live search draws again work too. */
  listen(document, 'click', (event) => {
    const button = event.target.closest?.('[data-row-toggle]');
    if (!button) return;
    const row = document.getElementById(button.getAttribute('aria-controls'));
    if (!row) return;
    const open = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', String(open));
    row.hidden = !open;
    const label = button.querySelector('.sr-only');
    if (label) label.textContent = open ? ': hide its items' : ': show its items';
  });
});
