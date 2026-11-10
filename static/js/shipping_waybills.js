(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('shipping_waybills', lifecycle => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  listen(document, 'click', event => {
    const trigger = event.target.closest('[data-shipment-waybill]');
    if (!trigger || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    const dialog = document.getElementById('shipment-waybill-dialog');
    if (!dialog) return;
    const form = dialog.querySelector('form');
    form.action = trigger.dataset.waybillAction;
    form.elements.workflow_context.value = trigger.dataset.waybillToken;
    form.elements.next.value = trigger.dataset.waybillNext;
    form.elements.waybill.value = '';
    dialog.querySelector('#shipment-waybill-patient').textContent = trigger.dataset.waybillPatient;
    dialog.querySelector('[data-dialog-close]').href = trigger.dataset.waybillNext;
  }, true);
});
