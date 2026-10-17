// A filter that applies as soon as it changes, like the Calendar's Clinician dropdown: no button needed.
document.addEventListener('change', (event) => {
  const field = event.target.closest('[data-submit-on-change]');
  if (!field || !field.form) return;
  if (field.form.requestSubmit) field.form.requestSubmit();
  else field.form.submit();
});
