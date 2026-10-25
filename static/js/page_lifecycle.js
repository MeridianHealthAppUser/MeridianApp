/* A controller owns its listeners, timers and background reads until the next page.
   The navigation controller disposes before replacing content, then mounts the new page.
   There are no global EventTarget/fetch patches: ownership is explicit at each call site. */
(() => {
  'use strict';
  if (window.MeridianPages) return;
  const controllers = new Map();
  const mounted = new Map();
  let enabled = true;
  let root = document;

  function createScope() {
    let active = true;
    const cleanups = [];
    const timers = new Set();
    const intervals = new Set();
    const frames = new Set();
    const requests = new Set();
    const cancelled = () => new DOMException('This page is no longer active', 'AbortError');
    const check = () => { if (!active) throw cancelled(); };
    const scope = {
      root,
      get isActive() { return active; },
      cleanup(callback) { if (active) cleanups.push(callback); else callback(); },
      on(target, type, listener, options) {
        if (!target || !active) return;
        const guarded = function (...args) {
          if (!active) return;
          return typeof listener === 'function' ? listener.apply(this, args) : listener.handleEvent(...args);
        };
        target.addEventListener(type, guarded, options);
        cleanups.push(() => target.removeEventListener(type, guarded, options));
      },
      timeout(callback, delay, ...args) {
        if (!active) return 0;
        const id = window.setTimeout(() => { timers.delete(id); if (active) callback(...args); }, delay);
        timers.add(id);
        return id;
      },
      interval(callback, delay, ...args) {
        if (!active) return 0;
        const id = window.setInterval(() => { if (active) callback(...args); }, delay);
        intervals.add(id);
        return id;
      },
      raf(callback) {
        if (!active) return 0;
        const id = window.requestAnimationFrame(time => { frames.delete(id); if (active) callback(time); });
        frames.add(id);
        return id;
      },
      async fetch(input, options = {}) {
        check();
        // Once submitted, mutations must finish. Controllers decide how to handle
        // their confirmation; leaving a page must never retry or undo a POST.
        const method = String(options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
        if (!['GET', 'HEAD'].includes(method)) return window.fetch(input, options);
        const controller = new AbortController();
        const previous = options.signal || (input instanceof Request ? input.signal : null);
        const abort = () => controller.abort();
        if (previous?.aborted) controller.abort();
        else previous?.addEventListener('abort', abort, { once: true });
        const release = () => { requests.delete(controller); previous?.removeEventListener('abort', abort); };
        requests.add(controller);
        try {
          const response = await window.fetch(input, { ...options, signal: controller.signal });
          check();
          // Cancellation remains active through the response body, not only headers.
          for (const name of ['text', 'json', 'blob', 'arrayBuffer', 'formData']) {
            const read = response[name].bind(response);
            Object.defineProperty(response, name, { configurable: true, value: async (...args) => {
              try { check(); const result = await read(...args); check(); return result; }
              finally { release(); }
            } });
          }
          return response;
        } catch (error) { release(); throw error; }
      },
      dispose() {
        if (!active) return;
        active = false;
        requests.forEach(controller => controller.abort());
        requests.clear();
        timers.forEach(id => window.clearTimeout(id));
        intervals.forEach(id => window.clearInterval(id));
        frames.forEach(id => window.cancelAnimationFrame(id));
        for (const cleanup of cleanups.reverse()) {
          try { cleanup(); } catch (error) { console.error('Page cleanup failed', error); }
        }
        cleanups.length = 0;
      },
    };
    return scope;
  }

  function mountOne(name, initialize) {
    if (!enabled || mounted.has(name)) return;
    const scope = createScope();
    mounted.set(name, scope);
    try {
      const cleanup = initialize(scope);
      if (typeof cleanup === 'function') scope.cleanup(cleanup);
    } catch (error) {
      scope.dispose();
      mounted.delete(name);
      console.error(`Unable to initialize ${name}`, error);
    }
  }

  window.MeridianPages = {
    register(name, initialize) {
      if (controllers.has(name)) return;
      controllers.set(name, initialize);
      if (document.readyState !== 'loading') mountOne(name, initialize);
    },
    mount(nextRoot = document) {
      root = nextRoot;
      enabled = true;
      controllers.forEach((initialize, name) => mountOne(name, initialize));
    },
    dispose() {
      enabled = false;
      mounted.forEach(scope => scope.dispose());
      mounted.clear();
    },
  };
  document.addEventListener('DOMContentLoaded', () => {
    if (enabled) window.MeridianPages.mount();
  }, { once: true });
})();
