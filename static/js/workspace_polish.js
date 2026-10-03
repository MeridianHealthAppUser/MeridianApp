/* Remember navigation groups and reveal the current desktop destination. */
(() => {
  function initializeRailNavigation() {
    document.querySelectorAll('[data-rail-navigation] [data-navigation-group]').forEach((group) => {
      const storageKey = `meridian.staff-navigation.v1.${group.dataset.navigationGroup}`;
      let savedState;
      try { savedState = window.localStorage.getItem(storageKey); } catch (_) { /* Storage is optional. */ }
      group.open = Boolean(group.querySelector('[aria-current="page"], [aria-current="true"]')) || savedState !== 'closed';
      // Native details may queue an initialization toggle; only remember a change.
      let previousOpen = group.open;
      group.addEventListener('toggle', () => {
        if (group.open === previousOpen) return;
        previousOpen = group.open;
        try { window.localStorage.setItem(storageKey, group.open ? 'open' : 'closed'); } catch (_) { /* Keep native toggling. */ }
      });
    });
    revealCurrentRail();
  }

  function revealCurrentRail() {
    document.querySelectorAll('[data-rail-navigation]').forEach((rail) => {
      if (!rail.clientHeight || rail.scrollHeight <= rail.clientHeight + 1) return;
      const current = rail.querySelector('[aria-current="page"], [aria-current="true"]');
      if (!current || !current.getClientRects().length) return;
      const bounds = rail.getBoundingClientRect();
      const item = current.getBoundingClientRect();
      if (item.top < bounds.top || item.bottom > bounds.bottom) {
        rail.scrollTop += item.top - bounds.top - (rail.clientHeight - item.height) / 2;
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeRailNavigation, { once: true });
  } else { initializeRailNavigation(); }
  window.addEventListener('pageshow', revealCurrentRail);
  let resizeFrame;
  window.addEventListener('resize', () => {
    window.cancelAnimationFrame(resizeFrame);
    resizeFrame = window.requestAnimationFrame(revealCurrentRail);
  });
})();

/* Keep the header and navigation in place and show a loader in the content area until the next page arrives. */
(() => {
  const SHOW_AFTER_MS = 150;
  const GIVE_UP_AFTER_MS = 15000;
  // The three arcs and dots of the Meridian mark.
  const SPINNER = `<svg class="workspace-page-loader__spinner" viewBox="0 0 64 64" aria-hidden="true" focusable="false">
    <g><path d="M45.16 36.79 A14 14 0 0 0 32 18" fill="none" stroke="#48c1c9" stroke-width="3" stroke-linecap="round"/><circle cx="26.05" cy="19.33" r="2.4" fill="#48c1c9"/></g>
    <g><path d="M50.79 38.84 A20 20 0 0 0 32 12" fill="none" stroke="#1599de" stroke-width="2.2" stroke-linecap="round"/><circle cx="26.65" cy="12.73" r="2.1" fill="#1599de"/></g>
    <g><path d="M56.43 40.89 A26 26 0 0 0 32 6" fill="none" stroke="#29698b" stroke-width="1.5" stroke-linecap="round"/><circle cx="27.27" cy="6.43" r="1.8" fill="#29698b"/></g>
  </svg>`;
  let showTimer;
  let giveUpTimer;
  // WebKit (Safari, and every browser on iPhone and iPad) stops painting as soon as the next page
  // starts loading, so a loader added after that is never seen. There it shows at once, and the
  // page leaves only after it has been painted (one frame, about 16ms later).
  const ua = navigator.userAgent;
  const PAINT_FIRST = /iP(hone|ad|od)/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
    || (/AppleWebKit/.test(ua) && !/(Chrome|Chromium|Edg|OPR|Android)/.test(ua));

  function afterPaint(callback) {
    requestAnimationFrame(() => requestAnimationFrame(callback));
  }

  function contentRegion() {
    return document.querySelector('.console-content, .patient-console__content, .mobile-console__content');
  }

  function showLoader() {
    const region = contentRegion();
    if (!region || region.classList.contains('is-page-loading')) return;
    const loader = document.createElement('div');
    loader.className = 'workspace-page-loader';
    loader.setAttribute('role', 'status');
    loader.innerHTML = `<div class="workspace-page-loader__inner">${SPINNER}<span class="workspace-page-loader__label">Loading</span></div>`;
    region.classList.add('is-page-loading');
    region.setAttribute('aria-busy', 'true');
    region.appendChild(loader);
    // A response the browser downloads never replaces this page.
    giveUpTimer = window.setTimeout(hideLoader, GIVE_UP_AFTER_MS);
  }

  function hideLoader() {
    window.clearTimeout(showTimer);
    window.clearTimeout(giveUpTimer);
    document.querySelectorAll('.workspace-page-loader').forEach((loader) => loader.remove());
    document.querySelectorAll('.is-page-loading').forEach((region) => {
      region.classList.remove('is-page-loading');
      region.removeAttribute('aria-busy');
    });
  }

  function queueLoader(event) {
    window.clearTimeout(showTimer);
    // Fast pages swap in before the loader would appear; prevented events never navigate.
    showTimer = window.setTimeout(() => { if (!event.defaultPrevented) showLoader(); }, SHOW_AFTER_MS);
  }

  function opensAnotherPage(link) {
    let url;
    try { url = new URL(link.href, window.location.href); } catch (_) { return false; }
    if (!/^https?:$/.test(url.protocol) || url.origin !== window.location.origin) return false;
    const samePage = url.pathname === window.location.pathname && url.search === window.location.search;
    return !(samePage && (url.hash || link.getAttribute('href').includes('#')));
  }

  // Listen on window: it hears clicks and submits last, after any other handler could cancel them.
  window.addEventListener('click', (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest && event.target.closest('a[href]');
    if (!link || link.closest('[data-no-loader]') || link.hasAttribute('download')) return;
    if ((link.target && link.target !== '_self') || !opensAnotherPage(link)) return;
    if (PAINT_FIRST) {
      event.preventDefault();
      showLoader();
      afterPaint(() => window.location.assign(link.href));
      return;
    }
    queueLoader(event);
  });

  window.addEventListener('submit', (event) => {
    if (event.defaultPrevented) return;
    const form = event.target;
    const submitter = event.submitter;
    // Read attributes: a field named "target" or "method" shadows the form property.
    const target = (submitter && submitter.getAttribute('formtarget')) || form.getAttribute('target');
    const method = (submitter && submitter.getAttribute('formmethod')) || form.getAttribute('method') || '';
    if (method.toLowerCase() === 'dialog' || (target && target !== '_self')) return;
    if (form.closest('[data-no-loader]') || (submitter && submitter.closest('[data-no-loader]'))) return;
    if (PAINT_FIRST) {
      // The second pass is our own resubmission once the loader has been painted.
      if (form.dataset.loaderPainted) { delete form.dataset.loaderPainted; return; }
      event.preventDefault();
      showLoader();
      afterPaint(() => {
        form.dataset.loaderPainted = '1';
        if (typeof form.requestSubmit === 'function') {
          form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
          return;
        }
        // Older Safari: keep the clicked button's name and value, which submit() would drop.
        if (submitter && submitter.name) {
          const field = document.createElement('input');
          field.type = 'hidden';
          field.name = submitter.name;
          field.value = submitter.value;
          form.appendChild(field);
        }
        HTMLFormElement.prototype.submit.call(form);
      });
      return;
    }
    queueLoader(event);
  });

  // Back and forward can restore this page from the cache with the loader still showing.
  window.addEventListener('pageshow', hideLoader);
})();

/* Inline editors: opening one focuses its field; Cancel or Escape restores the value and closes it. */
(() => {
  function closeEditor(editor) {
    const form = editor.querySelector('form');
    if (form) form.reset();
    editor.open = false;
    const summary = editor.querySelector('summary');
    if (summary) summary.focus();
  }

  // Toggle does not bubble, so listen while it travels down.
  document.addEventListener('toggle', (event) => {
    const editor = event.target;
    if (!editor.matches || !editor.matches('[data-inline-editor]') || !editor.open) return;
    const field = editor.querySelector('select, textarea, input:not([type="hidden"])');
    if (field) field.focus();
  }, true);

  document.addEventListener('click', (event) => {
    const cancel = event.target.closest && event.target.closest('[data-inline-editor-cancel]');
    const editor = cancel && cancel.closest('[data-inline-editor]');
    if (!editor) return;
    event.preventDefault();
    closeEditor(editor);
  });

  document.addEventListener('keydown', (event) => {
    const editor = event.key === 'Escape' && event.target.closest && event.target.closest('[data-inline-editor][open]');
    if (!editor) return;
    event.preventDefault();
    closeEditor(editor);
  });
})();

/* A form's submit buttons stay disabled until its required confirmation tick is checked.
   A button can instead name the one tick it waits for with data-requires-check. */
(() => {
  function connect(boxes, buttons) {
    if (!boxes.length || !buttons.length) return;
    const update = () => {
      const ready = boxes.every((box) => box.checked);
      buttons.forEach((button) => { button.disabled = !ready; });
    };
    boxes.forEach((box) => box.addEventListener('change', update));
    window.addEventListener('pageshow', update);
    update();
  }

  function initialize() {
    document.querySelectorAll('form').forEach((form) => {
      const paired = [...form.querySelectorAll('[data-requires-check]')];
      paired.forEach((button) => connect([document.getElementById(button.dataset.requiresCheck)].filter(Boolean), [button]));
      const boxes = [...form.querySelectorAll('input[type="checkbox"][required]')];
      // Buttons that skip validation (for example "Save draft") are left alone.
      const buttons = [...form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]')]
        .filter((button) => !button.hasAttribute('formnovalidate') && !paired.includes(button));
      connect(boxes, buttons);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, { once: true });
  } else { initialize(); }
})();

/* Pop-up pickers (such as the mobile patient section list) close on Escape or a tap elsewhere. */
(() => {
  const openPickers = () => document.querySelectorAll('.patient-workspace-picker[open]');
  document.addEventListener('click', (event) => {
    openPickers().forEach((picker) => { if (!picker.contains(event.target)) picker.open = false; });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    openPickers().forEach((picker) => { picker.open = false; picker.querySelector('summary').focus(); });
  });
})();
