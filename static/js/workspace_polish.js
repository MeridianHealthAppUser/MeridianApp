/* Remember navigation groups and reveal the current desktop destination. */
(() => {
  function initializeRailNavigation() {
    document.querySelectorAll('[data-rail-navigation] [data-navigation-group]').forEach((group) => {
      const storageKey = `meridian.staff-navigation.v1.${group.dataset.navigationGroup}`;
      let savedState;
      try { savedState = window.localStorage.getItem(storageKey); } catch (_) { /* Storage is optional. */ }
      group.open = Boolean(group.querySelector('[aria-current="page"]')) || savedState !== 'closed';
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
      const current = rail.querySelector('[aria-current="page"]');
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
