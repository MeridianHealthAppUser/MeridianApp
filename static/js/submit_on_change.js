(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('submit_on_change', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
// A filter that applies as soon as it changes, like the Calendar's Clinician dropdown: no button needed.
listen(document, 'change', (event) => {
  const field = event.target.closest('[data-submit-on-change]');
  if (!field || !field.form) return;
  if (field.form.requestSubmit) field.form.requestSubmit();
  else field.form.submit();
});

});
