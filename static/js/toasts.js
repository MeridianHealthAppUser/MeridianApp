/* Toasts: the close button dismisses one, and a confirmation that has faded out is removed.
   window.meridianToast(text, 'error') adds one from a script, above any open dialog. */
(() => {
  function wire(toast) {
    toast.addEventListener('animationend', (event) => {
      if (event.animationName === 'toast-out') toast.remove();
    });
    const close = toast.querySelector('[data-toast-close]');
    if (close) close.addEventListener('click', () => toast.remove());
  }
  document.querySelectorAll('[data-toast]').forEach(wire);

  window.meridianToast = (text, kind = 'success') => {
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
    wire(toast);
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
