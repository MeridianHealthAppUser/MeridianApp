/* The clinical Overview of a patient record: the log filter, the consult note pop-up, the
   prescription dialogs and inline summary editing. The Weight tab uses the consult note pop-up too.
   Without scripts, each of these still works through ordinary links and forms. */
/* Request blood tests: the button opens the preview with the filled form, and sending emails it. */
(() => {
  const card = document.querySelector('[data-bloods-form]');
  const dialog = document.getElementById('record-bloods-dialog');
  if (!card || !dialog || typeof dialog.showModal !== 'function') return;
  const field = card.querySelector('textarea[name="tests"]');
  const previewForm = dialog.querySelector('[data-bloods-preview-form]');
  const frame = dialog.querySelector('[data-bloods-frame]');
  function fill(value) {
    dialog.querySelectorAll('[data-bloods-tests-input]').forEach((input) => { input.value = value; });
    const preview = dialog.querySelector('[data-bloods-tests-preview]');
    if (preview) preview.textContent = value;
  }
  // A preview that comes back as a page rather than the PDF couldn't be made: say why in a toast too.
  let previewing = false;
  if (frame) frame.addEventListener('load', () => {
    if (!previewing) return;
    previewing = false;
    let page = null;
    try { page = frame.contentDocument; } catch (error) { return; }
    if (!page || page.contentType !== 'text/html' || !window.meridianToast) return;
    const reason = page.querySelector('[data-preview-error]');
    window.meridianToast(reason ? reason.textContent.trim() : 'The filled form couldn’t be loaded. Try again in a moment.', 'error');
  });
  function preview() {
    if (!previewForm || !frame) return;
    previewing = true;
    previewForm.submit();
  }
  card.addEventListener('submit', (event) => {
    if (!field.value.trim()) return;  // The browser asks for the required text.
    event.preventDefault();
    fill(field.value.trim());
    if (dialog.open) dialog.close();
    dialog.showModal();
    preview();
  });
  // A preview the server opened (without scripts) shows the form too.
  if (dialog.hasAttribute('open') && field.value.trim()) preview();
})();

(() => {
  const root = document.querySelector('[data-record-overview], [data-record-weight]');
  if (!root) return;
  root.classList.add('is-enhanced');

  /* Log filter: Clinical or Dispensed, without reloading the page. */
  const filterLinks = [...root.querySelectorAll('[data-log-filter]')];
  function showLog(name) {
    filterLinks.forEach((link) => {
      const active = link.dataset.logFilter === name;
      link.classList.toggle('is-active', active);
      if (active) link.setAttribute('aria-current', 'true'); else link.removeAttribute('aria-current');
    });
    root.querySelectorAll('[data-log-list]').forEach((list) => { list.hidden = list.dataset.logList !== name; });
    root.querySelectorAll('[data-log-count]').forEach((count) => { count.hidden = count.dataset.logCount !== name; });
    const url = new URL(window.location.href);
    url.searchParams.set('log', name);
    window.history.replaceState(null, '', url);
  }
  filterLinks.forEach((link) => link.addEventListener('click', (event) => {
    event.preventDefault();
    showLog(link.dataset.logFilter);
  }));

  /* Dialogs open as modals. One the server opened (after a form error) becomes a modal too. */
  function openDialog(dialog) {
    if (!dialog || typeof dialog.showModal !== 'function') return false;
    if (dialog.open) dialog.close();
    dialog.showModal();
    const field = dialog.querySelector('[role="alert"]') ? dialog.querySelector('input:not([type="hidden"]), select') : null;
    if (field) field.focus();
    return true;
  }
  document.querySelectorAll('[data-dialog-open]').forEach((trigger) => trigger.addEventListener('click', (event) => {
    if (openDialog(document.getElementById(trigger.dataset.dialogOpen))) event.preventDefault();
  }));
  document.querySelectorAll('.record-dialog').forEach((dialog) => {
    dialog.querySelectorAll('[data-dialog-close]').forEach((button) => button.addEventListener('click', (event) => {
      event.preventDefault();
      dialog.close();
    }));
    // A click on the backdrop closes the dialog.
    dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
    if (dialog.hasAttribute('open')) openDialog(dialog);
  });
  // Opening a dialog from the address bar, as the no-script links do, leaves it there; tidy the URL.
  const url = new URL(window.location.href);
  if (url.searchParams.has('prescription')) {
    url.searchParams.delete('prescription');
    window.history.replaceState(null, '', url.pathname + url.search);
  }

  /* Renew: the new expiry follows the months chosen. */
  const renew = document.querySelector('[data-renew-form]');
  if (renew) {
    const expiry = renew.querySelector('[data-renew-expiry]');
    renew.addEventListener('change', () => {
      const chosen = renew.querySelector('input[name="months"]:checked');
      if (chosen && expiry) expiry.textContent = chosen.dataset.expires;
    });
  }

  /* Change: the doses follow the medicine chosen, and the preview follows both and the months. */
  const change = document.querySelector('[data-change-form]');
  if (change) {
    const medicine = change.querySelector('[data-change-medicine]');
    const dose = change.querySelector('[data-change-dose]');
    const preview = change.querySelector('[data-change-preview]');
    const options = dose ? [...dose.options] : [];
    function updatePreview() {
      const chosen = dose && dose.selectedOptions[0];
      const months = change.querySelector('input[name="months"]:checked');
      if (preview) preview.textContent = chosen && months ? `${chosen.dataset.label} · to ${months.dataset.expires}` : '';
    }
    function fillDoses() {
      if (!medicine || !dose) return;
      const keep = dose.value;
      dose.replaceChildren(...options.filter((option) => option.dataset.medicine === medicine.value));
      dose.value = options.some((option) => option.value === keep && option.dataset.medicine === medicine.value)
        ? keep : (dose.options[0] ? dose.options[0].value : '');
      updatePreview();
    }
    if (medicine) medicine.addEventListener('change', fillDoses);
    change.addEventListener('change', (event) => { if (event.target !== medicine) updatePreview(); });
    fillDoses();
  }

  /* Consult dots on the weight chart open the note. "Show in log" finds it in the Clinical log, or
     from the Weight tab goes to it on the Overview. */
  const consult = document.getElementById('record-consult');
  const showButton = consult && consult.querySelector('[data-consult-show]');
  let consultEntry = null;
  function openConsult(id) {
    const note = document.getElementById(`record-consult-${id}`);
    if (!note || !consult) return;
    consult.querySelector('[data-consult-meta]').textContent = `${note.dataset.date} · ${note.dataset.by}`;
    consult.querySelector('[data-consult-text]').innerHTML = note.innerHTML;
    consultEntry = note.dataset.entry;
    if (showButton && showButton.dataset.logUrl) showButton.href = `${showButton.dataset.logUrl}#${consultEntry}`;
    openDialog(consult);
  }
  root.querySelectorAll('[data-consult]').forEach((dot) => {
    dot.addEventListener('click', () => openConsult(dot.dataset.consult));
    dot.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      openConsult(dot.dataset.consult);
    });
  });
  if (showButton && !showButton.dataset.logUrl) showButton.addEventListener('click', () => {
    consult.close();
    showLog('clinical');
    const entry = consultEntry && document.getElementById(consultEntry);
    if (!entry) return;
    entry.scrollIntoView({ behavior: 'smooth', block: 'center' });
    entry.classList.remove('is-highlighted');
    void entry.offsetWidth;
    entry.classList.add('is-highlighted');
  });
  // Arriving from "Show in log" on the Weight tab: the entry is highlighted as well.
  const arrived = window.location.hash.startsWith('#log-') && document.getElementById(window.location.hash.slice(1));
  if (arrived && root.contains(arrived)) arrived.classList.add('is-highlighted');

  /* Summary data: Enter or leaving the field saves, a changed answer saves, Escape cancels. */
  root.querySelectorAll('[data-summary-form]').forEach((form) => {
    const field = form.querySelector('[name="value"]');
    if (!field) return;
    const original = field.value;
    let saving = false;
    let pressingButton = false;
    const save = () => {
      if (saving) return;
      if (field.value === original) { form.closest('details').open = false; return; }
      if (!form.reportValidity()) return;
      saving = true;
      form.requestSubmit();
    };
    if (field.matches('[data-summary-select]')) field.addEventListener('change', save);
    // Pressing Save or Cancel takes focus from the field before the click lands, so leave it to the button.
    form.addEventListener('pointerdown', (event) => {
      if (event.target.closest('button, a')) pressingButton = true;
    });
    field.addEventListener('focus', () => { pressingButton = false; });
    field.addEventListener('blur', (event) => {
      if (pressingButton || (event.relatedTarget && form.contains(event.relatedTarget))) return;
      // Escape closes the editor first; a closed editor is not saved.
      window.setTimeout(() => { if (form.closest('details').open) save(); }, 0);
    });
  });
})();
