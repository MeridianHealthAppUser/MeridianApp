(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('prescription-changes', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
  /* Prescription changes (patient_prescription_changes.html), opened from a decision on the week-by-week timeline:
     that change is outlined and the dialog opens scrolled to it. From the button at the top of the timeline, none is.
     Runs in the capture phase, before dialogs.js opens the dialog and scrolls to its [data-dialog-scroll]. */
  listen(document, 'click', (event) => {
    const trigger = event.target.closest('[data-modal-open="prescription-changes"]');
    const dialog = trigger && document.getElementById('prescription-changes');
    if (!dialog) return;
    const key = trigger.dataset.change || '';
    dialog.querySelectorAll('.patient-changes__item').forEach((item) => {
      const selected = key !== '' && item.id === `change-${key}`;
      item.classList.toggle('is-selected', selected);
      item.toggleAttribute('data-dialog-scroll', selected);
      if (selected) item.setAttribute('aria-current', 'true'); else item.removeAttribute('aria-current');
    });
  }, true);
});
