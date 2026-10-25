(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('appointment_booking', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
  const fetch = lifecycle ? lifecycle.fetch : (...args) => window.fetch(...args);
/* Booking an appointment (templates/portal/includes/appointment_booking_fields.html), and moving one: Time is a grid
   of the clinician's day in 15-minute slots (static/css/time_slots.css, as Available time shows it). It waits for a
   date and a clinician, then shows every slot in their hours: the ones a booking of the chosen length can start in
   are free to choose, and the rest say why not. The Time list stays, hidden, as what the form sends. If the day
   can't be loaded, the list comes back with every quarter hour and booking checks the time when saved. Fields in a
   closed dialog, such as each booking's Edit appointment in Day view, wait until it opens. A free slot in Available
   time ([data-book-slot]) opens Create appointment with its clinician, date and time chosen. The form's button
   waits until everything it needs is chosen, the time included. */
(() => {
  document.querySelectorAll('[data-booking-form]').forEach((fields) => {
    const control = (name) => fields.querySelector(`[name="${name}"]`);
    const clinician = control('clinician');
    const date = control('date');
    const duration = control('duration_minutes');
    const patient = control('patient');
    const time = control('time');
    const appointmentType = control('appointment_type');
    if (!clinician || !date || !time || !window.fetch) return;
    const submit = fields.closest('form')?.querySelector('button[type="submit"]');
    const everyQuarterHour = [...time.options].map((option) => new Option(option.text, option.value));
    const originallyDisabled = time.disabled;
    let chosen = time.value;
    let pending = null;

    // The grid takes the Time field's place, with its label and any error from the last try.
    const field = time.closest('.console-form__field') || time.parentElement;
    const originallyHidden = field.hidden;
    const picker = document.createElement('div');
    picker.className = 'slot-picker';
    picker.setAttribute('role', 'group');
    const heading = document.createElement('p');
    heading.className = 'slot-picker__label';
    heading.id = `${time.id || 'time'}-slots`;
    heading.textContent = field.querySelector('label')?.textContent.trim() || 'Time';
    picker.setAttribute('aria-labelledby', heading.id);
    const status = document.createElement('p');
    status.className = 'slot-picker__status';
    status.setAttribute('aria-live', 'polite');
    const grid = document.createElement('ol');
    grid.className = 'slot-picker__slots';
    picker.append(heading, status, grid, ...field.querySelectorAll('.errorlist'));
    field.after(picker);
    field.hidden = true;
    lifecycle?.cleanup(() => {
      pending?.abort();
      const selected = time.value || chosen;
      time.replaceChildren(...everyQuarterHour.map(option => new Option(option.text, option.value, false, option.value === selected)));
      time.disabled = originallyDisabled;
      field.append(...picker.querySelectorAll('.errorlist'));
      field.hidden = originallyHidden;
      picker.remove();
    });

    function ready() {
      if (submit) submit.disabled = [patient, clinician, appointmentType, date, duration, time].some((input) => input && (!input.value || input.disabled));
    }

    function waiting(text) {
      time.replaceChildren(new Option(text, ''));
      time.disabled = true;
      status.textContent = text;
      grid.replaceChildren();
      ready();
    }

    function choose(value) {
      chosen = value;
      time.value = value;
      grid.querySelectorAll('button').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.time === value)));
      ready();
    }

    function chip(slot) {
      const item = document.createElement('li');
      const free = slot.state === 'free';
      const element = document.createElement(free ? 'button' : 'span');
      element.className = `schedule-slot schedule-slot--${slot.state}`;
      const at = document.createElement('time');
      at.textContent = slot.time;
      const label = document.createElement('small');
      label.textContent = slot.label;
      element.append(at, label);
      if (free) {
        element.type = 'button';
        element.dataset.time = slot.time;
        element.setAttribute('aria-pressed', String(slot.time === chosen));
        listen(element, 'click', () => choose(slot.time));
      } else {
        element.setAttribute('aria-disabled', 'true');
      }
      item.append(element);
      return item;
    }

    async function refresh() {
      if (!clinician.value || !date.value) {
        waiting('Pick a date and clinician to see their times.');
        return;
      }
      waiting('Finding free times…');
      if (pending) pending.abort();
      pending = new AbortController();
      const query = new URLSearchParams({ clinician: clinician.value, date: date.value, duration: duration ? duration.value : '15' });
      if (patient && patient.value) query.set('patient', patient.value);
      // Change time: the appointment being moved doesn't block times near its own.
      if (fields.dataset.excludeAppointment) query.set('appointment', fields.dataset.excludeAppointment);
      try {
        const response = await fetch(`${fields.dataset.timesUrl}?${query}`, { credentials: 'same-origin', signal: pending.signal });
        if (!response.ok) throw new Error('No times');
        const { times, slots } = await response.json();
        time.replaceChildren(new Option('Choose a time', ''), ...times.map((at) => new Option(at, at, false, at === chosen)));
        time.disabled = !times.length;
        status.textContent = slots.length ? (times.length ? '' : 'No free times on this day.') : 'Not available on this day.';
        grid.replaceChildren(...slots.map(chip));
        ready();
      } catch (error) {
        if (error.name === 'AbortError') return;
        // Without the day, the plain list with every quarter hour.
        time.replaceChildren(...everyQuarterHour.map((option) => new Option(option.text, option.value, false, option.value === chosen)));
        time.disabled = false;
        picker.hidden = true;
        field.hidden = false;
        ready();
      }
    }

    listen(time, 'change', () => { chosen = time.value; });
    listen(fields, 'change', ready);
    listen(fields, 'input', ready);
    [clinician, date, duration, patient].filter(Boolean).forEach((input) => listen(input, 'change', refresh));
    const dialog = fields.closest('dialog');
    if (dialog && !dialog.open) listen(dialog, 'dialog:open', refresh, { once: true });
    else refresh();
    if (dialog) {
      listen(document, 'click', (event) => {
        const slot = event.target.closest('[data-book-slot]');
        if (!slot || slot.dataset.modalOpen !== dialog.id) return;
        clinician.value = slot.dataset.clinician;
        date.value = slot.dataset.date;
        chosen = slot.dataset.time;
        refresh();
      });
    }
  });
})();

});
