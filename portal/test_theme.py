"""Light and dark mode: every colour in the app's stylesheets is a theme token with a light and a dark value
(scripts/theme_tokens.py writes static/css/theme.css). A new colour written straight into a stylesheet fails here,
so new styles follow the same rules: run the script and it becomes a token."""

import importlib.util
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

SCRIPT = Path(settings.BASE_DIR) / 'scripts' / 'theme_tokens.py'


def _script():
    spec = importlib.util.spec_from_file_location('theme_tokens', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ThemeTokenTests(SimpleTestCase):
    def test_every_stylesheet_colour_is_a_theme_token(self):
        tokens = _script()
        untokenised = {}
        for path in sorted(tokens.CSS.glob('*.css')):
            if path.name in tokens.SKIP:
                continue
            found = {}
            if tokens.tokenise(path.read_text(), found) != path.read_text():
                untokenised[path.name] = sorted(found)
        self.assertEqual(untokenised, {}, 'Run .venv/bin/python scripts/theme_tokens.py to turn these colours into tokens.')

    def test_every_token_has_a_light_and_a_dark_value(self):
        tokens = _script()
        theme = tokens.THEME.read_text()
        light, dark = theme.split(':root[data-theme="dark"]', 1)
        defined_light = set(re.findall(r'--c-([0-9a-f]{6}(?:-\d{3})?):', light))
        defined_dark = set(re.findall(r'--c-([0-9a-f]{6}(?:-\d{3})?):', dark.split('@media', 1)[0]))
        used = set()
        for path in tokens.CSS.glob('*.css'):
            if path.name != 'theme.css':
                used |= set(re.findall(r'var\(--c-([0-9a-f]{6}(?:-\d{3})?)\)', path.read_text()))
        self.assertEqual(used - defined_light, set())
        self.assertEqual(defined_light, defined_dark)

    def test_pages_load_the_theme_and_apply_the_saved_choice_before_drawing(self):
        base = (Path(settings.BASE_DIR) / 'templates' / 'base.html').read_text()
        self.assertIn("css/theme.css", base)
        self.assertLess(base.index("localStorage.getItem('meridian-theme')"), base.index('css/fonts.css'))
        self.assertIn('data-theme-choice="dark"', (Path(settings.BASE_DIR) / 'templates' / 'includes' / 'account_menu.html').read_text())
