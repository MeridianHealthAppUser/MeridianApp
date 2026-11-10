(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('subscription_payments', lifecycle => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  document.querySelectorAll('dialog.subscription-payments').forEach(dialog => {
    listen(dialog, 'close', () => {
      const url = new URL(window.location.href);
      if (url.searchParams.get('payments') !== dialog.id.replace('subscription-payments-', '')) return;
      url.searchParams.delete('payments');
      url.hash = '';
      if (window.MeridianNavigation?.replaceUrl) window.MeridianNavigation.replaceUrl(url.pathname + url.search);
      else window.history.replaceState(window.history.state, '', url.pathname + url.search);
    });
  });
  document.querySelectorAll('[data-subscription-discount-form]').forEach(form => {
    const refresh = () => { form.querySelector('[data-discount-payment-field]').hidden = form.elements.scope.value !== 'one'; };
    listen(form.elements.scope, 'change', refresh);
    refresh();
  });
  listen(document, 'click', event => {
    const link = event.target.closest('[data-discount-payment]');
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const form = link.closest('dialog').querySelector('[data-subscription-discount-form]');
    if (!form) return;
    event.preventDefault();
    form.elements.scope.value = 'one';
    form.elements.payment.value = link.dataset.discountPayment;
    form.elements.effective_on.value = link.dataset.discountDate;
    form.querySelector('[data-discount-payment-field]').hidden = false;
    form.elements.value.focus();
  });
});
