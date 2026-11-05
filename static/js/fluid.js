/* Apple-style motion on every workspace page, with static/css/fluid.css.

   Tabs: a thumb behind the current tab moves on springs, described as Apple does with a damping ratio (1 settles
   without overshoot) and a response (seconds). Its two edges are separate springs, the leading one quicker, so it
   stretches toward where it is going; a new choice mid-flight keeps the velocity it has. Every set of tabs is the
   same capsule (fluid.css), and its thumb can be dragged: it follows the pointer, resists past the ends and, let go,
   lands where its momentum carries it.
   Tab switches: app_navigation.js replaces the content with a skeleton while the next page loads. The header and tabs
   are put back above the skeleton, so the thumb keeps moving and only the content waits; the new content then slides
   in from the side of the chosen tab.
   Dialogs grow out of the control that opened them and go back into it. Buttons light up where they are pressed, and
   a form's submit button shows the activity indicator while the page posts. The side menu collapses to its icons.

   Nothing happens unless the page loads fluid.css. */
(() => {
  'use strict';
  if (window.MeridianFluid) return;
  window.MeridianFluid = true;

  const html = document.documentElement;
  // The page's stylesheet link, not its styles: app_navigation.js may be re-adding it to restore the order. In
  // production collectstatic names it with a content hash, fluid.1691610357ce.css (portal/test_fluid.py).
  const FLUID_CSS = /\/css\/fluid(?:\.[0-9a-f]{12})?\.css$/;
  const enabled = () => [...document.querySelectorAll('link[rel="stylesheet"]')]
    .some(link => link.media !== 'not all' && FLUID_CSS.test(new URL(link.href, window.location.href).pathname));
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const cssValue = name => getComputedStyle(html).getPropertyValue(name).trim();
  const easing = (name, fallback) => {
    const value = cssValue(name);
    return value && window.CSS?.supports?.('transition-timing-function', value) ? value : fallback;
  };
  const duration = (name, fallback) => parseFloat(cssValue(name)) || fallback;
  const region = () => document.querySelector('#workspace-content .console-content');

  const STEP = 1 / 240;
  class Spring {
    constructor(value = 0, parameters = { damping: 1, response: .35 }) {
      this.value = value; this.target = value; this.velocity = 0;
      this.configure(parameters);
    }
    configure({ damping, response }) {
      this.stiffness = (2 * Math.PI / response) ** 2;
      this.friction = 4 * Math.PI * damping / response;
    }
    // Advances by the frame's time in small fixed steps, so a long frame can't make it unstable.
    step(seconds) {
      for (let left = seconds; left > 0; left -= STEP) {
        const h = Math.min(STEP, left);
        this.velocity += (-this.stiffness * (this.value - this.target) - this.friction * this.velocity) * h;
        this.value += this.velocity * h;
      }
      const resting = Math.abs(this.velocity) < 2 && Math.abs(this.value - this.target) < .1;
      if (resting) { this.value = this.target; this.velocity = 0; }
      return !resting;
    }
    jump(value) { this.value = value; this.target = value; this.velocity = 0; }
  }

  // Runs springs on the display's frames until they all rest.
  function motion(springs, draw) {
    let frame = 0, last = 0;
    const tick = now => {
      const seconds = last ? Math.min((now - last) / 1000, 1 / 20) : 0;
      last = now;
      let moving = false;
      springs.forEach(spring => { moving = spring.step(seconds) || moving; });
      draw();
      frame = moving ? requestAnimationFrame(tick) : 0;
      if (!frame) last = 0;
    };
    return {
      start() { if (!frame) frame = requestAnimationFrame(tick); },
      stop() { cancelAnimationFrame(frame); frame = 0; last = 0; },
    };
  }

  // Where a flick would come to rest, as scrolling decelerates (Apple's projection); .99 suits a short control.
  const project = (velocity, rate = .99) => (velocity / 1000) * rate / (1 - rate);
  // Past an end, the thumb follows less the further it goes.
  const rubberband = (overshoot, dimension, constant = .55) => (overshoot * dimension * constant) / (dimension + constant * Math.abs(overshoot));

  /* Tab bars */
  const TAB_BARS = '.patient-workspace-tabs, .patient-order-subtabs, .ops-steps, .records-tabs, .staff-view-toggle, .record-chips';
  const LEAD = { damping: .82, response: .3 };
  const TRAIL = { damping: 1, response: .42 };
  const bars = new Map();
  const tabsOf = nav => [...nav.querySelectorAll('a[href]')].filter(link => !link.matches('.shipping-all-stages'));
  const currentTab = nav => tabsOf(nav).find(link => link.matches('.is-active, [aria-current="page"], [aria-selected="true"]'));
  const barKey = nav => `${[...nav.classList].filter(name => !name.startsWith('fluid-') && name !== 'is-dragging').join(' ')}|${nav.getAttribute('aria-label') || ''}`;
  const inCard = element => Boolean(element.closest('.staff-page-card'));

  function createBar(nav) {
    nav.classList.add('fluid-tabs');
    const thumb = document.createElement(nav.matches('ol, ul') ? 'li' : 'span');
    thumb.className = 'fluid-tab-thumb';
    thumb.setAttribute('aria-hidden', 'true');
    if (thumb.tagName === 'LI') thumb.setAttribute('role', 'presentation');
    nav.append(thumb);
    tabsOf(nav).forEach(link => { link.draggable = false; });

    const springs = { left: new Spring(), right: new Spring(), top: new Spring(), height: new Spring() };
    const draw = () => {
      thumb.style.transform = `translate3d(${springs.left.value}px, ${springs.top.value}px, 0)`;
      thumb.style.width = `${Math.max(0, springs.right.value - springs.left.value)}px`;
      thumb.style.height = `${springs.height.value}px`;
    };
    const run = motion(Object.values(springs), draw);
    // Layout positions (offset*), not on-screen boxes: a tab still springing back from a press is scaled.
    const measure = link => {
      let left = 0, top = 0;
      for (let node = link; node && node !== nav; node = node.offsetParent) { left += node.offsetLeft; top += node.offsetTop; }
      return { left, right: left + link.offsetWidth, top, height: link.offsetHeight };
    };
    const bar = {
      nav, springs, key: barKey(nav), target: null, suppressClick: false,
      moveTo(link, { instant = false } = {}) {
        if (!link) return;
        const to = measure(link);
        const forward = to.left >= springs.left.value;
        springs[forward ? 'right' : 'left'].configure(LEAD);
        springs[forward ? 'left' : 'right'].configure(TRAIL);
        springs.top.configure(TRAIL); springs.height.configure(TRAIL);
        Object.entries(to).forEach(([edge, value]) => {
          if (instant || reducedMotion.matches) springs[edge].jump(value);
          else springs[edge].target = value;
        });
        bar.target = link;
        if (instant || reducedMotion.matches) { run.stop(); draw(); } else run.start();
        // Tabs too wide for a narrow screen scroll; the chosen one is brought into view.
        if (nav.scrollWidth > nav.clientWidth && (to.left < nav.scrollLeft || to.right > nav.scrollLeft + nav.clientWidth)) {
          nav.scrollTo({ left: to.left - (nav.clientWidth - (to.right - to.left)) / 2, behavior: instant || reducedMotion.matches ? 'auto' : 'smooth' });
        }
      },
      draw, run, measure,
      stop() { run.stop(); },
      disconnect() { resize.disconnect(); changes.disconnect(); },
    };

    // Which edges have more tabs beyond them, for the fade (fluid.css).
    const edges = () => {
      const more = [nav.scrollLeft > 2 && 'start', nav.scrollLeft < nav.scrollWidth - nav.clientWidth - 2 && 'end'].filter(Boolean);
      if (more.length) nav.dataset.fluidMore = more.join(' ');
      else delete nav.dataset.fluidMore;
    };
    nav.addEventListener('scroll', edges, { passive: true });
    // A mouse wheel scrolls tabs that don't fit sideways, until they reach an end and the page takes over.
    nav.addEventListener('wheel', event => {
      if (event.ctrlKey || Math.abs(event.deltaY) <= Math.abs(event.deltaX) || nav.scrollWidth <= nav.clientWidth) return;
      const max = nav.scrollWidth - nav.clientWidth;
      if ((event.deltaY < 0 && nav.scrollLeft <= 0) || (event.deltaY > 0 && nav.scrollLeft >= max - 1)) return;
      event.preventDefault();
      nav.scrollLeft += event.deltaY;
    }, { passive: false });
    const resize = new ResizeObserver(() => {
      if (drag?.bar !== bar && bar.target?.isConnected) bar.moveTo(bar.target, { instant: true });
      nav.style.touchAction = nav.scrollWidth <= nav.clientWidth ? 'pan-y' : '';
      edges();
    });
    resize.observe(nav);
    tabsOf(nav).forEach(link => resize.observe(link));
    // A tab chosen in the page (Weekly shipping's stages) moves the thumb and slides its panel in.
    let shown = currentTab(nav);
    const changes = new MutationObserver(() => {
      const link = currentTab(nav);
      if (!link || link === shown) return;
      const tabs = tabsOf(nav);
      const direction = Math.sign(tabs.indexOf(link) - tabs.indexOf(shown));
      shown = link;
      if (link !== bar.target && drag?.bar !== bar) bar.moveTo(link);
      const panel = link.getAttribute('aria-controls') && document.getElementById(link.getAttribute('aria-controls'));
      if (panel && !panel.hidden) arrive([panel], direction);
    });
    changes.observe(nav, { attributes: true, attributeFilter: ['class', 'aria-selected', 'aria-current'], subtree: true });
    return bar;
  }

  function enhanceTabs() {
    const retired = new Map();
    bars.forEach((bar, nav) => {
      if (nav.isConnected) return;
      bar.disconnect(); bars.delete(nav); retired.set(bar.key, bar);
    });
    if (enabled()) {
      document.querySelectorAll(TAB_BARS).forEach(nav => {
        const link = currentTab(nav);
        if (bars.has(nav) || !link || tabsOf(nav).length < 2) return;
        const bar = createBar(nav);
        bars.set(nav, bar);
        // The bar this one replaces hands over where its thumb is and how fast it is going.
        const previous = retired.get(bar.key);
        if (previous && !reducedMotion.matches) {
          Object.entries(previous.springs).forEach(([edge, spring]) => {
            bar.springs[edge].value = spring.value; bar.springs[edge].velocity = spring.velocity;
          });
          bar.draw();
          bar.moveTo(link);
        } else bar.moveTo(link, { instant: true });
      });
    }
    retired.forEach(bar => bar.stop());
  }

  /* Dragging a thumb */
  let drag = null;
  function startDrag(event, bar, link) {
    const tabs = tabsOf(bar.nav);
    drag = { bar, link, pointerId: event.pointerId, startX: event.clientX, startLeft: bar.springs.left.value,
      width: bar.springs.right.value - bar.springs.left.value, history: [], dragging: false, tabs };
  }
  function moveDrag(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const { bar } = drag;
    const dx = event.clientX - drag.startX;
    // A little movement before committing, so a tap stays a tap.
    if (!drag.dragging) {
      if (Math.abs(dx) < 8) return;
      drag.dragging = true;
      bar.run.stop();
      bar.nav.classList.add('is-dragging');
      try { bar.nav.setPointerCapture(event.pointerId); } catch (_) { /* The pointer has already gone. */ }
    }
    const first = bar.measure(drag.tabs[0]);
    const last = bar.measure(drag.tabs[drag.tabs.length - 1]);
    const min = first.left, max = last.right - drag.width;
    let left = drag.startLeft + dx;
    if (left < min) left = min - rubberband(min - left, bar.nav.clientWidth);
    else if (left > max) left = max + rubberband(left - max, bar.nav.clientWidth);
    bar.springs.left.jump(left);
    bar.springs.right.jump(left + drag.width);
    bar.draw();
    const now = event.timeStamp;
    drag.history.push({ x: left, t: now });
    drag.history = drag.history.filter(sample => now - sample.t < 100);
  }
  function endDrag(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const { bar, history, tabs } = drag;
    const wasDragging = drag.dragging;
    drag = null;
    if (!wasDragging) return;
    bar.nav.classList.remove('is-dragging');
    const first = history[0], last = history[history.length - 1];
    const velocity = first && last && last.t > first.t ? (last.x - first.x) / ((last.t - first.t) / 1000) : 0;
    const width = bar.springs.right.value - bar.springs.left.value;
    const landing = bar.springs.left.value + project(event.type === 'pointercancel' ? 0 : velocity) + width / 2;
    const target = tabs.reduce((best, link) => {
      const box = bar.measure(link);
      const distance = Math.abs((box.left + box.right) / 2 - landing);
      return distance < best.distance ? { link, distance } : best;
    }, { link: bar.target, distance: Infinity }).link;
    // The release velocity carries straight into the springs, so there's no seam between dragging and settling.
    bar.springs.left.velocity = velocity; bar.springs.right.velocity = velocity;
    bar.moveTo(target);
    bar.suppressClick = true;
    window.setTimeout(() => {
      bar.suppressClick = false;
      if (target !== currentTab(bar.nav) && event.type !== 'pointercancel') target.click();
    });
  }

  /* Tab switches keep the header and tabs */
  let choice = null;
  let arriving = null;
  let lastNavigation = 0;

  // What stays in place for a tab: the tabs themselves, or for tabs in a card, the part of the card holding them.
  const tabAnchor = nav => {
    const card = nav.closest('.staff-page-card');
    return card ? [...card.children].find(child => child.contains(nav)) : nav;
  };

  // Kept while the next tab loads, tabs stay usable and everything else is inert.
  function freeze(node) {
    if (node.matches(TAB_BARS)) return;
    if (node.querySelector(TAB_BARS)) [...node.children].forEach(freeze);
    else node.inert = true;
  }

  function keepHeader(content, nav) {
    const anchor = tabAnchor(nav);
    const card = nav.closest('.staff-page-card');
    const top = anchor && [...content.children].find(child => child.contains(anchor));
    if (!top) return false;
    const kept = [...content.children].slice(0, [...content.children].indexOf(top) + 1);
    queueMicrotask(() => {
      const loading = content.querySelector(':scope > [data-navigation-loading]');
      if (!loading || !content.isConnected) return;
      for (let node = anchor; node && node !== content; node = node.parentElement) {
        while (node.nextElementSibling) node.nextElementSibling.remove();
      }
      kept.forEach(freeze);
      // Tabs in a card: the card stays, and only what is under its tabs waits, as a skeleton in the card.
      if (card) {
        const skeleton = Object.assign(document.createElement('div'), { className: 'fluid-card-skeleton' });
        skeleton.setAttribute('aria-hidden', 'true');
        for (let line = 0; line < 5; line += 1) skeleton.append(Object.assign(document.createElement('span'), { className: 'page-skeleton-line' }));
        card.append(skeleton);
        loading.classList.add('fluid-loading-in-card');
      }
      loading.before(...kept);
      loading.classList.add('fluid-loading-kept');
      if (arriving) arriving.kept = true;
    });
    return true;
  }

  document.addEventListener('workspace:navigating', event => {
    lastNavigation = performance.now();
    arriving = null;
    if (!enabled() || !event.detail?.url) return;
    const content = region();
    const recent = choice && performance.now() - choice.time < 800 && content?.contains(choice.nav) ? choice : null;
    choice = null;
    arriving = { direction: recent?.direction || 0, kept: false, scrollY: window.scrollY,
      inCard: Boolean(recent && inCard(recent.nav)), key: recent && barKey(recent.nav) };
    if (recent) keepHeader(content, recent.nav);
  });

  // New content arrives from the side of the tab chosen, or rises into place after any other visit.
  function arrive(blocks, direction = 0) {
    if (reducedMotion.matches) return;
    const spring = easing('--fluid-spring', 'cubic-bezier(.2, .8, .2, 1)');
    const time = duration('--fluid-medium', 515);
    blocks.filter(block => !block.matches('dialog, template, script') && block.getClientRects().length).forEach((block, index) => {
      block.animate([
        { opacity: 0, transform: `translate3d(${direction * 28}px, ${direction ? 0 : 10}px, 0)` },
        { opacity: 1, transform: 'none' },
      ], { duration: time, easing: spring, delay: Math.min(index, 4) * 45, fill: 'backwards' });
    });
  }

  document.addEventListener('workspace:navigated', () => {
    const entry = arriving;
    arriving = null;
    enhanceTabs();
    syncToggles();
    const content = region();
    if (!entry || !content || !enabled()) return;
    if (!entry.kept) { arrive([...content.children], entry.direction); return; }
    // Tabs in a card: the page stays where it was, and the card's content slides in under its tabs.
    const tabs = entry.inCard && [...content.querySelectorAll(TAB_BARS)].find(nav => barKey(nav) === entry.key);
    const header = tabs || [...content.querySelectorAll(TAB_BARS)].filter(nav => !inCard(nav)).pop();
    const anchor = tabs ? tabAnchor(tabs) : header;
    if (tabs) {
      window.scrollTo(window.scrollX, entry.scrollY);
      if (!document.activeElement || document.activeElement === document.body) currentTab(tabs)?.focus({ preventScroll: true });
    }
    const blocks = [];
    const stop = tabs ? tabs.closest('.staff-page-card') : content;
    for (let node = anchor; node && node !== stop; node = node.parentElement) {
      for (let next = node.nextElementSibling; next; next = next.nextElementSibling) blocks.push(next);
    }
    arrive(blocks, entry.direction);
  });

  /* Dialogs */
  let opener = null;
  const origins = new WeakMap();
  const closing = new WeakMap();

  // Where the dialog would be if it were still the control that opened it.
  function sourceTransform(dialog, source) {
    const box = dialog.getBoundingClientRect();
    const from = source?.isConnected && source.getClientRects().length ? source.getBoundingClientRect() : null;
    if (!from) return 'translate3d(0, 16px, 0) scale(.96)';
    const x = from.left + from.width / 2 - (box.left + box.width / 2);
    const y = from.top + from.height / 2 - (box.top + box.height / 2);
    return `translate3d(${x}px, ${y}px, 0) scale(${Math.min(.9, Math.max(.2, from.width / box.width))})`;
  }

  function stopClosing(dialog) {
    closing.get(dialog)?.forEach(animation => animation.cancel());
    closing.delete(dialog);
    dialog.inert = false;
  }

  function dismiss(dialog) {
    if (closing.has(dialog)) return;
    const quiet = reducedMotion.matches;
    const animations = [
      dialog.animate([{ opacity: 1, filter: 'blur(0)' }, { opacity: 0, filter: quiet ? 'blur(0)' : 'blur(4px)' }],
        { duration: quiet ? 150 : 220, delay: quiet ? 0 : 60, easing: 'ease-in', fill: 'forwards' }),
    ];
    if (!quiet) {
      animations.push(dialog.animate([{ transform: 'none' }, { transform: sourceTransform(dialog, origins.get(dialog)) }],
        { duration: duration('--fluid-fast', 295), easing: easing('--fluid-spring', 'ease-out'), fill: 'forwards' }));
    }
    try {
      animations.push(dialog.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 280, easing: 'ease-out', fill: 'forwards', pseudoElement: '::backdrop' }));
    } catch (_) { /* The browser can't animate the backdrop; it goes with the dialog. */ }
    closing.set(dialog, animations);
    dialog.inert = true;
    Promise.all(animations.map(animation => animation.finished)).then(() => {
      closing.delete(dialog);
      dialog.inert = false;
      dialog.close();
      animations.forEach(animation => animation.cancel());
    }, () => { /* Opened again before it finished closing. */ });
  }

  // dialogs.js fires dialog:open on the dialog just after showing it (it doesn't bubble, so this listens on the way down).
  document.addEventListener('dialog:open', event => {
    const dialog = event.target;
    if (!(dialog instanceof HTMLDialogElement) || !enabled()) return;
    stopClosing(dialog);
    const source = opener && performance.now() - opener.time < 1000 && opener.element.dataset.modalOpen === dialog.id ? opener.element : null;
    opener = null;
    origins.set(dialog, source);
    if (reducedMotion.matches) { dialog.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 150, easing: 'ease-out' }); return; }
    dialog.animate([{ transform: sourceTransform(dialog, source) }, { transform: 'none' }],
      { duration: duration('--fluid-bouncy', 555), easing: easing('--fluid-spring-bouncy', 'cubic-bezier(.2, .9, .3, 1.05)') });
    dialog.animate([{ opacity: 0, filter: 'blur(6px)' }, { opacity: 1, filter: 'blur(0)' }], { duration: 220, easing: 'ease-out' });
  }, true);

  // Escape closes through the same animation.
  document.addEventListener('cancel', event => {
    const dialog = event.target;
    if (!enabled() || !event.cancelable || !dialog.matches?.('dialog.app-dialog')) return;
    event.preventDefault();
    dismiss(dialog);
  }, true);

  /* Presses: dialogs, tabs and the glow under a finger */
  document.addEventListener('click', event => {
    if (!enabled() || event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    const target = event.target;
    const nav = target.closest?.(TAB_BARS);
    const bar = nav && bars.get(nav);
    if (bar?.suppressClick) { event.preventDefault(); event.stopImmediatePropagation(); return; }

    const dialog = target.closest?.('dialog.app-dialog[open]');
    if (dialog && dialog.matches(':modal') && (target === dialog || target.closest('[data-dialog-close]'))) {
      // Instead of dialogs.js closing it at once, it goes back where it came from first.
      event.preventDefault(); event.stopImmediatePropagation();
      dismiss(dialog);
      return;
    }

    const trigger = target.closest?.('[data-modal-open]');
    if (trigger) opener = { element: trigger, time: performance.now() };

    const link = target.closest?.('a[href]');
    if (!bar || !link || !tabsOf(nav).includes(link)) return;
    const tabs = tabsOf(nav);
    const time = performance.now();
    choice = { nav, link, time, direction: Math.sign(tabs.indexOf(link) - tabs.indexOf(bar.target || currentTab(nav))) };
    // The thumb sets off on the click, before the next page has been asked for.
    bar.moveTo(link);
    window.setTimeout(() => {
      // A visit that didn't happen (unsaved changes kept the page): back to the tab that is still current.
      if (event.defaultPrevented && lastNavigation < time && link.isConnected && currentTab(nav) !== link) bar.moveTo(currentTab(nav));
    });
  }, true);

  document.addEventListener('pointerdown', event => {
    if (!enabled() || event.button !== 0) return;
    const control = event.target.closest?.('.console-button, .filter-menu__toggle, .table-excel-download');
    if (control) {
      const box = control.getBoundingClientRect();
      control.style.setProperty('--fluid-x', `${event.clientX - box.left}px`);
      control.style.setProperty('--fluid-y', `${event.clientY - box.top}px`);
    }
    const link = event.target.closest?.('a[href]');
    const bar = link && bars.get(link.closest(TAB_BARS));
    if (bar && link === bar.target && !reducedMotion.matches
        && bar.nav.scrollWidth <= bar.nav.clientWidth) startDrag(event, bar, link);
  }, true);

  // A soft highlight on a set of tabs follows the pointer, as light moves across glass.
  let glare = 0;
  document.addEventListener('pointermove', event => {
    if (drag) { moveDrag(event); return; }
    if (event.pointerType !== 'mouse' || reducedMotion.matches) return;
    const nav = event.target.closest?.(TAB_BARS);
    if (!nav || !bars.has(nav)) return;
    cancelAnimationFrame(glare);
    glare = requestAnimationFrame(() => {
      const box = nav.getBoundingClientRect();
      nav.style.setProperty('--fluid-hx', `${event.clientX - box.left}px`);
      nav.style.setProperty('--fluid-hy', `${event.clientY - box.top}px`);
    });
  }, { passive: true });
  document.addEventListener('pointerup', endDrag, true);
  document.addEventListener('pointercancel', endDrag, true);

  /* The side menu. The toggle collapses it to its icons and back; the choice is kept for this browser and applied
     before the page is drawn (base.html). Collapsed, an icon shows its label beside it on hover, and a section's
     icon opens its pages beside it, on hover or press. */
  const wide = window.matchMedia('(min-width: 52.001rem)');
  const collapsed = () => html.dataset.sidebar === 'collapsed';
  const RAIL_ITEMS = '.console-sidebar a[href], .patient-navigation a[href]';
  const labelOf = element => (element.querySelector('.console-nav__text, .patient-navigation__label, .console-nav__label, :scope > span:not(.nav-badge)') || element).textContent.trim();

  function syncToggles() {
    document.querySelectorAll('[data-rail-toggle]').forEach(button => {
      button.setAttribute('aria-expanded', String(!collapsed()));
      button.setAttribute('aria-label', collapsed() ? 'Expand menu' : 'Collapse menu');
    });
  }
  function setCollapsed(value) {
    if (value) html.dataset.sidebar = 'collapsed';
    else delete html.dataset.sidebar;
    try { window.localStorage.setItem('meridian-sidebar', value ? 'collapsed' : 'expanded'); } catch (_) { /* Kept for this page only. */ }
    hideTip();
    closeFlyout();
    syncToggles();
  }

  const popoverHost = element => element.closest('.meridian-console');
  const canPop = typeof HTMLElement.prototype.showPopover === 'function';
  function place(popover, beside) {
    const rail = beside.closest('.console-sidebar, .patient-navigation') || beside;
    const box = beside.getBoundingClientRect();
    const left = Math.max(box.right, rail.getBoundingClientRect().right) + 10;
    const top = Math.min(Math.max(8, box.top + box.height / 2 - popover.offsetHeight / 2), window.innerHeight - popover.offsetHeight - 8);
    Object.assign(popover.style, { left: `${left}px`, top: `${top}px` });
  }

  let tip = null, tipTarget = null, tipTimer = 0, tipHiddenAt = 0;
  function showTip(target) {
    const host = popoverHost(target);
    if (!host || !canPop) return;
    if (!tip?.isConnected || tip.parentElement !== host) {
      tip?.remove();
      tip = Object.assign(document.createElement('div'), { className: 'fluid-rail-tip', popover: 'manual' });
      tip.setAttribute('aria-hidden', 'true');
      host.append(tip);
    }
    tip.textContent = target.getAttribute('aria-label') && target.matches('[data-rail-toggle]') ? target.getAttribute('aria-label') : labelOf(target);
    if (!tip.matches(':popover-open')) tip.showPopover();
    place(tip, target);
  }
  function hideTip() {
    window.clearTimeout(tipTimer);
    if (tip?.matches(':popover-open')) { tip.hidePopover(); tipHiddenAt = performance.now(); }
    tipTarget = null;
  }

  let flyout = null, flyoutGroup = null, flyoutTimer = 0, returnFocus = false;
  function closeFlyout() {
    window.clearTimeout(flyoutTimer);
    if (flyout?.matches(':popover-open')) flyout.hidePopover();
  }
  function openFlyout(group, { focus = false } = {}) {
    const host = popoverHost(group);
    const summary = group.querySelector('summary');
    if (!host || !summary || !canPop) return;
    window.clearTimeout(flyoutTimer);
    hideTip();
    if (!flyout?.isConnected || flyout.parentElement !== host) {
      flyout?.remove();
      flyout = Object.assign(document.createElement('div'), { className: 'fluid-rail-menu', popover: 'auto' });
      flyout.setAttribute('role', 'group');
      flyout.addEventListener('beforetoggle', event => {
        if (event.newState === 'closed') returnFocus = flyout.contains(document.activeElement);
      });
      flyout.addEventListener('toggle', event => {
        if (event.newState !== 'closed') return;
        const left = flyoutGroup;
        flyoutGroup?.removeAttribute('data-fluid-flyout');
        flyoutGroup = null;
        if (returnFocus) left?.querySelector('summary')?.focus();
      });
      flyout.addEventListener('click', event => { if (event.target.closest('a[href]')) { returnFocus = false; closeFlyout(); } });
      flyout.addEventListener('keydown', event => {
        if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
        const links = [...flyout.querySelectorAll('a[href]')];
        const index = links.indexOf(document.activeElement);
        links[(index + (event.key === 'ArrowDown' ? 1 : links.length - 1)) % links.length]?.focus();
        event.preventDefault();
      });
      host.append(flyout);
    }
    if (flyoutGroup !== group) {
      flyoutGroup?.removeAttribute('data-fluid-flyout');
      const title = labelOf(summary);
      const heading = Object.assign(document.createElement('p'), { className: 'fluid-rail-menu__title', textContent: title });
      heading.setAttribute('aria-hidden', 'true');
      flyout.setAttribute('aria-label', title);
      flyout.replaceChildren(heading, ...[...group.querySelectorAll('.console-nav__links a[href]')].map(link => link.cloneNode(true)));
      flyoutGroup = group;
      group.dataset.fluidFlyout = '';
    }
    if (!flyout.matches(':popover-open')) flyout.showPopover();
    place(flyout, summary);
    if (focus) flyout.querySelector('a[href]')?.focus();
  }

  document.addEventListener('click', event => {
    if (!enabled()) return;
    if (event.target.closest?.('[data-rail-toggle]')) { setCollapsed(!collapsed()); return; }
    const summary = collapsed() && wide.matches && event.target.closest?.('.console-sidebar .console-nav__toggle');
    if (!summary) return;
    // Collapsed, a section stays as it is; its pages open beside it instead.
    event.preventDefault();
    const group = summary.closest('.console-nav__group');
    if (flyoutGroup === group && flyout?.matches(':popover-open') && event.detail) closeFlyout();
    else openFlyout(group, { focus: event.detail === 0 });
  });

  document.addEventListener('pointerover', event => {
    if (event.pointerType !== 'mouse' || !wide.matches || !enabled()) return;
    const inFlyout = flyout?.contains(event.target);
    const group = collapsed() && !inFlyout && event.target.closest?.('.console-sidebar .console-nav__group');
    if (inFlyout || (group && group === flyoutGroup)) window.clearTimeout(flyoutTimer);
    else if (group) { window.clearTimeout(flyoutTimer); flyoutTimer = window.setTimeout(() => openFlyout(group), 140); }

    const target = event.target.closest?.('[data-rail-toggle]') || (collapsed() && event.target.closest?.(RAIL_ITEMS));
    if (!target || target === tipTarget) return;
    tipTarget = target;
    window.clearTimeout(tipTimer);
    // Once one label is showing, the next appears at once, as you run down the icons.
    const warm = tip?.matches(':popover-open') || performance.now() - tipHiddenAt < 400;
    tipTimer = window.setTimeout(() => { if (tipTarget === target) showTip(target); }, warm ? 0 : 450);
  });
  document.addEventListener('pointerout', event => {
    if (event.pointerType !== 'mouse') return;
    const to = event.relatedTarget;
    if (tipTarget && !tipTarget.contains(to)) hideTip();
    // Leaving a section (or its open pages) for anywhere else: a section about to open doesn't, an open one closes.
    const from = event.target.closest?.('.console-sidebar .console-nav__group') || (flyout?.contains(event.target) ? flyoutGroup : null);
    if (!from || from.contains(to) || flyout?.contains(to)) return;
    window.clearTimeout(flyoutTimer);
    if (flyoutGroup) flyoutTimer = window.setTimeout(closeFlyout, 260);
  });
  wide.addEventListener?.('change', () => { hideTip(); closeFlyout(); });

  /* Submitting: the button that sent a form waits with the activity indicator until the next page arrives. */
  function settle(button) { delete button.dataset.fluidBusy; button.removeAttribute('aria-busy'); }
  window.addEventListener('submit', event => {
    const form = event.target;
    const button = event.submitter;
    if (!enabled() || !button?.matches?.('.console-button') || (form.getAttribute('method') || 'get').toLowerCase() !== 'post'
        || (form.target && form.target !== '_self')) return;
    // After every other listener: a form a script sends itself is left alone.
    window.setTimeout(() => {
      if (event.defaultPrevented) return;
      button.dataset.fluidBusy = '';
      button.setAttribute('aria-busy', 'true');
      window.setTimeout(() => settle(button), 10000);
    });
  });
  window.addEventListener('pageshow', () => document.querySelectorAll('[data-fluid-busy]').forEach(settle));

  const start = () => { enhanceTabs(); syncToggles(); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
  document.fonts?.ready.then(() => bars.forEach(bar => bar.target?.isConnected && bar.moveTo(bar.target, { instant: true })));
})();
