/* Native details remains usable without JavaScript; enhance keyboard dismissal. */
(() => {
  document.querySelectorAll('[data-account-menu]').forEach(menu => {
    const trigger = menu.querySelector('summary');
    const items = () => [...menu.querySelectorAll('a,button')].filter(item => !item.disabled);
    const close = restoreFocus => { menu.open = false; trigger.setAttribute('aria-expanded', 'false'); if (restoreFocus) trigger.focus(); };
    menu.addEventListener('toggle', () => { trigger.setAttribute('aria-expanded', String(menu.open)); });
    document.addEventListener('pointerdown', event => { if (menu.open && !menu.contains(event.target)) close(false); });
    document.addEventListener('focusin', event => { if (menu.open && !menu.contains(event.target)) close(false); });
    menu.addEventListener('keydown', event => {
      if (event.key === 'Escape' && menu.open) { event.preventDefault(); close(true); return; }
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      menu.open = true;
      trigger.setAttribute('aria-expanded', 'true');
      const links = items(), current = links.indexOf(document.activeElement);
      if (!links.length) return;
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? links.length - 1 : current < 0 ? (event.key === 'ArrowUp' ? links.length - 1 : 0) : (current + (event.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length;
      links[index].focus();
    });
    window.addEventListener('pageshow', () => close(false));
  });
})();
