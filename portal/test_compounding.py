from datetime import timedelta
import json
import os
import subprocess
from unittest import skipUnless
from unittest.mock import patch

from django.test import Client
from django.conf import settings
from django.urls import reverse

from care.compounding import mark_compounding_submitted, update_compounding_draft
from care.models import AuditEvent, AuthorizationReviewReminder, CompoundingRecord, TreatmentAuthorization
from care.test_compounding import CompoundingFixture
from practices.services import ACTIVE_COMPANY_SESSION_KEY


class CompoundingPageTests(CompoundingFixture):
    def login(self, user=None, company=None, client=None):
        client = client or self.client
        client.force_login(user or self.doctor)
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def url(self, name, *args):
        return reverse(f'portal:{name}', args=args)

    def test_create_review_submit_are_distinct_explicit_signed_actions(self):
        self.login()
        create_url = self.url('compounding-create', self.auth.pk)
        form = self.client.get(create_url)
        self.assertContains(form, 'NOT A PRESCRIPTION')
        rejected = self.client.post(create_url, {'preparation_note': 'My draft'})
        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(CompoundingRecord.objects.exists())
        response = self.client.post(create_url, {'preparation_note': 'My draft', 'clinical_context': form.context['clinical_context']})
        self.assertEqual(response.status_code, 302)
        record = CompoundingRecord.objects.get()
        detail_url = self.url('compounding-detail', record.pk)
        detail = self.client.get(detail_url)
        self.assertContains(detail, 'Save draft note')
        self.assertNotContains(detail, 'Record completed manual submission')
        response = self.client.post(detail_url, {'action': 'review', 'confirm': 'on', 'clinical_context': detail.context['clinical_context']})
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(detail_url)
        self.assertContains(detail, 'Record completed manual submission')
        self.assertNotContains(detail, 'Save draft note')
        data = {'action': 'submit', 'confirm': 'on', 'external_reference': 'EXTERNAL-MANUAL-1', 'clinical_context': detail.context['clinical_context']}
        self.assertEqual(self.client.post(detail_url, data).status_code, 302)
        count = AuditEvent.objects.count()
        self.assertEqual(self.client.post(detail_url, data).status_code, 302)
        self.assertEqual(AuditEvent.objects.count(), count)
        record.refresh_from_db()
        self.assertEqual(record.status, 'submitted')

    def test_doctor_drafts_private_super_reads_only_finalized_and_admin_patient_denied(self):
        record = self.record()
        url = self.url('compounding-detail', record.pk)
        for actor, expected in ((self.colleague, 404), (self.super_admin, 404), (self.admin, 403), (self.patient_user, 403)):
            self.login(actor)
            self.assertEqual(self.client.get(url).status_code, expected)
        self.review_record(record)
        self.login(self.super_admin)
        response = self.client.get(url)
        self.assertContains(response, 'Read-only finalized summary')
        self.assertNotContains(response, 'name="clinical_context"')
        self.assertEqual(self.client.post(url, {'action': 'submit', 'confirm': 'on'}).status_code, 403)

    def test_patient_practice_switch_and_foreign_authorization_are_fail_closed(self):
        record = self.record()
        self.login()
        response = self.client.get(self.url('compounding-detail', record.pk))
        token = response.context['clinical_context']
        self.login(self.doctor, self.beta)
        response = self.client.post(self.url('compounding-detail', record.pk), {'action': 'review', 'confirm': 'on', 'clinical_context': token})
        self.assertEqual(response.status_code, 404)
        record.refresh_from_db()
        self.assertEqual(record.status, 'draft')

    def test_stale_note_and_expired_token_do_not_overwrite(self):
        record = self.record()
        self.login()
        url = self.url('compounding-detail', record.pk)
        old = self.client.get(url).context['clinical_context']
        updated = update_compounding_draft(record=record, actor=self.doctor, expected_revision=record.revision, preparation_note='Newer note')
        response = self.client.post(url, {'action': 'save', 'preparation_note': 'Stale note', 'clinical_context': old})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['draft_form'].non_field_errors())
        updated.refresh_from_db()
        self.assertEqual(updated.preparation_note, 'Newer note')
        with patch('django.core.signing.time.time', return_value=1):
            old = self.client.get(url).context['clinical_context']
        self.assertEqual(self.client.post(url, {'action': 'review', 'confirm': 'on', 'clinical_context': old}).status_code, 400)

    def test_print_is_doctor_only_marks_summary_not_prescription_and_head_has_no_audit(self):
        record = self.review_record()
        self.login()
        url = self.url('compounding-print', record.pk)
        before = AuditEvent.objects.count()
        self.assertEqual(self.client.head(url).status_code, 200)
        self.assertEqual(AuditEvent.objects.count(), before)
        response = self.client.get(url)
        self.assertContains(response, 'NOT A PRESCRIPTION')
        self.assertContains(response, 'not valid for dispensing')
        self.assertEqual(AuditEvent.objects.count(), before + 1)
        self.assertIn('no-store', response['Cache-Control'])
        self.login(self.super_admin)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_csrf_and_explicit_review_confirmation_are_required(self):
        record = self.record()
        self.login()
        url = self.url('compounding-detail', record.pk)
        token = self.client.get(url).context['clinical_context']
        self.assertEqual(self.client.post(url, {'action': 'review', 'clinical_context': token}).status_code, 400)
        secure = Client(enforce_csrf_checks=True)
        self.login(client=secure)
        self.assertEqual(secure.post(url, {'action': 'review', 'confirm': 'on', 'clinical_context': token}).status_code, 403)
        record.refresh_from_db()
        self.assertEqual(record.status, 'draft')

    def test_list_counts_and_history_are_real_and_paginated(self):
        for _ in range(23):
            self.record()
        self.login()
        response = self.client.get(self.url('compounding-list'))
        self.assertEqual(response.context['metrics']['awaiting'], 23)
        self.assertEqual(len(response.context['page_obj']), 20)
        self.assertNotContains(response, 'Email me the batch')

    def test_review_run_requires_super_admin_confirmation_and_scoped_token(self):
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(expires_on=self.today + timedelta(days=5))
        url = self.url('treatment-review-run')
        self.login(self.doctor)
        self.assertEqual(self.client.post(url, {'within_days': 30, 'confirm': 'on'}).status_code, 403)
        self.login(self.super_admin)
        page = self.client.get(self.url('treatment-review-rules'))
        self.assertEqual(page.context['due_page'].paginator.count, 1)
        self.assertFalse(AuthorizationReviewReminder.objects.exists())
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, {'within_days': 30, 'confirm': 'on'}).status_code, 400)
        values = {'within_days': 30, 'confirm': 'on', 'review_context': page.context['run_context']}
        self.assertEqual(self.client.post(url, values).status_code, 302)
        self.assertEqual(self.client.post(url, values).status_code, 302)
        self.assertEqual(AuthorizationReviewReminder.objects.count(), 1)

    @skipUnless(os.environ.get('MERIDIAN_PLAYWRIGHT_PATH'), 'Optional local browser runner is not configured')
    def test_compounding_and_review_layouts_at_desktop_and_mobile_widths(self):
        record = self.record()
        self.login()
        pages = []
        for name, url in (
            ('compounding-list', self.url('compounding-list')),
            ('compounding-create', self.url('compounding-create', self.auth.pk)),
            ('compounding-draft', self.url('compounding-detail', record.pk)),
            ('compounding-print', self.url('compounding-print', record.pk)),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            pages.append(dict(name=name, html=response.content.decode()))
        self.review_record(record)
        response = self.client.get(self.url('compounding-detail', record.pk))
        pages.append(dict(name='compounding-ready', html=response.content.decode()))
        self.login(self.super_admin)
        response = self.client.get(self.url('treatment-review-rules'))
        self.assertEqual(response.status_code, 200)
        pages.append(dict(name='review-rule-check', html=response.content.decode()))
        result = subprocess.run(['node', str(settings.BASE_DIR / 'scripts' / 'operations_layout_smoke.cjs')],
                                input=json.dumps(pages), text=True, capture_output=True, timeout=90, cwd=settings.BASE_DIR)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['checked'], 18)
