(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('shipping_notes', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  // Populate the shared dialog before dialogs.js opens it. Delegation also covers filtered table replacements.
  listen(document, 'click', event => {
    const trigger = event.target.closest('[data-shipment-note]');
    if (!trigger || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    const dialog = document.getElementById('shipment-note-dialog');
    if (!dialog) return;
    const editable = trigger.dataset.noteEditable === 'true';
    const note = trigger.dataset.note || '';
    const form = dialog.querySelector('form');
    form.action = trigger.dataset.noteAction;
    form.elements.workflow_context.value = trigger.dataset.noteToken || '';
    form.elements.list_query.value = trigger.dataset.noteQuery || '';
    form.elements.note.value = note;
    form.elements.note.readOnly = !editable;
    dialog.querySelector('#shipment-note-title').textContent = !editable ? 'View note' : (note ? 'Edit note' : 'Add note');
    dialog.querySelector('#shipment-note-patient').textContent = trigger.dataset.notePatient;
    dialog.querySelector('[data-note-save]').hidden = !editable;
    dialog.querySelector('[data-note-edit-hint]').hidden = !editable;
  }, true);
});
