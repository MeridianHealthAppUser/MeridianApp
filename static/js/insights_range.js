(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('insights_range', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
/* Insights' period (templates/portal/insights.html, Nov 2026): choosing a quick range shows it straight away, with no
   button to press. Custom opens the dates' calendar (static/js/date_picker.js); picking a range there shows it as
   Custom, and closing the calendar without one puts the quick range back as it was. Without scripts, a Show button
   sends the form. */
(() => {
  const form = document.querySelector('[data-insights-range]');
  const preset = form?.elements.range;
  const dates = form?.querySelector('[data-date-range]');
  if (!preset || !dates) return;
  const shown = preset.value;

  function send() {
    if (form.requestSubmit) form.requestSubmit();
    else form.submit();
  }

  listen(preset, 'change', () => {
    if (preset.value === 'custom') {
      // Once the dropdown has closed and given focus back.
      window.setTimeout(() => dates.click());
      return;
    }
    // A quick range is worked out from today on the server; the dates on show would only be ignored.
    form.elements.start.disabled = true;
    form.elements.end.disabled = true;
    send();
  });
  listen(dates, 'change', () => {
    preset.value = 'custom';
    send();
  });
  listen(dates, 'date-range:close', (event) => {
    if (!event.detail?.committed && preset.value === 'custom' && shown !== 'custom') preset.value = shown;
  });
  // Back to the page from the browser's history: the form as the page shows it, not as it was left.
  listen(window, 'pageshow', (event) => {
    if (!event.persisted) return;
    preset.value = shown;
    form.elements.start.disabled = false;
    form.elements.end.disabled = false;
  });
})();

});
