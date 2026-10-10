/* Booking an appointment (templates/portal/includes/appointment_booking_fields.html): Time waits for a date and a
   clinician, then lists that clinician's free quarter hours on the date for the chosen length. If the list can't be
   loaded, every quarter hour comes back and booking checks the time when saved. */
(() => {
  document.querySelectorAll('[data-booking-form]').forEach((fields) => {
    const control = (name) => fields.querySelector(`[name="${name}"]`);
    const clinician = control('clinician');
    const date = control('date');
    const duration = control('duration_minutes');
    const patient = control('patient');
    const time = control('time');
    if (!clinician || !date || !time || !window.fetch) return;
    const everyQuarterHour = [...time.options].map((option) => new Option(option.text, option.value));
    let chosen = time.value;
    let pending = null;

    function waiting(text) {
      time.replaceChildren(new Option(text, ''));
      time.disabled = true;
    }

    async function refresh() {
      if (!clinician.value || !date.value) {
        waiting('Pick date & clinician');
        return;
      }
      waiting('Finding free times…');
      if (pending) pending.abort();
      pending = new AbortController();
      const query = new URLSearchParams({ clinician: clinician.value, date: date.value, duration: duration ? duration.value : '15' });
      if (patient && patient.value) query.set('patient', patient.value);
      try {
        const response = await fetch(`${fields.dataset.timesUrl}?${query}`, { credentials: 'same-origin', signal: pending.signal });
        if (!response.ok) throw new Error('No times');
        const { times } = await response.json();
        if (!times.length) {
          waiting('No free times');
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
    [clinician, date, duration, patient].filter(Boolean).forEach((input) => input.addEventListener('change', refresh));
    refresh();
  });
})();
