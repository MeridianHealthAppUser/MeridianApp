(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('patient_portal', (lifecycle) => {
  const replaceUrl = value => window.MeridianNavigation?.replaceUrl ? window.MeridianNavigation.replaceUrl(value) : window.history.replaceState(window.history.state, '', value);
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
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

  // The reply box in a conversation grows as you type, up to about eight lines; on a phone it starts at two
  // (patient_mobile.css) so the newest messages and Send fit on the screen. Clearing it after sending shrinks it back.
  listen(document, 'input', (event) => {
    const box = event.target.closest?.('.patient-conversation-reply textarea');
    if (!box) return;
    box.style.height = '';
    if (box.scrollHeight > box.clientHeight) box.style.height = `${Math.min(box.scrollHeight + box.offsetHeight - box.clientHeight, 192)}px`;
  });

  // Weight charts (patient_weight_chart.html): a tap, drag or hover shows the nearest check-in's weight and date above
  // its dot; with the chart focused, the arrow keys step through them. Tapping elsewhere or Escape puts it away.
  const chartReading = new WeakMap();
  const chartPoints = plot => [...plot.querySelectorAll('[data-weight-point]')];
  function showReading(plot, point) {
    const callout = plot.querySelector('[data-weight-chart-callout]');
    const marks = plot.querySelectorAll('[data-weight-chart-ring], [data-weight-chart-guide]');
    if (!callout || point === chartReading.get(plot)) return;
    chartReading.set(plot, point);
    if (!point) {
      callout.hidden = true;
      marks.forEach(mark => mark.setAttribute('visibility', 'hidden'));
      return;
    }
    const [cx, cy] = [point.getAttribute('cx'), point.getAttribute('cy')];
    marks.forEach((mark) => {
      if (mark.matches('circle')) { mark.setAttribute('cx', cx); mark.setAttribute('cy', cy); } else { mark.setAttribute('x1', cx); mark.setAttribute('x2', cx); }
      mark.setAttribute('visibility', 'visible');
    });
    callout.textContent = `${point.dataset.weight} kg · ${point.dataset.date}`;
    callout.hidden = false;
    const box = plot.getBoundingClientRect();
    const dot = point.getBoundingClientRect();
    const half = callout.offsetWidth / 2;
    callout.style.left = `${Math.min(Math.max(dot.left + dot.width / 2 - box.left, half), box.width - half)}px`;
    callout.style.top = `${dot.top + dot.height / 2 - box.top}px`;
    // Below the dot when there's no room above it in the chart.
    callout.classList.toggle('is-below', dot.top - box.top < callout.offsetHeight + 12);
  }
  const nearestPoint = (plot, clientX) => chartPoints(plot).reduce((best, point) => {
    const box = point.getBoundingClientRect();
    const distance = Math.abs(box.left + box.width / 2 - clientX);
    return distance < best.distance ? { point, distance } : best;
  }, { point: null, distance: Infinity }).point;
  listen(document, 'pointerdown', (event) => {
    const plot = event.target.closest?.('[data-weight-chart]');
    document.querySelectorAll('[data-weight-chart]').forEach((other) => { if (other !== plot) showReading(other, null); });
    if (plot) showReading(plot, nearestPoint(plot, event.clientX));
  });
  listen(document, 'pointermove', (event) => {
    const plot = event.target.closest?.('[data-weight-chart]');
    if (plot && (event.pointerType === 'mouse' || event.buttons)) showReading(plot, nearestPoint(plot, event.clientX));
  });
  listen(document, 'pointerout', (event) => {
    const plot = event.target.closest?.('[data-weight-chart]');
    if (plot && event.pointerType === 'mouse' && !plot.contains(event.relatedTarget) && document.activeElement !== plot) showReading(plot, null);
  });
  listen(document, 'focusout', (event) => {
    const plot = event.target.closest?.('[data-weight-chart]');
    if (plot && !plot.contains(event.relatedTarget)) showReading(plot, null);
  });
  listen(document, 'keydown', (event) => {
    const plot = event.target.closest?.('[data-weight-chart]');
    const points = plot ? chartPoints(plot) : [];
    if (!points.length || !['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(event.key)) return;
    if (event.key === 'Escape') { showReading(plot, null); return; }
    event.preventDefault();
    const at = points.indexOf(chartReading.get(plot));
    const step = event.key === 'ArrowRight' ? 1 : -1;
    const next = event.key === 'Home' ? 0 : event.key === 'End' || at < 0 ? points.length - 1 : at + step;
    showReading(plot, points[Math.max(0, Math.min(points.length - 1, next))]);
  });

  listen(document, 'click', (event) => {
    const opener = event.target.closest('[data-weight-log-open]');
    if (opener && openWeightLog(true)) {
      event.preventDefault();
      replaceUrl('#weight-log');
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
  if (document.readyState === 'loading') listen(document, 'DOMContentLoaded', onLoad, { once: true });
  else onLoad();
})();

});
