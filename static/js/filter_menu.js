(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('filter_menu', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
/* Filters menus (templates/portal/includes/filter_menu.html). The menu closes on Escape, a click elsewhere or Cancel;
   Cancel also puts back the ticks it opened with. A section's search hides the choices that don't match. Without
   scripts the menu still opens and closes, and Apply works. */
(() => {
  const menus = [...document.querySelectorAll('[data-filter-menu]')];
  if (!menus.length) return;
  const EDGE = 16;

  function close(menu) {
    const form = menu.querySelector('form');
    form.reset();
    form.querySelectorAll('[data-filter-search]').forEach((search) => filter(search));
    menu.open = false;
  }

  function filter(search) {
    const words = search.value.trim().toLowerCase();
    search.closest('.filter-menu__options').querySelectorAll('li').forEach((item) => {
      item.hidden = Boolean(words) && !item.textContent.toLowerCase().includes(words);
    });
  }

  // The panel lines up with the right of Filters, and moves along when that would take it off the left of the screen.
  function place(menu) {
    const panel = menu.querySelector('.filter-menu__panel');
    panel.style.right = '0px';
    const left = panel.getBoundingClientRect().left;
    if (left < EDGE) panel.style.right = `${left - EDGE}px`;
  }

  menus.forEach((menu) => {
    listen(menu, 'toggle', () => { if (menu.open) place(menu); });
    if (menu.open) place(menu);
    listen(menu.querySelector('[data-filter-cancel]'), 'click', () => {
      close(menu);
      menu.querySelector('summary').focus();
    });
    menu.querySelectorAll('[data-filter-search]').forEach((search) => {
      listen(search, 'input', () => filter(search));
      // Enter in the search box doesn't apply the filters.
      listen(search, 'keydown', (event) => { if (event.key === 'Enter') event.preventDefault(); });
    });
  });

  listen(document, 'click', (event) => {
    menus.forEach((menu) => { if (menu.open && !menu.contains(event.target)) close(menu); });
  });
  listen(window, 'resize', () => menus.forEach((menu) => { if (menu.open) place(menu); }));
  listen(document, 'keydown', (event) => {
    if (event.key !== 'Escape') return;
    menus.forEach((menu) => {
      if (!menu.open) return;
      close(menu);
      menu.querySelector('summary').focus();
    });
  });
})();

});
