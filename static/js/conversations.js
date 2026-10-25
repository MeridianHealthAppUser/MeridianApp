(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('conversations', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
/* Conversations (the record's Messages tab, Patient messages and Team messages): newest message in
   view, Enter sends, and the reply box ready when a conversation was opened. The page stays at the
   top so the heading and tabs stay in view. On Patient messages and Team messages the block runs to
   the bottom of the window on desktop; the CSS uses where it starts. */
(() => {
  const block = document.querySelector('[data-record-messages]');
  if (!block) return;
  if (block.classList.contains('record-inbox--fill')) {
    const notes = block.querySelector('.conversation-notes');
    const thread = block.querySelector('[data-record-conversation]');
    // The conversation's height with the notes closed, kept while they are open.
    const keep = () => {
      if (thread && !(notes && notes.open)) block.style.setProperty('--conversation-height', `${thread.offsetHeight}px`);
    };
    const fit = () => {
      block.style.setProperty('--inbox-top', `${Math.round(block.getBoundingClientRect().top + window.scrollY)}px`);
      keep();
    };
    if (notes) listen(notes.querySelector('summary'), 'click', keep);
    fit();
    listen(window, 'resize', fit);
    listen(window, 'load', fit);
    if (document.fonts) document.fonts.ready.then(() => { if (!lifecycle || lifecycle.isActive) fit(); });
  }
  const conversation = block.querySelector('[data-record-conversation]');
  if (conversation) conversation.scrollTop = conversation.scrollHeight;
  const reply = block.querySelector('[data-record-reply] textarea');
  if (reply) {
    const grow = () => { reply.style.height = 'auto'; reply.style.height = `${Math.min(reply.scrollHeight, 160)}px`; };
    listen(reply, 'input', grow);
    listen(reply, 'keydown', (event) => {
      // Enter sends; Shift+Enter starts a new line.
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      if (reply.value.trim()) reply.form.requestSubmit();
    });
    const url = new URL(window.location.href);
    if (url.searchParams.has('thread') || url.searchParams.get('new') === '1' || reply.form.querySelector('.record-error')) {
      reply.focus({ preventScroll: true });
    }
  }
})();

});
