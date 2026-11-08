(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('basket_quantity', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
  // My Treatment's once-off items: an item already in the basket shows Update basket only once its quantity differs
  // from what's in the basket. Without scripts the button always shows.
  const sync = (form) => {
    const input = form.querySelector('input[name$="quantity"]');
    const button = form.querySelector('[data-basket-update]');
    if (!input || !button) return;
    button.hidden = input.value.trim() !== '' && Number(input.value) === Number(form.dataset.inBasket);
  };
  document.querySelectorAll('form[data-in-basket]').forEach(sync);
  listen(document, 'input', (event) => {
    const form = event.target.closest?.('form[data-in-basket]');
    if (form) sync(form);
  });
});
