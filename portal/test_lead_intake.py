"""The public enquiry flow never grants accounts, booking, or payment privileges."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import (
    Appointment, ConsentDocument, ConsentRecord, Lead, Payment,
    PracticeSettings, ScreeningQuestionnaire,
)
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(DEBUG=True)
@override_settings(MULTI_PRACTICE_ENABLED=True)
class PublicLeadIntakeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Alpha Intake Practice', slug='meridian-health')
        cls.other_company = Company.objects.create(name='Beta Intake Practice', slug='beta-intake')
        cls.inactive_company = Company.objects.create(name='Closed Intake Practice', slug='closed-intake', is_active=False)
        cls.documents = {}
        for company in (cls.company, cls.other_company):
            PracticeSettings.objects.create(company=company)
            for kind in (ConsentDocument.Kind.SERVICE, ConsentDocument.Kind.TELEHEALTH):
                cls.documents[company.pk, kind] = ConsentDocument.objects.create(
                    company=company, kind=kind, version=f'{company.slug}-{kind}-v1',
                    title=f'{company.name} {kind}', body=f'{company.name} current {kind} notice.',
                    effective_from=timezone.localdate() - timedelta(days=1),
                )

    def identity_counts(self):
        return {
            model._meta.label: model.objects.count()
            for model in (get_user_model(), CompanyMembership, Patient, Appointment, Payment)
        }

    def intake_data(self, client=None, **changes):
        client = client or self.client
        response = client.get(reverse('portal:questionnaire'))
        self.assertEqual(response.status_code, 200)
        token = response.context['form']['submission_token'].value()
        self.assertTrue(token)
        data = {
            'practice': self.company.pk,
            'first_name': 'Nadia', 'last_name': 'Intake',
            'email': 'new-enquiry@example.test', 'phone': '082 461 7730',
            'id_number': 'P-DEMO-001', 'height_cm': '168', 'weight_kg': '94',
            'adult': 'yes', 'weight_related_condition': 'no', 'pregnancy': 'no',
            'thyroid_history': 'no', 'pancreatitis': 'no',
            'health_context': '  Amlodipine 5 mg daily. No known allergies.\nPlease discuss my history.\n',
            'service_consent': 'on', 'submission_token': token,
        }
        data.update(changes)
        return data

    def submit(self, client=None, **changes):
        client = client or self.client
        data = self.intake_data(client, **changes)
        response = client.post(reverse('portal:questionnaire'), data, REMOTE_ADDR='192.0.2.41')
        return response, data

    def assert_successful_submission(self, response):
        self.assertRedirects(response, reverse('portal:questionnaire-result'), fetch_redirect_response=False)

    def assert_checkout_unavailable(self, client=None):
        self.assertRedirects(
            (client or self.client).get(reverse('portal:questionnaire-checkout')),
            reverse('portal:questionnaire-result'), fetch_redirect_response=False,
        )

    def test_public_form_has_expanded_fields_and_only_active_practices(self):
        response = self.client.get(reverse('portal:questionnaire'))
        form = response.context['form']
        for name in (
            'practice', 'first_name', 'last_name', 'email', 'phone', 'id_number',
            'height_cm', 'weight_kg', 'adult', 'weight_related_condition', 'pregnancy',
            'thyroid_history', 'pancreatitis', 'health_context', 'service_consent', 'submission_token',
        ):
            with self.subTest(field=name):
                self.assertIn(name, form.fields)
        self.assertNotIn('telehealth_consent', form.fields)
        self.assertCountEqual(form.fields['practice'].queryset, [self.company, self.other_company])
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))
        self.assertEqual(response.headers.get('Referrer-Policy'), 'same-origin')

    def test_submission_creates_only_lead_screening_and_anonymous_consents(self):
        before = self.identity_counts()
        response, data = self.submit()
        self.assert_successful_submission(response)
        lead = Lead.objects.get()
        self.assertEqual(lead.company, self.company)
        self.assertEqual(lead.id_number, data['id_number'])
        self.assertEqual(lead.email, data['email'])
        self.assertIsNone(lead.converted_patient_id)
        self.assertNotEqual(lead.stage, Lead.Stage.CONVERTED)
        questionnaire = ScreeningQuestionnaire.objects.get(lead=lead)
        self.assertEqual(questionnaire.company, self.company)
        self.assertEqual(questionnaire.status, ScreeningQuestionnaire.Status.SUBMITTED)
        self.assertIsNotNone(questionnaire.submitted_at)
        self.assertEqual(questionnaire.answers['health_context'], data['health_context'])
        for field in ('adult', 'weight_related_condition', 'pregnancy', 'thyroid_history', 'pancreatitis'):
            self.assertEqual(questionnaire.answers[field], data[field])
        consents = list(ConsentRecord.objects.filter(lead=lead))
        self.assertEqual(len(consents), 2)
        self.assertCountEqual([record.consent_type for record in consents], ['service', 'telehealth'])
        for consent in consents:
            self.assertEqual(consent.company, self.company)
            self.assertEqual(consent.document, self.documents[self.company.pk, consent.consent_type])
            self.assertEqual(consent.document_version, consent.document.version)
            self.assertTrue(consent.accepted)
            self.assertIsNotNone(consent.accepted_at)
            self.assertIsNone(consent.user_id)
            self.assertIsNone(consent.patient_id)
            self.assertEqual(consent.ip_address, '192.0.2.41')
            self.assertEqual(consent.source, 'public-questionnaire')
        self.assertEqual(self.identity_counts(), before)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_selected_practice_owns_every_intake_record_and_its_notice_versions(self):
        response, _ = self.submit(practice=self.other_company.pk)
        self.assert_successful_submission(response)
        lead = Lead.objects.get()
        self.assertEqual(lead.company, self.other_company)
        self.assertEqual(ScreeningQuestionnaire.objects.get().company, self.other_company)
        for consent in ConsentRecord.objects.all():
            self.assertEqual(consent.company, self.other_company)
            self.assertEqual(consent.document, self.documents[self.other_company.pk, consent.consent_type])

    def test_extra_account_and_payment_fields_cannot_convert_a_lead(self):
        before = self.identity_counts()
        response, _ = self.submit(
            username='attacker', password='not-an-account', is_superuser='true',
            paid='true', payment_status='paid', screening_status='cleared',
            stage='converted', company=self.other_company.pk,
        )
        self.assert_successful_submission(response)
        lead = Lead.objects.get()
        self.assertEqual(lead.company, self.company)
        self.assertIsNone(lead.converted_patient_id)
        self.assertNotEqual(lead.stage, Lead.Stage.CONVERTED)
        self.assertEqual(self.identity_counts(), before)

    def test_existing_account_email_does_not_link_or_sign_in_the_submitter(self):
        existing = get_user_model().objects.create_user(email='new-enquiry@example.test')
        before = self.identity_counts()
        response, _ = self.submit()
        self.assert_successful_submission(response)
        self.assertEqual(self.identity_counts(), before)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertFalse(Patient.objects.filter(user=existing).exists())
        self.assertFalse(ConsentRecord.objects.filter(user=existing).exists())

    def test_each_gatekeeping_answer_is_required(self):
        for field in ('adult', 'weight_related_condition', 'pregnancy', 'thyroid_history', 'pancreatitis'):
            with self.subTest(field=field):
                data = self.intake_data()
                data.pop(field)
                response = self.client.post(reverse('portal:questionnaire'), data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context['form'].errors)
                self.assertFalse(Lead.objects.exists())

    def test_invalid_dimensions_and_inactive_practice_are_rejected(self):
        invalid = (
            ('height_cm', '0'), ('height_cm', '261'), ('weight_kg', '0'),
            ('weight_kg', '401'), ('practice', self.inactive_company.pk),
            ('practice', '99999999'), ('thyroid_history', 'maybe'),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                response, _ = self.submit(**{field: value})
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context['form'].errors)
                self.assertFalse(Lead.objects.exists())

    def test_single_acceptance_is_required_and_no_partial_records_are_written(self):
        response, _ = self.submit(service_consent='')
        self.assertEqual(response.status_code, 200)
        self.assertIn('service_consent', response.context['form'].errors)
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(ScreeningQuestionnaire.objects.exists())
        self.assertFalse(ConsentRecord.objects.exists())

    def test_invalid_and_missing_submission_tokens_are_rejected_without_writes(self):
        for token in ('', 'invented-token', 'a:b:c'):
            with self.subTest(token=token):
                response, _ = self.submit(submission_token=token)
                self.assertIn(response.status_code, (200, 400, 403))
                self.assertFalse(Lead.objects.exists())

    def test_expired_submission_token_cannot_write_records(self):
        data = self.intake_data()
        future = timezone.now().timestamp() + 86401
        with patch('django.core.signing.time.time', return_value=future):
            response = self.client.post(reverse('portal:questionnaire'), data)
        self.assertIn(response.status_code, (200, 400, 403))
        self.assertFalse(Lead.objects.exists())

    def test_submission_token_cannot_be_replayed_by_another_browser(self):
        data = self.intake_data()
        other_browser = Client()
        self.intake_data(other_browser)
        response = other_browser.post(reverse('portal:questionnaire'), data)
        self.assertIn(response.status_code, (200, 400, 403))
        self.assertFalse(Lead.objects.exists())

    def test_double_submit_is_idempotent_and_refresh_does_not_create_records(self):
        response, data = self.submit()
        self.assert_successful_submission(response)
        original = Lead.objects.get()
        self.assert_successful_submission(self.client.post(reverse('portal:questionnaire'), data))
        for _ in range(2):
            self.assertEqual(self.client.get(reverse('portal:questionnaire-result')).status_code, 200)
        self.assertEqual(Lead.objects.get().pk, original.pk)
        self.assertEqual(ScreeningQuestionnaire.objects.count(), 1)
        self.assertEqual(ConsentRecord.objects.count(), 2)

    def test_notice_changed_after_form_load_requires_fresh_acceptance(self):
        data = self.intake_data()
        document = self.documents[self.company.pk, ConsentDocument.Kind.SERVICE]
        ConsentDocument.objects.filter(pk=document.pk).update(body='Materially revised service terms.')
        response = self.client.post(reverse('portal:questionnaire'), data)
        self.assertIn(response.status_code, (200, 400))
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(ConsentRecord.objects.exists())

    def test_csrf_is_enforced_on_public_submission(self):
        browser = Client(enforce_csrf_checks=True)
        data = self.intake_data(browser)
        self.assertEqual(browser.post(reverse('portal:questionnaire'), data).status_code, 403)
        self.assertFalse(Lead.objects.exists())

    def test_same_origin_browser_submission_passes_csrf_with_the_issued_token(self):
        browser = Client(enforce_csrf_checks=True)
        data = self.intake_data(browser)
        data['csrfmiddlewaretoken'] = browser.cookies['csrftoken'].value
        response = browser.post(reverse('portal:questionnaire'), data, HTTP_ORIGIN='http://testserver')
        self.assert_successful_submission(response)
        self.assertEqual(Lead.objects.count(), 1)

    def test_prototype_risk_answers_require_review(self):
        for changes in (
            {'adult': 'no'}, {'pregnancy': 'yes'}, {'thyroid_history': 'yes'},
            {'thyroid_history': 'unsure'}, {'pancreatitis': 'yes'},
            {'height_cm': '200', 'weight_kg': '100'},
            {'height_cm': '200', 'weight_kg': '112', 'weight_related_condition': 'no'},
        ):
            with self.subTest(changes=changes):
                browser = Client()
                response, _ = self.submit(browser, **changes)
                self.assert_successful_submission(response)
                self.assertEqual(Lead.objects.latest('pk').screening_status, Lead.ScreeningStatus.REFERRED)
                self.assert_checkout_unavailable(browser)

    def test_demo_clearance_uses_bmi_threshold_and_condition_answer(self):
        for changes in (
            {'height_cm': '200', 'weight_kg': '120', 'weight_related_condition': 'no'},
            {'height_cm': '200', 'weight_kg': '108', 'weight_related_condition': 'yes'},
            {'height_cm': '168', 'weight_kg': '94', 'weight_related_condition': 'no'},
        ):
            with self.subTest(changes=changes):
                response, _ = self.submit(Client(), **changes)
                self.assert_successful_submission(response)
                self.assertEqual(Lead.objects.latest('pk').screening_status, Lead.ScreeningStatus.CLEARED)

    def test_screening_uses_unrounded_bmi_not_displayed_rounded_value(self):
        response, _ = self.submit(height_cm='168', weight_kg='76.2', weight_related_condition='yes')
        self.assert_successful_submission(response)
        lead = Lead.objects.get()
        self.assertEqual(lead.bmi, Decimal('27.00'))
        self.assertEqual(lead.screening_status, Lead.ScreeningStatus.REFERRED)

    @override_settings(DEBUG=False)
    def test_production_submission_cannot_automatically_clear_a_patient(self):
        response, _ = self.submit()
        self.assert_successful_submission(response)
        self.assertEqual(Lead.objects.get().screening_status, Lead.ScreeningStatus.PENDING)
        self.assert_checkout_unavailable()

    @override_settings(DEBUG=False)
    def test_production_cannot_accept_missing_approved_consent_documents(self):
        ConsentDocument.objects.filter(company=self.company).update(is_active=False)
        response, _ = self.submit()
        self.assertIn(response.status_code, (200, 400))
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(ConsentRecord.objects.exists())

    def test_result_and_checkout_are_owned_by_the_submitting_session_only(self):
        response, _ = self.submit()
        self.assert_successful_submission(response)
        foreign = Client()
        for name in ('questionnaire-result', 'questionnaire-checkout'):
            with self.subTest(page=name):
                url = reverse(f'portal:{name}')
                own_response = self.client.get(url)
                self.assertEqual(own_response.status_code, 200)
                self.assertIn('no-store', own_response.headers.get('Cache-Control', ''))
                self.assertEqual(own_response.headers.get('Referrer-Policy'), 'same-origin')
                self.assertEqual(foreign.get(url, {'lead': Lead.objects.get().pk}).status_code, 404)

    def test_lead_query_parameter_cannot_select_another_sessions_result(self):
        self.submit(first_name='Original enquirer')
        own_lead = Lead.objects.get()
        other_browser = Client()
        self.submit(other_browser, first_name='Secret other enquirer', email='other-enquirer@example.test')
        foreign_lead = Lead.objects.exclude(pk=own_lead.pk).get()
        response = self.client.get(reverse('portal:questionnaire-result'), {'lead': foreign_lead.pk})
        self.assertIn(response.status_code, (200, 404))
        self.assertNotContains(response, foreign_lead.email, status_code=response.status_code)
        self.assertNotContains(response, foreign_lead.first_name, status_code=response.status_code)

    def test_changing_a_screening_answer_reassesses_the_same_lead(self):
        self.submit()
        original = Lead.objects.get()
        self.assertEqual(original.screening_status, Lead.ScreeningStatus.CLEARED)
        response = self.client.get(reverse('portal:questionnaire'), {'edit': '1'})
        form = response.context['form']
        data = {field: form[field].value() for field in form.fields}
        data.update({'pancreatitis': 'yes', 'service_consent': 'on'})
        self.assert_successful_submission(self.client.post(f'{reverse("portal:questionnaire")}?edit=1', data))
        original.refresh_from_db()
        self.assertEqual(Lead.objects.count(), 1)
        self.assertEqual(original.screening_status, Lead.ScreeningStatus.REFERRED)
        self.assertEqual(ScreeningQuestionnaire.objects.get().answers['pancreatitis'], 'yes')
        self.assert_checkout_unavailable()

    def test_anonymous_result_and_checkout_without_submission_return_not_found(self):
        for name in ('questionnaire-result', 'questionnaire-checkout'):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f'portal:{name}')).status_code, 404)

    def test_dummy_checkout_is_get_only_and_never_creates_accounts_or_bookings(self):
        response, _ = self.submit()
        self.assert_successful_submission(response)
        before = self.identity_counts()
        url = reverse('portal:questionnaire-checkout')
        response = self.client.get(url)
        self.assertContains(response, '630')
        self.assertEqual(self.client.post(url, {'paid': 'true', 'payment_status': 'success'}).status_code, 405)
        self.assertEqual(self.identity_counts(), before)
        self.assertIsNone(Lead.objects.get().converted_patient_id)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_edit_updates_the_same_lead_without_moving_it_to_another_practice(self):
        response, _ = self.submit()
        self.assert_successful_submission(response)
        original = Lead.objects.get()
        edit_response = self.client.get(reverse('portal:questionnaire'), {'edit': '1'})
        form = edit_response.context['form']
        self.assertEqual(form['first_name'].value(), original.first_name)
        data = {
            field: form[field].value()
            for field in form.fields
        }
        data.update({'health_context': 'Updated medication details.', 'service_consent': 'on'})
        self.assert_successful_submission(self.client.post(f'{reverse("portal:questionnaire")}?edit=1', data))
        self.assertEqual(Lead.objects.get().pk, original.pk)
        self.assertEqual(ScreeningQuestionnaire.objects.get().answers['health_context'], data['health_context'])
        edit_response = self.client.get(reverse('portal:questionnaire'), {'edit': '1'})
        data['submission_token'] = edit_response.context['form']['submission_token'].value()
        data['practice'] = self.other_company.pk
        data['first_name'] = 'Should not save'
        response = self.client.post(f'{reverse("portal:questionnaire")}?edit=1', data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('practice', response.context['form'].errors)
        original.refresh_from_db()
        self.assertEqual(original.company, self.company)
        self.assertEqual(original.first_name, 'Nadia')
        self.assertEqual(Lead.objects.count(), 1)


@override_settings(MULTI_PRACTICE_ENABLED=True)
class StaffLeadWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Alpha Leads Practice', slug='alpha-leads')
        cls.other_company = Company.objects.create(name='Beta Leads Practice', slug='beta-leads')
        cls.admin = get_user_model().objects.create_user(email='leads-admin@example.test')
        cls.super_admin = get_user_model().objects.create_user(email='leads-super@example.test')
        cls.doctor = get_user_model().objects.create_user(email='leads-doctor@example.test')
        cls.patient_user = get_user_model().objects.create_user(email='leads-patient@example.test')
        for user, role in (
            (cls.admin, CompanyMembership.Role.PRACTICE_ADMIN),
            (cls.super_admin, CompanyMembership.Role.SUPER_ADMIN),
            (cls.doctor, CompanyMembership.Role.DOCTOR),
        ):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        CompanyMembership.objects.create(
            company=cls.other_company, user=cls.super_admin, role=CompanyMembership.Role.SUPER_ADMIN,
        )
        Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Existing', last_name='Patient')
        cls.pending = Lead.objects.create(
            company=cls.company, first_name='Alice', last_name='Pending Enquiry',
            email='alice-lead@example.test', screening_status=Lead.ScreeningStatus.PENDING,
        )
        cls.cleared = Lead.objects.create(
            company=cls.company, first_name='Beth', last_name='Cleared Enquiry',
            email='beth-lead@example.test', screening_status=Lead.ScreeningStatus.CLEARED,
            stage=Lead.Stage.BOOKING,
        )
        cls.foreign = Lead.objects.create(
            company=cls.other_company, first_name='Confidential', last_name='Beta Enquiry',
            email='secret-beta@example.test',
        )
        ScreeningQuestionnaire.objects.create(
            company=cls.company, lead=cls.pending, status=ScreeningQuestionnaire.Status.SUBMITTED,
            answers={'health_context': 'Distinctive confidential clinical answer.'}, submitted_at=timezone.now(),
        )

    def login(self, user=None, company=None):
        self.client.force_login(user or self.admin)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def test_lead_pages_require_authentication(self):
        for url in (reverse('portal:staff-leads'), reverse('portal:lead-detail', args=[self.pending.pk])):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 302)

    def test_doctors_and_patients_cannot_open_lead_pages(self):
        for user in (self.doctor, self.patient_user):
            self.login(user)
            for url in (reverse('portal:staff-leads'), reverse('portal:lead-detail', args=[self.pending.pk])):
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)

    def test_administrators_can_open_only_their_current_practice_leads(self):
        for user in (self.admin, self.super_admin):
            self.login(user)
            response = self.client.get(reverse('portal:staff-leads'))
            self.assertContains(response, self.pending.last_name)
            self.assertContains(response, self.cleared.last_name)
            self.assertNotContains(response, self.foreign.last_name)
            self.assertEqual(self.client.get(reverse('portal:lead-detail', args=[self.pending.pk])).status_code, 200)
            self.assertEqual(self.client.get(reverse('portal:lead-detail', args=[self.foreign.pk])).status_code, 404)

    def test_deactivated_membership_cannot_access_leads(self):
        self.login()
        CompanyMembership.objects.filter(company=self.company, user=self.admin).update(is_active=False)
        self.assertEqual(self.client.get(reverse('portal:staff-leads')).status_code, 403)
        self.assertEqual(self.client.get(reverse('portal:lead-detail', args=[self.pending.pk])).status_code, 403)

    def test_leads_link_is_shown_only_to_administrative_roles(self):
        lead_url = reverse('portal:staff-leads')
        for user in (self.admin, self.super_admin):
            self.login(user)
            self.assertContains(self.client.get(reverse('portal:desktop-dashboard')), f'href="{lead_url}"')
        self.login(self.doctor)
        self.assertNotContains(self.client.get(reverse('portal:desktop-dashboard')), f'href="{lead_url}"')

    def test_lead_detail_contains_answers_while_list_keeps_health_text_out(self):
        self.login()
        self.assertNotContains(self.client.get(reverse('portal:staff-leads')), 'Distinctive confidential clinical answer.')
        self.assertContains(
            self.client.get(reverse('portal:lead-detail', args=[self.pending.pk])),
            'Distinctive confidential clinical answer.',
        )

    def test_search_status_and_stage_filters(self):
        self.login()
        url = reverse('portal:staff-leads')
        for params in (
            {'q': 'beth-lead@example.test'},
            {'screening_status': Lead.ScreeningStatus.CLEARED},
            {'stage': Lead.Stage.BOOKING},
        ):
            with self.subTest(params=params):
                response = self.client.get(url, params)
                self.assertContains(response, self.cleared.last_name)
                self.assertNotContains(response, self.pending.last_name)
                self.assertNotContains(response, self.foreign.last_name)

    def test_invalid_filters_do_not_fall_back_to_an_unfiltered_list(self):
        self.login()
        for field in ('screening_status', 'stage'):
            with self.subTest(field=field):
                response = self.client.get(reverse('portal:staff-leads'), {field: 'invented-state'})
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context['filter_form'].errors)
                self.assertNotContains(response, self.pending.last_name)
                self.assertNotContains(response, self.cleared.last_name)

    def test_lead_pages_are_read_only_even_with_a_forged_paid_post(self):
        self.login()
        for url in (reverse('portal:staff-leads'), reverse('portal:lead-detail', args=[self.pending.pk])):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {'paid': 'true', 'stage': 'converted'}).status_code, 405)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.stage, Lead.Stage.QUESTIONNAIRE)
        self.assertIsNone(self.pending.converted_patient_id)

    def test_practice_switch_preserves_leads_page_and_clears_old_filters(self):
        self.login(self.super_admin)
        url = reverse('portal:staff-leads')
        response = self.client.post(reverse('portal:activate-company', args=[self.other_company.slug]), {
            'next': f'{url}?q=Alice&screening_status=pending&stage=questionnaire&page=3',
        })
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(self.client.session[ACTIVE_COMPANY_SESSION_KEY], self.other_company.pk)
        response = self.client.get(url)
        self.assertContains(response, self.foreign.last_name)
        self.assertNotContains(response, self.pending.last_name)
        self.assertEqual(self.client.get(reverse('portal:lead-detail', args=[self.pending.pk])).status_code, 404)
