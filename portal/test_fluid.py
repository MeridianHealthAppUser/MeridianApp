"""The Apple-style motion (static/js/fluid.js) runs only on pages that load its stylesheet (static/css/fluid.css), and
finds that stylesheet by its file name. In production the name carries a content hash (collectstatic with
config.static_storage.MeridianManifestStaticFilesStorage), so the script must recognise both names, or production
pages keep the styles but lose the motion: dialogs, tab pills, tab switches and the side menu's collapse."""

import re

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.files import File
from django.test import SimpleTestCase

from config.static_storage import MeridianManifestStaticFilesStorage


class FluidStylesheetNameTests(SimpleTestCase):
    def pattern(self):
        source = (settings.BASE_DIR / 'static' / 'js' / 'fluid.js').read_text()
        match = re.search(r'const FLUID_CSS = /(.+)/;', source)
        self.assertIsNotNone(match, 'fluid.js names the stylesheet it looks for in FLUID_CSS.')
        return re.compile(match.group(1))

    def test_the_script_recognises_the_stylesheet_in_development_and_in_production(self):
        pattern = self.pattern()
        with open(finders.find('css/fluid.css'), 'rb') as handle:
            production = MeridianManifestStaticFilesStorage().hashed_name('css/fluid.css', content=File(handle))
        self.assertNotEqual(production, 'css/fluid.css')
        for name in ('css/fluid.css', production):
            with self.subTest(name=name):
                self.assertRegex(f'/static/{name}', pattern)

    def test_the_script_ignores_other_stylesheets(self):
        pattern = self.pattern()
        for name in ('css/fluid_print.css', 'css/console.css', 'css/fluid.css.map', 'vendor/fluid/css/other.css'):
            with self.subTest(name=name):
                self.assertNotRegex(f'/static/{name}', pattern)
