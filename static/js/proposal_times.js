/* Suggesting a different appointment time (templates/portal/includes/appointment_proposals.html): Time waits for a
   date, then lists the chosen appointment's clinician's free quarter hours that day (their working hours, not on
   leave, clear of other bookings). If the list can't be loaded, every quarter hour comes back and sending the
   suggestion checks the time. The form can be on a page more than once; each is set up once. */
(() => {
  document.querySelectorAll('[data-proposal-times-url]:not([data-proposal-times-ready])').forEach((form) => {
    form.dataset.proposalTimesReady = '';
    const appointment = form.querySelector('[name="appointment"]');
    const date = form.querySelector('[name="proposed_starts_at_0"]');
    const time = form.querySelector('[name="proposed_starts_at_1"]');
    if (!appointment || !date || !time || !window.fetch) return;
    const everyQuarterHour = [...time.options].map((option) => new Option(option.text, option.value));
    let chosen = time.value;
    let pending = null;

    function waiting(text) {
      time.replaceChildren(new Option(text, ''));
      time.disabled = true;
    }

    async function refresh() {
      if (!appointment.value || !date.value) {
        waiting(appointment.value ? 'Pick a date' : 'Pick an appointment & date');
        return;
      }
      waiting('Finding free times…');
      if (pending) pending.abort();
      pending = new AbortController();
      const query = new URLSearchParams({ appointment: appointment.value, date: date.value });
      try {
        const response = await fetch(`${form.dataset.proposalTimesUrl}?${query}`, { credentials: 'same-origin', signal: pending.signal });
        if (!response.ok) throw new Error('No times');
        const { times } = await response.json();
        if (!times.length) {
          waiting('No free times that day');
          return;
        }
        time.replaceChildren(new Option('Choose a time', ''), ...times.map((at) => new Option(at, at, false, at === chosen)));
        time.disabled = false;
      } catch (error) {
        if (error.name === 'AbortError') return;
        time.replaceChildren(...everyQuarterHour.map((option) => new Option(option.text, option.value, false, option.value === chosen)));
        time.disabled = false;
      }
    }

    time.addEventListener('change', () => { chosen = time.value; });
    [appointment, date].forEach((input) => input.addEventListener('change', refresh));
    refresh();
  });
})();
