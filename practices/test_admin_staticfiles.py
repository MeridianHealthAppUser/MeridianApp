"""Render Jazzmin using a real production manifest, not DEBUG static URLs."""

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment

from .models import Company, CompanyMembership, Patient


@override_settings(
    DEBUG=False,
    ALLOWED_HOSTS=['testserver'],
    SECURE_SSL_REDIRECT=True,
    MULTI_PRACTICE_ENABLED=False,
    SINGLE_PRACTICE_SLUG='meridian-health',
    VIDEO_ENABLED=False,
)
class ProductionAdminStaticfilesTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.static_directory = TemporaryDirectory(prefix='meridian-admin-static-test-')
        cls.addClassCleanup(cls.static_directory.cleanup)
        cls.static_settings = override_settings(
            STATIC_ROOT=cls.static_directory.name,
            STATIC_URL='/static/',
            STORAGES={
                'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                'staticfiles': {'BACKEND': 'config.static_storage.MeridianManifestStaticFilesStorage'},
            },
        )
        cls.static_settings.enable()
        cls.addClassCleanup(cls.static_settings.disable)
        # Collect once into an isolated directory for this class; never replace
        # the project's working staticfiles directory or its manifest.
        call_command('collectstatic', interactive=False, verbosity=0, stdout=StringIO())

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        cls.superuser = get_user_model().objects.create_superuser(
            email='production-static-test@example.test', password='StaticTest!2026',
        )
        CompanyMembership.objects.create(
            company=cls.company, user=cls.superuser, role=CompanyMembership.Role.SUPER_ADMIN,
        )

    def assert_hashed_brand_assets(self, response, include_js=True):
        names = [settings.JAZZMIN_SETTINGS['custom_css']]
        if include_js:
            names.append(settings.JAZZMIN_SETTINGS['custom_js'])
        for name in names:
            with self.subTest(asset=name):
                hashed_url = staticfiles_storage.url(name)
                self.assertNotEqual(hashed_url, f'/static/{name}')
                self.assertContains(response, hashed_url)

    def test_empty_dashboard_renders_with_default_theme_and_hashed_assets(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('admin:index'), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['meridian_dashboard']['activity']['has_data'])
        self.assertContains(response, 'data-theme-base="/static/vendor/bootswatch"')
        self.assert_hashed_brand_assets(response)

    def test_populated_dashboard_and_patient_changelist_render(self):
        patient = Patient.objects.create(company=self.company, first_name='Synthetic', last_name='Patient')
        Appointment.objects.create(
            company=self.company, patient=patient, clinician=self.superuser, starts_at=timezone.now(),
        )
        self.client.force_login(self.superuser)
        for url in (reverse('admin:index'), reverse('admin:practices_patient_changelist')):
            with self.subTest(url=url):
                response = self.client.get(url, secure=True)
                self.assertEqual(response.status_code, 200)
                self.assert_hashed_brand_assets(response)
        response = self.client.get(reverse('admin:index'), secure=True)
        self.assertEqual(response.context['meridian_dashboard']['appointment_total'], 1)

    def test_admin_login_renders_with_hashed_brand_styles(self):
        response = self.client.get(reverse('admin:login'), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assert_hashed_brand_assets(response, include_js=False)

    def test_directory_prefix_obeys_custom_static_url(self):
        for base_url in ('/static/', '/assets/', 'https://assets.example.test/meridian/'):
            with self.subTest(base_url=base_url), override_settings(STATIC_URL=base_url):
                self.assertEqual(staticfiles_storage.url('vendor/bootswatch'), f'{base_url}vendor/bootswatch')

    def test_actual_files_still_resolve_to_collected_hashed_files(self):
        for name in ('css/meridian_admin.css', 'js/meridian_admin.js',
                     'vendor/bootswatch/litera/bootstrap.min.css'):
            with self.subTest(asset=name):
                hashed_name = staticfiles_storage.hashed_files[name]
                self.assertNotEqual(name, hashed_name)
                self.assertEqual(staticfiles_storage.url(name), f'/static/{hashed_name}')
                self.assertTrue((Path(self.static_directory.name) / hashed_name).is_file())

    def test_missing_files_remain_strict_even_beneath_bootswatch(self):
        for name in ('css/does-not-exist.css', 'vendor/bootswatch/does-not-exist/bootstrap.min.css',
                     'vendor/bootswatch/missing.css', 'vendor/bootswatch-other'):
            with self.subTest(asset=name), self.assertRaisesMessage(ValueError, 'Missing staticfiles manifest entry'):
                staticfiles_storage.url(name)
