// The questionnaire inside the practice's WordPress screening page (docs/WORDPRESS_EMBED.md). It tells WordPress
// how tall it is, so the frame grows with it instead of scrolling inside the page, and to scroll back to the top of
// the form after each step. Leaving for the patient portal moves the whole tab, never just the frame.
// Messages: { source: 'meridian-questionnaire', height | scrollTo | navigate }. WordPress's listener checks they come
// from the app's address; they only go to the sites allowed to frame these pages (QUESTIONNAIRE_FRAME_ANCESTORS).
(function () {
  var script = document.currentScript;
  var framed = window.parent !== window;
  var origins = ((script && script.getAttribute('data-parent-origins')) || '').split(' ').filter(Boolean);
  origins.push(window.location.origin);

  function send(message) {
    if (!framed) return;
    message.source = 'meridian-questionnaire';
    origins.forEach(function (origin) {
      try { window.parent.postMessage(message, origin); } catch (error) { /* Not a valid origin: skip it. */ }
    });
  }

  var lastHeight = 0;
  function reportHeight() {
    var height = Math.ceil(document.body.getBoundingClientRect().bottom + window.pageYOffset);
    if (height > 0 && height !== lastHeight) {
      lastHeight = height;
      send({ height: height });
    }
  }

  function offsetOf(element) {
    return Math.max(0, Math.round(element.getBoundingClientRect().top + window.pageYOffset));
  }

  // Arriving from another questionnaire step: back to the top of the form, or to the #section the step asked for.
  // The first load comes from WordPress itself, so the visitor stays where they are on that page.
  function scrollParent() {
    var target = null;
    if (window.location.hash) {
      try { target = document.getElementById(decodeURIComponent(window.location.hash.slice(1))); } catch (error) { target = null; }
    }
    var fromStep = false;
    try { fromStep = !!document.referrer && new URL(document.referrer).origin === window.location.origin; } catch (error) { fromStep = false; }
    if (target) {
      window.scrollTo(0, 0);
      send({ scrollTo: offsetOf(target) });
    } else if (fromStep) {
      send({ scrollTo: 0 });
    }
  }

  function leave() {
    var exit = document.querySelector('[data-questionnaire-exit]');
    if (!exit) return;
    if (framed) send({ navigate: exit.href });
    else window.location.replace(exit.href);
  }

  function start() {
    reportHeight();
    scrollParent();
    leave();
    if ('ResizeObserver' in window) new ResizeObserver(reportHeight).observe(document.body);
    window.addEventListener('load', reportHeight);
    window.addEventListener('resize', reportHeight);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
}());
