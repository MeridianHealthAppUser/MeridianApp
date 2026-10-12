/* A From and an Until time ([data-time-range], two quarter-hour Time lists from care/widgets.py): Until waits for a
   From, then lists only the times after it, so From 08:00 starts Until at 08:15. An Until that's no longer after From
   is cleared. Saving still checks the end is after the start. */
(() => {
  document.querySelectorAll('[data-time-range]').forEach((range) => {
    const [start, end] = range.querySelectorAll('select.quarter-hour-time');
    if (!start || !end) return;
    const placeholder = end.options[0] && !end.options[0].value ? end.options[0].text : 'Time';
    const times = [...end.options].filter((option) => option.value).map((option) => option.value);

    function refresh() {
      const chosen = end.value;
      if (!start.value) {
        end.replaceChildren(new Option('Pick a start time', ''));
        end.disabled = true;
        return;
      }
      const later = times.filter((at) => at > start.value);
      end.replaceChildren(new Option(placeholder, ''), ...later.map((at) => new Option(at, at, false, at === chosen)));
      end.disabled = false;
    }

    start.addEventListener('change', refresh);
    refresh();
  });
})();
