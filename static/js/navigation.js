/* Keep the active item visible in scrollable navigation without moving the page. */
(() => {
  function revealCurrentNavigation() {
    document.querySelectorAll('[data-scroll-navigation]').forEach((navigation) => {
      if (!navigation.clientWidth || navigation.scrollWidth <= navigation.clientWidth + 1) return;
      const current = navigation.querySelector('[aria-current="page"], .is-active');
      if (!current) return;
      const navigationBounds = navigation.getBoundingClientRect();
      const currentBounds = current.getBoundingClientRect();
      navigation.scrollLeft += currentBounds.left - navigationBounds.left
        - (navigation.clientWidth - currentBounds.width) / 2;
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
