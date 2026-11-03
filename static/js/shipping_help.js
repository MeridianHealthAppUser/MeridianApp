(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('shipping_help', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  const initialized = new WeakSet();
  function initializeHelp() {
    document.querySelectorAll('[data-shipping-help]').forEach(help => {
      if (initialized.has(help)) return;
      const button = help.querySelector('button');
      const popup = help.querySelector('[popover]');
      if (typeof popup?.showPopover !== 'function') return;
      initialized.add(help);
      help.classList.add('has-native-popover');
      let timer;
      const isOpen = () => popup.matches(':popover-open');
      function close() { if (isOpen()) popup.hidePopover(); }
      function show() {
        clearTimeout(timer);
        if (!isOpen()) popup.showPopover();
        const rect = button.getBoundingClientRect();
        const box = popup.getBoundingClientRect();
        const left = Math.max(16, Math.min(rect.right - box.width, innerWidth - box.width - 16));
        const below = rect.bottom + 8;
        const top = below + box.height <= innerHeight - 16 ? below : Math.max(16, rect.top - box.height - 8);
        Object.assign(popup.style, {left:`${left}px`, top:`${top}px`});
      }
      function deferClose() {
        clearTimeout(timer);
        timer = setTimeout(() => { if (!help.matches(':hover,:focus-within')) close(); }, 160);
      }
      listen(help, 'pointerenter', show);
      listen(help, 'pointerleave', deferClose);
      listen(help, 'focusin', show);
      listen(help, 'focusout', deferClose);
      listen(button, 'click', show);
      listen(popup, 'toggle', () => button.setAttribute('aria-expanded', String(isOpen())));
      listen(window, 'resize', close);
      lifecycle?.cleanup(() => { clearTimeout(timer); close(); });
    });
  }
  initializeHelp();
  listen(document, 'messages:searched', initializeHelp);
});
