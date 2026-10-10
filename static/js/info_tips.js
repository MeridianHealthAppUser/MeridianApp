/* (i) tips: the note shows in a bubble while the mouse is over the (i), or the (i) has keyboard focus. A click or
   tap keeps it open until a click or tap elsewhere, or Escape. The bubble is placed against the window so a table
   or card that scrolls can't cut it off: above the (i), or below it when there's no room above. Without scripts it
   shows on hover and focus. */
(() => {
  const tips = [...document.querySelectorAll('[data-info-tip]')];
  if (!tips.length) return;
  document.documentElement.classList.add('info-tips-ready');
  const EDGE = 8;
  const GAP = 10;
  let current = null;

  function place(tip) {
    const button = tip.querySelector('.info-tip__button');
    const bubble = tip.querySelector('.info-tip__bubble');
    bubble.style.left = '0px';
    bubble.style.top = '0px';
    const anchor = button.getBoundingClientRect();
    const size = bubble.getBoundingClientRect();
    const middle = anchor.left + anchor.width / 2;
    const left = Math.max(EDGE, Math.min(middle - size.width / 2, window.innerWidth - size.width - EDGE));
    const below = anchor.top - size.height - GAP < EDGE;
    bubble.style.left = `${left}px`;
    bubble.style.top = `${below ? anchor.bottom + GAP : anchor.top - size.height - GAP}px`;
    bubble.style.setProperty('--info-tip-arrow', `${middle - left}px`);
    tip.classList.toggle('info-tip--below', below);
  }

  function show(tip, pinned = false) {
    if (current && current !== tip) hide(current);
    current = tip;
    tip.classList.add('is-open');
    if (pinned) tip.dataset.pinned = '';
    tip.querySelector('.info-tip__button').setAttribute('aria-expanded', 'true');
    place(tip);
  }

  function hide(tip) {
    tip.classList.remove('is-open', 'info-tip--below');
    delete tip.dataset.pinned;
    tip.querySelector('.info-tip__button').setAttribute('aria-expanded', 'false');
    if (current === tip) current = null;
  }

  tips.forEach((tip) => {
    const button = tip.querySelector('.info-tip__button');
    button.setAttribute('aria-expanded', 'false');
    tip.addEventListener('pointerenter', (event) => { if (event.pointerType === 'mouse') show(tip); });
    tip.addEventListener('pointerleave', (event) => {
      if (event.pointerType === 'mouse' && !('pinned' in tip.dataset)) hide(tip);
    });
    button.addEventListener('focus', () => { if (button.matches(':focus-visible')) show(tip); });
    button.addEventListener('blur', () => { if (!('pinned' in tip.dataset)) hide(tip); });
    button.addEventListener('click', (event) => {
      event.preventDefault();
      if ('pinned' in tip.dataset) hide(tip);
      else show(tip, true);
    });
  });

  document.addEventListener('click', (event) => {
    if (current && !current.contains(event.target)) hide(current);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && current) hide(current);
  });
  // Follow the (i) when the page, or the table it sits in, scrolls.
  window.addEventListener('scroll', () => { if (current) place(current); }, { capture: true, passive: true });
  window.addEventListener('resize', () => { if (current) place(current); });
})();
