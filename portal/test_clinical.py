"""Clinical pages retain signed ownership boundaries and private PDF delivery."""

from django.test import override_settings
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from care.clinical import create_lab_request, review_lab_request, save_consultation, submit_lab_result
from care.models import AuditEvent, ClinicalEncounter, ClinicalNote, ClinicalTask, LabRequest, LabResult
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF'


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ClinicalPortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Alpha Clinical Pages', slug='alpha-clinical-pages')
        cls.beta = Company.objects.create(name='Beta Clinical Pages', slug='beta-clinical-pages')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='clinical-pages-doctor@example.test')
        cls.colleague = users.create_user(email='clinical-pages-colleague@example.test')
        cls.admin = users.create_user(email='clinical-pages-admin@example.test')
        cls.super_admin = users.create_user(email='clinical-pages-super@example.test')
        cls.patient_user = users.create_user(email='clinical-pages-patient@example.test')
        cls.other_user = users.create_user(email='clinical-pages-other@example.test')
        for user, role in ((cls.doctor, 'doctor'), (cls.colleague, 'doctor'), (cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        CompanyMembership.objects.create(company=cls.beta, user=cls.doctor, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Alice', last_name='Clinical Patient')
        cls.other_patient = Patient.objects.create(company=cls.company, user=cls.other_user, first_name='Beth', last_name='Other Patient')
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.patient_user, first_name='Alice', last_name='Beta Patient')
        cls.occurred_at = timezone.now().replace(microsecond=0) - timedelta(hours=1)

    def login(self, user=None, company=None, client=None):
        client = client or self.client
        self.actor = user or self.doctor
        client.force_login(self.actor)
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def encounter(self, **overrides):
        values = {'company': self.company, 'patient': self.patient, 'actor': self.doctor,
                  'summary': 'Author-only draft narrative.', 'occurred_at': self.occurred_at, 'submission_key': uuid.uuid4()}
        values.update(overrides)
        return save_consultation(**values)

    def lab(self, **overrides):
        values = {'company': self.company, 'patient': self.patient, 'actor': self.doctor,
                  'panel_name': 'HbA1c and lipids', 'submission_key': uuid.uuid4()}
        values.update(overrides)
        return create_lab_request(**values)

    def uploaded_lab(self, **overrides):
        lab = self.lab(**overrides)
        submit_lab_result(lab_request=lab, actor=lab.requested_by, filename='clinical-report.pdf', content=PDF)
        lab.refresh_from_db()
        return lab

    def token(self, *, kind='consultation', record=None, patient=None, actor=None):
        from .clinical_forms import make_clinical_context

        patient = patient or self.patient
        request = RequestFactory().get('/consultations/')
        request.user = actor or self.actor
        return make_clinical_context(request, patient.company, patient, kind, record=record)

    def consultation_data(self, token, **overrides):
        data = {'clinical_context': token, 'appointment': '', 'occurred_at': self.occurred_at.isoformat(),
                'summary': 'Updated author narrative.', 'action': 'save'}
        data.update(overrides)
        return data

    def test_anonymous_clinical_and_patient_lab_pages_require_login(self):
        for route in ('clinical-consultations', 'clinical-labs', 'patient-labs'):
            url = reverse(f'portal:{route}')
            self.assertRedirects(self.client.get(url), f'{reverse("accounts:login")}?next={url}', fetch_redirect_response=False)

    def test_practice_admin_and_patient_accounts_cannot_open_staff_clinical_pages(self):
        encounter = self.encounter()
        lab = self.lab()
        for user in (self.admin, self.patient_user):
            self.login(user)
            for route, args in (
                ('clinical-consultations', []), ('clinical-labs', []),
                ('clinical-consultation-detail', [encounter.pk]), ('clinical-lab-detail', [lab.pk]),
            ):
                with self.subTest(user=user.email, route=route):
                    self.assertEqual(self.client.get(reverse(f'portal:{route}', args=args)).status_code, 403)

    def test_consultation_drafts_are_author_only_but_signed_records_are_clinically_readable(self):
        own = self.encounter()
        other_draft = self.encounter(actor=self.colleague, summary='Hidden colleague draft narrative.')
        other_signed = self.encounter(actor=self.colleague, summary='Shared signed narrative.', sign=True)
        foreign = self.encounter(company=self.beta, patient=self.beta_patient, summary='Foreign practice narrative.', sign=True)
        self.login()
        response = self.client.get(reverse('portal:clinical-consultations'), {'status': 'all'})
        self.assertContains(response, reverse('portal:clinical-consultation-detail', args=[own.pk]))
        self.assertContains(response, reverse('portal:clinical-consultation-detail', args=[other_signed.pk]))
        self.assertNotContains(response, reverse('portal:clinical-consultation-detail', args=[other_draft.pk]))
        self.assertNotContains(response, reverse('portal:clinical-consultation-detail', args=[foreign.pk]))
        self.assertEqual(self.client.get(reverse('portal:clinical-consultation-detail', args=[other_draft.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('portal:clinical-consultation-detail', args=[foreign.pk])).status_code, 404)
        self.login(self.super_admin)
        self.assertEqual(self.client.get(reverse('portal:clinical-consultation-detail', args=[own.pk])).status_code, 404)
        self.assertContains(self.client.get(reverse('portal:clinical-consultation-detail', args=[other_signed.pk])), 'Shared signed narrative.')

    def test_read_permission_never_grants_other_doctors_or_super_admin_edit_permission(self):
        encounter = self.encounter(sign=True)
        for actor in (self.colleague, self.super_admin):
            self.login(actor)
            response = self.client.post(reverse('portal:clinical-consultation-detail', args=[encounter.pk]), self.consultation_data(self.token(record=encounter)))
            self.assertEqual(response.status_code, 403)
        encounter.refresh_from_db()
        self.assertEqual(encounter.clinical_summary, 'Author-only draft narrative.')

    def test_doctor_creates_consultation_from_patient_page_and_replay_deduplicates(self):
        self.login()
        url = reverse('portal:clinical-consultation-create', args=[self.patient.pk])
        response = self.client.get(url)
        token = response.context['clinical_context']
        data = self.consultation_data(token)
        first = self.client.post(url, data)
        encounter = ClinicalEncounter.objects.get()
        self.assertRedirects(first, reverse('portal:clinical-consultation-detail', args=[encounter.pk]), fetch_redirect_response=False)
        self.assertEqual((encounter.company_id, encounter.patient_id, encounter.clinician_id), (self.company.pk, self.patient.pk, self.doctor.pk))
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual((ClinicalEncounter.objects.count(), ClinicalTask.objects.count()), (1, 1))

    def test_sign_action_requires_confirmation_and_creates_immutable_snapshot(self):
        encounter = self.encounter()
        self.login()
        url = reverse('portal:clinical-consultation-detail', args=[encounter.pk])
        token = self.client.get(url).context['clinical_context']
        data = self.consultation_data(token, action='sign')
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Updated author narrative.', status_code=400)
        encounter.refresh_from_db()
        self.assertEqual(encounter.status, 'draft')
        self.assertFalse(ClinicalNote.objects.exists())
        response = self.client.post(url, {**data, 'confirm_signature': 'on'})
        self.assertEqual(response.status_code, 302)
        encounter.refresh_from_db()
        self.assertEqual(encounter.status, 'signed')
        self.assertEqual(encounter.signed_note.body, 'Updated author narrative.')
        self.assertEqual(encounter.signing_task.status, 'done')

    def test_stale_consultation_revision_preserves_the_newer_draft(self):
        encounter = self.encounter()
        self.login()
        url = reverse('portal:clinical-consultation-detail', args=[encounter.pk])
        token = self.client.get(url).context['clinical_context']
        self.assertEqual(self.client.post(url, self.consultation_data(token, summary='First tab latest draft.')).status_code, 302)
        response = self.client.post(url, self.consultation_data(token, summary='Stale second tab draft.'))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Stale second tab draft.', status_code=400)
        encounter.refresh_from_db()
        self.assertEqual(encounter.clinical_summary, 'First tab latest draft.')

    def test_missing_invalid_expired_and_other_user_context_cannot_create_consultations(self):
        self.login()
        url = reverse('portal:clinical-consultation-create', args=[self.patient.pk])
        for token in ('', 'invalid-token', self.token(actor=self.colleague)):
            with self.subTest(token=token[:20]):
                response = self.client.post(url, self.consultation_data(token))
                self.assertEqual(response.status_code, 400)
                self.assertFalse(ClinicalEncounter.objects.exists())
        token = self.token()
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() + 12 * 3600 + 1):
            response = self.client.post(url, self.consultation_data(token))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ClinicalEncounter.objects.exists())

    def test_stale_practice_or_different_record_context_cannot_write(self):
        first = self.encounter()
        second = self.encounter()
        self.login()
        token = self.token(record=first)
        response = self.client.post(reverse('portal:clinical-consultation-detail', args=[second.pk]), self.consultation_data(token))
        self.assertEqual(response.status_code, 400)
        second.refresh_from_db()
        self.assertEqual(second.clinical_summary, 'Author-only draft narrative.')
        create_token = self.token()
        self.client.post(reverse('portal:activate-company', args=[self.beta.slug]), {'next': reverse('portal:clinical-consultations')})
        response = self.client.post(reverse('portal:clinical-consultation-create', args=[self.beta_patient.pk]), self.consultation_data(create_token))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ClinicalEncounter.objects.filter(company=self.beta).exists())

    def test_clinical_lists_are_paginated_and_filters_do_not_broaden_scope(self):
        for number in range(21):
            self.encounter(summary=f'Paged consultation {number}')
        self.login()
        first = self.client.get(reverse('portal:clinical-consultations'), {'status': 'all'})
        second = self.client.get(reverse('portal:clinical-consultations'), {'status': 'all', 'page': 2})
        self.assertEqual(len(first.context['page_obj']), 20)
        self.assertEqual(len(second.context['page_obj']), 1)
        invalid = self.client.get(reverse('portal:clinical-consultations'), {'status': 'invented'})
        self.assertEqual(list(invalid.context['page_obj']), [])
        foreign_filter = self.client.get(reverse('portal:clinical-consultations'), {'patient': self.beta_patient.pk})
        self.assertEqual(list(foreign_filter.context['page_obj']), [])

    def test_lab_create_uses_patient_practice_and_duplicate_post_is_idempotent(self):
        self.login()
        url = reverse('portal:clinical-lab-create', args=[self.patient.pk])
        token = self.client.get(url).context['clinical_context']
        data = {'clinical_context': token, 'panel_name': 'Requested blood panel', 'due_on': ''}
        response = self.client.post(url, data)
        lab = LabRequest.objects.get()
        self.assertRedirects(response, reverse('portal:clinical-lab-detail', args=[lab.pk]), fetch_redirect_response=False)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(LabRequest.objects.count(), 1)
        self.assertEqual((lab.company_id, lab.patient_id, lab.requested_by_id), (self.company.pk, self.patient.pk, self.doctor.pk))

    def test_patient_uploads_owned_pdf_but_cannot_address_other_records(self):
        lab = self.lab()
        other = self.lab(patient=self.other_patient)
        beta = self.lab(company=self.beta, patient=self.beta_patient)
        self.login(self.patient_user)
        for record in (other, beta):
            self.assertEqual(self.client.get(reverse('portal:patient-lab-detail', args=[record.pk])).status_code, 404)
        url = reverse('portal:patient-lab-detail', args=[lab.pk])
        token = self.client.get(url).context['clinical_context']
        response = self.client.post(url, {
            'clinical_context': token, 'action': 'upload',
            'report': SimpleUploadedFile('patient-report.pdf', PDF, content_type='application/pdf'),
        })
        self.assertEqual(response.status_code, 302)
        result = LabResult.objects.get()
        self.assertEqual((result.company_id, result.patient_id, result.uploaded_by_id), (self.company.pk, self.patient.pk, self.patient_user.pk))

    def test_invalid_pdf_upload_keeps_error_on_patient_page_without_creating_result(self):
        lab = self.lab()
        self.login(self.patient_user)
        url = reverse('portal:patient-lab-detail', args=[lab.pk])
        token = self.client.get(url).context['clinical_context']
        response = self.client.post(url, {
            'clinical_context': token, 'action': 'upload',
            'report': SimpleUploadedFile('not-a-report.pdf', b'not a PDF', content_type='application/pdf'),
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LabResult.objects.exists())
        lab.refresh_from_db()
        self.assertEqual(lab.status, 'requested')

    def test_only_requestor_can_review_and_patient_never_sees_internal_review_text(self):
        lab = self.uploaded_lab()
        self.login(self.colleague)
        url = reverse('portal:clinical-lab-detail', args=[lab.pk])
        response = self.client.post(url, {'clinical_context': self.token(kind='lab', record=lab), 'action': 'review', 'review_note': 'Not my review'})
        self.assertEqual(response.status_code, 403)
        self.login()
        token = self.client.get(url).context['clinical_context']
        response = self.client.post(url, {'clinical_context': token, 'action': 'review', 'review_note': 'Private interpretation not for patient display.'})
        self.assertEqual(response.status_code, 302)
        lab.refresh_from_db()
        self.assertEqual(lab.status, 'reviewed')
        self.assertEqual(lab.review_task.status, 'done')
        self.login(self.patient_user)
        for route, args in (('patient-labs', []), ('patient-lab-detail', [lab.pk]), ('patient-dashboard', [])):
            response = self.client.get(reverse(f'portal:{route}', args=args))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'Private interpretation not for patient display.')
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_pdf_download_is_owned_attachment_with_private_security_headers(self):
        lab = self.uploaded_lab()
        for user, route in (
            (self.doctor, 'clinical-lab-result-download'), (self.colleague, 'clinical-lab-result-download'),
            (self.super_admin, 'clinical-lab-result-download'), (self.patient_user, 'patient-lab-result-download'),
        ):
            with self.subTest(user=user.email):
                self.login(user)
                response = self.client.get(reverse(f'portal:{route}', args=[lab.pk]))
                self.assertEqual(response.status_code, 200)
                content = b''.join(response.streaming_content) if response.streaming else response.content
                self.assertEqual(content, PDF)
                self.assertEqual(response.headers['Content-Type'], 'application/pdf')
                self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'))
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                self.assertIn('no-store', response.headers['Cache-Control'])
        self.login(self.other_user)
        self.assertEqual(self.client.get(reverse('portal:patient-lab-result-download', args=[lab.pk])).status_code, 404)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse('portal:clinical-lab-result-download', args=[lab.pk])).status_code, 403)

    def test_cross_practice_pdf_download_is_not_found_even_with_membership_in_both(self):
        foreign = self.uploaded_lab(company=self.beta, patient=self.beta_patient)
        self.login()
        self.assertEqual(self.client.get(reverse('portal:clinical-lab-result-download', args=[foreign.pk])).status_code, 404)
        self.login(self.patient_user)
        self.assertEqual(self.client.get(reverse('portal:patient-lab-result-download', args=[foreign.pk])).status_code, 404)

    def test_every_clinical_write_requires_csrf(self):
        encounter = self.encounter()
        lab = self.lab()
        browser = Client(enforce_csrf_checks=True)
        self.login(client=browser)
        for route, args, data in (
            ('clinical-consultation-create', [self.patient.pk], self.consultation_data(self.token())),
            ('clinical-consultation-detail', [encounter.pk], self.consultation_data(self.token(record=encounter))),
            ('clinical-lab-create', [self.patient.pk], {'clinical_context': self.token(kind='lab'), 'panel_name': 'Blocked'}),
            ('clinical-lab-detail', [lab.pk], {'clinical_context': self.token(kind='lab', record=lab), 'action': 'review', 'review_note': 'Blocked'}),
        ):
            with self.subTest(route=route):
                self.assertEqual(browser.post(reverse(f'portal:{route}', args=args), data).status_code, 403)
        self.login(self.patient_user, client=browser)
        self.assertEqual(browser.post(reverse('portal:patient-lab-detail', args=[lab.pk]), {
            'clinical_context': self.token(kind='lab', record=lab), 'action': 'upload',
            'report': SimpleUploadedFile('blocked.pdf', PDF, content_type='application/pdf'),
        }).status_code, 403)
        self.assertFalse(LabResult.objects.exists())

    def test_generic_task_completion_route_cannot_fake_a_clinical_signature(self):
        encounter = self.encounter()
        self.login(self.admin)
        self.client.post(reverse('portal:task-complete', args=[encounter.signing_task_id]), {})
        encounter.refresh_from_db()
        self.assertEqual(encounter.status, 'draft')
        self.assertEqual(encounter.signing_task.status, 'open')
        self.assertFalse(ClinicalNote.objects.exists())

    def test_signed_consultation_and_lab_internal_notes_are_absent_from_patient_pages(self):
        self.encounter(summary='Signed clinician narrative not for portal.', sign=True)
        lab = self.uploaded_lab()
        review_lab_request(lab_request=lab, actor=self.doctor, review_note='Internal laboratory interpretation.')
        self.login(self.patient_user)
        for route in ('patient-dashboard', 'patient-progress', 'patient-account', 'patient-labs'):
            with self.subTest(route=route):
                response = self.client.get(reverse(f'portal:{route}'))
                self.assertNotContains(response, 'Signed clinician narrative not for portal.')
                self.assertNotContains(response, 'Internal laboratory interpretation.')

    def test_generated_tasks_link_to_clinical_workflows_without_generic_edit_or_complete(self):
        encounter = self.encounter()
        lab = self.uploaded_lab()
        self.login()
        response = self.client.get(reverse('portal:staff-tasks'))
        for task, workflow_url in (
            (encounter.signing_task, reverse('portal:clinical-consultation-detail', args=[encounter.pk])),
            (lab.review_task, reverse('portal:clinical-lab-detail', args=[lab.pk])),
        ):
            self.assertContains(response, f'href="{workflow_url}"')
            self.assertNotContains(response, f'href="{reverse("portal:task-edit", args=[task.pk])}"')
            self.assertNotContains(response, f'action="{reverse("portal:task-complete", args=[task.pk])}"')
            self.assertRedirects(self.client.get(reverse('portal:task-edit', args=[task.pk])), workflow_url, fetch_redirect_response=False)
            rendered = next(item for item in response.context['tasks'] if item.pk == task.pk)
            self.assertFalse(rendered.is_actionable)

    def test_administrators_cannot_follow_draft_task_links_or_use_generic_editor(self):
        encounter = self.encounter()
        task_url = reverse('portal:task-edit', args=[encounter.signing_task_id])
        draft_url = reverse('portal:clinical-consultation-detail', args=[encounter.pk])
        for actor in (self.admin, self.super_admin):
            self.login(actor)
            response = self.client.get(reverse('portal:staff-tasks'))
            self.assertNotContains(response, f'href="{draft_url}"')
            self.assertNotContains(response, encounter.clinical_summary)
            self.assertEqual(self.client.get(task_url).status_code, 403)
        for actor in (self.doctor, self.admin, self.super_admin):
            self.login(actor)
            self.assertEqual(self.client.post(task_url, {
                'title': 'Generic bypass', 'description': 'Generic edit', 'status': 'done',
                'priority': 'normal', 'patient': self.patient.pk, 'assigned_to': actor.pk,
            }).status_code, 403)
        encounter.refresh_from_db()
        self.assertEqual((encounter.status, encounter.signing_task.status), ('draft', 'open'))

    def test_other_doctors_patient_record_does_not_reveal_private_draft_task_links(self):
        encounter = self.encounter(summary='Private unsaved assessment details.')
        self.login(self.colleague)
        response = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'tasks'})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Private unsaved assessment details.')
        self.assertNotContains(response, f'href="{reverse("portal:clinical-consultation-detail", args=[encounter.pk])}"')
        task = next(item for item in response.context['tasks'] if item.pk == encounter.signing_task_id)
        self.assertTrue(task.is_clinical_workflow)
        self.assertEqual(task.workflow_url, '')

    def test_staff_practice_switch_preserves_clinical_sections_without_old_record_ids(self):
        encounter = self.encounter()
        lab = self.lab()
        for route, detail in (
            ('clinical-consultations', reverse('portal:clinical-consultation-detail', args=[encounter.pk])),
            ('clinical-labs', reverse('portal:clinical-lab-detail', args=[lab.pk])),
        ):
            for previous in (reverse(f'portal:{route}'), detail):
                with self.subTest(previous=previous):
                    self.login()
                    response = self.client.post(reverse('portal:activate-company', args=[self.beta.slug]), {
                        'next': f'{previous}?patient={self.patient.pk}&status=draft&page=3#old-record',
                    })
                    self.assertRedirects(response, reverse(f'portal:{route}'), fetch_redirect_response=False)
                    self.assertEqual(self.client.session[ACTIVE_COMPANY_SESSION_KEY], self.beta.pk)

    def test_switch_to_practice_admin_role_does_not_redirect_to_inaccessible_clinical_pages(self):
        CompanyMembership.objects.filter(company=self.beta, user=self.doctor).update(role='practice_admin')
        for route in ('clinical-consultations', 'clinical-labs'):
            self.login()
            response = self.client.post(reverse('portal:activate-company', args=[self.beta.slug]), {
                'next': f'{reverse(f"portal:{route}")}?patient={self.patient.pk}',
            })
            self.assertRedirects(response, reverse('portal:desktop-dashboard'), fetch_redirect_response=False)
            self.assertEqual(self.client.get(reverse(f'portal:{route}')).status_code, 403)

    def test_patient_practice_switch_returns_to_lab_list_without_old_record_ids(self):
        lab = self.lab()
        for previous in (reverse('portal:patient-labs'), reverse('portal:patient-lab-detail', args=[lab.pk]),
                         reverse('portal:patient-lab-result-download', args=[lab.pk])):
            with self.subTest(previous=previous):
                self.login(self.patient_user)
                response = self.client.post(reverse('portal:activate-patient-company', args=[self.beta.slug]), {
                    'next': f'{previous}?status=uploaded&page=4#old-report',
                })
                self.assertRedirects(response, reverse('portal:patient-labs'), fetch_redirect_response=False)
                self.assertEqual(self.client.get(reverse('portal:patient-lab-detail', args=[lab.pk])).status_code, 404)

    def test_head_private_pdf_has_no_body_and_does_not_write_download_audit(self):
        lab = self.uploaded_lab()
        for actor, route in ((self.doctor, 'clinical-lab-result-download'), (self.patient_user, 'patient-lab-result-download')):
            with self.subTest(actor=actor.email):
                self.login(actor)
                before = AuditEvent.objects.count()
                response = self.client.head(reverse(f'portal:{route}', args=[lab.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b'')
                self.assertEqual(response.headers['Content-Length'], str(len(PDF)))
                self.assertIn('no-store', response.headers['Cache-Control'])
                self.assertEqual(AuditEvent.objects.count(), before)

    def test_patient_cannot_forge_a_review_action_even_with_valid_owned_context(self):
        lab = self.uploaded_lab()
        self.login(self.patient_user)
        url = reverse('portal:patient-lab-detail', args=[lab.pk])
        token = self.client.get(url).context['clinical_context']
        before = AuditEvent.objects.count()
        response = self.client.post(url, {
            'clinical_context': token, 'action': 'review', 'review_note': 'Forged patient clinical review.',
            'report': SimpleUploadedFile('owned-report.pdf', PDF, content_type='application/pdf'),
        })
        self.assertEqual(response.status_code, 403)
        lab.refresh_from_db()
        self.assertEqual((lab.status, lab.result_summary, lab.review_task.status), ('uploaded', '', 'open'))
        self.assertIsNone(lab.reviewed_at)
        self.assertEqual(AuditEvent.objects.count(), before)
