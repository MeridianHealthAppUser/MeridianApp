(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('date_picker', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
/* Date fields' calendar (static/css/date_picker.css), Nov 2026. A date field (and a month field) opens our own
   calendar instead of the browser's, in the console's look, beside the field like the dropdown lists
   (static/js/select_menu.js). The field is still the browser's own: a date can still be typed into it, and its label,
   required, min and max, form resets and every script that reads or sets its value carry on as before; the calendar
   only sets the value and fires input and change, as the browser's calendar does. A click on the field, a tap, or
   Alt+Down or F4 opens it. The month's name opens the years and then the months, so a date of birth is a few presses
   away. Weeks start on Monday, as the schedule's do. On a phone (52rem and narrower) it rises as a sheet from the
   bottom. A field, or a box around it, marked data-native-date keeps the browser's calendar; so does a browser
   without popovers.
   A date range (Insights' period, Nov 2026) is a button marked data-date-range, naming its two hidden date fields in
   data-range-start and data-range-end (and its limits in data-min and data-max). Its calendar shows the range, and two
   presses pick a new one, first day then last, either way round, with the days between lit as the pointer or the
   keyboard moves. The fields take the new dates, the button's [data-range-text] shows them, and the button fires
   change. It fires date-range:close as the calendar closes, with detail.committed false if no range was picked. */
(() => {
  if (!('popover' in HTMLElement.prototype)) return;
  const EDGE = 8;
  const GAP = 4;
  const DAY = 864e5;
  const phone = window.matchMedia('(max-width: 52rem)');
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  const icon = (path, size = 18, className = '') => `<svg${className ? ` class="${className}"` : ''} viewBox="0 0 20 20" width="${size}" height="${size}" aria-hidden="true" focusable="false"><path d="${path}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  const ICON = {
    back: icon('m12 5-5 5 5 5'),
    ahead: icon('m8 5 5 5-5 5'),
    caret: icon('m6 8 4 4 4-4', 14, 'date-picker__caret'),
    close: icon('m5.5 5.5 9 9m0-9-9 9'),
  };
  const make = (tag, className) => Object.assign(document.createElement(tag), className ? { className } : {});
  const short = (t) => { const { y, m, d } = split(t); return `${d} ${MONTHS[m].slice(0, 3)} ${y}`; };
  function button(className, html = '') {
    const node = make('button', className);
    node.type = 'button';
    node.innerHTML = html;
    return node;
  }
  let picker = null;
  let tap = null;
  let pressed = null;
  let count = 0;
  let escaping = false;
  let swallow = false;

  // Days are whole UTC days, so no change of the clocks moves one.
  function day(y, m, d = 1) {
    const date = new Date(0);
    date.setUTCFullYear(y, m, d);
    return date.getTime();
  }
  function split(t) {
    const date = new Date(t);
    return { y: date.getUTCFullYear(), m: date.getUTCMonth(), d: date.getUTCDate(), weekday: (date.getUTCDay() + 6) % 7 };
  }
  const pad = (n, size = 2) => String(n).padStart(size, '0');
  const isoDay = (t) => { const { y, m, d } = split(t); return `${pad(y, 4)}-${pad(m + 1)}-${pad(d)}`; };
  const isoMonth = (t) => isoDay(t).slice(0, 7);
  const lastOf = (y, m) => split(day(y, m + 1, 0)).d;
  function addMonths(t, n) {
    const { y, m, d } = split(t);
    const to = split(day(y, m + n, 1));
    return day(to.y, to.m, Math.min(d, lastOf(to.y, to.m)));
  }
  // "2026-11-20" as that day (or "2026-11", for a month field, as its first); anything else is no day.
  function read(text, kind) {
    const match = /^(\d{4,6})-(\d{2})(?:-(\d{2}))?$/.exec(String(text || '').trim());
    if (!match || Boolean(match[3]) !== (kind === 'date')) return null;
    const [y, m, d] = [Number(match[1]), Number(match[2]) - 1, Number(match[3] || 1)];
    const t = day(y, m, d);
    const back = split(t);
    return back.y === y && back.m === m && back.d === d ? t : null;
  }
  function today() {
    const now = new Date();
    return day(now.getFullYear(), now.getMonth(), now.getDate());
  }

  function kindOf(field) {
    if (field instanceof HTMLButtonElement && field.hasAttribute('data-date-range')) return 'range';
    if (!(field instanceof HTMLInputElement)) return null;
    const type = (field.getAttribute('type') || '').toLowerCase();
    return type === 'date' || type === 'month' ? type : null;
  }
  const usable = (field) => Boolean(kindOf(field)) && !field.disabled && !field.readOnly && !field.closest('[data-native-date]');
  // A range's two fields, and the days they hold.
  const ends = (field) => [document.getElementById(field.dataset.rangeStart), document.getElementById(field.dataset.rangeEnd)];
  function rangeOf(field) {
    const [first, last] = ends(field).map((input) => read(input?.value, 'date'));
    return first !== null && last !== null && last < first ? { start: last, end: first } : { start: first, end: last };
  }
  // The field a tap is on: the field itself, or the words of its label.
  function fieldAt(target) {
    if (usable(target)) return target;
    if (!target?.closest || target.closest('a, button, input, select, textarea, [contenteditable]')) return null;
    const control = target.closest('label')?.control;
    return usable(control) ? control : null;
  }

  function labelText(field) {
    const named = field.getAttribute('aria-label');
    if (named) return named.trim();
    const label = field.labels?.[0];
    if (!label) return '';
    const copy = label.cloneNode(true);
    copy.querySelectorAll('input, select, textarea, .errorlist').forEach((node) => node.remove());
    return copy.textContent.replace(/\s+/g, ' ').trim();
  }

  function limits(field, kind) {
    const range = kind === 'range';
    const min = read(range ? field.dataset.min : field.min, range ? 'date' : kind);
    let max = read(range ? field.dataset.max : field.max, range ? 'date' : kind);
    if (kind === 'month' && max !== null) { const { y, m } = split(max); max = day(y, m + 1, 0); }
    return { min, max: min !== null && max !== null && max < min ? min : max };
  }
  const allowed = (t) => (picker.min === null || t >= picker.min) && (picker.max === null || t <= picker.max);
  const within = (t) => Math.min(Math.max(t, picker.min ?? -Infinity), picker.max ?? Infinity);
  // A month, or a year, can be chosen if a day in it can.
  const overlaps = (first, last) => (picker.min === null || last >= picker.min) && (picker.max === null || first <= picker.max);
  function years() {
    const now = split(picker.today).y;
    const at = split(picker.focus).y;
    return {
      first: Math.min(picker.min === null ? now - 110 : split(picker.min).y, at),
      last: Math.max(picker.max === null ? now + 20 : split(picker.max).y, at),
    };
  }
  // The one cell in the tab order, for each view: the day, its month, or its year.
  const keyOf = (t) => (picker.view === 'days' ? isoDay(t) : picker.view === 'months' ? isoMonth(t) : String(split(t).y));
  const current = () => picker?.body.querySelector('.date-picker__cell[tabindex="0"]');

  function build(field, kind, sheet) {
    const id = `date-picker-${++count}`;
    const name = field.dataset.dateLabel || labelText(field) || (kind === 'month' ? 'Choose a month' : kind === 'range' ? 'Choose dates' : 'Choose a date');
    const panel = make('div', `date-picker${sheet ? ' date-picker--sheet' : ''}`);
    panel.id = id;
    panel.popover = 'manual';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', name);
    if (sheet) {
      const head = make('div', 'date-picker__head');
      const title = make('p', 'date-picker__title');
      title.textContent = name;
      const shut = button('date-picker__close', ICON.close);
      shut.setAttribute('aria-label', 'Close');
      head.append(title, shut);
      panel.append(head);
    }
    const bar = make('div', 'date-picker__bar');
    const period = button('date-picker__period', `<span class="date-picker__period-text" id="${id}-period" aria-live="polite"></span>${ICON.caret}`);
    const steps = make('div', 'date-picker__steps');
    const back = button('date-picker__step', ICON.back);
    back.dataset.step = '-1';
    const ahead = button('date-picker__step', ICON.ahead);
    ahead.dataset.step = '1';
    steps.append(back, ahead);
    bar.append(period, steps);
    const body = make('div', 'date-picker__body');
    const foot = make('div', 'date-picker__foot');
    if (kind === 'range') {
      // What the next press does: the first day, then the last.
      const hint = make('p', 'date-picker__hint');
      hint.setAttribute('aria-live', 'polite');
      foot.append(hint);
      panel.append(bar, body, foot);
      return { panel, period, title: period.firstElementChild, back, ahead, body, now: null, hint };
    }
    if (!field.required) {
      const clear = button('date-picker__action');
      clear.textContent = 'Clear';
      clear.dataset.action = 'clear';
      foot.append(clear);
    }
    const now = button('date-picker__action');
    now.textContent = kind === 'month' ? 'This month' : 'Today';
    now.dataset.action = 'today';
    foot.append(now);
    panel.append(bar, body, foot);
    return { panel, period, title: period.firstElementChild, back, ahead, body, now };
  }

  function mark(cell, { key, t, focus, chosen, current: isNow, off }) {
    cell.dataset.key = key;
    cell.dataset.t = String(t);
    cell.tabIndex = focus ? 0 : -1;
    cell.setAttribute(cell.getAttribute('role') === 'gridcell' ? 'aria-selected' : 'aria-pressed', String(chosen));
    if (isNow) cell.setAttribute('aria-current', 'date');
    if (off) cell.setAttribute('aria-disabled', 'true');
  }

  function drawDays() {
    const { y, m } = split(picker.focus);
    const first = day(y, m, 1);
    const start = first - split(first).weekday * DAY;
    const grid = make('div', 'date-picker__grid');
    grid.setAttribute('role', 'grid');
    grid.setAttribute('aria-labelledby', picker.title.id);
    const head = make('div', 'date-picker__row');
    head.setAttribute('role', 'row');
    WEEKDAYS.forEach((name) => {
      const cell = make('span', 'date-picker__weekday');
      cell.setAttribute('role', 'columnheader');
      cell.setAttribute('aria-label', name);
      cell.textContent = name.slice(0, 2);
      head.append(cell);
    });
    grid.append(head);
    for (let week = 0; week < 6; week += 1) {
      const row = make('div', 'date-picker__row');
      row.setAttribute('role', 'row');
      for (let index = 0; index < 7; index += 1) {
        const t = start + (week * 7 + index) * DAY;
        const date = split(t);
        const cell = button('date-picker__cell date-picker__day');
        cell.setAttribute('role', 'gridcell');
        cell.textContent = String(date.d);
        cell.setAttribute('aria-label', `${WEEKDAYS[date.weekday]} ${date.d} ${MONTHS[date.m]} ${date.y}`);
        if (date.m !== m) cell.dataset.outside = '';
        mark(cell, { key: isoDay(t), t, focus: t === picker.focus, chosen: t === picker.value, current: t === picker.today, off: !allowed(t) });
        row.append(cell);
      }
      grid.append(row);
    }
    return grid;
  }

  function drawMonths() {
    const { y, m: at } = split(picker.focus);
    const chosen = picker.value === null ? null : split(picker.value);
    const now = split(picker.today);
    const grid = make('div', 'date-picker__months');
    grid.setAttribute('role', 'group');
    grid.setAttribute('aria-labelledby', picker.title.id);
    MONTHS.forEach((name, m) => {
      const cell = button('date-picker__cell date-picker__month');
      cell.textContent = name.slice(0, 3);
      cell.setAttribute('aria-label', `${name} ${y}`);
      mark(cell, {
        key: `${pad(y, 4)}-${pad(m + 1)}`, t: day(y, m, 1), focus: m === at, chosen: chosen?.y === y && chosen?.m === m,
        current: now.y === y && now.m === m, off: !overlaps(day(y, m, 1), day(y, m + 1, 0)),
      });
      grid.append(cell);
    });
    return grid;
  }

  function drawYears() {
    const { first, last } = years();
    const at = split(picker.focus).y;
    const chosen = picker.value === null ? null : split(picker.value).y;
    const now = split(picker.today).y;
    const list = make('div', 'date-picker__years');
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', 'Years');
    for (let y = first; y <= last; y += 1) {
      const cell = button('date-picker__cell date-picker__year');
      cell.textContent = String(y);
      mark(cell, { key: String(y), t: day(y, 0, 1), focus: y === at, chosen: y === chosen, current: y === now, off: !overlaps(day(y, 0, 1), day(y, 11, 31)) });
      list.append(cell);
    }
    return list;
  }

  function render() {
    const { view, kind, body } = picker;
    const inside = body.contains(document.activeElement);
    const { y, m } = split(picker.focus);
    body.replaceChildren(view === 'days' ? drawDays() : view === 'months' ? drawMonths() : drawYears());
    picker.panel.dataset.view = view;
    const text = view === 'months' || kind === 'month' ? String(y) : `${MONTHS[m]} ${y}`;
    picker.title.textContent = text;
    picker.period.setAttribute('aria-label', view === 'years' ? `Back to ${text}` : `${text}, choose another ${view === 'days' ? 'month or year' : 'year'}`);
    const unit = view === 'days' ? 'month' : 'year';
    picker.back.hidden = view === 'years';
    picker.ahead.hidden = view === 'years';
    picker.back.setAttribute('aria-label', `Previous ${unit}`);
    picker.ahead.setAttribute('aria-label', `Next ${unit}`);
    const [before, after] = view === 'days'
      ? [overlaps(day(y, m - 1, 1), day(y, m, 0)), overlaps(day(y, m + 1, 1), day(y, m + 2, 0))]
      : [overlaps(day(y - 1, 0, 1), day(y - 1, 11, 31)), overlaps(day(y + 1, 0, 1), day(y + 1, 11, 31))];
    // Marked rather than disabled, so a step the keyboard is on keeps its focus at the end of the range.
    picker.back.toggleAttribute('aria-disabled', !before);
    picker.ahead.toggleAttribute('aria-disabled', !after);
    if (view === 'years') reveal(current(), true);
    paint();
    if (inside) current()?.focus({ preventScroll: true });
  }

  // A range's days: the one chosen, or while picking, from the first day pressed to the day under the pointer (or
  // the keyboard). Its ends filled, the days between lit.
  function paint() {
    if (picker?.kind !== 'range') return;
    let [a, b] = picker.anchor !== null ? [picker.anchor, picker.hover ?? picker.anchor] : [picker.range.start, picker.range.end];
    if (a !== null && b !== null && b < a) [a, b] = [b, a];
    picker.panel.toggleAttribute('data-picking', picker.anchor !== null);
    picker.hint.textContent = picker.anchor === null ? 'Choose the first day' : `From ${short(picker.anchor)}: now choose the last day`;
    if (picker.view !== 'days') return;
    picker.body.querySelectorAll('.date-picker__day').forEach((cell) => {
      const t = Number(cell.dataset.t);
      const inside = a !== null && b !== null && t >= a && t <= b;
      cell.setAttribute('aria-selected', String(inside));
      if (!inside) cell.removeAttribute('data-range');
      else cell.dataset.range = a === b ? 'single' : t === a ? 'start' : t === b ? 'end' : 'between';
    });
  }

  // Keeps a year in sight in the years' scrolling list.
  function reveal(cell, centre = false) {
    const list = cell?.parentElement;
    if (!list?.classList.contains('date-picker__years')) return;
    const row = cell.getBoundingClientRect();
    const view = list.getBoundingClientRect();
    if (centre) list.scrollTop += row.top - view.top - (view.height - row.height) / 2;
    else if (row.top < view.top + 4) list.scrollTop -= view.top + 4 - row.top;
    else if (row.bottom > view.bottom - 4) list.scrollTop += row.bottom - view.bottom + 4;
  }

  // Moves the keyboard's place, drawing the calendar again only when it leaves the month (or year) on show.
  function show(t, { focus = false } = {}) {
    const before = split(picker.focus);
    const after = split(t);
    picker.focus = t;
    const same = picker.view === 'years' || (before.y === after.y && (picker.view === 'months' || before.m === after.m));
    if (same) {
      const key = keyOf(t);
      picker.body.querySelectorAll('.date-picker__cell').forEach((cell) => { cell.tabIndex = cell.dataset.key === key ? 0 : -1; });
    } else render();
    if (picker.kind === 'range' && picker.anchor !== null && picker.view === 'days') { picker.hover = t; paint(); }
    const cell = current();
    if (!focus || !cell) return;
    cell.focus({ preventScroll: true });
    reveal(cell);
  }

  function open(field, { via = 'mouse' } = {}) {
    close();
    const kind = kindOf(field);
    const sheet = phone.matches;
    const value = kind === 'range' ? null : read(field.value, kind);
    picker = { field, kind, sheet, touch: via === 'touch', value, today: today(), view: kind === 'month' ? 'months' : 'days', above: undefined, ...limits(field, kind) };
    if (kind === 'range') Object.assign(picker, { range: rangeOf(field), anchor: null, hover: null, committed: false });
    picker.focus = value ?? (kind === 'range' && picker.range.end !== null ? within(picker.range.end) : within(picker.today));
    Object.assign(picker, build(field, kind, sheet));
    const { panel } = picker;
    // A phone's keyboard, up for the field before, goes down: there's nothing to type here.
    const typing = document.activeElement;
    if (picker.touch && typing instanceof HTMLElement && typing.matches('input, textarea, [contenteditable]')) typing.blur();
    // Next to the field, so it belongs to the same dialog, form or Filters menu; outside a label, so a click in the
    // calendar isn't a click on the label too.
    (field.closest('label') || field).after(panel);
    field.setAttribute('aria-controls', panel.id);
    field.setAttribute('data-date-picker-open', '');
    if (kind === 'range') field.setAttribute('aria-expanded', 'true');
    panel.addEventListener('keydown', key);
    // While picking a range, the days between light up under the pointer.
    panel.addEventListener('pointerover', (event) => {
      const cell = event.target.closest?.('.date-picker__day');
      if (!picker || picker.anchor === null || !cell || cell.hasAttribute('aria-disabled')) return;
      picker.hover = Number(cell.dataset.t);
      paint();
    });
    // A press in the calendar leaves focus where it was: in the field, typing, or on the keyboard's day.
    panel.addEventListener('mousedown', (event) => event.preventDefault());
    panel.addEventListener('click', click);
    // Tab or Shift+Tab out of the calendar closes it (back to the field leaves it open); focus moves on as usual.
    panel.addEventListener('focusout', (event) => {
      const next = event.relatedTarget;
      if (picker?.panel === panel && next && next !== picker.field && !panel.contains(next)) close();
    });
    const { y, m } = split(picker.today);
    if (picker.now) picker.now.disabled = kind === 'month' ? !overlaps(day(y, m, 1), day(y, m + 1, 0)) : !allowed(picker.today);
    render();
    panel.showPopover();
    place();
    if (via === 'keyboard') current()?.focus({ preventScroll: true });
  }

  function close({ focus = false } = {}) {
    if (!picker) return;
    const { field, panel, kind, committed } = picker;
    picker = null;
    field.removeAttribute('aria-controls');
    field.removeAttribute('data-date-picker-open');
    if (kind === 'range') {
      field.setAttribute('aria-expanded', 'false');
      field.dispatchEvent(new CustomEvent('date-range:close', { bubbles: true, detail: { committed: Boolean(committed) } }));
    }
    if (focus && field.isConnected && document.activeElement !== field) field.focus({ preventScroll: true });
    if (panel.matches(':popover-open')) panel.hidePopover();
    // Gone once this click is over, so code further up still finds the click inside its menu (filter_menu.js).
    window.setTimeout(() => panel.remove());
  }

  function place() {
    if (!picker) return;
    const { field, panel, sheet } = picker;
    if (sheet) {
      // Above a phone's keyboard, if one is still up.
      const view = window.visualViewport;
      const covered = view ? Math.max(0, window.innerHeight - view.height - view.offsetTop) : 0;
      panel.style.bottom = `calc(${Math.round(covered)}px + .6rem + env(safe-area-inset-bottom))`;
      return;
    }
    const box = field.getBoundingClientRect();
    if ((!box.width && !box.height) || box.bottom < 0 || box.top > window.innerHeight) { close(); return; }
    const width = panel.offsetWidth;
    const height = panel.offsetHeight;
    const below = window.innerHeight - box.bottom - GAP - EDGE;
    const above = box.top - GAP - EDGE;
    // Below the field unless it only fits above; it stays on that side while the page scrolls.
    if (picker.above === undefined) picker.above = height > below && above > below;
    const top = picker.above ? box.top - GAP - height : box.bottom + GAP;
    // Lined up with the field's left edge, or its right edge if it would run off the window.
    const left = box.left + width > window.innerWidth - EDGE ? box.right - width : box.left;
    Object.assign(panel.style, {
      left: `${Math.round(Math.max(EDGE, Math.min(left, window.innerWidth - width - EDGE)))}px`,
      // A window too short for it either way: over the field, rather than off the screen.
      top: `${Math.round(Math.max(EDGE, Math.min(top, window.innerHeight - height - EDGE)))}px`,
    });
  }

  function commit(text) {
    if (!picker) return;
    const { field, touch } = picker;
    const changed = field.value !== text;
    field.value = text;
    // A phone would open its own calendar if focus went back to the field.
    close({ focus: !touch });
    if (!changed) return;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    field.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // A range's first press marks its first day; the second its last (or its first, if it's earlier).
  function pick(t) {
    if (picker.anchor === null) {
      picker.anchor = t;
      picker.hover = t;
      show(t);
      paint();
      return;
    }
    const [a, b] = t < picker.anchor ? [t, picker.anchor] : [picker.anchor, t];
    const { field, touch } = picker;
    const [first, last] = ends(field);
    const changed = first.value !== isoDay(a) || last.value !== isoDay(b);
    first.value = isoDay(a);
    last.value = isoDay(b);
    const text = field.querySelector('[data-range-text]');
    if (text) text.textContent = `${short(a)} – ${short(b)}`;
    picker.committed = true;
    close({ focus: !touch });
    if (changed) field.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function click(event) {
    if (!picker) return;
    const { panel } = picker;
    // On a phone, a tap on the dimmed page around the sheet lands on the sheet's backdrop.
    if (event.target === panel) {
      const box = panel.getBoundingClientRect();
      const inside = event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) { event.preventDefault(); close(); }
      return;
    }
    const control = event.target.closest('button');
    if (!control || control.disabled || control.hasAttribute('aria-disabled')) return;
    // Enter or Space on a cell, rather than a press: the keyboard follows the calendar into its next view.
    const keyboard = event.detail === 0;
    if (control.classList.contains('date-picker__close')) { close({ focus: !picker.touch }); return; }
    if (control.dataset.action === 'clear') { commit(''); return; }
    if (control.dataset.action === 'today') { commit(picker.kind === 'month' ? isoMonth(picker.today) : isoDay(picker.today)); return; }
    if (control.dataset.step) {
      show(within(addMonths(picker.focus, Number(control.dataset.step) * (picker.view === 'days' ? 1 : 12))));
      return;
    }
    const { m, d } = split(picker.focus);
    if (control === picker.period) {
      picker.view = picker.view !== 'years' ? 'years' : picker.kind === 'month' ? 'months' : 'days';
    } else {
      const t = Number(control.dataset.t);
      const { y } = split(t);
      if (control.classList.contains('date-picker__day')) {
        if (picker.kind === 'range') pick(t);
        else commit(isoDay(t));
        return;
      }
      if (control.classList.contains('date-picker__month')) {
        if (picker.kind === 'month') { commit(isoMonth(t)); return; }
        const month = split(t).m;
        picker.focus = within(day(y, month, Math.min(d, lastOf(y, month))));
        picker.view = 'days';
      } else if (control.classList.contains('date-picker__year')) {
        picker.focus = within(day(y, m, Math.min(d, lastOf(y, m))));
        picker.view = 'months';
      } else return;
    }
    render();
    if (keyboard) current()?.focus({ preventScroll: true });
  }

  // Arrow keys move a day (a month, a year) at a time and Up and Down a row; Home and End go to the row's ends (a
  // week's, for days); Page Up and Down move a month, or a year with Shift (in the years, 12 years).
  function key(event) {
    if (!picker) return;
    if (event.key === 'Escape' || (event.key === 'ArrowUp' && event.altKey)) {
      event.preventDefault();
      event.stopPropagation();
      // The same keypress mustn't go on to close the dialog the calendar is in (see cancel, below).
      escaping = true;
      window.setTimeout(() => { escaping = false; });
      close({ focus: true });
      return;
    }
    if (!event.target.closest?.('.date-picker__cell') || event.ctrlKey || event.metaKey || event.altKey) return;
    const { view } = picker;
    const t = picker.focus;
    const { y, m, weekday } = split(t);
    const across = view === 'days' ? 7 : view === 'months' ? 3 : 4;
    const by = (n) => (view === 'days' ? t + n * DAY : addMonths(t, view === 'months' ? n : n * 12));
    const { first, last } = years();
    let next;
    switch (event.key) {
      case 'ArrowLeft': next = by(-1); break;
      case 'ArrowRight': next = by(1); break;
      case 'ArrowUp': next = by(-across); break;
      case 'ArrowDown': next = by(across); break;
      case 'Home': next = view === 'days' ? by(-weekday) : view === 'months' ? by(-m) : by(first - y); break;
      case 'End': next = view === 'days' ? by(6 - weekday) : view === 'months' ? by(11 - m) : by(last - y); break;
      case 'PageUp':
      case 'PageDown': {
        const months = view === 'days' && !event.shiftKey ? 1 : view === 'years' ? 144 : 12;
        next = addMonths(t, event.key === 'PageDown' ? months : -months);
        break;
      }
      default: return;
    }
    event.preventDefault();
    if (view === 'years') {
      const at = split(next).y;
      next = addMonths(next, 12 * (Math.min(Math.max(at, first), last) - at));
    }
    show(within(next), { focus: true });
  }

  // Opening, instead of the browser's calendar: a click on the field (not one passed on by its label, which only
  // focuses it), and a second click closes it...
  listen(document, 'click', (event) => {
    const range = event.target.closest?.('[data-date-range]');
    if (range && usable(range)) {
      event.preventDefault();
      pressed = null;
      if (event.button !== 0) return;
      if (picker?.field === range) close();
      else open(range, { via: event.detail === 0 ? 'keyboard' : 'mouse' });
      return;
    }
    const field = event.target;
    if (!usable(field)) return;
    event.preventDefault();
    const own = pressed === field;
    pressed = null;
    if (event.button !== 0 || !own) return;
    if (picker?.field === field) close();
    else open(field);
  }, true);

  // ...a tap, on the field or its label (not a swipe that starts on it, which still scrolls the page)...
  listen(document, 'touchstart', (event) => {
    const field = fieldAt(event.target);
    const touch = event.touches[0];
    tap = field && event.touches.length === 1 ? { field, x: touch.clientX, y: touch.clientY } : null;
  }, { passive: true, capture: true });
  listen(document, 'touchmove', (event) => {
    const touch = event.touches[0];
    if (tap && touch && Math.hypot(touch.clientX - tap.x, touch.clientY - tap.y) > 10) tap = null;
  }, { passive: true, capture: true });
  listen(document, 'touchcancel', () => { tap = null; }, { passive: true, capture: true });
  listen(document, 'touchend', (event) => {
    const touched = tap;
    tap = null;
    if (!touched || !event.cancelable || fieldAt(event.target) !== touched.field) return;
    event.preventDefault();
    if (picker?.field === touched.field) close();
    else open(touched.field, { via: 'touch' });
  }, { passive: false, capture: true });

  // ...and Alt+Down or F4, the keys that open the browser's. Escape in the field closes it.
  listen(document, 'keydown', (event) => {
    const field = event.target;
    if (event.defaultPrevented || !usable(field)) return;
    const shown = picker?.field === field;
    if (shown && event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      escaping = true;
      window.setTimeout(() => { escaping = false; });
      close();
    } else if ((event.altKey && event.key === 'ArrowDown') || event.key === 'F4') {
      event.preventDefault();
      if (shown) current()?.focus({ preventScroll: true });
      else open(field, { via: 'keyboard' });
    }
  });

  // A date typed into the field shows in the calendar as it's typed.
  listen(document, 'input', (event) => {
    if (!picker || event.target !== picker.field) return;
    picker.value = read(picker.field.value, picker.kind);
    if (picker.value !== null) {
      picker.focus = picker.value;
      picker.view = picker.kind === 'month' ? 'months' : 'days';
    }
    render();
  }, true);
  // Tab out of the field closes it, unless it's into the calendar.
  listen(document, 'focusout', (event) => {
    if (!picker || event.target !== picker.field) return;
    const next = event.relatedTarget;
    if (next && next !== picker.field && !picker.panel.contains(next)) close();
  }, true);

  // A press anywhere else closes it, and that's all it does, as with the browser's own calendar: its click doesn't go
  // on to a button, a link or the dialog's backdrop (a tap on the dimmed page round a phone's sheet would otherwise
  // close the dialog under it too).
  listen(document, 'pointerdown', (event) => {
    swallow = false;
    pressed = event.target;
    if (!picker || picker.field.contains(event.target) || picker.panel.contains(event.target)) return;
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
  // Escape in the calendar closes the calendar, not the dialog it's in (on the window, ahead of fluid.js).
  listen(window, 'cancel', (event) => {
    if (!escaping && !(picker && event.target.contains?.(picker.panel))) return;
    event.preventDefault();
    event.stopPropagation();
    close({ focus: Boolean(picker && !picker.touch) });
  }, true);
  listen(window, 'scroll', (event) => { if (picker && !picker.panel.contains(event.target)) place(); }, { capture: true, passive: true });
  listen(window, 'resize', () => { if (picker && picker.sheet !== phone.matches) close(); else place(); });
  listen(window.visualViewport, 'resize', place);
  listen(window.visualViewport, 'scroll', place);
  lifecycle?.cleanup(() => close());
})();

});
