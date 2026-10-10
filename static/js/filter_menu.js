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
    menu.addEventListener('toggle', () => { if (menu.open) place(menu); });
    if (menu.open) place(menu);
    menu.querySelector('[data-filter-cancel]').addEventListener('click', () => {
      close(menu);
      menu.querySelector('summary').focus();
    });
    menu.querySelectorAll('[data-filter-search]').forEach((search) => {
      search.addEventListener('input', () => filter(search));
      // Enter in the search box doesn't apply the filters.
      search.addEventListener('keydown', (event) => { if (event.key === 'Enter') event.preventDefault(); });
    });
  });

  document.addEventListener('click', (event) => {
    menus.forEach((menu) => { if (menu.open && !menu.contains(event.target)) close(menu); });
  });
  window.addEventListener('resize', () => menus.forEach((menu) => { if (menu.open) place(menu); }));
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    menus.forEach((menu) => {
      if (!menu.open) return;
      close(menu);
      menu.querySelector('summary').focus();
    });
  });
})();
