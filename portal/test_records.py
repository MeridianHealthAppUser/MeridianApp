"""The merged record is permission-scoped, not a name-based patient search."""

from django.test import override_settings
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import (
    AuditEvent, ClinicalEncounter, ClinicalNote, ConsentRecord, LabRequest, LabResult,
    MedicationProduct, PatientEvent, PatientMedicalProfile, PatientMedicalProfileRevision,
    PatientSubscription, TreatmentAuthorization, WeightEntry,
)
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ClinicalRecordTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alpha = Company.objects.create(name='Record Alpha', slug='record-alpha')
        cls.beta = Company.objects.create(name='Record Beta', slug='record-beta')
        cls.gamma = Company.objects.create(name='Record Hidden', slug='record-hidden')
        cls.inactive = Company.objects.create(name='Record Inactive', slug='record-inactive', is_active=False)
        cls.doctor = get_user_model().objects.create_user(email='record-doctor@example.test', first_name='Review', last_name='Doctor')
        cls.colleague = get_user_model().objects.create_user(email='record-colleague@example.test', first_name='Other', last_name='Doctor')
        cls.admin = get_user_model().objects.create_user(email='record-admin@example.test')
        cls.super_admin = get_user_model().objects.create_user(email='record-super@example.test')
        cls.user = get_user_model().objects.create_user(email='record-patient@example.test')
        cls.other_user = get_user_model().objects.create_user(email='record-patient-other@example.test')
        for company in (cls.alpha, cls.beta, cls.inactive):
            for user, role in ((cls.doctor, 'doctor'), (cls.colleague, 'doctor'),
                               (cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin')):
                CompanyMembership.objects.create(company=company, user=user, role=role)
        cls.patient = Patient.objects.create(company=cls.alpha, user=cls.user, first_name='Same', last_name='Person', assigned_doctor=cls.doctor)
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.user, first_name='Linked', last_name='Person')
        cls.gamma_patient = Patient.objects.create(company=cls.gamma, user=cls.user, first_name='Hidden', last_name='Person')
        cls.inactive_patient = Patient.objects.create(company=cls.inactive, user=cls.user, first_name='Inactive', last_name='Person')
        cls.other_patient = Patient.objects.create(company=cls.beta, user=cls.other_user, first_name='Same', last_name='Person')
        cls.alpha_event = cls.event(cls.patient, 'Alpha visible event')
        cls.beta_event = cls.event(cls.beta_patient, 'Beta visible event')
        cls.event(cls.gamma_patient, 'UNAUTHORIZED_PRACTICE_SECRET')
        cls.event(cls.inactive_patient, 'INACTIVE_PRACTICE_SECRET')
        cls.event(cls.other_patient, 'SAME_NAME_OTHER_PATIENT_SECRET')
        cls.event(cls.patient, 'INTERNAL_EVENT_SECRET', is_patient_visible=False)
        cls.shared_note = ClinicalNote.objects.create(company=cls.alpha, patient=cls.patient, author=cls.colleague, body='Shared clinician note')
        ClinicalNote.objects.create(company=cls.alpha, patient=cls.patient, author=cls.colleague, body='OTHER_PRIVATE_NOTE_SECRET', is_private=True)
        ClinicalNote.objects.create(company=cls.alpha, patient=cls.patient, author=cls.doctor, body='OWN_PRIVATE_NOTE_SECRET', is_private=True)
        cls.signed_note = ClinicalNote.objects.create(company=cls.alpha, patient=cls.patient, author=cls.colleague, body='Signed duplicate must not appear twice')
        cls.signed = ClinicalEncounter.objects.create(company=cls.alpha, patient=cls.patient, clinician=cls.colleague,
            clinical_summary='Signed consultation summary', status='signed', signed_at=timezone.now(), signed_by=cls.colleague, signed_note=cls.signed_note)
        ClinicalEncounter.objects.create(company=cls.alpha, patient=cls.patient, clinician=cls.colleague, clinical_summary='OTHER_DRAFT_SECRET')
        ClinicalEncounter.objects.create(company=cls.alpha, patient=cls.patient, clinician=cls.doctor, clinical_summary='OWN_DRAFT_SECRET')
        cls.lab = LabRequest.objects.create(company=cls.alpha, patient=cls.patient, requested_by=cls.doctor,
            panel_name='Requested panel', status='reviewed', reviewed_by=cls.doctor, reviewed_at=timezone.now(), result_summary='Clinician reviewed the report.')
        cls.result = LabResult.objects.create(company=cls.alpha, patient=cls.patient, lab_request=cls.lab,
            uploaded_by=cls.user, filename='report.pdf', content=b'BINARY_REPORT_SECRET', size=20, sha256='0' * 64)
        cls.profile = PatientMedicalProfile.objects.create(company=cls.alpha, patient=cls.patient,
            revision=1, answers={'allergies': 'ALPHA_DOCTOR_ONLY_PROFILE', 'unexpected_key': 'UNEXPECTED_PROFILE_SECRET'})
        PatientMedicalProfileRevision.objects.create(company=cls.alpha, patient=cls.patient, profile=cls.profile,
            revision=1, answers=cls.profile.answers, saved_by=cls.user)
        beta_profile = PatientMedicalProfile.objects.create(company=cls.beta, patient=cls.beta_patient,
            revision=1, answers={'medications': 'BETA_DOCTOR_ONLY_PROFILE'})
        PatientMedicalProfileRevision.objects.create(company=cls.beta, patient=cls.beta_patient, profile=beta_profile,
            revision=1, answers=beta_profile.answers, saved_by=cls.user)
        cls.consent = ConsentRecord.objects.create(company=cls.alpha, patient=cls.patient, consent_type='service',
            document_version='v1', accepted=True, accepted_at=timezone.now(), ip_address='192.0.2.99')
        cls.weight = WeightEntry.objects.create(company=cls.alpha, patient=cls.patient, weight_kg='92.35')
        cls.product = MedicationProduct.objects.create(company=cls.alpha, name='Recorded medicine', price=100)
        cls.authorization = TreatmentAuthorization.objects.create(company=cls.alpha, patient=cls.patient,
            product=cls.product, prescribed_by=cls.doctor, max_dose='Recorded dose', expires_on=timezone.localdate() + timedelta(days=30))
        cls.subscription = PatientSubscription.objects.create(company=cls.alpha, patient=cls.patient, authorization=cls.authorization,
            plan_name='Local subscription', monthly_amount=1995, cycle_number=3, next_debit_on=timezone.localdate() + timedelta(days=5))
        AuditEvent.objects.create(company=cls.alpha, patient=cls.patient, actor=cls.doctor,
            action='patient.record_viewed', metadata={'note': 'RAW_AUDIT_METADATA_SECRET'}, ip_address='192.0.2.100')
        AuditEvent.objects.create(company=cls.alpha, patient=cls.patient, actor=cls.colleague,
            action='consultation.draft_saved', metadata={'body': 'DRAFT_AUDIT_SECRET'})

    @classmethod
    def event(cls, patient, title, **kwargs):
        return PatientEvent.objects.create(company=patient.company, patient=patient, category='clinical', title=title, **kwargs)

    def login(self, user=None, company=None):
        self.client.force_login(user or self.doctor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.alpha).pk
        session.save()

    def url(self, export=False, patient=None):
        return reverse('portal:staff-patient-record-export' if export else 'portal:staff-patient-record', args=[(patient or self.patient).pk])

    def page(self, export=False, **filters):
        return self.client.get(self.url(export), filters)

    def test_anonymous_redirected_and_nonclinical_roles_rejected(self):
        for export in (False, True):
            self.assertEqual(self.page(export).status_code, 302)
        for user in (self.admin, self.user):
            self.login(user)
            for export in (False, True):
                self.assertEqual(self.page(export).status_code, 403)

    def test_current_record_resolved_before_any_cross_practice_scope(self):
        self.login()
        for export in (False, True):
            response = self.client.get(self.url(export, self.beta_patient), {'scope': 'all', 'reason': 'direct_care'})
            self.assertEqual(response.status_code, 404)

    def test_current_practice_is_default_and_record_has_dedicated_links(self):
        self.login()
        response = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([record.pk for record in response.context['records']], [self.patient.pk])
        self.assertNotContains(response, 'Beta visible event')
        self.assertContains(response, reverse('portal:clinical-consultation-create', args=[self.patient.pk]))
        self.assertNotContains(response, 'name="summary"')
        self.assertNotContains(response, 'name="body"')

    def test_cross_practice_requires_a_valid_fixed_care_purpose(self):
        self.login()
        for reason in ('', 'anything I want'):
            before = AuditEvent.objects.count()
            response = self.page(scope='all', reason=reason)
            self.assertEqual(response.status_code, 200)
            self.assertIn('reason', response.context['filter_form'].errors)
            self.assertEqual(response.context['timeline_entries'], [])
            self.assertNotContains(response, 'ALPHA_DOCTOR_ONLY_PROFILE')
            self.assertEqual(self.page(True, scope='all', reason=reason).status_code, 400)
            self.assertEqual(AuditEvent.objects.count(), before)

    def test_cross_practice_links_same_user_only_in_active_clinical_memberships(self):
        self.login()
        response = self.page(True, scope='all', reason='covering_colleague')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual({record['patient_id'] for record in payload['records']}, {self.patient.pk, self.beta_patient.pk})
        self.assertContains(response, 'Alpha visible event')
        self.assertContains(response, 'Beta visible event')
        for secret in ('UNAUTHORIZED_PRACTICE_SECRET', 'INACTIVE_PRACTICE_SECRET', 'SAME_NAME_OTHER_PATIENT_SECRET'):
            self.assertNotContains(response, secret)

    def test_inactive_membership_or_nonclinical_role_excludes_foreign_clinical_record(self):
        self.login()
        membership = CompanyMembership.objects.get(company=self.beta, user=self.doctor)
        for changes in ({'is_active': False}, {'is_active': True, 'role': 'practice_admin'}):
            for key, value in changes.items():
                setattr(membership, key, value)
            membership.save()
            response = self.page(True, scope='all', reason='direct_care')
            self.assertEqual(len(response.json()['records']), 1)
            self.assertNotContains(response, 'Beta visible event')

    def test_unlinked_patient_is_never_matched_by_name(self):
        self.patient.user = None
        self.patient.save()
        self.login()
        response = self.page(True, scope='all', reason='direct_care')
        self.assertEqual(len(response.json()['records']), 1)

    def test_inactive_linked_patient_excluded(self):
        self.beta_patient.is_active = False
        self.beta_patient.save()
        self.login()
        self.assertEqual(len(self.page(True, scope='all', reason='direct_care').json()['records']), 1)

    def test_private_drafts_internal_events_and_attachment_bytes_never_exported(self):
        self.login()
        response = self.page(True)
        for secret in ('OTHER_PRIVATE_NOTE_SECRET', 'OWN_PRIVATE_NOTE_SECRET', 'OTHER_DRAFT_SECRET', 'OWN_DRAFT_SECRET',
                       'INTERNAL_EVENT_SECRET', 'BINARY_REPORT_SECRET', 'RAW_AUDIT_METADATA_SECRET', 'DRAFT_AUDIT_SECRET',
                       '192.0.2.99', '192.0.2.100', 'UNEXPECTED_PROFILE_SECRET'):
            self.assertNotContains(response, secret)
        self.assertContains(response, 'Shared clinician note')
        self.assertContains(response, 'Signed consultation summary')
        self.assertContains(response, 'Clinician reviewed the report.')
        self.assertNotContains(response, 'Signed duplicate must not appear twice')

    def test_export_queries_do_not_select_pdf_content_or_raw_audit_fields(self):
        self.login()
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            self.page(True)
        selects = [query['sql'].lower() for query in queries if query['sql'].lstrip().lower().startswith('select')]
        for query in selects:
            self.assertNotIn('"care_labresult"."content"', query)
            self.assertNotIn('"care_auditevent"."metadata"', query)
            self.assertNotIn('"care_auditevent"."ip_address"', query)

    def test_doctor_can_read_profile_answers_but_super_admin_cannot(self):
        self.login()
        response = self.page(True, scope='all', reason='direct_care')
        self.assertContains(response, 'ALPHA_DOCTOR_ONLY_PROFILE')
        self.assertContains(response, 'BETA_DOCTOR_ONLY_PROFILE')
        self.login(self.super_admin)
        for export in (False, True):
            response = self.page(export, scope='all', reason='direct_care')
            self.assertNotContains(response, 'ALPHA_DOCTOR_ONLY_PROFILE')
            self.assertNotContains(response, 'BETA_DOCTOR_ONLY_PROFILE')
            if export:
                self.assertContains(response, 'Signed consultation summary')

    def test_profile_access_is_decided_by_role_in_actual_practice(self):
        CompanyMembership.objects.filter(company=self.beta, user=self.doctor).update(role='super_admin')
        self.login()
        response = self.page(True, scope='all', reason='direct_care')
        self.assertContains(response, 'ALPHA_DOCTOR_ONLY_PROFILE')
        self.assertNotContains(response, 'BETA_DOCTOR_ONLY_PROFILE')
        self.assertContains(response, 'Beta visible event')

    def test_read_and_export_audited_once_in_each_actual_practice(self):
        self.login()
        for export, action in ((False, 'patient.clinical_record_viewed'), (True, 'patient.clinical_record_exported')):
            before = AuditEvent.objects.filter(action=action).count()
            self.page(export, scope='all', reason='covering_colleague')
            events = AuditEvent.objects.filter(action=action).order_by('-pk')[:2]
            self.assertEqual(AuditEvent.objects.filter(action=action).count(), before + 2)
            self.assertEqual({(event.company_id, event.patient_id) for event in events},
                             {(self.alpha.pk, self.patient.pk), (self.beta.pk, self.beta_patient.pk)})
            self.assertTrue(all(event.metadata['reason'] == 'covering_colleague' for event in events))

    def test_head_has_no_audit_writes_and_responses_are_not_cacheable(self):
        self.login()
        before = AuditEvent.objects.count()
        for export in (False, True):
            response = self.client.head(self.url(export))
            self.assertEqual(response.status_code, 200)
            self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_post_is_not_an_edit_or_an_export(self):
        self.login()
        before = AuditEvent.objects.count()
        for export in (False, True):
            self.assertEqual(self.client.post(self.url(export), {}).status_code, 405)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_export_is_json_attachment_with_safe_id_filename(self):
        self.login()
        response = self.page(True)
        self.assertEqual(response.headers['Content-Type'], 'application/json')
        self.assertEqual(response.headers['Content-Disposition'], f'attachment; filename="patient-{self.patient.pk}-clinical-record.json"')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.json()['schema_version'], 1)

    def test_invalid_categories_scope_and_dates_do_not_show_records(self):
        self.login()
        for data in ({'scope': 'foreign'}, {'category': 'secret'}, {'date_to': '9999-12-31'},
                     {'date_from': '2000-01-02', 'date_to': '2000-01-01'}):
            response = self.page(**data)
            self.assertTrue(response.context['filter_form'].errors)
            self.assertEqual(response.context['timeline_entries'], [])
            self.assertEqual(self.page(True, **data).status_code, 400)

    def test_date_and_category_filters_apply_to_every_timeline_source(self):
        self.login()
        response = self.page(True, category='clinical')
        self.assertTrue(all(entry['category'] == 'clinical' for entry in response.json()['timeline']))
        response = self.page(True, date_from='2000-01-01', date_to='2000-01-02')
        self.assertEqual(response.json()['timeline'], [])

    def test_timeline_paginates_twenty_and_export_includes_all_matching_rows(self):
        for number in range(25):
            self.event(self.patient, f'Pagination event {number}')
        self.login()
        first = self.page(category='clinical')
        second = self.page(category='clinical', page=2)
        self.assertEqual(len(first.context['timeline_entries']), 20)
        ids = lambda response: {(entry['kind'], entry['id']) for entry in response.context['timeline_entries']}
        self.assertFalse(ids(first) & ids(second))
        self.assertIn('category=clinical', second.context['pagination_query'])
        self.assertGreater(len(self.page(True, category='clinical').json()['timeline']), 25)

    def test_pagination_snapshot_prevents_own_read_audits_shifting_rows(self):
        self.login()
        for number in range(25):
            self.event(self.patient, f'Stable history event {number}')
        first = self.page()
        next_url = f'{self.url()}?{first.context["pagination_query"]}&page=2'
        second = self.client.get(next_url)
        ids = lambda response: {(entry['kind'], entry['id']) for entry in response.context['timeline_entries']}
        self.assertFalse(ids(first) & ids(second))
        self.assertEqual(first.context['page_obj'].paginator.count, second.context['page_obj'].paginator.count)
        self.assertIn('snapshot=', second.context['pagination_query'])

    def test_invalid_snapshot_is_rejected_instead_of_unbounded_history(self):
        self.login()
        for value in ('garbage', '9999-12-31T12:00:00Z'):
            self.assertTrue(self.page(snapshot=value).context['filter_form'].errors)
            self.assertEqual(self.page(True, snapshot=value).status_code, 400)

    def test_malformed_cross_tenant_rows_do_not_enter_record_or_sidebar(self):
        PatientEvent.objects.create(company=self.beta, patient=self.patient, category='clinical', title='MALFORMED_TENANT_SECRET')
        ClinicalNote.objects.create(company=self.beta, patient=self.patient, author=self.doctor, body='MALFORMED_NOTE_SECRET')
        WeightEntry.objects.create(company=self.beta, patient=self.patient, recorded_on=timezone.localdate() - timedelta(days=1), weight_kg='999.00')
        self.login()
        response = self.page(True, scope='all', reason='direct_care')
        for secret in ('MALFORMED_TENANT_SECRET', 'MALFORMED_NOTE_SECRET', '999.00'):
            self.assertNotContains(response, secret)

    def test_sidebar_current_authorization_weights_and_latest_consent_only(self):
        ConsentRecord.objects.create(company=self.alpha, patient=self.patient, consent_type='service', document_version='v2', accepted=False)
        self.login()
        response = self.page()
        self.assertEqual(response.context['authorization'].pk, self.authorization.pk)
        self.assertTrue(response.context['authorization'].is_current)
        self.assertEqual(response.context['record_weights'][0].pk, self.weight.pk)
        self.assertEqual(len(response.context['record_consents']), 1)
        self.assertEqual(response.context['record_consents'][0].document_version, 'v2')

    def test_expired_authorization_not_labelled_current(self):
        self.authorization.expires_on = timezone.localdate() - timedelta(days=1)
        self.authorization.save()
        self.login()
        response = self.page()
        self.assertFalse(response.context['authorization'].is_current)

    def test_foreign_timeline_entries_do_not_link_into_current_practice_routes(self):
        foreign = ClinicalEncounter.objects.create(company=self.beta, patient=self.beta_patient, clinician=self.doctor,
            status='signed', clinical_summary='Foreign signed entry', signed_at=timezone.now())
        self.login()
        response = self.page(scope='all', reason='direct_care', category='clinical')
        entries = [entry for entry in response.context['timeline_entries'] if entry['kind'] == 'consultation' and entry['id'] == foreign.pk]
        self.assertEqual(entries[0]['url'], '')

    def directory(self, **filters):
        return self.client.get(reverse('portal:patient-list'), filters)

    def test_directory_defaults_current_and_all_includes_only_staff_memberships(self):
        self.login(self.admin)
        self.assertEqual({patient.pk for patient in self.directory().context['patients']}, {self.patient.pk})
        response = self.directory(scope='all')
        self.assertEqual({patient.pk for patient in response.context['patients']}, {self.patient.pk, self.beta_patient.pk, self.other_patient.pk})
        self.assertNotContains(response, self.gamma.name)
        self.assertNotContains(response, self.inactive.name)
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_foreign_directory_rows_require_explicit_post_practice_switch(self):
        self.login()
        response = self.directory(scope='all')
        self.assertNotContains(response, f'href="{reverse("portal:patient-detail", args=[self.beta_patient.pk])}"')
        self.assertNotContains(response, f'href="{self.url(patient=self.beta_patient)}"')
        self.assertContains(response, f'action="{reverse("portal:activate-company", args=[self.beta.slug])}"')
        self.assertContains(response, f'name="next" value="{reverse("portal:patient-list")}"')

    def test_directory_auth_subscription_and_search_keep_practice_scoping(self):
        self.login()
        response = self.directory(q='Same', scope='current')
        self.assertEqual(len(response.context['patients']), 1)
        patient = response.context['patients'][0]
        self.assertEqual(patient.record_authorization.pk, self.authorization.pk)
        self.assertEqual(patient.record_subscription.cycle_number, 3)
        self.assertContains(response, 'Recorded medicine')
        self.assertContains(response, 'Auth expires')

    def test_directory_invalid_scope_does_not_broaden_query(self):
        self.login()
        response = self.directory(scope='everyone')
        self.assertIn('scope', response.context['filter_form'].errors)
        self.assertEqual(response.context['patients'], [])
