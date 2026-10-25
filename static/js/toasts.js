(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('toasts', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
/* Toasts: the close button dismisses one, and a confirmation that has faded out is removed.
   window.meridianToast(text, 'error') adds one from a script, above any open dialog. */
(() => {
  // The shell retains old errors and merges new server messages between visits.
  // Delegation keeps both usable while content loads without wiring each toast.
  listen(document, 'animationend', event => {
    if (event.animationName === 'toast-out' && event.target.matches?.('[data-toast]')) event.target.remove();
  });
  listen(document, 'click', event => {
    const close = event.target.closest?.('[data-toast-close]');
    const toast = close?.closest('[data-toast]');
    if (toast) { event.preventDefault(); toast.remove(); }
  });

  window.meridianToast = (text, kind = 'success') => {
    if (lifecycle && !lifecycle.isActive) return null;
    let stack = document.querySelector('.toast-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.className = 'toast-stack';
      document.body.append(stack);
    }
    const toast = document.createElement('div');
    toast.className = `flash-message toast ${kind}`;
    toast.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    toast.dataset.toast = '';
    const message = document.createElement('span');
    message.textContent = text;
    const close = document.createElement('button');
    close.className = 'toast__close';
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss');
    close.dataset.toastClose = '';
    close.textContent = '×';
    toast.append(message, close);
    stack.append(toast);
    // A modal dialog sits above the page, so the stack joins the top layer to show above it.
    let modalOpen = false;
    try { modalOpen = Boolean(document.querySelector('dialog:modal')); } catch (error) { /* An older browser. */ }
    if (modalOpen && typeof stack.showPopover === 'function') {
      stack.popover = 'manual';
      if (stack.matches(':popover-open')) stack.hidePopover();
      stack.showPopover();
    }
    return toast;
  };
})();

}, { persistent: true });
