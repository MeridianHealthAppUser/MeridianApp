/* One owner for authenticated URL-first navigation. Django remains the authority for every page. */
(() => {
  'use strict';
  if (window.MeridianNavigation || !window.fetch || !window.AbortController || !window.DOMParser) return;
  const configElement = document.getElementById('page-navigation-config');
  let config;
  try { config = JSON.parse(configElement?.textContent || 'null'); } catch (_) { return; }
  if (!config || config.version !== 1 || !config.scope || !document.getElementById('workspace-content')) return;

  const NATIVE_PATH = /^\/(?:accounts|admin|video|health|static|media|questionnaire)(?:\/|$)/;
  const FILE_PATH = /\.(?:pdf|ics|xlsx?|csv|zip|png|jpe?g|svg|webp|woff2?)(?:$|\/)/i;
  const REQUEST_TIMEOUT = 30000;
  let generation = 0;
  let pending = null;
  let sections = new Set();
  let sectionQueue = [];
  let renderedURL = window.location.href;
  let savingScroll = false;
  let routing = false;
  let historyPosition = Number.isInteger(history.state?.meridianPosition) ? history.state.meridianPosition : 0;
  let restoringCancelledPop = false;
  const formBaselines = new WeakMap();
  const editedForms = new Set();
  const loadedScripts = new Set([...document.querySelectorAll('script[src]')].map(script => new URL(script.src, location.href).href));
  const stylePromises = new Map();
  const scriptPromises = new Map();
  const root = () => document.getElementById('workspace-content');
  const region = () => root()?.querySelector('.console-content, .patient-console__content, .mobile-console__content') || root();
  const notify = (name, detail = {}) => document.dispatchEvent(new CustomEvent(name, { detail }));
  const absolute = value => {
    try {
      const url = new URL(value, window.location.href);
      return /^https?:$/.test(url.protocol) && url.origin === window.location.origin ? url : null;
    } catch (_) { return null; }
  };
  const nativeVisit = value => {
    const url = absolute(value);
    if (url) window.location.replace(url.href);
  };

  function stateWithScroll() {
    return { ...(history.state && typeof history.state === 'object' ? history.state : {}),
      meridianNavigation: true, meridianPosition: historyPosition, scroll: [window.scrollX, window.scrollY] };
  }
  function formValue(form) {
    return JSON.stringify([...form.elements].filter(field => field.name && !['submit', 'button', 'reset'].includes(field.type)).map(field =>
      [field.name, field.type === 'file' ? [...field.files].map(file => [file.name, file.size, file.lastModified])
        : ['checkbox', 'radio'].includes(field.type) ? field.checked : field.multiple && field.options
          ? [...field.selectedOptions].map(option => option.value) : field.value]));
  }
  function rememberForms() {
    editedForms.clear();
    document.querySelectorAll('form').forEach(form => formBaselines.set(form, formValue(form)));
  }
  function mayLeave() {
    const unsaved = [...editedForms].some(form => form.isConnected && (form.getAttribute('method') || 'get').toLowerCase() !== 'get'
      && formBaselines.has(form) && formBaselines.get(form) !== formValue(form));
    return !unsaved || window.confirm('You have unsaved changes. Leave this page without saving them?');
  }
  document.addEventListener('focusin', event => {
    const form = event.target.form;
    if (form && !formBaselines.has(form)) formBaselines.set(form, formValue(form));
  });
  for (const type of ['input', 'change']) document.addEventListener(type, event => {
    if (event.target.form) editedForms.add(event.target.form);
  });
  function replaceUrl(value) {
    const url = absolute(value);
    if (!url) return;
    history.replaceState(stateWithScroll(), '', url.href);
    if (!routing) renderedURL = url.href;
  }
  history.replaceState(stateWithScroll(), '', window.location.href);
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

  function acceptsURL(url) {
    return url && url.pathname !== '/' && !NATIVE_PATH.test(url.pathname) && !FILE_PATH.test(url.pathname)
      && !/(?:\/activate\/|\/download\/|\/export\/|\/print\/|\/preview(?:\/|$))/.test(url.pathname);
  }
  function canHandleLink(link, event) {
    if (!link || event?.defaultPrevented || (event && event.button !== 0)
        || event?.metaKey || event?.ctrlKey || event?.shiftKey || event?.altKey) return false;
    if (link.hasAttribute('download') || (link.target && link.target !== '_self')
        || link.closest('[data-native-navigation], [data-no-navigation], [data-no-loader]')) return false;
    const url = absolute(link.href);
    if (!acceptsURL(url)) return false;
    const current = new URL(window.location.href);
    return !(url.pathname === current.pathname && url.search === current.search
      && (url.hash || link.getAttribute('href')?.includes('#')));
  }
  function canHandleForm(form, event) {
    if (!form || event?.defaultPrevented || form.closest('[data-native-navigation], [data-no-navigation], [data-no-loader]')) return false;
    const submitter = event?.submitter;
    const method = submitter?.getAttribute('formmethod') || form.getAttribute('method') || 'get';
    const target = submitter?.getAttribute('formtarget') || form.getAttribute('target');
    const action = submitter?.getAttribute('formaction') || form.getAttribute('action') || window.location.href;
    return method.toLowerCase() === 'get' && (!target || target === '_self') && acceptsURL(absolute(action));
  }

  function updateActiveNavigation(url) {
    document.querySelectorAll('.patient-navigation, .console-nav, .staff-mobile-nav').forEach(nav => {
      let best = null, length = -1;
      nav.querySelectorAll('a[href]').forEach(link => {
        const candidate = absolute(link.href);
        if (!candidate) return;
        link.classList.remove('is-active'); link.removeAttribute('aria-current');
        const prefix = candidate.pathname.endsWith('/') ? candidate.pathname : `${candidate.pathname}/`;
        if ((url.pathname === candidate.pathname || url.pathname.startsWith(prefix)) && candidate.pathname.length > length) {
          best = link; length = candidate.pathname.length;
        }
      });
      if (best) { best.classList.add('is-active'); best.setAttribute('aria-current', 'page'); }
    });
  }

  function loading(title) {
    const content = region();
    if (!content) return;
    document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close?.());
    const section = document.createElement('section');
    section.className = 'page-navigation-loading'; section.dataset.navigationLoading = '';
    section.setAttribute('aria-busy', 'true');
    const heading = document.createElement('header'); heading.className = 'page-navigation-loading__heading';
    const h1 = document.createElement('h1'); h1.textContent = title || 'Loading';
    const label = document.createElement('p'); label.className = 'page-navigation-loading__label';
    label.setAttribute('role', 'status'); label.textContent = 'Loading your page…';
    heading.append(h1, label); section.append(heading);
    const grid = document.createElement('div'); grid.className = 'page-skeleton-grid'; grid.setAttribute('aria-hidden', 'true');
    for (let card = 0; card < 3; card += 1) {
      const box = document.createElement('div'); box.className = `page-skeleton-card${card === 2 ? ' page-skeleton-card--wide' : ''}`;
      for (let row = 0; row < 4; row += 1) {
        const line = document.createElement('span'); line.className = `page-skeleton-line${row === 0 ? ' page-skeleton-line--title' : ''}`; box.append(line);
      }
      grid.append(box);
    }
    section.append(grid); content.replaceChildren(section); content.classList.remove('is-page-loading');
    content.setAttribute('aria-busy', 'true');
    root()?.querySelectorAll('.workspace-page-loader').forEach(loader => loader.remove());
  }

  function errorPage(url, status = 0) {
    const content = region();
    if (!content) return;
    content.removeAttribute('aria-busy');
    const box = document.createElement('section'); box.className = 'page-navigation-error'; box.dataset.navigationError = '';
    box.setAttribute('role', 'alert');
    const h1 = document.createElement('h1');
    h1.textContent = status === 403 ? 'Access unavailable' : status === 404 ? 'Page not found' : 'We couldn’t load this page';
    const text = document.createElement('p');
    text.textContent = status === 403 ? 'Your account does not have access to this page.'
      : status === 404 ? 'This page is unavailable or its address has changed.' : 'Check your connection and try again.';
    box.append(h1, text);
    const actions = document.createElement('div'); actions.className = 'page-navigation-error__actions';
    if (![401, 403, 404].includes(status)) {
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'console-button console-button--primary';
      retry.dataset.navigationRetry = ''; retry.textContent = 'Try again';
      retry.addEventListener('click', () => navigate(url, { replace: true })); actions.append(retry);
    }
    const fallback = document.createElement('a'); const destination = new URL(url); destination.searchParams.set('__full', '1');
    fallback.href = destination.href; fallback.dataset.nativeNavigation = ''; fallback.textContent = 'Open full page'; actions.append(fallback);
    box.append(actions); content.replaceChildren(box); h1.tabIndex = -1; h1.focus({ preventScroll: true });
    // Header/menu behavior remains available while a destination is in an error state.
    window.MeridianPages?.mount(document);
    notify('workspace:navigation-error', { url: url.href, status });
  }

  function assetURL(value) {
    const url = absolute(typeof value === 'string' ? value : value?.url);
    return url && url.pathname.startsWith('/static/') ? url.href : null;
  }
  function loadStyles(styles = []) {
    return Promise.all(styles.map(value => {
      const href = assetURL(value);
      if (!href) throw new Error('Invalid page stylesheet');
      if (stylePromises.has(href)) return stylePromises.get(href);
      if ([...document.querySelectorAll('link[rel="stylesheet"]')].some(link => link.href === href)) return Promise.resolve();
      if (!stylePromises.has(href)) stylePromises.set(href, new Promise((resolve, reject) => {
        const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = href; link.dataset.navigationAsset = '';
        // An obsolete navigation must not activate a stylesheet after a newer page has rendered.
        link.media = 'not all';
        const failure = () => { window.clearTimeout(timer); stylePromises.delete(href); link.remove(); reject(new Error('Stylesheet unavailable')); };
        const timer = window.setTimeout(failure, REQUEST_TIMEOUT);
        link.onload = () => { window.clearTimeout(timer); resolve(); }; link.onerror = failure;
        document.head.append(link);
      }));
      return stylePromises.get(href);
    }));
  }
  function activateStyles(styles = []) {
    const wanted = styles.map(assetURL);
    document.querySelectorAll('link[rel="stylesheet"]').forEach(link => {
      if (assetURL(link.href) && !wanted.includes(link.href)) { link.media = 'not all'; }
    });
    // Preserve the server's CSS order, including the patient theme that deliberately comes last.
    for (const href of wanted) {
      const link = [...document.querySelectorAll('link[rel="stylesheet"]')].find(node => node.href === href);
      if (link) { link.media = ''; document.head.append(link); }
    }
  }
  async function loadScripts(scripts = [], current) {
    const jobs = [];
    for (const value of scripts) {
      if (current !== generation) return;
      const src = assetURL(value);
      if (!src) throw new Error('Invalid page script');
      if (loadedScripts.has(src)) continue;
      if (!scriptPromises.has(src)) scriptPromises.set(src, new Promise((resolve, reject) => {
        const script = document.createElement('script'); script.src = src; script.async = false;
        const failure = () => { window.clearTimeout(timer); scriptPromises.delete(src); script.remove(); reject(new Error('Page script unavailable')); };
        const timer = window.setTimeout(failure, REQUEST_TIMEOUT);
        script.onload = () => { window.clearTimeout(timer); loadedScripts.add(src); resolve(); };
        script.onerror = failure;
        document.head.append(script);
      }));
      jobs.push(scriptPromises.get(src));
    }
    // async=false retains execution order while the browser downloads page assets in parallel.
    await Promise.all(jobs);
  }
  function untilCancelled(promise, signal) {
    return new Promise((resolve, reject) => {
      const abort = () => reject(new DOMException('Navigation cancelled', 'AbortError'));
      promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
      if (signal.aborted) { abort(); return; }
      signal.addEventListener('abort', abort, { once: true });
    });
  }
  function replaceOutside(selector, html) {
    if (typeof html !== 'string') return;
    document.querySelectorAll(selector).forEach(node => node.remove());
    if (html) document.body.insertAdjacentHTML('beforeend', html);
  }

  function cancelSections() {
    sectionQueue = [];
    sections.forEach(controller => controller.abort()); sections.clear();
  }
  function loadDeferred(current, retryTarget = null) {
    if (current !== generation) return;
    const candidates = retryTarget ? [retryTarget] : [...root()?.querySelectorAll('[data-deferred-content][data-content-section]') || []];
    for (const target of candidates) {
      if (target.hasAttribute('data-deferred-loaded') || target.hasAttribute('data-deferred-pending')
          || (!retryTarget && target.hasAttribute('data-deferred-failed'))) continue;
      delete target.dataset.deferredFailed;
      target.dataset.deferredPending = '';
      sectionQueue.push({ target, current });
    }
    pumpSections();
  }
  function pumpSections() {
    while (sectionQueue.length && sections.size < 2) {
      const { target, current } = sectionQueue.shift();
      if (current !== generation || !target.isConnected) continue;
      (async () => {
        const url = absolute(target.dataset.contentUrl || window.location.href);
        if (!url) { delete target.dataset.deferredPending; return; }
        const controller = new AbortController(); sections.add(controller);
        const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
        target.setAttribute('aria-busy', 'true');
        try {
          const response = await fetch(url.href, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
            headers: { 'X-Meridian-Navigation': 'section', 'X-Meridian-Navigation-Scope': config.scope,
              'X-Meridian-Section': target.dataset.contentSection, 'Accept': 'application/json' } });
          if (current !== generation || !target.isConnected) return;
          if ([401, 409].includes(response.status)) { nativeVisit(window.location.href); return; }
          if (!response.ok) throw new Error('Section unavailable');
          const data = await response.json();
          if (current !== generation || !target.isConnected) return;
          if (data.scope !== config.scope || data.version !== 1 || typeof data.html !== 'string') throw new Error('Section context changed');
          if (data.redirect_url) { nativeVisit(data.redirect_url); return; }
          target.innerHTML = data.html; target.removeAttribute('aria-busy'); target.dataset.deferredLoaded = '';
        } catch (error) {
          if (current !== generation || !target.isConnected) return;
          target.removeAttribute('aria-busy');
          const text = document.createElement('p'); text.className = 'page-navigation-loading__label'; text.textContent = 'This section could not load.';
          const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'console-button console-button--quiet'; retry.textContent = 'Try again';
          target.dataset.deferredFailed = '';
          retry.addEventListener('click', () => loadDeferred(current, target), { once: true });
          target.replaceChildren(text, retry);
        } finally {
          window.clearTimeout(timeout); sections.delete(controller); delete target.dataset.deferredPending;
          if (current === generation) pumpSections();
        }
      })();
    }
  }

  async function navigate(value, options = {}) {
    const url = absolute(value);
    if (!acceptsURL(url)) { nativeVisit(value); return; }
    if (!options.initial && !mayLeave()) {
      if (options.pop) {
        const delta = historyPosition - options.position;
        if (Number.isInteger(delta) && delta !== 0) { restoringCancelledPop = true; history.go(delta); }
        else history.replaceState(stateWithScroll(), '', renderedURL);
      }
      return;
    }
    const current = ++generation;
    pending?.abort(); cancelSections();
    pending = new AbortController();
    const controller = pending;
    const oldScope = config.scope;
    routing = true;
    if (!options.pop) {
      history.replaceState(stateWithScroll(), '', window.location.href);
      if (!options.replace) historyPosition += 1;
      history[options.replace ? 'replaceState' : 'pushState']({ meridianNavigation: true, meridianPosition: historyPosition, scroll: [0, 0] }, '', url.href);
    } else if (Number.isInteger(options.position)) historyPosition = options.position;
    notify('workspace:navigating', { url: url.href });
    window.MeridianPages?.dispose();
    updateActiveNavigation(url); loading(options.title);
    document.title = `${options.title || 'Loading'} | Meridian`;
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
    try {
      const response = await fetch(url.href, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
        headers: { 'X-Meridian-Navigation': 'content', 'X-Meridian-Navigation-Scope': oldScope, 'Accept': 'application/json' } });
      if (current !== generation) return;
      if (!(response.headers.get('Content-Type') || '').includes('application/json')) { nativeVisit(url.href); return; }
      const data = await response.json();
      if (current !== generation) return;
      if (data.redirect_url) { nativeVisit(data.redirect_url); return; }
      if ([401, 409].includes(response.status)) { nativeVisit(url.href); return; }
      if (!response.ok) { errorPage(url, response.status); return; }
      if (data.version !== 1 || data.scope !== oldScope || data.layout !== config.layout
          || (config.build && data.build && data.build !== config.build)) { nativeVisit(url.href); return; }
      const canonical = absolute(data.canonical_url || url.href);
      if (!canonical || typeof data.html !== 'string') throw new Error('Invalid navigation response');
      canonical.hash = url.hash;
      await untilCancelled(loadStyles(data.assets?.styles || []), controller.signal);
      if (current !== generation) return;
      // Controllers register while suspended. Keep the loading layout in place until
      // controls can be initialized, so a newly typed draft cannot become its own baseline.
      await untilCancelled(loadScripts(data.assets?.scripts || [], current), controller.signal);
      if (current !== generation) return;
      activateStyles(data.assets?.styles || []);
      const fragment = new DOMParser().parseFromString(`<main id="navigation-fragment">${data.html}</main>`, 'text/html');
      const next = fragment.getElementById('navigation-fragment');
      if (!next || next.querySelector('base, meta[http-equiv], iframe[srcdoc]')) throw new Error('Invalid page content');
      // External page scripts are registered once via the declared asset list, never by innerHTML.
      next.querySelectorAll('script').forEach(script => {
        if (!['application/json', 'application/ld+json'].includes(script.type)) script.remove();
      });
      root().replaceChildren(...next.childNodes);
      root().removeAttribute('aria-busy');
      replaceOutside('[data-live-messages]', data.live_messages_html);
      replaceOutside('.toast-stack', data.toast_html);
      if (typeof data.body_class === 'string') document.body.className = data.body_class;
      config = { ...config, ...data, html: undefined, assets: undefined, shell: false };
      if (configElement) configElement.textContent = JSON.stringify({ version: 1, scope: config.scope, layout: config.layout,
        canonical_url: canonical.pathname + canonical.search, build: config.build, shell: false });
      history.replaceState({ meridianNavigation: true, meridianPosition: historyPosition, scroll: options.restoreScroll || [0, 0] }, '', canonical.href);
      renderedURL = canonical.href;
      document.title = data.title || 'Meridian';
      window.MeridianPages?.mount(document);
      rememberForms();
      if (options.restoreScroll) window.scrollTo(...options.restoreScroll);
      else if (canonical.hash) {
        let anchor;
        try { anchor = document.getElementById(decodeURIComponent(canonical.hash.slice(1))); } catch (_) { /* An invalid hash is ignored. */ }
        anchor?.scrollIntoView();
      } else window.scrollTo(0, 0);
      const heading = region()?.querySelector('h1');
      if (heading && !canonical.hash) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
      notify('workspace:navigated', { url: canonical.href });
      loadDeferred(current);
    } catch (error) {
      if (current === generation) errorPage(url);
    } finally {
      window.clearTimeout(timeout);
      if (current === generation) { pending = null; routing = false; }
    }
  }

  window.MeridianNavigation = { navigate, replaceUrl, canHandleLink, canHandleForm };
  window.addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (!canHandleLink(link, event)) return;
    event.preventDefault();
    const label = link.closest('nav') ? (link.querySelector('.patient-navigation__label, .console-nav__text') || link).textContent.trim().slice(0, 100) : '';
    navigate(link.href, { title: label });
  });
  window.addEventListener('submit', event => {
    const form = event.target;
    if (!canHandleForm(form, event)) return;
    event.preventDefault();
    const url = absolute(event.submitter?.getAttribute('formaction') || form.getAttribute('action') || window.location.href);
    const fields = new FormData(form);
    if (event.submitter?.name) fields.append(event.submitter.name, event.submitter.value);
    url.search = new URLSearchParams(fields).toString(); navigate(url.href);
  });
  window.addEventListener('popstate', event => {
    if (restoringCancelledPop) { restoringCancelledPop = false; return; }
    if (!routing && !root()?.querySelector('[data-navigation-loading], [data-navigation-error]')
        && new URL(renderedURL).pathname === location.pathname && new URL(renderedURL).search === location.search) {
      if (location.hash) {
        try { document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView(); } catch (_) { /* Invalid hashes are ignored. */ }
      } else window.scrollTo(...(event.state?.scroll || [0, 0]));
      renderedURL = window.location.href;
      return;
    }
    navigate(window.location.href, { pop: true, position: event.state?.meridianPosition, restoreScroll: event.state?.scroll || [0, 0] });
  });
  window.addEventListener('scroll', () => {
    if (routing || savingScroll) return;
    savingScroll = true;
    requestAnimationFrame(() => { if (!routing) history.replaceState(stateWithScroll(), '', location.href); savingScroll = false; });
  }, { passive: true });
  window.addEventListener('pagehide', () => { generation += 1; pending?.abort(); cancelSections(); window.MeridianPages?.dispose(); });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    // Recheck the current authorization instead of restoring cached clinical content.
    navigate(window.location.href, { replace: true, initial: true, restoreScroll: history.state?.scroll || [0, 0] });
  });
  rememberForms();
  if (config.shell || document.querySelector('[data-navigation-shell]')) navigate(window.location.href, { replace: true, initial: true });
  else loadDeferred(generation);
})();
