(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('conversation_management', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
/* Message action, on its page or in its modal: "Clinician to add" (or "Colleague to add") only shows, and must be
   chosen, while adding someone is the chosen action. Hidden, it's also disabled so it isn't sent. Without
   scripts, every field shows. */
(() => {
  document.querySelectorAll('[data-conversation-manage]').forEach((form) => {
    const action = form.querySelector('select[name="action"]');
    const fields = form.querySelectorAll('[data-shows-for-action]');
    if (!action || !fields.length) return;

    function sync() {
      fields.forEach((field) => {
        const shown = action.value === field.dataset.showsForAction;
        field.hidden = !shown;
        field.querySelectorAll('select, input').forEach((control) => {
          control.disabled = !shown;
          control.required = shown;
        });
      });
    }

    listen(action, 'change', sync);
    sync();
  });
})();

});
