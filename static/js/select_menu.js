(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('select_menu', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
/* Dropdown lists (static/css/select_menu.css), Nov 2026. Every single-choice <select> opens our own list instead of the
   browser's grey one, in the console's look, with a search box once it has 8 or more choices (patients, medicines,
   times). The closed box is still the select itself, so its look, its label, required, form resets and every script
   that reads or sets its value carry on as before: the list only sets the select's value and fires input and change,
   as choosing in the browser's list does. A click, a tap or the keyboard (arrows, Enter, Space, or typing to search)
   opens it. On a phone (52rem and narrower) it rises as a sheet from the bottom, and its search waits for a tap so the
   keyboard doesn't cover the list. data-select-search="always" or "off" on a select overrides the 8-choice rule; a
   select, or a box around it, marked data-native-select keeps the browser's list. A browser without popovers keeps
   the browser's list everywhere. */
(() => {
  if (!('popover' in HTMLElement.prototype)) return;
  const SEARCH_FROM = 8;
  const EDGE = 8;
  const GAP = 4;
  const phone = window.matchMedia('(max-width: 52rem)');
  const ICON = {
    search: '<svg class="select-menu__search-icon" viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" focusable="false"><circle cx="8.5" cy="8.5" r="5.25" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="m12.5 12.5 4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    check: '<svg class="select-menu__check" viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" focusable="false"><path d="m4.5 10.5 3.5 3.5 7.5-8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    close: '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" focusable="false"><path d="m5.5 5.5 9 9m0-9-9 9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  };
  const fold = (text) => text.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  const make = (tag, className) => Object.assign(document.createElement(tag), className ? { className } : {});
  let menu = null;
  let tap = null;
  let count = 0;
  let escaping = false;
  let swallow = false;

  const usable = (select) => select instanceof HTMLSelectElement && !select.multiple && select.size <= 1
    && !select.disabled && !select.closest('[data-native-select]');
  // Not a hidden choice, nor the "Choose a …" line of a required select, nor a disabled blank ("Not recorded").
  const shown = (select, option) => !option.hidden && !(option.value === '' && (select.required || option.disabled));

  function searchable(select, total) {
    const rule = select.dataset.selectSearch;
    if (rule === 'off') return false;
    return rule === 'always' || total >= SEARCH_FROM;
  }

  function labelText(select) {
    const named = select.getAttribute('aria-label');
    if (named) return named.trim();
    const label = select.labels?.[0];
    if (!label) return '';
    const copy = label.cloneNode(true);
    copy.querySelectorAll('select, .errorlist').forEach((node) => node.remove());
    return copy.textContent.replace(/\s+/g, ' ').trim();
  }

  function build(select, sheet) {
    const id = `select-menu-${++count}`;
    const name = labelText(select);
    const panel = make('div', `select-menu${sheet ? ' select-menu--sheet' : ''}`);
    panel.id = id;
    panel.popover = 'manual';

    if (sheet) {
      const head = make('div', 'select-menu__head');
      const title = make('p', 'select-menu__title');
      title.textContent = name || 'Choose';
      const shut = make('button', 'select-menu__close');
      shut.type = 'button';
      shut.setAttribute('aria-label', 'Close');
      shut.innerHTML = ICON.close;
      head.append(title, shut);
      panel.append(head);
    }

    const list = make('div', 'select-menu__list');
    list.id = `${id}-list`;
    list.tabIndex = -1;
    list.setAttribute('role', 'listbox');
    if (name) list.setAttribute('aria-label', name);
    const rows = [];
    const add = (option, parent) => {
      const row = make('div', 'select-menu__option');
      row.id = `${id}-${rows.length}`;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(option.selected));
      const disabled = option.matches(':disabled');
      if (disabled) row.setAttribute('aria-disabled', 'true');
      const text = make('span', 'select-menu__text');
      text.textContent = option.label;
      row.append(text);
      row.insertAdjacentHTML('beforeend', ICON.check);
      parent.append(row);
      rows.push({ row, option, disabled, text: fold(option.label) });
    };
    for (const child of select.children) {
      if (child instanceof HTMLOptGroupElement) {
        const options = [...child.children].filter((option) => option instanceof HTMLOptionElement && shown(select, option));
        if (!options.length) continue;
        const group = make('div', 'select-menu__group');
        group.setAttribute('role', 'group');
        const heading = make('div', 'select-menu__group-label');
        heading.id = `${id}-group-${list.children.length}`;
        heading.textContent = child.label;
        group.setAttribute('aria-labelledby', heading.id);
        group.append(heading);
        options.forEach((option) => add(option, group));
        list.append(group);
      } else if (child instanceof HTMLOptionElement && shown(select, child)) add(child, list);
    }

    let search = null;
    if (searchable(select, rows.length)) {
      const bar = make('div', 'select-menu__search-row');
      bar.innerHTML = ICON.search;
      search = make('input', 'select-menu__search');
      Object.assign(search, { type: 'search', placeholder: 'Search', autocomplete: 'off', spellcheck: false });
      search.setAttribute('autocapitalize', 'off');
      search.setAttribute('autocorrect', 'off');
      search.setAttribute('role', 'combobox');
      search.setAttribute('aria-autocomplete', 'list');
      search.setAttribute('aria-expanded', 'true');
      search.setAttribute('aria-controls', list.id);
      search.setAttribute('aria-label', name ? `Search ${name}` : 'Search');
      bar.append(search);
      panel.append(bar);
    }
    const empty = make('p', 'select-menu__empty');
    empty.setAttribute('aria-live', 'polite');
    empty.textContent = 'Nothing to choose';
    empty.hidden = Boolean(rows.length);
    panel.append(list, empty);
    return { panel, list, search, rows, empty };
  }

  function open(select, { via = 'mouse', seed = '' } = {}) {
    close();
    const sheet = phone.matches;
    const parts = build(select, sheet);
    menu = { select, sheet, touch: via === 'touch', active: null, above: undefined, typed: '', typedAt: 0, ...parts };
    const { panel, list, search, rows } = menu;
    // Next to the select, so it belongs to the same dialog, form or Filters menu; outside a label, so a click in the
    // list isn't a click on the label too.
    (select.closest('label') || select).after(panel);
    select.setAttribute('aria-expanded', 'true');
    select.setAttribute('aria-controls', list.id);
    panel.addEventListener('keydown', key);
    panel.addEventListener('input', (event) => { event.stopPropagation(); if (event.target === search) filter(); });
    panel.addEventListener('change', (event) => event.stopPropagation());
    // A press in the list leaves focus in the search box; the browser's scrollbar still drags.
    panel.addEventListener('mousedown', (event) => { if (event.target !== search) event.preventDefault(); });
    panel.addEventListener('click', click);
    panel.addEventListener('pointermove', (event) => {
      if (event.pointerType !== 'mouse') return;
      const entry = find(event.target);
      if (entry && entry !== menu?.active && !entry.disabled) activate(entry, { scroll: false });
    });
    // Tab or Shift+Tab out of the list closes it; focus moves on as usual.
    panel.addEventListener('focusout', (event) => {
      if (menu?.panel === panel && event.relatedTarget && !panel.contains(event.relatedTarget)) close();
    });
    panel.showPopover();
    place();
    // The current choice, or for the keyboard the first; a tap shows no highlight until there's a choice to show.
    const current = rows.find((entry) => entry.option.selected && !entry.disabled);
    activate(current || (menu.touch ? null : pool()[0]), { scroll: 'center' });
    if (search && seed) {
      search.value = seed;
      filter();
    }
    if (search && !menu.touch) search.focus({ preventScroll: true });
    else list.focus({ preventScroll: true });
  }

  function close({ focus = false } = {}) {
    if (!menu) return;
    const { select, panel } = menu;
    menu = null;
    select.removeAttribute('aria-expanded');
    select.removeAttribute('aria-controls');
    if (focus && select.isConnected) select.focus({ preventScroll: true });
    if (panel.matches(':popover-open')) panel.hidePopover();
    // Gone once this click is over, so code further up still finds the click inside its menu (filter_menu.js).
    window.setTimeout(() => panel.remove());
  }

  function place() {
    if (!menu) return;
    const { select, panel, sheet } = menu;
    if (sheet) {
      // Above a phone's keyboard while it's up.
      const view = window.visualViewport;
      const covered = view ? Math.max(0, window.innerHeight - view.height - view.offsetTop) : 0;
      panel.style.bottom = `calc(${Math.round(covered)}px + .6rem + env(safe-area-inset-bottom))`;
      panel.style.maxHeight = `${Math.round((view ? view.height : window.innerHeight) - 72)}px`;
      return;
    }
    const box = select.getBoundingClientRect();
    if ((!box.width && !box.height) || box.bottom < 0 || box.top > window.innerHeight) { close(); return; }
    const minimum = select.dataset.selectWidth === 'trigger' ? box.width : 192;
    const width = Math.min(Math.max(box.width, minimum), window.innerWidth - EDGE * 2);
    const left = Math.min(Math.max(box.left, EDGE), window.innerWidth - width - EDGE);
    const below = window.innerHeight - box.bottom - GAP - EDGE;
    const above = box.top - GAP - EDGE;
    Object.assign(panel.style, { left: `${left}px`, width: `${width}px` });
    // Below the box unless the list only fits above it; it stays on that side while the search narrows it.
    if (menu.above === undefined) {
      Object.assign(panel.style, { top: `${box.bottom + GAP}px`, bottom: 'auto', maxHeight: '' });
      menu.above = panel.offsetHeight > below && above > below;
    }
    const tallest = 22 * parseFloat(getComputedStyle(document.documentElement).fontSize);
    const room = Math.min(Math.max(menu.above ? above : below, 140), tallest);
    Object.assign(panel.style, {
      top: menu.above ? 'auto' : `${box.bottom + GAP}px`,
      bottom: menu.above ? `${window.innerHeight - box.top + GAP}px` : 'auto',
      maxHeight: `${Math.floor(room)}px`,
    });
  }

  const find = (target) => {
    const row = target.closest?.('.select-menu__option');
    return row ? menu?.rows.find((entry) => entry.row === row) : null;
  };
  const pool = () => (menu ? menu.rows.filter((entry) => !entry.row.hidden && !entry.disabled) : []);

  function activate(entry, { scroll = 'nearest' } = {}) {
    if (!menu) return;
    menu.active?.row.removeAttribute('data-active');
    menu.active = entry || null;
    const owner = menu.search || menu.list;
    if (!entry) { owner.removeAttribute('aria-activedescendant'); return; }
    entry.row.setAttribute('data-active', '');
    owner.setAttribute('aria-activedescendant', entry.row.id);
    if (!scroll) return;
    const { list } = menu;
    const row = entry.row.getBoundingClientRect();
    const view = list.getBoundingClientRect();
    if (scroll === 'center') list.scrollTop += row.top - view.top - (view.height - row.height) / 2;
    else if (row.top < view.top + 4) list.scrollTop -= view.top + 4 - row.top;
    else if (row.bottom > view.bottom - 4) list.scrollTop += row.bottom - view.bottom + 4;
  }

  function move(step) {
    const choices = pool();
    if (!choices.length) return;
    const at = choices.indexOf(menu.active);
    const last = choices.length - 1;
    let next = Math.min(last, Math.max(0, at + step));
    if (!Number.isFinite(step)) next = step > 0 ? last : 0;
    else if (at < 0) next = step > 0 ? 0 : last;
    activate(choices[next]);
  }

  // Every word typed has to start a word in the choice, accents aside: "m" finds Mia and Megan Fourie but not
  // "Demo", and "van der" finds Pieter van der Merwe. If nothing starts that way, anywhere in a word will do.
  function filter() {
    const { search, rows, list, empty } = menu;
    const words = fold(search.value).split(/\s+/).filter(Boolean);
    const starts = words.map((word) => new RegExp(`(^|[^a-z0-9])${word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`));
    let matches = rows.filter((entry) => starts.every((start) => start.test(entry.text)));
    if (!matches.length) matches = rows.filter((entry) => words.every((word) => entry.text.includes(word)));
    rows.forEach((entry) => { entry.row.hidden = !matches.includes(entry); });
    list.querySelectorAll('.select-menu__group').forEach((group) => {
      group.hidden = !group.querySelector('.select-menu__option:not([hidden])');
    });
    const any = rows.some((entry) => !entry.row.hidden);
    list.hidden = !any;
    empty.hidden = any;
    empty.textContent = rows.length ? `No matches for “${search.value.trim()}”` : 'Nothing to choose';
    if (!words.length) activate(rows.find((entry) => entry.option.selected && !entry.disabled) || pool()[0], { scroll: 'center' });
    else activate(pool()[0], { scroll: 'center' });
    place();
  }

  function choose(entry) {
    if (!menu || !entry || entry.disabled) return;
    const { select, touch } = menu;
    if (!select.contains(entry.option)) { close(); return; }
    const changed = !entry.option.selected;
    entry.option.selected = true;
    // A phone would open its own list again if focus went back to the select.
    close({ focus: !touch });
    if (!changed) return;
    select.dispatchEvent(new Event('input', { bubbles: true }));
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function click(event) {
    const { panel } = menu || {};
    if (!panel) return;
    if (event.target.closest('.select-menu__close')) { close({ focus: !menu.touch }); return; }
    // On a phone, a tap on the dimmed page around the sheet lands on the sheet's backdrop.
    if (event.target === panel) {
      const box = panel.getBoundingClientRect();
      const inside = event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) { event.preventDefault(); close(); }
      return;
    }
    const entry = find(event.target);
    if (entry) choose(entry);
  }

  function key(event) {
    if (!menu) return;
    const inSearch = event.target === menu.search;
    switch (event.key) {
      case 'Escape':
        event.preventDefault();
        event.stopPropagation();
        // The same keypress mustn't go on to close the dialog the list is in (see cancel, below).
        escaping = true;
        window.setTimeout(() => { escaping = false; });
        close({ focus: true });
        return;
      case 'ArrowDown':
        event.preventDefault();
        move(1);
        return;
      case 'ArrowUp':
        event.preventDefault();
        if (event.altKey) close({ focus: true });
        else move(-1);
        return;
      case 'PageDown':
      case 'PageUp':
        event.preventDefault();
        move(event.key === 'PageDown' ? 8 : -8);
        return;
      case 'Home':
      case 'End':
        if (inSearch) return;
        event.preventDefault();
        move(event.key === 'End' ? Infinity : -Infinity);
        return;
      case 'Enter':
        // Never the form's Enter: it chooses, as in the browser's list.
        event.preventDefault();
        event.stopPropagation();
        choose(menu.active);
        return;
      default:
    }
    if (inSearch || event.ctrlKey || event.metaKey || event.altKey || event.key.length !== 1) return;
    // In the list itself: Space chooses; letters jump (or go to the search box, when there is one).
    if (menu.search) {
      event.preventDefault();
      menu.search.value += event.key;
      menu.search.focus({ preventScroll: true });
      filter();
      return;
    }
    const now = event.timeStamp;
    if (event.key === ' ' && (!menu.typed || now - menu.typedAt > 700)) {
      event.preventDefault();
      choose(menu.active);
      return;
    }
    event.preventDefault();
    menu.typed = now - menu.typedAt > 700 ? event.key : menu.typed + event.key;
    menu.typedAt = now;
    const typed = fold(menu.typed);
    const choices = pool();
    const from = choices.indexOf(menu.active);
    // A letter (or the same letter again) moves on to the next choice starting with it.
    const repeat = [...typed].every((letter) => letter === typed[0]) ? typed[0] : null;
    const ordered = repeat ? [...choices.slice(from + 1), ...choices.slice(0, from + 1)] : choices;
    const hit = ordered.find((entry) => entry.text.startsWith(repeat || typed));
    if (hit) activate(hit);
  }

  // Opening, instead of the browser's own list. A mouse press opens it (and a second press closes it)...
  listen(document, 'mousedown', (event) => {
    const select = event.target.closest?.('select');
    if (event.button !== 0 || !usable(select)) return;
    event.preventDefault();
    if (menu?.select === select) { close({ focus: true }); return; }
    select.focus({ preventScroll: true });
    open(select);
  }, true);

  // ...a tap does (not a swipe that starts on the box, which still scrolls the page)...
  listen(document, 'touchstart', (event) => {
    const select = event.target.closest?.('select');
    const touch = event.touches[0];
    tap = usable(select) && event.touches.length === 1 ? { select, x: touch.clientX, y: touch.clientY } : null;
  }, { passive: true, capture: true });
  listen(document, 'touchmove', (event) => {
    const touch = event.touches[0];
    if (tap && touch && Math.hypot(touch.clientX - tap.x, touch.clientY - tap.y) > 10) tap = null;
  }, { passive: true, capture: true });
  listen(document, 'touchcancel', () => { tap = null; }, { passive: true, capture: true });
  listen(document, 'touchend', (event) => {
    const pressed = tap;
    tap = null;
    if (!pressed || !event.cancelable || !pressed.select.contains(event.target)) return;
    event.preventDefault();
    if (menu?.select === pressed.select) { close(); return; }
    open(pressed.select, { via: 'touch' });
  }, { passive: false, capture: true });

  // ...and so do the keys that open a select, or typing into one that has a search.
  listen(document, 'keydown', (event) => {
    const select = event.target;
    if (event.defaultPrevented || !usable(select) || menu?.select === select || event.ctrlKey || event.metaKey) return;
    if (['ArrowDown', 'ArrowUp', 'Enter', ' ', 'F4'].includes(event.key)) {
      event.preventDefault();
      open(select, { via: 'keyboard' });
    } else if (event.key.length === 1 && !event.altKey) {
      const total = [...select.options].filter((option) => shown(select, option)).length;
      if (!searchable(select, total)) return;
      event.preventDefault();
      open(select, { via: 'keyboard', seed: event.key });
    }
  });

  // A press anywhere else closes it, and that's all it does, as with the browser's own list: its click doesn't go on
  // to a button, a link or the dialog's backdrop (a tap on the dimmed page round a phone's sheet would otherwise close
  // the dialog under it too).
  listen(document, 'pointerdown', (event) => {
    swallow = false;
    if (!menu || menu.select.contains(event.target) || menu.panel.contains(event.target)) return;
    close();
    swallow = true;
  }, true);
  // On the window, ahead of fluid.js, which closes a dialog on a click on its backdrop.
  listen(window, 'click', (event) => {
    if (!swallow) return;
    swallow = false;
    event.preventDefault();
    event.stopPropagation();
  }, true);
  listen(document, 'keydown', () => { swallow = false; }, true);
  // Escape in the list closes the list, not the dialog it's in (on the window, ahead of fluid.js's closing animation).
  listen(window, 'cancel', (event) => {
    if (!escaping && !(menu && event.target.contains?.(menu.panel))) return;
    event.preventDefault();
    event.stopPropagation();
    close({ focus: true });
  }, true);
  listen(window, 'scroll', (event) => { if (menu && !menu.panel.contains(event.target)) place(); }, { capture: true, passive: true });
  listen(window, 'resize', () => { if (menu && menu.sheet !== phone.matches) close(); else place(); });
  listen(window.visualViewport, 'resize', place);
  listen(window.visualViewport, 'scroll', place);
  lifecycle?.cleanup(() => close());
})();

});
