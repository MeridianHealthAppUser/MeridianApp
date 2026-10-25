// Appearance in the account menu: Device follows the phone's or computer's setting; Light and Dark stay as chosen in
// this browser. base.html applies the saved choice before the page is drawn; this keeps the buttons in step.
(function () {
  var KEY = 'meridian-theme';

  function saved() {
    try { return localStorage.getItem(KEY) || 'system'; } catch (error) { return 'system'; }
  }

  function apply(choice) {
    if (choice === 'light' || choice === 'dark') {
      document.documentElement.setAttribute('data-theme', choice);
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    document.querySelectorAll('[data-theme-choice]').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.getAttribute('data-theme-choice') === choice));
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    apply(saved());
    document.addEventListener('click', function (event) {
      var button = event.target.closest('[data-theme-choice]');
      if (!button) return;
      var choice = button.getAttribute('data-theme-choice');
      try {
        if (choice === 'system') localStorage.removeItem(KEY); else localStorage.setItem(KEY, choice);
      } catch (error) { /* Private browsing: the choice lasts for this page only. */ }
      apply(choice);
    });
  });
}());
