"""Privacy writes preserve consent/history and tenant/account boundaries."""

from django.test import override_settings
import hashlib
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import (
    AuditEvent, ConsentDocument, ConsentRecord, PatientCommunicationPreference, PatientDataRequest,
    PatientDataRequestReply, PracticeSettings, WeightEntry,
)
from care.privacy import create_data_request, publish_policy_version, respond_to_data_request, save_communication_preference
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PrivacyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alpha = Company.objects.create(name='Privacy Alpha', slug='privacy-alpha')
        cls.beta = Company.objects.create(name='Privacy Beta', slug='privacy-beta')
        cls.hidden = Company.objects.create(name='Privacy Hidden', slug='privacy-hidden', is_active=False)
        cls.user = get_user_model().objects.create_user(email='privacy-patient@example.test', first_name='Private', last_name='Person')
        cls.other = get_user_model().objects.create_user(email='privacy-other@example.test')
        cls.doctor = get_user_model().objects.create_user(email='privacy-doctor@example.test')
        cls.admin = get_user_model().objects.create_user(email='privacy-admin@example.test', first_name='Practice', last_name='Administrator')
        cls.super_admin = get_user_model().objects.create_user(email='privacy-super@example.test')
        for company in (cls.alpha, cls.beta):
            for user, role in ((cls.doctor, 'doctor'), (cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin')):
                CompanyMembership.objects.create(company=company, user=user, role=role)
        cls.patient = Patient.objects.create(company=cls.alpha, user=cls.user, first_name='Private', last_name='Person')
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.user, first_name='Linked', last_name='Person')
        cls.other_patient = Patient.objects.create(company=cls.alpha, user=cls.other, first_name='Other', last_name='Person')
        cls.document = ConsentDocument.objects.create(company=cls.alpha, kind='service', version='v1', title='Approved combined notice',
            body='Alpha actual policy text. <script>alert(1)</script>', effective_from=timezone.localdate() - timedelta(days=2))
        cls.consent = ConsentRecord.objects.create(company=cls.alpha, patient=cls.patient, user=cls.user, document=cls.document,
            consent_type='service', document_version='v1', accepted=True, accepted_at=timezone.now(), ip_address='192.0.2.45')
        cls.request_record = PatientDataRequest.objects.create(company=cls.alpha, patient=cls.patient, kind='correction',
            description='Please review my recorded contact details.', submission_key=uuid.uuid4())
        cls.foreign_request = PatientDataRequest.objects.create(company=cls.beta, patient=cls.beta_patient, kind='question',
            description='FOREIGN_REQUEST_SECRET', submission_key=uuid.uuid4())
        cls.other_request = PatientDataRequest.objects.create(company=cls.alpha, patient=cls.other_patient, kind='access',
            description='OTHER_PATIENT_REQUEST_SECRET', submission_key=uuid.uuid4())
        cls.weight = WeightEntry.objects.create(company=cls.alpha, patient=cls.patient, weight_kg='94.00')
        PracticeSettings.objects.create(company=cls.alpha, support_email='support@example.test')

    def login(self, actor=None, company=None):
        self.client.force_login(actor or self.user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.alpha).pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.alpha).pk
        session.save()

    def url(self, name, pk=None):
        return reverse(f'portal:{name}', args=[pk] if pk else None)

    def get(self, name, pk=None, **query):
        return self.client.get(self.url(name, pk), query)

    def form_data(self, name, pk=None, **fields):
        response = self.get(name, pk)
        self.assertEqual(response.status_code, 200)
        return {'privacy_context': response.context['privacy_context'], **fields}

    def post(self, name, data, pk=None):
        return self.client.post(self.url(name, pk), data)

    def privacy_counts(self):
        return {model.__name__: model.objects.count() for model in (
            PatientCommunicationPreference, PatientDataRequest, PatientDataRequestReply, ConsentRecord, AuditEvent,
        )}

    def test_patient_privacy_pages_require_owned_active_patient(self):
        names = ('patient-privacy', 'patient-data-requests', 'patient-data-request-create')
        for name in names:
            self.assertEqual(self.get(name).status_code, 302)
        self.login(self.doctor)
        for name in names:
            self.assertEqual(self.get(name).status_code, 403)

    def test_patient_can_only_view_own_current_practice_requests(self):
        self.login()
        response = self.get('patient-data-requests')
        self.assertEqual([item.pk for item in response.context['page_obj']], [self.request_record.pk])
        for record in (self.foreign_request, self.other_request):
            self.assertEqual(self.get('patient-data-request-detail', record.pk).status_code, 404)

    def test_patient_get_and_head_are_read_only(self):
        self.login()
        before = self.privacy_counts()
        for name, pk in (('patient-privacy', None), ('patient-data-requests', None),
                         ('patient-data-request-create', None), ('patient-data-request-detail', self.request_record.pk)):
            for method in ('get', 'head'):
                response = getattr(self.client, method)(self.url(name, pk))
                self.assertEqual(response.status_code, 200)
                self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(self.privacy_counts(), before)

    def test_marketing_defaults_off_without_creating_preference(self):
        self.login()
        response = self.get('patient-privacy')
        self.assertFalse(response.context['form']['marketing_enabled'].value())
        self.assertFalse(PatientCommunicationPreference.objects.exists())

    def test_preference_changes_are_separate_from_immutable_consent(self):
        self.login()
        before = list(ConsentRecord.objects.values())
        data = self.form_data('patient-privacy', marketing_enabled='on')
        self.assertEqual(self.post('patient-privacy', data).status_code, 302)
        self.assertTrue(PatientCommunicationPreference.objects.get(patient=self.patient).marketing_enabled)
        data = self.form_data('patient-privacy')
        self.assertEqual(self.post('patient-privacy', data).status_code, 302)
        self.assertFalse(PatientCommunicationPreference.objects.get(patient=self.patient).marketing_enabled)
        self.assertEqual(list(ConsentRecord.objects.values()), before)
        self.assertFalse(PatientCommunicationPreference.objects.filter(patient=self.beta_patient).exists())

    def test_replaying_preference_has_no_additional_audit_write(self):
        self.login()
        data = self.form_data('patient-privacy', marketing_enabled='on')
        self.post('patient-privacy', data)
        before = self.privacy_counts()
        self.assertEqual(self.post('patient-privacy', data).status_code, 302)
        self.assertEqual(self.privacy_counts(), before)

    def test_preference_stale_other_tab_cannot_overwrite_new_setting(self):
        self.login()
        old = self.form_data('patient-privacy')
        data = self.form_data('patient-privacy', marketing_enabled='on')
        self.post('patient-privacy', data)
        response = self.post('patient-privacy', old)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertTrue(PatientCommunicationPreference.objects.get(patient=self.patient).marketing_enabled)

    def test_patient_forms_detect_changed_practice_and_preserve_draft(self):
        self.login()
        for name, fields in (('patient-privacy', {'marketing_enabled': 'on'}),
                             ('patient-data-request-create', {'kind': 'question', 'description': 'Keep my draft'})):
            self.login()
            data = self.form_data(name, **fields)
            self.login(company=self.beta)
            before = self.privacy_counts()
            response = self.post(name, data)
            self.assertTrue(response.context['form'].non_field_errors())
            self.assertEqual(self.privacy_counts(), before)
            if 'description' in fields:
                self.assertContains(response, fields['description'])

    def test_missing_tampered_and_expired_tokens_write_nothing(self):
        self.login()
        data = self.form_data('patient-data-request-create', kind='question', description='Token test')
        before = self.privacy_counts()
        for token in ('', 'invalid'):
            response = self.post('patient-data-request-create', {**data, 'privacy_context': token})
            self.assertTrue(response.context['form'].non_field_errors())
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() + 13 * 60 * 60):
            response = self.post('patient-data-request-create', data)
            self.assertTrue(response.context['form'].non_field_errors())
        self.assertEqual(self.privacy_counts(), before)

    def test_create_request_is_owned_idempotent_and_does_not_delete_records(self):
        self.login()
        data = self.form_data('patient-data-request-create', kind='deletion', description='Please review deletion. PRIVATE_DESCRIPTION')
        response = self.post('patient-data-request-create', data)
        self.assertEqual(response.status_code, 302)
        record = PatientDataRequest.objects.get(description=data['description'])
        self.assertEqual((record.company_id, record.patient_id, record.status), (self.alpha.pk, self.patient.pk, 'open'))
        before = self.privacy_counts()
        self.post('patient-data-request-create', data)
        self.assertEqual(self.privacy_counts(), before)
        self.assertTrue(WeightEntry.objects.filter(pk=self.weight.pk).exists())
        self.assertTrue(ConsentRecord.objects.filter(pk=self.consent.pk).exists())
        self.assertTrue(get_user_model().objects.get(pk=self.user.pk).is_active)
        self.assertNotIn('PRIVATE_DESCRIPTION', str(list(AuditEvent.objects.values('metadata'))))

    def test_request_duplicate_key_cannot_be_reused_for_different_content(self):
        self.login()
        data = self.form_data('patient-data-request-create', kind='question', description='First question')
        self.post('patient-data-request-create', data)
        response = self.post('patient-data-request-create', {**data, 'description': 'Different question'})
        self.assertTrue(response.context['form'].non_field_errors())

    def test_required_and_oversized_request_details_validated(self):
        self.login()
        for kind, description in (('question', ''), ('invalid', 'Hello'), ('question', 'x' * 5001)):
            data = self.form_data('patient-data-request-create', kind=kind, description=description)
            before = PatientDataRequest.objects.count()
            response = self.post('patient-data-request-create', data)
            self.assertTrue(response.context['form'].errors)
            self.assertEqual(PatientDataRequest.objects.count(), before)

    def test_staff_requests_only_for_practice_admin_and_super_admin(self):
        self.login(self.doctor)
        self.assertEqual(self.get('staff-data-requests').status_code, 403)
        self.assertEqual(self.get('staff-data-request-detail', self.request_record.pk).status_code, 403)
        for actor in (self.admin, self.super_admin):
            self.login(actor)
            response = self.get('staff-data-requests')
            self.assertEqual({record.pk for record in response.context['page_obj']}, {self.request_record.pk, self.other_request.pk})
            self.assertEqual(self.get('staff-data-request-detail', self.foreign_request.pk).status_code, 404)

    def test_staff_response_append_only_patient_visible_and_idempotent(self):
        self.login(self.admin)
        data = self.form_data('staff-data-request-detail', self.request_record.pk, status='in_review', body='Your request is being reviewed.')
        self.assertEqual(self.post('staff-data-request-detail', data, self.request_record.pk).status_code, 302)
        before = self.privacy_counts()
        self.assertEqual(self.post('staff-data-request-detail', data, self.request_record.pk).status_code, 302)
        self.assertEqual(self.privacy_counts(), before)
        next_data = self.form_data('staff-data-request-detail', self.request_record.pk, status='resolved', body='Here is our recorded response.')
        self.post('staff-data-request-detail', next_data, self.request_record.pk)
        self.assertEqual(PatientDataRequestReply.objects.filter(data_request=self.request_record).count(), 2)
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, 'resolved')
        self.login()
        response = self.get('patient-data-request-detail', self.request_record.pk)
        self.assertContains(response, 'Your request is being reviewed.')
        self.assertContains(response, 'Here is our recorded response.')
        self.assertNotContains(response, '<textarea')

    def test_staff_status_change_requires_patient_visible_explanation(self):
        self.login(self.admin)
        data = self.form_data('staff-data-request-detail', self.request_record.pk, status='declined', body='')
        response = self.post('staff-data-request-detail', data, self.request_record.pk)
        self.assertIn('body', response.context['form'].errors)
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, 'open')

    def test_staff_reply_stale_version_cannot_overwrite_another_response(self):
        self.login(self.admin)
        old = self.form_data('staff-data-request-detail', self.request_record.pk, status='resolved', body='Old version')
        new = self.form_data('staff-data-request-detail', self.request_record.pk, status='in_review', body='New response')
        self.post('staff-data-request-detail', new, self.request_record.pk)
        response = self.post('staff-data-request-detail', old, self.request_record.pk)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertEqual(PatientDataRequestReply.objects.count(), 1)

    def test_staff_reply_changed_practice_rejected(self):
        self.login(self.admin)
        data = self.form_data('staff-data-request-detail', self.request_record.pk, status='resolved', body='Wrong practice')
        self.login(self.admin, self.beta)
        before = PatientDataRequestReply.objects.count()
        response = self.post('staff-data-request-detail', data, self.foreign_request.pk)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertEqual(PatientDataRequestReply.objects.count(), before)

    def test_staff_request_head_does_not_audit_and_get_does(self):
        self.login(self.admin)
        before = AuditEvent.objects.filter(action='privacy.request_viewed').count()
        self.client.head(self.url('staff-data-request-detail', self.request_record.pk))
        self.assertEqual(AuditEvent.objects.filter(action='privacy.request_viewed').count(), before)
        response = self.get('staff-data-request-detail', self.request_record.pk)
        self.assertEqual(AuditEvent.objects.filter(action='privacy.request_viewed').count(), before + 1)
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_request_and_response_lists_paginate_without_leaks(self):
        for index in range(22):
            PatientDataRequest.objects.create(company=self.alpha, patient=self.patient, kind='question', description=f'Request {index}', submission_key=uuid.uuid4())
        self.login()
        first = self.get('patient-data-requests', status='open', kind='question')
        second = self.get('patient-data-requests', status='open', kind='question', page=2)
        self.assertEqual(len(first.context['page_obj']), 20)
        self.assertEqual(len(second.context['page_obj']), 2)
        for index in range(21):
            PatientDataRequestReply.objects.create(company=self.alpha, data_request=self.request_record, author=self.admin,
                body=f'Response {index}', status='in_review', submission_key=uuid.uuid4())
        self.assertEqual(len(self.get('patient-data-request-detail', self.request_record.pk, page=2).context['page_obj']), 1)

    def test_invalid_request_filter_does_not_broaden_list(self):
        self.login()
        response = self.get('patient-data-requests', status='invalid')
        self.assertTrue(response.context['filter_form'].errors)
        self.assertEqual(len(response.context['page_obj']), 0)

    def test_data_requests_cannot_be_edited_or_deleted_by_patient_post(self):
        self.login()
        self.assertEqual(self.post('patient-data-request-detail', {'description': 'overwrite'}, self.request_record.pk).status_code, 405)
        self.assertEqual(self.client.delete(self.url('patient-data-request-detail', self.request_record.pk)).status_code, 405)

    def test_access_history_only_actor_permitted_practices_and_sanitized_fields(self):
        AuditEvent.objects.create(company=self.alpha, actor=self.admin, patient=self.patient, action='privacy.request_viewed',
            target_type='care.patientdatarequest', target_id='PATIENT_IDENTIFIER_SECRET', metadata={'body': 'METADATA_SECRET'}, ip_address='192.0.2.90')
        AuditEvent.objects.create(company=self.beta, actor=self.admin, action='privacy.preference_updated')
        AuditEvent.objects.create(company=self.alpha, actor=self.doctor, action='OTHER_ACTOR_ACTION_SECRET')
        AuditEvent.objects.create(company=self.hidden, actor=self.admin, action='INACTIVE_PRACTICE_ACTION_SECRET')
        AuditEvent.objects.create(company=self.alpha, actor=self.admin, action='UNKNOWN_ACTION_SECRET', target_type='UNKNOWN_TARGET_SECRET')
        self.login(self.admin)
        response = self.get('account-access-history', scope='all')
        self.assertEqual(len(response.context['access_rows']), 3)
        for secret in ('METADATA_SECRET', '192.0.2.90', 'PATIENT_IDENTIFIER_SECRET', 'OTHER_ACTOR_ACTION_SECRET',
                       'INACTIVE_PRACTICE_ACTION_SECRET', 'UNKNOWN_ACTION_SECRET', 'UNKNOWN_TARGET_SECRET', 'Private Person'):
            self.assertNotContains(response, secret)
        self.assertContains(response, 'Privacy request viewed')
        self.assertEqual(len(self.get('account-access-history').context['access_rows']), 2)

    def test_access_history_invalid_scope_no_data_and_no_incidental_writes(self):
        self.login(self.admin)
        before = AuditEvent.objects.count()
        response = self.get('account-access-history', scope='everyone')
        self.assertTrue(response.context['filter_form'].errors)
        self.assertEqual(response.context['access_rows'], [])
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_patient_recent_activity_is_own_current_record_only(self):
        AuditEvent.objects.create(company=self.alpha, actor=self.user, patient=self.patient, action='privacy.preference_updated')
        AuditEvent.objects.create(company=self.beta, actor=self.user, patient=self.beta_patient, action='privacy.preference_updated')
        AuditEvent.objects.create(company=self.alpha, actor=self.admin, patient=self.patient, action='privacy.request_viewed')
        self.login()
        response = self.get('patient-privacy')
        self.assertEqual(len(response.context['access_rows']), 1)
        self.assertNotContains(response, '192.0.2.45')

    def policy_data(self, **changes):
        values = dict(kind='service', version='v2', title='Approved updated notice', body='Actual approved updated text.',
                      effective_from=timezone.localdate().isoformat(), confirm_publication='on')
        values.update(changes)
        return self.form_data('policy-create', **values)

    def test_policy_management_super_admin_only(self):
        for actor in (self.user, self.doctor, self.admin):
            self.login(actor)
            for name in ('policy-list', 'policy-create'):
                self.assertEqual(self.get(name).status_code, 403)
        self.login(self.super_admin)
        self.assertEqual(self.get('policy-list').status_code, 200)

    def test_policy_publishing_creates_version_with_hash_without_changing_consent(self):
        self.login(self.super_admin)
        before = list(ConsentRecord.objects.values())
        data = self.policy_data()
        self.assertEqual(self.post('policy-create', data).status_code, 302)
        document = ConsentDocument.objects.get(company=self.alpha, kind='service', version='v2')
        self.assertEqual(document.content_hash, hashlib.sha256(data['body'].encode()).hexdigest())
        self.assertEqual(list(ConsentRecord.objects.values()), before)
        self.document.refresh_from_db()
        self.assertIn('Alpha actual policy text', self.document.body)

    def test_policy_repeat_is_idempotent_but_changed_historical_version_rejected(self):
        self.login(self.super_admin)
        data = self.policy_data()
        self.post('policy-create', data)
        before = (ConsentDocument.objects.count(), AuditEvent.objects.count())
        self.post('policy-create', data)
        self.assertEqual((ConsentDocument.objects.count(), AuditEvent.objects.count()), before)
        response = self.post('policy-create', {**data, 'body': 'Overwrite existing version'})
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertEqual(ConsentDocument.objects.get(version='v2').body, data['body'])

    def test_policy_publish_requires_confirmation_and_valid_date(self):
        self.login(self.super_admin)
        for changes in ({'confirm_publication': ''}, {'effective_from': '9999-12-31'}):
            response = self.post('policy-create', self.policy_data(**changes))
            self.assertTrue(response.context['form'].errors)
        self.assertFalse(ConsentDocument.objects.filter(version='v2').exists())

    def test_policy_stale_practice_does_not_publish_elsewhere(self):
        self.login(self.super_admin)
        data = self.policy_data()
        self.login(self.super_admin, self.beta)
        response = self.post('policy-create', data)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertFalse(ConsentDocument.objects.filter(version='v2').exists())

    def test_public_policy_displays_actual_escaped_current_practice_document(self):
        for name in ('public-terms', 'public-privacy'):
            response = self.get(name, practice=self.alpha.pk)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Alpha actual policy text.')
            self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
            self.assertNotContains(response, '<script>alert(1)</script>')
            self.assertContains(response, 'Version v1')
            self.assertIn('no-store', response.headers['Cache-Control'])

    def test_future_and_inactive_policies_are_not_current_public_documents(self):
        ConsentDocument.objects.create(company=self.alpha, kind='service', version='future', title='Future', body='FUTURE_POLICY_SECRET',
            effective_from=timezone.localdate() + timedelta(days=1))
        ConsentDocument.objects.create(company=self.alpha, kind='service', version='inactive', title='Inactive', body='INACTIVE_POLICY_SECRET', is_active=False)
        response = self.get('public-terms', practice=self.alpha.pk)
        self.assertContains(response, 'Alpha actual policy text.')
        self.assertNotContains(response, 'FUTURE_POLICY_SECRET')
        self.assertNotContains(response, 'INACTIVE_POLICY_SECRET')

    def test_unpublished_practice_has_explicit_not_ready_state_not_invented_copy(self):
        response = self.get('public-privacy', practice=self.beta.pk)
        self.assertContains(response, 'No current notice published')
        self.assertContains(response, 'Clinical onboarding is not ready')
        self.assertNotContains(response, 'Alpha actual policy text.')

    def test_public_invalid_or_inactive_practice_does_not_fallback_to_other_documents(self):
        for practice in ('invalid', self.hidden.pk, 999999):
            response = self.get('public-terms', practice=practice)
            self.assertTrue(response.context['practice_form'].errors)
            self.assertNotContains(response, 'Alpha actual policy text.')

    def test_public_contact_uses_configured_email_and_does_not_send_messages(self):
        before = AuditEvent.objects.count()
        response = self.get('public-contact', practice=self.alpha.pk)
        self.assertContains(response, 'support@example.test')
        self.assertContains(response, 'does not send a message or an email')
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertEqual(self.post('public-contact', {'message': 'Do not send'}).status_code, 405)

    def test_service_rechecks_deactivated_patient_account(self):
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            create_data_request(actor=self.user, company=self.alpha, patient=self.patient, kind='question',
                description='Not permitted', submission_key=uuid.uuid4())

    def test_service_rechecks_admin_membership_and_cross_tenant_request(self):
        with self.assertRaises(PermissionDenied):
            respond_to_data_request(actor=self.admin, company=self.alpha, data_request=self.foreign_request,
                body='Cross tenant', status='resolved', submission_key=uuid.uuid4(), expected_updated_at=self.foreign_request.updated_at.isoformat())
        CompanyMembership.objects.filter(company=self.alpha, user=self.admin).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            respond_to_data_request(actor=self.admin, company=self.alpha, data_request=self.request_record,
                body='Not permitted', status='resolved', submission_key=uuid.uuid4(), expected_updated_at=self.request_record.updated_at.isoformat())

    def test_policy_service_rechecks_role_and_cannot_replace_accepted_document(self):
        with self.assertRaises(PermissionDenied):
            publish_policy_version(actor=self.admin, company=self.alpha, kind='service', version='v2', title='No', body='No',
                effective_from=timezone.localdate(), confirmed=True)
        with self.assertRaises(ValidationError):
            publish_policy_version(actor=self.super_admin, company=self.alpha, kind='service', version='v1', title='Overwrite', body='Overwrite',
                effective_from=timezone.localdate(), confirmed=True)
        self.assertEqual(ConsentRecord.objects.get(pk=self.consent.pk).document_id, self.document.pk)

    def test_csrf_required_for_all_mutating_privacy_forms(self):
        client = Client(enforce_csrf_checks=True)
        for actor, names in ((self.user, ('patient-privacy', 'patient-data-request-create')),
                             (self.super_admin, ('policy-create',))):
            client.force_login(actor)
            for name in names:
                self.assertEqual(client.post(self.url(name), {}).status_code, 403)

    def test_django_admin_cannot_overwrite_or_delete_policy_and_consent_history(self):
        from django.contrib import admin

        request = RequestFactory().get('/admin/')
        request.user = self.super_admin
        request.user.is_superuser = True
        request.user.is_staff = True
        for model, record in ((ConsentDocument, self.document), (ConsentRecord, self.consent)):
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_change_permission(request, record))
            self.assertFalse(model_admin.has_delete_permission(request, record))
            with self.assertRaises(PermissionDenied):
                model_admin.save_model(request, record, None, True)
            with self.assertRaises(PermissionDenied):
                model_admin.delete_model(request, record)
            with self.assertRaises(PermissionDenied):
                model_admin.delete_queryset(request, model.objects.all())
        self.assertTrue(ConsentRecord.objects.filter(pk=self.consent.pk).exists())
        self.assertTrue(ConsentDocument.objects.filter(pk=self.document.pk).exists())
