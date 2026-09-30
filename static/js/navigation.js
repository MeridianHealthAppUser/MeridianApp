/* Keep the active item visible in scrollable navigation without moving the page. */
(() => {
  function revealCurrentNavigation() {
    document.querySelectorAll('[data-scroll-navigation]').forEach((navigation) => {
      if (!navigation.clientWidth || !navigation.clientHeight) return;
      const current = navigation.querySelector('[aria-current="page"], .is-active');
      if (!current) return;
      const navigationBounds = navigation.getBoundingClientRect();
      const currentBounds = current.getBoundingClientRect();
      if (navigation.scrollWidth > navigation.clientWidth + 1 &&
          (currentBounds.left < navigationBounds.left || currentBounds.right > navigationBounds.right)) {
        navigation.scrollLeft += currentBounds.left - navigationBounds.left
          - (navigation.clientWidth - currentBounds.width) / 2;
      }
      if (navigation.scrollHeight > navigation.clientHeight + 1 &&
          (currentBounds.top < navigationBounds.top || currentBounds.bottom > navigationBounds.bottom)) {
        navigation.scrollTop += currentBounds.top - navigationBounds.top
          - (navigation.clientHeight - currentBounds.height) / 2;
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', revealCurrentNavigation, { once: true });
  } else {
    revealCurrentNavigation();
  }
  window.addEventListener('pageshow', revealCurrentNavigation);
  let resizeFrame;
  window.addEventListener('resize', () => {
    window.cancelAnimationFrame(resizeFrame);
    resizeFrame = window.requestAnimationFrame(revealCurrentNavigation);
  });
})();
