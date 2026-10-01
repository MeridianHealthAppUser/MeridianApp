/* Small conveniences on the patient pages. Every control still works without it. */
(() => {
  function openWeightLog(focus) {
    const log = document.getElementById('weight-log');
    if (!log) return false;
    log.open = true;
    log.scrollIntoView({ block: 'center', behavior: focus ? 'smooth' : 'auto' });
    const input = log.querySelector('input[name="weight_kg"]');
    if (focus && input) input.focus({ preventScroll: true });
    return true;
  }

  document.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-weight-log-open]');
    if (opener && openWeightLog(true)) {
      event.preventDefault();
      history.replaceState(null, '', '#weight-log');
      return;
    }
    const chip = event.target.closest('[data-subject-chip]');
    if (chip) {
      const subject = document.getElementById(chip.dataset.subjectTarget || 'thread_subject');
      if (!subject) return;
      subject.value = chip.dataset.subjectChip;
      document.querySelectorAll('[data-subject-chip]').forEach((other) => other.setAttribute('aria-pressed', String(other === chip)));
      subject.focus();
    }
  });

  function onLoad() {
    if (location.hash === '#weight-log') openWeightLog(false);
    // Subject suggestions only do something with this script, so they start hidden.
    document.querySelectorAll('[data-subject-chips]').forEach((row) => { row.hidden = false; });
    const compose = document.getElementById('new-thread');
    if (compose && new URLSearchParams(location.search).get('compose') === '1') {
      const subject = compose.querySelector('input[name="subject"]');
      const body = compose.querySelector('textarea[name="body"]');
      const target = subject && !subject.value ? subject : body;
      if (target) target.focus({ preventScroll: true });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', onLoad, { once: true });
  else onLoad();
})();
