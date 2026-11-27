/* Settings > Screening questions editor (templates/portal/screening_draft.html). Without scripts every panel is a plain
   form and the list a set of links, so the page still works. With them: picking a row shows its panel, typing in a
   panel autosaves it (600 ms after the last keystroke; the draft bar shows Saving… / Saved), Move up / Move down and
   dragging reorder the list (rows slide into place), and the patient preview reloads after each save, scrolled to the
   question being edited, at Full width or Phone size. At 1180px and under the preview lives in the Preview dialog
   instead. A refused save (draft published, discarded or replaced) stops autosave until the page is reloaded. */
(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('screening_builder', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  const list = document.querySelector('[data-screening-list]');
  if (!list) return;
  const rows = list.querySelector('[data-screening-rows]');
  const editor = document.querySelector('.screening-editor');
  const reorderUrl = list.dataset.reorderUrl;
  const draftId = list.dataset.draftId;
  const dialog = document.getElementById('screening-preview-dialog');
  const mainFrame = document.querySelector('iframe.screening-preview');
  const STALE = 'This draft was published or discarded. Reload the page.';
  const DELAY = 600;
  let stale = false;
  let leaving = false;
  let selectedKey = document.querySelector('[data-panel-for]:not([hidden])')?.dataset.panelFor || '';
  const MAX_ANSWERS = 12;
  const calm = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const stacked = () => window.matchMedia('(max-width: 1180px)').matches;
  const timers = new Map();
  const saving = new Map();
  const again = new Set();
  const unsaved = new Map();
  let reorderRun = null;
  let previewKey = selectedKey;
  let bypass = false;

  const csrf = form => form.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  const panelFor = key => document.querySelector(`form[data-screening-panel="${CSS.escape(key)}"]`);
  const rowFor = key => rows.querySelector(`[data-question-key="${CSS.escape(key)}"]`) || list.querySelector(`[data-fixed-key="${CSS.escape(key)}"]`);

  /* Messages */
  const alertBox = document.createElement('div');
  alertBox.className = 'screening-alert';
  alertBox.setAttribute('role', 'alert');
  alertBox.hidden = true;
  // The save state lives in the draft bar's slot; without one, in a line above the editor.
  const statusSlot = document.querySelector('[data-screening-status]');
  const status = statusSlot || document.createElement('p');
  if (!statusSlot) {
    status.className = 'screening-status';
    status.setAttribute('role', 'status');
  }
  editor.before(alertBox);
  if (!statusSlot) editor.before(status);
  editor.classList.add('is-live');
  function setStatus(text) {
    status.textContent = text;
    status.dataset.state = !text ? '' : text === 'Saving…' ? 'saving' : text === 'Saved' ? 'saved' : 'error';
  }
  function showAlert(message) { alertBox.textContent = message || STALE; alertBox.hidden = false; }
  function refuse(message) {
    stale = true;
    timers.forEach(timer => window.clearTimeout(timer));
    timers.clear();
    unsaved.clear();
    showAlert(message);
    setStatus('');
  }
  async function readJson(response) {
    try { return await response.json(); } catch (error) { return null; }
  }
  // Only a 409, a 404 or a login redirect means the draft is gone; a 500 or 403 can be retried.
  function staleMessage(response, data) {
    if (response.status === 404) return STALE;
    if (response.status === 409) return data?.errors?.__all__?.[0] || STALE;
    if (response.redirected) return 'Your session ended. Reload the page.';
    return null;
  }
  const hasPending = () => unsaved.size > 0 || saving.size > 0 || reorderRun !== null;

  /* Preview */
  const frames = () => [mainFrame, dialog?.querySelector('iframe')].filter(Boolean);
  function scrollFrame(frame) {
    try {
      const doc = frame.contentDocument;
      const card = previewKey && doc?.getElementById(`screening-q-${previewKey}`);
      if (!card) return;
      const top = card.getBoundingClientRect().top + frame.contentWindow.scrollY - 16;
      frame.contentWindow.scrollTo(0, Math.max(0, top));
    } catch (error) { /* a different origin: leave the preview where it is */ }
  }
  function reloadPreview() {
    frames().forEach(frame => {
      try { frame.contentWindow.location.reload(); } catch (error) { frame.src = frame.src; }
    });
  }
  function watchFrame(frame) { listen(frame, 'load', () => scrollFrame(frame)); }
  if (mainFrame) watchFrame(mainFrame);
  if (dialog) {
    listen(dialog, 'dialog:open', () => {
      const slot = dialog.querySelector('[data-preview-slot]');
      if (!slot || !mainFrame) return;
      let frame = slot.querySelector('iframe');
      if (!frame) {
        frame = document.createElement('iframe');
        frame.className = 'screening-preview';
        frame.title = 'Patient preview';
        frame.src = mainFrame.getAttribute('src');
        watchFrame(frame);
        slot.append(frame);
      } else {
        try { frame.contentWindow.location.reload(); } catch (error) { frame.src = frame.src; }
      }
    });
  }

  /* Preview size: Full width or Phone, remembered in this browser */
  const pane = document.querySelector('[data-preview-pane]');
  if (pane) {
    const DEVICE_KEY = 'meridian.screening-preview-device';
    const setDevice = device => {
      pane.dataset.device = device;
      pane.querySelectorAll('[data-preview-device]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.previewDevice === device)));
    };
    let remembered = null;
    try { remembered = window.localStorage.getItem(DEVICE_KEY); } catch (error) { /* storage blocked */ }
    if (remembered === 'phone' || remembered === 'desktop') setDevice(remembered);
    listen(pane, 'click', event => {
      const button = event.target.closest('[data-preview-device]');
      if (!button) return;
      setDevice(button.dataset.previewDevice);
      try { window.localStorage.setItem(DEVICE_KEY, button.dataset.previewDevice); } catch (error) { /* storage blocked */ }
    });
  }

  /* Selecting a question */
  function select(key, { push = true } = {}) {
    if (!panelFor(key)) return;
    selectedKey = key;
    previewKey = key;
    document.querySelectorAll('[data-panel-for]').forEach(panel => { panel.hidden = panel.dataset.panelFor !== key; });
    list.querySelectorAll('.screening-row').forEach(row => {
      row.classList.toggle('is-selected', (row.dataset.questionKey || row.dataset.fixedKey) === key);
    });
    if (push) {
      try { window.history.replaceState(window.history.state, '', `${window.location.pathname}?q=${encodeURIComponent(key)}`); } catch (error) { /* ignore */ }
    }
    frames().forEach(scrollFrame);
  }
  listen(list, 'click', event => {
    const row = event.target.closest('.screening-row');
    if (!row || event.button || event.metaKey || event.ctrlKey || event.shiftKey) return;
    if (event.target.closest('[data-move], .screening-row__retire, .screening-row__handle')) return;
    if (event.target.closest('[data-screening-edit]')) event.preventDefault();
    const key = row.dataset.questionKey || row.dataset.fixedKey;
    select(key);
    // One column (narrow screens): the panel sits below the list, so bring it into view.
    if (stacked()) panelFor(key)?.closest('.screening-panel')?.scrollIntoView({ behavior: calm() ? 'auto' : 'smooth', block: 'start' });
  });

  /* Options editor */
  const CHOICE = ['single', 'multiple'];
  function optionRows(form) { return [...form.querySelectorAll('[data-option-row]')]; }
  function serializeOptions(form) {
    const field = form.elements.options;
    if (!field) return;
    field.value = JSON.stringify(optionRows(form).map(row => {
      const value = row.querySelector('[data-option-value]').value;
      const item = {
        label: row.querySelector('[data-option-label]').value,
        flag: row.querySelector('[data-option-flag]').checked,
        reason: row.querySelector('[data-option-reason]').value,
      };
      return value ? { value, ...item } : item;
    }));
  }
  function syncPanel(form) {
    const type = form.elements.answer_type?.value;
    const fieldset = form.querySelector('[data-screening-options]');
    if (fieldset) {
      fieldset.hidden = !(CHOICE.includes(type) || type === 'yes_no');
      const fixed = type === 'yes_no';
      const items = optionRows(form);
      const add = fieldset.querySelector('[data-option-add]');
      if (add) { add.hidden = fixed; add.disabled = items.length >= MAX_ANSWERS; }
      items.forEach(row => {
        const remove = row.querySelector('[data-option-remove]');
        remove.hidden = fixed;
        remove.disabled = !fixed && items.length <= 2;
      });
      items.forEach(row => row.classList.toggle('is-flagged', row.querySelector('[data-option-flag]').checked));
    }
    const number = form.querySelector('.screening-number');
    if (number) number.hidden = type !== 'number';
  }
  function addOption(form) {
    const template = form.querySelector('template[data-option-template]');
    const add = form.querySelector('[data-option-add]');
    if (!template || !add) return null;
    const row = template.content.firstElementChild.cloneNode(true);
    add.before(row);
    return row;
  }
  // Yes/No is exactly the two rows yes and no: keep those, make any that is missing, drop the rest.
  function setYesNo(form) {
    const add = form.querySelector('[data-option-add]');
    if (!add) return;
    const byValue = {};
    optionRows(form).forEach(row => {
      const value = row.querySelector('[data-option-value]').value;
      if ((value === 'yes' || value === 'no') && !byValue[value]) byValue[value] = row; else row.remove();
    });
    [['yes', 'Yes'], ['no', 'No']].forEach(([value, label]) => {
      let row = byValue[value];
      if (!row) {
        row = addOption(form);
        if (!row) return;
        row.querySelector('[data-option-value]').value = value;
        row.querySelector('[data-option-label]').value = label;
      }
      add.before(row);
    });
  }
  document.querySelectorAll('form[data-screening-panel]').forEach(form => {
    syncPanel(form);
  });

  /* Errors */
  function clearErrors(form) {
    form.querySelectorAll('.screening-field-error').forEach(node => node.remove());
    form.querySelectorAll('[aria-invalid]').forEach(node => node.removeAttribute('aria-invalid'));
    const summary = form.querySelector('[data-panel-errors]');
    if (summary) summary.textContent = '';
  }
  function attach(node, message) {
    const span = document.createElement('span');
    span.className = 'screening-field-error';
    span.textContent = message;
    node.setAttribute('aria-invalid', 'true');
    node.after(span);
  }
  function showErrors(form, errors) {
    clearErrors(form);
    const loose = [];
    Object.entries(errors || {}).forEach(([name, messages]) => {
      (messages || []).forEach(message => {
        if (name === 'options') {
          const items = optionRows(form);
          let target = null;
          if (message.startsWith('Say why')) {
            const found = items.find(row => message.includes(`“${row.querySelector('[data-option-label]').value.trim()}”`) && row.querySelector('[data-option-flag]').checked);
            target = found?.querySelector('[data-option-reason]');
          } else if (message.startsWith('Every answer needs a label')) {
            target = items.find(row => !row.querySelector('[data-option-label]').value.trim())?.querySelector('[data-option-label]');
          } else if (message.startsWith('An answer label')) {
            target = items.find(row => row.querySelector('[data-option-label]').value.trim().length > 120)?.querySelector('[data-option-label]');
          }
          if (target) attach(target, message);
          else {
            const fieldset = form.querySelector('[data-screening-options]');
            if (fieldset) {
              const span = document.createElement('span');
              span.className = 'screening-field-error';
              span.textContent = message;
              fieldset.append(span);
            } else loose.push(message);
          }
          return;
        }
        const field = form.elements[name];
        const node = field && !field.length ? field : field?.[0];
        if (node && node.type !== 'hidden') attach(node, message); else loose.push(message);
      });
    });
    const summary = form.querySelector('[data-panel-errors]');
    if (summary && loose.length) summary.textContent = loose.join(' ');
  }

  /* Saving */
  function schedule(form, delay = DELAY) {
    if (stale) return;
    const key = form.dataset.screeningPanel;
    window.clearTimeout(timers.get(key));
    unsaved.set(key, form);
    timers.set(key, window.setTimeout(() => { timers.delete(key); save(form); }, delay));
  }
  function save(form) {
    const key = form.dataset.screeningPanel;
    if (stale) return Promise.resolve(false);
    if (saving.has(key)) { again.add(key); return saving.get(key); }
    const run = doSave(form, key).finally(() => {
      saving.delete(key);
      if (again.delete(key) && !stale) schedule(form, 0);
    });
    saving.set(key, run);
    return run;
  }
  async function doSave(form, key) {
    unsaved.delete(key);
    serializeOptions(form);
    setStatus('Saving…');
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        headers: { Accept: 'application/json', 'X-CSRFToken': csrf(form), 'X-Requested-With': 'XMLHttpRequest' },
        body: new URLSearchParams(new FormData(form)),
        credentials: 'same-origin',
        keepalive: true,
      });
      const data = await readJson(response);
      if (response.ok && data?.ok && !response.redirected) {
        clearErrors(form);
        const row = rowFor(key);
        if (row && data.row_html) {
          const holder = document.createElement('template');
          holder.innerHTML = data.row_html.trim();
          const fresh = holder.content.firstElementChild;
          if (fresh) { row.replaceWith(fresh); prepareRows(); }
        }
        const heading = form.querySelector('h3');
        if (heading) heading.textContent = form.elements.label.value;
        showSummary(data.summary_html);
        setStatus('Saved');
        previewKey = key;
        reloadPreview();
        return true;
      }
      const gone = staleMessage(response, data);
      if (gone) { refuse(gone); return false; }
      unsaved.set(key, form);
      if (response.status === 400) {
        setStatus('');
        showErrors(form, data?.errors);
        return 'invalid';
      } else {
        setStatus('Could not save. Try again.');
      }
    } catch (error) {
      unsaved.set(key, form);
      setStatus('Could not save. Check your connection; the next change tries again.');
    }
    return false;
  }
  function showSummary(html) {
    if (typeof html !== 'string') return;
    document.querySelectorAll('[data-screening-summary]').forEach(box => { box.innerHTML = html; });
  }
  // Saves every panel with an unsaved change and waits for saves and reorders in flight.
  // Resolves 'ok', 'invalid' (a 400 shows field errors), 'stale' (draft gone) or 'error' (anything else).
  async function flush() {
    let ok = true;
    let invalid = false;
    let forms = [...unsaved.values()];
    for (let pass = 0; pass < 5; pass += 1) {
      forms.forEach(form => { window.clearTimeout(timers.get(form.dataset.screeningPanel)); timers.delete(form.dataset.screeningPanel); });
      const runs = [...forms.map(save), ...saving.values()];
      if (reorderRun) runs.push(reorderRun);
      if (!runs.length) break;
      const results = await Promise.all(runs);
      if (results.includes('invalid')) invalid = true;
      if (results.includes(false) || invalid) ok = false;
      forms = [...timers.keys()].map(key => unsaved.get(key)).filter(Boolean);
      if (!forms.length) break;
    }
    if (stale) return 'stale';
    if (invalid) return 'invalid';
    return ok ? 'ok' : 'error';
  }
  function flushFailed() {
    const bad = document.querySelector('form[data-screening-panel] [aria-invalid]')?.closest('form[data-screening-panel]');
    if (bad && !stale) { select(bad.dataset.screeningPanel); bad.scrollIntoView({ block: 'center' }); }
    if (!stale) setStatus('Fix the highlighted problem first.');
  }
  // Runs go() once pending saves are flushed. A form in a dialog shows progress in the dialog and closes it on failure,
  // so the error is not left behind the modal. Repeat clicks on the same control are ignored while it waits.
  const guarding = new WeakSet();
  function guard(control, go) {
    if (guarding.has(control)) return;
    guarding.add(control);
    const dlg = control.closest?.('dialog');
    const buttons = dlg ? [...control.querySelectorAll('button[type="submit"]')] : [];
    let note = null;
    if (dlg) {
      buttons.forEach(button => { button.disabled = true; });
      note = document.createElement('p');
      note.className = 'screening-flush-note';
      note.setAttribute('role', 'status');
      note.textContent = 'Saving your last change…';
      control.prepend(note);
    }
    flush().then(result => {
      note?.remove();
      buttons.forEach(button => { button.disabled = false; });
      // Cancel, Keep editing or Escape while the flush ran: the user said no, so do nothing further.
      const dismissed = dlg && !dlg.open;
      if (result === 'ok') {
        if (dismissed) return;
        leaving = true;
        go();
        return;
      }
      dlg?.close();
      if (result === 'stale') { alertBox.scrollIntoView({ block: 'center' }); return; }
      if (result === 'invalid') { flushFailed(); return; }
      setStatus('Could not save your last change.');
      if (dismissed) return;
      if (window.confirm('Could not save your last change. Leave anyway?')) { leaving = true; go(); }
    }).finally(() => guarding.delete(control));
  }
  listen(document, 'input', event => {
    const form = event.target.closest?.('form[data-screening-panel]');
    if (form && !event.target.closest('template')) schedule(form);
  });
  listen(document, 'change', event => {
    const form = event.target.closest?.('form[data-screening-panel]');
    if (!form) return;
    if (event.target.name === 'answer_type') {
      const type = event.target.value;
      if (type === 'yes_no') setYesNo(form);
      if (CHOICE.includes(type)) while (optionRows(form).length < 2) addOption(form);
      syncPanel(form);
    } else if (event.target.matches('[data-option-flag]')) {
      syncPanel(form);
      // A flag needs its reason: go straight to it.
      if (event.target.checked) event.target.closest('[data-option-row]')?.querySelector('[data-option-reason]')?.focus();
    }
    schedule(form);
  });
  listen(document, 'submit', event => {
    const form = event.target.closest?.('form[data-screening-panel]');
    if (!form || event.target !== form) return;
    event.preventDefault();
    window.clearTimeout(timers.get(form.dataset.screeningPanel));
    save(form);
  });
  listen(document, 'click', event => {
    const form = event.target.closest?.('form[data-screening-panel]');
    if (!form) return;
    if (event.target.closest('[data-option-add]')) {
      if (optionRows(form).length >= MAX_ANSWERS) return;
      const row = addOption(form);
      syncPanel(form);
      row?.querySelector('[data-option-label]')?.focus();
      schedule(form);
    } else {
      const remove = event.target.closest('[data-option-remove]');
      if (!remove || remove.disabled) return;
      remove.closest('[data-option-row]').remove();
      syncPanel(form);
      schedule(form);
    }
  });

  /* Reordering */
  // Moves rows, then slides each one from where it was on screen to its new place. Positions are read from the screen
  // (mid-animation included) before any running slide is cancelled, so a second move carries on without a jump.
  function animateRows(mutate) {
    const items = [...rows.children];
    const first = new Map(items.map(item => [item, item.getBoundingClientRect().top]));
    items.forEach(item => item.getAnimations?.().forEach(animation => animation.cancel()));
    mutate();
    if (calm()) return;
    items.forEach(item => {
      const delta = first.get(item) - item.getBoundingClientRect().top;
      if (!delta || !item.animate) return;
      item.animate([{ transform: `translateY(${delta}px)` }, { transform: 'translateY(0)' }], { duration: 280, easing: 'cubic-bezier(.2, .8, .2, 1)' });
    });
  }
  const order = () => [...rows.querySelectorAll('[data-question-key]')].map(row => row.dataset.questionKey);
  function syncMoves() {
    const items = [...rows.querySelectorAll('[data-question-key]')];
    items.forEach((row, index) => {
      const up = row.querySelector('[data-move="up"]');
      const down = row.querySelector('[data-move="down"]');
      if (up) up.disabled = index === 0;
      if (down) down.disabled = index === items.length - 1;
    });
  }
  function reorder(previous, movedKey) {
    if (stale) return Promise.resolve(false);
    const run = doReorder(previous, movedKey).finally(() => { reorderRun = null; });
    reorderRun = run;
    return run;
  }
  function revert(previous) {
    previous.forEach(key => { const row = rowFor(key); if (row) rows.append(row); });
    syncMoves();
  }
  async function doReorder(previous, movedKey) {
    setStatus('Saving…');
    try {
      const response = await fetch(reorderUrl, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRFToken': csrf(document), 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ keys: order(), draft_id: Number(draftId) }),
        credentials: 'same-origin',
        keepalive: true,
      });
      const data = await readJson(response);
      if (response.ok && data?.ok && !response.redirected) {
        showSummary(data.summary_html);
        setStatus('Saved');
        previewKey = movedKey;
        reloadPreview();
        return true;
      }
      revert(previous);
      const gone = staleMessage(response, data);
      if (gone) refuse(gone); else setStatus(response.status === 400 ? 'Could not reorder.' : 'Could not save. Try again.');
    } catch (error) {
      revert(previous);
      setStatus('Could not reorder. Check your connection and try again.');
    }
    return false;
  }
  listen(list, 'click', event => {
    const button = event.target.closest('[data-move]');
    if (!button || stale) return;
    const row = button.closest('[data-question-key]');
    const previous = order();
    const sibling = button.dataset.move === 'up' ? row.previousElementSibling : row.nextElementSibling;
    if (!sibling) return;
    animateRows(() => { if (button.dataset.move === 'up') sibling.before(row); else sibling.after(row); });
    syncMoves();
    button.focus();
    reorder(previous, row.dataset.questionKey);
  });

  /* Dragging (native HTML5 events on the rows; the buttons above cover keyboards) */
  let dragged = null;
  let before = [];
  function prepareRows() {
    rows.querySelectorAll('[data-question-key]').forEach(row => { row.draggable = !stale; });
    syncMoves();
  }
  listen(rows, 'dragstart', event => {
    const row = event.target.closest?.('[data-question-key]');
    if (!row || stale) return;
    dragged = row;
    before = order();
    row.classList.add('is-dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', row.dataset.questionKey);
  });
  listen(rows, 'dragover', event => {
    if (!dragged) return;
    event.preventDefault();
    const over = event.target.closest('[data-question-key]');
    if (!over || over === dragged) return;
    const box = over.getBoundingClientRect();
    if (event.clientY < box.top + box.height / 2) over.before(dragged); else over.after(dragged);
  });
  listen(rows, 'drop', event => { if (dragged) event.preventDefault(); });
  listen(rows, 'dragend', () => {
    if (!dragged) return;
    dragged.classList.remove('is-dragging');
    const movedRow = dragged;
    dragged = null;
    syncMoves();
    if (order().join() !== before.join()) reorder(before, movedRow.dataset.questionKey);
  });

  document.querySelectorAll('form[data-screening-panel]').forEach(form => {
    if (!form.elements.options) return;
    form.elements.options.value = form.elements.options.value || '[]';
  });
  prepareRows();

  /* Leaving the page or posting another form first saves what is pending. */
  listen(document, 'submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.matches('[data-screening-panel]') || !hasPending()) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    guard(form, () => form.submit());
  }, true);
  listen(document, 'click', event => {
    if (bypass || event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[href]');
    if (!link || link.hasAttribute('data-screening-edit') || link.target || link.hasAttribute('download') || !hasPending()) return;
    const href = link.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    guard(link, () => {
      bypass = true;
      try { link.click(); } finally { bypass = false; }
    });
  }, true);
  listen(window, 'beforeunload', event => {
    if (stale || leaving || !hasPending()) return;
    event.preventDefault();
    event.returnValue = '';
  });
  lifecycle?.cleanup(() => {
    flush();
    alertBox.remove();
    if (statusSlot) setStatus(''); else status.remove();
    editor.classList.remove('is-live');
  });
});
