"""Single-practice mode removes selectors and enforces public/account boundaries."""

import uuid
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.profile import make_profile_context, save_own_profile
from care.forms import EligibilityQuestionnaireForm
from care.intake import save_intake
from care.models import AuditEvent, ConsentDocument, Lead, PracticeSettings, ScreeningQuestionnaire
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY

from .intake_views import LEAD_KEY
from .privacy_forms import PublicPracticeForm


@override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health', DEBUG=True)
class SinglePracticeSurfacesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        cls.other = Company.objects.create(name='Hidden Practice Sentinel', slug='hidden-practice')
        cls.staff = get_user_model().objects.create_user('single-staff@example.test', first_name='Sam')
        cls.patient_user = get_user_model().objects.create_user('single-patient@example.test')
        for company in (cls.company, cls.other):
            CompanyMembership.objects.create(company=company, user=cls.staff, role='super_admin')
            Patient.objects.create(company=company, user=cls.patient_user, first_name='Nadia', last_name='Example')
            PracticeSettings.objects.create(company=company, support_email=f'{company.slug}@example.test')
            for kind in ('service', 'telehealth'):
                ConsentDocument.objects.create(company=company, kind=kind, version='v1',
                    title=f'{company.name} {kind}', body=f'{company.name} published {kind} notice')

    def intake_data(self):
        response = self.client.get(reverse('portal:questionnaire'))
        return {
            'practice': self.company.pk, 'submission_token': response.context['form']['submission_token'].value(),
            'first_name': 'Enquiry', 'last_name': 'Example', 'email': 'enquiry@example.test',
            'phone': '0824617730', 'id_number': 'DEMO-INTAKE', 'height_cm': '168', 'weight_kg': '94',
            'adult': 'yes', 'weight_related_condition': 'no', 'pregnancy': 'no',
            'thyroid_history': 'no', 'pancreatitis': 'no', 'service_consent': 'on',
            'health_context': 'No regular medication or known allergies.',
        }

    def test_anonymous_questionnaire_hides_selector_and_other_notices(self):
        response = self.client.get(reverse('portal:questionnaire'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['multi_practice_enabled'])
        self.assertTrue(response.context['form']['practice'].is_hidden)
        self.assertEqual(list(response.context['form'].fields['practice'].queryset), [self.company])
        self.assertNotContains(response, 'Choose your practice')
        self.assertNotContains(response, self.other.name)
        self.assertContains(response, 'Meridian Health published service notice')

    def test_foreign_practice_query_is_rejected(self):
        response = self.client.get(reverse('portal:questionnaire'), {'practice': self.other.pk})
        self.assertEqual(response.status_code, 404)

    def test_foreign_practice_post_is_rejected_without_any_lead(self):
        data = self.intake_data()
        data['practice'] = self.other.pk
        response = self.client.post(reverse('portal:questionnaire'), data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('practice', response.context['form'].errors)
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(ScreeningQuestionnaire.objects.exists())

    def test_meridian_submission_remains_a_lead_not_an_account(self):
        before = (get_user_model().objects.count(), Patient.objects.count(), CompanyMembership.objects.count())
        response = self.client.post(reverse('portal:questionnaire'), self.intake_data())
        self.assertRedirects(response, reverse('portal:questionnaire-result'), fetch_redirect_response=False)
        self.assertEqual(Lead.objects.get().company, self.company)
        self.assertEqual(before, (get_user_model().objects.count(), Patient.objects.count(), CompanyMembership.objects.count()))

    def test_existing_other_practice_lead_is_unavailable_even_in_own_browser(self):
        lead = Lead.objects.create(company=self.other, submission_key=uuid.uuid4(),
            first_name='Hidden', last_name='Enquiry', email='hidden@example.test')
        session = self.client.session
        session[LEAD_KEY] = lead.pk
        session.save()
        for url in (reverse('portal:questionnaire') + '?edit=1',
                    reverse('portal:questionnaire-result'), reverse('portal:questionnaire-checkout')):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        self.assertTrue(Lead.objects.filter(pk=lead.pk).exists())

    def test_direct_intake_service_rechecks_practice_after_form_validation(self):
        form = Mock()
        form.is_valid.return_value = True
        form.cleaned_data = {'practice': self.other}
        with self.assertRaisesMessage(ValidationError, 'no longer accepting enquiries'):
            save_intake(form=form, submission_key=uuid.uuid4(), expected_notices=None)
        self.assertFalse(Lead.objects.exists())

    def test_public_policy_only_displays_meridian(self):
        for name in ('public-terms', 'public-privacy', 'public-contact'):
            with self.subTest(page=name):
                response = self.client.get(reverse(f'portal:{name}'))
                self.assertEqual(response.context['policy_company'], self.company)
                self.assertNotContains(response, 'View practice')
                self.assertNotContains(response, self.other.name)
                denied = self.client.get(reverse(f'portal:{name}'), {'practice': self.other.pk})
                self.assertIsNone(denied.context['policy_company'])
                self.assertNotContains(denied, self.other.name)
                self.assertTrue(denied.context['practice_form'].errors)

    def test_public_forms_fail_closed_without_configured_practice(self):
        with override_settings(SINGLE_PRACTICE_SLUG='missing-practice'):
            self.assertFalse(EligibilityQuestionnaireForm().fields['practice'].queryset.exists())
            self.assertFalse(PublicPracticeForm().fields['practice'].queryset.exists())
            response = self.client.get(reverse('portal:public-terms'))
            self.assertIsNone(response.context['policy_company'])
            self.assertNotContains(response, self.other.name)

    def test_staff_profile_has_only_meridian_and_no_switcher(self):
        self.client.force_login(self.staff)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.other.pk
        session.save()
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(list(response.context['membership_page']), [self.company])
        self.assertEqual(self.client.session[ACTIVE_COMPANY_SESSION_KEY], self.company.pk)
        self.assertNotContains(response, 'company-switcher')
        self.assertNotContains(response, self.other.name)
        self.assertNotContains(response, 'One login across your practices')

    def test_patient_profile_and_pages_hide_practice_switching(self):
        self.client.force_login(self.patient_user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.other.pk
        session.save()
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(list(response.context['membership_page']), [self.company])
        self.assertNotContains(response, 'company-switcher')
        self.assertNotContains(response, self.other.name)
        dashboard = self.client.get(reverse('portal:patient-dashboard'))
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(dashboard, 'practice selector above')

    def test_staff_navigation_hides_practice_management_but_keeps_user_management(self):
        self.client.force_login(self.staff)
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            with self.subTest(page=name):
                response = self.client.get(reverse(f'portal:{name}'))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, reverse('portal:management-practices'))
                self.assertNotContains(response, 'company-switcher')
                self.assertContains(response, reverse('portal:management-users'))

    def test_profile_save_does_not_modify_or_audit_other_practice_data(self):
        other_membership = CompanyMembership.objects.get(company=self.other, user=self.staff)
        before = (other_membership.role, other_membership.is_active, other_membership.updated_at)
        save_own_profile(actor=self.staff, context_token=make_profile_context(self.staff),
                         first_name='Samuel', last_name='Example')
        self.assertEqual(list(AuditEvent.objects.filter(action='account.profile_updated').values_list('company_id', flat=True)),
                         [self.company.pk])
        other_membership.refresh_from_db()
        self.assertEqual(before, (other_membership.role, other_membership.is_active, other_membership.updated_at))

    @override_settings(MULTI_PRACTICE_ENABLED=True)
    def test_switches_and_public_selection_can_be_restored_without_data_changes(self):
        response = self.client.get(reverse('portal:questionnaire'))
        self.assertContains(response, 'Choose your practice')
        self.assertContains(response, self.other.name)
        self.client.force_login(self.staff)
        response = self.client.get(reverse('accounts:profile'))
        self.assertContains(response, 'company-switcher')
        self.assertCountEqual(response.context['membership_page'], [self.company, self.other])
