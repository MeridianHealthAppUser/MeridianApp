/* Regular working hours (templates/portal/includes/schedule_availability.html): a day can be worked in more than one
   period, such as 08:00–11:00 and 13:00–16:00. An extra period with nothing in it waits behind "+ Add hours", and ×
   takes one away, clearing its times so it isn't saved. Without scripts every period shows and empty ones are ignored. */
(() => {
  document.querySelectorAll('[data-working-day]').forEach((day) => {
    const rows = [...day.querySelectorAll('[data-working-period]')];
    const add = day.querySelector('[data-period-add]');
    if (!add || rows.length < 2) return;
    const blank = (row) => [...row.querySelectorAll('select')].every((select) => !select.value);
    const refresh = () => { add.hidden = !rows.some((row) => row.hidden); };

    rows.slice(1).forEach((row) => {
      if (blank(row) && !row.querySelector('.errorlist')) row.hidden = true;
      const remove = row.querySelector('[data-period-remove]');
      remove.hidden = false;
      remove.addEventListener('click', () => {
        row.querySelectorAll('select').forEach((select) => {
          select.value = '';
          select.dispatchEvent(new Event('change'));
        });
        row.hidden = true;
        add.before(row); // Next time, it's added back last.
        refresh();
        add.focus();
      });
    });

    add.addEventListener('click', () => {
      const next = day.querySelector('[data-working-period][hidden]');
      if (!next) return;
      next.hidden = false;
      next.querySelector('select').focus();
      refresh();
    });
    refresh();
  });
})();
