/* Modal dialogs. A [data-modal-open="id"] button or link opens <dialog class="app-dialog" id="id"> as a modal;
   without scripts the link goes to a page with the same form instead. A dialog the server opened (after a form
   error) becomes a modal too. Cancel ([data-dialog-close]) or a click on the backdrop closes it. A dialog opens
   scrolled to its [data-dialog-scroll] element, if it has one. */
(() => {
  function open(dialog) {
    if (dialog.open) dialog.close();
    dialog.showModal();
    // After an error, start at the first field.
    const field = dialog.querySelector('.errorlist') ? dialog.querySelector('select, textarea, input:not([type="hidden"])') : null;
    if (field) field.focus();
    const mark = dialog.querySelector('[data-dialog-scroll]');
    if (mark) mark.scrollIntoView({ block: 'center' });
  }

  document.querySelectorAll('dialog.app-dialog').forEach((dialog) => {
    if (typeof dialog.showModal !== 'function') return;
    dialog.querySelectorAll('[data-dialog-close]').forEach((button) => button.addEventListener('click', (event) => {
      event.preventDefault();
      dialog.close();
    }));
    dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
    if (dialog.hasAttribute('open')) open(dialog);
  });

  // Conversation headers can refresh while their existing dialogs stay in place.
  document.addEventListener('click', (event) => {
    const trigger = event.target.closest('[data-modal-open]');
    if (!trigger) return;
    const dialog = document.getElementById(trigger.dataset.modalOpen);
    if (!dialog || typeof dialog.showModal !== 'function') return;
    event.preventDefault();
    open(dialog);
  });

  // The no-script link to a dialog (?update=status) leaves its query in the address; tidy it.
  const url = new URL(window.location.href);
  if (url.searchParams.has('update')) {
    url.searchParams.delete('update');
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);
  }
})();
