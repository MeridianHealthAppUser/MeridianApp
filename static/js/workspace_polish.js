/* Reveal the current desktop destination inside its own scrollable rail. */
(() => {
  function revealCurrentRail() {
    document.querySelectorAll('[data-rail-navigation]').forEach((rail) => {
      if (!rail.clientHeight || rail.scrollHeight <= rail.clientHeight + 1) return;
      const current = rail.querySelector('[aria-current="page"]');
      if (!current) return;
      const bounds = rail.getBoundingClientRect();
      const item = current.getBoundingClientRect();
      if (item.top < bounds.top || item.bottom > bounds.bottom) {
        rail.scrollTop += item.top - bounds.top - (rail.clientHeight - item.height) / 2;
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', revealCurrentRail, { once: true });
  } else { revealCurrentRail(); }
  window.addEventListener('pageshow', revealCurrentRail);
  let resizeFrame;
  window.addEventListener('resize', () => {
    window.cancelAnimationFrame(resizeFrame);
    resizeFrame = window.requestAnimationFrame(revealCurrentRail);
  });
})();
