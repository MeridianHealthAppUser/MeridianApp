(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('questionnaire', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
(() => {
  const form = document.querySelector('#screening-form');
  if (!form) return;
  const questions = [...form.querySelectorAll('[data-screening-question]')];
  const progress = document.querySelector('#screening-progress');
  const bmiValue = document.querySelector('#bmi-value');
  const heightInput = form.elements.namedItem('height_cm');
  const weightInput = form.elements.namedItem('weight_kg');
  const practiceInput = form.elements.namedItem('practice');
  const consentInput = form.elements.namedItem('service_consent');
  const documents = [...form.querySelectorAll('[data-practice-id]')];
  function updateProgress() {
    const answered = questions.filter(question => question.querySelector('input:checked')).length;
    if (progress) progress.textContent = `${answered} of ${questions.length} answered`;
  }
  // A phone set to South Africa types a comma as the decimal mark (119,8), which the form accepts too.
  const measure = input => Number(String(input?.value ?? '').trim().replace(',', '.'));
  function updateBmi() {
    const height = measure(heightInput);
    const weight = measure(weightInput);
    const bmi = weight / ((height / 100) ** 2);
    if (bmiValue) bmiValue.textContent = height > 0 && weight > 0 && Number.isFinite(bmi) && bmi < 1000 ? `BMI ${bmi.toFixed(1)}` : 'BMI -';
  }
  function updatePracticeDocuments() {
    if (!practiceInput || !documents.length) return;
    documents.forEach(section => { section.hidden = section.dataset.practiceId !== practiceInput.value; });
  }
  listen(form, 'change', updateProgress);
  listen(heightInput, 'input', updateBmi);
  listen(weightInput, 'input', updateBmi);
  listen(practiceInput, 'change', () => {
    // Each practice has its own screening questions: reload the form for the chosen practice (not while editing one).
    const params = new URLSearchParams(window.location.search);
    if (practiceInput.tagName === 'SELECT' && params.get('edit') !== '1') {
      params.set('practice', practiceInput.value);
      window.location.assign(`${window.location.pathname}?${params.toString()}`);
      return;
    }
    updatePracticeDocuments();
    if (consentInput) consentInput.checked = false;
  });
  updateProgress();
  updateBmi();
  updatePracticeDocuments();

  // The Terms and Conditions and the Privacy Policy, each read in a dialog (Dr Czech, 26 Nov 2026). Inside the
  // practice's WordPress page the frame is as tall as the form, so a centred dialog could open out of sight: it opens
  // by its button instead, and the page scrolls to it (static/js/questionnaire_embed.js).
  const embedded = document.documentElement.classList.contains('questionnaire-embed');
  form.querySelectorAll('[data-consent-open]').forEach((button) => {
    const dialog = document.getElementById(button.dataset.consentOpen);
    if (!dialog || typeof dialog.showModal !== 'function') return;
    button.hidden = false;
    listen(button, 'click', () => {
      if (embedded) {
        const top = Math.max(12, Math.round(button.getBoundingClientRect().top + window.pageYOffset) - 160);
        dialog.style.marginTop = `${top}px`;
        document.dispatchEvent(new CustomEvent('questionnaire:scroll-to', { detail: top }));
      }
      dialog.showModal();
    });
    listen(dialog, 'click', (event) => {
      // The close button, or a click on the shade around the dialog.
      if (event.target.closest('[data-consent-close]') || event.target === dialog) dialog.close();
    });
    listen(dialog, 'close', () => button.focus());
  });
})();

});
