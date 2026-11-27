/* Settings > Screening questions editor (templates/portal/screening_draft.html). Without scripts every panel is a plain
   form and the list a set of links, so the page still works. With them: picking a row shows its panel, typing in a
   panel autosaves it (600 ms after the last keystroke), Move up / Move down and dragging reorder the list, and the
   patient preview on the right reloads after each save, scrolled to the question being edited. At 1100px and under the
   preview lives in the Preview dialog instead. A refused save (draft published, discarded or replaced) stops autosave
   until the page is reloaded. */
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
  let selectedKey = document.querySelector('[data-panel-for]:not([hidden])')?.dataset.panelFor || '';
  const timers = new Map();
  const saving = new Map();
  const again = new Set();

  const csrf = form => form.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  const panelFor = key => document.querySelector(`form[data-screening-panel="${CSS.escape(key)}"]`);
  const rowFor = key => rows.querySelector(`[data-question-key="${CSS.escape(key)}"]`) || list.querySelector(`[data-fixed-key="${CSS.escape(key)}"]`);

  /* Messages */
  const alertBox = document.createElement('div');
  alertBox.className = 'screening-alert';
  alertBox.setAttribute('role', 'alert');
  alertBox.hidden = true;
  const status = document.createElement('p');
  status.className = 'screening-status';
  status.setAttribute('role', 'status');
  editor.before(alertBox, status);
  function showAlert(message) { alertBox.textContent = message || STALE; alertBox.hidden = false; }
  function refuse(message) {
    stale = true;
    timers.forEach(timer => window.clearTimeout(timer));
    timers.clear();
    showAlert(message);
    status.textContent = '';
  }
  async function readJson(response) {
    try { return await response.json(); } catch (error) { return null; }
  }
  function refusal(response, data) {
    if (response.status === 404) return STALE;
    return data?.errors?.__all__?.[0] || STALE;
  }

  /* Preview */
  const frames = () => [mainFrame, dialog?.querySelector('iframe')].filter(Boolean);
  function scrollFrame(frame) {
    try {
      const doc = frame.contentDocument;
      const card = selectedKey && doc?.getElementById(`screening-q-${selectedKey}`);
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

  /* Selecting a question */
  function select(key, { push = true } = {}) {
    if (!panelFor(key)) return;
    selectedKey = key;
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
    const link = event.target.closest('[data-screening-edit]');
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
    event.preventDefault();
    select(link.dataset.screeningEdit);
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
      if (add) add.hidden = fixed;
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
          const flagged = optionRows(form).find(row => {
            const label = row.querySelector('[data-option-label]').value.trim();
            return message.includes(`“${label}”`) && row.querySelector('[data-option-flag]').checked;
          });
          const target = message.startsWith('Say why') && flagged ? flagged.querySelector('[data-option-reason]') : null;
          if (target) attach(target, message); else loose.push(message);
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
    timers.set(key, window.setTimeout(() => { timers.delete(key); save(form); }, delay));
  }
  async function save(form) {
    if (stale) return;
    const key = form.dataset.screeningPanel;
    if (saving.get(key)) { again.add(key); return; }
    saving.set(key, true);
    serializeOptions(form);
    status.textContent = 'Saving…';
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        headers: { Accept: 'application/json', 'X-CSRFToken': csrf(form), 'X-Requested-With': 'XMLHttpRequest' },
        body: new URLSearchParams(new FormData(form)),
        credentials: 'same-origin',
      });
      const data = await readJson(response);
      if (response.ok && data?.ok) {
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
        status.textContent = 'Saved';
        reloadPreview();
      } else if (response.status === 400) {
        status.textContent = '';
        showErrors(form, data?.errors);
      } else {
        refuse(refusal(response, data));
      }
    } catch (error) {
      status.textContent = 'Could not save. Check your connection; the next change tries again.';
    } finally {
      saving.set(key, false);
      if (again.delete(key) && !stale) schedule(form, 0);
    }
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
      if (CHOICE.includes(type)) while (optionRows(form).length < 2) addOption(form);
      syncPanel(form);
    } else if (event.target.matches('[data-option-flag]')) {
      syncPanel(form);
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
  async function reorder(previous) {
    if (stale) return false;
    status.textContent = 'Saving…';
    try {
      const response = await fetch(reorderUrl, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRFToken': csrf(document), 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ keys: order(), draft_id: Number(draftId) }),
        credentials: 'same-origin',
      });
      const data = await readJson(response);
      if (response.ok && data?.ok) {
        status.textContent = 'Saved';
        reloadPreview();
        return true;
      }
      previous.forEach(key => { const row = rowFor(key); if (row) rows.append(row); });
      syncMoves();
      refuse(refusal(response, data));
    } catch (error) {
      previous.forEach(key => { const row = rowFor(key); if (row) rows.append(row); });
      syncMoves();
      status.textContent = 'Could not reorder. Check your connection and try again.';
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
    if (button.dataset.move === 'up') sibling.before(row); else sibling.after(row);
    syncMoves();
    button.focus();
    reorder(previous);
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
    dragged = null;
    syncMoves();
    if (order().join() !== before.join()) reorder(before);
  });

  document.querySelectorAll('form[data-screening-panel]').forEach(form => {
    if (!form.elements.options) return;
    form.elements.options.value = form.elements.options.value || '[]';
  });
  prepareRows();
  lifecycle?.cleanup(() => { timers.forEach(timer => window.clearTimeout(timer)); alertBox.remove(); status.remove(); });
});
