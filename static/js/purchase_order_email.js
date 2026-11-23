/* Email purchase order: recipient chips, CC/BCC, files to attach and a preview of an uploaded order form.
   Without this script the form still works: To, CC and BCC are plain comma-separated fields and files are chosen
   with the browser's own picker. */
(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('purchase_order_email', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
  const dialog = document.querySelector('[data-po-email]');
  if (!dialog) return;
  const form = dialog.querySelector('[data-po-email-form]');
  const EMAIL = /^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$/;
  const icon = path => `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="${path}"/></svg>`;
  const CLOSE = 'm6 6 12 12M18 6 6 18';

  // Recipients: the typed field becomes a row of removable chips; a hidden input carries the list to the server.
  dialog.querySelectorAll('[data-recipients]').forEach(box => {
    const input = box.querySelector('input');
    if (!input) return;
    const hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.name = input.name;
    input.removeAttribute('name');
    input.setAttribute('autocomplete', 'email');
    input.placeholder = 'Add email…';
    box.classList.add('is-enhanced');
    box.append(hidden);
    const orders = (box.dataset.ordersEmail || '').toLowerCase();
    const addresses = [];
    const sync = () => { hidden.value = [...addresses, input.value.trim()].filter(Boolean).join(', '); };
    const render = () => {
      box.querySelectorAll('.po-email__chip').forEach(chip => chip.remove());
      addresses.forEach((address, index) => {
        const chip = document.createElement('span');
        chip.className = 'po-email__chip';
        const text = document.createElement('span');
        text.textContent = address;
        chip.append(text);
        if (orders && address.toLowerCase() === orders) {
          const tag = document.createElement('em');
          tag.textContent = 'Orders';
          chip.append(tag);
        }
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.setAttribute('aria-label', `Remove ${address}`);
        remove.innerHTML = icon(CLOSE);
        listen(remove, 'click', () => { addresses.splice(index, 1); render(); input.focus(); });
        chip.append(remove);
        box.insertBefore(chip, input);
      });
      sync();
    };
    // Turn finished addresses into chips. Anything that isn't an email stays typed, so it can be corrected (and the
    // server names it if it's sent as it is). While typing, the part after the last separator is still in progress.
    const commit = (final) => {
      const parts = input.value.split(/[,;\n]/).map(part => part.trim());
      const typing = final ? '' : parts.pop();
      const invalid = [];
      parts.filter(Boolean).forEach(address => {
        if (!EMAIL.test(address)) invalid.push(address);
        else if (!addresses.some(known => known.toLowerCase() === address.toLowerCase())) addresses.push(address);
      });
      input.value = [...invalid, typing].filter(Boolean).join(', ');
      render();
    };
    input.value.split(/[,;\n]/).map(part => part.trim()).filter(Boolean).forEach(address => {
      if (!addresses.some(known => known.toLowerCase() === address.toLowerCase())) addresses.push(address);
    });
    input.value = '';
    render();
    listen(input, 'keydown', event => {
      if (event.key === 'Enter' || event.key === ',' || event.key === ';') {
        event.preventDefault();
        commit(true);
      } else if (event.key === 'Backspace' && !input.value && addresses.length) {
        addresses.pop();
        render();
      }
    });
    listen(input, 'input', () => { if (/[,;\n]/.test(input.value)) commit(false); else sync(); });
    listen(input, 'blur', () => commit(true));
    listen(box, 'click', event => { if (event.target === box) input.focus(); });
  });

  // CC and BCC open on request.
  dialog.querySelectorAll('[data-show-copy]').forEach(button => {
    listen(button, 'click', () => {
      const row = dialog.querySelector(`[data-copy-row="${button.dataset.showCopy}"]`);
      if (!row) return;
      row.hidden = false;
      button.hidden = true;
      row.querySelector('input:not([type=hidden])')?.focus();
    });
  });

  // Files: kept in a DataTransfer so they can be added in several goes and removed one at a time.
  const picker = dialog.querySelector('input[type=file][name=attachments]');
  const list = dialog.querySelector('[data-attachment-list]');
  const zone = dialog.querySelector('[data-drop-zone]');
  const count = dialog.querySelector('[data-attachment-count]');
  const error = dialog.querySelector('[data-attachment-error]');
  const previewEmpty = dialog.querySelector('[data-preview-empty]');
  const preview = dialog.querySelector('[data-upload-preview]');
  const ownForm = dialog.dataset.ownForm === 'true';
  const maxFiles = Number(dialog.dataset.maxFiles) || 5;
  const maxBytes = Number(dialog.dataset.maxBytes) || 10 * 1024 * 1024;
  const allowed = (picker?.accept || '').split(',').map(type => type.trim().replace(/^\./, '').toLowerCase()).filter(Boolean);
  let previewUrl = null;
  if (picker && list && zone && typeof DataTransfer !== 'undefined') {
    const store = new DataTransfer();
    const size = bytes => bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
    const extension = name => (name.includes('.') ? name.split('.').pop() : '').toLowerCase();
    const showError = message => { if (!error) return; error.textContent = message; error.hidden = !message; };
    const refreshPreview = () => {
      if (!ownForm || !preview) return;
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = null;
      preview.replaceChildren();
      const file = [...store.files].find(item => item.type === 'application/pdf' || item.type.startsWith('image/'));
      if (!file) {
        preview.hidden = true;
        if (previewEmpty) previewEmpty.hidden = false;
        return;
      }
      previewUrl = URL.createObjectURL(file);
      const view = file.type.startsWith('image/') ? document.createElement('img') : document.createElement('iframe');
      view.src = previewUrl;
      if (view.tagName === 'IMG') view.alt = file.name; else view.title = `Preview of ${file.name}`;
      preview.append(view);
      preview.hidden = false;
      if (previewEmpty) previewEmpty.hidden = true;
    };
    const render = () => {
      list.querySelectorAll('[data-chosen-file]').forEach(tile => tile.remove());
      [...store.files].forEach((file, index) => {
        const tile = document.createElement('div');
        tile.className = 'po-email__file';
        tile.dataset.chosenFile = '';
        const badge = document.createElement('span');
        badge.className = 'po-email__file-icon';
        badge.setAttribute('aria-hidden', 'true');
        badge.textContent = extension(file.name).toUpperCase().slice(0, 4) || 'FILE';
        const text = document.createElement('span');
        text.className = 'po-email__file-text';
        const name = document.createElement('strong');
        name.textContent = file.name;
        const meta = document.createElement('small');
        meta.textContent = size(file.size);
        text.append(name, meta);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.setAttribute('aria-label', `Remove ${file.name}`);
        remove.innerHTML = icon(CLOSE);
        listen(remove, 'click', () => {
          store.items.remove(index);
          picker.files = store.files;
          showError('');
          render();
          zone.focus?.();
        });
        tile.append(badge, text, remove);
        list.insertBefore(tile, zone);
      });
      if (count) count.textContent = String(store.files.length + (ownForm ? 0 : 1));
      refreshPreview();
    };
    const add = files => {
      let message = '';
      [...files].forEach(file => {
        if (!allowed.includes(extension(file.name))) {
          message = `“${file.name}” can’t be attached. Use a PDF, Excel, Word, CSV, PNG or JPG file.`;
          return;
        }
        if ([...store.files].some(known => known.name === file.name && known.size === file.size)) return;
        if (store.files.length >= maxFiles) {
          message = `Attach up to ${maxFiles} files.`;
          return;
        }
        const total = [...store.files].reduce((sum, known) => sum + known.size, 0);
        if (total + file.size > maxBytes) {
          message = 'Attachments must total 10 MB or less, including the purchase order PDF.';
          return;
        }
        store.items.add(file);
      });
      picker.files = store.files;
      showError(message);
      render();
    };
    listen(picker, 'change', () => {
      const chosen = [...picker.files].filter(file => ![...store.files].includes(file));
      picker.files = store.files;
      add(chosen);
    });
    ['dragenter', 'dragover'].forEach(type => listen(zone, type, event => { event.preventDefault(); zone.classList.add('is-dragging'); }));
    ['dragleave', 'drop'].forEach(type => listen(zone, type, () => zone.classList.remove('is-dragging')));
    listen(zone, 'drop', event => {
      event.preventDefault();
      if (event.dataTransfer?.files?.length) add(event.dataTransfer.files);
    });
    render();
  }

  // Recipients typed but not yet turned into chips still go with the form.
  listen(form, 'submit', () => dialog.querySelectorAll('[data-recipients] input:not([type=hidden])').forEach(input => input.dispatchEvent(new Event('blur'))));
  listen(dialog, 'close', () => { if (previewUrl) URL.revokeObjectURL(previewUrl); });
});
