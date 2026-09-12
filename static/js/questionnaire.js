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
  function updateBmi() {
    const height = Number(heightInput?.value);
    const weight = Number(weightInput?.value);
    const bmi = weight / ((height / 100) ** 2);
    if (bmiValue) bmiValue.textContent = height > 0 && weight > 0 && Number.isFinite(bmi) && bmi < 1000 ? `BMI ${bmi.toFixed(1)}` : 'BMI —';
  }
  function updatePracticeDocuments() {
    if (!practiceInput || !documents.length) return;
    documents.forEach(section => { section.hidden = section.dataset.practiceId !== practiceInput.value; });
  }
  form.addEventListener('change', updateProgress);
  heightInput?.addEventListener('input', updateBmi);
  weightInput?.addEventListener('input', updateBmi);
  practiceInput?.addEventListener('change', () => {
    updatePracticeDocuments();
    if (consentInput) consentInput.checked = false;
  });
  updateProgress();
  updateBmi();
  updatePracticeDocuments();
})();
